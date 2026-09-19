"""
The Microsoft 365 sign-in panel
===============================

One widget, used in two places: the "Microsoft 365 From Web" source on the
"1. Data Sources" tab and the "Microsoft 365 Management" operation on the
"3. Operations" tab.  Version 26, point 21 asks for the same two (here three)
sign-in methods in both, and building the panel twice would guarantee they
drift apart.

The panel's job is to collect credentials and to **say what each method can and
cannot do before the user tries it** - particularly that the user-name-and-
password method cannot satisfy multi-factor authentication.  Discovering that
as ``AADSTS50076`` after typing a password is a bad way to learn it.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from services.m365_auth import AuthMethod, M365Credentials

logger = logging.getLogger(__name__)


class M365ConnectionWidget(QGroupBox):
    """
    Collects everything needed to sign in to Microsoft 365.

    The fields shown follow the chosen method, so the panel never asks for a
    client secret when the user is signing in with a device code.
    """

    def __init__(self, title: str = "Microsoft 365 Connection", parent=None):
        super().__init__(title, parent)
        self._init_ui()
        self._on_method_changed()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.method_combo = QComboBox()
        for method in AuthMethod:
            self.method_combo.addItem(method.label, method)
        # App-only is the default: it is the only method that is never blocked
        # by a second factor and needs no interaction.
        self.method_combo.setCurrentIndex(
            self.method_combo.findData(AuthMethod.APP_ONLY))
        self.method_combo.currentIndexChanged.connect(self._on_method_changed)
        form.addRow("Sign in with:", self.method_combo)

        self.tenant_input = QLineEdit()
        self.tenant_input.setPlaceholderText("skola.onmicrosoft.com or the directory ID")
        self.tenant_input.setToolTip(
            "The tenant to sign in to.\n"
            "App-only sign-in needs it. The other two methods can work it out "
            "from the account."
        )
        self.tenant_row = form.rowCount()
        form.addRow("Tenant ID:", self.tenant_input)

        self.client_id_input = QLineEdit()
        self.client_id_input.setPlaceholderText(
            "Application (client) ID from Entra ID")
        self.client_id_input.setToolTip(
            "The application registration to sign in with.\n"
            "Leave it empty for the two user methods to use Microsoft's own "
            "public client."
        )
        form.addRow("Client ID:", self.client_id_input)

        self.client_secret_input = QLineEdit()
        self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_input.setPlaceholderText("Client secret value")
        form.addRow("Client Secret:", self.client_secret_input)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("admin@skola.cz")
        form.addRow("User name:", self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.password_input)

        layout.addLayout(form)
        self._form = form

        self.method_hint = QLabel()
        self.method_hint.setWordWrap(True)
        self.method_hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.method_hint)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #e6a93c; font-size: 11px;")
        layout.addWidget(self.warning_label)

    # -- behaviour --------------------------------------------------------

    @property
    def method(self) -> AuthMethod:
        """The chosen sign-in method."""
        return self.method_combo.currentData() or AuthMethod.APP_ONLY

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        """Show or hide one form row, label included."""
        field.setVisible(visible)
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _on_method_changed(self, *_args) -> None:
        """Show only the fields the chosen method actually uses."""
        method = self.method

        self._set_row_visible(self.client_secret_input,
                              method is AuthMethod.APP_ONLY)
        self._set_row_visible(self.username_input,
                              method is AuthMethod.USERNAME_PASSWORD)
        self._set_row_visible(self.password_input,
                              method is AuthMethod.USERNAME_PASSWORD)
        # Tenant and client id are used by every method, but only app-only
        # requires them; the placeholder text says so.
        self.tenant_input.setPlaceholderText(
            "Directory (tenant) ID — required"
            if method is AuthMethod.APP_ONLY
            else "Optional — worked out from the account")
        self.client_id_input.setPlaceholderText(
            "Application (client) ID — required"
            if method is AuthMethod.APP_ONLY
            else "Optional — Microsoft's public client is used")

        self.method_hint.setText(method.description)
        self._refresh_warning()

    def _refresh_warning(self) -> None:
        """Say up front what will surprise about this method."""
        warnings = self.credentials().warnings()
        self.warning_label.setText("  ".join(warnings))
        self.warning_label.setVisible(bool(warnings))

    # -- result -----------------------------------------------------------

    def credentials(self) -> M365Credentials:
        """What the user has filled in, as a credentials object."""
        return M365Credentials(
            method=self.method,
            tenant_id=self.tenant_input.text().strip(),
            client_id=self.client_id_input.text().strip(),
            client_secret=self.client_secret_input.text(),
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
        )

    def set_credentials(self, credentials: M365Credentials) -> None:
        """
        Fill the panel in, for a remembered connection.

        The secret and the password are **not** restored: they are never
        stored, so there is nothing to restore.
        """
        index = self.method_combo.findData(credentials.method)
        if index >= 0:
            self.method_combo.setCurrentIndex(index)
        self.tenant_input.setText(credentials.tenant_id or "")
        self.client_id_input.setText(credentials.client_id or "")
        self.username_input.setText(credentials.username or "")

    def problems(self) -> list:
        """What would stop this sign-in, checked without sending anything."""
        return self.credentials().problems()
