"""
Setting Microsoft 365 fields for several people at once

Version 26, point 21 e: "a window that will allow the user to set values for
certain fields in bulk. The window must also allow setting a password in bulk
for selected users (for example, if we want to set the same password for X
people)."

Design rule, learned the hard way
---------------------------------
Every field has a **tick box of its own**, and a field that is not ticked is
not written.  An earlier bulk edit in this application applied whatever the
widgets happened to hold, which meant that opening the window and pressing OK
silently blanked fields nobody had touched (version 24, point E05).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


class M365BulkEditDialog(QDialog):
    """Applies a chosen set of Microsoft 365 fields to several people."""

    def __init__(self, persons: List, sync_config=None, parent=None):
        """
        Args:
            persons: The people the changes will be applied to.
            sync_config: The current synchronisation configuration, so the
                generated names match what a synchronisation would produce.
            parent: Qt parent.
        """
        super().__init__(parent)
        self.persons = list(persons)
        self.sync_config = sync_config

        self.setWindowTitle(f"Bulk Edit — {len(self.persons)} person(s)")
        self.setModal(True)
        self.setMinimumWidth(640)

        #: ``field -> (tick box, value widget)``.
        self._rows: Dict[str, tuple] = {}
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        caption = QLabel(
            f"These changes will be applied to <b>{len(self.persons)}</b> "
            f"person(s).<br>"
            "<span style='color:#888;'>Only the ticked fields are written. "
            "Anything left unticked is not touched.</span>"
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        fields_group = QGroupBox("Fields")
        grid = QGridLayout(fields_group)

        self._add_row(grid, 0, 'm365_usage_location', "Usage location",
                      placeholder="CZ",
                      tooltip="Two-letter country code. Microsoft requires it "
                              "before a licence can be assigned.")
        self._add_row(grid, 1, 'm365_mail_nickname', "Alias prefix",
                      placeholder="trida6a",
                      tooltip="Written as-is. Letters, digits, '-' and '_' "
                              "only.")
        layout.addWidget(fields_group)

        generated_group = QGroupBox("Generated for each person")
        generated_layout = QVBoxLayout(generated_group)

        self.generate_upn_check = QCheckBox(
            "Rebuild the sign-in name from the configured format")
        self.generate_upn_check.setToolTip(
            "Each person gets their own name, built from the Sign-in Name "
            "Format.")
        generated_layout.addWidget(self.generate_upn_check)

        self.generate_display_check = QCheckBox(
            "Rebuild the display name from the configured format")
        generated_layout.addWidget(self.generate_display_check)
        layout.addWidget(generated_group)

        password_group = QGroupBox("Password")
        password_layout = QVBoxLayout(password_group)

        self.same_password_check = QCheckBox(
            "Set the same password for everyone")
        self.same_password_check.toggled.connect(self._on_password_mode)
        password_layout.addWidget(self.same_password_check)

        row = QHBoxLayout()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setEnabled(False)
        self.password_input.setPlaceholderText("The password everyone gets")
        row.addWidget(self.password_input, stretch=1)

        self.show_password_button = QPushButton("👁")
        self.show_password_button.setMaximumWidth(40)
        self.show_password_button.setEnabled(False)
        self.show_password_button.pressed.connect(
            lambda: self.password_input.setEchoMode(QLineEdit.EchoMode.Normal))
        self.show_password_button.released.connect(
            lambda: self.password_input.setEchoMode(QLineEdit.EchoMode.Password))
        row.addWidget(self.show_password_button)
        password_layout.addLayout(row)

        self.generate_passwords_check = QCheckBox(
            "Give everyone a different, newly generated password")
        self.generate_passwords_check.toggled.connect(self._on_password_mode)
        password_layout.addWidget(self.generate_passwords_check)

        note = QLabel(
            "The password is stored on each person and written to "
            "Microsoft 365 by the next synchronisation. One password for a "
            "whole class is convenient to hand out but means every pupil can "
            "sign in as any other until they change it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        password_layout.addWidget(note)
        layout.addWidget(password_group)

        self.enabled_check = QCheckBox("Set 'account enabled' to:")
        self.enabled_value = QCheckBox("enabled")
        self.enabled_value.setChecked(True)
        self.enabled_value.setEnabled(False)
        self.enabled_check.toggled.connect(self.enabled_value.setEnabled)
        enabled_row = QHBoxLayout()
        enabled_row.addWidget(self.enabled_check)
        enabled_row.addWidget(self.enabled_value)
        enabled_row.addStretch()
        layout.addLayout(enabled_row)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
        layout.addWidget(self.problem_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.apply_changes)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, grid: QGridLayout, row: int, field: str, label: str,
                 placeholder: str = "", tooltip: str = "") -> None:
        """One tick box plus one value field."""
        tick = QCheckBox(label)
        value = QLineEdit()
        value.setEnabled(False)
        value.setPlaceholderText(placeholder)
        if tooltip:
            tick.setToolTip(tooltip)
            value.setToolTip(tooltip)
        tick.toggled.connect(value.setEnabled)

        grid.addWidget(tick, row, 0)
        grid.addWidget(value, row, 1)
        self._rows[field] = (tick, value)

    def _on_password_mode(self, _checked: bool) -> None:
        """The two password modes exclude each other."""
        same = self.same_password_check.isChecked()
        generate = self.generate_passwords_check.isChecked()

        if same and generate:
            # Whichever was just ticked wins; the other is cleared.
            sender = self.sender()
            other = (self.generate_passwords_check if sender is
                     self.same_password_check else self.same_password_check)
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
            same = self.same_password_check.isChecked()

        self.password_input.setEnabled(same)
        self.show_password_button.setEnabled(same)

    # -- result -----------------------------------------------------------

    def problems(self) -> List[str]:
        """Everything that would stop the changes being applied."""
        problems: List[str] = []

        if self.same_password_check.isChecked() and not self.password_input.text():
            problems.append("Type the password everyone should get.")

        location_tick, location_value = self._rows['m365_usage_location']
        if location_tick.isChecked():
            text = location_value.text().strip()
            if len(text) != 2 or not text.isalpha():
                problems.append(
                    "The usage location is a two-letter country code, for "
                    "example CZ.")

        if not self._anything_chosen():
            problems.append("Nothing was ticked, so there is nothing to apply.")

        return problems

    def _anything_chosen(self) -> bool:
        """Whether the user asked for any change at all."""
        return any([
            any(tick.isChecked() for tick, _value in self._rows.values()),
            self.generate_upn_check.isChecked(),
            self.generate_display_check.isChecked(),
            self.same_password_check.isChecked(),
            self.generate_passwords_check.isChecked(),
            self.enabled_check.isChecked(),
        ])

    def apply_changes(self) -> None:
        """Write the ticked fields onto every person, then close."""
        problems = self.problems()
        if problems:
            self.problem_label.setText("  ".join(problems))
            return

        from utils.ad_utils import generate_password
        from utils.name_templates import TemplateError

        failures: List[str] = []

        for person in self.persons:
            for field, (tick, value) in self._rows.items():
                if tick.isChecked():
                    setattr(person, field, value.text().strip() or None)

            if self.generate_upn_check.isChecked() and self.sync_config:
                try:
                    person.m365_user_principal_name = \
                        self.sync_config.upn_for(person)
                except TemplateError as exc:
                    failures.append(
                        f"{person.first_name} {person.last_name}: {exc}")

            if self.generate_display_check.isChecked() and self.sync_config:
                try:
                    person.m365_display_name = \
                        self.sync_config.display_name_for(person)
                except TemplateError as exc:
                    failures.append(
                        f"{person.first_name} {person.last_name}: {exc}")

            if self.same_password_check.isChecked():
                person.m365_password = self.password_input.text()
            elif self.generate_passwords_check.isChecked():
                person.m365_password = generate_password()

            if self.enabled_check.isChecked():
                person.account_enabled = self.enabled_value.isChecked()

        if failures:
            QMessageBox.warning(
                self, "Some Names Could Not Be Built",
                "The other changes were applied.\n\n" + "\n".join(failures[:10])
                + ("\n..." if len(failures) > 10 else "")
            )

        self.accept()
