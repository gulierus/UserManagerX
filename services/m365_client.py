"""
Talking to Microsoft 365
========================

A thin, synchronous wrapper around the asynchronous Microsoft Graph SDK.

Why synchronous on the outside
------------------------------
``msgraph-sdk`` is built on ``httpx`` and every call is a coroutine.  The rest
of this application is not asynchronous, and it must not become so: the tasks
that use this client already run inside an :class:`AbstractProgressTask` worker
thread, which is what keeps the window responsive.  Rather than spreading
``async`` through the task classes, this client owns **one event loop** and
runs each call on it.

Threading rule
--------------
**An ``M365Client`` belongs to the thread that connected it.**  The event loop,
the HTTP connection pool and the token cache all live there.  In practice that
means: create and connect the client inside the worker thread of the task that
uses it, and close it there too.  Handing a connected client to another thread
is not supported and will misbehave in ways that are hard to see - the same
rule, and the same reason, as the Qt widgets in point 25.

What this module does not do
----------------------------
It does not know about :class:`models.Person` or :class:`models.Class`.  It
speaks Microsoft 365 - groups, teams and directory accounts - and returns the
objects from :mod:`models_m365`.  Mapping those onto the application's own
model is the job of the tasks that call it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from models_m365 import M365Group, M365Member, M365Team
from services.graph_compat import require_graph
from services.m365_auth import (
    AuthMethod, M365Credentials, build_credential, explain_auth_error,
)

logger = logging.getLogger(__name__)

#: The well-known identifier of the password authentication method.  It is the
#: same for every user; Microsoft documents it as a constant.
PASSWORD_METHOD_ID = "28c10230-6103-485e-b985-444c60001490"

#: Fields read for every group.  Asking for exactly what is used keeps the
#: response small on a tenant with hundreds of groups.
GROUP_SELECT = [
    "id", "displayName", "mailNickname", "description", "mail",
    "visibility", "groupTypes", "mailEnabled", "securityEnabled",
    "createdDateTime", "resourceProvisioningOptions",
]

#: Fields read for every account inside a group.
MEMBER_SELECT = [
    "id", "displayName", "userPrincipalName", "mail", "givenName",
    "surname", "accountEnabled",
]

#: How many objects one page asks for.  999 is Graph's maximum for these
#: collections; fewer requests means fewer round trips on a slow line.
PAGE_SIZE = 999


class M365Error(RuntimeError):
    """
    A Microsoft 365 operation failed, with a message meant for the user.

    Attributes:
        original: The exception Graph raised, for the log.
    """

    def __init__(self, message: str, original: Optional[Exception] = None):
        super().__init__(message)
        self.original = original


class M365Client:
    """
    A connected Microsoft 365 session.

    Use it as a context manager, or call :meth:`close` when finished - the
    credential and the HTTP pool both need releasing.
    """

    def __init__(self, credentials: M365Credentials,
                 device_code_callback: Optional[Callable] = None):
        """
        Args:
            credentials: How to sign in.
            device_code_callback: Called with the device-code prompt so the
                application can show it; see :func:`services.m365_auth.build_credential`.
        """
        self.credentials = credentials
        self._device_code_callback = device_code_callback

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._credential = None
        self._client = None

        #: Filled in by :meth:`connect`.
        self.tenant_id: str = ""
        self.tenant_domain: str = ""
        self.signed_in_as: str = ""
        self.is_connected: bool = False

    # -- lifetime ---------------------------------------------------------

    def __enter__(self) -> 'M365Client':
        self.connect()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """
        Return this client's event loop, creating it on first use.

        A private loop rather than ``asyncio.run`` per call: the SDK keeps a
        connection pool and a token cache bound to the loop that created them,
        and a fresh loop per call would throw both away every time.
        """
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coroutine):
        """Run one coroutine on this client's loop and return its result."""
        return self._ensure_loop().run_until_complete(coroutine)

    def close(self) -> None:
        """
        Release the session.

        Safe to call more than once, and safe to call on a client that never
        connected - closing is part of the failure path too.
        """
        try:
            if self._credential is not None and hasattr(self._credential, "close"):
                result = self._credential.close()
                if asyncio.iscoroutine(result):
                    self._run(result)
        except Exception:
            logger.debug("Closing the Microsoft 365 credential failed",
                         exc_info=True)

        try:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
        except Exception:
            logger.debug("Closing the Microsoft 365 event loop failed",
                         exc_info=True)

        self._credential = None
        self._client = None
        self._loop = None
        self.is_connected = False

    # -- connecting -------------------------------------------------------

    def connect(self) -> None:
        """
        Sign in and verify that the token actually works.

        Acquiring a token is not proof of anything useful: a token with no
        consented permissions looks identical until the first real request.
        This therefore makes one - ``/me`` for a user, ``/organization`` for
        the application - so a permission problem is reported at the
        connection panel rather than in the middle of a synchronisation.

        Raises:
            M365Error: With an explanation of what to do next.
        """
        require_graph()

        problems = self.credentials.problems()
        if problems:
            raise M365Error(" ".join(problems))

        from msgraph import GraphServiceClient

        try:
            self._credential = build_credential(self.credentials,
                                                self._device_code_callback)
            self._client = GraphServiceClient(
                credentials=self._credential, scopes=self.credentials.scopes)
        except Exception as exc:
            logger.exception("Could not build the Microsoft 365 client")
            raise M365Error(explain_auth_error(exc), exc) from exc

        try:
            if self.credentials.method.is_delegated:
                self._run(self._verify_delegated())
            else:
                self._run(self._verify_app_only())
        except M365Error:
            self.close()
            raise
        except Exception as exc:
            logger.exception("Microsoft 365 sign-in failed")
            self.close()
            raise M365Error(explain_auth_error(exc), exc) from exc

        self.is_connected = True
        logger.info("Connected to Microsoft 365 (%s)",
                    self.credentials.describe())

    async def _verify_delegated(self) -> None:
        """Read the signed-in account, proving the token represents a user."""
        me = await self._client.me.get()
        if me is None:
            raise M365Error(
                "Signed in, but Microsoft 365 returned no account for the "
                "token. Check that the application has the User.Read "
                "permission."
            )
        self.signed_in_as = (getattr(me, "user_principal_name", "") or
                             getattr(me, "display_name", "") or "")
        if "@" in self.signed_in_as:
            self.tenant_domain = self.signed_in_as.split("@", 1)[1]
        await self._read_organisation()

    async def _verify_app_only(self) -> None:
        """Read the organisation, proving the application's token works."""
        await self._read_organisation()
        if not self.tenant_id:
            raise M365Error(
                "Signed in, but the tenant could not be read. The application "
                "probably has no consented permissions yet - an administrator "
                "has to grant admin consent in Entra ID."
            )

    async def _read_organisation(self) -> None:
        """Record the tenant id and its primary domain, if they can be read."""
        try:
            organisation = await self._client.organization.get()
        except Exception:
            logger.debug("Could not read the organisation", exc_info=True)
            return

        entries = getattr(organisation, "value", None) or []
        if not entries:
            return

        first = entries[0]
        self.tenant_id = str(getattr(first, "id", "") or "")
        for domain in getattr(first, "verified_domains", None) or []:
            if getattr(domain, "is_default", False):
                self.tenant_domain = str(getattr(domain, "name", "") or "")
                break

    def origin_info(self) -> Dict[str, Any]:
        """
        What to record on a source loaded through this session.

        Never includes a secret or a password: a source is serialised into
        exports and shown in the Source Manager.
        """
        info = dict(self.credentials.redacted())
        info.update({
            'tenant_id': self.tenant_id or self.credentials.effective_tenant_id,
            'tenant_domain': self.tenant_domain,
            'signed_in_as': self.signed_in_as,
        })
        return info

    # -- reading ----------------------------------------------------------

    def _require_connection(self) -> None:
        if not self.is_connected or self._client is None:
            raise M365Error("Not connected to Microsoft 365.")

    def list_groups(self, progress: Optional[Callable[[int], None]] = None
                    ) -> List[M365Group]:
        """
        Read every group and team in the tenant.

        Args:
            progress: Called with the running total after each page, so a
                progress dialog can show something while a large tenant is
                paged through.

        Returns:
            The groups, ordered by display name.  A group that has a team is
            returned as an :class:`~models_m365.M365Team`.
        """
        self._require_connection()
        return self._run(self._list_groups(progress))

    async def _list_groups(self, progress) -> List[M365Group]:
        from msgraph.generated.groups.groups_request_builder import (
            GroupsRequestBuilder,
        )

        configuration = GroupsRequestBuilder.GroupsRequestBuilderGetRequestConfiguration(
            query_parameters=GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(
                top=PAGE_SIZE,
                select=GROUP_SELECT,
                orderby=["displayName"],
            )
        )

        groups: List[M365Group] = []
        try:
            page = await self._client.groups.get(request_configuration=configuration)
        except Exception as exc:
            raise M365Error(
                f"Could not read the groups. {explain_auth_error(exc)}", exc
            ) from exc

        while page is not None:
            for entry in getattr(page, "value", None) or []:
                groups.append(_group_from_entry(entry))
            if progress is not None:
                progress(len(groups))

            next_link = getattr(page, "odata_next_link", None)
            if not next_link:
                break
            try:
                page = await self._client.groups.with_url(next_link).get()
            except Exception as exc:
                raise M365Error(
                    f"Could not read the next page of groups. "
                    f"{explain_auth_error(exc)}", exc
                ) from exc

        return groups

    def load_group_people(self, group: M365Group) -> M365Group:
        """
        Fill in a group's owners and members.

        Done on demand rather than for every group at once: a tenant with 300
        groups would otherwise need 600 extra requests before the user has even
        looked at one.

        Args:
            group: The group to fill in.  Modified in place.

        Returns:
            The same group, so the call can be chained.
        """
        self._require_connection()
        self._run(self._load_group_people(group))
        return group

    async def _load_group_people(self, group: M365Group) -> None:
        group.owners = await self._read_people(
            self._client.groups.by_group_id(group.object_id).owners, group, "owners")
        for owner in group.owners:
            owner.is_owner = True
        group.members = await self._read_people(
            self._client.groups.by_group_id(group.object_id).members, group, "members")
        group.members_loaded = True

    async def _read_people(self, builder, group: M365Group,
                           what: str) -> List[M365Member]:
        """Page through one people collection of a group."""
        people: List[M365Member] = []
        try:
            page = await builder.get()
        except Exception as exc:
            # One unreadable group must not stop the whole wizard: report it
            # and carry on with an empty list.
            logger.warning("Could not read the %s of %r: %s", what,
                           group.display_name, exc)
            return people

        while page is not None:
            for entry in getattr(page, "value", None) or []:
                member = _member_from_entry(entry)
                if member is not None:
                    people.append(member)
            next_link = getattr(page, "odata_next_link", None)
            if not next_link:
                break
            try:
                page = await builder.with_url(next_link).get()
            except Exception as exc:
                logger.warning("Could not read the next page of %s of %r: %s",
                               what, group.display_name, exc)
                break
        return people

    def find_user(self, user_principal_name: str) -> Optional[M365Member]:
        """
        Look one account up by its sign-in name.

        Returns:
            The account, or ``None`` when there is none.  A lookup that fails
            for any other reason raises, because "not found" and "could not
            ask" must not look the same to the caller.
        """
        self._require_connection()
        upn = (user_principal_name or "").strip()
        if not upn:
            return None
        return self._run(self._find_user(upn))

    async def _find_user(self, upn: str) -> Optional[M365Member]:
        from msgraph.generated.users.users_request_builder import (
            UsersRequestBuilder,
        )

        configuration = UsersRequestBuilder.UsersRequestBuilderGetRequestConfiguration(
            query_parameters=UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
                filter=f"userPrincipalName eq '{_escape_filter(upn)}'",
                select=MEMBER_SELECT,
                top=2,
            )
        )
        try:
            page = await self._client.users.get(request_configuration=configuration)
        except Exception as exc:
            raise M365Error(
                f"Could not look up {upn}. {explain_auth_error(exc)}", exc
            ) from exc

        entries = getattr(page, "value", None) or []
        if not entries:
            return None
        return _member_from_entry(entries[0])

    # -- writing ----------------------------------------------------------

    def find_group(self, display_name: str) -> Optional[M365Group]:
        """
        Look one group up by its display name.

        Returns:
            The group, or ``None``.  When several groups share the name the
            first is returned and a warning is logged - Microsoft 365 does not
            enforce unique display names, so this is a real possibility.
        """
        self._require_connection()
        name = (display_name or "").strip()
        if not name:
            return None
        return self._run(self._find_group(name))

    async def _find_group(self, name: str) -> Optional[M365Group]:
        from msgraph.generated.groups.groups_request_builder import (
            GroupsRequestBuilder,
        )

        configuration = GroupsRequestBuilder.GroupsRequestBuilderGetRequestConfiguration(
            query_parameters=GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(
                filter=f"displayName eq '{_escape_filter(name)}'",
                select=GROUP_SELECT,
                top=2,
            )
        )
        try:
            page = await self._client.groups.get(request_configuration=configuration)
        except Exception as exc:
            raise M365Error(
                f"Could not look up the group {name!r}. "
                f"{explain_auth_error(exc)}", exc
            ) from exc

        entries = getattr(page, "value", None) or []
        if not entries:
            return None
        if len(entries) > 1:
            logger.warning("Several groups are called %r - using the first one",
                           name)
        return _group_from_entry(entries[0])

    def create_group(self, display_name: str, mail_nickname: str,
                     description: str = "",
                     owner_ids: Optional[List[str]] = None,
                     unified: bool = True,
                     visibility: str = "Private") -> M365Group:
        """
        Create a group.

        Args:
            display_name: The name shown in the portal.
            mail_nickname: The alias.  Microsoft requires it to be free of
                spaces and of most punctuation; :func:`mail_nickname_for`
                produces an acceptable one.
            description: Free text.
            owner_ids: Object ids of the accounts that should own the group.
                **Strongly recommended when signed in app-only**: there is no
                signed-in user to become the owner, so without this the group
                is created ownerless and nobody can manage it in the portal.
            unified: True for a Microsoft 365 group (the kind a team can be
                attached to), False for a plain security group.
            visibility: ``Private``, ``Public`` or ``HiddenMembership``.

        Returns:
            The group as Microsoft created it, including its new object id.
        """
        self._require_connection()
        return self._run(self._create_group(
            display_name, mail_nickname, description, owner_ids or [],
            unified, visibility))

    async def _create_group(self, display_name, mail_nickname, description,
                            owner_ids, unified, visibility) -> M365Group:
        from msgraph.generated.models.group import Group

        body = Group(
            display_name=display_name,
            mail_nickname=mail_nickname,
            description=description or None,
            mail_enabled=bool(unified),
            security_enabled=not bool(unified),
            group_types=["Unified"] if unified else [],
        )
        if unified:
            body.visibility = visibility

        if owner_ids:
            # Owners are attached in the creation request itself; Microsoft
            # accepts at most 20 here, which is far more than a class needs.
            body.additional_data = {
                "owners@odata.bind": [
                    f"https://graph.microsoft.com/v1.0/users/{owner_id}"
                    for owner_id in owner_ids[:20]
                ]
            }

        try:
            created = await self._client.groups.post(body)
        except Exception as exc:
            raise M365Error(
                f"Could not create the group {display_name!r}. "
                f"{explain_auth_error(exc)}", exc
            ) from exc

        if created is None:
            raise M365Error(
                f"Microsoft 365 accepted the request to create "
                f"{display_name!r} but returned no group."
            )
        return _group_from_entry(created)

    def add_group_members(self, group_id: str,
                          user_ids: List[str]) -> List[str]:
        """
        Add accounts to a group, one at a time.

        One request per account on purpose: a batch that fails tells you only
        that *something* failed, and a class of thirty would then have to be
        retried whole.

        Returns:
            The problems, one sentence each.  Empty when every account was
            added.  An account that is **already** a member is not a problem
            and is not reported.
        """
        self._require_connection()
        return self._run(self._add_group_members(group_id, user_ids))

    async def _add_group_members(self, group_id: str,
                                 user_ids: List[str]) -> List[str]:
        from msgraph.generated.models.reference_create import ReferenceCreate

        problems: List[str] = []
        for user_id in user_ids:
            if not user_id:
                continue
            body = ReferenceCreate(
                odata_id=f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"
            )
            try:
                await self._client.groups.by_group_id(group_id).members.ref.post(body)
            except Exception as exc:
                text = str(exc)
                # Microsoft answers an existing membership with this; it is the
                # state we wanted, so it is not a failure.
                if "already exist" in text.lower() or "One or more added object references already exist" in text:
                    logger.debug("%s is already a member of %s", user_id, group_id)
                    continue
                logger.warning("Could not add %s to %s: %s", user_id, group_id, exc)
                problems.append(f"{user_id} was not added ({exc})")
        return problems

    def create_team(self, group_id: str) -> bool:
        """
        Attach a Microsoft Team to an existing Microsoft 365 group.

        The group must be a Unified group **and must have at least one
        owner** - Microsoft refuses to build a team on an ownerless group.

        Returns:
            True when the team exists afterwards.

        Raises:
            M365Error: With Microsoft's reason when it refuses.
        """
        self._require_connection()
        return self._run(self._create_team(group_id))

    async def _create_team(self, group_id: str) -> bool:
        from msgraph.generated.models.team import Team

        try:
            await self._client.groups.by_group_id(group_id).team.put(Team())
        except Exception as exc:
            text = str(exc)
            if "already" in text.lower() and "team" in text.lower():
                logger.debug("Group %s already has a team", group_id)
                return True
            raise M365Error(
                f"Could not create the team. {explain_auth_error(exc)}", exc
            ) from exc
        return True

    def update_user(self, user_id: str, changes: Dict[str, Any]) -> None:
        """
        Write attributes onto an existing account.

        Args:
            user_id: The account's object id.
            changes: Graph attribute names to values, for example
                ``{"displayName": "Jan Novák", "givenName": "Jan"}``.

        Raises:
            M365Error: If the write is refused.
        """
        self._require_connection()
        if not changes:
            return
        self._run(self._update_user(user_id, changes))

    async def _update_user(self, user_id: str, changes: Dict[str, Any]) -> None:
        from msgraph.generated.models.user import User

        body = User()
        # The SDK's model uses snake_case attribute names; anything it does not
        # know goes through additional_data, which the serialiser merges in.
        extra: Dict[str, Any] = {}
        for name, value in changes.items():
            attribute = _SNAKE_CASE.get(name)
            if attribute and hasattr(body, attribute):
                setattr(body, attribute, value)
            else:
                extra[name] = value
        if extra:
            body.additional_data = extra

        try:
            await self._client.users.by_user_id(user_id).patch(body)
        except Exception as exc:
            raise M365Error(
                f"Could not update the account. {explain_auth_error(exc)}", exc
            ) from exc

    def set_password(self, user_id: str, password: str,
                     force_change: bool = False) -> None:
        """
        Set an account's password.

        Which API is used depends on how this session signed in, because the
        two are not interchangeable:

        * **Delegated** (device code, or user name and password) uses the
          password profile, which is what a signed-in administrator is allowed
          to write.
        * **App-only** uses the same profile - the authentication-methods
          endpoint under ``/authentication/passwordMethods`` is delegated-only
          and cannot be reached with an application token at all.

        Either way the caller needs a directory role that may reset that
        account's password, and an account holding a *higher* privileged role
        cannot be reset from a lower-privileged caller.

        Args:
            user_id: The account's object id.
            password: The new password.  It must satisfy the tenant's password
                policy or Microsoft refuses it.
            force_change: Whether the user must change it at the next sign-in.

        Raises:
            M365Error: With Microsoft's reason when it refuses.
        """
        self._require_connection()
        if not password:
            raise M365Error("No password was given.")
        self._run(self._set_password(user_id, password, force_change))

    async def _set_password(self, user_id: str, password: str,
                            force_change: bool) -> None:
        from msgraph.generated.models.password_profile import PasswordProfile
        from msgraph.generated.models.user import User

        body = User(password_profile=PasswordProfile(
            password=password,
            force_change_password_next_sign_in=bool(force_change),
        ))
        try:
            await self._client.users.by_user_id(user_id).patch(body)
        except Exception as exc:
            raise M365Error(
                f"Could not set the password. {explain_auth_error(exc)}", exc
            ) from exc

    def create_user(self, display_name: str, user_principal_name: str,
                    mail_nickname: str, password: str,
                    given_name: str = "", surname: str = "",
                    force_change: bool = True,
                    usage_location: str = "") -> M365Member:
        """
        Create an account.

        Args:
            display_name: The name shown in the portal.
            user_principal_name: The sign-in name; its domain must be a
                verified domain of the tenant.
            mail_nickname: The alias.
            password: The initial password, which must satisfy the tenant's
                policy.
            given_name: First name.
            surname: Last name.
            force_change: Whether the password must be changed at first
                sign-in.
            usage_location: Two-letter country code.  Microsoft requires it
                before a licence can be assigned, so it is worth setting now.

        Returns:
            The account as Microsoft created it, including its object id.
        """
        self._require_connection()
        return self._run(self._create_user(
            display_name, user_principal_name, mail_nickname, password,
            given_name, surname, force_change, usage_location))

    async def _create_user(self, display_name, user_principal_name,
                           mail_nickname, password, given_name, surname,
                           force_change, usage_location) -> M365Member:
        from msgraph.generated.models.password_profile import PasswordProfile
        from msgraph.generated.models.user import User

        body = User(
            account_enabled=True,
            display_name=display_name,
            user_principal_name=user_principal_name,
            mail_nickname=mail_nickname,
            given_name=given_name or None,
            surname=surname or None,
            password_profile=PasswordProfile(
                password=password,
                force_change_password_next_sign_in=bool(force_change),
            ),
        )
        if usage_location:
            body.usage_location = usage_location

        try:
            created = await self._client.users.post(body)
        except Exception as exc:
            raise M365Error(
                f"Could not create {user_principal_name}. "
                f"{explain_auth_error(exc)}", exc
            ) from exc

        if created is None:
            raise M365Error(
                f"Microsoft 365 accepted the request to create "
                f"{user_principal_name} but returned no account."
            )
        return _member_from_entry(created)


# ---------------------------------------------------------------------------
# Turning Graph objects into ours
# ---------------------------------------------------------------------------

#: Graph attribute name -> the SDK model's snake_case attribute.
_SNAKE_CASE = {
    "displayName": "display_name",
    "givenName": "given_name",
    "surname": "surname",
    "mail": "mail",
    "mailNickname": "mail_nickname",
    "userPrincipalName": "user_principal_name",
    "accountEnabled": "account_enabled",
    "usageLocation": "usage_location",
    "jobTitle": "job_title",
    "department": "department",
    "officeLocation": "office_location",
}


def _escape_filter(value: str) -> str:
    """
    Escape a value for an OData ``$filter`` string literal.

    A single quote is doubled; that is the whole of OData's escaping rule for
    string literals, and without it a name containing an apostrophe -
    ``O'Brien`` - produces a filter the service rejects.
    """
    return str(value or "").replace("'", "''")


def mail_nickname_for(name: str, fallback: str = "group") -> str:
    """
    Turn a display name into an alias Microsoft will accept.

    Microsoft rejects a mail nickname containing spaces or most punctuation, so
    "Trida 6.A" has to become "trida6a" before it can be used.

    Args:
        name: The display name.
        fallback: Used when nothing usable is left, so the result is never
            empty.

    Returns:
        A lower-case alias of ASCII letters, digits, ``-`` and ``_``.
    """
    from utils.ad_utils import remove_diacritics

    text = remove_diacritics(str(name or "")).lower()
    cleaned = ''.join(character if (character.isalnum() and character.isascii())
                      or character in '-_' else '' for character in text)
    return cleaned or fallback


def _group_from_entry(entry: Any) -> M365Group:
    """Build an :class:`M365Group` (or :class:`M365Team`) from a Graph group."""
    group_types = [str(t) for t in (getattr(entry, "group_types", None) or [])]

    # Microsoft reports an attached team through resourceProvisioningOptions,
    # which the SDK exposes on the model when it was selected.
    provisioning = getattr(entry, "resource_provisioning_options", None) or []
    has_team = any(str(option).lower() == "team" for option in provisioning)

    common = dict(
        object_id=str(getattr(entry, "id", "") or ""),
        display_name=str(getattr(entry, "display_name", "") or ""),
        mail_nickname=str(getattr(entry, "mail_nickname", "") or ""),
        description=str(getattr(entry, "description", "") or ""),
        mail=str(getattr(entry, "mail", "") or ""),
        visibility=str(getattr(entry, "visibility", "") or ""),
        group_types=group_types,
        mail_enabled=bool(getattr(entry, "mail_enabled", False)),
        security_enabled=bool(getattr(entry, "security_enabled", False)),
        created=getattr(entry, "created_date_time", None),
    )

    if has_team:
        return M365Team(**common)

    group = M365Group(**common)
    group.has_team = False
    return group


def _member_from_entry(entry: Any) -> Optional[M365Member]:
    """
    Build an :class:`M365Member` from a Graph directory object.

    Returns ``None`` for an object that is not a user - a group's members may
    include nested groups and service principals, which are not people and must
    not become pupils.
    """
    if entry is None:
        return None

    odata_type = str(getattr(entry, "odata_type", "") or "")
    if odata_type and "user" not in odata_type.lower():
        return None

    # A nested group has no user principal name; that is the reliable signal
    # when the type is not reported.
    upn = str(getattr(entry, "user_principal_name", "") or "")
    if not odata_type and not upn and not getattr(entry, "surname", None):
        return None

    # 'accountEnabled' that was not selected comes back as None, and bool(None)
    # is False - which would report every account the request did not ask about
    # as disabled.  "Not reported" has to mean "unknown", and for an account the
    # directory is actively serving, enabled is the honest reading.
    enabled = getattr(entry, "account_enabled", None)

    return M365Member(
        object_id=str(getattr(entry, "id", "") or ""),
        display_name=str(getattr(entry, "display_name", "") or ""),
        user_principal_name=upn,
        mail=str(getattr(entry, "mail", "") or ""),
        given_name=str(getattr(entry, "given_name", "") or ""),
        surname=str(getattr(entry, "surname", "") or ""),
        account_enabled=True if enabled is None else bool(enabled),
    )
