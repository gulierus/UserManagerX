"""
Unit tests for :mod:`utils.ad_utils`.

Covered:

* ``remove_diacritics`` / ``normalize_name_part`` - unicode folding rules,
  empty input, idempotency.
* ``build_username_base`` / ``generate_username`` - every index and length
  combination, multiple first/last names, out of range indexes, names that
  normalise to nothing, the 20 character limit, duplicate counters and the
  ``max_attempts`` guard.
* ``username_matches_convention`` - must accept everything the generator can
  emit and reject genuinely wrong user names.
* ``generate_password`` - policy bounds, required character classes, ambiguous
  character exclusion, broken policies, and a large sample that must always
  satisfy ``validate_password``.
* ``validate_username`` / ``validate_password`` / ``validate_email`` - issue
  types, severities and messages.
* ``generate_display_name`` - happy path and every error path.
* The LDAP entry points are only touched where they fail fast: no connection is
  ever attempted.

Tests marked ``bug`` + ``xfail`` assert the behaviour the production code
*should* have; they document defects that are reported separately.
"""

import re
import sys

import pytest

from models import Person
from utils import ad_utils
from utils.ad_utils import (
    USERNAME_MAX_LENGTH,
    USERNAME_PATTERN,
    ValidationIssue,
    add_users_to_ad,
    build_username_base,
    generate_display_name,
    generate_password,
    generate_username,
    normalize_name_part,
    remove_diacritics,
    reset_passwords,
    username_matches_convention,
    validate_email,
    validate_password,
    validate_username,
)
from utils.password_policy import (
    AMBIGUOUS_CHARACTERS,
    PasswordPolicy,
    set_password_policy,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _duplicate_candidate(base: str, counter: int) -> str:
    """Rebuild the name the duplicate loop produces for *counter*."""
    suffix = str(counter)
    return base[:USERNAME_MAX_LENGTH - len(suffix)] + suffix


def _messages(issues):
    """Return ``(field, issue_type, severity, message)`` for every issue."""
    return [(i.field, i.issue_type, i.severity, i.message) for i in issues]


# ---------------------------------------------------------------------------
# remove_diacritics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Novák", "Novak"),
    ("Šťastný", "Stastny"),
    ("Žížala", "Zizala"),
    ("Jürgen", "Jurgen"),
    ("Ångström", "Angstrom"),
    ("Éloïse", "Eloise"),
    ("İstanbul", "Istanbul"),
    ("Nový Jičín", "Novy Jicin"),
    ("plain", "plain"),
])
def test_remove_diacritics_strips_combining_marks(text, expected):
    """Combining marks are dropped while the base letters survive."""
    assert remove_diacritics(text) == expected


@pytest.mark.parametrize("text", ["", None])
def test_remove_diacritics_returns_empty_string_for_falsy_input(text):
    """Empty and None input collapse to the empty string, never None."""
    assert remove_diacritics(text) == ""


def test_remove_diacritics_applies_compatibility_decomposition():
    """NFKD also expands compatibility characters (ligatures, superscripts)."""
    assert remove_diacritics("ﬁ") == "fi"
    assert remove_diacritics("Jan²") == "Jan2"
    assert remove_diacritics("Ｊａｎ") == "Jan"


def test_remove_diacritics_transliterates_letters_nfkd_cannot_decompose():
    """Ł, ß and ø are single code points, so NFKD alone leaves them in place.

    They are transliterated explicitly, otherwise they survive into a user name
    that the sAMAccountName pattern then rejects.
    """
    assert remove_diacritics("Łódź") == "Lodz"
    assert remove_diacritics("Straße") == "Strasse"
    assert remove_diacritics("Ødegård") == "Odegard"
    assert remove_diacritics("Þórsdóttir") == "THorsdottir"


def test_remove_diacritics_is_idempotent():
    """Folding an already folded string changes nothing more."""
    once = remove_diacritics("Příliš žluťoučký kůň")
    assert remove_diacritics(once) == once


def test_remove_diacritics_returns_non_string_input_unchanged():
    """A non-string cannot be normalised; the guarded fallback returns it as is."""
    marker = 12345
    assert remove_diacritics(marker) is marker


# ---------------------------------------------------------------------------
# normalize_name_part
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Novák", "novak"),
    ("NOVÁK", "novak"),
    ("Anna-Marie", "annamarie"),
    ("O'Neill", "oneill"),
    ("de la Croix", "delacroix"),
    ("Jan 2", "jan2"),
    ("  Novák  ", "novak"),
    ("Ｊａｎ", "jan"),
    ("!!!", ""),
    ("   ", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_name_part_reduces_to_username_safe_characters(text, expected):
    """Diacritics, case and every non alphanumeric character are removed."""
    assert normalize_name_part(text) == expected


def test_normalize_name_part_is_idempotent():
    """Normalising twice gives the same result as normalising once."""
    once = normalize_name_part("Jean-Luc Étienne")
    assert normalize_name_part(once) == once == "jeanlucetienne"


@pytest.mark.bug
@pytest.mark.parametrize("text", ["Łukasz", "Straße", "Петров", "Øystein"])
def test_normalize_name_part_produces_ascii_only(text):
    """A user-name-safe form must be ASCII - AD user names are [a-z0-9]."""
    assert normalize_name_part(text).isascii()


# ---------------------------------------------------------------------------
# build_username_base
# ---------------------------------------------------------------------------

def test_build_username_base_default_is_lastname_plus_firstname():
    """The default base is the folded last name followed by the first name."""
    assert build_username_base("Jan", "Novák") == "novakjan"


@pytest.mark.parametrize("first,last,li,fi,expected", [
    ("Jan Petr", "Novák Svoboda", -1, 1, "svobodajan"),
    ("Jan Petr", "Novák Svoboda", 0, 0, "novaksvobodajanpetr"),
    ("Jan Petr", "Novák Svoboda", 1, 2, "novakpetr"),
    ("Jan Petr", "Novák Svoboda", 2, -1, "svobodapetr"),
    ("Jan Petr", "Novák Svoboda", 1, 1, "novakjan"),
])
def test_build_username_base_honours_name_indexes(first, last, li, fi, expected):
    """0 joins every part, -1 takes the last part, 1..n pick one part."""
    assert build_username_base(first, last, li, fi) == expected


@pytest.mark.parametrize("ll,fl,expected", [
    (None, None, "novakjan"),
    (3, None, "novjan"),
    (None, 1, "novakj"),
    (3, 1, "novj"),
    (1, 1, "nj"),
    (99, 99, "novakjan"),
])
def test_build_username_base_applies_length_limits(ll, fl, expected):
    """Length limits cut each part; a limit larger than the name is harmless."""
    assert build_username_base("Jan", "Novák", -1, 1, ll, fl) == expected


@pytest.mark.parametrize("index", [5, -2, 42])
def test_build_username_base_falls_back_to_first_part_for_unusable_index(index):
    """Unlike the generator, the validator helper is lenient: it uses part 1.

    None is NOT an unusable index - it means "the documented default" and is
    covered by test_generate_username_accepts_none_index_like_build_username_base.
    """
    assert build_username_base("Jan Petr", "Novák Svoboda",
                               last_name_index=index) == "novakjan"


@pytest.mark.parametrize("first,last", [
    ("", "Novák"), ("Jan", ""), ("   ", "Novák"), ("Jan", "   "),
    (None, "Novák"), ("Jan", None), (None, None),
    ("!!!", "Novák"), ("Jan", "---"),
])
def test_build_username_base_returns_empty_string_for_unusable_names(first, last):
    """Missing or unusable names produce "" instead of raising."""
    assert build_username_base(first, last) == ""


def test_build_username_base_truncates_to_the_samaccountname_limit():
    """The base never exceeds 20 characters."""
    base = build_username_base("Bartholomew", "Nejdlouhejsijmenonasvete")
    assert base == "nejdlouhejsijmenonas"
    assert len(base) == USERNAME_MAX_LENGTH


# ---------------------------------------------------------------------------
# generate_username - happy paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first,last,expected", [
    ("Jan", "Novák", "novakjan"),
    ("Anna-Marie", "Nováková", "novakovaannamarie"),
    ("Jean-Luc", "de la Croix", "croixjeanluc"),
    ("jan", "novak", "novakjan"),
    ("  Jan  ", "  Novák  ", "novakjan"),
])
def test_generate_username_builds_lowercase_ascii_name(first, last, expected):
    """Diacritics, punctuation and case disappear; last name comes first."""
    assert generate_username(first, last, set()) == expected


@pytest.mark.parametrize("li,fi,ll,fl,expected", [
    (-1, 1, None, None, "svobodajan"),
    (0, 1, None, None, "novaksvobodajan"),
    (1, 1, None, None, "novakjan"),
    (2, 2, None, None, "svobodapetr"),
    (-1, -1, None, None, "svobodapetr"),
    (0, 0, None, None, "novaksvobodajanpetr"),
    (1, 1, 3, 1, "novj"),
    (-1, 0, None, 2, "svobodaja"),
])
def test_generate_username_index_and_length_combinations(li, fi, ll, fl, expected):
    """Every documented index/length combination selects the right parts."""
    assert generate_username("Jan Petr", "Novák Svoboda", set(),
                             li, fi, ll, fl) == expected


def test_generate_username_is_deterministic_for_the_same_inputs():
    """The generator is pure: same names + same taken set, same result."""
    taken = {"novakjan", "novakjan2"}
    first = generate_username("Jan", "Novák", taken)
    assert first == generate_username("Jan", "Novák", set(taken)) == "novakjan3"
    assert taken == {"novakjan", "novakjan2"}, "the taken set must not be mutated"


@pytest.mark.parametrize("first,last", [
    ("Bartholomew", "Nejdlouhejsijmenonasvete"),
    ("Jan", "Novák"),
    ("Anna-Marie Alexandra", "Nováková Svobodová"),
])
def test_generate_username_never_exceeds_the_samaccountname_limit(first, last):
    """No generated name is longer than 20 characters."""
    assert len(generate_username(first, last, set())) <= USERNAME_MAX_LENGTH


def test_generate_username_truncates_a_long_base_to_twenty_characters():
    """A base longer than the limit is cut, it is not rejected."""
    assert generate_username("Bartholomew", "Nejdlouhejsijmenonasvete",
                             set()) == "nejdlouhejsijmenonas"


# ---------------------------------------------------------------------------
# generate_username - error paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first,last,message", [
    ("", "Novák", "First name cannot be empty"),
    ("   ", "Novák", "First name cannot be empty"),
    (None, "Novák", "First name cannot be empty"),
    ("Jan", "", "Last name cannot be empty"),
    ("Jan", "\t\n", "Last name cannot be empty"),
    ("Jan", None, "Last name cannot be empty"),
])
def test_generate_username_rejects_empty_names(first, last, message):
    """Empty, whitespace-only and None names raise ValueError."""
    with pytest.raises(ValueError, match=re.escape(message)):
        generate_username(first, last, set())


@pytest.mark.parametrize("first,last,message", [
    ("!!!", "Novák", "First name '!!!' contains no valid characters"),
    ("Jan", "---", "Last name '---' contains no valid characters"),
    ("...", "###", "Last name '###' contains no valid characters"),
])
def test_generate_username_rejects_names_that_normalize_to_nothing(first, last, message):
    """A name made only of punctuation leaves no usable characters."""
    with pytest.raises(ValueError, match=re.escape(message)):
        generate_username(first, last, set())


@pytest.mark.parametrize("li,fi,message", [
    (2, 1, "last_name_index 2 is out of range (1-1)"),
    (99, 1, "last_name_index 99 is out of range (1-1)"),
    (-2, 1, "last_name_index -2 is out of range (1-1)"),
    (1, 2, "first_name_index 2 is out of range (1-1)"),
    (1, -3, "first_name_index -3 is out of range (1-1)"),
])
def test_generate_username_rejects_out_of_range_indexes(li, fi, message):
    """Indexes outside 1..n (0 and -1 excepted) raise IndexError."""
    with pytest.raises(IndexError, match=re.escape(message)):
        generate_username("Jan", "Novák", set(), li, fi)


def test_generate_username_index_range_message_counts_all_parts():
    """The error message reports the real number of available parts."""
    with pytest.raises(IndexError, match=re.escape("(1-2)")):
        generate_username("Jan", "Novák Svoboda", set(), last_name_index=3)


def test_generate_username_accepts_none_index_like_build_username_base():
    """None is a documented Optional value meaning "use the default" (-1).

    generate_username and build_username_base must agree on it, otherwise the
    validator expects a different name than the generator produces.
    """
    assert generate_username("Jan Petr", "Novák Svoboda", set(),
                             last_name_index=None) == "svobodajan"
    assert build_username_base("Jan Petr", "Novák Svoboda",
                               last_name_index=None) == "svobodajan"
    # None and the explicit default must be indistinguishable
    assert (generate_username("Jan Petr", "Novák Svoboda", set(), last_name_index=None)
            == generate_username("Jan Petr", "Novák Svoboda", set()))


# ---------------------------------------------------------------------------
# generate_username - duplicates
# ---------------------------------------------------------------------------

def test_generate_username_appends_two_for_the_first_duplicate():
    """The first collision is resolved with the suffix 2, not 1."""
    assert generate_username("Jan", "Novák", {"novakjan"}) == "novakjan2"


def test_generate_username_counts_up_over_consecutive_duplicates():
    """Each additional collision increases the counter by one."""
    taken = {"novakjan"}
    produced = []
    for _ in range(5):
        name = generate_username("Jan", "Novák", taken)
        produced.append(name)
        taken.add(name)
    assert produced == ["novakjan2", "novakjan3", "novakjan4",
                        "novakjan5", "novakjan6"]


def test_generate_username_uses_two_digit_counters():
    """After nine collisions the counter simply becomes two digits."""
    taken = {"novakjan"} | {f"novakjan{i}" for i in range(2, 12)}
    assert generate_username("Jan", "Novák", taken) == "novakjan12"


def test_generate_username_keeps_the_limit_while_adding_a_counter():
    """A 20 character base is shortened so the counter still fits."""
    base = generate_username("Bartholomew", "Nejdlouhejsijmenonasvete", set())
    name = generate_username("Bartholomew", "Nejdlouhejsijmenonasvete", {base})
    assert name == "nejdlouhejsijmenona2"
    assert len(name) == USERNAME_MAX_LENGTH


def test_generate_username_result_is_never_already_taken():
    """Whatever comes back is guaranteed to be free."""
    taken = {"novakjan"} | {_duplicate_candidate("novakjan", c)
                            for c in range(2, 30)}
    name = generate_username("Jan", "Novák", taken)
    assert name not in taken
    assert name == "novakjan30"


def test_generate_username_raises_runtime_error_when_all_variants_are_taken():
    """The guard stops the loop instead of hanging when nothing is free."""
    taken = {"novakjan"} | {_duplicate_candidate("novakjan", c)
                            for c in range(2, 1200)}
    with pytest.raises(RuntimeError,
                       match="Could not generate unique username for Jan Novak"):
        generate_username("Jan", "Novak", taken)


def test_generate_username_uses_the_last_attempt_before_giving_up():
    """999 taken variants still leave the 1000th attempt usable."""
    taken = {"novakjan"} | {_duplicate_candidate("novakjan", c)
                            for c in range(2, 1000)}
    assert generate_username("Jan", "Novak", taken) == "novakjan1000"


@pytest.mark.bug
def test_generate_username_returns_a_free_name_found_on_the_last_attempt():
    """The 1000th attempt produces a free name, so it must be returned."""
    taken = {"novakjan"} | {_duplicate_candidate("novakjan", c)
                            for c in range(2, 1001)}
    free = _duplicate_candidate("novakjan", 1001)
    assert free not in taken
    assert generate_username("Jan", "Novak", taken) == free


@pytest.mark.parametrize("first,last,expected", [
    ("Łukasz", "Wiśniewski", "wisniewskilukasz"),   # Ł is not decomposed by NFKD
    ("Jürgen", "Straßer", "strasserjurgen"),        # ß expands to ss
    ("Jan", "Novák", "novakjan"),
    ("Øystein", "Ødegård", "odegardoystein"),
    ("Æsa", "Þórsdóttir", "thorsdottiraesa"),
])
def test_generate_username_output_always_matches_the_username_pattern(first, last, expected):
    """Whatever the generator emits must be a valid sAMAccountName."""
    name = generate_username(first, last, set())
    assert USERNAME_PATTERN.match(name), f"{name!r} is not a valid user name"
    assert name == expected


@pytest.mark.parametrize("first,last", [
    ("Иван", "Петров"),      # Cyrillic
    ("Ιωάννης", "Παπάς"),    # Greek
])
def test_generate_username_refuses_a_name_with_no_latin_characters(first, last):
    """A name with nothing transliterable must fail loudly, not invent a name.

    Making something up would produce a user name unrelated to the student;
    the callers report such records as skipped instead.
    """
    with pytest.raises(ValueError, match="contains no valid characters"):
        generate_username(first, last, set())


# ---------------------------------------------------------------------------
# generate_username <-> build_username_base agreement
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("first,last", [
    ("Jan", "Novák"),
    ("Jan Petr", "Novák Svoboda"),
    ("Anna-Marie", "Nováková"),
    ("Jean-Luc Étienne", "de la Croix"),
    ("Bartholomew", "Nejdlouhejsijmenonasvete"),
])
@pytest.mark.parametrize("li,fi,ll,fl", [
    (-1, 1, None, None),
    (0, 0, None, None),
    (1, 1, None, None),
    (-1, -1, None, None),
    (-1, 1, 3, 1),
    (-1, 1, None, 2),
])
def test_build_username_base_predicts_generate_username(first, last, li, fi, ll, fl):
    """The validator helper must agree with the generator on the base name."""
    assert (generate_username(first, last, set(), li, fi, ll, fl)
            == build_username_base(first, last, li, fi, ll, fl))


# ---------------------------------------------------------------------------
# username_matches_convention
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("first,last", [
    ("Jan", "Novák"),
    ("Jan Petr", "Novák Svoboda"),
    ("Anna-Marie", "Nováková"),
    ("Bartholomew", "Nejdlouhejsijmenonasvete"),
])
@pytest.mark.parametrize("li,fi,fl", [
    (-1, 1, None), (0, 0, None), (1, 1, None), (-1, -1, None),
    (0, 1, 1), (-1, 1, 2), (1, -1, 3),
])
def test_convention_accepts_every_variant_the_generator_emits(first, last, li, fi, fl):
    """Everything generate_username can produce must pass the convention check."""
    name = generate_username(first, last, set(), li, fi, None, fl)
    assert username_matches_convention(name, first, last)


@pytest.mark.parametrize("username", [
    "novakjan", "novakja", "novakj", "novak", "novakjan2", "novakjan12",
    "NovakJan", "NOVAKJAN", "nov",
])
def test_convention_accepts_tolerated_forms(username):
    """Shortened first names, the bare last name and counters are accepted."""
    assert username_matches_convention(username, "Jan", "Novák")


@pytest.mark.parametrize("username", [
    "jannovak",        # reversed order
    "svobodapetr",     # somebody else entirely
    "novakovajan",     # a different last name
    "novakjanicek",    # first name does not match
    "novakjanx",       # extra characters after the first name
    "xnovakjan",       # prefixed
    "novak2jan",       # counter in the middle
    "2novakjan",       # leading digit
    "12345",           # digits only
    "",                # nothing at all
])
def test_convention_rejects_names_that_do_not_follow_it(username):
    """Wrong order, wrong name or stray characters are rejected."""
    assert not username_matches_convention(username, "Jan", "Novák")


@pytest.mark.parametrize("username,first,last", [
    ("novaksvobodajan", "Jan", "Novák Svoboda"),
    ("svobodajan", "Jan", "Novák Svoboda"),
    ("novakjan", "Jan", "Novák Svoboda"),
    ("novakjanpetr", "Jan Petr", "Novák"),
    ("novakpetr", "Jan Petr", "Novák"),
])
def test_convention_accepts_any_of_several_name_parts(username, first, last):
    """With several first/last names every documented combination is valid."""
    assert username_matches_convention(username, first, last)


def test_convention_accepts_a_last_name_cut_by_the_length_limit():
    """A last name longer than 20 characters is truncated, not wrong."""
    assert username_matches_convention("nejdlouhejsijmenonas", "Jan",
                                       "Nejdlouhejsijmenonasvete")
    assert username_matches_convention("nejdlouhejsijmenona2", "Jan",
                                       "Nejdlouhejsijmenonasvete")


@pytest.mark.parametrize("first,last", [("", ""), ("Jan", ""), ("", "Novák"),
                                        (None, None), ("   ", "  ")])
def test_convention_passes_when_there_is_nothing_to_compare_with(first, last):
    """Without a name the check cannot fail - it returns True by design."""
    assert username_matches_convention("whatever", first, last)


@pytest.mark.bug
@pytest.mark.parametrize("ll,fl", [(3, None), (1, None), (3, 1), (1, 1)])
def test_convention_accepts_a_shortened_last_name(ll, fl):
    """A shortened last name is a supported generator variant."""
    name = generate_username("Jan", "Novák", set(), -1, 1, ll, fl)
    assert username_matches_convention(name, "Jan", "Novák")


# ---------------------------------------------------------------------------
# validate_username
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("username", [None, ""])
def test_validate_username_ignores_a_missing_username(make_person, username):
    """A missing user name is somebody else's problem - no issue here."""
    assert validate_username(make_person(ad_username=username)) == []


def test_validate_username_accepts_a_generated_name(make_person):
    """A freshly generated user name must validate without a single issue."""
    person = make_person("Jan", "Novák", ad_username=generate_username(
        "Jan", "Novák", set()))
    assert validate_username(person) == []


@pytest.mark.parametrize("username,expected", [
    ("a" * 21, "Username too long (21 chars, max 20)"),
    ("NovakJan", "Username must be lowercase"),
    ("novákjan", "Username must not contain diacritics"),
    ("1novakjan", "Username must start with a letter"),
    # '_' '.' '-' are legal in a sAMAccountName; a space is not.
    ("novak jan", "Username can only contain letters, numbers and . - _"),
])
def test_validate_username_reports_the_offending_rule(make_person, username, expected):
    """Every broken rule produces its own error message."""
    issues = validate_username(make_person("Jan", "Novák", ad_username=username))
    matching = [i for i in issues if i.message == expected]
    assert matching, _messages(issues)
    assert matching[0].severity == "error"
    assert matching[0].issue_type == "invalid_format"
    assert matching[0].field == "ad_username"


def test_validate_username_reports_every_broken_rule_at_once(make_person):
    """A thoroughly wrong user name collects several errors."""
    person = make_person("Jan", "Novák", ad_username="1Novák Jan" + "x" * 15)
    messages = {i.message for i in validate_username(person)}
    assert "Username must be lowercase" in messages
    assert "Username must not contain diacritics" in messages
    assert "Username must start with a letter" in messages
    assert "Username can only contain letters, numbers and . - _" in messages
    assert any(m.startswith("Username too long") for m in messages)


@pytest.mark.parametrize("username", ["novak.jan", "novak-jan", "novak_jan"])
def test_validate_username_accepts_the_separators_ad_allows(make_person, username):
    """AD permits . - _ in a sAMAccountName, so the format check must too.

    Rejecting them also made every user-name pattern that uses a separator
    (Operations tab -> "Username Format...") unusable.
    """
    issues = validate_username(make_person("Jan", "Novák", ad_username=username))
    assert not [i for i in issues if i.severity == "error"], _messages(issues)


def test_validate_username_warns_when_the_convention_is_broken(make_person):
    """A wrong pattern is only a warning and suggests the expected name."""
    person = make_person("Jan", "Novák", ad_username="jannovak")
    issues = validate_username(person)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "warning"
    assert issue.issue_type == "invalid_format"
    assert issue.person is person
    assert "novakjan" in issue.message


def test_validate_username_skips_the_convention_check_without_names(make_person):
    """Without a first or last name there is nothing to compare against."""
    person = make_person("", "", ad_username="something")
    assert validate_username(person) == []


def test_validate_username_accepts_the_exact_length_limit(make_person):
    """Exactly 20 characters is still allowed."""
    person = make_person("Jan", "N" * 20, ad_username="n" * 20)
    assert [i for i in validate_username(person)
            if i.severity == "error"] == []


@pytest.mark.bug
@pytest.mark.parametrize("first,last,username", [
    ("Иван", "Петров", "петровиван"),
    ("Jürgen", "Straßer", "straßerjurgen"),
    ("Łukasz", "Wiśniewski", "wisniewskiłukasz"),
])
def test_validate_username_flags_names_the_pattern_rejects(make_person, first,
                                                           last, username):
    """Anything USERNAME_PATTERN refuses has to produce an error."""
    assert USERNAME_PATTERN.match(username) is None, "precondition"
    issues = validate_username(make_person(first, last, ad_username=username))
    assert any(i.severity == "error" for i in issues), _messages(issues)


# ---------------------------------------------------------------------------
# generate_password
# ---------------------------------------------------------------------------

def test_generate_password_uses_the_policy_length_by_default():
    """Without an explicit length the policy's length wins."""
    policy = PasswordPolicy(length=14)
    assert len(generate_password(policy=policy)) == 14


@pytest.mark.parametrize("length", [8, 9, 16, 64])
def test_generate_password_honours_an_explicit_length(length):
    """An explicit length inside the policy bounds is used verbatim."""
    assert len(generate_password(length=length,
                                 policy=PasswordPolicy())) == length


@pytest.mark.parametrize("length,message", [
    (7, "Password length must be at least 8 characters (requested 7)"),
    (0, "Password length must be at least 8 characters (requested 0)"),
    (-5, "Password length must be at least 8 characters (requested -5)"),
    (65, "Password length must not exceed 64 characters (requested 65)"),
    (1000, "Password length must not exceed 64 characters (requested 1000)"),
])
def test_generate_password_rejects_lengths_outside_the_policy(length, message):
    """Both bounds are enforced with an explicit message."""
    with pytest.raises(ValueError, match=re.escape(message)):
        generate_password(length=length, policy=PasswordPolicy())


@pytest.mark.parametrize("policy,fragment", [
    (PasswordPolicy(length=2, min_length=8, max_length=64),
     "Generated length (2) is outside the accepted range 8-64"),
    (PasswordPolicy(require_special=True, special_characters=""),
     "Special characters are required but the character set is empty"),
    (PasswordPolicy(min_length=20, max_length=10, length=15),
     "Minimum length is greater than maximum length"),
    (PasswordPolicy(min_length=2, length=3, max_length=6),
     "Minimum length must be at least 4"),
])
def test_generate_password_refuses_a_broken_policy(policy, fragment):
    """A self-contradicting policy is reported instead of silently patched."""
    with pytest.raises(ValueError) as excinfo:
        generate_password(policy=policy)
    assert "The configured password format is not usable" in str(excinfo.value)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("policy", [
    PasswordPolicy(),
    PasswordPolicy(length=8, min_length=8, max_length=8),
    PasswordPolicy(length=4, min_length=4, max_length=10),
    PasswordPolicy(length=12, exclude_ambiguous=True),
    PasswordPolicy(length=10, special_characters="#"),
    PasswordPolicy(length=16, require_lowercase=False, require_uppercase=False,
                   require_digits=False, require_special=False),
], ids=["default", "fixed8", "shortest", "no-ambiguous", "one-special", "no-classes"])
def test_generated_passwords_always_satisfy_validate_password(policy, make_person):
    """A large sample of generated passwords never produces a single issue."""
    person = make_person()
    for _ in range(150):
        person.ad_password = generate_password(policy=policy)
        issues = validate_password(person, policy=policy)
        assert issues == [], (person.ad_password, _messages(issues))


def test_generate_password_contains_every_required_character_class():
    """One character of each required class is guaranteed, not hoped for."""
    policy = PasswordPolicy(length=8, special_characters="!@")
    for _ in range(100):
        password = generate_password(policy=policy)
        assert any(c.islower() for c in password), password
        assert any(c.isupper() for c in password), password
        assert any(c.isdigit() for c in password), password
        assert any(c in "!@" for c in password), password


def test_generate_password_omits_ambiguous_characters_when_asked():
    """exclude_ambiguous removes l, I, 1, O, 0, o, 5, S, 2 and Z entirely."""
    policy = PasswordPolicy(length=20, exclude_ambiguous=True)
    sample = "".join(generate_password(policy=policy) for _ in range(50))
    assert not set(sample) & set(AMBIGUOUS_CHARACTERS)


def test_generate_password_only_uses_the_configured_special_characters():
    """No special character outside the configured pool ever appears."""
    policy = PasswordPolicy(length=20, special_characters="#$")
    allowed = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$")
    sample = "".join(generate_password(policy=policy) for _ in range(50))
    assert set(sample) <= allowed


def test_generate_password_does_not_repeat_itself():
    """A cryptographic source must not hand out the same password twice."""
    passwords = {generate_password(policy=PasswordPolicy(length=12))
                 for _ in range(50)}
    assert len(passwords) == 50


def test_generate_password_falls_back_to_the_active_policy(isolated_settings):
    """policy=None uses the policy installed in the application."""
    set_password_policy(PasswordPolicy(length=11, min_length=8, max_length=32),
                        persist=False)
    assert len(generate_password()) == 11


def test_password_bounds_follow_the_active_policy(isolated_settings):
    """_password_bounds is read from the policy, not frozen at import time."""
    set_password_policy(PasswordPolicy(min_length=6, max_length=30, length=10),
                        persist=False)
    assert ad_utils._password_bounds() == (6, 30)


# ---------------------------------------------------------------------------
# validate_password
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("password", [None, ""])
def test_validate_password_ignores_a_missing_password(make_person, password):
    """An absent password is not a format problem."""
    assert validate_password(make_person(ad_password=password),
                             policy=PasswordPolicy()) == []


def test_validate_password_reports_a_short_password_as_error(make_person):
    """Too short is an error, and the message names both numbers."""
    person = make_person(ad_password="Abc1!de")
    issues = validate_password(person, policy=PasswordPolicy())
    assert _messages(issues) == [
        ("ad_password", "invalid_format", "error",
         "Password too short (7 chars, min 8)")]
    assert issues[0].person is person


def test_validate_password_reports_a_long_password_as_warning(make_person):
    """Too long is tolerated with a warning, not rejected."""
    person = make_person(ad_password="Ab1!" + "x" * 61)
    issues = validate_password(person, policy=PasswordPolicy())
    assert _messages(issues) == [
        ("ad_password", "invalid_format", "warning",
         "Password too long (65 chars, max 64)")]


@pytest.mark.parametrize("password,missing", [
    ("abcdefghij", "uppercase, digit, special character (!@#$%&*)"),
    ("ABCDEFGHIJ", "lowercase, digit, special character (!@#$%&*)"),
    ("Abcdefghij", "digit, special character (!@#$%&*)"),
    ("Abcdefgh1j", "special character (!@#$%&*)"),
    ("ABCDEFGH1!", "lowercase"),
])
def test_validate_password_lists_exactly_the_missing_classes(make_person,
                                                             password, missing):
    """The warning enumerates only the classes the policy actually demands."""
    issues = validate_password(make_person(ad_password=password),
                               policy=PasswordPolicy())
    assert _messages(issues) == [
        ("ad_password", "invalid_format", "warning",
         f"Password should contain: {missing}")]


def test_validate_password_accepts_anything_when_nothing_is_required(make_person):
    """A permissive policy reports no character class problems."""
    policy = PasswordPolicy(require_lowercase=False, require_uppercase=False,
                            require_digits=False, require_special=False)
    assert validate_password(make_person(ad_password="aaaaaaaaaa"),
                             policy=policy) == []


def test_validate_password_uses_the_configured_special_pool(make_person):
    """A special character outside the pool does not satisfy the requirement."""
    policy = PasswordPolicy(special_characters="#")
    person = make_person(ad_password="Abcdefg1!")
    issues = validate_password(person, policy=policy)
    assert _messages(issues) == [
        ("ad_password", "invalid_format", "warning",
         "Password should contain: special character (#)")]
    person.ad_password = "Abcdefg1#"
    assert validate_password(person, policy=policy) == []


def test_validate_password_lists_ambiguous_characters_sorted_and_deduplicated(
        make_person):
    """Every confusable character is reported once, in sorted order."""
    policy = PasswordPolicy(exclude_ambiguous=True)
    issues = validate_password(make_person(ad_password="Abcdef1!lOll"),
                               policy=policy)
    assert _messages(issues) == [
        ("ad_password", "invalid_format", "warning",
         "Password contains ambiguous characters: 1, O, l")]


def test_validate_password_ignores_ambiguous_characters_when_allowed(make_person):
    """Without exclude_ambiguous the very same password is fine."""
    assert validate_password(make_person(ad_password="Abcdef1!lOll"),
                             policy=PasswordPolicy()) == []


def test_validate_password_can_report_several_problems_at_once(make_person):
    """Length and character classes are checked independently."""
    issues = validate_password(make_person(ad_password="abc"),
                               policy=PasswordPolicy())
    assert [(i.severity, i.message) for i in issues] == [
        ("error", "Password too short (3 chars, min 8)"),
        ("warning", "Password should contain: uppercase, digit, "
                    "special character (!@#$%&*)")]


def test_validate_password_uses_the_active_policy_when_none_is_given(
        isolated_settings, make_person):
    """Without an explicit policy the configured one is used."""
    set_password_policy(PasswordPolicy(length=30, min_length=30, max_length=40),
                        persist=False)
    issues = validate_password(make_person(ad_password="Abcdefg1!"))
    assert [i.message for i in issues] == ["Password too short (9 chars, min 30)"]


# ---------------------------------------------------------------------------
# validate_email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email", [
    "jan@skola.cz",
    "jan.novak+tag@sub.skola.cz",
    "JAN@SKOLA.CZ",
    "j_a-n%1@skola-x.co.uk",
])
def test_validate_email_accepts_valid_addresses(make_person, email):
    """A well formed address produces no issue."""
    assert validate_email(make_person(ad_email=email)) == []


@pytest.mark.parametrize("email", [None, ""])
def test_validate_email_ignores_a_missing_address(make_person, email):
    """No address means nothing to validate."""
    assert validate_email(make_person(ad_email=email)) == []


@pytest.mark.parametrize("email", [
    "jan@skola",          # no TLD
    "jan@skola.c",        # one letter TLD
    "@skola.cz",          # no local part
    "jan.skola.cz",       # no @
    "jan novak@skola.cz", # space inside
    "jan@škola.cz",       # non-ASCII domain
    "jan@@skola.cz",
])
def test_validate_email_reports_invalid_addresses_as_error(make_person, email):
    """A malformed address is an error on the ad_email field."""
    issues = validate_email(make_person(ad_email=email))
    assert ("ad_email", "invalid_format", "error",
            "Invalid email format") in _messages(issues)


def test_validate_email_warns_about_surrounding_whitespace(make_person):
    """Whitespace alone is only a warning - the address itself is fine."""
    person = make_person(ad_email="  jan@skola.cz  ")
    issues = validate_email(person)
    assert _messages(issues) == [
        ("ad_email", "invalid_format", "warning",
         "Email contains leading/trailing whitespace")]
    assert issues[0].person is person


def test_validate_email_reports_whitespace_and_format_together(make_person):
    """A whitespace-only address is both untidy and invalid."""
    issues = validate_email(make_person(ad_email="   "))
    assert [(i.severity, i.message) for i in issues] == [
        ("warning", "Email contains leading/trailing whitespace"),
        ("error", "Invalid email format")]


# ---------------------------------------------------------------------------
# generate_display_name
# ---------------------------------------------------------------------------

def test_generate_display_name_uses_the_first_last_class_format():
    """The display name keeps diacritics and adds the class in brackets."""
    assert generate_display_name("Jan", "Novák", "9.A") == "Jan Novák (9.A)"


def test_generate_display_name_strips_surrounding_whitespace():
    """Each part is trimmed so the result has no double spaces."""
    assert generate_display_name(" Jan ", " Novák ", " 9.A ") == "Jan Novák (9.A)"


@pytest.mark.parametrize("first,last,class_name,message", [
    ("", "Novák", "9.A", "First name cannot be empty"),
    ("   ", "Novák", "9.A", "First name cannot be empty"),
    (None, "Novák", "9.A", "First name cannot be empty"),
    ("Jan", "", "9.A", "Last name cannot be empty"),
    ("Jan", None, "9.A", "Last name cannot be empty"),
    ("Jan", "\t", "9.A", "Last name cannot be empty"),
    ("Jan", "Novák", "", "Class name cannot be empty"),
    ("Jan", "Novák", None, "Class name cannot be empty"),
    ("Jan", "Novák", "  ", "Class name cannot be empty"),
])
def test_generate_display_name_rejects_missing_parts(first, last, class_name,
                                                     message):
    """Every missing part raises ValueError naming the field."""
    with pytest.raises(ValueError, match=re.escape(message)):
        generate_display_name(first, last, class_name)


def test_generate_display_name_checks_the_first_name_first():
    """When everything is missing the first name is reported."""
    with pytest.raises(ValueError, match="First name cannot be empty"):
        generate_display_name("", "", "")


# ---------------------------------------------------------------------------
# LDAP entry points - failure only, no connection is ever attempted
# ---------------------------------------------------------------------------

@pytest.fixture
def no_ldap3(monkeypatch):
    """Make ``import ldap3`` fail the way an uninstalled package would."""
    monkeypatch.setitem(sys.modules, "ldap3", None)
    monkeypatch.setitem(sys.modules, "ldap3.core", None)
    monkeypatch.setitem(sys.modules, "ldap3.core.exceptions", None)


def test_add_users_to_ad_explains_a_missing_ldap3(no_ldap3, make_source):
    """Without ldap3 the user gets an actionable RuntimeError, not ImportError."""
    with pytest.raises(RuntimeError,
                       match=re.escape("ldap3 library is not installed. "
                                       "Install it with: pip install ldap3")):
        add_users_to_ad(make_source("S", [("6.A", 1)]),
                        "ldap://server", "DC=x,DC=y", "admin", "secret")


def test_reset_passwords_explains_a_missing_ldap3(no_ldap3, make_person):
    """The same clear message is produced by the password reset path."""
    with pytest.raises(RuntimeError,
                       match=re.escape("ldap3 library is not installed. "
                                       "Install it with: pip install ldap3")):
        reset_passwords([make_person()], "ldap://server", "DC=x,DC=y",
                        "admin", "secret")


@pytest.mark.parametrize("source", [None, object(), "not-a-source"])
def test_add_users_to_ad_rejects_an_invalid_source_before_connecting(source):
    """A source without classes is refused up front, so nothing is dialled."""
    with pytest.raises(ValueError, match="Invalid source object"):
        add_users_to_ad(source, "ldap://server", "DC=x,DC=y", "admin", "secret")


@pytest.mark.parametrize("persons", [[], None])
def test_reset_passwords_returns_early_without_persons(persons):
    """An empty selection short-circuits before any connection is opened."""
    assert reset_passwords(persons, "ldap://server", "DC=x,DC=y",
                           "admin", "secret") == "No persons provided"


# ---------------------------------------------------------------------------
# ValidationIssue
# ---------------------------------------------------------------------------

def test_validation_issue_keeps_a_reference_to_the_person(make_person):
    """Issues carry the person itself so the UI can jump to the row."""
    person = make_person("Jan", "Novák")
    issue = ValidationIssue(person=person, field="ad_username",
                            issue_type="missing", message="m", severity="error")
    assert issue.person is person
    assert (issue.field, issue.issue_type, issue.severity) == (
        "ad_username", "missing", "error")


# ---------------------------------------------------------------------------
# validate_username - robustness corners
# ---------------------------------------------------------------------------

def test_validate_username_survives_names_without_usable_characters(make_person):
    """Punctuation-only names still warn instead of raising."""
    person = make_person("!!!", "???", ad_username="abcdef")
    issues = validate_username(person)
    assert [(i.severity, i.issue_type) for i in issues] == [
        ("warning", "invalid_format")]
    assert "expected last name + first name" in issues[0].message


def test_validate_username_swallows_errors_from_the_convention_check(make_person):
    """A non-string name cannot break validation of the rest of the record."""
    person = make_person(123, 456, ad_username="abcdef")
    assert validate_username(person) == []
