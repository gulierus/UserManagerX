"""
Unit tests for the Active Directory layer:
:mod:`services.ldap_compat`, :mod:`services.ad_client` and
:mod:`services.ad_group_service`.

No test ever opens a socket.  ``ADClient.connection`` is replaced by
:class:`FakeConnection`, a recorder that answers like an ``ldap3.Connection``
(``search``/``add``/``modify``/``delete``/``unbind``) and can be told to return
``False`` or to raise.  The three tests that exercise :meth:`ADClient.connect`
monkeypatch ``ldap3.Server`` / ``ldap3.Connection`` instead, because the real
classes are only imported inside the method.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect in the production code.
"""

import logging
from datetime import datetime

import pytest

from models import ADGroup, GroupTemplate, Person
from services import ldap_compat
from services.ad_client import ADClient, ADUserEntry
from services.ad_group_service import (
    ADGroupService,
    GroupDiscoveryResult,
    GroupTemplateManager,
)
from services.ldap_compat import (
    BASE,
    MODIFY_ADD,
    MODIFY_DELETE,
    MODIFY_REPLACE,
    SUBTREE,
    require_ldap3,
)

USER_DN = "CN=Jan Novák,OU=Students,DC=school,DC=local"
OU_DN = "OU=Students,DC=school,DC=local"
GROUP_A = "CN=Trida 6.A,OU=Groups,DC=school,DC=local"
GROUP_B = "CN=Žáci,OU=Groups,DC=school,DC=local"
GROUP_C = "CN=Teachers,OU=Groups,DC=school,DC=local"

DEFAULT_ATTRS = ["*", "modifyTimestamp", "createTimestamp"]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeAttr:
    """An ldap3-like attribute exposing both ``.value`` and ``.values``."""

    def __init__(self, value, values=None):
        self.value = value
        if values is not None:
            self.values = values
        elif isinstance(value, list):
            self.values = value
        elif value is None:
            self.values = []
        else:
            self.values = [value]


class ValueOnlyAttr:
    """Attribute object without a ``.values`` list (single-valued server)."""

    def __init__(self, value):
        self.value = value


class ExplodingAttr:
    """Attribute whose value cannot be read - simulates a decoding error."""

    @property
    def value(self):
        raise ValueError("cannot decode attribute")


class FakeEntry:
    """An ldap3-like entry: attribute access, ``entry_dn``, ``entry_attributes``."""

    def __init__(self, dn, **attributes):
        self.entry_dn = dn
        self.entry_attributes = list(attributes)
        self._attributes = {}
        for name, value in attributes.items():
            attr = value if isinstance(
                value, (FakeAttr, ValueOnlyAttr, ExplodingAttr)) else FakeAttr(value)
            self._attributes[name] = attr
            setattr(self, name, attr)

    def __getitem__(self, name):
        return self._attributes[name]


class FakeConnection:
    """
    Stand-in for ``ldap3.Connection`` that records every call.

    ``by_base`` maps a (case-insensitive) search base to the entries the server
    would return for it; without it every search yields ``entries``.
    """

    def __init__(self, entries=None, by_base=None):
        self.entries = list(entries or [])
        self.by_base = {key.lower(): list(value)
                        for key, value in (by_base or {}).items()}
        self.result = {"description": "insufficientAccessRights"}
        self.searches = []
        self.adds = []
        self.modifies = []
        self.deletes = []
        self.unbind_count = 0
        self.add_return = True
        self.modify_return = True
        self.delete_return = True
        self.search_error = None
        self.add_error = None
        self.modify_error = None
        self.delete_error = None
        self.unbind_error = None

    def search(self, search_base=None, search_filter=None, search_scope=None,
               attributes=None, **extra):
        call = {"search_base": search_base, "search_filter": search_filter,
                "search_scope": search_scope, "attributes": attributes}
        call.update(extra)
        self.searches.append(call)
        if self.search_error is not None:
            raise self.search_error
        if self.by_base:
            self.entries = list(self.by_base.get((search_base or "").lower(), []))
        return bool(self.entries)

    def add(self, dn, attributes=None, **extra):
        self.adds.append((dn, attributes))
        if self.add_error is not None:
            raise self.add_error
        return self.add_return

    def modify(self, dn, changes):
        self.modifies.append((dn, changes))
        if self.modify_error is not None:
            raise self.modify_error
        return self.modify_return

    def delete(self, dn):
        self.deletes.append(dn)
        if self.delete_error is not None:
            raise self.delete_error
        return self.delete_return

    def unbind(self):
        self.unbind_count += 1
        if self.unbind_error is not None:
            raise self.unbind_error


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

def make_client(connection=None):
    """An ADClient whose connection is the supplied fake (no network at all)."""
    client = ADClient("ldap://dc.school.local", "SCHOOL\\admin", "s3cret")
    client.connection = connection
    return client


def group_entry(name="Trida 6.A", dn=GROUP_A, description="Class group",
                group_type=-2147483646, members=()):
    """A FakeEntry shaped like an AD group object."""
    return FakeEntry(dn, cn=name, distinguishedName=dn, description=description,
                     groupType=group_type,
                     member=FakeAttr(list(members), list(members)))


@pytest.fixture
def conn():
    """A fresh fake LDAP connection."""
    return FakeConnection()


@pytest.fixture
def client(conn):
    """An ADClient already 'connected' to the fake connection."""
    return make_client(conn)


@pytest.fixture
def service(client):
    """An ADGroupService wired to the fake-backed client."""
    return ADGroupService(client)


@pytest.fixture
def fake_ldap3(monkeypatch):
    """Replace ``ldap3.Server``/``ldap3.Connection`` so connect() stays local."""
    import ldap3

    class Recorder:
        def __init__(self):
            self.servers = []
            self.connections = []
            self.server_error = None
            self.connection_error = None

    recorder = Recorder()

    class FakeServer:
        def __init__(self, host, get_info=None, **kwargs):
            if recorder.server_error is not None:
                raise recorder.server_error
            recorder.servers.append({"host": host, "get_info": get_info, **kwargs})
            self.host = host

    class FakeLdapConnection:
        def __init__(self, server, user=None, password=None, **kwargs):
            if recorder.connection_error is not None:
                raise recorder.connection_error
            call = {"server": server, "user": user, "password": password}
            call.update(kwargs)
            recorder.connections.append(call)
            self.server = server
            self.unbound = False

        def unbind(self):
            self.unbound = True

    monkeypatch.setattr(ldap3, "Server", FakeServer)
    monkeypatch.setattr(ldap3, "Connection", FakeLdapConnection)
    return recorder


# ---------------------------------------------------------------------------
# services.ldap_compat
# ---------------------------------------------------------------------------

class TestLdapCompat:
    """The compatibility shim must export the constants and gate operations."""

    @pytest.mark.parametrize("name", [
        "MODIFY_ADD", "MODIFY_DELETE", "MODIFY_REPLACE",
        "NTLM", "SUBTREE", "BASE", "ALL",
    ])
    def test_every_documented_constant_is_exported(self, name):
        """The shim re-exports each ldap3 constant the services import."""
        assert hasattr(ldap_compat, name)
        assert isinstance(getattr(ldap_compat, name), str)

    @pytest.mark.parametrize("name", [
        "MODIFY_ADD", "MODIFY_DELETE", "MODIFY_REPLACE",
        "NTLM", "SUBTREE", "BASE", "ALL",
    ])
    def test_constants_equal_the_real_ldap3_values(self, name):
        """With ldap3 installed the fallbacks must not shadow the real values."""
        ldap3 = pytest.importorskip("ldap3")
        assert getattr(ldap_compat, name) == getattr(ldap3, name)

    def test_availability_flag_is_true_and_import_error_none_when_installed(self):
        """ldap3 is part of the test environment, so the flag reports success."""
        pytest.importorskip("ldap3")
        assert ldap_compat.LDAP3_AVAILABLE is True
        assert ldap_compat.IMPORT_ERROR is None

    def test_require_ldap3_is_a_no_op_when_the_package_is_available(self):
        """Nothing is raised and nothing is returned when ldap3 is importable."""
        assert require_ldap3() is None

    def test_require_ldap3_raises_runtime_error_when_unavailable(self, monkeypatch):
        """Without ldap3 an AD operation fails with an actionable message."""
        monkeypatch.setattr(ldap_compat, "LDAP3_AVAILABLE", False)
        with pytest.raises(RuntimeError) as excinfo:
            require_ldap3()
        message = str(excinfo.value)
        assert "ldap3" in message
        assert "pip install ldap3" in message

    def test_require_ldap3_reads_the_flag_at_call_time_not_import_time(self, monkeypatch):
        """The already-imported reference honours a later flag change."""
        from services.ad_client import require_ldap3 as imported_into_client

        monkeypatch.setattr(ldap_compat, "LDAP3_AVAILABLE", False)
        with pytest.raises(RuntimeError):
            imported_into_client()
        monkeypatch.setattr(ldap_compat, "LDAP3_AVAILABLE", True)
        assert imported_into_client() is None


# ---------------------------------------------------------------------------
# ADClient - connection lifecycle
# ---------------------------------------------------------------------------

class TestADClientConnection:
    """connect / disconnect / context manager, never touching a real server."""

    def test_connect_binds_with_the_supplied_credentials_and_returns_true(self, fake_ldap3):
        """A successful bind stores the connection and reports success."""
        client = make_client()
        assert client.connect() is True
        assert client.connection is not None
        assert fake_ldap3.servers[0]["host"] == "ldap://dc.school.local"
        bind = fake_ldap3.connections[0]
        assert bind["user"] == "SCHOOL\\admin"
        assert bind["password"] == "s3cret"
        assert bind["auto_bind"] is True

    def test_connect_returns_false_and_leaves_connection_none_when_bind_fails(self, fake_ldap3):
        """Invalid credentials are swallowed into a False return value."""
        fake_ldap3.connection_error = RuntimeError("invalidCredentials")
        client = make_client()
        assert client.connect() is False
        assert client.connection is None

    def test_connect_returns_false_when_the_server_object_cannot_be_built(self, fake_ldap3):
        """A malformed server URL fails the same graceful way."""
        fake_ldap3.server_error = ValueError("bad host")
        client = make_client()
        assert client.connect() is False
        assert client.connection is None

    def test_connect_fails_fast_without_ldap3_and_never_builds_a_server(
            self, fake_ldap3, monkeypatch):
        """require_ldap3() aborts the bind before any ldap3 object is created."""
        monkeypatch.setattr(ldap_compat, "LDAP3_AVAILABLE", False)
        client = make_client()
        assert client.connect() is False
        assert client.connection is None
        assert fake_ldap3.servers == []
        assert fake_ldap3.connections == []

    def test_context_manager_connects_on_entry_and_unbinds_on_exit(self, fake_ldap3):
        """``with ADClient(...)`` binds once and always releases the handle."""
        client = ADClient("ldap://dc.school.local", "SCHOOL\\admin", "s3cret")
        with client as entered:
            assert entered is client
            assert client.connection is not None
            bound = client.connection
        assert bound.unbound is True
        assert client.connection is None

    def test_context_manager_yields_a_client_with_no_connection_when_bind_fails(
            self, fake_ldap3):
        """A failed bind does not raise - callers must test ``client.connection``."""
        fake_ldap3.connection_error = RuntimeError("server down")
        with ADClient("ldap://dc", "u", "p") as client:
            assert client.connection is None

    def test_context_manager_disconnects_even_when_the_body_raises(self, fake_ldap3):
        """An error inside the with-block propagates but still unbinds."""
        client = ADClient("ldap://dc.school.local", "SCHOOL\\admin", "s3cret")
        with pytest.raises(KeyError):
            with client:
                bound = client.connection
                raise KeyError("boom")
        assert bound.unbound is True
        assert client.connection is None

    def test_disconnect_without_a_connection_is_a_harmless_no_op(self):
        """Calling disconnect twice (or first) must not raise."""
        client = make_client()
        client.disconnect()
        client.disconnect()
        assert client.connection is None

    def test_disconnect_clears_the_handle_even_when_unbind_raises(self, conn, caplog):
        """A broken unbind is logged as a warning, never propagated."""
        conn.unbind_error = OSError("socket already closed")
        client = make_client(conn)
        with caplog.at_level(logging.WARNING, logger="services.ad_client"):
            client.disconnect()
        assert client.connection is None
        assert conn.unbind_count == 1
        assert any("socket already closed" in record.getMessage()
                   for record in caplog.records)


# ---------------------------------------------------------------------------
# ADUserEntry
# ---------------------------------------------------------------------------

class TestADUserEntry:
    """The tiny value object returned by get_user/search_users."""

    def test_get_returns_the_attribute_value_when_present(self):
        """Known attributes come straight out of the dictionary."""
        entry = ADUserEntry(dn=USER_DN, attributes={"sn": "Novák", "memberOf": []})
        assert entry.get("sn") == "Novák"
        assert entry.get("memberOf") == []

    @pytest.mark.parametrize("default", [None, "", "fallback", 0])
    def test_get_returns_the_default_for_an_unknown_attribute(self, default):
        """Missing attributes yield the caller's default, not a KeyError."""
        entry = ADUserEntry(dn=USER_DN, attributes={"sn": "Novák"})
        assert entry.get("homeDirectory", default) == default


# ---------------------------------------------------------------------------
# ADClient - create / delete
# ---------------------------------------------------------------------------

class TestADClientCreateUser:
    """create_user must inject the object classes and report server failures."""

    def test_create_user_adds_the_required_object_classes(self, client, conn):
        """The four AD user object classes are prepended to the attributes."""
        assert client.create_user(USER_DN, {"sAMAccountName": "novakjan"}) is True
        dn, attributes = conn.adds[0]
        assert dn == USER_DN
        assert attributes["objectClass"] == [
            "top", "person", "organizationalPerson", "user"]
        assert attributes["sAMAccountName"] == "novakjan"

    def test_create_user_lets_the_caller_override_the_object_classes(self, client, conn):
        """An explicit objectClass wins over the built-in default."""
        client.create_user(OU_DN, {"objectClass": ["organizationalUnit"]})
        assert conn.adds[0][1]["objectClass"] == ["organizationalUnit"]

    def test_create_user_preserves_unicode_attribute_values(self, client, conn):
        """Diacritics survive unchanged into the LDAP add call."""
        client.create_user(USER_DN, {"displayName": "Jan Ďurčovič", "sn": "Ďurčovič"})
        assert conn.adds[0][1]["displayName"] == "Jan Ďurčovič"

    def test_create_user_with_no_extra_attributes_still_sends_the_object_classes(
            self, client, conn):
        """An empty attribute mapping is valid and yields the bare user skeleton."""
        assert client.create_user(USER_DN, {}) is True
        assert conn.adds[0][1] == {
            "objectClass": ["top", "person", "organizationalPerson", "user"]}

    def test_create_user_returns_false_when_the_server_rejects_the_add(self, client, conn, caplog):
        """A False from ldap3 is returned and the server result is logged."""
        conn.add_return = False
        with caplog.at_level(logging.ERROR, logger="services.ad_client"):
            assert client.create_user(USER_DN, {"sn": "Novák"}) is False
        assert any("insufficientAccessRights" in record.getMessage()
                   for record in caplog.records)

    def test_create_user_returns_false_when_the_add_raises(self, client, conn):
        """An ldap3 exception is swallowed into a False result."""
        conn.add_error = RuntimeError("entryAlreadyExists")
        assert client.create_user(USER_DN, {"sn": "Novák"}) is False

    def test_create_user_returns_false_when_not_connected(self, conn):
        """Without a connection nothing is sent and False is returned."""
        client = make_client(None)
        assert client.create_user(USER_DN, {"sn": "Novák"}) is False
        assert conn.adds == []

    @pytest.mark.parametrize("bad", [None, "sn=Novak", 42, ["sn", "Novák"]])
    def test_create_user_returns_false_for_non_mapping_attributes(self, client, conn, bad):
        """Wrong-typed attributes fail cleanly instead of crashing the caller."""
        assert client.create_user(USER_DN, bad) is False
        assert conn.adds == []


class TestADClientDeleteUser:
    """delete_user passes the DN through and never raises."""

    def test_delete_user_sends_the_dn_and_returns_true(self, client, conn):
        """A successful delete reports True and records exactly one call."""
        assert client.delete_user(USER_DN) is True
        assert conn.deletes == [USER_DN]

    def test_delete_user_returns_false_when_the_server_refuses(self, client, conn):
        """A False from ldap3 is forwarded unchanged."""
        conn.delete_return = False
        assert client.delete_user(USER_DN) is False

    def test_delete_user_returns_false_when_the_delete_raises(self, client, conn):
        """noSuchObject and friends become a False result."""
        conn.delete_error = RuntimeError("noSuchObject")
        assert client.delete_user(USER_DN) is False

    def test_delete_user_returns_false_when_not_connected(self):
        """The RuntimeError raised for 'Not connected' is caught internally."""
        assert make_client(None).delete_user(USER_DN) is False


# ---------------------------------------------------------------------------
# ADClient - modify
# ---------------------------------------------------------------------------

class TestADClientModifyUser:
    """The (op, attribute, values) tuples are translated into ldap3 changes."""

    def test_modify_user_builds_the_ldap3_change_dictionary(self, client, conn):
        """Each operation becomes ``{attr: [(op, values)]}``."""
        ops = [(MODIFY_REPLACE, "sn", ["Novák"]),
               (MODIFY_REPLACE, "givenName", ["Jan"])]
        assert client.modify_user(USER_DN, ops) is True
        dn, changes = conn.modifies[0]
        assert dn == USER_DN
        assert changes == {"sn": [(MODIFY_REPLACE, ["Novák"])],
                           "givenName": [(MODIFY_REPLACE, ["Jan"])]}

    @pytest.mark.parametrize("raw, expected", [
        (["Novák"], ["Novák"]),
        ("Novák", ["Novák"]),
        (None, []),
        ([], []),
        (0, [0]),
        ("", [""]),
        (b"\x00", [b"\x00"]),
        (["a", "b"], ["a", "b"]),
    ])
    def test_modify_user_normalises_the_values_into_a_list(self, client, conn, raw, expected):
        """Scalars are wrapped, None becomes the empty (delete-all) list."""
        assert client.modify_user(USER_DN, [(MODIFY_REPLACE, "sn", raw)]) is True
        assert conn.modifies[0][1] == {"sn": [(MODIFY_REPLACE, expected)]}

    @pytest.mark.parametrize("malformed", [
        (MODIFY_REPLACE, "sn"),
        (MODIFY_REPLACE, "sn", ["a"], "extra"),
        (),
        ("only-one",),
    ])
    def test_modify_user_returns_false_when_every_operation_is_malformed(
            self, client, conn, malformed):
        """Tuples that are not 3-long are dropped; nothing is sent."""
        assert client.modify_user(USER_DN, [malformed]) is False
        assert conn.modifies == []

    def test_modify_user_applies_the_valid_operations_and_skips_the_broken_one(
            self, client, conn):
        """A malformed entry does not abort the rest of the batch."""
        ops = [(MODIFY_REPLACE, "sn", ["Novák"]), ("junk",)]
        assert client.modify_user(USER_DN, ops) is True
        assert conn.modifies[0][1] == {"sn": [(MODIFY_REPLACE, ["Novák"])]}

    def test_modify_user_returns_false_for_an_empty_operation_list(self, client, conn):
        """Nothing to change is reported as failure, and no call is made."""
        assert client.modify_user(USER_DN, []) is False
        assert conn.modifies == []

    def test_modify_user_returns_false_when_operations_is_not_iterable(self, client, conn):
        """Wrong-typed operations fail cleanly rather than raising."""
        assert client.modify_user(USER_DN, None) is False
        assert conn.modifies == []

    def test_modify_user_returns_false_when_the_server_rejects_the_change(
            self, client, conn, caplog):
        """The ldap3 result is logged and False is returned."""
        conn.modify_return = False
        with caplog.at_level(logging.ERROR, logger="services.ad_client"):
            assert client.modify_user(USER_DN, [(MODIFY_REPLACE, "sn", ["X"])]) is False
        assert any("insufficientAccessRights" in record.getMessage()
                   for record in caplog.records)

    def test_modify_user_returns_false_when_the_modify_raises(self, client, conn):
        """An ldap3 exception never escapes modify_user."""
        conn.modify_error = RuntimeError("constraintViolation")
        assert client.modify_user(USER_DN, [(MODIFY_REPLACE, "sn", ["X"])]) is False

    def test_modify_user_returns_false_when_not_connected(self):
        """'Not connected to AD' is caught and reported as False."""
        assert make_client(None).modify_user(USER_DN, [(MODIFY_REPLACE, "sn", ["X"])]) is False

    @pytest.mark.bug
    def test_modify_user_keeps_both_operations_on_the_same_attribute(self, client, conn):
        """Add+delete on one attribute must reach the server as two changes."""
        ops = [(MODIFY_ADD, "member", [GROUP_A]),
               (MODIFY_DELETE, "member", [GROUP_B])]
        assert client.modify_user(USER_DN, ops) is True
        assert conn.modifies[0][1]["member"] == [
            (MODIFY_ADD, [GROUP_A]), (MODIFY_DELETE, [GROUP_B])]


# ---------------------------------------------------------------------------
# ADClient - home directory
# ---------------------------------------------------------------------------

class TestADClientHomeDirectory:
    """set_home_directory / get_home_directory."""

    def test_set_home_directory_replaces_path_and_drive(self, client, conn):
        """Both attributes are replaced in a single modify call."""
        assert client.set_home_directory(USER_DN, r"\\srv\home\novakjan", "H:") is True
        dn, changes = conn.modifies[0]
        assert dn == USER_DN
        assert changes == {
            "homeDirectory": [(MODIFY_REPLACE, [r"\\srv\home\novakjan"])],
            "homeDrive": [(MODIFY_REPLACE, ["H:"])],
        }

    def test_set_home_directory_omits_the_drive_when_not_given(self, client, conn):
        """Only the path is touched when home_drive is None."""
        assert client.set_home_directory(USER_DN, r"\\srv\home\x") is True
        assert list(conn.modifies[0][1]) == ["homeDirectory"]

    def test_set_home_directory_can_set_only_the_drive_letter(self, client, conn):
        """An empty path with a drive letter still produces one change."""
        assert client.set_home_directory(USER_DN, "", "H:") is True
        assert list(conn.modifies[0][1]) == ["homeDrive"]

    @pytest.mark.parametrize("path, drive", [("", None), (None, None), ("", "")])
    def test_set_home_directory_returns_false_when_nothing_was_specified(
            self, client, conn, path, drive):
        """No path and no drive is a no-op that reports failure."""
        assert client.set_home_directory(USER_DN, path, drive) is False
        assert conn.modifies == []

    def test_set_home_directory_returns_false_when_the_modify_fails(self, client, conn):
        """The result of the underlying modify_user is passed through."""
        conn.modify_return = False
        assert client.set_home_directory(USER_DN, r"\\srv\home\x", "H:") is False

    def test_get_home_directory_returns_the_stored_path_and_drive(self, client, conn):
        """Both attributes are read back from the user entry."""
        conn.entries = [FakeEntry(USER_DN,
                                  homeDirectory=r"\\srv\home\novakjan",
                                  homeDrive="H:")]
        assert client.get_home_directory(USER_DN) == (r"\\srv\home\novakjan", "H:")

    def test_get_home_directory_returns_none_pair_for_an_unknown_user(self, client, conn):
        """A user that does not exist yields (None, None)."""
        conn.entries = []
        assert client.get_home_directory(USER_DN) == (None, None)

    def test_get_home_directory_returns_none_pair_when_attributes_are_absent(
            self, client, conn):
        """A user without a home directory yields (None, None), not a KeyError."""
        conn.entries = [FakeEntry(USER_DN, sn="Novák")]
        assert client.get_home_directory(USER_DN) == (None, None)

    def test_get_home_directory_requests_only_the_two_attributes(self, client, conn):
        """The read is narrowed to homeDirectory/homeDrive."""
        conn.entries = [FakeEntry(USER_DN, homeDirectory="x", homeDrive="H:")]
        client.get_home_directory(USER_DN)
        assert conn.searches[0]["attributes"] == ["homeDirectory", "homeDrive"]


# ---------------------------------------------------------------------------
# ADClient - reads
# ---------------------------------------------------------------------------

class TestADClientGetUser:
    """get_user maps an ldap3 entry onto ADUserEntry."""

    def test_get_user_searches_the_dn_with_base_scope_and_default_attributes(
            self, client, conn):
        """The lookup is a BASE-scoped (objectClass=user) search on the DN."""
        conn.entries = [FakeEntry(USER_DN, cn="Jan Novák")]
        client.get_user(USER_DN)
        call = conn.searches[0]
        assert call["search_base"] == USER_DN
        assert call["search_filter"] == "(objectClass=user)"
        assert call["search_scope"] == BASE
        assert call["attributes"] == DEFAULT_ATTRS

    def test_get_user_forwards_an_explicit_attribute_list(self, client, conn):
        """A caller-supplied attribute list is not expanded."""
        conn.entries = [FakeEntry(USER_DN, cn="Jan")]
        client.get_user(USER_DN, attributes=["cn"])
        assert conn.searches[0]["attributes"] == ["cn"]

    def test_get_user_keeps_an_empty_attribute_list_instead_of_expanding_it(
            self, client, conn):
        """Only ``None`` triggers the default list - ``[]`` is honoured as given."""
        conn.entries = [FakeEntry(USER_DN, cn="Jan")]
        client.get_user(USER_DN, attributes=[])
        assert conn.searches[0]["attributes"] == []

    def test_get_user_copies_every_attribute_and_the_servers_dn(self, client, conn):
        """The returned entry carries the server's canonical DN and all values."""
        conn.entries = [FakeEntry(USER_DN, cn="Jan Novák", sn="Novák",
                                  memberOf=FakeAttr([GROUP_A], [GROUP_A]))]
        entry = client.get_user("cn=jan novak,ou=students,dc=school,dc=local")
        assert isinstance(entry, ADUserEntry)
        assert entry.dn == USER_DN
        assert entry.attributes == {"cn": "Jan Novák", "sn": "Novák",
                                    "memberOf": [GROUP_A]}

    def test_get_user_returns_none_when_the_search_finds_nothing(self, client, conn):
        """An empty result set maps to None rather than an empty entry."""
        conn.entries = []
        assert client.get_user(USER_DN) is None

    def test_get_user_returns_none_when_the_search_raises(self, client, conn):
        """noSuchObject is swallowed into None."""
        conn.search_error = RuntimeError("noSuchObject")
        assert client.get_user(USER_DN) is None

    def test_get_user_returns_none_when_not_connected(self):
        """Without a connection the lookup reports None."""
        assert make_client(None).get_user(USER_DN) is None


class TestADClientSearchUsers:
    """search_users returns one ADUserEntry per hit, in server order."""

    def test_search_users_uses_subtree_scope_and_the_given_filter(self, client, conn):
        """Base DN, filter and SUBTREE scope are forwarded verbatim."""
        client.search_users(OU_DN, "(sAMAccountName=novakjan)")
        call = conn.searches[0]
        assert call["search_base"] == OU_DN
        assert call["search_filter"] == "(sAMAccountName=novakjan)"
        assert call["search_scope"] == SUBTREE
        assert call["attributes"] == DEFAULT_ATTRS

    def test_search_users_preserves_the_order_of_the_results(self, client, conn):
        """Entries are mapped one-to-one without reordering."""
        conn.entries = [FakeEntry(f"CN=User{i},{OU_DN}", cn=f"User{i}")
                        for i in range(3)]
        results = client.search_users(OU_DN, "(objectClass=user)")
        assert [r.dn for r in results] == [f"CN=User{i},{OU_DN}" for i in range(3)]
        assert [r.get("cn") for r in results] == ["User0", "User1", "User2"]

    def test_search_users_honours_an_explicit_empty_attribute_list(self, client, conn):
        """``attributes=[]`` means 'DN only' and is not replaced by the default."""
        client.search_users(OU_DN, "(objectClass=user)", attributes=[])
        assert conn.searches[0]["attributes"] == []

    def test_search_users_returns_an_empty_list_when_nothing_matches(self, client, conn):
        """No hits is an empty list, never None."""
        assert client.search_users(OU_DN, "(cn=nobody)") == []

    def test_search_users_returns_an_empty_list_when_the_search_raises(self, client, conn):
        """A bad filter is logged and reported as no results."""
        conn.search_error = RuntimeError("invalidFilter")
        assert client.search_users(OU_DN, "((broken") == []

    def test_search_users_returns_an_empty_list_when_not_connected(self):
        """Without a connection the search yields nothing."""
        assert make_client(None).search_users(OU_DN, "(objectClass=user)") == []


# ---------------------------------------------------------------------------
# ADClient - organizational units
# ---------------------------------------------------------------------------

class TestADClientOU:
    """create_ou / ou_exists."""

    def test_create_ou_sends_the_organizational_unit_object_class(self, client, conn):
        """The add carries objectClass=organizationalUnit and the ou name."""
        assert client.create_ou(OU_DN, "Students") is True
        dn, attributes = conn.adds[0]
        assert dn == OU_DN
        assert attributes == {"objectClass": ["organizationalUnit"], "ou": "Students"}

    def test_create_ou_keeps_diacritics_in_the_ou_name(self, client, conn):
        """A Czech OU name is passed through untouched."""
        client.create_ou("OU=Žáci,DC=school,DC=local", "Žáci")
        assert conn.adds[0][1]["ou"] == "Žáci"

    def test_create_ou_returns_false_when_the_server_refuses(self, client, conn):
        """A False result from ldap3 is forwarded."""
        conn.add_return = False
        assert client.create_ou(OU_DN, "Students") is False

    def test_create_ou_returns_false_when_the_add_raises(self, client, conn):
        """entryAlreadyExists becomes False."""
        conn.add_error = RuntimeError("entryAlreadyExists")
        assert client.create_ou(OU_DN, "Students") is False

    def test_create_ou_returns_false_when_not_connected(self):
        """No connection, no OU."""
        assert make_client(None).create_ou(OU_DN, "Students") is False

    def test_ou_exists_is_true_when_the_base_search_returns_an_entry(self, client, conn):
        """A single hit on the OU DN means the OU is there."""
        conn.entries = [FakeEntry(OU_DN, ou="Students")]
        assert client.ou_exists(OU_DN) is True
        call = conn.searches[0]
        assert call["search_filter"] == "(objectClass=organizationalUnit)"
        assert call["search_scope"] == BASE
        assert call["attributes"] == ["ou"]

    def test_ou_exists_is_false_when_the_search_is_empty(self, client, conn):
        """No entries means the OU does not exist."""
        assert client.ou_exists(OU_DN) is False

    def test_ou_exists_is_false_when_the_search_raises(self, client, conn):
        """A missing parent raises noSuchObject in ldap3 - reported as False."""
        conn.search_error = RuntimeError("noSuchObject")
        assert client.ou_exists(OU_DN) is False

    def test_ou_exists_is_false_when_not_connected(self):
        """Without a bind the OU cannot be confirmed."""
        assert make_client(None).ou_exists(OU_DN) is False


# ---------------------------------------------------------------------------
# ADGroupService - discovery
# ---------------------------------------------------------------------------

class TestDiscoverGroupsInOU:
    """discover_groups_in_ou turns ldap3 entries into ADGroup objects."""

    def test_discovery_maps_entries_onto_ad_groups(self, service, conn):
        """Name, DN, description, type and member count all come across."""
        conn.entries = [group_entry(members=[USER_DN, "CN=Other,DC=x"])]
        result = service.discover_groups_in_ou(OU_DN)
        assert isinstance(result, GroupDiscoveryResult)
        assert result.errors == []
        assert result.total_found == 1
        group = result.groups[0]
        assert isinstance(group, ADGroup)
        assert (group.name, group.dn) == ("Trida 6.A", GROUP_A)
        assert group.description == "Class group"
        assert group.group_type == "security"
        assert group.members_count == 2

    def test_discovery_searches_the_ou_with_subtree_scope(self, service, conn):
        """The whole subtree of the OU is scanned for group objects."""
        service.discover_groups_in_ou(OU_DN)
        call = conn.searches[0]
        assert call["search_base"] == OU_DN
        assert call["search_filter"] == "(objectClass=group)"
        assert call["search_scope"] == SUBTREE
        assert "member" in call["attributes"]

    def test_discovery_returns_an_empty_result_for_an_empty_ou(self, service, conn):
        """An OU without groups yields no groups and no errors."""
        result = service.discover_groups_in_ou(OU_DN)
        assert result.groups == []
        assert result.total_found == 0
        assert result.errors == []

    def test_discovery_skips_entries_without_a_distinguished_name(self, service, conn):
        """An entry the server returned without a DN cannot become a group."""
        conn.entries = [FakeEntry(GROUP_A, cn="Broken"), group_entry()]
        result = service.discover_groups_in_ou(OU_DN)
        assert [g.name for g in result.groups] == ["Trida 6.A"]
        assert result.total_found == 1

    def test_discovery_falls_back_to_unknown_when_the_cn_is_missing(self, service, conn):
        """A group object without a cn is still reported, named 'Unknown'."""
        conn.entries = [FakeEntry(GROUP_A, distinguishedName=GROUP_A)]
        group = service.discover_groups_in_ou(OU_DN).groups[0]
        assert group.name == "Unknown"
        assert group.description is None
        assert group.group_type == "unknown"
        assert group.members_count == 0

    def test_discovery_reports_a_broken_entry_but_keeps_the_others(self, service, conn):
        """One undecodable entry becomes an error string, not a lost batch."""
        conn.entries = [FakeEntry(GROUP_B, cn=ExplodingAttr(),
                                  distinguishedName=GROUP_B),
                        group_entry()]
        result = service.discover_groups_in_ou(OU_DN)
        assert [g.dn for g in result.groups] == [GROUP_A]
        assert len(result.errors) == 1
        assert "cannot decode attribute" in result.errors[0]

    def test_discovery_counts_zero_members_when_the_attribute_has_no_values(
            self, service, conn):
        """A single-valued member attribute without ``.values`` counts as zero."""
        entry = FakeEntry(GROUP_A, cn="Trida 6.A", distinguishedName=GROUP_A,
                          member=ValueOnlyAttr(USER_DN))
        conn.entries = [entry]
        assert service.discover_groups_in_ou(OU_DN).groups[0].members_count == 0

    @pytest.mark.parametrize("group_type_value, expected", [
        (-2147483646, "security"),
        (-2147483644, "security"),
        (-2147483640, "security"),
        (2, "distribution"),
        (8, "distribution"),
        (0, "distribution"),
        ("-2147483646", "unknown"),
        (None, "unknown"),
    ])
    def test_discovery_classifies_the_group_type_from_the_flags(
            self, service, conn, group_type_value, expected):
        """Negative flags mean security groups, non-negative distribution."""
        conn.entries = [group_entry(group_type=group_type_value)]
        assert service.discover_groups_in_ou(OU_DN).groups[0].group_type == expected

    def test_discovery_reports_an_error_when_not_connected(self, conn):
        """Discovery without a bind fails with a recorded error message."""
        result = ADGroupService(make_client(None)).discover_groups_in_ou(OU_DN)
        assert result.groups == []
        assert result.total_found == 0
        assert result.errors == ["Discovery failed: Not connected to AD"]

    def test_discovery_reports_an_error_when_the_search_raises(self, service, conn):
        """An LDAP error is captured in errors instead of propagating."""
        conn.search_error = RuntimeError("sizeLimitExceeded")
        result = service.discover_groups_in_ou(OU_DN)
        assert result.groups == []
        assert result.errors == ["Discovery failed: sizeLimitExceeded"]

    def test_discovery_refuses_to_search_without_ldap3(self, service, conn, monkeypatch):
        """require_ldap3 aborts the search and the message names the fix."""
        monkeypatch.setattr(ldap_compat, "LDAP3_AVAILABLE", False)
        result = service.discover_groups_in_ou(OU_DN)
        assert conn.searches == []
        assert result.groups == []
        assert "pip install ldap3" in result.errors[0]


# ---------------------------------------------------------------------------
# ADGroupService - single group lookup
# ---------------------------------------------------------------------------

class TestGetGroupByDN:
    """get_group_by_dn found / not-found / error."""

    def test_get_group_by_dn_returns_the_mapped_group(self, service, conn):
        """A hit is converted into a fully populated ADGroup."""
        conn.entries = [group_entry(members=[USER_DN])]
        group = service.get_group_by_dn(GROUP_A)
        assert group.name == "Trida 6.A"
        assert group.dn == GROUP_A
        assert group.members_count == 1
        call = conn.searches[0]
        assert call["search_base"] == GROUP_A
        assert call["search_filter"] == "(objectClass=group)"
        assert call["search_scope"] == BASE

    def test_get_group_by_dn_falls_back_to_the_requested_dn(self, service, conn):
        """Without a distinguishedName attribute the requested DN is kept."""
        conn.entries = [FakeEntry(GROUP_B, cn="Žáci")]
        group = service.get_group_by_dn(GROUP_B)
        assert group.dn == GROUP_B
        assert group.name == "Žáci"

    def test_get_group_by_dn_returns_none_when_the_group_is_missing(self, service, conn):
        """An empty result set maps to None."""
        assert service.get_group_by_dn(GROUP_A) is None

    def test_get_group_by_dn_returns_none_when_the_search_raises(self, service, conn):
        """LDAP errors never escape the service."""
        conn.search_error = RuntimeError("noSuchObject")
        assert service.get_group_by_dn(GROUP_A) is None

    def test_get_group_by_dn_returns_none_when_not_connected(self):
        """No bind means no group."""
        assert ADGroupService(make_client(None)).get_group_by_dn(GROUP_A) is None


class TestVerifyGroupsExist:
    """verify_groups_exist maps each DN onto a boolean."""

    def test_verify_groups_exist_reports_per_group_existence(self, service, conn):
        """Only the DNs the server answers for are marked True."""
        conn.by_base = {GROUP_A.lower(): [group_entry()], GROUP_B.lower(): []}
        groups = [ADGroup(name="Trida 6.A", dn=GROUP_A), ADGroup(name="Žáci", dn=GROUP_B)]
        assert service.verify_groups_exist(groups) == {GROUP_A: True, GROUP_B: False}

    def test_verify_groups_exist_returns_an_empty_map_for_an_empty_list(self, service):
        """Nothing to verify is an empty dictionary."""
        assert service.verify_groups_exist([]) == {}

    def test_verify_groups_exist_collapses_duplicate_dns_into_one_key(self, service, conn):
        """The same DN twice produces a single map entry."""
        conn.by_base = {GROUP_A.lower(): [group_entry()]}
        duplicates = [ADGroup(name="A", dn=GROUP_A), ADGroup(name="A again", dn=GROUP_A)]
        assert service.verify_groups_exist(duplicates) == {GROUP_A: True}

    def test_verify_groups_exist_marks_everything_false_when_not_connected(self):
        """Without a bind no group can be confirmed."""
        service = ADGroupService(make_client(None))
        groups = [ADGroup(name="A", dn=GROUP_A)]
        assert service.verify_groups_exist(groups) == {GROUP_A: False}


# ---------------------------------------------------------------------------
# ADGroupService - membership
# ---------------------------------------------------------------------------

class TestGroupMembership:
    """add_user_to_group / remove_user_from_group / get_user_groups."""

    def test_add_user_to_group_modifies_the_group_member_attribute(self, service, conn):
        """The member is added on the *group* object, not on the user."""
        assert service.add_user_to_group(USER_DN, GROUP_A) is True
        assert conn.modifies == [(GROUP_A, {"member": [(MODIFY_ADD, [USER_DN])]})]

    def test_remove_user_from_group_deletes_the_member_value(self, service, conn):
        """Removal uses MODIFY_DELETE with the user DN."""
        assert service.remove_user_from_group(USER_DN, GROUP_A) is True
        assert conn.modifies == [(GROUP_A, {"member": [(MODIFY_DELETE, [USER_DN])]})]

    @pytest.mark.parametrize("method", ["add_user_to_group", "remove_user_from_group"])
    def test_membership_change_returns_false_when_the_server_refuses(
            self, service, conn, method):
        """entryAlreadyExists / noSuchAttribute surface as False."""
        conn.modify_return = False
        assert getattr(service, method)(USER_DN, GROUP_A) is False

    @pytest.mark.parametrize("method", ["add_user_to_group", "remove_user_from_group"])
    def test_membership_change_returns_false_when_the_modify_raises(
            self, service, conn, method):
        """An exception from ldap3 is swallowed into False."""
        conn.modify_error = RuntimeError("insufficientAccessRights")
        assert getattr(service, method)(USER_DN, GROUP_A) is False

    @pytest.mark.parametrize("method", ["add_user_to_group", "remove_user_from_group"])
    def test_membership_change_returns_false_when_not_connected(self, method):
        """Without a bind nothing changes."""
        service = ADGroupService(make_client(None))
        assert getattr(service, method)(USER_DN, GROUP_A) is False

    def test_get_user_groups_resolves_every_member_of_entry(self, service, conn):
        """Each memberOf DN is expanded into a full ADGroup."""
        conn.by_base = {
            USER_DN.lower(): [FakeEntry(USER_DN,
                                        memberOf=FakeAttr([GROUP_A, GROUP_B],
                                                          [GROUP_A, GROUP_B]))],
            GROUP_A.lower(): [group_entry("Trida 6.A", GROUP_A)],
            GROUP_B.lower(): [group_entry("Žáci", GROUP_B)],
        }
        groups = service.get_user_groups(USER_DN)
        assert [g.name for g in groups] == ["Trida 6.A", "Žáci"]
        assert conn.searches[0]["attributes"] == ["memberOf"]

    def test_get_user_groups_drops_memberships_that_cannot_be_resolved(self, service, conn):
        """A dangling memberOf DN is skipped rather than faked."""
        conn.by_base = {
            USER_DN.lower(): [FakeEntry(USER_DN,
                                        memberOf=FakeAttr([GROUP_A, GROUP_C],
                                                          [GROUP_A, GROUP_C]))],
            GROUP_A.lower(): [group_entry("Trida 6.A", GROUP_A)],
            GROUP_C.lower(): [],
        }
        assert [g.dn for g in service.get_user_groups(USER_DN)] == [GROUP_A]

    def test_get_user_groups_returns_an_empty_list_for_an_unknown_user(self, service, conn):
        """A user that is not in AD has no groups."""
        conn.by_base = {"x": []}
        assert service.get_user_groups(USER_DN) == []

    def test_get_user_groups_returns_an_empty_list_without_member_of(self, service, conn):
        """A user with no memberOf attribute belongs to nothing."""
        conn.entries = [FakeEntry(USER_DN, cn="Jan Novák")]
        assert service.get_user_groups(USER_DN) == []

    def test_get_user_groups_returns_an_empty_list_when_the_search_raises(self, service, conn):
        """LDAP failures are logged and reported as no memberships."""
        conn.search_error = RuntimeError("operationsError")
        assert service.get_user_groups(USER_DN) == []


# ---------------------------------------------------------------------------
# ADGroupService - synchronisation
# ---------------------------------------------------------------------------

def person_with(groups, ad_dn=USER_DN):
    """A person carrying the desired group memberships."""
    return Person("Jan", "Novák", "6.A", ad_username="novakjan",
                  ad_dn=ad_dn, group_memberships=list(groups))


def sync_connection(current_group_dns):
    """A fake connection where the user is a member of *current_group_dns*."""
    by_base = {USER_DN: [FakeEntry(USER_DN,
                                   memberOf=FakeAttr(list(current_group_dns),
                                                     list(current_group_dns)))]}
    for dn in current_group_dns:
        by_base[dn] = [group_entry(dn.split(",")[0][3:], dn)]
    return FakeConnection(by_base=by_base)


@pytest.mark.integration
class TestSyncUserGroups:
    """sync_user_groups computes the add/remove difference against AD."""

    def test_sync_adds_the_missing_group_and_removes_the_stale_one(self):
        """Desired minus current is added, current minus desired removed."""
        conn = sync_connection([GROUP_A, GROUP_B])
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Žáci", dn=GROUP_B),
                              ADGroup(name="Teachers", dn=GROUP_C)])
        assert service.sync_user_groups(person) is True
        assert (GROUP_C, {"member": [(MODIFY_ADD, [USER_DN])]}) in conn.modifies
        assert (GROUP_A, {"member": [(MODIFY_DELETE, [USER_DN])]}) in conn.modifies
        assert len(conn.modifies) == 2

    def test_sync_is_idempotent_when_ad_already_matches(self):
        """No difference means no modify call at all."""
        conn = sync_connection([GROUP_A])
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Trida 6.A", dn=GROUP_A)])
        assert service.sync_user_groups(person) is True
        assert conn.modifies == []

    def test_sync_compares_distinguished_names_case_insensitively(self):
        """A DN that differs only in case is the same membership."""
        conn = sync_connection([GROUP_A])
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Trida 6.A", dn=GROUP_A.upper())])
        assert service.sync_user_groups(person) is True
        assert conn.modifies == []

    def test_sync_removes_every_group_when_the_person_wants_none(self):
        """An empty desired set clears the memberships."""
        conn = sync_connection([GROUP_A, GROUP_B])
        service = ADGroupService(make_client(conn))
        assert service.sync_user_groups(person_with([])) is True
        assert sorted(dn for dn, _ in conn.modifies) == sorted([GROUP_A, GROUP_B])
        assert all(changes == {"member": [(MODIFY_DELETE, [USER_DN])]}
                   for _, changes in conn.modifies)

    def test_sync_returns_false_but_still_attempts_every_change(self):
        """One rejected membership change fails the sync without aborting it."""
        conn = sync_connection([GROUP_A])
        conn.modify_return = False
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Teachers", dn=GROUP_C)])
        assert service.sync_user_groups(person) is False
        assert sorted(dn for dn, _ in conn.modifies) == sorted([GROUP_A, GROUP_C])

    @pytest.mark.parametrize("ad_dn", [None, ""])
    def test_sync_refuses_a_person_without_a_distinguished_name(self, ad_dn):
        """A person that is not in AD yet cannot have groups synchronised."""
        conn = sync_connection([GROUP_A])
        service = ADGroupService(make_client(conn))
        assert service.sync_user_groups(person_with([], ad_dn=ad_dn)) is False
        assert conn.modifies == []
        assert conn.searches == []

    def test_sync_returns_false_when_a_desired_group_has_no_dn(self):
        """A malformed ADGroup aborts the sync instead of crashing."""
        conn = sync_connection([GROUP_A])
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Broken", dn=None)])
        assert service.sync_user_groups(person) is False
        assert conn.modifies == []

    def test_sync_treats_an_unreadable_ad_state_as_no_current_groups(self):
        """When the memberOf lookup fails every desired group is (re)added."""
        conn = sync_connection([GROUP_A])
        conn.search_error = RuntimeError("operationsError")
        service = ADGroupService(make_client(conn))
        person = person_with([ADGroup(name="Teachers", dn=GROUP_C)])
        assert service.sync_user_groups(person) is True
        assert [dn for dn, _ in conn.modifies] == [GROUP_C]

    def test_sync_without_a_connection_reports_failure_for_desired_groups(self):
        """No bind: the adds fail, so the sync reports failure."""
        service = ADGroupService(make_client(None))
        person = person_with([ADGroup(name="Teachers", dn=GROUP_C)])
        assert service.sync_user_groups(person) is False


# ---------------------------------------------------------------------------
# GroupTemplateManager
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """An empty GroupTemplateManager."""
    return GroupTemplateManager()


def template(name="Teachers", groups=None, description="Staff groups"):
    """A GroupTemplate with one group by default."""
    if groups is None:
        groups = [ADGroup(name="Trida 6.A", dn=GROUP_A)]
    return GroupTemplate(name=name, description=description, groups=list(groups))


class TestGroupTemplateManager:
    """add / remove / get / get_all / exists / rename / copy."""

    def test_a_new_manager_holds_no_templates(self, manager):
        """The manager starts empty."""
        assert manager.get_all_templates() == []
        assert manager.get_template("Teachers") is None
        assert manager.template_exists("Teachers") is False

    def test_add_template_stores_the_template_and_reports_success(self, manager):
        """The very same object is kept and found again by name."""
        item = template()
        assert manager.add_template(item) is True
        assert manager.template_exists("Teachers") is True
        assert manager.get_template("Teachers") is item
        assert manager.get_all_templates() == [item]

    def test_add_template_rejects_a_duplicate_name_and_keeps_the_original(self, manager):
        """Names are unique; the second add is refused."""
        first = template()
        manager.add_template(first)
        assert manager.add_template(template(description="other")) is False
        assert manager.get_all_templates() == [first]

    def test_template_names_are_case_sensitive(self, manager):
        """'Teachers' and 'teachers' are two different templates."""
        assert manager.add_template(template("Teachers")) is True
        assert manager.add_template(template("teachers")) is True
        assert manager.template_exists("TEACHERS") is False
        assert len(manager.get_all_templates()) == 2

    def test_template_names_keep_their_diacritics(self, manager):
        """A Czech template name round-trips through exists/get."""
        manager.add_template(template("Učitelé"))
        assert manager.template_exists("Učitelé") is True
        assert manager.template_exists("Ucitele") is False
        assert manager.get_template("Učitelé").name == "Učitelé"

    def test_remove_template_deletes_only_the_named_template(self, manager):
        """The other templates are untouched."""
        manager.add_template(template("Teachers"))
        manager.add_template(template("Students"))
        assert manager.remove_template("Teachers") is True
        assert manager.get_template("Teachers") is None
        assert [t.name for t in manager.get_all_templates()] == ["Students"]

    @pytest.mark.parametrize("missing", ["Nope", "", "teachers"])
    def test_remove_template_reports_false_for_an_unknown_name(self, manager, missing):
        """Removing something that is not there changes nothing."""
        manager.add_template(template("Teachers"))
        assert manager.remove_template(missing) is False
        assert len(manager.get_all_templates()) == 1

    def test_remove_template_is_idempotent(self, manager):
        """The second removal simply reports False."""
        manager.add_template(template())
        assert manager.remove_template("Teachers") is True
        assert manager.remove_template("Teachers") is False

    def test_get_all_templates_returns_a_defensive_copy(self, manager):
        """Mutating the returned list must not corrupt the manager."""
        manager.add_template(template())
        snapshot = manager.get_all_templates()
        snapshot.append(template("Injected"))
        snapshot.clear()
        assert len(manager.get_all_templates()) == 1
        assert manager.template_exists("Injected") is False

    def test_rename_template_updates_the_name_and_the_modified_date(self, manager):
        """Renaming works (it used to raise NameError: datetime) and stamps the date."""
        item = template()
        item.modified_date = datetime(2020, 1, 1, 12, 0, 0)
        manager.add_template(item)
        assert manager.rename_template("Teachers", "Staff") is True
        assert item.name == "Staff"
        assert manager.template_exists("Staff") is True
        assert manager.template_exists("Teachers") is False
        assert manager.get_template("Staff") is item
        assert item.modified_date > datetime(2020, 1, 1, 12, 0, 0)

    def test_rename_template_keeps_the_groups_untouched(self, manager):
        """Only the identity changes, not the payload."""
        groups = [ADGroup(name="Trida 6.A", dn=GROUP_A),
                  ADGroup(name="Žáci", dn=GROUP_B)]
        item = template(groups=groups)
        manager.add_template(item)
        manager.rename_template("Teachers", "Učitelé")
        assert [g.dn for g in manager.get_template("Učitelé").groups] == [GROUP_A, GROUP_B]

    def test_rename_template_refuses_to_overwrite_an_existing_name(self, manager):
        """A collision leaves both templates as they were."""
        first = template("Teachers")
        second = template("Students")
        manager.add_template(first)
        manager.add_template(second)
        assert manager.rename_template("Teachers", "Students") is False
        assert first.name == "Teachers"
        assert [t.name for t in manager.get_all_templates()] == ["Teachers", "Students"]

    def test_rename_template_reports_false_for_an_unknown_template(self, manager):
        """Renaming something that does not exist adds nothing."""
        assert manager.rename_template("Ghost", "Staff") is False
        assert manager.get_all_templates() == []

    def test_rename_template_to_the_same_name_is_refused_as_a_collision(self, manager):
        """The uniqueness check fires before the lookup, so a no-op rename fails."""
        item = template()
        manager.add_template(item)
        assert manager.rename_template("Teachers", "Teachers") is False
        assert item.name == "Teachers"

    def test_copy_template_registers_an_independent_duplicate(self, manager):
        """The copy carries the description and groups under the new name."""
        original = template(groups=[ADGroup(name="Trida 6.A", dn=GROUP_A)])
        manager.add_template(original)
        copy = manager.copy_template("Teachers", "Teachers 2025")
        assert copy is not original
        assert copy.name == "Teachers 2025"
        assert copy.description == original.description
        assert [g.dn for g in copy.groups] == [GROUP_A]
        assert manager.get_template("Teachers 2025") is copy
        assert len(manager.get_all_templates()) == 2

    def test_copy_template_gives_the_duplicate_its_own_group_list(self, manager):
        """Adding a group to the copy must not change the original template."""
        original = template(groups=[ADGroup(name="Trida 6.A", dn=GROUP_A)])
        manager.add_template(original)
        copy = manager.copy_template("Teachers", "Teachers 2025")
        copy.add_group(ADGroup(name="Žáci", dn=GROUP_B))
        assert [g.dn for g in original.groups] == [GROUP_A]
        assert [g.dn for g in copy.groups] == [GROUP_A, GROUP_B]

    def test_copy_template_stamps_fresh_dates_on_the_duplicate(self, manager):
        """The copy is a new template, so it gets its own timestamps."""
        original = template()
        original.created_date = datetime(2019, 9, 1)
        manager.add_template(original)
        copy = manager.copy_template("Teachers", "Teachers 2025")
        assert copy.created_date is not None
        assert copy.created_date > datetime(2019, 9, 1)

    def test_copy_template_refuses_an_existing_target_name(self, manager):
        """A name collision returns None and adds nothing."""
        manager.add_template(template("Teachers"))
        manager.add_template(template("Students"))
        assert manager.copy_template("Teachers", "Students") is None
        assert len(manager.get_all_templates()) == 2

    def test_copy_template_returns_none_for_an_unknown_source(self, manager):
        """Copying a template that does not exist is a no-op."""
        assert manager.copy_template("Ghost", "Ghost 2") is None
        assert manager.get_all_templates() == []
