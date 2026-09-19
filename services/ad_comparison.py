"""
Comparing a person in the application with the same person in Active Directory

"Discover in AD" reads every account it can match and stores what the directory
returned.  This module answers the two questions the rest of the application
asks about that snapshot:

* **Do the two sides agree?** - which decides between
  :attr:`~models.ADStatus.MATCHES_AD` and
  :attr:`~models.ADStatus.DIFFERS_FROM_AD` (Version 26, point 18 c/d).
* **Which fields disagree, and what does Active Directory hold?** - which is
  what the coloured outlines and their tooltips show (point 19 b).

The snapshot is deliberately **not** refreshed on its own.  It is what the
directory held at the moment of discovery; if somebody changes Active Directory
afterwards, the application keeps showing the discovered value until the user
presses "Discover in AD" again.  That is the intended behaviour - a comparison
that silently re-read the directory would make the table jump around while the
user works in it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Person field -> Active Directory attribute.
#:
#: Only fields that really live in the directory belong here.  ``ad_password``
#: is absent because Active Directory never gives a password back, and
#: ``class_name`` because it is not an attribute but a position in the tree.
FIELD_TO_AD_ATTRIBUTE: Dict[str, str] = {
    'first_name': 'givenName',
    'last_name': 'sn',
    'ad_username': 'sAMAccountName',
    'ad_display_name': 'displayName',
    'ad_email': 'mail',
    'ad_description': 'description',
    'home_directory': 'homeDirectory',
    'home_drive': 'homeDrive',
    'group_memberships': 'memberOf',
}

#: Human readable name of every comparable field, for tooltips and reports.
FIELD_LABELS: Dict[str, str] = {
    'first_name': 'First name',
    'last_name': 'Last name',
    'ad_username': 'Username',
    'ad_display_name': 'Display name',
    'ad_email': 'Email',
    'ad_description': 'Description',
    'home_directory': 'Home directory',
    'home_drive': 'Home drive',
    'group_memberships': 'Groups',
}

#: Where a comparison is stored on the person.
DIFFERENCES_METADATA_KEY = 'ad_differences'

#: Where the discovered Active Directory attributes are stored.
AD_VALUES_METADATA_KEY = 'ad_current_values'


class FieldDifference:
    """
    One field whose value differs between the application and the directory.

    Attributes:
        field: The person attribute name, e.g. ``home_directory``.
        label: The human readable field name.
        app_value: What the application holds, already rendered as text.
        ad_value: What Active Directory held at discovery, as text.
    """

    __slots__ = ('field', 'label', 'app_value', 'ad_value')

    def __init__(self, field: str, app_value: str, ad_value: str):
        self.field = field
        self.label = FIELD_LABELS.get(field, field)
        self.app_value = app_value
        self.ad_value = ad_value

    def __repr__(self) -> str:                       # pragma: no cover - debugging
        return (f"FieldDifference({self.field!r}, app={self.app_value!r}, "
                f"ad={self.ad_value!r})")

    def __eq__(self, other) -> bool:
        return (isinstance(other, FieldDifference)
                and self.field == other.field
                and self.app_value == other.app_value
                and self.ad_value == other.ad_value)

    def as_dict(self) -> Dict[str, str]:
        """A plain dictionary, so the comparison can live in ``metadata``."""
        return {'field': self.field, 'label': self.label,
                'app_value': self.app_value, 'ad_value': self.ad_value}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FieldDifference':
        """Rebuild a difference stored with :meth:`as_dict`."""
        return cls(str(data.get('field', '')),
                   str(data.get('app_value', '')),
                   str(data.get('ad_value', '')))


def _as_text(value: Any) -> str:
    """
    Render one attribute value as the text a person would compare.

    ``None``, an empty list and an empty string all mean "not set", so they all
    render as ``''`` - otherwise a field the directory does not carry would be
    reported as different from an empty field in the application on every
    single person.
    """
    if value is None:
        return ''
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ', '.join(parts)
    return str(value).strip()


def _dn_set(value: Any) -> set:
    """
    Render a group value as a set of distinguished names, case-insensitively.

    Accepts what the application holds (``ADGroup`` objects) and what the
    directory returns (a list of DNs, or a single DN as a bare string).
    """
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    dns = set()
    for item in value:
        dn = getattr(item, 'dn', item)
        text = str(dn or '').strip()
        if text:
            dns.add(text.casefold())
    return dns


def _group_text(value: Any) -> str:
    """Render a group value as a readable, stably ordered list of names."""
    from utils.ad_entry_mapping import rdn_value

    if value is None:
        return ''
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    names = []
    for item in value:
        name = getattr(item, 'name', None)
        if not name:
            name = rdn_value(str(getattr(item, 'dn', item) or ''))
        name = str(name or '').strip()
        if name and name not in names:
            names.append(name)
    return ', '.join(sorted(names, key=str.casefold))


def ad_value_of(ad_values: Optional[Dict[str, Any]], field: str) -> Any:
    """
    Read one field out of the discovered Active Directory attributes.

    Args:
        ad_values: The snapshot stored by discovery, or ``None``.
        field: The person attribute name.

    Returns:
        The raw value the directory returned, or ``None`` when the attribute is
        not in the snapshot.
    """
    if not ad_values:
        return None
    attribute = FIELD_TO_AD_ATTRIBUTE.get(field)
    if not attribute:
        return None
    return ad_values.get(attribute)


def describe_ad_value(ad_values: Optional[Dict[str, Any]], field: str) -> str:
    """
    Render one Active Directory value as the text shown in a tooltip.

    Returns ``''`` when the directory holds no value for the field, which the
    caller renders as "(not set)" - the distinction between "empty" and "never
    discovered" is the caller's to make, using :func:`has_ad_snapshot`.
    """
    raw = ad_value_of(ad_values, field)
    if field == 'group_memberships':
        return _group_text(raw)
    return _as_text(raw)


def has_ad_snapshot(person: Any) -> bool:
    """
    Report whether "Discover in AD" has ever stored values for this person.

    Without a snapshot there is nothing to compare against, and the interface
    must say "not discovered yet" rather than "identical".
    """
    metadata = getattr(person, 'metadata', None) or {}
    return bool(metadata.get(AD_VALUES_METADATA_KEY))


def compare_person_with_ad(person: Any,
                           ad_values: Optional[Dict[str, Any]] = None
                           ) -> List[FieldDifference]:
    """
    Compare every comparable field of a person with the directory snapshot.

    Args:
        person: The person as the application holds them.
        ad_values: The snapshot.  Read from the person's metadata when omitted.

    Returns:
        One :class:`FieldDifference` per field that differs, in the fixed order
        of :data:`FIELD_TO_AD_ATTRIBUTE`.  An empty list means the two sides
        agree - and also means "nothing to compare" when the person was never
        discovered, so callers check :func:`has_ad_snapshot` first when that
        distinction matters.

    Note:
        An attribute the directory did not return is treated as "not set", not
        as "unknown".  A field the application leaves empty therefore matches a
        field Active Directory does not carry, which is what the user means by
        "identical".
    """
    if ad_values is None:
        metadata = getattr(person, 'metadata', None) or {}
        ad_values = metadata.get(AD_VALUES_METADATA_KEY)

    if not ad_values:
        return []

    differences: List[FieldDifference] = []

    for field in FIELD_TO_AD_ATTRIBUTE:
        app_raw = getattr(person, field, None)
        ad_raw = ad_value_of(ad_values, field)

        if field == 'group_memberships':
            # Groups are a set of DNs: order is meaningless and case is not
            # significant in a distinguished name.
            if _dn_set(app_raw) == _dn_set(ad_raw):
                continue
            differences.append(FieldDifference(
                field, _group_text(app_raw), _group_text(ad_raw)))
            continue

        app_text = _as_text(app_raw)
        ad_text = _as_text(ad_raw)
        if app_text == ad_text:
            continue
        differences.append(FieldDifference(field, app_text, ad_text))

    return differences


def store_comparison(person: Any,
                     differences: List[FieldDifference]) -> List[FieldDifference]:
    """
    Remember a comparison on the person so the table can paint it later.

    The differences are stored as plain dictionaries, because ``metadata`` is
    copied, compared and serialised elsewhere and must stay made of simple
    types.

    Args:
        person: The person to annotate.
        differences: The result of :func:`compare_person_with_ad`.

    Returns:
        The same list, for convenience.
    """
    metadata = getattr(person, 'metadata', None)
    if metadata is None:
        return differences
    metadata[DIFFERENCES_METADATA_KEY] = [d.as_dict() for d in differences]
    return differences


def stored_differences(person: Any) -> List[FieldDifference]:
    """
    Read back the comparison stored by :func:`store_comparison`.

    Returns an empty list when the person has never been discovered, or when
    the stored value is damaged - a broken annotation must not stop a table
    from being drawn.
    """
    metadata = getattr(person, 'metadata', None) or {}
    raw = metadata.get(DIFFERENCES_METADATA_KEY)
    if not raw:
        return []

    differences = []
    try:
        for item in raw:
            if isinstance(item, dict):
                differences.append(FieldDifference.from_dict(item))
    except TypeError:
        logger.debug("Damaged %s on %r", DIFFERENCES_METADATA_KEY, person,
                     exc_info=True)
        return []
    return differences


def differing_fields(person: Any) -> Dict[str, FieldDifference]:
    """
    The stored comparison as a ``field -> difference`` mapping.

    This is what the editing window and the person table look a field up in
    while they decide whether to outline it.
    """
    return {difference.field: difference for difference in stored_differences(person)}
