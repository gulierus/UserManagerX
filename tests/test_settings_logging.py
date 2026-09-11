"""
Unit tests for ``utils.settings_manager`` and ``utils.logging_config``.

Both modules persist small JSON documents and both carry a hand written path
validator, so the tests below focus on

* the persistence round trip (including unicode, corrupt and unreadable files),
* the merging rules against the built-in defaults,
* the category / typed-getter API and the Qt signals it emits,
* the validation performed by ``update_logging_config`` / ``update_config``,
* the two ``validate_path`` implementations, which must agree, and
* the log-file bookkeeping helpers.

Nothing here touches the real user settings: every test uses ``isolated_settings``
or ``isolated_logging_config`` (or builds its own object under ``tmp_path``).
Neither module can open a modal dialog, so the ``dialogs`` fixture is not needed.
"""

import json
import logging
import os
import stat
from pathlib import Path

import pytest

import utils.logging_config as lc
import utils.settings_manager as sm
from utils.logging_config import LoggingConfig
from utils.settings_manager import DEFAULT_SETTINGS, SettingsManager


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _fresh_manager() -> SettingsManager:
    """A second SettingsManager reading the same isolated settings file."""
    return sm.SettingsManager()


def _disk(settings: SettingsManager) -> dict:
    """The settings as they are currently stored on disk."""
    return json.loads(settings.settings_file.read_text(encoding="utf-8"))


@pytest.fixture
def signals():
    """Collect ``settings_changed`` / ``category_changed`` emissions."""

    class Collector:
        def __init__(self):
            self.changed = []      # (category, key, value)
            self.categories = []   # category

        def attach(self, manager):
            manager.settings_changed.connect(
                lambda c, k, v: self.changed.append((c, k, v)))
            manager.category_changed.connect(self.categories.append)
            return self

    return Collector()


@pytest.fixture
def clean_root_logging():
    """Restore the root logger after a test that calls ``setup_logging()``."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)


requires_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod-based permission tests are meaningless for root",
)


# ===========================================================================
# SettingsManager - persistence
# ===========================================================================

def test_load_returns_false_and_keeps_defaults_when_file_is_missing(isolated_settings):
    """A first run has no settings file: load() reports False, defaults stay."""
    assert not isolated_settings.settings_file.exists()
    assert isolated_settings.load() is False
    assert isolated_settings.get_category("general") == DEFAULT_SETTINGS["general"]


def test_save_then_load_round_trips_every_json_type(isolated_settings):
    """Values survive a save/load cycle through a brand new manager."""
    payload = {
        "an_int": 7,
        "a_float": 1.5,
        "a_true": True,
        "a_false": False,
        "a_none": None,
        "a_list": [1, "two", None],
        "a_dict": {"nested": {"deep": 1}},
    }
    for key, value in payload.items():
        isolated_settings.set(key, value, category="general", autosave=False)
    assert isolated_settings.save() is True

    reloaded = _fresh_manager()
    for key, value in payload.items():
        assert reloaded.get(key, category="general") == value


def test_save_writes_diacritics_verbatim_and_load_restores_them(isolated_settings):
    """Czech diacritics survive the round trip and are not \\u-escaped on disk."""
    name = "Přemysl Žluťoučký Kůň"
    isolated_settings.set("last_ad_username", name, category="general")

    raw = isolated_settings.settings_file.read_text(encoding="utf-8")
    assert name in raw            # ensure_ascii=False
    assert "\\u" not in raw

    assert _fresh_manager().get_str("last_ad_username", category="general") == name


def test_load_merges_stored_values_with_defaults_for_missing_keys(isolated_settings):
    """A partial file keeps its own values and inherits every missing default."""
    isolated_settings.settings_file.write_text(
        json.dumps({"general": {"theme": "Solarized"}}), encoding="utf-8")

    manager = _fresh_manager()
    assert manager.get_str("theme", category="general") == "Solarized"
    assert manager.get_bool("show_group_management_warning", category="general") is True
    assert manager.get_category("logging") == DEFAULT_SETTINGS["logging"]


def test_load_keeps_unknown_categories_that_are_dicts(isolated_settings):
    """Categories the current version does not know about are preserved."""
    isolated_settings.settings_file.write_text(
        json.dumps({"plugins": {"enabled": ["a", "b"]}}), encoding="utf-8")

    manager = _fresh_manager()
    assert manager.load() is True
    assert manager.get_category("plugins") == {"enabled": ["a", "b"]}


def test_load_drops_unknown_categories_that_are_not_dicts(isolated_settings):
    """A non-dict top level entry is ignored instead of corrupting the store."""
    isolated_settings.settings_file.write_text(
        json.dumps({"plugins": ["not", "a", "dict"]}), encoding="utf-8")

    assert _fresh_manager().get_category("plugins") == {}


def test_load_restores_defaults_when_a_known_category_is_not_a_dict(isolated_settings):
    """``"general": "oops"`` falls back to the factory values for that category."""
    isolated_settings.settings_file.write_text(
        json.dumps({"general": "oops", "export": {"auto_generate_filename": False}}),
        encoding="utf-8")

    manager = _fresh_manager()
    assert manager.get_category("general") == DEFAULT_SETTINGS["general"]
    assert manager.get_bool("auto_generate_filename", category="export") is False


@pytest.mark.parametrize("body", ["", "   \n\t ", "{not json", "{'single': 'quotes'}"])
def test_load_returns_false_for_empty_or_corrupt_file(isolated_settings, body):
    """Empty and syntactically broken files degrade to the defaults."""
    isolated_settings.settings_file.write_text(body, encoding="utf-8")

    manager = _fresh_manager()
    assert manager.load() is False
    assert manager.get_category("general") == DEFAULT_SETTINGS["general"]


@pytest.mark.parametrize("document", ["[]", '"a string"', "null", "42", "true"])
def test_load_returns_false_for_non_dict_json_document(isolated_settings, document):
    """Valid JSON that is not an object is rejected."""
    isolated_settings.settings_file.write_text(document, encoding="utf-8")

    manager = _fresh_manager()
    assert manager.load() is False
    assert manager.get_category("logging") == DEFAULT_SETTINGS["logging"]


def test_load_discards_in_memory_changes_when_the_file_is_corrupt(isolated_settings):
    """A corrupt file resets the in-memory store to the defaults."""
    isolated_settings.set("theme", "Neon", category="general", autosave=False)
    isolated_settings.settings_file.write_text("{broken", encoding="utf-8")

    assert isolated_settings.load() is False
    assert isolated_settings.get_str("theme", category="general") == "Dark"


@requires_non_root
def test_load_returns_false_when_the_file_cannot_be_read(isolated_settings):
    """A settings file without read permission is reported, not raised."""
    isolated_settings.settings_file.write_text(
        json.dumps({"general": {"theme": "Blue"}}), encoding="utf-8")
    isolated_settings.settings_file.chmod(0o000)
    try:
        manager = _fresh_manager()
        assert manager.load() is False
        assert manager.get_str("theme", category="general") == "Dark"
    finally:
        isolated_settings.settings_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_load_is_idempotent(isolated_settings):
    """Loading twice yields exactly the same store."""
    isolated_settings.settings_file.write_text(
        json.dumps({"general": {"theme": "Blue"}, "plugins": {"x": 1}}),
        encoding="utf-8")

    manager = _fresh_manager()
    first = manager.get_category("general"), manager.get_category("plugins")
    assert manager.load() is True
    assert (manager.get_category("general"), manager.get_category("plugins")) == first


def test_save_is_atomic_and_removes_its_temporary_file(isolated_settings, tmp_path):
    """Only settings.json is left behind - no stray mkstemp leftovers."""
    assert isolated_settings.save() is True
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["settings.json"]
    assert isolated_settings.save() is True
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["settings.json"]


def test_save_reports_failure_for_a_value_json_cannot_encode(isolated_settings):
    """A set() of an unserialisable value returns False instead of raising."""
    assert isolated_settings.set("broken", {1, 2}, category="general") is False


@pytest.mark.bug
def test_unserialisable_value_does_not_poison_later_saves(isolated_settings):
    """One bad value must not make the settings file unwritable forever."""
    assert isolated_settings.set("theme", "Light", category="general") is True
    assert isolated_settings.set("broken", {1, 2}, category="general") is False

    assert isolated_settings.set("theme", "Blue", category="general") is True
    assert _disk(isolated_settings)["general"]["theme"] == "Blue"


def test_export_and_import_round_trip_including_unicode(isolated_settings, tmp_path):
    """export_settings/import_settings survive a full round trip."""
    isolated_settings.set("last_ad_server", "škola.local", category="general")
    target = tmp_path / "exported.json"
    assert isolated_settings.export_settings(str(target)) is True

    isolated_settings.set("last_ad_server", "other", category="general")
    assert isolated_settings.import_settings(str(target)) is True
    assert isolated_settings.get_str("last_ad_server", category="general") == "škola.local"


def test_import_settings_rejects_a_non_dict_document(isolated_settings, tmp_path):
    """A JSON array cannot be imported and leaves the store untouched."""
    target = tmp_path / "bad.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")

    assert isolated_settings.import_settings(str(target)) is False
    assert isolated_settings.get_str("theme", category="general") == "Dark"


# ===========================================================================
# SettingsManager - get / set / categories
# ===========================================================================

def test_get_returns_default_for_unknown_key_and_unknown_category(isolated_settings):
    """Missing keys and whole missing categories both fall back to *default*."""
    assert isolated_settings.get("nope", "fallback") == "fallback"
    assert isolated_settings.get("theme", "fallback", category="nosuch") == "fallback"
    assert isolated_settings.get("nope") is None


def test_set_creates_an_unknown_category_on_demand(isolated_settings):
    """Writing into a new category creates it and persists it."""
    assert isolated_settings.set("token", "abc", category="plugins") is True
    assert isolated_settings.get_category("plugins") == {"token": "abc"}
    assert _disk(isolated_settings)["plugins"] == {"token": "abc"}


def test_set_emits_settings_changed_and_category_changed(isolated_settings, signals):
    """Every single-key write notifies both signals with the new value."""
    signals.attach(isolated_settings)
    isolated_settings.set("theme", "Light", category="general")

    assert signals.changed == [("general", "theme", "Light")]
    assert signals.categories == ["general"]


def test_set_with_autosave_false_defers_the_write(isolated_settings, signals):
    """autosave=False keeps the value in memory until save() is called."""
    signals.attach(isolated_settings)
    assert isolated_settings.set("theme", "Light", autosave=False) is True
    assert not isolated_settings.settings_file.exists()
    assert signals.changed == [("general", "theme", "Light")]

    assert isolated_settings.save() is True
    assert _disk(isolated_settings)["general"]["theme"] == "Light"


def test_set_with_emit_false_stores_without_signalling(isolated_settings, signals):
    """_emit=False is a silent write."""
    signals.attach(isolated_settings)
    assert isolated_settings.set("theme", "Light", _emit=False) is True

    assert isolated_settings.get_str("theme") == "Light"
    assert signals.changed == []
    assert signals.categories == []


def test_get_category_returns_a_copy_of_the_top_level_mapping(isolated_settings):
    """Mutating the returned dict must not reach the store."""
    snapshot = isolated_settings.get_category("general")
    snapshot["theme"] = "Tampered"
    snapshot["brand_new"] = 1

    assert isolated_settings.get_str("theme") == "Dark"
    assert isolated_settings.has("brand_new") is False


def test_get_category_returns_empty_dict_for_unknown_category(isolated_settings):
    """An unknown category behaves like an empty one."""
    assert isolated_settings.get_category("nosuch") == {}


def test_set_category_replaces_the_whole_category(isolated_settings):
    """Keys missing from *values* are dropped, not merged."""
    assert isolated_settings.set_category("general", {"theme": "Light"}) is True

    assert isolated_settings.get_category("general") == {"theme": "Light"}
    assert isolated_settings.get("show_group_management_warning") is None


def test_set_category_signals_only_the_keys_that_really_changed(isolated_settings, signals):
    """Unchanged keys are silent; the category signal fires exactly once."""
    signals.attach(isolated_settings)
    isolated_settings.set_category("general", {
        "theme": "Dark",              # unchanged
        "last_ad_server": "dc01",     # changed
    })

    assert signals.changed == [("general", "last_ad_server", "dc01")]
    assert signals.categories == ["general"]


@pytest.mark.parametrize("values", [None, "abc", 42, 3.5])
def test_set_category_rejects_values_that_are_not_a_mapping(isolated_settings, values):
    """A non-mapping replacement is refused and the category is left intact."""
    before = isolated_settings.get_category("general")

    assert isolated_settings.set_category("general", values) is False
    assert isolated_settings.get_category("general") == before


# ===========================================================================
# SettingsManager - typed getters
# ===========================================================================

@pytest.mark.parametrize("stored,expected", [
    (True, True), (False, False), (1, True), (0, False),
    ("", False), ("text", True), ([], False), ([0], True), (None, False),
])
def test_get_bool_follows_python_truthiness(isolated_settings, stored, expected):
    """get_bool() coerces whatever is stored with bool()."""
    isolated_settings.set("flag", stored, autosave=False)
    assert isolated_settings.get_bool("flag") is expected


def test_get_bool_returns_the_default_for_a_missing_key(isolated_settings):
    """The default is used - and coerced - when the key is absent."""
    assert isolated_settings.get_bool("missing", default=True) is True
    assert isolated_settings.get_bool("missing") is False


@pytest.mark.parametrize("stored,expected", [
    (5, 5), ("5", 5), (5.9, 5), (True, 1), (" 7 ", 7),
])
def test_get_int_converts_convertible_values(isolated_settings, stored, expected):
    """Numeric strings and floats are converted to int."""
    isolated_settings.set("num", stored, autosave=False)
    assert isolated_settings.get_int("num", default=-1) == expected


@pytest.mark.parametrize("stored", ["abc", "3.5", None, [], {}, "", "1,5"])
def test_get_int_returns_default_for_unconvertible_values(isolated_settings, stored):
    """Anything int() refuses gives the default back instead of raising."""
    isolated_settings.set("num", stored, autosave=False)
    assert isolated_settings.get_int("num", default=-1) == -1


@pytest.mark.parametrize("stored,expected", [
    (1, 1.0), ("1.5", 1.5), ("1e3", 1000.0), (2.5, 2.5),
])
def test_get_float_converts_convertible_values(isolated_settings, stored, expected):
    """Ints and numeric strings become floats."""
    isolated_settings.set("num", stored, autosave=False)
    assert isolated_settings.get_float("num", default=-1.0) == expected


@pytest.mark.parametrize("stored", ["abc", None, [], {}, ""])
def test_get_float_returns_default_for_unconvertible_values(isolated_settings, stored):
    """Anything float() refuses gives the default back."""
    isolated_settings.set("num", stored, autosave=False)
    assert isolated_settings.get_float("num", default=-1.0) == -1.0


@pytest.mark.parametrize("stored,expected", [
    ("plain", "plain"), (5, "5"), (True, "True"), ([1, 2], "[1, 2]"),
    ("Žluťoučký", "Žluťoučký"),
])
def test_get_str_stringifies_whatever_is_stored(isolated_settings, stored, expected):
    """get_str() never returns a non-string for a present key."""
    isolated_settings.set("val", stored, autosave=False)
    assert isolated_settings.get_str("val", default="fallback") == expected


def test_get_str_returns_default_when_the_stored_value_is_none(isolated_settings):
    """A stored ``None`` is treated as "unset", not stringified to "None"."""
    isolated_settings.set("val", None, autosave=False)
    assert isolated_settings.get_str("val", default="fallback") == "fallback"


# ===========================================================================
# SettingsManager - has / delete / reset
# ===========================================================================

def test_has_searches_every_category(isolated_settings):
    """has() is category agnostic."""
    assert isolated_settings.has("log_level") is True      # lives in "logging"
    assert isolated_settings.has("theme") is True          # lives in "general"
    assert isolated_settings.has("definitely_absent") is False


def test_delete_removes_the_key_and_persists_the_removal(isolated_settings, signals):
    """A successful delete saves the file and announces the change."""
    isolated_settings.save()
    signals.attach(isolated_settings)

    assert isolated_settings.delete("theme", category="general") is True
    assert isolated_settings.has("theme") is False
    assert "theme" not in _disk(isolated_settings)["general"]
    assert signals.changed == [("general", "theme", None)]
    assert signals.categories == ["general"]


def test_delete_returns_false_and_stays_silent_for_a_missing_key(isolated_settings, signals):
    """Deleting something that is not there is a no-op."""
    signals.attach(isolated_settings)

    assert isolated_settings.delete("definitely_absent") is False
    assert signals.changed == []
    assert signals.categories == []


def test_delete_with_an_unknown_category_is_a_no_op(isolated_settings):
    """Naming a category that does not exist deletes nothing."""
    assert isolated_settings.delete("theme", category="nosuch") is False
    assert isolated_settings.get_str("theme") == "Dark"


@pytest.mark.bug
def test_delete_without_category_removes_the_key_from_every_category(isolated_settings):
    """The documented "or all categories if None" must really mean all of them."""
    isolated_settings.set("dupe", 1, category="general", autosave=False)
    isolated_settings.set("dupe", 2, category="export", autosave=False)

    assert isolated_settings.delete("dupe") is True
    assert "dupe" not in isolated_settings.get_category("general")
    assert "dupe" not in isolated_settings.get_category("export")


@pytest.mark.bug
def test_delete_of_a_key_stored_as_none_is_reported_and_persisted(isolated_settings):
    """A key whose value is ``None`` is still a key: removing it must persist."""
    isolated_settings.set("nullable", None, category="general")
    assert _disk(isolated_settings)["general"]["nullable"] is None

    assert isolated_settings.delete("nullable", category="general") is True
    assert "nullable" not in isolated_settings.get_category("general")
    assert "nullable" not in _disk(isolated_settings)["general"]


@pytest.mark.bug
def test_delete_announces_the_category_the_key_was_removed_from(isolated_settings, signals):
    """Listeners filtering on their own category must be told about the removal."""
    isolated_settings.set("only_here", 7, category="export", autosave=False)
    signals.attach(isolated_settings)

    assert isolated_settings.delete("only_here") is True
    assert signals.categories == ["export"]
    assert signals.changed == [("export", "only_here", None)]


def test_reset_to_defaults_restores_values_and_persists_them(isolated_settings):
    """Every category goes back to the factory content, on disk as well."""
    isolated_settings.set("theme", "Neon", category="general")
    isolated_settings.set("log_level", "DEBUG", category="logging")

    assert isolated_settings.reset_to_defaults() is True
    assert isolated_settings.get_str("theme") == "Dark"
    assert isolated_settings.get_str("log_level", category="logging") == "INFO"
    assert _disk(isolated_settings)["general"]["theme"] == "Dark"


def test_reset_to_defaults_emits_one_category_signal_per_default_category(
        isolated_settings, signals):
    """The UI is told to refresh each category exactly once."""
    signals.attach(isolated_settings)

    isolated_settings.reset_to_defaults()
    assert sorted(signals.categories) == sorted(DEFAULT_SETTINGS)


def test_reset_to_defaults_drops_custom_categories(isolated_settings):
    """Categories that are not part of the defaults are wiped, file included."""
    isolated_settings.set("token", "abc", category="plugins")

    assert isolated_settings.reset_to_defaults() is True
    assert isolated_settings.get_category("plugins") == {}
    assert "plugins" not in _disk(isolated_settings)


def test_reset_to_defaults_is_idempotent(isolated_settings):
    """Calling it twice changes nothing the second time."""
    isolated_settings.set("theme", "Neon", category="general")
    isolated_settings.reset_to_defaults()
    first = isolated_settings.get_category("general")

    assert isolated_settings.reset_to_defaults() is True
    assert isolated_settings.get_category("general") == first


@pytest.mark.bug
def test_nested_default_values_are_not_shared_with_the_module_defaults(isolated_settings):
    """Writing into a nested default value must not rewrite DEFAULT_SETTINGS."""
    geometry = isolated_settings.get("geometry", category="window")
    try:
        geometry["main_window"] = [0, 0, 800, 600]
        assert DEFAULT_SETTINGS["window"]["geometry"] == {}
        assert _fresh_manager().get("geometry", category="window") == {}
    finally:
        DEFAULT_SETTINGS["window"]["geometry"].clear()


# ===========================================================================
# SettingsManager - logging configuration facade
# ===========================================================================

def test_get_logging_config_returns_a_detached_copy(isolated_settings):
    """The caller cannot edit the store through the returned dict."""
    config = isolated_settings.get_logging_config()
    config["log_level"] = "TAMPERED"

    assert isolated_settings.get_str("log_level", category="logging") == "INFO"


def test_update_logging_config_applies_and_persists_valid_values(isolated_settings, signals):
    """A valid update reaches memory, disk and the category signal."""
    signals.attach(isolated_settings)

    assert isolated_settings.update_logging_config(
        log_level="DEBUG", backup_count=0, max_bytes=2048) is True

    stored = isolated_settings.get_logging_config()
    assert (stored["log_level"], stored["backup_count"], stored["max_bytes"]) == \
        ("DEBUG", 0, 2048)
    assert _disk(isolated_settings)["logging"]["log_level"] == "DEBUG"
    assert signals.categories == ["logging"]


@pytest.mark.parametrize("kwargs", [
    {"log_level": "TRACE"},
    {"log_level": "debug"},
    {"log_level": ""},
    {"log_level": None},
    {"max_bytes": 0},
    {"max_bytes": -1},
    {"max_bytes": "abc"},
    {"max_bytes": None},
    {"backup_count": -1},
    {"backup_count": "abc"},
    {"log_dir": ""},
    {"log_dir": "   "},
    {"log_dir": None},
    {"log_dir": 5},
    {"log_dir": "../escape"},
    {"log_file": "bad|name.log"},
    {"log_file": "a\0b"},
])
def test_update_logging_config_rejects_invalid_values(isolated_settings, kwargs):
    """Every invalid value is refused and nothing is written."""
    before = isolated_settings.get_logging_config()

    assert isolated_settings.update_logging_config(**kwargs) is False
    assert isolated_settings.get_logging_config() == before
    assert not isolated_settings.settings_file.exists()


@pytest.mark.parametrize("value,expected", [("2048", 2048), (4096.0, 4096), (1, 1)])
def test_update_logging_config_coerces_numeric_max_bytes(isolated_settings, value, expected):
    """Numbers arriving as text from a QLineEdit are converted, not rejected."""
    assert isolated_settings.update_logging_config(max_bytes=value) is True
    assert isolated_settings.get_logging_config()["max_bytes"] == expected


def test_update_logging_config_ignores_unknown_keys_but_applies_known_ones(isolated_settings):
    """An unknown key is dropped with a warning; the rest still applies."""
    assert isolated_settings.update_logging_config(
        log_level="WARNING", not_a_setting=123) is True

    config = isolated_settings.get_logging_config()
    assert config["log_level"] == "WARNING"
    assert "not_a_setting" not in config


def test_update_logging_config_is_all_or_nothing(isolated_settings):
    """One invalid value must not leave the earlier ones half applied."""
    assert isolated_settings.update_logging_config(
        log_level="ERROR", max_bytes=0) is False

    assert isolated_settings.get_logging_config() == DEFAULT_SETTINGS["logging"]


@pytest.mark.parametrize("path", ["/var/log/app", "C:\\Logs", "logs/třída"])
def test_update_logging_config_accepts_browsable_absolute_paths(isolated_settings, path):
    """A folder picked with "Browse..." must be storable (the old code refused it)."""
    assert isolated_settings.update_logging_config(log_dir=path) is True
    assert isolated_settings.get_logging_config()["log_dir"] == path


# ===========================================================================
# Path validation - both implementations must agree
# ===========================================================================

PATH_CASES = [
    # (value, expected, id)
    ("logs", True, "relative"),
    ("logs/app", True, "relative-nested"),
    ("student_management.log", True, "plain-filename"),
    ("/var/log/app", True, "absolute-posix"),
    ("C:\\Logs", True, "windows-drive"),
    ("C:", True, "windows-drive-root"),
    ("\\\\server\\share\\logs", True, "unc"),
    ("my..logs", True, "double-dot-inside-segment"),
    ("logs/třída/záznam.log", True, "diacritics"),
    ("  logs  ", True, "surrounding-whitespace"),
    ("", False, "empty"),
    ("   ", False, "whitespace-only"),
    (None, False, "none"),
    (5, False, "not-a-string"),
    ("..", False, "traversal-alone"),
    ("../evil", False, "traversal-leading"),
    ("logs/../etc", False, "traversal-middle"),
    ("logs\\..\\etc", False, "traversal-backslash"),
    ("C:\\a:b", False, "two-colons"),
    ("1:\\logs", False, "digit-drive-letter"),
    (":\\logs", False, "leading-colon"),
    ("logs/a:b", False, "colon-inside-path"),
    ("log\0s", False, "nul-byte"),
    ("a<b", False, "angle-open"),
    ("a>b", False, "angle-close"),
    ('a"b', False, "double-quote"),
    ("a|b", False, "pipe"),
    ("a?b", False, "question-mark"),
    ("a*b", False, "asterisk"),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    "path,expected",
    [(value, expected) for value, expected, _ in PATH_CASES],
    ids=[name for _, _, name in PATH_CASES],
)
def test_validate_path_classifies_values_identically_in_both_modules(
        path, expected, isolated_logging_config):
    """SettingsManager._validate_path and LoggingConfig.validate_path must agree."""
    assert SettingsManager._validate_path(path) is expected
    assert isolated_logging_config.validate_path(path) is expected


# ===========================================================================
# LoggingConfig - load / save
# ===========================================================================

def test_load_config_returns_the_defaults_when_no_file_exists(tmp_path):
    """A missing config file is not an error."""
    config = LoggingConfig(config_file=str(tmp_path / "absent.json"))
    assert config.config == LoggingConfig.DEFAULT_CONFIG


def test_instance_config_is_detached_from_the_class_defaults(tmp_path):
    """Editing one instance must not change DEFAULT_CONFIG for the next one."""
    config = LoggingConfig(config_file=str(tmp_path / "absent.json"))
    config.config["log_level"] = "CRITICAL"
    config.config["log_dir"] = str(tmp_path)

    assert LoggingConfig.DEFAULT_CONFIG["log_level"] == "INFO"
    assert LoggingConfig(config_file=str(tmp_path / "absent.json")).config["log_level"] \
        == "INFO"


def test_load_config_merges_stored_keys_over_the_defaults(tmp_path):
    """Stored keys win, missing ones are filled in, extra ones are kept."""
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(json.dumps({
        "log_level": "ERROR", "backup_count": 2, "future_key": "kept",
    }), encoding="utf-8")

    config = LoggingConfig(config_file=str(config_file))
    assert config.config["log_level"] == "ERROR"
    assert config.config["backup_count"] == 2
    assert config.config["future_key"] == "kept"
    assert config.config["log_file"] == LoggingConfig.DEFAULT_CONFIG["log_file"]


@pytest.mark.parametrize("body", ["", "{oops", "[1, 2]", "null", '"a string"', "17"])
def test_load_config_falls_back_to_defaults_for_broken_documents(tmp_path, body):
    """Empty, corrupt and non-object documents all yield the defaults."""
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(body, encoding="utf-8")

    assert LoggingConfig(config_file=str(config_file)).config \
        == LoggingConfig.DEFAULT_CONFIG


@pytest.mark.parametrize("level", ["TRACE", "info", "", None, 10])
def test_load_config_replaces_an_invalid_log_level_with_info(tmp_path, level):
    """An unusable level in the file is corrected instead of crashing later."""
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(json.dumps({"log_level": level}), encoding="utf-8")

    assert LoggingConfig(config_file=str(config_file)).config["log_level"] == "INFO"


@pytest.mark.parametrize("key,bad_value", [
    ("log_dir", "../escape"),
    ("log_dir", ""),
    ("log_dir", 42),
    ("log_file", "we|ird.log"),
    ("log_file", None),
])
def test_load_config_replaces_invalid_paths_with_the_defaults(tmp_path, key, bad_value):
    """A path the validator rejects is swapped for the default one."""
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(json.dumps({key: bad_value}), encoding="utf-8")

    config = LoggingConfig(config_file=str(config_file))
    assert config.config[key] == LoggingConfig.DEFAULT_CONFIG[key]


@requires_non_root
def test_load_config_falls_back_when_the_file_cannot_be_read(tmp_path):
    """An unreadable config file degrades to the defaults."""
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(json.dumps({"log_level": "ERROR"}), encoding="utf-8")
    config_file.chmod(0o000)
    try:
        assert LoggingConfig(config_file=str(config_file)).config["log_level"] == "INFO"
    finally:
        config_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_load_config_falls_back_when_the_path_is_a_directory(tmp_path):
    """Pointing the config at a directory is handled, not raised."""
    directory = tmp_path / "config_dir"
    directory.mkdir()

    assert LoggingConfig(config_file=str(directory)).config \
        == LoggingConfig.DEFAULT_CONFIG


def test_save_config_round_trips_through_load_config(isolated_logging_config, tmp_path):
    """What save_config() writes is exactly what load_config() reads back."""
    config = isolated_logging_config
    config.config["log_file"] = "záznam.log"
    config.config["backup_count"] = 3

    assert config.save_config() is True
    assert LoggingConfig(config_file=config.config_file).config == config.config
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["logging_config.json"]


def test_save_config_overwrites_the_previous_file(isolated_logging_config):
    """Saving twice leaves a single, up to date document."""
    config = isolated_logging_config
    assert config.save_config() is True
    config.config["log_level"] = "ERROR"
    assert config.save_config() is True

    stored = json.loads(Path(config.config_file).read_text(encoding="utf-8"))
    assert stored["log_level"] == "ERROR"


def test_save_config_returns_false_for_an_unserialisable_config(isolated_logging_config):
    """A Path object in the config is reported, not raised."""
    isolated_logging_config.config["log_dir"] = Path("logs")
    assert isolated_logging_config.save_config() is False


@requires_non_root
def test_save_config_returns_false_when_the_directory_is_not_writable(tmp_path):
    """A read-only target directory yields False instead of an exception."""
    directory = tmp_path / "readonly"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        config = LoggingConfig(config_file=str(directory / "logging_config.json"))
        assert config.save_config() is False
    finally:
        directory.chmod(0o700)


# ===========================================================================
# LoggingConfig - directories, levels and update_config
# ===========================================================================

def test_ensure_log_dir_creates_the_directory_and_is_idempotent(isolated_logging_config):
    """The log directory is created once and re-created calls stay True."""
    config = isolated_logging_config
    log_dir = Path(config.config["log_dir"])
    assert not log_dir.exists()

    assert config.ensure_log_dir() is True
    assert log_dir.is_dir()
    assert config.ensure_log_dir() is True


def test_ensure_log_dir_creates_missing_parents(isolated_logging_config, tmp_path):
    """A nested log directory is created with all its parents."""
    config = isolated_logging_config
    config.config["log_dir"] = str(tmp_path / "a" / "b" / "logs")

    assert config.ensure_log_dir() is True
    assert Path(config.config["log_dir"]).is_dir()


def test_ensure_log_dir_returns_false_when_the_path_is_a_file(isolated_logging_config,
                                                              tmp_path):
    """An existing file where the directory should be is reported as failure."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    isolated_logging_config.config["log_dir"] = str(blocker)

    assert isolated_logging_config.ensure_log_dir() is False


def test_get_log_file_path_joins_directory_and_file(isolated_logging_config, tmp_path):
    """The full path is the configured directory plus the configured file name."""
    isolated_logging_config.config["log_file"] = "záznam.log"

    assert isolated_logging_config.get_log_dir() == tmp_path / "logs"
    assert isolated_logging_config.get_log_file_path() == tmp_path / "logs" / "záznam.log"


@pytest.mark.parametrize("name,expected", [
    ("DEBUG", logging.DEBUG), ("INFO", logging.INFO), ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR), ("CRITICAL", logging.CRITICAL),
])
def test_get_log_level_maps_known_names(isolated_logging_config, name, expected):
    """Every documented level name maps to the logging module constant."""
    assert isolated_logging_config.get_log_level(name) == expected


@pytest.mark.parametrize("name", ["TRACE", "debug", "", None, 20])
def test_get_log_level_falls_back_to_info(isolated_logging_config, name):
    """Anything unknown degrades to INFO instead of raising AttributeError."""
    assert isolated_logging_config.get_log_level(name) == logging.INFO


def test_update_config_persists_and_applies_valid_changes(isolated_logging_config,
                                                          clean_root_logging):
    """A valid update is stored, written and applied to the root logger."""
    config = isolated_logging_config

    assert config.update_config(log_level="DEBUG", backup_count=2,
                                console_enabled=False) is True

    stored = json.loads(Path(config.config_file).read_text(encoding="utf-8"))
    assert stored["log_level"] == "DEBUG"
    assert stored["backup_count"] == 2
    assert logging.getLogger().level == logging.DEBUG
    assert config.get_log_file_path().exists()


@pytest.mark.parametrize("kwargs", [
    {"log_level": "TRACE"},
    {"log_level": "debug"},
    {"log_level": None},
    {"max_bytes": 0},
    {"max_bytes": -1},
    {"max_bytes": "1024"},
    {"max_bytes": 1024.0},
    {"backup_count": -1},
    {"backup_count": "3"},
    {"log_dir": ""},
    {"log_dir": None},
    {"log_dir": "../escape"},
    {"log_file": "we|ird.log"},
])
def test_update_config_rejects_invalid_values_without_touching_state(
        isolated_logging_config, kwargs):
    """Rejected updates change neither the in-memory config nor the file."""
    config = isolated_logging_config
    before = dict(config.config)

    assert config.update_config(**kwargs) is False
    assert config.config == before
    assert not Path(config.config_file).exists()


def test_update_config_accepts_zero_backup_count(isolated_logging_config,
                                                 clean_root_logging):
    """backup_count == 0 (keep no rotations) is a legal boundary value."""
    assert isolated_logging_config.update_config(backup_count=0) is True
    assert isolated_logging_config.config["backup_count"] == 0


@pytest.mark.parametrize("kwargs", [{}, {"nonsense": 1}, {"nonsense": 1, "other": 2}])
def test_update_config_returns_false_when_no_valid_key_is_given(isolated_logging_config,
                                                                kwargs):
    """Nothing to do is reported as False."""
    config = isolated_logging_config
    before = dict(config.config)

    assert config.update_config(**kwargs) is False
    assert config.config == before


def test_update_config_ignores_unknown_keys_but_applies_the_valid_ones(
        isolated_logging_config, clean_root_logging):
    """Unknown keys are filtered out; the recognised ones still take effect."""
    config = isolated_logging_config

    assert config.update_config(log_level="WARNING", nonsense=1) is True
    assert config.config["log_level"] == "WARNING"
    assert "nonsense" not in config.config


def test_update_config_reverts_when_the_file_cannot_be_saved(isolated_logging_config,
                                                             monkeypatch):
    """A failing save rolls the configuration back to its previous state."""
    config = isolated_logging_config
    before = dict(config.config)
    monkeypatch.setattr(config, "save_config", lambda: False)

    assert config.update_config(log_level="ERROR") is False
    assert config.config == before


@pytest.mark.bug
def test_update_config_rejects_a_boolean_max_bytes(isolated_logging_config,
                                                   clean_root_logging):
    """A bool is not a byte count and must be refused like any other bad type."""
    config = isolated_logging_config

    assert config.update_config(max_bytes=True) is False
    assert config.config["max_bytes"] == LoggingConfig.DEFAULT_CONFIG["max_bytes"]


# ===========================================================================
# LoggingConfig - get_log_files
# ===========================================================================

def test_get_log_files_returns_empty_list_when_the_directory_is_missing(
        isolated_logging_config):
    """No log directory yet means no log files."""
    assert isolated_logging_config.get_log_files() == []


def test_get_log_files_returns_empty_list_for_an_empty_directory(isolated_logging_config):
    """An existing but empty directory yields an empty list."""
    isolated_logging_config.ensure_log_dir()
    assert isolated_logging_config.get_log_files() == []


def test_get_log_files_lists_current_and_rotated_files_newest_first(
        isolated_logging_config):
    """Current and rotated files are returned, sorted by modification time."""
    config = isolated_logging_config
    config.config["backup_count"] = 2
    log_dir = Path(config.config["log_dir"])
    log_dir.mkdir(parents=True)
    name = config.config["log_file"]

    (log_dir / name).write_text("current", encoding="utf-8")
    (log_dir / f"{name}.1").write_text("older", encoding="utf-8")
    (log_dir / f"{name}.2").write_text("oldest!", encoding="utf-8")
    os.utime(log_dir / name, (3000, 3000))
    os.utime(log_dir / f"{name}.1", (2000, 2000))
    os.utime(log_dir / f"{name}.2", (1000, 1000))

    files = config.get_log_files()
    assert [f["name"] for f in files] == [name, f"{name}.1", f"{name}.2"]
    assert [f["is_current"] for f in files] == [True, False, False]
    assert files[0]["size"] == len("current")
    assert files[2]["size"] == len("oldest!")
    assert files[0]["modified"] > files[1]["modified"] > files[2]["modified"]
    assert files[0]["path"] == log_dir / name


def test_get_log_files_ignores_rotations_beyond_backup_count(isolated_logging_config):
    """Only ``backup_count`` rotations are reported."""
    config = isolated_logging_config
    config.config["backup_count"] = 1
    log_dir = Path(config.config["log_dir"])
    log_dir.mkdir(parents=True)
    name = config.config["log_file"]
    for index, suffix in enumerate(("", ".1", ".2", ".3")):
        rotation = log_dir / f"{name}{suffix}"
        rotation.write_text("x", encoding="utf-8")
        os.utime(rotation, (5000 - index, 5000 - index))

    assert [f["name"] for f in config.get_log_files()] == [name, f"{name}.1"]


def test_get_log_files_ignores_unrelated_files(isolated_logging_config):
    """Files that are not this log (or its rotations) are not listed."""
    config = isolated_logging_config
    log_dir = Path(config.config["log_dir"])
    log_dir.mkdir(parents=True)
    (log_dir / "other.log").write_text("x", encoding="utf-8")
    (log_dir / f"{config.config['log_file']}.old").write_text("x", encoding="utf-8")

    assert config.get_log_files() == []


def test_get_log_files_reports_only_files_that_exist(isolated_logging_config):
    """Missing rotations are skipped without leaving holes in the list."""
    config = isolated_logging_config
    config.config["backup_count"] = 3
    log_dir = Path(config.config["log_dir"])
    log_dir.mkdir(parents=True)
    name = config.config["log_file"]
    (log_dir / f"{name}.2").write_text("only rotation two", encoding="utf-8")

    files = config.get_log_files()
    assert [f["name"] for f in files] == [f"{name}.2"]
    assert files[0]["is_current"] is False


@pytest.mark.bug
def test_get_log_files_survives_a_non_integer_backup_count_from_the_file(tmp_path):
    """A hand edited config must not turn the log viewer into a crash."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("x", encoding="utf-8")
    config_file = tmp_path / "logging_config.json"
    config_file.write_text(json.dumps({
        "backup_count": "3", "log_dir": str(log_dir), "log_file": "app.log",
    }), encoding="utf-8")

    config = LoggingConfig(config_file=str(config_file))
    assert [f["name"] for f in config.get_log_files()] == ["app.log"]


def test_get_logging_config_returns_the_isolated_singleton(isolated_logging_config):
    """The module level accessor hands out the very same instance every time."""
    assert lc.get_logging_config() is isolated_logging_config
    assert lc.get_logging_config() is lc.get_logging_config()
