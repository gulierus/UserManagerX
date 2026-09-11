"""
Unit tests for :mod:`utils.class_name_utils`.

The module under test has no Qt dependency and promises to be *total*: every
public entry point should return a value instead of raising for odd input.
These tests pin down the parser's structural decomposition, the round-trip
invariant of :meth:`ClassNameParts.rebuild`, template rendering/validation,
the conversion and shifting operations and the collection level analysis.
"""

import random

import pytest

from utils.class_name_utils import (
    MAX_NUMERAL_VALUE,
    MAX_PLAUSIBLE_ROMAN_VALUE,
    PREDEFINED_TEMPLATES,
    TEMPLATE_ARABIC,
    TEMPLATE_PLACEHOLDERS,
    TEMPLATE_ROMAN,
    ClassNameParts,
    ClassNameResult,
    NumeralStyle,
    TemplateError,
    analyze_class_names,
    convert_class_name,
    describe_signature,
    has_inner_whitespace,
    int_to_roman,
    parse_class_name,
    render_template,
    replace_whitespace,
    roman_to_int,
    shift_class_name,
    style_signature,
    validate_template,
)


# A pile of names that exercise every branch of the parser.  Used by the
# rebuild() round-trip invariant, which must hold for *every* input.
NASTY_NAMES = [
    "6.A", "IX.", "9A", "III. C", "ix.b", "06.A", "Blue class 6.A",
    "My sixth A", "Window", "", "   ", "\t", "\n ", " 6.A ", "  6. A  ",
    "IXA", "IXa", "MIX", "CIVIC", "DIM", "VIC", "XLI", "XLI.A", "L.A",
    "0", "0.A", "00.B", "6.ABCD", "6.ABC", "6..A", "6-A", "6 A", "6 . A",
    "6\xa0A", "A6", "IX.B extra", "40000.A", "Room 12345 class 6.A",
    "6.Á", "VI.Č", "9.ž", "i.a", "I", "-5.A", "6/A", "6|A", "IX 6", "6 IX",
    "Prima", "1.A/2.B", "třída 6.A", "6.A ", "  ", "V.", "..", "..6..A..",
]


# ---------------------------------------------------------------------------
# int_to_roman
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (1, "I"), (3, "III"), (4, "IV"), (6, "VI"), (9, "IX"), (10, "X"),
    (14, "XIV"), (40, "XL"), (49, "XLIX"), (90, "XC"), (400, "CD"),
    (500, "D"), (1009, "MIX"), (1990, "MCMXC"), (3999, "MMMCMXCIX"),
])
def test_int_to_roman_returns_the_canonical_numeral(value, expected):
    """int_to_roman uses subtractive notation, never repeated 'IIII' forms."""
    assert int_to_roman(value) == expected


@pytest.mark.parametrize("value", [0, -1, -3999, MAX_NUMERAL_VALUE + 1, 10 ** 6])
def test_int_to_roman_rejects_values_outside_the_supported_range(value):
    """Values below 1 or above MAX_NUMERAL_VALUE raise ValueError."""
    with pytest.raises(ValueError, match=r"out of range \(1-3999\)"):
        int_to_roman(value)


@pytest.mark.parametrize("value,type_name", [
    (True, "bool"), (False, "bool"), (1.0, "float"), ("5", "str"),
    (None, "NoneType"), ([6], "list"),
])
def test_int_to_roman_rejects_non_int_input_naming_the_type(value, type_name):
    """Booleans count as non-ints; the error message names the offending type."""
    with pytest.raises(ValueError, match=f"needs an int, got {type_name}"):
        int_to_roman(value)


# ---------------------------------------------------------------------------
# roman_to_int
# ---------------------------------------------------------------------------

def test_roman_to_int_round_trips_every_supported_value():
    """roman_to_int(int_to_roman(n)) == n for the whole supported range."""
    broken = [n for n in range(1, MAX_NUMERAL_VALUE + 1)
              if roman_to_int(int_to_roman(n)) != n]
    assert broken == []


@pytest.mark.parametrize("text", [
    "IIII", "VV", "IC", "IL", "IM", "XXXX", "VX", "MMMM", "IXI", "XM",
    "CIVIC", "DILL", "VIC", "XLI I", "IIIII", "LL", "DD", "VIIII",
])
def test_roman_to_int_rejects_non_canonical_numerals(text):
    """Look-alikes that int_to_roman would never produce are refused."""
    assert roman_to_int(text) is None


@pytest.mark.parametrize("text,expected", [
    ("ix", 9), ("IX", 9), ("iX", 9), ("  IX  ", 9), ("\tvi\n", 6),
    ("XIX", 19), ("XL", 40), ("XLI", 41), ("MMMCMXCIX", MAX_NUMERAL_VALUE),
])
def test_roman_to_int_is_case_insensitive_and_ignores_outer_spaces(text, expected):
    """Case and surrounding whitespace do not change the recognised value."""
    assert roman_to_int(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "ABC", "Window", "6", "IX9", "0", "-"])
def test_roman_to_int_returns_none_for_text_that_is_not_a_numeral(text):
    """Empty, None and non-Roman text yield None rather than an exception."""
    assert roman_to_int(text) is None


@pytest.mark.bug
@pytest.mark.parametrize("value", [123, 9.5, ["I", "X"]])
def test_roman_to_int_returns_none_for_non_string_input(value):
    """A total function should answer None for wrong-typed input, not crash."""
    assert roman_to_int(value) is None


# ---------------------------------------------------------------------------
# parse_class_name - structure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,prefix,numeral_text,value,style,separator,letter,suffix", [
        ("6.A", "", "6", 6, NumeralStyle.ARABIC, ".", "A", ""),
        ("IX.", "", "IX", 9, NumeralStyle.ROMAN, ".", "", ""),
        ("9A", "", "9", 9, NumeralStyle.ARABIC, "", "A", ""),
        ("III. C", "", "III", 3, NumeralStyle.ROMAN, ". ", "C", ""),
        ("ix.b", "", "ix", 9, NumeralStyle.ROMAN, ".", "b", ""),
        ("06.A", "", "06", 6, NumeralStyle.ARABIC, ".", "A", ""),
        ("Blue class 6.A", "Blue class ", "6", 6, NumeralStyle.ARABIC, ".", "A", ""),
        ("6 A", "", "6", 6, NumeralStyle.ARABIC, " ", "A", ""),
        ("6 . A", "", "6", 6, NumeralStyle.ARABIC, " . ", "A", ""),
        ("6-A", "", "6", 6, NumeralStyle.ARABIC, "-", "A", ""),
        ("IX.B extra", "", "IX", 9, NumeralStyle.ROMAN, ".", "B", " extra"),
        ("A6", "A", "6", 6, NumeralStyle.ARABIC, "", "", ""),
        ("6.Á", "", "6", 6, NumeralStyle.ARABIC, ".", "Á", ""),
        ("VI.Č", "", "VI", 6, NumeralStyle.ROMAN, ".", "Č", ""),
        ("12.C", "", "12", 12, NumeralStyle.ARABIC, ".", "C", ""),
    ])
def test_parse_class_name_splits_the_documented_shapes(
        name, prefix, numeral_text, value, style, separator, letter, suffix):
    """Every documented class-name shape decomposes into the expected parts."""
    parts = parse_class_name(name)
    assert (parts.prefix, parts.numeral_text, parts.numeral_value,
            parts.numeral_style, parts.separator, parts.letter, parts.suffix) == (
        prefix, numeral_text, value, style, separator, letter, suffix)


@pytest.mark.parametrize("name", ["Window", "My sixth A", "Prima", "", "   ", None, "\t"])
def test_parse_class_name_reports_free_text_without_a_numeral(name):
    """Unrecognisable names keep everything in prefix and report no numeral."""
    parts = parse_class_name(name)
    assert parts.has_numeral is False
    assert parts.numeral_value is None
    assert parts.numeral_style is NumeralStyle.NONE
    assert parts.prefix == parts.original


def test_parse_class_name_coerces_non_string_input_to_text():
    """Non-str input is stringified rather than rejected."""
    parts = parse_class_name(6)
    assert parts.original == "6"
    assert (parts.numeral_value, parts.numeral_style) == (6, NumeralStyle.ARABIC)


def test_parse_class_name_maps_none_onto_the_empty_name():
    """None behaves exactly like the empty string."""
    assert parse_class_name(None).original == ""
    assert parse_class_name(None).has_numeral is False


def test_parse_class_name_keeps_leading_and_trailing_spaces_out_of_the_numeral():
    """Outer whitespace lands in prefix/suffix and still counts as standard."""
    parts = parse_class_name("  6.A  ")
    assert (parts.prefix, parts.numeral_text, parts.letter, parts.suffix) == (
        "  ", "6", "A", "  ")
    assert parts.is_standard is True
    assert parts.has_surrounding_text is False


@pytest.mark.parametrize("name,numeral,letter", [
    ("IXA", "IX", "A"), ("IXa", "IX", "a"), ("VIC", "VI", "C"), ("VIa", "VI", "a"),
])
def test_parse_class_name_splits_glued_roman_numeral_and_section(name, numeral, letter):
    """"IXA" style names without a separator fall back to the glued heuristic."""
    parts = parse_class_name(name)
    assert parts.numeral_style is NumeralStyle.ROMAN
    assert (parts.numeral_text, parts.separator, parts.letter) == (numeral, "", letter)


@pytest.mark.parametrize("name", ["MIX", "CIVIC", "DIM", "Mix", "civic", "Dill", "L.A", "XLI.A"])
def test_parse_class_name_does_not_mistake_roman_letter_words_for_numerals(name):
    """Words made of Roman letters stay free text thanks to the plausibility cap."""
    parts = parse_class_name(name)
    assert parts.has_numeral is False
    assert parts.prefix == name


def test_parse_class_name_accepts_roman_values_up_to_the_plausibility_cap():
    """XL (40) is still a class numeral; the cap is inclusive."""
    parts = parse_class_name("XL.A")
    assert parts.numeral_value == MAX_PLAUSIBLE_ROMAN_VALUE == 40
    assert parts.numeral_style is NumeralStyle.ROMAN


def test_glued_heuristic_splits_an_over_cap_roman_token_into_numeral_and_letter():
    """"XLI" is 41 > cap, so it is read as class XL section I, not as 41."""
    parts = parse_class_name("XLI")
    assert (parts.numeral_value, parts.letter) == (40, "I")


@pytest.mark.parametrize("name,style,numeral", [
    ("6 IX", NumeralStyle.ARABIC, "6"),
    ("IX 6", NumeralStyle.ROMAN, "IX"),
])
def test_parse_class_name_prefers_whichever_notation_appears_first(name, style, numeral):
    """When both notations are present the leftmost one wins."""
    parts = parse_class_name(name)
    assert parts.numeral_style is style
    assert parts.numeral_text == numeral


@pytest.mark.parametrize("name,letter,suffix", [
    ("6.A", "A", ""), ("6.AB", "AB", ""), ("6.ABC", "ABC", ""), ("6.ABCD", "", "ABCD"),
])
def test_section_letter_is_limited_to_three_letters(name, letter, suffix):
    """A section token is at most three letters and must not be glued to more."""
    parts = parse_class_name(name)
    assert (parts.letter, parts.suffix) == (letter, suffix)


@pytest.mark.parametrize("name", ["40000.A", "Room 12345 class 6.A"])
def test_number_above_the_maximum_makes_the_whole_name_unparsed(name):
    """An out-of-range Arabic run disables numeral recognition for the name."""
    parts = parse_class_name(name)
    assert parts.has_numeral is False


def test_zero_is_recognised_as_an_arabic_numeral():
    """"0.A" parses with numeral_value 0 - the parser has no lower bound."""
    parts = parse_class_name("0.A")
    assert parts.numeral_value == 0
    assert parts.has_numeral is True


@pytest.mark.parametrize("name", NASTY_NAMES)
def test_rebuild_reproduces_the_original_name_exactly(name):
    """The documented invariant prefix+numeral+separator+letter+suffix == input."""
    parts = parse_class_name(name)
    assert parts.rebuild() == parts.original == name


def test_rebuild_invariant_survives_randomised_input():
    """Fuzzing the parser must never break the round-trip invariant."""
    rng = random.Random(20240917)
    alphabet = list("69031AaBbIXVixMCDLl .-_/:;|,ÁČž\t") + ["IX", "class", ""]
    for _ in range(3000):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
        parts = parse_class_name(name)
        assert parts.rebuild() == name, f"rebuild broke for {name!r}"


# ---------------------------------------------------------------------------
# ClassNameParts queries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,standard,surrounded,letter,lower_roman", [
    ("6.A", True, False, True, False),
    ("IX. B", True, False, True, False),
    ("9A", True, False, True, False),
    ("IX.", True, False, False, False),
    ("ix.b", True, False, True, True),
    ("Blue class 6.A", False, True, True, False),
    ("Window", False, True, False, False),
    ("6.ABCD", False, True, False, False),
])
def test_class_name_parts_queries_classify_the_name(
        name, standard, surrounded, letter, lower_roman):
    """is_standard / has_surrounding_text / has_letter / is_roman_lowercase."""
    parts = parse_class_name(name)
    assert (parts.is_standard, parts.has_surrounding_text,
            parts.has_letter, parts.is_roman_lowercase) == (
        standard, surrounded, letter, lower_roman)


@pytest.mark.parametrize("name,surrounded", [
    ("Window", True), ("", False), ("   ", False), ("6.A", False),
])
def test_has_surrounding_text_covers_free_text_names_too(name, surrounded):
    """A name with no numeral is all prefix, so it counts as surrounding text."""
    assert parse_class_name(name).has_surrounding_text is surrounded


@pytest.mark.parametrize("name,expected", [
    ("6.A", "Arabic (6, 9) = 6, section 'A'"),
    ("IX.", "Roman (VI, IX) = 9, no section letter"),
    ("Blue class 6.A", "Arabic (6, 9) = 6, section 'A', extra text around numeral"),
    ("Window", "no numeral recognised"),
])
def test_describe_summarises_the_parse_for_the_preview_dialog(name, expected):
    """describe() spells out notation, value, section letter and extra text."""
    assert parse_class_name(name).describe() == expected


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placeholder,expected", [
    ("{arabic}", "9"), ("{roman}", "IX"), ("{roman_lower}", "ix"),
    ("{number}", "ix"), ("{letter}", "b"), ("{letter_upper}", "B"),
    ("{letter_lower}", "b"), ("{prefix}", ""), ("{suffix}", ""),
    ("{separator}", ". "), ("{original}", "ix. b"),
])
def test_render_template_resolves_each_placeholder(placeholder, expected):
    """Every documented placeholder renders the matching part of "ix. b"."""
    assert render_template(placeholder, parse_class_name("ix. b")) == expected


def test_number_placeholder_keeps_the_notation_of_the_original_name():
    """{number} echoes Roman/Arabic and the original letter case."""
    assert render_template("{number}", parse_class_name("IX.B")) == "IX"
    assert render_template("{number}", parse_class_name("ix.b")) == "ix"
    assert render_template("{number}", parse_class_name("9.B")) == "9"


@pytest.mark.parametrize("template,name,expected", [
    ("{arabic:2}", "6.A", "06"),
    ("{arabic:1}", "6.A", "6"),
    ("{arabic:10}", "6.A", "0000000006"),
    ("{arabic:2}", "100.A", "100"),
    ("{arabic:}", "6.A", "6"),
    ("{arabic: 2}", "6.A", "06"),
])
def test_render_template_zero_pads_arabic_to_the_requested_width(template, name, expected):
    """The width argument pads but never truncates."""
    assert render_template(template, parse_class_name(name)) == expected


@pytest.mark.parametrize("template,message", [
    ("{arabic:x}", "not a valid width"),
    ("{arabic:2.5}", "not a valid width"),
    ("{arabic:0}", "must be between 1 and 10"),
    ("{arabic:11}", "must be between 1 and 10"),
    ("{arabic:-1}", "must be between 1 and 10"),
])
def test_render_template_rejects_a_bad_width_argument(template, message):
    """Non-numeric and out-of-bounds widths raise TemplateError."""
    with pytest.raises(TemplateError, match=message):
        render_template(template, parse_class_name("6.A"))


def test_render_template_rejects_an_unknown_placeholder_naming_it():
    """{bogus} is a well-formed token but not a known field."""
    with pytest.raises(TemplateError, match=r"unknown placeholder '\{bogus\}'"):
        render_template("{bogus}", parse_class_name("6.A"))


@pytest.mark.parametrize("template", ["{Arabic}", "{arabic name}", "{6}", "{}"])
def test_render_template_rejects_tokens_the_placeholder_syntax_does_not_accept(template):
    """Leftover braces are reported as an unsupported placeholder."""
    with pytest.raises(TemplateError, match="unsupported placeholder"):
        render_template(template, parse_class_name("6.A"))


@pytest.mark.parametrize("template", ["{arabic}", "{roman}", "{roman_lower}", "{number}"])
def test_render_template_refuses_number_placeholders_for_a_nameless_number(template):
    """A name without a numeral cannot fill a number placeholder."""
    with pytest.raises(TemplateError, match="no recognisable number"):
        render_template(template, parse_class_name("Window"))


def test_render_template_still_serves_text_placeholders_without_a_numeral():
    """Placeholders that need no number work on free-text names."""
    assert render_template("{original}!", parse_class_name("Window")) == "Window!"


@pytest.mark.parametrize("template", ["", "   ", None, "\t"])
def test_render_template_rejects_an_empty_template(template):
    """An empty or blank template is a TemplateError, not a silent no-op."""
    with pytest.raises(TemplateError, match="Template cannot be empty"):
        render_template(template, parse_class_name("6.A"))


def test_render_template_keeps_literal_text_around_placeholders():
    """Text outside placeholders is copied through verbatim."""
    assert render_template("Class {roman}-{letter_upper}!", parse_class_name("6.a")) == \
        "Class VI-A!"


def test_template_error_is_a_value_error():
    """TemplateError stays catchable as ValueError for legacy callers."""
    assert issubclass(TemplateError, ValueError)


# ---------------------------------------------------------------------------
# validate_template
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", [
    TEMPLATE_ROMAN, TEMPLATE_ARABIC, "{arabic:2}.{letter_upper}",
    "{roman_lower}-{letter_lower}", "{prefix}{number}{separator}{letter}{suffix}",
    "plain text", "{original}", "{arabic:10}",
])
def test_validate_template_accepts_usable_templates(template):
    """A usable template validates to None (no error message)."""
    assert validate_template(template) is None


@pytest.mark.parametrize("template,message", [
    ("", "Template cannot be empty."),
    ("   ", "Template cannot be empty."),
    (None, "Template cannot be empty."),
    ("{arabic", "Template has unbalanced { } brackets."),
    ("arabic}", "Template has unbalanced { } brackets."),
    ("{roman}{letter", "Template has unbalanced { } brackets."),
    ("{prefix}", "Template produces an empty class name."),
    ("{suffix}", "Template produces an empty class name."),
])
def test_validate_template_reports_the_exact_user_facing_message(template, message):
    """Unusable templates come back with a message ready to show the user."""
    assert validate_template(template) == message


@pytest.mark.parametrize("template,fragment", [
    ("{bogus}", "nknown placeholder"),
    ("{arabic:x}", "not a valid width"),
    ("{arabic:99}", "ust be between 1 and 10"),
])
def test_validate_template_forwards_render_errors(template, fragment):
    """Render-time problems are surfaced by validation without touching data."""
    result = validate_template(template)
    assert result is not None and fragment in result


@pytest.mark.bug
@pytest.mark.parametrize("template,typed", [
    ("{Arabic}", "{Arabic}"),
    ("{arabic:X}", "'X'"),
])
def test_validate_template_quotes_the_users_text_unchanged(template, typed):
    """The error must quote what the user wrote, not a case-mangled copy."""
    message = validate_template(template)
    assert message is not None
    assert typed in message


def test_predefined_templates_are_valid_and_documented():
    """Every shipped template validates and carries a label plus help text."""
    for key, (label, template, help_text) in PREDEFINED_TEMPLATES.items():
        assert validate_template(template) is None, key
        assert label.strip() and help_text.strip()


def test_every_documented_placeholder_can_actually_be_rendered():
    """TEMPLATE_PLACEHOLDERS must not advertise fields the renderer rejects."""
    parts = parse_class_name("6.A")
    for name in TEMPLATE_PLACEHOLDERS:
        assert render_template("[{%s}]" % name, parts).startswith("[")


# ---------------------------------------------------------------------------
# convert_class_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,template,expected", [
    ("6.A", TEMPLATE_ROMAN, "VI.A"),
    ("VI.A", TEMPLATE_ARABIC, "6.A"),
    ("9 b", TEMPLATE_ROMAN, "IX.B"),
    ("IX. b", TEMPLATE_ARABIC, "9.B"),
    ("ix.b", TEMPLATE_ROMAN, "IX.B"),
    ("IX.", TEMPLATE_ARABIC, "9."),
    ("06.A", TEMPLATE_ARABIC, "6.A"),
    ("IXA", TEMPLATE_ARABIC, "9.A"),
    ("6.A", "{arabic:2}.{letter_upper}", "06.A"),
    ("Blue class 6.A", "{prefix}{roman}.{letter_upper}{suffix}", "Blue class VI.A"),
])
def test_convert_class_name_renders_the_name_through_the_template(name, template, expected):
    """The happy path: recognised names are re-rendered by the template."""
    result = convert_class_name(name, template)
    assert result.result == expected
    assert result.recognised is True
    assert result.changed is (expected != name)


@pytest.mark.parametrize("template", [TEMPLATE_ROMAN, TEMPLATE_ARABIC])
def test_convert_class_name_is_idempotent(template):
    """Converting an already converted name changes nothing further."""
    first = convert_class_name("6.a", template)
    second = convert_class_name(first.result, template)
    assert second.result == first.result
    assert second.changed is False


def test_convert_class_name_strips_surrounding_whitespace_from_the_result():
    """A template with padding must not grow spaces onto the class name."""
    assert convert_class_name("6.A", "  {arabic}.{letter_upper}  ").result == "6.A"


@pytest.mark.parametrize("name", ["Window", "", None, "My sixth A"])
def test_convert_class_name_leaves_unrecognised_names_untouched(name):
    """Unrecognised names are reported, never silently dropped or renamed."""
    result = convert_class_name(name, TEMPLATE_ROMAN)
    assert result.recognised is False
    assert result.changed is False
    assert result.result == result.original
    assert "no recognisable number" in result.message


@pytest.mark.parametrize("template,fragment", [
    ("{bogus}", "unknown placeholder"),
    ("", "Template cannot be empty"),
    (None, "Template cannot be empty"),
    ("{Arabic}", "unsupported placeholder"),
])
def test_convert_class_name_never_raises_on_a_broken_template(template, fragment):
    """A broken template yields an explanatory result instead of an exception."""
    result = convert_class_name("6.A", template)
    assert result.result == "6.A"
    assert result.changed is False
    assert fragment in result.message


def test_convert_class_name_reports_a_template_that_renders_nothing():
    """A template collapsing to an empty string keeps the original name."""
    result = convert_class_name("6.A", "{prefix}")
    assert result.result == "6.A"
    assert result.changed is False
    assert result.message == "template produced an empty name"


@pytest.mark.bug
@pytest.mark.parametrize("name", ["0.A", "00.B", "Group 0"])
def test_convert_class_name_survives_a_zero_numeral(name):
    """A '0' numeral must give a ClassNameResult, not crash the preview."""
    result = convert_class_name(name, TEMPLATE_ROMAN)
    assert isinstance(result, ClassNameResult)
    assert result.result == name
    assert result.changed is False


@pytest.mark.bug
def test_render_template_reports_an_unrepresentable_number_as_template_error():
    """Numbers Roman notation cannot express must surface as TemplateError."""
    with pytest.raises(TemplateError):
        render_template("{roman}", parse_class_name("0.A"))


# ---------------------------------------------------------------------------
# shift_class_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,delta,expected", [
    ("6.A", 1, "7.A"),
    ("6.A", -5, "1.A"),
    ("6.A", 3, "9.A"),
    ("IX.B", 1, "X.B"),
    ("ix.b", 1, "x.b"),
    ("III. C", 2, "V. C"),
    ("Blue class 6.A", 1, "Blue class 7.A"),
    ("  6. A  ", 1, "  7. A  "),
    ("IX.", 1, "X."),
    ("IXA", 1, "XA"),
    ("6.A extra", 1, "7.A extra"),
])
def test_shift_class_name_moves_the_numeral_and_keeps_everything_else(name, delta, expected):
    """Prefix, separator, section letter and suffix survive the shift verbatim."""
    result = shift_class_name(name, delta, graduation_year=None)
    assert result.result == expected
    assert result.changed is True
    assert result.recognised is True
    assert result.removed is False


@pytest.mark.parametrize("name,delta,expected", [
    ("06.A", 1, "07.A"),
    ("006.A", 1, "007.A"),
    ("09.A", 1, "10.A"),
    ("099.A", 1, "100.A"),
    ("0006.A", 4, "0010.A"),
    ("6.A", 1, "7.A"),
    ("10.A", -5, "5.A"),
])
def test_shift_class_name_preserves_zero_padding(name, delta, expected):
    """Leading zeros are kept at their original width but never truncate."""
    assert shift_class_name(name, delta, graduation_year=None).result == expected


@pytest.mark.parametrize("name,expected", [
    ("VI.A", "VII.A"), ("vi.a", "vii.a"), ("Ix.b", "X.b"),
])
def test_shift_class_name_preserves_roman_letter_case(name, expected):
    """Lower-case Roman numerals stay lower case; mixed case becomes upper."""
    assert shift_class_name(name, 1, graduation_year=None).result == expected


@pytest.mark.parametrize("name", ["9.A", "IX.B", "ix.b", "09.C", "Blue class 9.A"])
def test_shift_class_name_marks_the_graduating_year_for_removal(name):
    """Whatever the notation, year 9 graduates when shifting upwards."""
    result = shift_class_name(name, 1)
    assert result.removed is True
    assert result.changed is False
    assert result.result == result.original
    assert result.message == "graduating year"
    assert result.skipped is False


def test_shift_class_name_does_not_graduate_on_a_downward_shift():
    """A negative delta never removes the graduating year."""
    result = shift_class_name("9.A", -1)
    assert result.removed is False
    assert result.result == "8.A"


@pytest.mark.parametrize("kwargs", [
    {"graduation_year": None},
    {"remove_graduating": False},
    {"graduation_year": 8},
])
def test_shift_class_name_shifts_year_nine_when_graduation_is_disabled(kwargs):
    """graduation_year=None, remove_graduating=False or another year keep 9."""
    result = shift_class_name("9.A", 1, **kwargs)
    assert result.removed is False
    assert result.result == "10.A"


def test_shift_class_name_can_graduate_a_different_year():
    """graduation_year is configurable, not hard-wired to 9."""
    assert shift_class_name("6.A", 1, graduation_year=6).removed is True


@pytest.mark.parametrize("name,delta,min_year,expected_year", [
    ("1.A", -1, 1, 0),
    ("6.A", -10, 1, -4),
    ("3.A", -1, 3, 2),
])
def test_shift_class_name_refuses_to_go_below_min_year(name, delta, min_year, expected_year):
    """A shift under the floor is skipped with an explanatory message."""
    result = shift_class_name(name, delta, min_year=min_year, graduation_year=None)
    assert result.changed is False
    assert result.skipped is True
    assert result.result == name
    assert result.message == (
        f"shift would produce year {expected_year} (minimum is {min_year})")


def test_shift_class_name_refuses_to_exceed_the_maximum_numeral():
    """MAX_NUMERAL_VALUE is the upper bound of a shift."""
    result = shift_class_name("3999.A", 1, graduation_year=None)
    assert result.changed is False
    assert result.result == "3999.A"
    assert result.message == (
        f"shift would produce year 4000 (maximum is {MAX_NUMERAL_VALUE})")


@pytest.mark.parametrize("name", ["Window", "", None, "My sixth A", "MIX"])
def test_shift_class_name_reports_names_without_a_number(name):
    """Names with no numeral are returned unchanged and flagged unrecognised."""
    result = shift_class_name(name, 1)
    assert result.recognised is False
    assert result.changed is False
    assert result.removed is False
    assert result.skipped is True
    assert result.message == "no number found in the class name"


def test_shift_class_name_with_zero_delta_changes_nothing():
    """delta=0 is a no-op that still reports the recognised numeral."""
    result = shift_class_name("6.A", 0)
    assert result.changed is False
    assert result.skipped is True
    assert result.result == "6.A"
    assert result.message == "6 → 6"


@pytest.mark.parametrize("name", ["6.A", "VI.A", "vi.b", "06.A", "Blue class 6.A", "IXA"])
def test_shift_up_then_down_restores_the_original_name(name):
    """Shifting +1 and then -1 is a round trip for every notation."""
    up = shift_class_name(name, 1, graduation_year=None)
    down = shift_class_name(up.result, -1, graduation_year=None)
    assert down.result == name


@pytest.mark.bug
@pytest.mark.parametrize("name,delta,min_year", [
    ("VI.A", -6, 0),
    ("VI.A", -10, -5),
])
def test_shift_class_name_stays_total_for_a_min_year_below_one(name, delta, min_year):
    """A caller supplied floor below 1 must not crash the Roman branch."""
    result = shift_class_name(name, delta, min_year=min_year, graduation_year=None)
    assert isinstance(result, ClassNameResult)
    assert result.result == name


# ---------------------------------------------------------------------------
# ClassNameResult
# ---------------------------------------------------------------------------

def test_preview_line_marks_every_outcome_distinctly():
    """Removed / unrecognised / unchanged / changed each get their own icon."""
    assert shift_class_name("6.A", 1).preview_line() == "✓ 6.A → 7.A"
    assert shift_class_name("9.A", 1).preview_line() == \
        "❌ 9.A → REMOVED (graduating year)"
    assert shift_class_name("Window", 1).preview_line() == \
        "⚠️ Window → unchanged (no number found in the class name)"
    assert shift_class_name("6.A", 0).preview_line() == "= 6.A → unchanged (6 → 6)"


def test_preview_line_falls_back_to_a_default_reason():
    """An unchanged result without a message still explains itself."""
    result = ClassNameResult(original="6.A", result="6.A")
    assert result.preview_line() == "= 6.A → unchanged (already in target format)"
    assert result.skipped is True


# ---------------------------------------------------------------------------
# whitespace helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("6.A", False), ("6. A", True), (" 6.A", True), ("6.A ", True),
    ("6\tA", True), ("6\nA", True), ("6\xa0A", True), ("", False),
    ("Window", False), ("Blue class 6.A", True),
    (None, False), (6, False), (["6.A"], False),
])
def test_has_inner_whitespace_detects_any_whitespace_character(name, expected):
    """Non-strings answer False; every whitespace kind counts as True."""
    assert has_inner_whitespace(name) is expected


@pytest.mark.parametrize("name,replacement,expected", [
    (" 6. A ", "", "6.A"),
    (" 6. A ", "-", "6.-A"),
    ("6 . A", "", "6.A"),
    ("  6   A  ", "", "6A"),
    ("6\t\nA", "", "6A"),
    ("6\xa0A", "", "6A"),
    ("6.A", "", "6.A"),
    ("", "", ""),
    ("   ", "", ""),
    ("Blue class 6.A", "_", "Blue_class_6.A"),
    (None, "", ""),
    (12, "", ""),
])
def test_replace_whitespace_trims_then_replaces(name, replacement, expected):
    """Outer whitespace is stripped first so no separators grow at the edges."""
    assert replace_whitespace(name, replacement) == expected


@pytest.mark.parametrize("name", [" 6. A ", "6\tA", "Window", "", "6.A"])
def test_replace_whitespace_is_idempotent(name):
    """Cleaning an already cleaned name changes nothing."""
    once = replace_whitespace(name)
    assert replace_whitespace(once) == once


def test_replace_whitespace_output_has_no_whitespace_left():
    """With the default replacement the result never reports inner whitespace."""
    for name in NASTY_NAMES:
        assert has_inner_whitespace(replace_whitespace(name)) is False


# ---------------------------------------------------------------------------
# style_signature / describe_signature
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,signature", [
    ("6.A", "arabic|dot|letter"),
    ("7.B", "arabic|dot|letter"),
    ("06.A", "arabic|dot|letter"),
    ("6. A", "arabic|dot+space|letter"),
    ("6 A", "arabic|space|letter"),
    ("9A", "arabic|none|letter"),
    ("6-A", "arabic|'-'|letter"),
    ("IX.B", "roman-upper|dot|letter"),
    ("ix.b", "roman-lower|dot|letter"),
    ("IX.", "roman-upper|dot|no-letter"),
    ("Blue class 6.A", "text+arabic|dot|letter"),
    ("Window", "free-text"),
    ("", "free-text"),
])
def test_style_signature_keys_names_by_their_naming_logic(name, signature):
    """Names following the same logic share a signature."""
    assert style_signature(parse_class_name(name)) == signature


@pytest.mark.parametrize("left,right,same", [
    ("6.A", "7.B", True),
    ("6.A", "12.C", True),
    ("6.A", "6. A", False),
    ("6.A", "VI.A", False),
    ("IX.A", "ix.a", False),
    ("6.A", "Blue class 6.A", False),
])
def test_style_signature_distinguishes_structurally_different_names(left, right, same):
    """Separator spacing, notation and case each change the signature."""
    equal = style_signature(parse_class_name(left)) == style_signature(parse_class_name(right))
    assert equal is same


@pytest.mark.parametrize("signature,expected", [
    ("free-text", "free text without a recognisable number"),
    ("arabic|dot|letter", "Arabic number, dot separator, with section letter"),
    ("roman-upper|none|no-letter",
     "upper-case Roman number, no separator, without section letter"),
    ("roman-lower|space|letter",
     "lower-case Roman number, space separator, with section letter"),
    ("text+arabic|dot+space|letter",
     "descriptive text around Arabic number, dot and space separator, "
     "with section letter"),
    ("arabic|'-'|letter", "Arabic number, '-' separator, with section letter"),
])
def test_describe_signature_translates_a_signature_into_english(signature, expected):
    """describe_signature is the user-facing rendering of a signature key."""
    assert describe_signature(signature) == expected


@pytest.mark.parametrize("signature", ["banana", "arabic|dot", "", "a|b|c|d"])
def test_describe_signature_passes_unknown_keys_through(signature):
    """A signature it cannot decode is returned unchanged, never crashing."""
    assert describe_signature(signature) == signature


@pytest.mark.parametrize("name", NASTY_NAMES)
def test_every_signature_can_be_described(name):
    """style_signature and describe_signature stay in step for any input."""
    described = describe_signature(style_signature(parse_class_name(name)))
    assert isinstance(described, str) and described


# ---------------------------------------------------------------------------
# analyze_class_names
# ---------------------------------------------------------------------------

def test_analyze_class_names_groups_flags_and_counts():
    """A mixed collection is split into signatures, unparsed, whitespace, ..."""
    names = ["6.A", "7.B", "6.A", "", "  ", "IX.C", "Window", None,
             "6. A", "Blue class 8.D"]
    analysis = analyze_class_names(names)

    assert analysis.names == ["6.A", "7.B", "6.A", "", "  ", "IX.C", "Window",
                             "", "6. A", "Blue class 8.D"]
    assert analysis.signatures["arabic|dot|letter"] == ["6.A", "7.B", "6.A"]
    assert analysis.signatures["roman-upper|dot|letter"] == ["IX.C"]
    assert analysis.unparsed == ["Window"]
    assert analysis.with_whitespace == ["6. A"]
    assert analysis.duplicates == ["6.A"]
    assert analysis.empty == ["", "  ", ""]
    assert analysis.is_consistent is False


def test_analyze_class_names_ignores_empty_names_when_grouping():
    """Empty names are collected separately and never given a signature."""
    analysis = analyze_class_names(["", "   ", None])
    assert analysis.empty == ["", "   ", ""]
    assert analysis.signatures == {}
    assert analysis.unparsed == []
    assert analysis.duplicates == []
    assert len(analysis.parsed) == 3


@pytest.mark.parametrize("names,duplicates", [
    (["6.A", "6.A"], ["6.A"]),
    (["6.A", "6.a"], []),
    (["6.A", "7.B", "6.A", "7.B"], ["6.A", "7.B"]),
    (["6.A", " 6.A"], []),
    ([], []),
])
def test_analyze_class_names_detects_exact_duplicates(names, duplicates):
    """Duplicate detection is exact - case and spacing make a different name."""
    assert analyze_class_names(names).duplicates == duplicates


@pytest.mark.parametrize("names,consistent", [
    ([], True),
    (["6.A"], True),
    (["6.A", "7.B", "12.C"], True),
    (["", "  "], True),
    (["6.A", "IX.B"], False),
    (["6.A", "6. A"], False),
    (["6.A", "Window"], False),
    (["Window"], False),
])
def test_is_consistent_requires_one_signature_and_no_unparsed_names(names, consistent):
    """Consistency means a single recognised style and nothing unparsed."""
    assert analyze_class_names(names).is_consistent is consistent


@pytest.mark.parametrize("names,style", [
    ([], NumeralStyle.NONE),
    (["Window", ""], NumeralStyle.NONE),
    (["6.A", "7.B"], NumeralStyle.ARABIC),
    (["IX.A", "VII.B", "6.C"], NumeralStyle.ROMAN),
    (["6.A", "IX.B"], NumeralStyle.ARABIC),
    (["6.A", "IX.B", "VII.C"], NumeralStyle.ROMAN),
    (["6.A", "Window"], NumeralStyle.ARABIC),
])
def test_dominant_style_follows_the_majority_notation(names, style):
    """Roman only wins with a strict majority; ties go to Arabic."""
    assert analyze_class_names(names).dominant_style is style


def test_analyze_class_names_coerces_non_strings_and_keeps_input_order():
    """Order is preserved and non-str entries are stringified like elsewhere."""
    analysis = analyze_class_names([9, "6.A", None, 7])
    assert analysis.names == ["9", "6.A", "", "7"]
    assert [p.numeral_value for p in analysis.parsed] == [9, 6, None, 7]


@pytest.mark.integration
def test_signature_groups_and_empty_names_account_for_every_input():
    """Each name lands in exactly one signature group or in `empty`."""
    names = NASTY_NAMES + ["6.A", "6.A"]
    analysis = analyze_class_names(names)
    grouped = sum(len(group) for group in analysis.signatures.values())
    assert grouped + len(analysis.empty) == len(names) == len(analysis.parsed)
    for signature, group in analysis.signatures.items():
        for name in group:
            assert style_signature(parse_class_name(name)) == signature


@pytest.mark.integration
def test_converting_a_mixed_collection_makes_it_consistent():
    """Applying one template to every recognised name removes style drift."""
    names = ["6.A", "IX.B", "ix. c", "9d", "06.E"]
    before = analyze_class_names(names)
    assert before.is_consistent is False

    converted = [convert_class_name(name, TEMPLATE_ROMAN).result for name in names]
    after = analyze_class_names(converted)
    assert converted == ["VI.A", "IX.B", "IX.C", "IX.D", "VI.E"]
    assert after.is_consistent is True
    assert after.dominant_style is NumeralStyle.ROMAN
    assert after.with_whitespace == []


@pytest.mark.integration
def test_analysis_flags_only_standard_names_as_containing_whitespace():
    """with_whitespace lists standard names only, not descriptive ones."""
    analysis = analyze_class_names(["6. A", "Blue class 6.A", "6.A", "Window"])
    assert analysis.with_whitespace == ["6. A"]


def test_class_name_parts_defaults_describe_an_unparsed_name():
    """A bare ClassNameParts reports no numeral, no letter and rebuilds empty."""
    parts = ClassNameParts(original="x")
    assert parts.has_numeral is False
    assert parts.has_letter is False
    assert parts.is_standard is False
    assert parts.rebuild() == ""
