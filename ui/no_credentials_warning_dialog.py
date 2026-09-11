"""
NoCredentialsWarningDialog
Warning shown when Group Management is opened without AD credentials.
Extracted to its own module for logical separation (Issue 3a).
"""

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from utils.settings_manager import get_settings


class NoCredentialsWarningDialog(QDialog):
    """
    Warning dialog shown when opening Group Management without AD credentials.

    Offers the user a 'Don't show this warning again' checkbox that, when
    accepted, persists the preference via SettingsManager.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Group Management — No AD Connection")
        self.setModal(True)
        self.setMinimumWidth(500)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("<h2>&#9888; Limited Functionality</h2>")
        layout.addWidget(title)

        info = QLabel(
            "<p>You are opening Group Management without providing Active Directory"
            " credentials.</p>"
            "<p><b>Available features:</b></p>"
            "<ul>"
            "<li>&#10003; Add groups manually by DN</li>"
            "<li>&#10003; Create and manage templates</li>"
            "<li>&#10003; Export/import groups and templates</li>"
            "<li>&#10003; Manage group lists</li>"
            "</ul>"
            "<p><b>Unavailable features:</b></p>"
            "<ul>"
            "<li>&#10007; Discover groups from AD</li>"
            "<li>&#10007; Verify groups in AD</li>"
            "<li>&#10007; Auto-fetch group details</li>"
            "</ul>"
            "<p><i>You can manually add groups by DN. To verify them later,"
            " provide AD credentials through the main window.</i></p>"
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "QLabel { background-color: #FFF3CD; padding: 15px;"
            " border: 1px solid #FFC107; }"
        )
        layout.addWidget(info)

        self.dont_show_checkbox = QCheckBox("Don't show this warning again")
        layout.addWidget(self.dont_show_checkbox)

        btn_row = QHBoxLayout()
        continue_btn = QPushButton("Continue")
        continue_btn.setDefault(True)
        continue_btn.clicked.connect(self.accept)
        btn_row.addWidget(continue_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def accept(self):
        """Persist 'don't show again' preference before accepting."""
        if self.dont_show_checkbox.isChecked():
            # Persist via SettingsManager, which emits category_changed("general")
            # so SettingsTab.warn_on_no_creds refreshes automatically.
            get_settings().set(
                "show_group_management_warning", False, category="general"
            )
        super().accept()
