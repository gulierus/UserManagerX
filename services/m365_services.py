"""
Discovering and synchronising people in Microsoft 365
=====================================================

Version 26, point 21 e.  The Active Directory counterpart is
:mod:`services.ad_services`, and the shape is deliberately the same - discover,
plan, execute - so that the two operations behave alike for the user.

What is *not* the same is the mapping.  Active Directory puts one
organisational unit under the base DN per class and creates the accounts
inside it.  Microsoft 365 has no such tree: a class becomes a **group**, the
accounts live in the tenant's flat user list, and membership of the group is
what says which class somebody is in.  A team is only created when the user
asks for one - the point is explicit that it must not happen automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from models import Class, Person, Source
from models_m365 import M365Group, M365Status
from services.m365_client import M365Client, M365Error, mail_nickname_for
from services.m365_comparison import (
    M365_VALUES_METADATA_KEY, changes_for_graph, compare_person_with_m365,
    store_comparison,
)
from utils.name_templates import (
    TemplateError, class_values, empty_placeholders, person_values, render,
)

logger = logging.getLogger(__name__)

#: The group name a class maps to, unless the user says otherwise.
DEFAULT_GROUP_TEMPLATE = "Trida-{class_name}"

#: How a sign-in name is built, unless the user says otherwise.
DEFAULT_UPN_TEMPLATE = "{last_name|ascii|lower|alnum}{first_name[0]|ascii|lower}@{domain}"

#: How a display name is built, unless the user says otherwise.
DEFAULT_DISPLAY_NAME_TEMPLATE = "{first_name} {last_name} ({class_name})"


@dataclass
class M365SyncConfig:
    """
    Everything the user chose about how synchronisation should behave.

    Attributes:
        group_name_template: How a class becomes a group name.  Placeholders
            come from the class - see
            :data:`utils.name_templates.CLASS_FIELDS`.
        upn_template: How a person becomes a sign-in name.  Placeholders come
            from the person - see :data:`utils.name_templates.PERSON_FIELDS`.
        display_name_template: How a person becomes a display name.
        domain: The tenant domain the sign-in names are built in.
        create_teams: Whether a Microsoft Team is attached to each group.
            **Off by default**, because the point requires that a team is not
            created automatically.
        owner_user_principal_names: Who owns a group this application creates.
            Strongly recommended for app-only sign-in, where there is no
            signed-in user to become the owner and the group would be created
            ownerless.
        usage_location: Two-letter country code stamped on new accounts, so a
            licence can be assigned to them afterwards.
        force_change_password: Whether a new account must change its password
            at first sign-in.
        update_existing: Whether an account that already exists has its fields
            brought into line.
    """

    group_name_template: str = DEFAULT_GROUP_TEMPLATE
    upn_template: str = DEFAULT_UPN_TEMPLATE
    display_name_template: str = DEFAULT_DISPLAY_NAME_TEMPLATE
    domain: str = ""
    create_teams: bool = False
    owner_user_principal_names: List[str] = field(default_factory=list)
    usage_location: str = ""
    force_change_password: bool = True
    update_existing: bool = True

    def problems(self) -> List[str]:
        """Everything that would stop a synchronisation, in plain words."""
        from utils.name_templates import CLASS_FIELDS, PERSON_FIELDS, validate

        problems: List[str] = []
        problems += [f"Group name: {problem}"
                     for problem in validate(self.group_name_template, CLASS_FIELDS)]
        problems += [f"Sign-in name: {problem}"
                     for problem in validate(self.upn_template, PERSON_FIELDS)]
        problems += [f"Display name: {problem}"
                     for problem in validate(self.display_name_template,
                                             PERSON_FIELDS)]

        if "{domain}" in self.upn_template and not (self.domain or "").strip():
            problems.append(
                "The sign-in name uses {domain}, but no tenant domain is known. "
                "Connect first, or type the domain."
            )
        if self.create_teams and not self.owner_user_principal_names:
            # Microsoft refuses to build a team on an ownerless group.
            problems.append(
                "A team cannot be created for a group with no owner. Name at "
                "least one group owner, or switch teams off."
            )
        return problems

    def group_name_for(self, school_class: Class) -> str:
        """
        The group a class maps to.

        Raises:
            TemplateError: If the template cannot be rendered - the caller
                turns that into a message rather than creating a group with a
                nonsensical name.
        """
        return render(self.group_name_template, class_values(school_class))

    def upn_for(self, person: Person) -> str:
        """The sign-in name a person should have."""
        return render(self.upn_template, person_values(person, self.domain))

    def display_name_for(self, person: Person) -> str:
        """The display name a person should have."""
        return render(self.display_name_template,
                      person_values(person, self.domain))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass
class M365DiscoveryResult:
    """What discovery found out about one person."""

    person: Person
    found: bool = False
    object_id: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class M365DiscoveryService:
    """Finds the Microsoft 365 account of each person and compares it."""

    def __init__(self, client: M365Client, config: M365SyncConfig):
        """
        Args:
            client: A connected client, belonging to this thread.
            config: Used to work out the sign-in name a person *should* have,
                which is how somebody who has never been synchronised is found.
        """
        self.client = client
        self.config = config

    def discover_persons(self, persons: List[Person],
                         progress=None) -> List[M365DiscoveryResult]:
        """
        Look every person up and record what was found.

        Args:
            persons: The people to look for.
            progress: Called as ``(index, total, person)`` after each lookup.

        Returns:
            One result per person, in the order given.
        """
        results: List[M365DiscoveryResult] = []
        total = len(persons)

        for index, person in enumerate(persons, start=1):
            result = self._discover_one(person)
            results.append(result)
            if progress is not None:
                progress(index, total, person)

        return results

    def _discover_one(self, person: Person) -> M365DiscoveryResult:
        """Look one person up, by sign-in name."""
        upn = self._candidate_upn(person)
        if not upn:
            person.m365_status = M365Status.UNKNOWN
            return M365DiscoveryResult(
                person=person,
                error="No sign-in name could be worked out for this person.")

        try:
            found = self.client.find_user(upn)
        except M365Error as exc:
            # "Could not ask" must not look like "not there": a failed lookup
            # would otherwise make the synchronisation create a duplicate.
            logger.warning("Could not look %s up: %s", upn, exc)
            person.m365_status = M365Status.UNKNOWN
            return M365DiscoveryResult(person=person, error=str(exc))

        if found is None:
            person.m365_status = M365Status.NOT_FOUND_IN_M365
            return M365DiscoveryResult(person=person, found=False)

        attributes = {
            'id': found.object_id,
            'displayName': found.display_name,
            'userPrincipalName': found.user_principal_name,
            'mail': found.mail,
            'givenName': found.given_name,
            'surname': found.surname,
            'accountEnabled': found.account_enabled,
        }

        person.m365_object_id = found.object_id
        person.metadata[M365_VALUES_METADATA_KEY] = attributes

        differences = store_comparison(
            person, compare_person_with_m365(person, attributes))
        person.m365_status = (M365Status.DIFFERS_FROM_M365 if differences
                              else M365Status.MATCHES_M365)

        return M365DiscoveryResult(person=person, found=True,
                                   object_id=found.object_id,
                                   attributes=attributes)

    def _candidate_upn(self, person: Person) -> str:
        """
        The sign-in name to look a person up by.

        What the application already holds wins: it is what a previous
        synchronisation actually created.  Only when there is nothing is the
        template used to guess.
        """
        known = (person.m365_user_principal_name or "").strip()
        if known:
            return known
        try:
            return self.config.upn_for(person)
        except TemplateError as exc:
            logger.debug("Cannot build a sign-in name for %s %s: %s",
                         person.first_name, person.last_name, exc)
            return ""


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class M365CreateUser:
    """One account to create."""

    person: Person
    user_principal_name: str
    display_name: str
    mail_nickname: str
    password: str


@dataclass
class M365UpdateUser:
    """One account to bring into line."""

    person: Person
    object_id: str
    changes: Dict[str, Any]


@dataclass
class M365GroupOperation:
    """One class to map onto a group."""

    school_class: Class
    group_name: str
    mail_nickname: str
    persons: List[Person] = field(default_factory=list)
    create_team: bool = False


@dataclass
class M365SyncPlan:
    """
    Everything a synchronisation would do, worked out before anything is done.

    Shown to the user for confirmation, exactly as the Active Directory plan
    is.  Nothing in here has happened yet.
    """

    create_users: List[M365CreateUser] = field(default_factory=list)
    update_users: List[M365UpdateUser] = field(default_factory=list)
    groups: List[M365GroupOperation] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to do."""
        return not (self.create_users or self.update_users or self.groups)

    def summary(self) -> str:
        """One line describing the plan."""
        return (f"{len(self.create_users)} account(s) to create, "
                f"{len(self.update_users)} to update, "
                f"{len(self.groups)} group(s), "
                f"{len(self.skipped)} person(s) skipped")


class M365SyncService:
    """Builds and carries out a Microsoft 365 synchronisation."""

    def __init__(self, client: M365Client, config: M365SyncConfig,
                 password_generator=None):
        """
        Args:
            client: A connected client, belonging to this thread.
            config: How names are built and what should happen.
            password_generator: Called with no arguments to produce a password
                for a new account.  Defaults to the application's own policy,
                so the Microsoft 365 rules can be configured in one place.
        """
        self.client = client
        self.config = config
        self._password_generator = password_generator or self._default_password

    @staticmethod
    def _default_password() -> str:
        """A password from the application's configured policy."""
        from utils.ad_utils import generate_password
        return generate_password()

    # -- planning ---------------------------------------------------------

    def create_sync_plan(self, source: Source) -> M365SyncPlan:
        """
        Work out what synchronising this source would do.

        Nothing is written and nothing is read from Microsoft - the plan is
        built entirely from what discovery already found, so it is instant and
        can be shown to the user before anything happens.

        Args:
            source: The source to synchronise.

        Returns:
            The plan, including the reasons anybody was left out.
        """
        plan = M365SyncPlan()
        plan.problems.extend(self.config.problems())

        for school_class in source.classes:
            try:
                group_name = self.config.group_name_for(school_class)
            except TemplateError as exc:
                plan.problems.append(
                    f"{school_class.name}: the group name cannot be built "
                    f"({exc})"
                )
                continue

            # A placeholder that resolves to nothing renders perfectly well -
            # "Trida-{grade}" becomes "Trida-" for a class called "Zaci" - and
            # every such class would then map to the SAME group.  That is
            # worse than refusing.
            missing = empty_placeholders(self.config.group_name_template,
                                         class_values(school_class))
            if missing:
                plan.problems.append(
                    f"{school_class.name}: the group name would be "
                    f"{group_name!r}, because "
                    + ", ".join(f"{{{name}}}" for name in missing)
                    + " is empty for this class"
                )
                continue

            operation = M365GroupOperation(
                school_class=school_class,
                group_name=group_name,
                mail_nickname=mail_nickname_for(group_name, "trida"),
                create_team=self.config.create_teams,
            )

            for person in school_class.persons:
                self._plan_person(person, operation, plan)

            plan.groups.append(operation)

        return plan

    def _plan_person(self, person: Person, operation: M365GroupOperation,
                     plan: M365SyncPlan) -> None:
        """Decide what should happen to one person."""
        who = f"{person.first_name} {person.last_name} ({person.class_name})"

        if person.m365_status is M365Status.MULTIPLE_M365_MATCHES:
            # Creating an account would add yet another duplicate.
            plan.skipped.append(
                f"{who}: several Microsoft 365 accounts match, resolve that "
                f"first")
            return

        if person.m365_status is M365Status.UNKNOWN:
            plan.skipped.append(
                f"{who}: not looked up yet - press 'Discover in Microsoft 365'")
            return

        operation.persons.append(person)

        if person.m365_status is M365Status.NOT_FOUND_IN_M365:
            self._plan_creation(person, plan, who)
            return

        if not self.config.update_existing:
            return

        changes = changes_for_graph(person)
        if changes and person.m365_object_id:
            plan.update_users.append(M365UpdateUser(
                person=person, object_id=person.m365_object_id,
                changes=changes))

    def _plan_creation(self, person: Person, plan: M365SyncPlan,
                       who: str) -> None:
        """Add one account to the creation list, or say why it cannot be."""
        try:
            upn = (person.m365_user_principal_name or "").strip() or \
                self.config.upn_for(person)
            display_name = (person.m365_display_name or "").strip() or \
                self.config.display_name_for(person)
        except TemplateError as exc:
            plan.skipped.append(f"{who}: {exc}")
            return

        local_part, _, domain_part = upn.partition("@")
        if not local_part.strip() or not domain_part.strip():
            # "@skola.cz" passes a naive "@ in it" test and is not a name.
            plan.skipped.append(
                f"{who}: {upn or '(empty)'} is not a usable sign-in name")
            return

        nickname = (person.m365_mail_nickname or "").strip() or \
            mail_nickname_for(upn.split("@", 1)[0], "user")

        password = (person.m365_password or "").strip() or \
            self._password_generator()

        plan.create_users.append(M365CreateUser(
            person=person,
            user_principal_name=upn,
            display_name=display_name,
            mail_nickname=nickname,
            password=password,
        ))

    # -- executing --------------------------------------------------------

    def execute_sync(self, plan: M365SyncPlan, progress=None,
                     log=None) -> 'M365SyncResult':
        """
        Carry out a plan.

        Every step is attempted independently: a password that Microsoft
        refuses must not cancel the group membership, and one failing account
        must not abandon the other twenty-nine.  That is the lesson of the
        Active Directory synchronisation, where an early ``return`` used to
        skip the group step entirely.

        Args:
            plan: What to do.
            progress: Called as ``(percent, message)``.
            log: Called as ``(message, level)`` where level is one of
                ``"info"``, ``"warning"``, ``"error"``, ``"success"``.

        Returns:
            What happened.
        """
        result = M365SyncResult()

        def say(message: str, level: str = "info") -> None:
            if log is not None:
                log(message, level)

        def step(percent: int, message: str) -> None:
            if progress is not None:
                progress(percent, message)

        total_steps = max(1, len(plan.create_users) + len(plan.update_users)
                          + len(plan.groups))
        done = 0

        # --- accounts first: a group cannot hold somebody who does not exist
        for operation in plan.create_users:
            done += 1
            step(int(100 * done / total_steps),
                 f"Creating {operation.user_principal_name}...")
            self._create_one(operation, result, say)

        for update in plan.update_users:
            done += 1
            step(int(100 * done / total_steps),
                 f"Updating {update.person.first_name} "
                 f"{update.person.last_name}...")
            self._update_one(update, result, say)

        # --- then the groups and their membership
        for operation in plan.groups:
            done += 1
            step(int(100 * done / total_steps),
                 f"Group {operation.group_name}...")
            self._sync_group(operation, result, say)

        step(100, "Done")
        return result

    def _create_one(self, operation: M365CreateUser, result: 'M365SyncResult',
                    say) -> None:
        """Create one account and record what happened to it."""
        person = operation.person
        try:
            created = self.client.create_user(
                display_name=operation.display_name,
                user_principal_name=operation.user_principal_name,
                mail_nickname=operation.mail_nickname,
                password=operation.password,
                given_name=person.first_name,
                surname=person.last_name,
                force_change=self.config.force_change_password,
                usage_location=self.config.usage_location,
            )
        except M365Error as exc:
            person.m365_status = M365Status.SYNC_INCOMPLETE
            result.failed.append(f"{operation.user_principal_name}: {exc}")
            say(f"Could not create {operation.user_principal_name}: {exc}",
                "error")
            return

        person.m365_object_id = created.object_id
        person.m365_user_principal_name = operation.user_principal_name
        person.m365_display_name = operation.display_name
        person.m365_mail_nickname = operation.mail_nickname
        person.m365_password = operation.password
        person.m365_status = M365Status.SYNC_SUCCEEDED
        person.reset_m365_dirty()

        result.created.append(operation.user_principal_name)
        say(f"Created {operation.user_principal_name}", "success")

    def _update_one(self, update: M365UpdateUser, result: 'M365SyncResult',
                    say) -> None:
        """Bring one account into line."""
        person = update.person
        try:
            self.client.update_user(update.object_id, update.changes)
        except M365Error as exc:
            person.m365_status = M365Status.SYNC_INCOMPLETE
            result.failed.append(
                f"{person.first_name} {person.last_name}: {exc}")
            say(f"Could not update {person.first_name} {person.last_name}: "
                f"{exc}", "error")
            return

        person.m365_status = M365Status.SYNC_SUCCEEDED
        person.reset_m365_dirty()
        result.updated.append(f"{person.first_name} {person.last_name}")
        say(f"Updated {person.first_name} {person.last_name} "
            f"({', '.join(sorted(update.changes))})", "success")

    def _sync_group(self, operation: M365GroupOperation,
                    result: 'M365SyncResult', say) -> None:
        """Make sure the group exists, holds the class, and has a team if asked."""
        try:
            group = self.client.find_group(operation.group_name)
        except M365Error as exc:
            result.failed.append(f"{operation.group_name}: {exc}")
            say(f"Could not look up the group {operation.group_name}: {exc}",
                "error")
            return

        if group is None:
            group = self._create_group(operation, result, say)
            if group is None:
                return
        else:
            result.groups_found.append(operation.group_name)
            say(f"Group {operation.group_name} already exists", "info")

        member_ids = [person.m365_object_id for person in operation.persons
                      if person.m365_object_id]
        if member_ids:
            problems = self.client.add_group_members(group.object_id,
                                                     member_ids)
            if problems:
                result.failed.extend(problems)
                for problem in problems:
                    say(problem, "warning")
            else:
                say(f"{len(member_ids)} member(s) in {operation.group_name}",
                    "success")

        # The team is last, and only when asked: the point is explicit that a
        # team must not appear on its own.
        if operation.create_team:
            self._attach_team(group, operation, result, say)

    def _create_group(self, operation: M365GroupOperation,
                      result: 'M365SyncResult', say) -> Optional[M365Group]:
        """Create the group a class maps to."""
        owner_ids = self._resolve_owners(say)

        if self.config.create_teams and not owner_ids:
            # Without an owner the team step below would fail anyway, and an
            # ownerless group cannot be managed in the portal either.
            say(f"{operation.group_name}: no owner could be resolved, so the "
                f"group would be unmanageable", "warning")

        try:
            group = self.client.create_group(
                display_name=operation.group_name,
                mail_nickname=operation.mail_nickname,
                description=f"Class {operation.school_class.name}",
                owner_ids=owner_ids,
            )
        except M365Error as exc:
            result.failed.append(f"{operation.group_name}: {exc}")
            say(f"Could not create the group {operation.group_name}: {exc}",
                "error")
            return None

        result.groups_created.append(operation.group_name)
        say(f"Created the group {operation.group_name}", "success")
        return group

    def _attach_team(self, group: M365Group, operation: M365GroupOperation,
                     result: 'M365SyncResult', say) -> None:
        """Attach a team to a group, if it has not got one already."""
        if group.has_team:
            say(f"{operation.group_name} already has a team", "info")
            return
        try:
            self.client.create_team(group.object_id)
        except M365Error as exc:
            result.failed.append(f"{operation.group_name} (team): {exc}")
            say(f"Could not create the team for {operation.group_name}: {exc}",
                "error")
            return
        result.teams_created.append(operation.group_name)
        say(f"Created the team for {operation.group_name}", "success")

    def _resolve_owners(self, say) -> List[str]:
        """
        Turn the configured owner sign-in names into object ids.

        Looked up once per synchronisation and cached, because the same owners
        are used for every group.
        """
        if getattr(self, "_owner_ids", None) is not None:
            return self._owner_ids

        owner_ids: List[str] = []
        for upn in self.config.owner_user_principal_names:
            upn = (upn or "").strip()
            if not upn:
                continue
            try:
                found = self.client.find_user(upn)
            except M365Error as exc:
                say(f"Could not look the owner {upn} up: {exc}", "warning")
                continue
            if found is None:
                say(f"No account called {upn} - it cannot own a group",
                    "warning")
                continue
            owner_ids.append(found.object_id)

        self._owner_ids = owner_ids
        return owner_ids


@dataclass
class M365SyncResult:
    """What a synchronisation actually did."""

    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    groups_created: List[str] = field(default_factory=list)
    groups_found: List[str] = field(default_factory=list)
    teams_created: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """True when nothing failed."""
        return not self.failed

    def summary(self) -> str:
        """One line for the finishing message."""
        parts = [
            f"{len(self.created)} account(s) created",
            f"{len(self.updated)} updated",
            f"{len(self.groups_created)} group(s) created",
        ]
        if self.teams_created:
            parts.append(f"{len(self.teams_created)} team(s) created")
        if self.failed:
            parts.append(f"{len(self.failed)} problem(s)")
        return ", ".join(parts)
