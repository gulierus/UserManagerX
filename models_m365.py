"""
Microsoft 365 domain objects
============================

Version 26, point 21 asks for *dedicated* classes for groups and teams rather
than using :class:`models.Class` directly, and it is right to: a Microsoft 365
group is not a school class.  It has an object id, a mail nickname, a
visibility, owners as well as members, and it may or may not have a team
attached.  A class has a name and a list of pupils.

The two meet in exactly one place - step 2 of the import wizard, where the user
turns each selected group into a class and gives it a name.  Everything before
that point works with the objects in this module; everything after it works
with :class:`models.Class`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

#: Values an earlier version might have written, mapped onto the members of
#: :class:`M365Status`.  Defined at module level because a plain dictionary
#: inside an ``Enum`` body becomes a member of the enumeration.
LEGACY_M365_STATUS_VALUES: Dict[str, str] = {
    "not_in_m365": "not_found_in_m365",
    "exists_in_m365": "differs_from_m365",
    "update_pending": "differs_from_m365",
    "synced": "sync_succeeded",
}


class M365Status(Enum):
    """
    What is known about a person's Microsoft 365 account.

    Deliberately the same seven states as :class:`models.ADStatus`, because the
    two operations answer the same questions and the user should not have to
    learn a second vocabulary.  They are separate enumerations rather than one
    shared enumeration because a person can be in both directories at once and
    be in a different state in each.
    """

    UNKNOWN = "unknown"
    NOT_FOUND_IN_M365 = "not_found_in_m365"
    MULTIPLE_M365_MATCHES = "multiple_m365_matches"
    MATCHES_M365 = "matches_m365"
    DIFFERS_FROM_M365 = "differs_from_m365"
    SYNC_SUCCEEDED = "sync_succeeded"
    SYNC_INCOMPLETE = "sync_incomplete"

    @classmethod
    def from_value(cls, value) -> 'M365Status':
        """
        Build a status from a stored value, accepting older spellings.

        Anything unrecognised becomes :attr:`UNKNOWN`: a status that cannot be
        read must not stop a file from loading.
        """
        if isinstance(value, cls):
            return value

        text = str(value or "").strip().lower()
        if not text:
            return cls.UNKNOWN
        try:
            return cls(text)
        except ValueError:
            pass

        legacy = LEGACY_M365_STATUS_VALUES.get(text)
        if legacy:
            return cls(legacy)

        logger.debug("Unknown Microsoft 365 status %r - treating it as unknown",
                     value)
        return cls.UNKNOWN

    @property
    def label(self) -> str:
        """Short text for a table cell."""
        return {
            M365Status.UNKNOWN: "Not checked",
            M365Status.NOT_FOUND_IN_M365: "Not in Microsoft 365",
            M365Status.MULTIPLE_M365_MATCHES: "Several matches",
            M365Status.MATCHES_M365: "Identical",
            M365Status.DIFFERS_FROM_M365: "Differs",
            M365Status.SYNC_SUCCEEDED: "Synchronised",
            M365Status.SYNC_INCOMPLETE: "Synchronised with errors",
        }[self]

    @property
    def description(self) -> str:
        """Full sentence for a tooltip."""
        return {
            M365Status.UNKNOWN:
                "Microsoft 365 has not been asked about this person yet. "
                "Press 'Discover in Microsoft 365'.",
            M365Status.NOT_FOUND_IN_M365:
                "No Microsoft 365 account matches this person. Synchronising "
                "will create one.",
            M365Status.MULTIPLE_M365_MATCHES:
                "Several Microsoft 365 accounts match this person, so it is "
                "not clear which one is hers. She is left out of the "
                "synchronisation until the ambiguity is resolved.",
            M365Status.MATCHES_M365:
                "Found in Microsoft 365, and every field the application "
                "manages holds the same value on both sides.",
            M365Status.DIFFERS_FROM_M365:
                "Found in Microsoft 365, but at least one field differs from "
                "what Microsoft 365 held when it was discovered.",
            M365Status.SYNC_SUCCEEDED:
                "The last synchronisation of this person finished with no "
                "errors.",
            M365Status.SYNC_INCOMPLETE:
                "The last synchronisation of this person finished, but one or "
                "more steps failed. See the log for what did not apply.",
        }[self]

    @property
    def is_in_m365(self) -> bool:
        """True when an account for this person is known to exist."""
        return self in (M365Status.MATCHES_M365, M365Status.DIFFERS_FROM_M365,
                        M365Status.SYNC_SUCCEEDED, M365Status.SYNC_INCOMPLETE)

    @property
    def is_settled(self) -> bool:
        """True when nothing is known to be waiting to be written."""
        return self in (M365Status.MATCHES_M365, M365Status.SYNC_SUCCEEDED)


# ---------------------------------------------------------------------------
# People inside a group
# ---------------------------------------------------------------------------

@dataclass
class M365Member:
    """
    One account as Microsoft 365 reports it inside a group.

    This is *not* a :class:`models.Person`: it is what the directory returned,
    before the application decides whether it is a pupil, a teacher or a
    service account.

    Attributes:
        object_id: The Microsoft 365 object id (a GUID).  The only stable key.
        display_name: The name shown in the portal.
        user_principal_name: The sign-in name, ``novakjan@skola.cz``.
        mail: The primary email address, which may differ from the UPN.
        given_name: First name, when the directory carries one.
        surname: Last name.
        is_owner: True when this account owns the group rather than only
            belonging to it.
        account_enabled: Whether the account may sign in.
        raw: Anything else the directory returned, kept for reference.
    """

    object_id: str = ""
    display_name: str = ""
    user_principal_name: str = ""
    mail: str = ""
    given_name: str = ""
    surname: str = ""
    is_owner: bool = False
    account_enabled: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def best_name(self) -> str:
        """The most useful name available, never empty for a real account."""
        if self.display_name.strip():
            return self.display_name.strip()
        if self.given_name or self.surname:
            return f"{self.given_name} {self.surname}".strip()
        return self.user_principal_name or self.mail or self.object_id

    def split_name(self) -> tuple:
        """
        Best effort ``(first_name, last_name)`` for this account.

        Microsoft 365 usually carries ``givenName`` and ``surname`` separately,
        which is the reliable answer.  When it does not, the display name is
        split on the last space - ``"Jan Novák"`` -> ``("Jan", "Novák")`` -
        because a person may have two given names but rarely two surnames in
        this field.  A single word becomes the first name, leaving the surname
        empty rather than inventing one.
        """
        if self.given_name.strip() or self.surname.strip():
            return (self.given_name.strip(), self.surname.strip())

        name = (self.display_name or "").strip()
        if not name:
            return ("", "")
        if " " not in name:
            return (name, "")
        first, _, last = name.rpartition(" ")
        return (first.strip(), last.strip())


# ---------------------------------------------------------------------------
# Groups and teams
# ---------------------------------------------------------------------------

@dataclass
class M365Group:
    """
    A Microsoft 365 or security group, as the directory holds it.

    Attributes:
        object_id: The group's GUID.
        display_name: The name shown in the portal.
        mail_nickname: The alias, used to build the group's address.
        description: Free text.
        mail: The group's email address, for mail-enabled groups.
        visibility: ``Public``, ``Private`` or ``HiddenMembership``.
        group_types: What Microsoft returned; ``Unified`` marks a
            Microsoft 365 group as opposed to a plain security group.
        mail_enabled: Whether the group has a mailbox.
        security_enabled: Whether the group can be used for permissions.
        created: When the group was created.
        owners: Accounts that may manage the group.
        members: Accounts that belong to it.
        has_team: Whether a Microsoft Team is attached.
        raw: Anything else the directory returned.
    """

    object_id: str = ""
    display_name: str = ""
    mail_nickname: str = ""
    description: str = ""
    mail: str = ""
    visibility: str = ""
    group_types: List[str] = field(default_factory=list)
    mail_enabled: bool = False
    security_enabled: bool = False
    created: Optional[datetime] = None
    owners: List[M365Member] = field(default_factory=list)
    members: List[M365Member] = field(default_factory=list)
    has_team: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    #: True once the owners and members have actually been read.  An empty
    #: member list means "no members" only when this is set; before that it
    #: means "not loaded yet", and the two must not be confused.
    members_loaded: bool = False

    @property
    def is_unified(self) -> bool:
        """True for a Microsoft 365 group, False for a plain security group."""
        return any(str(t).lower() == "unified" for t in self.group_types)

    @property
    def kind(self) -> str:
        """How the group is described to the user."""
        if self.has_team:
            return "Team"
        if self.is_unified:
            return "Microsoft 365"
        if self.security_enabled:
            return "Security"
        return "Group"

    @property
    def member_count(self) -> int:
        """How many members are known."""
        return len(self.members)

    @property
    def owner_names(self) -> List[str]:
        """Readable names of the owners, in the order the directory gave them."""
        return [owner.best_name for owner in self.owners]

    def matches(self, needle: str) -> bool:
        """
        Whether this group matches a search box entry.

        The name, the alias, the description and the email are all searched,
        case-insensitively, because a user looking for "6.A" may well have
        typed the alias rather than the display name.
        """
        text = (needle or "").strip().casefold()
        if not text:
            return True
        return any(text in (value or "").casefold() for value in
                   (self.display_name, self.mail_nickname, self.description,
                    self.mail))


@dataclass
class M365Team(M365Group):
    """
    A Microsoft Team - a Microsoft 365 group with a team attached.

    Modelled as a specialisation rather than a separate type because that is
    what it is in the directory: every team *is* a group, with the same id, and
    Microsoft adds team features on top.  Keeping them one hierarchy means the
    selection wizard lists both without special cases.

    Attributes:
        web_url: The link that opens the team in Microsoft Teams.
        is_archived: Whether the team is read-only.
    """

    web_url: str = ""
    is_archived: bool = False

    def __post_init__(self):
        # A team always has a team, by definition - the flag exists so a plain
        # group can say it has one too.
        self.has_team = True


@dataclass
class M365GroupSelection:
    """
    One group the user picked in step 1, and the class it becomes in step 2.

    Keeping the two together is what lets the wizard move back and forth
    between its steps without losing what was typed.

    Attributes:
        group: The group or team itself.
        class_name: The name the user gave the class, possibly still a
            template.
        include_owners: Whether the owners become pupils as well as the
            members.  Off by default: in a school group the owner is the
            teacher.
    """

    group: M365Group
    class_name: str = ""
    include_owners: bool = False

    def people(self) -> List[M365Member]:
        """
        The accounts that should become persons of the class.

        Owners are included only when asked for, and an account that is both
        an owner and a member is listed once.
        """
        chosen: List[M365Member] = []
        seen = set()
        for member in list(self.group.members) + (
                list(self.group.owners) if self.include_owners else []):
            key = (member.object_id or member.user_principal_name or
                   member.best_name).casefold()
            if key in seen:
                continue
            seen.add(key)
            chosen.append(member)
        return chosen
