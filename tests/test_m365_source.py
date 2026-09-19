"""
Tests for the "Microsoft 365 From Web" source (Version 26, point 21 a).

Building a source out of what the wizard returns - the part that decides what
the rest of the application actually sees.
"""

import pytest

from models import SourceManager
from models_m365 import M365Group, M365GroupSelection, M365Member
from services.m365_auth import AuthMethod, M365Credentials
from sources.m365_source import build_person, build_source

pytestmark = pytest.mark.gui


def member(first="Jan", last="Novák", object_id="u1", **overrides):
    values = dict(object_id=object_id, display_name=f"{first} {last}",
                  given_name=first, surname=last,
                  user_principal_name=f"{object_id}@skola.cz",
                  mail=f"{object_id}@skola.cz")
    values.update(overrides)
    return M365Member(**values)


def selection(class_name="6.A", members=None, owners=None, **group_kwargs):
    group = M365Group(object_id="g1", display_name="Trida-6.A",
                      members=list(members or [member()]),
                      owners=list(owners or []), **group_kwargs)
    group.members_loaded = True
    return M365GroupSelection(group=group, class_name=class_name)


class TestBuildPerson:
    """One Microsoft 365 account becomes one Person."""

    def test_the_name_is_split(self):
        person = build_person(member("Jan", "Novák"), "6.A")
        assert (person.first_name, person.last_name) == ("Jan", "Novák")

    def test_the_class_is_the_one_the_user_named(self):
        assert build_person(member(), "Sixth A").class_name == "Sixth A"

    def test_the_email_is_carried_across(self):
        assert build_person(member(), "6.A").ad_email == "u1@skola.cz"

    def test_the_sign_in_name_is_used_when_there_is_no_mail(self):
        person = build_person(member(mail=""), "6.A")
        assert person.ad_email == "u1@skola.cz"

    def test_the_microsoft_facts_go_in_the_metadata(self):
        # The two directories are separate: a person can be in both with
        # different values, so the typed AD fields must not be reused.
        person = build_person(member(), "6.A")
        assert person.metadata['m365_object_id'] == "u1"
        assert person.metadata['m365_user_principal_name'] == "u1@skola.cz"

    def test_the_account_state_is_carried_across(self):
        assert build_person(member(account_enabled=False),
                            "6.A").account_enabled is False

    def test_an_account_with_no_name_at_all_is_not_a_person(self):
        # A record nothing can be matched against is worse than no record.
        assert build_person(M365Member(object_id="x"), "6.A") is None

    def test_a_display_name_alone_is_enough(self):
        person = build_person(M365Member(object_id="x",
                                         display_name="Jan Novák"), "6.A")
        assert (person.first_name, person.last_name) == ("Jan", "Novák")

    def test_an_owner_is_marked_as_one(self):
        person = build_person(member(is_owner=True), "6.A")
        assert person.metadata['m365_was_group_owner'] is True


class TestBuildSource:
    """The whole source."""

    def test_one_class_per_selected_group(self):
        source = build_source([selection("6.A"),
                               selection("7.B")], "M365")
        assert [c.name for c in source.classes] == ["6.A", "7.B"]

    def test_the_source_is_read_only(self):
        # It mirrors a directory this application did not author.
        assert build_source([selection()], "M365").readonly is True

    def test_the_source_type_says_where_it_came_from(self):
        assert build_source([selection()], "M365").source_type == "microsoft_365"

    def test_the_people_land_in_their_class(self):
        source = build_source([selection(members=[member("Jan", "Novák", "u1"),
                                                  member("Eva", "Malá", "u2")])],
                              "M365")
        assert len(source.classes[0].persons) == 2

    def test_the_origin_is_recorded_on_the_source(self):
        source = build_source([selection()], "M365",
                              {'tenant_domain': "skola.onmicrosoft.com",
                               'auth_method': "app_only"})
        assert source.get_source_info('tenant_domain') == "skola.onmicrosoft.com"
        assert source.get_source_info('auth_method') == "app_only"

    def test_the_group_behind_each_class_is_remembered(self):
        source = build_source([selection()], "M365")
        assert source.classes[0].metadata['m365_group_id'] == "g1"
        assert source.classes[0].metadata['m365_group_name'] == "Trida-6.A"

    def test_a_group_with_no_class_name_is_skipped(self):
        assert build_source([selection(class_name="  ")], "M365").classes == []

    def test_accounts_without_a_name_are_counted_not_silently_dropped(self):
        source = build_source(
            [selection(members=[member(), M365Member(object_id="x")])], "M365")
        assert source.get_source_info('accounts_without_a_name') == 1

    def test_the_group_count_is_recorded(self):
        source = build_source([selection("6.A"), selection("7.B")], "M365")
        assert source.get_source_info('group_count') == 2

    def test_owners_are_included_only_when_asked(self):
        chosen = selection(members=[member("Jan", "Novák", "u1")],
                           owners=[member("Petr", "Uč", "t1")])
        assert len(build_source([chosen], "M365").classes[0].persons) == 1

        chosen.include_owners = True
        assert len(build_source([chosen], "M365").classes[0].persons) == 2

    def test_the_enrollment_year_is_calculated_when_the_source_is_added(self):
        # SourceManager does it for every source, whatever loaded it.
        manager = SourceManager()
        source = build_source([selection("6.A")], "M365")
        manager.add_source(source)
        assert source.classes[0].enrollment_year is not None


class TestTheWidget:
    """The page on the "1. Data Sources" tab."""

    @pytest.fixture
    def widget(self, qapp, source_manager):
        from sources.m365_source import MicrosoftM365SourceWidget
        made = MicrosoftM365SourceWidget(source_manager)
        yield made
        made.deleteLater()

    def test_it_offers_every_sign_in_method(self, widget):
        offered = [widget.connection.method_combo.itemData(i)
                   for i in range(widget.connection.method_combo.count())]
        assert offered == list(AuthMethod)

    def test_incomplete_credentials_stop_the_load_before_any_request(
            self, widget, dialogs):
        widget.on_load()
        assert dialogs.titles() == ["Missing Information"]

    def test_the_default_source_name_is_built_from_the_tenant(self, widget):
        assert widget._default_source_name(
            {'tenant_domain': "skola.onmicrosoft.com"}) == \
            "M365 skola.onmicrosoft.com"

    def test_the_default_source_name_is_unique(self, widget, make_source):
        widget.source_manager.add_source(make_source(name="M365 skola.cz"))
        assert widget._default_source_name(
            {'tenant_domain': "skola.cz"}) == "M365 skola.cz (2)"

    def test_a_tenant_that_cannot_be_read_still_gives_a_name(self, widget):
        assert widget._default_source_name({}) == "Microsoft 365"

    def test_the_panel_warns_about_the_password_method_up_front(self, widget):
        combo = widget.connection.method_combo
        combo.setCurrentIndex(combo.findData(AuthMethod.USERNAME_PASSWORD))
        assert "multi-factor" in widget.connection.warning_label.text()
