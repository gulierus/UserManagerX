"""
Editing one person's Microsoft 365 fields

Version 26, point 21 e: the Edit button on every row of the Microsoft 365
table opens a window "that will allow the user to set all possible fields that
are logically related to Microsoft 365 and the person".

Only Microsoft 365 fields appear here.  The Active Directory display name, the
home directory and the password flags belong to the other operation's editor -
a person can exist in both directories with different values, and mixing them
in one window is how they get confused.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from models import Person
from models_m365 import M365Status

logger = logging.getLogger(__name__)


class M365PersonDialog(QDialog):
    """Edits the Microsoft 365 fields of one person."""

    def __init__(self, person: Person, sync_config=None, parent=None):
        """
        Args:
            person: The person to edit.  Only written to when the dialog is
                accepted, so Cancel really cancels.
            sync_config: The current :class:`~services.m365_services.M365SyncConfig`,
                used by the "Generate" buttons so they produce exactly what a
                synchronisation would.
            parent: Qt parent.
        """
        super().__init__(parent)
        self.person = person
        self.sync_config = sync_config

        self.setWindowTitle(
            f"Microsoft 365 — {person.first_name} {person.last_name}")
        self.setModal(True)
        self.setMinimumWidth(620)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        identity = QGroupBox("Person")
        identity_layout = QFormLayout(identity)
        identity_layout.addRow("Name:", QLabel(
            f"{self.person.first_name} {self.person.last_name}"))
        identity_layout.addRow("Class:", QLabel(self.person.class_name))
        status_label = QLabel(self.person.m365_status.label)
        status_label.setToolTip(self.person.m365_status.description)
        identity_layout.addRow("Status:", status_label)
        if self.person.m365_object_id:
            object_label = QLabel(self.person.m365_object_id)
            object_label.setStyleSheet("color: #888;")
            identity_layout.addRow("Object ID:", object_label)
        layout.addWidget(identity)

        account = QGroupBox("Microsoft 365 Account")
        account_layout = QFormLayout(account)

        self.upn_input = QLineEdit(self.person.m365_user_principal_name or "")
        self.upn_input.setPlaceholderText("novakj@skola.onmicrosoft.com")
        account_layout.addRow("Sign-in name:",
                              self._with_generate(self.upn_input,
                                                  self._generate_upn))

        self.display_name_input = QLineEdit(self.person.m365_display_name or "")
        self.display_name_input.setPlaceholderText("Jan Novák (6.A)")
        account_layout.addRow("Display name:",
                              self._with_generate(self.display_name_input,
                                                  self._generate_display_name))

        self.nickname_input = QLineEdit(self.person.m365_mail_nickname or "")
        self.nickname_input.setPlaceholderText("novakj")
        self.nickname_input.setToolTip(
            "The alias Microsoft 365 builds the address from. Letters, digits, "
            "'-' and '_' only.")
        account_layout.addRow("Alias:",
                              self._with_generate(self.nickname_input,
                                                  self._generate_nickname))

        self.usage_location_input = QLineEdit(
            self.person.m365_usage_location or "")
        self.usage_location_input.setPlaceholderText("CZ")
        self.usage_location_input.setMaxLength(2)
        self.usage_location_input.setToolTip(
            "Two-letter country code. Microsoft requires it before a licence "
            "can be assigned to the account.")
        account_layout.addRow("Usage location:", self.usage_location_input)

        layout.addWidget(account)

        password_group = QGroupBox("Password")
        password_layout = QVBoxLayout(password_group)

        row = QHBoxLayout()
        self.password_input = QLineEdit(self.person.m365_password or "")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.password_input, stretch=1)

        show_button = QPushButton("👁")
        show_button.setMaximumWidth(40)
        show_button.setToolTip("Hold to show the password")
        show_button.pressed.connect(
            lambda: self.password_input.setEchoMode(QLineEdit.EchoMode.Normal))
        show_button.released.connect(
            lambda: self.password_input.setEchoMode(QLineEdit.EchoMode.Password))
        row.addWidget(show_button)

        generate_button = QPushButton("Generate New")
        generate_button.clicked.connect(self._generate_password)
        row.addWidget(generate_button)
        password_layout.addLayout(row)

        note = QLabel(
            "The password is written to Microsoft 365 when this person is "
            "created or when 'Set password' is used in Bulk Edit. It must "
            "satisfy the tenant's password policy, or Microsoft refuses it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        password_layout.addWidget(note)
        layout.addWidget(password_group)

        self.enabled_check = QCheckBox("Account enabled")
        self.enabled_check.setChecked(self.person.account_enabled)
        layout.addWidget(self.enabled_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_changes)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _with_generate(field: QLineEdit, slot) -> QHBoxLayout:
        """A field with a "Generate" button beside it."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, stretch=1)
        button = QPushButton("Generate")
        button.setToolTip("Build this from the configured format")
        button.clicked.connect(slot)
        row.addWidget(button)
        return row

    # -- generating -------------------------------------------------------

    def _generate_upn(self) -> None:
        """Build the sign-in name from the configured format."""
        self._generate_into(self.upn_input, "upn_for")

    def _generate_display_name(self) -> None:
        """Build the display name from the configured format."""
        self._generate_into(self.display_name_input, "display_name_for")

    def _generate_into(self, field: QLineEdit, method: str) -> None:
        """Run one of the configuration's name builders into a field."""
        from utils.name_templates import TemplateError

        if self.sync_config is None:
            return
        try:
            field.setText(getattr(self.sync_config, method)(self.person))
        except TemplateError as exc:
            # A broken template is a configuration problem, not a reason to
            # write an error message into an account field.
            field.setPlaceholderText(str(exc))
            logger.debug("Could not generate with %s: %s", method, exc)

    def _generate_nickname(self) -> None:
        """Derive the alias from the sign-in name."""
        from services.m365_client import mail_nickname_for

        upn = self.upn_input.text().strip()
        local_part = upn.split("@", 1)[0] if upn else (
            f"{self.person.last_name}{self.person.first_name[:1]}")
        self.nickname_input.setText(mail_nickname_for(local_part, "user"))

    def _generate_password(self) -> None:
        """Generate a password from the application's policy."""
        from utils.ad_utils import generate_password

        self.password_input.setText(generate_password())

    # -- result -----------------------------------------------------------

    def accept_changes(self) -> None:
        """
        Write the fields onto the person and close.

        Everything is written through the person's properties, so the dirty
        tracking and the Microsoft 365 status follow along by themselves.
        """
        self.person.m365_user_principal_name = \
            self.upn_input.text().strip() or None
        self.person.m365_display_name = \
            self.display_name_input.text().strip() or None
        self.person.m365_mail_nickname = \
            self.nickname_input.text().strip() or None
        self.person.m365_usage_location = \
            self.usage_location_input.text().strip().upper() or None
        self.person.m365_password = self.password_input.text() or None
        self.person.account_enabled = self.enabled_check.isChecked()

        self.accept()
