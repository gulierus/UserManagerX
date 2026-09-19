"""
Human readable default names for freshly loaded data sources.

Every loader used to invent its own name.  Active Directory in particular
pasted the raw connection URL into the source name, which produced entries
such as ``AD-ldaps://dc01.school.local:636`` in every source combo box - long,
full of punctuation and identical for two different parts of the same
directory.

This module turns the technical connection details into the same kind of short
label a person would write by hand, and makes sure the label does not collide
with a source that is already loaded.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Prefix that marks a source as coming from Active Directory.
AD_SOURCE_PREFIX = "AD"

#: Separator between the directory and the branch part of an AD source name.
NAME_SEPARATOR = " - "

#: Longest default name we are willing to produce.  Source names end up in
#: combo boxes and list rows, and anything past this is truncated by the
#: widget anyway - better to cut it ourselves at a sensible place.
MAX_NAME_LENGTH = 60


def _strip_scheme_and_port(server: str) -> str:
    """
    Reduce an LDAP server string to its bare host name.

    ``ldaps://dc01.school.local:636`` -> ``dc01.school.local``.  Anything that
    does not look like a URL is returned unchanged apart from surrounding
    whitespace, so a plain host name or IP address survives untouched.
    """
    host = (server or "").strip()
    if not host:
        return ""

    # Drop the scheme.  'ldap://', 'ldaps://' and anything else shaped alike.
    host = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", host)

    # Drop everything after the first slash - a URL may carry a path.
    host = host.split("/", 1)[0]

    # Drop the port.  An IPv6 literal is written '[::1]:636', so the port is
    # only the part after the *last* colon and only when it is numeric.
    if host.startswith("["):
        closing = host.find("]")
        if closing != -1:
            host = host[1:closing]
    elif host.count(":") == 1:
        head, _, tail = host.rpartition(":")
        if tail.isdigit():
            host = head

    return host.strip(". ")


def parse_dn(dn: str) -> List[tuple]:
    """
    Split a distinguished name into ``(attribute, value)`` pairs.

    Only the structure we need is understood: components separated by commas,
    each of them ``attribute=value``.  Escaped commas (``\\,``) stay inside
    their value, because a class called ``6.A, second group`` is spelled that
    way in the directory.

    Args:
        dn: The distinguished name, for example ``OU=Students,DC=school,DC=local``.

    Returns:
        A list of ``(attribute_lowercase, value)`` tuples, in directory order
        (most specific first).  Malformed components are skipped rather than
        raising - a name we cannot read must never stop a source from loading.
    """
    if not dn:
        return []

    components: List[tuple] = []
    for raw in re.split(r"(?<!\\),", dn):
        part = raw.strip()
        if not part or "=" not in part:
            continue
        attribute, _, value = part.partition("=")
        attribute = attribute.strip().lower()
        value = value.strip().replace("\\,", ",")
        if attribute and value:
            components.append((attribute, value))
    return components


def domain_from_dn(dn: str) -> str:
    """
    Rebuild the DNS domain from the ``DC=`` components of a DN.

    ``OU=Students,DC=school,DC=local`` -> ``school.local``.  Returns an empty
    string when the DN carries no ``DC=`` component at all.
    """
    labels = [value for attribute, value in parse_dn(dn) if attribute == "dc"]
    return ".".join(labels)


def branch_from_dn(dn: str) -> str:
    """
    Name the branch of the directory a DN points at.

    This is the left-most non-``DC=`` component: the organizational unit (or
    container) the search starts in.  ``OU=Students,OU=School,DC=a,DC=b``
    therefore yields ``Students``.  A DN that is only a domain root has no
    branch, and an empty string is returned.
    """
    for attribute, value in parse_dn(dn):
        if attribute != "dc":
            return value
    return ""


def _truncate(name: str) -> str:
    """Shorten an over-long name at a word boundary and mark the cut."""
    if len(name) <= MAX_NAME_LENGTH:
        return name
    cut = name[: MAX_NAME_LENGTH - 1].rstrip()
    # Prefer to cut between words so the result still reads as a name.
    space = cut.rfind(" ")
    if space > MAX_NAME_LENGTH // 2:
        cut = cut[:space]
    return f"{cut.rstrip()}…"


def build_ad_source_name(server: str, base_dn: str = "") -> str:
    """
    Build a readable default name for an Active Directory source.

    The name is assembled from the two pieces of information that actually
    distinguish one AD source from another for the user:

    * the directory itself - its DNS domain when the base DN names one,
      otherwise the server host name;
    * the branch that was loaded - the organizational unit the search started
      in, which is what makes two sources from the *same* server different.

    Examples:
        >>> build_ad_source_name("ldaps://dc01.school.local:636",
        ...                      "OU=Students,DC=school,DC=local")
        'AD school.local - Students'
        >>> build_ad_source_name("ldap://192.168.1.10", "DC=school,DC=local")
        'AD school.local'
        >>> build_ad_source_name("dc01.school.local", "")
        'AD dc01.school.local'
        >>> build_ad_source_name("", "")
        'AD'

    Args:
        server: The server string exactly as the user typed it - with or
            without a scheme and a port.
        base_dn: The search base.  May be empty.

    Returns:
        A name safe to show in a combo box.  Never empty, never longer than
        :data:`MAX_NAME_LENGTH`.
    """
    directory = domain_from_dn(base_dn) or _strip_scheme_and_port(server)
    branch = branch_from_dn(base_dn)

    parts = [AD_SOURCE_PREFIX]
    if directory:
        parts.append(f" {directory}")
    if branch and branch.lower() != directory.lower():
        parts.append(f"{NAME_SEPARATOR}{branch}")

    return _truncate("".join(parts).strip())


def unique_source_name(base_name: str, existing_names: Iterable[str]) -> str:
    """
    Turn a default name into one that is not taken yet.

    Loading the same directory twice used to create two sources with exactly
    the same name; every lookup by name then found the first one and the fresh
    data was unreachable.  A numeric suffix keeps them apart.

    Comparison is case-insensitive, because two sources differing only in case
    are indistinguishable to a person reading the list.

    Args:
        base_name: The preferred name.
        existing_names: Names already in use.

    Returns:
        ``base_name`` when it is free, otherwise ``base_name (2)``,
        ``base_name (3)`` and so on.
    """
    taken = {str(name).strip().casefold() for name in existing_names or ()}

    candidate = (base_name or "").strip()
    if not candidate:
        candidate = AD_SOURCE_PREFIX

    if candidate.casefold() not in taken:
        return candidate

    # The suffix must not push the name past the length limit, so shorten the
    # stem instead of the counter.
    for counter in range(2, 1000):
        suffix = f" ({counter})"
        stem = candidate
        if len(stem) + len(suffix) > MAX_NAME_LENGTH:
            stem = stem[: MAX_NAME_LENGTH - len(suffix)].rstrip()
        numbered = f"{stem}{suffix}"
        if numbered.casefold() not in taken:
            return numbered

    # Practically unreachable - a thousand sources with the same name.
    logger.warning("Could not find a free variant of source name %r", base_name)
    return candidate


def suggest_ad_source_name(server: str, base_dn: str,
                           existing_names: Optional[Sequence[str]] = None) -> str:
    """
    Convenience wrapper: build the default AD name and make it unique.

    Args:
        server: LDAP server as typed by the user.
        base_dn: Search base.
        existing_names: Names of the sources already loaded.

    Returns:
        A readable, unused source name.
    """
    return unique_source_name(build_ad_source_name(server, base_dn),
                              existing_names or ())
