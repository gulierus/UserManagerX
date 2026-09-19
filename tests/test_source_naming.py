"""
Tests for the readable default names of freshly loaded sources.

Version 25, point 26: the Active Directory loader used to paste the raw
connection URL into the source name ('AD-ldaps://dc01.school.local:636').
"""

import pytest

from utils.source_naming import (
    MAX_NAME_LENGTH,
    branch_from_dn,
    build_ad_source_name,
    domain_from_dn,
    parse_dn,
    suggest_ad_source_name,
    unique_source_name,
)


class TestParseDn:
    """The minimal DN parser the naming helper needs."""

    def test_splits_components_into_attribute_and_value(self):
        assert parse_dn("OU=Students,DC=school,DC=local") == [
            ("ou", "Students"), ("dc", "school"), ("dc", "local")
        ]

    def test_attribute_case_is_normalised_but_value_case_is_kept(self):
        assert parse_dn("ou=Students,dc=School") == [
            ("ou", "Students"), ("dc", "School")
        ]

    def test_surrounding_whitespace_is_ignored(self):
        assert parse_dn(" OU = Students , DC = school ") == [
            ("ou", "Students"), ("dc", "school")
        ]

    def test_escaped_comma_stays_inside_the_value(self):
        # A class really can be called '6.A, second group'.
        assert parse_dn(r"OU=6.A\, second group,DC=school") == [
            ("ou", "6.A, second group"), ("dc", "school")
        ]

    def test_malformed_components_are_skipped_not_raised(self):
        assert parse_dn("OU=Students,garbage,DC=school") == [
            ("ou", "Students"), ("dc", "school")
        ]

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_input_gives_an_empty_list(self, value):
        assert parse_dn(value) == []


class TestDomainAndBranch:
    """Which part of the DN becomes which part of the name."""

    def test_domain_is_rebuilt_from_the_dc_components(self):
        assert domain_from_dn("OU=Students,DC=school,DC=local") == "school.local"

    def test_domain_is_empty_without_dc_components(self):
        assert domain_from_dn("OU=Students") == ""

    def test_branch_is_the_leftmost_non_domain_component(self):
        assert branch_from_dn("OU=Students,OU=School,DC=a,DC=b") == "Students"

    def test_domain_root_has_no_branch(self):
        assert branch_from_dn("DC=school,DC=local") == ""


class TestBuildAdSourceName:
    """The default name shown to the user."""

    def test_domain_and_organizational_unit_are_used(self):
        assert build_ad_source_name(
            "ldaps://dc01.school.local:636",
            "OU=Students,DC=school,DC=local") == "AD school.local - Students"

    def test_domain_root_gives_the_domain_alone(self):
        assert build_ad_source_name(
            "ldap://192.168.1.10", "DC=school,DC=local") == "AD school.local"

    def test_host_is_used_when_the_base_dn_names_no_domain(self):
        assert build_ad_source_name("dc01.school.local", "") == "AD dc01.school.local"

    def test_scheme_and_port_never_reach_the_name(self):
        name = build_ad_source_name("ldaps://dc01.school.local:636", "")
        assert "ldaps" not in name and "://" not in name and "636" not in name

    def test_ipv6_literal_loses_its_brackets_and_port(self):
        assert build_ad_source_name("ldap://[fe80::1]:389", "") == "AD fe80::1"

    def test_bare_ipv6_literal_without_port_is_kept_whole(self):
        # A single colon plus a non-numeric tail must not be mistaken for a port.
        assert build_ad_source_name("fe80::1", "") == "AD fe80::1"

    def test_two_branches_of_one_directory_get_different_names(self):
        first = build_ad_source_name("dc.school.local", "OU=Students,DC=school,DC=local")
        second = build_ad_source_name("dc.school.local", "OU=Teachers,DC=school,DC=local")
        assert first != second

    def test_nothing_at_all_still_gives_a_usable_name(self):
        assert build_ad_source_name("", "") == "AD"

    def test_over_long_names_are_truncated(self):
        name = build_ad_source_name("dc.school.local",
                                    f"OU={'Very Long Unit ' * 10},DC=school,DC=local")
        assert len(name) <= MAX_NAME_LENGTH


class TestUniqueSourceName:
    """Loading the same directory twice must not create two identical names."""

    def test_free_name_is_returned_unchanged(self):
        assert unique_source_name("AD school.local", ["Other"]) == "AD school.local"

    def test_taken_name_gets_a_counter(self):
        assert unique_source_name("AD school.local",
                                  ["AD school.local"]) == "AD school.local (2)"

    def test_counter_keeps_climbing(self):
        taken = ["AD school.local", "AD school.local (2)", "AD school.local (3)"]
        assert unique_source_name("AD school.local", taken) == "AD school.local (4)"

    def test_comparison_ignores_case_and_surrounding_whitespace(self):
        assert unique_source_name("AD school.local",
                                  ["  ad SCHOOL.local  "]) == "AD school.local (2)"

    def test_empty_name_falls_back_instead_of_returning_nothing(self):
        assert unique_source_name("   ", []) == "AD"

    def test_counter_does_not_push_the_name_past_the_length_limit(self):
        base = "A" * MAX_NAME_LENGTH
        assert len(unique_source_name(base, [base])) <= MAX_NAME_LENGTH


class TestSuggestAdSourceName:
    """The wrapper the loader actually calls."""

    def test_builds_and_deduplicates_in_one_step(self):
        assert suggest_ad_source_name(
            "ldaps://dc.school.local", "OU=Students,DC=school,DC=local",
            ["AD school.local - Students"]) == "AD school.local - Students (2)"

    def test_missing_existing_names_is_accepted(self):
        assert suggest_ad_source_name("dc.school.local", "DC=school,DC=local") == \
            "AD school.local"
