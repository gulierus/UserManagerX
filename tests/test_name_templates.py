"""
Tests for name templates with field extraction (Version 26, point 21).

Two names are built from templates: the class name in step 2 of the
Microsoft 365 import, and the group name during synchronisation.  Both need to
pull *parts* of a field out, not only whole fields.
"""

import pytest

from utils.name_templates import (
    CLASS_FIELDS,
    GROUP_FIELDS,
    OPERATIONS,
    TemplateError,
    class_values,
    describe_operations,
    group_values,
    preview,
    render,
    validate,
)

VALUES = {
    'display_name': "Trida-6.A-2020",
    'mail_nickname': "trida6a",
    'description': "Sixth A class",
    'mail': "",
}


class TestWholeFields:
    """The simple case."""

    def test_a_field_is_substituted(self):
        assert render("{display_name}", VALUES) == "Trida-6.A-2020"

    def test_text_around_the_placeholder_is_kept(self):
        assert render("Class [{mail_nickname}]", VALUES) == "Class [trida6a]"

    def test_several_placeholders_are_all_substituted(self):
        assert render("{mail_nickname}/{display_name}", VALUES) == \
            "trida6a/Trida-6.A-2020"

    def test_an_empty_field_renders_as_nothing(self):
        assert render("[{mail}]", VALUES) == "[]"

    def test_a_template_without_placeholders_is_returned_as_is(self):
        assert render("Fixed name", VALUES) == "Fixed name"

    def test_surrounding_whitespace_is_removed(self):
        assert render("  {mail_nickname}  ", VALUES) == "trida6a"

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(TemplateError, match="Unknown placeholder"):
            render("{nope}", VALUES)

    def test_an_unknown_field_can_be_left_alone_for_a_preview(self):
        assert render("{nope}", VALUES, strict=False) == "{nope}"

    def test_an_unmatched_brace_is_refused(self):
        with pytest.raises(TemplateError, match="unmatched"):
            render("{display_name", VALUES)


class TestSlices:
    """Pulling a fixed part out of a field."""

    def test_a_range_is_cut_out(self):
        assert render("{display_name[6:9]}", VALUES) == "6.A"

    def test_an_open_end_runs_to_the_end(self):
        assert render("{mail_nickname[5:]}", VALUES) == "6a"

    def test_an_open_start_begins_at_the_beginning(self):
        assert render("{mail_nickname[:5]}", VALUES) == "trida"

    def test_a_negative_index_counts_from_the_end(self):
        assert render("{display_name[-4:]}", VALUES) == "2020"

    def test_a_single_index_gives_one_character(self):
        assert render("{mail_nickname[0]}", VALUES) == "t"

    def test_an_index_past_the_end_gives_nothing(self):
        # A short name must not make the whole template explode.
        assert render("{mail_nickname[99]}", VALUES) == ""

    def test_a_range_past_the_end_gives_what_there_is(self):
        assert render("{mail_nickname[5:99]}", VALUES) == "6a"

    def test_a_malformed_slice_is_refused(self):
        with pytest.raises(TemplateError, match="valid slice"):
            render("{display_name[a:b]}", VALUES)

    def test_an_unclosed_slice_is_refused(self):
        with pytest.raises(TemplateError):
            render("{display_name[6:}", VALUES)

    def test_an_empty_bracket_is_refused(self):
        with pytest.raises(TemplateError, match="needs an index"):
            render("{display_name[]}", VALUES)


class TestOperations:
    """The `|` chain."""

    def test_after_takes_what_follows_the_text(self):
        assert render("{display_name|after:Trida-}", VALUES) == "6.A-2020"

    def test_before_takes_what_precedes_the_text(self):
        assert render("{display_name|before:-2020}", VALUES) == "Trida-6.A"

    def test_after_gives_nothing_when_the_text_is_absent(self):
        assert render("{display_name|after:Zaci-}", VALUES) == ""

    def test_before_gives_nothing_when_the_text_is_absent(self):
        assert render("{display_name|before:Zaci}", VALUES) == ""

    def test_word_counts_from_one(self):
        assert render("{description|word:1}", VALUES) == "Sixth"

    def test_word_accepts_a_negative_index_for_the_last(self):
        assert render("{description|word:-1}", VALUES) == "class"

    def test_word_past_the_end_gives_nothing(self):
        assert render("{description|word:9}", VALUES) == ""

    def test_word_zero_is_refused(self):
        with pytest.raises(TemplateError, match="counted from 1"):
            render("{description|word:0}", VALUES)

    def test_word_without_a_number_is_refused(self):
        with pytest.raises(TemplateError, match="needs a number"):
            render("{description|word:x}", VALUES)

    def test_match_returns_the_first_match(self):
        assert render(r"{display_name|match:\d+\.[A-Z]}", VALUES) == "6.A"

    def test_match_prefers_the_first_capture_group(self):
        assert render(r"{display_name|match:Trida-(\d+)}", VALUES) == "6"

    def test_match_without_a_match_gives_nothing(self):
        assert render(r"{display_name|match:ZZZ}", VALUES) == ""

    def test_an_invalid_regular_expression_is_refused_readably(self):
        with pytest.raises(TemplateError, match="not a valid regular"):
            render("{display_name|match:[}", VALUES)

    def test_digits_keeps_only_the_numbers(self):
        assert render("{display_name|digits}", VALUES) == "62020"

    @pytest.mark.parametrize("operation,expected", [
        ("upper", "TRIDA6A"),
        ("lower", "trida6a"),
        ("title", "Trida6A"),
    ])
    def test_the_case_operations(self, operation, expected):
        assert render(f"{{mail_nickname|{operation}}}", VALUES) == expected

    def test_operations_chain_left_to_right(self):
        assert render("{display_name|after:Trida-|before:-2020|lower}",
                      VALUES) == "6.a"

    def test_a_slice_may_precede_the_chain(self):
        assert render("{display_name[6:9]|lower}", VALUES) == "6.a"

    def test_an_unknown_operation_lists_the_known_ones(self):
        with pytest.raises(TemplateError, match="Unknown operation"):
            render("{display_name|bogus}", VALUES)

    def test_an_operation_missing_its_argument_is_refused(self):
        with pytest.raises(TemplateError, match="needs a value"):
            render("{display_name|after}", VALUES)

    def test_text_that_is_neither_a_slice_nor_a_chain_is_refused(self):
        with pytest.raises(TemplateError, match="not understood"):
            render("{display_name qqq}", VALUES)

    def test_every_operation_is_described(self):
        for name, entry in OPERATIONS.items():
            assert entry[2], f"{name} has no help text"
        assert all(name in describe_operations() for name in OPERATIONS)


class TestValidate:
    """What the dialog checks before it lets the user press OK."""

    def test_a_good_template_has_no_problems(self):
        assert validate("{display_name}", GROUP_FIELDS) == []

    def test_an_empty_template_is_a_problem(self):
        assert validate("   ", GROUP_FIELDS) == ["The template is empty."]

    def test_a_template_without_a_placeholder_is_a_problem(self):
        # Every class would end up with the same name.
        problems = validate("6.A", GROUP_FIELDS)
        assert any("no placeholder" in problem for problem in problems)

    def test_an_unknown_field_is_a_problem(self):
        assert any("Unknown placeholder" in problem
                   for problem in validate("{nope}", GROUP_FIELDS))

    def test_a_broken_operation_is_a_problem(self):
        assert any("Unknown operation" in problem
                   for problem in validate("{display_name|bogus}", GROUP_FIELDS))


class TestPreview:
    """The live preview under a template field."""

    def test_a_good_template_previews_its_result(self):
        assert preview("{mail_nickname}", VALUES) == "trida6a"

    def test_a_broken_template_previews_the_reason(self):
        assert preview("{nope}", VALUES).startswith("⚠")

    def test_an_empty_result_is_labelled(self):
        assert preview("{mail}", VALUES) == "(empty)"


class TestGroupValues:
    """The dictionary built from a Microsoft 365 group."""

    @pytest.fixture
    def group(self):
        from models_m365 import M365Group, M365Member
        return M365Group(
            object_id="abc-123", display_name="Trida-6.A",
            mail_nickname="trida6a", description="Sixth A",
            mail="trida6a@skola.cz", visibility="Private",
            group_types=["Unified"],
            members=[M365Member(display_name="Jan Novák")],
        )

    def test_every_documented_field_is_present(self, group):
        assert set(group_values(group)) == set(GROUP_FIELDS)

    def test_the_values_are_taken_from_the_group(self, group):
        values = group_values(group)
        assert values['display_name'] == "Trida-6.A"
        assert values['mail_nickname'] == "trida6a"
        assert values['kind'] == "Microsoft 365"
        assert values['member_count'] == "1"

    def test_an_empty_group_still_gives_every_key(self):
        from models_m365 import M365Group
        assert set(group_values(M365Group())) == set(GROUP_FIELDS)

    def test_a_template_renders_against_a_real_group(self, group):
        assert render("{display_name|after:Trida-}", group_values(group)) == "6.A"


class TestClassValues:
    """The dictionary built from a school class."""

    @pytest.fixture
    def school_class(self, make_class):
        cls = make_class("6.A", 0)
        cls.enrollment_year = 2020
        return cls

    def test_every_documented_field_is_present(self, school_class):
        assert set(class_values(school_class)) == set(CLASS_FIELDS)

    def test_the_parts_of_the_class_name_are_derived(self, school_class):
        values = class_values(school_class)
        assert (values['class_name'], values['grade'], values['roman'],
                values['letter']) == ("6.A", "6", "VI", "A")

    def test_the_enrollment_year_is_included(self, school_class):
        assert class_values(school_class)['enrollment_year'] == "2020"

    def test_an_override_wins_over_the_stored_year(self, school_class):
        assert class_values(school_class, enrollment_year=2019)[
            'enrollment_year'] == "2019"

    def test_a_class_without_a_number_yields_empty_parts(self, make_class):
        values = class_values(make_class("Zaci", 0))
        assert values['grade'] == "" and values['roman'] == ""
        assert values['class_name'] == "Zaci"

    def test_a_template_renders_against_a_real_class(self, school_class):
        assert render("Trida-{class_name}", class_values(school_class)) == \
            "Trida-6.A"

    def test_the_school_year_looks_like_a_school_year(self, school_class):
        import re
        assert re.fullmatch(r"\d{4}/\d{4}",
                            class_values(school_class)['school_year'])
