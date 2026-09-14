"""
Active Directory search scope
=============================

Controls *where* the discovery step looks for a person.

By default the application searches the whole subtree below the Base DN. That
is the right thing for a small directory, but on a domain that keeps staff,
service accounts and former pupils under the same root it means every lookup
walks the entire tree - and a pupil who happens to share a name with someone in
another branch is found there.

Three scopes are offered:

``SUBTREE``
    Everything below the Base DN, to any depth. The original behaviour.
``ONE_LEVEL``
    Only the organisational units that sit **directly** in the Base DN, each
    searched one level deep. Nothing nested deeper is looked at.
``NAMED_OUS``
    Only organisational units the user names. The names are templates, so one
    entry can cover every class:

        Trida-{class_name}      ->  OU=Trida-6.A,<base dn>
        {enrollment_year}       ->  OU=2020,<base dn>
        Rocnik-{grade}          ->  OU=Rocnik-6,<base dn>

Placeholders are resolved per person, so the search base differs from pupil to
pupil - which is exactly what a per-class directory layout needs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)


class SearchScope(Enum):
    """Where discovery looks for a person."""

    SUBTREE = "subtree"
    ONE_LEVEL = "one_level"
    NAMED_OUS = "named_ous"

    @property
    def label(self) -> str:
        return {
            SearchScope.SUBTREE: "Whole subtree below the Base DN (default)",
            SearchScope.ONE_LEVEL: "Only the OUs directly in the Base DN",
            SearchScope.NAMED_OUS: "Only the organisational units I name",
        }[self]


#: ``placeholder -> description``, for the UI help text.
OU_PLACEHOLDERS = {
    "class_name": "The class exactly as it is stored (6.A, IX.B)",
    "grade": "The class number in Arabic digits (6.A -> 6)",
    "roman": "The class number in Roman numerals (6.A -> VI)",
    "letter": "The section letter (6.A -> A)",
    "enrollment_year": "The year the class started the first grade (6.A -> 2020)",
}

_PLACEHOLDER_RE = re.compile(r'\{(' + '|'.join(OU_PLACEHOLDERS) + r')\}')


@dataclass
class SearchScopeConfig:
    """
    The configured search scope.

    Attributes:
        scope: Which of the three strategies to use.
        ou_templates: OU name patterns, used by :attr:`SearchScope.NAMED_OUS`.
    """

    scope: SearchScope = SearchScope.SUBTREE
    ou_templates: List[str] = field(default_factory=list)

    def problems(self) -> List[str]:
        """Return the reasons this configuration cannot be used."""
        problems: List[str] = []
        if self.scope is SearchScope.NAMED_OUS:
            usable = [t for t in self.ou_templates if (t or "").strip()]
            if not usable:
                problems.append(
                    "No organisational unit was named, so nothing would be "
                    "searched."
                )
            for template in usable:
                leftover = _PLACEHOLDER_RE.sub('', template)
                unknown = re.search(r'\{[^}]*\}', leftover)
                if unknown:
                    problems.append(
                        f"{template!r} uses the unknown placeholder "
                        f"{unknown.group(0)}."
                    )
        return problems

    def describe(self) -> str:
        """One-line summary for a log entry or a status label."""
        if self.scope is SearchScope.NAMED_OUS:
            names = ", ".join(t for t in self.ou_templates if (t or "").strip())
            return f"{self.scope.label}: {names or '(none named)'}"
        return self.scope.label


def render_ou_template(template: str, person, escape=None) -> Optional[str]:
    """
    Resolve the placeholders of one OU template for one person.

    Args:
        template: The OU name pattern.
        person: The person the search is for.
        escape: Optional DN-escaping function applied to the result.

    Returns:
        The resolved OU name, or ``None`` when a placeholder cannot be filled
        in (an unparseable class name, for instance) - such a template is
        skipped rather than producing a nonsensical search base.
    """
    from utils.class_name_utils import int_to_roman, parse_class_name

    class_name = (getattr(person, "class_name", "") or "").strip()
    parts = parse_class_name(class_name)

    def value_for(name: str) -> Optional[str]:
        if name == "class_name":
            return class_name or None
        if name == "grade":
            return str(parts.numeral_value) if parts.has_numeral else None
        if name == "roman":
            if not parts.has_numeral:
                return None
            try:
                return int_to_roman(parts.numeral_value)
            except ValueError:
                return None
        if name == "letter":
            return parts.letter or None
        if name == "enrollment_year":
            # The value belongs to the Class; apply_enrollment_changes() stamps
            # a copy onto each student because a Person cannot reach its Class.
            year = getattr(person, "enrollment_year", None)
            if not year:
                metadata = getattr(person, "metadata", None) or {}
                year = metadata.get("enrollment_year")
            return str(year) if year else None
        return None                                  # pragma: no cover - regex guards

    resolved = template
    for match in _PLACEHOLDER_RE.finditer(template):
        replacement = value_for(match.group(1))
        if replacement is None:
            logger.debug("OU template %r cannot be resolved for %r (%s is unknown)",
                         template, class_name, match.group(1))
            return None
        resolved = resolved.replace(match.group(0), replacement)

    resolved = resolved.strip()
    if not resolved:
        return None
    return escape(resolved) if escape else resolved


def build_search_bases(config: SearchScopeConfig, base_dn: str, person) -> List[str]:
    """
    Return the DNs a person should be searched under, most specific first.

    Args:
        config: The configured scope.
        base_dn: The Base DN from the connection panel.
        person: The person being discovered.

    Returns:
        A list of search bases. For :attr:`SearchScope.NAMED_OUS` the Base DN
        itself is appended as a fallback, so a person whose class has no
        matching OU is still found rather than silently reported as missing.
    """
    base_dn = (base_dn or "").strip()
    if config.scope is not SearchScope.NAMED_OUS:
        return [base_dn]

    from services.ad_services import escape_dn_value

    bases: List[str] = []
    for template in config.ou_templates:
        if not (template or "").strip():
            continue
        resolved = render_ou_template(template, person, escape=escape_dn_value)
        if resolved:
            candidate = f"OU={resolved},{base_dn}"
            if candidate not in bases:
                bases.append(candidate)

    # Never end up with nothing to search.
    if base_dn not in bases:
        bases.append(base_dn)
    return bases


def ldap_scope(config: SearchScopeConfig):
    """
    Return the ldap3 search scope constant matching the configuration.

    ``NAMED_OUS`` searches one level inside each named OU, which is where user
    objects live in a per-class layout.
    """
    from services.ldap_compat import LEVEL, SUBTREE

    if config.scope is SearchScope.SUBTREE:
        return SUBTREE
    return LEVEL
