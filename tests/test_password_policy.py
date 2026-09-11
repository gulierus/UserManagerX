"""
Unit tests for :mod:`utils.password_policy`.

Covered
-------
* the dataclass defaults and the character pools (with / without
  ``exclude_ambiguous``),
* ``required_pools()`` / ``full_pool()`` including the "nothing is required"
  corner,
* every contradiction ``problems()`` is supposed to report, with the exact
  user facing wording,
* ``describe()``,
* ``to_dict`` / ``from_dict`` round trips, junk values, wrong types and an
  inconsistent stored policy falling back to the factory defaults,
* the module level accessors: caching, ``reload``, validation, persistence
  through the ``SettingsManager`` and ``reset_password_policy``.

The module never shows a dialog, so the ``dialogs`` fixture is not needed; but
every test that reaches the settings singleton uses ``isolated_settings`` so the
real user settings file is never read or written.
"""

import json
import string

import pytest

import utils.password_policy as pp
from utils.password_policy import (
    ABSOLUTE_MAX_LENGTH,
    ABSOLUTE_MIN_LENGTH,
    AMBIGUOUS_CHARACTERS,
    DEFAULT_SPECIAL_CHARACTERS,
    SETTINGS_CATEGORY,
    PasswordPolicy,
    get_password_policy,
    reset_password_policy,
    set_password_policy,
)

# Pools after the ambiguous characters ("lI1O0o5S2Z") have been removed.
FILTERED_LOWER = "abcdefghijkmnpqrstuvwxyz"
FILTERED_UPPER = "ABCDEFGHJKLMNPQRTUVWXY"
FILTERED_DIGITS = "346789"

FIELD_NAMES = {
    "length", "min_length", "max_length", "require_lowercase",
    "require_uppercase", "require_digits", "require_special",
    "special_characters", "exclude_ambiguous",
}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    """The factory policy has to be immediately usable."""

    def test_default_policy_has_no_contradictions(self):
        """The out-of-the-box policy must be usable without any editing."""
        policy = PasswordPolicy()
        assert policy.problems() == []
        assert policy.is_valid() is True

    def test_default_generated_length_is_inside_the_accepted_range(self):
        """The historic bug (generate 5, demand 8) must not come back."""
        policy = PasswordPolicy()
        assert policy.min_length <= policy.length <= policy.max_length

    def test_default_policy_requires_all_four_character_classes(self):
        """All four classes are required by default, so four pools contribute."""
        policy = PasswordPolicy()
        assert len(policy.required_pools()) == 4

    def test_default_policy_keeps_ambiguous_characters(self):
        """Ambiguity filtering is opt-in, not the default."""
        policy = PasswordPolicy()
        assert policy.exclude_ambiguous is False
        assert set(AMBIGUOUS_CHARACTERS) <= set(policy.full_pool())

    def test_absolute_limits_bracket_the_defaults(self):
        """The hard limits must actually admit the shipped defaults."""
        policy = PasswordPolicy()
        assert ABSOLUTE_MIN_LENGTH <= policy.min_length
        assert policy.max_length <= ABSOLUTE_MAX_LENGTH

    def test_two_default_policies_compare_equal_but_are_separate_objects(self):
        """Dataclass equality is by value; instances must not be shared."""
        first, second = PasswordPolicy(), PasswordPolicy()
        assert first == second
        assert first is not second


# ---------------------------------------------------------------------------
# Character pools
# ---------------------------------------------------------------------------

class TestCharacterPools:
    """``exclude_ambiguous`` must act on every pool, and only on it."""

    def test_pools_are_the_plain_ascii_sets_when_nothing_is_excluded(self):
        """Without filtering the pools are the untouched ASCII ranges."""
        policy = PasswordPolicy(exclude_ambiguous=False)
        assert policy.lowercase_pool == string.ascii_lowercase
        assert policy.uppercase_pool == string.ascii_uppercase
        assert policy.digit_pool == string.digits
        assert policy.special_pool == DEFAULT_SPECIAL_CHARACTERS

    def test_pools_drop_ambiguous_characters_when_asked(self):
        """The documented l/I/1/O/0 family disappears from every pool."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        assert policy.lowercase_pool == FILTERED_LOWER
        assert policy.uppercase_pool == FILTERED_UPPER
        assert policy.digit_pool == FILTERED_DIGITS

    def test_ambiguity_filter_is_case_sensitive(self):
        """Only the exact characters listed are removed: 'L', 'i', 's' survive."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        assert "l" not in policy.lowercase_pool and "i" in policy.lowercase_pool
        assert "o" not in policy.lowercase_pool
        assert "s" in policy.lowercase_pool and "z" in policy.lowercase_pool
        assert "L" in policy.uppercase_pool and "I" not in policy.uppercase_pool
        assert "S" not in policy.uppercase_pool and "Z" not in policy.uppercase_pool

    def test_filtering_never_reorders_or_duplicates_the_pool(self):
        """The filtered pool stays an ordered subsequence of the original."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        pool = policy.lowercase_pool
        assert len(set(pool)) == len(pool)
        assert list(pool) == [c for c in string.ascii_lowercase if c in set(pool)]

    def test_no_ambiguous_character_survives_anywhere_in_the_full_pool(self):
        """Even ambiguous characters typed into the special set are filtered."""
        policy = PasswordPolicy(
            exclude_ambiguous=True,
            special_characters=DEFAULT_SPECIAL_CHARACTERS + AMBIGUOUS_CHARACTERS,
        )
        assert set(policy.full_pool()).isdisjoint(set(AMBIGUOUS_CHARACTERS))
        assert policy.special_pool == DEFAULT_SPECIAL_CHARACTERS

    @pytest.mark.parametrize("special", ["", None])
    def test_empty_or_missing_special_set_yields_an_empty_special_pool(self, special):
        """An empty or ``None`` special set means "no special characters"."""
        policy = PasswordPolicy(special_characters=special)
        assert policy.special_pool == ""

    def test_special_pool_keeps_unicode_and_diacritics(self):
        """Non-ASCII special characters are passed through untouched."""
        policy = PasswordPolicy(special_characters="§±é≠čšž")
        assert policy.special_pool == "§±é≠čšž"

    def test_special_pool_keeps_unicode_when_filtering_ambiguous(self):
        """Filtering removes only the ASCII look-alikes, never diacritics."""
        policy = PasswordPolicy(special_characters="čšžO0l!", exclude_ambiguous=True)
        assert policy.special_pool == "čšž!"

    def test_special_pool_of_only_ambiguous_characters_collapses_to_empty(self):
        """A special set made of look-alikes vanishes when filtering is on."""
        policy = PasswordPolicy(special_characters="0O1l", exclude_ambiguous=True)
        assert policy.special_pool == ""


# ---------------------------------------------------------------------------
# required_pools() / full_pool()
# ---------------------------------------------------------------------------

class TestPoolSelection:
    """Which pools must contribute, and which may contribute."""

    def test_required_pools_follow_the_require_flags(self):
        """Only the classes that are switched on are returned."""
        policy = PasswordPolicy(require_uppercase=False, require_special=False)
        assert policy.required_pools() == [string.ascii_lowercase, string.digits]

    def test_required_pools_is_empty_when_nothing_is_required(self):
        """No requirement at all is allowed and yields no mandatory pool."""
        policy = PasswordPolicy(
            require_lowercase=False, require_uppercase=False,
            require_digits=False, require_special=False,
        )
        assert policy.required_pools() == []

    def test_required_pools_skips_a_required_but_empty_special_set(self):
        """An empty pool cannot contribute, so it is not handed to the generator."""
        policy = PasswordPolicy(special_characters="")
        assert policy.require_special is True
        assert policy.special_pool not in policy.required_pools()
        assert len(policy.required_pools()) == 3

    def test_required_pools_are_filtered_like_the_plain_pools(self):
        """The mandatory pools obey ``exclude_ambiguous`` as well."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        assert policy.required_pools() == [
            FILTERED_LOWER, FILTERED_UPPER, FILTERED_DIGITS,
            DEFAULT_SPECIAL_CHARACTERS,
        ]

    def test_full_pool_is_the_concatenation_of_every_class(self):
        """The generator pool is lower + upper + digits + specials, in order."""
        policy = PasswordPolicy()
        assert policy.full_pool() == (
            string.ascii_lowercase + string.ascii_uppercase
            + string.digits + DEFAULT_SPECIAL_CHARACTERS
        )
        assert len(policy.full_pool()) == 26 + 26 + 10 + len(DEFAULT_SPECIAL_CHARACTERS)

    def test_full_pool_drops_the_specials_when_the_set_is_emptied(self):
        """Emptying the special set removes those characters from generation."""
        policy = PasswordPolicy(special_characters="", require_special=False)
        assert policy.full_pool() == (
            string.ascii_lowercase + string.ascii_uppercase + string.digits
        )

    def test_full_pool_keeps_non_required_classes_as_filler(self):
        """Documented behaviour: unrequired classes still pad the password."""
        policy = PasswordPolicy(
            require_uppercase=False, require_digits=False, require_special=False,
        )
        pool = policy.full_pool()
        assert set(string.ascii_uppercase) <= set(pool)
        assert set(string.digits) <= set(pool)
        assert set(DEFAULT_SPECIAL_CHARACTERS) <= set(pool)

    def test_full_pool_is_never_empty_even_with_everything_switched_off(self):
        """The generator always has something to draw from."""
        policy = PasswordPolicy(
            require_lowercase=False, require_uppercase=False,
            require_digits=False, require_special=False, special_characters="",
        )
        assert policy.full_pool()
        assert set(string.ascii_letters) <= set(policy.full_pool())

    def test_full_pool_is_filtered_when_ambiguity_is_excluded(self):
        """The whole generator pool shrinks by exactly the ambiguous characters."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        expected = FILTERED_LOWER + FILTERED_UPPER + FILTERED_DIGITS
        assert policy.full_pool() == expected + DEFAULT_SPECIAL_CHARACTERS

    def test_pools_are_recomputed_and_reflect_later_mutation(self):
        """The pools are properties, not values frozen at construction."""
        policy = PasswordPolicy()
        assert "0" in policy.full_pool()
        policy.exclude_ambiguous = True
        assert "0" not in policy.full_pool()


# ---------------------------------------------------------------------------
# problems()
# ---------------------------------------------------------------------------

MIN_TOO_SMALL = f"Minimum length must be at least {ABSOLUTE_MIN_LENGTH}."
MAX_TOO_LARGE = f"Maximum length must not exceed {ABSOLUTE_MAX_LENGTH}."
MIN_OVER_MAX = "Minimum length is greater than maximum length."
EMPTY_SPECIALS = "Special characters are required but the character set is empty."
NO_POOLS = "No character type is available for generation."


class TestProblems:
    """Every contradiction the policy can contain, with its exact wording."""

    @pytest.mark.parametrize("kwargs", [
        {},
        {"length": 8, "min_length": 8, "max_length": 8},
        {"length": ABSOLUTE_MIN_LENGTH, "min_length": ABSOLUTE_MIN_LENGTH,
         "max_length": ABSOLUTE_MIN_LENGTH},
        {"length": ABSOLUTE_MAX_LENGTH, "min_length": ABSOLUTE_MIN_LENGTH,
         "max_length": ABSOLUTE_MAX_LENGTH},
        {"require_lowercase": False, "require_uppercase": False,
         "require_digits": False, "require_special": False,
         "special_characters": ""},
        {"exclude_ambiguous": True},
        {"special_characters": "!"},
    ])
    def test_usable_policies_report_no_problem(self, kwargs):
        """Boundary-but-legal configurations must be accepted."""
        assert PasswordPolicy(**kwargs).problems() == []

    def test_minimum_below_the_absolute_floor_is_reported(self):
        """``min_length`` under the hard floor of 4 is refused."""
        policy = PasswordPolicy(min_length=ABSOLUTE_MIN_LENGTH - 1, length=10)
        assert MIN_TOO_SMALL in policy.problems()

    def test_maximum_above_the_absolute_ceiling_is_reported(self):
        """``max_length`` over the hard ceiling of 128 is refused."""
        policy = PasswordPolicy(max_length=ABSOLUTE_MAX_LENGTH + 1)
        assert MAX_TOO_LARGE in policy.problems()

    def test_minimum_greater_than_maximum_is_reported(self):
        """A reversed range is a contradiction."""
        policy = PasswordPolicy(length=10, min_length=20, max_length=10)
        assert MIN_OVER_MAX in policy.problems()

    @pytest.mark.parametrize("length", [4, 7, 65, 100])
    def test_generated_length_outside_the_range_is_reported(self, length):
        """The generated length must sit inside min..max."""
        policy = PasswordPolicy(length=length)
        assert (f"Generated length ({length}) is outside the accepted "
                f"range 8-64.") in policy.problems()

    @pytest.mark.parametrize("length", [8, 9, 64])
    def test_generated_length_on_the_range_boundary_is_accepted(self, length):
        """The range is inclusive on both ends."""
        assert PasswordPolicy(length=length).problems() == []

    def test_length_too_small_for_the_required_classes_is_reported(self):
        """Four required classes cannot fit into a three character password."""
        policy = PasswordPolicy(length=3, min_length=3, max_length=10)
        assert ("Generated length (3) is too small for 4 required character "
                "types.") in policy.problems()

    def test_length_equal_to_the_number_of_required_classes_is_accepted(self):
        """One character per required class is exactly enough."""
        policy = PasswordPolicy(
            length=ABSOLUTE_MIN_LENGTH,
            min_length=ABSOLUTE_MIN_LENGTH,
            max_length=ABSOLUTE_MIN_LENGTH,
        )
        assert len(policy.required_pools()) == 4
        assert policy.problems() == []

    def test_required_class_count_shrinks_with_the_require_flags(self):
        """Switching classes off relaxes the minimum-length requirement."""
        policy = PasswordPolicy(
            length=2, min_length=2, max_length=10,
            require_uppercase=False, require_digits=False, require_special=False,
        )
        assert not any("too small for" in problem for problem in policy.problems())

    def test_required_special_set_that_is_empty_is_reported(self):
        """Requiring specials while offering none is a contradiction."""
        policy = PasswordPolicy(special_characters="", require_special=True)
        assert EMPTY_SPECIALS in policy.problems()

    def test_special_set_emptied_by_the_ambiguity_filter_is_reported(self):
        """The check looks at the *effective* pool, not the raw setting."""
        policy = PasswordPolicy(special_characters="0O1l", exclude_ambiguous=True)
        assert policy.special_characters
        assert EMPTY_SPECIALS in policy.problems()

    def test_empty_special_set_is_fine_when_specials_are_not_required(self):
        """No requirement, no complaint."""
        policy = PasswordPolicy(special_characters="", require_special=False)
        assert EMPTY_SPECIALS not in policy.problems()
        assert policy.problems() == []

    def test_no_available_character_type_cannot_be_provoked(self):
        """``full_pool()`` always falls back to letters, so this never fires."""
        hostile = [
            PasswordPolicy(require_lowercase=False, require_uppercase=False,
                           require_digits=False, require_special=False,
                           special_characters=""),
            PasswordPolicy(special_characters="", exclude_ambiguous=True,
                           require_special=False),
            PasswordPolicy(special_characters=None, require_special=False),
        ]
        for policy in hostile:
            assert NO_POOLS not in policy.problems()
            assert policy.full_pool()

    def test_several_contradictions_are_all_reported_together(self):
        """``problems()`` collects every issue instead of stopping at the first."""
        policy = PasswordPolicy(length=1, min_length=3, max_length=200)
        assert policy.problems() == [
            MIN_TOO_SMALL,
            MAX_TOO_LARGE,
            "Generated length (1) is outside the accepted range 3-200.",
            "Generated length (1) is too small for 4 required character types.",
        ]

    @pytest.mark.parametrize("length", [0, -1, -100])
    def test_non_positive_generated_length_is_rejected(self, length):
        """Zero and negative lengths are never usable."""
        assert PasswordPolicy(length=length).problems()

    def test_problems_returns_a_fresh_list_each_call(self):
        """Callers may mutate the result without poisoning the policy."""
        policy = PasswordPolicy(length=1)
        first = policy.problems()
        first.append("mutated")
        assert "mutated" not in policy.problems()

    def test_is_valid_mirrors_problems(self):
        """``is_valid()`` is exactly "no problems"."""
        good, bad = PasswordPolicy(), PasswordPolicy(min_length=1000)
        assert good.is_valid() is True and good.problems() == []
        assert bad.is_valid() is False and bad.problems() != []


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------

class TestDescribe:
    """The one-line summary shown in the Password Format dialog."""

    def test_default_policy_description(self):
        """The default policy renders every section in a fixed order."""
        assert PasswordPolicy().describe() == (
            "10 characters; must contain lower case, upper case, digit, "
            "special (!@#$%&*); accepted length 8-64"
        )

    def test_description_lists_only_the_required_classes(self):
        """Classes that are not required are not announced."""
        text = PasswordPolicy(require_digits=False, require_special=False).describe()
        assert "must contain lower case, upper case;" in text
        assert "digit" not in text and "special" not in text

    def test_description_drops_the_class_section_when_nothing_is_required(self):
        """Without requirements only the length information remains."""
        policy = PasswordPolicy(
            require_lowercase=False, require_uppercase=False,
            require_digits=False, require_special=False,
        )
        assert policy.describe() == "10 characters; accepted length 8-64"

    def test_description_mentions_the_ambiguity_filter(self):
        """The ambiguity switch is visible in the summary."""
        policy = PasswordPolicy(exclude_ambiguous=True)
        assert policy.describe().endswith(
            "without ambiguous characters; accepted length 8-64"
        )
        assert "without ambiguous characters" not in PasswordPolicy().describe()

    def test_description_shows_the_effective_special_pool(self):
        """The summary shows the filtered specials, not the raw setting."""
        policy = PasswordPolicy(special_characters="!0O@", exclude_ambiguous=True)
        assert "special (!@)" in policy.describe()

    def test_description_shows_custom_and_unicode_specials(self):
        """Unicode special characters reach the summary unchanged."""
        assert "special (§é)" in PasswordPolicy(special_characters="§é").describe()

    def test_description_reflects_a_custom_length_range(self):
        """The accepted range comes from the policy, not from the defaults."""
        policy = PasswordPolicy(length=16, min_length=12, max_length=32)
        assert policy.describe().startswith("16 characters;")
        assert policy.describe().endswith("accepted length 12-32")

    def test_description_of_an_unusable_policy_still_renders(self):
        """describe() never raises, even for a contradictory policy."""
        policy = PasswordPolicy(special_characters="", min_length=1000)
        assert "special ()" in policy.describe()
        assert policy.is_valid() is False


# ---------------------------------------------------------------------------
# to_dict() / from_dict()
# ---------------------------------------------------------------------------

class TestSerialisation:
    """Persistence must survive junk without ever producing a broken policy."""

    def test_to_dict_contains_exactly_the_policy_fields(self):
        """Only the stored fields are serialised - no derived pools."""
        assert set(PasswordPolicy().to_dict()) == FIELD_NAMES

    def test_to_dict_returns_an_independent_copy(self):
        """Mutating the exported dict must not touch the policy."""
        policy = PasswordPolicy()
        exported = policy.to_dict()
        exported["length"] = 99
        assert policy.length == 10
        assert policy.to_dict()["length"] == 10

    @pytest.mark.parametrize("policy", [
        PasswordPolicy(),
        PasswordPolicy(length=16, min_length=12, max_length=32),
        PasswordPolicy(exclude_ambiguous=True, special_characters="#-_"),
        PasswordPolicy(require_special=False, special_characters=""),
        PasswordPolicy(special_characters="čšž§"),
    ])
    def test_round_trip_preserves_a_valid_policy(self, policy):
        """to_dict -> from_dict is loss-free for every usable policy."""
        assert PasswordPolicy.from_dict(policy.to_dict()) == policy

    def test_round_trip_is_idempotent(self):
        """Repeated serialisation does not drift."""
        policy = PasswordPolicy(length=12, exclude_ambiguous=True)
        once = PasswordPolicy.from_dict(policy.to_dict())
        twice = PasswordPolicy.from_dict(once.to_dict())
        assert once == twice == policy

    def test_round_trip_through_json_preserves_the_policy(self):
        """The settings file is JSON, so the dict has to survive that too."""
        policy = PasswordPolicy(length=14, special_characters="!@#", max_length=40)
        restored = PasswordPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
        assert restored == policy

    @pytest.mark.parametrize("data", [None, {}, [], "junk", 42, ("length", 12)])
    def test_from_dict_falls_back_to_defaults_for_non_dict_input(self, data):
        """Anything that is not a populated dict yields the factory policy."""
        assert PasswordPolicy.from_dict(data) == PasswordPolicy()

    def test_from_dict_ignores_unknown_keys(self):
        """Keys from a newer or hand-edited settings file are skipped."""
        policy = PasswordPolicy.from_dict({"length": 12, "bogus": "x", "": None})
        assert policy.length == 12
        assert not hasattr(policy, "bogus")

    @pytest.mark.parametrize("stored,expected", [
        ("12", 12),
        (" 12 ", 12),
        (12.0, 12),
        (12.9, 12),      # truncation, not rounding
        ("0012", 12),
    ])
    def test_from_dict_coerces_numeric_strings_and_floats(self, stored, expected):
        """Values that convert cleanly to int configure the generator."""
        assert PasswordPolicy.from_dict({"length": stored}).length == expected

    def test_from_dict_turns_a_boolean_length_into_an_unusable_value(self):
        """``True`` converts to 1, which is not a usable length -> defaults."""
        assert PasswordPolicy.from_dict({"length": True}) == PasswordPolicy()

    @pytest.mark.parametrize("junk", ["abc", "", None, [], {}, [8], object()])
    def test_from_dict_ignores_unconvertible_numbers(self, junk):
        """A broken number falls back to the default for that field."""
        policy = PasswordPolicy.from_dict({"length": junk, "max_length": 40})
        assert policy.length == PasswordPolicy().length
        assert policy.max_length == 40

    def test_from_dict_keeps_the_good_values_when_one_field_is_junk(self):
        """One unusable value must not discard the rest of the policy."""
        policy = PasswordPolicy.from_dict(
            {"length": "not a number", "min_length": 6, "special_characters": "#"}
        )
        assert policy.min_length == 6
        assert policy.special_characters == "#"
        assert policy.length == PasswordPolicy().length

    @pytest.mark.parametrize("stored,expected", [
        (True, True), (False, False), (1, True), (0, False),
        ("yes", True), ("", False), (None, False), ([], False), ([0], True),
    ])
    def test_from_dict_coerces_flags_by_truthiness(self, stored, expected):
        """Boolean fields use Python truthiness (so "false" would be True)."""
        policy = PasswordPolicy.from_dict({"exclude_ambiguous": stored})
        assert policy.exclude_ambiguous is expected

    def test_from_dict_reads_a_string_flag_as_true(self):
        """Documents the truthiness trap: any non-empty string enables a flag."""
        assert PasswordPolicy.from_dict({"exclude_ambiguous": "false"}).exclude_ambiguous is True

    def test_from_dict_accepts_a_unicode_special_set(self):
        """Diacritics in the stored special set survive the load."""
        policy = PasswordPolicy.from_dict({"special_characters": "čšž§"})
        assert policy.special_characters == "čšž§"
        assert policy.special_pool == "čšž§"

    def test_from_dict_does_not_mutate_the_input(self):
        """Loading settings must leave the caller's dict untouched."""
        data = {"length": "12", "special_characters": "#"}
        original = dict(data)
        PasswordPolicy.from_dict(data)
        assert data == original

    @pytest.mark.parametrize("data", [
        {"min_length": 100, "max_length": 50},
        {"length": 200},
        {"min_length": 2},
        {"max_length": 500},
        {"special_characters": "", "require_special": True},
        {"length": "5", "min_length": "8"},
    ])
    def test_inconsistent_stored_policy_falls_back_to_defaults(self, data):
        """A contradictory settings block is dropped as a whole."""
        assert PasswordPolicy.from_dict(data) == PasswordPolicy()

    def test_inconsistent_stored_policy_is_logged(self, caplog):
        """The fallback is announced so an admin can find the bad setting."""
        with caplog.at_level("WARNING", logger=pp.__name__):
            PasswordPolicy.from_dict({"min_length": 100, "max_length": 50})
        assert "inconsistent" in caplog.text.lower()

    def test_unconvertible_value_is_logged(self, caplog):
        """A skipped value produces a warning, not a silent surprise."""
        with caplog.at_level("WARNING", logger=pp.__name__):
            PasswordPolicy.from_dict({"length": "abc"})
        assert "invalid password policy value" in caplog.text.lower()

    def test_from_dict_result_is_always_usable(self):
        """The documented contract: the returned policy is never broken."""
        for data in ({"length": -3}, {"min_length": 0, "max_length": 0},
                     {"special_characters": "0O", "exclude_ambiguous": True},
                     {"length": 4, "min_length": 4, "max_length": 4}):
            assert PasswordPolicy.from_dict(data).is_valid()

    @pytest.mark.bug
    def test_from_dict_treats_a_null_special_set_as_empty(self):
        """A JSON ``null`` special set must not become the letters N, o, n, e."""
        policy = PasswordPolicy.from_dict(
            {"special_characters": None, "require_special": False}
        )
        assert policy.special_pool in ("", DEFAULT_SPECIAL_CHARACTERS)
        assert "e" not in policy.special_pool

    @pytest.mark.bug
    # None is NOT junk - it means "no special characters" everywhere else in
    # this class and is covered by test_null_special_set_survives_a_save_and_reload.
    @pytest.mark.parametrize("junk", [True, ["!", "@"], {"a": 1}, 12])
    def test_from_dict_rejects_a_non_string_special_set(self, junk):
        """Junk in ``special_characters`` must not become password material."""
        policy = PasswordPolicy.from_dict(
            {"special_characters": junk, "require_special": False}
        )
        assert policy.special_characters == DEFAULT_SPECIAL_CHARACTERS

    @pytest.mark.bug
    def test_from_dict_survives_an_infinite_stored_length(self):
        """``json.load`` can produce ``inf``; from_dict must not explode."""
        assert PasswordPolicy.from_dict({"length": float("inf")}) == PasswordPolicy()

    def test_from_dict_survives_a_nan_stored_length(self):
        """``nan`` is unconvertible and must fall back like any other junk."""
        assert PasswordPolicy.from_dict({"length": float("nan")}) == PasswordPolicy()


# ---------------------------------------------------------------------------
# Module level accessors
# ---------------------------------------------------------------------------

class TestGetPasswordPolicy:
    """Caching, reloading and graceful degradation of the accessor."""

    def test_returns_defaults_when_nothing_is_stored(self, isolated_settings):
        """A fresh installation gets the factory policy."""
        assert get_password_policy() == PasswordPolicy()

    def test_result_is_cached_between_calls(self, isolated_settings, monkeypatch):
        """The generator may call this per password; it must not hit the disk."""
        import utils.settings_manager as sm
        calls = []
        real_get_settings = sm.get_settings

        def counting_get_settings():
            calls.append(1)
            return real_get_settings()

        monkeypatch.setattr(sm, "get_settings", counting_get_settings)
        first = get_password_policy()
        second = get_password_policy()
        assert first is second
        assert len(calls) == 1

    def test_reload_rereads_the_settings(self, isolated_settings, monkeypatch):
        """``reload=True`` bypasses the cache and builds a new policy."""
        import utils.settings_manager as sm
        calls = []
        real_get_settings = sm.get_settings

        def counting_get_settings():
            calls.append(1)
            return real_get_settings()

        monkeypatch.setattr(sm, "get_settings", counting_get_settings)
        first = get_password_policy()
        reloaded = get_password_policy(reload=True)
        assert len(calls) == 2
        assert reloaded is not first
        assert reloaded == first

    @pytest.mark.integration
    def test_reload_picks_up_a_policy_written_behind_our_back(self, isolated_settings):
        """An external change to the settings becomes visible after a reload."""
        assert get_password_policy().length == 10
        isolated_settings.set_category(SETTINGS_CATEGORY, {"length": 14})
        assert get_password_policy().length == 10, "cache must stay stable"
        assert get_password_policy(reload=True).length == 14

    @pytest.mark.integration
    def test_stored_policy_is_loaded_from_the_settings(self, isolated_settings):
        """A complete stored block replaces every default."""
        stored = PasswordPolicy(
            length=16, min_length=12, max_length=32,
            require_digits=False, special_characters="#-_",
            exclude_ambiguous=True,
        )
        isolated_settings.set_category(SETTINGS_CATEGORY, stored.to_dict())
        assert get_password_policy(reload=True) == stored

    @pytest.mark.integration
    def test_inconsistent_stored_policy_is_replaced_by_defaults(self, isolated_settings):
        """A corrupted settings block never disables password generation."""
        isolated_settings.set_category(
            SETTINGS_CATEGORY, {"length": 3, "min_length": 40, "max_length": 5}
        )
        assert get_password_policy(reload=True) == PasswordPolicy()

    def test_settings_failure_degrades_to_defaults(self, isolated_settings, monkeypatch, caplog):
        """A broken settings layer must not stop password generation."""
        import utils.settings_manager as sm

        def boom():
            raise RuntimeError("settings backend is gone")

        monkeypatch.setattr(sm, "get_settings", boom)
        with caplog.at_level("ERROR", logger=pp.__name__):
            policy = get_password_policy(reload=True)
        assert policy == PasswordPolicy()
        assert "Could not load the password policy" in caplog.text

    def test_the_returned_policy_is_the_shared_active_instance(self, isolated_settings):
        """Callers share one object, so mutating it changes the active policy."""
        policy = get_password_policy()
        policy.length = 12
        assert get_password_policy().length == 12
        assert get_password_policy() is policy


class TestSetPasswordPolicy:
    """Validation, activation and persistence of a new policy."""

    def test_valid_policy_becomes_the_active_one(self, isolated_settings):
        """After the call the accessor hands out the new policy."""
        policy = PasswordPolicy(length=16, min_length=12)
        assert set_password_policy(policy, persist=False) is True
        assert get_password_policy() is policy

    @pytest.mark.integration
    def test_policy_is_written_to_the_settings_file(self, isolated_settings):
        """Persisting must reach the JSON file, not only the in-memory dict."""
        policy = PasswordPolicy(length=16, min_length=12, special_characters="#")
        assert set_password_policy(policy) is True
        on_disk = json.loads(isolated_settings.settings_file.read_text(encoding="utf-8"))
        assert on_disk[SETTINGS_CATEGORY] == policy.to_dict()

    @pytest.mark.integration
    def test_persisted_policy_survives_a_reload(self, isolated_settings):
        """The saved policy is what a restart would load."""
        policy = PasswordPolicy(length=20, max_length=40, exclude_ambiguous=True)
        set_password_policy(policy)
        pp._active_policy = None
        assert get_password_policy() == policy

    def test_persist_false_leaves_the_settings_untouched(self, isolated_settings):
        """A preview policy must not be written to disk."""
        set_password_policy(PasswordPolicy(length=16), persist=False)
        assert isolated_settings.get_category(SETTINGS_CATEGORY) == {}
        assert not isolated_settings.settings_file.exists()

    def test_unpersisted_policy_is_lost_on_reload(self, isolated_settings):
        """``persist=False`` survives only until the next reload."""
        set_password_policy(PasswordPolicy(length=16, min_length=12), persist=False)
        assert get_password_policy().length == 16
        assert get_password_policy(reload=True) == PasswordPolicy()

    @pytest.mark.parametrize("policy,expected", [
        (PasswordPolicy(min_length=20, max_length=10, length=10), MIN_OVER_MAX),
        (PasswordPolicy(min_length=1), MIN_TOO_SMALL),
        (PasswordPolicy(max_length=500), MAX_TOO_LARGE),
        (PasswordPolicy(special_characters=""), EMPTY_SPECIALS),
        (PasswordPolicy(length=100), "outside the accepted range"),
    ])
    def test_invalid_policy_raises_value_error(self, isolated_settings, policy, expected):
        """The contradiction is reported verbatim in the exception message."""
        with pytest.raises(ValueError) as excinfo:
            set_password_policy(policy)
        assert expected in str(excinfo.value)

    def test_value_error_lists_every_problem(self, isolated_settings):
        """All contradictions are joined, so the dialog can show them at once."""
        broken = PasswordPolicy(length=1, min_length=3, max_length=200)
        with pytest.raises(ValueError) as excinfo:
            set_password_policy(broken, persist=False)
        assert str(excinfo.value) == "; ".join(broken.problems())

    def test_invalid_policy_does_not_replace_the_active_one(self, isolated_settings):
        """A rejected policy must leave the running configuration alone."""
        good = PasswordPolicy(length=16, min_length=12)
        set_password_policy(good, persist=False)
        with pytest.raises(ValueError):
            set_password_policy(PasswordPolicy(min_length=1))
        assert get_password_policy() is good

    def test_invalid_policy_is_rejected_even_without_persistence(self, isolated_settings):
        """Validation happens before the ``persist`` flag is consulted."""
        with pytest.raises(ValueError):
            set_password_policy(PasswordPolicy(max_length=1000), persist=False)
        assert isolated_settings.get_category(SETTINGS_CATEGORY) == {}

    def test_failed_persistence_returns_false_but_still_activates(
        self, isolated_settings, monkeypatch, caplog
    ):
        """A save failure is reported, yet the session keeps working."""
        import utils.settings_manager as sm

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(sm, "get_settings", boom)
        policy = PasswordPolicy(length=16, min_length=12)
        with caplog.at_level("ERROR", logger=pp.__name__):
            assert set_password_policy(policy) is False
        assert "Could not save the password policy" in caplog.text
        assert pp._active_policy is policy

    def test_setting_the_same_policy_twice_is_idempotent(self, isolated_settings):
        """Re-saving an unchanged policy neither fails nor changes the file."""
        policy = PasswordPolicy(length=16, min_length=12)
        assert set_password_policy(policy) is True
        first = isolated_settings.settings_file.read_text(encoding="utf-8")
        assert set_password_policy(policy) is True
        assert isolated_settings.settings_file.read_text(encoding="utf-8") == first

    @pytest.mark.integration
    def test_saving_replaces_the_previous_policy_completely(self, isolated_settings):
        """Stale keys from an older policy must not linger in the category."""
        isolated_settings.set_category(SETTINGS_CATEGORY, {"legacy_key": "x", "length": 9})
        set_password_policy(PasswordPolicy(length=16, min_length=12))
        assert "legacy_key" not in isolated_settings.get_category(SETTINGS_CATEGORY)

    @pytest.mark.integration
    @pytest.mark.bug
    def test_null_special_set_survives_a_save_and_reload(self, isolated_settings):
        """A policy without specials must reload as a policy without specials."""
        policy = PasswordPolicy(special_characters=None, require_special=False)
        set_password_policy(policy)
        reloaded = get_password_policy(reload=True)
        assert reloaded.special_pool == ""


class TestResetPasswordPolicy:
    """The "Restore defaults" button."""

    def test_reset_returns_and_activates_the_factory_policy(self, isolated_settings):
        """After a reset the accessor hands out exactly the returned policy."""
        set_password_policy(PasswordPolicy(length=32, max_length=64), persist=False)
        restored = reset_password_policy()
        assert restored == PasswordPolicy()
        assert get_password_policy() is restored

    @pytest.mark.integration
    def test_reset_persists_the_defaults(self, isolated_settings):
        """The defaults are written, so a restart does not resurrect the old policy."""
        set_password_policy(PasswordPolicy(length=32, max_length=64))
        reset_password_policy()
        assert isolated_settings.get_category(SETTINGS_CATEGORY) == PasswordPolicy().to_dict()
        pp._active_policy = None
        assert get_password_policy() == PasswordPolicy()

    def test_reset_is_idempotent(self, isolated_settings):
        """Resetting twice changes nothing and hands out fresh objects."""
        first = reset_password_policy()
        second = reset_password_policy()
        assert first == second == PasswordPolicy()
        assert first is not second

    def test_reset_still_works_when_persistence_fails(
        self, isolated_settings, monkeypatch
    ):
        """A broken settings layer must not block the restore button."""
        import utils.settings_manager as sm

        def boom():
            raise OSError("settings are read-only")

        monkeypatch.setattr(sm, "get_settings", boom)
        restored = reset_password_policy()
        assert restored == PasswordPolicy()
        assert pp._active_policy is restored

    @pytest.mark.integration
    def test_reset_repairs_a_corrupted_settings_block(self, isolated_settings):
        """Whatever junk was stored, the reset leaves a usable policy behind."""
        isolated_settings.set_category(SETTINGS_CATEGORY, {"length": "abc", "min_length": 99})
        restored = reset_password_policy()
        assert restored.is_valid()
        assert get_password_policy(reload=True) == PasswordPolicy()
