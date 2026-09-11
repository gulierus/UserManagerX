"""
Active Directory Group Management Service
Provides functionality for discovering, managing, and synchronizing AD groups
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any, Set
from dataclasses import dataclass
from models import Person, ADGroup, GroupTemplate
from services.ldap_compat import MODIFY_ADD, MODIFY_DELETE, require_ldap3

logger = logging.getLogger(__name__)


@dataclass
class GroupDiscoveryResult:
    """Result from group discovery operation"""
    groups: List[ADGroup]
    total_found: int
    errors: List[str]


class ADGroupService:
    """Service for Active Directory group operations"""
    
    def __init__(self, ad_client):
        """
        Initialize group service
        
        Args:
            ad_client: ADClient instance for LDAP operations
        """
        self.ad_client = ad_client
    
    def discover_groups_in_ou(self, ou_dn: str) -> GroupDiscoveryResult:
        """
        Discover all groups in a specific Organizational Unit
        
        Args:
            ou_dn: Distinguished Name of the OU to search
            
        Returns:
            GroupDiscoveryResult with discovered groups
        """
        groups = []
        errors = []
        
        try:
            if not self.ad_client.connection:
                raise RuntimeError("Not connected to AD")
            
            # Search for all group objects in the OU
            from services.ldap_compat import SUBTREE, require_ldap3
            require_ldap3()
            
            search_filter = '(objectClass=group)'
            
            logger.info(f"Searching for groups in: {ou_dn}")
            
            self.ad_client.connection.search(
                search_base=ou_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=['cn', 'distinguishedName', 'description', 
                           'groupType', 'member', 'objectClass']
            )
            
            for entry in self.ad_client.connection.entries:
                try:
                    # Extract group information
                    cn = entry.cn.value if hasattr(entry, 'cn') else 'Unknown'
                    dn = entry.distinguishedName.value if hasattr(entry, 'distinguishedName') else None
                    description = entry.description.value if hasattr(entry, 'description') else None
                    
                    # Get group type
                    group_type = self._determine_group_type(entry)
                    
                    # Count members
                    members_count = 0
                    if hasattr(entry, 'member'):
                        members = entry.member.values if hasattr(entry.member, 'values') else []
                        members_count = len(members) if members else 0
                    
                    if dn:
                        group = ADGroup(
                            name=cn,
                            dn=dn,
                            description=description,
                            group_type=group_type,
                            members_count=members_count
                        )
                        groups.append(group)
                        logger.debug(f"Found group: {cn} ({dn})")
                    
                except Exception as e:
                    logger.warning(f"Error processing group entry: {e}")
                    errors.append(f"Error processing group: {str(e)}")
            
            logger.info(f"Discovered {len(groups)} groups in {ou_dn}")
            
        except Exception as e:
            logger.exception(f"Error discovering groups in {ou_dn}")
            errors.append(f"Discovery failed: {str(e)}")
        
        return GroupDiscoveryResult(
            groups=groups,
            total_found=len(groups),
            errors=errors
        )
    
    def get_group_by_dn(self, group_dn: str) -> Optional[ADGroup]:
        """
        Retrieve a specific group by its DN
        
        Args:
            group_dn: Distinguished Name of the group
            
        Returns:
            ADGroup object or None if not found
        """
        try:
            if not self.ad_client.connection:
                raise RuntimeError("Not connected to AD")
            
            from services.ldap_compat import BASE
            
            self.ad_client.connection.search(
                search_base=group_dn,
                search_filter='(objectClass=group)',
                search_scope=BASE,
                attributes=['cn', 'distinguishedName', 'description', 
                           'groupType', 'member']
            )
            
            if not self.ad_client.connection.entries:
                logger.warning(f"Group not found: {group_dn}")
                return None
            
            entry = self.ad_client.connection.entries[0]
            
            cn = entry.cn.value if hasattr(entry, 'cn') else 'Unknown'
            dn = entry.distinguishedName.value if hasattr(entry, 'distinguishedName') else group_dn
            description = entry.description.value if hasattr(entry, 'description') else None
            group_type = self._determine_group_type(entry)
            
            # Count members
            members_count = 0
            if hasattr(entry, 'member'):
                members = entry.member.values if hasattr(entry.member, 'values') else []
                members_count = len(members) if members else 0
            
            return ADGroup(
                name=cn,
                dn=dn,
                description=description,
                group_type=group_type,
                members_count=members_count
            )
            
        except Exception as e:
            logger.exception(f"Error retrieving group {group_dn}")
            return None
    
    def verify_groups_exist(self, groups: List[ADGroup]) -> Dict[str, bool]:
        """
        Verify if groups exist in AD
        
        Args:
            groups: List of ADGroup objects to verify
            
        Returns:
            Dictionary mapping group DN to existence status
        """
        results = {}
        
        for group in groups:
            try:
                found_group = self.get_group_by_dn(group.dn)
                results[group.dn] = found_group is not None
            except Exception as e:
                logger.warning(f"Error verifying group {group.dn}: {e}")
                results[group.dn] = False
        
        return results
    
    def add_user_to_group(self, user_dn: str, group_dn: str) -> bool:
        """
        Add a user to a group
        
        Args:
            user_dn: Distinguished Name of the user
            group_dn: Distinguished Name of the group
            
        Returns:
            True if successful
        """
        try:
            if not self.ad_client.connection:
                raise RuntimeError("Not connected to AD")
            
            # Modify group to add member
            success = self.ad_client.connection.modify(
                group_dn,
                {'member': [(MODIFY_ADD, [user_dn])]}
            )
            
            if success:
                logger.info(f"Added user {user_dn} to group {group_dn}")
            else:
                logger.error(f"Failed to add user to group: {self.ad_client.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error adding user {user_dn} to group {group_dn}")
            return False
    
    def remove_user_from_group(self, user_dn: str, group_dn: str) -> bool:
        """
        Remove a user from a group
        
        Args:
            user_dn: Distinguished Name of the user
            group_dn: Distinguished Name of the group
            
        Returns:
            True if successful
        """
        try:
            if not self.ad_client.connection:
                raise RuntimeError("Not connected to AD")
            
            # Modify group to remove member
            success = self.ad_client.connection.modify(
                group_dn,
                {'member': [(MODIFY_DELETE, [user_dn])]}
            )
            
            if success:
                logger.info(f"Removed user {user_dn} from group {group_dn}")
            else:
                logger.error(f"Failed to remove user from group: {self.ad_client.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error removing user {user_dn} from group {group_dn}")
            return False
    
    def get_user_groups(self, user_dn: str) -> List[ADGroup]:
        """
        Get all groups a user is a member of
        
        Args:
            user_dn: Distinguished Name of the user
            
        Returns:
            List of ADGroup objects
        """
        groups = []
        
        try:
            if not self.ad_client.connection:
                raise RuntimeError("Not connected to AD")
            
            from services.ldap_compat import BASE
            
            # Get user's memberOf attribute
            self.ad_client.connection.search(
                search_base=user_dn,
                search_filter='(objectClass=user)',
                search_scope=BASE,
                attributes=['memberOf']
            )
            
            if not self.ad_client.connection.entries:
                logger.warning(f"User not found: {user_dn}")
                return groups
            
            entry = self.ad_client.connection.entries[0]
            
            if hasattr(entry, 'memberOf'):
                member_of = entry.memberOf.values if hasattr(entry.memberOf, 'values') else []
                
                for group_dn in member_of:
                    # Retrieve full group information
                    group = self.get_group_by_dn(group_dn)
                    if group:
                        groups.append(group)
            
            logger.debug(f"User {user_dn} is member of {len(groups)} groups")
            
        except Exception as e:
            logger.exception(f"Error retrieving groups for user {user_dn}")
        
        return groups
    
    def sync_user_groups(self, person: Person) -> bool:
        """
        Synchronize a user's group memberships with AD
        
        Args:
            person: Person object with desired group memberships
            
        Returns:
            True if all operations successful
        """
        if not person.ad_dn:
            logger.warning(f"Cannot sync groups for {person.first_name} {person.last_name} - no AD DN")
            return False
        
        try:
            # Get current groups from AD
            current_groups = self.get_user_groups(person.ad_dn)
            current_dns = set(g.dn.lower() for g in current_groups)
            
            # Get desired groups
            desired_dns = set(g.dn.lower() for g in person.group_memberships)
            
            # Find groups to add and remove
            to_add = desired_dns - current_dns
            to_remove = current_dns - desired_dns
            
            success = True
            
            # Add to new groups
            for dn in to_add:
                # Find the group object
                group = next((g for g in person.group_memberships if g.dn.lower() == dn), None)
                if group:
                    if not self.add_user_to_group(person.ad_dn, group.dn):
                        success = False
                        logger.error(f"Failed to add {person.ad_username} to group {group.name}")
            
            # Remove from old groups
            for dn in to_remove:
                # Find the group object
                group = next((g for g in current_groups if g.dn.lower() == dn), None)
                if group:
                    if not self.remove_user_from_group(person.ad_dn, group.dn):
                        success = False
                        logger.error(f"Failed to remove {person.ad_username} from group {group.name}")
            
            if success:
                logger.info(f"Successfully synced groups for {person.ad_username}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error syncing groups for {person.ad_username}")
            return False
    
    def _determine_group_type(self, entry) -> str:
        """
        Determine group type from LDAP entry
        
        Args:
            entry: LDAP entry object
            
        Returns:
            Group type string ('security', 'distribution', or 'unknown')
        """
        try:
            if hasattr(entry, 'groupType'):
                group_type_value = entry.groupType.value if hasattr(entry.groupType, 'value') else None
                
                if group_type_value is not None:
                    # AD group type flags
                    # -2147483646 = Global Security Group
                    # -2147483644 = Domain Local Security Group
                    # -2147483640 = Universal Security Group
                    # 2 = Global Distribution Group
                    # 4 = Domain Local Distribution Group
                    # 8 = Universal Distribution Group
                    
                    if isinstance(group_type_value, int):
                        if group_type_value < 0:
                            return 'security'
                        else:
                            return 'distribution'
            
            return 'unknown'
            
        except Exception as e:
            logger.debug(f"Error determining group type: {e}")
            return 'unknown'


class GroupTemplateManager:
    """Manager for group templates"""
    
    def __init__(self):
        """Initialize template manager"""
        self.templates: List[GroupTemplate] = []
        logger.info("GroupTemplateManager initialized")
    
    def add_template(self, template: GroupTemplate) -> bool:
        """
        Add a template to the manager
        
        Args:
            template: GroupTemplate to add
            
        Returns:
            True if added, False if name already exists
        """
        if self.template_exists(template.name):
            logger.warning(f"Template '{template.name}' already exists")
            return False
        
        self.templates.append(template)
        logger.info(f"Added template: {template.name}")
        return True
    
    def remove_template(self, template_name: str) -> bool:
        """
        Remove a template by name
        
        Args:
            template_name: Name of template to remove
            
        Returns:
            True if removed
        """
        for template in self.templates:
            if template.name == template_name:
                self.templates.remove(template)
                logger.info(f"Removed template: {template_name}")
                return True
        
        logger.warning(f"Template not found: {template_name}")
        return False
    
    def get_template(self, template_name: str) -> Optional[GroupTemplate]:
        """
        Get a template by name
        
        Args:
            template_name: Name of template
            
        Returns:
            GroupTemplate or None
        """
        for template in self.templates:
            if template.name == template_name:
                return template
        return None
    
    def template_exists(self, template_name: str) -> bool:
        """Check if a template with given name exists"""
        return any(t.name == template_name for t in self.templates)
    
    def get_all_templates(self) -> List[GroupTemplate]:
        """Get all templates"""
        return self.templates.copy()
    
    def rename_template(self, old_name: str, new_name: str) -> bool:
        """
        Rename a template
        
        Args:
            old_name: Current template name
            new_name: New template name
            
        Returns:
            True if renamed successfully
        """
        if self.template_exists(new_name):
            logger.warning(f"Cannot rename - template '{new_name}' already exists")
            return False
        
        template = self.get_template(old_name)
        if template:
            template.name = new_name
            template.modified_date = datetime.now()
            logger.info(f"Renamed template: {old_name} -> {new_name}")
            return True
        
        logger.warning(f"Template not found: {old_name}")
        return False
    
    def copy_template(self, template_name: str, new_name: str) -> Optional[GroupTemplate]:
        """
        Create a copy of a template
        
        Args:
            template_name: Name of template to copy
            new_name: Name for the copy
            
        Returns:
            New GroupTemplate or None if failed
        """
        if self.template_exists(new_name):
            logger.warning(f"Cannot copy - template '{new_name}' already exists")
            return None
        
        original = self.get_template(template_name)
        if original:
            copy = original.copy(new_name)
            self.templates.append(copy)
            logger.info(f"Copied template: {template_name} -> {new_name}")
            return copy
        
        logger.warning(f"Template not found: {template_name}")
        return None
