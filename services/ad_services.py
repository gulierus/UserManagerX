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
from services.ad_group_service import ADGroupService
from services.ldap_compat import MODIFY_REPLACE


logger = logging.getLogger(__name__)


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
    
    def __init__(self, ad_client):
        self.ad_client = ad_client
        
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
                    person.ad_status = ADStatus.EXISTS_IN_AD
                    person.ad_dn = result.ad_dn
                    person.ad_version = result.ad_version
                    # Store current AD values for conflict detection
                    person.metadata['ad_current_values'] = result.ad_attributes
                elif result.ambiguous:
                    person.ad_status = ADStatus.AMBIGUOUS
                else:
                    person.ad_status = ADStatus.NOT_IN_AD
                    
            except Exception as e:
                logger.exception(f"Error discovering person {person.first_name} {person.last_name}")
                results.append(DiscoveryResult(person=person, found_in_ad=False))
                
        return results
    
    def _discover_single_person(self, person: Person, base_dn: str) -> DiscoveryResult:
        """Discover a single person in AD"""
        
        # Strategy 1: Search by DN if available
        if person.ad_dn:
            try:
                entry = self.ad_client.get_user(person.ad_dn)
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
            matches = self.ad_client.search_users(
                base_dn,
                f"(sAMAccountName={person.ad_username})"
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
            matches = self.ad_client.search_users(
                base_dn,
                f"(mail={person.ad_email})"
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
        matches = self.ad_client.search_users(base_dn, search_filter)
        
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
                if not person.is_dirty() and person.ad_status == ADStatus.SYNCED:
                    continue  # Nothing to do
                
                if person.ad_status in [ADStatus.NOT_IN_AD, ADStatus.UNKNOWN] or not person.ad_dn:
                    # New user to create
                    if self._has_required_ad_data(person):
                        target_ou = f"OU=Trida-{person.class_name},{base_dn}"
                        attributes = self._prepare_creation_attributes(person, base_dn)
                        plan.create_operations.append(
                            CreateOperation(person, target_ou, attributes)
                        )
                    else:
                        plan.incomplete_persons.append(person)
                
                elif person.ad_status == ADStatus.EXISTS_IN_AD:
                    # Update existing user
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
        """Execute a create operation with password policy"""
        person = create_op.person
        
        try:
            # Ensure OU exists first
            if not self.ad_client.ou_exists(create_op.target_ou):
                ou_name = create_op.target_ou.split(',')[0].split('=')[1]
                logger.info(f"Creating OU: {create_op.target_ou}")
                if not self.ad_client.create_ou(create_op.target_ou, ou_name):
                    return OperationResult(
                        person=person,
                        status='FAILED',
                        message=f"Failed to create OU: {create_op.target_ou}"
                    )
            
            # Build DN
            cn = person.ad_display_name or person.ad_username
            dn = f"CN={cn},{create_op.target_ou}"
            
            # Prepare attributes with password policy
            attributes = create_op.attributes.copy()
            
            # Add user account control flags
            uac_flags = self._build_uac_flags(person)
            attributes['userAccountControl'] = str(uac_flags)
            
            # Create user in AD
            success = self.ad_client.create_user(dn, attributes)
            
            if not success:
                return OperationResult(
                    person=person,
                    status='FAILED',
                    message="Failed to create user in AD"
                )
            
            # Set password (must be done after creation)
            if person.ad_password:
                try:
                    self._set_password(dn, person.ad_password, person)
                except Exception as pwd_error:
                    # NEVER log the password itself - the log file is readable
                    # in the application and is often shared for support.
                    logger.error(
                        "User %s created but password setting failed: %s",
                        person.ad_username, pwd_error
                    )
                    # User is created but password failed - still mark as partial success
                    return OperationResult(
                        person=person,
                        status='SUCCESS',
                        message=f"Created user {person.ad_username} but password setting failed"
                    )
            
            # Update person with successful creation
            person.ad_dn = dn
            person.ad_status = ADStatus.SYNCED
            person.reset_dirty()
            
            # After creating/updating user, sync groups.  A failure here must
            # not undo the successful creation, so it is logged and reported
            # instead of propagating.
            if person.group_memberships:
                try:
                    ADGroupService(self.ad_client).sync_user_groups(person)
                except Exception as group_error:
                    logger.error("Group synchronisation failed for %s: %s",
                                 person.ad_username, group_error)

            # Fetch fresh timestamp
            entry = self.ad_client.get_user(dn)
            if entry:
                person.ad_version = entry.get('modifyTimestamp')
                person.metadata['ad_current_values'] = entry.attributes
            
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
    
    def _execute_update(
        self,
        update_op: UpdateOperation,
        conflict_strategy: ConflictStrategy,
        user_choices: Optional[Dict[str, Dict[str, str]]]
    ) -> OperationResult:
        """Execute an update operation with password policy"""
        person = update_op.person
        
        try:
            # Handle conflicts
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
            
            # Separate password/policy changes from regular attribute changes
            ldap_ops = []
            password_changed = False
            policy_changed = False
            
            for field, value in changes.items():
                if field == 'ad_password':
                    password_changed = True
                elif field in ['password_must_change', 'password_cannot_change', 
                              'password_never_expires', 'account_enabled']:
                    policy_changed = True
                else:
                    # Regular LDAP attribute
                    ldap_attr = self._map_field_to_ldap(field)
                    if ldap_attr:
                        # Ensure value is properly formatted for LDAP
                        ldap_value = [str(value)] if value is not None else []
                        ldap_ops.append((MODIFY_REPLACE, ldap_attr, ldap_value))
            
            # Apply regular attribute changes
            if ldap_ops:
                success = self.ad_client.modify_user(person.ad_dn, ldap_ops)
                if not success:
                    return OperationResult(
                        person=person,
                        status='FAILED',
                        message="Failed to update user attributes"
                    )
            
            # Apply password change
            if password_changed:
                try:
                    self._set_password(person.ad_dn, person.ad_password, person)
                except Exception as pwd_error:
                    logger.error(f"Password update failed: {pwd_error}")
                    return OperationResult(
                        person=person,
                        status='FAILED',
                        message=f"Failed to update password: {pwd_error}"
                    )
            
            # Apply policy changes
            if policy_changed:
                try:
                    self._update_account_flags(person)
                except Exception as policy_error:
                    logger.error(f"Policy update failed: {policy_error}")
                    return OperationResult(
                        person=person,
                        status='FAILED',
                        message=f"Failed to update account policy: {policy_error}"
                    )
            
            # After creating/updating user, sync groups
            if person.group_memberships:
                try:
                    ADGroupService(self.ad_client).sync_user_groups(person)
                except Exception as group_error:
                    logger.error("Group synchronisation failed for %s: %s",
                                 person.ad_username, group_error)

            # Update person status
            person.ad_status = ADStatus.SYNCED
            person.reset_dirty()
            
            # Fetch fresh timestamp
            entry = self.ad_client.get_user(person.ad_dn)
            if entry:
                person.ad_version = entry.get('modifyTimestamp')
                person.metadata['ad_current_values'] = entry.attributes
            
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
        mapping = {
            'first_name': 'givenName',
            'last_name': 'sn',
            'ad_username': 'sAMAccountName',
            'ad_email': 'mail',
            'ad_display_name': 'displayName',
            'ad_description': 'description'
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
        """Set password with proper encoding and policy"""
        try:
            # Validate password is not empty
            if not password:
                raise ValueError("Password cannot be empty")
            
            # Encode password in UTF-16-LE with quotes as required by AD
            password_value = f'"{password}"'
            try:
                password_encoded = password_value.encode('utf-16-le')
            except UnicodeEncodeError as e:
                raise ValueError(f"Password contains invalid characters: {e}")
            
            # Set password
            success = self.ad_client.modify_user(
                dn,
                [(MODIFY_REPLACE, 'unicodePwd', [password_encoded])]
            )
            
            if not success:
                raise RuntimeError("Failed to set password in AD")
            
            # If must change password, set pwdLastSet to 0
            if person.password_must_change:
                self.ad_client.modify_user(
                    dn,
                    [(MODIFY_REPLACE, 'pwdLastSet', ['0'])]
                )
            
            logger.info(f"Set password for {dn}")
            
        except Exception as e:
            logger.error(f"Failed to set password for {dn}: {e}")
            raise

    def _categorize_result(self, sync_result: SyncResult, op_result: OperationResult):
        """Add operation result to appropriate category"""
        if op_result.status == 'SUCCESS':
            sync_result.successful.append(op_result)
        elif op_result.status == 'FAILED':
            sync_result.failed.append(op_result)
        else:
            sync_result.skipped.append(op_result)
