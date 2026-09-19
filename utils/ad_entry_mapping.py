"""
Turning an Active Directory entry into a :class:`~models.Person`

The "1. Data Sources" tab used to request five attributes from the directory:

    ['givenName', 'sn', 'displayName', 'sAMAccountName', 'mail',
     'distinguishedName']

Everything else therefore arrived empty, no matter what the directory held.
That is why a person loaded from Active Directory showed **no home directory
and no groups** in the editing window: the two attributes were never asked
for.

This module is the one place that decides which attributes are read and how
they become person data, so the loader, the discovery step and any future
importer stay in agreement.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Every attribute a person record is built from.
#:
#: ldap3 only exposes attributes that were *requested*, so an attribute missing
#: from this list silently reads as empty - which is exactly the defect this
#: module exists to prevent.  Keep it in sync with
#: :meth:`ui.property_editor.PropertyEditor._store_original_values`, which
#: lists the fields the user can edit.
PERSON_ATTRIBUTES: List[str] = [
    'givenName',            # -> first_name
    'sn',                   # -> last_name
    'displayName',          # -> ad_display_name
    'sAMAccountName',       # -> ad_username
    'userPrincipalName',    # -> metadata (kept for reference)
    'mail',                 # -> ad_email
    'description',          # -> ad_description
    'homeDirectory',        # -> home_directory
    'homeDrive',            # -> home_drive
    'memberOf',             # -> group_memberships
    'distinguishedName',    # -> ad_dn, and its parent -> ad_ou_path
    'userAccountControl',   # -> account_enabled, password_never_expires
    'pwdLastSet',           # -> password_must_change
    'whenChanged',          # -> metadata (useful when comparing sources)
]

#: ``userAccountControl`` bits this module reads.
UAC_ACCOUNT_DISABLED = 0x0002
UAC_DONT_EXPIRE_PASSWORD = 0x10000
# Note: "user cannot change password" is NOT a userAccountControl bit in Active
# Directory - it is expressed by two access control entries on the object.
# Reading UAC for it would always report False, so the flag is left alone here
# rather than being filled in with a value that is wrong.


def attribute_text(entry: Any, name: str) -> str:
    """
    Read one LDAP attribute of an ldap3 entry as text.

    ldap3 exposes every *requested* attribute on the entry even when the
    directory returned no value for it - ``entry.givenName.value`` is then
    ``None``.  Wrapping that in ``str()`` produced the literal string
    ``'None'``, which looks like a real name and kept nameless accounts.
    A value the directory did not return must read as an empty string.

    Args:
        entry: The ldap3 entry.
        name: The attribute name.

    Returns:
        The first value as text, or ``''``.
    """
    try:
        attribute = entry[name]
    except (KeyError, AttributeError, TypeError):
        # The attribute was not requested / does not exist on this entry.
        return ''

    value = getattr(attribute, 'value', None)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return ''
    return str(value)


def attribute_values(entry: Any, name: str) -> List[str]:
    """
    Read a multi-valued LDAP attribute as a list of strings.

    ldap3 returns a bare string for a single-valued answer and a list for a
    multi-valued one, so both shapes have to be accepted.  ``memberOf`` in
    particular is a list for a user in several groups and a plain string for a
    user in exactly one - reading only ``.values`` missed the latter on some
    ldap3 versions.

    Args:
        entry: The ldap3 entry.
        name: The attribute name.

    Returns:
        Every value as text, empty entries removed.  Never ``None``.
    """
    try:
        attribute = entry[name]
    except (KeyError, AttributeError, TypeError):
        return []

    raw = getattr(attribute, 'values', None)
    if raw is None:
        raw = getattr(attribute, 'value', None)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def attribute_int(entry: Any, name: str) -> Optional[int]:
    """
    Read an LDAP attribute as an integer.

    Returns ``None`` when the attribute is absent or not a number, so a
    directory that answers with something unexpected cannot make the loader
    raise.
    """
    text = attribute_text(entry, name)
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        logger.debug("Attribute %s is not a number: %r", name, text)
        return None


def rdn_value(dn: str) -> str:
    """
    Return the value of the first component of a distinguished name.

    ``CN=Pupils,OU=Groups,DC=school,DC=local`` -> ``Pupils``.

    An escaped comma stays inside the value, so a group really called
    ``Pupils, year 6`` keeps its name.  A string that is not a DN at all is
    returned unchanged - it is still the best name available.
    """
    from utils.source_naming import parse_dn

    components = parse_dn(dn)
    if components:
        return components[0][1]
    return (dn or "").strip()


def parent_dn(dn: str) -> str:
    """
    Return the DN of the container an object sits in.

    ``CN=Jan Novak,OU=Trida-6A,DC=school,DC=local``
    -> ``OU=Trida-6A,DC=school,DC=local``

    Returns an empty string for a DN with a single component (or no DN at all).
    """
    import re

    text = (dn or "").strip()
    if not text:
        return ''
    # Split on the first unescaped comma - the same rule parse_dn() uses.
    parts = re.split(r'(?<!\\),', text, maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ''


def group_from_dn(dn: str):
    """
    Build an :class:`~models.ADGroup` from the DN the directory reported.

    The group's own entry is deliberately **not** fetched: ``memberOf`` already
    carries the DN, and reading every group separately would turn loading one
    class into dozens of extra round trips.  Name and DN are what the
    application shows and writes back.

    Args:
        dn: The group's distinguished name.

    Returns:
        The group, or ``None`` for an empty DN.
    """
    from models import ADGroup

    text = (dn or "").strip()
    if not text:
        return None
    return ADGroup(name=rdn_value(text), dn=text)


def groups_from_entry(entry: Any) -> List:
    """
    Build the group list of one person from the entry's ``memberOf``.

    Duplicates are removed (a directory may list the same DN twice) while the
    directory's own order is kept.
    """
    groups = []
    seen = set()
    for dn in attribute_values(entry, 'memberOf'):
        key = dn.casefold()
        if key in seen:
            continue
        seen.add(key)
        group = group_from_dn(dn)
        if group is not None:
            groups.append(group)
    return groups


def person_from_entry(entry: Any, class_name: str):
    """
    Build a :class:`~models.Person` from one Active Directory user entry.

    Args:
        entry: The ldap3 entry, searched with :data:`PERSON_ATTRIBUTES`.
        class_name: The class the person belongs to.

    Returns:
        The person, or ``None`` when the entry has neither a first nor a last
        name - such an object is a service account or a computer, not a pupil,
        and a record without a name cannot be matched against anything.
    """
    from models import Person

    first_name = attribute_text(entry, 'givenName')
    last_name = attribute_text(entry, 'sn')
    if not first_name or not last_name:
        return None

    dn = attribute_text(entry, 'distinguishedName') or getattr(entry, 'entry_dn', '') or ''

    uac = attribute_int(entry, 'userAccountControl')
    # An entry whose userAccountControl was not returned must not be reported
    # as disabled: 'unknown' is closer to enabled for an account the directory
    # is actively serving.
    account_enabled = True if uac is None else not bool(uac & UAC_ACCOUNT_DISABLED)
    password_never_expires = bool(uac & UAC_DONT_EXPIRE_PASSWORD) if uac else False

    # pwdLastSet == 0 is how Active Directory expresses "must change at next
    # logon".  Any other value (including a missing attribute) means it is not
    # required.
    password_must_change = attribute_int(entry, 'pwdLastSet') == 0

    person = Person(
        first_name=first_name,
        last_name=last_name,
        class_name=class_name,
        ad_username=attribute_text(entry, 'sAMAccountName') or None,
        ad_display_name=attribute_text(entry, 'displayName') or None,
        ad_email=attribute_text(entry, 'mail') or None,
        ad_description=attribute_text(entry, 'description') or None,
        ad_dn=dn or None,
        ad_ou_path=parent_dn(dn) or None,
        home_directory=attribute_text(entry, 'homeDirectory') or None,
        home_drive=attribute_text(entry, 'homeDrive') or None,
        group_memberships=groups_from_entry(entry),
        password_must_change=password_must_change,
        password_never_expires=password_never_expires,
        account_enabled=account_enabled,
    )

    # Kept out of the typed fields on purpose: the application does not edit
    # these, but they are useful when comparing two sources by hand.
    person.metadata['ad_dn'] = dn
    upn = attribute_text(entry, 'userPrincipalName')
    if upn:
        person.metadata['ad_user_principal_name'] = upn
    changed = attribute_text(entry, 'whenChanged')
    if changed:
        person.metadata['ad_when_changed'] = changed

    return person
