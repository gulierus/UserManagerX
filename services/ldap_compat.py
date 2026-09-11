"""
LDAP compatibility layer
========================

``ldap3`` is only needed for the Active Directory features.  Importing it at
module level in every service made it a hard dependency of the whole
application: without the package installed, ``main.py`` aborted with

    ModuleNotFoundError: No module named 'ldap3'

before a single window appeared, even for users who only export PDFs or work
with encrypted files.

This module re-exports the handful of ldap3 constants the services need and
provides safe fallbacks when the package is missing.  The AD operations
themselves check :data:`LDAP3_AVAILABLE` (or simply fail with a clear message
from :func:`require_ldap3`) instead of crashing at import time.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from ldap3 import (
        MODIFY_ADD, MODIFY_DELETE, MODIFY_REPLACE, NTLM, SUBTREE, BASE, ALL,
    )
    LDAP3_AVAILABLE = True
    IMPORT_ERROR = None
except ImportError as exc:                           # pragma: no cover - env dependent
    # The values mirror the ldap3 constants so code that only *builds* a
    # modification list keeps working; anything that talks to a server calls
    # require_ldap3() first and fails with a helpful message.
    MODIFY_ADD = 'MODIFY_ADD'
    MODIFY_DELETE = 'MODIFY_DELETE'
    MODIFY_REPLACE = 'MODIFY_REPLACE'
    NTLM = 'NTLM'
    SUBTREE = 'SUBTREE'
    BASE = 'BASE'
    ALL = 'ALL'
    LDAP3_AVAILABLE = False
    IMPORT_ERROR = exc
    logger.warning(
        "ldap3 is not installed - Active Directory features are disabled (%s)", exc
    )


def require_ldap3() -> None:
    """
    Raise a helpful error when an AD operation is attempted without ldap3.

    Raises:
        RuntimeError: If the ldap3 package is not installed.
    """
    if not LDAP3_AVAILABLE:
        raise RuntimeError(
            "The ldap3 library is required for Active Directory operations. "
            "Install it with: pip install ldap3"
        )
