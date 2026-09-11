"""
Group Management Dialog
Supports offline mode without AD credentials.

Changes applied
---------------
2a/2b  refresh_template_list() now stores the GroupTemplate object on each
       QListWidgetItem via UserRole so rename / copy / delete can retrieve it
       reliably (previously the data was never set, causing AttributeError and
       the "Selected item has no template data" message).

2b     rename_template() now reads the template from UserRole instead of
       trying to access .name on None.

2c     create_template_from_groups() uses an inline validation status area
       (styled like property_editor.py) and prevents dialog acceptance until
       all required fields are filled.

2d     handle_imported_groups() / handle_imported_templates():
       • No confirmation window if there are no duplicates.
       • A detailed side-by-side comparison window is shown for each duplicate
         so the user can make an informed decision.
       • A summary window is shown after the operation.

2e     GroupManagementDialog now accepts initial_groups and initial_templates
       constructor arguments so that groups/templates persist across dialog
       re-openings.  The template_manager is pre-populated from
       initial_templates.  ad_management.py passes the stored lists each time
       it re-creates the dialog.

3a     NoCredentialsWarningDialog moved to ui/no_credentials_warning_dialog.py.

3b/3c  _DuplicateGroupComparisonDialog and _DuplicateTemplateComparisonDialog
       moved to ui/duplicate_comparison_dialog.py with a redesigned UI that
       shows all duplicates at once (list on left, detail on right) and
       supports bulk actions.

3d     ExportDialog moved to ui/export_dialog.py.

3e     ImportDialog moved to ui/import_dialog.py (separated from ExportDialog).
"""

import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from models import ADGroup, GroupTemplate, VerificationStatus
from services.ad_client import ADClient
from services.ad_group_service import ADGroupService, GroupTemplateManager
from ui.no_credentials_warning_dialog import NoCredentialsWarningDialog
from ui.export_dialog import ExportDialog
from ui.import_dialog import ImportDialog
from ui.duplicate_comparison_dialog import (
    DuplicateGroupComparisonDialog,
    DuplicateTemplateComparisonDialog,
    DuplicateResolution,
)
from utils.group_tasks import (
    GroupDiscoveryTask, GroupExportTask, GroupImportTask,
    TemplateExportTask, TemplateImportTask,
)
from utils.progress_dialog import ProgressDialog
from utils.settings_manager import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GroupManagementDialog
# ---------------------------------------------------------------------------

class GroupManagementDialog(QDialog):
    """
    Dialog for managing AD groups and templates.

    Works with or without AD credentials (offline mode).
    Accepts *initial_groups* and *initial_templates* so that state is
    preserved across multiple openings of the dialog.
    """

    def __init__(
        self,
        ad_server: str = "",
        ad_username: str = "",
        ad_password: str = "",
        base_dn: str = "",
        initial_groups: Optional[List[ADGroup]] = None,
        initial_templates: Optional[List[GroupTemplate]] = None,
        parent=None,
    ):
        super().__init__(parent)

        self.ad_server = ad_server
        self.ad_username = ad_username
        self.ad_password = ad_password
        self.base_dn = base_dn
        self.has_credentials = bool(ad_server and ad_username and ad_password)

        # Pre-populate groups from previous session if provided
        self.groups: List[ADGroup] = list(initial_groups) if initial_groups else []

        # Pre-populate template manager from previous session
        self.template_manager = GroupTemplateManager()
        if initial_templates:
            for tmpl in initial_templates:
                self.template_manager.add_template(tmpl)

        self.setWindowTitle(
            "Group Management" + (" (Offline Mode)" if not self.has_credentials else "")
        )
        self.setModal(False)
        self.setMinimumSize(950, 750)

        self._init_ui()
        logger.info(
            "GroupManagementDialog initialised (credentials: %s, "
            "groups: %d, templates: %d)",
            self.has_credentials,
            len(self.groups),
            len(self.template_manager.get_all_templates()),
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Header
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<h2>&#128193; Active Directory Group Management</h2>"))
        if self.has_credentials:
            status = QLabel("&#128994; <i>Connected</i>")
            status.setStyleSheet("color: #4CAF50;")
        else:
            status = QLabel("&#128308; <i>Offline Mode</i>")
            status.setStyleSheet("color: #FF9800;")
        header_row.addWidget(status)
        header_row.addStretch()
        layout.addLayout(header_row)

        if not self.has_credentials:
            banner = QLabel(
                "&#8505; <i>Running in offline mode. You can add groups manually"
                " but cannot verify them in AD. Provide AD credentials in the main"
                " window to enable full features.</i>"
            )
            banner.setWordWrap(True)
            banner.setStyleSheet(
                "background-color: #FFF3CD; padding: 8px; border: 1px solid #FFC107;"
            )
            layout.addWidget(banner)

        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._create_groups_tab(), "Groups")
        self.tab_widget.addTab(self._create_templates_tab(), "Templates")
        layout.addWidget(self.tab_widget)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        # Populate lists from pre-loaded data
        self.refresh_group_list()
        self.refresh_template_list()

    # ---- Groups tab -------------------------------------------------------

    def _create_groups_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel(
            "Manage the list of available groups. "
            + ("Discover groups from AD or add them manually."
               if self.has_credentials else "Add groups manually by DN.")
        ))

        content = QHBoxLayout()

        # Left: group list
        left = QGroupBox("Available Groups")
        ll = QVBoxLayout(left)
        self.group_list = QListWidget()
        self.group_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.group_list.itemSelectionChanged.connect(self._on_group_selection_changed)
        self.group_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.group_list.customContextMenuRequested.connect(self._show_group_context_menu)
        ll.addWidget(self.group_list)
        stats = QHBoxLayout()
        self.group_count_label = QLabel("Groups: 0")
        self.group_verified_label = QLabel("Verified: 0")
        stats.addWidget(self.group_count_label)
        stats.addWidget(self.group_verified_label)
        stats.addStretch()
        ll.addLayout(stats)
        content.addWidget(left, 2)

        # Right: details + operations
        right = QGroupBox("Group Details & Operations")
        rl = QVBoxLayout(right)
        self.group_details_text = QTextEdit()
        self.group_details_text.setReadOnly(True)
        self.group_details_text.setMaximumHeight(180)
        rl.addWidget(QLabel("<b>Selected Group Details:</b>"))
        rl.addWidget(self.group_details_text)

        ops = QGroupBox("Operations")
        ol = QVBoxLayout(ops)

        if self.has_credentials:
            ol.addWidget(QLabel("<b>Discover Groups from AD:</b>"))
            ou_row = QHBoxLayout()
            ou_row.addWidget(QLabel("OU DN:"))
            self.ou_dn_input = QLineEdit()
            self.ou_dn_input.setPlaceholderText("OU=Groups,DC=example,DC=com")
            ou_row.addWidget(self.ou_dn_input)
            ol.addLayout(ou_row)
            discover_btn = QPushButton("&#128269; Discover Groups")
            discover_btn.clicked.connect(self.discover_groups)
            ol.addWidget(discover_btn)
            ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Add Group Manually:</b>"))
        dn_row = QHBoxLayout()
        dn_row.addWidget(QLabel("Group DN:"))
        self.group_dn_input = QLineEdit()
        self.group_dn_input.setPlaceholderText(
            "CN=Students,OU=Groups,DC=example,DC=com"
        )
        dn_row.addWidget(self.group_dn_input)
        ol.addLayout(dn_row)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name (optional):"))
        self.group_name_input = QLineEdit()
        self.group_name_input.setPlaceholderText("Auto-extracted from DN")
        name_row.addWidget(self.group_name_input)
        ol.addLayout(name_row)
        add_btn = QPushButton("&#10133; Add Group")
        add_btn.clicked.connect(self.add_group_manually)
        ol.addWidget(add_btn)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Verification:</b>"))
        verify_btn = QPushButton("&#10003; Verify Selected in AD")
        verify_btn.clicked.connect(self.verify_selected_groups)
        if not self.has_credentials:
            verify_btn.setEnabled(False)
            verify_btn.setToolTip("AD credentials required")
        ol.addWidget(verify_btn)
        self.auto_update_templates_check = QCheckBox(
            "Auto-update templates after verification"
        )
        self.auto_update_templates_check.setChecked(True)
        ol.addWidget(self.auto_update_templates_check)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Group Operations:</b>"))
        grp_ops = QHBoxLayout()
        remove_btn = QPushButton("&#10060; Remove Selected")
        remove_btn.clicked.connect(self.remove_selected_groups)
        grp_ops.addWidget(remove_btn)
        clear_btn = QPushButton("&#128465; Clear All")
        clear_btn.clicked.connect(self.clear_all_groups)
        grp_ops.addWidget(clear_btn)
        ol.addLayout(grp_ops)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Export/Import:</b>"))
        ei_row = QHBoxLayout()
        exp_btn = QPushButton("&#128190; Export Groups...")
        exp_btn.clicked.connect(self.export_groups)
        ei_row.addWidget(exp_btn)
        imp_btn = QPushButton("&#128194; Import Groups...")
        imp_btn.clicked.connect(self.import_groups)
        ei_row.addWidget(imp_btn)
        ol.addLayout(ei_row)
        ol.addStretch()

        rl.addWidget(ops)
        content.addWidget(right, 1)
        layout.addLayout(content)
        return widget

    # ---- Templates tab ----------------------------------------------------

    def _create_templates_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(QLabel(
            "Templates allow you to quickly assign multiple groups to users."
        ))

        content = QHBoxLayout()

        left = QGroupBox("Templates")
        ll = QVBoxLayout(left)
        self.template_list = QListWidget()
        self.template_list.itemSelectionChanged.connect(
            self._on_template_selection_changed
        )
        self.template_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.template_list.customContextMenuRequested.connect(
            self._show_template_context_menu
        )
        ll.addWidget(self.template_list)
        self.template_count_label = QLabel("Templates: 0")
        ll.addWidget(self.template_count_label)
        content.addWidget(left, 2)

        right = QGroupBox("Template Details & Operations")
        rl = QVBoxLayout(right)
        self.template_details_text = QTextEdit()
        self.template_details_text.setReadOnly(True)
        self.template_details_text.setMaximumHeight(220)
        rl.addWidget(QLabel("<b>Selected Template:</b>"))
        rl.addWidget(self.template_details_text)

        ops = QGroupBox("Operations")
        ol = QVBoxLayout(ops)

        ol.addWidget(QLabel("<b>Create New Template:</b>"))
        create_btn = QPushButton("&#10133; Create from Selected Groups")
        create_btn.clicked.connect(self.create_template_from_groups)
        ol.addWidget(create_btn)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Verification:</b>"))
        verify_tmpl_btn = QPushButton("&#10003; Verify Selected Template")
        verify_tmpl_btn.clicked.connect(self.verify_selected_template)
        if not self.has_credentials:
            verify_tmpl_btn.setEnabled(False)
            verify_tmpl_btn.setToolTip("AD credentials required")
        ol.addWidget(verify_tmpl_btn)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Template Operations:</b>"))
        tmpl_ops = QHBoxLayout()
        rename_btn = QPushButton("&#9999; Rename")
        rename_btn.clicked.connect(self.rename_template)
        tmpl_ops.addWidget(rename_btn)
        copy_btn = QPushButton("&#128203; Copy")
        copy_btn.clicked.connect(self.copy_template)
        tmpl_ops.addWidget(copy_btn)
        delete_btn = QPushButton("&#10060; Delete")
        delete_btn.clicked.connect(self.delete_template)
        tmpl_ops.addWidget(delete_btn)
        ol.addLayout(tmpl_ops)
        ol.addSpacing(10)

        ol.addWidget(QLabel("<b>Export/Import:</b>"))
        ei_row = QHBoxLayout()
        exp_btn = QPushButton("&#128190; Export Templates...")
        exp_btn.clicked.connect(self.export_templates)
        ei_row.addWidget(exp_btn)
        imp_btn = QPushButton("&#128194; Import Templates...")
        imp_btn.clicked.connect(self.import_templates)
        ei_row.addWidget(imp_btn)
        ol.addLayout(ei_row)
        ol.addStretch()

        rl.addWidget(ops)
        content.addWidget(right, 1)
        layout.addLayout(content)
        return widget

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    def add_group_manually(self):
        """Add a group manually by DN without AD verification."""
        group_dn = self.group_dn_input.text().strip()
        if not group_dn:
            QMessageBox.warning(self, "Missing Input", "Please enter a group DN.")
            return
        if any(g.dn.lower() == group_dn.lower() for g in self.groups):
            QMessageBox.warning(self, "Duplicate", "This group is already in the list.")
            return
        group_name = self.group_name_input.text().strip() or self._extract_cn_from_dn(group_dn)
        group = ADGroup(
            name=group_name,
            dn=group_dn,
            description="Manually added — not verified",
            verification_status=VerificationStatus.NOT_VERIFIED,
        )
        self.groups.append(group)
        self.refresh_group_list()
        self.group_dn_input.clear()
        self.group_name_input.clear()
        logger.info("Manually added group: %s (%s)", group_name, group_dn)
        QMessageBox.information(
            self, "Group Added",
            f"Group '{group_name}' added to the list.\n\n"
            "Note: The group has not been verified in AD. "
            "Use the 'Verify' button to check if it exists.",
        )

    def _extract_cn_from_dn(self, dn: str) -> str:
        """Extract the CN component from a Distinguished Name."""
        try:
            for part in dn.split(","):
                part = part.strip()
                if part.upper().startswith("CN="):
                    return part[3:]
            parts = dn.split(",")
            return parts[0].strip() if parts else "Unknown Group"
        except Exception as exc:
            logger.warning("Failed to extract CN from DN '%s': %s", dn, exc)
            return "Unknown Group"

    def verify_selected_groups(self):
        """Verify selected groups in AD."""
        if not self.has_credentials:
            QMessageBox.warning(self, "No Credentials",
                                "AD credentials are required for verification.")
            return
        selected = self.group_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select groups to verify.")
            return
        groups = self._groups_from_items(selected)
        if not groups:
            QMessageBox.warning(self, "No Selection", "Please select groups to verify.")
            return
        self._verify_groups_in_ad(groups)
        if self.auto_update_templates_check.isChecked():
            self._update_templates_with_groups(groups)

    def _verify_groups_in_ad(self, groups_to_verify: List[ADGroup]):
        try:
            with ADClient(self.ad_server, self.ad_username, self.ad_password) as client:
                if not client.connection:
                    QMessageBox.critical(self, "Connection Failed",
                                         "Failed to connect to Active Directory.")
                    return
                service = ADGroupService(client)
                verified = found = 0
                for group in groups_to_verify:
                    try:
                        ad_group = service.get_group_by_dn(group.dn)
                        if ad_group:
                            group.name = ad_group.name
                            group.description = ad_group.description
                            group.group_type = ad_group.group_type
                            group.members_count = ad_group.members_count
                            group.mark_verified(exists=True)
                            found += 1
                        else:
                            group.mark_verified(exists=False)
                        verified += 1
                    except Exception:
                        logger.exception("Error verifying group %s", group.dn)
                        group.mark_verified(exists=False)
                self.refresh_group_list()
                QMessageBox.information(
                    self, "Verification Results",
                    f"<b>Verification Complete</b><br><br>"
                    f"Verified: {verified} group(s)<br>"
                    f"Found in AD: {found}<br>"
                    f"Not found: {verified - found}",
                )
        except Exception:
            logger.exception("Error during group verification")
            QMessageBox.critical(self, "Verification Error",
                                  "An unexpected error occurred during verification.")

    def _update_templates_with_groups(self, updated_groups: List[ADGroup]):
        by_dn = {g.dn.lower(): g for g in updated_groups}
        for template in self.template_manager.get_all_templates():
            changed = False
            # The previous version stopped at the first matching group
            # ("break"), so a template containing several verified groups kept
            # the stale data of all but one of them.
            for i, group in enumerate(template.groups):
                updated = by_dn.get(group.dn.lower())
                if updated is not None:
                    template.groups[i] = updated
                    changed = True
            if changed:
                template.mark_verified()
        self.refresh_template_list()

    def remove_selected_groups(self):
        selected = self.group_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select groups to remove.")
            return
        reply = QMessageBox.question(
            self, "Confirm Removal",
            f"Remove {len(selected)} selected group(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            to_remove = self._groups_from_items(selected)
            # Compare by identity: ADGroup.__eq__ compares DNs, so list.remove()
            # could drop a different object that happens to share the DN.
            self.groups = [g for g in self.groups
                           if not any(g is victim for victim in to_remove)]
            self.refresh_group_list()

    def clear_all_groups(self):
        if not self.groups:
            return
        reply = QMessageBox.question(
            self, "Confirm Clear",
            f"Remove all {len(self.groups)} groups from the list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.groups.clear()
            self.refresh_group_list()

    def _groups_from_items(self, items) -> List[ADGroup]:
        """
        Return the groups behind the given list items.

        Args:
            items: Selected ``QListWidgetItem`` objects.

        Returns:
            The corresponding :class:`~models.ADGroup` objects, skipping items
            that carry none.
        """
        groups = []
        for item in items:
            group = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(group, ADGroup):
                groups.append(group)
        return groups

    def _on_group_selection_changed(self):
        selected = self.group_list.selectedItems()
        groups = self._groups_from_items(selected)
        if groups:
            self._show_group_details(groups[0])
        else:
            self.group_details_text.clear()

    def _show_group_details(self, group: ADGroup):
        details = (
            f"<b>Name:</b> {group.name}<br>"
            f"<b>DN:</b> {group.dn}<br>"
            f"<b>Type:</b> {group.group_type or 'Unknown'}<br>"
            f"<b>Members:</b> {group.members_count}<br>"
            f"<b>Description:</b> {group.description or 'No description'}<br><br>"
            f"<b>Verification Status:</b> {group.get_status_icon()} {group.get_status_text()}"
        )
        if group.last_verified:
            details += f"<br><b>Last Verified:</b> {group.last_verified.strftime('%Y-%m-%d %H:%M:%S')}"
        self.group_details_text.setHtml(details)

    def _show_group_context_menu(self, position):
        menu = QMenu()
        if self.has_credentials:
            verify_action = QAction("&#10003; Verify in AD", self)
            verify_action.triggered.connect(self.verify_selected_groups)
            menu.addAction(verify_action)
            menu.addSeparator()
        remove_action = QAction("&#10060; Remove", self)
        remove_action.triggered.connect(self.remove_selected_groups)
        menu.addAction(remove_action)
        menu.exec(self.group_list.viewport().mapToGlobal(position))

    def refresh_group_list(self):
        """Rebuild the group list widget from self.groups."""
        self.group_list.clear()
        verified_count = sum(1 for g in self.groups if g.is_verified())
        for group in self.groups:
            display = f"{group.get_status_icon()} {group.name}"
            if group.group_type:
                display += f" ({group.group_type})"
            item = QListWidgetItem(display)
            # Store the object itself: mapping a list row back to an index in
            # self.groups breaks as soon as the two get out of sync, and the
            # consequences range from editing to *deleting* the wrong group.
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setToolTip(
                f"DN: {group.dn}\n"
                f"Status: {group.get_status_text()}\n"
                + (f"Last verified: {group.last_verified.strftime('%Y-%m-%d %H:%M:%S')}"
                   if group.last_verified else "Never verified")
            )
            if group.verification_status == VerificationStatus.VERIFIED_EXISTS:
                item.setForeground(QColor("#4CAF50"))
            elif group.verification_status == VerificationStatus.VERIFIED_NOT_FOUND:
                item.setForeground(QColor("#F44336"))
            elif group.verification_status == VerificationStatus.NOT_VERIFIED:
                item.setForeground(QColor("#9E9E9E"))
            self.group_list.addItem(item)
        self.group_count_label.setText(f"Groups: {len(self.groups)} ({verified_count} verified)")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_groups(self):
        if not hasattr(self, "ou_dn_input"):
            return
        ou_dn = self.ou_dn_input.text().strip()
        if not ou_dn:
            QMessageBox.warning(self, "Missing Input", "Please enter an OU DN.")
            return
        task = GroupDiscoveryTask(
            server=self.ad_server,
            username=self.ad_username,
            password=self.ad_password,
            ou_dn=ou_dn,
        )
        dlg = ProgressDialog(task, self)
        dlg.start_task()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            discovered = task.get_discovered_groups()
            if discovered:
                for g in discovered:
                    g.mark_verified(exists=True)
                self._show_group_selection_dialog(discovered)
            else:
                QMessageBox.information(self, "No Groups Found",
                                         f"No groups were found in {ou_dn}.")

    def _show_group_selection_dialog(self, discovered: List[ADGroup]):
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Groups to Add")
        dlg.setModal(True)
        dlg.setMinimumSize(600, 400)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"<b>Found {len(discovered)} groups. Select groups to add:</b>"))
        list_w = QListWidget()
        existing_dns = {g.dn.lower() for g in self.groups}
        for group in discovered:
            text = f"{group.name} — {group.dn}"
            if group.group_type:
                text += f" ({group.group_type})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, group)
            if group.dn.lower() in existing_dns:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setText(f"{text} (already added)")
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            list_w.addItem(item)
        layout.addWidget(list_w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            added = 0
            for i in range(list_w.count()):
                item = list_w.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    group = item.data(Qt.ItemDataRole.UserRole)
                    if group and group not in self.groups:
                        self.groups.append(group)
                        added += 1
            self.refresh_group_list()
            if added:
                QMessageBox.information(self, "Groups Added",
                                         f"Added {added} group(s) to the list.")

    # ------------------------------------------------------------------
    # Export / Import — Groups
    # ------------------------------------------------------------------

    def export_groups(self):
        if not self.groups:
            QMessageBox.warning(self, "No Groups", "No groups to export.")
            return
        dlg = ExportDialog("Export Groups", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        file_path, method, password = dlg.get_values()
        if not file_path.endswith(".usrx"):
            file_path += ".usrx"
        if Path(file_path).exists():
            if QMessageBox.question(
                self, "File Exists",
                f"File already exists:\n{file_path}\n\nOverwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        task = GroupExportTask(
            groups=self.groups, output_path=file_path,
            password=password, encryption_method=method,
        )
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

    def import_groups(self):
        dlg = ImportDialog("Import Groups", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # The method is None when the user chose auto-detection, which makes
        # the decryption helper read it from the file - the behaviour the
        # dialog used to hard-code.
        file_path, password, method = dlg.get_full_values()
        task = GroupImportTask(input_path=file_path, password=password,
                               encryption_method=method)
        progress = ProgressDialog(task, self)
        progress.start_task()
        if progress.exec() == QDialog.DialogCode.Accepted:
            imported = task.get_imported_groups()
            if imported:
                self.handle_imported_groups(imported)

    def handle_imported_groups(self, imported_groups: List[ADGroup]):
        """
        Handle imported groups:
        • No dialog if there are no duplicates (silent add).
        • Detailed per-item comparison dialog for each duplicate.
        • Summary dialog after all decisions are made.
        """
        existing_dns = {g.dn.lower() for g in self.groups}
        new_groups: List[ADGroup] = []
        duplicate_pairs: List[tuple] = []  # (existing, imported)

        for group in imported_groups:
            if group.dn.lower() in existing_dns:
                existing = next(
                    g for g in self.groups if g.dn.lower() == group.dn.lower()
                )
                duplicate_pairs.append((existing, group))
            else:
                new_groups.append(group)

        replaced = skipped = 0

        if duplicate_pairs:
            cmp_dlg = DuplicateGroupComparisonDialog(duplicate_pairs, self)
            if cmp_dlg.exec() != QDialog.DialogCode.Accepted:
                # User cancelled — add only non-duplicate groups
                self.groups.extend(new_groups)
                self.refresh_group_list()
                return
            resolutions: Dict[str, DuplicateResolution] = cmp_dlg.get_resolutions()
            for existing, imported in duplicate_pairs:
                if resolutions.get(existing.dn) == DuplicateResolution.REPLACE:
                    self.groups = [
                        g for g in self.groups if g.dn.lower() != existing.dn.lower()
                    ]
                    self.groups.append(imported)
                    replaced += 1
                else:
                    skipped += 1

        # Add genuinely new groups
        self.groups.extend(new_groups)
        self.refresh_group_list()

        # Summary
        if new_groups or replaced or skipped:
            parts = [f"Added {len(new_groups)} new group(s)."]
            if replaced:
                parts.append(f"Replaced {replaced} duplicate(s).")
            if skipped:
                parts.append(f"Skipped {skipped} duplicate(s).")
            QMessageBox.information(self, "Import Complete", "\n".join(parts))

    # ------------------------------------------------------------------
    # Export / Import — Templates
    # ------------------------------------------------------------------

    def export_templates(self):
        templates = self.template_manager.get_all_templates()
        if not templates:
            QMessageBox.warning(self, "No Templates", "No templates to export.")
            return
        dlg = ExportDialog("Export Templates", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        file_path, method, password = dlg.get_values()
        if not file_path.endswith(".usrx"):
            file_path += ".usrx"
        if Path(file_path).exists():
            if QMessageBox.question(
                self, "File Exists",
                f"File already exists:\n{file_path}\n\nOverwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        task = TemplateExportTask(
            templates=list(templates),
            output_path=file_path,
            password=password,
            encryption_method=method,
        )
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

    def import_templates(self):
        dlg = ImportDialog("Import Templates", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        file_path, password, method = dlg.get_full_values()
        task = TemplateImportTask(input_path=file_path, password=password,
                                  encryption_method=method)
        progress = ProgressDialog(task, self)
        progress.start_task()
        if progress.exec() == QDialog.DialogCode.Accepted:
            imported = task.get_imported_templates()
            if imported:
                self.handle_imported_templates(imported)

    def handle_imported_templates(self, imported_templates: List[GroupTemplate]):
        """
        Handle imported templates:
        • No dialog if there are no duplicates (silent add).
        • Detailed per-item comparison dialog for each duplicate.
        • Summary dialog after all decisions are made.
        """
        if not imported_templates:
            return

        existing_names = {t.name for t in self.template_manager.get_all_templates()}
        new_templates: List[GroupTemplate] = []
        duplicate_pairs: List[tuple] = []  # (existing, imported)

        for tmpl in imported_templates:
            if tmpl.name in existing_names:
                existing = self.template_manager.get_template(tmpl.name)
                if existing:
                    duplicate_pairs.append((existing, tmpl))
            else:
                new_templates.append(tmpl)

        replaced = skipped = 0

        if duplicate_pairs:
            cmp_dlg = DuplicateTemplateComparisonDialog(duplicate_pairs, self)
            if cmp_dlg.exec() != QDialog.DialogCode.Accepted:
                for tmpl in new_templates:
                    self.template_manager.add_template(tmpl)
                self.refresh_template_list()
                return
            resolutions: Dict[str, DuplicateResolution] = cmp_dlg.get_resolutions()
            for existing, imported in duplicate_pairs:
                if resolutions.get(existing.name) == DuplicateResolution.REPLACE:
                    self.template_manager.remove_template(existing.name)
                    self.template_manager.add_template(imported)
                    replaced += 1
                else:
                    skipped += 1

        for tmpl in new_templates:
            self.template_manager.add_template(tmpl)

        self.refresh_template_list()

        if new_templates or replaced or skipped:
            parts = [f"Added {len(new_templates)} new template(s)."]
            if replaced:
                parts.append(f"Replaced {replaced} duplicate(s).")
            if skipped:
                parts.append(f"Skipped {skipped} duplicate(s).")
            QMessageBox.information(self, "Import Complete", "\n".join(parts))

    # ------------------------------------------------------------------
    # Template operations
    # ------------------------------------------------------------------

    def verify_selected_template(self):
        if not self.has_credentials:
            QMessageBox.warning(self, "No Credentials",
                                "AD credentials are required for verification.")
            return
        selected = self.template_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a template.")
            return
        template = selected[0].data(Qt.ItemDataRole.UserRole)
        if not template:
            QMessageBox.warning(self, "Invalid Selection",
                                "Selected item has no template data.")
            return
        self._verify_groups_in_ad(template.groups)
        template.mark_verified()
        self.refresh_template_list()
        self._show_template_details(template)

    def create_template_from_groups(self):
        """
        Create a new template from the groups in self.groups.

        The dialog has an inline validation status area (green/red messages)
        that updates on every change.  The OK button is disabled until all
        required conditions are met; if the user somehow triggers accept with
        invalid data, an informational message is shown.
        """
        if not self.groups:
            QMessageBox.warning(
                self, "No Groups",
                "Please add some groups first before creating a template.",
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Create Template")
        dlg.setModal(True)
        dlg.setMinimumSize(620, 560)

        layout = QVBoxLayout(dlg)

        # Template name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Template Name: <span style='color:#ff6b6b;'>*</span>"))
        name_input = QLineEdit()
        name_input.setPlaceholderText("Enter template name (required)")
        name_row.addWidget(name_input)
        layout.addLayout(name_row)

        # Description
        layout.addWidget(QLabel("Description (optional):"))
        desc_input = QTextEdit()
        desc_input.setMaximumHeight(70)
        desc_input.setPlaceholderText("Enter template description")
        layout.addWidget(desc_input)

        # Group selection
        layout.addWidget(QLabel(
            "<b>Select groups to include:</b> <span style='color:#ff6b6b;'>*</span>"
        ))
        group_list_w = QListWidget()
        for group in self.groups:
            item = QListWidgetItem(f"{group.name} — {group.dn}")
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            group_list_w.addItem(item)
        layout.addWidget(group_list_w)

        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(
            lambda: [
                group_list_w.item(i).setCheckState(Qt.CheckState.Checked)
                for i in range(group_list_w.count())
            ]
        )
        layout.addWidget(sel_all_btn)

        # ---- Inline validation status area ----
        layout.addWidget(QLabel("<b>Validation:</b>"))
        validation_display = QTextEdit()
        validation_display.setReadOnly(True)
        validation_display.setMaximumHeight(72)
        layout.addWidget(validation_display)

        # Dialog buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(btn_box)

        # ---- Validation logic ----
        def _validate_form() -> bool:
            """
            Re-check form state and update the validation display.
            Returns True if all conditions are satisfied.
            """
            issues: List[str] = []
            template_name = name_input.text().strip()

            if not template_name:
                issues.append("Template name is required.")
            elif self.template_manager.template_exists(template_name):
                issues.append(
                    f"A template named \"{template_name}\" already exists."
                )

            checked_count = sum(
                1 for i in range(group_list_w.count())
                if group_list_w.item(i).checkState() == Qt.CheckState.Checked
            )
            if checked_count == 0:
                issues.append("At least one group must be selected.")

            validation_display.clear()
            if issues:
                html = "<br>".join(
                    f'<span style="color:#ff6b6b;">&#10060; {iss}</span>'
                    for iss in issues
                )
                validation_display.setHtml(html)
                ok_btn.setEnabled(False)
                return False
            else:
                validation_display.setHtml(
                    '<span style="color:#4CAF50;">&#10003; Ready to create template</span>'
                )
                ok_btn.setEnabled(True)
                return True

        # Wire change signals to the validator
        name_input.textChanged.connect(lambda _: _validate_form())
        group_list_w.itemChanged.connect(lambda _: _validate_form())

        # Initial validation pass
        ok_btn.setEnabled(False)
        _validate_form()

        # The OK button is connected via the QDialogButtonBox; we intercept
        # acceptance to re-validate defensively before allowing the dialog to
        # close.
        def _on_accept():
            if not _validate_form():
                template_name = name_input.text().strip()
                msg_parts = []
                if not template_name:
                    msg_parts.append("• Template name is required.")
                elif self.template_manager.template_exists(template_name):
                    msg_parts.append(
                        f"• A template named \"{template_name}\" already exists."
                    )
                checked = sum(
                    1 for i in range(group_list_w.count())
                    if group_list_w.item(i).checkState() == Qt.CheckState.Checked
                )
                if checked == 0:
                    msg_parts.append("• At least one group must be selected.")
                QMessageBox.information(
                    dlg, "Required Fields Missing",
                    "Please fix the following before continuing:\n\n"
                    + "\n".join(msg_parts),
                )
                return
            dlg.accept()

        btn_box.accepted.connect(_on_accept)
        btn_box.rejected.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        template_name = name_input.text().strip()
        selected_groups: List[ADGroup] = [
            group_list_w.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(group_list_w.count())
            if group_list_w.item(i).checkState() == Qt.CheckState.Checked
        ]

        template = GroupTemplate(
            name=template_name,
            description=desc_input.toPlainText().strip(),
            groups=selected_groups,
            created_date=datetime.now(),
            modified_date=datetime.now(),
        )

        if self.template_manager.add_template(template):
            self.refresh_template_list()
            QMessageBox.information(
                self, "Template Created",
                f"Created template '{template_name}' with {len(selected_groups)} group(s).",
            )
        else:
            QMessageBox.warning(self, "Failed", "Failed to create template.")

    def rename_template(self):
        """Rename the selected template."""
        selected = self.template_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a template.")
            return
        # Retrieve template from UserRole — never rely on text parsing
        template: Optional[GroupTemplate] = selected[0].data(Qt.ItemDataRole.UserRole)
        if template is None:
            QMessageBox.warning(self, "Invalid Selection",
                                "Selected item has no template data.")
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename Template",
            f"Enter new name for '{template.name}':",
            QLineEdit.EchoMode.Normal,
            template.name,
        )
        if ok and new_name:
            new_name = new_name.strip()
            if new_name == template.name:
                return
            if not new_name:
                QMessageBox.warning(self, "Invalid Name", "Template name cannot be empty.")
                return
            if self.template_manager.rename_template(template.name, new_name):
                self.refresh_template_list()
                QMessageBox.information(self, "Success", f"Renamed template to '{new_name}'.")
            else:
                QMessageBox.warning(
                    self, "Failed",
                    f"Failed to rename template. Name '{new_name}' may already exist.",
                )

    def copy_template(self):
        """Copy the selected template."""
        selected = self.template_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a template.")
            return
        template: Optional[GroupTemplate] = selected[0].data(Qt.ItemDataRole.UserRole)
        if template is None:
            QMessageBox.warning(self, "Invalid Selection",
                                "Selected item has no template data.")
            return

        new_name, ok = QInputDialog.getText(
            self, "Copy Template",
            f"Enter name for copy of '{template.name}':",
            QLineEdit.EchoMode.Normal,
            f"{template.name} (Copy)",
        )
        if ok and new_name:
            new_name = new_name.strip()
            if not new_name:
                QMessageBox.warning(self, "Invalid Name", "Template name cannot be empty.")
                return
            copied = self.template_manager.copy_template(template.name, new_name)
            if copied:
                self.refresh_template_list()
                QMessageBox.information(self, "Success", f"Created copy '{new_name}'.")
            else:
                QMessageBox.warning(
                    self, "Failed",
                    f"Failed to copy template. Name '{new_name}' may already exist.",
                )

    def delete_template(self):
        """Delete the selected template."""
        selected = self.template_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a template.")
            return
        template: Optional[GroupTemplate] = selected[0].data(Qt.ItemDataRole.UserRole)
        if template is None:
            QMessageBox.warning(self, "Invalid Selection",
                                "Selected item has no template data.")
            return

        reply = QMessageBox.question(
            self, "Confirm Deletion",
            f"Delete template '{template.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.template_manager.remove_template(template.name):
                self.refresh_template_list()
                QMessageBox.information(self, "Success", "Template deleted.")
            else:
                QMessageBox.warning(self, "Failed", "Failed to delete template.")

    def _on_template_selection_changed(self):
        selected = self.template_list.selectedItems()
        if selected:
            template = selected[0].data(Qt.ItemDataRole.UserRole)
            if template:
                self._show_template_details(template)
        else:
            self.template_details_text.clear()

    def _show_template_details(self, template: GroupTemplate):
        def fmt_date(dt) -> str:
            return dt.strftime("%Y-%m-%d %H:%M") if dt else "Unknown"

        details = (
            f"<b>Name:</b> {template.name}<br>"
            f"<b>Description:</b> {template.description or 'No description'}<br>"
            f"<b>Groups:</b> {len(template.groups)}<br>"
            f"<b>Created:</b> {fmt_date(template.created_date)}<br>"
            f"<b>Modified:</b> {fmt_date(template.modified_date)}<br>"
        )
        if template.last_verified:
            details += f"<b>Last Verified:</b> {template.last_verified.strftime('%Y-%m-%d %H:%M:%S')}<br>"
        details += f"<b>Verification:</b> {template.get_verification_summary()}<br><br>"
        details += "<b>Groups in Template:</b><br>"
        for group in template.groups:
            details += f"{group.get_status_icon()} {group.name}<br>"
        self.template_details_text.setHtml(details)

    def _show_template_context_menu(self, position):
        menu = QMenu()
        rename_action = QAction("&#9999; Rename", self)
        rename_action.triggered.connect(self.rename_template)
        menu.addAction(rename_action)
        copy_action = QAction("&#128203; Copy", self)
        copy_action.triggered.connect(self.copy_template)
        menu.addAction(copy_action)
        menu.addSeparator()
        delete_action = QAction("&#10060; Delete", self)
        delete_action.triggered.connect(self.delete_template)
        menu.addAction(delete_action)
        menu.exec(self.template_list.viewport().mapToGlobal(position))

    def refresh_template_list(self):
        """
        Rebuild the template list widget.

        Each QListWidgetItem stores the corresponding GroupTemplate object
        via Qt.ItemDataRole.UserRole so that rename / copy / delete can
        retrieve it without fragile text parsing.
        """
        self.template_list.clear()
        templates = self.template_manager.get_all_templates()

        for template in templates:
            verified_count = template.get_verified_count()
            existing_count = template.get_existing_count()
            total = len(template.groups)

            display = f"{template.name} ({total} group{'s' if total != 1 else ''}"
            if total > 0:
                if verified_count == 0:
                    display += ", &#9888; unverified"
                elif verified_count == total:
                    if existing_count == verified_count:
                        display += ", &#10003; all verified"
                    else:
                        display += f", &#9888; {existing_count}/{verified_count} exist"
                else:
                    display += f", &#9888; {verified_count}/{total} verified"
            display += ")"

            item = QListWidgetItem(display)
            # Store the template object so operations can retrieve it reliably
            item.setData(Qt.ItemDataRole.UserRole, template)
            item.setToolTip(
                f"{template.description or 'No description'}\n"
                f"{template.get_verification_summary()}\n"
                + (f"Last verified: {template.last_verified.strftime('%Y-%m-%d %H:%M:%S')}"
                   if template.last_verified else "Not verified")
            )
            self.template_list.addItem(item)

        self.template_count_label.setText(f"Templates: {len(templates)}")

    # ------------------------------------------------------------------
    # Public accessors (called by ad_management.py after dialog closes)
    # ------------------------------------------------------------------

    def get_groups(self) -> List[ADGroup]:
        """Return a copy of the current group list."""
        return self.groups.copy()

    def get_templates(self) -> List[GroupTemplate]:
        """Return a copy of the current template list."""
        return self.template_manager.get_all_templates()


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def show_group_management_dialog(
    ad_server: str = "",
    ad_username: str = "",
    ad_password: str = "",
    base_dn: str = "",
    initial_groups: Optional[List[ADGroup]] = None,
    initial_templates: Optional[List[GroupTemplate]] = None,
    parent=None,
) -> Optional[GroupManagementDialog]:
    """
    Show the group management dialog with an optional no-credentials warning.

    Returns the :class:`GroupManagementDialog` instance so the caller can
    retrieve updated groups/templates after the dialog closes, or *None* if
    the user cancelled the warning dialog.
    """
    has_credentials = bool(ad_server and ad_username and ad_password)

    if not has_credentials:
        settings = get_settings()
        if settings.get_bool("show_group_management_warning", True, category="general"):
            warning = NoCredentialsWarningDialog(parent)
            if warning.exec() != QDialog.DialogCode.Accepted:
                return None

    return GroupManagementDialog(
        ad_server=ad_server,
        ad_username=ad_username,
        ad_password=ad_password,
        base_dn=base_dn,
        initial_groups=initial_groups,
        initial_templates=initial_templates,
        parent=parent,
    )
