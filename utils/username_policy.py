"""
User name policy
================

Describes *how a user name is built* from a person's data, the same way
:mod:`utils.password_policy` describes how a password is built.

Before this module the shape of a user name was hard-coded as
``<last name><first name>`` with a handful of optional index/length arguments
that only the code could reach - the user could not change the convention
without editing Python. The policy is a small template language instead:

    {last_name}{first_name:3}     ->  novakjan  ->  "novakjan"
    {last_name:4}.{first_name:1}  ->  "nova.j"
    {first_name:1}{last_name}     ->  "jnovak"

Placeholders
------------
``{first_name}`` / ``{last_name}``
    The whole name (all parts joined).
``{first_name:N}`` / ``{last_name:N}``
    The first *N* characters, e.g. ``{last_name:3}`` -> ``nov``.
``{first_name.part}`` / ``{last_name.part}``
    A specific part of a compound name: ``.first``, ``.last``, or a 1-based
    number. Combine with a length: ``{last_name.last:3}``.
``{class_name}``
    The class, folded to user-name-safe characters.
``{initials}``
    First letter of the first name + first letter of the last name.

Everything that is not a placeholder is copied through, so ``.`` and ``-`` can
be used as separators. The result is always folded to ASCII, lower-cased (by
default) and truncated to :attr:`UsernamePolicy.max_length`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Settings category used for persistence.
SETTINGS_CATEGORY = "username_policy"

#: sAMAccountName cannot be longer than this.
ABSOLUTE_MAX_LENGTH = 20

#: The convention the application used before the policy existed.
DEFAULT_TEMPLATE = "{last_name}{first_name}"

_PLACEHOLDER_RE = re.compile(
    r'\{(first_name|last_name|class_name|initials)'
    r'(?:\.(first|last|\d+))?'
    r'(?::(\d+))?\}'
)


class UsernameTemplateError(ValueError):
    """Raised when a user-name template cannot be used."""


@dataclass
class UsernamePolicy:
    """
    Description of the required user-name format.

    Attributes:
        template: The pattern the user name is built from.
        lowercase: Fold the result to lower case (AD user names are
            case-insensitive; lower case is the convention).
        max_length: Truncate to this many characters (max 20 for AD).
        separator: Inserted by the template itself; kept here only so the
            editor can offer a quick choice.
    """

    template: str = DEFAULT_TEMPLATE
    lowercase: bool = True
    max_length: int = ABSOLUTE_MAX_LENGTH
    separator: str = ""

    # -- consistency ------------------------------------------------------

    def problems(self) -> List[str]:
        """
        Check the policy for contradictions.

        Returns:
            Human readable problems; empty when the policy is usable.
        """
        problems: List[str] = []

        if not (self.template or "").strip():
            problems.append("The template is empty.")
            return problems

        if self.template.count('{') != self.template.count('}'):
            problems.append("The template has unbalanced { } brackets.")
            return problems

        if not _PLACEHOLDER_RE.search(self.template):
            problems.append(
                "The template contains no placeholder, so every person would "
                "get the same user name."
            )

        leftover = _PLACEHOLDER_RE.sub('', self.template)
        unknown = re.search(r'\{[^}]*\}', leftover)
        if unknown:
            problems.append(f"Unknown placeholder {unknown.group(0)}.")

        if self.max_length < 1:
            problems.append("Maximum length must be at least 1.")
        if self.max_length > ABSOLUTE_MAX_LENGTH:
            problems.append(
                f"Maximum length must not exceed {ABSOLUTE_MAX_LENGTH} "
                f"(the Active Directory sAMAccountName limit)."
            )

        return problems

    def is_valid(self) -> bool:
        """True when the policy has no contradictions."""
        return not self.problems()

    def describe(self) -> str:
        """One-line, human readable summary."""
        parts = [f"pattern {self.template!r}"]
        parts.append("lower case" if self.lowercase else "case preserved")
        parts.append(f"max {self.max_length} characters")
        return "; ".join(parts)

    # -- rendering --------------------------------------------------------

    def render(self, first_name: str, last_name: str, class_name: str = "") -> str:
        """
        Build the user name for one person (without the duplicate counter).

        Args:
            first_name: The person's first name.
            last_name: The person's last name.
            class_name: The person's class, for ``{class_name}``.

        Returns:
            The user name, folded to ASCII and truncated.

        Raises:
            UsernameTemplateError: If the template is unusable or the names
                contain no character a user name may hold.
        """
        problems = self.problems()
        if problems:
            raise UsernameTemplateError("; ".join(problems))

        # Imported here to avoid a circular import at module load time.
        from utils.ad_utils import normalize_name_part

        first_parts = (first_name or "").strip().split()
        last_parts = (last_name or "").strip().split()

        def pick(parts: List[str], selector: Optional[str]) -> str:
            if not parts:
                return ""
            if selector is None:
                return ''.join(parts)
            if selector == 'first':
                return parts[0]
            if selector == 'last':
                return parts[-1]
            try:
                index = int(selector)
            except ValueError:                       # pragma: no cover - regex guards
                return ''.join(parts)
            if 1 <= index <= len(parts):
                return parts[index - 1]
            return parts[0]

        def replace(match: re.Match) -> str:
            field, selector, length = match.group(1), match.group(2), match.group(3)

            if field == 'first_name':
                value = normalize_name_part(pick(first_parts, selector))
            elif field == 'last_name':
                value = normalize_name_part(pick(last_parts, selector))
            elif field == 'class_name':
                value = normalize_name_part(class_name)
            else:  # initials
                value = (normalize_name_part(pick(first_parts, 'first'))[:1]
                         + normalize_name_part(pick(last_parts, 'last'))[:1])

            if length:
                value = value[:int(length)]
            return value

        rendered = _PLACEHOLDER_RE.sub(replace, self.template)

        # Anything the template itself contributed must be user-name safe too.
        rendered = ''.join(c for c in rendered if c.isascii() and (c.isalnum() or c in '.-_'))

        if self.lowercase:
            rendered = rendered.lower()

        rendered = rendered[:self.max_length]

        if not rendered:
            raise UsernameTemplateError(
                f"'{first_name} {last_name}' produces an empty user name with "
                f"the pattern {self.template!r}"
            )
        return rendered

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the settings file."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "UsernamePolicy":
        """
        Build a policy from stored settings, ignoring unusable values.

        Args:
            data: Raw dictionary from the settings file (may be ``None``).

        Returns:
            A policy that is always usable.
        """
        policy = cls()
        if not isinstance(data, dict):
            return policy

        for name, default in asdict(policy).items():
            if name not in data:
                continue
            value = data[name]
            try:
                if isinstance(default, bool):
                    policy.__dict__[name] = bool(value)
                elif isinstance(default, int):
                    policy.__dict__[name] = int(value)
                elif value is None:
                    policy.__dict__[name] = ""
                else:
                    if not isinstance(value, str):
                        raise TypeError(f"{name} must be a string")
                    policy.__dict__[name] = value
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring invalid user name policy value %s=%r",
                               name, value)

        if not policy.is_valid():
            logger.warning("Stored user name policy is unusable (%s) - using defaults",
                           "; ".join(policy.problems()))
            return cls()
        return policy


# ---------------------------------------------------------------------------
# Application wide access
# ---------------------------------------------------------------------------

_active_policy: Optional[UsernamePolicy] = None


def get_username_policy(reload: bool = False) -> UsernamePolicy:
    """Return the policy currently in force (cached)."""
    global _active_policy

    if _active_policy is not None and not reload:
        return _active_policy

    try:
        from utils.settings_manager import get_settings
        _active_policy = UsernamePolicy.from_dict(
            get_settings().get_category(SETTINGS_CATEGORY)
        )
    except Exception:
        logger.exception("Could not load the user name policy - using defaults")
        _active_policy = UsernamePolicy()

    return _active_policy


def set_username_policy(policy: UsernamePolicy, persist: bool = True) -> bool:
    """
    Install a new policy and optionally store it.

    Raises:
        ValueError: If the policy contradicts itself.
    """
    global _active_policy

    problems = policy.problems()
    if problems:
        raise ValueError("; ".join(problems))

    _active_policy = policy
    if not persist:
        return True

    try:
        from utils.settings_manager import get_settings
        return get_settings().set_category(SETTINGS_CATEGORY, policy.to_dict())
    except Exception:
        logger.exception("Could not save the user name policy")
        return False


def reset_username_policy() -> UsernamePolicy:
    """Restore and activate the factory default policy."""
    policy = UsernamePolicy()
    try:
        set_username_policy(policy)
    except ValueError:                               # pragma: no cover - defensive
        logger.exception("Default user name policy is invalid")
    return policy
