"""
ImportDialog
Combined file-path + decryption-method + decryption-password dialog for
importing groups/templates.

The import operation needs:
  - a source file to open (read),
  - the decryption method, and
  - the decryption password.

The encryption method is written into the file (USRX header byte) or can be
recognised from its content, so *Auto-detect* is offered as the default and is
what the previous version of this dialog always used silently.  The user can
now override it - exactly like on the "Encrypted File" data source - which is
needed when

  * the file has no recognisable header (auto-detection then fails with
    "Cannot detect encryption method from file format"), or
  * the file was renamed / produced by another tool and the detection would
    pick the wrong method.
"""

import logging
from typing import Optional, Tuple

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from utils.encryption import get_available_methods

logger = logging.getLogger(__name__)


class ImportDialog(QDialog):
    """
    Single-window dialog for configuring the import of groups or templates.

    Combines source-file selection, decryption-method selection and password
    entry into one step.
    """

    #: ``(label, value)`` pairs offered in the method combo box.
    METHODS = (
        ("Auto-detect (recommended)", "auto"),
        ("GPG (OpenPGP)", "gpg"),
        ("AES-GCM", "aes-gcm"),
    )

    def __init__(self, title: str = "Import", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self._file_path = ""
        self._password = ""
        self._method: Optional[str] = None
        self._build_ui()
        self._update_method_availability()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- File selection ------------------------------------------------
        file_grp = QGroupBox("Source File")
        file_layout = QVBoxLayout(file_grp)

        row = QHBoxLayout()
        self._file_input = QLineEdit()
        self._file_input.setPlaceholderText("Select source file...")
        self._file_input.setReadOnly(True)
        row.addWidget(self._file_input, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        file_layout.addLayout(row)

        self._file_info = QLabel()
        self._file_info.setWordWrap(True)
        self._file_info.setStyleSheet(
            "color: #888; font-size: 10px; font-style: italic;"
        )
        file_layout.addWidget(self._file_info)

        layout.addWidget(file_grp)

        # --- Decryption method ---------------------------------------------
        method_grp = QGroupBox("Decryption Method")
        method_layout = QVBoxLayout(method_grp)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Method:"))
        self._method_combo = QComboBox()
        for label, value in self.METHODS:
            self._method_combo.addItem(label, value)
        self._method_combo.setToolTip(
            "Auto-detect reads the method from the file itself. Choose a "
            "method explicitly if detection fails or picks the wrong one."
        )
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_row.addWidget(self._method_combo, stretch=1)
        method_layout.addLayout(method_row)

        self._method_status = QLabel()
        self._method_status.setWordWrap(True)
        self._method_status.setStyleSheet("color: #aaa; font-size: 10px;")
        method_layout.addWidget(self._method_status)

        layout.addWidget(method_grp)

        # --- Password — note: no confirm field (decryption only) -----------
        pass_grp = QGroupBox("Decryption Password")
        pass_layout = QVBoxLayout(pass_grp)
        self._pass_input = QLineEdit()
        self._pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_input.setPlaceholderText("Enter decryption password")
        pass_layout.addWidget(self._pass_input)
        layout.addWidget(pass_grp)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Availability / detection
    # ------------------------------------------------------------------

    def _update_method_availability(self):
        """Disable methods whose backend is not installed on this machine."""
        try:
            available = get_available_methods()
        except Exception:
            logger.exception("Could not query available encryption methods")
            available = {}

        parts = []
        for index in range(self._method_combo.count()):
            value = self._method_combo.itemData(index)
            if value == "auto":
                continue

            usable = bool(available.get(value, False))
            label = dict((v, l) for l, v in self.METHODS)[value]
            self._method_combo.setItemText(
                index, label if usable else f"{label} - not available"
            )

            item = self._method_combo.model().item(index)
            if item is not None:
                item.setEnabled(usable)

            parts.append(f"{label}: {'available' if usable else 'not available'}")

        self._method_status.setText(" • ".join(parts))

    def _on_method_changed(self, _index: int):
        """Update the file filter hint when the method changes."""
        self._describe_file(self._file_input.text().strip())

    def _describe_file(self, path: str):
        """Show what the application can tell about the selected file."""
        if not path:
            self._file_info.setText("")
            return

        try:
            from utils.usrx_format import is_usrx_file
            if is_usrx_file(path):
                from utils.usrx_format import USRXFile
                header = USRXFile.read_header(path)
                method = str(header.get("encryption_type", "unknown")).upper()
                self._file_info.setText(
                    f"USRX container • encryption stored in the file: {method}"
                )
                return
        except Exception as exc:
            logger.debug("USRX header could not be read: %s", exc)

        try:
            from utils.encryption import detect_encryption_method
            detected = detect_encryption_method(path)
            self._file_info.setText(f"Detected encryption: {str(detected).upper()}")
        except Exception:
            self._file_info.setText(
                "Encryption method could not be detected automatically - "
                "please select it manually."
            )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _browse(self):
        method = self._method_combo.currentData()
        if method == "gpg":
            file_filter = "GPG Files (*.gpg *.asc);;USRX Files (*.usrx);;All Files (*)"
        elif method == "aes-gcm":
            file_filter = "Encrypted Files (*.enc *.json);;USRX Files (*.usrx);;All Files (*)"
        else:
            file_filter = "USRX Files (*.usrx);;All Files (*)"

        path, _ = QFileDialog.getOpenFileName(self, "Open File", "", file_filter)
        if path:
            self._file_input.setText(path)
            self._describe_file(path)

    def _on_accept(self):
        file_path = self._file_input.text().strip()
        if not file_path:
            QMessageBox.warning(self, "Missing File", "Please select a source file.")
            return

        method = self._method_combo.currentData()
        if method != "auto":
            try:
                available = get_available_methods()
            except Exception:
                available = {}
            if not available.get(method, False):
                QMessageBox.warning(
                    self, "Method Not Available",
                    f"The selected decryption method ({method}) is not "
                    f"available on this computer.\n\nChoose another method or "
                    f"install the required component."
                )
                return

        password = self._pass_input.text()
        if not password:
            QMessageBox.warning(self, "Missing Password",
                                "Please enter the decryption password.")
            return

        self._file_path = file_path
        self._password = password
        self._method = None if method == "auto" else method
        self.accept()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def get_values(self) -> Tuple[str, str]:
        """
        Return *(file_path, password)*.

        Kept for backward compatibility with callers that do not care about
        the method; use :meth:`get_full_values` to get the method as well.
        """
        return self._file_path, self._password

    def get_full_values(self) -> Tuple[str, str, Optional[str]]:
        """
        Return *(file_path, password, encryption_method)*.

        ``encryption_method`` is ``None`` when the user chose auto-detection,
        which is exactly what the decryption helpers expect for that case.
        """
        return self._file_path, self._password, self._method

    def get_method(self) -> Optional[str]:
        """Return the chosen method, or ``None`` for auto-detection."""
        return self._method
