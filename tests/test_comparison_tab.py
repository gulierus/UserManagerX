"""
Unit tests for the *Comparison and Synchronization* tab.

Modules under test
------------------
``ui.comparison_tab``            - :class:`SourcePanel`, :class:`ComparisonTab`
``ui.class_operations_dialogs``  - shift / analyse / numeral-conversion dialogs
``ui.class_template_widget``     - the template chooser with live preview
``ui.analysis_widgets``          - issue rendering and fix requests
``ui.merge_dialog``              - strategy, preview and pre-flight analysis

Everything here builds **real Qt widgets** (``QT_QPA_PLATFORM=offscreen`` is set
by ``conftest``), so every test that can reach a modal window requests the
``dialogs`` fixture - and the ones that open a nested ``QDialog`` additionally
request ``accept_dialogs``.  Without them a single ``QMessageBox.exec()`` would
block the whole suite.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour of a defect that
is still present in the production code.
"""

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QInputDialog, QLabel, QMessageBox, QTextEdit,
)

from models import Class, Person
from ui.analysis_widgets import AnalysisPanel, IssueWidget
from ui.class_operations_dialogs import (
    ClassNumeralConversionDialog, ClassShiftDialog, SourceAnalysisDialog,
)
from ui.class_template_widget import ClassTemplateWidget
from ui.comparison_tab import ComparisonTab, SourcePanel
from ui.merge_dialog import MergeSourcesDialog
from utils.class_name_utils import TEMPLATE_ARABIC, TEMPLATE_ROMAN
from utils.source_analysis import (
    AnalysisReport, FixOption, IssueSeverity, ParameterKind, SourceIssue,
)

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def queue_text(monkeypatch, *answers):
    """
    Feed ``QInputDialog.getText`` a fixed sequence of answers.

    Each answer is either a plain string (accepted) or an explicit
    ``(text, ok)`` tuple.  Once the queue is exhausted every further call
    reports "cancelled", which keeps a retry loop from spinning forever.

    Returns:
        The list of ``(title, label, prefilled)`` triples that were asked.
    """
    remaining = list(answers)
    asked = []

    def _get_text(*args, **kwargs):
        asked.append((
            args[1] if len(args) > 1 else "",
            args[2] if len(args) > 2 else "",
            args[4] if len(args) > 4 else None,
        ))
        if not remaining:
            return ("", False)
        answer = remaining.pop(0)
        return answer if isinstance(answer, tuple) else (answer, True)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(_get_text))
    return asked


def fake_property_editor(monkeypatch, changes=None, accepted=True):
    """
    Replace :class:`~ui.property_editor.PropertyEditorDialog` with a stub.

    The stub applies *changes* to the person it was given and then reports the
    requested dialog result, which is exactly what the real editor does after
    the user pressed OK - without building a 700-line dialog.

    Returns:
        A dict that receives the constructor arguments of the last instance.
    """
    captured = {}

    class _FakeEditor:
        def __init__(self, person, existing_usernames, available_groups=None,
                     available_templates=None, parent=None):
            captured["person"] = person
            captured["existing_usernames"] = existing_usernames
            captured["parent"] = parent
            self._person = person

        def exec(self):
            if not accepted:
                return QDialog.DialogCode.Rejected
            for attribute, value in (changes or {}).items():
                setattr(self._person, attribute, value)
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("ui.comparison_tab.PropertyEditorDialog", _FakeEditor)
    return captured


def tree_snapshot(panel):
    """``[(class name, [person labels])]`` exactly as the tree renders it."""
    tree = panel.tree
    return [
        (
            tree.topLevelItem(index).text(0),
            [tree.topLevelItem(index).child(child).text(0)
             for child in range(tree.topLevelItem(index).childCount())],
        )
        for index in range(tree.topLevelItemCount())
    ]


def class_item(panel, name):
    """Return the top level tree item rendering the class called *name*."""
    tree = panel.tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.text(0) == name:
            return item
    raise AssertionError(f"class {name!r} not in the tree: {tree_snapshot(panel)}")


def select_item(panel, item):
    """Make *item* the current and only selected row of the panel's tree."""
    panel.tree.clearSelection()
    panel.tree.setCurrentItem(item)
    item.setSelected(True)
    return item


def source_names(panel):
    """Every entry of the panel's source combo box, in order."""
    return [panel.source_combo.itemText(i) for i in range(panel.source_combo.count())]


def class_names(source):
    """Names of every class of *source*, in order."""
    return [cls.name for cls in source.classes]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def build_source(make_source, make_class, make_person):
    """
    Factory: ``build_source([("6.A", [("Jan", "Novák")])])``.

    A student entry is ``(first, last)`` - the stored ``class_name`` then
    matches the class it lives in - or ``(first, last, class_name, **kwargs)``
    to build the mismatching records the analysis is meant to find.
    """
    def _build(spec, name="Src", readonly=False, source_type="manual"):
        source = make_source(name, [], source_type=source_type, readonly=readonly)
        for class_name, people in spec:
            persons = []
            for entry in people:
                stored = entry[2] if len(entry) > 2 else class_name
                extra = entry[3] if len(entry) > 3 else {}
                persons.append(make_person(entry[0], entry[1], stored, **extra))
            source.add_class(make_class(class_name, persons=persons))
        return source
    return _build


@pytest.fixture
def panel_for(source_manager):
    """Factory for a :class:`SourcePanel` already showing *source*."""
    def _make(source=None, allow_edit=True, title="Output Source"):
        panel = SourcePanel(title, source_manager, allow_edit=allow_edit)
        if source is not None:
            if not any(existing is source for existing in source_manager.sources):
                source_manager.add_source(source)
            panel.refresh_sources()
            panel.source_combo.setCurrentText(source.name)
            assert panel.current_source is source
        return panel
    return _make


@pytest.fixture
def tab(qapp, source_manager):
    """A fully built :class:`ComparisonTab` on an empty source manager."""
    return ComparisonTab(source_manager)


@pytest.fixture
def loaded_tab(tab, source_manager, build_source):
    """Comparison tab with 'L' selected left and 'R' selected right."""
    left = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])], name="L")
    right = build_source([("6.A", [("Jan", "Novák"), ("Petr", "Dvořák")])], name="R")
    source_manager.add_source(left)
    source_manager.add_source(right)
    tab.left_panel.source_combo.setCurrentText("L")
    tab.right_panel.source_combo.setCurrentText("R")
    assert tab.left_panel.current_source is left
    assert tab.right_panel.current_source is right
    return tab, left, right


# ===========================================================================
# SourcePanel - source plumbing
# ===========================================================================

def test_a_fresh_panel_shows_only_the_placeholder_until_refresh_sources_runs(
        source_manager, build_source):
    """The constructor does not read the manager - refresh_sources does."""
    source_manager.add_source(build_source([], name="Alpha"))
    panel = SourcePanel("Left Source", source_manager)
    assert source_names(panel) == ["(Select Source)"]
    panel.refresh_sources()
    assert source_names(panel) == ["(Select Source)", "Alpha"]


def test_refresh_sources_keeps_the_selected_source_when_it_still_exists(
        source_manager, build_source, panel_for):
    """Adding another source must not disturb the current selection."""
    first = build_source([("6.A", [("Jan", "Novák")])], name="Alpha")
    panel = panel_for(first)
    source_manager.add_source(build_source([], name="Beta"))

    assert panel.source_combo.currentText() == "Alpha"
    assert panel.current_source is first
    assert source_names(panel) == ["(Select Source)", "Alpha", "Beta"]
    assert tree_snapshot(panel) == [("6.A", ["Jan Novák"])]


def test_refresh_sources_clears_a_selection_whose_source_disappeared(
        source_manager, build_source, panel_for):
    """Removing the displayed source empties the tree and the statistics."""
    source = build_source([("6.A", [("Jan", "Novák")])], name="Alpha")
    panel = panel_for(source)

    source_manager.remove_source(source)

    assert panel.source_combo.currentText() == "(Select Source)"
    assert panel.current_source is None
    assert tree_snapshot(panel) == []
    assert panel.stats_label.text() == ""


def test_refresh_sources_is_idempotent(source_manager, build_source, panel_for):
    """Calling it twice must not duplicate entries or change the selection."""
    panel = panel_for(build_source([], name="Alpha"))
    panel.refresh_sources()
    panel.refresh_sources()
    assert source_names(panel) == ["(Select Source)", "Alpha"]
    assert panel.source_combo.currentText() == "Alpha"


def test_selecting_the_placeholder_again_releases_the_source(
        build_source, panel_for):
    """Going back to '(Select Source)' clears everything the panel showed."""
    panel = panel_for(build_source([("6.A", [("Jan", "Novák")])]))
    panel.source_combo.setCurrentText("(Select Source)")
    assert panel.current_source is None
    assert tree_snapshot(panel) == []
    assert panel.stats_label.text() == ""


# ===========================================================================
# SourcePanel - the tree
# ===========================================================================

def test_refresh_tree_without_a_filter_shows_every_class_and_counts_totals(
        build_source, panel_for):
    """All classes are listed, empty ones included, with a total in the label."""
    panel = panel_for(build_source([
        ("6.A", [("Jan", "Novák"), ("Eva", "Malá")]),
        ("7.B", []),
    ]))
    assert tree_snapshot(panel) == [("6.A", ["Jan Novák", "Eva Malá"]), ("7.B", [])]
    assert panel.stats_label.text() == "Total: 2 classes, 2 persons"


def test_refresh_tree_marks_a_read_only_source_in_the_statistics(
        build_source, panel_for):
    """The read-only flag is spelled out next to the totals."""
    panel = panel_for(build_source([("6.A", [("Jan", "Novák")])], readonly=True))
    assert panel.stats_label.text() == "Total: 1 classes, 1 persons • read-only"


def test_refresh_tree_labels_a_class_without_a_name(build_source, panel_for):
    """An empty class name is rendered as '(unnamed class)', not as nothing."""
    panel = panel_for(build_source([("", [("Jan", "Novák", "")])]))
    assert tree_snapshot(panel) == [("(unnamed class)", ["Jan Novák"])]


def test_refresh_tree_stores_the_model_objects_on_the_items(
        build_source, panel_for):
    """Class items carry the Class, person items carry the Person."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    item = class_item(panel, "6.A")
    stored_class = item.data(0, Qt.ItemDataRole.UserRole)
    stored_person = item.child(0).data(0, Qt.ItemDataRole.UserRole)
    assert isinstance(stored_class, Class) and stored_class is source.classes[0]
    assert isinstance(stored_person, Person)
    assert stored_person is source.classes[0].persons[0]


def test_refresh_tree_with_a_filter_hides_classes_without_a_match(
        build_source, panel_for):
    """Only matching students survive, and empty classes vanish completely."""
    panel = panel_for(build_source([
        ("6.A", [("Jan", "Novák"), ("Eva", "Malá")]),
        ("7.B", [("Petr", "Dvořák")]),
    ]))
    panel.search_input.setText("nov")
    assert tree_snapshot(panel) == [("6.A", ["Jan Novák"])]
    assert panel.stats_label.text() == "Showing 1 persons (filtered)"


@pytest.mark.parametrize("query", ["novakova", "NOVÁKOVÁ", "Šárka", "sarka"])
def test_the_search_filter_ignores_case_and_diacritics(
        build_source, panel_for, query):
    """'Šárka Nováková' is found by every sensible spelling of her name."""
    panel = panel_for(build_source([("6.A", [("Šárka", "Nováková")])]))
    panel.search_input.setText(query)
    assert tree_snapshot(panel) == [("6.A", ["Šárka Nováková"])]


def test_an_unmatched_filter_empties_the_tree_but_keeps_the_source(
        build_source, panel_for):
    """A filter nobody matches hides every class - the source stays selected."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    panel.search_input.setText("zzz")
    assert tree_snapshot(panel) == []
    assert panel.stats_label.text() == "Showing 0 persons (filtered)"
    assert panel.current_source is source


def test_the_search_filter_survives_an_external_modification(
        source_manager, build_source, panel_for, make_person):
    """A modification signal repaints the tree *through* the active filter."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    panel.search_input.setText("nov")

    source.classes[0].add_person(make_person("Petr", "Dvořák", "6.A"))
    source_manager.notify_source_modified(source.name)

    assert tree_snapshot(panel) == [("6.A", ["Jan Novák"])]
    assert panel.stats_label.text() == "Showing 1 persons (filtered)"


def test_a_modification_of_another_source_leaves_the_panel_alone(
        source_manager, build_source, panel_for):
    """Only the source the panel displays triggers a repaint."""
    shown = build_source([("6.A", [("Jan", "Novák")])], name="Alpha")
    panel = panel_for(shown)
    shown.classes[0].persons.clear()          # change the model behind its back

    source_manager.notify_source_modified("SomeOtherSource")

    assert tree_snapshot(panel) == [("6.A", ["Jan Novák"])]


# ===========================================================================
# SourcePanel - guards
# ===========================================================================

@pytest.mark.parametrize("operation", [
    "on_add_person", "on_delete_persons", "on_add_class",
])
def test_person_and_class_operations_refuse_to_run_without_a_source(
        source_manager, dialogs, operation):
    """Every editing entry point starts with the 'no source' guard."""
    panel = SourcePanel("Output Source", source_manager, allow_edit=True)
    getattr(panel, operation)()
    assert dialogs.saw("Please select a source first")


@pytest.mark.parametrize("operation", [
    "on_add_person", "on_delete_persons", "on_add_class",
])
def test_person_and_class_operations_refuse_a_read_only_source(
        build_source, panel_for, dialogs, operation):
    """A read-only source is reported instead of being edited."""
    panel = panel_for(build_source([("6.A", [("Jan", "Novák")])], readonly=True))
    getattr(panel, operation)()
    assert dialogs.saw("This source is read-only")


def test_selecting_a_class_where_a_person_is_required_is_rejected(
        build_source, panel_for, dialogs):
    """'Edit person' on a class row explains what the user must select."""
    panel = panel_for(build_source([("6.A", [("Jan", "Novák")])]))
    select_item(panel, class_item(panel, "6.A"))
    panel.on_edit_person()
    assert dialogs.saw("Please select a person, not a class")


def test_selecting_a_person_where_a_class_is_required_is_rejected(
        build_source, panel_for, dialogs):
    """'Rename class' on a person row explains what the user must select."""
    panel = panel_for(build_source([("6.A", [("Jan", "Novák")])]))
    select_item(panel, class_item(panel, "6.A").child(0))
    panel.on_rename_class()
    assert dialogs.saw("Please select a class (not a person)")


# ===========================================================================
# SourcePanel - person operations
# ===========================================================================

def test_add_person_creates_the_missing_class_and_the_student(
        monkeypatch, build_source, panel_for, dialogs, source_manager):
    """A class that does not exist yet is created on the fly."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    modified = []
    source_manager.source_modified.connect(modified.append)

    queue_text(monkeypatch, "7.B", "Šárka", "Nováková")
    panel.on_add_person()

    assert class_names(source) == ["6.A", "7.B"]
    added = source.classes[1].persons[0]
    assert (added.first_name, added.last_name, added.class_name) == \
        ("Šárka", "Nováková", "7.B")
    assert dialogs.saw("Added Šárka Nováková to class 7.B")
    assert modified == [source.name]
    assert tree_snapshot(panel) == [("6.A", ["Jan Novák"]), ("7.B", ["Šárka Nováková"])]


def test_add_person_trims_the_entered_values(
        monkeypatch, build_source, panel_for, dialogs):
    """Surrounding whitespace is stripped from class and both names."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    queue_text(monkeypatch, "  6.A  ", "  Jan  ", "  Novák  ")
    panel.on_add_person()
    person = source.classes[0].persons[0]
    assert (person.first_name, person.last_name, person.class_name) == \
        ("Jan", "Novák", "6.A")
    assert class_names(source) == ["6.A"]


@pytest.mark.parametrize("answers,message", [
    ((("", True),), "Class name cannot be empty"),
    (("6.A", ("   ", True)), "First name cannot be empty"),
    (("6.A", "Jan", ("", True)), "Last name cannot be empty"),
])
def test_add_person_rejects_a_blank_value_at_every_prompt(
        monkeypatch, build_source, panel_for, dialogs, answers, message):
    """Each of the three prompts validates its own answer."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    queue_text(monkeypatch, *answers)
    panel.on_add_person()
    assert dialogs.saw(message)
    assert source.get_all_persons() == []


@pytest.mark.parametrize("prompts", [0, 1, 2])
def test_add_person_stops_silently_when_a_prompt_is_cancelled(
        monkeypatch, build_source, panel_for, dialogs, prompts):
    """Cancelling any prompt aborts without a message and without data."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    asked = queue_text(monkeypatch, *(["6.A", "Jan"][:prompts]))
    panel.on_add_person()
    assert source.get_all_persons() == []
    assert len(asked) == prompts + 1
    assert dialogs.kinds() == []


def test_add_person_reports_a_duplicate_ignoring_diacritics(
        monkeypatch, build_source, panel_for, dialogs):
    """'Jan Novak' collides with the existing 'Jan Novák'."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    queue_text(monkeypatch, "6.A", "Jan", "Novak")

    panel.on_add_person()

    assert dialogs.saw("already exists in class 6.A")
    assert len(source.classes[0].persons) == 1


def test_edit_person_hides_the_edited_persons_own_username_from_the_dialog(
        monkeypatch, build_source, panel_for, dialogs):
    """The uniqueness check must not complain about the person's own name."""
    source = build_source([("6.A", [
        ("Jan", "Novák", "6.A", {"ad_username": "novakjan"}),
        ("Eva", "Malá", "6.A", {"ad_username": "malaeva"}),
    ])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(0))
    captured = fake_property_editor(monkeypatch, {})

    panel.on_edit_person()

    assert captured["existing_usernames"] == {"malaeva"}
    assert captured["person"] is source.classes[0].persons[0]


def test_edit_person_moves_the_student_when_the_class_name_changed(
        monkeypatch, build_source, panel_for, dialogs):
    """A new class_name relocates the record into the matching class."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", [])])
    panel = panel_for(source)
    person = source.classes[0].persons[0]
    select_item(panel, class_item(panel, "6.A").child(0))
    fake_property_editor(monkeypatch, {"class_name": "7.B"})

    panel.on_edit_person()

    assert source.classes[0].persons == []
    assert source.classes[1].persons == [person]
    assert tree_snapshot(panel) == [("6.A", []), ("7.B", ["Jan Novák"])]


def test_edit_person_creates_the_target_class_when_it_is_missing(
        monkeypatch, build_source, panel_for, dialogs):
    """Moving into an unknown class adds that class instead of losing the student."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(0))
    fake_property_editor(monkeypatch, {"class_name": "9.C"})

    panel.on_edit_person()

    assert class_names(source) == ["6.A", "9.C"]
    assert [p.first_name for p in source.classes[1].persons] == ["Jan"]


def test_edit_person_warns_when_the_new_name_duplicates_a_classmate(
        monkeypatch, build_source, panel_for, dialogs):
    """Renaming a student onto an existing classmate raises the duplicate warning."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Petr", "Dvořák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(1))
    fake_property_editor(monkeypatch, {"first_name": "Jan", "last_name": "Novak"})

    panel.on_edit_person()

    assert dialogs.saw("now exists more than once in class 6.A")


def test_edit_person_does_not_warn_when_the_name_stays_unique(
        monkeypatch, build_source, panel_for, dialogs):
    """No duplicate warning when the renamed student is still the only one."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Petr", "Dvořák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(1))
    fake_property_editor(monkeypatch, {"last_name": "Dvorak"})

    panel.on_edit_person()

    assert not dialogs.saw("more than once")


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: moving a student into a class that already holds "
                          "the same name raises no duplicate warning",
                   strict=False)
def test_edit_person_warns_when_a_class_change_creates_a_duplicate(
        monkeypatch, build_source, panel_for, dialogs):
    """Moving a student onto a namesake is just as duplicate as renaming them."""
    source = build_source([
        ("6.A", [("Jan", "Novák")]),
        ("7.B", [("Jan", "Novák", "7.B")]),
    ])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "7.B").child(0))
    fake_property_editor(monkeypatch, {"class_name": "6.A"})

    panel.on_edit_person()

    assert [p.first_name for p in source.classes[0].persons] == ["Jan", "Jan"]
    assert dialogs.saw("more than once in class 6.A")


def test_edit_person_keeps_everything_when_the_dialog_is_cancelled(
        monkeypatch, build_source, panel_for, dialogs, source_manager):
    """A rejected editor neither moves the student nor notifies anybody."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", [])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(0))
    modified = []
    source_manager.source_modified.connect(modified.append)
    fake_property_editor(monkeypatch, {"class_name": "7.B"}, accepted=False)

    panel.on_edit_person()

    assert [p.first_name for p in source.classes[0].persons] == ["Jan"]
    assert modified == []


def test_delete_persons_needs_a_confirmation(build_source, panel_for, dialogs):
    """Answering 'No' keeps every record."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A").child(0))
    dialogs.question_answer = QMessageBox.StandardButton.No

    panel.on_delete_persons()

    assert len(source.classes[0].persons) == 2
    assert dialogs.saw("Delete 1 person(s)?")


def test_delete_persons_removes_every_selected_record(
        build_source, panel_for, dialogs):
    """Two selected students are deleted in one go and counted in the message."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá"),
                                    ("Petr", "Dvořák")])])
    panel = panel_for(source)
    item = class_item(panel, "6.A")
    panel.tree.clearSelection()
    item.child(0).setSelected(True)
    item.child(2).setSelected(True)

    panel.on_delete_persons()

    assert [p.first_name for p in source.classes[0].persons] == ["Eva"]
    assert dialogs.saw("Deleted 2 person(s)")


def test_delete_persons_reports_a_selection_without_any_person(
        build_source, panel_for, dialogs):
    """Selecting only a class row is refused with a dedicated message."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))

    panel.on_delete_persons()

    assert dialogs.saw("No persons selected")
    assert len(source.classes[0].persons) == 1


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: delete matches persons by value, so the first "
                          "equal record is removed instead of the selected one",
                   strict=False)
def test_delete_persons_removes_the_record_that_was_actually_selected(
        build_source, panel_for, dialogs):
    """Two students with identical data must still be deletable individually."""
    source = build_source([
        ("A", [("Jan", "Novák", "A")]),
        ("B", [("Jan", "Novák", "A")]),
    ])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "B").child(0))

    panel.on_delete_persons()

    assert [p.first_name for p in source.classes[1].persons] == []
    assert [p.first_name for p in source.classes[0].persons] == ["Jan"]


# ===========================================================================
# SourcePanel - class operations
# ===========================================================================

def test_add_class_appends_the_class_and_notifies(
        monkeypatch, build_source, panel_for, dialogs, source_manager):
    """A new, empty class shows up in the tree and in the modification signal."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    modified = []
    source_manager.source_modified.connect(modified.append)
    queue_text(monkeypatch, "  7.B  ")

    panel.on_add_class()

    assert class_names(source) == ["6.A", "7.B"]
    assert dialogs.saw("Added class 7.B")
    assert modified == [source.name]


@pytest.mark.parametrize("answer,message", [
    (("", True), "Class name cannot be empty"),
    (("   ", True), "Class name cannot be empty"),
    (("6.A", True), "Class 6.A already exists"),
])
def test_add_class_rejects_unusable_names(
        monkeypatch, build_source, panel_for, dialogs, answer, message):
    """Empty, blank and already existing names are all refused."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    queue_text(monkeypatch, answer)

    panel.on_add_class()

    assert dialogs.saw(message)
    assert class_names(source) == ["6.A"]


def test_add_class_stops_silently_when_cancelled(
        monkeypatch, build_source, panel_for, dialogs):
    """Cancelling the prompt adds nothing and says nothing."""
    source = build_source([("6.A", [])])
    panel = panel_for(source)
    asked = queue_text(monkeypatch, ("7.B", False))
    panel.on_add_class()
    assert class_names(source) == ["6.A"]
    assert len(asked) == 1
    assert dialogs.kinds() == []


def test_rename_class_updates_the_class_and_every_student_record(
        monkeypatch, build_source, panel_for, dialogs):
    """Renaming rewrites ``class_name`` on the students as well."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    asked = queue_text(monkeypatch, "7.A")

    panel.on_rename_class()

    assert class_names(source) == ["7.A"]
    assert {p.class_name for p in source.classes[0].persons} == {"7.A"}
    assert asked[0][2] == "6.A"                 # old name is pre-filled
    assert dialogs.saw("Renamed class from 6.A to 7.A")


def test_rename_class_does_nothing_when_the_name_is_unchanged(
        monkeypatch, build_source, panel_for, dialogs):
    """Confirming the very same name is a no-op without a success message."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    asked = queue_text(monkeypatch, "6.A")

    panel.on_rename_class()

    assert class_names(source) == ["6.A"]
    assert len(asked) == 1
    assert dialogs.kinds() == []


def test_rename_class_rejects_an_empty_name(
        monkeypatch, build_source, panel_for, dialogs):
    """A blank new name is refused and the class keeps its old one."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    queue_text(monkeypatch, "   ")

    panel.on_rename_class()

    assert class_names(source) == ["6.A"]
    assert dialogs.saw("Class name cannot be empty")


@pytest.mark.parametrize("answer,expected_classes,expected_counts", [
    (QMessageBox.StandardButton.Yes, ["7.B"], [2]),
    (QMessageBox.StandardButton.No, ["6.A", "7.B"], [1, 1]),
])
def test_rename_class_onto_an_existing_class_asks_before_merging(
        monkeypatch, build_source, panel_for, dialogs,
        answer, expected_classes, expected_counts):
    """The students are only merged into the existing class after a 'Yes'."""
    source = build_source([("6.A", [("Jan", "Novák")]),
                           ("7.B", [("Eva", "Malá")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    dialogs.question_answer = answer
    queue_text(monkeypatch, "7.B")

    panel.on_rename_class()

    assert class_names(source) == expected_classes
    assert [len(c.persons) for c in source.classes] == expected_counts
    assert dialogs.saw("Move the 1 student(s) of 6.A into it?")


@pytest.mark.parametrize("answer,remaining", [
    (QMessageBox.StandardButton.Yes, ["7.B"]),
    (QMessageBox.StandardButton.No, ["6.A", "7.B"]),
])
def test_delete_class_removes_it_only_after_confirmation(
        build_source, panel_for, dialogs, answer, remaining):
    """The class and its students survive a declined confirmation."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", [])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    dialogs.question_answer = answer

    panel.on_delete_class()

    assert class_names(source) == remaining
    assert dialogs.saw("Delete class 6.A with 1 person(s)?")


def test_convert_selected_class_rewrites_the_numeral(
        build_source, panel_for, accept_dialogs):
    """The default Roman template turns '6.A' into 'VI.A', students included."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))

    panel.on_convert_selected_class()

    assert class_names(source) == ["VI.A"]
    assert source.classes[0].persons[0].class_name == "VI.A"
    assert accept_dialogs.saw("Converted class 6.A → VI.A")


def test_convert_selected_class_warns_when_no_number_is_present(
        build_source, panel_for, accept_dialogs):
    """A class called 'Window' has nothing to convert and is left alone."""
    source = build_source([("Window", [("Jan", "Novák", "Window")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "Window"))

    panel.on_convert_selected_class()

    assert class_names(source) == ["Window"]
    assert accept_dialogs.saw("No number could be found in 'Window'")


def test_convert_selected_class_reports_a_name_already_in_the_target_format(
        build_source, panel_for, accept_dialogs):
    """'VI.A' is already Roman, so the dialog reports 'nothing to do'."""
    source = build_source([("VI.A", [("Jan", "Novák", "VI.A")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "VI.A"))

    panel.on_convert_selected_class()

    assert class_names(source) == ["VI.A"]
    assert accept_dialogs.saw("already has the target format")


@pytest.mark.parametrize("answer,expected", [
    (QMessageBox.StandardButton.Yes, (["VI.A"], [2])),
    (QMessageBox.StandardButton.No, (["6.A", "VI.A"], [1, 1])),
])
def test_convert_selected_class_asks_before_merging_into_an_existing_class(
        build_source, panel_for, accept_dialogs, answer, expected):
    """Converting '6.A' while 'VI.A' exists needs an explicit confirmation."""
    source = build_source([("6.A", [("Jan", "Novák")]),
                           ("VI.A", [("Eva", "Malá", "VI.A")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    accept_dialogs.question_answer = answer

    panel.on_convert_selected_class()

    assert (class_names(source), [len(c.persons) for c in source.classes]) == expected


def test_convert_selected_class_is_abandoned_when_the_dialog_is_cancelled(
        build_source, panel_for, accept_dialogs):
    """A rejected conversion dialog leaves the class untouched."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    panel = panel_for(source)
    select_item(panel, class_item(panel, "6.A"))
    accept_dialogs.exec_result = QDialog.DialogCode.Rejected

    panel.on_convert_selected_class()

    assert class_names(source) == ["6.A"]


# ===========================================================================
# ComparisonTab - construction and wiring
# ===========================================================================

def test_the_tab_builds_three_panels_and_only_the_output_one_can_edit(tab):
    """Left and right get the source operations, the output panel the editors."""
    assert [p.title for p in (tab.left_panel, tab.right_panel, tab.output_panel)] == \
        ["Left Source", "Right Source", "Output Source"]
    assert (tab.left_panel.allow_edit, tab.right_panel.allow_edit) == (False, False)
    assert tab.output_panel.allow_edit is True
    assert hasattr(tab.left_panel, "duplicate_button")
    assert not hasattr(tab.output_panel, "duplicate_button")


def test_every_panel_is_filled_when_the_tab_is_built(source_manager, build_source):
    """``refresh_all_panels`` runs during construction, not only on demand."""
    source_manager.add_source(build_source([], name="Alpha"))
    fresh = ComparisonTab(source_manager)
    for panel in (fresh.left_panel, fresh.right_panel, fresh.output_panel):
        assert source_names(panel) == ["(Select Source)", "Alpha"]


@pytest.mark.parametrize("signal_name,method", [
    ("duplicate_requested", "copy_to_output"),
    ("shift_requested", "shift_classes"),
    ("convert_requested", "convert_class_numerals"),
    ("analyze_requested", "analyze_source"),
])
@pytest.mark.parametrize("side", ["left", "right"])
def test_each_panel_signal_reaches_its_own_side(tab, signal_name, method, side):
    """The lambdas must not capture the wrong panel."""
    seen = []
    setattr(tab, method, lambda which: seen.append(which))
    panel = tab.left_panel if side == "left" else tab.right_panel

    getattr(panel, signal_name).emit()

    assert seen == [side]


def test_the_duplicate_button_emits_the_panel_signal(tab, dialogs):
    """Pressing the button is what emits ``duplicate_requested``."""
    seen = []
    tab.left_panel.duplicate_requested.connect(lambda: seen.append(1))

    tab.left_panel.duplicate_button.click()

    assert seen == [1]
    # Nothing is selected, so the tab's own slot stops with its guard.
    assert dialogs.saw("No source is selected in the Left Source panel")


@pytest.mark.parametrize("side,title", [("left", "Left Source"), ("right", "Right Source")])
def test_operations_name_the_panel_that_has_no_source(tab, dialogs, side, title):
    """The warning tells the user *which* panel is empty."""
    assert tab._panel_source(side) is None
    assert dialogs.saw(f"No source is selected in the {title} panel")


# ===========================================================================
# ComparisonTab._ask_for_source_name
# ===========================================================================

def test_ask_for_source_name_returns_none_when_cancelled(tab, monkeypatch, dialogs):
    """Cancelling the prompt aborts the whole operation."""
    queue_text(monkeypatch, ("Whatever", False))
    assert tab._ask_for_source_name("Title", "suggested") is None


def test_ask_for_source_name_strips_whitespace(tab, monkeypatch, dialogs):
    """The confirmed name never carries surrounding spaces."""
    queue_text(monkeypatch, "  Result  ")
    assert tab._ask_for_source_name("Title", "suggested") == "Result"


def test_ask_for_source_name_rejects_an_empty_name_and_asks_again(
        tab, monkeypatch, dialogs):
    """An empty answer produces a warning and a second prompt."""
    asked = queue_text(monkeypatch, "   ", "Result")

    assert tab._ask_for_source_name("Name It", "suggested") == "Result"

    assert dialogs.saw("Name cannot be empty")
    assert len(asked) == 2
    assert asked[0] == ("Name It", "Enter name for the new source:", "suggested")


def test_ask_for_source_name_accepts_a_duplicate_when_confirmed(
        tab, monkeypatch, dialogs, build_source, source_manager):
    """Answering 'Yes' to the duplicate warning keeps the entered name."""
    source_manager.add_source(build_source([], name="Taken"))
    queue_text(monkeypatch, "Taken")
    dialogs.warning_answer = QMessageBox.StandardButton.Yes

    assert tab._ask_for_source_name("Title", "suggested") == "Taken"
    assert dialogs.saw("Source 'Taken' already exists")


def test_ask_for_source_name_asks_again_when_the_duplicate_is_declined(
        tab, monkeypatch, dialogs, build_source, source_manager):
    """Declining re-opens the prompt pre-filled with the rejected name."""
    source_manager.add_source(build_source([], name="Taken"))
    asked = queue_text(monkeypatch, "Taken", "Free")
    dialogs.warning_answer = QMessageBox.StandardButton.No

    assert tab._ask_for_source_name("Title", "suggested") == "Free"

    assert [entry[2] for entry in asked] == ["suggested", "Taken"]


# ===========================================================================
# ComparisonTab._store_result
# ===========================================================================

def test_store_result_updates_the_original_in_place(
        tab, build_source, source_manager, dialogs):
    """The first button replaces the classes of the original source."""
    original = build_source([("6.A", [("Jan", "Novák")])], name="L")
    source_manager.add_source(original)
    repaired = original.deep_copy()
    repaired.classes[0].name = "VI.A"
    modified = []
    source_manager.source_modified.connect(modified.append)
    dialogs.clicked_button_index = 0

    tab._store_result(repaired, original, "L-fixed", "Analyze Source")

    assert class_names(original) == ["VI.A"]
    assert original.classes is repaired.classes
    assert modified == ["L"]
    assert source_manager.get_source_names() == ["L"]
    assert dialogs.saw("now contains 1 class(es) and 1 student(s)")


def test_store_result_creates_a_new_editable_source(
        tab, build_source, source_manager, dialogs, monkeypatch):
    """The second button stores the working copy under a new name."""
    original = build_source([("6.A", [("Jan", "Novák")])], name="L")
    source_manager.add_source(original)
    repaired = original.deep_copy()
    repaired.classes[0].name = "VI.A"
    dialogs.clicked_button_index = 1
    queue_text(monkeypatch, "L-fixed")

    tab._store_result(repaired, original, "L-fixed", "Analyze Source")

    assert source_manager.get_source_names() == ["L", "L-fixed"]
    stored = source_manager.get_source_by_name("L-fixed")
    assert (stored.readonly, stored.source_type) == (False, "manual")
    assert class_names(original) == ["6.A"]      # the original is untouched
    assert dialogs.saw("Created source 'L-fixed' with 1 class(es)")


def test_store_result_discards_the_working_copy_on_the_last_button(
        tab, build_source, source_manager, dialogs):
    """'Discard' changes nothing anywhere."""
    original = build_source([("6.A", [("Jan", "Novák")])], name="L")
    source_manager.add_source(original)
    repaired = original.deep_copy()
    repaired.classes[0].name = "VI.A"
    dialogs.clicked_button_index = 2

    tab._store_result(repaired, original, "L-fixed", "Analyze Source")

    assert class_names(original) == ["6.A"]
    assert source_manager.get_source_names() == ["L"]
    assert dialogs.kinds() == ["box_exec"]


def test_store_result_offers_no_in_place_update_for_a_read_only_source(
        tab, build_source, source_manager, dialogs, monkeypatch):
    """A read-only original may only be stored as a new source."""
    original = build_source([("6.A", [("Jan", "Novák")])], name="L", readonly=True)
    source_manager.add_source(original)
    repaired = original.deep_copy()
    repaired.classes[0].name = "VI.A"
    dialogs.clicked_button_index = 0             # first button = "Create new source…"
    queue_text(monkeypatch, "Copy")

    tab._store_result(repaired, original, "L-fixed", "Analyze Source")

    assert class_names(original) == ["6.A"]
    assert source_manager.get_source_names() == ["L", "Copy"]


def test_store_result_aborts_when_the_new_name_is_cancelled(
        tab, build_source, source_manager, dialogs, monkeypatch):
    """Cancelling the name prompt stores nothing at all."""
    original = build_source([("6.A", [("Jan", "Novák")])], name="L")
    source_manager.add_source(original)
    repaired = original.deep_copy()
    dialogs.clicked_button_index = 1
    queue_text(monkeypatch, ("X", False))

    tab._store_result(repaired, original, "L-fixed", "Convert Class Numerals")

    assert source_manager.get_source_names() == ["L"]


# ===========================================================================
# ComparisonTab - operations
# ===========================================================================

def test_copy_to_output_creates_an_independent_editable_copy(
        loaded_tab, source_manager, monkeypatch, dialogs):
    """The duplicate is editable and no longer shares any data with the original."""
    tab, left, _right = loaded_tab
    left.readonly = True
    queue_text(monkeypatch, "L-copy")

    tab.copy_to_output("left")

    copy = source_manager.get_source_by_name("L-copy")
    assert (copy.readonly, copy.source_type) == (False, "manual")
    assert class_names(copy) == class_names(left)
    copy.classes[0].persons[0].first_name = "Changed"
    assert left.classes[0].persons[0].first_name == "Jan"
    assert dialogs.saw("Created copy: L-copy")


def test_copy_to_output_stops_when_the_name_is_cancelled(
        loaded_tab, source_manager, monkeypatch, dialogs):
    """No copy is created when the user cancels the name prompt."""
    tab, _left, _right = loaded_tab
    queue_text(monkeypatch, ("L-copy", False))

    tab.copy_to_output("left")

    assert source_manager.get_source_names() == ["L", "R"]


@pytest.mark.integration
def test_shift_classes_creates_the_shifted_source_and_reports_the_counts(
        loaded_tab, source_manager, monkeypatch, accept_dialogs):
    """The new source holds the shifted classes; the original is untouched."""
    tab, left, _right = loaded_tab
    queue_text(monkeypatch, "L-shifted")

    tab.shift_classes("left")

    shifted = source_manager.get_source_by_name("L-shifted")
    assert class_names(shifted) == ["7.A"]
    assert {p.class_name for p in shifted.get_all_persons()} == {"7.A"}
    assert class_names(left) == ["6.A"]
    assert accept_dialogs.saw("Created shifted source: L-shifted")
    assert accept_dialogs.saw("Classes: 1")


@pytest.mark.integration
def test_shift_classes_stops_when_the_dialog_is_rejected(
        loaded_tab, source_manager, monkeypatch, accept_dialogs):
    """A cancelled shift dialog never asks for a name."""
    tab, _left, _right = loaded_tab
    accept_dialogs.exec_result = QDialog.DialogCode.Rejected
    asked = queue_text(monkeypatch, "L-shifted")

    tab.shift_classes("left")

    assert source_manager.get_source_names() == ["L", "R"]
    assert asked == []


def test_convert_class_numerals_reports_a_source_without_classes(
        tab, source_manager, build_source, dialogs):
    """An empty source is refused before the dialog is even built."""
    empty = build_source([], name="Empty")
    source_manager.add_source(empty)
    tab.left_panel.source_combo.setCurrentText("Empty")

    tab.convert_class_numerals("left")

    assert dialogs.saw("'Empty' contains no classes")


@pytest.mark.integration
def test_convert_class_numerals_updates_the_source_in_place(
        loaded_tab, accept_dialogs):
    """Converting to Roman and choosing 'update' rewrites the original."""
    tab, left, _right = loaded_tab
    accept_dialogs.clicked_button_index = 0

    tab.convert_class_numerals("left")

    assert class_names(left) == ["VI.A"]
    assert {p.class_name for p in left.get_all_persons()} == {"VI.A"}
    assert accept_dialogs.saw("now contains 1 class(es) and 2 student(s)")


@pytest.mark.integration
def test_convert_class_numerals_reports_when_nothing_would_change(
        tab, source_manager, build_source, accept_dialogs):
    """A source that is already Roman produces the 'nothing changed' message."""
    source = build_source([("VI.A", [("Jan", "Novák", "VI.A")])], name="Roman")
    source_manager.add_source(source)
    tab.left_panel.source_combo.setCurrentText("Roman")

    tab.convert_class_numerals("left")

    assert accept_dialogs.saw("No class name needed to be changed")
    assert class_names(source) == ["VI.A"]


def test_merge_sources_requires_both_panels(tab, source_manager, build_source, dialogs):
    """One selected source is not enough for a merge."""
    source_manager.add_source(build_source([("6.A", [])], name="L"))
    tab.left_panel.source_combo.setCurrentText("L")

    tab.merge_sources()

    assert dialogs.saw("Please select both left and right sources")


@pytest.mark.integration
def test_merge_sources_creates_the_union_and_reports_the_statistics(
        loaded_tab, source_manager, monkeypatch, accept_dialogs):
    """The success message repeats the numbers the merge produced."""
    tab, _left, _right = loaded_tab
    queue_text(monkeypatch, "Merged")

    tab.merge_sources()

    merged = source_manager.get_source_by_name("Merged")
    assert sorted(p.last_name for p in merged.get_all_persons()) == \
        ["Dvořák", "Malá", "Novák"]
    assert accept_dialogs.saw("Merged sources using the 'union' strategy")
    assert accept_dialogs.saw("Result: 3 persons in 1 classes")
    assert accept_dialogs.saw("Only in left: 1 • only in right: 1 • in both: 1")


@pytest.mark.integration
def test_merge_sources_reports_collapsed_duplicate_records(
        tab, source_manager, build_source, monkeypatch, accept_dialogs):
    """Duplicates that silently disappear during the merge are counted."""
    left = build_source([("6.A", [("Jan", "Novák"), ("Jan", "Novák")])], name="L")
    right = build_source([("6.A", [("Jan", "Novák")])], name="R")
    source_manager.add_source(left)
    source_manager.add_source(right)
    tab.left_panel.source_combo.setCurrentText("L")
    tab.right_panel.source_combo.setCurrentText("R")
    queue_text(monkeypatch, "Merged")

    tab.merge_sources()

    assert accept_dialogs.saw("Duplicate records collapsed: 1")


@pytest.mark.integration
def test_analyze_source_reports_that_nothing_was_applied(
        tab, source_manager, build_source, accept_dialogs):
    """Accepting the analysis dialog without a fix saves nothing."""
    source = build_source([("6.A", [("Jan", "Novák")])], name="L")
    source_manager.add_source(source)
    tab.left_panel.source_combo.setCurrentText("L")

    tab.analyze_source("left")

    assert accept_dialogs.saw("No solution was applied, nothing to save")


# ===========================================================================
# ClassShiftDialog
# ===========================================================================

@pytest.fixture
def shift_dialog(qapp, build_source, dialogs):
    """Shift dialog over a source that exercises every shift outcome."""
    source = build_source([
        ("8.A", [("Jan", "Novák", "8.A"), ("Eva", "Malá", "8.A")]),
        ("9.B", [("Petr", "Dvořák", "9.B")]),
        ("Window", [("Ota", "Černý", "Window")]),
    ], name="S", readonly=True)
    return ClassShiftDialog(source), source


def test_shift_preview_lists_one_line_per_class_with_the_student_count(shift_dialog):
    """Every class gets a preview line telling what will happen to it."""
    dialog, _source = shift_dialog
    lines = dialog._preview_text.toPlainText().splitlines()
    assert lines == [
        "✓ 8.A → 9.A (2 student(s))",
        "❌ 9.B → REMOVED (graduating year) (1 student(s))",
        "⚠️ Window → unchanged (no number found in the class name) (1 student(s))",
    ]
    assert dialog._preview_summary.text() == (
        "1 class(es) will be shifted, 1 removed (1 student(s) leave), "
        "1 have no number and are copied unchanged, 0 stay as they are."
    )


def test_shift_result_drops_the_graduating_year_and_keeps_the_rest(shift_dialog):
    """9.B leaves the school, 8.A moves up, 'Window' is copied unchanged."""
    dialog, _source = shift_dialog
    result = dialog.build_result_source("NEW")

    assert class_names(result) == ["9.A", "Window"]
    assert (result.name, result.readonly, result.source_type) == ("NEW", False, "manual")
    assert {p.class_name for p in result.classes[0].persons} == {"9.A"}
    assert result.classes[1].persons[0].class_name == "Window"


def test_shift_never_modifies_the_original_source(shift_dialog):
    """The dialog works on a copy - the panel's source stays as it was."""
    dialog, source = shift_dialog
    before = [(c.name, [p.class_name for p in c.persons]) for c in source.classes]

    dialog.build_result_source("NEW")

    assert [(c.name, [p.class_name for p in c.persons]) for c in source.classes] == before
    assert source.readonly is True


def test_shift_keeps_the_graduating_classes_when_the_option_is_off(shift_dialog):
    """Unticking 'remove the graduating year' shifts that class too."""
    dialog, _source = shift_dialog
    dialog._graduation_check.setChecked(False)

    result = dialog.build_result_source("NEW")

    assert class_names(result) == ["9.A", "10.B", "Window"]


def test_shift_merges_classes_that_end_up_with_the_same_name(qapp, build_source, dialogs):
    """A blocked class and a shifted one that collide become a single class."""
    source = build_source([
        ("1.A", [("Jan", "Novák", "1.A")]),
        ("2.A", [("Eva", "Malá", "2.A"), ("Petr", "Dvořák", "2.A")]),
    ], name="S")
    dialog = ClassShiftDialog(source)
    dialog._delta_spin.setValue(-1)

    result = dialog.build_result_source("NEW")

    assert class_names(result) == ["1.A"]
    assert len(result.classes[0].persons) == 3
    assert {p.class_name for p in result.classes[0].persons} == {"1.A"}
    assert class_names(source) == ["1.A", "2.A"]


def test_shift_warns_when_no_class_name_carries_a_number(qapp, build_source, dialogs):
    """A source of free-text class names produces an explicit warning."""
    source = build_source([("Window", []), ("Blue class", [])], name="S")
    dialog = ClassShiftDialog(source)

    assert "none of the class names contains a number" in dialog._warning_label.text()
    assert dialog._warning_label.isHidden() is False
    assert class_names(dialog.build_result_source("NEW")) == ["Window", "Blue class"]


def test_shift_warns_about_a_source_without_any_class(qapp, build_source, dialogs):
    """An empty source cannot produce anything and says so."""
    dialog = ClassShiftDialog(build_source([], name="S"))
    assert dialog._preview_text.toPlainText() == "This source contains no classes."
    assert "the result would be empty" in dialog._warning_label.text().lower()


@pytest.mark.integration
def test_a_fix_on_the_analysis_tab_updates_the_shift_preview(
        qapp, build_source, dialogs):
    """Unifying the names makes classes shiftable, and the preview says so."""
    source = build_source([("6.A", []), ("VI.B", [])], name="S")
    dialog = ClassShiftDialog(source)

    dialog.analysis_panel.issue_widget("inconsistent_class_names")._on_apply()

    assert class_names(dialog.working_source) == ["6.A", "6.B"]
    assert dialog._preview_text.toPlainText().splitlines() == [
        "✓ 6.A → 7.A (0 student(s))",
        "✓ 6.B → 7.B (0 student(s))",
    ]
    assert dialog.has_changes() is True
    assert class_names(source) == ["6.A", "VI.B"]


def test_accepting_a_shift_that_changes_nothing_asks_for_a_confirmation(
        qapp, build_source, dialogs):
    """Declining sends the user to the analysis tab instead of accepting."""
    dialog = ClassShiftDialog(build_source([("Window", [])], name="S"))
    dialogs.question_answer = QMessageBox.StandardButton.No
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == []
    assert dialog._tabs.currentIndex() == 1
    assert dialogs.saw("Create the new source anyway?")


# ===========================================================================
# SourceAnalysisDialog
# ===========================================================================

def test_analysis_dialog_titles_itself_after_the_side_and_the_source(
        qapp, build_source, dialogs):
    """The window title names both the panel and the analysed source."""
    dialog = SourceAnalysisDialog(build_source([("6.A", [])], name="S"),
                                  side="Left Source")
    assert dialog.windowTitle() == "Analyze Left Source - S"
    assert SourceAnalysisDialog(build_source([], name="S")).windowTitle() == \
        "Analyze Source - S"


def test_analysis_dialog_starts_without_changes_and_with_a_disabled_save(
        qapp, build_source, dialogs):
    """Nothing was applied yet, so there is nothing to save."""
    dialog = SourceAnalysisDialog(build_source([("6.A", [])], name="S"))
    assert dialog.has_changes() is False
    assert dialog._apply_button.isEnabled() is False
    assert dialog._changes_label.isHidden() is True


@pytest.mark.integration
def test_analysis_dialog_chains_fixes_on_the_working_copy(
        qapp, build_source, dialogs):
    """Each solution runs on the data the previous one produced."""
    source = build_source([("6.A", [("Jan", "Novák", "7.B")]), ("Empty", [])],
                          name="S")
    dialog = SourceAnalysisDialog(source)
    assert {i.key for i in dialog.analysis_panel.report().issues} >= {
        "class_person_mismatch", "empty_classes"}

    dialog.analysis_panel.issue_widget("class_person_mismatch")._on_apply()
    assert "class_person_mismatch" not in \
        {i.key for i in dialog.analysis_panel.report().issues}

    dialog.analysis_panel.issue_widget("empty_classes")._on_apply()

    repaired = dialog.get_repaired_source()
    assert class_names(repaired) == ["6.A"]
    assert repaired.classes[0].persons[0].class_name == "6.A"
    assert dialog.has_changes() is True
    assert len(dialog.fixes_applied) == 2
    assert dialog._apply_button.isEnabled() is True
    # The source the dialog was opened on is never touched.
    assert class_names(source) == ["6.A", "Empty"]
    assert source.classes[0].persons[0].class_name == "7.B"


def test_analysis_dialog_reports_a_fix_that_could_not_be_applied(
        qapp, build_source, dialogs):
    """An unknown solution key is refused with a warning, not an exception."""
    dialog = SourceAnalysisDialog(build_source([("6.A", []), ("", [])], name="S"))
    issue = dialog.analysis_panel.report().issues[0]

    dialog.on_fix_requested(issue, "does_not_exist", "")

    assert dialog.has_changes() is False
    assert dialogs.saw("Unknown solution 'does_not_exist'")


def test_closing_the_analysis_dialog_confirms_before_dropping_the_fixes(
        qapp, build_source, dialogs):
    """Declining the confirmation keeps the dialog open."""
    source = build_source([("6.A", [("Jan", "Novák", "7.B")])], name="S")
    dialog = SourceAnalysisDialog(source)
    dialog.analysis_panel.issue_widget("class_person_mismatch")._on_apply()
    rejected = []
    dialog.rejected.connect(lambda: rejected.append(1))
    dialogs.question_answer = QMessageBox.StandardButton.No

    dialog.reject()
    assert rejected == []
    assert dialogs.saw("1 solution(s) were applied to a working copy")

    dialogs.question_answer = QMessageBox.StandardButton.Yes
    dialog.reject()
    assert rejected == [1]


def test_a_clean_analysis_dialog_closes_without_a_question(
        qapp, build_source, dialogs):
    """Nothing to discard means no confirmation at all."""
    dialog = SourceAnalysisDialog(build_source([("6.A", [("Jan", "Novák")])], name="S"))
    rejected = []
    dialog.rejected.connect(lambda: rejected.append(1))

    dialog.reject()

    assert rejected == [1]
    assert dialogs.kinds() == []


# ===========================================================================
# ClassNumeralConversionDialog
# ===========================================================================

def test_conversion_dialog_defaults_to_the_roman_template(qapp, dialogs):
    """Roman is pre-selected and immediately usable."""
    dialog = ClassNumeralConversionDialog(["6.A", "7.B"], title="Convert")
    assert dialog.get_template() == TEMPLATE_ROMAN
    assert dialog._ok_button.isEnabled() is True
    assert dialog.convert_name("6.A").result == "VI.A"
    assert [r.result for r in dialog.get_results()] == ["VI.A", "VII.B"]


def test_conversion_dialog_disables_ok_for_an_invalid_custom_template(qapp, dialogs):
    """An unusable template must not be confirmable."""
    dialog = ClassNumeralConversionDialog(["6.A"])
    dialog.template_widget._custom_input.setText("{nope}")

    assert dialog.get_template() == ""
    assert dialog._ok_button.isEnabled() is False


def test_conversion_dialog_refuses_to_accept_an_invalid_template(qapp, dialogs):
    """Accepting anyway shows the concrete validation error."""
    dialog = ClassNumeralConversionDialog(["6.A"])
    dialog.template_widget._custom_input.setText("{nope}")
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == []
    assert dialogs.saw("Unknown placeholder '{nope}'")


@pytest.mark.parametrize("answer,expected", [
    (QMessageBox.StandardButton.Yes, [1]),
    (QMessageBox.StandardButton.No, []),
])
def test_conversion_dialog_asks_when_no_name_would_change(
        qapp, dialogs, answer, expected):
    """Converting 'VI.A' to Roman changes nothing - the user must confirm."""
    dialog = ClassNumeralConversionDialog(["VI.A"])
    dialogs.question_answer = answer
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == expected
    assert dialogs.saw("None of the class names would change")


def test_conversion_dialog_accepts_an_empty_name_list_without_a_question(
        qapp, dialogs):
    """With nothing to preview there is nothing to warn about."""
    dialog = ClassNumeralConversionDialog([])
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == [1]
    assert dialogs.kinds() == []


# ===========================================================================
# ClassTemplateWidget
# ===========================================================================

def test_template_widget_starts_on_roman_and_previews_every_name(qapp):
    """The preview table has one row per class name from the very start."""
    widget = ClassTemplateWidget(["6.A", "VI.A", "Window"])
    assert widget.get_template() == TEMPLATE_ROMAN
    assert widget.is_valid() is True
    assert widget.get_error() is None
    assert widget._preview_table.rowCount() == 3
    assert [widget._preview_table.item(row, 1).text() for row in range(3)] == \
        ["VI.A", "VI.A", "Window"]


@pytest.mark.parametrize("row,note,color", [
    (0, "Arabic (6, 9) = 6, section 'A'", "#4caf50"),
    (1, "already in the target format", "#888888"),
    (2, "no number found - left unchanged", "#ffa726"),
])
def test_template_widget_colours_every_preview_note_by_its_outcome(
        qapp, row, note, color):
    """Changed rows are green, untouched ones grey and skipped ones orange."""
    widget = ClassTemplateWidget(["6.A", "VI.A", "Window"])
    item = widget._preview_table.item(row, 2)
    assert item.text() == note
    assert item.foreground().color().name() == color


def test_template_widget_summarises_the_outcome(qapp):
    """The summary counts changed, already-matching and skipped names."""
    widget = ClassTemplateWidget(["6.A", "VI.A", "Window"])
    assert widget._summary_label.text().startswith(
        "1 name(s) will change, 1 already match the format, "
        "1 contain no number and stay as they are."
    )


def test_template_widget_warns_about_names_that_would_collide(qapp):
    """'6.A' and 'VI.A' both become 'VI.A' - the user is told they will merge."""
    widget = ClassTemplateWidget(["6.A", "VI.A"])
    summary = widget._summary_label.text()
    assert "1 name collision(s)" in summary
    assert "6.A + VI.A → VI.A" in summary
    assert "merged into one" in summary


def test_template_widget_has_no_collision_warning_for_distinct_results(qapp):
    """Names that stay distinct produce no collision block."""
    widget = ClassTemplateWidget(["6.A", "7.B"])
    assert "collision" not in widget._summary_label.text()


def test_typing_a_custom_template_selects_the_custom_mode(qapp):
    """The user does not have to tick the radio button first."""
    widget = ClassTemplateWidget(["6.A"])
    emitted = []
    widget.template_changed.connect(emitted.append)

    widget._custom_input.setText("Class {arabic}-{letter_upper}")

    assert widget._custom_radio.isChecked() is True
    assert widget.get_template() == "Class {arabic}-{letter_upper}"
    assert widget._preview_table.item(0, 1).text() == "Class 6-A"
    assert emitted[-1] == "Class {arabic}-{letter_upper}"


@pytest.mark.parametrize("template,error_fragment", [
    ("", "cannot be empty"),
    ("   ", "cannot be empty"),
    ("{arabic", "unbalanced"),
    ("{Arabic}", "Unsupported placeholder '{Arabic}'"),
    ("{nope}.{letter}", "Unknown placeholder '{nope}'"),
])
def test_template_widget_reports_an_unusable_custom_template(
        qapp, template, error_fragment):
    """An invalid template blocks the preview and explains the problem."""
    widget = ClassTemplateWidget(["6.A"])
    widget._custom_radio.setChecked(True)
    widget._custom_input.setText(template)

    assert widget.get_template() == ""
    assert widget.is_valid() is False
    assert error_fragment in widget.get_error()
    assert widget._error_label.isHidden() is False
    assert widget.get_results() == []
    assert "Fix the template to see a preview." in widget._summary_label.text()


@pytest.mark.parametrize("template,roman,arabic,custom", [
    (TEMPLATE_ROMAN, True, False, False),
    (TEMPLATE_ARABIC, False, True, False),
    ("{prefix}{arabic}", False, False, True),
])
def test_select_template_picks_the_matching_radio_button(
        qapp, template, roman, arabic, custom):
    """A predefined template selects its own radio, anything else the custom one."""
    widget = ClassTemplateWidget(["6.A"])
    widget.select_template(template)
    assert (widget._roman_radio.isChecked(),
            widget._arabic_radio.isChecked(),
            widget._custom_radio.isChecked()) == (roman, arabic, custom)
    assert widget.get_template() == template


def test_set_class_names_rebuilds_the_preview(qapp):
    """Replacing the names repaints the table, empty input included."""
    widget = ClassTemplateWidget(["6.A"])
    widget.set_class_names(["9.B", "10.C"])
    assert [r.result for r in widget.get_results()] == ["IX.B", "X.C"]
    assert widget._preview_table.rowCount() == 2

    widget.set_class_names([])
    assert widget.get_results() == []
    assert widget._preview_table.rowCount() == 0


def test_template_widget_can_be_built_without_a_preview(qapp):
    """``show_preview=False`` still computes the results, it only hides them."""
    widget = ClassTemplateWidget(["6.A"], show_preview=False)
    assert widget._preview_table is None
    assert widget._summary_label is None
    assert [r.result for r in widget.get_results()] == ["VI.A"]


def test_template_widget_renders_an_empty_name_placeholder(qapp):
    """A class without a name is shown as '(empty)' instead of a blank cell."""
    widget = ClassTemplateWidget([""])
    assert widget._preview_table.item(0, 0).text() == "(empty)"
    assert widget._preview_table.item(0, 1).text() == "(empty)"


# ===========================================================================
# AnalysisPanel / IssueWidget
# ===========================================================================

def make_issue(key="demo", severity=IssueSeverity.WARNING, fixes=None, **kwargs):
    """Build a :class:`SourceIssue` for the presentation tests."""
    return SourceIssue(
        key=key,
        title=kwargs.pop("title", "Demo issue"),
        severity=severity,
        summary=kwargs.pop("summary", "Something is odd."),
        fixes=fixes if fixes is not None else [
            FixOption(key="do_it", label="Do it"),
            FixOption(key="keep", label="Keep", is_noop=True),
        ],
        **kwargs,
    )


def test_analysis_panel_renders_one_widget_per_issue(qapp):
    """Every issue of the report becomes an addressable IssueWidget."""
    panel = AnalysisPanel()
    report = AnalysisReport(title="R", issues=[make_issue("a"), make_issue("b")],
                            stats={"Classes": 2, "Students": 7})

    panel.set_report(report)

    assert panel.report() is report
    assert [w.issue.key for w in panel._issue_widgets] == ["a", "b"]
    assert panel.issue_widget("b").issue.key == "b"
    assert panel.issue_widget("missing") is None
    assert panel._stats_layout.rowCount() == 2
    assert panel._stats_box.isHidden() is False


def test_analysis_panel_replaces_the_previous_issues(qapp):
    """A second report must not leave the widgets of the first one behind."""
    panel = AnalysisPanel()
    panel.set_report(AnalysisReport(title="R", issues=[make_issue("a"), make_issue("b")]))
    panel.set_report(AnalysisReport(title="R", issues=[make_issue("c")]))

    assert [w.issue.key for w in panel._issue_widgets] == ["c"]
    assert panel.issue_widget("a") is None
    assert panel._container_layout.count() == 2      # one issue + the stretch


def test_analysis_panel_celebrates_a_clean_report(qapp):
    """A report without issues shows the green headline and a placeholder."""
    panel = AnalysisPanel()
    panel.set_report(AnalysisReport(title="R"))

    assert "No problems" in panel._headline.text()
    assert panel._stats_box.isHidden() is True
    placeholder = panel._container_layout.itemAt(0).widget()
    assert isinstance(placeholder, QLabel)
    assert "Everything checked out" in placeholder.text()


def test_analysis_panel_headline_counts_every_severity(qapp):
    """Errors, warnings and notes are counted separately in the headline."""
    panel = AnalysisPanel()
    panel.set_report(AnalysisReport(title="My report", issues=[
        make_issue("e", IssueSeverity.ERROR),
        make_issue("w1"), make_issue("w2"),
        make_issue("i", IssueSeverity.INFO),
    ]))

    headline = panel._headline.text()
    assert "My report" in headline
    assert "1 problem(s) that block a correct result" in headline
    assert "2 warning(s)" in headline
    assert "1 note(s)" in headline


def test_issue_widget_preselects_the_first_solution(qapp):
    """The recommended solution is the one that is offered by default."""
    widget = IssueWidget(make_issue())
    assert widget.selected_fix().key == "do_it"
    assert widget.selected_parameter() == ""


def test_issue_widget_emits_the_issue_fix_key_and_parameter(qapp):
    """``fix_requested`` carries everything the owner needs to apply the fix."""
    issue = make_issue(fixes=[FixOption(
        key="unify_custom", label="Custom", parameter_kind=ParameterKind.TEMPLATE,
        parameter_label="Template:", parameter_default=TEMPLATE_ARABIC,
    )])
    widget = IssueWidget(issue)
    payloads = []
    widget.fix_requested.connect(lambda *args: payloads.append(args))

    assert widget.selected_parameter() == TEMPLATE_ARABIC
    widget._on_apply()

    assert payloads == [(issue, "unify_custom", TEMPLATE_ARABIC)]


def test_issue_widget_refuses_an_invalid_template_parameter(qapp):
    """A broken template is reported in place and never leaves the widget."""
    widget = IssueWidget(make_issue(fixes=[FixOption(
        key="unify_custom", label="Custom", parameter_kind=ParameterKind.TEMPLATE,
        parameter_default="{nope}",
    )]))
    payloads = []
    widget.fix_requested.connect(lambda *args: payloads.append(args))

    widget._on_apply()

    assert payloads == []
    assert widget._status_label.text() == "Unknown placeholder '{nope}'"
    assert "#ff6b6b" in widget._status_label.styleSheet()


def test_issue_widget_does_not_emit_for_a_no_op_solution(qapp):
    """'Keep everything' is answered locally instead of touching the data."""
    widget = IssueWidget(make_issue(fixes=[FixOption(key="keep", label="Keep",
                                                     is_noop=True)]))
    payloads = []
    widget.fix_requested.connect(lambda *args: payloads.append(args))

    widget._on_apply()

    assert payloads == []
    assert widget._status_label.text() == "Nothing to change for this solution."


def test_issue_widget_truncates_a_long_list_of_affected_items(qapp):
    """Only the first 25 entries are listed, the rest is summarised."""
    widget = IssueWidget(make_issue(affected=[f"item {i}" for i in range(30)]))
    text = widget.findChildren(QTextEdit)[0].toPlainText()

    assert text.splitlines()[0] == "• item 0"
    assert text.splitlines()[IssueWidget.MAX_AFFECTED_SHOWN - 1] == "• item 24"
    assert text.endswith("… and 5 more")


def test_issue_widget_can_hide_the_solutions_completely(qapp):
    """A read-only rendering offers no fix controls at all."""
    widget = IssueWidget(make_issue(), allow_fixes=False)
    assert widget.selected_fix() is None
    assert widget.selected_parameter() == ""
    widget.show_status("ignored")                # must not raise


def test_analysis_panel_forwards_the_fix_request_of_its_children(qapp):
    """The panel is a pure relay between the issue widgets and its owner."""
    panel = AnalysisPanel()
    issue = make_issue("a")
    panel.set_report(AnalysisReport(title="R", issues=[issue]))
    payloads = []
    panel.fix_requested.connect(lambda *args: payloads.append(args))

    panel.issue_widget("a")._on_apply()

    assert payloads == [(issue, "do_it", "")]


# ===========================================================================
# MergeSourcesDialog
# ===========================================================================

@pytest.fixture
def merge_dialog(qapp, build_source, dialogs):
    """Merge dialog over two sources that use different class-name styles."""
    left = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])], name="L")
    right = build_source([("VI.A", [("Jan", "Novák", "VI.A"),
                                    ("Petr", "Dvořák", "VI.A")])],
                         name="R", readonly=True)
    return MergeSourcesDialog(left, right), left, right


def test_merge_dialog_works_on_editable_copies_of_both_sources(merge_dialog, dialogs):
    """The dialog never hands out - or modifies - the selected sources."""
    dialog, left, right = merge_dialog
    assert dialog.get_left_source() is not left
    assert dialog.get_right_source() is not right
    assert dialog.get_right_source().readonly is False
    assert right.readonly is True
    assert dialog.has_changes() is False


def test_merge_dialog_defaults_to_union_and_shows_the_expected_count(merge_dialog, dialogs):
    """Union is pre-selected and the label predicts the resulting size."""
    dialog, _left, _right = merge_dialog
    assert dialog.get_strategy() == "union"
    label = dialog._expected_label.text()
    assert "Expected result with 'union': 4 student(s)" in label
    assert "Left: 2" in label and "Right: 2" in label
    assert "in both: 0" in label


def test_switching_the_strategy_recomputes_the_expected_count(merge_dialog, dialogs):
    """Intersection of two differently named classes is empty - and flagged."""
    dialog, _left, _right = merge_dialog

    dialog._intersection_radio.setChecked(True)

    assert dialog.get_strategy() == "intersection"
    label = dialog._expected_label.text()
    assert "Expected result with 'intersection': 0 student(s)" in label
    assert "The result would be empty" in label
    assert "no_common_persons" in {i.key for i in dialog.analysis_panel.report().issues}


def test_merge_dialog_fix_is_routed_to_both_working_copies(merge_dialog, dialogs):
    """A 'both' issue unifies the class names on either side."""
    dialog, left, right = merge_dialog
    issue = [i for i in dialog.analysis_panel.report().issues
             if i.key == "cross_source_class_style"][0]
    assert issue.target == "both"

    dialog._on_fix_requested(issue, "unify_arabic", "")

    assert class_names(dialog.get_left_source()) == ["6.A"]
    assert class_names(dialog.get_right_source()) == ["6.A"]
    assert class_names(left) == ["6.A"] and class_names(right) == ["VI.A"]
    assert dialog.has_changes() is True
    assert "Left: Renamed 0 class(es). Right: Renamed 1 class(es)." in \
        dialog._changes_label.text()
    assert "Expected result with 'union': 3 student(s)" in dialog._expected_label.text()


def test_merge_dialog_fix_is_routed_to_one_side_only(qapp, build_source, dialogs):
    """A left-hand issue must not rewrite the right-hand working copy."""
    left = build_source([("6.A", [("Jan", "Novák"), ("Jan", "Novak", "6.A")])],
                        name="L")
    right = build_source([("6.A", [("Jan", "Novák")])], name="R")
    dialog = MergeSourcesDialog(left, right)
    issue = [i for i in dialog.analysis_panel.report().issues
             if i.key == "duplicate_persons"][0]
    assert issue.target == "left"

    dialog._on_fix_requested(issue, "remove_duplicate_persons", "")

    assert len(dialog.get_left_source().get_all_persons()) == 1
    assert len(dialog.get_right_source().get_all_persons()) == 1
    assert len(left.get_all_persons()) == 2


def test_merge_dialog_fix_is_routed_to_the_right_side_only(
        qapp, build_source, dialogs):
    """A right-hand issue must not rewrite the left-hand working copy."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="L")
    right = build_source([("6.A", [("Jan", "Novák"), ("Jan", "Novak", "6.A")])],
                         name="R")
    dialog = MergeSourcesDialog(left, right)
    issue = [i for i in dialog.analysis_panel.report().issues
             if i.key == "duplicate_persons"][0]
    assert issue.target == "right"

    dialog._on_fix_requested(issue, "remove_duplicate_persons", "")

    assert len(dialog.get_right_source().get_all_persons()) == 1
    assert len(dialog.get_left_source().get_all_persons()) == 1
    assert len(right.get_all_persons()) == 2
    assert dialog._changes_label.text().endswith(
        "Right: Removed 1 duplicate student record(s).")


def test_merge_dialog_reports_a_fix_that_was_refused(merge_dialog, dialogs):
    """An unknown solution key is refused without changing anything."""
    dialog, _left, _right = merge_dialog
    issue = dialog.analysis_panel.report().issues[0]

    dialog._on_fix_requested(issue, "nonsense", "")

    assert dialog.has_changes() is False
    assert dialogs.saw("Unknown solution 'nonsense'")


@pytest.mark.parametrize("answer,expected_accepts,expected_tab", [
    (QMessageBox.StandardButton.Yes, [1], 0),
    (QMessageBox.StandardButton.No, [], 2),
])
def test_merging_with_blocking_problems_needs_a_confirmation(
        qapp, build_source, dialogs, answer, expected_accepts, expected_tab):
    """Declining the confirmation opens the analysis tab instead of merging."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="L")
    right = build_source([("7.B", [("Eva", "Malá", "7.B")])], name="R")
    dialog = MergeSourcesDialog(left, right)
    dialog._intersection_radio.setChecked(True)
    assert dialog.analysis_panel.report().has_blocking_issues is True
    dialogs.question_answer = answer
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == expected_accepts
    assert dialog._tabs.currentIndex() == expected_tab
    assert dialogs.saw("Merge anyway?")


def test_merging_a_clean_pair_is_accepted_straight_away(qapp, build_source, dialogs):
    """Without blocking issues the dialog closes without a question."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="L")
    right = build_source([("6.A", [("Jan", "Novák")])], name="R")
    dialog = MergeSourcesDialog(left, right)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(1))

    dialog._on_accept()

    assert accepted == [1]
    assert dialogs.kinds() == []
