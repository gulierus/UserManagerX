"""
Fourth tab: Application Settings

Changes
-------
Issue 1 — Bug fix: save_all_settings() previously read UI values AFTER the
           first set() call had already emitted category_changed("general"),
           which triggered _reload_general_settings() and reset the checkbox
           back to its persisted (old) value before it was saved.  The fix
           is to capture every UI value into local variables before any set()
           call is made.

Issue 2 — Redesigned layout: categories are now shown in a QListWidget on the
           left; the corresponding settings panel is displayed on the right
           inside a QStackedWidget.  A QSplitter separates both sides so the
           user can adjust the proportions.

Issue 3a — NoCredentialsWarningDialog moved to its own module; SettingsTab
            connects to category_changed so the checkbox stays in sync with
            external changes (e.g. the dialog itself unchecking it).

Issue 4  — "Apply Logging Configuration" button removed; logging is
            re-applied automatically on every category_changed("logging").

Issue 5  — Log Directory section now uses LogDirectoryWidget which lets the
            user configure (a) the base path, (b) the folder name, and
            (c) the log filename independently.
"""

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from ui.log_directory_widget import LogDirectoryWidget
from ui.source_combo import ACCESS_SETTING_CATEGORY, ACCESS_SETTING_KEY
from utils.settings_manager import (
    DEFAULT_SETTINGS, get_settings, VALID_LOG_LEVELS,
)

logger = logging.getLogger(__name__)


#: Shared look of a "category / section chooser" panel.
#:
#: The panel used to be a flat dark grey (a black overlay), which read as
#: switched-off rather than as a sidebar.  It is now a light, slightly blue
#: tinted surface picked up from the selection colour (#3a5f9f), so it lifts
#: away from the window background without drawing attention to itself and
#: stays correct under every one of the six themes - the colour is defined with
#: alpha, so it tints whatever the theme puts behind it instead of fighting it.
CATEGORY_PANEL_STYLE = (
    "QFrame#categoryContainer {"
    "  background-color: rgba(122, 162, 224, 28);"
    "  border: 1px solid rgba(122, 162, 224, 70);"
    "  border-radius: 6px;"
    "}"
)

#: Matching list style for the items inside such a panel.
CATEGORY_LIST_STYLE = (
    "QListWidget { border: none; background: transparent; font-size: 13px; }"
    "QListWidget::item { padding: 8px 12px; margin: 1px 0; }"
    "QListWidget::item:hover { background: rgba(122, 162, 224, 55);"
    "  border-radius: 4px; }"
    "QListWidget::item:selected { background: #3a5f9f; color: white;"
    "  border-radius: 4px; }"
)

#: Caption above the list ("CATEGORIES", "OPERATIONS", ...).
CATEGORY_CAPTION_STYLE = (
    "font-size: 10px; font-weight: bold; color: #8fa9c8;"
    " letter-spacing: 1px; padding: 4px 6px;"
)


class SettingsTab(QWidget):
    """
    Fourth tab — Application Settings.

    Layout
    ------
    ┌──────────────┬──────────────────────────────────────────┐
    │  Category    │                                          │
    │  list        │  Settings panel for selected category   │
    │  (left)      │  (right, inside QStackedWidget)         │
    └──────────────┴──────────────────────────────────────────┘
    The divider between left and right is a draggable QSplitter.
    """

    THEMES = {
        "Dark": {
            "window": QColor(53, 53, 53), "window_text": Qt.GlobalColor.white,
            "base": QColor(35, 35, 35), "alternate_base": QColor(53, 53, 53),
            "tooltip_base": QColor(25, 25, 25), "tooltip_text": Qt.GlobalColor.white,
            "text": Qt.GlobalColor.white, "button": QColor(53, 53, 53),
            "button_text": Qt.GlobalColor.white, "bright_text": Qt.GlobalColor.red,
            "link": QColor(42, 130, 218), "highlight": QColor(42, 130, 218),
            "highlighted_text": Qt.GlobalColor.black,
        },
        "Light": {
            "window": QColor(240, 240, 240), "window_text": Qt.GlobalColor.black,
            "base": Qt.GlobalColor.white, "alternate_base": QColor(245, 245, 245),
            "tooltip_base": QColor(255, 255, 220), "tooltip_text": Qt.GlobalColor.black,
            "text": Qt.GlobalColor.black, "button": QColor(240, 240, 240),
            "button_text": Qt.GlobalColor.black, "bright_text": Qt.GlobalColor.blue,
            "link": QColor(0, 0, 255), "highlight": QColor(0, 120, 215),
            "highlighted_text": Qt.GlobalColor.white,
        },
        "Blue": {
            "window": QColor(43, 87, 151), "window_text": Qt.GlobalColor.white,
            "base": QColor(25, 50, 90), "alternate_base": QColor(43, 87, 151),
            "tooltip_base": QColor(15, 30, 60), "tooltip_text": Qt.GlobalColor.white,
            "text": Qt.GlobalColor.white, "button": QColor(43, 87, 151),
            "button_text": Qt.GlobalColor.white, "bright_text": Qt.GlobalColor.yellow,
            "link": QColor(100, 180, 255), "highlight": QColor(70, 130, 220),
            "highlighted_text": Qt.GlobalColor.white,
        },
        "Green": {
            "window": QColor(46, 82, 66), "window_text": Qt.GlobalColor.white,
            "base": QColor(28, 50, 40), "alternate_base": QColor(46, 82, 66),
            "tooltip_base": QColor(18, 35, 28), "tooltip_text": Qt.GlobalColor.white,
            "text": Qt.GlobalColor.white, "button": QColor(46, 82, 66),
            "button_text": Qt.GlobalColor.white, "bright_text": Qt.GlobalColor.yellow,
            "link": QColor(100, 255, 180), "highlight": QColor(76, 175, 80),
            "highlighted_text": Qt.GlobalColor.white,
        },
        "Purple": {
            "window": QColor(71, 49, 102), "window_text": Qt.GlobalColor.white,
            "base": QColor(45, 30, 65), "alternate_base": QColor(71, 49, 102),
            "tooltip_base": QColor(30, 20, 45), "tooltip_text": Qt.GlobalColor.white,
            "text": Qt.GlobalColor.white, "button": QColor(71, 49, 102),
            "button_text": Qt.GlobalColor.white, "bright_text": Qt.GlobalColor.yellow,
            "link": QColor(186, 104, 200), "highlight": QColor(142, 68, 173),
            "highlighted_text": Qt.GlobalColor.white,
        },
        "Warm": {
            "window": QColor(90, 70, 60), "window_text": Qt.GlobalColor.white,
            "base": QColor(60, 45, 35), "alternate_base": QColor(90, 70, 60),
            "tooltip_base": QColor(45, 35, 25), "tooltip_text": Qt.GlobalColor.white,
            "text": Qt.GlobalColor.white, "button": QColor(90, 70, 60),
            "button_text": Qt.GlobalColor.white, "bright_text": QColor(255, 200, 100),
            "link": QColor(255, 160, 100), "highlight": QColor(205, 127, 50),
            "highlighted_text": Qt.GlobalColor.white,
        },
    }

    # Category list items: (display label, icon emoji)
    _CATEGORIES = [
        ("Appearance", "🎨"),
        ("General", "⚙"),
        ("Logging", "📋"),
        ("About", "ℹ"),
    ]

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self._init_ui()
        self._load_all_settings()
        self._connect_settings_signals()

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_settings_signals(self):
        """
        Connect to SettingsManager so the UI stays in sync with external
        changes (e.g. NoCredentialsWarningDialog toggling the checkbox).
        """
        self.settings.category_changed.connect(self._on_settings_category_changed)

    def _on_settings_category_changed(self, category: str):
        """Route a category-changed notification to the right refresh method."""
        if category == "general":
            self._reload_general_settings()
        elif category == "logging":
            self._reload_logging_settings()
            self._apply_logging_from_settings_silently()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Header
        header = QLabel("⚙ Application Settings")
        header.setStyleSheet("font-size: 16px; font-weight: bold; padding: 4px 0;")
        outer.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep)

        # ---- Main area: category list | settings panel ----
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: category list inside its own framed container so the two
        # halves of the tab are clearly separated (Issue 16).
        category_container = QFrame()
        category_container.setFrameShape(QFrame.Shape.StyledPanel)
        category_container.setObjectName("categoryContainer")
        category_container.setStyleSheet(CATEGORY_PANEL_STYLE)
        category_layout = QVBoxLayout(category_container)
        category_layout.setContentsMargins(6, 6, 6, 6)
        category_layout.setSpacing(4)

        category_caption = QLabel("CATEGORIES")
        category_caption.setStyleSheet(CATEGORY_CAPTION_STYLE)
        category_layout.addWidget(category_caption)

        self._category_list = QListWidget()
        self._category_list.setMinimumWidth(150)
        self._category_list.setStyleSheet(CATEGORY_LIST_STYLE)
        for label, icon in self._CATEGORIES:
            item = QListWidgetItem(f"{icon}  {label}")
            self._category_list.addItem(item)
        self._category_list.currentRowChanged.connect(self._on_category_selected)
        category_layout.addWidget(self._category_list, stretch=1)

        category_container.setMinimumWidth(170)
        category_container.setMaximumWidth(260)
        splitter.addWidget(category_container)

        # Right: stacked settings panels (built after self._category_list exists)
        panel_container = QFrame()
        panel_container.setFrameShape(QFrame.Shape.StyledPanel)
        panel_container.setObjectName("panelContainer")
        panel_container.setStyleSheet(
            "QFrame#panelContainer {"
            "  border: 1px solid rgba(255, 255, 255, 40);"
            "  border-radius: 6px;"
            "}"
        )
        panel_layout = QVBoxLayout(panel_container)
        panel_layout.setContentsMargins(2, 2, 2, 2)

        self._panel_title = QLabel()
        self._panel_title.setStyleSheet(
            "font-size: 13px; font-weight: bold; padding: 8px 10px 4px 10px;"
        )
        panel_layout.addWidget(self._panel_title)

        title_separator = QFrame()
        title_separator.setFrameShape(QFrame.Shape.HLine)
        title_separator.setFrameShadow(QFrame.Shadow.Sunken)
        panel_layout.addWidget(title_separator)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_appearance_panel())
        self._stack.addWidget(self._build_general_panel())
        self._stack.addWidget(self._build_logging_panel())
        self._stack.addWidget(self._build_about_panel())
        panel_layout.addWidget(self._stack, stretch=1)

        splitter.addWidget(panel_container)

        splitter.setSizes([190, 600])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, stretch=1)

        # Select first category by default
        self._category_list.setCurrentRow(0)

        # Bottom action buttons
        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾 Save All Settings")
        save_btn.setMinimumHeight(34)
        save_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        save_btn.clicked.connect(self.save_all_settings)
        btn_row.addWidget(save_btn)

        reset_btn = QPushButton("↺ Reset to Defaults")
        reset_btn.setMinimumHeight(34)
        reset_btn.clicked.connect(self.reset_to_defaults)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()
        outer.addLayout(btn_row)

    def _on_category_selected(self, row: int):
        """Show the panel of the selected category and update its heading."""
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
            label, icon = self._CATEGORIES[row]
            self._panel_title.setText(f"{icon}  {label}")

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------

    def _wrap_in_scroll(self, widget: QWidget) -> QScrollArea:
        """Wrap a settings panel in a scroll area."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build_appearance_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        theme_grp = QGroupBox("Color Theme")
        tl = QVBoxLayout(theme_grp)

        row = QHBoxLayout()
        row.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(self.THEMES.keys()))
        row.addWidget(self.theme_combo)

        apply_btn = QPushButton("Apply Now")
        apply_btn.clicked.connect(self.apply_theme)
        row.addWidget(apply_btn)
        row.addStretch()
        tl.addLayout(row)

        desc = QLabel("Choose a color theme. Click 'Apply Now' to preview immediately.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; font-style: italic;")
        tl.addWidget(desc)

        swatch_row = QHBoxLayout()
        for name in self.THEMES:
            btn = QPushButton(name)
            btn.setMinimumHeight(52)
            btn.setStyleSheet(self._swatch_style(name))
            btn.clicked.connect(lambda _, t=name: self._pick_theme(t))
            swatch_row.addWidget(btn)
        tl.addLayout(swatch_row)

        layout.addWidget(theme_grp)
        layout.addStretch()
        return self._wrap_in_scroll(container)

    def _build_general_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        grp = QGroupBox("Group Management")
        gl = QVBoxLayout(grp)
        self.warn_on_no_creds = QCheckBox(
            "Show warning when opening Group Management without AD credentials"
        )
        self.warn_on_no_creds.setToolTip(
            "When enabled, a warning dialog appears if you open Group Management "
            "without entering AD server credentials."
        )
        gl.addWidget(self.warn_on_no_creds)
        layout.addWidget(grp)

        source_grp = QGroupBox("Source Lists")
        sl = QVBoxLayout(source_grp)
        self.show_source_access = QCheckBox(
            "Show whether a source is read-only or editable in source lists"
        )
        self.show_source_access.setToolTip(
            "When enabled, every source selector appends the access mode to the "
            "name - \"Students 2025 (editable)\", \"AD school.local (read-only)\" - "
            "so you can tell before selecting a source whether it can be edited."
        )
        sl.addWidget(self.show_source_access)
        layout.addWidget(source_grp)

        layout.addStretch()
        return self._wrap_in_scroll(container)

    @staticmethod
    def _reset_button(tooltip: str, slot) -> QPushButton:
        """
        Create the small "reset to default" button used across the panels.

        Args:
            tooltip: Explanation shown on hover (names the default value).
            slot: Callable that restores the default.

        Returns:
            The configured button.
        """
        button = QPushButton("↺")
        button.setFixedWidth(30)
        button.setToolTip(tooltip)
        button.clicked.connect(lambda: slot())
        return button

    def _with_reset(self, widget: QWidget, tooltip: str, slot) -> QWidget:
        """Wrap *widget* and a reset button into one row widget."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addWidget(self._reset_button(tooltip, slot))
        row.addStretch()
        return container

    def _with_reset_layout(self, widget: QWidget, tooltip: str, slot) -> QHBoxLayout:
        """Return a layout holding *widget* followed by a reset button."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addWidget(self._reset_button(tooltip, slot))
        row.addStretch()
        return row

    def _build_logging_panel(self) -> QWidget:
        """
        Logging configuration panel.

        The 'Apply Logging Configuration' button has been removed.
        Logging is automatically re-applied when the 'logging' category changes
        in SettingsManager (via save_all_settings or external callers).

        Log directory uses LogDirectoryWidget (Issue 5) which lets the user
        configure base path, folder name, and log filename independently.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        defaults = DEFAULT_SETTINGS["logging"]

        # Log level
        level_grp = QGroupBox("Log Level")
        ll = QHBoxLayout(level_grp)
        ll.addWidget(QLabel("Level:"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(sorted(VALID_LOG_LEVELS))
        ll.addWidget(self.log_level_combo)
        ll.addWidget(self._reset_button(
            f"Reset to default log level ({defaults['log_level']})",
            lambda: self.log_level_combo.setCurrentText(defaults["log_level"]),
        ))
        ll.addStretch()
        layout.addWidget(level_grp)

        # File rotation
        rot_grp = QGroupBox("Log File Rotation")
        rl = QFormLayout(rot_grp)

        default_mb = max(1, int(defaults["max_bytes"]) // (1024 * 1024))
        self.max_size_spin = QSpinBox()
        self.max_size_spin.setRange(1, 500)
        self.max_size_spin.setSuffix(" MB")
        rl.addRow("Max file size:", self._with_reset(
            self.max_size_spin,
            f"Reset to default maximum file size ({default_mb} MB)",
            lambda: self.max_size_spin.setValue(default_mb),
        ))

        self.backup_count_spin = QSpinBox()
        self.backup_count_spin.setRange(0, 20)
        rl.addRow("Backup count:", self._with_reset(
            self.backup_count_spin,
            f"Reset to default backup count ({defaults['backup_count']})",
            lambda: self.backup_count_spin.setValue(int(defaults["backup_count"])),
        ))
        layout.addWidget(rot_grp)

        # Output targets
        out_grp = QGroupBox("Output Targets")
        ol = QVBoxLayout(out_grp)

        self.file_logging_check = QCheckBox("Write logs to file")
        ol.addLayout(self._with_reset_layout(
            self.file_logging_check,
            f"Reset to default ({'enabled' if defaults['file_enabled'] else 'disabled'})",
            lambda: self.file_logging_check.setChecked(bool(defaults["file_enabled"])),
        ))

        self.console_logging_check = QCheckBox("Write logs to console")
        ol.addLayout(self._with_reset_layout(
            self.console_logging_check,
            f"Reset to default ({'enabled' if defaults['console_enabled'] else 'disabled'})",
            lambda: self.console_logging_check.setChecked(bool(defaults["console_enabled"])),
        ))
        layout.addWidget(out_grp)

        # Log directory — three-part widget (Issue 5)
        log_cfg = self.settings.get_logging_config()
        self.log_dir_widget = LogDirectoryWidget(parent=self)
        self.log_dir_widget.set_from_log_dir_and_file(
            log_cfg.get("log_dir", DEFAULT_SETTINGS["logging"]["log_dir"]),
            log_cfg.get("log_file", DEFAULT_SETTINGS["logging"]["log_file"]),
        )
        layout.addWidget(self.log_dir_widget)

        # Informational note
        note = QLabel(
            "<i>&#8505; Logging configuration is applied automatically when you"
            " click <b>Save All Settings</b>.</i>"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #aaa; font-size: 10px;")
        layout.addWidget(note)

        layout.addStretch()
        return self._wrap_in_scroll(container)

    def _build_about_panel(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)
        about = QLabel(
            "<b>Student Management System</b><br>"
            "Version: 2.0.0<br>"
            "Built with PyQt6 &amp; ReportLab<br><br>"
            "<b>Features:</b><br>"
            "• Multi-source data management<br>"
            "• EduPage integration<br>"
            "• Active Directory synchronisation<br>"
            "• Conflict detection and resolution<br>"
            "• Encrypted PDF &amp; JSON export<br>"
            "• Group &amp; template management"
        )
        about.setWordWrap(True)
        layout.addWidget(about)
        layout.addStretch()
        return self._wrap_in_scroll(container)

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _load_all_settings(self):
        """Load all settings from SettingsManager into UI."""
        theme = self.settings.get_str("theme", "Dark", category="general")
        if theme in self.THEMES:
            self.theme_combo.setCurrentText(theme)
        self._reload_general_settings()
        self._reload_logging_settings()

    def _reload_general_settings(self):
        """Reload only the general-category controls from SettingsManager."""
        self.warn_on_no_creds.blockSignals(True)
        self.warn_on_no_creds.setChecked(
            self.settings.get_bool(
                "show_group_management_warning", True, category="general"
            )
        )
        self.warn_on_no_creds.blockSignals(False)

        self.show_source_access.blockSignals(True)
        self.show_source_access.setChecked(
            self.settings.get_bool(
                ACCESS_SETTING_KEY, True, category=ACCESS_SETTING_CATEGORY
            )
        )
        self.show_source_access.blockSignals(False)

    def _reload_logging_settings(self):
        """Reload only the logging-category controls from SettingsManager."""
        log_cfg = self.settings.get_logging_config()

        self.log_level_combo.blockSignals(True)
        level = log_cfg.get("log_level", "INFO")
        idx = self.log_level_combo.findText(level)
        if idx >= 0:
            self.log_level_combo.setCurrentIndex(idx)
        self.log_level_combo.blockSignals(False)

        mb = log_cfg.get("max_bytes", 10 * 1024 * 1024) // (1024 * 1024)
        self.max_size_spin.setValue(max(1, mb))
        self.backup_count_spin.setValue(log_cfg.get("backup_count", 5))
        self.file_logging_check.setChecked(log_cfg.get("file_enabled", True))
        self.console_logging_check.setChecked(log_cfg.get("console_enabled", True))

        # Restore the three-part log path into the dedicated widget
        self.log_dir_widget.set_from_log_dir_and_file(
            log_cfg.get("log_dir", "logs"),
            log_cfg.get("log_file", "student_management.log"),
        )

    def save_all_settings(self):
        """
        Save all UI values into SettingsManager.

        Bug fix (Issue 1)
        -----------------
        All UI values are captured into local variables BEFORE any set() call
        is made.  Previously, the first set() call emitted category_changed,
        which triggered _reload_general_settings(), which read the old
        persisted value back from SettingsManager and reset the checkbox —
        causing the new value entered by the user to be lost.
        """
        try:
            # --- Capture ALL values before any write ---
            theme_val          = self.theme_combo.currentText()
            warn_val           = self.warn_on_no_creds.isChecked()
            source_access_val  = self.show_source_access.isChecked()
            log_level_val      = self.log_level_combo.currentText()
            max_bytes_val      = self.max_size_spin.value() * 1024 * 1024
            backup_count_val   = self.backup_count_spin.value()
            file_enabled_val   = self.file_logging_check.isChecked()
            console_enabled_val = self.console_logging_check.isChecked()

            # --- Validate the log location BEFORE writing anything ---------
            if not self._ensure_usable_log_path(file_enabled_val):
                return

            log_dir_val  = self.log_dir_widget.get_log_dir()
            log_file_val = self.log_dir_widget.get_log_filename()

            # --- Write (signals may fire, but values are already captured) ---
            # Bug fix: set() returns False when the value could not be written
            # (an unwritable settings file, a non-serialisable value).  Both
            # results used to be discarded, so a completely failed save still
            # ended in a dialog promising "The other settings were saved".
            theme_saved = self.settings.set("theme", theme_val,
                                            category="general")
            warning_saved = self.settings.set(
                "show_group_management_warning", warn_val, category="general"
            )
            source_access_saved = self.settings.set(
                ACCESS_SETTING_KEY, source_access_val,
                category=ACCESS_SETTING_CATEGORY
            )
            general_saved = theme_saved and warning_saved and source_access_saved
            logging_saved = self.settings.update_logging_config(
                log_level=log_level_val,
                max_bytes=max_bytes_val,
                backup_count=backup_count_val,
                file_enabled=file_enabled_val,
                console_enabled=console_enabled_val,
                log_dir=log_dir_val,
                log_file=log_file_val,
            )

            if not (general_saved and logging_saved):
                self._report_save_failure(general_saved, logging_saved,
                                          log_dir_val, log_file_val)
                return

            QMessageBox.information(self, "Saved", "Settings saved successfully.")
            logger.info("All settings saved.")
        except Exception as e:
            logger.exception("Error saving settings")
            QMessageBox.critical(self, "Save Error", f"Failed to save settings:\n{e}")

    def _report_save_failure(self, general_saved: bool, logging_saved: bool,
                             log_dir_val: str, log_file_val: str) -> None:
        """
        Tell the user exactly which part of the save failed.

        Only the categories that really reached the disk may be reported as
        saved: when the settings file itself cannot be written, both writes
        fail and nothing at all was stored.
        """
        if general_saved:
            # Only update_logging_config() refused - it validates its values.
            message = (
                "The logging configuration could not be saved.\n\n"
                f"Log directory: {log_dir_val}\n"
                f"Log file: {log_file_val}\n\n"
                "Correct the values and try again. The other settings "
                "were saved."
            )
            logger.error("Logging configuration rejected: dir=%r file=%r",
                         log_dir_val, log_file_val)
        elif logging_saved:
            message = (
                "The appearance and general settings could not be saved.\n\n"
                f"Settings file: {self.settings.settings_file}\n\n"
                "Check that the file and its folder can be written to, then "
                "try again."
            )
            logger.error("General settings could not be written to %s",
                         self.settings.settings_file)
        else:
            message = (
                "The settings could not be stored.\n\n"
                f"Settings file: {self.settings.settings_file}\n"
                f"Log directory: {log_dir_val}\n"
                f"Log file: {log_file_val}\n\n"
                "Your changes were not stored. Check that the settings "
                "file can be written to and that the log path is valid, "
                "then try again."
            )
            logger.error("Nothing could be saved: settings file %s, "
                         "log dir=%r file=%r",
                         self.settings.settings_file, log_dir_val, log_file_val)

        QMessageBox.critical(self, "Save Error", message)

    def _ensure_usable_log_path(self, file_logging_enabled: bool) -> bool:
        """
        Make sure the configured log directory can really be used.

        Handles the situation described in issue 20: the user types an invalid
        path - or clears the field completely - and presses *Save All Settings*.

        Before this check, such a value was either rejected silently by
        ``update_logging_config()`` (which still reported "saved successfully")
        or accepted and then failed when the handler was created, leaving the
        application without a log file and only a message on stderr.

        Args:
            file_logging_enabled: Whether writing to a file is switched on at
                all. When it is off, an unusable directory is only a warning.

        Returns:
            True when saving may continue.
        """
        problems = self.log_dir_widget.validate(check_writable=file_logging_enabled)
        if not problems:
            return True

        details = "\n".join(f"• {problem}" for problem in problems)

        if not file_logging_enabled:
            reply = QMessageBox.warning(
                self, "Log Path Problem",
                f"The configured log directory cannot be used:\n\n{details}\n\n"
                "Writing logs to a file is switched off, so this does not break "
                "anything right now.\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            return reply == QMessageBox.StandardButton.Yes

        box = QMessageBox(self)
        box.setWindowTitle("Invalid Log Path")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("The configured log directory cannot be used.")
        box.setInformativeText(
            f"{details}\n\nChoose how to continue:"
        )
        fix_button = box.addButton("Let me fix it", QMessageBox.ButtonRole.RejectRole)
        default_button = box.addButton("Use the default location",
                                       QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(fix_button)
        box.exec()

        if box.clickedButton() is default_button:
            self.log_dir_widget.reset_to_defaults()
            remaining = self.log_dir_widget.validate(check_writable=True)
            if remaining:
                QMessageBox.critical(
                    self, "Save Error",
                    "Even the default log location cannot be used:\n\n"
                    + "\n".join(f"• {problem}" for problem in remaining)
                )
                return False
            logger.warning("Invalid log path replaced by the default location")
            return True

        # "Let me fix it" - jump to the logging panel and stop saving
        self._select_category("Logging")
        return False

    def _select_category(self, name: str) -> None:
        """Show the settings panel of the given category."""
        for index, (label, _icon) in enumerate(self._CATEGORIES):
            if label == name:
                self._category_list.setCurrentRow(index)
                return

    def reset_to_defaults(self):
        """Reset all settings to factory defaults."""
        reply = QMessageBox.question(
            self, "Reset Settings",
            "Reset all settings to factory defaults?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.settings.reset_to_defaults()
            self._load_all_settings()
            QMessageBox.information(self, "Reset", "Settings reset to defaults.")

    # ------------------------------------------------------------------
    # Logging — silent auto-apply
    # ------------------------------------------------------------------

    def _apply_logging_from_settings_silently(self):
        """
        Re-initialise Python logging handlers from the current SettingsManager
        logging configuration without showing any dialog.
        Called automatically when the 'logging' category changes.
        """
        try:
            self._setup_logging_handlers()
        except Exception as e:
            logger.warning("Could not re-initialise logging handlers: %s", e)

    def _setup_logging_handlers(self):
        """Re-initialise root logger handlers from current settings."""
        import logging.handlers

        cfg = self.settings.get_logging_config()
        log_dir = Path(cfg.get("log_dir", "logs")).expanduser()
        if not log_dir.is_absolute():
            from ui.log_directory_widget import get_application_directory
            log_dir = Path(get_application_directory()) / log_dir
        log_file = cfg.get("log_file", "student_management.log")
        level_name = cfg.get("log_level", "INFO")
        fmt = cfg.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        datefmt = cfg.get("date_format", "%Y-%m-%d %H:%M:%S")
        max_bytes = cfg.get("max_bytes", 10 * 1024 * 1024)
        backup_count = cfg.get("backup_count", 5)
        file_enabled = cfg.get("file_enabled", True)
        console_enabled = cfg.get("console_enabled", True)

        level = getattr(logging, level_name, logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)

        # Was: root.handlers.clear() - which dropped the handlers without
        # closing them, so the previous rotating log file stayed open for the
        # rest of the session (on Windows it could then not be renamed or
        # deleted, and its buffered records were never flushed).
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:                        # a broken handler must
                pass                                 # not stop the reconfig

        formatter = logging.Formatter(fmt, datefmt=datefmt)

        if file_enabled:
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                fh = logging.handlers.RotatingFileHandler(
                    str(log_dir / log_file),
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                fh.setFormatter(formatter)
                fh.setLevel(level)
                root.addHandler(fh)
            except (PermissionError, OSError) as e:
                print(f"Warning: Cannot create log file handler: {e}", file=sys.stderr)

        if console_enabled:
            ch = logging.StreamHandler()
            ch.setFormatter(formatter)
            ch.setLevel(level)
            root.addHandler(ch)

        # Keep the LoggingConfig singleton - which the "5. Logs" tab uses to
        # find and list the log files - in sync with the settings written here.
        # Without this, changing the log directory left the Logs tab looking at
        # the previous location.
        try:
            from utils.logging_config import get_logging_config
            log_config = get_logging_config()
            if log_config is not None:
                log_config.config.update({
                    "log_dir": str(log_dir),
                    "log_file": log_file,
                    "max_bytes": max_bytes,
                    "backup_count": backup_count,
                    "log_level": level_name,
                    "console_enabled": console_enabled,
                    "file_enabled": file_enabled,
                })
                log_config.save_config()
        except Exception as exc:
            logger.warning("Could not update the shared logging configuration: %s", exc)

        logger.debug("Logging handlers re-initialised (level=%s)", level_name)

    # ------------------------------------------------------------------
    # Theme helpers
    # ------------------------------------------------------------------

    def _pick_theme(self, name: str):
        self.theme_combo.setCurrentText(name)

    def apply_theme(self):
        name = self.theme_combo.currentText()
        if name not in self.THEMES:
            return
        try:
            self._apply_theme(name)
            self.settings.set("theme", name, category="general")
            logger.info("Applied theme: %s", name)
        except Exception as e:
            logger.exception("Error applying theme")
            QMessageBox.critical(self, "Error", f"Failed to apply theme:\n{e}")

    def _apply_theme(self, name: str):
        colors = self.THEMES[name]
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, colors["window"])
        palette.setColor(QPalette.ColorRole.WindowText, colors["window_text"])
        palette.setColor(QPalette.ColorRole.Base, colors["base"])
        palette.setColor(QPalette.ColorRole.AlternateBase, colors["alternate_base"])
        palette.setColor(QPalette.ColorRole.ToolTipBase, colors["tooltip_base"])
        palette.setColor(QPalette.ColorRole.ToolTipText, colors["tooltip_text"])
        palette.setColor(QPalette.ColorRole.Text, colors["text"])
        palette.setColor(QPalette.ColorRole.Button, colors["button"])
        palette.setColor(QPalette.ColorRole.ButtonText, colors["button_text"])
        palette.setColor(QPalette.ColorRole.BrightText, colors["bright_text"])
        palette.setColor(QPalette.ColorRole.Link, colors["link"])
        palette.setColor(QPalette.ColorRole.Highlight, colors["highlight"])
        palette.setColor(QPalette.ColorRole.HighlightedText, colors["highlighted_text"])
        QApplication.setPalette(palette)

    def _swatch_style(self, name: str) -> str:
        t = self.THEMES[name]
        w = t["window"]
        h = t["highlight"]
        text_c = "white" if name != "Light" else "black"
        return (
            f"QPushButton {{"
            f"  background-color: rgb({w.red()},{w.green()},{w.blue()});"
            f"  color: {text_c};"
            f"  border: 2px solid rgb({h.red()},{h.green()},{h.blue()});"
            f"  border-radius: 5px; font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: rgb({h.red()},{h.green()},{h.blue()});"
            f"}}"
        )
