"""
FilePathSelectorWidget
A reusable widget for selecting a file output path, optionally with
auto-generated filename support (Issue 4).

Behaviour
---------
Auto-generate OFF
    A "Browse…" button opens a standard "Save As" dialog where the user
    picks both folder and filename.  The full path is shown in the path
    field.

Auto-generate ON
    A "Browse…" button opens a folder-only dialog; the user picks where
    the file will be saved.  The *filename* itself is generated
    automatically.  Next to the checkbox a small label shows what the
    auto-generated filename will be (e.g. "export_20240315_120000.aes").
    The path field shows the *full* final path (folder + auto-name).

Switching between modes
    Each mode remembers its own folder/path independently.  Switching back
    and forth restores whatever the user had entered previously.

Usage::

    widget = FilePathSelectorWidget(
        label="Output File",
        # called to produce just the filename part when auto-generate is on:
        auto_name_factory=lambda: f"export_{datetime.now():%Y%m%d_%H%M%S}.aes",
        # file-dialog filter for manual mode:
        file_filter="AES Files (*.aes);;All Files (*)",
        default_suffix=".aes",
        parent=self,
    )
    # connect to know when the path changes:
    widget.path_changed.connect(lambda p: print("new path:", p))
    # read the current full path:
    full_path = widget.get_path()
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


class FilePathSelectorWidget(QWidget):
    """
    Reusable file-path selector with optional auto-generate support.

    Signals
    -------
    path_changed(str)
        Emitted whenever the effective full path changes.
    """

    path_changed = pyqtSignal(str)

    def __init__(
        self,
        label: str = "Output File",
        auto_name_factory: Optional[Callable[[], str]] = None,
        file_filter: str = "All Files (*)",
        default_suffix: str = "",
        group_box: bool = True,
        parent=None,
    ):
        """
        Parameters
        ----------
        label
            Title for the surrounding QGroupBox (or QLabel if group_box=False).
        auto_name_factory
            Callable that returns *only the filename* part (no directory) when
            auto-generate is enabled.  If None, the auto-generate checkbox is
            hidden entirely.
        file_filter
            Filter string passed to QFileDialog in manual mode.
        default_suffix
            Extension automatically appended if the user omits it in manual
            mode (e.g. ".pdf").
        group_box
            Wrap in a QGroupBox when True, use a plain QLabel header otherwise.
        """
        super().__init__(parent)

        self._auto_name_factory = auto_name_factory
        self._file_filter = file_filter
        self._default_suffix = default_suffix

        # Memory: last known path for each mode
        self._manual_path: str = ""
        self._auto_folder: str = ""

        self._build_ui(label, group_box)
        self._update_mode(auto=self._auto_name_factory is not None)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, label: str, use_group_box: bool):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        if use_group_box:
            container = QGroupBox(label)
            inner = QVBoxLayout(container)
            outer.addWidget(container)
        else:
            inner = outer

        # Auto-generate checkbox + preview label (only when factory provided)
        if self._auto_name_factory is not None:
            auto_row = QHBoxLayout()
            self._auto_checkbox = QCheckBox("Auto-generate filename")
            self._auto_checkbox.setChecked(True)
            self._auto_checkbox.toggled.connect(self._on_auto_toggled)
            auto_row.addWidget(self._auto_checkbox)

            # Small label that shows what the auto-filename will be
            self._auto_name_label = QLabel()
            self._auto_name_label.setStyleSheet(
                "color: #aaa; font-size: 10px; font-style: italic;"
            )
            auto_row.addWidget(self._auto_name_label, stretch=1)
            inner.addLayout(auto_row)
        else:
            self._auto_checkbox = None
            self._auto_name_label = None

        # Path field + browse button
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path:"))
        self._path_input = QLineEdit()
        self._path_input.setReadOnly(True)
        self._path_input.setPlaceholderText("No path selected…")
        self._path_input.textChanged.connect(
            lambda text: self.path_changed.emit(text)
        )
        path_row.addWidget(self._path_input, stretch=1)

        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(self._browse_btn)
        inner.addLayout(path_row)

    # ------------------------------------------------------------------
    # Mode management
    # ------------------------------------------------------------------

    def _on_auto_toggled(self, checked: bool):
        """Handle switching between auto and manual mode."""
        # Save the *current* path into the appropriate memory slot before
        # switching, so we can restore it when the mode is toggled back.
        if checked:
            # switching TO auto → save what the user had typed as manual path
            self._manual_path = self._path_input.text()
        else:
            # switching FROM auto → save the folder that was selected
            current = self._path_input.text()
            if current:
                self._auto_folder = str(Path(current).parent)

        self._update_mode(auto=checked)

    def _update_mode(self, auto: bool):
        """Refresh the UI state for the given mode."""
        if auto and self._auto_name_factory is not None:
            # Auto mode: path is folder + generated name
            auto_name = self._auto_name_factory()
            if self._auto_name_label is not None:
                self._auto_name_label.setText(f"Filename: {auto_name}")

            if self._auto_folder:
                full = str(Path(self._auto_folder) / auto_name)
            else:
                full = ""

            self._path_input.setPlaceholderText(
                "Select a folder to determine the save location…"
            )
            self._path_input.setText(full)
        else:
            # Manual mode
            if self._auto_name_label is not None:
                self._auto_name_label.setText("")
            self._path_input.setPlaceholderText("Select output file…")
            self._path_input.setText(self._manual_path)

    def _on_browse(self):
        """Open the appropriate file dialog depending on the current mode."""
        if self._auto_checkbox is not None and self._auto_checkbox.isChecked():
            # Auto mode → folder-only dialog
            start = self._auto_folder or str(Path.home())
            folder = QFileDialog.getExistingDirectory(
                self, "Select Save Folder", start
            )
            if folder:
                self._auto_folder = folder
                auto_name = self._auto_name_factory()
                if self._auto_name_label is not None:
                    self._auto_name_label.setText(f"Filename: {auto_name}")
                self._path_input.setText(str(Path(folder) / auto_name))
        else:
            # Manual mode → standard "Save As" dialog
            start = self._manual_path or str(Path.home())
            path, _ = QFileDialog.getSaveFileName(
                self, "Select Output File", start, self._file_filter
            )
            if path:
                if self._default_suffix and not path.endswith(self._default_suffix):
                    path += self._default_suffix
                self._manual_path = path
                self._path_input.setText(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_path(self) -> str:
        """Return the current full file path (may be empty)."""
        return self._path_input.text()

    def set_path(self, path: str):
        """
        Programmatically set the path.

        In auto mode the folder is extracted and the auto-name is appended.
        In manual mode the path is set verbatim.
        """
        if not path:
            self._path_input.setText("")
            return

        p = Path(path)
        if self._auto_checkbox is not None and self._auto_checkbox.isChecked():
            self._auto_folder = str(p.parent)
            self._update_mode(auto=True)
        else:
            self._manual_path = path
            self._path_input.setText(path)

    def is_auto_mode(self) -> bool:
        """Return True if auto-generate mode is active."""
        return (
            self._auto_checkbox is not None and self._auto_checkbox.isChecked()
        )

    def set_auto_mode(self, auto: bool):
        """Switch between auto and manual mode programmatically."""
        if self._auto_checkbox is not None:
            self._auto_checkbox.setChecked(auto)

    def refresh_auto_name(self):
        """
        Force a refresh of the auto-generated filename.

        Call this when the underlying data that influences the filename
        changes (e.g. source name or timestamp).
        """
        if self.is_auto_mode() and self._auto_name_factory is not None:
            self._update_mode(auto=True)
