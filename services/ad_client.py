"""
Active Directory Client - Low-level LDAP operations wrapper
UPDATED VERSION - Added home directory and group management support
"""

import logging
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass
from ssl import CERT_NONE

from services.ldap_compat import MODIFY_REPLACE, NTLM, require_ldap3

logger = logging.getLogger(__name__)


@dataclass
class ADUserEntry:
    """Represents an AD user entry"""
    dn: str
    attributes: Dict[str, Any]
    
    def get(self, attr_name: str, default=None):
        """Get attribute value"""
        return self.attributes.get(attr_name, default)


class ADClient:
    """Low-level wrapper around ldap3 for AD operations"""
    
    def __init__(self, server: str, username: str, password: str):
        """
        Initialize AD client
        
        Args:
            server: LDAP server URL (ldap://dc.example.com)
            username: Admin username (domain\\user)
            password: Admin password
        """
        self.server = server
        self.username = username
        self.password = password
        self.connection = None
        
    def connect(self) -> bool:
        """Establish connection to AD"""
        try:
            require_ldap3()
            from ldap3 import Server, Connection, ALL, Tls
            
            # Test 
            # tls_conf = Tls(validate=CERT_NONE)
            # server_obj = Server(self.server, port=636, use_ssl=True, get_info=ALL)
            # self.connection = Connection(
            #     server_obj,
            #     user=self.username,
            #     password=self.password,
            #     authentication=NTLM,
            #     auto_bind=True
            # )

            server_obj = Server(self.server, get_info=ALL)
            self.connection = Connection(
                server_obj,
                user=self.username,
                password=self.password,
                auto_bind=True
            )
            logger.info(f"Connected to AD server: {self.server}")
            return True
            
        except Exception as e:
            logger.exception("Failed to connect to AD")
            return False
    
    def disconnect(self):
        """Close connection to AD"""
        if self.connection:
            try:
                self.connection.unbind()
                logger.info("Disconnected from AD")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.connection = None
    
    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()
    
    def create_user(self, dn: str, attributes: Dict[str, Any]) -> bool:
        """
        Create a new user in AD
        
        Args:
            dn: Distinguished Name for new user
            attributes: LDAP attributes
            
        Returns:
            True if successful
        """
        try:
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            # Ensure required object classes
            ldap_attrs = {
                'objectClass': ['top', 'person', 'organizationalPerson', 'user'],
                **attributes
            }
            
            success = self.connection.add(dn, attributes=ldap_attrs)
            
            if success:
                logger.info(f"Created user: {dn}")
            else:
                logger.error(f"Failed to create user {dn}: {self.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error creating user {dn}")
            return False
    
    def modify_user(self, dn: str, operations: List[Tuple]) -> bool:
        """
        Modify an existing user in AD
        
        Args:
            dn: Distinguished Name of user
            operations: List of (operation_type, attribute, values) tuples
                       where operation_type is from ldap3 (MODIFY_REPLACE, MODIFY_ADD, etc.)
            
        Returns:
            True if successful
        """
        try:
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            # Build changes dictionary in ldap3 format
            # Format: {attribute: [(operation, [values])]}
            changes = {}
            for operation in operations:
                if len(operation) != 3:
                    logger.error(f"Invalid operation format: {operation}")
                    continue
                    
                op_type, attr, values = operation
                
                # Ensure values is a list
                if not isinstance(values, list):
                    values = [values] if values is not None else []
                
                # Append instead of assign: ldap3 accepts a LIST of operations
                # per attribute, and overwriting it meant that e.g. "remove this
                # group, add that group" silently lost the first half.
                changes.setdefault(attr, []).append((op_type, values))
            
            if not changes:
                logger.warning(f"No valid changes to apply for {dn}")
                return False
            
            success = self.connection.modify(dn, changes)
            
            if success:
                logger.info(f"Modified user: {dn}")
            else:
                logger.error(f"Failed to modify user {dn}: {self.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error modifying user {dn}")
            return False
    
    def set_home_directory(self, user_dn: str, home_path: str, 
                          home_drive: Optional[str] = None) -> bool:
        """
        Set home directory for a user
        
        Args:
            user_dn: Distinguished Name of user
            home_path: UNC path to home directory (e.g., \\\\server\\share\\user)
            home_drive: Drive letter (e.g., 'H:') - optional
            
        Returns:
            True if successful
        """
        try:
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            operations = []
            
            # Set home directory path
            if home_path:
                operations.append((MODIFY_REPLACE, 'homeDirectory', [home_path]))
            
            # Set home drive if specified
            if home_drive:
                operations.append((MODIFY_REPLACE, 'homeDrive', [home_drive]))
            
            if not operations:
                logger.warning("No home directory changes specified")
                return False
            
            success = self.modify_user(user_dn, operations)
            
            if success:
                logger.info(f"Set home directory for {user_dn}: {home_path}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error setting home directory for {user_dn}")
            return False
    
    def get_home_directory(self, user_dn: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Get home directory settings for a user
        
        Args:
            user_dn: Distinguished Name of user
            
        Returns:
            Tuple of (home_directory, home_drive) or (None, None)
        """
        try:
            user_entry = self.get_user(user_dn, attributes=['homeDirectory', 'homeDrive'])
            
            if not user_entry:
                return None, None
            
            home_dir = user_entry.get('homeDirectory')
            home_drive = user_entry.get('homeDrive')
            
            return home_dir, home_drive
            
        except Exception as e:
            logger.exception(f"Error getting home directory for {user_dn}")
            return None, None
    
    def get_user(self, dn: str, attributes: Optional[List[str]] = None) -> Optional[ADUserEntry]:
        """
        Get a user by DN
        
        Args:
            dn: Distinguished Name
            attributes: List of attributes to fetch (None = all)
            
        Returns:
            ADUserEntry or None if not found
        """
        try:
            from services.ldap_compat import BASE
            
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            if attributes is None:
                attributes = ['*', 'modifyTimestamp', 'createTimestamp']
            
            self.connection.search(
                search_base=dn,
                search_filter='(objectClass=user)',
                search_scope=BASE,
                attributes=attributes
            )
            
            if self.connection.entries:
                entry = self.connection.entries[0]
                attrs = {}
                for attr in entry.entry_attributes:
                    value = entry[attr].value
                    attrs[attr] = value
                
                return ADUserEntry(dn=entry.entry_dn, attributes=attrs)
            
            return None
            
        except Exception as e:
            logger.exception(f"Error getting user {dn}")
            return None
    
    def search_users(
        self,
        base_dn: str,
        search_filter: str,
        attributes: Optional[List[str]] = None
    ) -> List[ADUserEntry]:
        """
        Search for users
        
        Args:
            base_dn: Base DN to search in
            search_filter: LDAP search filter
            attributes: Attributes to retrieve
            
        Returns:
            List of matching ADUserEntry objects
        """
        try:
            from services.ldap_compat import SUBTREE
            
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            if attributes is None:
                attributes = ['*', 'modifyTimestamp', 'createTimestamp']
            
            self.connection.search(
                search_base=base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=attributes
            )
            
            results = []
            for entry in self.connection.entries:
                attrs = {}
                for attr in entry.entry_attributes:
                    value = entry[attr].value
                    attrs[attr] = value
                
                results.append(ADUserEntry(dn=entry.entry_dn, attributes=attrs))
            
            logger.debug(f"Search found {len(results)} results")
            return results
            
        except Exception as e:
            logger.exception(f"Error searching users")
            return []
    
    def delete_user(self, dn: str) -> bool:
        """
        Delete a user from AD
        
        Args:
            dn: Distinguished Name
            
        Returns:
            True if successful
        """
        try:
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            success = self.connection.delete(dn)
            
            if success:
                logger.info(f"Deleted user: {dn}")
            else:
                logger.error(f"Failed to delete user {dn}: {self.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error deleting user {dn}")
            return False
    
    def create_ou(self, ou_dn: str, ou_name: str) -> bool:
        """
        Create an organizational unit
        
        Args:
            ou_dn: Full DN of OU to create
            ou_name: Name of OU
            
        Returns:
            True if successful
        """
        try:
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            attributes = {
                'objectClass': ['organizationalUnit'],
                'ou': ou_name
            }
            
            success = self.connection.add(ou_dn, attributes=attributes)
            
            if success:
                logger.info(f"Created OU: {ou_dn}")
            else:
                logger.error(f"Failed to create OU {ou_dn}: {self.connection.result}")
            
            return success
            
        except Exception as e:
            logger.exception(f"Error creating OU {ou_dn}")
            return False
    
    def ou_exists(self, ou_dn: str) -> bool:
        """Check if an OU exists"""
        try:
            from services.ldap_compat import BASE
            
            if not self.connection:
                raise RuntimeError("Not connected to AD")
            
            self.connection.search(
                search_base=ou_dn,
                search_filter='(objectClass=organizationalUnit)',
                search_scope=BASE,
                attributes=['ou']
            )
            
            return len(self.connection.entries) > 0
            
        except Exception as e:
            logger.debug(f"OU {ou_dn} does not exist or error occurred: {e}")
            return False
