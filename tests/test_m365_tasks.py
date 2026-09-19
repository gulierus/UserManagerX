"""
Tests for the Microsoft 365 background tasks (Version 26, point 21).

Every Graph request is a network round trip, so all of them run in a worker
thread behind the progress dialog - the machinery of points 13 and 25.
"""

import pytest

from models_m365 import M365Group
from services.m365_auth import AuthMethod, M365Credentials
from services.m365_client import M365Error
from utils.m365_tasks import (
    M365ConnectTask, M365LoadGroupsTask, M365RefreshGroupTask,
)
from utils.progress_tasks import LogLevel

pytestmark = pytest.mark.gui


class FakeClient:
    """Stands in for a connected M365Client."""

    def __init__(self, groups=None, unreadable=()):
        self.groups = list(groups or [])
        self.unreadable = set(unreadable)
        self.tenant_id = "tenant-guid"
        self.tenant_domain = "skola.onmicrosoft.com"
        self.signed_in_as = "admin@skola.cz"
        self.closed = False
        self.loaded = []

    def list_groups(self, progress=None):
        if progress is not None:
            progress(len(self.groups))
        return list(self.groups)

    def load_group_people(self, group):
        if group.display_name in self.unreadable:
            raise M365Error(f"cannot read {group.display_name}")
        group.members_loaded = True
        self.loaded.append(group.display_name)
        return group

    def origin_info(self):
        return {'tenant_id': self.tenant_id,
                'tenant_domain': self.tenant_domain,
                'signed_in_as': self.signed_in_as}

    def close(self):
        self.closed = True


@pytest.fixture
def credentials():
    return M365Credentials(method=AuthMethod.APP_ONLY, tenant_id="t",
                           client_id="c", client_secret="s")


def install_client(monkeypatch, client):
    """Make every task's open_client() return *client* instead of signing in."""
    from utils import m365_tasks

    def fake_open_client(self, credentials, device_code_callback=None):
        self.emit_log("Signed in", LogLevel.SUCCESS)
        return client

    monkeypatch.setattr(m365_tasks.M365TaskMixin, "open_client",
                        fake_open_client)
    return client


def run(task):
    """
    Run a task synchronously, the way the progress dialog would.

    The finishing message is captured from the ``task_finished`` signal - the
    base class reports it that way rather than storing it, so a test that wants
    it has to listen.
    """
    messages = []
    task.task_finished.connect(
        lambda _ok, _when, message: messages.append(message))
    task.run()
    task.finish_message = messages[-1] if messages else ""
    return task


class TestConnectTask:
    """Pressing "Connect"."""

    def test_it_records_who_we_are(self, credentials, monkeypatch):
        install_client(monkeypatch, FakeClient())
        task = run(M365ConnectTask(credentials))
        assert task.signed_in_as == "admin@skola.cz"
        assert task.tenant_domain == "skola.onmicrosoft.com"

    def test_the_result_message_names_the_tenant_and_the_account(
            self, credentials, monkeypatch):
        install_client(monkeypatch, FakeClient())
        task = run(M365ConnectTask(credentials))
        assert "skola.onmicrosoft.com" in task.finish_message
        assert "admin@skola.cz" in task.finish_message

    def test_the_connected_client_is_handed_to_the_caller(self, credentials,
                                                          monkeypatch):
        client = install_client(monkeypatch, FakeClient())
        task = run(M365ConnectTask(credentials))
        assert task.client is client and client.closed is False

    def test_a_failure_is_reported_not_raised(self, credentials, monkeypatch):
        from utils import m365_tasks

        def exploding(self, credentials, device_code_callback=None):
            raise M365Error("AADSTS50076")

        monkeypatch.setattr(m365_tasks.M365TaskMixin, "open_client", exploding)
        task = run(M365ConnectTask(credentials))
        assert task.client is None


class TestLoadGroupsTask:
    """Reading the tenant."""

    @pytest.fixture
    def groups(self):
        return [M365Group(object_id="g1", display_name="Trida-6.A"),
                M365Group(object_id="g2", display_name="Trida-7.B")]

    def test_every_group_comes_back(self, credentials, groups, monkeypatch):
        install_client(monkeypatch, FakeClient(groups))
        task = run(M365LoadGroupsTask(credentials))
        assert [g.display_name for g in task.groups] == \
            ["Trida-6.A", "Trida-7.B"]

    def test_the_people_are_read_for_every_group(self, credentials, groups,
                                                 monkeypatch):
        client = install_client(monkeypatch, FakeClient(groups))
        run(M365LoadGroupsTask(credentials))
        assert client.loaded == ["Trida-6.A", "Trida-7.B"]

    def test_the_people_can_be_left_unread(self, credentials, groups,
                                           monkeypatch):
        client = install_client(monkeypatch, FakeClient(groups))
        run(M365LoadGroupsTask(credentials, load_people=False))
        assert client.loaded == []

    def test_the_origin_information_is_captured(self, credentials, groups,
                                                monkeypatch):
        install_client(monkeypatch, FakeClient(groups))
        task = run(M365LoadGroupsTask(credentials))
        assert task.origin_info['tenant_domain'] == "skola.onmicrosoft.com"

    def test_one_unreadable_group_does_not_lose_the_others(
            self, credentials, groups, monkeypatch):
        install_client(monkeypatch,
                       FakeClient(groups, unreadable={"Trida-6.A"}))
        task = run(M365LoadGroupsTask(credentials))
        assert task.unreadable == ["Trida-6.A"]
        assert len(task.groups) == 2

    def test_the_summary_mentions_the_unreadable_groups(self, credentials,
                                                        groups, monkeypatch):
        install_client(monkeypatch,
                       FakeClient(groups, unreadable={"Trida-6.A"}))
        task = run(M365LoadGroupsTask(credentials))
        assert "could not be read" in task.finish_message

    def test_an_empty_tenant_is_not_an_error(self, credentials, monkeypatch):
        install_client(monkeypatch, FakeClient([]))
        task = run(M365LoadGroupsTask(credentials))
        assert task.groups == []


class TestRefreshGroupTask:
    """Re-reading a handful of groups."""

    def test_the_requested_groups_are_read(self, credentials, monkeypatch):
        groups = [M365Group(object_id="g1", display_name="Trida-6.A")]
        client = install_client(monkeypatch, FakeClient(groups))
        run(M365RefreshGroupTask(credentials, groups))
        assert client.loaded == ["Trida-6.A"]

    def test_an_unreadable_group_is_reported(self, credentials, monkeypatch):
        groups = [M365Group(object_id="g1", display_name="Trida-6.A")]
        install_client(monkeypatch,
                       FakeClient(groups, unreadable={"Trida-6.A"}))
        task = run(M365RefreshGroupTask(credentials, groups))
        assert task.unreadable == ["Trida-6.A"]


class TestDeviceCodePrompt:
    """The device code has to reach the user."""

    def test_the_code_is_logged_where_the_user_can_see_it(self, credentials):
        # Without this the SDK prints it to a console nobody is watching and
        # the sign-in looks like a hang.
        task = M365ConnectTask(credentials)
        seen = []
        task.log_message.connect(lambda text, *_: seen.append(text))

        task.device_code_prompt("https://microsoft.com/devicelogin", "ABC-123",
                                None)

        assert any("ABC-123" in text for text in seen)
        assert any("devicelogin" in text for text in seen)
