"""
LogDirectoryWidget
A dedicated widget for configuring where and how log files are stored.

Unlike the generic FilePathSelectorWidget, this widget separates the
configuration into three distinct, independently editable parts:

    (a) Base path — the directory in which the log *folder* will be created.
    (b) Folder name — the name of the sub-directory that holds all log files.
    (c) Log filename — the name of the rotating log file itself.

The widget always shows a read-only preview of the full resulting path so
the user can instantly see what the combined value looks like.

Design notes
------------
• Each field has its own "Browse…" / "Reset to default" affordance.
• Switching between the fields never resets the others.
• The base path is always shown as a real, absolute directory. Leaving it
  empty is not a hidden state any more: the widget fills in the application
  directory so the user can see where the logs actually end up.
• The widget never writes to SettingsManager directly; the parent widget
  reads the three individual values via get_base_path(), get_folder_name(),
  and get_log_filename() (or the combined get_full_path()).
• validate() reports every problem that would make logging fail, so the
  caller can refuse to save an unusable configuration instead of silently
  losing the file log.
"""

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)

_DEFAULT_FOLDER_NAME = "logs"
_DEFAULT_LOG_FILE = "student_management.log"

#: Characters that are not allowed in a path on Windows (and confusing on any
#: other platform).  The drive colon is handled separately.
_INVALID_PATH_CHARS = '<>"|?*'

#: Characters that are never allowed inside a bare file name.
_INVALID_FILENAME_CHARS = '<>:"|?*/\\'


def get_application_directory() -> str:
    """
    Return the directory the application runs from.

    Uses the location of the executable when the application is frozen
    (PyInstaller) and the current working directory otherwise, which is what
    a relative ``logs/`` path resolves against.

    Returns:
        Absolute path as a string.
    """
    try:
        if getattr(sys, 'frozen', False):
            return str(Path(sys.executable).resolve().parent)
        return str(Path.cwd().resolve())
    except Exception:                                # pragma: no cover - defensive
        logger.exception("Could not determine the application directory")
        return str(Path.cwd())


class LogDirectoryWidget(QWidget):
    """
    Three-part log-path configurator:

        base_path / folder_name / log_filename

    Signals
    -------
    path_changed(str)
        Emitted whenever any of the three components changes.
        Carries the full combined path as a convenience.
    """

    path_changed = pyqtSignal(str)

    def __init__(
        self,
        default_base_path: str = "",
        default_folder_name: str = _DEFAULT_FOLDER_NAME,
        default_log_filename: str = _DEFAULT_LOG_FILE,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        # An empty base path means "the application directory" - show it
        # instead of hiding it behind a placeholder.
        self._default_base = default_base_path or get_application_directory()
        self._default_folder = default_folder_name or _DEFAULT_FOLDER_NAME
        self._default_file = default_log_filename or _DEFAULT_LOG_FILE

        self._build_ui()
        self.set_values(self._default_base, self._default_folder, self._default_file)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        grp = QGroupBox("Log Directory")
        form = QFormLayout(grp)

        # (a) Base path — the parent directory where the log folder is created
        base_row = QHBoxLayout()
        self._base_input = QLineEdit()
        # NO placeholder here, on purpose.  A greyed-out path in an empty field
        # reads as "this is the value", and the widget then silently
        # substituted it - so clearing the field and pressing Save looked like
        # it had worked.  An empty base path is now visibly empty, and invalid.
        self._base_input.setToolTip(
            "The directory in which the log folder will be created.\n"
            "It must not be empty - press ↺ to restore the default."
        )
        self._base_input.textChanged.connect(self._on_any_change)
        base_row.addWidget(self._base_input, stretch=1)

        browse_base_btn = QPushButton("Browse…")
        browse_base_btn.setFixedWidth(80)
        browse_base_btn.setToolTip("Choose the parent directory for the log folder")
        browse_base_btn.clicked.connect(self._browse_base_path)
        base_row.addWidget(browse_base_btn)

        reset_base_btn = QPushButton("↺")
        reset_base_btn.setFixedWidth(30)
        # Was: setText(get_application_directory()) - which threw away a
        # default_base_path handed to the constructor.  Like its two siblings
        # this button restores the widget's own default (which is the
        # application directory only when no other default was configured).
        reset_base_btn.setToolTip(
            "Reset to default base path (the application directory unless "
            "another default was configured)"
        )
        reset_base_btn.clicked.connect(
            lambda: self._base_input.setText(self._default_base)
        )
        base_row.addWidget(reset_base_btn)

        form.addRow("Base path:", base_row)

        # (b) Folder name — the sub-directory name inside base_path
        folder_row = QHBoxLayout()
        self._folder_input = QLineEdit()
        self._folder_input.setPlaceholderText(_DEFAULT_FOLDER_NAME)
        self._folder_input.setToolTip(
            "Name of the sub-directory inside the base path where log files are stored."
        )
        self._folder_input.textChanged.connect(self._on_any_change)
        folder_row.addWidget(self._folder_input, stretch=1)

        reset_folder_btn = QPushButton("↺")
        reset_folder_btn.setFixedWidth(30)
        reset_folder_btn.setToolTip(f"Reset to default folder name ({_DEFAULT_FOLDER_NAME!r})")
        reset_folder_btn.clicked.connect(
            lambda: self._folder_input.setText(self._default_folder)
        )
        folder_row.addWidget(reset_folder_btn)

        form.addRow("Folder name:", folder_row)

        # (c) Log filename
        file_row = QHBoxLayout()
        self._file_input = QLineEdit()
        self._file_input.setPlaceholderText(_DEFAULT_LOG_FILE)
        self._file_input.setToolTip(
            "Name of the log file (the rotating handler will append .1, .2, … for backups)."
        )
        self._file_input.textChanged.connect(self._on_any_change)
        file_row.addWidget(self._file_input, stretch=1)

        reset_file_btn = QPushButton("↺")
        reset_file_btn.setFixedWidth(30)
        reset_file_btn.setToolTip(f"Reset to default filename ({_DEFAULT_LOG_FILE!r})")
        reset_file_btn.clicked.connect(
            lambda: self._file_input.setText(self._default_file)
        )
        file_row.addWidget(reset_file_btn)

        form.addRow("Log filename:", file_row)

        # Full-path preview (read-only, but selectable and copyable)
        self._preview_field = QLineEdit()
        self._preview_field.setReadOnly(True)
        self._preview_field.setToolTip(
            "The complete path of the log file. Select and copy it if you need it."
        )
        self._preview_field.setStyleSheet(
            "QLineEdit { font-size: 13px; font-family: Consolas, monospace;"
            " padding: 4px; background: transparent; border: 1px solid #555;"
            " border-radius: 3px; }"
        )
        self._preview_field.setMinimumHeight(28)
        form.addRow("Full path:", self._preview_field)

        # Validation feedback
        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 10px;")
        form.addRow("", self._status_label)

        outer.addWidget(grp)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _browse_base_path(self):
        start = self._base_input.text().strip() or get_application_directory()
        folder = QFileDialog.getExistingDirectory(
            self, "Select Base Directory for Log Folder", start
        )
        if folder:
            self._base_input.setText(folder)

    def _on_any_change(self):
        """Refresh the preview label, the status line and emit path_changed."""
        full_path = self.get_full_path()
        self._preview_field.setText(full_path)

        problems = self.validate()
        if problems:
            self._status_label.setStyleSheet("color: #ff6b6b; font-size: 10px;")
            self._status_label.setText("⚠ " + "  ".join(problems))
        else:
            self._status_label.setStyleSheet("color: #4CAF50; font-size: 10px;")
            self._status_label.setText("✓ Path is usable")

        self.path_changed.emit(full_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_base_path(self) -> str:
        """
        Return the base-path component.

        No fallback: an empty field is reported as empty so :meth:`validate`
        can refuse it.  Substituting the application directory here made a
        cleared field save successfully while displaying nothing.
        """
        return self._base_input.text().strip()

    def get_effective_base_path(self) -> str:
        """
        Return a base path that is always usable.

        Same as :meth:`get_base_path`, but falls back to the application
        directory. Used for the preview, which must show something even while
        the user is mid-edit.
        """
        return self._base_input.text().strip() or get_application_directory()

    def get_folder_name(self) -> str:
        """Return the folder-name component, falling back to the default."""
        return self._folder_input.text().strip() or _DEFAULT_FOLDER_NAME

    def get_log_filename(self) -> str:
        """Return the log-filename component, falling back to the default."""
        return self._file_input.text().strip() or _DEFAULT_LOG_FILE

    def get_full_path(self) -> str:
        """
        Return the complete, absolute path of the log file.

        ``base_path / folder_name / log_filename``
        """
        try:
            return str(Path(self.get_log_dir()) / self.get_log_filename())
        except (OSError, ValueError) as exc:         # pragma: no cover - defensive
            logger.debug("Could not build the log path: %s", exc)
            return (f"{self.get_base_path()}{os.sep}{self.get_folder_name()}"
                    f"{os.sep}{self.get_log_filename()}")

    def get_log_dir(self) -> str:
        """
        Return the directory part (base_path / folder_name) as an absolute path.

        This is what SettingsManager's 'log_dir' key expects.
        """
        base = self.get_base_path()
        folder = self.get_folder_name()
        try:
            log_dir = (Path(base) / folder).expanduser()
            # A hand-typed base path may be relative ("mylogs").  The getter
            # promises an absolute directory, and a relative path would end up
            # under the application directory once the handler opens the file,
            # so resolve it there instead of handing out a relative fragment.
            if not log_dir.is_absolute():
                log_dir = Path(get_application_directory()) / log_dir
            return str(log_dir)
        except (OSError, ValueError):                # pragma: no cover - defensive
            return f"{base}{os.sep}{folder}"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, check_writable: bool = False) -> List[str]:
        """
        Check whether the configured path can actually be used for logging.

        Args:
            check_writable: Also try to create the directory and verify that a
                file can be written there.  This touches the file system, so it
                is only done when the user saves.

        Returns:
            A list of human readable problems; empty when everything is fine.
        """
        problems: List[str] = []

        base_raw = self._base_input.text().strip()
        folder_raw = self._folder_input.text().strip()
        file_raw = self._file_input.text().strip()

        # An empty base path used to be silently replaced by the application
        # directory, so clearing the field and pressing "Save All Settings"
        # reported success while the field showed nothing.
        if not base_raw:
            problems.append(
                "Base path is empty - enter a directory, or press ↺ to restore "
                "the application directory."
            )
            return problems

        # --- character checks ------------------------------------------
        if any(char in base_raw for char in _INVALID_PATH_CHARS):
            problems.append(
                f"Base path contains invalid characters ({_INVALID_PATH_CHARS})."
            )
        if '\0' in base_raw:
            problems.append("Base path contains a null character.")

        if folder_raw and any(char in folder_raw for char in _INVALID_FILENAME_CHARS):
            problems.append(
                f"Folder name must not contain any of {_INVALID_FILENAME_CHARS}."
            )
        # A NUL is not part of _INVALID_FILENAME_CHARS, and the file system
        # probe below cannot report it either: Path.exists() answers False for
        # a path with an embedded null byte instead of raising.  Without these
        # two checks the status line claimed such a path was usable while
        # opening the log file would fail with a ValueError.
        if '\0' in folder_raw:
            problems.append("Folder name contains a null character.")
        if file_raw and any(char in file_raw for char in _INVALID_FILENAME_CHARS):
            problems.append(
                f"Log filename must not contain any of {_INVALID_FILENAME_CHARS}."
            )
        if '\0' in file_raw:
            problems.append("Log filename contains a null character.")

        if problems:
            return problems

        # --- file system checks ----------------------------------------
        log_dir = Path(self.get_log_dir())

        try:
            existing = log_dir
            while not existing.exists() and existing.parent != existing:
                existing = existing.parent

            if existing.exists() and not existing.is_dir():
                problems.append(f"'{existing}' is a file, not a directory.")
                return problems

            if log_dir.exists() and not log_dir.is_dir():
                problems.append(f"'{log_dir}' already exists and is not a directory.")
                return problems

            if check_writable:
                log_dir.mkdir(parents=True, exist_ok=True)
                probe = log_dir / ".write_test"
                probe.write_text("", encoding="utf-8")
                probe.unlink()
            elif existing.exists() and not os.access(str(existing), os.W_OK):
                problems.append(f"No permission to write into '{existing}'.")

        except PermissionError:
            problems.append(f"No permission to create or write into '{log_dir}'.")
        except OSError as exc:
            problems.append(f"The path cannot be used: {exc.strerror or exc}.")
        except Exception as exc:                     # pragma: no cover - defensive
            problems.append(f"The path cannot be used: {exc}.")

        return problems

    def is_valid(self) -> bool:
        """True when the configured path passes the quick checks."""
        return not self.validate()

    # ------------------------------------------------------------------
    # Setters
    # ------------------------------------------------------------------

    def set_values(
        self,
        base_path: str = "",
        folder_name: str = _DEFAULT_FOLDER_NAME,
        log_filename: str = _DEFAULT_LOG_FILE,
    ):
        """
        Programmatically populate all three fields.

        Emits path_changed once (after the last field is set).
        """
        for widget in (self._base_input, self._folder_input, self._file_input):
            widget.blockSignals(True)

        self._base_input.setText(base_path or get_application_directory())
        self._folder_input.setText(folder_name or _DEFAULT_FOLDER_NAME)
        self._file_input.setText(log_filename or _DEFAULT_LOG_FILE)

        for widget in (self._base_input, self._folder_input, self._file_input):
            widget.blockSignals(False)

        # Single update
        self._on_any_change()

    def set_from_log_dir_and_file(self, log_dir: str, log_file: str):
        """
        Split a combined 'log_dir' value into base_path + folder_name.

        Relative values (the factory default is simply ``"logs"``) are resolved
        against the application directory, so the user always sees a real
        location instead of an ambiguous relative fragment.

        Examples
        --------
        log_dir="logs", log_file="app.log"
          → base=<application directory>, folder="logs", file="app.log"

        log_dir="/var/log/myapp/logs", log_file="app.log"
          → base="/var/log/myapp", folder="logs", file="app.log"
        """
        raw = (log_dir or "").strip() or _DEFAULT_FOLDER_NAME
        path = Path(raw).expanduser()

        if not path.is_absolute():
            path = Path(get_application_directory()) / path

        folder = path.name or _DEFAULT_FOLDER_NAME
        base = str(path.parent)

        self.set_values(base, folder, log_file or _DEFAULT_LOG_FILE)

    def reset_to_defaults(self):
        """Restore the factory default location."""
        self.set_values(get_application_directory(), _DEFAULT_FOLDER_NAME,
                        _DEFAULT_LOG_FILE)
