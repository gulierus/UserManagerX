"""
Active Directory Services: Discovery, Planning, Sync, and Conflict Resolution
FIXED VERSION - Removed circular imports, fixed password encoding, improved error handling
"""

import logging
from typing import List, Dict, Set, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from models import Person, Source, ADStatus
from utils.ad_entry_mapping import PERSON_ATTRIBUTES
from services.ad_comparison import (
    AD_VALUES_METADATA_KEY, compare_person_with_ad, store_comparison,
)

#: Attributes every discovery and refresh asks the directory for.
#:
#: ``'*'`` alone is not enough to rely on: it returns the attributes the entry
#: carries, and a server may leave constructed attributes such as ``memberOf``
#: out of it.  Naming them explicitly is what makes "Discover in AD" fill in
#: the display name, the email, the home directory and the group list
#: (Version 26, point 19 a).
AD_READ_ATTRIBUTES = list(dict.fromkeys(
    PERSON_ATTRIBUTES + ['*', 'modifyTimestamp', 'createTimestamp']
))
from services.ad_group_service import ADGroupService
from services.ldap_compat import MODIFY_REPLACE


logger = logging.getLogger(__name__)


#: Characters that carry a special meaning inside an RDN value (RFC 4514).
_DN_SPECIAL_CHARACTERS = '\\,+"<>;='


def escape_dn_value(value: str) -> str:
    """
    Escape a value so it stays a *single* component of a distinguished name.

    ``6,A`` -> ``6\\,A``

    DN components used to be built by plain string formatting, so a class name
    (or a display name) containing a comma silently split the DN into two extra
    components: the account was created somewhere else in the tree, or the
    server rejected the request as invalid DN syntax.

    Args:
        value: The raw attribute value.

    Returns:
        The value with every RFC 4514 special character escaped.
    """
    if not value:
        return ''

    escaped = ''.join(
        '\\' + character if character in _DN_SPECIAL_CHARACTERS else character
        for character in str(value)
    )

    # A trailing space is significant and is escaped first: every backslash of
    # the original has already been doubled above, so a space at the end of
    # *escaped* can only come from a trailing space of the original.  Testing
    # for an already-escaped space here instead ("\\ ") also matched a value
    # ending in backslash + space and left that space unescaped.
    if escaped.endswith(' '):
        escaped = escaped[:-1] + '\\ '

    # A leading '#' or space is significant as well.  This has to happen after
    # the trailing space, otherwise a value consisting of a single space is
    # escaped twice.
    if escaped[:1] in ('#', ' '):
        escaped = '\\' + escaped

    return escaped


def unescape_dn_value(value: str) -> str:
    """Inverse of :func:`escape_dn_value` - ``6\\,A`` becomes ``6,A``."""
    result = []
    index = 0
    while index < len(value):
        if value[index] == '\\' and index + 1 < len(value):
            result.append(value[index + 1])
            index += 2
        else:
            result.append(value[index])
            index += 1
    return ''.join(result)


def first_rdn_value(dn: str) -> str:
    """
    The plain value of the first component of *dn*.

    ``OU=Trida-6\\,A,DC=skola`` -> ``Trida-6,A``.  Splitting on every comma and
    on every '=' mangled exactly those names that had to be escaped.

    The components are walked character by character rather than split with a
    "comma not preceded by a backslash" pattern: in ``OU=Trida-6\\\\,DC=skola``
    the comma *is* preceded by a backslash, but that backslash is itself
    escaped, so the comma really is the separator.
    """
    characters = []
    escaped = False
    for character in dn or '':
        if escaped:
            characters.append(character)
            escaped = False
        elif character == '\\':
            characters.append(character)
            escaped = True
        elif character == ',':
            break
        else:
            characters.append(character)

    first_component = ''.join(characters)
    _, separator, value = first_component.partition('=')
    return unescape_dn_value(value if separator else first_component)


class ConflictStrategy(Enum):
    """Strategy for resolving conflicts"""
    LOCAL_WINS = "local_wins"
    REMOTE_WINS = "remote_wins"
    AUTO_MERGE = "auto_merge"
    USER_PROMPT = "user_prompt"


@dataclass
class DiscoveryResult:
    """Result from AD discovery operation"""
    person: Person
    found_in_ad: bool
    ad_dn: Optional[str] = None
    ad_attributes: Dict[str, Any] = field(default_factory=dict)
    ad_version: Optional[str] = None
    ambiguous: bool = False  # Multiple matches found
    multiple_matches: List[Dict] = field(default_factory=list)


@dataclass
class ConflictInfo:
    """Information about a detected conflict"""
    person: Person
    local_changes: Dict[str, Any]
    remote_changes: Dict[str, Any]
    conflicting_fields: Set[str]
    non_conflicting_local: Dict[str, Any]
    current_ad_state: Dict[str, Any]
    ad_modified_time: Optional[str] = None


@dataclass
class ConflictResolution:
    """Result of conflict resolution"""
    merged_changes: Dict[str, Any]
    skipped_fields: Set[str] = field(default_factory=set)


@dataclass
class CreateOperation:
    """Operation to create new user in AD"""
    person: Person
    target_ou: str
    attributes: Dict[str, Any]


@dataclass
class UpdateOperation:
    """Operation to update existing user in AD"""
    person: Person
    changes: Dict[str, Any]
    has_conflict: bool = False
    conflict_info: Optional[ConflictInfo] = None


@dataclass
class SyncPlan:
    """Plan for synchronizing with AD"""
    create_operations: List[CreateOperation] = field(default_factory=list)
    update_operations: List[UpdateOperation] = field(default_factory=list)
    incomplete_persons: List[Person] = field(default_factory=list)
    
    @property
    def statistics(self) -> Dict[str, int]:
        """Get statistics about the plan"""
        conflicts = sum(1 for op in self.update_operations if op.has_conflict)
        return {
            'total_operations': len(self.create_operations) + len(self.update_operations),
            'creates': len(self.create_operations),
            'updates': len(self.update_operations),
            'conflicts': conflicts,
            'incomplete': len(self.incomplete_persons),
            'requires_user_action': conflicts + len(self.incomplete_persons)
        }


@dataclass
class OperationResult:
    """Result of a single sync operation"""
    person: Person
    status: str  # 'SUCCESS', 'FAILED', 'SKIPPED'
    message: str
    error: Optional[Exception] = None


@dataclass
class SyncResult:
    """Result of complete synchronization"""
    total_processed: int
    successful: List[OperationResult] = field(default_factory=list)
    failed: List[OperationResult] = field(default_factory=list)
    skipped: List[OperationResult] = field(default_factory=list)
    duration: float = 0.0
    
    @property
    def statistics(self) -> Dict[str, int]:
        """Get statistics"""
        return {
            'total': self.total_processed,
            'successful': len(self.successful),
            'failed': len(self.failed),
            'skipped': len(self.skipped)
        }


class ADDiscoveryService:
    """Service for discovering persons in Active Directory"""
    
    def __init__(self, ad_client, scope_config=None):
        """
        Args:
            ad_client: The connected :class:`~services.ad_client.ADClient`.
            scope_config: Optional :class:`~utils.ad_search_scope.SearchScopeConfig`
                restricting where persons are looked for. ``None`` searches the
                whole subtree below the Base DN, which is the historical
                behaviour.
        """
        self.ad_client = ad_client
        self.scope_config = scope_config

    def _search_bases(self, person: Person, base_dn: str) -> List[str]:
        """Return the DNs *person* should be searched under."""
        if self.scope_config is None:
            return [base_dn]
        from utils.ad_search_scope import build_search_bases
        return build_search_bases(self.scope_config, base_dn, person)

    def _search(self, person: Person, base_dn: str, search_filter: str):
        """
        Run one filter across every configured search base.

        Args:
            person: The person being discovered (the bases can depend on them).
            base_dn: The configured Base DN.
            search_filter: The LDAP filter.

        Returns:
            The combined matches, de-duplicated by DN.
        """
        seen = set()
        matches = []
        for search_base in self._search_bases(person, base_dn):
            try:
                found = self.ad_client.search_users(
                    search_base, search_filter, attributes=AD_READ_ATTRIBUTES)
            except Exception as exc:
                # A named OU that does not exist is normal when one template
                # covers classes that are not all present in the directory.
                logger.debug("Search in %r failed: %s", search_base, exc)
                continue
            for entry in found or []:
                if entry.dn not in seen:
                    seen.add(entry.dn)
                    matches.append(entry)
        return matches

    def discover_persons(self, persons: List[Person], base_dn: str) -> List[DiscoveryResult]:
        """
        Discover which persons exist in AD
        
        Args:
            persons: List of persons to check
            base_dn: Base DN to search in
            
        Returns:
            List of discovery results
        """
        results = []
        
        for person in persons:
            try:
                result = self._discover_single_person(person, base_dn)
                results.append(result)
                
                # Update person status based on discovery
                if result.found_in_ad and not result.ambiguous:
                    person.ad_dn = result.ad_dn
                    person.ad_version = result.ad_version
                    # Store current AD values for conflict detection and for
                    # the field-by-field comparison below.
                    person.metadata[AD_VALUES_METADATA_KEY] = result.ad_attributes

                    # Finding the account is only half the answer: the status
                    # has to say whether the two sides also agree.
                    differences = store_comparison(
                        person, compare_person_with_ad(person, result.ad_attributes))
                    person.ad_status = (ADStatus.DIFFERS_FROM_AD if differences
                                        else ADStatus.MATCHES_AD)
                    if differences:
                        logger.debug(
                            "%s %s differs from AD in: %s",
                            person.first_name, person.last_name,
                            ", ".join(d.label for d in differences))
                elif result.ambiguous:
                    person.ad_status = ADStatus.MULTIPLE_AD_MATCHES
                else:
                    person.ad_status = ADStatus.NOT_FOUND_IN_AD
                    
            except Exception as e:
                logger.exception(f"Error discovering person {person.first_name} {person.last_name}")
                results.append(DiscoveryResult(person=person, found_in_ad=False))
                
        return results
    
    def _discover_single_person(self, person: Person, base_dn: str) -> DiscoveryResult:
        """Discover a single person in AD"""
        
        # Strategy 1: Search by DN if available
        if person.ad_dn:
            try:
                entry = self.ad_client.get_user(person.ad_dn,
                                                attributes=AD_READ_ATTRIBUTES)
                if entry:
                    return DiscoveryResult(
                        person=person,
                        found_in_ad=True,
                        ad_dn=person.ad_dn,
                        ad_attributes=entry.attributes,
                        ad_version=entry.get('modifyTimestamp')
                    )
            except Exception as e:
                logger.warning(f"DN {person.ad_dn} not found, trying other methods: {e}")
        
        # Strategy 2: Search by username
        if person.ad_username:
            matches = self._search(
                person, base_dn, f"(sAMAccountName={person.ad_username})"
            )
            if len(matches) == 1:
                entry = matches[0]
                return DiscoveryResult(
                    person=person,
                    found_in_ad=True,
                    ad_dn=entry.dn,
                    ad_attributes=entry.attributes,
                    ad_version=entry.get('modifyTimestamp')
                )
            elif len(matches) > 1:
                return DiscoveryResult(
                    person=person,
                    found_in_ad=True,
                    ambiguous=True,
                    multiple_matches=[{'dn': m.dn, 'attrs': m.attributes} for m in matches]
                )
        
        # Strategy 3: Search by email
        if person.ad_email:
            matches = self._search(
                person, base_dn, f"(mail={person.ad_email})"
            )
            if len(matches) == 1:
                entry = matches[0]
                return DiscoveryResult(
                    person=person,
                    found_in_ad=True,
                    ad_dn=entry.dn,
                    ad_attributes=entry.attributes,
                    ad_version=entry.get('modifyTimestamp')
                )
        
        # Strategy 4: Search by name
        search_filter = f"(&(givenName={person.first_name})(sn={person.last_name}))"
        matches = self._search(person, base_dn, search_filter)
        
        if len(matches) == 1:
            entry = matches[0]
            return DiscoveryResult(
                person=person,
                found_in_ad=True,
                ad_dn=entry.dn,
                ad_attributes=entry.attributes,
                ad_version=entry.get('modifyTimestamp')
            )
        elif len(matches) > 1:
            return DiscoveryResult(
                person=person,
                found_in_ad=True,
                ambiguous=True,
                multiple_matches=[{'dn': m.dn, 'attrs': m.attributes} for m in matches]
            )
        
        # Not found
        return DiscoveryResult(person=person, found_in_ad=False)


class ConflictDetector:
    """Service for detecting conflicts between local and AD changes"""
    
    def __init__(self, ad_client):
        self.ad_client = ad_client
    
    def check_conflict(self, person: Person) -> Optional[ConflictInfo]:
        """
        Check if there's a conflict between local changes and AD state
        
        Returns:
            ConflictInfo if conflict detected, None otherwise
        """
        if not person.is_dirty() or not person.ad_dn:
            return None
        
        try:
            # Get fresh data from AD
            fresh_entry = self.ad_client.get_user(person.ad_dn)
            if not fresh_entry:
                logger.warning(f"Person {person.ad_dn} not found in AD")
                return None
            
            fresh_timestamp = fresh_entry.get('modifyTimestamp')
            
            # Check if AD was modified since last sync
            if person.ad_version and fresh_timestamp != person.ad_version:
                # AD was modified, check for conflicts
                local_changes = person.get_changes()
                original_ad_values = person.metadata.get('ad_current_values', {})
                current_ad_values = fresh_entry.attributes
                
                # Detect which fields changed in AD
                remote_changes = {}
                for key in local_changes.keys():
                    ad_key = self._map_to_ad_attribute(key)
                    if ad_key in current_ad_values:
                        original = original_ad_values.get(ad_key)
                        current = current_ad_values.get(ad_key)
                        if original != current:
                            remote_changes[key] = current
                
                # Find conflicting fields (changed in both places)
                conflicting_fields = set(local_changes.keys()) & set(remote_changes.keys())
                
                if conflicting_fields:
                    # We have actual conflicts
                    non_conflicting = {
                        k: v for k, v in local_changes.items()
                        if k not in conflicting_fields
                    }
                    
                    return ConflictInfo(
                        person=person,
                        local_changes=local_changes,
                        remote_changes=remote_changes,
                        conflicting_fields=conflicting_fields,
                        non_conflicting_local=non_conflicting,
                        current_ad_state=current_ad_values,
                        ad_modified_time=fresh_timestamp
                    )
            
            return None
            
        except Exception as e:
            logger.exception(f"Error checking conflict for {person.ad_dn}")
            return None
    
    def _map_to_ad_attribute(self, field_name: str) -> str:
        """Map person field name to AD attribute name"""
        mapping = {
            'first_name': 'givenName',
            'last_name': 'sn',
            'ad_username': 'sAMAccountName',
            'ad_email': 'mail',
            'ad_display_name': 'displayName',
            'ad_description': 'description'
        }
        return mapping.get(field_name, field_name)


class SyncPlanner:
    """Service for creating synchronization plans"""
    
    def __init__(self, ad_client, conflict_detector: ConflictDetector):
        self.ad_client = ad_client
        self.conflict_detector = conflict_detector
    
    def create_sync_plan(self, source: Source, base_dn: str) -> SyncPlan:
        """
        Create a synchronization plan for all dirty persons in source
        
        Args:
            source: Source with persons to sync
            base_dn: Base DN in AD
            
        Returns:
            SyncPlan with operations to perform
        """
        plan = SyncPlan()
        
        for cls in source.classes:
            for person in cls.persons:
                if not person.is_dirty() and person.ad_status.is_settled:
                    # Nothing local is waiting, and the person either matches
                    # Active Directory or was synchronised successfully.
                    continue

                if person.ad_status == ADStatus.MULTIPLE_AD_MATCHES:
                    # Several AD accounts match this person, so we do not know
                    # which one is hers.  Creating a user would add yet another
                    # duplicate - the ambiguity has to be resolved by the user
                    # first, so she is left out of the plan entirely.
                    logger.warning(
                        "Skipping %s %s (%s): several AD accounts match, "
                        "resolve the ambiguity before synchronising",
                        person.first_name, person.last_name, person.class_name
                    )
                    continue
                
                if (person.ad_status in (ADStatus.NOT_FOUND_IN_AD, ADStatus.UNKNOWN)
                        or not person.ad_dn):
                    # New user to create
                    if self._has_required_ad_data(person):
                        # The class name is data, not DN syntax: a comma in it
                        # used to split the OU into two components.
                        target_ou = (f"OU=Trida-{escape_dn_value(person.class_name)}"
                                     f",{base_dn}")
                        attributes = self._prepare_creation_attributes(person, base_dn)
                        plan.create_operations.append(
                            CreateOperation(person, target_ou, attributes)
                        )
                    else:
                        plan.incomplete_persons.append(person)
                
                else:
                    # Everything that is left has a DN and is known to AD:
                    # EXISTS_IN_AD, UPDATE_PENDING (a SYNCED person that was
                    # edited) or a still dirty SYNCED person.  Only
                    # EXISTS_IN_AD used to be handled here, so every edit of an
                    # already synchronised person was silently dropped from the
                    # plan and never reached Active Directory.
                    changes = person.get_changes()
                    if not changes:
                        continue
                    
                    # Check for conflicts
                    conflict_info = self.conflict_detector.check_conflict(person)
                    
                    plan.update_operations.append(
                        UpdateOperation(
                            person=person,
                            changes=changes,
                            has_conflict=(conflict_info is not None),
                            conflict_info=conflict_info
                        )
                    )
        
        return plan
    
    def _has_required_ad_data(self, person: Person) -> bool:
        """Check if person has minimum required data for AD creation"""
        return all([
            person.first_name,
            person.last_name,
            person.ad_username,
            person.ad_password
        ])
    
    @staticmethod
    def _domain_from_base_dn(base_dn: str) -> str:
        """
        Derive the DNS domain from a base DN.

        ``OU=Students,DC=skola,DC=cz`` -> ``skola.cz``

        Args:
            base_dn: The configured base DN.

        Returns:
            The dotted domain, or an empty string when it cannot be derived.
        """
        parts = []
        for component in (base_dn or "").split(','):
            component = component.strip()
            if component.upper().startswith('DC='):
                parts.append(component[3:].strip())
        return '.'.join(p for p in parts if p)

    def _prepare_creation_attributes(self, person: Person,
                                     base_dn: str = "") -> Dict[str, Any]:
        """Prepare LDAP attributes for creating a new user"""
        # The domain was hardcoded to "domain.local", so EVERY account created
        # by the application got a userPrincipalName from a domain that does not
        # exist - users could not sign in with it.
        domain = self._domain_from_base_dn(base_dn)

        attrs = {
            'givenName': person.first_name,
            'sn': person.last_name,
            'sAMAccountName': person.ad_username,
            'displayName': person.ad_display_name or f"{person.first_name} {person.last_name}",
        }

        if domain:
            attrs['userPrincipalName'] = f"{person.ad_username}@{domain}"
        else:
            logger.warning(
                "No DC= component in the base DN - userPrincipalName is not set "
                "for %s", person.ad_username
            )
        
        if person.ad_email:
            attrs['mail'] = person.ad_email
        if person.ad_description:
            attrs['description'] = person.ad_description
        
        return attrs


class ConflictResolver:
    """Service for resolving conflicts"""
    
    def resolve(
        self,
        conflict_info: ConflictInfo,
        strategy: ConflictStrategy,
        user_choices: Optional[Dict[str, str]] = None
    ) -> Optional[ConflictResolution]:
        """
        Resolve a conflict using specified strategy
        
        Args:
            conflict_info: Information about the conflict
            strategy: Resolution strategy
            user_choices: User's choices for USER_PROMPT strategy
            
        Returns:
            ConflictResolution with merged changes, or None if skipped
        """
        if strategy == ConflictStrategy.LOCAL_WINS:
            return ConflictResolution(
                merged_changes=conflict_info.local_changes.copy()
            )
        
        elif strategy == ConflictStrategy.REMOTE_WINS:
            # Discard local changes, use AD values
            return ConflictResolution(
                merged_changes={},
                skipped_fields=set(conflict_info.local_changes.keys())
            )
        
        elif strategy == ConflictStrategy.AUTO_MERGE:
            # Apply non-conflicting changes automatically
            if conflict_info.conflicting_fields:
                # Has conflicts, need user prompt
                return None
            else:
                return ConflictResolution(
                    merged_changes=conflict_info.local_changes.copy()
                )
        
        elif strategy == ConflictStrategy.USER_PROMPT:
            if not user_choices:
                return None
            
            merged = conflict_info.non_conflicting_local.copy()
            skipped = set()
            
            for field in conflict_info.conflicting_fields:
                choice = user_choices.get(field)
                if choice == 'local':
                    merged[field] = conflict_info.local_changes[field]
                elif choice == 'remote':
                    # Use AD value (which means don't update this field)
                    skipped.add(field)
                elif choice and choice.startswith('custom:'):
                    merged[field] = choice[7:]  # Strip 'custom:' prefix
                else:
                    skipped.add(field)
            
            return ConflictResolution(
                merged_changes=merged,
                skipped_fields=skipped
            )
        
        return None


class PersonsSyncService:
    """Service for executing synchronization with AD"""
    
    def __init__(self, ad_client, conflict_resolver: ConflictResolver):
        self.ad_client = ad_client
        self.conflict_resolver = conflict_resolver
    
    def execute_sync(
        self,
        sync_plan: SyncPlan,
        conflict_strategy: ConflictStrategy = ConflictStrategy.USER_PROMPT,
        user_conflict_choices: Optional[Dict[str, Dict[str, str]]] = None
    ) -> SyncResult:
        """
        Execute synchronization plan
        
        Args:
            sync_plan: Plan to execute
            conflict_strategy: How to handle conflicts
            user_conflict_choices: User choices for conflicts (person_id -> field -> choice)
            
        Returns:
            SyncResult with statistics
        """
        start_time = datetime.now()
        result = SyncResult(
            total_processed=len(sync_plan.create_operations) + len(sync_plan.update_operations)
        )
        
        # Phase 1: Create new users
        for create_op in sync_plan.create_operations:
            op_result = self._execute_create(create_op)
            self._categorize_result(result, op_result)
        
        # Phase 2: Update existing users
        for update_op in sync_plan.update_operations:
            op_result = self._execute_update(
                update_op,
                conflict_strategy,
                user_conflict_choices
            )
            self._categorize_result(result, op_result)
        
        result.duration = (datetime.now() - start_time).total_seconds()
        return result
    
    def _execute_create(self, create_op: CreateOperation) -> OperationResult:
        """
        Create a user in AD and apply everything that belongs to it.

        Each step (account, password, groups, home directory) is INDEPENDENT:
        a step that fails is recorded and the remaining steps still run. The
        previous version returned as soon as the password step failed, which is
        why users were created but never ended up in their groups - the group
        step simply never executed.

        Args:
            create_op: The planned creation.

        Returns:
            An :class:`OperationResult` whose message names every failed step.
        """
        person = create_op.person
        failures: List[str] = []

        try:
            # --- the organisational unit -----------------------------------
            if not self.ad_client.ou_exists(create_op.target_ou):
                # Escaped RDNs must not be split on every comma/'=' - the OU
                # name of "OU=Trida-6\,A,..." is "Trida-6,A", not "Trida-6\".
                ou_name = first_rdn_value(create_op.target_ou)
                logger.info(f"Creating OU: {create_op.target_ou}")
                if not self.ad_client.create_ou(create_op.target_ou, ou_name):
                    # Without the OU the account cannot be created at all, so
                    # this one genuinely is terminal.
                    return OperationResult(
                        person=person,
                        status='FAILED',
                        message=f"Failed to create OU: {create_op.target_ou}"
                    )

            # --- the account ------------------------------------------------
            # Build DN - the CN is user data and is escaped for the same
            # reason as the class name in the target OU.
            cn = person.ad_display_name or person.ad_username
            dn = f"CN={escape_dn_value(cn)},{create_op.target_ou}"

            attributes = create_op.attributes.copy()
            attributes['userAccountControl'] = str(self._build_uac_flags(person))

            if not self.ad_client.create_user(dn, attributes):
                return OperationResult(
                    person=person,
                    status='FAILED',
                    message="Failed to create user in AD"
                )

            # The account exists from here on, so remember where it is *before*
            # anything else can fail.  The DN used to be recorded only at the
            # very end, so a failing password step left the person without a
            # DN and the next synchronisation created a second account.
            person.ad_dn = dn
            # The account exists, but nothing else has been applied yet.
            person.ad_status = ADStatus.DIFFERS_FROM_AD

            # --- everything that can fail on its own ------------------------
            failures.extend(self._apply_post_account_steps(person, dn))

            # --- final state -------------------------------------------------
            self._refresh_ad_state(person, dn)

            if failures:
                person.ad_status = ADStatus.SYNC_INCOMPLETE
                return OperationResult(
                    person=person,
                    status='SUCCESS',
                    message=(f"Created user {person.ad_username}, but: "
                             + "; ".join(failures))
                )

            person.ad_status = ADStatus.SYNC_SUCCEEDED
            person.reset_dirty()
            return OperationResult(
                person=person,
                status='SUCCESS',
                message=f"Created user {person.ad_username}"
            )

        except Exception as e:
            logger.exception(f"Error creating user {person.ad_username}")
            return OperationResult(
                person=person,
                status='FAILED',
                message=str(e),
                error=e
            )

    def _apply_post_account_steps(self, person: Person, dn: str) -> List[str]:
        """
        Run every per-user step that is independent of the others.

        Password, account flags, group memberships and the home directory are
        each attempted regardless of whether an earlier one failed, so a single
        problem (most often the password, which Active Directory refuses over
        an unencrypted connection) can no longer silently cancel the rest of
        the synchronisation.

        Args:
            person: The person being synchronised.
            dn: The distinguished name of their AD account.

        Returns:
            A list of human readable failure descriptions; empty when all
            steps succeeded.
        """
        failures: List[str] = []

        # --- password ------------------------------------------------------
        if person.ad_password:
            try:
                self._set_password(dn, person.ad_password, person)
            except Exception as exc:
                # NEVER log the password itself - the log file is readable in
                # the application and is often shared for support.
                logger.error("Password not set for %s: %s", person.ad_username, exc)
                failures.append(f"password not set ({exc})")

        # --- group memberships ---------------------------------------------
        if person.group_memberships:
            try:
                if not ADGroupService(self.ad_client).sync_user_groups(person):
                    failures.append("group memberships were not fully applied")
            except Exception as exc:
                logger.error("Group synchronisation failed for %s: %s",
                             person.ad_username, exc)
                failures.append(f"groups not applied ({exc})")

        # --- home directory --------------------------------------------------
        if person.home_directory:
            try:
                if not self.ad_client.set_home_directory(
                    dn, person.home_directory, person.home_drive
                ):
                    failures.append("home directory was not set")
            except Exception as exc:
                logger.error("Home directory not set for %s: %s",
                             person.ad_username, exc)
                failures.append(f"home directory not set ({exc})")

        return failures

    def _refresh_ad_state(self, person: Person, dn: str) -> None:
        """
        Re-read the account after writing to it.

        This gives the conflict detector a fresh baseline and, just as
        importantly, refreshes the field-by-field comparison - otherwise the
        table would keep outlining the fields the synchronisation has just
        brought into agreement.
        """
        try:
            entry = self.ad_client.get_user(dn, attributes=AD_READ_ATTRIBUTES)
            if entry:
                person.ad_version = entry.get('modifyTimestamp')
                person.metadata[AD_VALUES_METADATA_KEY] = entry.attributes
                store_comparison(
                    person, compare_person_with_ad(person, entry.attributes))
        except Exception as exc:
            logger.warning("Could not re-read %s after the update: %s", dn, exc)

    def _execute_update(
        self,
        update_op: UpdateOperation,
        conflict_strategy: ConflictStrategy,
        user_choices: Optional[Dict[str, Dict[str, str]]]
    ) -> OperationResult:
        """
        Update an existing AD user.

        Like :meth:`_execute_create`, every step is independent: a refused
        password no longer cancels the group and home-directory steps.

        Args:
            update_op: The planned update.
            conflict_strategy: How to resolve a conflict.
            user_choices: Per-person field choices for USER_PROMPT.

        Returns:
            An :class:`OperationResult` naming every failed step.
        """
        person = update_op.person
        failures: List[str] = []

        try:
            # --- conflict resolution ---------------------------------------
            if update_op.has_conflict and update_op.conflict_info:
                person_choices = user_choices.get(str(id(person)), {}) if user_choices else {}
                resolution = self.conflict_resolver.resolve(
                    update_op.conflict_info,
                    conflict_strategy,
                    person_choices
                )

                if resolution is None:
                    return OperationResult(
                        person=person,
                        status='SKIPPED',
                        message="Conflict not resolved"
                    )

                changes = resolution.merged_changes
            else:
                changes = update_op.changes

            if not changes:
                return OperationResult(
                    person=person,
                    status='SKIPPED',
                    message="No changes to apply"
                )

            # --- split the changes by the mechanism that applies them -------
            ldap_ops = []
            password_changed = False
            policy_changed = False

            for field, value in changes.items():
                if field == 'ad_password':
                    password_changed = True
                elif field in ('password_must_change', 'password_cannot_change',
                               'password_never_expires', 'account_enabled'):
                    policy_changed = True
                else:
                    ldap_attr = self._map_field_to_ldap(field)
                    if ldap_attr:
                        ldap_value = [str(value)] if value is not None else []
                        ldap_ops.append((MODIFY_REPLACE, ldap_attr, ldap_value))

            # --- plain attributes -------------------------------------------
            if ldap_ops:
                try:
                    if not self.ad_client.modify_user(person.ad_dn, ldap_ops):
                        failures.append("attributes were not updated")
                except Exception as exc:
                    logger.error("Attribute update failed for %s: %s",
                                 person.ad_username, exc)
                    failures.append(f"attributes not updated ({exc})")

            # --- password ----------------------------------------------------
            if password_changed and person.ad_password:
                try:
                    self._set_password(person.ad_dn, person.ad_password, person)
                except Exception as exc:
                    logger.error("Password not updated for %s: %s",
                                 person.ad_username, exc)
                    failures.append(f"password not set ({exc})")

            # --- account flags ------------------------------------------------
            if policy_changed:
                try:
                    self._update_account_flags(person)
                except Exception as exc:
                    logger.error("Account policy not updated for %s: %s",
                                 person.ad_username, exc)
                    failures.append(f"account policy not updated ({exc})")

            # --- groups -------------------------------------------------------
            if person.group_memberships:
                try:
                    if not ADGroupService(self.ad_client).sync_user_groups(person):
                        failures.append("group memberships were not fully applied")
                except Exception as exc:
                    logger.error("Group synchronisation failed for %s: %s",
                                 person.ad_username, exc)
                    failures.append(f"groups not applied ({exc})")

            # --- final state -----------------------------------------------------
            self._refresh_ad_state(person, person.ad_dn)

            if failures:
                person.ad_status = ADStatus.SYNC_INCOMPLETE
                return OperationResult(
                    person=person,
                    status='FAILED',
                    message=(f"Updated user {person.ad_username} with problems: "
                             + "; ".join(failures))
                )

            person.ad_status = ADStatus.SYNC_SUCCEEDED
            person.reset_dirty()
            return OperationResult(
                person=person,
                status='SUCCESS',
                message=f"Updated user {person.ad_username}"
            )

        except Exception as e:
            logger.exception(f"Error updating user {person.ad_username}")
            return OperationResult(
                person=person,
                status='FAILED',
                message=str(e),
                error=e
            )

    def _map_field_to_ldap(self, field_name: str) -> Optional[str]:
        """Map person field name to LDAP attribute name"""
        # The home directory and drive are editable in the property editor and
        # are tracked as dirty fields, but they were missing from this mapping:
        # the change was mapped to None, quietly dropped, and the sync still
        # reported success.  ad_ou_path stays unmapped on purpose - moving an
        # object needs a modify-DN operation, not an attribute write.
        mapping = {
            'first_name': 'givenName',
            'last_name': 'sn',
            'ad_username': 'sAMAccountName',
            'ad_email': 'mail',
            'ad_display_name': 'displayName',
            'ad_description': 'description',
            'home_directory': 'homeDirectory',
            'home_drive': 'homeDrive'
        }
        return mapping.get(field_name)

    def _update_account_flags(self, person: Person):
        """Update account control flags"""
        try:
            flags = self._build_uac_flags(person)
            
            self.ad_client.modify_user(
                person.ad_dn,
                [(MODIFY_REPLACE, 'userAccountControl', [str(flags)])]
            )
            
            # Handle must change password
            if person.password_must_change:
                self.ad_client.modify_user(
                    person.ad_dn,
                    [(MODIFY_REPLACE, 'pwdLastSet', ['0'])]
                )
            
            logger.info(f"Updated account flags for {person.ad_dn}")
            
        except Exception as e:
            logger.error(f"Failed to update account flags for {person.ad_dn}: {e}")
            raise    

    def _build_uac_flags(self, person: Person) -> int:
        """
        Build User Account Control flags for AD.
        
        UAC flags:
        - ACCOUNTDISABLE = 0x0002 (2)
        - NORMAL_ACCOUNT = 0x0200 (512)
        - DONT_EXPIRE_PASSWORD = 0x10000 (65536)
        """
        flags = 0x0200  # NORMAL_ACCOUNT
        
        # Account disabled?
        if not person.account_enabled:
            flags |= 0x0002  # ACCOUNTDISABLE
        
        # Password never expires?
        if person.password_never_expires:
            flags |= 0x10000  # DONT_EXPIRE_PASSWORD
        
        return flags
    
    def _set_password(self, dn: str, password: str, person: Person):
        """
        Set a user's password and apply the "must change" flag.

        The encoding and the encrypted-channel requirement are handled by
        :meth:`services.ad_client.ADClient.set_password`.

        Args:
            dn: Distinguished Name of the user.
            password: The new password.
            person: The person, for the password policy flags.

        Raises:
            RuntimeError: If the password could not be set.
        """
        if not password:
            raise ValueError("Password cannot be empty")

        self.ad_client.set_password(dn, password)

        # If the user must change the password at next logon, pwdLastSet=0.
        # A failure here must not invalidate the password that was just set.
        if person.password_must_change:
            try:
                self.ad_client.modify_user(
                    dn, [(MODIFY_REPLACE, 'pwdLastSet', ['0'])]
                )
            except Exception as exc:
                logger.warning("Password was set for %s but the 'must change "
                               "at next logon' flag was not: %s", dn, exc)

    def _categorize_result(self, sync_result: SyncResult, op_result: OperationResult):
        """Add operation result to appropriate category"""
        if op_result.status == 'SUCCESS':
            sync_result.successful.append(op_result)
        elif op_result.status == 'FAILED':
            sync_result.failed.append(op_result)
        else:
            sync_result.skipped.append(op_result)
