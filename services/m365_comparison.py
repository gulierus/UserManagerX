"""
Comparing a person with their Microsoft 365 account

The counterpart of :mod:`services.ad_comparison`, and it answers the same two
questions - do the two sides agree, and which fields do not - so that
:class:`~models_m365.M365Status` can distinguish *Identical* from *Differs*
(Version 26, point 21 e, which asks for a Status column with the same meaning
as the Active Directory one).

The fields are different, though, and that is the point of a separate module:
Microsoft 365 has a user principal name and a mail nickname, and it has no home
directory, no home drive and no distinguished name.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.ad_comparison import FieldDifference

logger = logging.getLogger(__name__)

#: Person field -> Microsoft Graph attribute.
#:
#: ``m365_password`` is absent because Microsoft never gives a password back,
#: and ``class_name`` because a class is a group membership rather than an
#: attribute of the account.
FIELD_TO_GRAPH_ATTRIBUTE: Dict[str, str] = {
    'first_name': 'givenName',
    'last_name': 'surname',
    'm365_user_principal_name': 'userPrincipalName',
    'm365_display_name': 'displayName',
    'm365_mail_nickname': 'mailNickname',
    'm365_usage_location': 'usageLocation',
    'account_enabled': 'accountEnabled',
}

#: Human readable name of every comparable field.
FIELD_LABELS: Dict[str, str] = {
    'first_name': 'First name',
    'last_name': 'Last name',
    'm365_user_principal_name': 'Sign-in name',
    'm365_display_name': 'Display name',
    'm365_mail_nickname': 'Alias',
    'm365_usage_location': 'Usage location',
    'account_enabled': 'Account enabled',
}

#: Where a comparison is stored on the person.
DIFFERENCES_METADATA_KEY = 'm365_differences'

#: Where the discovered Microsoft 365 attributes are stored.
M365_VALUES_METADATA_KEY = 'm365_current_values'


def _as_text(value: Any) -> str:
    """Render one value as the text a person would compare."""
    if value is None:
        return ''
    if isinstance(value, bool):
        # 'Yes'/'No' rather than 'True'/'False': the tooltip is read by a user.
        return 'Yes' if value else 'No'
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ', '.join(parts)
    return str(value).strip()


def graph_value_of(values: Optional[Dict[str, Any]], field: str) -> Any:
    """Read one field out of the discovered Microsoft 365 attributes."""
    if not values:
        return None
    attribute = FIELD_TO_GRAPH_ATTRIBUTE.get(field)
    if not attribute:
        return None
    return values.get(attribute)


def has_m365_snapshot(person: Any) -> bool:
    """Whether "Discover in Microsoft 365" has ever stored values for them."""
    metadata = getattr(person, 'metadata', None) or {}
    return bool(metadata.get(M365_VALUES_METADATA_KEY))


def compare_person_with_m365(person: Any,
                             values: Optional[Dict[str, Any]] = None
                             ) -> List[FieldDifference]:
    """
    Compare every comparable field of a person with the Microsoft 365 snapshot.

    Args:
        person: The person as the application holds them.
        values: The snapshot.  Read from the person's metadata when omitted.

    Returns:
        One :class:`~services.ad_comparison.FieldDifference` per differing
        field, in the fixed order of :data:`FIELD_TO_GRAPH_ATTRIBUTE`.

    Note:
        A field Microsoft did not return is treated as "not set" rather than
        "unknown", so an empty field on both sides counts as identical.  The
        one exception is ``accountEnabled``, where Microsoft always answers and
        a missing value really would be unknown - it is skipped instead of
        being reported as a difference against every person.
    """
    if values is None:
        metadata = getattr(person, 'metadata', None) or {}
        values = metadata.get(M365_VALUES_METADATA_KEY)

    if not values:
        return []

    differences: List[FieldDifference] = []

    for field, attribute in FIELD_TO_GRAPH_ATTRIBUTE.items():
        if field == 'account_enabled' and attribute not in values:
            continue

        app_text = _as_text(getattr(person, field, None))
        graph_text = _as_text(values.get(attribute))
        if app_text == graph_text:
            continue

        difference = FieldDifference(field, app_text, graph_text)
        difference.label = FIELD_LABELS.get(field, field)
        differences.append(difference)

    return differences


def store_comparison(person: Any,
                     differences: List[FieldDifference]) -> List[FieldDifference]:
    """Remember a comparison on the person, as plain data."""
    metadata = getattr(person, 'metadata', None)
    if metadata is None:
        return differences
    metadata[DIFFERENCES_METADATA_KEY] = [d.as_dict() for d in differences]
    return differences


def stored_differences(person: Any) -> List[FieldDifference]:
    """Read back the comparison stored by :func:`store_comparison`."""
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
    """The stored comparison as a ``field -> difference`` mapping."""
    return {difference.field: difference
            for difference in stored_differences(person)}


def changes_for_graph(person: Any) -> Dict[str, Any]:
    """
    The attributes that would have to be written to bring Microsoft 365 into
    line with the application.

    Args:
        person: The person to synchronise.

    Returns:
        ``graph attribute -> value``, empty when nothing needs writing.  Only
        fields the application actually holds a value for are included: an
        empty field here means "not managed", not "clear it in the directory".
    """
    differences = differing_fields(person)
    changes: Dict[str, Any] = {}

    for field, attribute in FIELD_TO_GRAPH_ATTRIBUTE.items():
        if field not in differences:
            continue
        value = getattr(person, field, None)
        if isinstance(value, bool):
            changes[attribute] = value
            continue
        text = (value or "") if isinstance(value, str) else value
        if not text:
            # Blanking a directory attribute because the application happens
            # not to manage it would destroy data nobody asked to remove.
            continue
        changes[attribute] = text

    return changes
