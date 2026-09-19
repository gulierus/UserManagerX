"""
Tests for comparing a person with the Active Directory snapshot.

Version 26, points 18 c/d (is the person identical to AD or not?) and 19 b
(which fields differ, and what does AD hold?).
"""

import pytest

from models import ADGroup
from services.ad_comparison import (
    AD_VALUES_METADATA_KEY,
    DIFFERENCES_METADATA_KEY,
    FIELD_LABELS,
    FIELD_TO_AD_ATTRIBUTE,
    FieldDifference,
    ad_value_of,
    compare_person_with_ad,
    describe_ad_value,
    differing_fields,
    has_ad_snapshot,
    store_comparison,
    stored_differences,
)


@pytest.fixture
def person(make_person):
    """A person whose every comparable field is filled in."""
    return make_person(
        "Jan", "Novák", "6.A",
        ad_username="novakjan",
        ad_display_name="Jan Novák",
        ad_email="jan@skola.cz",
        ad_description="Pupil",
        home_directory=r"\\srv\students\novakjan",
        home_drive="H:",
        group_memberships=[ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=cz")],
    )


@pytest.fixture
def matching_snapshot():
    """What Active Directory returns for that person when the two agree."""
    return {
        'givenName': "Jan",
        'sn': "Novák",
        'sAMAccountName': "novakjan",
        'displayName': "Jan Novák",
        'mail': "jan@skola.cz",
        'description': "Pupil",
        'homeDirectory': r"\\srv\students\novakjan",
        'homeDrive': "H:",
        'memberOf': ["CN=Zaci,DC=skola,DC=cz"],
    }


class TestTheFieldMap:
    """Which fields are compared at all."""

    def test_every_mapped_field_has_a_label(self):
        assert set(FIELD_TO_AD_ATTRIBUTE) == set(FIELD_LABELS)

    def test_the_password_is_not_compared(self):
        # Active Directory never gives a password back.
        assert 'ad_password' not in FIELD_TO_AD_ATTRIBUTE

    def test_the_class_is_not_compared(self):
        # The class is a position in the tree, not an attribute.
        assert 'class_name' not in FIELD_TO_AD_ATTRIBUTE

    def test_the_fields_the_report_names_are_all_compared(self):
        for field in ('ad_display_name', 'ad_email', 'home_directory',
                      'group_memberships'):
            assert field in FIELD_TO_AD_ATTRIBUTE


class TestCompare:
    """The comparison itself."""

    def test_identical_values_produce_no_differences(self, person,
                                                     matching_snapshot):
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_a_changed_field_is_reported(self, person, matching_snapshot):
        person.ad_email = "novy@skola.cz"
        differences = compare_person_with_ad(person, matching_snapshot)
        assert [d.field for d in differences] == ['ad_email']

    def test_the_difference_carries_both_values(self, person, matching_snapshot):
        person.ad_email = "novy@skola.cz"
        difference = compare_person_with_ad(person, matching_snapshot)[0]
        assert difference.app_value == "novy@skola.cz"
        assert difference.ad_value == "jan@skola.cz"
        assert difference.label == "Email"

    def test_several_differences_come_back_in_a_fixed_order(self, person,
                                                            matching_snapshot):
        person.ad_email = "novy@skola.cz"
        person.first_name = "Honza"
        assert [d.field for d in compare_person_with_ad(person, matching_snapshot)] \
            == ['first_name', 'ad_email']

    def test_a_home_directory_only_in_the_application_is_a_difference(
            self, person, matching_snapshot):
        del matching_snapshot['homeDirectory']
        differences = compare_person_with_ad(person, matching_snapshot)
        assert [d.field for d in differences] == ['home_directory']
        assert differences[0].ad_value == ""

    def test_none_and_empty_and_missing_all_count_as_not_set(self, make_person):
        person = make_person(ad_email=None)
        assert compare_person_with_ad(person, {'mail': "", 'givenName': "Jan",
                                               'sn': "Novák"}) == []

    def test_surrounding_whitespace_is_not_a_difference(self, person,
                                                        matching_snapshot):
        matching_snapshot['mail'] = "  jan@skola.cz  "
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_a_single_valued_attribute_returned_as_a_list_still_matches(
            self, person, matching_snapshot):
        matching_snapshot['mail'] = ["jan@skola.cz"]
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_no_snapshot_means_nothing_to_compare(self, person):
        assert compare_person_with_ad(person, None) == []

    def test_the_snapshot_is_read_from_the_metadata_when_not_passed(
            self, person, matching_snapshot):
        person.metadata[AD_VALUES_METADATA_KEY] = matching_snapshot
        person.ad_email = "novy@skola.cz"
        assert [d.field for d in compare_person_with_ad(person)] == ['ad_email']


class TestGroupComparison:
    """Groups are a set, not a list."""

    def test_the_same_groups_in_another_order_are_not_a_difference(
            self, person, matching_snapshot):
        person.add_to_group(ADGroup(name="Trida", dn="CN=Trida,DC=skola,DC=cz"))
        matching_snapshot['memberOf'] = ["CN=Trida,DC=skola,DC=cz",
                                         "CN=Zaci,DC=skola,DC=cz"]
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_a_dn_in_another_case_is_not_a_difference(self, person,
                                                      matching_snapshot):
        matching_snapshot['memberOf'] = ["cn=zaci,dc=skola,dc=cz"]
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_a_missing_group_is_a_difference(self, person, matching_snapshot):
        matching_snapshot['memberOf'] = []
        differences = compare_person_with_ad(person, matching_snapshot)
        assert [d.field for d in differences] == ['group_memberships']
        assert differences[0].app_value == "Zaci"
        assert differences[0].ad_value == ""

    def test_a_single_dn_returned_as_a_string_still_matches(self, person,
                                                           matching_snapshot):
        matching_snapshot['memberOf'] = "CN=Zaci,DC=skola,DC=cz"
        assert compare_person_with_ad(person, matching_snapshot) == []

    def test_group_names_are_shown_not_dns(self, person, matching_snapshot):
        matching_snapshot['memberOf'] = ["CN=Ucitele,DC=skola,DC=cz"]
        difference = compare_person_with_ad(person, matching_snapshot)[0]
        assert difference.ad_value == "Ucitele"

    def test_a_person_with_no_groups_matches_a_directory_with_none(self,
                                                                   make_person):
        assert compare_person_with_ad(make_person(first_name="Jan",
                                                  last_name="Novák"),
                                      {'givenName': "Jan", 'sn': "Novák"}) == []


class TestReadingOneValue:
    """What the tooltip shows for a single field."""

    def test_a_value_is_returned_as_text(self, matching_snapshot):
        assert describe_ad_value(matching_snapshot, 'ad_email') == "jan@skola.cz"

    def test_groups_are_rendered_as_names(self, matching_snapshot):
        assert describe_ad_value(matching_snapshot, 'group_memberships') == "Zaci"

    def test_a_field_the_directory_does_not_carry_is_empty(self,
                                                            matching_snapshot):
        del matching_snapshot['homeDrive']
        assert describe_ad_value(matching_snapshot, 'home_drive') == ""

    def test_an_unmapped_field_reads_as_nothing(self, matching_snapshot):
        assert ad_value_of(matching_snapshot, 'ad_password') is None

    def test_no_snapshot_reads_as_nothing(self):
        assert describe_ad_value(None, 'ad_email') == ""


class TestSnapshotPresence:
    """"Never discovered" is not the same as "identical"."""

    def test_a_person_without_a_snapshot_is_reported_as_such(self, person):
        assert has_ad_snapshot(person) is False

    def test_a_person_with_a_snapshot_is_reported_as_such(self, person,
                                                          matching_snapshot):
        person.metadata[AD_VALUES_METADATA_KEY] = matching_snapshot
        assert has_ad_snapshot(person) is True

    def test_an_empty_snapshot_does_not_count(self, person):
        person.metadata[AD_VALUES_METADATA_KEY] = {}
        assert has_ad_snapshot(person) is False


class TestStoringTheComparison:
    """The result has to survive until the table is drawn."""

    def test_the_differences_are_stored_as_plain_data(self, person,
                                                      matching_snapshot):
        person.ad_email = "novy@skola.cz"
        store_comparison(person, compare_person_with_ad(person, matching_snapshot))
        stored = person.metadata[DIFFERENCES_METADATA_KEY]
        assert isinstance(stored, list) and isinstance(stored[0], dict)

    def test_the_stored_differences_read_back_unchanged(self, person,
                                                        matching_snapshot):
        person.ad_email = "novy@skola.cz"
        original = store_comparison(
            person, compare_person_with_ad(person, matching_snapshot))
        assert stored_differences(person) == original

    def test_a_person_with_no_comparison_has_no_differences(self, person):
        assert stored_differences(person) == []

    def test_a_damaged_annotation_does_not_raise(self, person):
        person.metadata[DIFFERENCES_METADATA_KEY] = "not a list of dicts"
        assert stored_differences(person) == []

    def test_the_mapping_is_keyed_by_field(self, person, matching_snapshot):
        person.ad_email = "novy@skola.cz"
        store_comparison(person, compare_person_with_ad(person, matching_snapshot))
        assert set(differing_fields(person)) == {'ad_email'}

    def test_storing_an_empty_comparison_clears_the_previous_one(
            self, person, matching_snapshot):
        person.ad_email = "novy@skola.cz"
        store_comparison(person, compare_person_with_ad(person, matching_snapshot))
        person.ad_email = "jan@skola.cz"
        store_comparison(person, compare_person_with_ad(person, matching_snapshot))
        assert stored_differences(person) == []


class TestFieldDifference:
    """The small value object."""

    def test_it_round_trips_through_a_dictionary(self):
        difference = FieldDifference('ad_email', "a@b.cz", "c@d.cz")
        assert FieldDifference.from_dict(difference.as_dict()) == difference

    def test_an_unknown_field_falls_back_to_its_own_name(self):
        assert FieldDifference('mystery', "a", "b").label == "mystery"
