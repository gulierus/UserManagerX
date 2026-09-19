"""
Tests for Microsoft 365 discovery and synchronisation (Version 26, point 21 e).

A class maps to a *group*, not to an organisational unit, and a team is only
created when the user asks for one.
"""

import pytest

from models import Class, Person, Source
from models_m365 import M365Group, M365Member, M365Status
from services.m365_client import M365Error
from services.m365_comparison import (
    M365_VALUES_METADATA_KEY, compare_person_with_m365, store_comparison,
)
from services.m365_services import (
    DEFAULT_DISPLAY_NAME_TEMPLATE,
    DEFAULT_GROUP_TEMPLATE,
    DEFAULT_UPN_TEMPLATE,
    M365DiscoveryService,
    M365SyncConfig,
    M365SyncService,
)


class FakeClient:
    """A Microsoft 365 that answers exactly what a test says."""

    def __init__(self):
        self.users = {}          # upn -> M365Member
        self.groups = {}         # display name -> M365Group
        self.created_users = []
        self.created_groups = []
        self.updated = []
        self.memberships = []
        self.teams = []
        self.fail_on = set()

    def find_user(self, upn):
        if "find_user" in self.fail_on:
            raise M365Error("the directory is unavailable")
        return self.users.get(upn)

    def find_group(self, name):
        if "find_group" in self.fail_on:
            raise M365Error("the directory is unavailable")
        return self.groups.get(name)

    def create_user(self, display_name, user_principal_name, mail_nickname,
                    password, given_name="", surname="", force_change=True,
                    usage_location=""):
        if "create_user" in self.fail_on:
            raise M365Error("the password does not meet the policy")
        member = M365Member(object_id=f"id-{len(self.created_users)}",
                            display_name=display_name,
                            user_principal_name=user_principal_name,
                            given_name=given_name, surname=surname)
        self.created_users.append({
            'upn': user_principal_name, 'display_name': display_name,
            'mail_nickname': mail_nickname, 'password': password,
            'force_change': force_change, 'usage_location': usage_location,
        })
        self.users[user_principal_name] = member
        return member

    def create_group(self, display_name, mail_nickname, description="",
                     owner_ids=None, unified=True, visibility="Private"):
        if "create_group" in self.fail_on:
            raise M365Error("not allowed")
        group = M365Group(object_id=f"g-{len(self.created_groups)}",
                          display_name=display_name,
                          mail_nickname=mail_nickname,
                          group_types=["Unified"])
        self.created_groups.append({'name': display_name,
                                    'nickname': mail_nickname,
                                    'owner_ids': list(owner_ids or [])})
        self.groups[display_name] = group
        return group

    def add_group_members(self, group_id, user_ids):
        if "add_members" in self.fail_on:
            return [f"{uid} was not added" for uid in user_ids]
        self.memberships.append((group_id, list(user_ids)))
        return []

    def create_team(self, group_id):
        if "create_team" in self.fail_on:
            raise M365Error("the group has no owner")
        self.teams.append(group_id)
        return True

    def update_user(self, object_id, changes):
        if "update_user" in self.fail_on:
            raise M365Error("refused")
        self.updated.append((object_id, dict(changes)))


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def config():
    return M365SyncConfig(domain="skola.cz")


def person(first="Jan", last="Novák", class_name="6.A", **overrides):
    return Person(first_name=first, last_name=last, class_name=class_name,
                  **overrides)


def source_of(*classes):
    made = Source(name="Roster", source_type="manual")
    for school_class in classes:
        made.add_class(school_class)
    return made


def class_of(name, persons):
    school_class = Class(name=name)
    for one in persons:
        school_class.add_person(one)
    return school_class


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

class TestConfig:
    """What the user chose, and whether it can work."""

    def test_the_defaults_are_usable(self, config):
        assert config.problems() == []

    def test_a_team_is_not_created_by_default(self, config):
        # The point is explicit that it must not happen automatically.
        assert config.create_teams is False

    def test_a_group_name_is_built_from_the_class(self, config):
        assert config.group_name_for(Class(name="6.A")) == "Trida-6.A"

    def test_a_sign_in_name_is_built_from_the_person(self, config):
        assert config.upn_for(person()) == "novakj@skola.cz"

    def test_the_sign_in_name_has_no_diacritics(self, config):
        assert config.upn_for(person("Žofie", "Křížová")) == "krizovaz@skola.cz"

    def test_a_display_name_is_built_from_the_person(self, config):
        assert config.display_name_for(person()) == "Jan Novák (6.A)"

    def test_a_broken_group_template_is_a_problem(self):
        problems = M365SyncConfig(domain="s.cz",
                                  group_name_template="{nope}").problems()
        assert any("Group name" in problem for problem in problems)

    def test_a_sign_in_name_without_a_domain_is_a_problem(self):
        problems = M365SyncConfig(domain="").problems()
        assert any("tenant domain" in problem for problem in problems)

    def test_teams_without_an_owner_are_a_problem(self):
        # Microsoft refuses to build a team on an ownerless group.
        problems = M365SyncConfig(domain="s.cz", create_teams=True).problems()
        assert any("no owner" in problem for problem in problems)

    def test_teams_with_an_owner_are_fine(self):
        assert M365SyncConfig(domain="s.cz", create_teams=True,
                              owner_user_principal_names=["t@s.cz"]
                              ).problems() == []

    def test_the_documented_defaults(self):
        assert DEFAULT_GROUP_TEMPLATE == "Trida-{class_name}"
        assert "{domain}" in DEFAULT_UPN_TEMPLATE
        assert "{class_name}" in DEFAULT_DISPLAY_NAME_TEMPLATE


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    """Finding the account of each person."""

    @pytest.fixture
    def service(self, client, config):
        return M365DiscoveryService(client, config)

    def test_a_person_who_is_not_there_is_reported_as_missing(self, service):
        one = person()
        service.discover_persons([one])
        assert one.m365_status is M365Status.NOT_FOUND_IN_M365

    def test_an_identical_account_is_reported_as_identical(self, service,
                                                           client):
        one = person(m365_display_name="Jan Novák (6.A)",
                     m365_user_principal_name="novakj@skola.cz")
        client.users["novakj@skola.cz"] = M365Member(
            object_id="u1", display_name="Jan Novák (6.A)",
            user_principal_name="novakj@skola.cz",
            given_name="Jan", surname="Novák", account_enabled=False)

        service.discover_persons([one])
        assert one.m365_status is M365Status.MATCHES_M365

    def test_a_differing_account_is_reported_as_differing(self, service,
                                                          client):
        one = person(m365_display_name="Jan Novák (6.A)")
        client.users["novakj@skola.cz"] = M365Member(
            object_id="u1", display_name="Something Else",
            user_principal_name="novakj@skola.cz",
            given_name="Jan", surname="Novák")

        service.discover_persons([one])
        assert one.m365_status is M365Status.DIFFERS_FROM_M365

    def test_the_object_id_is_recorded(self, service, client):
        one = person()
        client.users["novakj@skola.cz"] = M365Member(
            object_id="u1", user_principal_name="novakj@skola.cz",
            given_name="Jan", surname="Novák")

        service.discover_persons([one])
        assert one.m365_object_id == "u1"

    def test_the_known_sign_in_name_wins_over_the_template(self, service,
                                                           client):
        # It is what a previous synchronisation actually created.
        one = person(m365_user_principal_name="special@skola.cz")
        client.users["special@skola.cz"] = M365Member(
            object_id="u9", user_principal_name="special@skola.cz",
            given_name="Jan", surname="Novák")

        service.discover_persons([one])
        assert one.m365_object_id == "u9"

    def test_a_failed_lookup_is_not_reported_as_missing(self, service, client):
        # "Could not ask" must not become "create a duplicate".
        client.fail_on.add("find_user")
        one = person()
        results = service.discover_persons([one])
        assert one.m365_status is M365Status.UNKNOWN
        assert results[0].error

    def test_progress_is_reported(self, service):
        seen = []
        service.discover_persons([person(), person("Eva", "Malá")],
                                 progress=lambda i, t, p: seen.append((i, t)))
        assert seen == [(1, 2), (2, 2)]


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

class TestPlanning:
    """What a synchronisation would do."""

    @pytest.fixture
    def service(self, client, config):
        return M365SyncService(client, config,
                               password_generator=lambda: "Fixed-Pass1!")

    def test_a_class_becomes_a_group(self, service):
        plan = service.create_sync_plan(source_of(class_of("6.A", [person()])))
        assert [g.group_name for g in plan.groups] == ["Trida-6.A"]

    def test_the_group_gets_a_usable_alias(self, service):
        plan = service.create_sync_plan(source_of(class_of("6.A", [person()])))
        # Microsoft rejects spaces and most punctuation in an alias; the
        # hyphen and the underscore are kept because they are allowed.
        assert plan.groups[0].mail_nickname == "trida-6a"

    def test_a_person_who_is_not_in_m365_is_created(self, service):
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert [c.user_principal_name for c in plan.create_users] == \
            ["novakj@skola.cz"]

    def test_a_created_account_gets_a_password(self, service):
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert plan.create_users[0].password == "Fixed-Pass1!"

    def test_a_password_the_person_already_has_is_kept(self, service):
        one = person(m365_password="Chosen1!")
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert plan.create_users[0].password == "Chosen1!"

    def test_a_differing_person_is_updated(self, service):
        one = person(m365_display_name="Jan Novák (6.A)")
        one.m365_object_id = "u1"
        one.metadata[M365_VALUES_METADATA_KEY] = {
            'displayName': "Old", 'givenName': "Jan", 'surname': "Novák"}
        store_comparison(one, compare_person_with_m365(one))
        one.m365_status = M365Status.DIFFERS_FROM_M365

        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert plan.update_users[0].changes == {'displayName': "Jan Novák (6.A)"}

    def test_an_identical_person_needs_no_update(self, service):
        one = person()
        one.m365_object_id = "u1"
        one.m365_status = M365Status.MATCHES_M365
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert plan.update_users == []

    def test_updating_can_be_switched_off(self, client):
        config = M365SyncConfig(domain="skola.cz", update_existing=False)
        one = person(m365_display_name="Jan Novák (6.A)")
        one.m365_object_id = "u1"
        one.metadata[M365_VALUES_METADATA_KEY] = {
            'displayName': "Old", 'givenName': "Jan", 'surname': "Novák"}
        store_comparison(one, compare_person_with_m365(one))
        one.m365_status = M365Status.DIFFERS_FROM_M365

        plan = M365SyncService(client, config).create_sync_plan(
            source_of(class_of("6.A", [one])))
        assert plan.update_users == []

    def test_a_person_who_was_never_looked_up_is_skipped(self, service):
        # Creating an account for somebody who may already have one would
        # produce a duplicate.
        plan = service.create_sync_plan(source_of(class_of("6.A", [person()])))
        assert plan.create_users == []
        assert any("Discover" in reason for reason in plan.skipped)

    def test_an_ambiguous_person_is_skipped(self, service):
        one = person()
        one.m365_status = M365Status.MULTIPLE_M365_MATCHES
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))
        assert plan.create_users == []
        assert any("several" in reason.lower() for reason in plan.skipped)

    def test_a_class_whose_group_name_cannot_be_built_is_reported(self, client):
        # "Zaci" has no grade, so "Trida-{grade}" would render as "Trida-" -
        # and every such class would map to that same group.
        config = M365SyncConfig(domain="s.cz",
                                group_name_template="Trida-{grade}")
        plan = M365SyncService(client, config).create_sync_plan(
            source_of(class_of("Zaci", [person(class_name="Zaci")])))
        assert plan.groups == []
        assert any("{grade}" in problem for problem in plan.problems)

    def test_a_sign_in_name_with_an_empty_local_part_is_refused(self, client):
        # "@skola.cz" passes a naive "@ in it" test and is not a name.
        config = M365SyncConfig(domain="skola.cz",
                                upn_template="{username}@{domain}")
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        plan = M365SyncService(client, config).create_sync_plan(
            source_of(class_of("6.A", [one])))
        assert plan.create_users == []
        assert any("sign-in name" in reason for reason in plan.skipped)

    def test_an_empty_plan_says_so(self, service):
        assert M365SyncService.create_sync_plan(
            service, source_of()).is_empty is True


# ---------------------------------------------------------------------------
# executing
# ---------------------------------------------------------------------------

class TestExecuting:
    """Carrying the plan out."""

    @pytest.fixture
    def service(self, client, config):
        return M365SyncService(client, config,
                               password_generator=lambda: "Fixed-Pass1!")

    def plan_for(self, service, persons, status=M365Status.NOT_FOUND_IN_M365,
                 class_name="6.A"):
        for one in persons:
            one.m365_status = status
        return service.create_sync_plan(
            source_of(class_of(class_name, persons)))

    def test_the_accounts_are_created(self, service, client):
        result = service.execute_sync(self.plan_for(service, [person()]))
        assert [u['upn'] for u in client.created_users] == ["novakj@skola.cz"]
        assert result.created == ["novakj@skola.cz"]

    def test_the_person_remembers_their_account(self, service):
        one = person()
        service.execute_sync(self.plan_for(service, [one]))
        assert one.m365_object_id == "id-0"
        assert one.m365_user_principal_name == "novakj@skola.cz"
        assert one.m365_status is M365Status.SYNC_SUCCEEDED

    def test_the_group_is_created_and_filled(self, service, client):
        service.execute_sync(self.plan_for(service, [person()]))
        assert [g['name'] for g in client.created_groups] == ["Trida-6.A"]
        assert client.memberships == [("g-0", ["id-0"])]

    def test_an_existing_group_is_reused_not_duplicated(self, service, client):
        client.groups["Trida-6.A"] = M365Group(object_id="existing",
                                               display_name="Trida-6.A")
        result = service.execute_sync(self.plan_for(service, [person()]))
        assert client.created_groups == []
        assert result.groups_found == ["Trida-6.A"]

    def test_no_team_is_created_unless_asked(self, service, client):
        service.execute_sync(self.plan_for(service, [person()]))
        assert client.teams == []

    def test_a_team_is_created_when_asked(self, client):
        config = M365SyncConfig(domain="skola.cz", create_teams=True,
                                owner_user_principal_names=["t@skola.cz"])
        client.users["t@skola.cz"] = M365Member(object_id="owner",
                                                user_principal_name="t@skola.cz")
        service = M365SyncService(client, config,
                                  password_generator=lambda: "P1!")
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        plan = service.create_sync_plan(source_of(class_of("6.A", [one])))

        result = service.execute_sync(plan)
        assert client.teams == ["g-0"]
        assert result.teams_created == ["Trida-6.A"]

    def test_the_owner_is_bound_when_the_group_is_created(self, client):
        config = M365SyncConfig(domain="skola.cz",
                                owner_user_principal_names=["t@skola.cz"])
        client.users["t@skola.cz"] = M365Member(object_id="owner",
                                                user_principal_name="t@skola.cz")
        service = M365SyncService(client, config,
                                  password_generator=lambda: "P1!")
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        service.execute_sync(service.create_sync_plan(
            source_of(class_of("6.A", [one]))))
        assert client.created_groups[0]['owner_ids'] == ["owner"]

    def test_a_failed_account_does_not_abandon_the_group(self, service, client):
        client.fail_on.add("create_user")
        result = service.execute_sync(self.plan_for(service, [person()]))
        assert result.failed
        # The group step still ran.
        assert client.created_groups

    def test_a_failed_account_is_marked_as_incomplete(self, service, client):
        client.fail_on.add("create_user")
        one = person()
        service.execute_sync(self.plan_for(service, [one]))
        assert one.m365_status is M365Status.SYNC_INCOMPLETE

    def test_a_failed_membership_is_reported_without_stopping(self, service,
                                                              client):
        client.fail_on.add("add_members")
        result = service.execute_sync(self.plan_for(service, [person()]))
        assert result.failed and result.created

    def test_a_failed_team_does_not_undo_the_group(self, client):
        config = M365SyncConfig(domain="skola.cz", create_teams=True,
                                owner_user_principal_names=["t@skola.cz"])
        client.users["t@skola.cz"] = M365Member(object_id="owner",
                                                user_principal_name="t@skola.cz")
        client.fail_on.add("create_team")
        service = M365SyncService(client, config,
                                  password_generator=lambda: "P1!")
        one = person()
        one.m365_status = M365Status.NOT_FOUND_IN_M365
        result = service.execute_sync(service.create_sync_plan(
            source_of(class_of("6.A", [one]))))
        assert result.groups_created == ["Trida-6.A"]
        assert any("team" in problem for problem in result.failed)

    def test_the_forced_password_change_reaches_microsoft(self, service,
                                                          client):
        service.execute_sync(self.plan_for(service, [person()]))
        assert client.created_users[0]['force_change'] is True

    def test_progress_and_log_are_reported(self, service):
        steps, logged = [], []
        service.execute_sync(self.plan_for(service, [person()]),
                             progress=lambda p, m: steps.append(p),
                             log=lambda m, l: logged.append((m, l)))
        assert steps and steps[-1] == 100
        assert any(level == "success" for _message, level in logged)

    def test_the_result_summarises_itself(self, service):
        result = service.execute_sync(self.plan_for(service, [person()]))
        assert "created" in result.summary() and result.succeeded is True
