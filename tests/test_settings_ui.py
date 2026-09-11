"""
Unit tests for the settings user interface:

* ``ui.log_directory_widget`` - the three-part (base path / folder / file name)
  log location configurator, its fallbacks, its validation and its live status
  line, and
* ``ui.settings_tab`` - the four category panels, the load/save round trip, the
  "unusable log path" negotiation, every reset button and the theme handling.

Isolation rules followed here
-----------------------------
* ``isolated_settings`` + ``isolated_logging_config`` keep both JSON documents
  inside ``tmp_path``; ``app_dir`` redirects ``get_application_directory()``
  there too, so a saved configuration can never point the real logging handlers
  at the developer's project directory.
* ``dialogs`` (and the local ``choose_button``) replace every modal dialog, so
  nothing blocks.
* ``_isolate_root_logging`` puts the root logger back the way it was, because
  ``SettingsTab._setup_logging_handlers()`` clears and replaces its handlers.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect in the production code.
"""

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QApplication, QMessageBox, QPushButton, QScrollArea,
)

import ui.log_directory_widget as ldw
from ui.log_directory_widget import LogDirectoryWidget
from ui.settings_tab import SettingsTab
from utils.settings_manager import DEFAULT_SETTINGS

pytestmark = pytest.mark.gui

LOG_DEFAULTS = DEFAULT_SETTINGS["logging"]


# ---------------------------------------------------------------------------
# Local fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_root_logging():
    """Restore the root logger: the tab clears and replaces its handlers."""
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in original:
            try:
                handler.close()
            except Exception:
                pass
            root.removeHandler(handler)
    root.handlers[:] = original
    root.setLevel(original_level)


@pytest.fixture(autouse=True)
def _restore_palette(qapp):
    """``apply_theme()`` repaints the whole application - undo that."""
    original = QApplication.palette()
    yield
    QApplication.setPalette(original)


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Pretend the application runs from a throw-away directory."""
    directory = tmp_path / "appdir"
    directory.mkdir()
    monkeypatch.setattr(ldw, "get_application_directory", lambda: str(directory))
    return directory


@pytest.fixture
def widget(qapp, app_dir):
    """A LogDirectoryWidget whose factory default lives inside tmp_path."""
    instance = LogDirectoryWidget()
    yield instance
    instance.deleteLater()


@pytest.fixture
def tab(qapp, isolated_settings, isolated_logging_config, app_dir, dialogs):
    """A SettingsTab wired to the isolated settings and logging documents."""
    instance = SettingsTab()
    yield instance
    instance.close()
    instance.deleteLater()


@pytest.fixture
def choose_button(monkeypatch, dialogs):
    """
    Make ``QMessageBox.exec()`` click the button whose label contains a needle.

    ``QMessageBox.buttons()`` is ordered by platform convention, so picking a
    button by index (as the shared recorder does) is not portable.
    """
    def _install(needle):
        def _exec(self):
            dialogs.calls.append(("box_exec", self.windowTitle(), self.text()))
            chosen = None
            for button in self.buttons():
                if needle.lower() in button.text().lower():
                    chosen = button
                    break
            self._test_clicked = chosen
            return QMessageBox.StandardButton.Ok
        monkeypatch.setattr(QMessageBox, "exec", _exec)
    return _install


def reset_button_next_to(target):
    """Return the ``↺`` button that follows *target* inside its own row."""
    pending = [target.parentWidget().layout()]
    while pending:
        layout = pending.pop()
        if layout is None:
            continue
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is target:
                following = layout.itemAt(index + 1)
                assert following is not None, "no widget follows the target"
                return following.widget()
            if item.layout() is not None:
                pending.append(item.layout())
    raise AssertionError(f"{target!r} is not part of its parent's layout")


def button_with_tooltip(parent, needle):
    """Return the single button of *parent* whose tooltip contains *needle*."""
    found = [b for b in parent.findChildren(QPushButton)
             if needle.lower() in b.toolTip().lower()]
    assert len(found) == 1, f"{len(found)} buttons match {needle!r}"
    return found[0]


def emissions(widget_under_test):
    """Record every ``path_changed`` payload."""
    received = []
    widget_under_test.path_changed.connect(received.append)
    return received


def on_disk(settings):
    """The settings as they are currently stored in the JSON file."""
    return json.loads(settings.settings_file.read_text(encoding="utf-8"))


# ===========================================================================
# get_application_directory
# ===========================================================================

def test_get_application_directory_is_the_resolved_working_directory(
        monkeypatch, tmp_path):
    """Not frozen: a relative 'logs' folder resolves against the cwd."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert ldw.get_application_directory() == str(tmp_path.resolve())


def test_get_application_directory_follows_the_executable_when_frozen(
        monkeypatch, tmp_path):
    """A PyInstaller build logs next to the executable, not next to the cwd."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(bundle / "UserManagerX"))
    assert ldw.get_application_directory() == str(bundle.resolve())


# ===========================================================================
# LogDirectoryWidget - defaults and getters
# ===========================================================================

def test_new_widget_shows_the_application_directory_as_real_text(widget, app_dir):
    """The base path is filled in, never left to a grey placeholder."""
    assert widget._base_input.text() == str(app_dir)
    assert widget.get_base_path() == str(app_dir)


def test_new_widget_uses_the_documented_folder_and_file_defaults(widget):
    """A fresh widget points at <app dir>/logs/student_management.log."""
    assert widget.get_folder_name() == "logs"
    assert widget.get_log_filename() == "student_management.log"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_base_path_falls_back_to_the_application_directory(
        widget, app_dir, blank):
    """Clearing the field is not a hidden state - the app directory is used."""
    widget._base_input.setText(blank)
    assert widget.get_base_path() == str(app_dir)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_folder_name_falls_back_to_logs(widget, blank):
    """An empty folder field means the default 'logs' sub-directory."""
    widget._folder_input.setText(blank)
    assert widget.get_folder_name() == "logs"


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_log_filename_falls_back_to_the_default_file(widget, blank):
    """An empty file field means 'student_management.log'."""
    widget._file_input.setText(blank)
    assert widget.get_log_filename() == "student_management.log"


def test_getters_strip_surrounding_whitespace(widget, tmp_path):
    """Whitespace a user pastes in must not end up in the path."""
    widget.set_values(f"  {tmp_path}  ", "  lógy  ", "  žurnál.log  ")
    assert widget.get_base_path() == str(tmp_path)
    assert widget.get_folder_name() == "lógy"
    assert widget.get_log_filename() == "žurnál.log"


def test_get_full_path_joins_the_three_components_with_diacritics(
        widget, tmp_path):
    """base / folder / file survives unicode without mangling."""
    widget.set_values(str(tmp_path), "lógy", "žurnál.log")
    assert widget.get_log_dir() == str(tmp_path / "lógy")
    assert widget.get_full_path() == str(tmp_path / "lógy" / "žurnál.log")


def test_get_log_dir_expands_a_tilde_base_path(widget):
    """'~' must become the real home directory, never a literal folder."""
    widget.set_values("~", "logs", "app.log")
    assert widget.get_log_dir() == str(Path.home() / "logs")
    assert Path(widget.get_full_path()).is_absolute()


def test_default_paths_are_absolute(widget, app_dir):
    """The out-of-the-box configuration is an absolute location."""
    assert Path(widget.get_log_dir()).is_absolute()
    assert Path(widget.get_full_path()).is_absolute()


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: a relative base path is never resolved against "
                          "the application directory",
                   strict=False)
@pytest.mark.parametrize("getter", ["get_log_dir", "get_full_path"])
def test_a_relative_base_path_still_yields_an_absolute_location(widget, getter):
    """Both getters promise an absolute path, whatever the user typed."""
    widget.set_values("relative_logs", "logs", "app.log")
    assert Path(getattr(widget, getter)()).is_absolute()


def test_preview_field_is_read_only_and_mirrors_the_full_path(widget, tmp_path):
    """The preview always shows exactly what get_full_path() returns."""
    widget.set_values(str(tmp_path), "lg", "a.log")
    assert widget._preview_field.isReadOnly()
    assert widget._preview_field.text() == widget.get_full_path()


# ===========================================================================
# LogDirectoryWidget - set_from_log_dir_and_file
# ===========================================================================

def test_set_from_log_dir_resolves_a_relative_value_against_the_app_dir(
        widget, app_dir):
    """The factory value 'logs' must be shown as a real location."""
    widget.set_from_log_dir_and_file("logs", "app.log")
    assert widget.get_base_path() == str(app_dir)
    assert widget.get_folder_name() == "logs"
    assert widget.get_log_filename() == "app.log"
    assert widget.get_log_dir() == str(app_dir / "logs")


def test_set_from_log_dir_resolves_a_nested_relative_value(widget, app_dir):
    """Only the last segment is the folder name; the rest is the base path."""
    widget.set_from_log_dir_and_file("var/log/app", "app.log")
    assert widget.get_base_path() == str(app_dir / "var" / "log")
    assert widget.get_folder_name() == "app"


@pytest.mark.parametrize("stored, base, folder", [
    ("/var/log/myapp/logs", "/var/log/myapp", "logs"),
    ("/var/log/myapp/logs/", "/var/log/myapp", "logs"),
    ("/var/log/škola/lógy", "/var/log/škola", "lógy"),
    ("/", "/", "logs"),
])
def test_set_from_log_dir_splits_an_absolute_value(widget, stored, base, folder):
    """An absolute stored directory is split into parent + last segment."""
    widget.set_from_log_dir_and_file(stored, "app.log")
    assert (widget.get_base_path(), widget.get_folder_name()) == (base, folder)


def test_set_from_log_dir_expands_a_tilde_value(widget):
    """A stored '~/mylogs' is shown expanded, not as a literal '~'."""
    widget.set_from_log_dir_and_file("~/mylogs", "app.log")
    assert widget.get_base_path() == str(Path.home())
    assert widget.get_folder_name() == "mylogs"


@pytest.mark.parametrize("log_dir, log_file", [
    ("", ""),
    (None, None),
    ("   ", "   "),
])
def test_set_from_log_dir_falls_back_to_defaults_for_empty_values(
        widget, app_dir, log_dir, log_file):
    """Empty or missing stored values restore the factory location."""
    widget.set_from_log_dir_and_file(log_dir, log_file)
    assert widget.get_log_dir() == str(app_dir / "logs")
    assert widget.get_log_filename() == "student_management.log"


def test_set_from_log_dir_round_trips_its_own_output(widget, tmp_path):
    """Feeding get_log_dir() back in is a no-op (idempotency)."""
    widget.set_from_log_dir_and_file(str(tmp_path / "a" / "b"), "app.log")
    first = (widget.get_base_path(), widget.get_folder_name(),
             widget.get_log_filename())
    widget.set_from_log_dir_and_file(widget.get_log_dir(),
                                     widget.get_log_filename())
    assert (widget.get_base_path(), widget.get_folder_name(),
            widget.get_log_filename()) == first


# ===========================================================================
# LogDirectoryWidget - signals, browse and resets
# ===========================================================================

def test_set_values_emits_path_changed_exactly_once(widget, tmp_path):
    """All three fields are written, but the listener hears one update."""
    seen = emissions(widget)
    widget.set_values(str(tmp_path), "lg", "a.log")
    assert seen == [str(tmp_path / "lg" / "a.log")]


def test_typing_into_a_field_emits_the_new_combined_path(widget, tmp_path):
    """Every keystroke reports the whole path, not just the edited part."""
    widget.set_values(str(tmp_path), "lg", "a.log")
    seen = emissions(widget)
    widget._folder_input.setText("other")
    assert seen == [str(tmp_path / "other" / "a.log")]


def test_browse_writes_the_chosen_directory_into_the_base_field(
        widget, dialogs, tmp_path):
    """The directory picked in the file dialog becomes the base path."""
    chosen = tmp_path / "picked"
    chosen.mkdir()
    dialogs.directory_answer = str(chosen)
    button_with_tooltip(widget, "choose the parent directory").click()
    assert widget.get_base_path() == str(chosen)
    assert dialogs.kinds() == ["directory"]


def test_browse_keeps_the_current_base_path_when_cancelled(
        widget, dialogs, tmp_path):
    """Cancelling returns an empty string, which must not clear the field."""
    widget.set_values(str(tmp_path), "lg", "a.log")
    dialogs.directory_answer = ""
    button_with_tooltip(widget, "choose the parent directory").click()
    assert widget.get_base_path() == str(tmp_path)


def test_folder_reset_button_restores_the_configured_default_folder(qapp, app_dir):
    """The ↺ next to the folder name restores the constructor's default."""
    custom = LogDirectoryWidget(default_folder_name="lg",
                                default_log_filename="x.log")
    custom._folder_input.setText("elsewhere")
    button_with_tooltip(custom, "default folder name").click()
    assert custom.get_folder_name() == "lg"


def test_filename_reset_button_restores_the_configured_default_filename(
        qapp, app_dir):
    """The ↺ next to the log file restores the constructor's default."""
    custom = LogDirectoryWidget(default_log_filename="x.log")
    custom._file_input.setText("other.log")
    button_with_tooltip(custom, "default filename").click()
    assert custom.get_log_filename() == "x.log"


def test_base_reset_button_restores_the_application_directory(widget, app_dir):
    """The ↺ next to the base path returns to the application directory."""
    widget._base_input.setText("/somewhere/else")
    button_with_tooltip(widget, "application directory").click()
    assert widget.get_base_path() == str(app_dir)


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: the base-path reset button ignores the "
                          "default_base_path given to the constructor",
                   strict=False)
def test_base_reset_button_restores_the_configured_default_base(qapp, tmp_path,
                                                                app_dir):
    """Like its two siblings, it must restore the constructor's default."""
    configured = tmp_path / "configured"
    custom = LogDirectoryWidget(default_base_path=str(configured))
    custom._base_input.setText("/somewhere/else")
    button_with_tooltip(custom, "application directory").click()
    assert custom.get_base_path() == str(configured)


def test_reset_to_defaults_restores_the_factory_location(widget, app_dir):
    """All three fields go back to <app dir>/logs/student_management.log."""
    widget.set_values("/somewhere/else", "weird", "weird.txt")
    widget.reset_to_defaults()
    assert widget.get_full_path() == str(
        app_dir / "logs" / "student_management.log")


def test_reset_to_defaults_is_idempotent(widget):
    """Resetting twice changes nothing the second time."""
    widget.reset_to_defaults()
    first = widget.get_full_path()
    seen = emissions(widget)
    widget.reset_to_defaults()
    assert widget.get_full_path() == first
    assert seen == [first]


# ===========================================================================
# LogDirectoryWidget - validate()
# ===========================================================================

def test_validate_accepts_the_factory_configuration(widget):
    """The default location is usable without touching the file system."""
    assert widget.validate() == []
    assert widget.is_valid() is True


@pytest.mark.parametrize("bad", ['<', '>', '"', '|', '?', '*'])
def test_validate_rejects_invalid_characters_in_the_base_path(
        widget, tmp_path, bad):
    """Characters no platform accepts in a path are reported one by one."""
    widget.set_values(f"{tmp_path}{bad}dir", "logs", "a.log")
    problems = widget.validate()
    assert len(problems) == 1
    assert "base path contains invalid characters" in problems[0].lower()


@pytest.mark.parametrize("bad", ['<', '>', ':', '"', '|', '?', '*', '/', '\\'])
def test_validate_rejects_invalid_characters_in_the_folder_name(
        widget, tmp_path, bad):
    """A folder name is a single segment: separators are refused too."""
    widget.set_values(str(tmp_path), f"lo{bad}gs", "a.log")
    problems = widget.validate()
    assert any("folder name must not contain" in p.lower() for p in problems)


@pytest.mark.parametrize("bad", [':', '/', '\\', '*'])
def test_validate_rejects_invalid_characters_in_the_log_filename(
        widget, tmp_path, bad):
    """The log file is a bare name, not a path."""
    widget.set_values(str(tmp_path), "logs", f"app{bad}.log")
    problems = widget.validate()
    assert any("log filename must not contain" in p.lower() for p in problems)


def test_validate_reports_every_character_problem_at_once(widget, tmp_path):
    """A broken folder *and* a broken file name give two messages."""
    widget.set_values(str(tmp_path), "lo|gs", "a<b.log")
    assert len(widget.validate()) == 2


def test_validate_reports_a_null_byte_in_the_base_path(widget, tmp_path):
    """A NUL can never reach the file system layer."""
    widget.set_values(f"{tmp_path}/a\0b", "logs", "a.log")
    problems = widget.validate()
    assert problems == ["Base path contains a null character."]


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: a NUL byte in the folder name or log file name "
                          "is not reported; the status line claims the path is "
                          "usable",
                   strict=False)
@pytest.mark.parametrize("folder, filename", [
    ("lo\0gs", "app.log"),
    ("logs", "ap\0p.log"),
])
def test_validate_reports_a_null_byte_in_the_folder_or_the_filename(
        widget, tmp_path, folder, filename):
    """validate() promises every problem that would make logging fail."""
    widget.set_values(str(tmp_path), folder, filename)
    assert widget.validate() != []


def test_validate_stops_before_touching_the_file_system(widget):
    """A character problem short-circuits: no message about the location."""
    widget.set_values("/nonexistent<dir", "logs", "a.log")
    problems = widget.validate()
    assert len(problems) == 1
    assert "invalid characters" in problems[0]


def test_validate_reports_a_base_path_that_is_really_a_file(widget, tmp_path):
    """Logging into a file-as-a-directory must be refused."""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("data", encoding="utf-8")
    widget.set_values(str(blocker), "logs", "a.log")
    problems = widget.validate()
    assert problems == [f"'{blocker}' is a file, not a directory."]


def test_validate_reports_a_log_folder_that_is_really_a_file(widget, tmp_path):
    """The same check applies to the folder component itself."""
    blocker = tmp_path / "logs"
    blocker.write_text("data", encoding="utf-8")
    widget.set_values(str(tmp_path), "logs", "a.log")
    assert widget.validate() == [f"'{blocker}' is a file, not a directory."]


def test_validate_accepts_a_directory_that_does_not_exist_yet(widget, tmp_path):
    """A deep new path is fine as long as an existing ancestor is writable."""
    widget.set_values(str(tmp_path / "a" / "b" / "c"), "logs", "a.log")
    assert widget.validate() == []
    assert not (tmp_path / "a").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_validate_reports_a_directory_it_cannot_write_into(widget, tmp_path):
    """A read-only parent is reported before anything is written."""
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        widget.set_values(str(locked), "logs", "a.log")
        problems = widget.validate()
        assert problems == [f"No permission to write into '{locked}'."]
    finally:
        os.chmod(locked, 0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_validate_reports_the_permission_error_raised_while_creating(
        widget, tmp_path):
    """check_writable turns the failed mkdir into a readable message."""
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        widget.set_values(str(locked), "logs", "a.log")
        problems = widget.validate(check_writable=True)
        assert problems == [
            f"No permission to create or write into '{locked / 'logs'}'."]
    finally:
        os.chmod(locked, 0o700)


def test_validate_with_check_writable_creates_the_directory(widget, tmp_path):
    """Saving really prepares the location - and cleans up its probe file."""
    target = tmp_path / "deep" / "nested"
    widget.set_values(str(target), "logs", "a.log")
    assert widget.validate(check_writable=True) == []
    created = target / "logs"
    assert created.is_dir()
    assert list(created.iterdir()) == []


def test_validate_without_check_writable_creates_nothing(widget, tmp_path):
    """The quick check must stay side-effect free (it runs on every keystroke)."""
    target = tmp_path / "untouched"
    widget.set_values(str(target), "logs", "a.log")
    assert widget.validate() == []
    assert not target.exists()


def test_is_valid_mirrors_validate(widget, tmp_path):
    """is_valid() is just the boolean view of the quick check."""
    widget.set_values(str(tmp_path), "lo|gs", "a.log")
    assert widget.is_valid() is False


# ===========================================================================
# LogDirectoryWidget - live status label
# ===========================================================================

def test_status_label_confirms_a_usable_path(widget, tmp_path):
    """A good path gets the green confirmation line."""
    widget.set_values(str(tmp_path), "lg", "a.log")
    assert widget._status_label.text() == "✓ Path is usable"
    assert "4CAF50" in widget._status_label.styleSheet()


def test_status_label_shows_the_problem_while_typing(widget, tmp_path):
    """Typing an impossible folder name warns immediately, in red."""
    widget.set_values(str(tmp_path), "lg", "a.log")
    widget._folder_input.setText("lo|gs")
    assert widget._status_label.text().startswith("⚠")
    assert "folder name must not contain" in widget._status_label.text().lower()
    assert "ff6b6b" in widget._status_label.styleSheet()


def test_status_label_recovers_when_the_path_is_fixed(widget, tmp_path):
    """The warning disappears again once the value is corrected."""
    widget.set_values(str(tmp_path), "lo|gs", "a.log")
    assert widget._status_label.text().startswith("⚠")
    widget._folder_input.setText("logs")
    assert widget._status_label.text() == "✓ Path is usable"


# ===========================================================================
# SettingsTab - construction and navigation
# ===========================================================================

def test_all_four_category_panels_are_built(tab):
    """One scrollable panel per category, in the order of the list."""
    assert tab._category_list.count() == 4
    assert tab._stack.count() == 4
    assert all(isinstance(tab._stack.widget(i), QScrollArea) for i in range(4))


def test_the_first_category_is_selected_with_its_heading(tab):
    """Appearance is shown on open, and the heading names it."""
    assert tab._category_list.currentRow() == 0
    assert tab._stack.currentIndex() == 0
    assert tab._panel_title.text() == "🎨  Appearance"


@pytest.mark.parametrize("row, heading", [
    (0, "🎨  Appearance"),
    (1, "⚙  General"),
    (2, "📋  Logging"),
    (3, "ℹ  About"),
])
def test_selecting_a_category_switches_the_stack_and_the_heading(
        tab, row, heading):
    """The list, the stacked panel and the heading stay in step."""
    tab._category_list.setCurrentRow(row)
    assert tab._stack.currentIndex() == row
    assert tab._panel_title.text() == heading


@pytest.mark.parametrize("row", [-1, 4, 99])
def test_an_out_of_range_category_keeps_the_current_panel(tab, row):
    """A stray row index must not raise or blank the heading."""
    tab._category_list.setCurrentRow(2)
    tab._on_category_selected(row)
    assert tab._stack.currentIndex() == 2
    assert tab._panel_title.text() == "📋  Logging"


def test_select_category_by_name_shows_that_panel(tab):
    """The internal helper used by the error paths finds the right row."""
    tab._select_category("Logging")
    assert tab._category_list.currentRow() == 2


def test_select_category_ignores_an_unknown_name(tab):
    """An unknown category name leaves the selection alone."""
    tab._category_list.setCurrentRow(1)
    tab._select_category("Nonexistent")
    assert tab._category_list.currentRow() == 1


# ===========================================================================
# SettingsTab - loading
# ===========================================================================

def test_load_all_settings_shows_the_stored_theme_and_checkbox(
        qapp, isolated_settings, isolated_logging_config, app_dir, dialogs):
    """Stored values are visible the moment the tab is built."""
    isolated_settings.set("theme", "Purple", category="general")
    isolated_settings.set("show_group_management_warning", False,
                          category="general")
    built = SettingsTab()
    try:
        assert built.theme_combo.currentText() == "Purple"
        assert built.warn_on_no_creds.isChecked() is False
    finally:
        built.deleteLater()


def test_load_all_settings_ignores_a_theme_that_does_not_exist(
        qapp, isolated_settings, isolated_logging_config, app_dir, dialogs):
    """A hand-edited, unknown theme must not blank the combo box."""
    isolated_settings.set("theme", "Neon", category="general")
    built = SettingsTab()
    try:
        assert built.theme_combo.currentText() == \
            DEFAULT_SETTINGS["general"]["theme"]
    finally:
        built.deleteLater()


def test_logging_panel_mirrors_the_stored_logging_configuration(
        qapp, isolated_settings, isolated_logging_config, app_dir, dialogs,
        tmp_path):
    """Bytes become MB, and the directory is split across the three fields."""
    isolated_settings.update_logging_config(
        log_level="ERROR", max_bytes=7 * 1024 * 1024, backup_count=2,
        file_enabled=False, console_enabled=False,
        log_dir=str(tmp_path / "store" / "lg"), log_file="own.log",
    )
    built = SettingsTab()
    try:
        assert built.log_level_combo.currentText() == "ERROR"
        assert built.max_size_spin.value() == 7
        assert built.backup_count_spin.value() == 2
        assert built.file_logging_check.isChecked() is False
        assert built.console_logging_check.isChecked() is False
        assert built.log_dir_widget.get_base_path() == str(tmp_path / "store")
        assert built.log_dir_widget.get_folder_name() == "lg"
        assert built.log_dir_widget.get_log_filename() == "own.log"
    finally:
        built.deleteLater()


@pytest.mark.parametrize("stored, shown", [
    (1024, 1),                      # below the 1 MB minimum of the spin box
    (1024 * 1024, 1),
    (500 * 1024 * 1024, 500),       # exactly the maximum
    (999 * 1024 * 1024, 500),       # clamped to the maximum
])
def test_reload_clamps_the_max_size_into_the_spinbox_range(
        tab, isolated_settings, stored, shown):
    """The spin box covers 1..500 MB; stored values are clamped into it."""
    isolated_settings.update_logging_config(max_bytes=stored)
    assert tab.max_size_spin.value() == shown


def test_reload_general_settings_discards_unsaved_ui_changes(
        tab, isolated_settings):
    """A reload is a re-read of the store, not a merge with the UI."""
    isolated_settings.set("show_group_management_warning", True,
                          category="general")
    tab.warn_on_no_creds.setChecked(False)
    tab._reload_general_settings()
    assert tab.warn_on_no_creds.isChecked() is True


def test_an_external_general_change_refreshes_the_checkbox(tab,
                                                           isolated_settings):
    """The warning dialog can untick the box; the tab must follow."""
    assert tab.warn_on_no_creds.isChecked() is True
    isolated_settings.set("show_group_management_warning", False,
                          category="general")
    assert tab.warn_on_no_creds.isChecked() is False


def test_an_external_logging_change_refreshes_the_logging_panel(
        tab, isolated_settings):
    """Anything that updates the logging category is mirrored in the panel."""
    isolated_settings.update_logging_config(log_level="DEBUG", backup_count=1)
    assert tab.log_level_combo.currentText() == "DEBUG"
    assert tab.backup_count_spin.value() == 1


# ===========================================================================
# SettingsTab - saving
# ===========================================================================

@pytest.mark.integration
def test_save_all_settings_persists_every_panel_value(tab, isolated_settings,
                                                      dialogs, tmp_path):
    """One click writes appearance, general and logging to the JSON file."""
    tab.theme_combo.setCurrentText("Blue")
    tab.warn_on_no_creds.setChecked(False)
    tab.log_level_combo.setCurrentText("DEBUG")
    tab.max_size_spin.setValue(7)
    tab.backup_count_spin.setValue(3)
    tab.file_logging_check.setChecked(True)
    tab.console_logging_check.setChecked(False)
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")

    tab.save_all_settings()

    stored = on_disk(isolated_settings)
    assert stored["general"]["theme"] == "Blue"
    assert stored["general"]["show_group_management_warning"] is False
    assert stored["logging"]["log_level"] == "DEBUG"
    assert stored["logging"]["max_bytes"] == 7 * 1024 * 1024
    assert stored["logging"]["backup_count"] == 3
    assert stored["logging"]["console_enabled"] is False
    assert stored["logging"]["log_dir"] == str(tmp_path / "store" / "lg")
    assert stored["logging"]["log_file"] == "own.log"
    assert dialogs.saw("saved successfully")


@pytest.mark.integration
def test_save_keeps_the_checkbox_value_the_user_just_changed(
        tab, isolated_settings, dialogs, tmp_path):
    """
    Regression: writing the theme first re-loads the general panel, which used
    to overwrite the unsaved checkbox with its old persisted value.
    """
    isolated_settings.set("show_group_management_warning", True,
                          category="general")
    tab.warn_on_no_creds.setChecked(False)
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")

    tab.save_all_settings()

    assert isolated_settings.get_bool("show_group_management_warning",
                                      category="general") is False
    assert tab.warn_on_no_creds.isChecked() is False


@pytest.mark.integration
def test_save_reports_failure_when_the_logging_config_is_rejected(
        tab, isolated_settings, dialogs, tmp_path):
    """A log file name SettingsManager refuses must not be reported as saved."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "..")

    tab.save_all_settings()

    assert dialogs.saw("could not be saved")
    assert not dialogs.saw("saved successfully")
    assert isolated_settings.get_logging_config()["log_file"] == \
        LOG_DEFAULTS["log_file"]


def test_save_reports_an_unexpected_error_instead_of_crashing(
        tab, isolated_settings, dialogs, monkeypatch, tmp_path):
    """Any exception while writing becomes a critical dialog, not a traceback."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tab.settings, "set", boom)
    tab.save_all_settings()

    assert dialogs.kinds() == ["critical"]
    assert "disk on fire" in dialogs.texts()[0]


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: save_all_settings() ignores the False returned "
                          "by SettingsManager.set() and still claims the other "
                          "settings were saved",
                   strict=False)
@pytest.mark.integration
def test_save_does_not_claim_success_when_the_settings_file_is_unwritable(
        tab, isolated_settings, dialogs, tmp_path):
    """Nothing reached the disk, so nothing may be reported as saved."""
    logs_root = tmp_path / "logs_root"
    logs_root.mkdir()
    tab.log_dir_widget.set_values(str(logs_root), "lg", "own.log")
    tab.theme_combo.setCurrentText("Green")

    os.chmod(tmp_path, 0o500)
    try:
        tab.save_all_settings()
    finally:
        os.chmod(tmp_path, 0o700)

    assert not isolated_settings.settings_file.exists()
    assert not dialogs.saw("The other settings were saved")


# ===========================================================================
# SettingsTab - _ensure_usable_log_path
# ===========================================================================

def test_ensure_usable_log_path_accepts_a_valid_location(tab, dialogs, tmp_path):
    """A writable directory passes silently, without any dialog."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")
    assert tab._ensure_usable_log_path(True) is True
    assert dialogs.calls == []
    assert (tmp_path / "store" / "lg").is_dir()


def test_ensure_usable_log_path_creates_nothing_when_file_logging_is_off(
        tab, dialogs, tmp_path):
    """Without file logging the check stays read-only: no directory is made."""
    target = tmp_path / "store"
    tab.log_dir_widget.set_values(str(target), "lg", "own.log")
    assert tab._ensure_usable_log_path(False) is True
    assert dialogs.calls == []
    assert not target.exists()


def test_ensure_usable_log_path_falls_back_to_the_default_location(
        tab, dialogs, choose_button, app_dir):
    """'Use the default location' repairs the widget and lets the save go on."""
    choose_button("default location")
    tab.log_dir_widget.set_values("/broken<path", "lg", "own.log")

    assert tab._ensure_usable_log_path(True) is True
    assert tab.log_dir_widget.get_log_dir() == str(app_dir / "logs")
    assert dialogs.saw("The configured log directory cannot be used")


def test_ensure_usable_log_path_stops_and_opens_the_logging_panel(
        tab, dialogs, choose_button):
    """'Let me fix it' aborts the save and shows the offending panel."""
    choose_button("fix it")
    tab._category_list.setCurrentRow(0)
    tab.log_dir_widget.set_values("/broken<path", "lg", "own.log")

    assert tab._ensure_usable_log_path(True) is False
    assert tab._category_list.currentRow() == 2
    assert tab._stack.currentIndex() == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_ensure_usable_log_path_reports_an_unusable_default_location(
        tab, dialogs, choose_button, app_dir):
    """When even the default cannot be created the user is told, not fooled."""
    choose_button("default location")
    tab.log_dir_widget.set_values("/broken<path", "lg", "own.log")
    os.chmod(app_dir, 0o500)
    try:
        assert tab._ensure_usable_log_path(True) is False
    finally:
        os.chmod(app_dir, 0o700)
    assert dialogs.saw("Even the default log location cannot be used")


@pytest.mark.parametrize("answer, expected", [
    (QMessageBox.StandardButton.Yes, True),
    (QMessageBox.StandardButton.No, False),
])
def test_ensure_usable_log_path_only_warns_when_file_logging_is_off(
        tab, dialogs, answer, expected):
    """Without file logging an unusable path is a question, not a blocker."""
    dialogs.warning_answer = answer
    tab.log_dir_widget.set_values("/broken<path", "lg", "own.log")

    assert tab._ensure_usable_log_path(False) is expected
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("Save anyway?")


@pytest.mark.integration
def test_save_is_abandoned_when_the_user_wants_to_fix_the_path(
        tab, isolated_settings, dialogs, choose_button):
    """Nothing at all is written while the log path is still unusable."""
    choose_button("fix it")
    isolated_settings.set("theme", "Dark", category="general")
    tab.theme_combo.setCurrentText("Warm")
    tab.log_dir_widget.set_values("/broken<path", "lg", "own.log")

    tab.save_all_settings()

    assert isolated_settings.get_str("theme", category="general") == "Dark"
    assert not dialogs.saw("saved successfully")


# ===========================================================================
# SettingsTab - reset buttons
# ===========================================================================

def test_log_level_reset_button_restores_info(tab):
    """The documented default log level is INFO."""
    tab.log_level_combo.setCurrentText("CRITICAL")
    button = reset_button_next_to(tab.log_level_combo)
    assert "INFO" in button.toolTip()
    button.click()
    assert tab.log_level_combo.currentText() == LOG_DEFAULTS["log_level"]


def test_max_size_reset_button_restores_ten_megabytes(tab):
    """The documented default rotation size is 10 MB."""
    tab.max_size_spin.setValue(42)
    button = reset_button_next_to(tab.max_size_spin)
    assert "10 MB" in button.toolTip()
    button.click()
    assert tab.max_size_spin.value() * 1024 * 1024 == LOG_DEFAULTS["max_bytes"]


def test_backup_count_reset_button_restores_five(tab):
    """The documented default backup count is 5."""
    tab.backup_count_spin.setValue(0)
    button = reset_button_next_to(tab.backup_count_spin)
    assert "(5)" in button.toolTip()
    button.click()
    assert tab.backup_count_spin.value() == LOG_DEFAULTS["backup_count"]


@pytest.mark.parametrize("attribute, key", [
    ("file_logging_check", "file_enabled"),
    ("console_logging_check", "console_enabled"),
])
def test_output_target_reset_buttons_restore_enabled(tab, attribute, key):
    """Both output targets are enabled by default."""
    checkbox = getattr(tab, attribute)
    checkbox.setChecked(not LOG_DEFAULTS[key])
    button = reset_button_next_to(checkbox)
    assert "enabled" in button.toolTip()
    button.click()
    assert checkbox.isChecked() is LOG_DEFAULTS[key]


def test_reset_buttons_do_not_write_to_the_settings_file(tab,
                                                         isolated_settings):
    """A reset only changes the UI; nothing is persisted before Save."""
    tab.log_level_combo.setCurrentText("CRITICAL")
    reset_button_next_to(tab.log_level_combo).click()
    assert isolated_settings.get_logging_config()["log_level"] == \
        LOG_DEFAULTS["log_level"]


# ===========================================================================
# SettingsTab - reset to defaults
# ===========================================================================

@pytest.mark.integration
def test_reset_to_defaults_restores_every_panel_after_confirmation(
        tab, isolated_settings, dialogs, app_dir):
    """Confirming the question resets the store and reloads all panels."""
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    isolated_settings.set("theme", "Green", category="general")
    isolated_settings.update_logging_config(log_level="CRITICAL",
                                            backup_count=1)

    tab.reset_to_defaults()

    assert tab.theme_combo.currentText() == DEFAULT_SETTINGS["general"]["theme"]
    assert tab.log_level_combo.currentText() == LOG_DEFAULTS["log_level"]
    assert tab.backup_count_spin.value() == LOG_DEFAULTS["backup_count"]
    assert tab.log_dir_widget.get_log_dir() == str(app_dir / "logs")
    assert dialogs.saw("Settings reset to defaults")


def test_reset_to_defaults_does_nothing_when_the_user_declines(
        tab, isolated_settings, dialogs):
    """Answering No leaves both the store and the UI untouched."""
    dialogs.question_answer = QMessageBox.StandardButton.No
    isolated_settings.set("theme", "Green", category="general")
    tab.theme_combo.setCurrentText("Warm")

    tab.reset_to_defaults()

    assert isolated_settings.get_str("theme", category="general") == "Green"
    assert tab.theme_combo.currentText() == "Warm"
    assert dialogs.kinds() == ["question"]


# ===========================================================================
# SettingsTab - themes
# ===========================================================================

def test_apply_theme_repaints_the_application_and_stores_the_choice(
        tab, isolated_settings, qapp):
    """'Apply Now' changes the palette immediately and remembers the theme."""
    tab.theme_combo.setCurrentText("Light")
    tab.apply_theme()
    palette = QApplication.palette()
    assert palette.color(QPalette.ColorRole.Window) == \
        SettingsTab.THEMES["Light"]["window"]
    assert isolated_settings.get_str("theme", category="general") == "Light"


def test_apply_theme_ignores_a_theme_that_is_not_defined(
        tab, isolated_settings, qapp, dialogs):
    """An unknown name is a no-op: no palette change, nothing persisted."""
    before = QApplication.palette().color(QPalette.ColorRole.Window)
    tab.theme_combo.addItem("Neon")
    tab.theme_combo.setCurrentText("Neon")
    tab.apply_theme()
    assert QApplication.palette().color(QPalette.ColorRole.Window) == before
    assert isolated_settings.get_str("theme", category="general") != "Neon"
    assert dialogs.calls == []


def test_a_swatch_button_only_selects_the_theme(tab, isolated_settings, qapp):
    """Clicking a colour swatch pre-selects it; applying is a separate step."""
    before = QApplication.palette().color(QPalette.ColorRole.Window)
    swatch = next(b for b in tab.findChildren(QPushButton) if b.text() == "Purple")
    swatch.click()
    assert tab.theme_combo.currentText() == "Purple"
    assert QApplication.palette().color(QPalette.ColorRole.Window) == before


@pytest.mark.parametrize("name", list(SettingsTab.THEMES))
def test_every_theme_defines_the_colours_the_palette_needs(tab, name, qapp):
    """Each theme can be applied without a KeyError and paints the window."""
    tab._apply_theme(name)
    palette = QApplication.palette()
    assert palette.color(QPalette.ColorRole.Window) == \
        SettingsTab.THEMES[name]["window"]
    assert palette.color(QPalette.ColorRole.Highlight) == \
        SettingsTab.THEMES[name]["highlight"]


# ===========================================================================
# SettingsTab - logging handlers
# ===========================================================================

@pytest.mark.integration
def test_saving_re_initialises_the_logging_handlers(tab, dialogs, tmp_path):
    """After a save the root logger really writes to the configured file."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")
    tab.log_level_combo.setCurrentText("WARNING")
    tab.file_logging_check.setChecked(True)
    tab.console_logging_check.setChecked(False)

    tab.save_all_settings()

    root = logging.getLogger()
    files = [h for h in root.handlers if hasattr(h, "baseFilename")]
    assert len(files) == 1
    assert Path(files[0].baseFilename) == tmp_path / "store" / "lg" / "own.log"
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1


@pytest.mark.integration
def test_disabling_file_logging_leaves_no_file_handler(tab, dialogs, tmp_path):
    """With the file target off, nothing is written to disk."""
    target = tmp_path / "store"
    tab.log_dir_widget.set_values(str(target), "lg", "own.log")
    tab.file_logging_check.setChecked(False)
    tab.console_logging_check.setChecked(True)
    dialogs.warning_answer = QMessageBox.StandardButton.Yes

    tab.save_all_settings()

    root = logging.getLogger()
    assert [h for h in root.handlers if hasattr(h, "baseFilename")] == []
    assert not (target / "lg" / "own.log").exists()


@pytest.mark.integration
def test_saving_keeps_the_shared_logging_config_in_sync(
        tab, dialogs, isolated_logging_config, tmp_path):
    """The Logs tab reads LoggingConfig, so it must follow the new location."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")
    tab.backup_count_spin.setValue(2)

    tab.save_all_settings()

    assert isolated_logging_config.config["log_dir"] == \
        str(tmp_path / "store" / "lg")
    assert isolated_logging_config.config["log_file"] == "own.log"
    assert isolated_logging_config.config["backup_count"] == 2
    written = json.loads(
        Path(isolated_logging_config.config_file).read_text(encoding="utf-8"))
    assert written["log_file"] == "own.log"


@pytest.mark.integration
def test_saving_twice_leaves_exactly_one_file_handler(tab, dialogs, tmp_path):
    """Saving is idempotent: handlers are replaced, never stacked up."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")
    tab.file_logging_check.setChecked(True)
    tab.console_logging_check.setChecked(True)

    tab.save_all_settings()
    first = on_disk(tab.settings)["logging"]
    tab.save_all_settings()

    assert on_disk(tab.settings)["logging"] == first
    root = logging.getLogger()
    assert len([h for h in root.handlers if hasattr(h, "baseFilename")]) == 1
    assert len(root.handlers) == 2


@pytest.mark.integration
def test_the_save_button_writes_the_settings(tab, isolated_settings, dialogs,
                                             tmp_path):
    """The green button at the bottom is wired to save_all_settings()."""
    tab.log_dir_widget.set_values(str(tmp_path / "store"), "lg", "own.log")
    tab.theme_combo.setCurrentText("Warm")
    button = next(b for b in tab.findChildren(QPushButton)
                  if "Save All Settings" in b.text())
    button.click()
    assert on_disk(isolated_settings)["general"]["theme"] == "Warm"


def test_the_reset_button_asks_before_resetting(tab, isolated_settings,
                                                dialogs):
    """The reset button is wired to the confirmation question."""
    dialogs.question_answer = QMessageBox.StandardButton.No
    button = next(b for b in tab.findChildren(QPushButton)
                  if "Reset to Defaults" in b.text())
    button.click()
    assert dialogs.kinds() == ["question"]
    assert dialogs.saw("cannot be undone")


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: _setup_logging_handlers() clears the root "
                          "handlers without closing them, leaking the open log "
                          "file",
                   strict=False)
def test_replacing_the_handlers_closes_the_previous_log_file(tab, tmp_path):
    """Re-applying the configuration must not leak the old file handle."""
    tab.settings.update_logging_config(log_dir=str(tmp_path / "one"),
                                       log_file="one.log")
    previous = [h for h in logging.getLogger().handlers
                if hasattr(h, "baseFilename")]
    assert previous, "expected a file handler to start from"

    tab.settings.update_logging_config(log_dir=str(tmp_path / "two"),
                                       log_file="two.log")

    stream = previous[0].stream
    assert stream is None or stream.closed
