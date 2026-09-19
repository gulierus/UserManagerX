"""
End-to-end integration tests: :mod:`main`, :mod:`ui.operations_tab` and
:mod:`ui.source_selection_tab`.

What is covered
---------------
* ``EqualWidthTabBar`` - the tab bar that gives every tab one shared width.
* The whole ``StudentManagementSystem`` window: five tabs, each of them
  rendering, one shared ``SourceManager``, theme handling and shutdown.
* ``OperationsTab`` - the source combo, the automatic refresh driven by the
  ``SourceManager`` signals and the propagation of the selected source to the
  three operation widgets.
* ``SourceSelectionTab`` - the stacked configuration pages and the load button
  of the EduPage page (which must look exactly like the Encrypted File one).
* A realistic school-year rollover: two read-only sources with mixed
  Roman/Arabic class names -> analyse -> fix -> shift -> merge -> generate
  credentials -> serialise and back, asserting the data after every step and
  that the two read-only inputs are never modified.
* A regression guard against the hard dependency that used to stop the whole
  application from starting: no project module may import ``ldap3`` or
  ``PyQt6.QtWebEngineWidgets`` at module level.

House rules
-----------
Nothing here opens a network connection.  Every test that can reach a modal
dialog requests the ``dialogs`` fixture, the settings file and the logging
configuration are redirected into ``tmp_path``, and the application-wide Qt
palette/style plus the root logger handlers are restored after every test that
builds the main window.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect that is still present in the production code.
"""

import ast
import json
import logging
import unicodedata
from pathlib import Path

import pytest

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QCloseEvent, QPalette
from PyQt6.QtWidgets import (
    QApplication, QStyleFactory, QTabBar, QTabWidget, QWidget,
)

from models import Class, Person, Source, SourceManager
from utils.source_analysis import analyze_merge, analyze_source, apply_fix
from utils.source_serialization import source_to_dict, source_from_dict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def build_source(name, spec, source_type="manual", readonly=False):
    """``build_source("S", [("6.A", [("Jan", "Novák")])])`` -> a whole source."""
    source = Source(name=name, source_type=source_type, readonly=readonly)
    for class_name, people in spec:
        cls = Class(name=class_name)
        for first, last in people:
            cls.add_person(Person(first, last, class_name))
        source.add_class(cls)
    return source


def snapshot(source):
    """Everything about a source a read-only guarantee has to preserve."""
    return (
        source.name,
        source.source_type,
        source.readonly,
        [
            (
                cls.name,
                [
                    (p.first_name, p.last_name, p.class_name,
                     p.ad_username, p.ad_password, p.ad_display_name)
                    for p in cls.persons
                ],
            )
            for cls in source.classes
        ],
    )


def dispose(qapp, *widgets):
    """Destroy Qt widgets for real: deferred deletion needs an explicit flush."""
    for widget in widgets:
        widget.close()
        widget.setParent(None)
        widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def combo_items(combo):
    """
    The sources a combo box offers, in order.

    Source combos show the access mode next to the name ("Roster (editable)")
    and keep the plain source name in the item data, so this returns the data
    where there is one and the visible text otherwise - which is what the
    placeholder row and the non-source combos need.  The decoration itself is
    covered by tests/test_source_combo.py.
    """
    names = []
    for index in range(combo.count()):
        data = combo.itemData(index)
        names.append(data if isinstance(data, str) and data else combo.itemText(index))
    return names


def combo_labels(combo):
    """The visible texts of a QComboBox, in order."""
    return [combo.itemText(i) for i in range(combo.count())]


def select_source(combo, name):
    """
    Select a source in a source combo box by its name.

    ``setCurrentText`` cannot be used any more: the visible text carries the
    access mode, so the plain name matches no row.
    """
    from ui.source_combo import find_source_index
    index = find_source_index(combo, name)
    assert index >= 0, f"{name!r} is not in the combo: {combo_labels(combo)}"
    combo.setCurrentIndex(index)


def selected_source(combo):
    """The name of the source a combo box currently shows."""
    from ui.source_combo import combo_source_name
    return combo_source_name(combo)


def list_items(widget):
    """The visible texts of a QListWidget, in order."""
    return [widget.item(i).text() for i in range(widget.count())]


def is_ascii_lower_alnum(text):
    """True when *text* is a plain lowercase ASCII sAMAccountName."""
    return bool(text) and all(c.isdigit() or ("a" <= c <= "z") for c in text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def main_module(qapp):
    """
    Import ``main`` without letting its module-level logging setup run.

    ``main.py`` calls ``setup_application_logging()`` while it is being
    imported, which would create a ``logs/`` directory next to the sources.
    Replacing the function *before* the import keeps the test run free of side
    effects outside ``tmp_path``.
    """
    import utils.logging_config as lc

    original = lc.setup_application_logging
    lc.setup_application_logging = lambda: None
    try:
        import main as main_mod
    finally:
        lc.setup_application_logging = original
    return main_mod


@pytest.fixture
def qt_globals_restored(qapp):
    """Undo the application-wide palette and style the main window installs."""
    palette = QApplication.palette()
    style_name = QApplication.style().objectName()
    yield
    QApplication.setPalette(palette)
    restored = QStyleFactory.create(style_name)
    if restored is not None:
        QApplication.setStyle(restored)


@pytest.fixture
def root_logger_clean():
    """Remove every logging handler a test attached to the root logger."""
    before = list(logging.getLogger().handlers)
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)


@pytest.fixture
def build_window(main_module, qapp, dialogs, isolated_settings,
                 isolated_logging_config, qt_globals_restored, root_logger_clean):
    """Factory that builds main windows and disposes of them afterwards."""
    created = []

    def _build():
        window = main_module.StudentManagementSystem()
        created.append(window)
        return window

    yield _build

    dispose(qapp, *created)


@pytest.fixture
def window(build_window):
    """A fully built :class:`main.StudentManagementSystem`."""
    return build_window()


@pytest.fixture
def tab_bar_widget(main_module, qapp):
    """A bare ``QTabWidget`` wired up exactly like the application's."""
    widget = QTabWidget()
    widget.setTabBar(main_module.EqualWidthTabBar(widget))
    widget.setElideMode(Qt.TextElideMode.ElideNone)
    widget.setUsesScrollButtons(False)
    yield widget
    dispose(qapp, widget)


@pytest.fixture
def ops_tab(qapp, dialogs, isolated_settings, source_manager):
    """A standalone :class:`ui.operations_tab.OperationsTab`."""
    from ui.operations_tab import OperationsTab

    tab = OperationsTab(source_manager)
    yield tab
    dispose(qapp, tab)


@pytest.fixture
def selection_tab(qapp, dialogs, isolated_settings, source_manager):
    """A standalone :class:`ui.source_selection_tab.SourceSelectionTab`."""
    from ui.source_selection_tab import SourceSelectionTab

    tab = SourceSelectionTab(source_manager)
    yield tab
    dispose(qapp, tab)


@pytest.fixture
def rollover_sources():
    """
    The two read-only inputs of the realistic workflow.

    ``EduPage-2024`` uses Roman class names and still contains the graduating
    year; ``Backup-2025`` is an encrypted-file export of the following year
    written with Arabic numerals.
    """
    edupage = build_source(
        "EduPage-2024",
        [
            ("VI.A", [("Jan", "Novák"), ("Šárka", "Nováková")]),
            ("VI.B", [("Petr", "Černý")]),
            ("IX.A", [("Lucie", "Dvořáková")]),
        ],
        source_type="edupage",
        readonly=True,
    )
    backup = build_source(
        "Backup-2025",
        [
            ("7.A", [("Jan", "Novák"), ("Šárka", "Nováková")]),
            ("7.B", [("Petr", "Černý"), ("Eva", "Malá")]),
        ],
        source_type="file",
        readonly=True,
    )
    return edupage, backup


# ===========================================================================
# EqualWidthTabBar
# ===========================================================================

@pytest.mark.gui
class TestEqualWidthTabBar:
    """Every tab must be exactly as wide as the widest one."""

    LABELS = ["1. Data Sources", "2. Comparison and Sync", "3. Operations",
              "4. Settings", "5. Logs"]

    def _fill(self, widget, labels=None):
        for label in (labels if labels is not None else self.LABELS):
            widget.addTab(QWidget(), label)
        return widget.tabBar()

    def test_every_tab_reports_the_same_size_hint(self, tab_bar_widget):
        """Five labels of very different lengths yield one identical hint."""
        bar = self._fill(tab_bar_widget)
        hints = [bar.tabSizeHint(i) for i in range(bar.count())]
        assert len({(h.width(), h.height()) for h in hints}) == 1

    def test_the_shared_width_is_the_widest_natural_label_plus_the_padding(
            self, tab_bar_widget, main_module):
        """The shared width is max(natural widths) + PADDING, nothing else."""
        bar = self._fill(tab_bar_widget)
        natural = [QTabBar.tabSizeHint(bar, i).width() for i in range(bar.count())]
        expected = max(natural) + main_module.EqualWidthTabBar.PADDING
        assert [bar.tabSizeHint(i).width() for i in range(bar.count())] == \
            [expected] * bar.count()

    def test_the_shared_height_is_the_tallest_natural_height(self, tab_bar_widget):
        """The height is the tallest natural height and carries no padding."""
        bar = self._fill(tab_bar_widget)
        natural = [QTabBar.tabSizeHint(bar, i).height() for i in range(bar.count())]
        assert [bar.tabSizeHint(i).height() for i in range(bar.count())] == \
            [max(natural)] * bar.count()

    def test_laid_out_tabs_all_occupy_the_same_width(self, tab_bar_widget, qapp):
        """The hint really reaches the layout: the drawn rectangles match."""
        bar = self._fill(tab_bar_widget)
        tab_bar_widget.show()
        qapp.processEvents()
        widths = {bar.tabRect(i).width() for i in range(bar.count())}
        assert len(widths) == 1
        assert widths.pop() > 0

    def test_renaming_one_tab_to_a_longer_label_widens_every_tab(
            self, tab_bar_widget, main_module):
        """A longer label on one tab grows all of them, not only that one."""
        bar = self._fill(tab_bar_widget)
        before = bar.tabSizeHint(0).width()

        tab_bar_widget.setTabText(4, "5. Logs, diagnostics and troubleshooting")
        after = [bar.tabSizeHint(i).width() for i in range(bar.count())]

        assert len(set(after)) == 1
        assert after[0] > before
        natural = [QTabBar.tabSizeHint(bar, i).width() for i in range(bar.count())]
        assert after[0] == max(natural) + main_module.EqualWidthTabBar.PADDING

    def test_removing_the_widest_tab_shrinks_every_remaining_tab(self, tab_bar_widget):
        """The width is recomputed live, so it also goes back down."""
        bar = self._fill(tab_bar_widget)
        wide = max(range(bar.count()),
                   key=lambda i: QTabBar.tabSizeHint(bar, i).width())
        before = bar.tabSizeHint(0).width()

        tab_bar_widget.removeTab(wide)
        after = [bar.tabSizeHint(i).width() for i in range(bar.count())]

        assert len(set(after)) == 1
        assert after[0] < before

    def test_a_lone_tab_gets_its_own_width_plus_the_padding(
            self, tab_bar_widget, main_module):
        """With a single tab the maximum is the tab itself."""
        bar = self._fill(tab_bar_widget, ["4. Settings"])
        natural = QTabBar.tabSizeHint(bar, 0).width()
        assert bar.tabSizeHint(0).width() == \
            natural + main_module.EqualWidthTabBar.PADDING

    def test_the_size_hint_is_stable_across_repeated_calls(self, tab_bar_widget):
        """Asking twice must not accumulate the padding."""
        bar = self._fill(tab_bar_widget)
        first = [bar.tabSizeHint(i).width() for i in range(bar.count())]
        second = [bar.tabSizeHint(i).width() for i in range(bar.count())]
        third = [bar.tabSizeHint(i).width() for i in range(bar.count())]
        assert first == second == third

    def test_identical_labels_get_the_padding_exactly_once(
            self, tab_bar_widget, main_module):
        """Two equal labels: the hint is the natural width plus one padding."""
        bar = self._fill(tab_bar_widget, ["Same", "Same"])
        natural = QTabBar.tabSizeHint(bar, 0).width()
        padding = main_module.EqualWidthTabBar.PADDING
        assert bar.tabSizeHint(0).width() == natural + padding
        assert bar.tabSizeHint(1).width() == natural + padding

    def test_no_label_is_ever_squeezed_below_its_own_natural_width(
            self, tab_bar_widget):
        """Equalising must widen the narrow tabs, never narrow the wide one."""
        bar = self._fill(tab_bar_widget)
        for index in range(bar.count()):
            assert bar.tabSizeHint(index).width() >= \
                QTabBar.tabSizeHint(bar, index).width()

    def test_labels_with_diacritics_take_part_in_the_maximum(
            self, tab_bar_widget, main_module):
        """A non-ASCII caption is measured like every other one."""
        bar = self._fill(tab_bar_widget,
                         ["Logy", "Nastavení a předvolby aplikace UserManagerX"])
        natural = [QTabBar.tabSizeHint(bar, i).width() for i in range(2)]
        expected = max(natural) + main_module.EqualWidthTabBar.PADDING
        assert natural[1] > natural[0]
        assert bar.tabSizeHint(0).width() == expected
        assert bar.tabSizeHint(1).width() == expected


# ===========================================================================
# The main window
# ===========================================================================

EXPECTED_TABS = [
    (0, "1. Data Sources", "source_selection_tab", "SourceSelectionTab"),
    (1, "2. Comparison and Sync", "comparison_tab", "ComparisonTab"),
    (2, "3. Operations", "operations_tab", "OperationsTab"),
    (3, "4. Settings", "settings_tab", "SettingsTab"),
    (4, "5. Logs", "log_viewer_tab", "LogViewerTab"),
    (5, "6. Source Manager", "source_manager_tab", "SourceManagerTab"),
]


@pytest.mark.gui
@pytest.mark.integration
class TestMainWindow:
    """The whole application window, built for real."""

    def test_the_window_builds_the_documented_tabs_in_order(self, window):
        """Every documented tab exists and carries its numbered caption."""
        bar = window.tab_widget
        assert bar.count() == len(EXPECTED_TABS)
        assert [bar.tabText(i) for i in range(bar.count())] == \
            [label for _, label, _, _ in EXPECTED_TABS]

    @pytest.mark.parametrize("index,label,attribute,class_name", EXPECTED_TABS)
    def test_each_tab_is_the_expected_widget(self, window, index, label,
                                             attribute, class_name):
        """Tab N is the widget the window also stores under its own name."""
        widget = window.tab_widget.widget(index)
        assert widget is getattr(window, attribute)
        assert type(widget).__name__ == class_name

    @pytest.mark.parametrize("index,label,attribute,class_name", EXPECTED_TABS)
    def test_each_tab_renders_when_it_is_selected(self, window, qapp, index,
                                                  label, attribute, class_name):
        """Selecting a tab really lays it out - it becomes visible and sized."""
        window.show()
        window.tab_widget.setCurrentIndex(index)
        qapp.processEvents()

        widget = window.tab_widget.currentWidget()
        assert widget is getattr(window, attribute)
        assert widget.isVisible()
        assert widget.width() > 0 and widget.height() > 0
        assert widget.layout() is not None

    def test_the_window_uses_the_equal_width_tab_bar(self, window, main_module, qapp):
        """The window installs EqualWidthTabBar and all five tabs match."""
        bar = window.tab_widget.tabBar()
        assert isinstance(bar, main_module.EqualWidthTabBar)

        window.show()
        qapp.processEvents()
        assert len({bar.tabRect(i).width() for i in range(5)}) == 1

    def test_the_tab_bar_neither_elides_nor_scrolls(self, window):
        """Equal widths only work when Qt is told not to shrink the labels."""
        assert window.tab_widget.elideMode() == Qt.TextElideMode.ElideNone
        assert window.tab_widget.usesScrollButtons() is False

    def test_the_window_announces_itself_and_reserves_room_for_the_tabs(self, window):
        """Title and minimum size are the documented ones."""
        assert window.windowTitle() == "Student Management System"
        assert window.minimumWidth() == 1200
        assert window.minimumHeight() == 800

    def test_a_tab_added_later_is_equalised_as_well(self, window, qapp):
        """The bar keeps its promise for tabs the window did not create."""
        before = window.tab_widget.count()
        window.tab_widget.addTab(QWidget(), "A much longer extra caption")
        window.show()
        qapp.processEvents()

        bar = window.tab_widget.tabBar()
        assert bar.count() == before + 1
        assert len({bar.tabRect(i).width() for i in range(bar.count())}) == 1

    def test_the_window_starts_without_any_source(self, window):
        """Nothing is loaded until the user loads it on the first tab."""
        assert window.source_manager.get_source_names() == []
        assert combo_items(window.operations_tab.source_combo) == ["(Select Source)"]
        assert window.operations_tab.current_source is None

    def test_all_data_tabs_share_one_source_manager(self, window):
        """One SourceManager drives every tab, so nothing can drift apart."""
        manager = window.source_manager
        assert isinstance(manager, SourceManager)
        assert window.source_selection_tab.source_manager is manager
        assert window.comparison_tab.source_manager is manager
        assert window.operations_tab.source_manager is manager
        assert window.operations_tab.ad_widget.source_manager is manager
        assert window.operations_tab.pdf_widget.source_manager is manager
        assert window.operations_tab.json_widget.source_manager is manager

    def test_a_new_source_reaches_every_tab_that_lists_sources(self, window):
        """Adding a source updates the operations tab and all three panels."""
        source = build_source("Roster", [("6.A", [("Jan", "Novák")])])
        window.source_manager.add_source(source)

        assert "Roster" in combo_items(window.operations_tab.source_combo)
        for panel in (window.comparison_tab.left_panel,
                      window.comparison_tab.right_panel,
                      window.comparison_tab.output_panel):
            assert "Roster" in combo_items(panel.source_combo)

    def test_a_failing_tab_constructor_reports_a_critical_dialog_and_reraises(
            self, main_module, monkeypatch, dialogs, isolated_settings,
            isolated_logging_config, qt_globals_restored, root_logger_clean):
        """A broken tab must not fail silently - the user is told and it raises."""
        class Boom:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("settings tab exploded")

        monkeypatch.setattr(main_module, "SettingsTab", Boom)

        with pytest.raises(RuntimeError, match="settings tab exploded"):
            main_module.StudentManagementSystem()

        assert "critical" in dialogs.kinds()
        assert dialogs.saw("Failed to create application tabs")
        assert dialogs.saw("settings tab exploded")

    def test_close_event_cleans_up_the_font_manager(self, window, main_module,
                                                    monkeypatch):
        """Closing the window releases the temporary font files."""
        calls = []

        class FakeFontManager:
            def cleanup(self):
                calls.append("cleanup")

        monkeypatch.setattr(main_module, "get_font_manager",
                            lambda: FakeFontManager())

        event = QCloseEvent()
        window.closeEvent(event)

        assert calls == ["cleanup"]
        assert event.isAccepted()

    def test_close_event_still_accepts_when_the_font_cleanup_fails(
            self, window, main_module, monkeypatch):
        """A broken font manager must never trap the user in the application."""
        def explode():
            raise RuntimeError("no font manager")

        monkeypatch.setattr(main_module, "get_font_manager", explode)

        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()

    def test_the_settings_tab_shows_the_stored_theme(self, build_window,
                                                     isolated_settings):
        """The stored theme is what the Settings tab preselects."""
        isolated_settings.set("theme", "Blue", category="general")
        window = build_window()
        assert window.settings_tab.theme_combo.currentText() == "Blue"

    @pytest.mark.bug
    def test_the_saved_theme_is_applied_when_the_window_opens(
            self, build_window, isolated_settings):
        """A user who saved 'Blue' must see the blue palette after a restart."""
        from ui.settings_tab import SettingsTab

        isolated_settings.set("theme", "Blue", category="general")
        build_window()

        expected = SettingsTab.THEMES["Blue"]["window"]
        assert QApplication.palette().color(QPalette.ColorRole.Window) == expected

    @pytest.mark.bug
    def test_closing_the_window_detaches_the_log_viewer_log_handler(
            self, window, qapp):
        """The log viewer's root-logger handler must not outlive the window."""
        from ui.log_viewer_tab import RealTimeLogHandler

        root = logging.getLogger()
        assert any(isinstance(h, RealTimeLogHandler) for h in root.handlers)

        window.close()
        qapp.processEvents()

        assert not any(isinstance(h, RealTimeLogHandler) for h in root.handlers)


# ===========================================================================
# OperationsTab
# ===========================================================================

@pytest.mark.gui
class TestOperationsTabSourceSelection:
    """The combo, the automatic refresh and the propagation of the source."""

    def test_the_source_combo_starts_with_only_the_placeholder(self, ops_tab):
        """An empty SourceManager leaves nothing but the placeholder entry."""
        assert combo_items(ops_tab.source_combo) == ["(Select Source)"]
        assert ops_tab.current_source is None

    def test_selecting_a_source_propagates_it_to_all_three_widgets(
            self, ops_tab, source_manager):
        """One selection has to reach AD management, PDF export and JSON export."""
        source = build_source(
            "Roster",
            [("6.A", [("Jan", "Novák"), ("Eva", "Malá")]), ("6.B", [("Petr", "Černý")])],
        )
        source_manager.add_source(source)

        select_source(ops_tab.source_combo, "Roster")

        assert ops_tab.current_source is source
        assert ops_tab.ad_widget.current_source is source
        assert ops_tab.pdf_widget.current_source is source
        assert ops_tab.json_widget.current_source is source
        assert list_items(ops_tab.pdf_widget.class_list) == ["6.A", "6.B"]
        assert ops_tab.ad_widget.person_table.rowCount() == 3
        assert "Roster" in ops_tab.json_widget.source_info_label.text()
        assert "Students: 3" in ops_tab.json_widget.source_info_label.text()
        assert ops_tab.json_widget.export_btn.isEnabled()

    def test_selecting_the_placeholder_clears_all_three_widgets(
            self, ops_tab, source_manager):
        """Going back to '(Select Source)' must disarm every operation widget."""
        source_manager.add_source(build_source("Roster", [("6.A", [("Jan", "Novák")])]))
        select_source(ops_tab.source_combo, "Roster")

        select_source(ops_tab.source_combo, "(Select Source)")

        assert ops_tab.current_source is None
        assert ops_tab.ad_widget.current_source is None
        assert ops_tab.pdf_widget.current_source is None
        assert ops_tab.json_widget.current_source is None
        assert ops_tab.pdf_widget.class_list.count() == 0
        assert ops_tab.ad_widget.person_table.rowCount() == 0
        assert not ops_tab.json_widget.export_btn.isEnabled()
        assert "No source selected" in ops_tab.json_widget.source_info_label.text()

    def test_switching_between_two_sources_propagates_the_new_one(
            self, ops_tab, source_manager):
        """The widgets follow the combo, they do not keep the first choice."""
        first = build_source("First", [("6.A", [("Jan", "Novák")])])
        second = build_source("Second", [("7.B", [("Eva", "Malá")]),
                                         ("7.C", [("Petr", "Černý")])])
        source_manager.add_source(first)
        source_manager.add_source(second)

        select_source(ops_tab.source_combo, "First")
        select_source(ops_tab.source_combo, "Second")

        assert ops_tab.current_source is second
        assert ops_tab.pdf_widget.current_source is second
        assert list_items(ops_tab.pdf_widget.class_list) == ["7.B", "7.C"]

    def test_selecting_the_same_source_twice_is_idempotent(
            self, ops_tab, source_manager):
        """Re-selecting must not duplicate rows in the operation widgets."""
        source_manager.add_source(
            build_source("Roster", [("6.A", [("Jan", "Novák"), ("Eva", "Malá")])])
        )
        select_source(ops_tab.source_combo, "Roster")
        rows = ops_tab.ad_widget.person_table.rowCount()
        classes = list_items(ops_tab.pdf_widget.class_list)

        ops_tab.on_source_changed("Roster")

        assert ops_tab.ad_widget.person_table.rowCount() == rows
        assert list_items(ops_tab.pdf_widget.class_list) == classes

    def test_an_added_source_appears_without_any_refresh_button(
            self, ops_tab, source_manager):
        """The tab advertises itself as auto-updated - the signal must do it."""
        source_manager.add_source(build_source("Alpha", []))
        source_manager.add_source(build_source("Beta", []))

        assert combo_items(ops_tab.source_combo) == \
            ["(Select Source)", "Alpha", "Beta"]

    def test_a_removed_source_disappears_from_the_combo(self, ops_tab, source_manager):
        """Removing a source drops exactly that entry."""
        alpha = build_source("Alpha", [])
        beta = build_source("Beta", [])
        source_manager.add_source(alpha)
        source_manager.add_source(beta)

        source_manager.remove_source(alpha)

        assert combo_items(ops_tab.source_combo) == ["(Select Source)", "Beta"]

    def test_the_selection_survives_an_unrelated_source_being_added(
            self, ops_tab, source_manager):
        """Adding another source must not steal the user's current choice."""
        keep = build_source("Keep", [("6.A", [("Jan", "Novák")])])
        source_manager.add_source(keep)
        select_source(ops_tab.source_combo, "Keep")

        source_manager.add_source(build_source("Other", [("9.Z", [])]))

        assert selected_source(ops_tab.source_combo) == "Keep"
        assert ops_tab.current_source is keep
        assert ops_tab.ad_widget.current_source is keep

    def test_repeated_signals_never_duplicate_combo_entries(
            self, ops_tab, source_manager):
        """Several refreshes in a row leave one entry per source."""
        source_manager.add_source(build_source("Alpha", []))
        for _ in range(5):
            ops_tab.on_sources_changed()

        assert combo_items(ops_tab.source_combo) == ["(Select Source)", "Alpha"]

    def test_unicode_source_names_survive_the_combo_round_trip(
            self, ops_tab, source_manager):
        """Diacritics in a source name must not break the lookup by text."""
        name = "Třída – Škola Kožušany 2024/25"
        source = build_source(name, [("VI.A", [("Šárka", "Nováková")])])
        source_manager.add_source(source)

        select_source(ops_tab.source_combo, name)

        assert selected_source(ops_tab.source_combo) == name
        assert ops_tab.current_source is source
        assert ops_tab.json_widget.current_source is source

    def test_a_combo_entry_without_a_matching_source_selects_nothing(
            self, ops_tab, source_manager):
        """A stale entry must clear the widgets instead of keeping old data."""
        source_manager.add_source(build_source("Roster", [("6.A", [("Jan", "Novák")])]))
        select_source(ops_tab.source_combo, "Roster")

        ops_tab.source_combo.addItem("Ghost")
        select_source(ops_tab.source_combo, "Ghost")

        assert ops_tab.current_source is None
        assert ops_tab.ad_widget.current_source is None
        assert ops_tab.pdf_widget.current_source is None
        assert ops_tab.json_widget.current_source is None

    def test_two_sources_with_the_same_name_are_both_listed(
            self, ops_tab, source_manager):
        """The combo mirrors the manager; it never silently swallows an entry."""
        source_manager.add_source(build_source("Same", [("6.A", [])]))
        source_manager.add_source(build_source("Same", [("7.A", [])]))

        assert combo_items(ops_tab.source_combo) == \
            ["(Select Source)", "Same", "Same"]

    @pytest.mark.bug
    def test_removing_the_selected_source_disarms_the_operation_widgets(
            self, ops_tab, source_manager):
        """A deleted source must not stay operable behind '(Select Source)'."""
        source = build_source("Roster", [("6.A", [("Jan", "Novák")])])
        source_manager.add_source(source)
        select_source(ops_tab.source_combo, "Roster")

        source_manager.remove_source(source)

        assert selected_source(ops_tab.source_combo) == "(Select Source)"
        assert ops_tab.current_source is None
        assert ops_tab.ad_widget.current_source is None
        assert ops_tab.pdf_widget.current_source is None
        assert ops_tab.json_widget.current_source is None

    @pytest.mark.bug
    def test_a_modified_source_refreshes_every_operation_widget(
            self, ops_tab, source_manager):
        """Editing a source on tab 2 must be visible on tab 3 immediately."""
        source = build_source("Roster", [("6.A", [("Jan", "Novák")])])
        source_manager.add_source(source)
        select_source(ops_tab.source_combo, "Roster")

        source.add_class(Class(name="6.B", persons=[Person("Eva", "Malá", "6.B")]))
        source_manager.notify_source_modified("Roster")

        assert ops_tab.ad_widget.person_table.rowCount() == 2
        assert list_items(ops_tab.pdf_widget.class_list) == ["6.A", "6.B"]
        assert "Classes: 2" in ops_tab.json_widget.source_info_label.text()


@pytest.mark.gui
class TestOperationsTabOperationList:
    """The operation list drives the stacked widget on the right."""

    def test_every_documented_operation_is_offered(self, ops_tab):
        """The list and the stack have to stay in step."""
        from ui.operations_tab import OperationsTab

        assert list_items(ops_tab.operation_list) == OperationsTab.OPERATIONS
        assert ops_tab.operation_stack.count() == len(OperationsTab.OPERATIONS)

    def test_every_page_is_told_about_the_source(self, ops_tab, make_source):
        """A page the tab forgets would silently operate on the wrong data."""
        source = make_source(name="Roster", classes=[("6.A", 2)])
        ops_tab.source_manager.add_source(source)
        select_source(ops_tab.source_combo, "Roster")

        for widget in ops_tab.operation_widgets():
            assert widget.current_source is source

    @pytest.mark.parametrize("row,attribute", [
        (0, "ad_widget"),
        (1, "m365_widget"),
        (2, "pdf_widget"),
        (3, "json_widget"),
    ])
    def test_choosing_an_operation_shows_the_matching_widget(
            self, ops_tab, row, attribute):
        """Row N of the list shows page N of the stack."""
        ops_tab.operation_list.setCurrentRow(row)
        assert ops_tab.operation_stack.currentIndex() == row
        assert ops_tab.operation_stack.currentWidget() is getattr(ops_tab, attribute)

    def test_walking_through_every_operation_and_back_is_stable(self, ops_tab):
        """Switching forwards and backwards always lands on the same page."""
        for row in (0, 1, 2, 3, 2, 1, 0, 3, 0):
            ops_tab.operation_list.setCurrentRow(row)
            assert ops_tab.operation_stack.currentIndex() == row

    def test_an_empty_selection_leaves_the_stack_where_it_was(self, ops_tab):
        """currentRowChanged(-1) is a deselection, not a page change."""
        ops_tab.operation_list.setCurrentRow(2)
        ops_tab.on_operation_changed(-1)
        assert ops_tab.operation_stack.currentIndex() == 2

    @pytest.mark.bug
    def test_the_initially_visible_operation_is_the_highlighted_one(self, ops_tab):
        """The page on screen and the highlighted list row must agree."""
        assert ops_tab.operation_stack.currentWidget() is ops_tab.ad_widget
        assert ops_tab.operation_list.currentRow() == 0


# ===========================================================================
# SourceSelectionTab
# ===========================================================================

SOURCE_TYPES = [
    (0, "EduPage", "edupage_widget"),
    (1, "Encrypted JSON File", "file_widget"),
    (2, "Active Directory", "ad_widget"),
    (3, "Microsoft 365 From Web", "m365_widget"),
]


@pytest.mark.gui
class TestSourceSelectionTab:
    """The first tab picks a source type and shows its configuration page."""

    def test_every_documented_source_type_is_offered(self, selection_tab):
        """Combo entries and stacked pages must line up one to one."""
        assert combo_items(selection_tab.source_combo) == \
            [label for _index, label, _attribute in SOURCE_TYPES]
        assert selection_tab.config_stack.count() == len(SOURCE_TYPES)
        assert selection_tab.config_stack.currentIndex() == 0

    def test_the_combo_and_the_stack_cannot_drift_apart(self, selection_tab):
        """One list decides both, so a new type cannot be added to only one."""
        from ui.source_selection_tab import SourceSelectionTab
        assert combo_items(selection_tab.source_combo) == \
            SourceSelectionTab.SOURCE_TYPES
        assert selection_tab.config_stack.count() == \
            len(SourceSelectionTab.SOURCE_TYPES)

    @pytest.mark.parametrize("index,label,attribute", SOURCE_TYPES)
    def test_choosing_a_source_type_shows_the_matching_page(
            self, selection_tab, index, label, attribute):
        """Selecting a type by text switches the stack to its widget."""
        select_source(selection_tab.source_combo, label)
        assert selection_tab.config_stack.currentIndex() == index
        assert selection_tab.config_stack.currentWidget() is \
            getattr(selection_tab, attribute)

    def test_switching_types_back_and_forth_is_idempotent(self, selection_tab):
        """Any path through the combo ends on the page that belongs to it."""
        for index in (2, 0, 1, 2, 1, 0):
            selection_tab.source_combo.setCurrentIndex(index)
            assert selection_tab.config_stack.currentIndex() == index

    def test_all_three_source_widgets_share_the_tabs_source_manager(
            self, selection_tab, source_manager):
        """Whatever a page loads must land in the application's one manager."""
        assert selection_tab.edupage_widget.source_manager is source_manager
        assert selection_tab.file_widget.source_manager is source_manager
        assert selection_tab.ad_widget.source_manager is source_manager

    def test_the_edupage_load_button_matches_the_encrypted_file_one(
            self, selection_tab):
        """Both data sources must offer the same looking load button."""
        edupage = selection_tab.edupage_widget.load_btn
        encrypted = selection_tab.file_widget.load_btn

        assert edupage.styleSheet() == encrypted.styleSheet()
        assert edupage.minimumHeight() == encrypted.minimumHeight()
        assert edupage.sizePolicy().horizontalPolicy() == \
            encrypted.sizePolicy().horizontalPolicy()

    def test_no_load_button_carries_a_hand_rolled_stylesheet(self, selection_tab):
        """A per-button stylesheet is exactly what made the two drift apart."""
        for widget in (selection_tab.edupage_widget,
                       selection_tab.file_widget,
                       selection_tab.ad_widget):
            assert widget.load_btn.styleSheet() == ""

    def test_every_load_button_is_enabled_and_labelled(self, selection_tab):
        """A disabled or unnamed load button would make a page unusable."""
        for widget in (selection_tab.edupage_widget,
                       selection_tab.file_widget,
                       selection_tab.ad_widget):
            assert widget.load_btn.isEnabled()
            assert widget.load_btn.text().strip()


@pytest.mark.gui
class TestEduPageLoadButton:
    """The EduPage page must validate before it touches the network."""

    @pytest.fixture
    def edupage(self, selection_tab, monkeypatch):
        """The EduPage widget with every coordinator entry point recorded."""
        widget = selection_tab.edupage_widget
        calls = []

        for name in ("execute_mode1_single_phase", "execute_mode1_two_phase",
                     "execute_mode2_load"):
            monkeypatch.setattr(
                widget.coordinator, name,
                (lambda n: lambda *args: calls.append((n, args)))(name),
            )
        widget.recorded = calls
        return widget

    @pytest.mark.parametrize("subdomain,username,password", [
        ("", "", ""),
        ("school", "", "secret"),
        ("", "teacher@school.cz", "secret"),
        ("school", "teacher@school.cz", ""),
        ("   ", "teacher@school.cz", "secret"),
    ])
    def test_incomplete_credentials_warn_and_never_reach_the_network(
            self, edupage, dialogs, subdomain, username, password):
        """Any missing field stops the load before a connection is opened."""
        edupage.subdomain_input.setText(subdomain)
        edupage.username_input.setText(username)
        edupage.password_input.setText(password)

        edupage.on_load_clicked()

        assert edupage.recorded == []
        assert dialogs.saw("Missing Information")

    def test_single_phase_mode_forwards_the_trimmed_credentials(
            self, edupage, dialogs):
        """Mode 1 / single phase passes (username, password, subdomain)."""
        edupage.subdomain_input.setText("  school  ")
        edupage.username_input.setText("  teacher@school.cz ")
        edupage.password_input.setText(" s3cret ")

        edupage.on_load_clicked()

        assert edupage.recorded == [
            ("execute_mode1_single_phase",
             ("teacher@school.cz", " s3cret ", "school")),
        ]
        assert dialogs.calls == []

    def test_two_phase_mode_uses_the_two_phase_entry_point(self, edupage, dialogs):
        """Picking 'Two Phase' changes which coordinator method runs."""
        edupage.subdomain_input.setText("school")
        edupage.username_input.setText("teacher@school.cz")
        edupage.password_input.setText("s3cret")
        edupage.two_phase_radio.setChecked(True)

        edupage.on_load_clicked()

        assert [name for name, _ in edupage.recorded] == \
            ["execute_mode1_two_phase"]

    def test_mode_two_goes_to_its_own_entry_point(self, edupage, dialogs):
        """The reserved mode must not silently fall back to mode 1."""
        edupage.subdomain_input.setText("school")
        edupage.username_input.setText("teacher@school.cz")
        edupage.password_input.setText("s3cret")
        edupage.mode2_radio.setChecked(True)

        edupage.on_load_clicked()

        assert [name for name, _ in edupage.recorded] == ["execute_mode2_load"]

    def test_a_failing_coordinator_is_reported_instead_of_crashing(
            self, edupage, dialogs, monkeypatch):
        """An exception on start-up becomes a critical dialog, not a traceback."""
        def explode(*args):
            raise RuntimeError("edupage_api missing")

        monkeypatch.setattr(edupage.coordinator,
                            "execute_mode1_single_phase", explode)
        edupage.subdomain_input.setText("school")
        edupage.username_input.setText("teacher@school.cz")
        edupage.password_input.setText("s3cret")

        edupage.on_load_clicked()

        assert "critical" in dialogs.kinds()
        assert dialogs.saw("edupage_api missing")


# ===========================================================================
# SourceManager signals reaching the tabs
# ===========================================================================

@pytest.mark.gui
@pytest.mark.integration
class TestSignalPlumbing:
    """The SourceManager signals are the only refresh mechanism there is."""

    def test_source_added_reaches_the_panels_and_the_operations_tab(self, window):
        """One add_source() call updates four combos at once."""
        window.source_manager.add_source(build_source("Alpha", [("6.A", [])]))

        assert "Alpha" in combo_items(window.operations_tab.source_combo)
        assert "Alpha" in combo_items(window.comparison_tab.left_panel.source_combo)
        assert "Alpha" in combo_items(window.comparison_tab.right_panel.source_combo)
        assert "Alpha" in combo_items(window.comparison_tab.output_panel.source_combo)

    def test_source_modified_refreshes_only_the_panel_showing_that_source(
            self, window):
        """A modification repaints the panels that display the modified source."""
        alpha = build_source("Alpha", [("6.A", [("Jan", "Novák")])])
        beta = build_source("Beta", [("7.A", [("Eva", "Malá")])])
        window.source_manager.add_source(alpha)
        window.source_manager.add_source(beta)

        left = window.comparison_tab.left_panel
        right = window.comparison_tab.right_panel
        select_source(left.source_combo, "Alpha")
        select_source(right.source_combo, "Beta")

        alpha.classes[0].add_person(Person("Petr", "Černý", "6.A"))
        window.source_manager.notify_source_modified("Alpha")

        assert left.tree.topLevelItem(0).childCount() == 2
        assert right.tree.topLevelItem(0).childCount() == 1
        assert "2 persons" in left.stats_label.text()

    def test_removing_the_selected_source_clears_the_comparison_panel(self, window):
        """The panel drops its tree and its current source with the entry."""
        alpha = build_source("Alpha", [("6.A", [("Jan", "Novák")])])
        window.source_manager.add_source(alpha)
        panel = window.comparison_tab.left_panel
        select_source(panel.source_combo, "Alpha")
        assert panel.current_source is alpha

        window.source_manager.remove_source(alpha)

        assert panel.current_source is None
        assert panel.tree.topLevelItemCount() == 0
        assert selected_source(panel.source_combo) == "(Select Source)"
        assert panel.stats_label.text() == ""

    def test_generating_credentials_notifies_the_comparison_panels(self, window):
        """The AD widget announces its edits so the tree shows them."""
        source = build_source("Roster", [("6.A", [("Jan", "Novák")])])
        window.source_manager.add_source(source)

        received = []
        window.source_manager.source_modified.connect(received.append)

        select_source(window.operations_tab.source_combo, "Roster")
        window.operations_tab.ad_widget.generate_all_credentials()

        assert received == ["Roster"]
        assert source.get_all_persons()[0].ad_username


# ===========================================================================
# The realistic end-to-end workflow
# ===========================================================================

@pytest.mark.integration
@pytest.mark.gui
class TestSchoolYearRollover:
    """
    Two read-only sources with mixed Roman/Arabic class names are analysed,
    repaired, shifted, merged, given credentials and serialised - and the two
    inputs come out of it untouched.
    """

    def test_the_merge_analysis_spots_the_mixed_class_name_styles(
            self, rollover_sources):
        """Roman on the left, Arabic on the right: no student matches."""
        edupage, backup = rollover_sources
        report = analyze_merge(edupage, backup, "union")

        assert "cross_source_class_style" in [i.key for i in report.issues]
        assert report.stats["Students in both"] == 0
        assert report.stats["Result with 'intersection'"] == 0

    def test_unifying_the_names_works_on_a_copy_and_leaves_the_input_alone(
            self, rollover_sources):
        """Fixes are applied to a working copy - the read-only source is safe."""
        edupage, backup = rollover_sources
        before = snapshot(edupage)

        report = analyze_merge(edupage, backup, "union")
        issue = next(i for i in report.issues if i.key == "cross_source_class_style")
        working = edupage.deep_copy()
        result = apply_fix(working, issue, "unify_arabic")

        assert result.applied and result.changed == 3
        assert [c.name for c in working.classes] == ["6.A", "6.B", "9.A"]
        assert [p.class_name for p in working.get_all_persons()] == \
            ["6.A", "6.A", "6.B", "9.A"]
        assert snapshot(edupage) == before

    def test_shifting_the_repaired_copy_graduates_the_leaving_year(
            self, rollover_sources, dialogs, qapp):
        """+1 year turns 6.A/6.B into 7.A/7.B and drops the graduating 9.A."""
        from ui.class_operations_dialogs import ClassShiftDialog

        edupage, backup = rollover_sources
        working = edupage.deep_copy()
        report = analyze_merge(edupage, backup, "union")
        issue = next(i for i in report.issues if i.key == "cross_source_class_style")
        apply_fix(working, issue, "unify_arabic")

        dialog = ClassShiftDialog(working)
        try:
            shifted = dialog.build_result_source("EduPage-2025")
        finally:
            dispose(qapp, dialog)

        assert [(c.name, len(c.persons)) for c in shifted.classes] == \
            [("7.A", 2), ("7.B", 1)]
        assert {p.class_name for p in shifted.get_all_persons()} == {"7.A", "7.B"}
        assert shifted.readonly is False
        assert shifted.name == "EduPage-2025"
        # "Lucie Dvořáková" reached year 9 and left the school
        assert "Dvořáková" not in {p.last_name for p in shifted.get_all_persons()}

    def test_the_full_rollover_produces_one_consistent_roster(
            self, rollover_sources, dialogs, qapp, source_manager, ops_tab):
        """Analyse -> fix -> shift -> merge -> credentials -> serialise."""
        from ui.class_operations_dialogs import ClassShiftDialog

        edupage, backup = rollover_sources
        edupage_before, backup_before = snapshot(edupage), snapshot(backup)

        # 1 - the analysis finds the style mismatch
        report = analyze_merge(edupage, backup, "union")
        issue = next(i for i in report.issues if i.key == "cross_source_class_style")

        # 2 - the fix runs on a working copy
        working = edupage.deep_copy()
        assert apply_fix(working, issue, "unify_arabic").changed == 3

        # 3 - the shift creates next year's source
        dialog = ClassShiftDialog(working)
        try:
            shifted = dialog.build_result_source("EduPage-2025")
        finally:
            dispose(qapp, dialog)

        # 4 - now the two sides speak the same language
        after = analyze_merge(shifted, backup, "union")
        assert [i.key for i in after.issues] == []
        assert after.stats["Students in both"] == 3
        assert after.stats["Students only in right"] == 1

        # 5 - the merge keeps the left records and adds the new student
        merged, stats = SourceManager.merge_sources_detailed(
            shifted, backup, "union", "Roster 2025/26"
        )
        assert stats["result_persons"] == 4
        assert [(c.name, len(c.persons)) for c in merged.classes] == \
            [("7.A", 2), ("7.B", 2)]
        assert merged.readonly is False
        assert analyze_source(merged).is_clean

        # 6 - the operations tab drives the credential generation
        source_manager.add_source(merged)
        select_source(ops_tab.source_combo, "Roster 2025/26")
        assert ops_tab.ad_widget.current_source is merged
        ops_tab.ad_widget.generate_all_credentials()

        persons = merged.get_all_persons()
        usernames = [p.ad_username for p in persons]
        assert sorted(usernames) == \
            ["cernypetr", "malaeva", "novakjan", "novakovasarka"]
        assert all(is_ascii_lower_alnum(u) for u in usernames)
        assert len(set(usernames)) == len(usernames)
        assert all(p.ad_password for p in persons)
        assert next(p for p in persons if p.last_name == "Nováková").ad_display_name \
            == "Šárka Nováková (7.A)"

        # 7 - a serialisation round trip loses nothing
        payload = source_to_dict(merged)
        assert json.dumps(payload)          # really JSON serialisable
        restored = source_from_dict(payload, readonly=False)
        assert snapshot(restored) == snapshot(merged)

        # 8 - both read-only inputs came through untouched
        assert snapshot(edupage) == edupage_before
        assert snapshot(backup) == backup_before

    def test_an_intersection_of_the_repaired_sources_keeps_the_shared_students(
            self, rollover_sources, dialogs, qapp):
        """The other merge strategy drops the student only the backup knows."""
        from ui.class_operations_dialogs import ClassShiftDialog

        edupage, backup = rollover_sources
        working = edupage.deep_copy()
        report = analyze_merge(edupage, backup, "union")
        issue = next(i for i in report.issues if i.key == "cross_source_class_style")
        apply_fix(working, issue, "unify_arabic")

        dialog = ClassShiftDialog(working)
        try:
            shifted = dialog.build_result_source("EduPage-2025")
        finally:
            dispose(qapp, dialog)

        merged = SourceManager.merge_sources(shifted, backup, "intersection", "Common")

        names = sorted(f"{p.first_name} {p.last_name}" for p in merged.get_all_persons())
        assert names == ["Jan Novák", "Petr Černý", "Šárka Nováková"]

    def test_the_merged_roster_is_independent_of_both_inputs(
            self, rollover_sources):
        """Editing the result must not write back into the source data."""
        edupage, backup = rollover_sources
        backup_before = snapshot(backup)

        merged = SourceManager.merge_sources(backup, backup, "union", "Copy")
        for person in merged.get_all_persons():
            person.ad_username = "overwritten"
            person.class_name = "99.Z"

        assert snapshot(backup) == backup_before
        assert all(p.ad_username is None for p in backup.get_all_persons())

    def test_credentials_survive_the_serialisation_round_trip(
            self, rollover_sources, dialogs, ops_tab, source_manager):
        """User name, password and display name are all written and read back."""
        _, backup = rollover_sources
        editable = SourceManager.merge_sources(backup, backup, "union", "Editable")
        source_manager.add_source(editable)

        select_source(ops_tab.source_combo, "Editable")
        ops_tab.ad_widget.generate_all_credentials()

        restored = source_from_dict(source_to_dict(editable), readonly=True)

        assert snapshot(restored)[3] == snapshot(editable)[3]
        assert all(p.ad_password for p in restored.get_all_persons())
        assert restored.readonly is True

    def test_a_read_only_source_refuses_credential_generation(
            self, rollover_sources, dialogs, ops_tab, source_manager):
        """The read-only flag reaches all the way into the operations tab."""
        _, backup = rollover_sources
        source_manager.add_source(backup)
        select_source(ops_tab.source_combo, "Backup-2025")

        ops_tab.ad_widget.generate_all_credentials()

        assert dialogs.saw("Read-Only Source")
        assert all(p.ad_username is None for p in backup.get_all_persons())

    def test_generating_credentials_twice_keeps_the_names_unique(
            self, rollover_sources, dialogs, ops_tab, source_manager):
        """A second run must not hand two students the same user name."""
        _, backup = rollover_sources
        editable = SourceManager.merge_sources(backup, backup, "union", "Editable")
        source_manager.add_source(editable)
        select_source(ops_tab.source_combo, "Editable")

        ops_tab.ad_widget.generate_all_credentials()
        first = [p.ad_username for p in editable.get_all_persons()]
        ops_tab.ad_widget.generate_all_credentials()
        second = [p.ad_username for p in editable.get_all_persons()]

        assert first == second
        assert len(set(second)) == len(second)


@pytest.mark.integration
@pytest.mark.gui
class TestEncryptedFileEndToEnd:
    """An encrypted export loaded on tab 1 has to reach tab 2 and tab 3."""

    PASSWORD = "Str0ng-Test-Passw0rd"

    def _write_export(self, tmp_path, source, file_name="roster.aes"):
        from utils.encryption import encrypt_file_aes_gcm

        path = tmp_path / file_name
        encrypt_file_aes_gcm(json.dumps(source_to_dict(source)),
                             str(path), self.PASSWORD)
        return path

    def test_a_loaded_export_reaches_every_tab_as_a_read_only_source(
            self, window, tmp_path, dialogs):
        """File -> SourceManager -> operations tab and comparison panels."""
        original = build_source(
            "Export",
            [("6.A", [("Jan", "Novák"), ("Šárka", "Nováková")]),
             ("6.B", [("Petr", "Černý")])],
        )
        path = self._write_export(tmp_path, original)

        file_widget = window.source_selection_tab.file_widget
        file_widget.method_combo.setCurrentIndex(2)      # AES-GCM
        file_widget.file_input.setText(str(path))
        file_widget.password_input.setText(self.PASSWORD)
        dialogs.text_answer = ("Loaded roster", True)

        file_widget.on_load()

        loaded = window.source_manager.get_source_by_name("Loaded roster")
        assert loaded is not None
        assert loaded.readonly is True
        assert [c.name for c in loaded.classes] == ["6.A", "6.B"]
        assert sorted(p.last_name for p in loaded.get_all_persons()) == \
            ["Novák", "Nováková", "Černý"]
        assert "Loaded roster" in combo_items(window.operations_tab.source_combo)
        assert "Loaded roster" in \
            combo_items(window.comparison_tab.left_panel.source_combo)
        assert file_widget.password_input.text() == ""

    def test_a_wrong_password_reports_the_failure_and_adds_nothing(
            self, window, tmp_path, dialogs):
        """A bad password must not leave a half-built source behind."""
        path = self._write_export(tmp_path,
                                  build_source("Export", [("6.A", [("Jan", "Novák")])]))

        file_widget = window.source_selection_tab.file_widget
        file_widget.method_combo.setCurrentIndex(2)
        file_widget.file_input.setText(str(path))
        file_widget.password_input.setText("completely-wrong-password")

        file_widget.on_load()

        assert window.source_manager.get_source_names() == []
        assert "critical" in dialogs.kinds()

    def test_loading_the_same_name_twice_replaces_the_old_source(
            self, window, tmp_path, dialogs):
        """The 'Replace it?' promise has to be kept - no ghost duplicate."""
        from PyQt6.QtWidgets import QMessageBox

        first = build_source("Export", [("6.A", [("Jan", "Novák")])])
        second = build_source("Export", [("6.A", [("Jan", "Novák")]),
                                         ("6.B", [("Eva", "Malá")])])

        file_widget = window.source_selection_tab.file_widget
        file_widget.method_combo.setCurrentIndex(2)
        file_widget.password_input.setText(self.PASSWORD)
        dialogs.text_answer = ("Roster", True)

        file_widget.file_input.setText(
            str(self._write_export(tmp_path, first, "first.aes")))
        file_widget.on_load()

        dialogs.question_answer = QMessageBox.StandardButton.Yes
        file_widget.password_input.setText(self.PASSWORD)
        file_widget.file_input.setText(
            str(self._write_export(tmp_path, second, "second.aes")))
        file_widget.on_load()

        assert window.source_manager.get_source_names() == ["Roster"]
        assert len(window.source_manager.get_source_by_name("Roster").classes) == 2
        assert combo_items(window.operations_tab.source_combo) == \
            ["(Select Source)", "Roster"]


# ===========================================================================
# Hard-dependency regression guard
# ===========================================================================

FORBIDDEN_TOP_LEVEL_MODULES = (
    "ldap3",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineCore",
)

SKIPPED_DIRECTORIES = {
    "__pycache__", ".git", ".venv", "venv", "env", "build", "dist",
    "tests", "docs", ".pytest_cache",
}


def project_python_files():
    """Every ``.py`` file that belongs to the application itself."""
    files = []
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if any(part in SKIPPED_DIRECTORIES for part in relative.parts):
            continue
        files.append(path)
    return files


def _handles_import_error(handler):
    """True when an ``except`` clause would catch a missing package."""
    node = handler.type
    if node is None:
        return True
    candidates = node.elts if isinstance(node, ast.Tuple) else [node]
    names = []
    for candidate in candidates:
        if isinstance(candidate, ast.Name):
            names.append(candidate.id)
        elif isinstance(candidate, ast.Attribute):
            names.append(candidate.attr)
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception")
               for n in names)


def _imported_modules(node):
    """The dotted module names an import statement pulls in."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def _is_forbidden(module, wanted):
    return any(module == name or module.startswith(name + ".") for name in wanted)


def unguarded_imports(tree, wanted=FORBIDDEN_TOP_LEVEL_MODULES):
    """
    Find imports of *wanted* that run unconditionally when the module loads.

    An import is considered safe when it lives inside a function (deferred
    until the feature is used) or inside a ``try`` whose ``except`` catches
    ``ImportError`` (optional dependency with a fallback).

    Returns:
        A list of ``(line number, module name)`` tuples.
    """
    found = []

    def scan(node, guarded):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if not guarded:
                for module in _imported_modules(node):
                    if _is_forbidden(module, wanted):
                        found.append((node.lineno, module))
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in node.body:
                scan(child, True)
            return
        if isinstance(node, ast.Try):
            inner = guarded or any(_handles_import_error(h) for h in node.handlers)
            for child in node.body:
                scan(child, inner)
            for handler in node.handlers:
                for child in handler.body:
                    scan(child, guarded)
            for child in list(node.orelse) + list(node.finalbody):
                scan(child, guarded)
            return
        for child in ast.iter_child_nodes(node):
            scan(child, guarded)

    scan(tree, False)
    return found


def parse_project_file(path):
    """Parse a project file, tolerating the UTF-8 BOM some of them carry."""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


class TestOptionalDependencyGuard:
    """
    ``ldap3`` and QtWebEngine are optional.

    Importing either of them at module level used to stop the whole
    application from starting for users who only export PDFs or work with
    encrypted files.
    """

    def test_the_scan_actually_walks_the_application(self):
        """A guard that scans nothing would pass for the wrong reason."""
        files = project_python_files()
        relative = {str(p.relative_to(PROJECT_ROOT)) for p in files}

        assert len(files) > 30
        assert {"main.py", "ui/operations_tab.py", "ui/source_selection_tab.py",
                "services/ldap_compat.py", "ui/pdf_export_dialog.py",
                "sources/ad_source.py"} <= relative
        assert not any("tests" in p.parts for p in files)

    @pytest.mark.parametrize("module", FORBIDDEN_TOP_LEVEL_MODULES)
    def test_no_project_module_imports_it_unconditionally(self, module):
        """Every use must be deferred into a function or guarded by try/except."""
        offenders = []
        for path in project_python_files():
            for line, name in unguarded_imports(parse_project_file(path), (module,)):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line} -> {name}")

        assert offenders == [], (
            f"{module} must not be imported at module level: " + ", ".join(offenders)
        )

    def test_the_scanner_reports_a_deliberate_top_level_import(self):
        """Meta-test: the guard is able to fail."""
        tree = ast.parse("import logging\nimport ldap3\n")
        assert unguarded_imports(tree) == [(2, "ldap3")]

    def test_the_scanner_reports_a_top_level_from_import(self):
        """``from ldap3 import Server`` is just as fatal as ``import ldap3``."""
        tree = ast.parse("from ldap3 import Server\n")
        assert unguarded_imports(tree) == [(1, "ldap3")]

    def test_the_scanner_reports_a_class_body_import(self):
        """A class body also executes at import time."""
        tree = ast.parse("class A:\n    from PyQt6.QtWebEngineWidgets import X\n")
        assert unguarded_imports(tree) == [(2, "PyQt6.QtWebEngineWidgets")]

    @pytest.mark.parametrize("source", [
        "def load():\n    import ldap3\n",
        "class A:\n    def load(self):\n        from ldap3 import Server\n",
        "try:\n    import ldap3\nexcept ImportError:\n    ldap3 = None\n",
        "try:\n    import ldap3\nexcept (ImportError, OSError):\n    ldap3 = None\n",
        "try:\n    from PyQt6.QtWebEngineWidgets import V\n"
        "except ModuleNotFoundError:\n    V = None\n",
    ])
    def test_the_scanner_accepts_deferred_and_guarded_imports(self, source):
        """Function-local and ImportError-guarded imports are the fix, not the bug."""
        assert unguarded_imports(ast.parse(source)) == []

    def test_the_scanner_still_flags_a_try_that_catches_something_else(self):
        """``except ValueError`` around an import does not make it optional."""
        tree = ast.parse("try:\n    import ldap3\nexcept ValueError:\n    pass\n")
        assert unguarded_imports(tree) == [(2, "ldap3")]

    def test_the_ldap_shim_exposes_an_availability_flag(self):
        """services.ldap_compat is the single place that knows about ldap3."""
        from services import ldap_compat

        assert isinstance(ldap_compat.LDAP3_AVAILABLE, bool)
        assert unguarded_imports(
            parse_project_file(PROJECT_ROOT / "services" / "ldap_compat.py")
        ) == []

    @pytest.mark.parametrize("module_name", [
        "ui.pdf_export_dialog",
        "ui.pdf_viewer_window",
    ])
    def test_the_pdf_preview_modules_expose_a_web_engine_flag(self, module_name):
        """Without QtWebEngine the preview is disabled, not the application."""
        import importlib

        module = importlib.import_module(module_name)
        assert isinstance(module.WEB_ENGINE_AVAILABLE, bool)

    def test_the_application_entry_point_has_no_optional_imports(self):
        """main.py is the first module to load; it must import nothing optional."""
        assert unguarded_imports(parse_project_file(PROJECT_ROOT / "main.py")) == []

    @pytest.mark.parametrize("relative", [
        "ui/operations_tab.py",
        "ui/source_selection_tab.py",
    ])
    def test_the_tabs_under_test_have_no_optional_imports(self, relative):
        """Both tabs are built during start-up, so neither may need a package."""
        assert unguarded_imports(parse_project_file(PROJECT_ROOT / relative)) == []


# ===========================================================================
# Unicode sanity for the whole pipeline
# ===========================================================================

@pytest.mark.integration
@pytest.mark.parametrize("first,last,expected", [
    ("Jan", "Novák", "novakjan"),
    ("Šárka", "Nováková", "novakovasarka"),
    ("Ľuboš", "Ďurčo", "durcolubos"),
    ("Zoë", "Müller", "mullerzoe"),
    ("Łukasz", "Weiß", "weisslukasz"),
])
def test_diacritics_are_folded_before_they_reach_active_directory(
        first, last, expected, dialogs, ops_tab, source_manager):
    """Credential generation has to produce plain ASCII sAMAccountNames."""
    source = build_source("Roster", [("6.A", [(first, last)])])
    source_manager.add_source(source)
    select_source(ops_tab.source_combo, "Roster")

    ops_tab.ad_widget.generate_all_credentials()

    person = source.get_all_persons()[0]
    assert person.ad_username == expected
    assert person.ad_username == unicodedata.normalize("NFKD", person.ad_username)
    assert person.ad_display_name == f"{first} {last} (6.A)"
