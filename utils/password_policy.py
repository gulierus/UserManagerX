"""
Password policy
===============

Single source of truth for *how a generated password has to look*.

Before this module existed, password generation and password validation
disagreed with each other:

* ``generate_password()`` produced 5 characters by default,
* ``validate_password()`` demanded at least 8,

so every freshly generated password was immediately reported as
``❌ ad_password: Password too short (min 8 chars)``.

Both sides now read the same :class:`PasswordPolicy`, which the user can edit
in the *Password Format* dialog on the *Operations* tab.  The policy is stored
with the rest of the application settings, so it survives a restart.
"""

from __future__ import annotations

import logging
import string
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Settings category used for persistence.
SETTINGS_CATEGORY = "password_policy"

#: Characters that are easy to confuse when a password is read from paper.
AMBIGUOUS_CHARACTERS = "lI1O0o5S2Z"

#: Special characters offered by default.  Deliberately conservative: these
#: work in Active Directory, in URLs and in CSV exports without escaping.
DEFAULT_SPECIAL_CHARACTERS = "!@#$%&*"

#: Hard limits, independent of what the user configures.
ABSOLUTE_MIN_LENGTH = 4
ABSOLUTE_MAX_LENGTH = 128


@dataclass
class PasswordPolicy:
    """
    Description of the required password format.

    Attributes:
        length: Number of characters a newly generated password gets.
        min_length: Shortest password accepted by the validator.
        max_length: Longest password accepted by the validator.
        require_lowercase: A lower-case letter must be present.
        require_uppercase: An upper-case letter must be present.
        require_digits: A digit must be present.
        require_special: A special character must be present.
        special_characters: The pool of allowed special characters.
        exclude_ambiguous: Leave out characters like ``l``, ``I``, ``1``,
            ``O`` and ``0`` that are easy to mix up.
    """

    length: int = 10
    min_length: int = 8
    max_length: int = 64
    require_lowercase: bool = True
    require_uppercase: bool = True
    require_digits: bool = True
    require_special: bool = True
    special_characters: str = DEFAULT_SPECIAL_CHARACTERS
    exclude_ambiguous: bool = False

    # -- character pools --------------------------------------------------

    def _filtered(self, characters: str) -> str:
        """Remove ambiguous characters when the policy asks for it."""
        if not self.exclude_ambiguous:
            return characters
        return ''.join(c for c in characters if c not in AMBIGUOUS_CHARACTERS)

    @property
    def lowercase_pool(self) -> str:
        return self._filtered(string.ascii_lowercase)

    @property
    def uppercase_pool(self) -> str:
        return self._filtered(string.ascii_uppercase)

    @property
    def digit_pool(self) -> str:
        return self._filtered(string.digits)

    @property
    def special_pool(self) -> str:
        return self._filtered(self.special_characters or "")

    def required_pools(self) -> List[str]:
        """Return the character pools that must each contribute at least once."""
        pools = []
        if self.require_lowercase:
            pools.append(self.lowercase_pool)
        if self.require_uppercase:
            pools.append(self.uppercase_pool)
        if self.require_digits:
            pools.append(self.digit_pool)
        if self.require_special:
            pools.append(self.special_pool)
        return [pool for pool in pools if pool]

    def _raw_pool(self) -> str:
        """
        The character pool WITHOUT the safety fallback.

        :meth:`full_pool` falls back to the ASCII letters so generation can
        never fail; that fallback also made the "no character type available"
        consistency check in :meth:`problems` unreachable, hiding a policy that
        excludes every character class.
        """
        return (self.lowercase_pool + self.uppercase_pool
                + self.digit_pool + self.special_pool)

    def full_pool(self) -> str:
        """
        Return every character a generated password may contain.

        Character classes that are not required are still used as filler so
        the password stays varied, unless the user emptied the special set.
        """
        pool = self.lowercase_pool + self.uppercase_pool + self.digit_pool
        pool += self.special_pool
        return pool or string.ascii_letters

    # -- consistency ------------------------------------------------------

    def problems(self) -> List[str]:
        """
        Check the policy itself for contradictions.

        Returns:
            A list of human readable problems; empty when the policy is usable.
        """
        problems: List[str] = []

        if self.min_length < ABSOLUTE_MIN_LENGTH:
            problems.append(
                f"Minimum length must be at least {ABSOLUTE_MIN_LENGTH}."
            )
        if self.max_length > ABSOLUTE_MAX_LENGTH:
            problems.append(
                f"Maximum length must not exceed {ABSOLUTE_MAX_LENGTH}."
            )
        if self.min_length > self.max_length:
            problems.append("Minimum length is greater than maximum length.")
        if not (self.min_length <= self.length <= self.max_length):
            problems.append(
                f"Generated length ({self.length}) is outside the accepted "
                f"range {self.min_length}-{self.max_length}."
            )

        required = self.required_pools()
        if self.length < len(required):
            problems.append(
                f"Generated length ({self.length}) is too small for "
                f"{len(required)} required character types."
            )
        if self.require_special and not self.special_pool:
            problems.append(
                "Special characters are required but the character set is empty."
            )
        if not self._raw_pool():
            problems.append("No character type is available for generation.")

        return problems

    def is_valid(self) -> bool:
        """True when the policy has no contradictions."""
        return not self.problems()

    def describe(self) -> str:
        """Return a one-line, human readable summary of the policy."""
        parts = [f"{self.length} characters"]
        classes = []
        if self.require_lowercase:
            classes.append("lower case")
        if self.require_uppercase:
            classes.append("upper case")
        if self.require_digits:
            classes.append("digit")
        if self.require_special:
            classes.append(f"special ({self.special_pool})")
        if classes:
            parts.append("must contain " + ", ".join(classes))
        if self.exclude_ambiguous:
            parts.append("without ambiguous characters")
        parts.append(f"accepted length {self.min_length}-{self.max_length}")
        return "; ".join(parts)

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the policy for the settings file."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PasswordPolicy":
        """
        Build a policy from stored settings, ignoring unusable values.

        Args:
            data: Raw dictionary from the settings file (may be ``None``).

        Returns:
            A policy that is always usable - unknown or broken values fall back
            to the defaults.
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
                    # int(float("inf")) raises OverflowError, which is neither
                    # TypeError nor ValueError and used to escape from here.
                    numeric = int(value)
                    if numeric != numeric or abs(numeric) > ABSOLUTE_MAX_LENGTH * 100:
                        raise ValueError(f"{name} out of any sane range")
                    policy.__dict__[name] = numeric
                elif value is None:
                    # Everywhere else in this class None means "empty"
                    # (see special_pool); preserve that rather than restoring
                    # the factory set and changing the user's configuration.
                    policy.__dict__[name] = ""
                else:
                    # str(None) is the string "None", which would silently
                    # become a set of four literal password characters.
                    if not isinstance(value, str):
                        raise TypeError(f"{name} must be a string")
                    policy.__dict__[name] = value
            except (TypeError, ValueError, OverflowError):
                logger.warning("Ignoring invalid password policy value %s=%r",
                               name, value)

        if not policy.is_valid():
            logger.warning(
                "Stored password policy is inconsistent (%s) - using defaults",
                "; ".join(policy.problems()),
            )
            return cls()
        return policy


# ---------------------------------------------------------------------------
# Application wide access
# ---------------------------------------------------------------------------

_active_policy: Optional[PasswordPolicy] = None


def get_password_policy(reload: bool = False) -> PasswordPolicy:
    """
    Return the policy currently in force.

    The policy is loaded from the application settings on first use and then
    cached, so the generator can call this for every single password without
    touching the disk.

    Args:
        reload: Force a reload from the settings file.

    Returns:
        The active :class:`PasswordPolicy`.
    """
    global _active_policy

    if _active_policy is not None and not reload:
        return _active_policy

    try:
        from utils.settings_manager import get_settings
        stored = get_settings().get_category(SETTINGS_CATEGORY)
        _active_policy = PasswordPolicy.from_dict(stored)
    except Exception:
        logger.exception("Could not load the password policy - using defaults")
        _active_policy = PasswordPolicy()

    return _active_policy


def set_password_policy(policy: PasswordPolicy, persist: bool = True) -> bool:
    """
    Install a new policy and optionally store it in the settings file.

    Args:
        policy: The policy to activate.
        persist: Whether to write it to the settings file.

    Returns:
        True when the policy was stored (or when persistence was not asked for).

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
        logger.exception("Could not save the password policy")
        return False


def reset_password_policy() -> PasswordPolicy:
    """Restore and activate the factory default policy."""
    policy = PasswordPolicy()
    try:
        set_password_policy(policy)
    except ValueError:                               # pragma: no cover - defensive
        logger.exception("Default password policy is invalid")
    return policy
