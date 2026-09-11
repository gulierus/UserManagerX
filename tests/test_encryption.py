"""
Unit tests for :mod:`utils.encryption` and :mod:`utils.usrx_format`.

Scope
-----
* the USRX binary container: write/read round trip, header parsing, and the
  ways a container can be damaged (wrong magic, truncated header, truncated
  data, unreadable metadata);
* AES-GCM encryption in both the *standard* (JSON) and the *usrx* container
  layout, including wrong passwords and tampered ciphertext;
* format/method auto-detection and the informational helpers;
* the graceful-degradation branches for GPG.  ``python-gnupg`` is deliberately
  **not** installed here, so the "not available" paths are asserted for real and
  the plumbing around them is exercised with an injected stub module instead of
  a real GnuPG binary.

Neither module under test touches Qt, the network or the user's settings, so no
dialog fixture is required; every file written by these tests lives in
``tmp_path``.
"""

import base64
import importlib.util
import json
import logging
import struct
import sys
import types
from pathlib import Path

import pytest

from utils import encryption as enc
from utils import usrx_format as usrx
from utils.usrx_format import USRXFile, USRXFormatError

# --------------------------------------------------------------------------
# constants & helpers
# --------------------------------------------------------------------------

PASSWORD = "correct horse battery staple"
SECRET = '{"class": "6.A", "user": "novakjan"}'
UNICODE_SECRET = "Žluťoučký kůň úpěl ďábelské ódy — 6.A / IX. 🎓"

GNUPG_INSTALLED = importlib.util.find_spec("gnupg") is not None
needs_no_gnupg = pytest.mark.skipif(
    GNUPG_INSTALLED, reason="python-gnupg is installed in this environment"
)

_DELETE = object()

FIXED_FIELDS = 20  # magic(4) + version(2) + type(1) + flags(1) + hsize(4) + dsize(8)


def _aes_variant(sample: Path, tmp_path: Path, name: str, **changes) -> Path:
    """Copy the sample AES JSON file, applying ``changes`` (``_DELETE`` removes)."""
    payload = json.loads(sample.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is _DELETE:
            payload.pop(key, None)
        else:
            payload[key] = value
    target = tmp_path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _flip_b64(value: str) -> str:
    """Return *value* (base64) with the last raw byte flipped - same length."""
    raw = bytearray(base64.b64decode(value))
    raw[-1] ^= 0xFF
    return base64.b64encode(bytes(raw)).decode("ascii")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _flip_usrx_payload_byte(path: Path) -> None:
    """Corrupt the first byte of the data section of a USRX file in place."""
    header = USRXFile.read_header(str(path))
    blob = bytearray(path.read_bytes())
    blob[header["header_size"]] ^= 0xFF
    path.write_bytes(bytes(blob))


def _usrx_with_metadata(path: Path, metadata, data: bytes = b"ciphertext",
                        encryption_type: str = "aes-gcm") -> Path:
    usrx.write_usrx_file(str(path), data, encryption_type, metadata)
    return path


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aes_sample(tmp_path_factory):
    """A read-only, correctly encrypted standard-format file (KDF runs once)."""
    path = tmp_path_factory.mktemp("aes_sample") / "sample.aes"
    enc.encrypt_file_aes_gcm(SECRET, str(path), PASSWORD)
    return path


@pytest.fixture(scope="module")
def usrx_sample(tmp_path_factory):
    """A read-only, correctly encrypted USRX container (KDF runs once)."""
    path = tmp_path_factory.mktemp("usrx_sample") / "sample.usrx"
    enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "aes-gcm", "usrx",
                                 {"content_type": "ad_groups"})
    return path


@pytest.fixture
def gpg_armored(tmp_path):
    """A file that looks like an ASCII-armored OpenPGP message."""
    path = tmp_path / "message.gpg"
    path.write_text(
        "-----BEGIN PGP MESSAGE-----\n\njA0ECQMC\n=abcd\n-----END PGP MESSAGE-----\n",
        encoding="utf-8",
    )
    return path


class _FakeGPGResult:
    """Stand-in for the result object python-gnupg returns."""

    def __init__(self, text="", ok=True, status="ok"):
        self.text = text
        self.ok = ok
        self.status = status

    def __str__(self):
        return self.text


@pytest.fixture
def fake_gnupg(monkeypatch):
    """
    Install a stub ``gnupg`` module so the GPG plumbing can be exercised.

    The returned handle collects every stub instance and lets a test steer the
    results (``encrypt_result``/``decrypt_result``/``version``).
    """

    class Handle:
        def __init__(self):
            self.instances = []
            self.encrypt_result = None
            self.decrypt_result = None
            self.version = (2, 4, 0)
            self.constructor_error = None

    handle = Handle()

    class FakeGPG:
        def __init__(self, gnupghome=None, **kwargs):
            if handle.constructor_error is not None:
                raise handle.constructor_error
            self.gnupghome = gnupghome
            self.encoding = None
            self.encrypt_calls = []
            self.decrypt_calls = []
            handle.instances.append(self)

        @property
        def version(self):
            return handle.version

        def encrypt(self, data, **kwargs):
            self.encrypt_calls.append((data, kwargs))
            if handle.encrypt_result is not None:
                return handle.encrypt_result
            return _FakeGPGResult(
                "-----BEGIN PGP MESSAGE-----\n{}\n-----END PGP MESSAGE-----\n".format(data)
            )

        def decrypt(self, data, **kwargs):
            self.decrypt_calls.append((data, kwargs))
            if handle.decrypt_result is not None:
                return handle.decrypt_result
            lines = data.split("\n")
            return _FakeGPGResult(lines[1] if len(lines) > 1 else "")

    module = types.ModuleType("gnupg")
    module.GPG = FakeGPG
    monkeypatch.setitem(sys.modules, "gnupg", module)
    return handle


# ==========================================================================
# USRX container - happy paths
# ==========================================================================

class TestUsrxContainer:

    def test_round_trip_returns_payload_type_and_metadata(self, tmp_path):
        """write() then read() gives back the exact payload, label and metadata."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"\x00\x01payload\n\xff", "aes-gcm",
                             {"content_type": "ad_groups", "total": 3})
        data, encryption_type, metadata = usrx.read_usrx_file(str(path))
        assert data == b"\x00\x01payload\n\xff"
        assert encryption_type == "aes-gcm"
        assert metadata["content_type"] == "ad_groups"
        assert metadata["total"] == 3

    @pytest.mark.parametrize("label", ["none", "aes-gcm", "gpg"])
    def test_every_supported_encryption_label_round_trips(self, tmp_path, label):
        """The three documented encryption labels survive a write/read cycle."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"x", label, {})
        assert usrx.read_usrx_file(str(path))[1] == label

    def test_metadata_keeps_diacritics_and_emoji(self, tmp_path):
        """Non-ASCII metadata survives the UTF-8 header round trip unchanged."""
        path = tmp_path / "c.usrx"
        metadata = {"note": UNICODE_SECRET, "třída": "6.A", "jméno": "Novák"}
        usrx.write_usrx_file(str(path), b"x", "aes-gcm", dict(metadata))
        read_back = usrx.read_usrx_file(str(path))[2]
        for key, value in metadata.items():
            assert read_back[key] == value

    def test_header_size_is_measured_in_bytes_not_characters(self, tmp_path):
        """file size == header_size + data_size even for multi-byte metadata."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"0123456789", "aes-gcm",
                             {"note": UNICODE_SECRET})
        header = USRXFile.read_header(str(path))
        assert header["header_size"] + header["data_size"] == path.stat().st_size
        assert header["data_size"] == 10

    def test_write_stamps_the_producing_application(self, tmp_path):
        """Every container records who wrote it, even without caller metadata."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"x", "none", None)
        assert usrx.read_usrx_file(str(path))[2]["created_by"] == "User Manager X 1.0"

    def test_empty_payload_round_trips(self, tmp_path):
        """A zero-byte data section is written and read back as b''."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"", "none", {})
        data, _, _ = usrx.read_usrx_file(str(path))
        assert data == b""
        assert USRXFile.read_header(str(path))["data_size"] == 0

    def test_binary_payload_is_byte_exact(self, tmp_path):
        """All 256 byte values survive, including NUL and newline."""
        path = tmp_path / "c.usrx"
        payload = bytes(range(256)) * 4
        usrx.write_usrx_file(str(path), payload, "aes-gcm", {})
        assert usrx.read_usrx_file(str(path))[0] == payload

    def test_writing_the_same_input_twice_is_byte_identical(self, tmp_path):
        """The container writer is deterministic - no timestamps or padding."""
        first, second = tmp_path / "a.usrx", tmp_path / "b.usrx"
        for target in (first, second):
            usrx.write_usrx_file(str(target), b"payload", "aes-gcm", {"n": 1})
        assert first.read_bytes() == second.read_bytes()

    def test_version_is_reported_from_the_header(self, tmp_path):
        """read_header exposes the format version stored in the file."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"x", "none", {})
        header = USRXFile.read_header(str(path))
        assert (header["version_major"], header["version_minor"]) == (
            usrx.VERSION_MAJOR, usrx.VERSION_MINOR)
        assert header["flags"] == 0

    def test_get_usrx_info_matches_read_header(self, tmp_path):
        """The convenience wrapper returns exactly the header dictionary."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"xyz", "aes-gcm", {"k": "v"})
        assert usrx.get_usrx_info(str(path)) == USRXFile.read_header(str(path))

    @pytest.mark.slow
    def test_large_payload_round_trips(self, tmp_path):
        """A payload well past one chunk is stored and returned intact."""
        path = tmp_path / "big.usrx"
        payload = b"chunk" * 60_000  # ~300 kB
        usrx.write_usrx_file(str(path), payload, "aes-gcm", {})
        data, _, _ = usrx.read_usrx_file(str(path))
        assert len(data) == len(payload) and data == payload


# ==========================================================================
# USRX container - detection and damaged files
# ==========================================================================

class TestUsrxDetectionAndDamage:

    def test_detect_format_accepts_a_written_container(self, tmp_path):
        """A file produced by write() is recognised as USRX."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"x", "none", {})
        assert USRXFile.detect_format(str(path)) is True
        assert usrx.is_usrx_file(str(path)) is True

    @pytest.mark.parametrize("content, label", [
        (b"", "empty file"),
        (b"USR", "shorter than the magic"),
        (b"USRY\x01\x00", "wrong magic"),
        (b"{\"algorithm\": \"AES-256-GCM\"}", "standard JSON file"),
        ("-----BEGIN PGP MESSAGE-----\n".encode(), "armored GPG file"),
    ])
    def test_detect_format_rejects_foreign_content(self, tmp_path, content, label):
        """Anything that does not start with the magic bytes is not USRX."""
        path = tmp_path / "foreign.bin"
        path.write_bytes(content)
        assert USRXFile.detect_format(str(path)) is False, label

    def test_detect_format_returns_false_for_a_missing_file(self, tmp_path):
        """A non-existent path is reported as 'not USRX' instead of raising."""
        assert USRXFile.detect_format(str(tmp_path / "nope.usrx")) is False

    def test_detect_format_returns_false_for_a_directory(self, tmp_path):
        """A directory is reported as 'not USRX' instead of raising."""
        assert USRXFile.detect_format(str(tmp_path)) is False

    def test_read_header_rejects_wrong_magic_bytes(self, tmp_path):
        """A foreign file is refused with a message naming the magic bytes."""
        path = tmp_path / "foreign.usrx"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
        with pytest.raises(USRXFormatError, match="magic bytes"):
            USRXFile.read_header(str(path))

    @pytest.mark.parametrize("keep", [4, 5, 6, 7, 8, 10, 15, 19],
                             ids=lambda n: "%d_bytes" % n)
    def test_read_header_rejects_a_truncated_header(self, tmp_path, keep):
        """Every truncation of the fixed header raises USRXFormatError."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"payload", "aes-gcm", {"k": "v"})
        path.write_bytes(path.read_bytes()[:keep])
        with pytest.raises(USRXFormatError):
            USRXFile.read_header(str(path))

    def test_read_header_rejects_metadata_that_is_not_utf8(self, tmp_path):
        """A corrupted metadata section is reported as a format error."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"payload", "aes-gcm", {"k": "v"})
        blob = bytearray(path.read_bytes())
        blob[FIXED_FIELDS] = 0xFF
        path.write_bytes(bytes(blob))
        with pytest.raises(USRXFormatError):
            USRXFile.read_header(str(path))

    def test_read_header_rejects_a_header_size_beyond_the_end_of_file(self, tmp_path):
        """A header_size larger than the file cannot silently yield metadata."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"payload", "aes-gcm", {"k": "v"})
        blob = bytearray(path.read_bytes())
        blob[8:12] = struct.pack("I", 100_000)
        path.write_bytes(bytes(blob))
        with pytest.raises(USRXFormatError):
            USRXFile.read_header(str(path))

    def test_read_header_reports_an_unknown_encryption_code_as_unknown(self, tmp_path):
        """An encryption byte outside the known set is labelled 'unknown'."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"payload", "aes-gcm", {})
        blob = bytearray(path.read_bytes())
        blob[6] = 99
        path.write_bytes(bytes(blob))
        assert USRXFile.read_header(str(path))["encryption_type"] == "unknown"

    def test_read_of_a_missing_file_raises_usrx_format_error(self, tmp_path):
        """read() turns a missing file into the module's own error type."""
        with pytest.raises(USRXFormatError):
            usrx.read_usrx_file(str(tmp_path / "missing.usrx"))

    def test_write_with_non_dict_metadata_raises_usrx_format_error(self, tmp_path):
        """A list where a mapping is expected fails loudly, not silently."""
        with pytest.raises(USRXFormatError):
            usrx.write_usrx_file(str(tmp_path / "c.usrx"), b"x", "none", ["a", "b"])

    def test_write_with_unserialisable_metadata_raises_usrx_format_error(self, tmp_path):
        """Metadata that JSON cannot represent is rejected."""
        with pytest.raises(USRXFormatError):
            usrx.write_usrx_file(str(tmp_path / "c.usrx"), b"x", "none",
                                 {"blob": b"raw bytes"})

    def test_write_into_a_missing_directory_raises_usrx_format_error(self, tmp_path):
        """A path whose parent does not exist fails with the module's error."""
        with pytest.raises(USRXFormatError):
            usrx.write_usrx_file(str(tmp_path / "no" / "dir" / "c.usrx"),
                                 b"x", "none", {})

    @pytest.mark.bug
    def test_read_rejects_a_container_truncated_in_the_data_section(self, tmp_path):
        """A file shorter than its declared data_size must not read as valid."""
        path = tmp_path / "c.usrx"
        usrx.write_usrx_file(str(path), b"A" * 100, "aes-gcm", {})
        path.write_bytes(path.read_bytes()[:-40])
        with pytest.raises(USRXFormatError):
            usrx.read_usrx_file(str(path))

    @pytest.mark.bug
    def test_write_rejects_an_unknown_encryption_label(self, tmp_path):
        """A misspelled algorithm name must fail, not fall back to 'none'."""
        path = tmp_path / "c.usrx"
        with pytest.raises((USRXFormatError, ValueError)):
            usrx.write_usrx_file(str(path), b"ciphertext", "aes-256-gcm", {})

    @pytest.mark.bug
    def test_write_does_not_mutate_the_callers_metadata(self, tmp_path):
        """Writing a container must not change the dictionary it was given."""
        metadata = {"content_type": "ad_groups"}
        usrx.write_usrx_file(str(tmp_path / "c.usrx"), b"x", "none", metadata)
        assert metadata == {"content_type": "ad_groups"}


# ==========================================================================
# helpers: key derivation and input validation
# ==========================================================================

class TestKeyDerivationAndValidation:

    def test_derived_key_is_256_bit_and_deterministic(self):
        """The same password and salt always derive the same 32-byte key."""
        salt = b"0123456789abcdef"
        key = enc._derive_key_aes(PASSWORD, salt)
        assert len(key) == 32
        assert key == enc._derive_key_aes(PASSWORD, salt)

    def test_a_different_salt_derives_a_different_key(self):
        """The salt actually participates in the derivation."""
        assert enc._derive_key_aes(PASSWORD, b"0" * 16) != \
               enc._derive_key_aes(PASSWORD, b"1" * 16)

    def test_unicode_passwords_derive_a_key(self):
        """A password with diacritics is encoded as UTF-8, not rejected."""
        key = enc._derive_key_aes("heslo-ěščřžýáíé-🔑", b"0" * 16)
        assert len(key) == 32
        assert key != enc._derive_key_aes("heslo-escrzyaie-", b"0" * 16)

    @pytest.mark.parametrize("password", ["", None])
    def test_empty_password_is_rejected(self, password):
        """A missing password is refused with a clear ValueError."""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            enc._validate_password(password)

    def test_short_password_is_accepted_but_warned_about(self, caplog):
        """A short password only produces a warning - it is not fatal."""
        with caplog.at_level(logging.WARNING, logger="utils.encryption"):
            enc._validate_password("abc")
        assert "less than 8" in caplog.text

    def test_empty_file_path_is_rejected(self):
        """An empty path is refused before any file system access."""
        with pytest.raises(ValueError, match="File path cannot be empty"):
            enc._validate_file_path("")

    def test_missing_file_is_reported_as_file_not_found(self, tmp_path):
        """must_exist=True raises FileNotFoundError naming the path."""
        missing = tmp_path / "nope.aes"
        with pytest.raises(FileNotFoundError, match="nope.aes"):
            enc._validate_file_path(str(missing), must_exist=True)


# ==========================================================================
# availability probing
# ==========================================================================

class TestAvailability:

    def test_get_available_methods_lists_both_backends(self):
        """The report always covers exactly the two supported methods."""
        methods = enc.get_available_methods()
        assert set(methods) == {"gpg", "aes-gcm"}
        assert all(isinstance(value, bool) for value in methods.values())

    def test_aes_backend_is_available(self):
        """PyCryptodome is installed, so AES-GCM is reported as usable."""
        assert enc.check_aes_available() is True
        assert enc.get_available_methods()["aes-gcm"] is True

    @needs_no_gnupg
    def test_gpg_is_unavailable_without_python_gnupg(self):
        """Without the python-gnupg package GPG is reported as unusable."""
        assert enc.check_gpg_available() is False
        assert enc.get_available_methods()["gpg"] is False

    def test_gpg_is_available_when_the_library_reports_a_version(self, fake_gnupg):
        """A working gnupg module flips the availability report to True."""
        assert enc.check_gpg_available() is True
        assert enc.get_available_methods()["gpg"] is True

    def test_gpg_is_unavailable_when_the_binary_reports_no_version(self, fake_gnupg):
        """An installed library without a GnuPG binary still counts as unusable."""
        fake_gnupg.version = None
        assert enc.check_gpg_available() is False

    def test_gpg_is_unavailable_when_the_library_raises(self, fake_gnupg):
        """A gnupg module that blows up on construction degrades gracefully."""
        fake_gnupg.constructor_error = OSError("gpg executable not found")
        assert enc.check_gpg_available() is False


# ==========================================================================
# AES-GCM, standard (JSON) format
# ==========================================================================

class TestAesStandardFormat:

    @pytest.mark.parametrize("payload", [
        "plain ascii",
        UNICODE_SECRET,
        '{"json": ["with", "nested", 1, 2, 3]}',
        "line one\nline two\r\n\ttabbed",
        " ",
        "0",
        "a" * 5000,
    ], ids=["ascii", "unicode", "json", "newlines", "space", "zero", "long"])
    def test_round_trip_preserves_the_payload(self, tmp_path, payload):
        """Whatever goes in comes back out byte-for-byte after decryption."""
        path = tmp_path / "secret.aes"
        enc.encrypt_file(payload, str(path), PASSWORD)
        assert enc.decrypt_file(str(path), PASSWORD) == payload

    def test_ciphertext_is_stored_as_json_with_the_expected_parameters(self, aes_sample):
        """The on-disk layout carries a 16-byte salt, 96-bit nonce and 128-bit tag."""
        payload = json.loads(aes_sample.read_text(encoding="utf-8"))
        assert payload["algorithm"] == "AES-256-GCM"
        assert payload["version"] == "1.0"
        assert len(base64.b64decode(payload["salt"])) == 16
        assert len(base64.b64decode(payload["nonce"])) == 12
        assert len(base64.b64decode(payload["tag"])) == 16
        assert SECRET not in aes_sample.read_text(encoding="utf-8")

    def test_encrypting_twice_uses_a_fresh_salt_and_nonce(self, tmp_path):
        """Encryption is randomised: two runs never produce the same file."""
        first, second = tmp_path / "a.aes", tmp_path / "b.aes"
        enc.encrypt_file(SECRET, str(first), PASSWORD)
        enc.encrypt_file(SECRET, str(second), PASSWORD)
        left = json.loads(first.read_text(encoding="utf-8"))
        right = json.loads(second.read_text(encoding="utf-8"))
        assert left["salt"] != right["salt"]
        assert left["nonce"] != right["nonce"]
        assert left["ciphertext"] != right["ciphertext"]
        assert enc.decrypt_file(str(first), PASSWORD) == \
               enc.decrypt_file(str(second), PASSWORD) == SECRET

    def test_encryption_creates_missing_parent_directories(self, tmp_path):
        """A nested output path is created rather than failing."""
        path = tmp_path / "deep" / "nested" / "secret.aes"
        enc.encrypt_file(SECRET, str(path), PASSWORD)
        assert path.exists()
        assert enc.decrypt_file(str(path), PASSWORD) == SECRET

    def test_wrong_password_is_rejected_with_a_clear_message(self, aes_sample):
        """A bad password produces a RuntimeError that says so."""
        with pytest.raises(RuntimeError, match="incorrect password"):
            enc.decrypt_file_aes_gcm(str(aes_sample), "wrong-password")

    @pytest.mark.parametrize("field", ["ciphertext", "tag", "nonce", "salt"])
    def test_tampering_with_any_component_is_detected(self, aes_sample, tmp_path, field):
        """Flipping a byte of any stored component fails authentication."""
        original = json.loads(aes_sample.read_text(encoding="utf-8"))
        target = _aes_variant(aes_sample, tmp_path, "tampered.aes",
                              **{field: _flip_b64(original[field])})
        with pytest.raises(RuntimeError, match="AES-GCM decryption failed"):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    @pytest.mark.bug
    def test_a_tampered_file_is_not_blamed_on_the_password(self, aes_sample, tmp_path):
        """A corrupted file must say so - the code raises that message internally."""
        original = json.loads(aes_sample.read_text(encoding="utf-8"))
        target = _aes_variant(aes_sample, tmp_path, "tampered2.aes",
                              ciphertext=_flip_b64(original["ciphertext"]))
        with pytest.raises(RuntimeError, match="corrupted"):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    def test_appending_data_to_the_ciphertext_is_detected(self, aes_sample, tmp_path):
        """Extra ciphertext bytes break the GCM tag."""
        original = json.loads(aes_sample.read_text(encoding="utf-8"))
        extended = base64.b64decode(original["ciphertext"]) + b"\x00"
        target = _aes_variant(aes_sample, tmp_path, "extended.aes",
                              ciphertext=_b64(extended))
        with pytest.raises(RuntimeError, match="AES-GCM decryption failed"):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    @pytest.mark.parametrize("password", ["", None])
    def test_encryption_refuses_an_empty_password(self, tmp_path, password):
        """The standard path validates the password before writing anything."""
        path = tmp_path / "secret.aes"
        with pytest.raises(ValueError, match="Password cannot be empty"):
            enc.encrypt_file(SECRET, str(path), password)
        assert not path.exists()

    def test_decryption_refuses_an_empty_password(self, aes_sample):
        """An empty password is refused before the file is even parsed."""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            enc.decrypt_file(str(aes_sample), "")

    def test_encryption_refuses_empty_data(self, tmp_path):
        """There is nothing to encrypt, so the call fails instead of writing."""
        path = tmp_path / "secret.aes"
        with pytest.raises(ValueError, match="cannot be empty"):
            enc.encrypt_file("", str(path), PASSWORD)
        assert not path.exists()

    def test_encryption_refuses_an_empty_output_path(self):
        """An empty destination path is a ValueError, not an OSError."""
        with pytest.raises(ValueError, match="File path cannot be empty"):
            enc.encrypt_file(SECRET, "", PASSWORD)

    def test_decrypting_a_missing_file_raises_file_not_found(self, tmp_path):
        """A missing input is reported as FileNotFoundError, not RuntimeError."""
        with pytest.raises(FileNotFoundError):
            enc.decrypt_file(str(tmp_path / "missing.aes"), PASSWORD)

    @pytest.mark.parametrize("method", ["des", "rot13", "AES-GCM", "", None])
    def test_encrypt_file_rejects_an_unknown_method(self, tmp_path, method):
        """Only 'gpg' and 'aes-gcm' are accepted; anything else is a ValueError."""
        with pytest.raises(ValueError, match="Unknown encryption method"):
            enc.encrypt_file(SECRET, str(tmp_path / "x.aes"), PASSWORD, method=method)

    def test_decrypt_file_rejects_an_unknown_method(self, aes_sample):
        """An explicit unsupported method is a ValueError, not a silent fallback."""
        with pytest.raises(ValueError, match="Unknown encryption method"):
            enc.decrypt_file(str(aes_sample), PASSWORD, method="rot13")

    def test_decrypt_file_auto_detects_aes(self, aes_sample):
        """Omitting the method detects AES-GCM from the file itself."""
        assert enc.decrypt_file(str(aes_sample), PASSWORD) == SECRET


# ==========================================================================
# AES-GCM, malformed input files
# ==========================================================================

class TestAesMalformedFiles:

    @pytest.mark.parametrize("field",
                             ["version", "algorithm", "salt", "nonce", "tag", "ciphertext"])
    def test_a_missing_field_is_reported_by_name(self, aes_sample, tmp_path, field):
        """The error message names the field that is missing from the file."""
        target = _aes_variant(aes_sample, tmp_path, "missing.aes", **{field: _DELETE})
        with pytest.raises((ValueError, RuntimeError),
                           match="missing '%s' field" % field):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    @pytest.mark.bug
    def test_a_missing_field_raises_value_error_as_documented(self, aes_sample, tmp_path):
        """decrypt_file_aes_gcm documents ValueError for an invalid file format."""
        target = _aes_variant(aes_sample, tmp_path, "missing_tag.aes", tag=_DELETE)
        with pytest.raises(ValueError, match="missing 'tag' field"):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    def test_an_unsupported_algorithm_is_named_in_the_error(self, aes_sample, tmp_path):
        """A file encrypted with another algorithm is refused explicitly."""
        target = _aes_variant(aes_sample, tmp_path, "cbc.aes", algorithm="AES-128-CBC")
        with pytest.raises((ValueError, RuntimeError),
                           match="Unsupported encryption algorithm: AES-128-CBC"):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    @pytest.mark.parametrize("field, raw, expected", [
        ("salt", b"\x00" * 8, "Invalid salt size"),
        ("salt", b"\x00" * 32, "Invalid salt size"),
        ("nonce", b"\x00" * 16, "Invalid nonce size"),
        ("nonce", b"", "Invalid nonce size"),
        ("tag", b"\x00" * 8, "Invalid tag size"),
    ])
    def test_component_sizes_are_validated(self, aes_sample, tmp_path,
                                           field, raw, expected):
        """Wrongly sized salt/nonce/tag are refused before any key derivation."""
        target = _aes_variant(aes_sample, tmp_path, "sized.aes", **{field: _b64(raw)})
        with pytest.raises((ValueError, RuntimeError), match=expected):
            enc.decrypt_file_aes_gcm(str(target), PASSWORD)

    @pytest.mark.parametrize("content", ["", "not json at all", "{ broken", "[]", "null"])
    def test_structurally_wrong_input_is_refused(self, tmp_path, content):
        """A file that is not the expected JSON document is refused, not parsed."""
        path = tmp_path / "broken.aes"
        path.write_text(content, encoding="utf-8")
        with pytest.raises((ValueError, RuntimeError)):
            enc.decrypt_file_aes_gcm(str(path), PASSWORD)

    def test_invalid_json_syntax_raises_value_error(self, tmp_path):
        """Broken JSON is reported as an invalid file format."""
        path = tmp_path / "broken.aes"
        path.write_text("{ not: valid", encoding="utf-8")
        with pytest.raises(ValueError, match="not a valid JSON file"):
            enc.decrypt_file_aes_gcm(str(path), PASSWORD)

    def test_a_binary_file_is_refused_without_leaking_a_unicode_error(self, tmp_path):
        """Feeding a binary blob in fails as RuntimeError, not UnicodeDecodeError."""
        path = tmp_path / "binary.aes"
        path.write_bytes(bytes(range(256)))
        with pytest.raises(RuntimeError):
            enc.decrypt_file_aes_gcm(str(path), PASSWORD)


# ==========================================================================
# format / method detection and information helpers
# ==========================================================================

class TestDetection:

    def test_aes_json_is_detected(self, aes_sample):
        """A standard AES container is detected from its JSON body."""
        assert enc.detect_encryption_method(str(aes_sample)) == "aes-gcm"

    def test_armored_gpg_header_is_detected(self, gpg_armored):
        """An ASCII-armored OpenPGP message is detected from its first line."""
        assert enc.detect_encryption_method(str(gpg_armored)) == "gpg"

    def test_a_binary_file_cannot_be_detected(self, tmp_path):
        """Undecodable bytes produce a ValueError, never a UnicodeDecodeError."""
        path = tmp_path / "blob.bin"
        path.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xd8\xff\xe0")
        with pytest.raises(ValueError, match="Cannot detect encryption method"):
            enc.detect_encryption_method(str(path))

    @pytest.mark.parametrize("content, label", [
        ("", "empty file"),
        ("hello world", "plain text"),
        ("{}", "empty JSON object"),
        ('{"algorithm": "ChaCha20-Poly1305"}', "another algorithm"),
        ('{"version": "1.0"}', "JSON without an algorithm"),
        ("-----BEGIN CERTIFICATE-----", "an unrelated PEM block"),
    ])
    def test_unrecognised_content_raises_value_error(self, tmp_path, content, label):
        """Anything that is neither armored GPG nor an AES document is refused."""
        path = tmp_path / "unknown.dat"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot detect encryption method"):
            enc.detect_encryption_method(str(path))

    def test_detection_of_a_missing_file_raises_file_not_found(self, tmp_path):
        """A missing path is FileNotFoundError, as documented."""
        with pytest.raises(FileNotFoundError):
            enc.detect_encryption_method(str(tmp_path / "missing.aes"))

    def test_a_usrx_container_is_not_a_standard_format(self, usrx_sample):
        """The binary container is not mistaken for a standard AES/GPG file."""
        with pytest.raises(ValueError):
            enc.detect_encryption_method(str(usrx_sample))

    @pytest.mark.parametrize("builder, expected", [
        ("usrx", "usrx"),
        ("aes", "standard"),
        ("text", "standard"),
    ])
    def test_detect_file_format(self, tmp_path, aes_sample, usrx_sample,
                                builder, expected):
        """detect_file_format only distinguishes the USRX container."""
        if builder == "usrx":
            path = usrx_sample
        elif builder == "aes":
            path = aes_sample
        else:
            path = tmp_path / "plain.txt"
            path.write_text("hello", encoding="utf-8")
        assert enc.detect_file_format(str(path)) == expected


class TestInformationHelpers:

    def test_encryption_info_for_an_aes_file(self, aes_sample):
        """The report carries the method, algorithm, version, kdf and size."""
        info = enc.get_encryption_info(str(aes_sample))
        assert info["method"] == "aes-gcm"
        assert info["algorithm"] == "AES-256-GCM"
        assert info["version"] == "1.0"
        assert info["kdf"] == "auto"
        assert info["file_size"] == aes_sample.stat().st_size

    def test_encryption_info_for_an_armored_gpg_file(self, gpg_armored):
        """A GPG file reports the OpenPGP algorithm and no AES fields."""
        info = enc.get_encryption_info(str(gpg_armored))
        assert info["method"] == "gpg"
        assert info["algorithm"] == "GPG/OpenPGP"
        assert "kdf" not in info

    def test_encryption_info_of_an_unreadable_file_raises_value_error(self, tmp_path):
        """An undetectable file raises rather than returning a half-filled dict."""
        path = tmp_path / "unknown.dat"
        path.write_text("nothing useful here", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot read encryption info"):
            enc.get_encryption_info(str(path))

    def test_encryption_info_of_a_missing_file_raises_file_not_found(self, tmp_path):
        """The existence check happens before detection."""
        with pytest.raises(FileNotFoundError):
            enc.get_encryption_info(str(tmp_path / "missing.aes"))

    def test_file_info_tags_a_standard_file(self, aes_sample):
        """get_file_info adds format='standard' to the encryption report."""
        info = enc.get_file_info(str(aes_sample))
        assert info["format"] == "standard"
        assert info["method"] == "aes-gcm"
        assert info["algorithm"] == "AES-256-GCM"

    def test_file_info_returns_the_usrx_header(self, usrx_sample):
        """For a container the header is returned, tagged format='usrx'."""
        info = enc.get_file_info(str(usrx_sample))
        assert info["format"] == "usrx"
        assert info["encryption_type"] == "aes-gcm"
        assert info["metadata"]["content_type"] == "ad_groups"
        assert info["data_size"] > 0

    @pytest.mark.parametrize("content", [b"", b"random bytes", b"\x00\xff\xfe"])
    def test_file_info_never_raises_for_junk(self, tmp_path, content):
        """Unknown files come back as {'format': 'unknown', 'error': ...}."""
        path = tmp_path / "junk.bin"
        path.write_bytes(content)
        info = enc.get_file_info(str(path))
        assert info["format"] == "unknown"
        assert info["error"]

    def test_file_info_of_a_missing_file_reports_unknown(self, tmp_path):
        """A missing path is reported, not raised, by get_file_info."""
        info = enc.get_file_info(str(tmp_path / "missing.usrx"))
        assert info["format"] == "unknown"


# ==========================================================================
# AES-GCM inside the USRX container
# ==========================================================================

class TestUsrxEncryption:

    @pytest.mark.integration
    @pytest.mark.parametrize("payload", ["plain", UNICODE_SECRET, SECRET],
                             ids=["ascii", "unicode", "json"])
    def test_round_trip_through_the_container(self, tmp_path, payload):
        """encrypt_file_with_format/decrypt_file_with_format round trip."""
        path = tmp_path / "export.usrx"
        enc.encrypt_file_with_format(payload, str(path), PASSWORD, "aes-gcm", "usrx")
        assert enc.decrypt_file_with_format(str(path), PASSWORD) == payload

    def test_the_container_carries_the_encryption_parameters(self, usrx_sample):
        """Salt, nonce and tag are stored in the header with the right sizes."""
        metadata = usrx.read_usrx_file(str(usrx_sample))[2]
        assert metadata["algorithm"] == "AES-256-GCM"
        assert metadata["kdf"] == "auto"
        assert len(base64.b64decode(metadata["salt"])) == 16
        assert len(base64.b64decode(metadata["nonce"])) == 12
        assert len(base64.b64decode(metadata["tag"])) == 16

    def test_caller_metadata_is_preserved_next_to_the_parameters(self, usrx_sample):
        """User metadata survives alongside the injected crypto parameters."""
        info = usrx.get_usrx_info(str(usrx_sample))
        assert info["encryption_type"] == "aes-gcm"
        assert info["metadata"]["content_type"] == "ad_groups"
        assert info["metadata"]["created_by"] == "User Manager X 1.0"

    def test_the_plaintext_never_appears_in_the_container(self, usrx_sample):
        """No part of the secret leaks into the header or the data section."""
        blob = usrx_sample.read_bytes()
        assert SECRET.encode("utf-8") not in blob
        assert b"novakjan" not in blob

    def test_the_default_format_is_standard(self, tmp_path):
        """Without an explicit format the JSON layout is written."""
        path = tmp_path / "export.aes"
        enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "aes-gcm")
        assert usrx.is_usrx_file(str(path)) is False
        assert json.loads(path.read_text(encoding="utf-8"))["algorithm"] == "AES-256-GCM"
        assert enc.decrypt_file_with_format(str(path), PASSWORD) == SECRET

    def test_format_is_auto_detected_on_decryption(self, tmp_path):
        """The same call decrypts both layouts without being told which."""
        standard = tmp_path / "a.aes"
        container = tmp_path / "b.usrx"
        enc.encrypt_file_with_format(SECRET, str(standard), PASSWORD, "aes-gcm", "standard")
        enc.encrypt_file_with_format(SECRET, str(container), PASSWORD, "aes-gcm", "usrx")
        assert enc.decrypt_file_with_format(str(standard), PASSWORD) == SECRET
        assert enc.decrypt_file_with_format(str(container), PASSWORD) == SECRET

    def test_wrong_password_on_a_container_is_rejected(self, usrx_sample):
        """A wrong password fails authentication with an explicit message."""
        with pytest.raises(RuntimeError, match="incorrect password or corrupted file"):
            enc.decrypt_file_with_format(str(usrx_sample), "not-the-password")

    def test_tampered_container_payload_is_detected(self, tmp_path):
        """Flipping a ciphertext byte inside the container breaks the GCM tag."""
        path = tmp_path / "export.usrx"
        enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "aes-gcm", "usrx")
        _flip_usrx_payload_byte(path)
        with pytest.raises(RuntimeError, match="incorrect password or corrupted file"):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    @pytest.mark.parametrize("field", ["tag", "nonce", "salt"])
    def test_tampered_container_parameters_are_detected(self, tmp_path, field):
        """Rewriting a stored crypto parameter is caught by the tag check."""
        path = tmp_path / "export.usrx"
        enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "aes-gcm", "usrx")
        data, method, metadata = usrx.read_usrx_file(str(path))
        metadata[field] = _flip_b64(metadata[field])
        usrx.write_usrx_file(str(path), data, method, metadata)
        with pytest.raises(RuntimeError, match="incorrect password or corrupted file"):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    @pytest.mark.parametrize("file_format", ["standard", "usrx"])
    def test_an_unknown_method_is_rejected_in_both_formats(self, tmp_path, file_format):
        """A bogus algorithm name fails the same way in both layouts."""
        path = tmp_path / "export.bin"
        with pytest.raises(ValueError, match="Unsupported encryption method"):
            enc.encrypt_file_with_format(SECRET, str(path), PASSWORD,
                                         "twofish", file_format)
        assert not path.exists()

    def test_decrypting_a_container_declared_as_standard_fails_loudly(self, usrx_sample):
        """Forcing the wrong layout raises instead of returning garbage."""
        with pytest.raises((ValueError, RuntimeError)):
            enc.decrypt_file_with_format(str(usrx_sample), PASSWORD,
                                         file_format="standard")

    @pytest.mark.bug
    def test_the_container_path_refuses_an_empty_password(self, tmp_path):
        """Password validation must not depend on the chosen file format."""
        path = tmp_path / "export.usrx"
        with pytest.raises(ValueError, match="Password cannot be empty"):
            enc.encrypt_file_with_format(SECRET, str(path), "", "aes-gcm", "usrx")

    @pytest.mark.bug
    def test_the_container_path_refuses_empty_data(self, tmp_path):
        """Empty input is rejected in the standard path and must be here too."""
        path = tmp_path / "export.usrx"
        with pytest.raises(ValueError, match="cannot be empty"):
            enc.encrypt_file_with_format("", str(path), PASSWORD, "aes-gcm", "usrx")

    @pytest.mark.bug
    def test_encryption_does_not_mutate_the_callers_metadata(self, tmp_path):
        """The metadata dictionary handed in must come back unchanged."""
        metadata = {"content_type": "ad_groups", "total_groups": 3}
        enc.encrypt_file_with_format(SECRET, str(tmp_path / "e.usrx"), PASSWORD,
                                     "aes-gcm", "usrx", metadata)
        assert metadata == {"content_type": "ad_groups", "total_groups": 3}


# ==========================================================================
# _decrypt_from_usrx - damaged containers
# ==========================================================================

class TestUsrxDecryptionErrors:

    @pytest.mark.parametrize("missing", ["salt", "nonce", "tag"])
    def test_a_missing_parameter_raises_runtime_error_naming_it(self, tmp_path, missing):
        """A missing crypto parameter must never surface as a bare KeyError."""
        metadata = {"salt": _b64(b"0" * 16), "nonce": _b64(b"0" * 12),
                    "tag": _b64(b"0" * 16)}
        metadata.pop(missing)
        path = _usrx_with_metadata(tmp_path / "bad.usrx", metadata)
        with pytest.raises(RuntimeError, match=missing):
            enc._decrypt_from_usrx(str(path), PASSWORD)

    def test_all_missing_parameters_are_listed(self, tmp_path):
        """A foreign container names every parameter it is missing."""
        path = _usrx_with_metadata(tmp_path / "foreign.usrx", {"note": "hi"})
        with pytest.raises(RuntimeError) as excinfo:
            enc._decrypt_from_usrx(str(path), PASSWORD)
        message = str(excinfo.value)
        assert "salt" in message and "nonce" in message and "tag" in message
        assert "damaged" in message.lower()

    def test_a_missing_parameter_is_not_a_key_error(self, tmp_path):
        """Regression guard: KeyError must not escape to the caller."""
        path = _usrx_with_metadata(tmp_path / "foreign.usrx", {"note": "hi"})
        with pytest.raises(RuntimeError):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    @pytest.mark.parametrize("value", ["abcde", "AAAAA"],
                             ids=["five-chars", "bad-padding"])
    def test_undecodable_padding_raises_runtime_error(self, tmp_path, value):
        """A base64 string of impossible length is reported as a format error."""
        metadata = {"salt": value, "nonce": _b64(b"0" * 12), "tag": _b64(b"0" * 16)}
        path = _usrx_with_metadata(tmp_path / "bad.usrx", metadata)
        with pytest.raises(RuntimeError, match="invalid encryption parameters"):
            enc._decrypt_from_usrx(str(path), PASSWORD)

    @pytest.mark.parametrize("value", [None, 42, ["a"], {"k": "v"}],
                             ids=["none", "int", "list", "dict"])
    def test_non_string_parameters_raise_runtime_error(self, tmp_path, value):
        """A wrongly typed parameter must not leak a TypeError."""
        metadata = {"salt": value, "nonce": _b64(b"0" * 12), "tag": _b64(b"0" * 16)}
        path = _usrx_with_metadata(tmp_path / "bad.usrx", metadata)
        with pytest.raises(RuntimeError, match="invalid encryption parameters"):
            enc._decrypt_from_usrx(str(path), PASSWORD)

    @pytest.mark.parametrize("field, raw", [
        ("nonce", b"abcd"),
        ("tag", b"ab"),
        ("salt", b"short"),
    ])
    def test_wrongly_sized_parameters_raise_runtime_error(self, tmp_path, field, raw):
        """Parameters of the wrong length fail as RuntimeError, not ValueError."""
        metadata = {"salt": _b64(b"0" * 16), "nonce": _b64(b"0" * 12),
                    "tag": _b64(b"0" * 16)}
        metadata[field] = _b64(raw)
        path = _usrx_with_metadata(tmp_path / "bad.usrx", metadata)
        with pytest.raises(RuntimeError):
            enc._decrypt_from_usrx(str(path), PASSWORD)

    def test_an_unencrypted_container_reports_an_unsupported_method(self, tmp_path):
        """A container labelled 'none' cannot be decrypted."""
        path = _usrx_with_metadata(tmp_path / "plain.usrx", {}, b"data", "none")
        with pytest.raises(ValueError, match="Unsupported encryption method"):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    def test_an_explicit_method_overrides_the_container_label(self, tmp_path):
        """The caller may force a method; an unknown one is refused."""
        path = _usrx_with_metadata(tmp_path / "c.usrx", {"salt": _b64(b"0" * 16)})
        with pytest.raises(ValueError, match="Unsupported encryption method"):
            enc.decrypt_file_with_format(str(path), PASSWORD,
                                         encryption_method="serpent")

    def test_a_damaged_container_is_refused(self, tmp_path):
        """A truncated header cannot be decrypted at all."""
        path = tmp_path / "export.usrx"
        enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "aes-gcm", "usrx")
        path.write_bytes(path.read_bytes()[:10])
        with pytest.raises((USRXFormatError, RuntimeError, ValueError)):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    @pytest.mark.bug
    @pytest.mark.parametrize("value", ["!!!", "", "@@@@", "%%%%", "*"])
    def test_undecodable_parameters_raise_runtime_error(self, tmp_path, value):
        """Garbage parameters must be reported as a damaged-file RuntimeError."""
        metadata = {"salt": value, "nonce": value, "tag": value}
        path = _usrx_with_metadata(tmp_path / "bad.usrx", metadata)
        with pytest.raises(RuntimeError):
            enc._decrypt_from_usrx(str(path), PASSWORD)


# ==========================================================================
# GPG - unavailable in this environment
# ==========================================================================

@needs_no_gnupg
class TestGpgUnavailable:

    def test_encrypt_file_reports_gpg_as_unavailable(self, tmp_path):
        """The unified entry point explains how to make GPG available."""
        path = tmp_path / "out.gpg"
        with pytest.raises(RuntimeError, match="GPG encryption is not available"):
            enc.encrypt_file(SECRET, str(path), PASSWORD, method="gpg")
        assert not path.exists()

    def test_decrypt_file_reports_gpg_as_unavailable(self, gpg_armored):
        """Auto-detection finds GPG and then refuses with a helpful message."""
        with pytest.raises(RuntimeError, match="GPG decryption is not available"):
            enc.decrypt_file(str(gpg_armored), PASSWORD)

    def test_encrypt_file_gpg_names_the_missing_library(self, tmp_path):
        """The low-level helper names python-gnupg and the pip command."""
        with pytest.raises(RuntimeError, match="python-gnupg"):
            enc.encrypt_file_gpg(SECRET, str(tmp_path / "out.gpg"), PASSWORD)

    def test_decrypt_file_gpg_names_the_missing_library(self, gpg_armored):
        """Decryption fails with the same actionable message."""
        with pytest.raises(RuntimeError, match="python-gnupg"):
            enc.decrypt_file_gpg(str(gpg_armored), PASSWORD)

    def test_gpg_into_a_usrx_container_fails_without_writing_output(self, tmp_path):
        """The container is not created when the GPG step cannot run."""
        path = tmp_path / "out.usrx"
        with pytest.raises(RuntimeError, match="python-gnupg"):
            enc.encrypt_file_with_format(SECRET, str(path), PASSWORD, "gpg", "usrx")
        assert not path.exists()

    def test_decrypting_a_gpg_container_reports_the_missing_library(self, tmp_path):
        """A GPG container produced elsewhere degrades with a clear error."""
        path = _usrx_with_metadata(
            tmp_path / "gpg.usrx",
            {"algorithm": "GPG/OpenPGP"},
            b"-----BEGIN PGP MESSAGE-----\nbody\n-----END PGP MESSAGE-----\n",
            "gpg",
        )
        with pytest.raises(RuntimeError, match="python-gnupg"):
            enc.decrypt_file_with_format(str(path), PASSWORD)

    def test_gpg_decryption_still_checks_the_input_exists(self, tmp_path):
        """A missing file is FileNotFoundError even before the library check."""
        with pytest.raises(FileNotFoundError):
            enc.decrypt_file_gpg(str(tmp_path / "missing.gpg"), PASSWORD)

    def test_gpg_encryption_still_validates_the_password(self, tmp_path):
        """Input validation happens before the availability check."""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            enc.encrypt_file_gpg(SECRET, str(tmp_path / "out.gpg"), "")


# ==========================================================================
# GPG - plumbing exercised against an injected stub library
# ==========================================================================

class TestGpgWithStubLibrary:

    def test_encryption_writes_the_armored_result_and_cleans_up(self, tmp_path,
                                                                fake_gnupg):
        """The armored output is written and the temporary GPG home is removed."""
        path = tmp_path / "out.gpg"
        enc.encrypt_file_gpg("payload", str(path), PASSWORD)
        assert path.read_text(encoding="utf-8").startswith("-----BEGIN PGP MESSAGE-----")
        homes = [instance.gnupghome for instance in fake_gnupg.instances
                 if instance.gnupghome]
        assert homes, "GPG should have been created with a temporary home"
        assert not any(Path(home).exists() for home in homes)

    def test_encryption_requests_aes256_with_a_passphrase(self, tmp_path, fake_gnupg):
        """Symmetric AES256 encryption is requested with the given passphrase."""
        enc.encrypt_file_gpg("payload", str(tmp_path / "out.gpg"), PASSWORD)
        instance = fake_gnupg.instances[-1]
        _, kwargs = instance.encrypt_calls[0]
        assert kwargs["symmetric"] == "AES256"
        assert kwargs["passphrase"] == PASSWORD
        assert kwargs["recipients"] is None
        assert kwargs["armor"] is True
        assert "--cipher-algo" in kwargs["extra_args"]

    def test_a_failed_encryption_is_reported_and_writes_nothing(self, tmp_path,
                                                                fake_gnupg):
        """When GnuPG refuses, no half-written file is left behind."""
        fake_gnupg.encrypt_result = _FakeGPGResult("", ok=False, status="no such key")
        path = tmp_path / "out.gpg"
        with pytest.raises(RuntimeError, match="no such key"):
            enc.encrypt_file_gpg("payload", str(path), PASSWORD)
        assert not path.exists()

    def test_a_failed_decryption_is_reported_as_a_wrong_password(self, gpg_armored,
                                                                 fake_gnupg):
        """A rejected passphrase produces an explicit 'incorrect password' error."""
        fake_gnupg.decrypt_result = _FakeGPGResult("", ok=False,
                                                   status="decryption failed")
        with pytest.raises(RuntimeError, match="incorrect password"):
            enc.decrypt_file_gpg(str(gpg_armored), PASSWORD)

    def test_an_empty_encrypted_file_is_reported(self, tmp_path, fake_gnupg):
        """A zero-byte GPG file is refused with a message that says it is empty."""
        path = tmp_path / "empty.gpg"
        path.write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError, match="empty"):
            enc.decrypt_file_gpg(str(path), PASSWORD)

    @pytest.mark.integration
    def test_round_trip_through_the_unified_interface(self, tmp_path, fake_gnupg):
        """encrypt_file/decrypt_file work end to end once the library is present."""
        path = tmp_path / "out.gpg"
        enc.encrypt_file("payload", str(path), PASSWORD, method="gpg")
        assert enc.detect_encryption_method(str(path)) == "gpg"
        assert enc.get_encryption_info(str(path))["algorithm"] == "GPG/OpenPGP"
        assert enc.decrypt_file(str(path), PASSWORD) == "payload"

    @pytest.mark.integration
    def test_round_trip_of_a_gpg_usrx_container(self, tmp_path, fake_gnupg):
        """A GPG payload can be stored in, and read back from, a USRX container."""
        path = tmp_path / "out.usrx"
        enc.encrypt_file_with_format("payload", str(path), PASSWORD, "gpg", "usrx",
                                     {"content_type": "ad_groups"})
        info = usrx.get_usrx_info(str(path))
        assert info["encryption_type"] == "gpg"
        assert info["metadata"]["algorithm"] == "GPG/OpenPGP"
        assert info["metadata"]["content_type"] == "ad_groups"
        assert enc.decrypt_file_with_format(str(path), PASSWORD) == "payload"
