"""
Tests for the Microsoft 365 dialogs (Version 26, point 21 e).

The three format windows, the per-person editor and the bulk editor.
"""

import pytest
from PyQt6.QtWidgets import QDialogButtonBox

from models import Person
from services.m365_services import (
    DEFAULT_DISPLAY_NAME_TEMPLATE, DEFAULT_GROUP_TEMPLATE, DEFAULT_UPN_TEMPLATE,
    M365SyncConfig,
)
from ui.m365_bulk_edit_dialog import M365BulkEditDialog
from ui.m365_format_dialog import M365NameFormatDialog
from ui.m365_person_dialog import M365PersonDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def config():
    return M365SyncConfig(domain="skola.cz")


def person(first="Jan", last="Novák", class_name="6.A", **overrides):
    return Person(first_name=first, last_name=last, class_name=class_name,
                  **overrides)


class TestNameFormatDialog:
    """Editing one template, with a live preview."""

    @pytest.fixture
    def dialog(self, qapp):
        made = M365NameFormatDialog(
            "Sign-in Name Format", DEFAULT_UPN_TEMPLATE, "person",
            "why it matters", "skola.cz")
        yield made
        made.deleteLater()

    def test_the_preview_shows_a_real_result(self, dialog):
        # The example is a Czech name on purpose: diacritics are the case that
        # goes wrong.
        assert "krizovaz@skola.cz" in dialog.preview_label.text()

    def test_the_preview_follows_the_template(self, dialog):
        dialog.template_input.setText("{first_name|ascii|lower}@{domain}")
        assert "zofie@skola.cz" in dialog.preview_label.text()

    def test_a_broken_template_is_reported(self, dialog):
        dialog.template_input.setText("{nope}")
        assert dialog.problems()
        assert dialog.problem_label.text() != ""

    def test_a_broken_template_cannot_be_accepted(self, dialog):
        dialog.template_input.setText("{nope}")
        ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok.isEnabled() is False

    def test_a_good_template_can_be_accepted(self, dialog):
        ok = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok.isEnabled() is True

    def test_the_real_tenant_domain_is_used_in_the_preview(self, qapp):
        made = M365NameFormatDialog("t", "{domain}", "person", "why",
                                    "realschool.cz")
        assert "realschool.cz" in made.preview_label.text()
        made.deleteLater()

    def test_the_class_variant_offers_class_fields(self, qapp):
        made = M365NameFormatDialog("Group Name", DEFAULT_GROUP_TEMPLATE,
                                    "class", "why")
        assert made.problems() == []
        made.template_input.setText("{first_name}")
        assert made.problems()      # a person field, not a class field
        made.deleteLater()

    def test_the_template_comes_back_trimmed(self, dialog):
        dialog.template_input.setText("  {class_name}  ")
        assert dialog.template() == "{class_name}"


class TestPersonDialog:
    """Editing one person's Microsoft 365 fields."""

    @pytest.fixture
    def dialog(self, qapp, config):
        one = person()
        made = M365PersonDialog(one, config)
        yield made
        made.deleteLater()

    def test_generating_builds_the_sign_in_name(self, dialog):
        dialog._generate_upn()
        assert dialog.upn_input.text() == "novakj@skola.cz"

    def test_generating_builds_the_display_name(self, dialog):
        dialog._generate_display_name()
        assert dialog.display_name_input.text() == "Jan Novák (6.A)"

    def test_the_alias_is_derived_from_the_sign_in_name(self, dialog):
        dialog.upn_input.setText("novakj@skola.cz")
        dialog._generate_nickname()
        assert dialog.nickname_input.text() == "novakj"

    def test_generating_a_password_fills_the_field(self, dialog):
        dialog._generate_password()
        assert len(dialog.password_input.text()) > 0

    def test_accepting_writes_the_fields_onto_the_person(self, dialog):
        dialog.upn_input.setText("novakj@skola.cz")
        dialog.display_name_input.setText("Jan Novák")
        dialog.usage_location_input.setText("cz")
        dialog.accept_changes()

        assert dialog.person.m365_user_principal_name == "novakj@skola.cz"
        assert dialog.person.m365_display_name == "Jan Novák"
        # A country code is upper case in Microsoft 365.
        assert dialog.person.m365_usage_location == "CZ"

    def test_accepting_marks_the_person_dirty(self, dialog):
        dialog.upn_input.setText("novakj@skola.cz")
        dialog.accept_changes()
        assert dialog.person.is_dirty() is True

    def test_an_empty_field_becomes_none_not_an_empty_string(self, dialog):
        dialog.accept_changes()
        assert dialog.person.m365_user_principal_name is None

    def test_a_broken_template_does_not_write_an_error_into_the_field(self,
                                                                      qapp):
        # Writing "⚠ Unknown placeholder" into a sign-in name would be worse
        # than doing nothing.
        broken = M365SyncConfig(domain="skola.cz", upn_template="{nope}")
        made = M365PersonDialog(person(), broken)
        made._generate_upn()
        assert made.upn_input.text() == ""
        made.deleteLater()

    def test_it_does_not_offer_active_directory_fields(self, dialog):
        for forbidden in ("home_path_input", "home_drive_input",
                          "cannot_change_checkbox"):
            assert not hasattr(dialog, forbidden)


class TestBulkEditDialog:
    """Setting fields for several people at once."""

    @pytest.fixture
    def people(self):
        return [person("Jan", "Novák"), person("Eva", "Malá")]

    @pytest.fixture
    def dialog(self, qapp, people, config):
        made = M365BulkEditDialog(people, config)
        yield made
        made.deleteLater()

    def test_nothing_ticked_is_refused(self, dialog):
        assert any("Nothing was ticked" in problem
                   for problem in dialog.problems())

    def test_an_unticked_field_is_not_written(self, dialog, people):
        # Applying whatever the widgets happen to hold is how version 24's
        # bulk edit silently blanked fields nobody had touched.
        people[0].m365_usage_location = "CZ"
        dialog.same_password_check.setChecked(True)
        dialog.password_input.setText("Trida2026!")
        dialog.apply_changes()
        assert people[0].m365_usage_location == "CZ"

    def test_a_ticked_field_is_written_to_everyone(self, dialog, people):
        tick, value = dialog._rows['m365_usage_location']
        tick.setChecked(True)
        value.setText("CZ")
        dialog.apply_changes()
        assert [p.m365_usage_location for p in people] == ["CZ", "CZ"]

    def test_one_password_can_be_set_for_everyone(self, dialog, people):
        dialog.same_password_check.setChecked(True)
        dialog.password_input.setText("Trida2026!")
        dialog.apply_changes()
        assert [p.m365_password for p in people] == ["Trida2026!", "Trida2026!"]

    def test_everyone_can_get_a_different_password(self, dialog, people):
        dialog.generate_passwords_check.setChecked(True)
        dialog.apply_changes()
        passwords = [p.m365_password for p in people]
        assert all(passwords) and passwords[0] != passwords[1]

    def test_the_two_password_modes_exclude_each_other(self, dialog):
        dialog.same_password_check.setChecked(True)
        dialog.generate_passwords_check.setChecked(True)
        assert dialog.same_password_check.isChecked() is False

    def test_one_password_with_no_password_typed_is_refused(self, dialog):
        dialog.same_password_check.setChecked(True)
        assert any("Type the password" in problem
                   for problem in dialog.problems())

    def test_the_sign_in_names_can_be_rebuilt(self, dialog, people):
        dialog.generate_upn_check.setChecked(True)
        dialog.apply_changes()
        assert [p.m365_user_principal_name for p in people] == \
            ["novakj@skola.cz", "malae@skola.cz"]

    def test_the_display_names_can_be_rebuilt(self, dialog, people):
        dialog.generate_display_check.setChecked(True)
        dialog.apply_changes()
        assert people[0].m365_display_name == "Jan Novák (6.A)"

    def test_a_bad_usage_location_is_refused(self, dialog):
        tick, value = dialog._rows['m365_usage_location']
        tick.setChecked(True)
        value.setText("Czechia")
        assert any("two-letter" in problem for problem in dialog.problems())

    def test_the_account_state_can_be_set(self, dialog, people):
        dialog.enabled_check.setChecked(True)
        dialog.enabled_value.setChecked(True)
        dialog.apply_changes()
        assert all(p.account_enabled for p in people)

    def test_a_person_whose_name_cannot_be_built_does_not_stop_the_rest(
            self, qapp, people, dialogs):
        broken = M365SyncConfig(domain="skola.cz",
                                upn_template="{enrollment_year}@{domain}")
        made = M365BulkEditDialog(people, broken)
        made.generate_upn_check.setChecked(True)
        made.same_password_check.setChecked(True)
        made.password_input.setText("Trida2026!")
        made.apply_changes()
        # The password still went on, even though no name could be built.
        assert all(p.m365_password == "Trida2026!" for p in people)
        made.deleteLater()
