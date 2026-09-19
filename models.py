"""
Data models for Student Management System
VERSION 6 - Added verification tracking for groups and templates
"""

import logging
from typing import List, Optional, Dict, Any, Set
from dataclasses import dataclass, field
from copy import deepcopy
import unicodedata
from enum import Enum
from datetime import datetime
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


#: Values written by earlier versions of the application, mapped onto the
#: members of :class:`ADStatus`.
#:
#: ``create_pending`` was never assigned anywhere, so it could only ever mean
#: "nothing is known".  ``exists_in_ad`` said only that an account was found
#: and nothing about whether the values agreed; it is read back as "differs",
#: because that is the interpretation that cannot cause a difference to be
#: missed.
#:
#: Defined at module level on purpose: a plain dictionary inside an ``Enum``
#: body becomes a member of the enumeration.
LEGACY_AD_STATUS_VALUES = {
    "not_in_ad": "not_found_in_ad",
    "exists_in_ad": "differs_from_ad",
    "create_pending": "unknown",
    "update_pending": "differs_from_ad",
    "synced": "sync_succeeded",
    "ambiguous": "multiple_ad_matches",
}


class ADStatus(Enum):
    """
    What is known about a person's Active Directory account.

    The members answer exactly one question each, so a status can never mean
    two things at once:

    ========================  ==================================================
    Member                    Meaning
    ========================  ==================================================
    ``UNKNOWN``               Nothing has been looked up yet.
    ``NOT_FOUND_IN_AD``       Searched for, not present in the searched area.
    ``MULTIPLE_AD_MATCHES``   Several accounts match - we do not know which one.
    ``MATCHES_AD``            Found, and every comparable field is identical.
    ``DIFFERS_FROM_AD``       Found, but at least one field differs.
    ``SYNC_SUCCEEDED``        Synchronisation finished with no errors.
    ``SYNC_INCOMPLETE``       Synchronisation finished, but something failed.
    ========================  ==================================================

    The comparison behind ``MATCHES_AD`` / ``DIFFERS_FROM_AD`` is made against
    the values "Discover in AD" read, not against the directory as it is right
    now - see :mod:`services.ad_comparison`.
    """

    UNKNOWN = "unknown"
    NOT_FOUND_IN_AD = "not_found_in_ad"
    MULTIPLE_AD_MATCHES = "multiple_ad_matches"
    MATCHES_AD = "matches_ad"
    DIFFERS_FROM_AD = "differs_from_ad"
    SYNC_SUCCEEDED = "sync_succeeded"
    SYNC_INCOMPLETE = "sync_incomplete"

    @classmethod
    def from_value(cls, value) -> 'ADStatus':
        """
        Build a status from a stored value, accepting older spellings.

        Args:
            value: An :class:`ADStatus`, one of its values, or a value written
                by an earlier version of the application.

        Returns:
            The matching member, or :attr:`UNKNOWN` for anything unrecognised -
            a status that cannot be read must not stop a file from loading.
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

        legacy = LEGACY_AD_STATUS_VALUES.get(text)
        if legacy:
            return cls(legacy)

        logger.debug("Unknown AD status %r - treating it as unknown", value)
        return cls.UNKNOWN

    @property
    def label(self) -> str:
        """Short text for a table cell."""
        return {
            ADStatus.UNKNOWN: "Not checked",
            ADStatus.NOT_FOUND_IN_AD: "Not in AD",
            ADStatus.MULTIPLE_AD_MATCHES: "Several matches",
            ADStatus.MATCHES_AD: "Identical",
            ADStatus.DIFFERS_FROM_AD: "Differs",
            ADStatus.SYNC_SUCCEEDED: "Synchronised",
            ADStatus.SYNC_INCOMPLETE: "Synchronised with errors",
        }[self]

    @property
    def description(self) -> str:
        """Full sentence for a tooltip."""
        return {
            ADStatus.UNKNOWN:
                "Active Directory has not been asked about this person yet. "
                "Press 'Discover in AD'.",
            ADStatus.NOT_FOUND_IN_AD:
                "This person was not found in the searched area of Active "
                "Directory. Synchronising will create the account.",
            ADStatus.MULTIPLE_AD_MATCHES:
                "Several Active Directory accounts match this person, so it is "
                "not clear which one is hers. She is left out of the "
                "synchronisation until the ambiguity is resolved.",
            ADStatus.MATCHES_AD:
                "Found in Active Directory, and every field the application "
                "manages holds the same value on both sides.",
            ADStatus.DIFFERS_FROM_AD:
                "Found in Active Directory, but at least one field differs "
                "from what Active Directory held when it was discovered.",
            ADStatus.SYNC_SUCCEEDED:
                "The last synchronisation of this person finished with no "
                "errors.",
            ADStatus.SYNC_INCOMPLETE:
                "The last synchronisation of this person finished, but one or "
                "more steps failed. See the log for what did not apply.",
        }[self]

    @property
    def is_in_ad(self) -> bool:
        """True when an account for this person is known to exist."""
        return self in (ADStatus.MATCHES_AD, ADStatus.DIFFERS_FROM_AD,
                        ADStatus.SYNC_SUCCEEDED, ADStatus.SYNC_INCOMPLETE)

    @property
    def is_settled(self) -> bool:
        """True when nothing is known to be waiting to be written to AD."""
        return self in (ADStatus.MATCHES_AD, ADStatus.SYNC_SUCCEEDED)


class VerificationStatus(Enum):
    """Status of group verification in AD"""
    NOT_VERIFIED = "not_verified"
    VERIFIED_EXISTS = "verified_exists"
    VERIFIED_NOT_FOUND = "verified_not_found"
    VERIFICATION_FAILED = "verification_failed"


@dataclass
class ADGroup:
    """
    Represents an Active Directory group with verification tracking
    
    Attributes:
        name: Group name (CN)
        dn: Distinguished Name of the group
        description: Optional group description
        group_type: Type of group (security, distribution)
        members_count: Number of members (if known)
        verification_status: Whether group was verified in AD
        last_verified: When was the group last verified
        verification_error: Error message if verification failed
        metadata: Additional group information
    """
    name: str
    dn: str
    description: Optional[str] = None
    group_type: Optional[str] = None
    members_count: int = 0
    verification_status: VerificationStatus = VerificationStatus.NOT_VERIFIED
    last_verified: Optional[datetime] = None
    verification_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def mark_verified(self, exists: bool, error: Optional[str] = None):
        """
        Mark group as verified
        
        Args:
            exists: Whether group exists in AD
            error: Error message if verification failed
        """
        self.last_verified = datetime.now()
        
        if error:
            self.verification_status = VerificationStatus.VERIFICATION_FAILED
            self.verification_error = error
        elif exists:
            self.verification_status = VerificationStatus.VERIFIED_EXISTS
            self.verification_error = None
        else:
            self.verification_status = VerificationStatus.VERIFIED_NOT_FOUND
            self.verification_error = None
    
    def is_verified(self) -> bool:
        """Check if group has been verified"""
        return self.verification_status != VerificationStatus.NOT_VERIFIED
    
    def exists_in_ad(self) -> bool:
        """Check if group exists in AD (based on last verification)"""
        return self.verification_status == VerificationStatus.VERIFIED_EXISTS
    
    def get_status_icon(self) -> str:
        """Get icon representing verification status"""
        if self.verification_status == VerificationStatus.VERIFIED_EXISTS:
            return "✓"
        elif self.verification_status == VerificationStatus.VERIFIED_NOT_FOUND:
            return "✗"
        elif self.verification_status == VerificationStatus.VERIFICATION_FAILED:
            return "⚠"
        else:
            return "?"
    
    def get_status_text(self) -> str:
        """Get text description of verification status"""
        if self.verification_status == VerificationStatus.VERIFIED_EXISTS:
            return "Verified - Exists in AD"
        elif self.verification_status == VerificationStatus.VERIFIED_NOT_FOUND:
            return "Verified - Not found in AD"
        elif self.verification_status == VerificationStatus.VERIFICATION_FAILED:
            return f"Verification failed: {self.verification_error}"
        else:
            return "Not verified"
    
    def __eq__(self, other):
        """Groups are equal if they have the same DN"""
        if not isinstance(other, ADGroup):
            return False
        return self.dn.lower() == other.dn.lower()
    
    def __hash__(self):
        """Hash based on DN for use in sets"""
        return hash(self.dn.lower())
    
    def __repr__(self):
        return f"ADGroup({self.name}, {self.dn}, {self.verification_status.value})"


@dataclass
class GroupTemplate:
    """
    Template for quickly assigning multiple groups to users
    
    Attributes:
        name: Template name
        description: Template description
        groups: List of ADGroup objects in this template
        last_verified: When template groups were last verified
        metadata: Additional template information
        created_date: When template was created
        modified_date: When template was last modified
    """
    name: str
    description: str = ""
    groups: List[ADGroup] = field(default_factory=list)
    last_verified: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_date: Optional[datetime] = None
    modified_date: Optional[datetime] = None
    
    def __post_init__(self):
        """Set creation date if not provided"""
        if self.created_date is None:
            self.created_date = datetime.now()
        if self.modified_date is None:
            self.modified_date = datetime.now()
    
    def add_group(self, group: ADGroup):
        """Add a group to this template"""
        if group not in self.groups:
            self.groups.append(group)
            self.modified_date = datetime.now()
    
    def remove_group(self, group: ADGroup):
        """Remove a group from this template"""
        if group in self.groups:
            self.groups.remove(group)
            self.modified_date = datetime.now()
    
    def get_group_count(self) -> int:
        """Get number of groups in template"""
        return len(self.groups)
    
    def get_verified_count(self) -> int:
        """Get number of verified groups"""
        return sum(1 for g in self.groups if g.is_verified())
    
    def get_existing_count(self) -> int:
        """Get number of groups that exist in AD"""
        return sum(1 for g in self.groups if g.exists_in_ad())
    
    def has_unverified_groups(self) -> bool:
        """Check if template has any unverified groups"""
        return any(not g.is_verified() for g in self.groups)
    
    def mark_verified(self):
        """Mark template as verified (update timestamp)"""
        self.last_verified = datetime.now()
    
    def get_verification_summary(self) -> str:
        """Get summary of verification status"""
        total = len(self.groups)
        verified = self.get_verified_count()
        existing = self.get_existing_count()
        
        if verified == 0:
            return "Not verified"
        elif verified == total:
            return f"All verified ({existing}/{total} exist in AD)"
        else:
            return f"Partially verified ({verified}/{total} verified, {existing} exist)"
    
    def copy(self, new_name: str) -> 'GroupTemplate':
        """Create a copy of this template with a new name"""
        return GroupTemplate(
            name=new_name,
            description=self.description,
            groups=self.groups.copy(),
            metadata=self.metadata.copy()
        )
    
    def __repr__(self):
        return f"GroupTemplate({self.name}, {len(self.groups)} groups)"


@dataclass
class Person:
    """Represents a student/person with all necessary attributes and automatic dirty tracking"""
    
    # Basic info (private fields with properties)
    _first_name: str = field(default="", init=False, repr=False)
    _last_name: str = field(default="", init=False, repr=False)
    _class_name: str = field(default="", init=False, repr=False)
    
    # Active Directory attributes (private fields with properties)
    _ad_username: Optional[str] = field(default=None, init=False, repr=False)
    _ad_password: Optional[str] = field(default=None, init=False, repr=False)
    _ad_display_name: Optional[str] = field(default=None, init=False, repr=False)
    _ad_email: Optional[str] = field(default=None, init=False, repr=False)
    _ad_description: Optional[str] = field(default=None, init=False, repr=False)
    _ad_ou_path: Optional[str] = field(default=None, init=False, repr=False)
    _ad_dn: Optional[str] = field(default=None, init=False, repr=False)
    
    # Microsoft 365 attributes (private fields with properties).
    #
    # Deliberately separate from the Active Directory ones: a person can exist
    # in both directories with different values, and point 21 requires the
    # Microsoft 365 table not to show Active Directory fields.
    _m365_user_principal_name: Optional[str] = field(default=None, init=False, repr=False)
    _m365_display_name: Optional[str] = field(default=None, init=False, repr=False)
    _m365_mail_nickname: Optional[str] = field(default=None, init=False, repr=False)
    _m365_password: Optional[str] = field(default=None, init=False, repr=False)
    _m365_usage_location: Optional[str] = field(default=None, init=False, repr=False)

    # Home Directory (private field with property)
    _home_directory: Optional[str] = field(default=None, init=False, repr=False)
    _home_drive: Optional[str] = field(default=None, init=False, repr=False)
    
    # Group memberships (private field with property)
    _group_memberships: List[ADGroup] = field(default_factory=list, init=False, repr=False)
    
    # Password policy settings (private fields with properties)
    _password_must_change: bool = field(default=False, init=False, repr=False)
    _password_cannot_change: bool = field(default=False, init=False, repr=False)
    _password_never_expires: bool = field(default=False, init=False, repr=False)
    
    # Account status (private field with property)
    _account_enabled: bool = field(default=False, init=False, repr=False)
    
    # AD synchronization tracking
    ad_status: ADStatus = field(default=ADStatus.UNKNOWN)
    ad_version: Optional[str] = None
    ad_last_sync: Optional[datetime] = None

    # Microsoft 365 synchronization tracking.  The object id is assigned by
    # Microsoft and never edited here, so it needs no dirty tracking.
    m365_object_id: Optional[str] = None
    m365_status: 'M365Status' = None
    m365_last_sync: Optional[datetime] = None
    
    # Additional metadata
    metadata: Dict = field(default_factory=dict)
    
    # Dirty tracking - private fields
    _original_values: Dict[str, Any] = field(default_factory=dict, repr=False)
    _dirty_fields: Set[str] = field(default_factory=set, repr=False)
    _tracking_enabled: bool = field(default=True, repr=False)
    #: The status held before the first local edit, so undoing every edit can
    #: restore it instead of guessing between "Identical" and "Synchronised".
    _status_before_edit: Optional['ADStatus'] = field(default=None, repr=False)
    #: The same, for Microsoft 365.
    _m365_status_before_edit: Optional['M365Status'] = field(default=None,
                                                             repr=False)
    
    def __init__(self, first_name: str, last_name: str, class_name: str,
                 ad_username: Optional[str] = None,
                 ad_password: Optional[str] = None,
                 ad_display_name: Optional[str] = None,
                 ad_email: Optional[str] = None,
                 ad_description: Optional[str] = None,
                 ad_ou_path: Optional[str] = None,
                 ad_dn: Optional[str] = None,
                 home_directory: Optional[str] = None,
                 home_drive: Optional[str] = None,
                 m365_user_principal_name: Optional[str] = None,
                 m365_display_name: Optional[str] = None,
                 m365_mail_nickname: Optional[str] = None,
                 m365_password: Optional[str] = None,
                 m365_usage_location: Optional[str] = None,
                 m365_object_id: Optional[str] = None,
                 m365_status=None,
                 group_memberships: Optional[List[ADGroup]] = None,
                 password_must_change: bool = False,
                 password_cannot_change: bool = False,
                 password_never_expires: bool = False,
                 account_enabled: bool = False,
                 ad_status: ADStatus = ADStatus.UNKNOWN,
                 ad_version: Optional[str] = None,
                 ad_last_sync: Optional[datetime] = None,
                 metadata: Optional[Dict] = None):
        """Initialize person with automatic dirty tracking"""
        # Initialize tracking fields first
        self._original_values = {}
        self._dirty_fields = set()
        self._status_before_edit = None
        self._m365_status_before_edit = None
        self._tracking_enabled = False  # Disable during initialization
        
        # Set values without triggering dirty tracking
        self._first_name = first_name
        self._last_name = last_name
        self._class_name = class_name
        self._ad_username = ad_username
        self._ad_password = ad_password
        self._ad_display_name = ad_display_name
        self._ad_email = ad_email
        self._ad_description = ad_description
        self._ad_ou_path = ad_ou_path
        self._ad_dn = ad_dn
        
        # Microsoft 365
        self._m365_user_principal_name = m365_user_principal_name
        self._m365_display_name = m365_display_name
        self._m365_mail_nickname = m365_mail_nickname
        self._m365_password = m365_password
        self._m365_usage_location = m365_usage_location

        # Home Directory
        self._home_directory = home_directory
        self._home_drive = home_drive
        
        # Group memberships
        self._group_memberships = group_memberships.copy() if group_memberships else []
        
        # Password policy
        self._password_must_change = password_must_change
        self._password_cannot_change = password_cannot_change
        self._password_never_expires = password_never_expires
        
        # Account status
        self._account_enabled = account_enabled
        
        self.ad_status = ad_status
        self.ad_version = ad_version
        self.ad_last_sync = ad_last_sync

        from models_m365 import M365Status
        self.m365_object_id = m365_object_id
        self.m365_status = m365_status or M365Status.UNKNOWN
        self.m365_last_sync = None
        self._m365_status_before_edit = None

        self.metadata = metadata or {}
        
        # Capture original values
        self._capture_original_values()
        
        # Enable tracking after initialization
        self._tracking_enabled = True
    
    # Properties with automatic dirty tracking (same as before - truncated for brevity)
    @property
    def first_name(self) -> str:
        return self._first_name
    
    @first_name.setter
    def first_name(self, value: str):
        if self._tracking_enabled and self._first_name != value:
            self._first_name = value
            self._mark_dirty('first_name')
        else:
            self._first_name = value
    
    @property
    def last_name(self) -> str:
        return self._last_name
    
    @last_name.setter
    def last_name(self, value: str):
        if self._tracking_enabled and self._last_name != value:
            self._last_name = value
            self._mark_dirty('last_name')
        else:
            self._last_name = value
    
    @property
    def class_name(self) -> str:
        return self._class_name
    
    @class_name.setter
    def class_name(self, value: str):
        if self._tracking_enabled and self._class_name != value:
            self._class_name = value
            self._mark_dirty('class_name')
        else:
            self._class_name = value
    
    @property
    def ad_username(self) -> Optional[str]:
        return self._ad_username
    
    @ad_username.setter
    def ad_username(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_username != value:
            self._ad_username = value
            self._mark_dirty('ad_username')
        else:
            self._ad_username = value
    
    @property
    def ad_password(self) -> Optional[str]:
        return self._ad_password
    
    @ad_password.setter
    def ad_password(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_password != value:
            self._ad_password = value
            self._mark_dirty('ad_password')
        else:
            self._ad_password = value
    
    @property
    def ad_display_name(self) -> Optional[str]:
        return self._ad_display_name
    
    @ad_display_name.setter
    def ad_display_name(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_display_name != value:
            self._ad_display_name = value
            self._mark_dirty('ad_display_name')
        else:
            self._ad_display_name = value
    
    @property
    def ad_email(self) -> Optional[str]:
        return self._ad_email
    
    @ad_email.setter
    def ad_email(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_email != value:
            self._ad_email = value
            self._mark_dirty('ad_email')
        else:
            self._ad_email = value
    
    @property
    def ad_description(self) -> Optional[str]:
        return self._ad_description
    
    @ad_description.setter
    def ad_description(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_description != value:
            self._ad_description = value
            self._mark_dirty('ad_description')
        else:
            self._ad_description = value
    
    @property
    def ad_ou_path(self) -> Optional[str]:
        return self._ad_ou_path
    
    @ad_ou_path.setter
    def ad_ou_path(self, value: Optional[str]):
        if self._tracking_enabled and self._ad_ou_path != value:
            self._ad_ou_path = value
            self._mark_dirty('ad_ou_path')
        else:
            self._ad_ou_path = value
    
    @property
    def ad_dn(self) -> Optional[str]:
        return self._ad_dn
    
    @ad_dn.setter
    def ad_dn(self, value: Optional[str]):
        self._ad_dn = value
    
    @property
    def home_directory(self) -> Optional[str]:
        return self._home_directory
    
    @home_directory.setter
    def home_directory(self, value: Optional[str]):
        if self._tracking_enabled and self._home_directory != value:
            self._home_directory = value
            self._mark_dirty('home_directory')
        else:
            self._home_directory = value
    
    @property
    def home_drive(self) -> Optional[str]:
        return self._home_drive
    
    @home_drive.setter
    def home_drive(self, value: Optional[str]):
        if self._tracking_enabled and self._home_drive != value:
            self._home_drive = value
            self._mark_dirty('home_drive')
        else:
            self._home_drive = value
    
    @property
    def group_memberships(self) -> List[ADGroup]:
        return self._group_memberships
    
    @group_memberships.setter
    def group_memberships(self, value: Optional[List[ADGroup]]):
        """
        Replace the group memberships.

        The assignment is ALWAYS applied.  The previous version compared the
        old and the new DN sets first and threw the assignment away when they
        matched - so replacing a group object with a refreshed one that has the
        same DN (which is exactly what "verify groups in AD" produces) silently
        kept the stale object.  Whether the change counts as *dirty* is decided
        by :meth:`_mark_dirty`, which does the DN comparison anyway.

        ``None`` is accepted and means "no groups", matching the constructor.
        """
        new_list = list(value) if value else []
        self._group_memberships = new_list
        if self._tracking_enabled:
            self._mark_dirty('group_memberships')
    
    def add_to_group(self, group: ADGroup):
        """Add person to a group"""
        if group not in self._group_memberships:
            self._group_memberships.append(group)
            if self._tracking_enabled:
                self._mark_dirty('group_memberships')
    
    def remove_from_group(self, group: ADGroup):
        """Remove person from a group"""
        if group in self._group_memberships:
            self._group_memberships.remove(group)
            if self._tracking_enabled:
                self._mark_dirty('group_memberships')
    
    def is_in_group(self, group: ADGroup) -> bool:
        """Check if person is in a specific group"""
        return group in self._group_memberships
    
    def get_group_dns(self) -> List[str]:
        """Get list of group DNs"""
        return [g.dn for g in self._group_memberships]
    
    @property
    def password_must_change(self) -> bool:
        return self._password_must_change
    
    @password_must_change.setter
    def password_must_change(self, value: bool):
        if self._tracking_enabled and self._password_must_change != value:
            self._password_must_change = value
            self._mark_dirty('password_must_change')
        else:
            self._password_must_change = value
    
    @property
    def password_cannot_change(self) -> bool:
        return self._password_cannot_change
    
    @password_cannot_change.setter
    def password_cannot_change(self, value: bool):
        if self._tracking_enabled and self._password_cannot_change != value:
            self._password_cannot_change = value
            self._mark_dirty('password_cannot_change')
        else:
            self._password_cannot_change = value
    
    @property
    def password_never_expires(self) -> bool:
        return self._password_never_expires
    
    @password_never_expires.setter
    def password_never_expires(self, value: bool):
        if self._tracking_enabled and self._password_never_expires != value:
            self._password_never_expires = value
            self._mark_dirty('password_never_expires')
        else:
            self._password_never_expires = value
    
    @property
    def account_enabled(self) -> bool:
        return self._account_enabled
    
    @account_enabled.setter
    def account_enabled(self, value: bool):
        if self._tracking_enabled and self._account_enabled != value:
            self._account_enabled = value
            self._mark_dirty('account_enabled')
        else:
            self._account_enabled = value
    
    @property
    def m365_user_principal_name(self) -> Optional[str]:
        """The sign-in name in Microsoft 365 (novakjan@skola.cz)"""
        return self._m365_user_principal_name

    @m365_user_principal_name.setter
    def m365_user_principal_name(self, value: Optional[str]):
        if self._tracking_enabled and self._m365_user_principal_name != value:
            self._m365_user_principal_name = value
            self._mark_dirty('m365_user_principal_name')
        else:
            self._m365_user_principal_name = value

    @property
    def m365_display_name(self) -> Optional[str]:
        """The name Microsoft 365 shows for this person"""
        return self._m365_display_name

    @m365_display_name.setter
    def m365_display_name(self, value: Optional[str]):
        if self._tracking_enabled and self._m365_display_name != value:
            self._m365_display_name = value
            self._mark_dirty('m365_display_name')
        else:
            self._m365_display_name = value

    @property
    def m365_mail_nickname(self) -> Optional[str]:
        """The alias Microsoft 365 builds the address from"""
        return self._m365_mail_nickname

    @m365_mail_nickname.setter
    def m365_mail_nickname(self, value: Optional[str]):
        if self._tracking_enabled and self._m365_mail_nickname != value:
            self._m365_mail_nickname = value
            self._mark_dirty('m365_mail_nickname')
        else:
            self._m365_mail_nickname = value

    @property
    def m365_password(self) -> Optional[str]:
        """The Microsoft 365 password this application generated"""
        return self._m365_password

    @m365_password.setter
    def m365_password(self, value: Optional[str]):
        if self._tracking_enabled and self._m365_password != value:
            self._m365_password = value
            self._mark_dirty('m365_password')
        else:
            self._m365_password = value

    @property
    def m365_usage_location(self) -> Optional[str]:
        """Two-letter country code, required before a licence can be assigned"""
        return self._m365_usage_location

    @m365_usage_location.setter
    def m365_usage_location(self, value: Optional[str]):
        if self._tracking_enabled and self._m365_usage_location != value:
            self._m365_usage_location = value
            self._mark_dirty('m365_usage_location')
        else:
            self._m365_usage_location = value

    def _mark_dirty(self, field_name: str):
        """
        Record that a field was edited, and keep the AD status in step.

        Editing a person who agreed with Active Directory makes her differ
        from it; undoing every edit makes her agree again.  The status she had
        before the first edit is remembered, so undoing the change restores
        "Identical" or "Synchronised" - whichever it actually was - instead of
        collapsing both into one.
        """
        original_value = self._original_values.get(field_name)
        current_value = getattr(self, f"_{field_name}", None)

        if field_name == 'group_memberships':
            if original_value is None:
                original_dns = set()
            else:
                original_dns = set(g.dn.lower() for g in original_value)
            current_dns = set(g.dn.lower() for g in current_value) if current_value else set()
            changed = current_dns != original_dns
        else:
            changed = current_value != original_value

        if changed:
            self._dirty_fields.add(field_name)
            self._note_local_change(field_name)
        else:
            # Undoing a change must clear the pending state again.  Without
            # this, a person whose edit was reverted stayed "differs" for ever
            # and was re-sent to AD on every synchronisation.
            self._dirty_fields.discard(field_name)
            if not self._dirty_fields:
                self._note_local_change_undone()

    #: Fields that exist only in Active Directory.
    AD_ONLY_FIELDS = frozenset({
        'ad_username', 'ad_password', 'ad_display_name', 'ad_email',
        'ad_description', 'ad_ou_path', 'home_directory', 'home_drive',
        'group_memberships',
    })

    #: Fields that exist only in Microsoft 365.
    M365_ONLY_FIELDS = frozenset({
        'm365_user_principal_name', 'm365_display_name', 'm365_mail_nickname',
        'm365_password', 'm365_usage_location',
    })

    def _affects_ad(self, field_name: str) -> bool:
        """Whether editing this field makes the person differ from AD."""
        return field_name not in self.M365_ONLY_FIELDS

    def _affects_m365(self, field_name: str) -> bool:
        """Whether editing this field makes the person differ from Microsoft 365."""
        return field_name not in self.AD_ONLY_FIELDS

    def _note_local_change(self, field_name: str) -> None:
        """
        A field was edited, so the person no longer agrees with a directory.

        Which directory depends on the field: a name or an account flag is
        shared and moves both, while ``ad_email`` moves only Active Directory
        and ``m365_display_name`` only Microsoft 365.
        """
        from models_m365 import M365Status

        if self._affects_ad(field_name) and self.ad_status.is_settled:
            self._status_before_edit = self.ad_status
            self.ad_status = ADStatus.DIFFERS_FROM_AD

        if self._affects_m365(field_name) and self.m365_status.is_settled:
            self._m365_status_before_edit = self.m365_status
            self.m365_status = M365Status.DIFFERS_FROM_M365

    def _note_local_change_undone(self) -> None:
        """Every edit was undone: go back to the statuses held before them."""
        from models_m365 import M365Status

        if (self.ad_status is ADStatus.DIFFERS_FROM_AD
                and self._status_before_edit is not None):
            self.ad_status = self._status_before_edit
            self._status_before_edit = None

        if (self.m365_status is M365Status.DIFFERS_FROM_M365
                and self._m365_status_before_edit is not None):
            self.m365_status = self._m365_status_before_edit
            self._m365_status_before_edit = None
    
    def _capture_original_values(self):
        """Capture current state as original values"""
        self._original_values = {
            'first_name': self._first_name,
            'last_name': self._last_name,
            'class_name': self._class_name,
            'ad_username': self._ad_username,
            'ad_password': self._ad_password,
            'ad_display_name': self._ad_display_name,
            'ad_email': self._ad_email,
            'ad_description': self._ad_description,
            'ad_ou_path': self._ad_ou_path,
            'home_directory': self._home_directory,
            'home_drive': self._home_drive,
            'group_memberships': self._group_memberships.copy() if self._group_memberships else [],
            'password_must_change': self._password_must_change,
            'password_cannot_change': self._password_cannot_change,
            'password_never_expires': self._password_never_expires,
            'account_enabled': self._account_enabled,
            'm365_user_principal_name': self._m365_user_principal_name,
            'm365_display_name': self._m365_display_name,
            'm365_mail_nickname': self._m365_mail_nickname,
            'm365_password': self._m365_password,
            'm365_usage_location': self._m365_usage_location,
        }
        self._dirty_fields.clear()
    
    def mark_field_dirty(self, field_name: str):
        """Manually mark a field as modified"""
        if field_name in self._original_values:
            self._mark_dirty(field_name)
    
    def is_dirty(self) -> bool:
        """Check if person has unsaved changes"""
        return len(self._dirty_fields) > 0
    
    def get_dirty_fields(self) -> Set[str]:
        """Get set of modified field names"""
        return self._dirty_fields.copy()
    
    def get_changes(self) -> Dict[str, Any]:
        """Get dictionary of changed values"""
        changes = {}
        for field in self._dirty_fields:
            changes[field] = getattr(self, field)
        return changes
    
    def reset_dirty(self):
        """
        Clear dirty tracking after a successful Active Directory sync.

        The person now holds what was written, so the state she had before the
        edits is no longer relevant.

        Note:
            ``_dirty_fields`` is one set for both directories - it means
            "edited since last written anywhere".  Synchronising to Active
            Directory therefore also clears the flag for Microsoft 365, which
            only matters when a person was edited for one directory and
            synchronised to the other; the Microsoft 365 *status* is left
            alone, so nothing claims a synchronisation that did not happen.
        """
        self._capture_original_values()
        self._status_before_edit = None
        if self._ad_dn:
            self.ad_status = ADStatus.SYNC_SUCCEEDED
            self.ad_last_sync = datetime.now()

    def reset_m365_dirty(self):
        """
        Clear dirty tracking after a successful Microsoft 365 sync.

        The counterpart of :meth:`reset_dirty`; see the note there about the
        shared dirty set.
        """
        from models_m365 import M365Status

        self._capture_original_values()
        self._m365_status_before_edit = None
        if self.m365_object_id:
            self.m365_status = M365Status.SYNC_SUCCEEDED
            self.m365_last_sync = datetime.now()
    
    def get_normalized_name(self) -> tuple:
        """Get normalized name for comparison"""
        first = self.normalize_string(self.first_name)
        last = self.normalize_string(self.last_name)
        return (first, last, self.class_name)
    
    @staticmethod
    def normalize_string(s: str) -> str:
        """Remove diacritics and convert to lowercase"""
        if not s:
            return ""
        nfkd = unicodedata.normalize('NFKD', s)
        result = ''.join([c for c in nfkd if not unicodedata.combining(c)])
        return result.lower()
    
    def matches_search(self, query: str) -> bool:
        """Check if person matches search query"""
        normalized_query = self.normalize_string(query)
        normalized_first = self.normalize_string(self.first_name)
        normalized_last = self.normalize_string(self.last_name)
        
        return (normalized_query in normalized_first or 
                normalized_query in normalized_last or
                normalized_query in f"{normalized_first} {normalized_last}")
    
    def is_same_person(self, other: 'Person') -> bool:
        """Check if two persons are the same based on name and class"""
        return self.get_normalized_name() == other.get_normalized_name()
    
    def __repr__(self):
        return f"Person({self.first_name} {self.last_name}, {self.class_name})"

@dataclass
class Class:
    """
    Represents a class/grade with students.

    Attributes:
        name: The class designation as the source delivered it ("6.A", "IX.B").
        persons: The students in this class.
        enrollment_year: The calendar year in which this class started the
            FIRST grade. Derived from the grade in :attr:`name` and the current
            school year; ``None`` until it has been calculated.
        metadata: Anything a source wants to carry along.
    """

    name: str
    persons: List[Person] = field(default_factory=list)
    enrollment_year: Optional[int] = None
    metadata: Dict = field(default_factory=dict)
    
    def add_person(self, person: Person):
        """Add a person to this class"""
        if person.class_name != self.name:
            logger.warning(f"Person {person} class name doesn't match class {self.name}")
        self.persons.append(person)
        
    def remove_person(self, person: Person):
        """
        Remove a person from this class.

        The person is matched **by identity**, not by value.  ``Person`` is a
        dataclass, so two different students carrying the same data (the very
        duplicates the source analysis reports) compare equal - and
        ``list.remove()`` would then delete the first of them instead of the
        one the user actually selected.

        Args:
            person: The exact person object to remove.
        """
        for index, existing in enumerate(self.persons):
            if existing is person:
                del self.persons[index]
                return
            
    def find_person_by_name(self, first_name: str, last_name: str) -> Optional[Person]:
        """Find person by name"""
        normalized_first = Person.normalize_string(first_name)
        normalized_last = Person.normalize_string(last_name)
        
        for person in self.persons:
            person_first = Person.normalize_string(person.first_name)
            person_last = Person.normalize_string(person.last_name)
            if person_first == normalized_first and person_last == normalized_last:
                return person
        return None
    
    def search_persons(self, query: str) -> List[Person]:
        """Search persons by query"""
        return [p for p in self.persons if p.matches_search(query)]
    
    def __repr__(self):
        return f"Class({self.name}, {len(self.persons)} students)"

@dataclass
class Source:
    """Represents a data source with classes"""
    
    name: str
    source_type: str
    classes: List[Class] = field(default_factory=list)
    readonly: bool = False
    metadata: Dict = field(default_factory=dict)
    source_info: Dict[str, Any] = field(default_factory=dict)
    
    def set_source_info(self, key: str, value: Any):
        """Set source-specific information"""
        self.source_info[key] = value
        logger.debug(f"Source {self.name}: set info {key} = {value}")
    
    def get_source_info(self, key: str, default=None) -> Any:
        """Get source-specific information"""
        return self.source_info.get(key, default)
    
    def add_class(self, cls: Class):
        """Add a class to this source"""
        self.classes.append(cls)
        
    def remove_class(self, cls: Class):
        """
        Remove a class from this source.

        Matched by identity - two classes with the same name and the same
        students would otherwise be indistinguishable.

        Args:
            cls: The exact class object to remove.
        """
        for index, existing in enumerate(self.classes):
            if existing is cls:
                del self.classes[index]
                return
            
    def find_class_by_name(self, name: str) -> Optional[Class]:
        """Find class by name"""
        for cls in self.classes:
            if cls.name == name:
                return cls
        return None
    
    def get_all_persons(self) -> List[Person]:
        """Get all persons from all classes"""
        persons = []
        for cls in self.classes:
            persons.extend(cls.persons)
        return persons
    
    def search_persons(self, query: str) -> List[Person]:
        """Search all persons by query"""
        results = []
        for cls in self.classes:
            results.extend(cls.search_persons(query))
        return results
    
    def deep_copy(self) -> 'Source':
        """Create a deep copy of this source"""
        return deepcopy(self)
    
    def get_statistics(self) -> Dict:
        """Get statistics about this source"""
        total_persons = sum(len(cls.persons) for cls in self.classes)
        return {
            'total_classes': len(self.classes),
            'total_persons': total_persons,
            'classes': [{'name': cls.name, 'count': len(cls.persons)} for cls in self.classes]
        }
    
    def __repr__(self):
        return f"Source({self.name}, {len(self.classes)} classes, {self.source_type})"


class SourceManager(QObject):
    """Manages all data sources with automatic notifications"""
    
    source_added = pyqtSignal(object)
    source_removed = pyqtSignal(str)
    source_modified = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.sources: List[Source] = []
        logger.info("SourceManager initialized")
        
    def add_source(self, source: Source):
        """
        Add a new source and emit signal.

        The enrollment year of every class is calculated here, so it happens
        for data from ANY source - EduPage, an encrypted file or Active
        Directory - without each loader having to remember to do it.
        """
        self.sources.append(source)
        logger.info(f"Added source: {source.name}")
        self._update_enrollment_years(source)
        self.source_added.emit(source)

    @staticmethod
    def _update_enrollment_years(source: Source):
        """
        Fill in the enrollment year of a freshly added source.

        Failures are logged and swallowed: a missing network or an odd class
        name must never stop a source from being loaded.
        """
        try:
            from utils.enrollment_service import update_source_enrollment_years
            update_source_enrollment_years(source, silent=True)
        except Exception:
            logger.exception("Could not calculate the enrollment years of %r",
                             getattr(source, "name", "?"))
        
    def remove_source(self, source: Source):
        """
        Remove a source and emit the corresponding signal.

        Matched by identity so two sources that happen to carry the same name
        and content cannot be confused with each other.

        Args:
            source: The exact source object to remove.
        """
        for index, existing in enumerate(self.sources):
            if existing is source:
                source_name = source.name
                del self.sources[index]
                logger.info(f"Removed source: {source_name}")
                self.source_removed.emit(source_name)
                return
    
    def notify_source_modified(self, source_name: str):
        """Notify that a source was modified"""
        logger.debug(f"Source modified: {source_name}")
        self.source_modified.emit(source_name)
    
    def source_name_exists(self, name: str) -> bool:
        """Check if source name already exists"""
        return any(s.name == name for s in self.sources)
            
    def get_source_by_name(self, name: str) -> Optional[Source]:
        """Get source by name"""
        for source in self.sources:
            if source.name == name:
                return source
        return None
    
    def get_source_names(self) -> List[str]:
        """Get list of all source names"""
        return [s.name for s in self.sources]
    
    #: Merge strategies understood by :meth:`merge_sources`.
    MERGE_STRATEGIES = ('union', 'intersection')

    @staticmethod
    def merge_sources(left: Source, right: Source, strategy: str,
                      result_name: str) -> Source:
        """
        Merge two sources using the specified strategy.

        Args:
            left: Left source - its data wins for persons present on both sides.
            right: Right source.
            strategy: ``'union'`` or ``'intersection'``.
            result_name: Name of the resulting source.

        Returns:
            A new, editable :class:`Source` containing *copies* of the selected
            persons.

        Raises:
            ValueError: If a source is missing or the strategy is unknown.
        """
        source, _stats = SourceManager.merge_sources_detailed(
            left, right, strategy, result_name
        )
        return source

    @staticmethod
    def merge_sources_detailed(left: Source, right: Source, strategy: str,
                               result_name: str) -> tuple:
        """
        Merge two sources and report what happened.

        Compared with the naive implementation this method

        * copies the persons instead of sharing them, so editing the result
          never modifies the two input sources;
        * keeps the class order of the left source and appends classes that
          only exist on the right;
        * counts the duplicates that were collapsed on either side.

        Args:
            left: Left source (wins for persons present in both).
            right: Right source.
            strategy: ``'union'`` or ``'intersection'``.
            result_name: Name of the resulting source.

        Returns:
            ``(merged_source, statistics_dict)``

        Raises:
            ValueError: If a source is missing or the strategy is unknown.
        """
        if left is None or right is None:
            raise ValueError("Both a left and a right source are required")
        if strategy not in SourceManager.MERGE_STRATEGIES:
            raise ValueError(f"Unknown merge strategy: {strategy}")

        logger.info(
            f"Merging sources: {left.name} and {right.name} "
            f"with strategy: {strategy}"
        )

        result = Source(name=result_name, source_type='manual', readonly=False)

        def index(source: Source) -> tuple:
            """Return ``(unique_persons_by_key, duplicate_count)``."""
            mapping = {}
            duplicates = 0
            for person in source.get_all_persons():
                key = person.get_normalized_name()
                if key in mapping:
                    duplicates += 1
                    continue
                mapping[key] = person
            return mapping, duplicates

        left_persons, left_duplicates = index(left)
        right_persons, right_duplicates = index(right)

        if strategy == 'union':
            merged_persons = dict(left_persons)
            for key, person in right_persons.items():
                merged_persons.setdefault(key, person)
        else:  # intersection
            merged_persons = {
                key: person for key, person in left_persons.items()
                if key in right_persons
            }

        # Preserve a predictable class order: left classes first, then the
        # classes that only exist on the right, then anything unexpected.
        class_order = [cls.name for cls in left.classes]
        class_order += [cls.name for cls in right.classes if cls.name not in class_order]

        class_map: Dict[str, Class] = {}
        for person in merged_persons.values():
            class_name = person.class_name
            cls = class_map.get(class_name)
            if cls is None:
                cls = Class(name=class_name)
                class_map[class_name] = cls
            # Copy the person so the result is fully independent of the inputs
            cls.persons.append(deepcopy(person))

        for class_name in class_order:
            if class_name in class_map:
                result.add_class(class_map.pop(class_name))
        for cls in class_map.values():
            result.add_class(cls)

        stats = {
            'strategy': strategy,
            'left_persons': len(left.get_all_persons()),
            'right_persons': len(right.get_all_persons()),
            'left_unique': len(left_persons),
            'right_unique': len(right_persons),
            'left_duplicates_collapsed': left_duplicates,
            'right_duplicates_collapsed': right_duplicates,
            'only_left': len(set(left_persons) - set(right_persons)),
            'only_right': len(set(right_persons) - set(left_persons)),
            'in_both': len(set(left_persons) & set(right_persons)),
            'result_persons': len(result.get_all_persons()),
            'result_classes': len(result.classes),
        }

        logger.info(f"Merge complete: {stats['result_persons']} persons "
                    f"in {stats['result_classes']} classes")
        return result, stats

