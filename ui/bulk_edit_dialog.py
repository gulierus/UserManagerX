"""
Bulk Edit Dialog
Dialog for bulk editing multiple persons at once
Extended with Group Memberships and Home Directory support
"""

import logging
from typing import List
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QCheckBox, QGroupBox, QListWidget,
    QListWidgetItem, QMessageBox, QInputDialog, QDialogButtonBox,
    QRadioButton, QButtonGroup, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt

from models import Person, ADGroup, GroupTemplate
from utils.ad_utils import generate_password
from utils.home_directory_utils import HomeDirectoryPathGenerator, PlaceholderError, HomeDirectoryTemplateManager

logger = logging.getLogger(__name__)


class BulkEditDialog(QDialog):
    """Dialog for bulk editing person properties"""
    
    def __init__(self, selected_persons: List[Person],
                 available_groups: List[ADGroup] = None,
                 available_templates: List[GroupTemplate] = None,
                 parent=None):
        """
        Initialize bulk edit dialog
        
        Args:
            selected_persons: List of persons to edit
            available_groups: Available groups for selection
            available_templates: Available group templates
            parent: Parent widget
        """
        super().__init__(parent)
        self.selected_persons = selected_persons
        self.available_groups = available_groups or []
        self.available_templates = available_templates or []
        self.changes = {}
        self.selected_groups = []
        
        self.setWindowTitle(f"Bulk Edit - {len(selected_persons)} persons selected")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setMinimumHeight(400)
        self.resize(650, 600)
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        # Outer layout holds scroll area + buttons
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # Scroll area for main content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        layout.setContentsMargins(10, 10, 10, 10)

        scroll.setWidget(scroll_widget)
        outer_layout.addWidget(scroll, stretch=1)
        
        # Info header
        info = QLabel(
            f"<b>Bulk edit {len(self.selected_persons)} selected persons</b><br>"
            "Select which properties to change. Changes will be applied to all selected persons."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # === ACCOUNT STATUS ===
        status_group = QGroupBox("Account Status")
        status_layout = QVBoxLayout(status_group)
        
        self.change_enabled = QCheckBox("Change account enabled status")
        status_layout.addWidget(self.change_enabled)
        
        self.enabled_value = QCheckBox("Account Enabled")
        self.enabled_value.setEnabled(False)
        self.change_enabled.toggled.connect(self.enabled_value.setEnabled)
        status_layout.addWidget(self.enabled_value)
        
        layout.addWidget(status_group)
        
        # === PASSWORD POLICY ===
        policy_group = QGroupBox("Password Policy")
        policy_layout = QVBoxLayout(policy_group)
        
        self.change_must_change = QCheckBox("Change 'must change password at next logon'")
        policy_layout.addWidget(self.change_must_change)
        
        self.must_change_value = QCheckBox("User must change password at next logon")
        self.must_change_value.setEnabled(False)
        self.change_must_change.toggled.connect(self.must_change_value.setEnabled)
        policy_layout.addWidget(self.must_change_value)
        
        policy_layout.addSpacing(5)
        
        self.change_cannot_change = QCheckBox("Change 'user cannot change password'")
        policy_layout.addWidget(self.change_cannot_change)
        
        self.cannot_change_value = QCheckBox("User cannot change password")
        self.cannot_change_value.setEnabled(False)
        self.change_cannot_change.toggled.connect(self.cannot_change_value.setEnabled)
        policy_layout.addWidget(self.cannot_change_value)
        
        policy_layout.addSpacing(5)
        
        self.change_never_expires = QCheckBox("Change 'password never expires'")
        policy_layout.addWidget(self.change_never_expires)
        
        self.never_expires_value = QCheckBox("Password never expires")
        self.never_expires_value.setEnabled(False)
        self.change_never_expires.toggled.connect(self.never_expires_value.setEnabled)
        policy_layout.addWidget(self.never_expires_value)
        
        layout.addWidget(policy_group)
        
        # === CREDENTIALS ===
        cred_group = QGroupBox("Credentials")
        cred_layout = QVBoxLayout(cred_group)
        
        self.change_password = QCheckBox("Generate new passwords for all selected persons")
        cred_layout.addWidget(self.change_password)
        
        warning = QLabel(
            "⚠️ <i>Warning: This will generate new random passwords for all selected persons.</i>"
        )
        warning.setStyleSheet("color: #ff6b6b; font-size: 10px;")
        warning.setWordWrap(True)
        cred_layout.addWidget(warning)
        
        layout.addWidget(cred_group)
        
        # === GROUP MEMBERSHIPS ===
        groups_group = QGroupBox("Group Memberships")
        groups_layout = QVBoxLayout(groups_group)
        
        self.change_groups = QCheckBox("Modify group memberships")
        groups_layout.addWidget(self.change_groups)
        
        # Group selection method
        method_layout = QVBoxLayout()
        method_layout.setContentsMargins(20, 0, 0, 0)
        
        self.group_method_group = QButtonGroup()
        
        self.select_groups_radio = QRadioButton("Select specific groups")
        self.select_groups_radio.setEnabled(False)
        self.group_method_group.addButton(self.select_groups_radio)
        method_layout.addWidget(self.select_groups_radio)
        
        self.select_groups_btn = QPushButton("Select Groups...")
        self.select_groups_btn.setEnabled(False)
        self.select_groups_btn.clicked.connect(self.select_groups_dialog)
        method_layout.addWidget(self.select_groups_btn)
        
        method_layout.addSpacing(5)
        
        self.apply_template_radio = QRadioButton("Apply template")
        self.apply_template_radio.setEnabled(False)
        self.group_method_group.addButton(self.apply_template_radio)
        method_layout.addWidget(self.apply_template_radio)
        
        self.template_combo = QComboBox()
        self.template_combo.setEnabled(False)
        self.template_combo.addItem("-- Select Template --", None)
        for template in self.available_templates:
            self.template_combo.addItem(
                f"{template.name} ({len(template.groups)} groups)",
                template
            )
        method_layout.addWidget(self.template_combo)
        
        groups_layout.addLayout(method_layout)
        
        # Action selection
        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(20, 0, 0, 0)
        action_layout.addWidget(QLabel("Action:"))
        self.group_action_combo = QComboBox()
        self.group_action_combo.setEnabled(False)
        self.group_action_combo.addItem("Add to groups", "add")
        self.group_action_combo.addItem("Remove from groups", "remove")
        self.group_action_combo.addItem("Replace all groups", "replace")
        action_layout.addWidget(self.group_action_combo)
        groups_layout.addLayout(action_layout)
        
        # Connect enable/disable logic
        self.change_groups.toggled.connect(self.on_change_groups_toggled)
        self.select_groups_radio.toggled.connect(self.on_group_method_changed)
        self.apply_template_radio.toggled.connect(self.on_group_method_changed)
        
        layout.addWidget(groups_group)
        
        # === HOME DIRECTORY ===
        home_group = QGroupBox("Home Directory")
        home_layout = QVBoxLayout(home_group)
        
        self.change_home = QCheckBox("Set home directory from template")
        home_layout.addWidget(self.change_home)
        
        # Template selection
        template_layout = QHBoxLayout()
        template_layout.setContentsMargins(20, 0, 0, 0)
        template_layout.addWidget(QLabel("Template:"))
        
        self.home_template_combo = QComboBox()
        self.home_template_combo.setEnabled(False)
        self.home_template_combo.addItem("-- Select Template --", None)
        
        # Load home directory templates with tooltips showing path
        template_manager = HomeDirectoryTemplateManager()
        for name, template_path in template_manager.get_all_templates().items():
            self.home_template_combo.addItem(name, template_path)
            idx = self.home_template_combo.count() - 1
            self.home_template_combo.setItemData(
                idx,
                f"<b>{name}</b><br><code>{template_path}</code>",
                Qt.ItemDataRole.ToolTipRole
            )

        self.home_template_combo.currentIndexChanged.connect(self._update_home_template_preview)
        self.home_template_combo.setToolTip("Select a template. Hover over items to see the path.")

        template_layout.addWidget(self.home_template_combo, 1)

        # Template preview label
        self.home_template_preview = QLabel()
        self.home_template_preview.setStyleSheet(
            "color: #aaa; font-size: 9px; font-style: italic; padding: 2px 0;"
        )
        self.home_template_preview.setWordWrap(True)
        home_layout.addLayout(template_layout)

        home_layout.addWidget(self.home_template_preview)
        
        # Drive letter
        drive_layout = QHBoxLayout()
        drive_layout.setContentsMargins(20, 0, 0, 0)
        drive_layout.addWidget(QLabel("Drive letter (optional):"))
        self.home_drive_input = QLineEdit()
        self.home_drive_input.setEnabled(False)
        self.home_drive_input.setPlaceholderText("H:")
        self.home_drive_input.setMaximumWidth(60)
        drive_layout.addWidget(self.home_drive_input)
        drive_layout.addStretch()
        home_layout.addLayout(drive_layout)
        
        # Connect enable/disable
        self.change_home.toggled.connect(self.home_template_combo.setEnabled)
        self.change_home.toggled.connect(self.home_drive_input.setEnabled)
        
        # Info
        home_info = QLabel(
            "<i>Each person will have a unique path generated based on their information</i>"
        )
        home_info.setStyleSheet("color: #888; font-size: 10px;")
        home_info.setWordWrap(True)
        home_layout.addWidget(home_info)
        
        layout.addWidget(home_group)
        
        # === SUMMARY ===
        summary_group = QGroupBox("Summary")
        summary_layout = QVBoxLayout(summary_group)
        
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        summary_layout.addWidget(self.summary_label)
        
        layout.addWidget(summary_group)
        
        # Update summary when changes are made
        for checkbox in [self.change_enabled, self.change_must_change, 
                        self.change_cannot_change, self.change_never_expires,
                        self.change_password, self.change_groups, self.change_home]:
            checkbox.toggled.connect(self.update_summary)
        
        self.group_action_combo.currentIndexChanged.connect(self.update_summary)
        self.update_summary()
        
        # === DIALOG BUTTONS ===
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_changes)
        button_box.rejected.connect(self.reject)
        outer_layout.addWidget(button_box)
    
    def on_change_groups_toggled(self, checked):
        """Handle group change checkbox toggle"""
        self.select_groups_radio.setEnabled(checked)
        self.apply_template_radio.setEnabled(checked)
        self.group_action_combo.setEnabled(checked)
        
        if checked and not self.select_groups_radio.isChecked() and not self.apply_template_radio.isChecked():
            self.select_groups_radio.setChecked(True)
    
    def on_group_method_changed(self):
        """Handle group method selection change"""
        self.select_groups_btn.setEnabled(self.select_groups_radio.isChecked())
        self.template_combo.setEnabled(self.apply_template_radio.isChecked())
        self.update_summary()
    
    def _update_home_template_preview(self, index):
        """Update the template preview label when a template is selected"""
        template_path = self.home_template_combo.currentData()
        if template_path and hasattr(self, 'home_template_preview'):
            self.home_template_preview.setText(f"Path: {template_path}")
        elif hasattr(self, 'home_template_preview'):
            self.home_template_preview.setText("")

    def select_groups_dialog(self):
        """Show dialog to select groups"""
        if not self.available_groups:
            QMessageBox.information(
                self, "No Groups",
                "No groups available. Please use Group Management to add groups first."
            )
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Groups")
        dialog.setModal(True)
        dialog.setMinimumSize(500, 400)
        
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("<b>Select groups:</b>"))
        
        # List with checkboxes
        list_widget = QListWidget()
        
        # Pre-select previously selected groups
        selected_dns = set(g.dn.lower() for g in self.selected_groups)
        
        for group in self.available_groups:
            item = QListWidgetItem(f"{group.name} - {group.dn}")
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            
            if group.dn.lower() in selected_dns:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            
            list_widget.addItem(item)
        
        layout.addWidget(list_widget)
        
        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Get selected groups
            self.selected_groups = []
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    group = item.data(Qt.ItemDataRole.UserRole)
                    self.selected_groups.append(group)
            
            self.update_summary()
    
    def update_summary(self):
        """Update summary of changes"""
        changes = []
        
        if self.change_enabled.isChecked():
            status = "enabled" if self.enabled_value.isChecked() else "disabled"
            changes.append(f"Account status: {status}")
        
        if self.change_must_change.isChecked():
            changes.append(f"Must change password: {self.must_change_value.isChecked()}")
        
        if self.change_cannot_change.isChecked():
            changes.append(f"Cannot change password: {self.cannot_change_value.isChecked()}")
        
        if self.change_never_expires.isChecked():
            changes.append(f"Password never expires: {self.never_expires_value.isChecked()}")
        
        if self.change_password.isChecked():
            changes.append("Generate new passwords")
        
        if self.change_groups.isChecked():
            action = self.group_action_combo.currentData()
            
            if self.select_groups_radio.isChecked():
                group_count = len(self.selected_groups)
                changes.append(f"{action.title()} {group_count} selected group(s)")
            elif self.apply_template_radio.isChecked():
                template = self.template_combo.currentData()
                if template:
                    changes.append(f"{action.title()} groups from template '{template.name}'")
                else:
                    changes.append(f"{action.title()} groups from template (not selected)")
        
        if self.change_home.isChecked():
            template = self.home_template_combo.currentData()
            if template:
                drive = self.home_drive_input.text().strip()
                drive_text = f" (Drive: {drive})" if drive else ""
                changes.append(f"Set home directory from template{drive_text}")
            else:
                changes.append("Set home directory (template not selected)")
        
        if changes:
            summary = f"<b>Changes to apply to {len(self.selected_persons)} persons:</b><br>"
            summary += "<br>".join(f"• {c}" for c in changes)
        else:
            summary = "<i>No changes selected</i>"
        
        self.summary_label.setText(summary)
    
    def accept_changes(self):
        """Collect changes and accept"""
        if not any([self.change_enabled.isChecked(), 
                   self.change_must_change.isChecked(),
                   self.change_cannot_change.isChecked(),
                   self.change_never_expires.isChecked(),
                   self.change_password.isChecked(),
                   self.change_groups.isChecked(),
                   self.change_home.isChecked()]):
            QMessageBox.warning(self, "No Changes", "No changes selected")
            return
        
        # Validate group changes
        if self.change_groups.isChecked():
            if self.select_groups_radio.isChecked() and not self.selected_groups:
                QMessageBox.warning(self, "No Groups", "Please select at least one group")
                return
            
            if self.apply_template_radio.isChecked():
                template = self.template_combo.currentData()
                if not template:
                    QMessageBox.warning(self, "No Template", "Please select a template")
                    return
        
        # Validate home directory changes
        if self.change_home.isChecked():
            template = self.home_template_combo.currentData()
            if not template:
                QMessageBox.warning(self, "No Template", 
                                  "Please select a home directory template")
                return
        
        # Confirm
        reply = QMessageBox.question(
            self,
            "Confirm Bulk Edit",
            f"Apply these changes to {len(self.selected_persons)} persons?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.No:
            return
        
        # Collect changes
        if self.change_enabled.isChecked():
            self.changes['account_enabled'] = self.enabled_value.isChecked()
        
        if self.change_must_change.isChecked():
            self.changes['password_must_change'] = self.must_change_value.isChecked()
        
        if self.change_cannot_change.isChecked():
            self.changes['password_cannot_change'] = self.cannot_change_value.isChecked()
        
        if self.change_never_expires.isChecked():
            self.changes['password_never_expires'] = self.never_expires_value.isChecked()
        
        if self.change_password.isChecked():
            self.changes['generate_password'] = True
        
        # Apply group changes
        if self.change_groups.isChecked():
            action = self.group_action_combo.currentData()
            
            if self.select_groups_radio.isChecked():
                groups = self.selected_groups
            else:  # Template
                template = self.template_combo.currentData()
                groups = template.groups if template else []
            
            self.changes['group_action'] = action
            self.changes['groups'] = groups
        
        # Apply home directory changes
        if self.change_home.isChecked():
            self.changes['home_directory_template'] = self.home_template_combo.currentData()
            self.changes['home_drive'] = self.home_drive_input.text().strip() or None
        
        # Apply changes to all persons
        failed_count = 0
        
        for person in self.selected_persons:
            try:
                # Simple property changes
                for key, value in self.changes.items():
                    if key == 'generate_password':
                        # A misconfigured password format must not abort the
                        # whole bulk edit - it is counted as a failure instead.
                        person.ad_password = generate_password()
                    elif key == 'group_action':
                        # Handled separately below
                        pass
                    elif key == 'groups':
                        # Handled separately below
                        pass
                    elif key == 'home_directory_template':
                        # Handled separately below
                        pass
                    elif key == 'home_drive':
                        # Handled separately below
                        pass
                    elif hasattr(person, key):
                        setattr(person, key, value)
                
                # Handle group changes
                if 'group_action' in self.changes:
                    action = self.changes['group_action']
                    groups = self.changes['groups']
                    
                    if action == 'add':
                        # Add groups
                        for group in groups:
                            if not person.is_in_group(group):
                                person.add_to_group(group)
                    elif action == 'remove':
                        # Remove groups
                        for group in groups:
                            if person.is_in_group(group):
                                person.remove_from_group(group)
                    elif action == 'replace':
                        # Replace all groups
                        person.group_memberships = groups.copy()
                
                # Handle home directory
                if 'home_directory_template' in self.changes:
                    template = self.changes['home_directory_template']
                    drive = self.changes.get('home_drive')
                    
                    try:
                        generator = HomeDirectoryPathGenerator()
                        path = generator.generate_path(template, person)
                        person.home_directory = path
                        person.home_drive = drive
                    except PlaceholderError as e:
                        logger.error(f"Failed to generate home path for {person}: {e}")
                        failed_count += 1
            
            except Exception as e:
                logger.exception(f"Error applying changes to {person}")
                failed_count += 1
        
        # Show result
        success_count = len(self.selected_persons) - failed_count
        
        if failed_count > 0:
            QMessageBox.warning(
                self, "Partial Success",
                f"Applied changes to {success_count} persons.\n"
                f"Failed for {failed_count} persons. Check logs for details."
            )
        else:
            QMessageBox.information(
                self, "Success",
                f"Successfully applied changes to {success_count} persons"
            )
        
        self.accept()
