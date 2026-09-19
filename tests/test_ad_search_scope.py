"""
Tests for the Active Directory search scope.

The scope decides *where* the application looks - for a person during
discovery (Operations tab) and for the class organisational units when a
source is loaded ("1. Data Sources" tab, Version 26 point 24).
"""

import pytest

from utils.ad_search_scope import (
    DEFAULT_CLASS_OU_PATTERN,
    OU_PLACEHOLDERS,
    SearchScope,
    SearchScopeConfig,
    build_class_ou_filter,
    build_search_bases,
    class_name_from_ou,
    class_ou_patterns,
    class_ou_search_scope,
    escape_ldap_filter_value,
    ldap_scope,
    ou_template_to_pattern,
    render_ou_template,
)


def named(*templates):
    """A configuration that searches only the named organisational units."""
    return SearchScopeConfig(scope=SearchScope.NAMED_OUS,
                             ou_templates=list(templates))


class TestSearchScopeConfig:
    """The configuration object itself."""

    def test_the_default_is_the_whole_subtree(self):
        assert SearchScopeConfig().scope is SearchScope.SUBTREE

    def test_every_scope_has_a_label(self):
        assert all(scope.label for scope in SearchScope)

    def test_the_subtree_scope_has_no_problems(self):
        assert SearchScopeConfig().problems() == []

    def test_naming_no_unit_is_a_problem(self):
        problems = named("  ", "").problems()
        assert len(problems) == 1 and "nothing would be searched" in problems[0]

    def test_an_unknown_placeholder_is_a_problem(self):
        problems = named("Trida-{clas_name}").problems()
        assert any("{clas_name}" in problem for problem in problems)

    def test_a_known_placeholder_is_not_a_problem(self):
        assert named("Trida-{class_name}").problems() == []

    def test_describe_names_the_units(self):
        assert "Zaci" in named("Zaci").describe()

    def test_describe_says_so_when_nothing_is_named(self):
        assert "(none named)" in named("").describe()


class TestRenderOuTemplate:
    """Resolving a template for one person (Operations tab)."""

    def test_the_class_name_is_substituted(self, make_person):
        person = make_person(class_name="6.A")
        assert render_ou_template("Trida-{class_name}", person) == "Trida-6.A"

    def test_the_grade_is_the_arabic_number(self, make_person):
        assert render_ou_template("Rocnik-{grade}",
                                  make_person(class_name="6.A")) == "Rocnik-6"

    def test_the_roman_numeral_is_produced(self, make_person):
        assert render_ou_template("{roman}",
                                  make_person(class_name="6.A")) == "VI"

    def test_the_letter_is_extracted(self, make_person):
        assert render_ou_template("{letter}",
                                  make_person(class_name="6.A")) == "A"

    def test_the_enrollment_year_is_read_from_the_person(self, make_person):
        person = make_person(class_name="6.A")
        person.metadata["enrollment_year"] = 2020
        assert render_ou_template("{enrollment_year}", person) == "2020"

    def test_a_template_that_cannot_be_resolved_is_skipped(self, make_person):
        # No enrollment year has been calculated for this person.
        assert render_ou_template("{enrollment_year}",
                                  make_person(class_name="6.A")) is None

    def test_a_class_without_a_number_has_no_grade(self, make_person):
        assert render_ou_template("Rocnik-{grade}",
                                  make_person(class_name="Zaci")) is None

    def test_a_template_without_placeholders_is_returned_as_is(self, make_person):
        assert render_ou_template("Zaci", make_person(class_name="6.A")) == "Zaci"

    def test_the_escape_function_is_applied(self, make_person):
        assert render_ou_template("{class_name}", make_person(class_name="6,A"),
                                  escape=lambda v: v.replace(",", r"\,")) == r"6\,A"


class TestBuildSearchBases:
    """Which DNs a person is searched under."""

    def test_the_subtree_scope_searches_the_base_dn(self, make_person):
        assert build_search_bases(SearchScopeConfig(), "DC=school,DC=local",
                                  make_person()) == ["DC=school,DC=local"]

    def test_a_named_unit_becomes_a_search_base(self, make_person):
        bases = build_search_bases(named("Trida-{class_name}"), "DC=x",
                                   make_person(class_name="6.A"))
        assert bases[0] == "OU=Trida-6.A,DC=x"

    def test_the_base_dn_is_always_the_last_resort(self, make_person):
        bases = build_search_bases(named("Trida-{class_name}"), "DC=x",
                                   make_person(class_name="6.A"))
        assert bases[-1] == "DC=x"

    def test_an_unresolvable_template_leaves_only_the_base_dn(self, make_person):
        assert build_search_bases(named("{enrollment_year}"), "DC=x",
                                  make_person(class_name="6.A")) == ["DC=x"]

    def test_duplicate_bases_are_listed_once(self, make_person):
        bases = build_search_bases(named("Zaci", "Zaci"), "DC=x", make_person())
        assert bases == ["OU=Zaci,DC=x", "DC=x"]


class TestLdapScope:
    """The ldap3 constant used for a person search."""

    def test_the_subtree_scope_searches_the_subtree(self):
        from services.ldap_compat import SUBTREE
        assert ldap_scope(SearchScopeConfig()) == SUBTREE

    def test_the_other_scopes_search_one_level(self):
        from services.ldap_compat import LEVEL
        assert ldap_scope(SearchScopeConfig(scope=SearchScope.ONE_LEVEL)) == LEVEL
        assert ldap_scope(named("Zaci")) == LEVEL


class TestEscapeLdapFilterValue:
    """Nothing typed by the user may change the meaning of a filter."""

    @pytest.mark.parametrize("raw,expected", [
        ("(", r"\28"),
        (")", r"\29"),
        ("\\", r"\5c"),
        ("\0", r"\00"),
    ])
    def test_the_special_characters_are_escaped(self, raw, expected):
        assert escape_ldap_filter_value(raw) == expected

    def test_a_wildcard_is_escaped_by_default(self):
        assert escape_ldap_filter_value("6*A") == r"6\2aA"

    def test_a_wildcard_can_be_kept(self):
        assert escape_ldap_filter_value("Trida-*", keep_wildcards=True) == "Trida-*"

    def test_ordinary_text_is_untouched(self):
        assert escape_ldap_filter_value("Trida-6A") == "Trida-6A"

    def test_none_is_accepted(self):
        assert escape_ldap_filter_value(None) == ""


class TestOuTemplateToPattern:
    """A template becomes a name pattern during discovery."""

    def test_a_placeholder_becomes_a_wildcard(self):
        assert ou_template_to_pattern("Trida-{class_name}") == "Trida-*"

    @pytest.mark.parametrize("placeholder", sorted(OU_PLACEHOLDERS))
    def test_every_known_placeholder_becomes_a_wildcard(self, placeholder):
        assert ou_template_to_pattern(f"X-{{{placeholder}}}") == "X-*"

    def test_a_template_without_placeholders_names_one_unit(self):
        assert ou_template_to_pattern("Zaci") == "Zaci"

    def test_adjacent_placeholders_collapse_into_one_wildcard(self):
        # '**' means no more than '*' and some servers refuse it.
        assert ou_template_to_pattern("{grade}{letter}") == "*"

    def test_an_unknown_placeholder_is_refused(self):
        # Neither "match nothing" nor "match everything" would be honest.
        assert ou_template_to_pattern("Trida-{clas_name}") is None

    def test_a_blank_template_is_refused(self):
        assert ou_template_to_pattern("   ") is None

    def test_a_parenthesis_cannot_break_out_of_the_filter(self):
        assert ou_template_to_pattern("A(b)") == r"A\28b\29"


class TestClassOuPatterns:
    """Which unit names count as a class."""

    def test_the_convention_is_used_when_no_unit_is_named(self):
        assert class_ou_patterns(SearchScopeConfig()) == [DEFAULT_CLASS_OU_PATTERN]

    def test_the_one_level_scope_also_uses_the_convention(self):
        assert class_ou_patterns(SearchScopeConfig(scope=SearchScope.ONE_LEVEL)) == \
            [DEFAULT_CLASS_OU_PATTERN]

    def test_the_named_units_are_used_in_order(self):
        assert class_ou_patterns(named("Zaci", "Ucitele")) == ["Zaci", "Ucitele"]

    def test_duplicates_are_removed(self):
        assert class_ou_patterns(named("Zaci", "Zaci")) == ["Zaci"]

    def test_a_configuration_naming_nothing_usable_falls_back(self):
        # Better to search by convention than to search nothing at all.
        assert class_ou_patterns(named("{unknown}")) == [DEFAULT_CLASS_OU_PATTERN]


class TestBuildClassOuFilter:
    """The LDAP filter that finds the class units."""

    def test_the_default_filter_looks_for_the_convention(self):
        assert build_class_ou_filter(SearchScopeConfig()) == \
            "(&(objectClass=organizationalUnit)(ou=Trida-*))"

    def test_several_names_are_combined_with_or(self):
        assert build_class_ou_filter(named("Zaci", "Ucitele")) == \
            "(&(objectClass=organizationalUnit)(|(ou=Zaci)(ou=Ucitele)))"

    def test_one_name_needs_no_or(self):
        assert build_class_ou_filter(named("Zaci")) == \
            "(&(objectClass=organizationalUnit)(ou=Zaci))"

    def test_the_filter_always_restricts_to_organisational_units(self):
        assert "objectClass=organizationalUnit" in build_class_ou_filter(named("Zaci"))

    def test_the_filter_stays_balanced_with_a_hostile_name(self):
        built = build_class_ou_filter(named("A)(objectClass=*"))
        assert built.count("(") == built.count(")")


class TestClassOuSearchScope:
    """How deep the unit search goes."""

    def test_the_subtree_scope_searches_the_subtree(self):
        from services.ldap_compat import SUBTREE
        assert class_ou_search_scope(SearchScopeConfig()) == SUBTREE

    def test_the_one_level_scope_searches_one_level(self):
        from services.ldap_compat import LEVEL
        assert class_ou_search_scope(
            SearchScopeConfig(scope=SearchScope.ONE_LEVEL)) == LEVEL

    def test_a_named_unit_is_found_wherever_it_sits(self):
        from services.ldap_compat import SUBTREE
        assert class_ou_search_scope(named("Zaci")) == SUBTREE


class TestClassNameFromOu:
    """The unit name becomes the class name."""

    def test_the_conventional_prefix_is_removed(self):
        assert class_name_from_ou("Trida-6A") == "6A"

    def test_the_prefix_is_removed_in_any_spelling(self):
        # LDAP matched it case-insensitively, so the directory may answer with
        # any spelling of the prefix.
        assert class_name_from_ou("trida-7b") == "7b"

    def test_a_unit_without_the_prefix_keeps_its_name(self):
        assert class_name_from_ou("Zaci") == "Zaci"

    def test_a_unit_called_exactly_the_prefix_keeps_its_name(self):
        # Stripping it would leave a nameless class.
        assert class_name_from_ou("Trida-") == "Trida-"

    def test_surrounding_whitespace_is_removed(self):
        assert class_name_from_ou("  Trida-6A  ") == "6A"

    def test_an_empty_name_stays_empty(self):
        assert class_name_from_ou("") == ""
