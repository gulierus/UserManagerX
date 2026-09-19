"""
Microsoft 365 background tasks
==============================

Every Graph request is a network round trip, and a school tenant has hundreds
of groups.  Doing any of it on the GUI thread would freeze the window exactly
the way the enrollment-year lookup used to (point 13) - and, worse, a crash
there is the failure mode of point 25.

Each task here therefore does its work in an :class:`AbstractProgressTask`
worker thread, behind the same :class:`ProgressDialog` the EduPage import uses,
and hands back plain data for the GUI thread to display.

Threading rule
--------------
An :class:`~services.m365_client.M365Client` belongs to the thread that
connected it, because its event loop and its connection pool live there.  Every
task below therefore **creates its own client inside** :meth:`execute`, which
runs in the worker thread.  The connected client is handed to the caller
afterwards only so the next task can be given the *credentials* again - not so
it can be used from the GUI thread.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from models_m365 import M365Group
from services.m365_auth import M365Credentials
from services.m365_client import M365Client, M365Error
from utils.progress_tasks import AbstractProgressTask, LogLevel

logger = logging.getLogger(__name__)


class M365TaskMixin:
    """
    The parts every Microsoft 365 task shares.

    Kept separate from the task classes so the connection logic is written
    once; a task that needs a connection calls :meth:`open_client` first.
    """

    def device_code_prompt(self, verification_uri, user_code, expires_on):
        """
        Show the device code in the progress dialog's log.

        Called by ``azure-identity`` **from the worker thread**, so it must not
        touch a widget.  ``emit_log`` is a Qt signal and is delivered to the
        GUI thread by Qt itself - which is exactly the rule point 25 was about.
        """
        self.emit_log(
            f"Open {verification_uri} and enter the code:  {user_code}",
            LogLevel.WARNING,
        )
        self.emit_progress(-1, f"Waiting for sign-in — code {user_code}")

    def open_client(self, credentials: M365Credentials,
                    device_code_callback: Optional[Callable] = None
                    ) -> M365Client:
        """
        Sign in, reporting each step to the progress dialog.

        Args:
            credentials: How to sign in.
            device_code_callback: Shows the device code, when that method is
                used.

        Returns:
            The connected client.  It belongs to *this* thread.

        Raises:
            M365Error: With an explanation of what to do next.
        """
        self.emit_progress(-1, "Signing in to Microsoft 365...")
        self.emit_log(f"Signing in: {credentials.describe()}", LogLevel.INFO)

        for warning in credentials.warnings():
            self.emit_log(warning, LogLevel.WARNING)

        # Without a callback the SDK prints the code to a console nobody is
        # looking at, and the sign-in appears to hang.
        client = M365Client(credentials,
                            device_code_callback or self.device_code_prompt)
        client.connect()

        if client.signed_in_as:
            self.emit_log(f"Signed in as {client.signed_in_as}", LogLevel.SUCCESS)
        else:
            self.emit_log("Signed in as the application", LogLevel.SUCCESS)
        if client.tenant_domain:
            self.emit_log(f"Tenant: {client.tenant_domain}", LogLevel.INFO)

        return client


class M365ConnectTask(AbstractProgressTask, M365TaskMixin):
    """
    Sign in and stop.

    Used by the connection panels, where the user presses "Connect" and wants
    to know whether the credentials work before doing anything else.
    """

    def __init__(self, credentials: M365Credentials,
                 device_code_callback: Optional[Callable] = None,
                 task_name: str = "Microsoft 365: Connect"):
        super().__init__(task_name, is_deterministic=False, can_pause=False)
        self.credentials = credentials
        self._device_code_callback = device_code_callback

        #: Filled in by :meth:`execute`; ``None`` when the sign-in failed.
        self.client: Optional[M365Client] = None
        self.tenant_id: str = ""
        self.tenant_domain: str = ""
        self.signed_in_as: str = ""

    def execute(self) -> str:
        """Sign in and record who we are."""
        self.client = self.open_client(self.credentials,
                                       self._device_code_callback)
        self.tenant_id = self.client.tenant_id
        self.tenant_domain = self.client.tenant_domain
        self.signed_in_as = self.client.signed_in_as

        who = self.signed_in_as or "the application"
        where = self.tenant_domain or self.tenant_id or "Microsoft 365"
        return f"Connected to {where} as {who}"

    def cleanup(self) -> None:
        """
        Release the session when the task was cancelled or failed.

        A successful run hands the client to the caller, which becomes
        responsible for closing it; closing it here would hand back a dead
        session.
        """
        if self.is_cancelled() and self.client is not None:
            self.client.close()
            self.client = None


class M365LoadGroupsTask(AbstractProgressTask, M365TaskMixin):
    """
    Sign in and read every group and team, with their owners and members.

    The members are what step 1 of the wizard shows on the right, so they are
    read here rather than one group at a time while the user clicks around -
    a click that waits for the network feels broken.

    The trade-off is the opposite one for a very large tenant, which is why the
    task is cancellable and reports its progress per group.
    """

    def __init__(self, credentials: M365Credentials,
                 device_code_callback: Optional[Callable] = None,
                 load_people: bool = True,
                 task_name: str = "Microsoft 365: Load groups and teams"):
        """
        Args:
            credentials: How to sign in.
            device_code_callback: Shows the device code, when used.
            load_people: Whether to read the owners and members of every
                group.  Off makes the load much faster and leaves
                ``members_loaded`` False, so the caller knows the lists are not
                "empty" but "unread".
            task_name: Shown in the progress dialog.
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.credentials = credentials
        self._device_code_callback = device_code_callback
        self.load_people = load_people

        #: Filled in by :meth:`execute`.
        self.groups: List[M365Group] = []
        self.client: Optional[M365Client] = None
        self.origin_info: dict = {}
        #: Groups whose people could not be read, by name.
        self.unreadable: List[str] = []

    def execute(self) -> str:
        """Sign in, list the groups, then fill in their people."""
        self.client = self.open_client(self.credentials,
                                       self._device_code_callback)
        self.origin_info = self.client.origin_info()
        self.check_cancelled()

        self.emit_progress(5, "Reading the groups and teams...")
        self.groups = self.client.list_groups(progress=self._on_groups_page)
        self.check_cancelled()

        teams = sum(1 for group in self.groups if group.has_team)
        self.emit_log(
            f"Found {len(self.groups)} group(s), {teams} of them with a team",
            LogLevel.SUCCESS,
        )

        if not self.load_people:
            self.emit_progress(100, "Done")
            return f"Loaded {len(self.groups)} group(s)"

        self._load_people()

        people = sum(group.member_count for group in self.groups)
        summary = (f"Loaded {len(self.groups)} group(s) and "
                   f"{people} membership(s)")
        if self.unreadable:
            summary += f"; {len(self.unreadable)} group(s) could not be read"
        return summary

    def _on_groups_page(self, total: int) -> None:
        """Report progress while the group list is being paged through."""
        self.emit_progress(-1, f"Reading the groups and teams... ({total})")

    def _load_people(self) -> None:
        """Read the owners and members of every group, one by one."""
        total = len(self.groups) or 1
        for index, group in enumerate(self.groups, start=1):
            self.check_cancelled()
            percent = 10 + int(85 * index / total)
            self.emit_progress(
                percent, f"Reading {group.display_name} ({index}/{total})...")
            try:
                self.client.load_group_people(group)
            except M365Error as exc:
                # One group the account cannot see must not lose the other
                # 299; the wizard shows it with an unread member list.
                logger.warning("Could not read %r: %s", group.display_name, exc)
                self.emit_log(f"{group.display_name}: {exc}", LogLevel.WARNING)
                self.unreadable.append(group.display_name)

        self.emit_progress(100, "Done")

    def cleanup(self) -> None:
        """Release the session when the run did not finish."""
        if self.is_cancelled() and self.client is not None:
            self.client.close()
            self.client = None


class M365RefreshGroupTask(AbstractProgressTask, M365TaskMixin):
    """
    Re-read the owners and members of a handful of groups.

    Used when the user asks for a group's details after choosing not to load
    everybody up front, and after a synchronisation has changed a group.
    """

    def __init__(self, credentials: M365Credentials, groups: List[M365Group],
                 device_code_callback: Optional[Callable] = None,
                 task_name: str = "Microsoft 365: Read group members"):
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.credentials = credentials
        self.groups = list(groups)
        self._device_code_callback = device_code_callback

        self.client: Optional[M365Client] = None
        self.unreadable: List[str] = []

    def execute(self) -> str:
        """Fill in the people of each requested group."""
        self.client = self.open_client(self.credentials,
                                       self._device_code_callback)

        total = len(self.groups) or 1
        for index, group in enumerate(self.groups, start=1):
            self.check_cancelled()
            self.emit_progress(int(100 * index / total),
                               f"Reading {group.display_name}...")
            try:
                self.client.load_group_people(group)
            except M365Error as exc:
                logger.warning("Could not read %r: %s", group.display_name, exc)
                self.emit_log(f"{group.display_name}: {exc}", LogLevel.WARNING)
                self.unreadable.append(group.display_name)

        read = len(self.groups) - len(self.unreadable)
        return f"Read the members of {read} group(s)"

    def cleanup(self) -> None:
        if self.is_cancelled() and self.client is not None:
            self.client.close()
            self.client = None
