"""
Unit tests for the Active Directory management UI:

* :class:`operations.ad_management.ADManagementWidget` (and its
  ``ColumnVisibilityDialog``),
* :class:`ui.bulk_edit_dialog.BulkEditDialog`,
* :class:`ui.property_editor.PropertyEditorDialog`.

Every test in this module runs with the modal dialogs replaced by the shared
recorder (``accept_dialogs`` -> ``dialogs``) and with the settings file
redirected into ``tmp_path`` - the password policy is read from the settings,
so without that isolation the real user configuration would be touched.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect found in the production code; they are expected to fail until it is
fixed.
"""

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QMessageBox, QPushButton

from models import ADGroup, ADStatus, GroupTemplate, Person, Source
import operations.ad_management as ad_management
from operations.ad_management import ADManagementWidget, ColumnVisibilityDialog
from ui.bulk_edit_dialog import BulkEditDialog
from ui.property_editor import PropertyEditorDialog
from utils.password_policy import PasswordPolicy, get_password_policy, set_password_policy


pytestmark = pytest.mark.gui

ACCEPTED = QDialog.DialogCode.Accepted
REJECTED = QDialog.DialogCode.Rejected

DEFAULT_COLUMNS = ADManagementWidget.DEFAULT_COLUMNS
ALL_COLUMNS = ADManagementWidget.ALL_COLUMNS


# ---------------------------------------------------------------------------
# module wide safety net
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_dialogs_no_real_settings(accept_dialogs, isolated_settings):
    """Stub every modal dialog and redirect the settings for every test here."""
    return accept_dialogs


# ---------------------------------------------------------------------------
# helpers and fixtures
# ---------------------------------------------------------------------------

def group(name="Students", dn=None):
    """Build an ADGroup; the DN defaults to ``CN=<name>,DC=school,DC=local``."""
    return ADGroup(name=name, dn=dn or f"CN={name},DC=school,DC=local",
                   group_type="security")


def row_texts(table, column=0):
    """Return the text of *column* for every row, top to bottom."""
    return [table.item(row, column).text() if table.item(row, column) else None
            for row in range(table.rowCount())]


def headers(table):
    """Return the horizontal header labels."""
    return [table.horizontalHeaderItem(i).text()
            for i in range(table.columnCount())]


def actions_column(table):
    """Index of the trailing 'Actions' column."""
    return table.columnCount() - 1


@pytest.fixture
def widget(qapp, source_manager):
    """A fresh ADManagementWidget wired to an empty SourceManager."""
    made = ADManagementWidget(source_manager)
    yield made
    made.close()


@pytest.fixture
def source_of(make_class):
    """Factory: ``source_of([person, ...], readonly=True)`` -> a one-class Source."""
    def _build(persons, name="Source", readonly=False, class_name="6.A"):
        source = Source(name=name, source_type="manual", readonly=readonly)
        source.add_class(make_class(class_name, persons=list(persons)))
        return source
    return _build


@pytest.fixture
def loaded(widget, source_of, make_person):
    """
    Widget showing four persons with deliberately different field values.

    Returns ``(widget, source, persons)``.
    """
    persons = [
        make_person("Zoe", "Zeman", "9.B", ad_username="zemanzoe",
                    ad_email="zemanzoe@skola.cz", account_enabled=True,
                    ad_status=ADStatus.SYNCED),
        make_person("Adam", "Adamec", "6.A"),
        make_person("Milan", "Novák", "7.C", ad_username="novakmilan",
                    ad_password="Str0ng!pass", ad_display_name="Milan Novák (7.C)"),
        make_person("Ivana", "Čapková", "6.A", ad_username="capkovaivana",
                    account_enabled=True),
    ]
    source = source_of(persons)
    widget.set_source(source)
    return widget, source, persons


@pytest.fixture
def notifications(source_manager):
    """Collect every ``source_modified`` notification the widget emits."""
    seen = []
    source_manager.source_modified.connect(seen.append)
    return seen


def install_property_editor(monkeypatch, result=ACCEPTED):
    """
    Replace PropertyEditorDialog with a recorder.

    Returns the list of ``(person, existing_usernames)`` tuples it was opened
    with.
    """
    opened = []

    class Recorder:
        def __init__(self, person, existing_usernames, available_groups=None,
                     available_templates=None, parent=None):
            opened.append((person, existing_usernames))

        def exec(self):
            return result

    monkeypatch.setattr(ad_management, "PropertyEditorDialog", Recorder)
    return opened


def install_bulk_dialog(monkeypatch, configure):
    """
    Install a BulkEditDialog factory that configures a *real* dialog and lets
    ``exec()`` run its real ``accept_changes()``.

    Returns a dict that is filled with what the dialog itself produced, so a
    test can compare it with the state after the widget is done.
    """
    record = {}

    def factory(selected, available_groups=None, available_templates=None,
                parent=None):
        dialog = BulkEditDialog(selected, available_groups, available_templates,
                                parent)
        configure(dialog)

        def _exec():
            dialog.accept_changes()
            record["changes"] = dict(dialog.changes)
            record["passwords"] = [p.ad_password for p in selected]
            record["groups"] = [list(p.group_memberships) for p in selected]
            return dialog.result()

        dialog.exec = _exec
        return dialog

    monkeypatch.setattr(ad_management, "BulkEditDialog", factory)
    return record


def apply_bulk(dialog, dialogs):
    """Run the real ``accept_changes`` and report whether the dialog accepted."""
    dialog.accept_changes()
    return dialog.result() == ACCEPTED


# ===========================================================================
# ADManagementWidget.refresh_person_table
# ===========================================================================

def test_refresh_without_source_clears_rows_and_columns(widget):
    """With no source selected the table is completely empty."""
    widget.refresh_person_table()
    assert widget.person_table.rowCount() == 0
    assert widget.person_table.columnCount() == 0


@pytest.mark.parametrize("visible,expected", [
    (["Name"], ["Name", "Actions"]),
    (list(DEFAULT_COLUMNS),
     ["Name", "Class", "Username", "Email", "Status", "Enabled", "Dirty",
      "Actions"]),
    (["Class", "Enabled"], ["Class", "Enabled", "Actions"]),
    (list(ALL_COLUMNS), list(ALL_COLUMNS) + ["Actions"]),
])
def test_refresh_builds_the_selected_columns_plus_actions(loaded, visible,
                                                          expected):
    """The header is exactly the visible columns followed by 'Actions'."""
    widget, _source, persons = loaded
    widget.visible_columns = visible
    widget.refresh_person_table()

    assert headers(widget.person_table) == expected
    assert widget.person_table.columnCount() == len(visible) + 1
    assert widget.person_table.rowCount() == len(persons)


def test_refresh_lists_persons_in_source_order_until_the_user_sorts(loaded):
    """A table nobody sorted keeps the order of ``get_all_persons()``."""
    widget, _source, persons = loaded
    expected = [f"{p.first_name} {p.last_name}" for p in persons]
    # no sort indicator == the user has not clicked a header yet
    widget.person_table.horizontalHeader().setSortIndicator(
        -1, Qt.SortOrder.AscendingOrder)

    widget.refresh_person_table()
    assert row_texts(widget.person_table) == expected

    widget.refresh_person_table()
    assert row_texts(widget.person_table) == expected


def test_refresh_keeps_the_users_sort_order(loaded):
    """Repainting the table does not throw away the column the user sorted by."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    sorted_names = row_texts(widget.person_table)

    widget.refresh_person_table()

    assert row_texts(widget.person_table) == sorted_names
    assert sorted_names == sorted(sorted_names)


@pytest.mark.parametrize("column,expected", [
    ("Username", "(not set)"),
    ("Email", "(not set)"),
    ("Display Name", "(not set)"),
    ("Password", "(not set)"),
])
def test_refresh_marks_empty_ad_fields_as_not_set(widget, source_of,
                                                  make_person, column,
                                                  expected):
    """Missing AD values are shown as '(not set)', never as 'None'."""
    widget.visible_columns = [column]
    widget.set_source(source_of([make_person("Adam", "Adamec", "6.A")]))

    assert row_texts(widget.person_table) == [expected]


def test_refresh_masks_the_password(widget, source_of, make_person):
    """A stored password is shown as bullets, never in clear text."""
    widget.visible_columns = ["Password"]
    widget.set_source(source_of([make_person(ad_password="Str0ng!pass")]))

    shown = row_texts(widget.person_table)[0]
    assert shown == "•" * 8
    assert "Str0ng" not in shown


@pytest.mark.parametrize("enabled,expected", [(True, "✓"), (False, "✗")])
def test_refresh_shows_the_account_state_as_a_symbol(widget, source_of,
                                                     make_person, enabled,
                                                     expected):
    """The Enabled column is a check mark for enabled and a cross otherwise."""
    widget.visible_columns = ["Enabled"]
    widget.set_source(source_of([make_person(account_enabled=enabled)]))

    assert row_texts(widget.person_table) == [expected]


def test_refresh_flags_only_modified_persons_as_dirty(widget, source_of,
                                                      make_person):
    """The Dirty column is ticked for a changed person and blank for a fresh one."""
    untouched = make_person("Adam", "Adamec", "6.A")
    changed = make_person("Zoe", "Zeman", "9.B")
    changed.ad_email = "zemanzoe@skola.cz"

    widget.visible_columns = ["Dirty"]
    widget.set_source(source_of([untouched, changed]))

    assert row_texts(widget.person_table) == ["", "✓"]


def test_refresh_shows_the_password_policy_columns(widget, source_of,
                                                   make_person):
    """Must-change / cannot-change / never-expires each get their own tick."""
    person = make_person(password_must_change=True, password_never_expires=True)
    widget.visible_columns = ["Must Change Pwd", "Cannot Change Pwd",
                              "Never Expires"]
    widget.set_source(source_of([person]))

    assert [row_texts(widget.person_table, c)[0] for c in range(3)] == \
        ["✓", "", "✓"]


def test_refresh_shows_the_ad_status_value(widget, source_of, make_person):
    """The Status column shows the enum *value*, not its name."""
    widget.visible_columns = ["Status"]
    widget.set_source(source_of([make_person(ad_status=ADStatus.UPDATE_PENDING)]))

    assert row_texts(widget.person_table) == [ADStatus.UPDATE_PENDING.value]


def test_refresh_renders_diacritics_unchanged(widget, source_of, make_person):
    """Czech names reach the table exactly as they were entered."""
    widget.set_source(source_of([make_person("Žofie", "Křížová", "6.A")]))

    assert row_texts(widget.person_table) == ["Žofie Křížová"]
    assert row_texts(widget.person_table, 1) == ["6.A"]


def test_refresh_stores_the_person_on_every_cell_of_its_row(loaded):
    """Each populated cell carries its own Person, so sorting cannot desync it."""
    widget, _source, persons = loaded
    table = widget.person_table

    for row in range(table.rowCount()):
        stored = [table.item(row, col).data(Qt.ItemDataRole.UserRole)
                  for col in range(actions_column(table))]
        assert all(isinstance(person, Person) for person in stored)
        assert len({id(person) for person in stored}) == 1
        assert id(stored[0]) in {id(p) for p in persons}


def test_refresh_without_visible_columns_keeps_only_the_actions_column(loaded):
    """An empty column selection degrades gracefully instead of crashing."""
    widget, _source, persons = loaded
    widget.visible_columns = []
    widget.refresh_person_table()

    assert widget.person_table.columnCount() == 1
    assert widget.person_table.rowCount() == len(persons)
    assert widget._person_at_row(0) is None


def test_refresh_after_persons_were_removed_shrinks_the_table(loaded):
    """Removing students from the source removes their rows on the next repaint."""
    widget, source, persons = loaded
    school_class = source.classes[0]
    school_class.remove_person(persons[0])
    school_class.remove_person(persons[1])

    widget.refresh_person_table()

    assert widget.person_table.rowCount() == 2
    assert set(row_texts(widget.person_table)) == {"Milan Novák", "Ivana Čapková"}


@pytest.mark.bug
def test_refresh_does_not_leak_edit_buttons(loaded):
    """Repainting the table must not pile up orphaned Edit buttons."""
    widget, _source, persons = loaded

    for _ in range(4):
        widget.refresh_person_table()

    buttons = widget.person_table.findChildren(QPushButton)
    assert len(buttons) == len(persons)


def test_switching_away_from_a_source_empties_the_table(loaded):
    """Selecting 'no source' clears the table instead of showing stale rows."""
    widget, _source, _persons = loaded
    assert widget.person_table.rowCount() == 4

    widget.set_source(None)

    assert widget.person_table.rowCount() == 0
    assert widget.person_table.columnCount() == 0


def test_widget_repaints_when_its_own_source_is_modified_elsewhere(loaded):
    """A notification for the displayed source repaints the table."""
    widget, source, persons = loaded
    source.classes[0].remove_person(persons[0])

    widget.source_manager.notify_source_modified(source.name)

    assert widget.person_table.rowCount() == 3
    assert "Zoe Zeman" not in row_texts(widget.person_table)


def test_widget_ignores_a_notification_for_another_source(loaded):
    """A modification of a different source does not repaint this one."""
    widget, source, persons = loaded
    source.classes[0].remove_person(persons[0])

    widget.source_manager.notify_source_modified("Some Other Source")

    assert widget.person_table.rowCount() == 4


# ===========================================================================
# _person_at_row / get_selected_persons (sorting)
# ===========================================================================

@pytest.mark.parametrize("sort_column", range(len(DEFAULT_COLUMNS) + 1))
def test_person_at_row_follows_any_sorted_column(loaded, sort_column):
    """After sorting by any column the row still reports the person it shows."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(sort_column, Qt.SortOrder.AscendingOrder)

    for row in range(widget.person_table.rowCount()):
        person = widget._person_at_row(row)
        assert person is not None
        assert widget.person_table.item(row, 0).text() == \
            f"{person.first_name} {person.last_name}"


def test_person_at_row_follows_a_descending_sort(loaded):
    """Descending order is handled just like ascending order."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.DescendingOrder)

    names = row_texts(widget.person_table)
    assert names == sorted(names, reverse=True)
    assert widget._person_at_row(0).last_name == "Zeman"


@pytest.mark.parametrize("row", [-1, 4, 99])
def test_person_at_row_returns_none_outside_the_table(loaded, row):
    """A row index that does not exist yields None instead of raising."""
    widget, _source, _persons = loaded
    assert widget._person_at_row(row) is None


def test_get_selected_persons_follows_the_sorted_view(loaded):
    """The selection maps to the persons shown, not to the source order."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    widget.person_table.selectRow(0)

    selected = widget.get_selected_persons()

    assert [p.last_name for p in selected] == ["Adamec"]
    assert selected[0] is widget._person_at_row(0)


def test_get_selected_persons_returns_the_rows_in_view_order(loaded):
    """A multi-row selection is returned top-down, as the user sees it."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.DescendingOrder)
    table = widget.person_table
    table.selectRow(0)
    table.selectionModel().select(
        table.model().index(2, 0),
        table.selectionModel().SelectionFlag.Select |
        table.selectionModel().SelectionFlag.Rows,
    )

    selected = widget.get_selected_persons()

    assert len(selected) == 2
    assert selected[0] is widget._person_at_row(0)
    assert selected[1] is widget._person_at_row(2)


def test_get_selected_persons_is_empty_without_a_source(widget):
    """No source means no selection, whatever the table contains."""
    assert widget.get_selected_persons() == []


def test_get_selected_persons_is_empty_without_a_selection(loaded):
    """Nothing selected returns an empty list rather than every person."""
    widget, _source, _persons = loaded
    widget.person_table.clearSelection()
    assert widget.get_selected_persons() == []


def test_the_edit_button_edits_the_person_of_its_own_row_after_sorting(loaded,
                                                                       monkeypatch):
    """Clicking Edit in a row opens the person that row displays."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)

    edited = []
    monkeypatch.setattr(widget, "edit_person", edited.append)

    table = widget.person_table
    for row in range(table.rowCount()):
        edited.clear()
        table.cellWidget(row, actions_column(table)).click()
        assert edited[0] is widget._person_at_row(row)


# ===========================================================================
# edit_person / edit_selected_person
# ===========================================================================

def test_edit_selected_person_warns_when_nothing_is_selected(loaded, dialogs):
    """Without a selection the user is told to pick a person first."""
    widget, _source, _persons = loaded
    widget.person_table.clearSelection()

    widget.edit_selected_person()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select a person")


def test_edit_selected_person_opens_the_row_the_user_sees(loaded, monkeypatch):
    """After sorting, Edit opens the person displayed in the selected row."""
    widget, _source, _persons = loaded
    opened = install_property_editor(monkeypatch)
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    widget.person_table.selectRow(1)
    expected = widget._person_at_row(1)

    widget.edit_selected_person()

    assert [person for person, _ in opened] == [expected]


def test_edit_person_refreshes_and_notifies_when_the_editor_is_accepted(
        loaded, monkeypatch, notifications):
    """Accepting the property editor marks the source as modified."""
    widget, source, persons = loaded
    install_property_editor(monkeypatch, result=ACCEPTED)

    widget.edit_person(persons[0])

    assert notifications == [source.name]


def test_edit_person_does_not_notify_when_the_editor_is_cancelled(
        loaded, monkeypatch, notifications):
    """Cancelling the property editor leaves the source untouched."""
    widget, _source, persons = loaded
    install_property_editor(monkeypatch, result=REJECTED)

    widget.edit_person(persons[0])

    assert notifications == []


def test_edit_person_refuses_a_readonly_source(widget, source_of, make_person,
                                               monkeypatch, dialogs):
    """A read-only source cannot be edited and no editor is opened."""
    person = make_person()
    widget.set_source(source_of([person], readonly=True))
    opened = install_property_editor(monkeypatch)
    dialogs.clear()

    widget.edit_person(person)

    assert opened == []
    assert dialogs.saw("read-only")


def test_edit_person_without_a_source_opens_nothing(widget, make_person,
                                                    monkeypatch, dialogs):
    """Editing before a source was chosen is a silent no-op."""
    opened = install_property_editor(monkeypatch)

    widget.edit_person(make_person())

    assert opened == []
    assert dialogs.calls == []


def test_edit_person_does_not_reserve_the_persons_own_username(
        loaded, monkeypatch):
    """The edited person's own user name is not reported as already taken."""
    widget, _source, persons = loaded
    opened = install_property_editor(monkeypatch)

    widget.edit_person(persons[0])           # zemanzoe

    taken = opened[0][1]
    assert "zemanzoe" not in taken
    assert {"novakmilan", "capkovaivana"} <= taken


@pytest.mark.bug
def test_edit_person_reserves_the_username_of_a_field_identical_twin(
        widget, source_of, make_person, monkeypatch):
    """Another student's user name stays taken even when all fields match."""
    first = make_person("Jan", "Novák", "6.A", ad_username="novakjan")
    twin = make_person("Jan", "Novák", "6.A", ad_username="novakjan")
    assert first == twin and first is not twin
    widget.set_source(source_of([first, twin]))
    opened = install_property_editor(monkeypatch)

    widget.edit_person(first)

    assert "novakjan" in opened[0][1]


# ===========================================================================
# bulk_set_enabled
# ===========================================================================

def test_bulk_set_enabled_enables_every_selected_account(loaded, dialogs,
                                                         notifications):
    """Confirming the question enables all selected accounts and reports it."""
    widget, source, persons = loaded
    widget.person_table.selectAll()
    dialogs.clear()

    widget.bulk_set_enabled(True)

    assert all(p.account_enabled for p in persons)
    assert dialogs.kinds() == ["question", "information"]
    assert dialogs.saw("Enabled 4 account(s)")
    assert notifications == [source.name]


def test_bulk_set_enabled_disables_only_the_selected_rows(loaded, dialogs):
    """Unselected persons keep their previous account state."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    widget.person_table.selectRow(0)
    selected = widget._person_at_row(0)
    others = [widget._person_at_row(r)
              for r in range(1, widget.person_table.rowCount())]
    before = [p.account_enabled for p in others]
    dialogs.clear()

    widget.bulk_set_enabled(True)

    assert selected.account_enabled is True
    assert [p.account_enabled for p in others] == before


def test_bulk_set_enabled_declined_changes_nothing(loaded, dialogs,
                                                    notifications):
    """Answering 'No' to the confirmation leaves every account alone."""
    widget, _source, persons = loaded
    widget.person_table.selectAll()
    before = [p.account_enabled for p in persons]
    dialogs.question_answer = QMessageBox.StandardButton.No
    dialogs.clear()

    widget.bulk_set_enabled(False)

    assert [p.account_enabled for p in persons] == before
    assert dialogs.kinds() == ["question"]
    assert notifications == []


def test_bulk_set_enabled_without_selection_warns(loaded, dialogs):
    """Nothing selected produces a warning instead of touching everybody."""
    widget, _source, persons = loaded
    widget.person_table.clearSelection()
    dialogs.clear()

    widget.bulk_set_enabled(False)

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select persons")
    assert [p.account_enabled for p in persons] == [True, False, False, True]


def test_bulk_set_enabled_marks_the_changed_persons_dirty(loaded, dialogs):
    """The change goes through the property setter, so dirty tracking sees it."""
    widget, _source, _persons = loaded
    widget.person_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    widget.person_table.selectRow(0)
    person = widget._person_at_row(0)
    assert not person.is_dirty()
    dialogs.clear()

    widget.bulk_set_enabled(True)

    assert "account_enabled" in person.get_dirty_fields()


# ===========================================================================
# _describe_person / _generate_credentials_for
# ===========================================================================

@pytest.mark.parametrize("first,last,class_name,expected", [
    ("Jan", "Novák", "6.A", "Jan Novák [6.A]"),
    ("", "Novák", "6.A", "Novák [6.A]"),
    ("Jan", "", "6.A", "Jan [6.A]"),
    ("", "", "6.A", "(no name) [6.A]"),
    ("Jan", "Novák", "", "Jan Novák [(no class)]"),
    ("", "", "", "(no name) [(no class)]"),
])
def test_describe_person_never_leaves_a_label_empty(make_person, first, last,
                                                    class_name, expected):
    """A person with missing data still gets a readable label."""
    person = make_person(first, last, class_name)
    assert ADManagementWidget._describe_person(person) == expected


def test_generate_credentials_for_fills_all_three_fields(widget, make_person):
    """A complete person gets user name, password and display name."""
    person = make_person("Jan", "Novák", "6.A")
    taken = set()

    assert widget._generate_credentials_for(person, taken) is None
    assert person.ad_username == "novakjan"
    assert person.ad_password
    assert person.ad_display_name == "Jan Novák (6.A)"
    assert taken == {"novakjan"}


def test_generate_credentials_for_avoids_names_already_taken(widget,
                                                             make_person):
    """The generated user name is unique and is added to the taken set."""
    taken = {"novakjan"}
    person = make_person("Jan", "Novák", "6.A")

    assert widget._generate_credentials_for(person, taken) is None
    assert person.ad_username == "novakjan2"
    assert taken == {"novakjan", "novakjan2"}


@pytest.mark.parametrize("first,last,expected", [
    ("", "Novák", "missing first name"),
    ("Jan", "", "missing last name"),
    ("   ", "Novák", "missing first name"),
    ("Jan", "\t", "missing last name"),
    ("", "", "missing first name and last name"),
])
def test_generate_credentials_for_reports_incomplete_names(widget, make_person,
                                                           first, last,
                                                           expected):
    """An incomplete name is reported instead of raising ValueError."""
    person = make_person(first, last, "6.A")

    assert widget._generate_credentials_for(person, set()) == expected
    assert person.ad_username is None
    assert person.ad_password is None


def test_generate_credentials_for_reports_an_unusable_name(widget,
                                                           make_person):
    """A name without a single usable character is reported, not raised."""
    person = make_person("Jan", "???", "6.A")

    reason = widget._generate_credentials_for(person, set())

    assert reason is not None
    assert "user name could not be generated" in reason
    assert person.ad_username is None


def test_generate_credentials_for_without_a_class_uses_the_plain_name(
        widget, make_person):
    """Without a class the display name is just 'First Last'."""
    person = make_person("Eva", "Malá", "")

    assert widget._generate_credentials_for(person, set()) is None
    assert person.ad_display_name == "Eva Malá"
    assert person.ad_username == "malaeva"


def test_generate_credentials_for_reports_a_broken_password_policy(
        widget, make_person, monkeypatch):
    """A password that cannot be generated skips the person with a reason."""
    def explode():
        raise ValueError("length is smaller than the required classes")

    monkeypatch.setattr(ad_management, "generate_password", explode)
    person = make_person("Jan", "Novák", "6.A")

    reason = widget._generate_credentials_for(person, set())

    assert reason is not None and "password could not be generated" in reason
    assert person.ad_password is None


# ===========================================================================
# generate_all_credentials / generate_missing_credentials
# ===========================================================================

@pytest.fixture
def mixed_source(widget, source_of, make_person):
    """
    A source with one healthy person and one of every broken variant.

    Returns ``(widget, dict_of_persons)``.
    """
    people = {
        "good": make_person("Jan", "Novák", "6.A"),
        "no_last": make_person("Adéla", "", "6.A", ad_username="old-adela",
                               ad_password="old-pass",
                               ad_display_name="old-display"),
        "no_first": make_person("", "Dvořák", "6.A"),
        "neither": make_person("", "", "6.A"),
        "no_class": make_person("Eva", "Malá", ""),
    }
    widget.set_source(source_of(list(people.values())))
    return widget, people


def test_generate_all_credentials_serves_the_good_persons(mixed_source,
                                                           dialogs):
    """Broken records never stop the students that can be processed."""
    widget, people = mixed_source
    dialogs.clear()

    widget.generate_all_credentials()

    assert people["good"].ad_username == "novakjan"
    assert people["good"].ad_password
    assert people["no_class"].ad_username == "malaeva"
    assert people["no_class"].ad_display_name == "Eva Malá"


def test_generate_all_credentials_keeps_the_values_of_skipped_persons(
        mixed_source, dialogs):
    """A skipped student keeps the credentials that were there before."""
    widget, people = mixed_source
    dialogs.clear()

    widget.generate_all_credentials()

    assert people["no_last"].ad_username == "old-adela"
    assert people["no_last"].ad_password == "old-pass"
    assert people["no_last"].ad_display_name == "old-display"
    assert people["no_first"].ad_username is None
    assert people["neither"].ad_username is None


def test_generate_all_credentials_reports_how_many_were_skipped(mixed_source,
                                                                 dialogs):
    """The summary names both counts in a single detailed message box."""
    widget, _people = mixed_source
    dialogs.clear()

    widget.generate_all_credentials()

    assert dialogs.kinds() == ["box_exec"]
    assert dialogs.saw("Generated credentials for 2 student(s).")
    assert dialogs.saw("3 student(s) were skipped.")


def test_generate_all_credentials_notifies_the_source_manager(mixed_source,
                                                              notifications):
    """Even a partly failed run marks the source as modified."""
    widget, _people = mixed_source
    widget.generate_all_credentials()
    assert notifications == ["Source"]


def test_generate_all_credentials_gives_twins_different_usernames(
        widget, source_of, make_person, dialogs):
    """Two students with the same name do not end up with the same login."""
    twins = [make_person("Jan", "Novák", "6.A"),
             make_person("Jan", "Novák", "6.A")]
    widget.set_source(source_of(twins))
    dialogs.clear()

    widget.generate_all_credentials()

    assert [p.ad_username for p in twins] == ["novakjan", "novakjan2"]


def test_generate_all_credentials_is_stable_when_run_twice(
        widget, source_of, make_person, dialogs):
    """A second run keeps the same user names and only refreshes passwords."""
    persons = [make_person("Jan", "Novák", "6.A"),
               make_person("Eva", "Malá", "6.A")]
    widget.set_source(source_of(persons))

    widget.generate_all_credentials()
    usernames = [p.ad_username for p in persons]
    passwords = [p.ad_password for p in persons]

    widget.generate_all_credentials()

    assert [p.ad_username for p in persons] == usernames
    assert [p.ad_password for p in persons] != passwords


def test_generate_all_credentials_succeeds_quietly_when_nothing_is_broken(
        widget, source_of, make_person, dialogs):
    """A clean run shows a plain success message, not a warning box."""
    widget.set_source(source_of([make_person("Jan", "Novák", "6.A")]))
    dialogs.clear()

    widget.generate_all_credentials()

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("Generated credentials for 1 students")


@pytest.mark.parametrize("kind,needle", [
    ("no_source", "select a source"),
    ("readonly", "read-only"),
    ("empty", "no persons found"),
])
def test_credential_generation_refuses_impossible_situations(
        widget, source_of, make_person, dialogs, kind, needle):
    """Missing, read-only and empty sources are rejected with an explanation."""
    person = make_person("Jan", "Novák", "6.A")
    if kind == "readonly":
        widget.set_source(source_of([person], readonly=True))
    elif kind == "empty":
        widget.set_source(Source(name="Empty", source_type="manual"))
    dialogs.clear()

    widget.generate_all_credentials()
    widget.generate_missing_credentials()

    assert widget._prepare_credential_generation() is None
    assert dialogs.saw(needle)
    assert len(dialogs.calls) == 3          # one refusal per attempt
    assert person.ad_username is None and person.ad_password is None


def test_generate_missing_credentials_leaves_complete_persons_alone(
        widget, source_of, make_person, dialogs):
    """A student who already has both values is not touched."""
    complete = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                           ad_password="Str0ng!pass")
    empty = make_person("Eva", "Malá", "6.A")
    widget.set_source(source_of([complete, empty]))
    dialogs.clear()

    widget.generate_missing_credentials()

    assert complete.ad_username == "novakjan"
    assert complete.ad_password == "Str0ng!pass"
    assert empty.ad_username == "malaeva"
    assert empty.ad_password


def test_generate_missing_credentials_reports_when_there_is_nothing_to_do(
        widget, source_of, make_person, dialogs):
    """All students complete -> a 'Nothing To Do' information message."""
    widget.set_source(source_of([
        make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                    ad_password="Str0ng!pass")]))
    dialogs.clear()

    widget.generate_missing_credentials()

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("already have credentials")


def test_generate_missing_credentials_skips_and_reports_broken_records(
        mixed_source, dialogs):
    """Incomplete names are skipped; complete ones are not even attempted."""
    widget, people = mixed_source
    dialogs.clear()

    widget.generate_missing_credentials()

    assert people["good"].ad_username == "novakjan"
    assert people["no_first"].ad_username is None
    assert people["neither"].ad_username is None
    # 'no_last' already has both values, so it is not processed at all
    assert people["no_last"].ad_username == "old-adela"
    assert dialogs.saw("2 student(s) were skipped.")


@pytest.mark.bug
def test_generate_missing_credentials_keeps_an_existing_username(
        widget, source_of, make_person, dialogs):
    """Only the missing value is filled in - an existing login must survive."""
    person = make_person("Petr", "Svoboda", "6.A", ad_username="svobodapetr")
    widget.set_source(source_of([person]))

    widget.generate_missing_credentials()

    assert person.ad_password
    assert person.ad_username == "svobodapetr"


# ===========================================================================
# _report_generation_result
# ===========================================================================

def test_report_generation_result_celebrates_a_clean_run(widget, dialogs):
    """Nothing skipped -> a plain success message."""
    dialogs.clear()
    widget._report_generation_result(7, [], "nothing to do")

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("Generated credentials for 7 students")


def test_report_generation_result_uses_the_nothing_message(widget, dialogs):
    """Nothing generated and nothing skipped -> the caller's own message."""
    dialogs.clear()
    widget._report_generation_result(0, [], "All students already have credentials")

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("All students already have credentials")


def test_report_generation_result_explains_that_skipped_data_is_kept(widget,
                                                                      dialogs):
    """The warning box promises that skipped students keep their data."""
    dialogs.clear()
    widget._report_generation_result(1, [("Adéla [6.A]", "missing last name")],
                                     "nothing to do")

    assert dialogs.kinds() == ["box_exec"]
    assert dialogs.saw("1 student(s) were skipped.")


def test_report_generation_result_truncates_a_long_skip_list(widget, dialogs,
                                                             monkeypatch):
    """At most 15 records are listed; the rest is summarised as '… and N more'."""
    details = []
    monkeypatch.setattr(QMessageBox, "setDetailedText",
                        lambda self, text: details.append(text))
    skipped = [(f"Student {i} [6.A]", "missing last name") for i in range(18)]

    widget._report_generation_result(2, skipped, "nothing to do")

    assert len(details) == 1
    assert details[0].count("•") == 15
    assert "… and 3 more" in details[0]


# ===========================================================================
# analyze_source
# ===========================================================================

@pytest.mark.bug
def test_analysis_lists_field_identical_persons_separately(qapp, source_of,
                                                           make_person):
    """Two students with identical data are two students, not one.

    Person is a dataclass, so twins compare equal and are unhashable; grouping
    the issues by value would collapse them into a single row - and putting
    them in a set used to raise TypeError outright.
    """
    from ui.ad_analysis_dialog import ADAnalysisDialog

    twins = [make_person("Jan", "Novák", "6.A"),
             make_person("Jan", "Novák", "6.A")]
    assert twins[0] == twins[1]
    dialog = ADAnalysisDialog(source_of(twins))

    assert dialog._table.rowCount() == 2
    assert "2</b> with problems" in dialog._summary_label.text()

    # ...and fixing them must give each its own user name
    dialog.fix_all()
    assert twins[0].ad_username != twins[1].ad_username


def test_analyze_source_opens_the_detailed_dialog(widget, source_of, make_person,
                                                 monkeypatch, dialogs):
    """The analysis is a resizable, actionable window - not a message box."""
    opened = {}

    class FakeDialog:
        def __init__(self, source, parent=None):
            opened["source"] = source
            self.changed = False

        def exec(self):
            return 0

    monkeypatch.setattr("operations.ad_management.ADAnalysisDialog", FakeDialog)
    source = source_of([make_person("Jan", "Novák", "6.A")])
    widget.set_source(source)

    widget.analyze_source()

    assert opened["source"] is source


def test_analyze_source_still_reports_an_empty_source(widget, source_of, dialogs):
    """An empty source needs no table - the message box is still right there."""
    widget.set_source(source_of([]))
    dialogs.clear()

    widget.analyze_source()

    assert dialogs.saw("contains no persons")


def test_analysis_dialog_lists_only_persons_with_problems(qapp, source_of,
                                                          make_person):
    """A complete person is not listed; an incomplete one is."""
    from ui.ad_analysis_dialog import ADAnalysisDialog

    complete = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                           ad_password="Str0ng!pass", ad_email="j@skola.cz",
                           ad_display_name="Jan Novák (6.A)")
    incomplete = make_person("Eva", "Malá", "6.A")
    dialog = ADAnalysisDialog(source_of([complete, incomplete]))

    names = [dialog._table.item(row, 0).text()
             for row in range(dialog._table.rowCount())]
    assert names == ["Eva Malá"]
    assert "1</b> with problems" in dialog._summary_label.text()


def test_analysis_dialog_highlights_the_offending_cells(qapp, source_of,
                                                        make_person):
    """The cell that is wrong is coloured, the ones that are fine are not."""
    from ui.ad_analysis_dialog import ADAnalysisDialog, ERROR_COLOR

    dialog = ADAnalysisDialog(source_of([make_person("Eva", "Malá", "6.A")]))

    # column 2 is the user name, which is missing -> error colour
    assert dialog._table.item(0, 2).background().color() == ERROR_COLOR
    # column 1 is the class, which is present -> untouched
    assert dialog._table.item(0, 1).background().color() != ERROR_COLOR


def test_analysis_dialog_fixes_one_person(qapp, source_of, make_person):
    """The per-row Fix button fills in what can be generated."""
    from ui.ad_analysis_dialog import ADAnalysisDialog

    person = make_person("Eva", "Malá", "6.A")
    dialog = ADAnalysisDialog(source_of([person]))

    dialog.fix_person(person)

    assert person.ad_username
    assert person.ad_password
    assert person.ad_display_name
    assert dialog.changed is True
    assert dialog._table.rowCount() == 0


def test_analysis_dialog_fixes_every_listed_person(qapp, source_of, make_person):
    """Fix All repairs everything that can be repaired."""
    from ui.ad_analysis_dialog import ADAnalysisDialog

    people = [make_person("Eva", "Malá", "6.A"),
              make_person("Jan", "Novák", "7.B")]
    dialog = ADAnalysisDialog(source_of(people))

    dialog.fix_all()

    assert all(p.ad_username and p.ad_password for p in people)
    assert len({p.ad_username for p in people}) == 2, "user names must be unique"
    assert dialog._table.rowCount() == 0


def test_analysis_dialog_never_invents_a_missing_surname(qapp, source_of,
                                                         make_person):
    """A name only a human can supply is reported, never generated."""
    from ui.ad_analysis_dialog import ADAnalysisDialog

    person = make_person("Madonna", "", "6.A")
    dialog = ADAnalysisDialog(source_of([person]))

    dialog.fix_person(person)

    assert person.ad_username is None
    assert person.last_name == ""
    assert "missing last name" in dialog._status_label.text()
    # still listed, because the problem is still there
    assert dialog._table.rowCount() == 1

def test_analyze_source_without_a_source_warns(widget, dialogs):
    """Analysis needs a source and says so."""
    dialogs.clear()
    widget.analyze_source()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select a source")


def test_analyze_source_reports_an_empty_source(widget, dialogs):
    """An empty source produces the dedicated 'no persons' message."""
    widget.set_source(Source(name="Empty", source_type="manual"))
    dialogs.clear()

    widget.analyze_source()

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("contains no persons")


def test_analyze_source_rejects_an_incompatible_source(widget, dialogs):
    """An object that is not a Source is refused instead of crashing."""
    class NotASource:
        name = "weird"
        readonly = False

    widget.current_source = NotASource()
    dialogs.clear()

    widget.analyze_source()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("does not support analysis")


def test_analyze_source_survives_a_broken_source(widget, dialogs):
    """An exception inside the validator is reported, not propagated."""
    class Broken:
        name = "broken"
        readonly = False

        def get_all_persons(self):
            return [object()]

    widget.current_source = Broken()
    dialogs.clear()

    widget.analyze_source()

    assert dialogs.kinds() == ["critical"]
    assert dialogs.saw("Analysis Error")


# ===========================================================================
# open_password_format / select_columns / ColumnVisibilityDialog
# ===========================================================================

def test_open_password_format_confirms_the_active_policy(widget, dialogs):
    """Accepting the dialog explains how new passwords will look."""
    set_password_policy(PasswordPolicy(length=14), persist=False)
    dialogs.clear()

    widget.open_password_format()

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("Password Format Saved")
    assert dialogs.saw("14 characters")
    assert dialogs.saw("Existing passwords are not changed")


def test_open_password_format_cancelled_says_nothing(widget, dialogs):
    """Cancelling the dialog leaves the user without a message."""
    dialogs.exec_result = REJECTED
    dialogs.clear()

    widget.open_password_format()

    assert dialogs.calls == []


def test_open_password_format_repaints_the_table(loaded, dialogs):
    """The table is repainted because validation depends on the policy."""
    widget, _source, persons = loaded
    widget.person_table.setRowCount(0)

    widget.open_password_format()

    assert widget.person_table.rowCount() == len(persons)


def test_select_columns_applies_the_new_selection(loaded, monkeypatch):
    """The columns ticked in the dialog become the visible columns."""
    widget, _source, _persons = loaded
    monkeypatch.setattr(ColumnVisibilityDialog, "get_visible_columns",
                        lambda self: ["Name", "Password"])

    widget.select_columns()

    assert widget.visible_columns == ["Name", "Password"]
    assert headers(widget.person_table) == ["Name", "Password", "Actions"]


def test_select_columns_cancelled_keeps_the_previous_columns(loaded, dialogs,
                                                             monkeypatch):
    """A cancelled dialog changes nothing."""
    widget, _source, _persons = loaded
    monkeypatch.setattr(ColumnVisibilityDialog, "get_visible_columns",
                        lambda self: ["Name"])
    dialogs.exec_result = REJECTED

    widget.select_columns()

    assert widget.visible_columns == list(DEFAULT_COLUMNS)


def test_select_columns_refuses_to_hide_everything(loaded, dialogs,
                                                   monkeypatch):
    """Deselecting every column is rejected with a warning."""
    widget, _source, _persons = loaded
    monkeypatch.setattr(ColumnVisibilityDialog, "get_visible_columns",
                        lambda self: [])
    dialogs.clear()

    widget.select_columns()

    assert widget.visible_columns == list(DEFAULT_COLUMNS)
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("at least one column")


def test_select_columns_round_trip_keeps_the_current_selection(loaded):
    """Confirming the dialog unchanged is idempotent."""
    widget, _source, _persons = loaded
    before = list(widget.visible_columns)

    widget.select_columns()

    assert widget.visible_columns == before


def test_column_dialog_reports_the_preselected_columns_in_column_order(qapp):
    """The result follows the declared column order, not the tick order."""
    dialog = ColumnVisibilityDialog(list(ALL_COLUMNS), ["Dirty", "Name"])
    assert dialog.get_visible_columns() == ["Name", "Dirty"]


def test_column_dialog_ignores_unknown_preselected_columns(qapp):
    """A stored column that no longer exists simply disappears."""
    dialog = ColumnVisibilityDialog(["Name", "Class"], ["Name", "Gone"])
    assert dialog.get_visible_columns() == ["Name"]


def test_column_dialog_select_and_deselect_all(qapp):
    """The two helper buttons tick and untick every entry."""
    dialog = ColumnVisibilityDialog(list(ALL_COLUMNS), ["Name"])

    dialog.select_all()
    assert dialog.get_visible_columns() == list(ALL_COLUMNS)

    dialog.deselect_all()
    assert dialog.get_visible_columns() == []


def test_column_dialog_does_not_touch_the_callers_list(qapp):
    """The dialog works on a copy of the visible-column list."""
    visible = ["Name"]
    dialog = ColumnVisibilityDialog(list(ALL_COLUMNS), visible)
    dialog.select_all()

    assert visible == ["Name"]
    assert dialog.get_visible_columns() != visible


# ===========================================================================
# bulk_edit_persons (widget <-> BulkEditDialog)
# ===========================================================================

@pytest.mark.integration
def test_bulk_edit_applies_the_dialog_changes_exactly_once(loaded, dialogs,
                                                           monkeypatch):
    """The widget must not re-apply what the dialog already wrote."""
    widget, _source, persons = loaded
    widget.available_groups = [group("Students")]

    def configure(dialog):
        dialog.change_password.setChecked(True)
        dialog.change_groups.setChecked(True)
        dialog.select_groups_radio.setChecked(True)
        dialog.selected_groups = list(widget.available_groups)
        dialog.group_action_combo.setCurrentIndex(0)      # add

    record = install_bulk_dialog(monkeypatch, configure)
    widget.person_table.selectAll()

    widget.bulk_edit_persons()

    assert all(record["passwords"])
    assert record["passwords"] == [p.ad_password for p in persons]
    assert all(len(p.group_memberships) == 1 for p in persons)


@pytest.mark.integration
def test_bulk_edit_never_creates_instruction_attributes_on_a_person(
        loaded, monkeypatch):
    """group_action/groups/home_directory_template are commands, not fields."""
    widget, _source, persons = loaded
    template = group("Teachers")

    def configure(dialog):
        dialog.change_groups.setChecked(True)
        dialog.select_groups_radio.setChecked(True)
        dialog.selected_groups = [template]
        dialog.group_action_combo.setCurrentIndex(2)      # replace

    install_bulk_dialog(monkeypatch, configure)
    widget.person_table.selectAll()

    widget.bulk_edit_persons()

    for person in persons:
        for bogus in ("group_action", "groups", "home_directory_template",
                      "generate_password"):
            assert not hasattr(person, bogus)
        assert person.group_memberships == [template]


@pytest.mark.integration
def test_bulk_edit_notifies_the_source_manager(loaded, monkeypatch,
                                               notifications):
    """A successful bulk edit marks the source as modified exactly once."""
    widget, source, _persons = loaded
    install_bulk_dialog(monkeypatch,
                        lambda d: d.change_enabled.setChecked(True))
    widget.person_table.selectAll()

    widget.bulk_edit_persons()

    assert notifications == [source.name]


def test_bulk_edit_without_selection_warns(loaded, dialogs):
    """Bulk editing nothing is refused."""
    widget, _source, _persons = loaded
    widget.person_table.clearSelection()
    dialogs.clear()

    widget.bulk_edit_persons()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select persons")


def test_bulk_edit_refuses_a_readonly_source(widget, source_of, make_person,
                                             dialogs, monkeypatch):
    """A read-only source cannot be bulk edited."""
    person = make_person()
    widget.set_source(source_of([person], readonly=True))
    install_bulk_dialog(monkeypatch, lambda d: d.change_enabled.setChecked(True))
    widget.person_table.selectAll()
    dialogs.clear()

    widget.bulk_edit_persons()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("read-only")
    assert person.account_enabled is False


@pytest.mark.integration
def test_bulk_edit_cancelled_leaves_everything_untouched(loaded, dialogs,
                                                         monkeypatch,
                                                         notifications):
    """A rejected dialog neither changes persons nor notifies anybody."""
    widget, _source, persons = loaded
    before = [(p.ad_password, p.account_enabled) for p in persons]

    class Rejecting:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return REJECTED

    monkeypatch.setattr(ad_management, "BulkEditDialog", Rejecting)
    widget.person_table.selectAll()

    widget.bulk_edit_persons()

    assert [(p.ad_password, p.account_enabled) for p in persons] == before
    assert notifications == []


# ===========================================================================
# BulkEditDialog.accept_changes
# ===========================================================================

@pytest.fixture
def two_persons(make_person):
    """Two unrelated students to bulk edit."""
    return [make_person("Jan", "Novák", "6.A"),
            make_person("Eva", "Malá", "6.A")]


def test_bulk_dialog_refuses_an_empty_change_set(two_persons, dialogs):
    """Pressing OK without ticking anything warns and keeps the dialog open."""
    dialog = BulkEditDialog(two_persons)
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is False
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("no changes selected")
    assert dialog.changes == {}


@pytest.mark.parametrize("enabled", [True, False])
def test_bulk_dialog_sets_the_account_state(two_persons, dialogs, enabled):
    """The account state is written to every selected person."""
    for person in two_persons:
        person.account_enabled = not enabled
    dialog = BulkEditDialog(two_persons)
    dialog.change_enabled.setChecked(True)
    dialog.enabled_value.setChecked(enabled)
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is True
    assert [p.account_enabled for p in two_persons] == [enabled, enabled]
    assert dialog.changes == {"account_enabled": enabled}
    assert dialogs.saw("Successfully applied changes to 2 persons")


@pytest.mark.parametrize("checkbox,value_box,field", [
    ("change_must_change", "must_change_value", "password_must_change"),
    ("change_cannot_change", "cannot_change_value", "password_cannot_change"),
    ("change_never_expires", "never_expires_value", "password_never_expires"),
])
@pytest.mark.parametrize("value", [True, False])
def test_bulk_dialog_sets_each_password_policy_flag(two_persons, dialogs,
                                                    checkbox, value_box, field,
                                                    value):
    """Every policy check box maps to its own Person field."""
    for person in two_persons:
        setattr(person, field, not value)
    dialog = BulkEditDialog(two_persons)
    getattr(dialog, checkbox).setChecked(True)
    getattr(dialog, value_box).setChecked(value)

    assert apply_bulk(dialog, dialogs) is True
    assert [getattr(p, field) for p in two_persons] == [value, value]
    assert dialog.changes == {field: value}


def test_bulk_dialog_generates_a_separate_password_per_person(two_persons,
                                                              dialogs):
    """Every selected person gets their own fresh password."""
    for person in two_persons:
        person.ad_password = "old"
    dialog = BulkEditDialog(two_persons)
    dialog.change_password.setChecked(True)

    assert apply_bulk(dialog, dialogs) is True

    passwords = [p.ad_password for p in two_persons]
    assert all(pwd and pwd != "old" for pwd in passwords)
    assert passwords[0] != passwords[1]
    assert len(set(passwords[0])) > 1


def test_bulk_dialog_counts_a_failing_password_generator_as_a_failure(
        two_persons, dialogs, monkeypatch):
    """One broken password does not abort the run for the other students."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("policy is unusable")
        return "Generated1!"

    monkeypatch.setattr("ui.bulk_edit_dialog.generate_password", flaky)
    dialog = BulkEditDialog(two_persons)
    dialog.change_password.setChecked(True)
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is True
    assert two_persons[0].ad_password is None
    assert two_persons[1].ad_password == "Generated1!"
    assert dialogs.saw("Failed for 1 persons")


def test_bulk_dialog_adds_groups_without_duplicating_them(two_persons,
                                                          dialogs):
    """'Add' is idempotent for a group the person is already a member of."""
    students, teachers = group("Students"), group("Teachers")
    two_persons[0].add_to_group(students)
    dialog = BulkEditDialog(two_persons, available_groups=[students, teachers])
    dialog.change_groups.setChecked(True)
    dialog.select_groups_radio.setChecked(True)
    dialog.selected_groups = [students, teachers]
    dialog.group_action_combo.setCurrentIndex(0)

    assert apply_bulk(dialog, dialogs) is True
    assert [p.get_group_dns() for p in two_persons] == \
        [[students.dn, teachers.dn], [students.dn, teachers.dn]]


def test_bulk_dialog_removes_only_the_listed_groups(two_persons, dialogs):
    """'Remove' keeps every membership that was not selected."""
    students, teachers = group("Students"), group("Teachers")
    for person in two_persons:
        person.add_to_group(students)
        person.add_to_group(teachers)
    dialog = BulkEditDialog(two_persons, available_groups=[students, teachers])
    dialog.change_groups.setChecked(True)
    dialog.select_groups_radio.setChecked(True)
    dialog.selected_groups = [students]
    dialog.group_action_combo.setCurrentIndex(1)

    assert apply_bulk(dialog, dialogs) is True
    assert [p.get_group_dns() for p in two_persons] == \
        [[teachers.dn], [teachers.dn]]


def test_bulk_dialog_removing_a_group_nobody_has_is_harmless(two_persons,
                                                             dialogs):
    """Removing a membership that does not exist changes nothing."""
    students, teachers = group("Students"), group("Teachers")
    two_persons[0].add_to_group(students)
    dialog = BulkEditDialog(two_persons, available_groups=[teachers])
    dialog.change_groups.setChecked(True)
    dialog.select_groups_radio.setChecked(True)
    dialog.selected_groups = [teachers]
    dialog.group_action_combo.setCurrentIndex(1)

    assert apply_bulk(dialog, dialogs) is True
    assert [p.get_group_dns() for p in two_persons] == [[students.dn], []]


def test_bulk_dialog_replaces_groups_from_a_template(two_persons, dialogs):
    """'Replace' via a template drops the old memberships."""
    old, new = group("Old"), group("New")
    two_persons[0].add_to_group(old)
    template = GroupTemplate(name="Year 6", groups=[new])
    dialog = BulkEditDialog(two_persons, available_templates=[template])
    dialog.change_groups.setChecked(True)
    dialog.apply_template_radio.setChecked(True)
    dialog.template_combo.setCurrentIndex(1)
    dialog.group_action_combo.setCurrentIndex(2)

    assert apply_bulk(dialog, dialogs) is True
    assert [p.get_group_dns() for p in two_persons] == [[new.dn], [new.dn]]
    assert dialog.changes["group_action"] == "replace"
    assert dialog.changes["groups"] == [new]


def test_bulk_dialog_replace_gives_every_person_its_own_list(two_persons,
                                                             dialogs):
    """The replaced membership lists must not be shared between persons."""
    students = group("Students")
    template = GroupTemplate(name="Year 6", groups=[students])
    dialog = BulkEditDialog(two_persons, available_templates=[template])
    dialog.change_groups.setChecked(True)
    dialog.apply_template_radio.setChecked(True)
    dialog.template_combo.setCurrentIndex(1)
    dialog.group_action_combo.setCurrentIndex(2)
    assert apply_bulk(dialog, dialogs) is True

    two_persons[0].add_to_group(group("Extra"))

    assert len(two_persons[1].group_memberships) == 1
    assert len(template.groups) == 1


def test_bulk_dialog_demands_at_least_one_group(two_persons, dialogs):
    """Ticking 'modify groups' without choosing any is refused."""
    dialog = BulkEditDialog(two_persons, available_groups=[group("Students")])
    dialog.change_groups.setChecked(True)
    dialog.select_groups_radio.setChecked(True)
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is False
    assert dialogs.saw("select at least one group")
    assert dialog.changes == {}


def test_bulk_dialog_demands_a_group_template(two_persons, dialogs):
    """The template mode needs a template that is actually selected."""
    template = GroupTemplate(name="Year 6", groups=[group("Students")])
    dialog = BulkEditDialog(two_persons, available_templates=[template])
    dialog.change_groups.setChecked(True)
    dialog.apply_template_radio.setChecked(True)
    dialog.template_combo.setCurrentIndex(0)          # "-- Select Template --"
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is False
    assert dialogs.saw("select a template")


def home_template_index(dialog, pattern="\\\\server\\home\\{username}"):
    """Index of the built-in home-directory template *pattern*."""
    for index in range(dialog.home_template_combo.count()):
        if dialog.home_template_combo.itemData(index) == pattern:
            return index
    raise AssertionError(f"template {pattern!r} not offered by the dialog")


def test_bulk_dialog_generates_a_home_path_per_person(dialogs, make_person):
    """Each person gets their own path and the shared drive letter."""
    persons = [make_person("Jan", "Novák", "6.A", ad_username="novakjan"),
               make_person("Eva", "Malá", "6.A", ad_username="malaeva")]
    dialog = BulkEditDialog(persons)
    dialog.change_home.setChecked(True)
    dialog.home_template_combo.setCurrentIndex(home_template_index(dialog))
    dialog.home_drive_input.setText("  H:  ")

    assert apply_bulk(dialog, dialogs) is True
    assert [p.home_directory for p in persons] == \
        ["\\\\server\\home\\novakjan", "\\\\server\\home\\malaeva"]
    assert [p.home_drive for p in persons] == ["H:", "H:"]


def test_bulk_dialog_stores_no_drive_when_the_field_is_blank(dialogs,
                                                             make_person):
    """A blank drive letter is stored as None, not as an empty string."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan")
    dialog = BulkEditDialog([person])
    dialog.change_home.setChecked(True)
    dialog.home_template_combo.setCurrentIndex(home_template_index(dialog))
    dialog.home_drive_input.setText("   ")

    assert apply_bulk(dialog, dialogs) is True
    assert person.home_drive is None
    assert dialog.changes["home_drive"] is None


def test_bulk_dialog_reports_a_failing_home_template(dialogs, make_person):
    """A person the template cannot be filled in for is counted as failed."""
    without_username = make_person("Jan", "Novák", "6.A")
    without_username.home_directory = "\\\\old\\path"
    with_username = make_person("Eva", "Malá", "6.A", ad_username="malaeva")
    dialog = BulkEditDialog([without_username, with_username])
    dialog.change_home.setChecked(True)
    dialog.change_enabled.setChecked(True)
    dialog.enabled_value.setChecked(True)
    dialog.home_template_combo.setCurrentIndex(home_template_index(dialog))
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is True

    assert without_username.home_directory == "\\\\old\\path"
    assert with_username.home_directory == "\\\\server\\home\\malaeva"
    assert [p.account_enabled for p in (without_username, with_username)] == \
        [True, True]
    assert dialogs.saw("Applied changes to 1 persons")
    assert dialogs.saw("Failed for 1 persons")


def test_bulk_dialog_demands_a_home_template(two_persons, dialogs):
    """The home-directory section needs a template before it can run."""
    dialog = BulkEditDialog(two_persons)
    dialog.change_home.setChecked(True)
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is False
    assert dialogs.saw("select a home directory template")


def test_bulk_dialog_aborts_when_the_confirmation_is_declined(two_persons,
                                                              dialogs):
    """Answering 'No' to the confirmation applies nothing at all."""
    dialog = BulkEditDialog(two_persons)
    dialog.change_enabled.setChecked(True)
    dialog.enabled_value.setChecked(True)
    dialogs.question_answer = QMessageBox.StandardButton.No
    dialogs.clear()

    assert apply_bulk(dialog, dialogs) is False
    assert [p.account_enabled for p in two_persons] == [False, False]
    assert dialog.changes == {}
    assert dialogs.kinds() == ["question"]


def test_bulk_dialog_summary_lists_every_selected_change(two_persons):
    """The summary label mirrors what will happen when OK is pressed."""
    dialog = BulkEditDialog(two_persons)
    assert "No changes selected" in dialog.summary_label.text()

    dialog.change_enabled.setChecked(True)
    dialog.enabled_value.setChecked(True)
    dialog.change_password.setChecked(True)
    dialog.update_summary()

    summary = dialog.summary_label.text()
    assert "Account status: enabled" in summary
    assert "Generate new passwords" in summary
    assert f"{len(two_persons)} persons" in summary


def test_bulk_dialog_group_selection_is_disabled_until_it_is_requested(
        two_persons):
    """The group widgets only come alive when 'modify groups' is ticked."""
    dialog = BulkEditDialog(two_persons, available_groups=[group("Students")])
    assert not dialog.select_groups_radio.isEnabled()
    assert not dialog.group_action_combo.isEnabled()

    dialog.change_groups.setChecked(True)

    assert dialog.select_groups_radio.isEnabled()
    assert dialog.select_groups_radio.isChecked()      # sensible default
    assert dialog.group_action_combo.isEnabled()


def test_bulk_dialog_without_groups_explains_group_management(two_persons,
                                                              dialogs):
    """Selecting groups without any configured points at Group Management."""
    dialog = BulkEditDialog(two_persons, available_groups=[])
    dialogs.clear()

    dialog.select_groups_dialog()

    assert dialogs.kinds() == ["information"]
    assert dialogs.saw("no groups available")
    assert dialog.selected_groups == []


# ===========================================================================
# PropertyEditorDialog - generators
# ===========================================================================

@pytest.fixture
def editor(make_person):
    """Factory: ``editor(person, {"taken"})`` -> a PropertyEditorDialog."""
    def _build(person=None, existing=None, **kwargs):
        return PropertyEditorDialog(person or make_person(),
                                    existing if existing is not None else set(),
                                    **kwargs)
    return _build


def test_editor_generates_a_username_without_touching_the_person(editor,
                                                                 make_person):
    """Generate fills the field only - the person is written on OK."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)

    dialog.generate_username()

    assert dialog.username_input.text() == "novakjan"
    assert person.ad_username is None


def test_editor_generated_username_is_folded_to_ascii(editor, make_person):
    """Diacritics are removed so the login is a valid sAMAccountName."""
    dialog = editor(make_person("Žofie", "Křížová", "6.A"))

    dialog.generate_username()

    assert dialog.username_input.text() == "krizovazofie"


def test_editor_generated_username_avoids_the_taken_ones(editor, make_person):
    """An occupied login gets a numeric suffix."""
    dialog = editor(make_person("Jan", "Novák", "6.A"), {"novakjan"})

    dialog.generate_username()

    assert dialog.username_input.text() == "novakjan2"


@pytest.mark.parametrize("first,last", [
    ("", "Novák"),
    ("Jan", ""),
    ("   ", "   "),
])
def test_editor_username_generation_needs_both_names(editor, make_person,
                                                     dialogs, first, last):
    """Without both names the user is warned and nothing is generated."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))
    dialog.first_name_input.setText(first)
    dialog.last_name_input.setText(last)
    dialogs.clear()

    dialog.generate_username()

    assert dialog.username_input.text() == ""
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("first and last name required")


@pytest.mark.bug
@pytest.mark.parametrize("first,last", [
    ("Анна", "Иванова"),
    ("Jan", "???"),
])
def test_editor_username_generation_survives_a_non_latin_name(editor,
                                                              make_person,
                                                              dialogs, first,
                                                              last):
    """A name that cannot be folded to ASCII is reported, not raised."""
    dialog = editor(make_person(first, last, "6.A"))
    dialogs.clear()

    dialog.generate_username()          # must not raise

    assert dialog.username_input.text() == ""
    assert dialogs.kinds() == ["warning"]


def test_editor_generates_a_policy_conforming_password(editor, make_person):
    """The generated password follows the active policy and is random."""
    set_password_policy(PasswordPolicy(length=16), persist=False)
    dialog = editor(make_person("Jan", "Novák", "6.A"))

    dialog.generate_password()
    first = dialog.password_display.text()
    dialog.generate_password()
    second = dialog.password_display.text()

    assert len(first) == len(second) == 16
    assert first != second

    policy = get_password_policy()
    for pool in (policy.lowercase_pool, policy.uppercase_pool,
                 policy.digit_pool, policy.special_pool):
        assert any(character in pool for character in first)


def test_editor_generates_the_display_name_from_the_form(editor, make_person):
    """The display name uses the *current* form values, not the stored ones."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)
    dialog.class_name_input.setText("7.C")

    dialog.generate_display_name()

    assert dialog.display_name_input.text() == "Jan Novák (7.C)"
    assert person.ad_display_name is None


@pytest.mark.parametrize("first,last,class_name", [
    ("", "Novák", "6.A"),
    ("Jan", "", "6.A"),
    ("Jan", "Novák", "  "),
])
def test_editor_display_name_generation_needs_name_and_class(editor,
                                                             make_person,
                                                             dialogs, first,
                                                             last, class_name):
    """All three values are required before a display name can be built."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))
    dialog.first_name_input.setText(first)
    dialog.last_name_input.setText(last)
    dialog.class_name_input.setText(class_name)
    dialogs.clear()

    dialog.generate_display_name()

    assert dialog.display_name_input.text() == ""
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("class required")


def test_editor_generates_the_home_path_into_the_field_only(editor,
                                                            make_person):
    """Generating a home path fills the input, the person is written on OK."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan")
    dialog = editor(person)
    dialog.home_template_combo.setCurrentIndex(home_template_index(dialog))

    dialog.generate_home_path()

    assert dialog.home_path_input.text() == "\\\\server\\home\\novakjan"
    assert person.home_directory is None


def test_editor_home_path_reports_an_unusable_template(editor, make_person,
                                                       dialogs):
    """A placeholder that cannot be resolved is reported as a warning."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))   # no user name
    dialog.home_template_combo.setCurrentIndex(home_template_index(dialog))
    dialogs.clear()

    dialog.generate_home_path()

    assert dialog.home_path_input.text() == ""
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("failed to generate path")


def test_editor_home_path_without_a_template_warns(editor, dialogs):
    """The Generate button needs a template first."""
    dialog = editor()
    dialogs.clear()

    dialog.generate_home_path()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select a template first")


@pytest.mark.bug
@pytest.mark.parametrize("method,needle", [
    ("generate_username", "Generated username"),
    ("generate_password", "Generated new password"),
    ("generate_display_name", "Generated display name"),
])
def test_editor_keeps_the_generation_confirmation_visible(editor, make_person,
                                                          method, needle):
    """What was generated stays readable in the validation area."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))

    getattr(dialog, method)()

    assert needle in dialog.validation_text.toPlainText()


# ===========================================================================
# PropertyEditorDialog - validation, groups and accept_changes
# ===========================================================================

def test_editor_validation_lists_the_missing_ad_fields(editor, make_person):
    """A bare person is invalid and every missing field is named."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))

    assert dialog.validate() is False

    shown = dialog.validation_text.toPlainText()
    assert "ad_username" in shown
    assert "ad_password" in shown
    assert "ad_display_name" in shown


def test_editor_validation_passes_for_a_complete_person(editor, make_person):
    """A fully filled form validates and says so."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                         ad_password="Str0ng!pass1",
                         ad_display_name="Jan Novák (6.A)",
                         ad_email="novakjan@skola.cz")
    dialog = editor(person)

    assert dialog.validate() is True
    assert "All validations passed" in dialog.validation_text.toPlainText()


def test_editor_validation_uses_the_form_not_the_person(editor, make_person):
    """Validation follows what is typed, and never writes to the person."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)
    dialog.username_input.setText("novakjan")
    dialog.password_display.setText("Str0ng!pass1")
    dialog.display_name_input.setText("Jan Novák (6.A)")

    assert dialog.validate() is True
    assert person.ad_username is None
    assert person.ad_password is None
    assert not person.is_dirty()


def test_editor_clearing_all_groups_needs_a_confirmation(editor, make_person,
                                                         dialogs):
    """Answering 'No' to 'Remove all group memberships?' keeps them."""
    person = make_person("Jan", "Novák", "6.A")
    students = group("Students")
    person.add_to_group(students)
    dialog = editor(person)
    dialogs.question_answer = QMessageBox.StandardButton.No
    dialogs.clear()

    dialog.clear_all_groups()

    assert person.group_memberships == [students]
    assert dialogs.kinds() == ["question"]


def test_editor_group_list_shows_a_placeholder_when_empty(editor,
                                                          make_person):
    """An empty membership list shows one disabled placeholder row."""
    dialog = editor(make_person("Jan", "Novák", "6.A"))

    assert dialog.current_groups_list.count() == 1
    assert dialog.current_groups_list.item(0).text() == "No groups assigned"


@pytest.mark.bug
def test_editor_cancel_restores_the_group_memberships(editor, make_person,
                                                      dialogs):
    """Cancelling the editor must not leave group changes behind."""
    person = make_person("Jan", "Novák", "6.A")
    students = group("Students")
    person.add_to_group(students)
    dialog = editor(person)

    dialog.clear_all_groups()          # confirmed by the stub
    dialog.reject()

    assert person.group_memberships == [students]


def test_editor_accept_writes_every_field(editor, make_person, dialogs):
    """OK copies the whole form onto the person, whitespace trimmed."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)
    dialog.first_name_input.setText("  Žofie ")
    dialog.last_name_input.setText(" Křížová ")
    dialog.class_name_input.setText(" 9.B ")
    dialog.username_input.setText(" krizovazofie ")
    dialog.password_display.setText("Str0ng!pass1")
    dialog.display_name_input.setText(" Žofie Křížová (9.B) ")
    dialog.email_input.setText(" zofie@skola.cz ")
    dialog.description_input.setText(" pupil ")
    dialog.home_path_input.setText("  \\\\srv\\home\\zk ")
    dialog.home_drive_input.setText(" H: ")
    dialog.change_password_checkbox.setChecked(True)
    dialog.cannot_change_checkbox.setChecked(True)
    dialog.never_expires_checkbox.setChecked(True)
    dialog.account_enabled_checkbox.setChecked(True)

    dialog.accept_changes()

    assert dialog.result() == ACCEPTED
    assert (person.first_name, person.last_name, person.class_name) == \
        ("Žofie", "Křížová", "9.B")
    assert person.ad_username == "krizovazofie"
    assert person.ad_password == "Str0ng!pass1"
    assert person.ad_display_name == "Žofie Křížová (9.B)"
    assert person.ad_email == "zofie@skola.cz"
    assert person.ad_description == "pupil"
    assert person.home_directory == "\\\\srv\\home\\zk"
    assert person.home_drive == "H:"
    assert (person.password_must_change, person.password_cannot_change,
            person.password_never_expires, person.account_enabled) == \
        (True, True, True, True)


def test_editor_accept_marks_the_changed_fields_dirty(editor, make_person):
    """Writing through the property setters feeds the dirty tracking."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                         ad_password="Str0ng!pass1",
                         ad_display_name="Jan Novák (6.A)")
    dialog = editor(person)
    dialog.email_input.setText("novakjan@skola.cz")

    dialog.accept_changes()

    assert person.get_dirty_fields() == {"ad_email"}


def test_editor_accept_turns_cleared_optional_fields_into_none(editor,
                                                               make_person):
    """An emptied optional field becomes None, never an empty string."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                         ad_password="Str0ng!pass1",
                         ad_display_name="Jan Novák (6.A)",
                         ad_email="novakjan@skola.cz", ad_description="pupil",
                         home_directory="\\\\srv\\home\\jn", home_drive="H:")
    dialog = editor(person)
    dialog.email_input.setText("")
    dialog.description_input.setText("   ")
    dialog.home_path_input.setText("")
    dialog.home_drive_input.setText("  ")

    dialog.accept_changes()

    assert person.ad_email is None
    assert person.ad_description is None
    assert person.home_directory is None
    assert person.home_drive is None


@pytest.mark.parametrize("field,value", [
    ("first_name_input", "   "),
    ("last_name_input", ""),
    ("class_name_input", "\t"),
])
def test_editor_accept_refuses_incomplete_basic_data(editor, make_person,
                                                     dialogs, field, value):
    """First name, last name and class are mandatory."""
    person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                         ad_password="Str0ng!pass1",
                         ad_display_name="Jan Novák (6.A)")
    dialog = editor(person)
    getattr(dialog, field).setText(value)
    dialogs.clear()

    dialog.accept_changes()

    assert dialog.result() != ACCEPTED
    assert (person.first_name, person.last_name, person.class_name) == \
        ("Jan", "Novák", "6.A")
    assert dialogs.saw("first name, last name and class are required")


def test_editor_accept_asks_before_saving_an_invalid_person(editor,
                                                            make_person,
                                                            dialogs):
    """Confirming the validation warning writes the incomplete data anyway."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)
    dialog.first_name_input.setText("Honza")
    dialogs.clear()

    dialog.accept_changes()

    assert dialogs.kinds() == ["question"]
    assert dialogs.saw("apply changes anyway")
    assert person.first_name == "Honza"
    assert dialog.result() == ACCEPTED


def test_editor_accept_declining_the_warning_changes_nothing(editor,
                                                             make_person,
                                                             dialogs):
    """Answering 'No' to the validation warning aborts the save."""
    person = make_person("Jan", "Novák", "6.A")
    dialog = editor(person)
    dialog.first_name_input.setText("Honza")
    dialogs.question_answer = QMessageBox.StandardButton.No
    dialogs.clear()

    dialog.accept_changes()

    assert person.first_name == "Jan"
    assert not person.is_dirty()
    assert dialog.result() != ACCEPTED
