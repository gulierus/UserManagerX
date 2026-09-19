"""
Tests for the Microsoft 365 client (Version 26, point 21).

Nothing here talks to Microsoft.  The conversions are exercised against the
SDK's real model objects, and the request methods against a stubbed Graph
client, so a change in the SDK's shape shows up as a failure rather than at
run time in a school.
"""

import asyncio

import pytest

from models_m365 import M365Group, M365Member, M365Team
from services.m365_auth import AuthMethod, M365Credentials
from services.m365_client import (
    GROUP_SELECT,
    MEMBER_SELECT,
    PASSWORD_METHOD_ID,
    M365Client,
    M365Error,
    _escape_filter,
    _group_from_entry,
    _member_from_entry,
    mail_nickname_for,
)


# ---------------------------------------------------------------------------
# conversions
# ---------------------------------------------------------------------------

def graph_group(**attributes):
    """A real SDK Group object with the given attributes set."""
    from msgraph.generated.models.group import Group
    entry = Group()
    for name, value in attributes.items():
        setattr(entry, name, value)
    return entry


def graph_user(**attributes):
    """A real SDK User object with the given attributes set."""
    from msgraph.generated.models.user import User
    entry = User()
    for name, value in attributes.items():
        setattr(entry, name, value)
    return entry


class TestGroupConversion:
    """A Graph group becomes an M365Group."""

    def test_the_fields_are_carried_across(self):
        group = _group_from_entry(graph_group(
            id="abc", display_name="Trida-6.A", mail_nickname="trida6a",
            description="Sixth A", mail="trida6a@skola.cz",
            visibility="Private", group_types=["Unified"],
            mail_enabled=True, security_enabled=False))
        assert group.object_id == "abc"
        assert group.display_name == "Trida-6.A"
        assert group.mail_nickname == "trida6a"
        assert group.visibility == "Private"

    def test_a_unified_group_is_recognised(self):
        assert _group_from_entry(graph_group(group_types=["Unified"])).kind == \
            "Microsoft 365"

    def test_a_group_with_a_team_becomes_a_team(self):
        group = _group_from_entry(graph_group(
            group_types=["Unified"], resource_provisioning_options=["Team"]))
        assert isinstance(group, M365Team) and group.has_team is True

    def test_a_group_without_a_team_is_not_one(self):
        group = _group_from_entry(graph_group(group_types=["Unified"],
                                              resource_provisioning_options=[]))
        assert not isinstance(group, M365Team) and group.has_team is False

    def test_missing_fields_become_empty_strings_not_none(self):
        group = _group_from_entry(graph_group(id="abc"))
        assert group.display_name == "" and group.description == ""

    def test_a_security_group_is_recognised(self):
        group = _group_from_entry(graph_group(group_types=[],
                                              security_enabled=True))
        assert group.kind == "Security"


class TestMemberConversion:
    """A Graph directory object becomes an M365Member, or nothing."""

    def test_the_fields_are_carried_across(self):
        member = _member_from_entry(graph_user(
            id="u1", display_name="Jan Novák", user_principal_name="j@skola.cz",
            mail="j@skola.cz", given_name="Jan", surname="Novák"))
        assert member.object_id == "u1"
        assert member.split_name() == ("Jan", "Novák")

    def test_an_unreported_account_state_counts_as_enabled(self):
        # bool(None) is False, which would report every account the request did
        # not ask about as disabled.
        member = _member_from_entry(graph_user(id="u1",
                                               user_principal_name="j@skola.cz"))
        assert member.account_enabled is True

    def test_a_disabled_account_is_reported_as_disabled(self):
        member = _member_from_entry(graph_user(
            id="u1", user_principal_name="j@skola.cz", account_enabled=False))
        assert member.account_enabled is False

    def test_nothing_at_all_converts_to_nothing(self):
        assert _member_from_entry(None) is None

    def test_a_nested_group_is_not_a_person(self):
        # A group's members may include other groups; they must not become
        # pupils.
        assert _member_from_entry(graph_group(id="g1",
                                              display_name="Other")) is None

    def test_an_object_typed_as_something_else_is_not_a_person(self):
        entry = graph_user(id="s1", user_principal_name="x@y.cz")
        entry.odata_type = "#microsoft.graph.servicePrincipal"
        assert _member_from_entry(entry) is None

    def test_an_object_typed_as_a_user_is_a_person(self):
        entry = graph_user(id="u1", user_principal_name="x@y.cz")
        entry.odata_type = "#microsoft.graph.user"
        assert _member_from_entry(entry) is not None


class TestMailNickname:
    """Microsoft rejects most punctuation in an alias."""

    @pytest.mark.parametrize("name,expected", [
        ("Trida 6.A", "trida6a"),
        ("Žáci 9.Č", "zaci9c"),
        ("already-fine_1", "already-fine_1"),
        ("UPPER", "upper"),
    ])
    def test_a_display_name_becomes_a_usable_alias(self, name, expected):
        assert mail_nickname_for(name) == expected

    def test_nothing_usable_falls_back(self):
        # An empty alias would be rejected by Microsoft.
        assert mail_nickname_for("!!!") == "group"
        assert mail_nickname_for("") == "group"

    def test_the_fallback_can_be_chosen(self):
        assert mail_nickname_for("", fallback="trida") == "trida"


class TestFilterEscaping:
    """A name with an apostrophe must not break an OData filter."""

    def test_a_single_quote_is_doubled(self):
        assert _escape_filter("O'Brien") == "O''Brien"

    def test_ordinary_text_is_untouched(self):
        assert _escape_filter("Novak") == "Novak"

    def test_none_is_accepted(self):
        assert _escape_filter(None) == ""


# ---------------------------------------------------------------------------
# requests, against a stubbed Graph client
# ---------------------------------------------------------------------------

class Page:
    """One page of a Graph collection."""

    def __init__(self, value, next_link=None):
        self.value = value
        self.odata_next_link = next_link


class Recorder:
    """Records the calls a request builder received."""

    def __init__(self):
        self.calls = []


class FakeCollection:
    """A Graph collection that answers with prepared pages."""

    def __init__(self, pages, recorder, name):
        self._pages = list(pages)
        self._recorder = recorder
        self._name = name
        self.ref = self

    async def get(self, request_configuration=None):
        self._recorder.calls.append((f"{self._name}.get", request_configuration))
        return self._pages.pop(0) if self._pages else None

    async def post(self, body=None, request_configuration=None):
        self._recorder.calls.append((f"{self._name}.post", body))
        return getattr(self, "post_result", None)

    async def put(self, body=None, request_configuration=None):
        self._recorder.calls.append((f"{self._name}.put", body))
        return getattr(self, "put_result", None)

    async def patch(self, body=None, request_configuration=None):
        self._recorder.calls.append((f"{self._name}.patch", body))
        return getattr(self, "patch_result", None)

    def with_url(self, url):
        self._recorder.calls.append((f"{self._name}.with_url", url))
        return self


@pytest.fixture
def client():
    """
    A connected M365Client whose Graph client is a stub.

    ``connect()`` is deliberately not called: it would reach Microsoft.  The
    flag is set by hand, which is exactly the state the request methods expect.
    """
    made = M365Client(M365Credentials(method=AuthMethod.APP_ONLY,
                                      tenant_id="t", client_id="c",
                                      client_secret="s"))
    made.is_connected = True
    yield made
    made.close()


class TestConnectionGuard:
    """Nothing may be attempted before signing in."""

    def test_reading_groups_without_a_connection_is_refused(self):
        made = M365Client(M365Credentials(method=AuthMethod.DEVICE_CODE))
        with pytest.raises(M365Error, match="Not connected"):
            made.list_groups()

    def test_connecting_with_incomplete_credentials_is_refused_locally(self):
        made = M365Client(M365Credentials(method=AuthMethod.APP_ONLY))
        with pytest.raises(M365Error, match="Tenant ID"):
            made.connect()


class TestListGroups:
    """Reading every group in the tenant."""

    def _install(self, client, pages):
        recorder = Recorder()
        collection = FakeCollection(pages, recorder, "groups")
        client._client = type("Stub", (), {"groups": collection})()
        return recorder, collection

    def test_the_groups_come_back_converted(self, client):
        self._install(client, [Page([graph_group(id="1", display_name="6.A")])])
        groups = client.list_groups()
        assert [g.display_name for g in groups] == ["6.A"]

    def test_every_page_is_read(self, client):
        self._install(client, [
            Page([graph_group(id="1", display_name="6.A")], next_link="next"),
            Page([graph_group(id="2", display_name="6.B")]),
        ])
        assert len(client.list_groups()) == 2

    def test_the_requested_fields_are_the_documented_ones(self, client):
        recorder, _ = self._install(client, [Page([])])
        client.list_groups()
        configuration = recorder.calls[0][1]
        assert configuration.query_parameters.select == GROUP_SELECT

    def test_progress_is_reported_after_each_page(self, client):
        self._install(client, [
            Page([graph_group(id="1")], next_link="next"),
            Page([graph_group(id="2")]),
        ])
        seen = []
        client.list_groups(progress=seen.append)
        assert seen == [1, 2]

    def test_an_empty_tenant_gives_an_empty_list(self, client):
        self._install(client, [Page([])])
        assert client.list_groups() == []

    def test_a_refusal_is_explained(self, client):
        recorder = Recorder()

        class Exploding(FakeCollection):
            async def get(self, request_configuration=None):
                raise RuntimeError("Authorization_RequestDenied")

        client._client = type("Stub", (), {
            "groups": Exploding([], recorder, "groups")})()

        with pytest.raises(M365Error, match="permission"):
            client.list_groups()


class TestLoadGroupPeople:
    """Owners and members, read on demand."""

    def _install(self, client, owners, members, group_id="g1"):
        recorder = Recorder()
        owners_collection = FakeCollection([Page(owners)], recorder, "owners")
        members_collection = FakeCollection([Page(members)], recorder, "members")
        item = type("Item", (), {"owners": owners_collection,
                                 "members": members_collection})()
        groups = type("Groups", (), {
            "by_group_id": staticmethod(lambda _id: item)})()
        client._client = type("Stub", (), {"groups": groups})()
        return recorder

    def test_the_members_are_loaded(self, client):
        self._install(client, [], [graph_user(id="u1", display_name="Jan",
                                              user_principal_name="j@s.cz")])
        group = client.load_group_people(M365Group(object_id="g1"))
        assert [m.display_name for m in group.members] == ["Jan"]

    def test_the_owners_are_marked_as_owners(self, client):
        self._install(client, [graph_user(id="o1", display_name="Eva",
                                          user_principal_name="e@s.cz")], [])
        group = client.load_group_people(M365Group(object_id="g1"))
        assert group.owners[0].is_owner is True

    def test_loading_is_recorded_so_empty_is_not_mistaken_for_unread(self,
                                                                     client):
        self._install(client, [], [])
        group = client.load_group_people(M365Group(object_id="g1"))
        assert group.members_loaded is True and group.members == []

    def test_a_group_that_cannot_be_read_does_not_stop_the_wizard(self, client):
        recorder = Recorder()

        class Exploding(FakeCollection):
            async def get(self, request_configuration=None):
                raise RuntimeError("no access to this group")

        item = type("Item", (), {
            "owners": Exploding([], recorder, "owners"),
            "members": Exploding([], recorder, "members")})()
        client._client = type("Stub", (), {"groups": type("G", (), {
            "by_group_id": staticmethod(lambda _id: item)})()})()

        group = client.load_group_people(M365Group(object_id="g1"))
        assert group.members == [] and group.members_loaded is True


class TestCreateGroup:
    """Creating a group, and the owner question."""

    def _install(self, client, created):
        recorder = Recorder()
        collection = FakeCollection([], recorder, "groups")
        collection.post_result = created
        client._client = type("Stub", (), {"groups": collection})()
        return recorder

    def test_the_created_group_comes_back_converted(self, client):
        self._install(client, graph_group(id="new", display_name="6.A",
                                          group_types=["Unified"]))
        group = client.create_group("6.A", "trida6a")
        assert group.object_id == "new" and group.is_unified

    def test_a_unified_group_is_mail_enabled(self, client):
        recorder = self._install(client, graph_group(id="new"))
        client.create_group("6.A", "trida6a", unified=True)
        body = recorder.calls[0][1]
        assert body.mail_enabled is True and body.group_types == ["Unified"]

    def test_a_security_group_is_not(self, client):
        recorder = self._install(client, graph_group(id="new"))
        client.create_group("6.A", "trida6a", unified=False)
        body = recorder.calls[0][1]
        assert body.security_enabled is True and body.group_types == []

    def test_the_owners_are_bound_in_the_creation_request(self, client):
        # App-only has no signed-in user to become the owner, so an ownerless
        # group would be unmanageable in the portal.
        recorder = self._install(client, graph_group(id="new"))
        client.create_group("6.A", "trida6a", owner_ids=["o1", "o2"])
        bound = recorder.calls[0][1].additional_data["owners@odata.bind"]
        assert len(bound) == 2 and bound[0].endswith("/users/o1")

    def test_at_most_twenty_owners_are_bound(self, client):
        # Microsoft refuses more in the creation request itself.
        recorder = self._install(client, graph_group(id="new"))
        client.create_group("6.A", "t", owner_ids=[f"o{i}" for i in range(30)])
        assert len(recorder.calls[0][1].additional_data[
            "owners@odata.bind"]) == 20

    def test_a_refusal_names_the_group(self, client):
        recorder = Recorder()

        class Exploding(FakeCollection):
            async def post(self, body=None, request_configuration=None):
                raise RuntimeError("AADSTS65001")

        client._client = type("Stub", (), {
            "groups": Exploding([], recorder, "groups")})()

        with pytest.raises(M365Error, match="6.A"):
            client.create_group("6.A", "trida6a")


class TestAddMembers:
    """Adding accounts to a group."""

    def _install(self, client, error=None):
        recorder = Recorder()

        class Members(FakeCollection):
            async def post(self, body=None, request_configuration=None):
                recorder.calls.append(("members.ref.post", body))
                if error:
                    raise RuntimeError(error)

        members = Members([], recorder, "members")
        item = type("Item", (), {"members": members})()
        client._client = type("Stub", (), {"groups": type("G", (), {
            "by_group_id": staticmethod(lambda _id: item)})()})()
        return recorder

    def test_each_account_is_added_separately(self, client):
        recorder = self._install(client)
        assert client.add_group_members("g1", ["u1", "u2"]) == []
        assert len(recorder.calls) == 2

    def test_an_account_that_is_already_a_member_is_not_a_problem(self, client):
        self._install(client, error="One or more added object references already exist")
        assert client.add_group_members("g1", ["u1"]) == []

    def test_a_real_failure_is_reported_without_stopping_the_rest(self, client):
        self._install(client, error="something went wrong")
        problems = client.add_group_members("g1", ["u1", "u2"])
        assert len(problems) == 2

    def test_empty_identifiers_are_skipped(self, client):
        recorder = self._install(client)
        client.add_group_members("g1", ["", None, "u1"])
        assert len(recorder.calls) == 1


class TestSetPassword:
    """Setting a password writes the password profile."""

    def _install(self, client, error=None):
        recorder = Recorder()

        class UserItem:
            async def patch(self, body=None, request_configuration=None):
                recorder.calls.append(("users.patch", body))
                if error:
                    raise RuntimeError(error)

        client._client = type("Stub", (), {"users": type("U", (), {
            "by_user_id": staticmethod(lambda _id: UserItem())})()})()
        return recorder

    def test_the_password_is_written(self, client):
        recorder = self._install(client)
        client.set_password("u1", "N0vé-Heslo!")
        profile = recorder.calls[0][1].password_profile
        assert profile.password == "N0vé-Heslo!"

    def test_the_forced_change_is_written(self, client):
        recorder = self._install(client)
        client.set_password("u1", "x", force_change=True)
        assert recorder.calls[0][1].password_profile \
            .force_change_password_next_sign_in is True

    def test_an_empty_password_is_refused_before_any_request(self, client):
        recorder = self._install(client)
        with pytest.raises(M365Error, match="No password"):
            client.set_password("u1", "")
        assert recorder.calls == []

    def test_a_refusal_is_explained(self, client):
        self._install(client, error="Authorization_RequestDenied")
        with pytest.raises(M365Error, match="permission"):
            client.set_password("u1", "x")

    def test_the_password_method_id_is_the_documented_constant(self):
        assert PASSWORD_METHOD_ID == "28c10230-6103-485e-b985-444c60001490"


class TestUpdateUser:
    """Writing attributes onto an account."""

    def _install(self, client):
        recorder = Recorder()

        class UserItem:
            async def patch(self, body=None, request_configuration=None):
                recorder.calls.append(("users.patch", body))

        client._client = type("Stub", (), {"users": type("U", (), {
            "by_user_id": staticmethod(lambda _id: UserItem())})()})()
        return recorder

    def test_a_known_attribute_is_set_on_the_model(self, client):
        recorder = self._install(client)
        client.update_user("u1", {"displayName": "Jan Novák"})
        assert recorder.calls[0][1].display_name == "Jan Novák"

    def test_an_unknown_attribute_goes_through_additional_data(self, client):
        recorder = self._install(client)
        client.update_user("u1", {"extensionAttribute1": "6.A"})
        assert recorder.calls[0][1].additional_data == \
            {"extensionAttribute1": "6.A"}

    def test_nothing_to_change_sends_no_request(self, client):
        recorder = self._install(client)
        client.update_user("u1", {})
        assert recorder.calls == []


class TestLifetime:
    """The event loop and the credential have to be released."""

    def test_closing_twice_is_safe(self, client):
        client.close()
        client.close()

    def test_closing_marks_the_client_disconnected(self, client):
        client.close()
        assert client.is_connected is False

    def test_closing_a_client_that_never_connected_is_safe(self):
        M365Client(M365Credentials(method=AuthMethod.DEVICE_CODE)).close()

    def test_the_origin_information_carries_no_secret(self, client):
        client.credentials.client_secret = "s3cr3t-do-not-leak"
        assert "s3cr3t-do-not-leak" not in str(client.origin_info())

    def test_the_origin_information_names_the_tenant(self, client):
        client.tenant_id = "tenant-guid"
        client.tenant_domain = "skola.onmicrosoft.com"
        info = client.origin_info()
        assert info['tenant_id'] == "tenant-guid"
        assert info['tenant_domain'] == "skola.onmicrosoft.com"
