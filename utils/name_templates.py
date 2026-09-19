"""
Name templates with field extraction
====================================

Version 26, point 21 needs two name templates that did not exist before:

* step 2 of the Microsoft 365 import builds a **class name** out of the fields
  of the group or team it came from;
* synchronisation builds a **group name** out of the fields of the class.

Both need more than whole-field substitution.  A group called
``Trida-6.A-2020`` should be able to yield the class ``6.A``, which means
pulling a *part* of a field out - the request asks for exactly that: "make sure
the placeholder also supports extracting specific parts of the given field".

Syntax
------
A placeholder is a field name in braces, optionally followed by operations
separated by ``|``::

    {display_name}                      the whole field
    {display_name[6:]}                  a Python slice of it
    {display_name[0]}                   one character
    {display_name|after:Trida-}         everything after the first "Trida-"
    {display_name|before:-2020}         everything before the first "-2020"
    {display_name|word:2}               the second space-separated word
    {display_name|match:\\d+}            the first match of a regular expression
    {display_name|after:Trida-|upper}   operations chain, left to right

Slices use Python's own semantics, negative indices included, and they never
raise: a slice past the end of a short value yields what there is.

Design note
-----------
This module knows nothing about Microsoft 365 or about classes.  It renders a
template against a plain ``field -> value`` dictionary, and the two callers
supply their own dictionaries.  That keeps it testable on its own and makes it
reusable for the next thing that needs a name built out of fields.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


class TemplateError(ValueError):
    """A template cannot be rendered, with a message meant for the user."""


#: ``{field...}`` - the field name, then everything up to the closing brace.
#: Braces cannot nest, which keeps the pattern simple and the syntax readable.
_PLACEHOLDER_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)([^{}]*)\}')

#: ``[start:end]`` or ``[index]`` directly after the field name.
_SLICE_RE = re.compile(r'^\[\s*(-?\d+)?\s*(?::\s*(-?\d+)?\s*)?\]')


def _slice_value(value: str, spec: str) -> str:
    """
    Apply a ``[...]`` slice to a value.

    Args:
        value: The text to cut.
        spec: The bracket expression, including the brackets.

    Returns:
        The requested part.  An index outside the value yields an empty string
        rather than raising - a template must not explode on a short name.

    Raises:
        TemplateError: If the bracket expression is not a slice at all.
    """
    match = _SLICE_RE.match(spec)
    if not match or match.end() != len(spec):
        raise TemplateError(
            f"{spec!r} is not a valid slice. Write [2:5], [2:] or [0]."
        )

    start_text, end_text = match.group(1), match.group(2)
    has_colon = ':' in spec

    if not has_colon:
        if start_text is None:
            raise TemplateError("[] needs an index, for example [0].")
        index = int(start_text)
        try:
            return value[index]
        except IndexError:
            return ''

    start = int(start_text) if start_text is not None else None
    end = int(end_text) if end_text is not None else None
    return value[start:end]


def _operation_after(value: str, argument: str) -> str:
    """Everything after the first occurrence of *argument*; '' if absent."""
    if not argument:
        return value
    _, separator, tail = value.partition(argument)
    return tail if separator else ''


def _operation_before(value: str, argument: str) -> str:
    """Everything before the first occurrence of *argument*; '' if absent."""
    if not argument:
        return value
    head, separator, _ = value.partition(argument)
    return head if separator else ''


def _operation_word(value: str, argument: str) -> str:
    """The n-th whitespace-separated word, counting from 1."""
    try:
        index = int(argument)
    except (TypeError, ValueError):
        raise TemplateError(f"word:{argument!r} needs a number, e.g. word:2.")
    if index == 0:
        raise TemplateError("word:0 does not exist - words are counted from 1.")

    words = value.split()
    position = index - 1 if index > 0 else index
    try:
        return words[position]
    except IndexError:
        return ''


def _operation_match(value: str, argument: str) -> str:
    """
    The first match of a regular expression, or its first capture group.

    An invalid expression is reported as a template error rather than escaping
    as a ``re.error`` nobody can read.
    """
    if not argument:
        raise TemplateError("match: needs a regular expression.")
    try:
        found = re.search(argument, value)
    except re.error as exc:
        raise TemplateError(f"match:{argument!r} is not a valid regular "
                            f"expression ({exc}).") from exc
    if not found:
        return ''
    if found.groups():
        return found.group(1) or ''
    return found.group(0)


def _operation_digits(value: str, _argument: str) -> str:
    """Only the digits of the value, in order."""
    return ''.join(character for character in value if character.isdigit())


#: ``name -> (function, takes_argument, help text)``.
OPERATIONS: Dict[str, tuple] = {
    'after':  (_operation_after,  True,
               "everything after the first occurrence of the text"),
    'before': (_operation_before, True,
               "everything before the first occurrence of the text"),
    'word':   (_operation_word,   True,
               "the n-th word, counting from 1 (use -1 for the last)"),
    'match':  (_operation_match,  True,
               "the first match of a regular expression"),
    'upper':  (lambda value, _arg: value.upper(), False, "upper case"),
    'lower':  (lambda value, _arg: value.lower(), False, "lower case"),
    'title':  (lambda value, _arg: value.title(), False, "Title Case"),
    'strip':  (lambda value, _arg: value.strip(), False,
               "without surrounding spaces"),
    'digits': (_operation_digits, False, "only the digits"),
}


def _apply_operations(value: str, spec: str) -> str:
    """
    Apply the ``|`` chain (and a leading slice) that follows a field name.

    Args:
        value: The field's value.
        spec: Everything between the field name and the closing brace.

    Returns:
        The transformed value.

    Raises:
        TemplateError: On an unknown operation or a malformed slice.
    """
    spec = (spec or '').strip()
    if not spec:
        return value

    # A slice, if any, comes first and binds to the field itself.
    if spec.startswith('['):
        closing = spec.find(']')
        if closing == -1:
            raise TemplateError("A slice is missing its closing ']'.")
        value = _slice_value(value, spec[:closing + 1])
        spec = spec[closing + 1:].strip()

    if not spec:
        return value

    if not spec.startswith('|'):
        raise TemplateError(
            f"{spec!r} is not understood. Operations start with '|', "
            f"for example |after:Trida-."
        )

    for step in spec[1:].split('|'):
        step = step.strip()
        if not step:
            continue
        name, _, argument = step.partition(':')
        name = name.strip().lower()

        entry = OPERATIONS.get(name)
        if entry is None:
            known = ', '.join(sorted(OPERATIONS))
            raise TemplateError(
                f"Unknown operation {name!r}. Available: {known}."
            )

        function, takes_argument, _help = entry
        if takes_argument and not argument:
            raise TemplateError(f"{name} needs a value, for example {name}:6.")
        value = function(value, argument)

    return value


def render(template: str, values: Mapping[str, str],
           strict: bool = True) -> str:
    """
    Render a template against a field dictionary.

    Args:
        template: The template text.
        values: ``field -> value``.  Missing values count as empty.
        strict: When True, an unknown field name is an error.  When False it is
            left in place, which is what a live preview wants while the user is
            still typing.

    Returns:
        The rendered name, with surrounding whitespace removed.

    Raises:
        TemplateError: On an unknown field (when *strict*), an unknown
            operation, or a malformed slice.
    """
    text = template or ''

    def replace(match: re.Match) -> str:
        field_name = match.group(1)
        if field_name not in values:
            if strict:
                known = ', '.join(sorted(values)) or "none"
                raise TemplateError(
                    f"Unknown placeholder {{{field_name}}}. Available: {known}."
                )
            return match.group(0)
        raw = values.get(field_name)
        return _apply_operations('' if raw is None else str(raw),
                                 match.group(2))

    rendered = _PLACEHOLDER_RE.sub(replace, text)

    # A stray brace is almost always a typo - '{display_name' renders as itself
    # and the user sees a name with a brace in it and no idea why.
    if strict:
        leftover = _PLACEHOLDER_RE.sub('', text)
        if '{' in leftover or '}' in leftover:
            raise TemplateError(
                "The template has an unmatched '{' or '}'."
            )

    return rendered.strip()


def validate(template: str, fields: Mapping[str, str]) -> List[str]:
    """
    Report everything wrong with a template, without rendering it for real.

    Args:
        template: The template text.
        fields: ``field -> description`` (or any mapping with the right keys);
            only the key names are used.

    Returns:
        A list of problems, empty when the template is usable.
    """
    problems: List[str] = []

    if not (template or '').strip():
        problems.append("The template is empty.")
        return problems

    probe = {name: "sample" for name in fields}
    try:
        render(template, probe, strict=True)
    except TemplateError as exc:
        problems.append(str(exc))

    if not _PLACEHOLDER_RE.search(template):
        problems.append(
            "The template has no placeholder, so every name would come out "
            "the same."
        )

    return problems


def describe_operations() -> str:
    """One line of help listing the operations, for a dialog."""
    return " · ".join(f"|{name} {entry[2]}" for name, entry in
                      sorted(OPERATIONS.items()))


def preview(template: str, values: Mapping[str, str]) -> str:
    """
    Render a template for display, turning a failure into readable text.

    Used by the live preview under a template field, where an exception would
    be worse than a message.
    """
    try:
        rendered = render(template, values, strict=True)
    except TemplateError as exc:
        return f"⚠ {exc}"
    return rendered or "(empty)"


# ---------------------------------------------------------------------------
# Field providers
# ---------------------------------------------------------------------------
#
# The engine above is deliberately ignorant of the application's own types.
# These two functions are the bridge: each turns one object into the plain
# dictionary the engine renders against, and each is paired with a description
# map the dialogs show as help.

#: ``field -> description`` for a template naming a class after a group.
GROUP_FIELDS: Dict[str, str] = {
    'display_name': "The group or team name (Trida-6.A)",
    'mail_nickname': "The alias used in the group's address (trida-6a)",
    'description': "The group's description",
    'mail': "The group's email address",
    'visibility': "Public, Private or HiddenMembership",
    'kind': "Team, Microsoft 365, Security or Group",
    'member_count': "How many members the group has",
    'object_id': "The group's identifier in Microsoft 365",
}

#: ``field -> description`` for a template naming a group after a class.
CLASS_FIELDS: Dict[str, str] = {
    'class_name': "The class exactly as it is stored (6.A)",
    'grade': "The class number in Arabic digits (6.A -> 6)",
    'roman': "The class number in Roman numerals (6.A -> VI)",
    'letter': "The section letter (6.A -> A)",
    'enrollment_year': "The year the class started the first grade (2020)",
    'school_year': "The school year the class is in now (2025/2026)",
}


def group_values(group) -> Dict[str, str]:
    """
    Build the field dictionary for a Microsoft 365 group or team.

    Args:
        group: An :class:`models_m365.M365Group`.

    Returns:
        ``field -> text``.  Every key of :data:`GROUP_FIELDS` is present, so a
        template can never fail because the directory left a field empty.
    """
    return {
        'display_name': str(getattr(group, 'display_name', '') or ''),
        'mail_nickname': str(getattr(group, 'mail_nickname', '') or ''),
        'description': str(getattr(group, 'description', '') or ''),
        'mail': str(getattr(group, 'mail', '') or ''),
        'visibility': str(getattr(group, 'visibility', '') or ''),
        'kind': str(getattr(group, 'kind', '') or ''),
        'member_count': str(getattr(group, 'member_count', 0)),
        'object_id': str(getattr(group, 'object_id', '') or ''),
    }


def class_values(school_class, enrollment_year=None) -> Dict[str, str]:
    """
    Build the field dictionary for a school class.

    Args:
        school_class: A :class:`models.Class`, or anything with a ``name``.
        enrollment_year: Overrides the year carried by the class, for a caller
            that has just calculated one.

    Returns:
        ``field -> text``.  A part that cannot be derived from the class name -
        the grade of a class called "Zaci", for instance - comes back as an
        empty string rather than raising, so one template can cover a source
        whose classes are not all named the same way.
    """
    from utils.class_name_utils import int_to_roman, parse_class_name

    name = str(getattr(school_class, 'name', '') or '').strip()
    parts = parse_class_name(name)

    grade = str(parts.numeral_value) if parts.has_numeral else ''
    roman = ''
    if parts.has_numeral:
        try:
            roman = int_to_roman(parts.numeral_value)
        except ValueError:
            roman = ''

    year = enrollment_year
    if year is None:
        year = getattr(school_class, 'enrollment_year', None)

    # The school year the class is in *now*, written the way a school writes
    # it.  Derived from this computer's clock rather than from the internet:
    # a template preview must never make a network request.
    school_year = ''
    try:
        from utils.school_year import get_system_year, school_year_start
        start = school_year_start(get_system_year())
        school_year = f"{start}/{start + 1}"
    except Exception:
        logger.debug("Could not derive the school year", exc_info=True)

    return {
        'class_name': name,
        'grade': grade,
        'roman': roman,
        'letter': parts.letter or '',
        'enrollment_year': str(year) if year else '',
        'school_year': school_year,
    }
