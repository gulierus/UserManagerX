"""
Tests for the "Microsoft 365 Management" operation (Version 26, point 21 e).

It mirrors the Active Directory operation, but over Microsoft 365 fields: the
table must not show Active Directory ones, because a person can exist in both
directories with different values.
"""

import pytest
from PyQt6.QtWidgets import QDialog

from models import Class, Person, Source
from models_m365 import M365Status
from operations.m365_management import STATUS_COLORS, M365ManagementWidget
from services.m365_auth import AuthMethod

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _no_real_dialogs(accept_dialogs, isolated_settings):
    return accept_dialogs


@pytest.fixture
def widget(qapp, source_manager):
    made = M365ManagementWidget(source_manager)
    yield made
    made.close()


def person(first="Jan", last="Novák", class_name="6.A", **overrides):
    return Person(first_name=first, last_name=last, class_name=class_name,
                  **overrides)


@pytest.fixture
def loaded(widget, source_manager):
    """The widget showing two people."""
    source = Source(name="Roster", source_type="manual")
    school_class = Class(name="6.A")
    school_class.add_person(person("Jan", "Novák"))
    school_class.add_person(person("Eva", "Malá"))
    source.add_class(school_class)
    source_manager.add_source(source)
    widget.set_source(source)
    return widget


def row_texts(table, column=0):
    return [table.item(row, column).text() for row in range(table.rowCount())]


class TestTheColumns:
    """Which fields the table offers."""

    def test_only_microsoft_365_fields_are_offered(self, widget):
        # The point is explicit: no Active Directory fields here.
        for forbidden in ("Cannot Change Pwd", "Home Directory", "Groups"):
            assert forbidden not in widget.ALL_COLUMNS

    def test_the_microsoft_365_fields_are_all_there(self, widget):
        for expected in ("Sign-in name", "Display name", "Alias", "Password",
                         "Usage location"):
            assert expected in widget.ALL_COLUMNS

    def test_status_and_dirty_are_offered_like_the_other_operation(self,
                                                                   widget):
        assert "Status" in widget.ALL_COLUMNS and "Dirty" in widget.ALL_COLUMNS

    def test_the_default_columns_are_all_real_columns(self, widget):
        assert set(widget.DEFAULT_COLUMNS) <= set(widget.ALL_COLUMNS)


class TestTheTable:
    """What the table draws."""

    def test_one_row_per_person(self, loaded):
        assert loaded.person_table.rowCount() == 2

    def test_the_names_are_shown(self, loaded):
        loaded.visible_columns = ["Name"]
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table) == ["Jan Novák", "Eva Malá"]

    def test_an_unset_field_says_so_rather_than_showing_none(self, loaded):
        loaded.visible_columns = ["Sign-in name"]
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table) == ["(not set)", "(not set)"]

    def test_the_password_is_never_shown_in_the_table(self, loaded):
        # The table is the thing people take screenshots of.
        loaded.persons()[0].m365_password = "Secret1!"
        loaded.visible_columns = ["Password"]
        loaded.refresh_person_table()
        assert "Secret1!" not in row_texts(loaded.person_table)[0]

    def test_the_status_cell_shows_the_readable_label(self, loaded):
        loaded.persons()[0].m365_status = M365Status.NOT_FOUND_IN_M365
        loaded.visible_columns = ["Status"]
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table)[0] == \
            M365Status.NOT_FOUND_IN_M365.label

    def test_the_status_cell_explains_itself(self, loaded):
        loaded.visible_columns = ["Status"]
        loaded.refresh_person_table()
        assert loaded.person_table.item(0, 0).toolTip() == \
            M365Status.UNKNOWN.description

    @pytest.mark.parametrize("status", list(M365Status))
    def test_every_status_except_unknown_has_a_colour(self, status):
        if status is M365Status.UNKNOWN:
            assert status not in STATUS_COLORS
        else:
            assert status in STATUS_COLORS

    def test_the_dirty_column_follows_an_edit(self, loaded):
        loaded.visible_columns = ["Dirty"]
        loaded.persons()[0].m365_display_name = "Jan Novák (6.A)"
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table) == ["✓", ""]

    def test_the_group_column_uses_the_current_template(self, loaded):
        loaded.visible_columns = ["Group"]
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table) == ["Trida-6.A", "Trida-6.A"]

    def test_every_row_has_an_edit_button(self, loaded):
        last_column = len(loaded.visible_columns)
        assert loaded.person_table.cellWidget(0, last_column) is not None

    def test_an_empty_source_draws_an_empty_table(self, widget):
        widget.set_source(Source(name="Empty", source_type="manual"))
        assert widget.person_table.rowCount() == 0

    def test_the_summary_counts_the_states(self, loaded):
        assert "2 person(s)" in loaded.summary_label.text()
        assert "not checked" in loaded.summary_label.text()


class TestSelection:
    """Which people a command applies to."""

    def test_nothing_selected_means_everybody(self, loaded):
        # The same rule the Active Directory table uses.
        assert len(loaded.selected_persons()) == 2

    def test_a_selected_row_narrows_it(self, loaded):
        loaded.person_table.selectRow(0)
        assert [p.first_name for p in loaded.selected_persons()] == ["Jan"]


class TestTheSettings:
    """The formats and the synchronisation configuration."""

    def test_the_defaults_are_usable_once_a_tenant_is_known(self, widget):
        widget._tenant_domain = "skola.cz"
        assert widget.sync_config().problems() == []

    def test_teams_are_off_by_default(self, widget):
        assert widget.create_teams_check.isChecked() is False
        assert widget.sync_config().create_teams is False

    def test_the_owners_are_split_on_commas_and_semicolons(self, widget):
        widget.owners_input.setText("a@s.cz, b@s.cz; c@s.cz")
        assert widget.sync_config().owner_user_principal_names == \
            ["a@s.cz", "b@s.cz", "c@s.cz"]

    def test_the_tenant_domain_reaches_the_configuration(self, widget):
        widget._tenant_domain = "skola.onmicrosoft.com"
        assert widget.sync_config().domain == "skola.onmicrosoft.com"

    def test_the_format_combo_offers_all_four_windows(self, widget):
        offered = [widget.format_combo.itemData(i)
                   for i in range(widget.format_combo.count())]
        assert offered == [None, "upn", "display_name", "password", "group"]

    def test_choosing_a_format_resets_the_combo(self, widget, monkeypatch):
        # It is a menu, not a setting: it must not stay on the last choice.
        monkeypatch.setattr(widget, "_open_name_format", lambda _what: None)
        widget._on_format_chosen(1)
        assert widget.format_combo.currentIndex() == 0

    def test_a_changed_group_template_reaches_the_table(self, loaded):
        loaded._group_template = "Class-{class_name}"
        loaded.visible_columns = ["Group"]
        loaded.refresh_person_table()
        assert row_texts(loaded.person_table)[0] == "Class-6.A"


class TestGuards:
    """Nothing may be attempted without the things it needs."""

    def test_synchronising_without_a_source_is_refused(self, widget, dialogs):
        widget.synchronize()
        assert dialogs.titles() == ["No Source"]

    def test_discovering_without_a_source_is_refused(self, widget, dialogs):
        widget.discover_in_m365()
        assert dialogs.titles() == ["No Source"]

    def test_bulk_editing_without_a_source_is_refused(self, widget, dialogs):
        widget.bulk_edit()
        assert dialogs.titles() == ["No Source"]

    def test_connecting_without_credentials_is_refused_locally(self, widget,
                                                               dialogs):
        widget.connect_to_m365()
        assert dialogs.titles() == ["Missing Information"]

    def test_synchronising_without_credentials_is_refused(self, loaded,
                                                          dialogs):
        loaded.synchronize()
        assert dialogs.titles() == ["Missing Information"]

    def test_a_broken_template_stops_the_synchronisation(self, loaded, dialogs):
        loaded.connection.method_combo.setCurrentIndex(
            loaded.connection.method_combo.findData(AuthMethod.DEVICE_CODE))
        loaded._group_template = "{nope}"
        loaded.synchronize()
        assert dialogs.titles()[-1] == "Check the Settings"


class TestTheEditors:
    """The per-person and bulk editors."""

    def test_editing_a_person_writes_the_fields_back(self, loaded,
                                                     monkeypatch):
        from ui import m365_person_dialog

        def fake_exec(self):
            self.upn_input.setText("novakj@skola.cz")
            self.accept_changes()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(m365_person_dialog.M365PersonDialog, "exec",
                            fake_exec)

        chosen = loaded.persons()[0]
        loaded.edit_person(chosen)
        assert chosen.m365_user_principal_name == "novakj@skola.cz"

    def test_bulk_editing_applies_to_the_selection(self, loaded, monkeypatch):
        from ui import m365_bulk_edit_dialog

        def fake_exec(self):
            self.same_password_check.setChecked(True)
            self.password_input.setText("Trida2026!")
            self.apply_changes()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(m365_bulk_edit_dialog.M365BulkEditDialog, "exec",
                            fake_exec)

        loaded.person_table.selectRow(0)
        loaded.bulk_edit()
        assert loaded.persons()[0].m365_password == "Trida2026!"
        assert loaded.persons()[1].m365_password is None
