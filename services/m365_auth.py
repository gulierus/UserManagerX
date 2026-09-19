"""
Signing in to Microsoft 365
===========================

Three ways in, and the differences between them are not cosmetic - they decide
whether multi-factor authentication can work at all, and which Graph calls are
allowed afterwards.  See ``docs/VERSION_26_POINT_21_ANALYSIS.md`` for the full
reasoning; the short version:

``USERNAME_PASSWORD``
    The OAuth 2.0 Resource Owner Password Credentials flow.  The program sends
    the two strings and gets a token back.  **There is no step at which a
    second factor could be requested**, so an account with MFA enforced fails
    with ``AADSTS50076``.  It also does not work for federated or guest
    accounts, and the app registration must allow public client flows.

``DEVICE_CODE``
    The application prints a short code and a URL; the user completes the
    sign-in on any device, **including the second factor**.  This is the only
    way to sign in as a real administrator with MFA without the application
    driving a browser.

``APP_ONLY``
    Client credentials - a client id and a client secret.  There is no user, so
    the question of a second factor does not arise.  An administrator consents
    to the application permissions once.  This is the method for unattended
    work, but note that the password-reset endpoint under
    ``/authentication/passwordMethods`` is delegated-only and cannot be used
    this way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from services.graph_compat import GRAPH_AVAILABLE, require_graph

logger = logging.getLogger(__name__)

#: Delegated scopes.  The same four the C# tool asks for.
DELEGATED_SCOPES: List[str] = [
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
    "UserAuthenticationMethod.ReadWrite.All",
    "Directory.ReadWrite.All",
]

#: App-only uses the ``.default`` scope: the application permissions an
#: administrator has already consented to, whatever they are.
APP_ONLY_SCOPES: List[str] = ["https://graph.microsoft.com/.default"]

#: Microsoft's own public client id, used by the Graph command line tools.  It
#: lets a user sign in without registering an application first, which is what
#: makes the device-code option usable out of the box.  An organisation that
#: registers its own application puts its client id in the field instead.
DEFAULT_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


class AuthMethod(Enum):
    """How the application signs in to Microsoft 365."""

    USERNAME_PASSWORD = "username_password"
    DEVICE_CODE = "device_code"
    APP_ONLY = "app_only"

    @property
    def label(self) -> str:
        """The text shown in the method combo box."""
        return {
            AuthMethod.USERNAME_PASSWORD: "User name and password",
            AuthMethod.DEVICE_CODE: "Device code (supports 2FA)",
            AuthMethod.APP_ONLY: "App-only (client ID and secret)",
        }[self]

    @property
    def description(self) -> str:
        """The sentence shown under the method combo box."""
        return {
            AuthMethod.USERNAME_PASSWORD:
                "Signs in with an account's user name and password. This flow "
                "cannot ask for a second factor, so it fails for any account "
                "with multi-factor authentication enforced - which normally "
                "includes every administrator. Use a service account that is "
                "exempt, or choose another method.",
            AuthMethod.DEVICE_CODE:
                "Shows a short code to type at microsoft.com/devicelogin. The "
                "sign-in, including multi-factor authentication, happens "
                "there - on this computer or on a phone. This application "
                "never sees the password.",
            AuthMethod.APP_ONLY:
                "Signs in as the application itself with a client ID and a "
                "client secret. There is no user and therefore no second "
                "factor. An administrator must consent to the application "
                "permissions once. Note that resetting a password through the "
                "authentication-methods API is not available this way.",
        }[self]

    @property
    def supports_mfa(self) -> bool:
        """
        Whether a second factor can be satisfied with this method.

        ``APP_ONLY`` reports ``True`` in the sense that it is never blocked by
        MFA: there is no user to challenge.
        """
        return self is not AuthMethod.USERNAME_PASSWORD

    @property
    def is_delegated(self) -> bool:
        """True when the token represents a *user* rather than the application."""
        return self is not AuthMethod.APP_ONLY


@dataclass
class M365Credentials:
    """
    Everything needed for one sign-in.

    Attributes:
        method: Which flow to use.
        tenant_id: The directory (tenant) id or a verified domain.
            ``organizations`` is accepted for the two delegated flows and lets
            the user's own tenant be resolved at sign-in.
        client_id: The application (client) id.  Defaults to Microsoft's public
            client for the delegated flows.
        client_secret: Only for :attr:`AuthMethod.APP_ONLY`.
        username: Only for :attr:`AuthMethod.USERNAME_PASSWORD`.
        password: Only for :attr:`AuthMethod.USERNAME_PASSWORD`.
    """

    method: AuthMethod = AuthMethod.APP_ONLY
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""

    @property
    def scopes(self) -> List[str]:
        """The scopes this method asks for."""
        return (APP_ONLY_SCOPES if self.method is AuthMethod.APP_ONLY
                else list(DELEGATED_SCOPES))

    @property
    def effective_client_id(self) -> str:
        """
        The client id to sign in with.

        The delegated flows fall back to Microsoft's public client so the user
        can sign in without registering an application first; app-only has no
        such fallback, because a secret always belongs to a specific
        registration.
        """
        given = (self.client_id or "").strip()
        if given:
            return given
        if self.method is AuthMethod.APP_ONLY:
            return ""
        return DEFAULT_PUBLIC_CLIENT_ID

    @property
    def effective_tenant_id(self) -> str:
        """
        The tenant to sign in against.

        Empty is accepted for the delegated flows and becomes
        ``organizations``, which resolves to whatever tenant the account
        belongs to.  App-only must name its tenant - there is no user to derive
        it from.
        """
        given = (self.tenant_id or "").strip()
        if given:
            return given
        if self.method is AuthMethod.APP_ONLY:
            return ""
        return "organizations"

    def problems(self) -> List[str]:
        """
        Everything that would stop this sign-in, in plain words.

        Returns:
            A list of problems, empty when the credentials are usable.  The
            connection panel shows these *before* attempting to connect, so a
            missing field does not come back as a Microsoft error code.
        """
        problems: List[str] = []

        if self.method is AuthMethod.APP_ONLY:
            if not (self.tenant_id or "").strip():
                problems.append(
                    "App-only sign-in needs the Tenant ID (the directory id "
                    "from Entra ID)."
                )
            if not (self.client_id or "").strip():
                problems.append("App-only sign-in needs the Client ID.")
            if not self.client_secret:
                problems.append("App-only sign-in needs the Client Secret.")

        elif self.method is AuthMethod.USERNAME_PASSWORD:
            if not (self.username or "").strip():
                problems.append("Enter the user name (name@school.cz).")
            if not self.password:
                problems.append("Enter the password.")

        # DEVICE_CODE needs nothing beyond the defaults.
        return problems

    def warnings(self) -> List[str]:
        """
        Things that are allowed but likely to surprise, said up front.

        Returns:
            A list of warnings.  These do not stop the sign-in.
        """
        warnings: List[str] = []
        if self.method is AuthMethod.USERNAME_PASSWORD:
            warnings.append(
                "This sign-in method cannot ask for a second factor. If the "
                "account has multi-factor authentication enforced - which is "
                "normal for administrators - Microsoft will refuse it "
                "(AADSTS50076). Use 'Device code' for such an account."
            )
        if self.method is AuthMethod.APP_ONLY:
            warnings.append(
                "App-only sign-in cannot use the authentication-methods API, "
                "so passwords are set through the user's password profile "
                "instead. That works for pupils but not for accounts holding "
                "an administrator role."
            )
        return warnings

    def describe(self) -> str:
        """One line for a log entry or a status label - never a secret."""
        if self.method is AuthMethod.APP_ONLY:
            return (f"{self.method.label} · tenant "
                    f"{self.effective_tenant_id or '?'} · client "
                    f"{self.effective_client_id or '?'}")
        who = (self.username or "").strip() or "(chosen at sign-in)"
        return f"{self.method.label} · {who}"

    def redacted(self) -> dict:
        """
        A dictionary safe to put in a log or a source's origin information.

        The secret and the password are never included - a source is
        serialised into exports and shown in the Source Manager.
        """
        return {
            'auth_method': self.method.value,
            'tenant_id': self.effective_tenant_id,
            'client_id': self.effective_client_id,
            'username': (self.username or "").strip(),
        }


def build_credential(credentials: M365Credentials,
                     device_code_callback: Optional[Callable] = None):
    """
    Create the ``azure-identity`` credential for a sign-in.

    Args:
        credentials: What the user filled in.
        device_code_callback: Called as ``(verification_uri, user_code,
            expires_on)`` for :attr:`AuthMethod.DEVICE_CODE`, so the
            application can show the code instead of printing it to a console
            nobody is watching.

    Returns:
        A credential object the Graph client accepts.

    Raises:
        RuntimeError: If the Graph packages are not installed.
        ValueError: If the credentials are incomplete - the message names what
            is missing.
    """
    require_graph()

    problems = credentials.problems()
    if problems:
        raise ValueError(" ".join(problems))

    method = credentials.method

    if method is AuthMethod.APP_ONLY:
        # Only this one has an async implementation in azure-identity; the
        # Graph token provider awaits the result when it is awaitable and uses
        # it directly when it is not, so both kinds work.
        from azure.identity.aio import ClientSecretCredential
        return ClientSecretCredential(
            tenant_id=credentials.effective_tenant_id,
            client_id=credentials.effective_client_id,
            client_secret=credentials.client_secret,
        )

    if method is AuthMethod.USERNAME_PASSWORD:
        from azure.identity import UsernamePasswordCredential
        return UsernamePasswordCredential(
            client_id=credentials.effective_client_id,
            username=credentials.username.strip(),
            password=credentials.password,
            tenant_id=credentials.effective_tenant_id,
        )

    from azure.identity import DeviceCodeCredential
    return DeviceCodeCredential(
        client_id=credentials.effective_client_id,
        tenant_id=credentials.effective_tenant_id,
        prompt_callback=device_code_callback,
    )


def explain_auth_error(error: Exception) -> str:
    """
    Turn a sign-in failure into a sentence that says what to do next.

    Microsoft's error codes are precise and unreadable.  The ones below are the
    failures this application can actually provoke, so they are translated;
    anything else is passed through unchanged rather than guessed at.

    Args:
        error: The exception raised while acquiring a token.

    Returns:
        The explanation, always ending with the original message so nothing is
        hidden.
    """
    text = str(error)

    explanations = [
        ("AADSTS50076",
         "This account requires multi-factor authentication, and the "
         "user-name-and-password method cannot ask for it. Choose "
         "'Device code' instead."),
        ("AADSTS50079",
         "This account must register for multi-factor authentication before "
         "it can sign in. Choose 'Device code' and complete the registration."),
        ("AADSTS50126",
         "The user name or the password is wrong."),
        ("AADSTS50034",
         "No such account exists in this tenant. Check the user name and the "
         "Tenant ID."),
        ("AADSTS7000215",
         "The client secret is wrong, or it belongs to a different "
         "application."),
        ("AADSTS700016",
         "No application with this Client ID exists in this tenant."),
        ("AADSTS65001",
         "Nobody has consented to these permissions yet. An administrator has "
         "to grant admin consent to the application in Entra ID."),
        ("AADSTS53003",
         "A Conditional Access policy blocked this sign-in - for example "
         "because it requires a managed device."),
        ("AADSTS90002",
         "No such tenant. Check the Tenant ID."),
        ("Authorization_RequestDenied",
         "The sign-in worked, but this account or application does not have "
         "permission for what was asked. Check the four Graph permissions and "
         "that admin consent was granted."),
        ("unsupported_grant_type",
         "The application registration does not allow public client flows, "
         "which the user-name-and-password and device-code methods need."),
    ]

    for code, explanation in explanations:
        if code in text:
            return f"{explanation}\n\n(Microsoft said: {text})"

    return text
