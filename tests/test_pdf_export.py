"""
Unit tests for the PDF export stack.

Covered modules
---------------
``operations.pdf_export``
    ``PDFExportWidget`` - loading a source into the table, the row/record
    mapping that survives sorting, bulk editing, column management and the
    export guards.
``utils.pdf_generator``
    ``PDFGenerator`` - real PDFs rendered into ``BytesIO`` / ``QBuffer`` plus
    the column-width, row-height and validation helpers.
``ui.pdf_export_settings_widget``
    validation of the export settings and the settings round trip.
``ui.pdf_export_dialog``
    the dialog with ``WEB_ENGINE_AVAILABLE`` forced to ``False`` so no real
    browser view is ever created.

Every test that can reach a modal window asks for the ``dialogs`` /
``accept_dialogs`` fixtures - without them the suite would block forever.
Nothing is written outside ``tmp_path`` and no network is touched.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect in the production code.
"""

import logging
import re
from io import BytesIO
from pathlib import Path

import pytest

from PyQt6.QtCore import QBuffer, QPoint, Qt
from PyQt6.QtWidgets import (
    QDialog, QMenu, QMessageBox, QTableWidgetItem, QTableWidgetSelectionRange,
)

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

import ui.pdf_export_dialog as pdf_export_dialog_module
import operations.pdf_export as pdf_export_module
from models import ADStatus
from operations.pdf_export import PDFExportWidget
from ui.pdf_export_dialog import PDFExportDialog
from ui.pdf_export_settings_widget import PDFExportSettingsWidget
from utils.pdf_generator import PageMarginDrawer, PDFGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The columns ``_person_to_dict`` is expected to produce, in order.
EXPECTED_COLUMNS = [
    'First Name', 'Last Name', 'Class', 'Username', 'Password', 'Email',
    'Display Name', 'Enabled', 'Must Change Password',
    'Cannot Change Password', 'Password Never Expires', 'AD Status',
]


def settings(**overrides):
    """A complete, valid PDFGenerator settings dict; keyword args override it."""
    base = {
        'selected_columns': ['First Name', 'Last Name'],
        'column_widths': {},
        'orientation': 'portrait',
        'margins': {'top': 2.0, 'bottom': 2.0, 'left': 2.0, 'right': 2.0},
        'row_height': {'auto': True, 'value': 0.8},
        'text_wrap': True,
        'alignment': {'horizontal': 'LEFT', 'vertical': 'MIDDLE'},
        'header_color': '#4a6fa5',
        'font': {'family': 'Helvetica', 'size': 10},
        'include_title': False,
        'title_text': '',
        'include_date': False,
    }
    base.update(overrides)
    return base


def assert_is_pdf(data):
    """Fail unless *data* really is a complete PDF document."""
    assert isinstance(data, bytes), f"expected bytes, got {type(data)}"
    assert data.startswith(b"%PDF-"), f"not a PDF header: {data[:12]!r}"
    assert data.rstrip().endswith(b"%%EOF"), f"truncated PDF: {data[-12:]!r}"


def page_count(data):
    """Number of pages of a generated PDF, read from its page-tree ``/Count``."""
    counts = re.findall(rb"/Count\s+(\d+)", data)
    assert counts, "PDF has no /Count entry - cannot determine the page count"
    return max(int(value) for value in counts)


def select_classes(widget, *names):
    """Select exactly the named classes in the widget's class list."""
    widget.class_list.clearSelection()
    for index in range(widget.class_list.count()):
        item = widget.class_list.item(index)
        if item.text() in names:
            item.setSelected(True)


def cell_texts(table, column=0):
    """The visible texts of one table column, top to bottom."""
    return [table.item(row, column).text() for row in range(table.rowCount())]


def record_indices(table, column=0):
    """The record index every visible row of *column* points at."""
    return [table.item(row, column).data(Qt.ItemDataRole.UserRole)
            for row in range(table.rowCount())]


class FakeColumnDialog:
    """Stand-in for ColumnManagementDialog with a scripted outcome."""

    visible = []
    new_custom = []
    deleted = []
    accepted = True

    def __init__(self, all_columns, visible_columns, custom_columns, parent=None):
        self.all_columns = all_columns
        self.visible_columns = visible_columns
        self.custom_columns = custom_columns

    def exec(self):
        return QDialog.DialogCode.Accepted if self.accepted else QDialog.DialogCode.Rejected

    def get_visible_columns(self):
        return list(self.visible)

    def get_new_custom_columns(self):
        return list(self.new_custom)

    def get_deleted_columns(self):
        return list(self.deleted)


class FakeBulkDialog:
    """Stand-in for BulkEditTableDialog with a scripted set of changes."""

    changes = {}
    accepted = True
    seen = []

    def __init__(self, selected_rows, columns, table_data, parent=None):
        FakeBulkDialog.seen = list(selected_rows)
        self.columns = columns
        self.table_data = table_data

    def exec(self):
        return QDialog.DialogCode.Accepted if self.accepted else QDialog.DialogCode.Rejected

    def get_changes(self):
        return dict(self.changes)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def no_web_engine(monkeypatch):
    """Pretend PyQt6-WebEngine is missing so no real web view is ever built."""
    monkeypatch.setattr(pdf_export_dialog_module, "WEB_ENGINE_AVAILABLE", False)
    monkeypatch.setattr(pdf_export_dialog_module, "QWebEngineView", None)


@pytest.fixture
def no_menus(monkeypatch):
    """Neutralise ``QMenu.exec`` - a real context menu would block the suite."""
    shown = []
    monkeypatch.setattr(
        QMenu, "exec",
        lambda self, *args, **kwargs: shown.append(
            [action.text() for action in self.actions()]) or None,
    )
    return shown


@pytest.fixture
def school(make_source, make_class, make_person):
    """
    A source with three classes:

    ``6.A`` holds Cyril/Adam/Bruno (deliberately *not* alphabetical),
    ``7.B`` is empty and ``8.C`` holds two students with diacritics.
    """
    source = make_source("School", [])
    source.add_class(make_class("6.A", persons=[
        make_person("Cyril", "Third", "6.A", ad_username="cyril",
                    ad_password="pw-cyril"),
        make_person("Adam", "First", "6.A", ad_username="adam",
                    ad_password="pw-adam"),
        make_person("Bruno", "Second", "6.A", ad_username="bruno",
                    ad_password="pw-bruno"),
    ]))
    source.add_class(make_class("7.B", persons=[]))
    source.add_class(make_class("8.C", persons=[
        make_person("Žofie", "Černá", "8.C", ad_username="cernazo"),
        make_person("Řehoř", "Dvořák", "8.C", ad_username="dvorare"),
    ]))
    return source


@pytest.fixture
def widget(qapp, source_manager):
    """A bare PDFExportWidget with no source attached."""
    return PDFExportWidget(source_manager)


@pytest.fixture
def loaded(widget, school, dialogs):
    """A PDFExportWidget showing the three students of class 6.A."""
    widget.set_source(school)
    select_classes(widget, "6.A")
    widget.load_table_from_source()
    dialogs.clear()
    return widget


@pytest.fixture
def settings_widget(qapp):
    """A settings widget over three columns, all of them selected."""
    return PDFExportSettingsWidget(['First Name', 'Last Name', 'Password'],
                                   ['First Name', 'Last Name', 'Password'])


@pytest.fixture
def export_dialog(qapp, no_web_engine, accept_dialogs):
    """A PDF export dialog over two rows, built without the web engine."""
    rows = [
        {'First Name': 'Žofie', 'Last Name': 'Černá', 'Password': 'abc'},
        {'First Name': 'Adam', 'Last Name': 'First', 'Password': 'def'},
    ]
    dialog = PDFExportDialog(
        table_data=rows,
        column_headers=['First Name', 'Last Name', 'Password'],
        visible_columns=['First Name', 'Last Name'],
    )
    yield dialog
    dialog.close()


# ===========================================================================
# PDFExportWidget._person_to_dict
# ===========================================================================

@pytest.mark.gui
class TestPersonToDict:

    def test_every_exported_field_is_taken_from_the_person(self, widget, make_person):
        """A fully populated person is mapped field by field."""
        person = make_person(
            "Jan", "Novák", "6.A", ad_username="novakjan",
            ad_password="Str0ng!", ad_email="jan@skola.cz",
            ad_display_name="Jan Novák", account_enabled=True,
            password_must_change=True, ad_status=ADStatus.SYNC_SUCCEEDED,
        )
        assert widget._person_to_dict(person) == {
            'First Name': 'Jan', 'Last Name': 'Novák', 'Class': '6.A',
            'Username': 'novakjan', 'Password': 'Str0ng!',
            'Email': 'jan@skola.cz', 'Display Name': 'Jan Novák',
            'Enabled': 'Yes', 'Must Change Password': 'Yes',
            'Cannot Change Password': 'No', 'Password Never Expires': 'No',
            'AD Status': ADStatus.SYNC_SUCCEEDED.label,
        }

    def test_column_order_follows_the_documented_layout(self, widget, make_person):
        """The dict order decides the table columns, so it must be stable."""
        assert list(widget._person_to_dict(make_person())) == EXPECTED_COLUMNS

    def test_unset_ad_fields_become_empty_strings_not_none(self, widget, make_person):
        """``None`` must never reach the PDF as the text "None"."""
        row = widget._person_to_dict(make_person())
        for column in ('Username', 'Password', 'Email', 'Display Name'):
            assert row[column] == ''

    @pytest.mark.parametrize("attribute,column", [
        ("account_enabled", "Enabled"),
        ("password_must_change", "Must Change Password"),
        ("password_cannot_change", "Cannot Change Password"),
        ("password_never_expires", "Password Never Expires"),
    ])
    @pytest.mark.parametrize("value,expected", [(True, "Yes"), (False, "No")])
    def test_flags_are_rendered_as_yes_or_no(self, widget, make_person,
                                             attribute, column, value, expected):
        """Every boolean account flag is exported as a readable Yes/No."""
        person = make_person(**{attribute: value})
        assert widget._person_to_dict(person)[column] == expected

    def test_diacritics_survive_the_conversion_unchanged(self, widget, make_person):
        """Czech names must not be transliterated on the way into the table."""
        person = make_person("Žofie", "Dvořáková", "9.Č")
        row = widget._person_to_dict(person)
        assert (row['First Name'], row['Last Name'], row['Class']) == \
            ("Žofie", "Dvořáková", "9.Č")

    def test_ad_status_is_exported_as_readable_text_not_the_enum_repr(
            self, widget, make_person):
        """A PDF is read by a person: 'Not checked', not 'ADStatus.UNKNOWN'."""
        person = make_person(ad_status=ADStatus.UNKNOWN)
        assert widget._person_to_dict(person)['AD Status'] == "Not checked"

    @pytest.mark.parametrize("status", list(ADStatus))
    def test_no_status_reaches_the_pdf_as_an_enum_repr(self, widget, make_person,
                                                       status):
        exported = widget._person_to_dict(make_person(ad_status=status))['AD Status']
        assert "ADStatus" not in exported and exported


# ===========================================================================
# PDFExportWidget - class selection panel
# ===========================================================================

@pytest.mark.gui
class TestClassSelectionPanel:

    def test_setting_a_source_lists_its_classes_in_source_order(self, widget,
                                                                school):
        """The list mirrors the source, including the empty class."""
        widget.set_source(school)
        assert [widget.class_list.item(i).text()
                for i in range(widget.class_list.count())] == ["6.A", "7.B", "8.C"]

    def test_setting_a_new_source_replaces_the_previous_class_list(self, widget,
                                                                   school,
                                                                   make_source):
        """Switching sources must not leave classes of the old one behind."""
        widget.set_source(school)
        widget.set_source(make_source("Other", [("1.A", 0)]))

        assert widget.class_list.count() == 1
        assert widget.class_list.item(0).text() == "1.A"

    def test_clearing_the_source_empties_the_class_list(self, widget, school):
        """``set_source(None)`` is the "no source" state, not a crash."""
        widget.set_source(school)
        widget.set_source(None)

        assert widget.current_source is None
        assert widget.class_list.count() == 0

    def test_select_all_and_deselect_all_cover_every_class(self, widget, school):
        """The two helper buttons are exact opposites."""
        widget.set_source(school)

        widget.select_all_classes()
        assert len(widget.class_list.selectedItems()) == 3

        widget.select_no_classes()
        assert widget.class_list.selectedItems() == []


# ===========================================================================
# PDFExportWidget.load_table_from_source
# ===========================================================================

@pytest.mark.gui
class TestLoadTableFromSource:

    def test_without_a_source_it_warns_and_loads_nothing(self, widget, dialogs):
        """No source selected - the user gets a warning, the table stays empty."""
        widget.load_table_from_source()
        assert dialogs.kinds() == ["warning"]
        assert dialogs.saw("No Source")
        assert widget.table_data == []
        assert widget.table.rowCount() == 0

    def test_without_a_class_selection_it_warns_and_loads_nothing(self, widget,
                                                                  school, dialogs):
        """A source but no selected class - nothing is loaded."""
        widget.set_source(school)
        dialogs.clear()
        widget.load_table_from_source()
        assert dialogs.saw("No Classes")
        assert widget.table_data == []

    def test_loading_one_class_fills_table_data_headers_and_counter(self, widget,
                                                                    school, dialogs):
        """The happy path populates data, headers and the row counter."""
        widget.set_source(school)
        select_classes(widget, "6.A")
        widget.load_table_from_source()

        assert [row['First Name'] for row in widget.table_data] == \
            ["Cyril", "Adam", "Bruno"]
        assert widget.column_headers == EXPECTED_COLUMNS
        assert widget.visible_columns == EXPECTED_COLUMNS
        assert widget.custom_columns == []
        assert widget.selected_classes == ["6.A"]
        assert widget.table.rowCount() == 3
        assert widget.table.columnCount() == len(EXPECTED_COLUMNS)
        assert widget.row_count_label.text() == "Rows: 3"
        assert dialogs.calls == []

    def test_rows_follow_source_order_not_selection_order(self, widget, school,
                                                          dialogs):
        """Classes are collected in source order however they were selected."""
        widget.set_source(school)
        select_classes(widget, "8.C", "6.A")
        widget.load_table_from_source()
        assert [row['Class'] for row in widget.table_data] == \
            ["6.A", "6.A", "6.A", "8.C", "8.C"]
        assert [row['First Name'] for row in widget.table_data][-2:] == \
            ["Žofie", "Řehoř"]

    def test_first_load_asks_no_overwrite_question(self, widget, school, dialogs):
        """There is nothing to lose yet, so no confirmation is shown."""
        widget.set_source(school)
        select_classes(widget, "6.A")
        widget.load_table_from_source()
        assert "question" not in dialogs.kinds()

    def test_declining_the_overwrite_question_keeps_the_old_data(self, loaded,
                                                                 dialogs):
        """Answering No to "Clear Table Data" must abort the load."""
        loaded.table_data[0]['Password'] = "hand-typed"
        dialogs.question_answer = QMessageBox.StandardButton.No
        select_classes(loaded, "8.C")

        loaded.load_table_from_source()

        assert dialogs.saw("Clear Table Data")
        assert loaded.selected_classes == ["6.A"]
        assert loaded.table_data[0]['Password'] == "hand-typed"
        assert len(loaded.table_data) == 3

    def test_accepting_the_overwrite_question_replaces_the_data(self, loaded,
                                                               dialogs):
        """Answering Yes throws the current table away."""
        dialogs.question_answer = QMessageBox.StandardButton.Yes
        select_classes(loaded, "8.C")

        loaded.load_table_from_source()

        assert [row['First Name'] for row in loaded.table_data] == ["Žofie", "Řehoř"]
        assert loaded.selected_classes == ["8.C"]
        assert loaded.table.rowCount() == 2

    def test_reloading_drops_previously_added_custom_columns(self, loaded, dialogs):
        """A fresh load rebuilds the column set from the source."""
        loaded.column_headers.append("Note")
        loaded.custom_columns.append("Note")
        loaded.visible_columns.append("Note")
        select_classes(loaded, "8.C")

        loaded.load_table_from_source()

        assert loaded.custom_columns == []
        assert "Note" not in loaded.column_headers
        assert all("Note" not in row for row in loaded.table_data)

    def test_selecting_only_an_empty_class_informs_the_user(self, loaded, dialogs):
        """A class without persons produces an explanatory message."""
        select_classes(loaded, "7.B")
        loaded.load_table_from_source()
        assert dialogs.saw("No Data")
        assert loaded.table_data == []

    @pytest.mark.bug
    def test_selecting_only_an_empty_class_clears_the_displayed_table(self, loaded,
                                                                      dialogs):
        """After an empty load the table must not keep showing the old students."""
        select_classes(loaded, "7.B")
        loaded.load_table_from_source()

        assert loaded.table.rowCount() == 0, "stale rows are still displayed"
        assert loaded.row_count_label.text() == "Rows: 0"


# ===========================================================================
# PDFExportWidget._populate_table
# ===========================================================================

@pytest.mark.gui
class TestPopulateTable:

    def test_every_cell_remembers_the_record_it_shows(self, loaded):
        """The UserRole payload is what makes sorting safe."""
        for row in range(loaded.table.rowCount()):
            for column in range(loaded.table.columnCount()):
                item = loaded.table.item(row, column)
                assert item.data(Qt.ItemDataRole.UserRole) == row

    def test_only_visible_columns_are_shown_and_are_matched_by_name(self, loaded):
        """Hiding columns changes the table, never the underlying records."""
        loaded.visible_columns = ['Username', 'First Name']
        loaded._populate_table()

        assert loaded.table.columnCount() == 2
        headers = [loaded.table.horizontalHeaderItem(i).text() for i in range(2)]
        assert headers == ['Username', 'First Name']
        assert cell_texts(loaded.table, 0) == ['cyril', 'adam', 'bruno']
        assert all('Password' in row for row in loaded.table_data)

    def test_a_column_missing_from_a_record_renders_as_an_empty_cell(self, loaded):
        """Records that never got a custom column show a blank, not a KeyError."""
        loaded.column_headers.append("Note")
        loaded.visible_columns = ['First Name', 'Note']
        loaded.table_data[1]['Note'] = "second"
        loaded._populate_table()

        notes = cell_texts(loaded.table, 1)
        assert notes == ['', 'second', '']

    def test_populating_keeps_sorting_enabled_for_the_user(self, loaded):
        """Sorting is switched off only while the cells are being written."""
        assert loaded.table.isSortingEnabled() is True

    def test_row_counter_matches_the_number_of_records(self, loaded):
        """The counter is refreshed from the data, not from the widget."""
        loaded.table_data = loaded.table_data[:2]
        loaded._populate_table()
        assert loaded.row_count_label.text() == "Rows: 2"


# ===========================================================================
# row <-> record mapping (_data_index_for_row / _sync_table_data)
# ===========================================================================

@pytest.mark.gui
class TestRowToRecordMapping:

    def test_unsorted_rows_map_onto_themselves(self, loaded):
        """Without sorting the visual row and the record index coincide."""
        assert [loaded._data_index_for_row(row) for row in range(3)] == [0, 1, 2]

    def test_after_sorting_the_row_maps_to_the_record_it_displays(self, loaded):
        """Sorting reorders the rows - the mapping must follow the items."""
        loaded.table.sortItems(0, Qt.SortOrder.AscendingOrder)

        assert cell_texts(loaded.table, 0) == ["Adam", "Bruno", "Cyril"]
        assert record_indices(loaded.table, 0) == [1, 2, 0]
        assert [loaded._data_index_for_row(row) for row in range(3)] == [1, 2, 0]

    def test_out_of_range_rows_return_none(self, loaded):
        """Asking for a row that does not exist is not an error."""
        assert loaded._data_index_for_row(99) is None
        assert loaded._data_index_for_row(-1) is None

    def test_rows_without_an_index_payload_return_none(self, loaded):
        """Hand-made items carry no record index and must not be guessed."""
        loaded.table.setSortingEnabled(False)
        for column in range(loaded.table.columnCount()):
            loaded.table.setItem(0, column, QTableWidgetItem("manual"))

        assert loaded._data_index_for_row(0) is None

    def test_a_stale_index_beyond_the_data_is_rejected(self, loaded):
        """Indices left over from a bigger table must not be trusted."""
        loaded.table.setSortingEnabled(False)
        item = QTableWidgetItem("ghost")
        item.setData(Qt.ItemDataRole.UserRole, 99)
        loaded.table.setItem(0, 0, item)
        for column in range(1, loaded.table.columnCount()):
            loaded.table.setItem(0, column, QTableWidgetItem(""))

        assert loaded._data_index_for_row(0) is None

    def test_sync_writes_an_edit_to_the_record_the_row_shows(self, loaded):
        """The regression that mixed up user names and passwords after sorting."""
        loaded.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        username_column = EXPECTED_COLUMNS.index('Username')
        loaded.table.item(0, username_column).setText("edited-adam")

        loaded._sync_table_data()

        by_name = {row['First Name']: row['Username'] for row in loaded.table_data}
        assert by_name == {"Adam": "edited-adam", "Bruno": "bruno", "Cyril": "cyril"}

    def test_sync_is_idempotent(self, loaded):
        """Running the sync twice changes nothing the second time."""
        loaded.table.sortItems(0, Qt.SortOrder.DescendingOrder)
        loaded._sync_table_data()
        first = [dict(row) for row in loaded.table_data]
        loaded._sync_table_data()
        assert loaded.table_data == first

    def test_sync_only_touches_visible_columns(self, loaded):
        """Hidden values are kept even though they are not on screen."""
        loaded.visible_columns = ['First Name']
        loaded._populate_table()
        loaded.table.item(0, 0).setText("Renamed")

        loaded._sync_table_data()

        assert loaded.table_data[0]['First Name'] == "Renamed"
        assert loaded.table_data[0]['Username'] == "cyril"

    def test_sync_skips_unmatched_rows_and_logs_them(self, loaded, caplog):
        """An unmappable row is reported, never written to a random record."""
        loaded.table.setSortingEnabled(False)
        for column in range(loaded.table.columnCount()):
            loaded.table.setItem(1, column, QTableWidgetItem("orphan"))

        with caplog.at_level(logging.WARNING, logger="operations.pdf_export"):
            loaded._sync_table_data()

        assert "could not be matched" in caplog.text
        assert loaded.table_data[1]['First Name'] == "Adam"
        assert all(row['First Name'] != "orphan" for row in loaded.table_data)


# ===========================================================================
# PDFExportWidget.bulk_edit_rows
# ===========================================================================

@pytest.mark.gui
class TestBulkEditRows:

    def test_an_empty_table_is_reported_instead_of_opening_the_dialog(self, widget,
                                                                     dialogs):
        """Nothing to edit - the user is told so."""
        widget.bulk_edit_rows()
        assert dialogs.saw("No Data")

    def test_without_a_selection_the_user_is_asked_to_select_rows(self, loaded,
                                                                  dialogs):
        """Bulk edit needs rows; none selected means a warning."""
        loaded.table.clearSelection()
        loaded.bulk_edit_rows()
        assert dialogs.saw("No Selection")

    def test_changes_land_on_the_records_the_selected_rows_show(self, loaded,
                                                               dialogs, monkeypatch):
        """Selecting the first visual row of a sorted table must edit Adam."""
        loaded.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        loaded.table.selectRow(0)

        FakeBulkDialog.changes = {'Password': 'BULK'}
        FakeBulkDialog.accepted = True
        monkeypatch.setattr(pdf_export_module, "BulkEditTableDialog", FakeBulkDialog)

        loaded.bulk_edit_rows()

        assert FakeBulkDialog.seen == [1]
        by_name = {row['First Name']: row['Password'] for row in loaded.table_data}
        assert by_name == {"Adam": "BULK", "Bruno": "pw-bruno", "Cyril": "pw-cyril"}
        assert dialogs.saw("Applied changes to 1 row")

    def test_every_selected_row_is_translated_exactly_once(self, loaded, dialogs,
                                                           monkeypatch):
        """A row selection yields one index per row, not one per cell."""
        loaded.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        last_column = loaded.table.columnCount() - 1
        for row in (0, 2):
            loaded.table.setRangeSelected(
                QTableWidgetSelectionRange(row, 0, row, last_column), True)

        FakeBulkDialog.changes = {'Email': 'x@y.cz'}
        FakeBulkDialog.accepted = True
        monkeypatch.setattr(pdf_export_module, "BulkEditTableDialog", FakeBulkDialog)

        loaded.bulk_edit_rows()

        assert sorted(FakeBulkDialog.seen) == [0, 1]
        assert len(FakeBulkDialog.seen) == len(set(FakeBulkDialog.seen))
        edited = {row['First Name'] for row in loaded.table_data
                  if row['Email'] == 'x@y.cz'}
        assert edited == {"Adam", "Cyril"}

    def test_a_cancelled_dialog_changes_nothing(self, loaded, dialogs, monkeypatch):
        """Rejecting the bulk-edit dialog leaves the records untouched."""
        loaded.table.selectRow(0)
        FakeBulkDialog.changes = {'Password': 'NOPE'}
        FakeBulkDialog.accepted = False
        monkeypatch.setattr(pdf_export_module, "BulkEditTableDialog", FakeBulkDialog)

        loaded.bulk_edit_rows()

        assert [row['Password'] for row in loaded.table_data] == \
            ["pw-cyril", "pw-adam", "pw-bruno"]
        assert not dialogs.saw("Applied changes")


# ===========================================================================
# column management
# ===========================================================================

@pytest.mark.gui
class TestColumnManagement:

    def test_manage_columns_without_data_tells_the_user_to_load_first(self, widget,
                                                                      dialogs):
        """No columns exist yet, so the dialog is not opened."""
        widget.manage_columns()
        assert dialogs.saw("No Data")

    def test_a_new_custom_column_is_added_to_every_record(self, loaded, dialogs,
                                                          monkeypatch):
        """Custom columns start empty on all rows and become visible."""
        FakeColumnDialog.visible = EXPECTED_COLUMNS + ["Poznámka"]
        FakeColumnDialog.new_custom = ["Poznámka"]
        FakeColumnDialog.deleted = []
        FakeColumnDialog.accepted = True
        monkeypatch.setattr(pdf_export_module, "ColumnManagementDialog",
                            FakeColumnDialog)

        loaded.manage_columns()

        assert loaded.column_headers[-1] == "Poznámka"
        assert loaded.custom_columns == ["Poznámka"]
        assert all(row["Poznámka"] == '' for row in loaded.table_data)
        assert loaded.table.columnCount() == len(EXPECTED_COLUMNS) + 1

    def test_a_deleted_custom_column_disappears_everywhere(self, loaded, dialogs,
                                                            monkeypatch):
        """Deleting removes the column from headers, visibility and the data."""
        loaded.column_headers.append("Note")
        loaded.custom_columns.append("Note")
        loaded.visible_columns.append("Note")
        for row in loaded.table_data:
            row["Note"] = "x"

        FakeColumnDialog.visible = list(EXPECTED_COLUMNS)
        FakeColumnDialog.new_custom = []
        FakeColumnDialog.deleted = ["Note"]
        FakeColumnDialog.accepted = True
        monkeypatch.setattr(pdf_export_module, "ColumnManagementDialog",
                            FakeColumnDialog)

        loaded.manage_columns()

        assert "Note" not in loaded.column_headers
        assert "Note" not in loaded.custom_columns
        assert "Note" not in loaded.visible_columns
        assert all("Note" not in row for row in loaded.table_data)

    def test_a_cancelled_column_dialog_changes_nothing(self, loaded, dialogs,
                                                        monkeypatch):
        """Rejecting the dialog keeps the previous column layout."""
        before = list(loaded.visible_columns)
        FakeColumnDialog.visible = ["First Name"]
        FakeColumnDialog.new_custom = ["Never"]
        FakeColumnDialog.deleted = ["Password"]
        FakeColumnDialog.accepted = False
        monkeypatch.setattr(pdf_export_module, "ColumnManagementDialog",
                            FakeColumnDialog)

        loaded.manage_columns()

        assert loaded.visible_columns == before
        assert "Never" not in loaded.column_headers
        assert all("Password" in row for row in loaded.table_data)

    def test_hiding_a_column_keeps_its_values_in_the_records(self, loaded, dialogs,
                                                             monkeypatch):
        """Visibility is a view setting - the data must survive it."""
        FakeColumnDialog.visible = ["First Name", "Username"]
        FakeColumnDialog.new_custom = []
        FakeColumnDialog.deleted = []
        FakeColumnDialog.accepted = True
        monkeypatch.setattr(pdf_export_module, "ColumnManagementDialog",
                            FakeColumnDialog)

        loaded.manage_columns()

        assert loaded.visible_columns == ["First Name", "Username"]
        assert loaded.column_headers == EXPECTED_COLUMNS
        assert loaded.table_data[0]['Password'] == "pw-cyril"


@pytest.mark.gui
class TestRenameColumn:

    def test_renaming_moves_the_values_to_the_new_key(self, loaded, dialogs):
        """Headers, visibility and every record follow the new name."""
        dialogs.text_answer = ("Heslo", True)
        loaded.rename_column(EXPECTED_COLUMNS.index('Password'), 'Password')

        assert 'Heslo' in loaded.column_headers
        assert 'Password' not in loaded.column_headers
        assert loaded.visible_columns[EXPECTED_COLUMNS.index('Password')] == 'Heslo'
        assert loaded.table_data[0]['Heslo'] == "pw-cyril"
        assert 'Password' not in loaded.table_data[0]

    def test_renaming_a_custom_column_updates_the_custom_list(self, loaded, dialogs):
        """Otherwise the column could no longer be deleted afterwards."""
        loaded.column_headers.append("Note")
        loaded.custom_columns.append("Note")
        loaded.visible_columns.append("Note")
        for row in loaded.table_data:
            row["Note"] = "n"

        dialogs.text_answer = ("Poznámka", True)
        loaded.rename_column(len(loaded.visible_columns) - 1, "Note")

        assert loaded.custom_columns == ["Poznámka"]
        assert loaded.table_data[0]["Poznámka"] == "n"

    def test_a_duplicate_name_is_refused(self, loaded, dialogs):
        """Two columns with the same name would collide in the record dicts."""
        dialogs.text_answer = ("Username", True)
        loaded.rename_column(0, 'First Name')

        assert dialogs.saw("Duplicate Name")
        assert loaded.column_headers == EXPECTED_COLUMNS
        assert loaded.table_data[0]['First Name'] == "Cyril"

    @pytest.mark.parametrize("answer", [
        ("Neues", False),   # user pressed Cancel
        ("First Name", True),  # unchanged name
        ("", True),          # empty name
    ])
    def test_cancelled_or_pointless_renames_do_nothing(self, loaded, dialogs, answer):
        """Only a real, confirmed new name may touch the data."""
        dialogs.text_answer = answer
        loaded.rename_column(0, 'First Name')

        assert loaded.column_headers == EXPECTED_COLUMNS
        assert loaded.table_data[0]['First Name'] == "Cyril"


@pytest.mark.gui
class TestDeleteColumn:

    def test_confirmed_deletion_removes_the_column_from_data_and_view(self, loaded,
                                                                      dialogs):
        """Deleting a custom column drops it everywhere at once."""
        loaded.column_headers.append("Note")
        loaded.custom_columns.append("Note")
        loaded.visible_columns.append("Note")
        for row in loaded.table_data:
            row["Note"] = "x"
        dialogs.question_answer = QMessageBox.StandardButton.Yes

        loaded.delete_column("Note")

        assert "Note" not in loaded.column_headers
        assert "Note" not in loaded.visible_columns
        assert "Note" not in loaded.custom_columns
        assert all("Note" not in row for row in loaded.table_data)
        assert loaded.table.columnCount() == len(EXPECTED_COLUMNS)

    def test_declined_deletion_keeps_everything(self, loaded, dialogs):
        """Answering No to the confirmation must be a complete no-op."""
        dialogs.question_answer = QMessageBox.StandardButton.No
        loaded.delete_column("Password")

        assert "Password" in loaded.column_headers
        assert loaded.table_data[0]['Password'] == "pw-cyril"


@pytest.mark.gui
class TestClearTable:

    def test_confirmed_clear_resets_data_columns_and_counter(self, loaded, dialogs):
        """Everything the widget holds is dropped at once."""
        dialogs.question_answer = QMessageBox.StandardButton.Yes
        loaded.clear_table()

        assert loaded.table_data == []
        assert loaded.column_headers == []
        assert loaded.visible_columns == []
        assert loaded.custom_columns == []
        assert loaded.table.rowCount() == 0
        assert loaded.table.columnCount() == 0
        assert loaded.row_count_label.text() == "Rows: 0"

    def test_declined_clear_keeps_the_table(self, loaded, dialogs):
        """The confirmation really guards the data."""
        dialogs.question_answer = QMessageBox.StandardButton.No
        loaded.clear_table()

        assert len(loaded.table_data) == 3
        assert loaded.table.rowCount() == 3

    def test_clearing_an_empty_table_asks_nothing(self, widget, dialogs):
        """No data, no confirmation dialog."""
        widget.clear_table()
        assert dialogs.calls == []


@pytest.mark.gui
class TestHeaderContextMenu:

    def test_a_click_outside_any_column_opens_no_menu(self, loaded, no_menus):
        """``logicalIndexAt`` returns -1 there - the handler must bail out."""
        loaded.visible_columns = []
        loaded.table.setColumnCount(0)

        loaded.show_header_context_menu(QPoint(5, 5))

        assert no_menus == []


# ===========================================================================
# PDFExportWidget.export_to_pdf
# ===========================================================================

@pytest.mark.gui
class TestExportToPdfGuards:

    def test_an_empty_table_is_refused_before_any_dialog_is_built(self, widget,
                                                                  dialogs):
        """Exporting nothing would produce a header-only PDF - refuse early."""
        widget.export_to_pdf()

        assert dialogs.saw("Table is empty")
        assert not hasattr(widget, "_pdf_export_dialog")

    @pytest.mark.integration
    def test_export_hands_the_current_records_to_the_dialog(self, loaded,
                                                            accept_dialogs,
                                                            no_web_engine):
        """The dialog works on the widget's live data and columns."""
        loaded.export_to_pdf()

        dialog = loaded._pdf_export_dialog
        assert dialog.table_data is loaded.table_data
        assert dialog.column_headers == loaded.column_headers
        assert dialog.visible_columns == loaded.visible_columns

    @pytest.mark.integration
    def test_export_saves_the_settings_back_without_the_password(self, loaded,
                                                                  accept_dialogs,
                                                                  no_web_engine):
        """Settings are remembered for the next export - secrets are not."""
        loaded.export_to_pdf()

        saved = loaded.saved_export_settings
        assert saved['selected_columns'] == loaded.visible_columns
        assert 'password' not in saved
        assert 'encryption_method' not in saved

    @pytest.mark.integration
    def test_a_pending_cell_edit_reaches_the_export_on_the_right_record(
            self, loaded, accept_dialogs, no_web_engine):
        """export_to_pdf syncs first - even when the table has been sorted."""
        loaded.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        password_column = EXPECTED_COLUMNS.index('Password')
        loaded.table.item(0, password_column).setText("typed-for-adam")

        loaded.export_to_pdf()

        by_name = {row['First Name']: row['Password']
                   for row in loaded._pdf_export_dialog.table_data}
        assert by_name["Adam"] == "typed-for-adam"
        assert by_name["Cyril"] == "pw-cyril"


# ===========================================================================
# PDFGenerator - real documents
# ===========================================================================

class TestPDFGeneratorOutput:

    def test_normal_data_produces_a_one_page_pdf(self, qapp):
        """The ordinary case: a handful of rows on a single A4 page."""
        rows = [{'First Name': f'First{i}', 'Last Name': f'Last{i}'}
                for i in range(5)]
        buffer = BytesIO()
        data = PDFGenerator(rows, settings()).generate_to_buffer(buffer)

        assert_is_pdf(data)
        assert buffer.getvalue() == data
        assert page_count(data) == 1

    def test_an_empty_table_still_yields_a_header_only_pdf(self, qapp):
        """No records must not mean an exception or an empty file."""
        data = PDFGenerator([], settings()).generate_to_buffer(BytesIO())

        assert_is_pdf(data)
        assert page_count(data) == 1

    def test_a_single_column_is_rendered(self, qapp):
        """The narrowest possible table is still a valid document."""
        data = PDFGenerator(
            [{'First Name': 'Jan'}],
            settings(selected_columns=['First Name']),
        ).generate_to_buffer(BytesIO())

        assert_is_pdf(data)

    def test_no_selected_columns_is_rejected_by_reportlab(self, qapp):
        """A table without columns cannot be built - it must not be silent."""
        generator = PDFGenerator([{'A': '1'}], settings(selected_columns=[]))
        with pytest.raises(ValueError):
            generator.generate_to_buffer(BytesIO())

    def test_diacritics_and_title_survive_generation(self, qapp):
        """Unicode content must not crash the generator."""
        rows = [{'First Name': 'Žofie', 'Last Name': 'Dvořáková'},
                {'First Name': 'Řehoř', 'Last Name': 'Čermák'}]
        data = PDFGenerator(
            rows,
            settings(include_title=True, title_text='Třída 6.A – hesla',
                     include_date=True),
        ).generate_to_buffer(BytesIO())

        assert_is_pdf(data)
        assert len(data) > 1000

    def test_angle_brackets_in_the_title_do_not_break_the_export(self, qapp):
        """The title is free text too - '<' must not abort the generation."""
        data = PDFGenerator(
            [{'First Name': 'Jan', 'Last Name': 'Novak'}],
            settings(include_title=True, title_text='Hesla 6.A a<b'),
        ).generate_to_buffer(BytesIO())

        assert_is_pdf(data)

    @pytest.mark.slow
    def test_many_rows_are_paginated_with_a_repeated_header(self, qapp):
        """A whole school year does not fit on one page."""
        rows = [{'First Name': f'First{i}', 'Last Name': f'Last{i}'}
                for i in range(500)]
        data = PDFGenerator(rows, settings()).generate_to_buffer(BytesIO())

        assert_is_pdf(data)
        assert page_count(data) > 1

    def test_a_long_value_is_wrapped_instead_of_overflowing(self, qapp):
        """Wrapped cells grow downwards; the document still builds."""
        rows = [{'First Name': 'x' * 400, 'Last Name': 'short'}]
        data = PDFGenerator(rows, settings(text_wrap=True)).generate_to_buffer(BytesIO())

        assert_is_pdf(data)

    def test_a_long_value_without_wrapping_still_builds(self, qapp):
        """Without wrapping the text overflows visually but must not fail."""
        rows = [{'First Name': 'x' * 400, 'Last Name': 'short'}]
        data = PDFGenerator(rows, settings(text_wrap=False)).generate_to_buffer(BytesIO())

        assert_is_pdf(data)
        assert page_count(data) == 1

    def test_generate_to_file_writes_exactly_the_returned_bytes(self, qapp, tmp_path):
        """The file on disk and the returned buffer must agree."""
        target = tmp_path / "export.pdf"
        data = PDFGenerator([{'First Name': 'Jan', 'Last Name': 'Novák'}],
                            settings()).generate_to_file(str(target))

        assert target.read_bytes() == data
        assert_is_pdf(target.read_bytes())

    def test_generating_into_a_qbuffer_fills_it(self, qapp):
        """The preview path hands in a QBuffer instead of a BytesIO."""
        buffer = QBuffer()
        data = PDFGenerator([{'First Name': 'Jan', 'Last Name': 'N'}],
                            settings()).generate_to_buffer(buffer)

        assert bytes(buffer.data()) == data
        assert_is_pdf(bytes(buffer.data()))

    def test_generating_into_an_already_open_qbuffer_appends_the_document(self, qapp):
        """A buffer the caller opened itself must not be reopened or lost."""
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        data = PDFGenerator([{'First Name': 'Jan', 'Last Name': 'N'}],
                            settings()).generate_to_buffer(buffer)

        assert bytes(buffer.data()) == data

    @pytest.mark.bug
    def test_a_failed_generation_does_not_destroy_an_existing_file(self, qapp,
                                                                   tmp_path):
        """An export that cannot be rendered must leave the old file alone."""
        target = tmp_path / "previous.pdf"
        target.write_bytes(b"%PDF-1.4 previous export")

        generator = PDFGenerator([{'A': '1'}], settings(selected_columns=[]))
        with pytest.raises(ValueError):
            generator.generate_to_file(str(target))

        assert target.read_bytes() == b"%PDF-1.4 previous export"

    def test_the_margin_drawer_draws_all_four_page_margins(self, qapp):
        """``show_margins`` draws the dashed guides on every page."""
        drawn = []

        class RecordingCanvas:
            def __getattr__(self, name):
                def call(*args, **kwargs):
                    drawn.append(name)
                return call

        drawer = PageMarginDrawer({'top': 2.0, 'bottom': 2.0,
                                   'left': 2.0, 'right': 2.0})

        class Doc:
            pagesize = A4

        drawer.draw_margins(RecordingCanvas(), Doc())

        assert drawn.count("line") == 4
        assert drawn[0] == "saveState" and drawn[-1] == "restoreState"

    def test_show_margins_produces_a_valid_preview_document(self, qapp):
        """The preview build path (with the margin drawer) also works."""
        data = PDFGenerator([{'First Name': 'Jan', 'Last Name': 'N'}],
                            settings(), show_margins=True
                            ).generate_to_buffer(BytesIO())
        assert_is_pdf(data)


class TestPDFGeneratorCellPreparation:

    def test_the_header_row_comes_first_and_uses_the_selected_columns(self, qapp):
        """Column order in the PDF is the order of ``selected_columns``."""
        generator = PDFGenerator(
            [{'A': '1', 'B': '2'}],
            settings(selected_columns=['B', 'A'], text_wrap=False),
        )
        prepared = generator._prepare_table_data()

        assert prepared[0] == ['B', 'A']
        assert prepared[1] == ['2', '1']

    @pytest.mark.parametrize("value,expected", [
        (None, 'None'),
        (0, '0'),
        (True, 'True'),
        (1.5, '1.5'),
        ('', ''),
    ])
    def test_non_string_values_are_stringified(self, qapp, value, expected):
        """The table may hold anything; the PDF only holds text."""
        generator = PDFGenerator([{'A': value}],
                                 settings(selected_columns=['A'], text_wrap=False))
        assert generator._prepare_table_data()[1] == [expected]

    def test_a_missing_column_becomes_an_empty_cell(self, qapp):
        """Records that never got a custom column must not raise KeyError."""
        generator = PDFGenerator([{'A': '1'}],
                                 settings(selected_columns=['A', 'B'],
                                          text_wrap=False))
        assert generator._prepare_table_data()[1] == ['1', '']

    def test_wrapped_cells_are_paragraphs(self, qapp):
        """Wrapping is implemented by turning cells into flowables."""
        generator = PDFGenerator([{'A': 'text'}],
                                 settings(selected_columns=['A'], text_wrap=True))
        cell = generator._prepare_table_data()[1][0]

        assert isinstance(cell, Paragraph)
        assert cell.getPlainText() == 'text'

    @pytest.mark.bug
    def test_angle_brackets_in_a_value_are_kept_verbatim(self, qapp):
        """A password like ``P@ss<word>X`` must reach the PDF unchanged."""
        generator = PDFGenerator([{'Password': 'P@ss<word>X'}],
                                 settings(selected_columns=['Password'],
                                          text_wrap=True))
        cell = generator._prepare_table_data()[1][0]

        assert cell.getPlainText() == 'P@ss<word>X'

    @pytest.mark.bug
    def test_a_lone_angle_bracket_does_not_break_the_export(self, qapp):
        """``a<b`` in any cell currently aborts the entire PDF generation."""
        generator = PDFGenerator([{'Password': 'a<b'}],
                                 settings(selected_columns=['Password'],
                                          text_wrap=True))
        data = generator.generate_to_buffer(BytesIO())

        assert_is_pdf(data)

    def test_an_ampersand_is_accepted(self, qapp):
        """Escaping-sensitive input that ReportLab does cope with."""
        generator = PDFGenerator([{'A': 'Tom & Jerry'}],
                                 settings(selected_columns=['A'], text_wrap=True))
        assert generator._prepare_table_data()[1][0].getPlainText() == 'Tom & Jerry'


# ===========================================================================
# PDFGenerator helpers
# ===========================================================================

class TestColumnWidths:

    def available(self, orientation='portrait', left=2.0, right=2.0):
        """Page width minus the horizontal margins, in points."""
        page = landscape(A4)[0] if orientation == 'landscape' else A4[0]
        return page - (left + right) * cm

    def test_auto_columns_share_the_printable_width_equally(self, qapp):
        """Three auto columns each get a third of the usable width."""
        generator = PDFGenerator([], settings(selected_columns=['A', 'B', 'C']))
        widths = generator._calculate_column_widths()

        assert len(widths) == 3
        assert widths[0] == pytest.approx(widths[1]) == pytest.approx(widths[2])
        assert sum(widths) == pytest.approx(self.available())

    def test_landscape_offers_more_width_than_portrait(self, qapp):
        """The orientation is taken into account when sharing the width."""
        portrait = PDFGenerator([], settings(selected_columns=['A'])
                                )._calculate_column_widths()
        wide = PDFGenerator([], settings(selected_columns=['A'],
                                         orientation='landscape')
                            )._calculate_column_widths()

        assert wide[0] > portrait[0]
        assert wide[0] == pytest.approx(self.available('landscape'))

    def test_manual_widths_are_converted_from_centimetres(self, qapp):
        """A width of 3 means three centimetres."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B'],
            column_widths={'A': 3, 'B': 5},
        ))
        assert generator._calculate_column_widths() == \
            pytest.approx([3 * cm, 5 * cm])

    def test_manual_widths_are_scaled_down_to_fit_the_page(self, qapp):
        """Two 15 cm columns cannot both fit on A4 - they are scaled."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B'],
            column_widths={'A': 15, 'B': 15},
        ))
        widths = generator._calculate_column_widths()

        assert sum(widths) == pytest.approx(self.available())
        assert widths[0] == pytest.approx(widths[1])

    def test_auto_columns_take_what_the_manual_ones_leave_over(self, qapp):
        """Mixed configuration: the rest is split between the auto columns."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B', 'C'],
            column_widths={'A': 5},
        ))
        widths = generator._calculate_column_widths()

        assert widths[0] == pytest.approx(5 * cm)
        assert widths[1] == pytest.approx(widths[2])
        assert sum(widths) == pytest.approx(self.available())

    def test_auto_columns_never_get_a_negative_width(self, qapp):
        """When the manual columns eat the page, the rest collapses to zero."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B'],
            column_widths={'A': 40},
        ))
        widths = generator._calculate_column_widths()

        assert widths[1] == 0
        assert all(width >= 0 for width in widths)
        assert sum(widths) <= self.available() + 1e-6

    def test_a_column_without_an_entry_counts_as_auto(self, qapp):
        """Columns added after the width dialog ran must not raise."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B'],
            column_widths={'A': 'auto'},
        ))
        widths = generator._calculate_column_widths()
        assert widths[0] == pytest.approx(widths[1])

    def test_margins_reduce_the_available_width(self, qapp):
        """Wider margins leave less room for the table."""
        narrow = PDFGenerator([], settings(
            selected_columns=['A'],
            margins={'top': 2.0, 'bottom': 2.0, 'left': 5.0, 'right': 5.0},
        ))._calculate_column_widths()

        assert narrow[0] == pytest.approx(self.available(left=5.0, right=5.0))

    def test_no_columns_gives_no_widths(self, qapp):
        """An empty selection yields an empty width list, not a crash."""
        generator = PDFGenerator([], settings(selected_columns=[]))
        assert generator._calculate_column_widths() == []


class TestRowHeights:

    def test_automatic_height_lets_reportlab_decide(self, qapp):
        """``None`` is ReportLab's "measure it yourself" marker."""
        generator = PDFGenerator([], settings(row_height={'auto': True,
                                                          'value': 0.8}))
        assert generator._calculate_row_heights(10) is None

    @pytest.mark.parametrize("rows", [0, 1, 7])
    def test_manual_height_is_repeated_for_every_row(self, qapp, rows):
        """Every row - header included - gets the configured height."""
        generator = PDFGenerator([], settings(row_height={'auto': False,
                                                          'value': 1.5}))
        heights = generator._calculate_row_heights(rows)

        assert heights == [1.5 * cm] * rows

    def test_a_fixed_row_height_still_paginates(self, qapp):
        """Fixed heights must not make the table overflow one page."""
        rows = [{'First Name': f'F{i}', 'Last Name': 'x'} for i in range(80)]
        data = PDFGenerator(rows, settings(row_height={'auto': False,
                                                       'value': 1.0})
                            ).generate_to_buffer(BytesIO())
        assert page_count(data) > 1


class TestValidateSettings:

    def test_a_sane_configuration_reports_no_warnings(self, qapp):
        """Nothing to complain about for two auto columns on A4."""
        generator = PDFGenerator([], settings())
        is_valid, warnings = generator.validate_settings()

        assert is_valid is True
        assert warnings == []

    def test_oversized_manual_widths_are_reported(self, qapp):
        """The user is told when the configured widths exceed the page."""
        generator = PDFGenerator([], settings(
            selected_columns=['A', 'B'],
            column_widths={'A': 15, 'B': 15},
        ))
        _, warnings = generator.validate_settings()

        assert any("exceed" in warning for warning in warnings)

    def test_many_auto_columns_are_reported(self, qapp):
        """16 auto columns on portrait A4 earn a legibility warning."""
        columns = [f"C{i}" for i in range(16)]
        generator = PDFGenerator([], settings(
            selected_columns=columns,
            column_widths={col: 'auto' for col in columns}))
        _, warnings = generator.validate_settings()

        assert any("Large number of columns" in warning for warning in warnings)

    def test_fifteen_columns_are_still_accepted(self, qapp):
        """The warning threshold is *more* than fifteen columns."""
        columns = [f"C{i}" for i in range(15)]
        generator = PDFGenerator([], settings(
            selected_columns=columns,
            column_widths={col: 'auto' for col in columns}))
        _, warnings = generator.validate_settings()

        assert not any("Large number of columns" in w for w in warnings)

    def test_one_manual_width_silences_the_column_count_warning(self, qapp):
        """The warning is only about *all* columns sharing the page equally."""
        columns = [f"C{i}" for i in range(16)]
        widths = {col: 'auto' for col in columns}
        widths['C0'] = 1
        generator = PDFGenerator([], settings(selected_columns=columns,
                                              column_widths=widths))
        _, warnings = generator.validate_settings()

        assert not any("Large number of columns" in w for w in warnings)

    def test_an_unknown_font_is_reported(self, qapp):
        """A font that is not registered would silently fall back."""
        generator = PDFGenerator([], settings(
            font={'family': 'No Such Font ŽŠČ', 'size': 10}))
        _, warnings = generator.validate_settings()

        assert any("No Such Font" in warning for warning in warnings)

    def test_an_unknown_font_falls_back_to_helvetica(self, qapp):
        """The fallback keeps the PDF renderable."""
        generator = PDFGenerator([], settings(
            font={'family': 'No Such Font', 'size': 10}))

        assert generator.reportlab_font == 'Helvetica'
        assert generator._get_bold_font_variant() == 'Helvetica-Bold'


class TestStyling:

    @pytest.mark.parametrize("family,regular,bold", [
        ('Helvetica', 'Helvetica', 'Helvetica-Bold'),
        ('Times', 'Times-Roman', 'Times-Bold'),
        ('Courier', 'Courier', 'Courier-Bold'),
    ])
    def test_the_header_row_uses_the_bold_variant_of_the_body_font(self, qapp,
                                                                   family, regular,
                                                                   bold):
        """Built-in fonts have a known bold face; the header uses it."""
        generator = PDFGenerator([], settings(font={'family': family, 'size': 10}))

        assert generator.reportlab_font == regular
        assert generator._get_bold_font_variant() == bold

    @pytest.mark.parametrize("horizontal,expected", [
        ('LEFT', 0), ('CENTER', 1), ('RIGHT', 2),
    ])
    def test_cell_paragraphs_follow_the_configured_alignment(self, qapp,
                                                             horizontal, expected):
        """TA_LEFT/TA_CENTER/TA_RIGHT are 0/1/2 in ReportLab."""
        generator = PDFGenerator([], settings(
            alignment={'horizontal': horizontal, 'vertical': 'TOP'}))

        assert generator._get_cell_style().alignment == expected

    def test_an_unknown_alignment_falls_back_to_left(self, qapp):
        """Settings from a newer version must not break the rendering."""
        generator = PDFGenerator([], settings(
            alignment={'horizontal': 'JUSTIFY', 'vertical': 'SIDEWAYS'}))

        assert generator._get_cell_style().alignment == 0

    def test_the_cell_style_uses_the_configured_font_size(self, qapp):
        """The font size from the settings reaches the cell paragraphs."""
        generator = PDFGenerator([], settings(font={'family': 'Helvetica',
                                                    'size': 7}))
        assert generator._get_cell_style().fontSize == 7

    def test_the_table_style_carries_the_header_colour_and_alignment(self, qapp):
        """The chosen header background and alignment end up in the commands."""
        generator = PDFGenerator([], settings(
            header_color='#123456',
            alignment={'horizontal': 'RIGHT', 'vertical': 'BOTTOM'}))
        commands = generator._create_table_style().getCommands()

        background = [c for c in commands if c[0] == 'BACKGROUND'][0]
        assert background[3].hexval() == '0x123456'
        assert ('ALIGN', (0, 1), (-1, -1), 'RIGHT') in commands
        assert ('VALIGN', (0, 1), (-1, -1), 'BOTTOM') in commands


# ===========================================================================
# PDFExportSettingsWidget
# ===========================================================================

@pytest.mark.gui
class TestSettingsWidgetValidation:

    def test_preview_needs_at_least_one_column(self, settings_widget):
        """Deselecting everything is refused with an explanation."""
        settings_widget._deselect_all_columns()
        is_valid, error = settings_widget.validate_preview_settings()

        assert is_valid is False
        assert "at least one column" in error

    def test_preview_accepts_the_default_selection(self, settings_widget):
        """The columns handed in by the dialog are pre-selected."""
        assert settings_widget.validate_preview_settings() == (True, "")
        assert settings_widget._get_selected_columns() == \
            ['First Name', 'Last Name', 'Password']

    def test_select_and_deselect_all_toggle_every_checkbox(self, settings_widget):
        """The quick-select buttons cover all columns."""
        settings_widget._deselect_all_columns()
        assert settings_widget._get_selected_columns() == []
        settings_widget._select_all_columns()
        assert settings_widget._get_selected_columns() == \
            ['First Name', 'Last Name', 'Password']

    def test_export_without_an_output_file_is_refused(self, settings_widget):
        """The auto-generated name still needs a folder."""
        is_valid, error = settings_widget.validate_export_settings()

        assert is_valid is False
        assert "output file" in error

    def test_a_whitespace_only_path_counts_as_no_file(self, settings_widget):
        """"   " is not a usable output path."""
        settings_widget.file_selector.set_auto_mode(False)
        settings_widget.file_selector.set_path("   ")

        is_valid, error = settings_widget.validate_export_settings()

        assert is_valid is False
        assert "output file" in error

    def test_the_column_check_runs_before_the_file_check(self, settings_widget):
        """The first thing the user has to fix is reported first."""
        settings_widget._deselect_all_columns()
        _, error = settings_widget.validate_export_settings()

        assert "column" in error

    @pytest.mark.parametrize("password,confirm,expected", [
        ("", "", "Please enter a password"),
        ("longenough", "different", "Passwords do not match"),
        ("short12", "short12", "at least 8 characters"),
    ])
    def test_password_problems_are_reported(self, settings_widget, tmp_path,
                                            password, confirm, expected):
        """Every password rule produces its own message."""
        settings_widget.file_selector.set_auto_mode(False)
        settings_widget.file_selector.set_path(str(tmp_path / "out.pdf"))
        settings_widget.encryption_widget.password_input.setText(password)
        settings_widget.encryption_widget.password_confirm.setText(confirm)

        is_valid, error = settings_widget.validate_export_settings()

        assert is_valid is False
        assert expected in error

    def test_an_eight_character_password_is_accepted(self, settings_widget, tmp_path):
        """Exactly eight characters is the documented lower boundary."""
        settings_widget.file_selector.set_auto_mode(False)
        settings_widget.file_selector.set_path(str(tmp_path / "out.pdf"))
        settings_widget.encryption_widget.set_password("12345678")

        assert settings_widget.validate_export_settings() == (True, "")

    def test_an_unavailable_method_is_refused(self, settings_widget, tmp_path):
        """GPG without python-gnupg must not pass validation."""
        combo = settings_widget.encryption_widget.method_combo
        index = combo.findData("gpg-unavailable")
        if index < 0:
            pytest.skip("GPG is available in this environment")
        combo.setCurrentIndex(index)
        settings_widget.file_selector.set_auto_mode(False)
        settings_widget.file_selector.set_path(str(tmp_path / "out.pdf"))
        settings_widget.encryption_widget.set_password("longenough1")

        is_valid, error = settings_widget.validate_export_settings()

        assert is_valid is False
        assert "not available" in error

    @pytest.mark.bug
    def test_pikepdf_is_refused_when_the_library_is_missing(self, settings_widget,
                                                            tmp_path):
        """Validation must not green-light an export that cannot run."""
        try:
            import pikepdf  # noqa: F401
        except ImportError:
            pass
        else:
            pytest.skip("pikepdf is installed in this environment")

        combo = settings_widget.encryption_widget.method_combo
        index = combo.findData("pikepdf")
        assert index >= 0, "pikepdf is offered by the PDF export dialog"
        combo.setCurrentIndex(index)
        settings_widget.file_selector.set_auto_mode(False)
        settings_widget.file_selector.set_path(str(tmp_path / "out.pdf"))
        settings_widget.encryption_widget.set_password("longenough1")

        is_valid, error = settings_widget.validate_export_settings()

        assert is_valid is False
        assert "not available" in error


@pytest.mark.gui
class TestSettingsRoundTrip:

    saved = {
        'orientation': 'landscape',
        'margins': {'top': 1.0, 'bottom': 1.5, 'left': 0.5, 'right': 3.0},
        'row_height': {'auto': False, 'value': 1.2},
        'text_wrap': False,
        'alignment': {'horizontal': 'CENTER', 'vertical': 'BOTTOM'},
        'header_color': '#123456',
        'font': {'family': 'Courier', 'size': 14},
        'include_title': True,
        'title_text': 'Žáci 6.A',
        'include_date': False,
    }

    def widget_with(self, saved, columns=('A', 'B', 'C')):
        return PDFExportSettingsWidget(list(columns), list(columns),
                                       saved_settings=dict(saved))

    def test_defaults_are_produced_when_nothing_was_saved(self, qapp):
        """A fresh widget offers a complete, usable settings dict."""
        produced = self.widget_with({}).get_settings_without_password()

        assert produced['orientation'] == 'portrait'
        assert produced['margins'] == {'top': 2.0, 'bottom': 2.0,
                                       'left': 2.0, 'right': 2.0}
        assert produced['row_height'] == {'auto': True, 'value': 0.8}
        assert produced['text_wrap'] is True
        assert produced['alignment'] == {'horizontal': 'LEFT',
                                         'vertical': 'MIDDLE'}
        assert produced['header_color'] == '#4a6fa5'
        assert produced['include_title'] is True
        assert produced['title_text'] == 'Table Export'
        assert produced['include_date'] is True
        assert produced['auto_filename'] is True

    @pytest.mark.parametrize("key", list(saved))
    def test_saved_values_come_back_unchanged(self, qapp, key):
        """Every layout/content setting survives a save/restore cycle."""
        produced = self.widget_with(self.saved).get_settings_without_password()
        assert produced[key] == self.saved[key]

    def test_the_produced_settings_drive_a_real_pdf(self, qapp):
        """The round trip stays compatible with what PDFGenerator expects."""
        produced = self.widget_with(self.saved).get_settings_without_password()
        data = PDFGenerator([{'A': '1', 'B': '2', 'C': '3'}],
                            produced).generate_to_buffer(BytesIO())
        assert_is_pdf(data)

    def test_an_unknown_saved_font_falls_back_to_the_first_available(self, qapp):
        """A font from another machine must not leave the combo empty."""
        widget = self.widget_with({'font': {'family': 'Nonexistent ŽŠ',
                                            'size': 11}})
        produced = widget.get_settings_without_password()

        assert produced['font']['family'] == widget.font_combo.itemText(0)
        assert produced['font']['size'] == 11

    def test_a_manually_chosen_output_path_is_restored_verbatim(self, qapp,
                                                                tmp_path):
        """Manual mode keeps the exact file the user picked."""
        target = str(tmp_path / "hesla.pdf")
        widget = self.widget_with({'auto_filename': False,
                                   'output_path': target})
        produced = widget.get_settings_without_password()

        assert produced['auto_filename'] is False
        assert produced['output_path'] == target

    def test_auto_mode_keeps_the_folder_but_regenerates_the_file_name(self, qapp,
                                                                      tmp_path):
        """With auto naming only the directory of a saved path is reused."""
        target = tmp_path / "old-name.pdf"
        widget = self.widget_with({'auto_filename': True,
                                   'output_path': str(target)})
        produced = widget.get_settings_without_password()

        assert Path(produced['output_path']).parent == tmp_path
        assert Path(produced['output_path']).name.startswith("table_export_")
        assert produced['output_path'].endswith(".pdf")

    @pytest.mark.bug
    def test_the_saved_column_selection_is_restored(self, qapp):
        """Columns the user unticked must stay unticked next time."""
        widget = self.widget_with({'selected_columns': ['C']})
        assert widget.get_settings_without_password()['selected_columns'] == ['C']

    @pytest.mark.bug
    def test_the_saved_column_widths_are_restored(self, qapp):
        """Configured widths are saved, so they must also be read back."""
        widget = self.widget_with({'column_widths': {'A': 3.0, 'B': 'auto',
                                                     'C': 'auto'}})
        assert widget.get_settings_without_password()['column_widths']['A'] == 3.0

    def test_the_export_settings_add_the_encryption_fields(self, qapp, tmp_path):
        """``get_settings_for_export`` is the only one carrying the password."""
        widget = self.widget_with({})
        widget.file_selector.set_auto_mode(False)
        widget.file_selector.set_path(str(tmp_path / "out.pdf"))
        widget.encryption_widget.set_password("longenough1")

        exported = widget.get_settings_for_export()

        assert exported['password'] == "longenough1"
        assert exported['output_path'] == str(tmp_path / "out.pdf")
        assert exported['file_format'] in ('standard', 'usrx')
        assert 'password' not in widget.get_settings_without_password()
        assert 'password' not in widget.get_settings_for_preview()


# ===========================================================================
# PDFExportDialog (without QtWebEngine)
# ===========================================================================

@pytest.mark.gui
class TestPDFExportDialogWithoutWebEngine:

    def test_no_web_view_is_created_and_a_placeholder_explains_why(self,
                                                                   export_dialog):
        """Without PyQt6-WebEngine the preview area is a plain label."""
        from PyQt6.QtWidgets import QLabel

        assert export_dialog.web_view is None
        labels = [label.text() for label in export_dialog.findChildren(QLabel)]
        assert any("preview is not available" in text for text in labels)
        assert any("PyQt6-WebEngine" in text for text in labels)

    def test_the_settings_widget_is_built_from_the_column_lists(self, export_dialog):
        """All headers are offered, the visible ones are pre-selected."""
        widget = export_dialog.settings_widget

        assert sorted(widget.column_checkboxes) == \
            ['First Name', 'Last Name', 'Password']
        assert widget._get_selected_columns() == ['First Name', 'Last Name']

    @pytest.mark.integration
    def test_generating_a_preview_fills_the_buffer_with_a_pdf(self, export_dialog,
                                                              accept_dialogs):
        """Export works without the web engine; only the display is skipped."""
        export_dialog.generate_preview()

        assert export_dialog.preview_buffer is not None
        assert_is_pdf(export_dialog.preview_buffer.getvalue())
        assert not accept_dialogs.saw("Preview Error")

    @pytest.mark.integration
    def test_generating_twice_replaces_the_previous_buffer(self, export_dialog,
                                                           accept_dialogs):
        """The old buffer is closed, a new one takes its place."""
        export_dialog.generate_preview()
        first = export_dialog.preview_buffer
        export_dialog.generate_preview()

        assert export_dialog.preview_buffer is not first
        assert_is_pdf(export_dialog.preview_buffer.getvalue())
        assert first.closed

    def test_a_preview_without_columns_is_refused(self, export_dialog,
                                                  accept_dialogs):
        """The validation error is shown and nothing is generated."""
        export_dialog.settings_widget._deselect_all_columns()
        export_dialog.generate_preview()

        assert accept_dialogs.saw("Invalid Settings")
        assert export_dialog.preview_buffer is None

    @pytest.mark.integration
    def test_generator_warnings_are_shown_but_the_preview_is_built(self, qapp,
                                                                   no_web_engine,
                                                                   accept_dialogs):
        """Sixteen auto columns warn about legibility - and still render."""
        columns = [f"C{i}" for i in range(16)]
        dialog = PDFExportDialog(table_data=[{c: 'x' for c in columns}],
                                 column_headers=columns,
                                 visible_columns=columns)
        try:
            dialog.generate_preview()

            assert accept_dialogs.saw("Configuration Warnings")
            assert_is_pdf(dialog.preview_buffer.getvalue())
        finally:
            dialog.close()

    def test_export_with_invalid_settings_writes_no_file(self, export_dialog,
                                                         accept_dialogs, tmp_path):
        """No output path configured - the export must not start."""
        export_dialog.export_to_file()

        assert accept_dialogs.saw("Invalid Settings")
        assert list(tmp_path.iterdir()) == []

    def test_declining_the_overwrite_question_leaves_the_file_alone(
            self, export_dialog, accept_dialogs, tmp_path):
        """Answering No to "File Exists" aborts before anything is written."""
        target = tmp_path / "existing.pdf"
        target.write_bytes(b"ORIGINAL")

        widget = export_dialog.settings_widget
        widget.file_selector.set_auto_mode(False)
        widget.file_selector.set_path(str(target))
        widget.encryption_widget.set_password("longenough1")
        accept_dialogs.question_answer = QMessageBox.StandardButton.No

        export_dialog.export_to_file()

        assert accept_dialogs.saw("File Exists")
        assert target.read_bytes() == b"ORIGINAL"

    def test_closing_releases_the_preview_buffer(self, export_dialog,
                                                 accept_dialogs):
        """The generated PDF must not stay in memory after the dialog closes."""
        export_dialog.generate_preview()
        buffer = export_dialog.preview_buffer

        export_dialog.close()

        assert export_dialog.preview_buffer is None
        assert buffer.closed

    def test_close_dialog_reports_accepted(self, export_dialog, accept_dialogs):
        """The widget saves its settings afterwards, so Accepted is expected."""
        export_dialog.close_dialog()
        assert export_dialog.result() == QDialog.DialogCode.Accepted

    def test_toggling_the_settings_panel_flips_visibility_and_label(
            self, export_dialog, accept_dialogs):
        """The button hides the settings and offers to show them again."""
        export_dialog.show()
        try:
            assert export_dialog.settings_panel.isVisible() is True

            export_dialog.toggle_settings_panel()
            assert export_dialog.settings_panel.isVisible() is False
            assert export_dialog.toggle_settings_btn.text() == "▶ Show Settings"

            export_dialog.toggle_settings_panel()
            assert export_dialog.settings_panel.isVisible() is True
            assert export_dialog.toggle_settings_btn.text() == "◀ Hide Settings"
        finally:
            export_dialog.hide()

    def test_settings_without_password_are_forwarded_from_the_widget(
            self, export_dialog, accept_dialogs):
        """The widget reads the remembered settings back through the dialog."""
        produced = export_dialog.get_settings_without_password()

        assert produced['selected_columns'] == ['First Name', 'Last Name']
        assert 'password' not in produced
