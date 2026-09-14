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
    
    def __init__(self, server: str, username: str, password: str,
                 use_ssl: Optional[bool] = None, use_start_tls: bool = True,
                 validate_certificate: bool = False):
        """
        Initialize AD client

        Args:
            server: LDAP server URL (``ldap://dc.example.com`` or
                ``ldaps://dc.example.com``)
            username: Admin username (domain\\user)
            password: Admin password
            use_ssl: Force LDAPS on/off. ``None`` derives it from the URL
                scheme (``ldaps://`` -> True).
            use_start_tls: When the connection is not already LDAPS, try to
                upgrade it with StartTLS. Active Directory REFUSES to set a
                password over an unencrypted channel, so this is on by default.
            validate_certificate: Verify the server certificate. Off by
                default because school domain controllers commonly use a
                self-signed certificate.
        """
        self.server = server
        self.username = username
        self.password = password
        self.connection = None

        self.use_ssl = use_ssl
        self.use_start_tls = use_start_tls
        self.validate_certificate = validate_certificate

        #: True once the channel is encrypted (LDAPS or a successful StartTLS).
        #: Password operations check this before they even try.
        self.is_secure = False
        #: Why the channel could not be encrypted, for the error message.
        self.tls_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _resolve_ssl(self) -> bool:
        """Decide whether to open the connection as LDAPS."""
        if self.use_ssl is not None:
            return bool(self.use_ssl)
        return str(self.server or "").strip().lower().startswith("ldaps://")

    def connect(self) -> bool:
        """
        Establish a connection to AD, encrypting it where possible.

        Active Directory only accepts a password change (``unicodePwd``) over
        an encrypted channel. Over plain LDAP it answers

            result 53, unwillingToPerform,
            "0000001F: SvcErr: DSID-031A1260, problem 5003 (WILL_NOT_PERFORM)"

        which is exactly the error users were seeing. The connection is
        therefore opened as LDAPS when the URL says so, and otherwise upgraded
        with StartTLS. A failed upgrade is not fatal - everything except
        password changes works fine unencrypted - but it is remembered in
        :attr:`tls_error` so the password step can explain itself.

        Returns:
            True when the bind succeeded.
        """
        try:
            require_ldap3()
            from ldap3 import Server, Connection, ALL, Tls

            use_ssl = self._resolve_ssl()

            tls_configuration = None
            try:
                import ssl as _ssl
                tls_configuration = Tls(
                    validate=_ssl.CERT_REQUIRED if self.validate_certificate
                    else _ssl.CERT_NONE
                )
            except Exception as exc:                 # pragma: no cover - defensive
                logger.warning("Could not build a TLS configuration: %s", exc)

            server_obj = Server(self.server, use_ssl=use_ssl, get_info=ALL,
                                tls=tls_configuration)
            self.connection = Connection(
                server_obj,
                user=self.username,
                password=self.password,
                auto_bind=True
            )

            self.is_secure = bool(use_ssl)
            self.tls_error = None

            if not self.is_secure and self.use_start_tls:
                try:
                    if self.connection.start_tls():
                        self.is_secure = True
                        logger.info("Upgraded the AD connection with StartTLS")
                    else:
                        self.tls_error = str(self.connection.result)
                        logger.warning("StartTLS was refused by the server: %s",
                                       self.tls_error)
                except Exception as exc:
                    self.tls_error = str(exc)
                    logger.warning("StartTLS failed: %s", exc)

            logger.info("Connected to AD server: %s (encrypted: %s)",
                        self.server, "yes" if self.is_secure else "NO")
            return True

        except Exception as e:
            logger.exception("Failed to connect to AD")
            return False

    def require_secure_channel(self, operation: str = "this operation") -> None:
        """
        Raise a helpful error when the channel is not encrypted.

        Args:
            operation: Name of the operation for the message.

        Raises:
            RuntimeError: If the connection is not encrypted.
        """
        if self.is_secure:
            return

        detail = f" (StartTLS failed: {self.tls_error})" if self.tls_error else ""
        raise RuntimeError(
            f"Active Directory refuses {operation} over an unencrypted "
            f"connection{detail}.\n\n"
            f"Use an 'ldaps://' server address (port 636), or enable StartTLS, "
            f"and make sure the domain controller has a server certificate "
            f"installed."
        )

    def set_password(self, dn: str, password: str) -> bool:
        """
        Set a user's password.

        Uses the dedicated ldap3 helper, which builds the UTF-16-LE encoded
        ``unicodePwd`` value the way Active Directory expects and picks the
        right modification for the server.

        Args:
            dn: Distinguished Name of the user.
            password: The new password.

        Returns:
            True on success.

        Raises:
            RuntimeError: If not connected, the channel is unencrypted, or the
                server refused the change.
        """
        if not self.connection:
            raise RuntimeError("Not connected to AD")
        if not password:
            raise ValueError("Password cannot be empty")

        self.require_secure_channel("a password change")

        try:
            success = self.connection.extend.microsoft.modify_password(dn, password)
        except Exception as exc:
            logger.error("Password change for %s raised: %s", dn, exc)
            raise RuntimeError(f"Failed to set the password: {exc}")

        if not success:
            result = self.connection.result or {}
            description = result.get('description', 'unknown error')
            message = result.get('message', '')
            logger.error("Failed to set password for %s: %s", dn, result)

            if 'WILL_NOT_PERFORM' in str(message) or description == 'unwillingToPerform':
                raise RuntimeError(
                    "Active Directory refused the password change "
                    "(unwillingToPerform). The usual causes are an "
                    "unencrypted connection, a password that does not meet the "
                    "domain password policy, or the account used for the "
                    "connection lacking the 'Reset password' permission."
                )
            raise RuntimeError(f"Failed to set the password: {description} {message}")

        logger.info("Password set for %s", dn)
        return True
    
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
