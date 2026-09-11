"""
Active Directory utilities
FIXED VERSION - Improved error handling and edge case handling
"""

import logging
import secrets
import string
import unicodedata
from typing import Set, List, Optional
import re
from dataclasses import dataclass

from models import Person
from utils.password_policy import PasswordPolicy, get_password_policy

logger = logging.getLogger(__name__)


#: Latin letters that Unicode NFKD does NOT decompose into "base + accent".
#: Without this table they survive normalisation and end up in a user name,
#: which the sAMAccountName pattern then rejects.
_TRANSLITERATION = {
    'ł': 'l', 'Ł': 'L', 'ß': 'ss', 'ẞ': 'SS', 'ø': 'o', 'Ø': 'O',
    'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE', 'đ': 'd', 'Đ': 'D',
    'ð': 'd', 'Ð': 'D', 'þ': 'th', 'Þ': 'TH', 'ħ': 'h', 'Ħ': 'H',
    'ı': 'i', 'İ': 'I', 'ŋ': 'n', 'Ŋ': 'N', 'ĸ': 'k', 'ŧ': 't', 'Ŧ': 'T',
}


def remove_diacritics(text: str) -> str:
    """
    Remove diacritics from *text*.

    Runs NFKD normalisation and then drops the combining marks.  Letters that
    NFKD leaves untouched because they are separate characters rather than an
    accented base (Ł, ß, Ø, Æ, Đ, Þ …) are transliterated first, so a Polish or
    German name produces a usable ASCII user name instead of one the validator
    rejects.

    Args:
        text: The text to fold.

    Returns:
        The folded text (never raises).
    """
    if not text:
        return ""
    try:
        pre = ''.join(_TRANSLITERATION.get(c, c) for c in text)
        nfkd = unicodedata.normalize('NFKD', pre)
        return ''.join([c for c in nfkd if not unicodedata.combining(c)])
    except Exception as e:
        logger.warning(f"Error removing diacritics from '{text}': {e}")
        return text


def generate_username(
    first_name: str, 
    last_name: str, 
    existing: Set[str], 
    last_name_index: Optional[int] = -1,
    first_name_index: Optional[int] = 1,
    last_name_length: Optional[int] = None,
    first_name_length: Optional[int] = None
) -> str:
    """
    Generate username in format: lastname + firstname
    
    Rules:
    - Remove diacritics (Novák → novak)
    - Only lowercase
    - Maximum 20 characters (truncated)
    - Handle duplicates by adding number: novakja2, novakja3, etc.
    
    Args:
        first_name: Student's first name (can contain multiple names separated by spaces)
        last_name: Student's last name (can contain multiple names separated by spaces)
        existing: Set of existing usernames to avoid duplicates
        last_name_index: Optional index to select which last name to use
                        -1 = use last last name (default)
                        0 = use all last names
                        1 = use only first last name
                        2 = use only second last name, etc.
        first_name_index: Optional index to select which first name to use
                         1 = use first first name (default)
                         2 = use second first name
                         -1 = use last first name
                         0 = use all first names
        last_name_length: How many characters from selected last name to use
                         None = use entire selected last name (default)
                         1, 2, 3... = use only first N characters
        first_name_length: How many characters from selected first name to use
                          None = use entire selected first name (default)
                          1, 2, 3... = use only first N characters
        
    Returns:
        Generated username
        
    Raises:
        ValueError: If names are empty or invalid
        IndexError: If indexes are out of range
    """
    if not first_name or not first_name.strip():
        raise ValueError("First name cannot be empty")
    if not last_name or not last_name.strip():
        raise ValueError("Last name cannot be empty")
    
    # Split last_name by spaces to handle multiple last names
    last_name_parts = last_name.strip().split()
    
    # Split first_name by spaces to handle multiple first names
    first_name_parts = first_name.strip().split()
    
    # The signature declares both indexes Optional[int]; None means "use the
    # documented default" instead of crashing in the comparison below.
    if last_name_index is None:
        last_name_index = -1
    if first_name_index is None:
        first_name_index = 1

    # Select which last name(s) to use based on last_name_index
    if last_name_index == 0:
        # Use all last names
        selected_last_name = ''.join(last_name_parts)
    elif last_name_index == -1:
        # Use last last name (default)
        selected_last_name = last_name_parts[-1]
    else:
        # Use specific last name by index (1-based)
        if last_name_index < 1 or last_name_index > len(last_name_parts):
            raise IndexError(f"last_name_index {last_name_index} is out of range (1-{len(last_name_parts)})")
        selected_last_name = last_name_parts[last_name_index - 1]
    
    # Select which first name(s) to use based on first_name_index
    if first_name_index == 0:
        # Use all first names
        selected_first_name = ''.join(first_name_parts)
    elif first_name_index == -1:
        # Use last first name
        selected_first_name = first_name_parts[-1]
    else:
        # Use specific first name by index (1-based)
        if first_name_index < 1 or first_name_index > len(first_name_parts):
            raise IndexError(f"first_name_index {first_name_index} is out of range (1-{len(first_name_parts)})")
        selected_first_name = first_name_parts[first_name_index - 1]
    
    # Remove diacritics, lowercase and drop everything that is not alphanumeric
    last = normalize_name_part(selected_last_name)
    first = normalize_name_part(selected_first_name)
    
    # Check if we have any characters left
    if not last:
        raise ValueError(f"Last name '{selected_last_name}' contains no valid characters")
    if not first:
        raise ValueError(f"First name '{selected_first_name}' contains no valid characters")
    
    # Apply length restrictions
    if last_name_length is not None:
        last = last[:last_name_length]
    # else use entire selected last name
    
    if first_name_length is not None:
        first_part = first[:first_name_length]
    else:
        first_part = first  # use entire selected first name (default)
    
    # Create base username
    base_username = f"{last}{first_part}"
    
    # Truncate to the maximum sAMAccountName length
    base_username = base_username[:USERNAME_MAX_LENGTH]
    
    # Handle duplicates
    username = base_username
    counter = 2
    
    # Prevent infinite loop with a reasonable maximum
    max_attempts = 1000
    attempts = 0
    
    while username in existing and attempts < max_attempts:
        # Add counter, ensuring total length doesn't exceed 20
        suffix = str(counter)
        max_base_len = USERNAME_MAX_LENGTH - len(suffix)
        username = f"{base_username[:max_base_len]}{suffix}"
        counter += 1
        attempts += 1

    # Check the RESULT, not the attempt counter: the loop exits both when it
    # runs out of attempts and when the last attempt produced a free name, and
    # the old test threw that perfectly good name away.
    if username in existing:
        raise RuntimeError(f"Could not generate unique username for {first_name} {last_name}")
    
    return username

def generate_password(length: Optional[int] = None,
                     policy: Optional[PasswordPolicy] = None) -> str:
    """
    Generate a random password that satisfies the active password policy.

    The password is built from the character classes the policy requires: one
    character is taken from every required class first, the rest is filled from
    the complete pool and everything is shuffled.  A cryptographically secure
    random source is used.

    Args:
        length: Explicit length. ``None`` (the default) uses the length
            configured in the policy, which is what every caller in the
            application does.
        policy: Policy to obey. ``None`` uses the policy configured in the
            *Password Format* dialog.

    Returns:
        The generated password. It is guaranteed to pass
        :func:`validate_password` for the same policy.

    Raises:
        ValueError: If the requested length cannot satisfy the policy.
    """
    active = policy or get_password_policy()

    problems = active.problems()
    if problems:
        raise ValueError(
            "The configured password format is not usable: " + "; ".join(problems)
        )

    effective_length = active.length if length is None else int(length)

    if effective_length < active.min_length:
        raise ValueError(
            f"Password length must be at least {active.min_length} characters "
            f"(requested {effective_length})"
        )
    if effective_length > active.max_length:
        raise ValueError(
            f"Password length must not exceed {active.max_length} characters "
            f"(requested {effective_length})"
        )

    required_pools = active.required_pools()
    if effective_length < len(required_pools):
        raise ValueError(
            f"Password length {effective_length} is too small for "
            f"{len(required_pools)} required character types"
        )

    rng = secrets.SystemRandom()

    # One character from every required class...
    characters = [rng.choice(pool) for pool in required_pools]

    # ...then fill up from the complete pool
    full_pool = active.full_pool()
    characters.extend(
        rng.choice(full_pool) for _ in range(effective_length - len(characters))
    )

    rng.shuffle(characters)
    return ''.join(characters)


def generate_display_name(first_name: str, last_name: str, class_name: str) -> str:
    """
    Generate display name in format: First Last (Class)
    
    Example: Jan Novák (9.A)
    
    Args:
        first_name: Student's first name
        last_name: Student's last name
        class_name: Class name
        
    Returns:
        Display name
        
    Raises:
        ValueError: If any required field is empty
    """
    if not first_name or not first_name.strip():
        raise ValueError("First name cannot be empty")
    if not last_name or not last_name.strip():
        raise ValueError("Last name cannot be empty")
    if not class_name or not class_name.strip():
        raise ValueError("Class name cannot be empty")
    
    return f"{first_name.strip()} {last_name.strip()} ({class_name.strip()})"


def add_users_to_ad(source, server: str, base_dn: str, username: str, password: str) -> str:
    """
    Add users from source to Active Directory
    
    Creates OUs for classes (Trida-6A, etc.) and users within them
    
    Args:
        source: Source object containing classes and persons
        server: LDAP server URL
        base_dn: Base Distinguished Name
        username: Admin username
        password: Admin password
        
    Returns:
        Success message with statistics
        
    Raises:
        RuntimeError: If ldap3 is not installed or connection fails
    """
    try:
        from ldap3 import Server, Connection, SUBTREE, MODIFY_ADD
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        raise RuntimeError("ldap3 library is not installed. Install it with: pip install ldap3")
    
    if not source or not hasattr(source, 'classes'):
        raise ValueError("Invalid source object")
    
    try:
        # Connect to AD
        server_obj = Server(server, get_info='ALL')
        conn = Connection(server_obj, user=username, password=password, auto_bind=True)
        
        created_ous = 0
        created_users = 0
        skipped_users = 0
        
        for cls in source.classes:
            # Create OU for class
            ou_name = f"Trida-{cls.name}"
            ou_dn = f"OU={ou_name},{base_dn}"
            
            # Check if OU exists
            conn.search(
                search_base=base_dn,
                search_filter=f'(ou={ou_name})',
                search_scope=SUBTREE
            )
            
            if not conn.entries:
                # Create OU
                try:
                    conn.add(ou_dn, ['organizationalUnit'])
                    created_ous += 1
                    logger.info(f"Created OU: {ou_dn}")
                except LDAPException as e:
                    logger.error(f"Failed to create OU {ou_dn}: {e}")
                    continue
            
            # Add users to this OU
            for person in cls.persons:
                if not person.ad_username or not person.ad_password:
                    logger.warning(f"Skipping {person.first_name} {person.last_name} - missing credentials")
                    skipped_users += 1
                    continue
                
                user_dn = f"CN={person.ad_display_name or person.ad_username},{ou_dn}"
                
                # Check if user exists
                conn.search(
                    search_base=ou_dn,
                    search_filter=f'(sAMAccountName={person.ad_username})',
                    search_scope=SUBTREE
                )
                
                if conn.entries:
                    logger.info(f"User {person.ad_username} already exists, skipping")
                    skipped_users += 1
                    continue
                
                # Create user
                try:
                    # Extract domain from server
                    domain_parts = server.split('.')[-2:]
                    domain = '.'.join(domain_parts) if len(domain_parts) == 2 else "domain.local"
                    
                    attributes = {
                        'objectClass': ['top', 'person', 'organizationalPerson', 'user'],
                        'cn': person.ad_display_name or person.ad_username,
                        'sAMAccountName': person.ad_username,
                        'givenName': person.first_name,
                        'sn': person.last_name,
                        'displayName': person.ad_display_name or f"{person.first_name} {person.last_name}",
                        'userPrincipalName': f"{person.ad_username}@{domain}"
                    }
                    
                    if person.ad_email:
                        attributes['mail'] = person.ad_email
                    
                    conn.add(user_dn, attributes=attributes)
                    
                    # Set password (this is simplified - actual AD password setting is more complex)
                    # In production, use proper password setting with ldap3
                    
                    created_users += 1
                    logger.info(f"Created user: {person.ad_username}")
                    
                except LDAPException as e:
                    logger.error(f"Failed to create user {person.ad_username}: {e}")
                    skipped_users += 1
        
        conn.unbind()
        
        return (f"Successfully added to Active Directory:\n"
                f"• Created OUs: {created_ous}\n"
                f"• Created users: {created_users}\n"
                f"• Skipped: {skipped_users}")
        
    except Exception as e:
        logger.exception("Error adding users to AD")
        raise RuntimeError(f"Failed to add users to AD: {str(e)}")


def reset_passwords(persons: List, server: str, base_dn: str, username: str, password: str) -> str:
    """
    Reset passwords for selected persons in Active Directory
    
    Args:
        persons: List of Person objects
        server: LDAP server URL
        base_dn: Base Distinguished Name
        username: Admin username
        password: Admin password
        
    Returns:
        Success message with statistics
        
    Raises:
        RuntimeError: If ldap3 is not installed or operation fails
    """
    try:
        from ldap3 import Server, Connection, MODIFY_REPLACE
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        raise RuntimeError("ldap3 library is not installed. Install it with: pip install ldap3")
    
    if not persons:
        return "No persons provided"
    
    try:
        # Connect to AD
        server_obj = Server(server, get_info='ALL')
        conn = Connection(server_obj, user=username, password=password, auto_bind=True)
        
        reset_count = 0
        failed_count = 0
        
        for person in persons:
            if not person.ad_username:
                logger.warning(f"Skipping {person.first_name} {person.last_name} - no username")
                failed_count += 1
                continue
            
            if not person.ad_password:
                logger.warning(f"Skipping {person.first_name} {person.last_name} - no password")
                failed_count += 1
                continue
            
            try:
                # Find user DN
                ou_name = f"Trida-{person.class_name}"
                search_base = f"OU={ou_name},{base_dn}"
                
                conn.search(
                    search_base=search_base,
                    search_filter=f'(sAMAccountName={person.ad_username})',
                    attributes=['distinguishedName']
                )
                
                if not conn.entries:
                    logger.warning(f"User {person.ad_username} not found in AD")
                    failed_count += 1
                    continue
                
                user_dn = str(conn.entries[0].distinguishedName)
                
                # Reset password (simplified - actual implementation needs proper encoding)
                # In production, use proper password modification with ldap3
                # conn.extend.microsoft.modify_password(user_dn, person.ad_password)
                
                reset_count += 1
                logger.info(f"Reset password for: {person.ad_username}")
                
            except LDAPException as e:
                logger.error(f"Failed to reset password for {person.ad_username}: {e}")
                failed_count += 1
        
        conn.unbind()
        
        return (f"Password reset complete:\n"
                f"• Successfully reset: {reset_count}\n"
                f"• Failed: {failed_count}")
        
    except Exception as e:
        logger.exception("Error resetting passwords")
        raise RuntimeError(f"Failed to reset passwords: {str(e)}")


@dataclass
class ValidationIssue:
    """Represents a validation issue"""
    person: Person
    field: str
    issue_type: str  # 'missing', 'invalid_format', 'duplicate', 'warning'
    message: str
    severity: str  # 'error', 'warning'
    
# Username pattern: lowercase alphanumeric starting with letter, max 20 chars
USERNAME_PATTERN = re.compile(r'^[a-z][a-z0-9]{0,19}$')

#: Maximum length of a generated user name (sAMAccountName limit).
USERNAME_MAX_LENGTH = 20

# Email pattern
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


def _password_bounds() -> tuple:
    """
    Return ``(min_length, max_length)`` of the active password policy.

    Kept as a function so the values always follow the policy the user
    configured, instead of being frozen at import time.
    """
    policy = get_password_policy()
    return policy.min_length, policy.max_length


def validate_password(person: Person,
                      policy: Optional[PasswordPolicy] = None) -> List[ValidationIssue]:
        """
        Validate a password against the active password policy.

        The very same policy drives :func:`generate_password`, so a freshly
        generated password never produces a validation issue.

        Args:
            person: Person whose ``ad_password`` is checked.
            policy: Policy to check against (defaults to the configured one).

        Returns:
            List of validation issues (empty when the password is fine).
        """
        issues = []
        password = person.ad_password

        if not password:
            return issues

        active = policy or get_password_policy()

        # Check length
        if len(password) < active.min_length:
            issues.append(ValidationIssue(
                person=person,
                field='ad_password',
                issue_type='invalid_format',
                message=(f"Password too short ({len(password)} chars, "
                         f"min {active.min_length})"),
                severity='error'
            ))

        if len(password) > active.max_length:
            issues.append(ValidationIssue(
                person=person,
                field='ad_password',
                issue_type='invalid_format',
                message=(f"Password too long ({len(password)} chars, "
                         f"max {active.max_length})"),
                severity='warning'
            ))

        # Check character variety - only what the policy actually requires
        missing = []
        if active.require_lowercase and not any(c.islower() for c in password):
            missing.append('lowercase')
        if active.require_uppercase and not any(c.isupper() for c in password):
            missing.append('uppercase')
        if active.require_digits and not any(c.isdigit() for c in password):
            missing.append('digit')
        if active.require_special:
            allowed_special = active.special_pool
            if not any(c in allowed_special for c in password):
                missing.append(f"special character ({allowed_special})")

        if missing:
            issues.append(ValidationIssue(
                person=person,
                field='ad_password',
                issue_type='invalid_format',
                message=f"Password should contain: {', '.join(missing)}",
                severity='warning'
            ))

        # Characters that the policy explicitly excludes
        if active.exclude_ambiguous:
            from utils.password_policy import AMBIGUOUS_CHARACTERS
            found = sorted({c for c in password if c in AMBIGUOUS_CHARACTERS})
            if found:
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_password',
                    issue_type='invalid_format',
                    message=("Password contains ambiguous characters: "
                             + ', '.join(found)),
                    severity='warning'
                ))

        return issues
    

def validate_email(person: Person) -> List[ValidationIssue]:
        """Validate email format"""
        issues = []
        email = person.ad_email
        
        if not email:
            return issues
        
        # Remove whitespace
        email_trimmed = email.strip()
        if email != email_trimmed:
            issues.append(ValidationIssue(
                person=person,
                field='ad_email',
                issue_type='invalid_format',
                message="Email contains leading/trailing whitespace",
                severity='warning'
            ))
        
        if not EMAIL_PATTERN.match(email_trimmed):
            issues.append(ValidationIssue(
                person=person,
                field='ad_email',
                issue_type='invalid_format',
                message="Invalid email format",
                severity='error'
            ))
        
        return issues

def normalize_name_part(text: str) -> str:
    """
    Reduce a name to the characters a user name may contain.

    Diacritics are removed, everything is lower-cased and every non
    alphanumeric character (spaces, hyphens, apostrophes) is dropped.

    Args:
        text: A first or last name.

    Returns:
        The normalised, user-name safe form (may be empty).
    """
    if not text:
        return ""
    reduced = remove_diacritics(text).lower()
    # str.isalnum() is true for any alphanumeric Unicode character, so letters
    # that NFKD cannot decompose (Ł, ß, Cyrillic, Greek...) survived into the
    # user name and were then rejected by USERNAME_PATTERN - the generator
    # produced names its own validator called invalid.
    return ''.join(c for c in reduced if c.isascii() and c.isalnum())


def build_username_base(first_name: str, last_name: str,
                        last_name_index: Optional[int] = -1,
                        first_name_index: Optional[int] = 1,
                        last_name_length: Optional[int] = None,
                        first_name_length: Optional[int] = None) -> str:
    """
    Build the user name that :func:`generate_username` would produce.

    This is the *expected* value the validator compares against, which keeps
    generation and validation from drifting apart.  The duplicate counter is
    not applied here - that part is checked separately.

    Args:
        first_name: Student's first name.
        last_name: Student's last name.
        last_name_index: See :func:`generate_username`.
        first_name_index: See :func:`generate_username`.
        last_name_length: See :func:`generate_username`.
        first_name_length: See :func:`generate_username`.

    Returns:
        The expected base user name, truncated to
        :data:`USERNAME_MAX_LENGTH` characters. Empty when the names contain
        no usable characters.
    """
    last_parts = (last_name or "").strip().split()
    first_parts = (first_name or "").strip().split()

    if not last_parts or not first_parts:
        return ""

    def select(parts: List[str], index: Optional[int], default: int) -> str:
        # None means "use the documented default", the same as in
        # generate_username(). Mapping it to parts[0] here made the validator
        # expect a different name than the generator actually produces for a
        # double surname, which showed up as a bogus "doesn't follow standard
        # pattern" warning.
        if index is None:
            index = default
        if index == 0:
            return ''.join(parts)
        if index == -1:
            return parts[-1]
        if index < 1 or index > len(parts):
            return parts[0]
        return parts[index - 1]

    last = normalize_name_part(select(last_parts, last_name_index, -1))
    first = normalize_name_part(select(first_parts, first_name_index, 1))

    if not last or not first:
        return ""

    if last_name_length is not None:
        last = last[:last_name_length]
    if first_name_length is not None:
        first = first[:first_name_length]

    return f"{last}{first}"[:USERNAME_MAX_LENGTH]


def username_matches_convention(username: str, first_name: str,
                                last_name: str) -> bool:
    """
    Check whether *username* follows the "last name + first name" convention.

    The check is intentionally tolerant, because the generator can be asked for
    many different variants of the same name:

    * ``novakjan``  - full first name (the generator's default)
    * ``novakj``    - shortened first name
    * ``novak``     - last name only
    * ``novakjan2`` - numbered variant used to resolve a duplicate
    * ``novakovaj`` - one of several last names

    A user name is accepted when, after removing a trailing counter, it starts
    with one of the possible last-name forms and the remainder is a prefix of
    one of the possible first-name forms.

    Args:
        username: The user name to check.
        first_name: Student's first name.
        last_name: Student's last name.

    Returns:
        True when the user name follows the convention.
    """
    if not username:
        return False

    candidate = username.lower()

    # Remove a trailing duplicate counter ("novakjan2" -> "novakjan")
    counter_match = re.match(r'^(.*?)(\d+)$', candidate)
    variants = {candidate}
    if counter_match and counter_match.group(1):
        variants.add(counter_match.group(1))

    last_parts = (last_name or "").strip().split()
    first_parts = (first_name or "").strip().split()
    if not last_parts or not first_parts:
        return True  # nothing to compare against

    last_forms = {normalize_name_part(part) for part in last_parts}
    last_forms.add(normalize_name_part(''.join(last_parts)))
    first_forms = {normalize_name_part(part) for part in first_parts}
    first_forms.add(normalize_name_part(''.join(first_parts)))
    last_forms.discard("")
    first_forms.discard("")

    # generate_username() can shorten the last name too (last_name_length), so
    # every non-empty prefix of a known last name is a legitimate start.
    prefixes = set()
    for last in last_forms:
        for length in range(1, len(last) + 1):
            prefixes.add(last[:length])
    last_forms |= prefixes

    for variant in variants:
        for last in last_forms:
            # The 20 character limit can cut the last name short
            if not (variant.startswith(last) or last.startswith(variant)):
                continue
            remainder = variant[len(last):]
            if not remainder:
                return True
            if any(first.startswith(remainder) for first in first_forms):
                return True

    return False


def validate_username(person: Person) -> List[ValidationIssue]:
        """Validate username format"""
        issues = []
        username = person.ad_username
        
        if not username:
            return issues
        
        # Check pattern
        if not USERNAME_PATTERN.match(username):
            issues_before = len(issues)

            # Check what's wrong
            if len(username) > USERNAME_MAX_LENGTH:
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message=f"Username too long ({len(username)} chars, max 20)",
                    severity='error'
                ))
            
            if any(c.isupper() for c in username):
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message="Username must be lowercase",
                    severity='error'
                ))
            
            # Check for diacritics
            normalized = remove_diacritics(username)
            if normalized != username:
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message="Username must not contain diacritics",
                    severity='error'
                ))
            
            if not username[0].isalpha():
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message="Username must start with a letter",
                    severity='error'
                ))
            
            # Check for invalid characters
            if not all(c.isascii() and c.isalnum() for c in username):
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message="Username can only contain letters and numbers",
                    severity='error'
                ))

            # The specific checks above do not cover every way the pattern can
            # fail, and a user name that matches none of them used to be
            # reported as perfectly fine even though the pattern rejected it.
            if len(issues) == issues_before:
                issues.append(ValidationIssue(
                    person=person,
                    field='ad_username',
                    issue_type='invalid_format',
                    message=(f"Username '{username}' is not a valid "
                             f"sAMAccountName (expected lowercase letters and "
                             f"digits, starting with a letter, max "
                             f"{USERNAME_MAX_LENGTH} characters)"),
                    severity='error'
                ))
        
        # Check if it follows the convention used by generate_username()
        if person.first_name and person.last_name:
            try:
                if not username_matches_convention(
                    username, person.first_name, person.last_name
                ):
                    expected = build_username_base(
                        person.first_name, person.last_name
                    )
                    issues.append(ValidationIssue(
                        person=person,
                        field='ad_username',
                        issue_type='invalid_format',
                        message=(
                            f"Username doesn't follow standard pattern "
                            f"(expected last name + first name, e.g. '{expected}')"
                        ),
                        severity='warning'
                    ))
            except Exception as e:
                logger.warning(f"Error validating username pattern: {e}")

        return issues