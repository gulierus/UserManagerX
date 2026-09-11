"""
Source serialisation
====================

One single implementation of "Source <-> plain dictionary", used by

* :mod:`utils.json_export_task`   - writing the encrypted JSON export,
* :mod:`operations.json_export`   - the export widget,
* :mod:`sources.file_source`      - reading an encrypted JSON file back in.

Before this module existed, each of those three places had its own copy of the
conversion.  The copies had drifted apart:

* the export dropped the home directory, the group memberships, the password
  policy flags and the account status, so an export/import round trip silently
  lost that data;
* ``JSONExportWidget.source_to_json()`` built the dictionary but never returned
  it.

Keeping the conversion in one place makes a round trip lossless and keeps the
three call sites in sync.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from models import ADGroup, Class, Person, Source, VerificationStatus

logger = logging.getLogger(__name__)

#: Bumped whenever the on-disk structure changes in an incompatible way.
EXPORT_FORMAT_VERSION = 2


def _plain_copy(value: Any) -> Any:
    """
    Return a JSON-safe, independent copy of *value*.

    Two problems are solved at once:

    * **Aliasing** - the exported dictionary used to hold the *same* metadata
      dict as the live model, so editing the imported source also changed the
      exported one (and vice versa).
    * **Serialisability** - metadata legitimately holds values ``json.dumps``
      cannot encode (the AD discovery step stores ``datetime`` objects there),
      which made the whole export fail at the very last step. Those values are
      converted to their text form.
    """
    if isinstance(value, dict):
        return {str(k): _plain_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_copy(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def group_to_dict(group: ADGroup) -> Dict[str, Any]:
    """Convert an :class:`~models.ADGroup` to a plain dictionary."""
    return {
        "name": group.name,
        "dn": group.dn,
        "description": group.description,
        "group_type": group.group_type,
        "members_count": group.members_count,
        "metadata": _plain_copy(group.metadata),
    }


def group_from_dict(data: Dict[str, Any]) -> Optional[ADGroup]:
    """
    Rebuild an :class:`~models.ADGroup` from a dictionary.

    Args:
        data: Dictionary produced by :func:`group_to_dict`.

    Returns:
        The group, or ``None`` when the entry is unusable.
    """
    if not isinstance(data, dict):
        return None

    dn = data.get("dn")
    if not dn or not isinstance(dn, str):
        return None

    # A single malformed entry must not abort the import of the whole file.
    try:
        members_count = int(data.get("members_count") or 0)
    except (TypeError, ValueError):
        logger.warning("Group %r has an unusable members_count %r - using 0",
                       dn, data.get("members_count"))
        members_count = 0

    return ADGroup(
        name=data.get("name") or dn.split(",")[0].split("=")[-1],
        dn=dn,
        description=data.get("description"),
        group_type=data.get("group_type"),
        members_count=members_count,
        # Verification state is machine specific and is deliberately not
        # carried over - the groups must be verified against the target AD.
        verification_status=VerificationStatus.NOT_VERIFIED,
        metadata=_plain_copy(data.get("metadata")) or {},
    )


def person_to_dict(person: Person) -> Dict[str, Any]:
    """Convert a :class:`~models.Person` to a plain dictionary."""
    return {
        "first_name": person.first_name,
        "last_name": person.last_name,
        "class_name": person.class_name,
        "ad_username": person.ad_username,
        "ad_password": person.ad_password,
        "ad_display_name": person.ad_display_name,
        "ad_email": person.ad_email,
        "ad_description": person.ad_description,
        "ad_ou_path": person.ad_ou_path,
        # The distinguished name identifies the account in AD; dropping it
        # forced a fresh discovery after every export/import round trip.
        "ad_dn": person.ad_dn,
        # --- previously lost on export -------------------------------------
        "home_directory": person.home_directory,
        "home_drive": person.home_drive,
        "group_memberships": [group_to_dict(g) for g in person.group_memberships],
        "password_must_change": person.password_must_change,
        "password_cannot_change": person.password_cannot_change,
        "password_never_expires": person.password_never_expires,
        "account_enabled": person.account_enabled,
        "metadata": _plain_copy(person.metadata),
    }


def person_from_dict(data: Dict[str, Any]) -> Person:
    """
    Rebuild a :class:`~models.Person` from a dictionary.

    Args:
        data: Dictionary produced by :func:`person_to_dict`. Files written by
            older versions simply lack the newer keys; they default to the
            same values a freshly created person has.

    Returns:
        The person.

    Raises:
        ValueError: If a required field is missing.
    """
    for field in ("first_name", "last_name", "class_name"):
        if field not in data:
            raise ValueError(f"Person is missing required '{field}' field")
        if data[field] is None:
            # An explicit null used to build a Person with no name at all,
            # which then broke credential generation much further downstream.
            raise ValueError(f"Person field '{field}' must not be null")

    groups: List[ADGroup] = []
    for raw_group in data.get("group_memberships") or []:
        group = group_from_dict(raw_group)
        if group is not None:
            groups.append(group)
        else:
            logger.warning("Skipping unusable group entry: %r", raw_group)

    person_metadata = data.get("metadata")
    if person_metadata is not None and not isinstance(person_metadata, dict):
        logger.warning("Ignoring non-dictionary person metadata: %r", person_metadata)
        person_metadata = None

    return Person(
        first_name=data["first_name"],
        last_name=data["last_name"],
        class_name=data["class_name"],
        ad_username=data.get("ad_username"),
        ad_password=data.get("ad_password"),
        ad_display_name=data.get("ad_display_name"),
        ad_email=data.get("ad_email"),
        ad_description=data.get("ad_description"),
        ad_ou_path=data.get("ad_ou_path"),
        ad_dn=data.get("ad_dn"),
        home_directory=data.get("home_directory"),
        home_drive=data.get("home_drive"),
        group_memberships=groups,
        password_must_change=bool(data.get("password_must_change", False)),
        password_cannot_change=bool(data.get("password_cannot_change", False)),
        password_never_expires=bool(data.get("password_never_expires", False)),
        account_enabled=bool(data.get("account_enabled", False)),
        metadata=_plain_copy(person_metadata) or {},
    )


def source_to_dict(source: Source) -> Dict[str, Any]:
    """
    Convert a whole :class:`~models.Source` into a JSON-serialisable dictionary.

    Args:
        source: The source to convert.

    Returns:
        The dictionary representation.
    """
    data: Dict[str, Any] = {
        "format_version": EXPORT_FORMAT_VERSION,
        "name": source.name,
        "source_type": source.source_type,
        "metadata": _plain_copy(source.metadata),
        "source_info": _plain_copy(source.source_info),
        "export_date": datetime.now().isoformat(),
        "classes": [],
    }

    for cls in source.classes:
        data["classes"].append({
            "name": cls.name,
            "metadata": cls.metadata,
            "persons": [person_to_dict(person) for person in cls.persons],
        })

    logger.debug(
        "Converted source '%s' to a dictionary (%d classes, %d persons)",
        source.name, len(source.classes), len(source.get_all_persons()),
    )
    return data


def source_from_dict(data: Dict[str, Any], default_name: str = "Imported",
                     readonly: bool = True) -> Source:
    """
    Rebuild a :class:`~models.Source` from a dictionary.

    Args:
        data: Dictionary produced by :func:`source_to_dict`.
        default_name: Name to use when the file carries none.
        readonly: Whether the resulting source is marked read-only.

    Returns:
        The reconstructed source.

    Raises:
        ValueError: If the structure is invalid.
    """
    if not isinstance(data, dict):
        raise ValueError("The file does not contain a JSON object")

    stored_metadata = data.get("metadata")
    if stored_metadata is not None and not isinstance(stored_metadata, dict):
        logger.warning("Ignoring non-dictionary 'metadata' field: %r", stored_metadata)
        stored_metadata = None

    source = Source(
        name=data.get("name") or default_name,
        source_type=data.get("source_type", "file"),
        readonly=readonly,
        metadata=_plain_copy(stored_metadata) or {},
    )

    stored_info = data.get("source_info")
    if isinstance(stored_info, dict):
        source.source_info.update(stored_info)

    classes_data = data.get("classes", [])
    if not isinstance(classes_data, list):
        raise ValueError("'classes' field must be a list")

    for class_data in classes_data:
        if not isinstance(class_data, dict):
            raise ValueError("Each class must be a dictionary")
        if "name" not in class_data:
            raise ValueError("Class is missing required 'name' field")

        class_metadata = class_data.get("metadata")
        if class_metadata is not None and not isinstance(class_metadata, dict):
            logger.warning("Ignoring non-dictionary class metadata: %r", class_metadata)
            class_metadata = None

        cls = Class(
            name=class_data["name"],
            metadata=_plain_copy(class_metadata) or {},
        )

        persons_data = class_data.get("persons", [])
        if not isinstance(persons_data, list):
            raise ValueError(f"'persons' field in class '{cls.name}' must be a list")

        for person_data in persons_data:
            if not isinstance(person_data, dict):
                raise ValueError("Each person must be a dictionary")
            cls.add_person(person_from_dict(person_data))

        source.add_class(cls)

    logger.info("Parsed source '%s' with %d classes", source.name, len(source.classes))
    return source
