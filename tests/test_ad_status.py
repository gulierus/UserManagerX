"""
Tests for the redesigned ADStatus (Version 26, point 18).

Each member has to answer exactly one question, so that the seven situations
the request lists can be told apart without guessing:

a) nothing is known                  -> UNKNOWN
b) not found in the searched area    -> NOT_FOUND_IN_AD
c) found, at least one field differs -> DIFFERS_FROM_AD
d) found, every field identical      -> MATCHES_AD
e) synchronised with no errors       -> SYNC_SUCCEEDED
f) synchronised, something failed    -> SYNC_INCOMPLETE
g) several accounts match            -> MULTIPLE_AD_MATCHES
"""

import pytest

from models import ADStatus, LEGACY_AD_STATUS_VALUES


class TestTheMembers:
    """The set of states itself."""

    def test_every_required_situation_has_its_own_member(self):
        assert set(ADStatus) == {
            ADStatus.UNKNOWN,
            ADStatus.NOT_FOUND_IN_AD,
            ADStatus.DIFFERS_FROM_AD,
            ADStatus.MATCHES_AD,
            ADStatus.SYNC_SUCCEEDED,
            ADStatus.SYNC_INCOMPLETE,
            ADStatus.MULTIPLE_AD_MATCHES,
        }

    def test_no_two_members_share_a_value(self):
        values = [status.value for status in ADStatus]
        assert len(values) == len(set(values))

    def test_being_found_and_agreeing_are_two_different_states(self):
        # The old EXISTS_IN_AD said only "found" and was used for both.
        assert ADStatus.MATCHES_AD is not ADStatus.DIFFERS_FROM_AD

    def test_a_partial_sync_is_not_the_same_as_a_pending_edit(self):
        # The old UPDATE_PENDING meant both.
        assert ADStatus.SYNC_INCOMPLETE is not ADStatus.DIFFERS_FROM_AD

    def test_the_dead_create_pending_state_is_gone(self):
        assert not hasattr(ADStatus, "CREATE_PENDING")


class TestDescriptions:
    """Every state has to be explainable to the user."""

    @pytest.mark.parametrize("status", list(ADStatus))
    def test_every_status_has_a_short_label(self, status):
        assert status.label and not status.label.startswith("ADStatus")

    @pytest.mark.parametrize("status", list(ADStatus))
    def test_every_status_has_a_full_sentence(self, status):
        assert len(status.description) > 30

    def test_no_two_statuses_share_a_label(self):
        labels = [status.label for status in ADStatus]
        assert len(labels) == len(set(labels))


class TestClassification:
    """The two questions the rest of the code asks about a status."""

    @pytest.mark.parametrize("status", [
        ADStatus.MATCHES_AD, ADStatus.DIFFERS_FROM_AD,
        ADStatus.SYNC_SUCCEEDED, ADStatus.SYNC_INCOMPLETE,
    ])
    def test_these_mean_an_account_exists(self, status):
        assert status.is_in_ad is True

    @pytest.mark.parametrize("status", [
        ADStatus.UNKNOWN, ADStatus.NOT_FOUND_IN_AD, ADStatus.MULTIPLE_AD_MATCHES,
    ])
    def test_these_do_not_promise_an_account(self, status):
        assert status.is_in_ad is False

    @pytest.mark.parametrize("status", [ADStatus.MATCHES_AD,
                                        ADStatus.SYNC_SUCCEEDED])
    def test_these_mean_nothing_is_waiting(self, status):
        assert status.is_settled is True

    @pytest.mark.parametrize("status", [
        ADStatus.UNKNOWN, ADStatus.NOT_FOUND_IN_AD, ADStatus.DIFFERS_FROM_AD,
        ADStatus.SYNC_INCOMPLETE, ADStatus.MULTIPLE_AD_MATCHES,
    ])
    def test_these_still_have_work_outstanding(self, status):
        assert status.is_settled is False


class TestFromValue:
    """Reading a status written by an earlier version."""

    @pytest.mark.parametrize("status", list(ADStatus))
    def test_a_member_is_returned_unchanged(self, status):
        assert ADStatus.from_value(status) is status

    @pytest.mark.parametrize("status", list(ADStatus))
    def test_a_current_value_round_trips(self, status):
        assert ADStatus.from_value(status.value) is status

    @pytest.mark.parametrize("legacy,expected", [
        ("not_in_ad", ADStatus.NOT_FOUND_IN_AD),
        ("exists_in_ad", ADStatus.DIFFERS_FROM_AD),
        ("create_pending", ADStatus.UNKNOWN),
        ("update_pending", ADStatus.DIFFERS_FROM_AD),
        ("synced", ADStatus.SYNC_SUCCEEDED),
        ("ambiguous", ADStatus.MULTIPLE_AD_MATCHES),
    ])
    def test_every_old_value_still_reads(self, legacy, expected):
        assert ADStatus.from_value(legacy) is expected

    def test_every_legacy_value_maps_onto_a_real_member(self):
        for target in LEGACY_AD_STATUS_VALUES.values():
            assert ADStatus(target) in ADStatus

    def test_case_and_whitespace_are_forgiven(self):
        assert ADStatus.from_value("  SYNCED  ") is ADStatus.SYNC_SUCCEEDED

    @pytest.mark.parametrize("value", ["", None, "nonsense", 42])
    def test_anything_unrecognised_becomes_unknown(self, value):
        # A status that cannot be read must not stop a file from loading.
        assert ADStatus.from_value(value) is ADStatus.UNKNOWN


class TestStatusFollowsLocalEdits:
    """Editing a person changes what is true about her account."""

    def test_editing_a_matching_person_makes_her_differ(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_status=ADStatus.MATCHES_AD)
        person.ad_email = "novy@skola.cz"
        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_editing_a_synchronised_person_makes_her_differ(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_status=ADStatus.SYNC_SUCCEEDED)
        person.ad_email = "novy@skola.cz"
        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_undoing_the_edit_restores_identical(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_email="a@skola.cz",
                             ad_status=ADStatus.MATCHES_AD)
        person.ad_email = "novy@skola.cz"
        person.ad_email = "a@skola.cz"
        assert person.ad_status is ADStatus.MATCHES_AD

    def test_undoing_the_edit_restores_synchronised(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_email="a@skola.cz",
                             ad_status=ADStatus.SYNC_SUCCEEDED)
        person.ad_email = "novy@skola.cz"
        person.ad_email = "a@skola.cz"
        assert person.ad_status is ADStatus.SYNC_SUCCEEDED

    def test_one_undone_edit_does_not_clear_another(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_email="a@skola.cz",
                             ad_status=ADStatus.MATCHES_AD)
        person.ad_email = "novy@skola.cz"
        person.ad_display_name = "Jan Novák"
        person.ad_email = "a@skola.cz"
        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_a_group_change_counts_as_an_edit(self, make_person):
        from models import ADGroup

        person = make_person(ad_dn="CN=Jan,DC=x", ad_status=ADStatus.MATCHES_AD)
        person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=x"))
        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_undoing_a_group_change_restores_the_status(self, make_person):
        from models import ADGroup

        person = make_person(ad_dn="CN=Jan,DC=x", ad_status=ADStatus.MATCHES_AD)
        group = ADGroup(name="Zaci", dn="CN=Zaci,DC=x")
        person.add_to_group(group)
        person.remove_from_group(group)
        assert person.ad_status is ADStatus.MATCHES_AD

    def test_editing_a_person_who_is_not_in_ad_leaves_her_status_alone(self,
                                                                       make_person):
        # "Not in AD" stays true no matter what is edited locally.
        person = make_person(ad_status=ADStatus.NOT_FOUND_IN_AD)
        person.ad_email = "novy@skola.cz"
        assert person.ad_status is ADStatus.NOT_FOUND_IN_AD

    def test_editing_an_ambiguous_person_does_not_hide_the_ambiguity(self,
                                                                     make_person):
        person = make_person(ad_status=ADStatus.MULTIPLE_AD_MATCHES)
        person.ad_email = "novy@skola.cz"
        assert person.ad_status is ADStatus.MULTIPLE_AD_MATCHES

    def test_reset_dirty_reports_a_successful_synchronisation(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_status=ADStatus.DIFFERS_FROM_AD)
        person.ad_email = "novy@skola.cz"
        person.reset_dirty()
        assert person.ad_status is ADStatus.SYNC_SUCCEEDED

    def test_reset_dirty_forgets_the_pre_edit_status(self, make_person):
        person = make_person(ad_dn="CN=Jan,DC=x", ad_email="a@skola.cz",
                             ad_status=ADStatus.MATCHES_AD)
        person.ad_email = "novy@skola.cz"
        person.reset_dirty()
        # The person now holds what was written, so reverting to the value she
        # had before the edit is a new edit, not an undo.
        person.ad_email = "a@skola.cz"
        assert person.ad_status is ADStatus.DIFFERS_FROM_AD
