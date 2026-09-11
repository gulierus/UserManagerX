"""
Source analysis engine
======================

Pre-flight checks for the operations offered on the *Comparison and Sync* tab:

* ``Shift Classes (Left/Right Source)``
* ``Merge Both Sources``
* ``Analyze Source (Left/Right Source)``

The module answers two questions for a given data source:

1. **What is wrong with the data?**  Every problem that can make one of the
   operations above fail - or silently produce a useless result - is reported
   as a :class:`SourceIssue`.
2. **How can it be fixed?**  Each issue carries one or more :class:`FixOption`
   objects.  The UI shows them to the user, the user picks one (or none), and
   :func:`apply_fix` performs the change.

Design rules
------------
* No Qt dependency - the engine is pure Python and unit testable.
* Analysis never modifies data.  Only :func:`apply_fix` does.
* Fixes are applied one at a time and the caller is expected to re-run the
  analysis afterwards, so later fixes always see the data produced by the
  earlier ones.
* The same problem always offers the same solutions, no matter which entry
  point (shift / merge / analyse) discovered it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from models import Class, Person, Source
from utils.class_name_utils import (
    ClassNameResult,
    NumeralStyle,
    TEMPLATE_ARABIC,
    TEMPLATE_ROMAN,
    analyze_class_names,
    convert_class_name,
    describe_signature,
    has_inner_whitespace,
    parse_class_name,
    replace_whitespace,
    shift_class_name,
    style_signature,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class IssueSeverity(Enum):
    """How badly an issue affects the requested operation."""

    ERROR = "error"      # operation cannot produce a usable result
    WARNING = "warning"  # operation works but the result may surprise the user
    INFO = "info"        # worth knowing, nothing is broken

    @property
    def icon(self) -> str:
        return {
            IssueSeverity.ERROR: "❌",
            IssueSeverity.WARNING: "⚠️",
            IssueSeverity.INFO: "ℹ️",
        }[self]


class ParameterKind(Enum):
    """Kind of extra input a fix needs from the user."""

    NONE = "none"
    TEXT = "text"        # free text (e.g. a replacement character)
    TEMPLATE = "template"  # a class-name template


@dataclass
class FixOption:
    """
    One possible solution for a :class:`SourceIssue`.

    Attributes:
        key: Stable identifier used by :func:`apply_fix`.
        label: Short text shown on the radio button.
        description: Longer explanation shown under the label.
        parameter_kind: Whether the fix needs extra input from the user.
        parameter_label: Caption for the input field.
        parameter_default: Pre-filled value for the input field.
        is_noop: True for the "change nothing" option.
    """

    key: str
    label: str
    description: str = ""
    parameter_kind: ParameterKind = ParameterKind.NONE
    parameter_label: str = ""
    parameter_default: str = ""
    is_noop: bool = False


@dataclass
class SourceIssue:
    """
    A single problem detected in a source.

    Attributes:
        key: Stable identifier of the problem type.
        title: Short title shown in the issue list.
        severity: How serious the problem is.
        summary: One-line summary including counts.
        detail: Multi-line explanation of what happens if it is not fixed.
        affected: Human readable list of affected items.
        fixes: Available solutions, most recommended first.
        impacts: Names of the operations this issue can break.
        target: Which source a fix must be applied to - ``""`` (the analysed
            source), ``"left"``, ``"right"`` or ``"both"``.  Only used by the
            merge analysis, which inspects two sources at once.
    """

    key: str
    title: str
    severity: IssueSeverity
    summary: str
    detail: str = ""
    affected: List[str] = field(default_factory=list)
    fixes: List[FixOption] = field(default_factory=list)
    impacts: List[str] = field(default_factory=list)
    target: str = ""

    @property
    def is_fixable(self) -> bool:
        """True when at least one fix actually changes something."""
        return any(not fix.is_noop for fix in self.fixes)

    def get_fix(self, key: str) -> Optional[FixOption]:
        """Return the fix with *key*, or ``None``."""
        for fix in self.fixes:
            if fix.key == key:
                return fix
        return None


@dataclass
class AnalysisReport:
    """
    Complete result of analysing one source (or a pair of sources).

    Attributes:
        title: Caption describing what was analysed.
        issues: All detected issues, most severe first.
        stats: Key/value statistics for the summary panel.
    """

    title: str
    issues: List[SourceIssue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> List[SourceIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.ERROR]

    @property
    def warnings(self) -> List[SourceIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.WARNING]

    @property
    def infos(self) -> List[SourceIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.INFO]

    @property
    def has_blocking_issues(self) -> bool:
        return bool(self.errors)

    @property
    def is_clean(self) -> bool:
        """True when nothing at all was found."""
        return not self.issues

    def sort(self) -> None:
        """Order issues by severity (errors first), keeping detection order."""
        order = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1, IssueSeverity.INFO: 2}
        self.issues.sort(key=lambda i: order[i.severity])


@dataclass
class FixResult:
    """Outcome of :func:`apply_fix`."""

    applied: bool
    changed: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# Shared fix definitions
# ---------------------------------------------------------------------------

def _unify_fixes(include_keep: bool = True,
                 keep_label: str = "Keep the names as they are",
                 keep_description: str = "") -> List[FixOption]:
    """
    Build the standard set of solutions for inconsistent class names.

    The very same options are offered by the shift dialog, the merge dialog
    and the source analysis dialog so the user always sees one consistent
    vocabulary.
    """
    fixes = [
        FixOption(
            key="unify_arabic",
            label='Unify all class names to Arabic numerals (e.g. "6.A", "9.B")',
            description=(
                "Every class whose number can be recognised is rewritten to "
                "the '<number>.<LETTER>' form using Arabic digits. Names "
                "without a recognisable number are left untouched."
            ),
        ),
        FixOption(
            key="unify_roman",
            label='Unify all class names to Roman numerals (e.g. "VI.A", "IX.B")',
            description=(
                "Every class whose number can be recognised is rewritten to "
                "the '<ROMAN>.<LETTER>' form. Names without a recognisable "
                "number are left untouched."
            ),
        ),
        FixOption(
            key="unify_custom",
            label="Unify using my own template",
            description=(
                "Build the new class name yourself from placeholders such as "
                "{arabic}, {roman}, {letter_upper}, {prefix} and {suffix}."
            ),
            parameter_kind=ParameterKind.TEMPLATE,
            parameter_label="Template:",
            parameter_default=TEMPLATE_ARABIC,
        ),
    ]
    if include_keep:
        fixes.append(FixOption(
            key="keep",
            label=keep_label,
            description=keep_description or (
                "Nothing is changed. Operations that need the number will "
                "still work on every name in which a number can be found, and "
                "will skip the rest."
            ),
            is_noop=True,
        ))
    return fixes


def _whitespace_fixes() -> List[FixOption]:
    """Standard solutions for whitespace inside standard class names."""
    return [
        FixOption(
            key="remove_whitespace",
            label='Remove all spaces (e.g. "6. A" → "6.A")',
            description=(
                "Every whitespace character inside the affected class names is "
                "deleted. Leading and trailing spaces are removed as well."
            ),
        ),
        FixOption(
            key="replace_whitespace",
            label="Replace the spaces with another character",
            description=(
                'Every whitespace run is replaced with the character you '
                'enter, e.g. "6. A" → "6.-A" when you enter "-".'
            ),
            parameter_kind=ParameterKind.TEXT,
            parameter_label="Replacement:",
            parameter_default="-",
        ),
        FixOption(
            key="keep",
            label="Keep the spaces",
            description="Nothing is changed.",
            is_noop=True,
        ),
    ]


# ---------------------------------------------------------------------------
# Helper operations on the model
# ---------------------------------------------------------------------------

def rename_class(source: Source, cls: Class, new_name: str) -> bool:
    """
    Rename *cls* inside *source*, merging it into an existing class if needed.

    Every person of the class gets the new ``class_name`` so the model stays
    consistent (dirty tracking happens automatically through the property
    setters).

    Args:
        source: Source that owns the class.
        cls: The class to rename.
        new_name: The new class name.

    Returns:
        True when something was actually changed.
    """
    if new_name == cls.name:
        return False

    existing = next(
        (c for c in source.classes if c is not cls and c.name == new_name), None
    )

    for person in cls.persons:
        person.class_name = new_name

    if existing is None:
        cls.name = new_name
        return True

    # Target already exists -> merge the two classes
    existing.persons.extend(cls.persons)
    cls.persons = []
    # Source.remove_class() matches by identity. list.remove() compares by
    # value, and Class is a dataclass, so two empty classes with the same name
    # are "equal" - it could delete the merge TARGET instead of the source.
    before = len(source.classes)
    source.remove_class(cls)
    if len(source.classes) == before:                # pragma: no cover - defensive
        logger.warning("Class %r was not part of source %r", cls.name, source.name)
    logger.info("Merged class %r into existing class %r", cls.name, new_name)
    return True


def apply_template_to_source(source: Source, template: str) -> Tuple[int, List[ClassNameResult]]:
    """
    Re-render every class name of *source* through *template*.

    Args:
        source: Source to modify in place.
        template: Class-name template.

    Returns:
        ``(number_of_changed_classes, per_class_results)``
    """
    results: List[ClassNameResult] = []
    changed = 0

    for cls in list(source.classes):
        result = convert_class_name(cls.name, template)
        results.append(result)
        if result.changed and rename_class(source, cls, result.result):
            changed += 1

    return changed, results


def preview_template_on_names(names: List[str], template: str) -> List[ClassNameResult]:
    """Render *template* against *names* without touching any data."""
    return [convert_class_name(name, template) for name in names]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_empty_source(source: Source) -> Optional[SourceIssue]:
    """The source contains no classes at all."""
    if source.classes:
        return None
    return SourceIssue(
        key="empty_source",
        title="Source contains no classes",
        severity=IssueSeverity.ERROR,
        summary="The source has no classes, so there is nothing to process.",
        detail=(
            "Shifting produces an empty source and merging can only take "
            "persons from the other side. Load or create data first."
        ),
        impacts=["Shift Classes", "Merge Both Sources"],
        fixes=[FixOption(key="keep", label="Continue anyway",
                         description="No data is changed.", is_noop=True)],
    )


def _check_empty_classes(source: Source) -> Optional[SourceIssue]:
    """Classes that contain no persons."""
    empty = [c for c in source.classes if not c.persons]
    if not empty:
        return None
    return SourceIssue(
        key="empty_classes",
        title="Empty classes",
        severity=IssueSeverity.INFO,
        summary=f"{len(empty)} class(es) contain no students.",
        detail=(
            "Empty classes are carried through every operation and show up in "
            "the result as empty entries. They are harmless but usually "
            "unwanted."
        ),
        affected=[c.name for c in empty],
        impacts=["Shift Classes", "Merge Both Sources"],
        fixes=[
            FixOption(key="remove_empty", label="Remove the empty classes",
                      description="The listed classes are deleted from the source."),
            FixOption(key="keep", label="Keep them", description="Nothing is changed.",
                      is_noop=True),
        ],
    )


def _check_duplicate_class_names(source: Source) -> Optional[SourceIssue]:
    """Two or more Class objects that carry the same name."""
    counts: Dict[str, int] = {}
    for cls in source.classes:
        counts[cls.name] = counts.get(cls.name, 0) + 1
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if not duplicates:
        return None
    return SourceIssue(
        key="duplicate_class_names",
        title="Duplicate class names",
        severity=IssueSeverity.WARNING,
        summary=f"{len(duplicates)} class name(s) exist more than once.",
        detail=(
            "Two class objects with the same name make lookups ambiguous: "
            "'find class by name' always returns the first one, so students "
            "of the second class can be skipped or edited in the wrong "
            "place. Shifting can also merge them unexpectedly."
        ),
        affected=duplicates,
        impacts=["Shift Classes", "Merge Both Sources", "Class editing"],
        fixes=[
            FixOption(key="merge_duplicates", label="Merge classes with the same name",
                      description="All students end up in a single class object."),
            FixOption(key="keep", label="Keep them separate",
                      description="Nothing is changed.", is_noop=True),
        ],
    )


def _check_class_person_mismatch(source: Source) -> Optional[SourceIssue]:
    """Persons whose ``class_name`` does not match the class they are stored in."""
    mismatched: List[Tuple[Class, Person]] = []
    for cls in source.classes:
        for person in cls.persons:
            if (person.class_name or "") != cls.name:
                mismatched.append((cls, person))
    if not mismatched:
        return None

    listing = [
        f"{p.first_name} {p.last_name}: stored in '{c.name}', "
        f"class_name = '{p.class_name}'"
        for c, p in mismatched[:50]
    ]
    return SourceIssue(
        key="class_person_mismatch",
        title="Students with a mismatching class name",
        severity=IssueSeverity.WARNING,
        summary=f"{len(mismatched)} student(s) carry a class name that differs "
                f"from the class they are stored in.",
        detail=(
            "Merging groups students by their own class_name, while shifting "
            "and the tree view work with the class object. A mismatch "
            "therefore moves students to a different class as soon as one of "
            "those operations runs."
        ),
        affected=listing,
        impacts=["Shift Classes", "Merge Both Sources"],
        fixes=[
            FixOption(key="sync_from_class",
                      label="Use the class the student is stored in",
                      description="Each student's class_name is overwritten with "
                                  "the name of its class object."),
            FixOption(key="move_to_matching",
                      label="Move each student to the class named in their record",
                      description="Students are moved into the class that matches "
                                  "their class_name; missing classes are created."),
            FixOption(key="keep", label="Leave as it is",
                      description="Nothing is changed.", is_noop=True),
        ],
    )


def _check_missing_person_names(source: Source) -> Optional[SourceIssue]:
    """Persons without a first or last name."""
    incomplete: List[Tuple[Class, Person]] = []
    for cls in source.classes:
        for person in cls.persons:
            first = (person.first_name or "").strip()
            last = (person.last_name or "").strip()
            if not first or not last:
                incomplete.append((cls, person))
    if not incomplete:
        return None

    listing = [
        f"{c.name}: '{p.first_name}' '{p.last_name}'" for c, p in incomplete[:50]
    ]
    return SourceIssue(
        key="missing_person_names",
        title="Students with an incomplete name",
        severity=IssueSeverity.WARNING,
        summary=f"{len(incomplete)} student(s) are missing a first or last name.",
        detail=(
            "Persons are matched by (first name, last name, class) during a "
            "merge, so records without a full name collapse into each other. "
            "Credential generation on the Operations tab also needs both "
            "names."
        ),
        affected=listing,
        impacts=["Merge Both Sources", "Generate credentials"],
        fixes=[
            FixOption(key="remove_incomplete", label="Remove those students",
                      description="The listed records are deleted from the source."),
            FixOption(key="keep", label="Keep them",
                      description="Nothing is changed; they stay in the data.",
                      is_noop=True),
        ],
    )


def _check_duplicate_persons(source: Source) -> Optional[SourceIssue]:
    """Persons that appear more than once under the same identity."""
    seen: Dict[Tuple[str, str, str], int] = {}
    examples: Dict[Tuple[str, str, str], Person] = {}
    for person in source.get_all_persons():
        key = person.get_normalized_name()
        seen[key] = seen.get(key, 0) + 1
        examples.setdefault(key, person)
    duplicates = [key for key, count in seen.items() if count > 1]
    if not duplicates:
        return None

    # get_normalized_name() folds diacritics and case, so printing the key
    # showed "sarka novakova" where the user expects "Šárka Nováková".
    listing = []
    for key in duplicates[:50]:
        person = examples[key]
        listing.append(f"{person.first_name} {person.last_name} "
                       f"({person.class_name}) - {seen[key]}x")
    return SourceIssue(
        key="duplicate_persons",
        title="Duplicate students",
        severity=IssueSeverity.WARNING,
        summary=f"{len(duplicates)} student identity/identities appear more than once.",
        detail=(
            "A merge keys students by (first name, last name, class). Every "
            "duplicate therefore collapses into one record and the extra "
            "copies disappear from the result without notice."
        ),
        affected=listing,
        impacts=["Merge Both Sources"],
        fixes=[
            FixOption(key="remove_duplicate_persons",
                      label="Keep only the first record of each student",
                      description="Later duplicates are removed from the source."),
            FixOption(key="keep", label="Keep all records",
                      description="Nothing is changed.", is_noop=True),
        ],
    )


def _check_class_name_consistency(source: Source) -> List[SourceIssue]:
    """Class-name shape problems: unparseable names, mixed styles, whitespace."""
    issues: List[SourceIssue] = []
    names = [c.name for c in source.classes]
    if not names:
        return issues

    analysis = analyze_class_names(names)

    # --- names in which no number can be found ---------------------------
    if analysis.unparsed:
        all_unparsed = len(analysis.unparsed) == len(names)
        issues.append(SourceIssue(
            key="unparseable_class_names",
            title="Class names without a recognisable number",
            severity=IssueSeverity.ERROR if all_unparsed else IssueSeverity.WARNING,
            summary=(
                f"{len(analysis.unparsed)} of {len(names)} class name(s) contain "
                f"no number that could be shifted or converted."
            ),
            detail=(
                "Shifting and Roman/Arabic conversion both work on the number "
                "inside the class name. Names such as 'Blue class' or 'Window' "
                "have none, so those classes are passed through unchanged.\n"
                + ("Because this applies to EVERY class, shifting this source "
                   "would produce a source without a single shifted class."
                   if all_unparsed else "")
            ),
            affected=list(analysis.unparsed),
            impacts=["Shift Classes", "Convert Class Numerals"],
            fixes=[
                FixOption(key="keep", label="Keep the names and skip those classes",
                          description="The classes stay exactly as they are and are "
                                      "carried over unchanged by every operation.",
                          is_noop=True),
                FixOption(key="remove_unparseable",
                          label="Remove the classes that have no number",
                          description="The listed classes and their students are "
                                      "deleted from the source."),
            ],
        ))

    # --- more than one naming logic --------------------------------------
    real_signatures = {
        sig: members for sig, members in analysis.signatures.items()
        if sig != "free-text"
    }
    if len(real_signatures) > 1:
        affected = []
        for signature, members in sorted(real_signatures.items()):
            sample = ", ".join(members[:6])
            if len(members) > 6:
                sample += ", …"
            affected.append(f"{describe_signature(signature)} → {sample}")

        issues.append(SourceIssue(
            key="inconsistent_class_names",
            title="Class names do not follow one consistent format",
            severity=IssueSeverity.WARNING,
            summary=f"{len(real_signatures)} different class-name formats were found.",
            detail=(
                "Mixed formats (for example '6.A' next to 'IX.B') are handled "
                "correctly by the shift function, but the result keeps the "
                "mixture: '6.A' becomes '7.A' while 'IX.B' becomes 'X.B'. "
                "Sorting, comparing and merging with another source all become "
                "unreliable because the same class can be written in two ways."
            ),
            affected=affected,
            impacts=["Shift Classes", "Merge Both Sources", "Comparison"],
            fixes=_unify_fixes(
                keep_label="Shift / merge without unifying the names",
                keep_description=(
                    "Nothing is renamed. Every class name in which a number can "
                    "be found is still processed correctly - the number is "
                    "changed in place and the surrounding text is preserved, "
                    "e.g. 'Blue class 6.A' → 'Blue class 7.A'."
                ),
            ),
        ))

    # --- whitespace inside otherwise standard names ----------------------
    if analysis.with_whitespace:
        issues.append(SourceIssue(
            key="whitespace_in_class_names",
            title="Class names contain spaces",
            severity=IssueSeverity.WARNING,
            summary=f"{len(analysis.with_whitespace)} standard class name(s) "
                    f"contain one or more spaces.",
            detail=(
                "These names follow the usual '<number>.<letter>' logic but "
                "contain spaces, e.g. '6. A' or ' IX.B '. Spaces make two "
                "otherwise identical classes look different, which produces "
                "duplicate classes after a merge and makes lookups by name "
                "fail.\n"
                "Class names that are not built this way (for example "
                "'Blue class I.A') are not touched by this fix."
            ),
            affected=list(analysis.with_whitespace),
            impacts=["Merge Both Sources", "Class lookup"],
            fixes=_whitespace_fixes(),
        ))

    # --- empty class names -----------------------------------------------
    if analysis.empty:
        issues.append(SourceIssue(
            key="empty_class_names",
            title="Classes without a name",
            severity=IssueSeverity.WARNING,
            summary=f"{len(analysis.empty)} class(es) have an empty name.",
            detail=(
                "An empty class name cannot be looked up, cannot be shifted "
                "and produces an unnamed group in every export."
            ),
            affected=[f"(empty) - {len(c.persons)} student(s)"
                      for c in source.classes if not (c.name or "").strip()],
            impacts=["Shift Classes", "Merge Both Sources", "Export"],
            fixes=[
                FixOption(key="name_unnamed", label="Give them a placeholder name",
                          description="Unnamed classes are renamed to the text you "
                                      "enter, numbered when there is more than one.",
                          parameter_kind=ParameterKind.TEXT,
                          parameter_label="Name:", parameter_default="Unnamed"),
                FixOption(key="remove_unnamed", label="Remove the unnamed classes",
                          description="The classes and their students are deleted."),
                FixOption(key="keep", label="Keep them",
                          description="Nothing is changed.", is_noop=True),
            ],
        ))

    return issues


def _check_readonly(source: Source) -> Optional[SourceIssue]:
    """Read-only sources cannot be edited in place."""
    if not getattr(source, "readonly", False):
        return None
    return SourceIssue(
        key="readonly_source",
        title="Source is read-only",
        severity=IssueSeverity.INFO,
        summary=f"'{source.name}' is marked read-only.",
        detail=(
            "Fixes are always applied to a working copy, so this source itself "
            "is never modified. When you save the result you will be asked for "
            "a new source name."
        ),
        impacts=["Editing"],
        fixes=[FixOption(key="keep", label="Understood",
                         description="No data is changed.", is_noop=True)],
    )


# ---------------------------------------------------------------------------
# Public analysis entry points
# ---------------------------------------------------------------------------

def analyze_source(source: Source, title: Optional[str] = None) -> AnalysisReport:
    """
    Run every general data check against *source*.

    This is the analysis behind the ``Analyze Source`` buttons and is also
    embedded into the shift and merge dialogs.

    Args:
        source: The source to inspect (never modified).
        title: Optional caption for the report.

    Returns:
        A populated :class:`AnalysisReport`.
    """
    report = AnalysisReport(title=title or f"Analysis of '{source.name}'")

    checks: List[Callable[[Source], Optional[SourceIssue]]] = [
        _check_empty_source,
        _check_duplicate_class_names,
        _check_class_person_mismatch,
        _check_duplicate_persons,
        _check_missing_person_names,
        _check_empty_classes,
        _check_readonly,
    ]

    for check in checks:
        try:
            issue = check(source)
        except Exception:                            # pragma: no cover - defensive
            logger.exception("Source check %s failed", getattr(check, "__name__", check))
            continue
        if issue:
            report.issues.append(issue)

    try:
        report.issues.extend(_check_class_name_consistency(source))
    except Exception:                                # pragma: no cover - defensive
        logger.exception("Class-name consistency check failed")

    persons = source.get_all_persons()
    names = [c.name for c in source.classes]
    analysis = analyze_class_names(names)
    report.stats = {
        "Classes": len(source.classes),
        "Students": len(persons),
        "Class names with a number": sum(1 for p in analysis.parsed if p.has_numeral),
        "Class-name formats": len([s for s in analysis.signatures if s != "free-text"]),
        "Dominant numeral style": analysis.dominant_style.label,
        "Read-only": "yes" if getattr(source, "readonly", False) else "no",
    }
    report.sort()
    return report


def analyze_shift(source: Source, delta: int = 1, *,
                  graduation_year: Optional[int] = 9,
                  remove_graduating: bool = True) -> AnalysisReport:
    """
    Analyse a source specifically for the *Shift Classes* operation.

    On top of the general checks this adds the problems that only appear when
    the numbers are actually moved: nothing to shift at all, name collisions
    after the shift, and classes that leave the school.

    Args:
        source: Source that would be shifted.
        delta: Number of years to shift by.
        graduation_year: Year that graduates, or ``None``.
        remove_graduating: Whether the graduating year is removed.

    Returns:
        A populated :class:`AnalysisReport`.
    """
    report = analyze_source(source, title=f"Shift analysis of '{source.name}'")

    results = [
        shift_class_name(cls.name, delta, graduation_year=graduation_year,
                         remove_graduating=remove_graduating)
        for cls in source.classes
    ]

    shiftable = [r for r in results if r.changed]
    removed = [r for r in results if r.removed]
    skipped = [r for r in results if not r.recognised]
    blocked = [r for r in results if r.recognised and r.skipped and not r.removed]

    # --- the user asked for no movement at all ----------------------------
    if delta == 0:
        report.issues.insert(0, SourceIssue(
            key="no_shift_requested",
            title="The shift is set to 0 years",
            severity=IssueSeverity.WARNING,
            summary="No class would change because the shift amount is zero.",
            detail=(
                "Set the shift amount to a positive number to move the classes "
                "up, or to a negative number to move them back."
            ),
            impacts=["Shift Classes"],
            fixes=[FixOption(key="keep", label="I will change the amount",
                             description="No data is changed.", is_noop=True)],
        ))
        report.stats.update({
            "Classes that will be shifted": 0,
            "Classes that will be removed": len(removed),
            "Classes without a number (skipped)": len(skipped),
        })
        report.sort()
        return report

    # --- nothing at all would be shifted ---------------------------------
    if source.classes and not shiftable and not removed:
        report.issues.insert(0, SourceIssue(
            key="nothing_to_shift",
            title="No class can be shifted",
            severity=IssueSeverity.ERROR,
            summary="None of the class names contains a number that can be shifted.",
            detail=(
                "The new source would contain the same class names as the "
                "original one - or, in older versions of this application, no "
                "classes at all. Unify the class names first, or rename the "
                "classes so that they contain a year number."
            ),
            affected=[r.original for r in results],
            impacts=["Shift Classes"],
            fixes=_unify_fixes(
                keep_label="Continue anyway",
                keep_description="The shift runs but changes nothing.",
            ),
        ))

    # --- collisions ------------------------------------------------------
    final_names: Dict[str, List[str]] = {}
    for result in results:
        if result.removed:
            continue
        final_names.setdefault(result.result, []).append(result.original)
    collisions = {name: sources for name, sources in final_names.items() if len(sources) > 1}

    if collisions:
        report.issues.insert(0, SourceIssue(
            key="shift_collision",
            title="Different classes would get the same name",
            severity=IssueSeverity.WARNING,
            summary=f"{len(collisions)} class name(s) would be produced by more "
                    f"than one class.",
            detail=(
                "Example: '6.A' and 'VI.A' are the same class written in two "
                "ways; after the shift both become the same class. Their "
                "students are then put together into one class.\n"
                "Unifying the names first makes the result predictable."
            ),
            affected=[f"{' + '.join(origins)} → {name}"
                      for name, origins in sorted(collisions.items())],
            impacts=["Shift Classes"],
            fixes=_unify_fixes(
                keep_label="Merge the colliding classes",
                keep_description="Students of all colliding classes end up in one "
                                 "class with the shared name.",
            ),
        ))

    # --- graduating classes ----------------------------------------------
    if removed:
        student_count = sum(
            len(c.persons) for c in source.classes
            if any(r.original == c.name and r.removed for r in results)
        )
        report.issues.append(SourceIssue(
            key="graduating_classes",
            title="Graduating classes will be removed",
            severity=IssueSeverity.INFO,
            summary=f"{len(removed)} class(es) with {student_count} student(s) "
                    f"reach year {graduation_year} and leave the school.",
            detail=(
                "Classes at the graduation year are not shifted; they are "
                "dropped from the new source. Use the option in the shift "
                "dialog if you want to keep and shift them instead."
            ),
            affected=[r.original for r in removed],
            impacts=["Shift Classes"],
            fixes=[FixOption(key="keep", label="That is what I want",
                             description="No data is changed now; the classes are "
                                         "dropped when the shift runs.",
                             is_noop=True)],
        ))

    # --- classes that cannot move ----------------------------------------
    if blocked:
        report.issues.append(SourceIssue(
            key="shift_out_of_range",
            title="Some classes cannot be shifted",
            severity=IssueSeverity.WARNING,
            summary=f"{len(blocked)} class(es) would end up outside the allowed "
                    f"year range.",
            detail="Those classes are carried over unchanged.",
            affected=[f"{r.original}: {r.message}" for r in blocked],
            impacts=["Shift Classes"],
            fixes=[FixOption(key="keep", label="Carry them over unchanged",
                             description="Nothing is changed.", is_noop=True)],
        ))

    report.stats.update({
        "Classes that will be shifted": len(shiftable),
        "Classes that will be removed": len(removed),
        "Classes without a number (skipped)": len(skipped),
    })
    report.sort()
    return report


def analyze_merge(left: Source, right: Source, strategy: str) -> AnalysisReport:
    """
    Analyse a pair of sources for the *Merge Both Sources* operation.

    Args:
        left: Left source.
        right: Right source.
        strategy: ``'union'`` or ``'intersection'``.

    Returns:
        A populated :class:`AnalysisReport`.
    """
    report = AnalysisReport(title=f"Merge analysis: '{left.name}' + '{right.name}'")

    if left is right:
        report.issues.append(SourceIssue(
            key="same_source_twice",
            title="Both sides are the same source",
            severity=IssueSeverity.WARNING,
            summary="The left and the right panel point to the same source.",
            detail=(
                "A union produces an exact copy, an intersection produces the "
                "same copy as well. Select two different sources to get a "
                "meaningful result."
            ),
            impacts=["Merge Both Sources"],
            fixes=[FixOption(key="keep", label="Continue anyway",
                             description="No data is changed.", is_noop=True)],
        ))

    # Per-source data problems are relevant for the merge too.
    for side, source in (("Left", left), ("Right", right)):
        sub = analyze_source(source)
        for issue in sub.issues:
            if issue.key in ("readonly_source", "empty_classes"):
                continue
            if "Merge Both Sources" not in issue.impacts:
                continue
            clone = SourceIssue(
                key=issue.key,
                title=f"{side} source: {issue.title}",
                severity=issue.severity,
                summary=issue.summary,
                detail=issue.detail,
                affected=issue.affected,
                fixes=issue.fixes,
                impacts=issue.impacts,
                target=side.lower(),
            )
            report.issues.append(clone)

    left_persons = left.get_all_persons()
    right_persons = right.get_all_persons()

    # --- empty sides ------------------------------------------------------
    for side, persons, source in (("Left", left_persons, left), ("Right", right_persons, right)):
        if not persons:
            report.issues.append(SourceIssue(
                key="empty_merge_side",
                title=f"{side} source has no students",
                severity=(IssueSeverity.ERROR if strategy == "intersection"
                          else IssueSeverity.WARNING),
                summary=f"'{source.name}' contains no students.",
                detail=(
                    "An intersection with an empty source is always empty."
                    if strategy == "intersection" else
                    "A union simply returns the other source."
                ),
                impacts=["Merge Both Sources"],
                fixes=[FixOption(key="keep", label="Continue anyway",
                                 description="No data is changed.", is_noop=True)],
            ))

    # --- class-name style mismatch between the two sides ------------------
    left_analysis = analyze_class_names([c.name for c in left.classes])
    right_analysis = analyze_class_names([c.name for c in right.classes])
    left_styles = {s for s in left_analysis.signatures if s != "free-text"}
    right_styles = {s for s in right_analysis.signatures if s != "free-text"}

    if left_styles and right_styles and not (left_styles & right_styles):
        report.issues.append(SourceIssue(
            key="cross_source_class_style",
            title="The two sources name their classes differently",
            severity=IssueSeverity.WARNING,
            summary="No class-name format is shared by both sources.",
            detail=(
                "Students are matched by (first name, last name, class name). "
                "When the same class is called '6.A' on one side and 'VI.A' on "
                "the other, the very same student counts as two different "
                "people: a union keeps both records and an intersection finds "
                "nothing at all.\n"
                "Unify the class names of both sources first."
            ),
            affected=[
                f"Left: {', '.join(sorted(describe_signature(s) for s in left_styles))}",
                f"Right: {', '.join(sorted(describe_signature(s) for s in right_styles))}",
            ],
            impacts=["Merge Both Sources"],
            target="both",
            fixes=_unify_fixes(
                keep_label="Merge without unifying",
                keep_description="The result contains both spellings of each class.",
            ),
        ))

    # --- overlap statistics ----------------------------------------------
    left_keys = {p.get_normalized_name() for p in left_persons}
    right_keys = {p.get_normalized_name() for p in right_persons}
    common = left_keys & right_keys

    if strategy == "intersection" and not common and (left_persons and right_persons):
        report.issues.append(SourceIssue(
            key="no_common_persons",
            title="The two sources have no student in common",
            severity=IssueSeverity.ERROR,
            summary="An intersection would produce an empty source.",
            detail=(
                "Students are compared by first name, last name and class "
                "name, ignoring case and diacritics. If you expected an "
                "overlap, the class names most likely differ between the two "
                "sources - unify them and try again."
            ),
            impacts=["Merge Both Sources"],
            target="both",
            fixes=_unify_fixes(
                keep_label="Continue anyway",
                keep_description="The merge runs and produces an empty source.",
            ),
        ))

    # --- same person, different class ------------------------------------
    left_by_person = {}
    for person in left_persons:
        key = (Person.normalize_string(person.first_name),
               Person.normalize_string(person.last_name))
        left_by_person.setdefault(key, set()).add(person.class_name)

    moved: List[str] = []
    for person in right_persons:
        key = (Person.normalize_string(person.first_name),
               Person.normalize_string(person.last_name))
        classes = left_by_person.get(key)
        if classes and person.class_name not in classes:
            moved.append(
                f"{person.first_name} {person.last_name}: "
                f"{', '.join(sorted(classes))} (left) vs {person.class_name} (right)"
            )

    if moved:
        report.issues.append(SourceIssue(
            key="same_person_different_class",
            title="The same student appears in different classes",
            severity=IssueSeverity.WARNING,
            summary=f"{len(moved)} student(s) are in one class on the left and in "
                    f"another class on the right.",
            detail=(
                "Because the class is part of the identity, such a student is "
                "treated as two different people: a union keeps both records "
                "(the student appears twice, once per class) and an "
                "intersection drops the student completely.\n"
                "This is normal when one source is a year older than the "
                "other - shift the older source first so both sides use the "
                "same school year."
            ),
            affected=moved[:50],
            impacts=["Merge Both Sources"],
            fixes=[FixOption(key="keep", label="Continue - this is expected",
                             description="No data is changed.", is_noop=True)],
        ))

    # --- conflicting AD data for identical students -----------------------
    right_index = {p.get_normalized_name(): p for p in right_persons}
    conflicts: List[str] = []
    for person in left_persons:
        other = right_index.get(person.get_normalized_name())
        if other is None:
            continue
        for attribute in ("ad_username", "ad_email", "ad_display_name"):
            left_value = getattr(person, attribute, None)
            right_value = getattr(other, attribute, None)
            if left_value and right_value and left_value != right_value:
                conflicts.append(
                    f"{person.first_name} {person.last_name} - {attribute}: "
                    f"'{left_value}' vs '{right_value}'"
                )

    if conflicts:
        report.issues.append(SourceIssue(
            key="conflicting_person_data",
            title="Students exist on both sides with different data",
            severity=IssueSeverity.INFO,
            summary=f"{len(conflicts)} attribute value(s) differ between the two sources.",
            detail=(
                "Both merge strategies keep the record of the LEFT source, so "
                "the values shown on the right are discarded. Swap the panels "
                "if you would rather keep the right-hand values."
            ),
            affected=conflicts[:50],
            impacts=["Merge Both Sources"],
            fixes=[FixOption(key="keep", label="Keep the left-hand values",
                             description="No data is changed.", is_noop=True)],
        ))

    report.stats = {
        "Left students": len(left_persons),
        "Right students": len(right_persons),
        "Students only in left": len(left_keys - right_keys),
        "Students only in right": len(right_keys - left_keys),
        "Students in both": len(common),
        "Result with 'union'": len(left_keys | right_keys),
        "Result with 'intersection'": len(common),
    }
    report.sort()
    return report


# ---------------------------------------------------------------------------
# Fix application
# ---------------------------------------------------------------------------

def _fix_unify(source: Source, template: str) -> FixResult:
    changed, results = apply_template_to_source(source, template)
    unrecognised = sum(1 for r in results if not r.recognised)
    message = f"Renamed {changed} class(es)."
    if unrecognised:
        message += f" {unrecognised} class name(s) had no number and were left unchanged."
    return FixResult(applied=True, changed=changed, message=message)


def _fix_whitespace(source: Source, replacement: str) -> FixResult:
    changed = 0
    for cls in list(source.classes):
        parts = parse_class_name(cls.name)
        if not (parts.is_standard and has_inner_whitespace(cls.name)):
            continue
        new_name = replace_whitespace(cls.name, replacement)
        if new_name and new_name != cls.name and rename_class(source, cls, new_name):
            changed += 1
    return FixResult(applied=True, changed=changed,
                     message=f"Cleaned {changed} class name(s).")


def _fix_merge_duplicate_classes(source: Source) -> FixResult:
    by_name: Dict[str, Class] = {}
    merged = 0
    for cls in list(source.classes):
        first = by_name.get(cls.name)
        if first is None:
            by_name[cls.name] = cls
            continue
        first.persons.extend(cls.persons)
        cls.persons = []
        source.classes.remove(cls)
        merged += 1
    return FixResult(applied=True, changed=merged,
                     message=f"Merged {merged} duplicate class object(s).")


def _fix_remove_empty_classes(source: Source) -> FixResult:
    empty = [c for c in source.classes if not c.persons]
    for cls in empty:
        source.classes.remove(cls)
    return FixResult(applied=True, changed=len(empty),
                     message=f"Removed {len(empty)} empty class(es).")


def _fix_sync_class_names(source: Source) -> FixResult:
    changed = 0
    for cls in source.classes:
        for person in cls.persons:
            if (person.class_name or "") != cls.name:
                person.class_name = cls.name
                changed += 1
    return FixResult(applied=True, changed=changed,
                     message=f"Updated {changed} student record(s).")


def _fix_move_to_matching_class(source: Source) -> FixResult:
    moved = 0
    for cls in list(source.classes):
        for person in list(cls.persons):
            target_name = person.class_name or ""
            if target_name == cls.name:
                continue
            target = source.find_class_by_name(target_name)
            if target is None:
                target = Class(name=target_name)
                source.add_class(target)
            cls.persons.remove(person)
            target.persons.append(person)
            moved += 1
    return FixResult(applied=True, changed=moved,
                     message=f"Moved {moved} student(s) into the matching class.")


def _fix_remove_incomplete_persons(source: Source) -> FixResult:
    removed = 0
    for cls in source.classes:
        keep = []
        for person in cls.persons:
            if (person.first_name or "").strip() and (person.last_name or "").strip():
                keep.append(person)
            else:
                removed += 1
        cls.persons = keep
    return FixResult(applied=True, changed=removed,
                     message=f"Removed {removed} incomplete student record(s).")


def _fix_remove_duplicate_persons(source: Source) -> FixResult:
    seen = set()
    removed = 0
    for cls in source.classes:
        keep = []
        for person in cls.persons:
            key = person.get_normalized_name()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            keep.append(person)
        cls.persons = keep
    return FixResult(applied=True, changed=removed,
                     message=f"Removed {removed} duplicate student record(s).")


def _fix_remove_unparseable_classes(source: Source) -> FixResult:
    # Only the classes the issue actually listed: a class with an EMPTY name
    # also has no numeral, but it belongs to the 'empty_class_names' issue and
    # has its own solutions. Deleting it here destroyed students the user was
    # never shown.
    victims = [c for c in source.classes
               if (c.name or "").strip() and not parse_class_name(c.name).has_numeral]
    for cls in victims:
        source.classes.remove(cls)
    return FixResult(applied=True, changed=len(victims),
                     message=f"Removed {len(victims)} class(es) without a number.")


def _fix_name_unnamed_classes(source: Source, name: str) -> FixResult:
    base = (name or "Unnamed").strip() or "Unnamed"
    unnamed = [c for c in source.classes if not (c.name or "").strip()]
    changed = 0
    for index, cls in enumerate(unnamed, start=1):
        new_name = base if len(unnamed) == 1 else f"{base} {index}"
        if rename_class(source, cls, new_name):
            changed += 1
    return FixResult(applied=True, changed=changed,
                     message=f"Named {changed} class(es).")


def _fix_remove_unnamed_classes(source: Source) -> FixResult:
    victims = [c for c in source.classes if not (c.name or "").strip()]
    for cls in victims:
        source.classes.remove(cls)
    return FixResult(applied=True, changed=len(victims),
                     message=f"Removed {len(victims)} unnamed class(es).")


def apply_fix(source: Source, issue: SourceIssue, fix_key: str,
              parameter: Optional[str] = None) -> FixResult:
    """
    Apply the chosen solution of *issue* to *source*.

    The source is modified in place; callers that must not touch the original
    data are expected to pass a working copy.

    Args:
        source: Source to modify.
        issue: The issue the user is fixing.
        fix_key: Key of the chosen :class:`FixOption`.
        parameter: Extra input for fixes that need one.

    Returns:
        A :class:`FixResult` describing what happened.
    """
    fix = issue.get_fix(fix_key)
    if fix is None:
        return FixResult(applied=False, message=f"Unknown solution '{fix_key}'.")
    if fix.is_noop:
        return FixResult(applied=True, changed=0, message="No change requested.")

    try:
        if fix_key == "unify_arabic":
            return _fix_unify(source, TEMPLATE_ARABIC)
        if fix_key == "unify_roman":
            return _fix_unify(source, TEMPLATE_ROMAN)
        if fix_key == "unify_custom":
            return _fix_unify(source, parameter or TEMPLATE_ARABIC)
        if fix_key == "remove_whitespace":
            return _fix_whitespace(source, "")
        if fix_key == "replace_whitespace":
            return _fix_whitespace(source, parameter if parameter is not None else "-")
        if fix_key == "merge_duplicates":
            return _fix_merge_duplicate_classes(source)
        if fix_key == "remove_empty":
            return _fix_remove_empty_classes(source)
        if fix_key == "sync_from_class":
            return _fix_sync_class_names(source)
        if fix_key == "move_to_matching":
            return _fix_move_to_matching_class(source)
        if fix_key == "remove_incomplete":
            return _fix_remove_incomplete_persons(source)
        if fix_key == "remove_duplicate_persons":
            return _fix_remove_duplicate_persons(source)
        if fix_key == "remove_unparseable":
            return _fix_remove_unparseable_classes(source)
        if fix_key == "name_unnamed":
            return _fix_name_unnamed_classes(source, parameter or "Unnamed")
        if fix_key == "remove_unnamed":
            return _fix_remove_unnamed_classes(source)
    except Exception as exc:
        logger.exception("Failed to apply fix %s for issue %s", fix_key, issue.key)
        return FixResult(applied=False, message=f"Could not apply the fix: {exc}")

    return FixResult(applied=False, message=f"Solution '{fix_key}' is not implemented.")
