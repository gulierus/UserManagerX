"""
ExportDialog
Combined file-path + encryption-password dialog for exporting groups/templates.
Extracted to its own module for logical separation (Issue 3d).
"""

from typing import Tuple

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from utils.encryption import get_available_methods


class ExportDialog(QDialog):
    """
    Single-window dialog for configuring the export of groups or templates.

    Combines file-path selection, encryption method, and password entry into
    one step, replacing the old sequential QFileDialog + QInputDialog pattern.
    """

    def __init__(self, title: str = "Export", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self._file_path = ""
        self._method = "aes-gcm"
        self._password = ""
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # File selection
        file_grp = QGroupBox("Destination File")
        fl = QHBoxLayout(file_grp)
        self._file_input = QLineEdit()
        self._file_input.setPlaceholderText("Select destination file...")
        self._file_input.setReadOnly(True)
        fl.addWidget(self._file_input, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        fl.addWidget(browse_btn)
        layout.addWidget(file_grp)

        # Encryption method
        enc_grp = QGroupBox("Encryption Method")
        el = QHBoxLayout(enc_grp)
        self._method_combo = QComboBox()
        self._method_combo.addItem("AES-GCM (Recommended)", "aes-gcm")
        self._method_combo.addItem("GPG / OpenPGP", "gpg")
        el.addWidget(self._method_combo)
        el.addStretch()
        layout.addWidget(enc_grp)

        # Password
        pass_grp = QGroupBox("Encryption Password")
        from PyQt6.QtWidgets import QVBoxLayout as _VL
        pl = _VL(pass_grp)
        self._pass_input = QLineEdit()
        self._pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_input.setPlaceholderText("Enter encryption password")
        pl.addWidget(self._pass_input)
        self._pass_confirm = QLineEdit()
        self._pass_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_confirm.setPlaceholderText("Confirm password")
        pl.addWidget(self._pass_confirm)
        layout.addWidget(pass_grp)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save File", "", "USRX Files (*.usrx);;All Files (*)"
        )
        if path:
            self._file_input.setText(path)

    def _on_accept(self):
        file_path = self._file_input.text().strip()
        if not file_path:
            QMessageBox.warning(self, "Missing File", "Please select a destination file.")
            return

        # The chosen backend has to exist *before* the password is collected and
        # the export starts: picking GPG on a machine without GPG used to be
        # accepted here and only blew up inside the encryption layer, after the
        # user had entered the password twice.  ImportDialog already checks this.
        method = self._method_combo.currentData()
        try:
            available = get_available_methods()
        except Exception:
            available = {}
        if not available.get(method, False):
            QMessageBox.warning(
                self, "Method Not Available",
                f"The selected encryption method ({method}) is not available "
                f"on this computer.\n\nChoose another method or install the "
                f"required component."
            )
            return

        password = self._pass_input.text()
        if not password:
            QMessageBox.warning(self, "Missing Password", "Please enter a password.")
            return
        if password != self._pass_confirm.text():
            QMessageBox.warning(
                self, "Password Mismatch", "Passwords do not match. Please re-enter."
            )
            return
        self._file_path = file_path
        self._password = password
        self._method = method
        self.accept()

    def get_values(self) -> Tuple[str, str, str]:
        """Return *(file_path, method, password)*."""
        return self._file_path, self._method, self._password
