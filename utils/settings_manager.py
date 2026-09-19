"""
Settings Manager — Extended Version with Qt Signal Propagation
Persistent storage for all application settings using JSON.
Supports structured categories, defaults, robust error handling,
and Qt signal/slot based change propagation.

Signals
-------
settings_changed(category: str, key: str, value: Any)
    Emitted whenever a single key inside any category changes.

category_changed(category: str)
    Emitted whenever any key inside a specific category changes,
    or when an entire category is replaced at once.
"""

import logging
import json
import os
import sys
import tempfile
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal, QStandardPaths

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default settings organised by category
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: Dict[str, Dict[str, Any]] = {
    "general": {
        "show_group_management_warning": True,
        # Append "(read-only)" / "(editable)" to every source name shown in a
        # source combo box, so the user sees before picking a source whether
        # it can be edited at all.
        "show_source_access_in_lists": True,
        "theme": "Dark",
        "last_ad_server": "",
        "last_ad_base_dn": "",
        "last_ad_username": "",
    },
    "window": {
        "geometry": {},
    },
    "logging": {
        "log_dir": "logs",
        "log_file": "student_management.log",
        "max_bytes": 10 * 1024 * 1024,
        "backup_count": 5,
        "log_level": "INFO",
        "console_enabled": True,
        "file_enabled": True,
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "date_format": "%Y-%m-%d %H:%M:%S",
    },
    "export": {
        "default_encryption_method": "aes-gcm",
        "auto_generate_filename": True,
    },
}

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class SettingsManager(QObject):
    """
    Manager for persistent application settings with category support
    and Qt signal/slot based change propagation.

    Two signals are provided:

    settings_changed(category, key, value)
        Emitted for every individual key change, regardless of category.

    category_changed(category)
        Emitted when any key inside *category* changes, or when the
        entire category dict is replaced.  Listeners that care only
        about a particular section can connect to this signal and
        filter by the category argument.
    """

    # Emitted whenever any single setting changes.
    # Arguments: (category: str, key: str, new_value: Any)
    settings_changed = pyqtSignal(str, str, object)

    # Emitted whenever any key inside a specific category changes.
    # Argument: (category: str)
    category_changed = pyqtSignal(str)

    def __init__(self, app_name: str = "StudentManagementSystem", parent=None):
        super().__init__(parent)
        self.app_name = app_name
        self._settings: Dict[str, Dict[str, Any]] = {}
        self.settings_file = self._get_settings_file()
        self._load_defaults()
        self.load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_settings_file(self) -> Path:
        try:
            app_data_dir = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
            if not app_data_dir:
                raise ValueError("Empty AppDataLocation")
            settings_dir = Path(app_data_dir)
        except Exception:
            settings_dir = Path.home() / f".{self.app_name}"

        try:
            settings_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"Warning: Cannot create settings directory: {e}", file=sys.stderr)
            settings_dir = Path(tempfile.gettempdir()) / self.app_name
            try:
                settings_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                settings_dir = Path.cwd()

        return settings_dir / "settings.json"

    def _load_defaults(self) -> None:
        """
        Populate the in-memory store from DEFAULT_SETTINGS.

        A deep copy is required: ``dict(vals)`` copies only the top level, so
        the nested dictionaries (``window.geometry``) stayed shared with the
        module-level DEFAULT_SETTINGS. Writing a window geometry therefore
        mutated the defaults themselves, and every later "reset to defaults"
        restored the modified value.
        """
        self._settings = {cat: deepcopy(vals) for cat, vals in DEFAULT_SETTINGS.items()}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> bool:
        """Load settings from the JSON file, merging with defaults."""
        if not self.settings_file.exists():
            logger.info("Settings file not found; using defaults.")
            return False
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                raw = f.read()
            if not raw.strip():
                return False
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                logger.error("Settings file has invalid format; using defaults.")
                return False
            for cat, defaults in DEFAULT_SETTINGS.items():
                if cat in loaded and isinstance(loaded[cat], dict):
                    merged = dict(defaults)
                    merged.update(loaded[cat])
                    self._settings[cat] = merged
                else:
                    self._settings[cat] = dict(defaults)
            for cat, values in loaded.items():
                if cat not in self._settings and isinstance(values, dict):
                    self._settings[cat] = dict(values)
            logger.info("Settings loaded from: %s", self.settings_file)
            return True
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in settings file: %s", e)
        except PermissionError:
            logger.error("Permission denied reading settings file.")
        except OSError as e:
            logger.error("OS error reading settings: %s", e)
        except Exception:
            logger.exception("Unexpected error loading settings")
        self._load_defaults()
        return False

    def save(self) -> bool:
        """Persist current settings to the JSON file atomically."""
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            logger.error("Cannot create settings directory: %s", e)
            return False

        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json",
                dir=str(self.settings_file.parent),
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
            shutil.move(tmp_path, str(self.settings_file))
            tmp_path = None
            logger.info("Settings saved to: %s", self.settings_file)
            return True
        except PermissionError:
            logger.error("Permission denied writing settings file.")
        except OSError as e:
            logger.error("OS error saving settings: %s", e)
        except Exception:
            logger.exception("Unexpected error saving settings")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return False

    # ------------------------------------------------------------------
    # Category API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None, category: str = "general") -> Any:
        """Return the value of *key* within *category*, or *default*."""
        try:
            return self._settings.get(category, {}).get(key, default)
        except Exception:
            return default

    def set(
        self,
        key: str,
        value: Any,
        category: str = "general",
        *,
        autosave: bool = True,
        _emit: bool = True,
    ) -> bool:
        """
        Set *key* = *value* inside *category*.

        Emits :attr:`settings_changed` (category, key, value) and
        :attr:`category_changed` (category) after the value is stored.
        """
        try:
            # Reject a value the settings file cannot hold BEFORE storing it.
            # Previously such a value stayed in the store and made every
            # subsequent save() fail, silently freezing all settings.
            try:
                json.dumps({key: value})
            except (TypeError, ValueError) as exc:
                logger.error("Refusing to store non-serialisable setting "
                             "[%s][%s]: %s", category, key, exc)
                return False

            if category not in self._settings:
                self._settings[category] = {}
            self._settings[category][key] = value
            saved = self.save() if autosave else True
            if _emit:
                self.settings_changed.emit(category, key, value)
                self.category_changed.emit(category)
            return saved
        except Exception:
            logger.exception("Error setting [%s][%s]", category, key)
            return False

    def get_category(self, category: str) -> Dict[str, Any]:
        """Return a shallow copy of all key/value pairs in *category*."""
        try:
            return dict(self._settings.get(category, {}))
        except Exception:
            return {}

    def set_category(
        self,
        category: str,
        values: Dict[str, Any],
        *,
        autosave: bool = True,
        _emit: bool = True,
    ) -> bool:
        """
        Replace the entire *category* dict with *values*.

        Emits :attr:`category_changed` (category) and one
        :attr:`settings_changed` per key that actually changed value.
        """
        try:
            old = self._settings.get(category, {})
            self._settings[category] = dict(values)
            saved = self.save() if autosave else True
            if _emit:
                for k, v in values.items():
                    if old.get(k) != v:
                        self.settings_changed.emit(category, k, v)
                self.category_changed.emit(category)
            return saved
        except Exception:
            logger.exception("Error setting category [%s]", category)
            return False

    # ------------------------------------------------------------------
    # Typed helpers
    # ------------------------------------------------------------------

    def get_bool(self, key: str, default: bool = False, category: str = "general") -> bool:
        try:
            return bool(self.get(key, default, category))
        except Exception:
            return default

    def get_str(self, key: str, default: str = "", category: str = "general") -> str:
        val = self.get(key, default, category)
        return str(val) if val is not None else default

    def get_int(self, key: str, default: int = 0, category: str = "general") -> int:
        try:
            return int(self.get(key, default, category))
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, default: float = 0.0, category: str = "general") -> float:
        try:
            return float(self.get(key, default, category))
        except (ValueError, TypeError):
            return default

    # ------------------------------------------------------------------
    # Legacy flat-key helpers (backward compat)
    # ------------------------------------------------------------------

    def has(self, key: str) -> bool:
        """Return True if *key* exists in any category."""
        return any(key in cat_vals for cat_vals in self._settings.values())

    def delete(self, key: str, category: Optional[str] = None) -> bool:
        """
        Delete *key* from *category*, or from every category when it is None.

        Three defects are fixed here:

        * ``any(... for ...)`` short-circuits, so the key was removed only from
          the FIRST category that contained it and stayed in the others;
        * a key whose value was ``None`` popped as ``None`` and was judged
          "not found", so it disappeared from memory but was never written out
          and came back on the next start;
        * the change was always announced for category ``"general"``, so the
          listeners of the category that really changed never refreshed.

        Args:
            key: The setting to remove.
            category: Restrict the deletion to this category.

        Returns:
            True when the key was removed AND the file was written.
        """
        try:
            categories = [category] if category else list(self._settings.keys())

            touched = []
            for name in categories:
                values = self._settings.get(name)
                if isinstance(values, dict) and key in values:
                    del values[key]
                    touched.append(name)

            if not touched:
                return False

            saved = self.save()

            for name in touched:
                self.settings_changed.emit(name, key, None)
                self.category_changed.emit(name)

            return saved
        except Exception:
            logger.exception("Error deleting setting")
            return False

    def reset_to_defaults(self) -> bool:
        """Reset all settings to factory defaults and emit change signals."""
        self._load_defaults()
        saved = self.save()
        for cat in self._settings:
            self.category_changed.emit(cat)
        return saved

    # ------------------------------------------------------------------
    # Logging config integration
    # ------------------------------------------------------------------

    def get_logging_config(self) -> Dict[str, Any]:
        """Return the full logging configuration dict."""
        return self.get_category("logging")

    def update_logging_config(self, **kwargs) -> bool:
        """
        Update one or more logging configuration values with validation.
        Emits :attr:`category_changed` (\"logging\") on success.
        """
        current = self.get_category("logging")
        defaults = DEFAULT_SETTINGS["logging"]
        for key, value in kwargs.items():
            if key not in defaults:
                logger.warning("Unknown logging key ignored: %s", key)
                continue
            if key == "log_level" and value not in VALID_LOG_LEVELS:
                logger.error("Invalid log_level: %s", value)
                return False
            if key == "max_bytes":
                try:
                    value = int(value)
                    assert value > 0
                except Exception:
                    logger.error("Invalid max_bytes: %s", value)
                    return False
            if key == "backup_count":
                try:
                    value = int(value)
                    assert value >= 0
                except Exception:
                    logger.error("Invalid backup_count: %s", value)
                    return False
            if key in ("log_dir", "log_file") and not self._validate_path(value):
                logger.error("Invalid path for '%s': %s", key, value)
                return False
            current[key] = value
        return self.set_category("logging", current)

    @staticmethod
    def _validate_path(path: str) -> bool:
        """
        Check that a configured log path is usable.

        The previous implementation rejected *every* absolute path (anything
        starting with ``/`` or ``\\``, and anything containing ``:``), which
        made it impossible to store a log directory chosen with the "Browse…"
        button: ``C:\\Logs`` and ``/var/log/app`` were both refused, the
        update silently returned False and the settings dialog still reported
        "Settings saved successfully".

        Absolute paths are now accepted; only genuinely invalid values are
        rejected.

        Args:
            path: The value configured for ``log_dir`` or ``log_file``.

        Returns:
            True when the value can be used as a path.
        """
        if not path or not isinstance(path, str):
            return False

        candidate = path.strip()
        if not candidate:
            return False

        if "\0" in candidate:
            return False

        # Reject path traversal, but only as a complete segment: a directory
        # called "my..logs" is perfectly fine.
        segments = candidate.replace("\\", "/").split("/")
        if any(segment == ".." for segment in segments):
            return False

        # Characters that are invalid on Windows.  A colon is allowed only as
        # the drive separator ("C:\\...").
        if any(c in candidate for c in '<>"|?*'):
            return False

        colon_index = candidate.find(":")
        if colon_index != -1 and colon_index != 1:
            return False
        if colon_index == 1 and not candidate[0].isalpha():
            return False
        if candidate.count(":") > 1:
            return False

        return True

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------

    def export_settings(self, file_path: str) -> bool:
        """Export current settings to a JSON file."""
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            logger.exception("Error exporting settings")
            return False

    def import_settings(self, file_path: str) -> bool:
        """Import settings from a JSON file, merging with current values."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                imported = json.load(f)
            if not isinstance(imported, dict):
                return False
            for cat, values in imported.items():
                if isinstance(values, dict):
                    existing = self._settings.get(cat, {})
                    existing.update(values)
                    self._settings[cat] = existing
                    self.category_changed.emit(cat)
            return self.save()
        except Exception:
            logger.exception("Error importing settings")
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_settings_instance: Optional[SettingsManager] = None


def get_settings() -> SettingsManager:
    """Return the application-wide :class:`SettingsManager` singleton."""
    global _settings_instance
    if _settings_instance is None:
        try:
            _settings_instance = SettingsManager()
        except Exception as e:
            print(f"Fatal: Could not initialize SettingsManager: {e}", file=sys.stderr)
            # Emergency fallback without persistence
            inst = SettingsManager.__new__(SettingsManager)
            QObject.__init__(inst)
            inst.app_name = "StudentManagementSystem"
            inst._settings = {cat: dict(vals) for cat, vals in DEFAULT_SETTINGS.items()}
            inst.settings_file = Path(tempfile.gettempdir()) / "settings.json"
            _settings_instance = inst
    return _settings_instance
