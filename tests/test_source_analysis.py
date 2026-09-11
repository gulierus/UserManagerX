"""
Unit tests for :mod:`utils.source_analysis`.

The module under test is pure Python (no Qt, no I/O, no network), so these
tests never need the dialog stubs - nothing in ``utils.source_analysis`` can
open a modal window.  Everything is built from the shared model factories
(``make_person`` / ``make_class`` / ``make_source``) so no real user data is
touched.

Tests marked ``bug`` + ``xfail`` document defects found in the production code;
they assert the *correct* behaviour and are expected to fail until the defect
is fixed.
"""

import types

import pytest

from models import Person
from utils.class_name_utils import TEMPLATE_ARABIC, TEMPLATE_ROMAN
from utils.source_analysis import (
    AnalysisReport,
    FixOption,
    FixResult,
    IssueSeverity,
    ParameterKind,
    SourceIssue,
    _check_class_name_consistency,
    _check_class_person_mismatch,
    _check_duplicate_class_names,
    _check_duplicate_persons,
    _check_empty_classes,
    _check_empty_source,
    _check_missing_person_names,
    _check_readonly,
    analyze_merge,
    analyze_shift,
    analyze_source,
    apply_fix,
    apply_template_to_source,
    preview_template_on_names,
    rename_class,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def build_source(make_source, make_class, make_person):
    """
    Factory: ``build_source([("6.A", [("Jan", "Novák")]), ("7.B", [])])``.

    A student entry is ``(first, last)`` - the stored ``class_name`` then
    equals the class it lives in - or ``(first, last, class_name)`` to build
    the mismatching records the analysis is supposed to find.
    """
    def _build(spec, name="TestSource", readonly=False):
        source = make_source(name, [], readonly=readonly)
        for class_name, people in spec:
            persons = []
            for entry in people:
                first, last = entry[0], entry[1]
                stored = entry[2] if len(entry) > 2 else class_name
                extra = entry[3] if len(entry) > 3 else {}
                persons.append(make_person(first, last, stored, **extra))
            source.add_class(make_class(class_name, persons=persons))
        return source
    return _build


def snapshot(source):
    """Compare-able copy of everything the analysis is allowed to leave alone."""
    return [
        (cls.name, [(p.first_name, p.last_name, p.class_name) for p in cls.persons])
        for cls in source.classes
    ]


def issue_of(report, key):
    """Return the single issue with *key* (fails loudly when it is missing)."""
    matches = [issue for issue in report.issues if issue.key == key]
    assert matches, f"issue {key!r} not found, got {[i.key for i in report.issues]}"
    assert len(matches) == 1, f"issue {key!r} reported {len(matches)} times"
    return matches[0]


def issue_for(source, key):
    """Run the general analysis and pick one issue out of the report."""
    return issue_of(analyze_source(source), key)


# ===========================================================================
# _check_empty_source
# ===========================================================================

def test_check_empty_source_reports_blocking_error_when_no_class_exists(build_source):
    """A source without a single class is an ERROR that blocks shift and merge."""
    issue = _check_empty_source(build_source([]))
    assert issue.key == "empty_source"
    assert issue.severity is IssueSeverity.ERROR
    assert issue.impacts == ["Shift Classes", "Merge Both Sources"]
    assert issue.is_fixable is False          # only the "continue anyway" no-op


def test_check_empty_source_accepts_a_source_whose_only_class_is_empty(build_source):
    """One class without students is still a class - no empty_source issue."""
    assert _check_empty_source(build_source([("6.A", [])])) is None


# ===========================================================================
# _check_empty_classes
# ===========================================================================

def test_check_empty_classes_lists_exactly_the_classes_without_students(build_source):
    """Only student-less classes are reported, as an INFO with a remove fix."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", []), ("8.C", [])])
    issue = _check_empty_classes(source)
    assert issue.severity is IssueSeverity.INFO
    assert issue.affected == ["7.B", "8.C"]
    assert issue.summary.startswith("2 class(es)")
    assert [f.key for f in issue.fixes] == ["remove_empty", "keep"]


def test_check_empty_classes_returns_none_when_every_class_has_students(build_source):
    """No empty class means no issue at all."""
    assert _check_empty_classes(build_source([("6.A", [("A", "B")])])) is None


# ===========================================================================
# _check_duplicate_class_names
# ===========================================================================

def test_check_duplicate_class_names_reports_every_name_once_sorted(build_source):
    """Three '6.A' and two '1.B' are two duplicated *names*, sorted."""
    source = build_source([("6.A", []), ("1.B", []), ("6.A", []),
                           ("6.A", []), ("1.B", []), ("9.Z", [])])
    issue = _check_duplicate_class_names(source)
    assert issue.severity is IssueSeverity.WARNING
    assert issue.affected == ["1.B", "6.A"]
    assert issue.summary.startswith("2 class name(s)")


@pytest.mark.parametrize("names", [
    ["6.A", "6.a"],          # class names are compared case sensitively
    ["6.A", "6. A"],         # whitespace makes two different names
    ["6.A", "VI.A"],         # same class, two notations - still two names
    [],                      # no classes at all
    ["6.A"],
])
def test_check_duplicate_class_names_returns_none_for_distinct_names(build_source, names):
    """Only byte-identical names count as duplicates."""
    source = build_source([(name, []) for name in names])
    assert _check_duplicate_class_names(source) is None


# ===========================================================================
# _check_class_person_mismatch
# ===========================================================================

def test_check_class_person_mismatch_names_both_sides_of_the_conflict(build_source):
    """The listing shows where the student is stored and what the record says."""
    source = build_source([("VI.B", [("Eva", "Malá", "7.C"), ("Ján", "Novák")])])
    issue = _check_class_person_mismatch(source)
    assert issue.severity is IssueSeverity.WARNING
    assert issue.affected == ["Eva Malá: stored in 'VI.B', class_name = '7.C'"]
    assert issue.summary.startswith("1 student(s)")
    assert [f.key for f in issue.fixes] == ["sync_from_class", "move_to_matching", "keep"]


def test_check_class_person_mismatch_treats_none_class_name_as_empty(make_person, make_class,
                                                                    make_source):
    """A ``None`` class_name matches an unnamed class and clashes with a named one."""
    unnamed = make_class("", persons=[make_person("A", "B", None)])
    named = make_class("6.A", persons=[make_person("C", "D", None)])

    assert _check_class_person_mismatch(make_source("S", [])) is None
    only_unnamed = make_source("S", [])
    only_unnamed.add_class(unnamed)
    assert _check_class_person_mismatch(only_unnamed) is None

    with_named = make_source("S", [])
    with_named.add_class(named)
    issue = _check_class_person_mismatch(with_named)
    assert issue.affected == ["C D: stored in '6.A', class_name = 'None'"]


def test_check_class_person_mismatch_counts_all_but_lists_at_most_50(build_source):
    """The summary keeps the real number, the listing is capped for the UI."""
    people = [(f"First{i}", f"Last{i}", "9.Z") for i in range(60)]
    issue = _check_class_person_mismatch(build_source([("6.A", people)]))
    assert issue.summary.startswith("60 student(s)")
    assert len(issue.affected) == 50


def test_check_class_person_mismatch_is_silent_when_every_record_matches(build_source):
    """Consistent data produces no issue."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", [("Eva", "Malá")])])
    assert _check_class_person_mismatch(source) is None


# ===========================================================================
# _check_missing_person_names
# ===========================================================================

@pytest.mark.parametrize("first,last,incomplete", [
    ("Jan", "Novák", False),
    ("", "Novák", True),
    ("Jan", "", True),
    ("   ", "Novák", True),          # whitespace only is not a name
    ("Jan", "\t\n", True),
    ("Ľ", "Ď", False),               # one diacritic letter is a valid name
])
def test_check_missing_person_names_uses_stripped_names(build_source, first, last, incomplete):
    """Blank and whitespace-only names count as missing, single letters do not."""
    issue = _check_missing_person_names(build_source([("6.A", [(first, last)])]))
    assert (issue is not None) is incomplete
    if incomplete:
        assert issue.severity is IssueSeverity.WARNING
        assert issue.affected == [f"6.A: '{first}' '{last}'"]
        assert issue.impacts == ["Merge Both Sources", "Generate credentials"]


def test_check_missing_person_names_counts_all_but_lists_at_most_50(build_source):
    """Sixty broken records are counted, fifty are listed."""
    issue = _check_missing_person_names(
        build_source([("6.A", [("", f"Last{i}") for i in range(60)])]))
    assert issue.summary.startswith("60 student(s)")
    assert len(issue.affected) == 50


# ===========================================================================
# _check_duplicate_persons
# ===========================================================================

def test_check_duplicate_persons_ignores_case_and_diacritics(build_source):
    """'Ján Novák' and 'JAN NOVAK' in the same class are one identity."""
    source = build_source([("6.A", [("Ján", "Novák"), ("JAN", "NOVAK")])])
    issue = _check_duplicate_persons(source)
    assert issue.severity is IssueSeverity.WARNING
    assert issue.summary.startswith("1 student identity")
    assert issue.impacts == ["Merge Both Sources"]


def test_check_duplicate_persons_keeps_the_class_part_of_the_identity(build_source):
    """The same name in two different classes is not a duplicate."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.A", [("Jan", "Novák")])])
    assert _check_duplicate_persons(source) is None


def test_check_duplicate_persons_counts_identities_not_records(build_source):
    """Three copies of one student are still a single duplicated identity."""
    source = build_source([("6.A", [("Jan", "Novák")] * 3),
                           ("6.A", [("Eva", "Malá")] * 2)])
    issue = _check_duplicate_persons(source)
    assert issue.summary.startswith("2 student identity/identities")
    assert len(issue.affected) == 2


@pytest.mark.bug
def test_check_duplicate_persons_shows_the_original_spelling(build_source):
    """The affected list must show the student as written, not lower-cased ASCII."""
    source = build_source([("6.A", [("Ján", "Novák"), ("Ján", "Novák")])])
    issue = _check_duplicate_persons(source)
    assert len(issue.affected) == 1
    entry = issue.affected[0]
    # the real, accented spelling - not the folded comparison key "jan novak"
    assert "Ján Novák" in entry
    assert "6.A" in entry


# ===========================================================================
# _check_class_name_consistency
# ===========================================================================

def test_check_class_name_consistency_returns_nothing_for_a_source_without_classes(
        build_source):
    """No names, no name problems."""
    assert _check_class_name_consistency(build_source([])) == []


def test_check_class_name_consistency_is_quiet_for_uniform_names(build_source):
    """'6.A' next to '7.B' is one single format - nothing to report."""
    assert _check_class_name_consistency(
        build_source([("6.A", []), ("7.B", [])])) == []


@pytest.mark.parametrize("names,expected_severity", [
    (["Blue class", "Green room"], IssueSeverity.ERROR),     # every name unparseable
    (["6.A", "Blue class"], IssueSeverity.WARNING),          # only some of them
])
def test_check_class_name_consistency_escalates_when_no_name_can_be_shifted(
        build_source, names, expected_severity):
    """Unparseable names are an ERROR only when they are *all* unparseable."""
    issues = {i.key: i for i in _check_class_name_consistency(
        build_source([(name, []) for name in names]))}
    issue = issues["unparseable_class_names"]
    assert issue.severity is expected_severity
    assert issue.affected == [n for n in names if n != "6.A"]
    assert [f.key for f in issue.fixes] == ["keep", "remove_unparseable"]
    assert ("EVERY class" in issue.detail) is (expected_severity is IssueSeverity.ERROR)


def test_check_class_name_consistency_reports_mixed_numeral_styles(build_source):
    """Arabic next to Roman is two formats and offers the unify solutions."""
    issues = {i.key: i for i in _check_class_name_consistency(
        build_source([("6.A", []), ("VI.B", [])]))}
    issue = issues["inconsistent_class_names"]
    assert issue.severity is IssueSeverity.WARNING
    assert issue.summary.startswith("2 different class-name formats")
    assert [f.key for f in issue.fixes] == ["unify_arabic", "unify_roman",
                                            "unify_custom", "keep"]
    assert issue.affected == [
        "Arabic number, dot separator, with section letter → 6.A",
        "upper-case Roman number, dot separator, with section letter → VI.B",
    ]


def test_check_class_name_consistency_does_not_count_free_text_as_a_format(build_source):
    """One real format plus free text is not an inconsistency."""
    keys = [i.key for i in _check_class_name_consistency(
        build_source([("6.A", []), ("7.B", []), ("Blue class", [])]))]
    assert "inconsistent_class_names" not in keys
    assert "unparseable_class_names" in keys


def test_check_class_name_consistency_flags_whitespace_only_in_standard_names(build_source):
    """'6. A' and ' 9.B ' are cleanable, 'Blue class 6. C' is left alone."""
    issues = {i.key: i for i in _check_class_name_consistency(
        build_source([("6. A", []), (" 9.B ", []), ("Blue class 6. C", [])]))}
    issue = issues["whitespace_in_class_names"]
    assert issue.affected == ["6. A", " 9.B "]
    assert [f.key for f in issue.fixes] == ["remove_whitespace",
                                            "replace_whitespace", "keep"]
    replace = issue.get_fix("replace_whitespace")
    assert replace.parameter_kind is ParameterKind.TEXT
    assert replace.parameter_default == "-"


def test_check_class_name_consistency_reports_unnamed_classes_with_their_size(build_source):
    """Empty and whitespace-only names are reported with their student count."""
    issues = {i.key: i for i in _check_class_name_consistency(
        build_source([("", [("A", "B")]), ("   ", []), ("6.A", [])]))}
    issue = issues["empty_class_names"]
    assert issue.summary.startswith("2 class(es)")
    assert issue.affected == ["(empty) - 1 student(s)", "(empty) - 0 student(s)"]
    assert [f.key for f in issue.fixes] == ["name_unnamed", "remove_unnamed", "keep"]
    assert issue.get_fix("name_unnamed").parameter_default == "Unnamed"


# ===========================================================================
# _check_readonly
# ===========================================================================

def test_check_readonly_is_an_info_that_names_the_source(build_source):
    """A read-only source is worth knowing about but breaks nothing."""
    issue = _check_readonly(build_source([("6.A", [])], name="EduPage", readonly=True))
    assert issue.severity is IssueSeverity.INFO
    assert "'EduPage'" in issue.summary
    assert issue.is_fixable is False


def test_check_readonly_returns_none_for_writable_and_for_attribute_less_sources(
        build_source):
    """Objects without a ``readonly`` attribute are treated as writable."""
    assert _check_readonly(build_source([("6.A", [])])) is None
    assert _check_readonly(types.SimpleNamespace(name="no-such-attribute")) is None


# ===========================================================================
# analyze_source
# ===========================================================================

def test_analyze_source_reports_a_clean_source_as_clean(build_source):
    """Consistent data yields an empty issue list and honest statistics."""
    source = build_source([("6.A", [("Jan", "Novák")]),
                           ("7.A", [("Eva", "Malá"), ("Ján", "Kováč")])],
                          name="Clean")
    report = analyze_source(source)
    assert report.is_clean
    assert report.has_blocking_issues is False
    assert report.title == "Analysis of 'Clean'"
    assert report.stats == {
        "Classes": 2,
        "Students": 3,
        "Class names with a number": 2,
        "Class-name formats": 1,
        "Dominant numeral style": "Arabic (6, 9)",
        "Read-only": "no",
    }


def test_analyze_source_finds_every_kind_of_problem_at_once(build_source):
    """One deliberately broken source triggers the whole check list."""
    source = build_source([
        ("6.A", [("Jan", "Novák"), ("Jan", "Novák")]),   # duplicate students
        ("6.A", []),                                     # duplicate + empty class
        ("VI.B", [("", "Nemec"), ("Eva", "Malá", "7.C")]),  # missing name, mismatch
        ("", [("A", "B")]),                              # unnamed class
        ("Blue class", [("X", "Y")]),                    # unparseable name
    ], readonly=True)
    keys = {issue.key for issue in analyze_source(source).issues}
    assert keys == {
        "duplicate_class_names", "class_person_mismatch", "duplicate_persons",
        "missing_person_names", "empty_classes", "readonly_source",
        "unparseable_class_names", "inconsistent_class_names", "empty_class_names",
    }


def test_analyze_source_sorts_errors_before_warnings_before_infos(build_source):
    """``AnalysisReport.sort`` puts the blocking problems on top."""
    source = build_source([("Blue class", [("A", "B")]), ("Green room", [])])
    report = analyze_source(source)
    severities = [issue.severity for issue in report.issues]
    assert severities == sorted(severities, key=lambda s: ["error", "warning",
                                                           "info"].index(s.value))
    assert [i.key for i in report.errors] == ["unparseable_class_names"]
    assert [i.key for i in report.infos] == ["empty_classes"]
    assert report.warnings == []
    assert report.has_blocking_issues is True


def test_analyze_source_accepts_a_custom_title(build_source):
    """The caller can override the caption of the report."""
    report = analyze_source(build_source([("6.A", [])]), title="Before the shift")
    assert report.title == "Before the shift"


@pytest.mark.parametrize("names,expected_style,expected_formats", [
    (["6.A", "7.B"], "Arabic (6, 9)", 1),
    (["VI.A", "IX.B"], "Roman (VI, IX)", 1),
    (["6.A", "VI.A"], "Arabic (6, 9)", 2),      # tie goes to Arabic
    (["6.A", "VI.A", "IX.B"], "Roman (VI, IX)", 2),
    (["Blue", "Green"], "no numeral", 0),
    ([], "no numeral", 0),
])
def test_analyze_source_statistics_describe_the_class_names(build_source, names,
                                                            expected_style,
                                                            expected_formats):
    """Dominant style and format count summarise the naming of the source."""
    stats = analyze_source(build_source([(n, []) for n in names])).stats
    assert stats["Dominant numeral style"] == expected_style
    assert stats["Class-name formats"] == expected_formats
    assert stats["Classes"] == len(names)


def test_analyze_source_marks_a_read_only_source_in_the_statistics(build_source):
    """The read-only flag reaches both the issue list and the summary panel."""
    report = analyze_source(build_source([("6.A", [("A", "B")])], readonly=True))
    assert report.stats["Read-only"] == "yes"
    assert [i.key for i in report.issues] == ["readonly_source"]


def test_analyze_source_never_modifies_the_source_and_is_repeatable(build_source):
    """Analysis is read-only: two runs agree and the data is untouched."""
    source = build_source([("6. A", [("Jan", "Novák", "7.C")]), ("VI.B", []),
                           ("", [("", "Malá")])])
    before = snapshot(source)
    first = analyze_source(source)
    second = analyze_source(source)
    assert snapshot(source) == before
    assert [i.key for i in first.issues] == [i.key for i in second.issues]
    assert first.stats == second.stats


def test_source_issue_helpers_expose_the_offered_fixes(build_source):
    """``get_fix`` finds by key and ``is_fixable`` ignores the no-op option."""
    issue = issue_for(build_source([("6.A", []), ("6.A", [])]), "duplicate_class_names")
    assert issue.is_fixable is True
    assert issue.get_fix("merge_duplicates").is_noop is False
    assert issue.get_fix("keep").is_noop is True
    assert issue.get_fix("no_such_fix") is None
    assert AnalysisReport(title="x").is_clean is True


# ===========================================================================
# analyze_shift
# ===========================================================================

def test_analyze_shift_keeps_the_general_issues_and_adds_shift_statistics(build_source):
    """The shift report is the general report plus three shift counters."""
    source = build_source([("6.A", [("A", "B")]), ("VI.A", [("C", "D")]),
                           ("9.B", [("E", "F"), ("G", "H")]), ("Blue", [])])
    report = analyze_shift(source, 1)
    assert report.stats["Classes that will be shifted"] == 2
    assert report.stats["Classes that will be removed"] == 1
    assert report.stats["Classes without a number (skipped)"] == 1
    assert {"unparseable_class_names", "inconsistent_class_names"} <= {
        i.key for i in report.issues}


def test_analyze_shift_reports_graduating_classes_with_their_students(build_source):
    """The graduating year leaves the school - INFO, with the head count."""
    source = build_source([("9.A", [("A", "B"), ("C", "D")]), ("8.A", [("E", "F")])])
    issue = issue_of(analyze_shift(source, 1, graduation_year=9), "graduating_classes")
    assert issue.severity is IssueSeverity.INFO
    assert issue.affected == ["9.A"]
    assert "2 student(s)" in issue.summary
    assert "year 9" in issue.summary


@pytest.mark.parametrize("kwargs", [
    {"graduation_year": None},
    {"remove_graduating": False},
])
def test_analyze_shift_does_not_graduate_when_the_option_is_off(build_source, kwargs):
    """Without graduation the year-9 class is shifted like any other."""
    source = build_source([("9.A", [("A", "B")])])
    report = analyze_shift(source, 1, **kwargs)
    assert "graduating_classes" not in {i.key for i in report.issues}
    assert report.stats["Classes that will be shifted"] == 1
    assert report.stats["Classes that will be removed"] == 0


def test_analyze_shift_detects_names_that_collide_after_the_shift(build_source):
    """A blocked '1.A' and a shifted '2.A' both end up as '1.A'."""
    source = build_source([("1.A", [("A", "B")]), ("2.A", [("C", "D")])])
    issue = issue_of(analyze_shift(source, -1), "shift_collision")
    assert issue.severity is IssueSeverity.WARNING
    assert issue.affected == ["1.A + 2.A → 1.A"]
    assert issue.summary.startswith("1 class name(s)")


def test_analyze_shift_ignores_removed_classes_when_looking_for_collisions(build_source):
    """A graduating class disappears, so it cannot collide with anything."""
    source = build_source([("9.A", [("A", "B")]), ("8.A", [("C", "D")])])
    keys = {i.key for i in analyze_shift(source, 1, graduation_year=9).issues}
    assert "shift_collision" not in keys


def test_analyze_shift_reports_nothing_to_shift_when_no_name_has_a_number(build_source):
    """Free-text class names make the whole operation pointless - ERROR."""
    source = build_source([("Blue class", [("A", "B")]), ("Green room", [])])
    issue = issue_of(analyze_shift(source, 1), "nothing_to_shift")
    assert issue.severity is IssueSeverity.ERROR
    assert issue.affected == ["Blue class", "Green room"]
    assert [f.key for f in issue.fixes] == ["unify_arabic", "unify_roman",
                                            "unify_custom", "keep"]


def test_analyze_shift_is_silent_about_nothing_to_shift_when_one_class_moves(build_source):
    """A single shiftable class is enough to make the operation useful."""
    source = build_source([("Blue class", []), ("6.A", [("A", "B")])])
    assert "nothing_to_shift" not in {i.key for i in analyze_shift(source, 1).issues}


def test_analyze_shift_reports_classes_that_would_leave_the_year_range(build_source):
    """'1.A' cannot go down to year 0 - it is carried over unchanged."""
    source = build_source([("1.A", [("A", "B")]), ("5.A", [("C", "D")])])
    issue = issue_of(analyze_shift(source, -1), "shift_out_of_range")
    assert issue.severity is IssueSeverity.WARNING
    assert issue.affected == ["1.A: shift would produce year 0 (minimum is 1)"]
    assert issue.is_fixable is False


def test_analyze_shift_stops_at_the_highest_supported_numeral(build_source):
    """Year 3999 is the ceiling: it cannot move and swallows year 3998."""
    source = build_source([("3998.A", [("A", "B")]), ("3999.A", [("C", "D")])])
    report = analyze_shift(source, 1)
    assert issue_of(report, "shift_out_of_range").affected == [
        "3999.A: shift would produce year 4000 (maximum is 3999)"]
    assert issue_of(report, "shift_collision").affected == ["3998.A + 3999.A → 3999.A"]
    assert report.stats["Classes that will be shifted"] == 1


def test_analyze_shift_on_an_empty_source_adds_no_shift_issues(build_source):
    """Nothing to shift, but the empty-source error is still the only one."""
    report = analyze_shift(build_source([], name="Empty"), 1)
    assert [i.key for i in report.issues] == ["empty_source"]
    assert report.stats["Classes that will be shifted"] == 0
    assert report.title == "Shift analysis of 'Empty'"


def test_analyze_shift_does_not_modify_the_source(build_source):
    """Analysing a shift must not shift anything yet."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("9.B", [("Eva", "Malá")])])
    before = snapshot(source)
    analyze_shift(source, 1)
    assert snapshot(source) == before


@pytest.mark.bug
def test_analyze_shift_with_zero_delta_does_not_blame_the_class_names(build_source):
    """A zero shift changes nothing, but the names are perfectly shiftable."""
    source = build_source([("6.A", [("A", "B")]), ("7.B", [("C", "D")])])
    report = analyze_shift(source, 0)
    out_of_range = [i for i in report.issues if i.key == "shift_out_of_range"]
    assert out_of_range == [], "years 6 and 7 are inside the allowed range"
    nothing = [i for i in report.issues if i.key == "nothing_to_shift"]
    assert not nothing or "number" not in nothing[0].summary


# ===========================================================================
# analyze_merge
# ===========================================================================

def test_analyze_merge_warns_when_both_panels_point_at_one_source(build_source):
    """Merging a source with itself is a no-op the user should know about."""
    source = build_source([("6.A", [("Jan", "Novák")])], name="Same")
    report = analyze_merge(source, source, "union")
    issue = issue_of(report, "same_source_twice")
    assert issue.severity is IssueSeverity.WARNING
    assert report.title == "Merge analysis: 'Same' + 'Same'"
    assert report.stats["Students in both"] == 1


def test_analyze_merge_flags_two_sources_that_share_no_class_name_format(build_source):
    """Arabic on the left, Roman on the right - the merge cannot match anybody."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="Left")
    right = build_source([("VI.A", [("Jan", "Novák", "VI.A")])], name="Right")
    issue = issue_of(analyze_merge(left, right, "union"), "cross_source_class_style")
    assert issue.target == "both"
    assert issue.affected == [
        "Left: Arabic number, dot separator, with section letter",
        "Right: upper-case Roman number, dot separator, with section letter",
    ]
    assert [f.key for f in issue.fixes] == ["unify_arabic", "unify_roman",
                                            "unify_custom", "keep"]


@pytest.mark.parametrize("right_names", [
    ["6.B"],                 # identical format
    ["6.B", "IX.C"],         # one shared format is enough
    ["Blue class"],          # free text is not a format at all
    [],                      # no classes on the right
])
def test_analyze_merge_accepts_any_shared_or_missing_class_format(build_source, right_names):
    """The style warning only fires when both sides have formats and share none."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="Left")
    right = build_source([(n, []) for n in right_names], name="Right")
    keys = {i.key for i in analyze_merge(left, right, "union").issues}
    assert "cross_source_class_style" not in keys


@pytest.mark.parametrize("strategy,expected", [
    ("intersection", True),
    ("union", False),
])
def test_analyze_merge_reports_no_common_persons_only_for_intersection(build_source,
                                                                      strategy, expected):
    """An empty intersection is an ERROR; a union of the same data is fine."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="Left")
    right = build_source([("6.A", [("Eva", "Malá", "6.A")])], name="Right")
    report = analyze_merge(left, right, strategy)
    issue_keys = {i.key for i in report.issues}
    assert ("no_common_persons" in issue_keys) is expected
    if expected:
        issue = issue_of(report, "no_common_persons")
        assert issue.severity is IssueSeverity.ERROR
        assert issue.target == "both"


def test_analyze_merge_does_not_report_no_common_persons_when_a_side_is_empty(build_source):
    """An empty side is already reported as empty_merge_side - no double message."""
    left = build_source([], name="Left")
    right = build_source([("6.A", [("Jan", "Novák")])], name="Right")
    keys = {i.key for i in analyze_merge(left, right, "intersection").issues}
    assert "no_common_persons" not in keys
    assert "empty_merge_side" in keys


@pytest.mark.parametrize("strategy,severity", [
    ("intersection", IssueSeverity.ERROR),
    ("union", IssueSeverity.WARNING),
])
def test_analyze_merge_grades_an_empty_side_by_strategy(build_source, strategy, severity):
    """An empty side kills an intersection but only weakens a union."""
    left = build_source([("6.A", [])], name="LeftEmpty")
    right = build_source([("6.A", [("Jan", "Novák")])], name="Right")
    issue = issue_of(analyze_merge(left, right, strategy), "empty_merge_side")
    assert issue.severity is severity
    assert issue.title == "Left source has no students"
    assert "'LeftEmpty'" in issue.summary


def test_analyze_merge_detects_the_same_student_in_two_different_classes(build_source):
    """Matching is diacritics insensitive, so 'Ján Novak' is the same student."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="Left")
    right = build_source([("7.A", [("Ján", "Novak", "7.A")])], name="Right")
    report = analyze_merge(left, right, "union")
    issue = issue_of(report, "same_person_different_class")
    assert issue.affected == ["Ján Novak: 6.A (left) vs 7.A (right)"]
    assert issue.severity is IssueSeverity.WARNING
    assert report.stats["Students in both"] == 0
    assert report.stats["Result with 'union'"] == 2


def test_analyze_merge_lists_conflicting_attributes_of_identical_students(build_source):
    """Only attributes that are filled in on both sides and differ are conflicts."""
    left = build_source([("6.A", [("Jan", "Novák", "6.A",
                                   {"ad_username": "jnovak",
                                    "ad_email": "jan@skola.sk",
                                    "ad_display_name": "Jan Novák"})])], name="Left")
    right = build_source([("6.A", [("Jan", "Novák", "6.A",
                                    {"ad_username": "novak.j",
                                     "ad_email": "jan@skola.sk"})])], name="Right")
    issue = issue_of(analyze_merge(left, right, "union"), "conflicting_person_data")
    assert issue.severity is IssueSeverity.INFO
    assert issue.affected == ["Jan Novák - ad_username: 'jnovak' vs 'novak.j'"]
    assert issue.is_fixable is False


def test_analyze_merge_routes_per_source_issues_to_the_side_they_belong_to(build_source):
    """Cloned issues are prefixed with the side and carry the fix target."""
    left = build_source([("6.A", [("", "Nemec")]), ("6.A", [])], name="Left")
    right = build_source([("6.A", [("Eva", "Malá", "7.C")])], name="Right")
    report = analyze_merge(left, right, "union")
    routed = {(i.key, i.target, i.title) for i in report.issues
              if i.target in ("left", "right")}
    assert routed == {
        ("duplicate_class_names", "left", "Left source: Duplicate class names"),
        ("missing_person_names", "left",
         "Left source: Students with an incomplete name"),
        ("class_person_mismatch", "right",
         "Right source: Students with a mismatching class name"),
    }


def test_analyze_merge_drops_per_source_issues_that_cannot_affect_a_merge(build_source):
    """read-only, empty classes and unshiftable names are irrelevant here."""
    left = build_source([("6.A", [("Jan", "Novák")]), ("7.B", []),
                         ("Blue class", [("X", "Y")])], name="Left", readonly=True)
    right = build_source([("6.A", [("Jan", "Novák", "6.A")])], name="Right")
    keys = {i.key for i in analyze_merge(left, right, "union").issues}
    assert "readonly_source" not in keys
    assert "empty_classes" not in keys
    assert "unparseable_class_names" not in keys


def test_analyze_merge_statistics_count_the_overlap(build_source):
    """The stats predict the size of both merge results."""
    left = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])], name="Left")
    right = build_source([("6.A", [("JAN", "NOVAK", "6.A"),
                                   ("Peter", "Kováč", "6.A")])], name="Right")
    stats = analyze_merge(left, right, "union").stats
    assert stats == {
        "Left students": 2,
        "Right students": 2,
        "Students only in left": 1,
        "Students only in right": 1,
        "Students in both": 1,
        "Result with 'union'": 3,
        "Result with 'intersection'": 1,
    }


def test_analyze_merge_lists_every_left_class_a_moved_student_was_found_in(build_source):
    """A student stored twice on the left is reported with both classes."""
    left = build_source([("6.A", [("Jan", "Novák")]), ("7.A", [("Jan", "Novák")])],
                        name="Left")
    right = build_source([("8.A", [("Jan", "Novák", "8.A")])], name="Right")
    issue = issue_of(analyze_merge(left, right, "union"), "same_person_different_class")
    assert issue.affected == ["Jan Novák: 6.A, 7.A (left) vs 8.A (right)"]


def test_analyze_merge_treats_an_unknown_strategy_like_a_union(build_source):
    """An unexpected strategy string must not escalate anything to an ERROR."""
    left = build_source([], name="Left")
    right = build_source([("6.A", [("Jan", "Novák", "6.A")])], name="Right")
    report = analyze_merge(left, right, "bogus-strategy")
    assert issue_of(report, "empty_merge_side").severity is IssueSeverity.WARNING
    assert "no_common_persons" not in {i.key for i in report.issues}
    assert report.stats["Result with 'union'"] == 1


def test_analyze_merge_does_not_modify_either_source(build_source):
    """A merge analysis is read-only on both sides."""
    left = build_source([("6. A", [("Jan", "Novák", "7.C")])], name="Left")
    right = build_source([("VI.A", [("", "Malá", "VI.A")])], name="Right")
    before = (snapshot(left), snapshot(right))
    analyze_merge(left, right, "intersection")
    assert (snapshot(left), snapshot(right)) == before


# ===========================================================================
# rename_class
# ===========================================================================

def test_rename_class_propagates_the_new_name_to_every_student(build_source):
    """Renaming keeps the model consistent and reports that it changed data."""
    source = build_source([("6.A", [("Jan", "Novák"), ("Eva", "Malá")])])
    cls = source.classes[0]
    assert rename_class(source, cls, "7.A") is True
    assert cls.name == "7.A"
    assert [p.class_name for p in cls.persons] == ["7.A", "7.A"]
    assert all(p.is_dirty() for p in cls.persons)


def test_rename_class_to_the_same_name_changes_nothing(build_source):
    """Renaming to the current name is a no-op and does not dirty the students."""
    source = build_source([("6.A", [("Jan", "Novák")])])
    cls = source.classes[0]
    assert rename_class(source, cls, "6.A") is False
    assert cls.persons[0].is_dirty() is False


def test_rename_class_merges_into_an_existing_class_of_that_name(build_source):
    """The renamed class disappears and its students join the existing one."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.A", [("Eva", "Malá")])])
    six, seven = source.classes
    assert rename_class(source, six, "7.A") is True
    assert snapshot(source) == [("7.A", [("Eva", "Malá", "7.A"),
                                         ("Jan", "Novák", "7.A")])]
    assert six.persons == []
    assert len(source.classes) == 1


def test_rename_class_on_a_class_outside_the_source_still_moves_the_students(
        build_source, make_class, make_person):
    """A detached class merges its students in without raising."""
    source = build_source([("7.A", [("Eva", "Malá")])])
    orphan = make_class("6.A", persons=[make_person("Jan", "Novák", "6.A")])
    assert rename_class(source, orphan, "7.A") is True
    assert [p.first_name for p in source.classes[0].persons] == ["Eva", "Jan"]


@pytest.mark.bug
def test_rename_class_removes_exactly_the_renamed_class_object(build_source):
    """Merging away one '6.A' must not detach a second, identical '6.A'."""
    source = build_source([("6.A", []), ("6.A", [("Jan", "Novák")]), ("7.A", [])])
    first_six, second_six, seven = source.classes

    rename_class(source, second_six, "7.A")

    assert [p.first_name for p in seven.persons] == ["Jan"]
    assert any(c is first_six for c in source.classes), \
        "the untouched empty '6.A' was removed instead of the renamed one"
    assert not any(c is second_six for c in source.classes)


# ===========================================================================
# apply_template_to_source / preview_template_on_names
# ===========================================================================

def test_apply_template_to_source_returns_one_result_per_class(build_source):
    """Every class is reported, changed or not, in source order."""
    source = build_source([("VI.A", [("Jan", "Novák")]), ("6.B", []), ("Blue", [])])
    changed, results = apply_template_to_source(source, TEMPLATE_ARABIC)
    assert changed == 1
    assert [(r.original, r.result, r.changed, r.recognised) for r in results] == [
        ("VI.A", "6.A", True, True),
        ("6.B", "6.B", False, True),
        ("Blue", "Blue", False, False),
    ]
    assert snapshot(source) == [("6.A", [("Jan", "Novák", "6.A")]),
                                ("6.B", []), ("Blue", [])]


def test_apply_template_to_source_merges_classes_that_converge(build_source):
    """'VI.A' and '6.A' are one class once the names are unified."""
    source = build_source([("VI.A", [("Jan", "Novák")]), ("6.A", [("Eva", "Malá")])])
    changed, results = apply_template_to_source(source, TEMPLATE_ARABIC)
    assert changed == 1
    assert snapshot(source) == [("6.A", [("Eva", "Malá", "6.A"),
                                         ("Jan", "Novák", "6.A")])]


def test_apply_template_to_source_is_idempotent(build_source):
    """Converting twice to the same template changes nothing the second time."""
    source = build_source([("VI.A", [("Jan", "Novák")]), ("9 b", [])])
    apply_template_to_source(source, TEMPLATE_ROMAN)
    after_first = snapshot(source)
    changed, _ = apply_template_to_source(source, TEMPLATE_ROMAN)
    assert changed == 0
    assert snapshot(source) == after_first == [("VI.A", [("Jan", "Novák", "VI.A")]),
                                               ("IX.B", [])]


def test_apply_template_to_source_leaves_everything_alone_for_a_broken_template(
        build_source):
    """An unusable template renders nothing and touches no class."""
    source = build_source([("VI.A", [("Jan", "Novák")])])
    before = snapshot(source)
    changed, results = apply_template_to_source(source, "{not_a_placeholder}")
    assert changed == 0
    assert snapshot(source) == before
    assert results[0].message == "unknown placeholder '{not_a_placeholder}'"


def test_apply_template_to_source_on_an_empty_source(build_source):
    """No classes, no results, no changes."""
    assert apply_template_to_source(build_source([]), TEMPLATE_ARABIC) == (0, [])


def test_preview_template_on_names_touches_no_data(build_source):
    """The preview renders names without needing a source at all."""
    results = preview_template_on_names(["VI.A", "Blue"], TEMPLATE_ARABIC)
    assert [(r.original, r.result, r.changed) for r in results] == [
        ("VI.A", "6.A", True), ("Blue", "Blue", False)]


# ===========================================================================
# apply_fix - dispatching
# ===========================================================================

def test_apply_fix_rejects_a_key_the_issue_does_not_offer(build_source):
    """An unknown solution is refused without touching the data."""
    source = build_source([("6.A", []), ("7.B", [])])
    issue = issue_for(source, "empty_classes")
    before = snapshot(source)
    result = apply_fix(source, issue, "remove_unnamed")
    assert result == FixResult(applied=False, changed=0,
                               message="Unknown solution 'remove_unnamed'.")
    assert snapshot(source) == before


def test_apply_fix_accepts_the_no_op_option_without_changing_anything(build_source):
    """'keep' is applied successfully and changes zero records."""
    source = build_source([("6.A", []), ("7.B", [])])
    issue = issue_for(source, "empty_classes")
    before = snapshot(source)
    result = apply_fix(source, issue, "keep")
    assert (result.applied, result.changed, result.message) == (
        True, 0, "No change requested.")
    assert snapshot(source) == before


def test_apply_fix_reports_an_unexpected_failure_instead_of_raising(build_source):
    """A source that explodes while being fixed produces applied=False."""
    issue = issue_for(build_source([("6.A", []), ("7.B", [])]), "empty_classes")

    class ExplodingSource:
        name = "broken"

        @property
        def classes(self):
            raise RuntimeError("storage went away")

    result = apply_fix(ExplodingSource(), issue, "remove_empty")
    assert result.applied is False
    assert result.message == "Could not apply the fix: storage went away"


def test_apply_fix_reports_a_fix_key_that_has_no_implementation(build_source):
    """A hand-built option with an unknown key is refused, not silently applied."""
    source = build_source([("6.A", [])])
    issue = SourceIssue(key="made_up", title="t", severity=IssueSeverity.WARNING,
                        summary="s", fixes=[FixOption(key="teleport", label="l")])
    result = apply_fix(source, issue, "teleport")
    assert result.applied is False
    assert result.message == "Solution 'teleport' is not implemented."


# ===========================================================================
# apply_fix - class name unification
# ===========================================================================

@pytest.mark.parametrize("fix_key,parameter,expected", [
    ("unify_arabic", None, ["6.A", "9.B", "Blue"]),
    ("unify_roman", None, ["VI.A", "IX.B", "Blue"]),
    ("unify_custom", "{roman}-{letter_lower}", ["VI-a", "IX-b", "Blue"]),
    ("unify_custom", None, ["6.A", "9.B", "Blue"]),   # falls back to Arabic
    ("unify_custom", "", ["6.A", "9.B", "Blue"]),     # empty template too
    ("keep", None, ["6.A", "IX.B", "Blue"]),
])
def test_apply_fix_unify_rewrites_recognised_names_only(build_source, fix_key,
                                                        parameter, expected):
    """Names without a number survive every unification untouched."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("IX.B", [("Eva", "Malá")]),
                           ("Blue", [("X", "Y")])])
    issue = issue_for(source, "inconsistent_class_names")
    result = apply_fix(source, issue, fix_key, parameter)
    assert result.applied is True
    assert [c.name for c in source.classes] == expected
    assert [p.class_name for p in source.get_all_persons()] == expected


def test_apply_fix_unify_mentions_the_names_it_could_not_touch(build_source):
    """The message tells the user how many names had no number."""
    source = build_source([("6.A", []), ("IX.B", []), ("Blue", []), ("Green", [])])
    issue = issue_for(source, "inconsistent_class_names")
    result = apply_fix(source, issue, "unify_arabic")
    assert result.changed == 1
    assert result.message == ("Renamed 1 class(es). 2 class name(s) had no number "
                              "and were left unchanged.")


def test_apply_fix_unify_with_an_unusable_template_changes_nothing(build_source):
    """A broken custom template degrades to a no-op instead of corrupting names."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("IX.B", [])])
    issue = issue_for(source, "inconsistent_class_names")
    before = snapshot(source)
    result = apply_fix(source, issue, "unify_custom", "{nope}")
    assert result.changed == 0
    assert snapshot(source) == before


# ===========================================================================
# apply_fix - whitespace
# ===========================================================================

@pytest.mark.parametrize("fix_key,parameter,expected", [
    ("remove_whitespace", None, ["6.A", "9.B", "Blue class 6. C"]),
    ("replace_whitespace", "-", ["6.-A", "9.B", "Blue class 6. C"]),
    ("replace_whitespace", None, ["6.-A", "9.B", "Blue class 6. C"]),  # default "-"
    ("replace_whitespace", "", ["6.A", "9.B", "Blue class 6. C"]),
    ("replace_whitespace", "_", ["6._A", "9.B", "Blue class 6. C"]),
])
def test_apply_fix_whitespace_only_cleans_standard_names(build_source, fix_key,
                                                         parameter, expected):
    """Descriptive names keep their spaces; standard ones are normalised."""
    source = build_source([("6. A", [("Jan", "Novák")]), (" 9.B ", []),
                           ("Blue class 6. C", [])])
    issue = issue_for(source, "whitespace_in_class_names")
    result = apply_fix(source, issue, fix_key, parameter)
    assert [c.name for c in source.classes] == expected
    assert result.changed == 2
    assert result.message == "Cleaned 2 class name(s)."
    assert source.classes[0].persons[0].class_name == expected[0]


def test_apply_fix_unify_upper_cases_diacritic_section_letters(build_source):
    """National alphabets survive the conversion; the letter is upper-cased."""
    source = build_source([("6.á", [("Ľubica", "Ďurišová")]), ("IX.Č", [])])
    issue = issue_for(source, "inconsistent_class_names")
    result = apply_fix(source, issue, "unify_arabic")
    assert result.changed == 2
    assert snapshot(source) == [("6.Á", [("Ľubica", "Ďurišová", "6.Á")]), ("9.Č", [])]


def test_apply_fix_remove_whitespace_keeps_the_original_letter_case(build_source):
    """Cleaning spaces is not a conversion - '6. á' stays lower case."""
    source = build_source([("6. á", [("Ľubica", "Ďurišová")])])
    issue = issue_for(source, "whitespace_in_class_names")
    apply_fix(source, issue, "remove_whitespace")
    assert snapshot(source) == [("6.á", [("Ľubica", "Ďurišová", "6.á")])]


def test_apply_fix_remove_whitespace_merges_the_two_spellings_of_a_class(build_source):
    """'6. A' and '6.A' become one class with all students."""
    source = build_source([("6. A", [("Jan", "Novák")]), ("6.A", [("Eva", "Malá")])])
    issue = issue_for(source, "whitespace_in_class_names")
    result = apply_fix(source, issue, "remove_whitespace")
    assert result.changed == 1
    assert snapshot(source) == [("6.A", [("Eva", "Malá", "6.A"),
                                         ("Jan", "Novák", "6.A")])]


# ===========================================================================
# apply_fix - structural fixes
# ===========================================================================

def test_apply_fix_merge_duplicates_collects_students_in_the_first_object(build_source):
    """Three '6.A' objects become one, keeping the source order of students."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("6.A", [("Eva", "Malá")]),
                           ("6.A", []), ("7.B", [])])
    issue = issue_for(source, "duplicate_class_names")
    result = apply_fix(source, issue, "merge_duplicates")
    assert result.changed == 2
    assert result.message == "Merged 2 duplicate class object(s)."
    assert snapshot(source) == [
        ("6.A", [("Jan", "Novák", "6.A"), ("Eva", "Malá", "6.A")]),
        ("7.B", []),
    ]


def test_apply_fix_remove_empty_deletes_only_student_less_classes(build_source):
    """Classes with students survive."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("7.B", []), ("8.C", [])])
    issue = issue_for(source, "empty_classes")
    result = apply_fix(source, issue, "remove_empty")
    assert result.changed == 2
    assert [c.name for c in source.classes] == ["6.A"]


def test_apply_fix_sync_from_class_overwrites_the_student_record(build_source):
    """The class object wins; only the wrong records are touched."""
    source = build_source([("6.A", [("Jan", "Novák", "7.A"), ("Eva", "Malá")])])
    issue = issue_for(source, "class_person_mismatch")
    result = apply_fix(source, issue, "sync_from_class")
    assert result.changed == 1
    assert result.message == "Updated 1 student record(s)."
    assert [p.class_name for p in source.classes[0].persons] == ["6.A", "6.A"]
    assert source.classes[0].persons[0].get_dirty_fields() == {"class_name"}
    assert source.classes[0].persons[1].is_dirty() is False


def test_apply_fix_move_to_matching_uses_an_existing_class(build_source):
    """The student is moved into the class named in their own record."""
    source = build_source([("6.A", [("Jan", "Novák", "7.A")]), ("7.A", [])])
    issue = issue_for(source, "class_person_mismatch")
    result = apply_fix(source, issue, "move_to_matching")
    assert result.changed == 1
    assert result.message == "Moved 1 student(s) into the matching class."
    assert snapshot(source) == [("6.A", []), ("7.A", [("Jan", "Novák", "7.A")])]


def test_apply_fix_move_to_matching_creates_the_missing_class(build_source):
    """A class named in a record but absent from the source is created."""
    source = build_source([("6.A", [("Jan", "Novák", "7.A")])])
    issue = issue_for(source, "class_person_mismatch")
    apply_fix(source, issue, "move_to_matching")
    assert snapshot(source) == [("6.A", []), ("7.A", [("Jan", "Novák", "7.A")])]


def test_apply_fix_move_to_matching_swaps_two_students_without_looping(build_source):
    """Two students that swapped classes end up in the right place, once."""
    source = build_source([("6.A", [("Jan", "Novák", "7.A")]),
                           ("7.A", [("Eva", "Malá", "6.A")])])
    issue = issue_for(source, "class_person_mismatch")
    result = apply_fix(source, issue, "move_to_matching")
    assert result.changed == 2
    assert snapshot(source) == [("6.A", [("Eva", "Malá", "6.A")]),
                                ("7.A", [("Jan", "Novák", "7.A")])]


def test_apply_fix_move_to_matching_creates_an_unnamed_class_for_a_blank_record(
        build_source):
    """A student without a class name lands in a new unnamed class."""
    source = build_source([("6.A", [("Jan", "Novák", "")])])
    issue = issue_for(source, "class_person_mismatch")
    apply_fix(source, issue, "move_to_matching")
    assert snapshot(source) == [("6.A", []), ("", [("Jan", "Novák", "")])]


def test_apply_fix_remove_incomplete_drops_records_without_a_full_name(build_source):
    """Whitespace-only names count as missing too."""
    source = build_source([("6.A", [("Jan", "Novák"), ("", "Novák"),
                                    ("Jan", "   "), ("", "")])])
    issue = issue_for(source, "missing_person_names")
    result = apply_fix(source, issue, "remove_incomplete")
    assert result.changed == 3
    assert snapshot(source) == [("6.A", [("Jan", "Novák", "6.A")])]


def test_apply_fix_remove_duplicate_persons_keeps_the_first_record(build_source):
    """Case and diacritics are ignored, and duplicates across equally named
    class objects are removed too."""
    source = build_source([("6.A", [("Ján", "Novák"), ("JAN", "NOVAK")]),
                           ("6.A", [("Jan", "Novak", "6.A")])])
    issue = issue_for(source, "duplicate_persons")
    result = apply_fix(source, issue, "remove_duplicate_persons")
    assert result.changed == 2
    assert snapshot(source) == [("6.A", [("Ján", "Novák", "6.A")]), ("6.A", [])]


def test_apply_fix_remove_unparseable_deletes_classes_without_a_number(build_source):
    """The free-text class and its students are dropped."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("Blue class", [("X", "Y")])])
    issue = issue_for(source, "unparseable_class_names")
    result = apply_fix(source, issue, "remove_unparseable")
    assert result.changed == 1
    assert result.message == "Removed 1 class(es) without a number."
    assert snapshot(source) == [("6.A", [("Jan", "Novák", "6.A")])]


@pytest.mark.bug
def test_apply_fix_remove_unparseable_keeps_classes_it_did_not_list(build_source):
    """Only the classes shown to the user may be deleted - unnamed ones are a
    separate issue with its own fixes."""
    source = build_source([("6.A", [("Jan", "Novák")]), ("Blue class", [("X", "Y")]),
                           ("", [("Eva", "Malá", "")])])
    issue = issue_for(source, "unparseable_class_names")
    assert issue.affected == ["Blue class"]

    result = apply_fix(source, issue, "remove_unparseable")

    assert result.changed == 1
    assert snapshot(source) == [("6.A", [("Jan", "Novák", "6.A")]),
                                ("", [("Eva", "Malá", "")])]


@pytest.mark.parametrize("parameter,expected", [
    ("Trieda", ["Trieda 1", "Trieda 2", "6.A"]),
    (None, ["Unnamed 1", "Unnamed 2", "6.A"]),
    ("", ["Unnamed 1", "Unnamed 2", "6.A"]),
    ("   ", ["Unnamed 1", "Unnamed 2", "6.A"]),
    ("  Trieda  ", ["Trieda 1", "Trieda 2", "6.A"]),
])
def test_apply_fix_name_unnamed_numbers_several_classes(build_source, parameter,
                                                        expected):
    """Blank placeholders fall back to 'Unnamed' and are trimmed."""
    source = build_source([("", [("Jan", "Novák", "")]), ("   ", []), ("6.A", [])])
    issue = issue_for(source, "empty_class_names")
    result = apply_fix(source, issue, "name_unnamed", parameter)
    assert result.changed == 2
    assert [c.name for c in source.classes] == expected
    assert source.classes[0].persons[0].class_name == expected[0]


def test_apply_fix_name_unnamed_does_not_number_a_single_class(build_source):
    """One unnamed class gets the plain placeholder, without an index."""
    source = build_source([("", [("Jan", "Novák", "")]), ("6.A", [])])
    issue = issue_for(source, "empty_class_names")
    apply_fix(source, issue, "name_unnamed", "Trieda")
    assert [c.name for c in source.classes] == ["Trieda", "6.A"]


def test_apply_fix_name_unnamed_merges_into_an_existing_placeholder(build_source):
    """When the placeholder name already exists the students are merged in."""
    source = build_source([("", [("Jan", "Novák", "")]),
                           ("Unnamed", [("Eva", "Malá")])])
    issue = issue_for(source, "empty_class_names")
    result = apply_fix(source, issue, "name_unnamed", "Unnamed")
    assert result.changed == 1
    assert snapshot(source) == [("Unnamed", [("Eva", "Malá", "Unnamed"),
                                             ("Jan", "Novák", "Unnamed")])]


def test_apply_fix_remove_unnamed_deletes_blank_named_classes(build_source):
    """Whitespace-only names count as unnamed as well."""
    source = build_source([("", [("Jan", "Novák", "")]), ("   ", []), ("6.A", [])])
    issue = issue_for(source, "empty_class_names")
    result = apply_fix(source, issue, "remove_unnamed")
    assert result.changed == 2
    assert result.message == "Removed 2 unnamed class(es)."
    assert [c.name for c in source.classes] == ["6.A"]


# ===========================================================================
# apply_fix - idempotency
# ===========================================================================

IDEMPOTENCY_CASES = {
    "unify_arabic": ("inconsistent_class_names", None),
    "unify_roman": ("inconsistent_class_names", None),
    "unify_custom": ("inconsistent_class_names", "{arabic}-{letter_upper}"),
    "remove_whitespace": ("whitespace_in_class_names", None),
    "replace_whitespace": ("whitespace_in_class_names", "-"),
    "merge_duplicates": ("duplicate_class_names", None),
    "remove_empty": ("empty_classes", None),
    "sync_from_class": ("class_person_mismatch", None),
    "move_to_matching": ("class_person_mismatch", None),
    "remove_incomplete": ("missing_person_names", None),
    "remove_duplicate_persons": ("duplicate_persons", None),
    "remove_unparseable": ("unparseable_class_names", None),
    "name_unnamed": ("empty_class_names", "Trieda"),
    "remove_unnamed": ("empty_class_names", None),
}


@pytest.mark.parametrize("fix_key", sorted(IDEMPOTENCY_CASES))
def test_apply_fix_is_idempotent(build_source, fix_key):
    """Applying any fix a second time reports zero further changes."""
    issue_key, parameter = IDEMPOTENCY_CASES[fix_key]
    source = build_source([
        ("6. A", [("Jan", "Novák", "9.Z"), ("Jan", "Novák", "9.Z")]),
        ("IX.B", [("", "Malá")]),
        ("IX.B", []),
        ("", [("Peter", "Kováč", "")]),
        ("Blue class", [("X", "Y")]),
    ])
    issue = issue_for(source, issue_key)

    first = apply_fix(source, issue, fix_key, parameter)
    assert first.applied is True and first.changed > 0
    after_first = snapshot(source)

    second = apply_fix(source, issue, fix_key, parameter)
    assert second.applied is True
    assert second.changed == 0
    assert snapshot(source) == after_first


# ===========================================================================
# Chained fixes / integration
# ===========================================================================

@pytest.mark.integration
def test_chained_fixes_always_see_the_data_produced_by_the_previous_one(build_source):
    """Five fixes in a row, each re-analysed, end in a completely clean source."""
    source = build_source([
        ("6. A", [("Jan", "Novák")]),
        ("VI.A", [("Eva", "Malá")]),
        ("9.C", [("Petra", "Ďurišová", "9.D")]),
        ("", []),
        ("Blue class", []),
    ], name="Messy")

    steps = [
        ("whitespace_in_class_names", "remove_whitespace", None),
        ("inconsistent_class_names", "unify_arabic", None),
        ("class_person_mismatch", "sync_from_class", None),
        ("empty_class_names", "remove_unnamed", None),
        ("unparseable_class_names", "remove_unparseable", None),
    ]
    for issue_key, fix_key, parameter in steps:
        issue = issue_for(source, issue_key)
        result = apply_fix(source, issue, fix_key, parameter)
        assert result.applied is True, f"{fix_key} was refused"
        assert result.changed == 1, f"{fix_key} changed {result.changed} item(s)"

    assert snapshot(source) == [
        ("6.A", [("Jan", "Novák", "6.A"), ("Eva", "Malá", "6.A")]),
        ("9.C", [("Petra", "Ďurišová", "9.C")]),
    ]
    assert analyze_source(source).is_clean


@pytest.mark.integration
def test_unifying_both_sides_removes_the_cross_source_style_warning(build_source):
    """The fix offered by the merge analysis really makes the two sides match."""
    left = build_source([("6.A", [("Jan", "Novák")])], name="Left")
    right = build_source([("VI.A", [("Ján", "Novak", "VI.A")])], name="Right")

    issue = issue_of(analyze_merge(left, right, "intersection"),
                     "cross_source_class_style")
    assert issue.target == "both"
    for source in (left, right):
        assert apply_fix(source, issue, "unify_arabic").applied is True

    report = analyze_merge(left, right, "intersection")
    keys = {i.key for i in report.issues}
    assert "cross_source_class_style" not in keys
    assert "no_common_persons" not in keys
    assert report.stats["Students in both"] == 1


@pytest.mark.integration
def test_fixing_the_shift_blocker_makes_the_source_shiftable(build_source):
    """After unifying free-text names nothing_to_shift is gone."""
    source = build_source([("Blue class", [("Jan", "Novák")]),
                           ("Green room", [("Eva", "Malá")])])
    issue = issue_of(analyze_shift(source, 1), "nothing_to_shift")
    assert apply_fix(source, issue, "unify_arabic").changed == 0

    # Free text cannot be unified - the user has to rename the classes instead.
    rename_class(source, source.classes[0], "6.A")
    rename_class(source, source.classes[1], "7.B")

    report = analyze_shift(source, 1)
    assert "nothing_to_shift" not in {i.key for i in report.issues}
    assert report.stats["Classes that will be shifted"] == 2
    assert [p.class_name for p in source.get_all_persons()] == ["6.A", "7.B"]


def test_person_identity_used_by_the_analysis_ignores_case_and_diacritics():
    """Sanity check of the identity the whole merge analysis is built on."""
    left = Person(first_name="Ján", last_name="Novák", class_name="6.A")
    right = Person(first_name="JAN", last_name="novak", class_name="6.A")
    other_class = Person(first_name="Ján", last_name="Novák", class_name="6.B")
    assert left.get_normalized_name() == right.get_normalized_name() == (
        "jan", "novak", "6.A")
    assert left.get_normalized_name() != other_class.get_normalized_name()
