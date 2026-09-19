"""
Microsoft Graph compatibility layer
===================================

``msgraph-sdk`` and ``azure-identity`` are only needed for the Microsoft 365
features.  Importing either at module level in the services that use them would
make them a hard dependency of the whole application: without the packages
installed, ``main.py`` would abort with

    ModuleNotFoundError: No module named 'msgraph'

before a single window appeared, for users who only export PDFs or work with
Active Directory.  That exact failure already happened once with ``ldap3``
(version 24, point E03), which is why :mod:`services.ldap_compat` exists; this
module is its counterpart for Microsoft 365.

Everything here is import-safe.  Code that only *builds* a request keeps
working without the packages; anything that actually talks to Microsoft calls
:func:`require_graph` first and fails with a message a person can act on.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Pip requirement lines, quoted in the error messages so the user can copy
#: them straight into a terminal.
GRAPH_REQUIREMENTS = "msgraph-sdk azure-identity"

try:
    from azure.identity.aio import (           # type: ignore
        ClientSecretCredential,
        DeviceCodeCredential,
        UsernamePasswordCredential,
    )
    AZURE_IDENTITY_AVAILABLE = True
    AZURE_IDENTITY_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:                        # pragma: no cover - env dependent
    ClientSecretCredential = None               # type: ignore
    DeviceCodeCredential = None                 # type: ignore
    UsernamePasswordCredential = None           # type: ignore
    AZURE_IDENTITY_AVAILABLE = False
    AZURE_IDENTITY_IMPORT_ERROR = exc

try:
    from msgraph import GraphServiceClient      # type: ignore
    MSGRAPH_AVAILABLE = True
    MSGRAPH_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:                        # pragma: no cover - env dependent
    GraphServiceClient = None                   # type: ignore
    MSGRAPH_AVAILABLE = False
    MSGRAPH_IMPORT_ERROR = exc

#: True only when *both* halves are importable - a credential without a client
#: (or the other way round) cannot do anything useful.
GRAPH_AVAILABLE = AZURE_IDENTITY_AVAILABLE and MSGRAPH_AVAILABLE

#: The first import error, for the message shown to the user.
IMPORT_ERROR = MSGRAPH_IMPORT_ERROR or AZURE_IDENTITY_IMPORT_ERROR

if not GRAPH_AVAILABLE:                         # pragma: no cover - env dependent
    logger.warning(
        "The Microsoft Graph packages are not installed - Microsoft 365 "
        "features are disabled (%s)", IMPORT_ERROR
    )


def require_graph() -> None:
    """
    Raise a helpful error when a Microsoft 365 operation is attempted without
    the packages.

    Raises:
        RuntimeError: If ``msgraph-sdk`` or ``azure-identity`` is missing.
    """
    if GRAPH_AVAILABLE:
        return

    missing = []
    if not MSGRAPH_AVAILABLE:
        missing.append("msgraph-sdk")
    if not AZURE_IDENTITY_AVAILABLE:
        missing.append("azure-identity")

    raise RuntimeError(
        f"Microsoft 365 operations need {' and '.join(missing)}. "
        f"Install them with: pip install {GRAPH_REQUIREMENTS}"
    )


def graph_unavailable_message() -> str:
    """
    One sentence explaining why the Microsoft 365 features are switched off.

    Returns an empty string when the packages *are* available, so a caller can
    use the result directly as "is there a problem to show".
    """
    if GRAPH_AVAILABLE:
        return ""
    return (
        "The Microsoft 365 features need two extra packages that are not "
        f"installed. Install them with:\n\n    pip install {GRAPH_REQUIREMENTS}\n\n"
        f"({IMPORT_ERROR})"
    )
