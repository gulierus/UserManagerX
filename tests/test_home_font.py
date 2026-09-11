"""
Unit tests for :mod:`utils.home_directory_utils` and :mod:`utils.font_manager`.

``utils.home_directory_utils`` is pure Python: it turns a template such as
``\\\\server\\users\\{last_name}\\{first_name:1:1}`` into a concrete home
directory for one :class:`models.Person`.  Nothing in it can open a dialog, so
those tests need no stubs - only the shared ``make_person`` factory.

``utils.font_manager`` does touch Qt (``QFontDatabase``) and the filesystem: it
writes every custom font to a temporary file so ReportLab can register it.  The
``font_manager`` fixture below therefore

* resets the process-wide singleton so one test cannot see another's fonts, and
* redirects :mod:`tempfile` into ``tmp_path``, so the production code's
  ``NamedTemporaryFile`` calls stay inside the test sandbox and can be counted.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and are expected
to fail until the production defect they document is fixed.
"""

import tempfile
import types
from pathlib import Path

import pytest

from PyQt6.QtGui import QFontDatabase

import utils.font_manager as font_manager_module
from utils.font_manager import FontManager, get_font_manager, initialize_fonts
from utils.home_directory_utils import (
    HomeDirectoryPathGenerator,
    HomeDirectoryTemplateManager,
    NamePartSpec,
    PlaceholderError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = PROJECT_ROOT / "resources" / "fonts"

G = HomeDirectoryPathGenerator


# ---------------------------------------------------------------------------
# Local helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def student(make_person):
    """The canonical single-part person: Jan Novák, 6.A, novakj."""
    return make_person("Jan", "Novák", "6.A", ad_username="novakj")


@pytest.fixture
def manager():
    """A fresh template manager (it is a plain object, not a singleton)."""
    return HomeDirectoryTemplateManager()


@pytest.fixture
def font_manager(qapp, tmp_path, monkeypatch):
    """
    A pristine :class:`FontManager` whose temp font files land in ``tmp_path``.

    Both the class-level singleton and the module-level global are reset, so the
    manager starts with nothing but the three built-in ReportLab fonts.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(FontManager, "_instance", None)
    monkeypatch.setattr(font_manager_module, "_font_manager", None)
    instance = FontManager()
    yield instance
    instance.cleanup()


@pytest.fixture
def regular_ttf():
    """Absolute path of the shipped DejaVu Sans regular face."""
    path = FONT_DIR / "DejaVuSans.ttf"
    if not path.is_file():
        pytest.skip("DejaVu font resources are not present in this checkout")
    return str(path)


@pytest.fixture
def bold_ttf():
    """Absolute path of the shipped DejaVu Sans bold face (same Qt family)."""
    path = FONT_DIR / "DejaVuSans-Bold.ttf"
    if not path.is_file():
        pytest.skip("DejaVu font resources are not present in this checkout")
    return str(path)


def temp_font_files(tmp_path):
    """Every temp font file the production code left inside the sandbox."""
    return sorted(tmp_path.glob("font_*.ttf"))


# ===========================================================================
# NamePartSpec
# ===========================================================================

def test_name_part_spec_defaults_to_first_part_and_all_characters():
    """The bare spec means 'part 1, no truncation'."""
    spec = NamePartSpec()
    assert (spec.part_index, spec.char_count) == (1, None)


def test_name_part_spec_repr_names_part_and_chars():
    """The custom __repr__ survives the dataclass decorator."""
    assert repr(NamePartSpec(part_index=-1, char_count=3)) == \
        "NamePartSpec(part=-1, chars=3)"


def test_name_part_specs_with_equal_fields_compare_equal():
    """Two specs describing the same extraction are interchangeable."""
    assert NamePartSpec(2, 4) == NamePartSpec(part_index=2, char_count=4)
    assert NamePartSpec(2, 4) != NamePartSpec(2, 5)


# ===========================================================================
# _extract_name_part
# ===========================================================================

@pytest.mark.parametrize("name, part_index, char_count, expected", [
    ("Novák", 1, None, "Novák"),            # single part, whole name
    ("Novák", -1, None, "Novák"),           # last part == the only part
    ("Novák", 0, None, "Novák"),            # "all parts" of one part
    ("Nováková Svobodová", 1, None, "Nováková"),
    ("Nováková Svobodová", 2, None, "Svobodová"),
    ("Nováková Svobodová", -1, None, "Svobodová"),
    ("Nováková Svobodová", 0, None, "NovákováSvobodová"),  # joined, no space
    ("Jan Petr", 2, 2, "Pe"),
    ("Novák", 1, 1, "N"),
])
def test_extract_name_part_selects_part_then_truncates(name, part_index,
                                                       char_count, expected):
    """Part selection happens first, the character limit is applied to it."""
    spec = NamePartSpec(part_index=part_index, char_count=char_count)
    assert G._extract_name_part(name, spec) == expected


def test_extract_name_part_ignores_surrounding_and_repeated_whitespace():
    """A sloppily typed name still yields clean parts."""
    spec = NamePartSpec(part_index=2, char_count=None)
    assert G._extract_name_part("  Jan   Petr  ", spec) == "Petr"


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_extract_name_part_rejects_blank_names(name):
    """An empty or whitespace-only name is a placeholder error, not a crash."""
    with pytest.raises(PlaceholderError, match="empty"):
        G._extract_name_part(name, NamePartSpec())


def test_extract_name_part_rejects_part_index_past_the_end():
    """Asking for part 3 of a two-part name names the real limit."""
    with pytest.raises(PlaceholderError) as excinfo:
        G._extract_name_part("Jan Petr", NamePartSpec(part_index=3))
    assert "out of range" in str(excinfo.value)
    assert "2 parts" in str(excinfo.value)


@pytest.mark.parametrize("part_index", [-2, -10])
def test_extract_name_part_rejects_part_indexes_below_minus_one(part_index):
    """Only -1 counts from the end; anything further back is an error."""
    with pytest.raises(PlaceholderError, match="out of range"):
        G._extract_name_part("Jan Petr", NamePartSpec(part_index=part_index))


@pytest.mark.parametrize("char_count", [0, -1, -5])
def test_extract_name_part_rejects_non_positive_char_count(char_count):
    """Truncating to zero characters would silently produce an empty folder."""
    with pytest.raises(PlaceholderError, match="at least 1"):
        G._extract_name_part("Novák", NamePartSpec(char_count=char_count))


def test_extract_name_part_char_count_longer_than_name_returns_whole_name():
    """Asking for more characters than exist is not an error."""
    assert G._extract_name_part("Jan", NamePartSpec(1, 99)) == "Jan"


def test_extract_name_part_truncation_keeps_diacritics():
    """Truncation must not fold 'á' away - the accent is part of the name."""
    assert G._extract_name_part("Nováková", NamePartSpec(1, 4)) == "Nová"


def test_extract_name_part_truncation_counts_code_points_not_graphemes():
    """
    Decomposed (NFD) input loses its accent when truncated - the limit counts
    code points, so the combining acute falls outside the slice.
    """
    decomposed = "Nova\u0301kova\u0301"   # looks identical to "Nováková"
    assert G._extract_name_part(decomposed, NamePartSpec(1, 4)) == "Nova"
    assert G._extract_name_part(decomposed, NamePartSpec(1, 5)) == "Nova\u0301"
    # the precomposed spelling of the same name keeps its accent at 4 characters
    assert G._extract_name_part("Nováková", NamePartSpec(1, 4)) == "Nová"


def test_extract_name_part_with_non_integer_part_index_raises_type_error():
    """A hand-built spec with a string index fails loudly instead of silently."""
    with pytest.raises(TypeError):
        G._extract_name_part("Jan Petr", NamePartSpec(part_index="2"))


# ===========================================================================
# generate_path - every placeholder form
# ===========================================================================

@pytest.mark.parametrize("template, expected", [
    ("{first_name}", "Jan"),
    ("{last_name}", "Novák"),
    ("{last_name:1}", "Novák"),
    ("{last_name:-1}", "Novák"),
    ("{first_name:1}", "Jan"),
    ("{first_name:1:3}", "Jan"),
    ("{first_name:1:1}", "J"),
    ("{first_name:0}", "Jan"),
    ("{username}", "novakj"),
    ("{class_name}", "6.A"),
])
def test_generate_path_resolves_every_placeholder_form(student, template, expected):
    """Each documented placeholder spelling resolves to its documented value."""
    assert G.generate_path(template, student) == expected


def test_generate_path_builds_a_unc_path_from_several_placeholders(student):
    """The headline use case: a UNC share with surname and given name."""
    template = "\\\\server\\users\\{last_name}\\{first_name}"
    assert G.generate_path(template, student) == "\\\\server\\users\\Novák\\Jan"


def test_generate_path_builds_initials_from_two_truncated_placeholders(student):
    """Adjacent placeholders keep their order after the reverse-order rewrite."""
    assert G.generate_path("{last_name:1:1}{first_name:1:1}", student) == "NJ"


def test_generate_path_replaces_every_occurrence_of_a_repeated_placeholder(student):
    """The same placeholder used twice is expanded twice."""
    assert G.generate_path("{username}/{username}", student) == "novakj/novakj"


def test_generate_path_does_not_re_expand_a_substituted_value(student):
    """A name that itself looks like a placeholder is inserted literally."""
    student.first_name = "{last_name}"
    assert G.generate_path("{first_name}_{last_name}", student) == "{last_name}_Novák"


def test_generate_path_without_placeholders_returns_the_template_unchanged():
    """A constant template is legal - every user simply shares the folder."""
    assert G.generate_path("C:\\shared\\home", None) == "C:\\shared\\home"


@pytest.mark.parametrize("template", [
    "{Last_Name}",          # capitals are not part of the placeholder grammar
    "{ first_name }",       # padding spaces
    "{first_name:1:3:5}",   # a third segment is not supported
    "{first_name",          # unbalanced
    "{}",                   # no field name
])
def test_generate_path_leaves_malformed_braces_untouched(student, template):
    """Text that does not match the placeholder grammar is copied verbatim."""
    assert G.generate_path(template, student) == template


@pytest.mark.parametrize("template", ["", None])
def test_generate_path_rejects_an_empty_template(student, template):
    """An empty template can never produce a path."""
    with pytest.raises(PlaceholderError, match="Template cannot be empty"):
        G.generate_path(template, student)


def test_generate_path_rejects_an_unknown_placeholder(student):
    """An unknown field is reported with the offending placeholder text."""
    with pytest.raises(PlaceholderError) as excinfo:
        G.generate_path("H:\\{nickname}", student)
    assert "{nickname}" in str(excinfo.value)
    assert "nickname" in str(excinfo.value)


def test_generate_path_rejects_a_part_index_past_the_end_of_the_name(student):
    """'{first_name:2}' on a one-word given name is an error, not an empty string."""
    with pytest.raises(PlaceholderError) as excinfo:
        G.generate_path("{first_name:2}", student)
    assert "{first_name:2}" in str(excinfo.value)
    assert "out of range" in str(excinfo.value)


def test_generate_path_rejects_a_zero_character_count(student):
    """'{first_name:1:0}' would produce an empty folder name."""
    with pytest.raises(PlaceholderError, match="Character count"):
        G.generate_path("{first_name:1:0}", student)


@pytest.mark.parametrize("template, field", [
    ("{first_name}", "First name"),
    ("{last_name}", "Last name"),
    ("{username}", "Username"),
    ("{class_name}", "Class name"),
])
def test_generate_path_names_the_field_that_is_missing(make_person, template, field):
    """Every missing source field produces its own, identifiable message."""
    person = make_person("", "", "")
    with pytest.raises(PlaceholderError) as excinfo:
        G.generate_path(template, person)
    assert field in str(excinfo.value)


def test_generate_path_without_a_username_reports_the_username(make_person):
    """A person that was never given an AD account cannot get {username}."""
    person = make_person("Jan", "Novák", "6.A")           # ad_username is None
    with pytest.raises(PlaceholderError, match="Username is empty"):
        G.generate_path("\\\\srv\\home\\{username}", person)


def test_generate_path_with_an_empty_class_still_resolves_other_fields(make_person):
    """An unclassified person only breaks templates that ask for the class."""
    person = make_person("Jan", "Novák", "", ad_username="novakj")
    assert G.generate_path("H:\\{username}", person) == "H:\\novakj"
    with pytest.raises(PlaceholderError, match="Class name is empty"):
        G.generate_path("H:\\{class_name}\\{username}", person)


def test_generate_path_with_no_person_raises_placeholder_error():
    """A missing person is reported as a placeholder failure, not AttributeError."""
    with pytest.raises(PlaceholderError) as excinfo:
        G.generate_path("{first_name}", None)
    assert "{first_name}" in str(excinfo.value)


def test_generate_path_accepts_any_object_carrying_the_four_fields():
    """The generator only reads attributes, so a stand-in record works too."""
    record = types.SimpleNamespace(first_name="Ján", last_name="Kováč",
                                   class_name="IX.", ad_username="kovacj")
    assert G.generate_path("{class_name}\\{last_name}", record) == "IX.\\Kováč"


def test_generate_path_honours_custom_default_specs(make_person):
    """The bare placeholders follow the caller-supplied defaults."""
    person = make_person("Jan Petr", "Nováková Svobodová", "6.A")
    result = G.generate_path(
        "{first_name}-{last_name}", person,
        default_first_spec=NamePartSpec(part_index=2, char_count=2),
        default_last_spec=NamePartSpec(part_index=1, char_count=4),
    )
    assert result == "Pe-Nová"


def test_generate_path_explicit_part_index_overrides_the_default_one(make_person):
    """A part index written into the template wins over the default spec."""
    person = make_person("Jan Petr", "Novák", "6.A")
    result = G.generate_path(
        "{first_name:2}", person,
        default_first_spec=NamePartSpec(part_index=1, char_count=None),
    )
    assert result == "Petr"


def test_generate_path_falls_back_per_field_not_per_placeholder(make_person):
    """'{first_name:2}' overrides only the part - the default limit still bites."""
    person = make_person("Jan Petr", "Novák", "6.A")
    result = G.generate_path(
        "{first_name:2}", person,
        default_first_spec=NamePartSpec(part_index=1, char_count=1),
    )
    assert result == "P"


def test_generate_path_default_char_count_applies_when_template_omits_it(make_person):
    """A default char_count truncates even the bare '{first_name}' form."""
    person = make_person("Jan", "Novák", "6.A")
    result = G.generate_path("{first_name}", person,
                             default_first_spec=NamePartSpec(1, 1))
    assert result == "J"


def test_generate_path_is_idempotent_and_leaves_the_person_alone(student):
    """Generating a path twice gives the same answer and changes no field."""
    template = "\\\\srv\\{class_name}\\{last_name}_{first_name:1:1}"
    first = G.generate_path(template, student)
    second = G.generate_path(template, student)
    assert first == second == "\\\\srv\\6.A\\Novák_J"
    assert (student.first_name, student.last_name, student.class_name) == \
        ("Jan", "Novák", "6.A")
    assert student.home_directory is None
    assert not student.is_dirty()


def test_generate_path_works_when_called_on_an_instance(student):
    """The UI instantiates the generator, so the classmethods must bind."""
    generator = HomeDirectoryPathGenerator()
    assert generator.generate_path("{username}", student) == "novakj"


def test_generate_path_keeps_diacritics_in_the_result(make_person):
    """Accented names must reach the path verbatim - no ASCII folding here."""
    person = make_person("Žofie", "Čapková", "9.C", ad_username="capkovaz")
    assert G.generate_path("\\\\srv\\{last_name}\\{first_name}", person) == \
        "\\\\srv\\Čapková\\Žofie"


def test_generate_path_uses_the_last_part_for_a_compound_surname(make_person):
    """The explicit part selectors address a two-word surname unambiguously."""
    person = make_person("Jan", "Nováková Svobodová", "6.A")
    assert G.generate_path("{last_name:1}", person) == "Nováková"
    assert G.generate_path("{last_name:-1}", person) == "Svobodová"
    assert G.generate_path("{last_name:0}", person) == "NovákováSvobodová"


@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: bare {last_name} keeps only the last part of a "
                          "compound surname", strict=False)
def test_bare_last_name_placeholder_keeps_the_whole_surname(make_person):
    """'{last_name}' is documented as the *full* last name, not just one part."""
    person = make_person("Jan", "Nováková Svobodová", "6.A")
    assert G.generate_path("{last_name}", person) == "Nováková Svobodová"


# ===========================================================================
# validate_template
# ===========================================================================

@pytest.mark.parametrize("template", [
    "{first_name}",
    "{last_name:-1}",
    "{last_name:0}",
    "{first_name:1:3}",
    "\\\\server\\users\\{last_name}\\{first_name:1:1}",
    "H:\\{username}",
    "{class_name}{username}",
])
def test_validate_template_accepts_well_formed_templates(template):
    """Anything the generator can expand must validate without complaints."""
    assert G.validate_template(template) == []


@pytest.mark.parametrize("template", ["", None])
def test_validate_template_reports_an_empty_template_once(template):
    """An empty template gets exactly one issue and stops there."""
    assert G.validate_template(template) == ["Template is empty"]


def test_validate_template_reports_whitespace_only_template():
    """Whitespace is not a path - and it has no placeholders either."""
    issues = G.validate_template("   ")
    assert "Template contains only whitespace" in issues
    assert "Template contains no placeholders" in issues


def test_validate_template_flags_a_constant_template():
    """A template without placeholders would give every student one folder."""
    assert G.validate_template("C:\\home\\students") == \
        ["Template contains no placeholders"]


def test_validate_template_names_the_unknown_placeholder():
    """The issue text quotes the field so the user can find the typo."""
    assert G.validate_template("{nickname}") == ["Unknown placeholder: {nickname}"]


def test_validate_template_does_not_validate_parts_of_unknown_placeholders():
    """One typo produces one issue, not a cascade."""
    assert len(G.validate_template("{nickname:-7:0}")) == 1


@pytest.mark.parametrize("template", ["{last_name:-2}", "{first_name:-99}"])
def test_validate_template_rejects_part_indexes_below_minus_one(template):
    """Only -1, 0 and positive indexes are meaningful."""
    issues = G.validate_template(template)
    assert len(issues) == 1
    assert "part index" in issues[0].lower()


def test_validate_template_rejects_a_zero_character_count():
    """A zero-character limit is reported before anyone generates a path."""
    issues = G.validate_template("{first_name:1:0}")
    assert len(issues) == 1
    assert "character count" in issues[0].lower()


def test_validate_template_reports_every_problem_in_reading_order():
    """All issues come back at once, ordered like the template reads."""
    issues = G.validate_template("{nickname}\\{last_name:-3}\\{first_name:1:0}")
    assert len(issues) == 3
    assert "nickname" in issues[0]
    assert "{last_name:-3}" in issues[1]
    assert "{first_name:1:0}" in issues[2]


def test_validate_template_ignores_text_that_is_not_a_placeholder():
    """Braces that do not match the grammar count as literal text."""
    assert G.validate_template("{Last_Name} {username}") == []


def test_validate_template_cannot_know_whether_a_part_index_exists():
    """Part 5 is structurally valid; only generation can find it out of range."""
    assert G.validate_template("{last_name:5}") == []


# ===========================================================================
# get_available_placeholders / get_example_templates
# ===========================================================================

def test_available_placeholders_lists_the_four_supported_fields():
    """Exactly the fields generate_path knows how to resolve."""
    assert set(G.get_available_placeholders()) == \
        {"first_name", "last_name", "username", "class_name"}


def test_available_placeholders_returns_a_defensive_copy():
    """A caller mutating the dict must not poison the class-level table."""
    placeholders = G.get_available_placeholders()
    placeholders["hacked"] = "nope"
    placeholders.pop("username")
    assert "hacked" not in G.get_available_placeholders()
    assert "username" in G.get_available_placeholders()
    assert "hacked" not in G.AVAILABLE_PLACEHOLDERS


def test_every_placeholder_has_a_non_empty_description():
    """The UI shows these strings next to the placeholder."""
    assert all(bool(text.strip())
               for text in G.get_available_placeholders().values())


def test_example_templates_have_a_template_and_a_description():
    """Both keys are consumed by the template combo box."""
    examples = G.get_example_templates()
    assert len(examples) >= 5
    for example in examples:
        assert set(example) == {"template", "description"}
        assert example["template"].strip()
        assert example["description"].strip()


def test_every_example_template_validates_and_generates(student):
    """The shipped examples must not be the first thing a user trips over."""
    for example in G.get_example_templates():
        template = example["template"]
        assert G.validate_template(template) == [], template
        path = G.generate_path(template, student)
        assert "{" not in path, template


def test_example_templates_are_not_shared_between_calls():
    """Mutating one caller's list must not corrupt the next caller's."""
    first = G.get_example_templates()
    first.append({"template": "{username}", "description": "injected"})
    first[0]["template"] = "tampered"
    second = G.get_example_templates()
    assert len(second) == len(first) - 1
    assert second[0]["template"] != "tampered"


# ===========================================================================
# HomeDirectoryTemplateManager
# ===========================================================================

def test_new_manager_is_preloaded_with_the_example_templates(manager):
    """Defaults are the examples, numbered 'Template 1' upwards."""
    examples = G.get_example_templates()
    templates = manager.get_all_templates()
    assert len(templates) == len(examples)
    assert list(templates) == [f"Template {i}" for i in range(1, len(examples) + 1)]
    assert templates["Template 1"] == examples[0]["template"]


def test_add_template_stores_a_valid_template(manager):
    """A new, valid template is accepted and immediately retrievable."""
    assert manager.add_template("Initials", "H:\\{last_name:1:1}{first_name:1:1}")
    assert manager.get_template("Initials") == "H:\\{last_name:1:1}{first_name:1:1}"
    assert manager.template_exists("Initials")


def test_add_template_refuses_a_duplicate_name_and_keeps_the_original(manager):
    """The second add must not silently overwrite the first template."""
    manager.add_template("Mine", "H:\\{username}")
    assert manager.add_template("Mine", "X:\\{class_name}") is False
    assert manager.get_template("Mine") == "H:\\{username}"


def test_add_template_refuses_to_overwrite_a_default(manager):
    """'Template 1' already exists, so adding it again fails."""
    original = manager.get_template("Template 1")
    assert manager.add_template("Template 1", "H:\\{username}") is False
    assert manager.get_template("Template 1") == original


@pytest.mark.parametrize("template", [
    "",                       # empty
    None,                     # missing
    "   ",                    # whitespace only
    "C:\\fixed\\folder",      # no placeholders
    "{nickname}",             # unknown placeholder
    "{first_name:1:0}",       # zero character count
    "{last_name:-4}",         # impossible part index
])
def test_add_template_refuses_invalid_templates(manager, template):
    """A template that cannot generate a path never enters the catalogue."""
    before = manager.get_all_templates()
    assert manager.add_template("Broken", template) is False
    assert manager.template_exists("Broken") is False
    assert manager.get_all_templates() == before


def test_add_template_performs_no_validation_on_the_name(manager):
    """
    Documented behaviour: only the *template* is validated, the name is not -
    a blank name is accepted today and shows up as a nameless combo entry.
    """
    assert manager.add_template("", "H:\\{username}") is True
    assert manager.get_template("") == "H:\\{username}"
    assert manager.template_exists("") is True


def test_remove_template_deletes_it_once_and_then_reports_false(manager):
    """Removal is idempotent: the second call simply finds nothing."""
    assert manager.remove_template("Template 2") is True
    assert manager.template_exists("Template 2") is False
    assert manager.remove_template("Template 2") is False
    assert manager.get_template("Template 2") is None


def test_remove_template_ignores_an_unknown_name(manager):
    """Removing something that was never there leaves the catalogue intact."""
    before = manager.get_all_templates()
    assert manager.remove_template("No Such Template") is False
    assert manager.get_all_templates() == before


def test_removed_name_can_be_added_again(manager):
    """The duplicate check follows the current content, not history."""
    manager.remove_template("Template 1")
    assert manager.add_template("Template 1", "H:\\{username}") is True
    assert manager.get_template("Template 1") == "H:\\{username}"


def test_get_template_returns_none_for_an_unknown_name(manager):
    """Lookup misses are None, not KeyError."""
    assert manager.get_template("Nope") is None
    assert manager.get_template("") is None


def test_template_lookup_is_case_and_space_sensitive(manager):
    """Names are exact dictionary keys."""
    assert manager.template_exists("Template 1") is True
    assert manager.template_exists("template 1") is False
    assert manager.template_exists(" Template 1") is False


def test_get_all_templates_returns_a_copy(manager):
    """The UI iterates this dict; mutating it must not touch the manager."""
    snapshot = manager.get_all_templates()
    snapshot["Template 1"] = "tampered"
    snapshot["Injected"] = "{username}"
    del snapshot["Template 2"]
    assert manager.get_template("Template 1") != "tampered"
    assert manager.template_exists("Injected") is False
    assert manager.template_exists("Template 2") is True


def test_two_managers_do_not_share_state(manager):
    """Every dialog builds its own manager; they must not leak into each other."""
    other = HomeDirectoryTemplateManager()
    manager.add_template("Mine", "H:\\{username}")
    other.remove_template("Template 1")
    assert other.template_exists("Mine") is False
    assert manager.template_exists("Template 1") is True


def test_every_stored_template_can_generate_a_path(manager, student):
    """Whatever survives add_template is usable by the Generate button."""
    manager.add_template("Initials", "H:\\{last_name:1:1}{first_name:1:1}")
    for name, template in manager.get_all_templates().items():
        assert G.generate_path(template, student), name


# ===========================================================================
# FontManager - identity and built-in fonts
# ===========================================================================

def test_font_manager_is_a_singleton(font_manager):
    """Every construction and the module helper return one shared object."""
    assert FontManager() is font_manager
    assert get_font_manager() is font_manager
    assert get_font_manager() is get_font_manager()


def test_repeated_construction_does_not_duplicate_the_builtin_fonts(font_manager):
    """__init__ must be a no-op on the existing singleton."""
    FontManager()
    FontManager()
    families = font_manager.get_available_families()
    assert families == sorted(set(families))


def test_fresh_manager_offers_the_three_builtin_families(font_manager):
    """Without resources the PDF export still has Helvetica, Times and Courier."""
    assert font_manager.get_available_families() == ["Courier", "Helvetica", "Times"]


def test_get_available_families_is_sorted_and_detached(font_manager):
    """The combo box gets a sorted copy it may mutate freely."""
    families = font_manager.get_available_families()
    families.append("Injected")
    families.sort(reverse=True)
    assert "Injected" not in font_manager.get_available_families()
    assert font_manager.get_available_families() == ["Courier", "Helvetica", "Times"]


@pytest.mark.parametrize("qt_family, expected", [
    ("Helvetica", "Helvetica"),
    ("Times", "Times-Roman"),
    ("Courier", "Courier"),
    ("Comic Sans MS", "Helvetica"),   # unknown -> documented fallback
    ("", "Helvetica"),
    (None, "Helvetica"),
    ("helvetica", "Helvetica"),       # lookup is case sensitive
])
def test_get_reportlab_font_name_maps_or_falls_back(font_manager, qt_family, expected):
    """Unknown Qt families fall back to Helvetica instead of breaking the PDF."""
    assert font_manager.get_reportlab_font_name(qt_family) == expected


@pytest.mark.parametrize("family, available", [
    ("Helvetica", True),
    ("Times", True),
    ("DejaVu Sans", False),
    ("", False),
    (None, False),
])
def test_is_font_available_only_knows_registered_families(font_manager, family, available):
    """Availability is about *this* manager, not about the whole system."""
    assert font_manager.is_font_available(family) is available


def test_get_font_info_describes_a_builtin_font(font_manager):
    """A built-in font is available, mapped, not custom - and has styles."""
    info = font_manager.get_font_info("Times")
    assert info["family"] == "Times"
    assert info["available"] is True
    assert info["reportlab_name"] == "Times-Roman"
    assert info["is_custom"] is False
    assert info["is_builtin"] is True
    assert isinstance(info["styles"], list)


def test_get_font_info_for_an_unknown_family_has_no_styles_key(font_manager):
    """Callers must use .get('styles') - the key is absent when unavailable."""
    info = font_manager.get_font_info("Nonexistent Face")
    assert info["available"] is False
    assert info["reportlab_name"] is None
    assert info["is_builtin"] is False
    assert "styles" not in info


def test_get_font_info_rejects_a_non_string_family(font_manager):
    """None is not a font name; the attribute error is not swallowed."""
    with pytest.raises(AttributeError):
        font_manager.get_font_info(None)


def test_system_fonts_are_the_registered_families_qt_also_knows(font_manager):
    """get_system_fonts() never invents families the manager has not registered."""
    system = font_manager.get_system_fonts()
    assert system == sorted(system)
    assert set(system) <= set(font_manager.get_available_families())
    assert not any(name.startswith("DejaVu") for name in system)
    qt_families = set(QFontDatabase.families())
    assert set(system) <= qt_families


def test_fresh_manager_reports_no_custom_fonts(font_manager):
    """Nothing is custom until a font file is actually loaded."""
    assert font_manager.get_custom_fonts() == []


# ===========================================================================
# FontManager - loading fonts without compiled-in resources
# ===========================================================================

def test_loading_from_a_missing_resource_prefix_changes_nothing(font_manager, tmp_path):
    """A .qrc bundle that was never compiled in must not break startup."""
    before = font_manager.get_available_families()
    font_manager.load_custom_fonts_from_resources(":/no_such_prefix")
    assert font_manager.get_available_families() == before
    assert font_manager.get_custom_fonts() == []
    assert font_manager.reportlab_font_files == {}
    assert temp_font_files(tmp_path) == []


def test_loading_from_the_default_prefix_never_raises(font_manager):
    """Whether or not the resources are compiled in, the built-ins survive."""
    font_manager.load_custom_fonts_from_resources()
    families = font_manager.get_available_families()
    assert {"Helvetica", "Times", "Courier"} <= set(families)
    assert all(name.startswith("DejaVu") for name in font_manager.get_custom_fonts())


@pytest.mark.parametrize("resource_path", [
    ":/fonts/NoSuchFont.ttf",
    ":/",
    "",
    "/definitely/not/here/Font.ttf",
])
def test_load_font_from_resource_returns_false_for_unreadable_paths(font_manager,
                                                                    resource_path):
    """Every unreadable source is a False, never an exception."""
    assert font_manager._load_font_from_resource(resource_path) is False
    assert font_manager.reportlab_font_files == {}


def test_load_font_from_resource_rejects_an_empty_file(font_manager, tmp_path):
    """A zero-byte font file is detected before Qt is asked to parse it."""
    empty = tmp_path / "empty.ttf"
    empty.write_bytes(b"")
    assert font_manager._load_font_from_resource(str(empty)) is False
    assert font_manager.get_custom_fonts() == []


def test_load_font_from_resource_rejects_a_file_that_is_not_a_font(font_manager,
                                                                   tmp_path):
    """Garbage bytes are refused by Qt and reported as a failed load."""
    junk = tmp_path / "junk.ttf"
    junk.write_bytes(b"this is not a font" * 32)
    assert font_manager._load_font_from_resource(str(junk)) is False
    assert font_manager.get_available_families() == ["Courier", "Helvetica", "Times"]
    assert temp_font_files(tmp_path) == []


def test_load_font_from_resource_rejects_a_directory(font_manager, tmp_path):
    """A directory exists but cannot be opened as a font."""
    assert font_manager._load_font_from_resource(str(tmp_path)) is False


def test_load_font_from_resource_returns_false_when_qt_reports_no_family(
        font_manager, regular_ttf):
    """A font Qt accepts but cannot name is not registered anywhere."""
    font_manager.font_database = types.SimpleNamespace(
        addApplicationFontFromData=lambda data: 7,
        applicationFontFamilies=lambda font_id: [],
    )
    assert font_manager._load_font_from_resource(regular_ttf) is False
    assert font_manager.reportlab_font_files == {}


def test_load_font_from_resource_survives_a_failing_font_database(font_manager,
                                                                  regular_ttf):
    """An exception from Qt is logged and turned into a False."""
    def boom(data):
        raise RuntimeError("font database exploded")

    font_manager.font_database = types.SimpleNamespace(
        addApplicationFontFromData=boom,
        applicationFontFamilies=lambda font_id: ["Never"],
    )
    assert font_manager._load_font_from_resource(regular_ttf) is False
    assert font_manager.is_font_available("Never") is False


def test_register_font_with_reportlab_cleans_up_after_invalid_data(font_manager,
                                                                   tmp_path):
    """A rejected font leaves neither a mapping nor a temp file behind."""
    font_manager._register_font_with_reportlab("Bogus Family", b"not a font", "x")
    assert "Bogus Family" not in font_manager.qt_to_reportlab_map
    assert font_manager.reportlab_font_files == {}
    assert temp_font_files(tmp_path) == []


# ===========================================================================
# FontManager - loading a real font file (Qt + ReportLab together)
# ===========================================================================

@pytest.mark.integration
def test_loading_a_real_font_registers_it_with_qt_and_reportlab(font_manager,
                                                                regular_ttf,
                                                                tmp_path):
    """The happy path: the family becomes selectable and PDF-usable."""
    assert font_manager._load_font_from_resource(regular_ttf) is True

    assert font_manager.is_font_available("DejaVu Sans")
    assert "DejaVu Sans" in font_manager.get_available_families()
    assert font_manager.get_custom_fonts() == ["DejaVu Sans"]
    assert font_manager.get_reportlab_font_name("DejaVu Sans") == "DejaVuSans"

    spooled = temp_font_files(tmp_path)
    assert len(spooled) == 1
    assert spooled[0].read_bytes() == Path(regular_ttf).read_bytes()


@pytest.mark.integration
def test_loading_the_same_font_twice_does_not_duplicate_the_family(font_manager,
                                                                   regular_ttf):
    """Re-running the loader keeps the family list free of duplicates."""
    font_manager._load_font_from_resource(regular_ttf)
    font_manager._load_font_from_resource(regular_ttf)
    families = font_manager.get_available_families()
    assert families.count("DejaVu Sans") == 1


@pytest.mark.integration
def test_reportlab_name_strips_spaces_from_the_family(font_manager, regular_ttf):
    """ReportLab font names must not contain spaces."""
    font_manager._load_font_from_resource(regular_ttf)
    rl_name = font_manager.get_reportlab_font_name("DejaVu Sans")
    assert " " not in rl_name
    assert rl_name in font_manager.reportlab_font_files


@pytest.mark.integration
def test_cleanup_deletes_the_temp_font_file(font_manager, regular_ttf, tmp_path):
    """The spooled copy is removed when the application closes."""
    font_manager._load_font_from_resource(regular_ttf)
    assert temp_font_files(tmp_path)
    font_manager.cleanup()
    assert temp_font_files(tmp_path) == []


def test_cleanup_before_any_load_is_a_no_op(font_manager, tmp_path):
    """Closing an application that never loaded a font must not raise."""
    font_manager.cleanup()
    assert font_manager.reportlab_font_files == {}
    assert temp_font_files(tmp_path) == []


@pytest.mark.integration
def test_cleanup_is_safe_to_call_twice(font_manager, regular_ttf, tmp_path):
    """A second cleanup finds the files already gone and stays quiet."""
    font_manager._load_font_from_resource(regular_ttf)
    font_manager.cleanup()
    font_manager.cleanup()
    assert temp_font_files(tmp_path) == []
    assert font_manager.get_available_families().count("DejaVu Sans") == 1


@pytest.mark.integration
def test_cleanup_survives_a_temp_file_someone_else_deleted(font_manager,
                                                           regular_ttf, tmp_path):
    """External deletion of the spool file is not an error at shutdown."""
    font_manager._load_font_from_resource(regular_ttf)
    for spooled in temp_font_files(tmp_path):
        spooled.unlink()
    font_manager.cleanup()
    assert temp_font_files(tmp_path) == []


@pytest.mark.bug
@pytest.mark.integration
@pytest.mark.xfail(reason="BUG: a second face of the same family overwrites the "
                          "first ReportLab registration", strict=False)
def test_every_loaded_face_gets_its_own_reportlab_registration(font_manager,
                                                               regular_ttf,
                                                               bold_ttf):
    """
    DejaVuSans.ttf and DejaVuSans-Bold.ttf share the Qt family 'DejaVu Sans',
    so both are registered under the ReportLab name 'DejaVuSans' and the
    regular face is lost.
    """
    font_manager._load_font_from_resource(regular_ttf)
    regular_spool = font_manager.reportlab_font_files["DejaVuSans"]

    font_manager._load_font_from_resource(bold_ttf)

    assert len(font_manager.reportlab_font_files) == 2
    assert font_manager.reportlab_font_files["DejaVuSans"] == regular_spool


@pytest.mark.bug
@pytest.mark.integration
@pytest.mark.xfail(reason="BUG: cleanup() leaks the temp file orphaned by the "
                          "second face of a family", strict=False)
def test_cleanup_removes_every_temp_file_the_manager_created(font_manager,
                                                             regular_ttf,
                                                             bold_ttf,
                                                             tmp_path):
    """Two loaded faces spool two files; cleanup must delete both of them."""
    font_manager._load_font_from_resource(regular_ttf)
    font_manager._load_font_from_resource(bold_ttf)
    assert len(temp_font_files(tmp_path)) == 2

    font_manager.cleanup()

    assert temp_font_files(tmp_path) == []


@pytest.mark.integration
def test_cleanup_leaves_the_registration_table_in_place(font_manager, regular_ttf):
    """
    Documented behaviour: cleanup() deletes the files but keeps the mapping, so
    the manager still advertises a font whose spool file is gone.
    """
    font_manager._load_font_from_resource(regular_ttf)
    font_manager.cleanup()
    assert font_manager.reportlab_font_files                      # not cleared
    assert font_manager.is_font_available("DejaVu Sans")
    assert font_manager.get_reportlab_font_name("DejaVu Sans") == "DejaVuSans"


# ===========================================================================
# initialize_fonts
# ===========================================================================

def test_initialize_fonts_loads_resources_only_when_asked(font_manager, monkeypatch):
    """The resource-less startup path must not touch the resource loader."""
    calls = []
    monkeypatch.setattr(
        FontManager, "load_custom_fonts_from_resources",
        lambda self, resource_prefix=":/fonts": calls.append(resource_prefix),
    )

    initialize_fonts(load_custom_fonts=False)
    assert calls == []

    initialize_fonts(load_custom_fonts=True)
    assert calls == [":/fonts"]


def test_initialize_fonts_uses_the_shared_manager(font_manager, monkeypatch):
    """initialize_fonts must configure the very instance the UI later reads."""
    monkeypatch.setattr(
        FontManager, "load_custom_fonts_from_resources",
        lambda self, resource_prefix=":/fonts": None,
    )
    initialize_fonts(load_custom_fonts=True)
    assert get_font_manager() is font_manager
    assert font_manager.get_available_families() == ["Courier", "Helvetica", "Times"]
