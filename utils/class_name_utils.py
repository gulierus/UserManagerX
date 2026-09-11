"""
Class name utilities
====================

Central engine for everything the application needs to do with *class names*.

Class names are stored as plain strings for compatibility reasons, which means
a user - or an external system such as EduPage - may put literally anything in
them::

    "6.A"             Arabic numeral, dot separator, section letter
    "IX.B"            Roman numeral, dot separator, section letter
    "9A"              Arabic numeral, no separator
    "III. C"          Roman numeral, dot + space separator
    "IX."             Roman numeral, dot, *no* section letter
    "Blue class 6.A"  a standard name wrapped in descriptive text
    "My sixth A"      no numeral at all
    "Window"          arbitrary text

Every public function in this module is *total*: it never raises for unusual
input.  Instead it reports what it was able to recognise and what it was not,
so the calling UI can present the user with an informed choice.

The module deliberately has **no Qt dependency** so it can be unit tested and
reused from background threads.

Main entry points
-----------------
``parse_class_name(name)``
    Split an arbitrary class name into ``prefix / numeral / separator /
    letter / suffix``.

``convert_class_name(name, template)``
    Re-render a class name through a placeholder template - this is what the
    Roman <-> Arabic conversion feature uses.

``shift_class_name(name, delta)``
    Move the numeral of a class name up or down while preserving every other
    character of the original string.

``analyze_class_names(names)``
    Report on the consistency of a whole collection of class names.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roman numerals
# ---------------------------------------------------------------------------

#: Highest value the application is willing to treat as a class numeral.
#: School years never come close to this, but the limit keeps the conversion
#: functions from producing absurd output for accidental matches.
MAX_NUMERAL_VALUE = 3999

#: Largest value a *Roman* token may have before it is treated as ordinary
#: text instead of a class numeral.  Without this cap, English words made up
#: of Roman letters (``"MIX"`` = 1009, ``"CIVIC"``...) would be silently
#: interpreted as class years.  Arabic numerals are unambiguous and are not
#: capped here.
MAX_PLAUSIBLE_ROMAN_VALUE = 40

_ROMAN_TO_ARABIC: Dict[str, int] = {
    'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000
}

_ARABIC_TO_ROMAN: Tuple[Tuple[int, str], ...] = (
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
    (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
    (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
)


def int_to_roman(value: int) -> str:
    """
    Convert a positive integer into its canonical Roman representation.

    Args:
        value: Integer in the range 1 .. :data:`MAX_NUMERAL_VALUE`.

    Returns:
        Upper-case Roman numeral, e.g. ``9 -> "IX"``.

    Raises:
        ValueError: If *value* is outside the supported range.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Roman conversion needs an int, got {type(value).__name__}")
    if value < 1 or value > MAX_NUMERAL_VALUE:
        raise ValueError(f"Value {value} out of range (1-{MAX_NUMERAL_VALUE})")

    remaining = value
    out: List[str] = []
    for amount, glyph in _ARABIC_TO_ROMAN:
        while remaining >= amount:
            out.append(glyph)
            remaining -= amount
    return ''.join(out)


def roman_to_int(text: str) -> Optional[int]:
    """
    Convert a Roman numeral to an integer.

    Only *canonical* numerals are accepted: the round trip
    ``int_to_roman(roman_to_int(x)) == x.upper()`` must hold.  This rejects
    look-alikes such as ``"IIII"``, ``"IC"`` or ordinary words that happen to
    consist of Roman letters (e.g. ``"DILL"``), which keeps the parser from
    mangling free-form class names.

    Args:
        text: Candidate Roman numeral (case insensitive).

    Returns:
        The integer value, or ``None`` if *text* is not a canonical numeral.
    """
    if not text or not isinstance(text, str):
        return None

    candidate = text.strip().upper()
    if not candidate or any(ch not in _ROMAN_TO_ARABIC for ch in candidate):
        return None

    total = 0
    previous = 0
    for char in reversed(candidate):
        current = _ROMAN_TO_ARABIC[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current

    if total < 1 or total > MAX_NUMERAL_VALUE:
        return None

    # Canonical check - rejects "IIII", "VV", "IC", ...
    try:
        if int_to_roman(total) != candidate:
            return None
    except ValueError:
        return None

    return total


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class NumeralStyle(Enum):
    """Which notation a class numeral is written in."""

    ARABIC = "arabic"
    ROMAN = "roman"
    NONE = "none"

    @property
    def label(self) -> str:
        return {
            NumeralStyle.ARABIC: "Arabic (6, 9)",
            NumeralStyle.ROMAN: "Roman (VI, IX)",
            NumeralStyle.NONE: "no numeral",
        }[self]


# Letters that may form the "section" part of a class name.  Includes accented
# characters because national alphabets are used in practice.
_LETTER_CHARS = r'A-Za-zÀ-ɏ'

# A section token is at most three letters and must not be glued to more text.
_SECTION_RE = re.compile(rf'([{_LETTER_CHARS}]{{1,3}})(?![{_LETTER_CHARS}])')

# Characters that are accepted between the numeral and the section letter.
_SEPARATOR_RE = re.compile(r'[\s.\-–—_/:,;|]*')

_ARABIC_RE = re.compile(r'\d+')

# A standalone Roman token: a run of Roman letters not glued to other letters.
_ROMAN_TOKEN_RE = re.compile(
    rf'(?<![{_LETTER_CHARS}])([IVXLCDMivxlcdm]+)(?![{_LETTER_CHARS}])'
)

# Fallback for names such as "IXA" where the numeral and the section letter are
# written without any separator at all.
_GLUED_RE = re.compile(rf'^([IVXLCDMivxlcdm]+)([{_LETTER_CHARS}]{{1,2}})$')

#: Maximum length of a name that the "glued Roman" heuristic is applied to.
_GLUED_MAX_LEN = 6


@dataclass(frozen=True)
class ClassNameParts:
    """
    Structural decomposition of a class name.

    The original string can always be rebuilt as::

        prefix + numeral_text + separator + letter + suffix

    Attributes:
        original: The unmodified input string.
        prefix: Everything before the numeral (often empty).
        numeral_text: The numeral exactly as written (``"6"``, ``"IX"``, ``""``).
        numeral_value: Numeric value of the numeral, or ``None``.
        numeral_style: Whether the numeral was Arabic, Roman or absent.
        separator: Characters between numeral and section letter.
        letter: The section token (``"A"``, ``"B"``, ``""``).
        suffix: Everything after the section letter.
    """

    original: str
    prefix: str = ""
    numeral_text: str = ""
    numeral_value: Optional[int] = None
    numeral_style: NumeralStyle = NumeralStyle.NONE
    separator: str = ""
    letter: str = ""
    suffix: str = ""

    # -- queries ---------------------------------------------------------

    @property
    def has_numeral(self) -> bool:
        """True when a numeral was recognised."""
        return self.numeral_value is not None

    @property
    def has_letter(self) -> bool:
        """True when a section letter was recognised."""
        return bool(self.letter)

    @property
    def is_standard(self) -> bool:
        """
        True for names built purely as ``numeral + separator + letter``.

        ``"6.A"``, ``"IX. B"`` and ``"9A"`` are standard;
        ``"Blue class 6.A"`` and ``"Window"`` are not.
        """
        return (
            self.has_numeral
            and not self.prefix.strip()
            and not self.suffix.strip()
        )

    @property
    def has_surrounding_text(self) -> bool:
        """True when free-form text surrounds the recognised numeral."""
        return bool(self.prefix.strip() or self.suffix.strip())

    @property
    def is_roman_lowercase(self) -> bool:
        """True when the Roman numeral was written in lower case."""
        return (
            self.numeral_style is NumeralStyle.ROMAN
            and self.numeral_text.islower()
        )

    def rebuild(self) -> str:
        """Reassemble the original string from its parts (round-trip check)."""
        return (
            f"{self.prefix}{self.numeral_text}{self.separator}"
            f"{self.letter}{self.suffix}"
        )

    def describe(self) -> str:
        """Human readable summary used by preview dialogs."""
        if not self.has_numeral:
            return "no numeral recognised"
        bits = [f"{self.numeral_style.label} = {self.numeral_value}"]
        if self.has_letter:
            bits.append(f"section '{self.letter}'")
        else:
            bits.append("no section letter")
        if self.has_surrounding_text:
            bits.append("extra text around numeral")
        return ", ".join(bits)


def _find_roman_span(text: str) -> Optional[Tuple[int, int, int]]:
    """
    Locate the first standalone canonical Roman numeral.

    Returns:
        ``(start, end, value)`` or ``None``.
    """
    for match in _ROMAN_TOKEN_RE.finditer(text):
        value = roman_to_int(match.group(1))
        if value is not None and value <= MAX_PLAUSIBLE_ROMAN_VALUE:
            return match.start(1), match.end(1), value
    return None


def _find_arabic_span(text: str) -> Optional[Tuple[int, int, int]]:
    """
    Locate the first Arabic numeral.

    Returns:
        ``(start, end, value)`` or ``None``.
    """
    match = _ARABIC_RE.search(text)
    if not match:
        return None
    try:
        value = int(match.group(0))
    except ValueError:                              # pragma: no cover - defensive
        return None
    if value < 0 or value > MAX_NUMERAL_VALUE:
        return None
    return match.start(), match.end(), value


def parse_class_name(name: Optional[str]) -> ClassNameParts:
    """
    Split an arbitrary class name into its structural parts.

    The function never raises.  When no numeral can be found, the whole input
    ends up in :attr:`ClassNameParts.prefix` and
    :attr:`ClassNameParts.has_numeral` is ``False``.

    Args:
        name: Class name as stored on the model (may be ``None``).

    Returns:
        A :class:`ClassNameParts` instance whose :meth:`ClassNameParts.rebuild`
        is guaranteed to reproduce the input exactly.
    """
    original = name if isinstance(name, str) else ("" if name is None else str(name))

    if not original.strip():
        return ClassNameParts(original=original, prefix=original)

    arabic = _find_arabic_span(original)
    roman = _find_roman_span(original)

    chosen: Optional[Tuple[int, int, int]] = None
    style = NumeralStyle.NONE

    if arabic and roman:
        # Whichever notation appears first wins; a tie is impossible because the
        # character classes are disjoint.
        if arabic[0] <= roman[0]:
            chosen, style = arabic, NumeralStyle.ARABIC
        else:
            chosen, style = roman, NumeralStyle.ROMAN
    elif arabic:
        chosen, style = arabic, NumeralStyle.ARABIC
    elif roman:
        chosen, style = roman, NumeralStyle.ROMAN

    if chosen is None:
        # Last resort: names such as "IXA" where numeral and section letter are
        # written without a separator.  Restricted to short, purely alphabetic
        # strings so ordinary words are not mangled.
        stripped = original.strip()
        if len(stripped) <= _GLUED_MAX_LEN:
            glued = _GLUED_RE.match(stripped)
            if glued:
                value = roman_to_int(glued.group(1))
                if value is not None and value <= MAX_PLAUSIBLE_ROMAN_VALUE:
                    lead = original[:original.index(stripped)]
                    trail = original[original.index(stripped) + len(stripped):]
                    return ClassNameParts(
                        original=original,
                        prefix=lead,
                        numeral_text=glued.group(1),
                        numeral_value=value,
                        numeral_style=NumeralStyle.ROMAN,
                        separator="",
                        letter=glued.group(2),
                        suffix=trail,
                    )
        return ClassNameParts(original=original, prefix=original)

    start, end, value = chosen
    prefix = original[:start]
    numeral_text = original[start:end]
    rest = original[end:]

    sep_match = _SEPARATOR_RE.match(rest)
    separator = sep_match.group(0) if sep_match else ""
    after_sep = rest[len(separator):]

    letter = ""
    section_match = _SECTION_RE.match(after_sep)
    if section_match:
        letter = section_match.group(1)
        suffix = after_sep[len(letter):]
    else:
        # No section letter - the separator characters are really a suffix
        # unless they are pure punctuation directly attached to the numeral
        # (e.g. the EduPage style "IX." which has a trailing dot).
        if separator and not separator.strip():
            # Separator was whitespace only -> treat it as suffix
            suffix = separator + after_sep
            separator = ""
        else:
            suffix = after_sep

    return ClassNameParts(
        original=original,
        prefix=prefix,
        numeral_text=numeral_text,
        numeral_value=value,
        numeral_style=style,
        separator=separator,
        letter=letter,
        suffix=suffix,
    )


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

class TemplateError(ValueError):
    """Raised when a class-name template cannot be used."""


_PLACEHOLDER_RE = re.compile(r'\{([a-z_]+)(?::([^{}]*))?\}')

#: Template that normalises every recognised name to ``"IX.B"`` style.
TEMPLATE_ROMAN = "{roman}.{letter_upper}"

#: Template that normalises every recognised name to ``"9.B"`` style.
TEMPLATE_ARABIC = "{arabic}.{letter_upper}"

#: Ready-made templates offered in the UI: ``key -> (label, template, help)``.
PREDEFINED_TEMPLATES: Dict[str, Tuple[str, str, str]] = {
    "roman": (
        "Unified Roman numerals",
        TEMPLATE_ROMAN,
        'Rewrites every recognised class name to Roman style, e.g. '
        '"6.A" -> "VI.A", "9 b" -> "IX.B", "IX.B" -> "IX.B".',
    ),
    "arabic": (
        "Unified Arabic numerals",
        TEMPLATE_ARABIC,
        'Rewrites every recognised class name to Arabic style, e.g. '
        '"VI.A" -> "6.A", "IX. b" -> "9.B", "6.A" -> "6.A".',
    ),
}

#: ``placeholder -> description`` used to build the in-dialog help text.
TEMPLATE_PLACEHOLDERS: Dict[str, str] = {
    "arabic": "Class number in Arabic notation (6, 9). Optional width: {arabic:2} -> 06",
    "roman": "Class number in Roman notation (VI, IX)",
    "roman_lower": "Class number in lower-case Roman notation (vi, ix)",
    "number": "Class number in the notation used by the original name",
    "letter": "Section letter exactly as written in the original name",
    "letter_upper": "Section letter in upper case",
    "letter_lower": "Section letter in lower case",
    "prefix": "Text that appeared before the number in the original name",
    "suffix": "Text that appeared after the section letter in the original name",
    "separator": "Characters that separated number and letter in the original name",
    "original": "The complete original class name",
}


def _roman_or_fail(value: int) -> str:
    """
    Render *value* as a Roman numeral, or raise :class:`TemplateError`.

    Roman notation has no symbol for zero or for negative numbers, so a class
    literally named ``"0.A"`` cannot be converted.  Letting ``int_to_roman``'s
    bare ``ValueError`` escape crashed the conversion of a whole source because
    of one odd class name; a TemplateError is caught by
    :func:`convert_class_name` and reported next to that single name.
    """
    try:
        return int_to_roman(value)
    except ValueError:
        raise TemplateError(
            f"the number {value} cannot be written as a Roman numeral"
        )


def _placeholder_value(field_name: str, argument: Optional[str],
                       parts: ClassNameParts) -> str:
    """Resolve a single placeholder to its textual value."""
    if field_name in ("arabic", "number", "roman", "roman_lower"):
        if parts.numeral_value is None:
            raise TemplateError("the class name contains no recognisable number")

    if field_name == "arabic":
        text = str(parts.numeral_value)
        if argument:
            try:
                width = int(argument)
            except ValueError:
                raise TemplateError(f"'{argument}' is not a valid width for {{arabic}}")
            if width < 1 or width > 10:
                raise TemplateError("width for {arabic} must be between 1 and 10")
            text = text.zfill(width)
        return text

    if field_name == "roman":
        return _roman_or_fail(parts.numeral_value)

    if field_name == "roman_lower":
        return _roman_or_fail(parts.numeral_value).lower()

    if field_name == "number":
        if parts.numeral_style is NumeralStyle.ROMAN:
            roman = _roman_or_fail(parts.numeral_value)
            return roman.lower() if parts.is_roman_lowercase else roman
        return str(parts.numeral_value)

    if field_name == "letter":
        return parts.letter
    if field_name == "letter_upper":
        return parts.letter.upper()
    if field_name == "letter_lower":
        return parts.letter.lower()
    if field_name == "prefix":
        return parts.prefix
    if field_name == "suffix":
        return parts.suffix
    if field_name == "separator":
        return parts.separator
    if field_name == "original":
        return parts.original

    raise TemplateError(f"unknown placeholder '{{{field_name}}}'")


def render_template(template: str, parts: ClassNameParts) -> str:
    """
    Render *template* using the values of *parts*.

    Args:
        template: Template string containing ``{placeholder}`` tokens.
        parts: Parsed class name supplying the values.

    Returns:
        The rendered class name.

    Raises:
        TemplateError: If the template is empty, contains an unknown
            placeholder, or needs a number the class name does not have.
    """
    if not template or not template.strip():
        raise TemplateError("Template cannot be empty")

    def _replace(match: re.Match) -> str:
        return _placeholder_value(match.group(1), match.group(2), parts)

    rendered = _PLACEHOLDER_RE.sub(_replace, template)

    # Detect leftovers such as "{unknown name}" that the placeholder pattern
    # did not even recognise as a token.
    leftover = re.search(r'\{[^}]*\}', rendered)
    if leftover:
        raise TemplateError(f"unsupported placeholder '{leftover.group(0)}'")

    return rendered


def validate_template(template: str) -> Optional[str]:
    """
    Check a user supplied template without applying it to real data.

    Args:
        template: The template to check.

    Returns:
        ``None`` when the template is usable, otherwise an error message that
        can be shown directly to the user.
    """
    if not template or not template.strip():
        return "Template cannot be empty."

    unbalanced = template.count('{') != template.count('}')
    if unbalanced:
        return "Template has unbalanced { } brackets."

    probe = ClassNameParts(
        original="6.A", prefix="", numeral_text="6", numeral_value=6,
        numeral_style=NumeralStyle.ARABIC, separator=".", letter="A", suffix="",
    )
    try:
        result = render_template(template, probe)
    except TemplateError as exc:
        # .capitalize() upper-cases the first letter AND lower-cases the rest,
        # which rewrote the placeholder the user actually typed
        # ("{Arabic}" -> "unsupported placeholder '{arabic}'").
        message = str(exc)
        return message[:1].upper() + message[1:] if message else message

    if not result.strip():
        return "Template produces an empty class name."
    return None


# ---------------------------------------------------------------------------
# Conversion / shifting results
# ---------------------------------------------------------------------------

@dataclass
class ClassNameResult:
    """
    Outcome of a conversion or shift for a single class name.

    Attributes:
        original: The name the operation started from.
        result: The resulting name (equals *original* when skipped).
        changed: True when *result* differs from *original*.
        recognised: True when the numeral needed by the operation was found.
        removed: True when the class is to be dropped (graduating year).
        message: Short explanation for previews and logs.
    """

    original: str
    result: str
    changed: bool = False
    recognised: bool = True
    removed: bool = False
    message: str = ""

    @property
    def skipped(self) -> bool:
        """True when nothing happened to this class name."""
        return not self.changed and not self.removed

    def preview_line(self) -> str:
        """One-line preview entry with a status icon."""
        if self.removed:
            return f"❌ {self.original} → REMOVED ({self.message})"
        if not self.recognised:
            return f"⚠️ {self.original} → unchanged ({self.message})"
        if not self.changed:
            return f"= {self.original} → unchanged ({self.message or 'already in target format'})"
        return f"✓ {self.original} → {self.result}"


def convert_class_name(name: Optional[str], template: str) -> ClassNameResult:
    """
    Re-render a class name through a template.

    Names whose numeral cannot be recognised are returned unchanged with
    ``recognised = False`` - they are never silently dropped.

    Args:
        name: Class name to convert.
        template: Template to render (see :data:`TEMPLATE_PLACEHOLDERS`).

    Returns:
        A :class:`ClassNameResult`.
    """
    original = name if isinstance(name, str) else ("" if name is None else str(name))
    parts = parse_class_name(original)

    try:
        rendered = render_template(template, parts)
    except TemplateError as exc:
        return ClassNameResult(
            original=original, result=original, changed=False,
            recognised=parts.has_numeral, message=str(exc),
        )

    rendered = rendered.strip()
    if not rendered:
        return ClassNameResult(
            original=original, result=original, changed=False,
            recognised=parts.has_numeral,
            message="template produced an empty name",
        )

    return ClassNameResult(
        original=original,
        result=rendered,
        changed=rendered != original,
        recognised=True,
        message=parts.describe(),
    )


def shift_class_name(name: Optional[str], delta: int = 1, *,
                     graduation_year: Optional[int] = 9,
                     remove_graduating: bool = True,
                     min_year: int = 1) -> ClassNameResult:
    """
    Shift the numeral inside a class name while preserving everything else.

    ``"6.A"`` becomes ``"7.A"``, ``"IX.B"`` becomes ``"X.B"`` and
    ``"Blue class 6.A"`` becomes ``"Blue class 7.A"``.  The notation of the
    original name (Arabic vs. Roman, upper vs. lower case) is preserved.

    Args:
        name: Class name to shift.
        delta: How many years to move (may be negative).
        graduation_year: Numeral that graduates out of the school, or ``None``
            when no year graduates.
        remove_graduating: When True, classes at *graduation_year* are marked
            for removal instead of being shifted.
        min_year: Lowest numeral that may result from a downward shift.

    Returns:
        A :class:`ClassNameResult` describing what should happen.
    """
    original = name if isinstance(name, str) else ("" if name is None else str(name))
    parts = parse_class_name(original)

    if not parts.has_numeral:
        return ClassNameResult(
            original=original, result=original, changed=False, recognised=False,
            message="no number found in the class name",
        )

    if (graduation_year is not None and remove_graduating
            and parts.numeral_value == graduation_year and delta > 0):
        return ClassNameResult(
            original=original, result=original, changed=False, recognised=True,
            removed=True, message="graduating year",
        )

    new_value = parts.numeral_value + delta

    if new_value < min_year:
        return ClassNameResult(
            original=original, result=original, changed=False, recognised=True,
            message=f"shift would produce year {new_value} (minimum is {min_year})",
        )
    if new_value > MAX_NUMERAL_VALUE:
        return ClassNameResult(
            original=original, result=original, changed=False, recognised=True,
            message=f"shift would produce year {new_value} (maximum is {MAX_NUMERAL_VALUE})",
        )

    if parts.numeral_style is NumeralStyle.ROMAN:
        try:
            new_numeral = int_to_roman(new_value)
        except ValueError:
            # Callers may lower min_year; Roman notation still cannot express
            # zero or negative years, so report it instead of crashing.
            return ClassNameResult(
                original=original, result=original, changed=False,
                recognised=True,
                message=(f"year {new_value} cannot be written as a Roman "
                         f"numeral - the name was left unchanged"),
            )
        if parts.is_roman_lowercase:
            new_numeral = new_numeral.lower()
    else:
        # Preserve zero padding such as "06.A"
        width = len(parts.numeral_text)
        new_numeral = str(new_value)
        if parts.numeral_text.startswith('0') and len(new_numeral) < width:
            new_numeral = new_numeral.zfill(width)

    result = (
        f"{parts.prefix}{new_numeral}{parts.separator}{parts.letter}{parts.suffix}"
    )

    return ClassNameResult(
        original=original,
        result=result,
        changed=result != original,
        recognised=True,
        message=f"{parts.numeral_value} → {new_value}",
    )


# ---------------------------------------------------------------------------
# Whitespace handling
# ---------------------------------------------------------------------------

def has_inner_whitespace(name: Optional[str]) -> bool:
    """True when *name* contains any whitespace character."""
    if not isinstance(name, str):
        return False
    return any(ch.isspace() for ch in name)


def replace_whitespace(name: Optional[str], replacement: str = "") -> str:
    """
    Replace every whitespace character in *name* with *replacement*.

    Leading and trailing whitespace is always removed first so that
    ``" 6. A "`` becomes ``"6.A"`` (or ``"6.-A"`` for ``replacement='-'``)
    rather than growing extra separators at the edges.

    Args:
        name: Class name to clean.
        replacement: String each inner whitespace run is replaced with.

    Returns:
        The cleaned class name.
    """
    if not isinstance(name, str):
        return ""
    return re.sub(r'\s+', replacement, name.strip())


# ---------------------------------------------------------------------------
# Collection level analysis
# ---------------------------------------------------------------------------

def style_signature(parts: ClassNameParts) -> str:
    """
    Build a comparable "shape" key for a parsed class name.

    Two class names share a signature when they follow the same naming logic,
    e.g. ``"6.A"`` and ``"7.B"`` both give ``"arabic|dot|letter"`` while
    ``"IX.B"`` gives ``"roman|dot|letter"``.
    """
    if not parts.has_numeral:
        return "free-text"

    if parts.has_surrounding_text:
        shape = "text+"
    else:
        shape = ""

    separator = parts.separator
    if separator == "":
        sep_key = "none"
    elif separator.strip() == "":
        sep_key = "space"
    elif separator.strip() == "." and separator != ".":
        sep_key = "dot+space"
    elif separator == ".":
        sep_key = "dot"
    else:
        sep_key = f"'{separator.strip()}'"

    letter_key = "letter" if parts.has_letter else "no-letter"
    case_key = "lower" if parts.is_roman_lowercase else "upper"
    style_key = parts.numeral_style.value
    if parts.numeral_style is NumeralStyle.ROMAN:
        style_key = f"{style_key}-{case_key}"

    return f"{shape}{style_key}|{sep_key}|{letter_key}"


def describe_signature(signature: str) -> str:
    """Turn a signature produced by :func:`style_signature` into English."""
    if signature == "free-text":
        return "free text without a recognisable number"

    prefix = ""
    core = signature
    if core.startswith("text+"):
        prefix = "descriptive text around "
        core = core[len("text+"):]

    try:
        style_key, sep_key, letter_key = core.split("|")
    except ValueError:                              # pragma: no cover - defensive
        return signature

    style_text = {
        "arabic": "Arabic number",
        "roman-upper": "upper-case Roman number",
        "roman-lower": "lower-case Roman number",
    }.get(style_key, style_key)

    sep_text = {
        "none": "no separator",
        "space": "space separator",
        "dot": "dot separator",
        "dot+space": "dot and space separator",
    }.get(sep_key, f"{sep_key} separator")

    letter_text = "with section letter" if letter_key == "letter" else "without section letter"

    return f"{prefix}{style_text}, {sep_text}, {letter_text}"


@dataclass
class ClassNameAnalysis:
    """
    Result of analysing a collection of class names.

    Attributes:
        names: The analysed names, in input order.
        parsed: Parsed form of every name, in the same order.
        signatures: ``signature -> [names]`` grouping.
        unparsed: Names in which no number could be found.
        with_whitespace: Standard names that contain whitespace.
        duplicates: Names that appear more than once.
        empty: Names that are empty or whitespace only.
    """

    names: List[str] = field(default_factory=list)
    parsed: List[ClassNameParts] = field(default_factory=list)
    signatures: Dict[str, List[str]] = field(default_factory=dict)
    unparsed: List[str] = field(default_factory=list)
    with_whitespace: List[str] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    empty: List[str] = field(default_factory=list)

    @property
    def is_consistent(self) -> bool:
        """True when every recognised name follows the same naming logic."""
        real = {sig for sig in self.signatures if sig != "free-text"}
        return len(real) <= 1 and not self.unparsed

    @property
    def dominant_style(self) -> NumeralStyle:
        """The numeral notation used by most of the recognised names."""
        arabic = sum(1 for p in self.parsed if p.numeral_style is NumeralStyle.ARABIC)
        roman = sum(1 for p in self.parsed if p.numeral_style is NumeralStyle.ROMAN)
        if roman > arabic:
            return NumeralStyle.ROMAN
        if arabic > 0:
            return NumeralStyle.ARABIC
        return NumeralStyle.NONE


def analyze_class_names(names: Sequence[Optional[str]]) -> ClassNameAnalysis:
    """
    Analyse a collection of class names for consistency problems.

    Args:
        names: Class names to inspect (order is preserved).

    Returns:
        A populated :class:`ClassNameAnalysis`.
    """
    analysis = ClassNameAnalysis()
    seen: Dict[str, int] = {}

    for raw in names:
        name = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        analysis.names.append(name)

        parts = parse_class_name(name)
        analysis.parsed.append(parts)

        if not name.strip():
            analysis.empty.append(name)
            continue

        signature = style_signature(parts)
        analysis.signatures.setdefault(signature, []).append(name)

        if not parts.has_numeral:
            analysis.unparsed.append(name)
        elif parts.is_standard and has_inner_whitespace(name):
            analysis.with_whitespace.append(name)

        seen[name] = seen.get(name, 0) + 1

    analysis.duplicates = [name for name, count in seen.items() if count > 1]
    return analysis
