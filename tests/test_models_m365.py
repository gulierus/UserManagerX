"""
Tests for the Microsoft 365 domain objects (Version 26, point 21).

The point asks for dedicated classes for groups and teams rather than using
``Class`` directly; a Class is built from one of them only in step 2 of the
import wizard.
"""

import pytest

from models_m365 import (
    LEGACY_M365_STATUS_VALUES,
    M365Group,
    M365GroupSelection,
    M365Member,
    M365Status,
    M365Team,
)


class TestM365Status:
    """The seven states, mirroring ADStatus."""

    def test_the_same_seven_situations_are_covered(self):
        assert len(M365Status) == 7

    @pytest.mark.parametrize("status", list(M365Status))
    def test_every_status_has_a_label_and_a_description(self, status):
        assert status.label and len(status.description) > 30

    def test_no_two_statuses_share_a_label(self):
        labels = [status.label for status in M365Status]
        assert len(labels) == len(set(labels))

    @pytest.mark.parametrize("status", list(M365Status))
    def test_a_value_round_trips(self, status):
        assert M365Status.from_value(status.value) is status

    @pytest.mark.parametrize("legacy,expected", [
        ("not_in_m365", M365Status.NOT_FOUND_IN_M365),
        ("exists_in_m365", M365Status.DIFFERS_FROM_M365),
        ("update_pending", M365Status.DIFFERS_FROM_M365),
        ("synced", M365Status.SYNC_SUCCEEDED),
    ])
    def test_older_spellings_still_read(self, legacy, expected):
        assert M365Status.from_value(legacy) is expected

    def test_every_legacy_value_maps_onto_a_real_member(self):
        for target in LEGACY_M365_STATUS_VALUES.values():
            assert M365Status(target) in M365Status

    @pytest.mark.parametrize("value", ["", None, "nonsense", 7])
    def test_anything_unrecognised_becomes_unknown(self, value):
        assert M365Status.from_value(value) is M365Status.UNKNOWN

    @pytest.mark.parametrize("status", [
        M365Status.MATCHES_M365, M365Status.DIFFERS_FROM_M365,
        M365Status.SYNC_SUCCEEDED, M365Status.SYNC_INCOMPLETE,
    ])
    def test_these_mean_an_account_exists(self, status):
        assert status.is_in_m365 is True

    @pytest.mark.parametrize("status", [
        M365Status.UNKNOWN, M365Status.NOT_FOUND_IN_M365,
        M365Status.MULTIPLE_M365_MATCHES,
    ])
    def test_these_do_not_promise_an_account(self, status):
        assert status.is_in_m365 is False

    def test_only_the_quiet_states_are_settled(self):
        settled = {s for s in M365Status if s.is_settled}
        assert settled == {M365Status.MATCHES_M365, M365Status.SYNC_SUCCEEDED}


class TestM365Member:
    """One account inside a group."""

    def test_the_display_name_is_the_best_name(self):
        assert M365Member(display_name="Jan Novák").best_name == "Jan Novák"

    def test_the_given_and_surname_are_used_when_there_is_no_display_name(self):
        assert M365Member(given_name="Jan", surname="Novák").best_name == \
            "Jan Novák"

    def test_the_sign_in_name_is_the_last_resort(self):
        assert M365Member(user_principal_name="novakjan@skola.cz").best_name == \
            "novakjan@skola.cz"

    def test_the_object_id_is_better_than_nothing(self):
        assert M365Member(object_id="abc").best_name == "abc"

    def test_the_directory_fields_are_preferred_for_splitting(self):
        member = M365Member(display_name="Something Else", given_name="Jan",
                            surname="Novák")
        assert member.split_name() == ("Jan", "Novák")

    def test_a_display_name_splits_on_the_last_space(self):
        # Two given names, one surname - the common case.
        assert M365Member(display_name="Jan Petr Novák").split_name() == \
            ("Jan Petr", "Novák")

    def test_a_single_word_becomes_the_first_name(self):
        # Inventing a surname would be worse than leaving it empty.
        assert M365Member(display_name="Jan").split_name() == ("Jan", "")

    def test_nothing_at_all_splits_into_nothing(self):
        assert M365Member().split_name() == ("", "")

    def test_surrounding_whitespace_is_removed(self):
        assert M365Member(display_name="  Jan Novák  ").split_name() == \
            ("Jan", "Novák")


class TestM365Group:
    """A group as the directory holds it."""

    def test_a_unified_group_is_a_microsoft_365_group(self):
        assert M365Group(group_types=["Unified"]).kind == "Microsoft 365"

    def test_the_group_type_is_matched_case_insensitively(self):
        assert M365Group(group_types=["unified"]).is_unified is True

    def test_a_security_group_says_so(self):
        assert M365Group(security_enabled=True).kind == "Security"

    def test_a_group_with_a_team_is_called_a_team(self):
        assert M365Group(group_types=["Unified"], has_team=True).kind == "Team"

    def test_a_plain_group_has_a_name_too(self):
        assert M365Group().kind == "Group"

    def test_the_member_count_follows_the_members(self):
        group = M365Group(members=[M365Member(), M365Member()])
        assert group.member_count == 2

    def test_the_owner_names_are_readable(self):
        group = M365Group(owners=[M365Member(display_name="Eva Malá")])
        assert group.owner_names == ["Eva Malá"]

    def test_not_loaded_is_not_the_same_as_empty(self):
        # An empty member list means "no members" only once they were read.
        assert M365Group().members_loaded is False


class TestSearching:
    """The search box in step 1 of the wizard."""

    @pytest.fixture
    def group(self):
        return M365Group(display_name="Trida-6.A", mail_nickname="trida6a",
                         description="Sixth A", mail="trida6a@skola.cz")

    @pytest.mark.parametrize("needle", ["6.A", "trida", "TRIDA6A", "sixth",
                                        "skola.cz"])
    def test_every_searchable_field_matches(self, group, needle):
        assert group.matches(needle) is True

    def test_an_empty_search_matches_everything(self, group):
        assert group.matches("") is True and group.matches("  ") is True

    def test_something_absent_does_not_match(self, group):
        assert group.matches("9.C") is False


class TestM365Team:
    """A team is a group with a team attached."""

    def test_a_team_is_a_group(self):
        assert isinstance(M365Team(), M365Group)

    def test_a_team_always_has_a_team(self):
        assert M365Team().has_team is True

    def test_a_team_carries_its_link(self):
        assert M365Team(web_url="https://teams…").web_url.startswith("https")


class TestM365GroupSelection:
    """What the wizard carries between its two steps."""

    @pytest.fixture
    def selection(self):
        group = M365Group(
            display_name="Trida-6.A",
            owners=[M365Member(object_id="o1", display_name="Eva Malá",
                               is_owner=True)],
            members=[M365Member(object_id="m1", display_name="Jan Novák")],
        )
        return M365GroupSelection(group=group)

    def test_only_the_members_become_pupils_by_default(self, selection):
        # In a school group the owner is the teacher.
        assert [p.display_name for p in selection.people()] == ["Jan Novák"]

    def test_the_owners_can_be_included(self, selection):
        selection.include_owners = True
        assert sorted(p.display_name for p in selection.people()) == \
            ["Eva Malá", "Jan Novák"]

    def test_somebody_who_is_both_is_listed_once(self, selection):
        selection.group.owners.append(selection.group.members[0])
        selection.include_owners = True
        assert len(selection.people()) == 2

    def test_the_class_name_starts_empty(self, selection):
        assert selection.class_name == ""
