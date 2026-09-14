"""
User name format dialog
=======================

Lets the user define how generated user names are built, the same way the
*Password Format* dialog defines how passwords are built.

The dialog edits a :class:`~utils.username_policy.UsernamePolicy` and stores it
in the application settings. ``generate_username()`` and the user-name
validator both read that policy, so a change here immediately affects
generation *and* validation - a generated user name can never be reported as
"doesn't follow standard pattern".
"""

import logging
from typing import List, Optional

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from utils.username_policy import (
    ABSOLUTE_MAX_LENGTH, DEFAULT_TEMPLATE, UsernamePolicy,
    UsernameTemplateError, get_username_policy, set_username_policy,
)

logger = logging.getLogger(__name__)


class UsernamePolicyDialog(QDialog):
    """Editor for the application-wide user name format."""

    #: Ready-made patterns offered in the combo box.
    PRESETS = [
        ("Surname + first name (default)", "{last_name}{first_name}"),
        ("Surname + first letter of the first name", "{last_name}{first_name:1}"),
        ("Surname + first 3 letters of the first name", "{last_name}{first_name:3}"),
        ("First 4 of surname . first letter", "{last_name:4}.{first_name:1}"),
        ("First letter + surname", "{first_name:1}{last_name}"),
        ("Initials + class", "{initials}{class_name}"),
    ]

    #: Names used for the live preview.
    SAMPLES = [
        ("Jan", "Novák", "6.A"),
        ("Anna Marie", "Nováková Svobodová", "7.B"),
        ("Šárka", "Čermáková", "9.C"),
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Username Format")
        self.setModal(True)
        self.setMinimumWidth(640)

        self._policy = get_username_policy()
        self._build_ui()
        self._load(self._policy)
        self._refresh_preview()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "Define how generated user names are built. The same pattern is "
            "used to <b>generate</b> and to <b>validate</b> user names, so a "
            "generated name always passes validation."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        # --- the pattern --------------------------------------------------
        pattern_group = QGroupBox("Pattern")
        pattern_form = QFormLayout(pattern_group)

        self._preset_combo = QComboBox()
        self._preset_combo.addItem("-- choose a ready-made pattern --", None)
        for label, template in self.PRESETS:
            self._preset_combo.addItem(label, template)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_chosen)
        pattern_form.addRow("Preset:", self._preset_combo)

        self._template_input = QLineEdit()
        self._template_input.setPlaceholderText(DEFAULT_TEMPLATE)
        self._template_input.textChanged.connect(self._refresh_preview)
        pattern_form.addRow("Pattern:", self._template_input)

        layout.addWidget(pattern_group)

        # --- placeholder help ----------------------------------------------
        help_label = QLabel(
            "<b>Placeholders</b><br>"
            "<code>{first_name}</code> / <code>{last_name}</code> — the whole name<br>"
            "<code>{last_name:3}</code> — the first 3 letters "
            "(any number works)<br>"
            "<code>{last_name.last}</code> — the last part of a compound "
            "surname; <code>.first</code> or a number picks another part<br>"
            "<code>{last_name.last:3}</code> — the two combined<br>"
            "<code>{class_name}</code> — the class &nbsp;·&nbsp; "
            "<code>{initials}</code> — first letters of both names<br><br>"
            "Anything else is copied through, so <code>.</code>, <code>-</code> "
            "and <code>_</code> can be used as separators. Diacritics are "
            "removed automatically and a counter is appended when a name is "
            "already taken."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #999; font-size: 10px;")
        layout.addWidget(help_label)

        # --- options ---------------------------------------------------------
        options_group = QGroupBox("Options")
        options_form = QFormLayout(options_group)

        self._lowercase_check = QCheckBox("Convert to lower case")
        self._lowercase_check.toggled.connect(self._refresh_preview)
        options_form.addRow("", self._lowercase_check)

        self._max_length_spin = QSpinBox()
        self._max_length_spin.setRange(1, ABSOLUTE_MAX_LENGTH)
        self._max_length_spin.setToolTip(
            f"Active Directory allows at most {ABSOLUTE_MAX_LENGTH} characters "
            f"in a sAMAccountName."
        )
        self._max_length_spin.valueChanged.connect(self._refresh_preview)
        options_form.addRow("Maximum length:", self._max_length_spin)

        layout.addWidget(options_group)

        # --- preview -----------------------------------------------------------
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)

        self._preview_table = QTableWidget(0, 2)
        self._preview_table.setHorizontalHeaderLabels(["Person", "User name"])
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._preview_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self._preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._preview_table.setMaximumHeight(130)
        preview_layout.addWidget(self._preview_table)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 10px;")
        preview_layout.addWidget(self._status_label)

        layout.addWidget(preview_group)

        # --- buttons ------------------------------------------------------------
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        button_box.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self._on_restore_defaults)
        layout.addWidget(button_box)

    # -- data ----------------------------------------------------------------

    def _load(self, policy: UsernamePolicy) -> None:
        """Copy *policy* into the widgets."""
        for widget in (self._template_input, self._lowercase_check,
                       self._max_length_spin):
            widget.blockSignals(True)
        self._template_input.setText(policy.template)
        self._lowercase_check.setChecked(policy.lowercase)
        self._max_length_spin.setValue(policy.max_length)
        for widget in (self._template_input, self._lowercase_check,
                       self._max_length_spin):
            widget.blockSignals(False)

    def build_policy(self) -> UsernamePolicy:
        """Build a policy from the current widget values."""
        return UsernamePolicy(
            template=self._template_input.text().strip() or DEFAULT_TEMPLATE,
            lowercase=self._lowercase_check.isChecked(),
            max_length=self._max_length_spin.value(),
        )

    # -- slots ----------------------------------------------------------------

    def _on_preset_chosen(self, _index: int) -> None:
        template = self._preset_combo.currentData()
        if template:
            self._template_input.setText(template)

    def _refresh_preview(self, *_args) -> None:
        """Render the sample names through the current pattern."""
        policy = self.build_policy()
        problems = policy.problems()

        if problems:
            self._status_label.setStyleSheet("color: #ff6b6b; font-size: 10px;")
            self._status_label.setText(
                "<br>".join(f"✖ {problem}" for problem in problems)
            )
            self._preview_table.setRowCount(0)
            return

        self._preview_table.setRowCount(len(self.SAMPLES))
        taken = set()
        for row, (first, last, class_name) in enumerate(self.SAMPLES):
            try:
                rendered = policy.render(first, last, class_name)
                while rendered in taken:
                    rendered += "2"
                taken.add(rendered)
            except UsernameTemplateError as exc:
                rendered = f"⚠ {exc}"
            self._preview_table.setItem(row, 0, QTableWidgetItem(f"{first} {last}"))
            self._preview_table.setItem(row, 1, QTableWidgetItem(rendered))

        self._status_label.setStyleSheet("color: #aaa; font-size: 10px;")
        self._status_label.setText(policy.describe())

    def _on_restore_defaults(self) -> None:
        self._load(UsernamePolicy())
        self._refresh_preview()

    def _on_accept(self) -> None:
        policy = self.build_policy()
        problems = policy.problems()
        if problems:
            QMessageBox.warning(
                self, "Invalid Pattern",
                "The user name pattern cannot be used:\n\n"
                + "\n".join(f"• {problem}" for problem in problems)
            )
            return

        try:
            saved = set_username_policy(policy)
        except ValueError as exc:                    # pragma: no cover - guarded above
            QMessageBox.warning(self, "Invalid Pattern", str(exc))
            return

        if not saved:
            QMessageBox.warning(
                self, "Not Saved",
                "The pattern is active for this session but could not be "
                "written to the settings file."
            )

        self._policy = policy
        logger.info("User name policy updated: %s", policy.describe())
        self.accept()

    def get_policy(self) -> UsernamePolicy:
        """Return the policy that was confirmed."""
        return self._policy
