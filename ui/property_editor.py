"""
Property Editor Dialog for editing person properties
VERSION 5 - Added Group Memberships and Home Directory support
"""

import logging
from typing import List, Set
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFormLayout, QGroupBox, QMessageBox,
    QCheckBox, QDialogButtonBox, QTextEdit, QListWidget,
    QListWidgetItem, QComboBox, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt

from models import Person, ADGroup, GroupTemplate
from services.ad_group_service import GroupTemplateManager
from utils.ad_utils import generate_username, generate_password, generate_display_name
from utils.home_directory_utils import HomeDirectoryPathGenerator, PlaceholderError, HomeDirectoryTemplateManager
from services.ad_validator import ADValidator

logger = logging.getLogger(__name__)


class PropertyEditorDialog(QDialog):
    """Dialog for editing all person properties including groups and home directory"""
    
    def __init__(self, person: Person, existing_usernames: set, 
                 available_groups: List[ADGroup] = None,
                 available_templates: List[GroupTemplate] = None,
                 parent=None):
        """
        Initialize property editor
        
        Args:
            person: Person to edit
            existing_usernames: Set of existing usernames to check for duplicates
            available_groups: List of available groups for selection
            available_templates: List of available group templates
            parent: Parent widget
        """
        super().__init__(parent)
        self.person = person
        self.existing_usernames = existing_usernames
        self.available_groups = available_groups or []
        self.available_templates = available_templates or []
        self.original_values = {}
        
        self.setWindowTitle(f"Edit Properties - {person.first_name} {person.last_name}")
        self.setModal(True)
        self.setMinimumWidth(750)
        self.setMinimumHeight(450)
        self.resize(800, 650)
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        # Outer layout: scroll area + buttons
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        layout.setContentsMargins(10, 10, 10, 10)

        scroll.setWidget(scroll_widget)
        outer_layout.addWidget(scroll, stretch=1)
        
        # Create tab-like sections using group boxes
        
        # === BASIC INFORMATION ===
        basic_group = QGroupBox("Basic Information")
        basic_layout = QFormLayout(basic_group)
        
        self.first_name_input = QLineEdit(self.person.first_name)
        self.first_name_input.textChanged.connect(self.on_name_changed)
        basic_layout.addRow("First Name:", self.first_name_input)
        
        self.last_name_input = QLineEdit(self.person.last_name)
        self.last_name_input.textChanged.connect(self.on_name_changed)
        basic_layout.addRow("Last Name:", self.last_name_input)
        
        self.class_name_input = QLineEdit(self.person.class_name)
        self.class_name_input.textChanged.connect(self.on_class_changed)
        basic_layout.addRow("Class:", self.class_name_input)
        
        layout.addWidget(basic_group)
        
        # === AD PROPERTIES ===
        ad_group = QGroupBox("Active Directory Properties")
        ad_layout = QFormLayout(ad_group)
        
        # Username
        username_layout = QHBoxLayout()
        self.username_input = QLineEdit(self.person.ad_username or "")
        self.username_input.textChanged.connect(self.on_username_changed)
        username_layout.addWidget(self.username_input)
        gen_username_btn = QPushButton("Generate")
        gen_username_btn.clicked.connect(self.generate_username)
        username_layout.addWidget(gen_username_btn)
        ad_layout.addRow("Username:", username_layout)
        
        # Password
        password_layout = QHBoxLayout()
        self.password_display = QLineEdit(self.person.ad_password or "")
        self.password_display.setReadOnly(True)
        self.password_display.setEchoMode(QLineEdit.EchoMode.Password)
        password_layout.addWidget(self.password_display)
        show_password_btn = QPushButton("👁")
        show_password_btn.setMaximumWidth(40)
        show_password_btn.pressed.connect(lambda: self.password_display.setEchoMode(QLineEdit.EchoMode.Normal))
        show_password_btn.released.connect(lambda: self.password_display.setEchoMode(QLineEdit.EchoMode.Password))
        password_layout.addWidget(show_password_btn)
        gen_password_btn = QPushButton("Generate New")
        gen_password_btn.clicked.connect(self.generate_password)
        password_layout.addWidget(gen_password_btn)
        ad_layout.addRow("Password:", password_layout)
        
        # Display Name
        display_layout = QHBoxLayout()
        self.display_name_input = QLineEdit(self.person.ad_display_name or "")
        display_layout.addWidget(self.display_name_input)
        gen_display_btn = QPushButton("Generate")
        gen_display_btn.clicked.connect(self.generate_display_name)
        display_layout.addWidget(gen_display_btn)
        ad_layout.addRow("Display Name:", display_layout)
        
        # Email
        self.email_input = QLineEdit(self.person.ad_email or "")
        ad_layout.addRow("Email:", self.email_input)
        
        # Description
        self.description_input = QLineEdit(self.person.ad_description or "")
        ad_layout.addRow("Description:", self.description_input)
        
        layout.addWidget(ad_group)
        
        # === HOME DIRECTORY ===
        home_group = QGroupBox("Home Directory")
        home_layout = QVBoxLayout(home_group)
        
        # Path input
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("Path:"))
        self.home_path_input = QLineEdit(self.person.home_directory or "")
        self.home_path_input.setPlaceholderText(r"\\server\share\user or H:\user")
        path_layout.addWidget(self.home_path_input)
        home_layout.addLayout(path_layout)
        
        # Drive letter
        drive_layout = QHBoxLayout()
        drive_layout.addWidget(QLabel("Drive:"))
        self.home_drive_input = QLineEdit(self.person.home_drive or "")
        self.home_drive_input.setPlaceholderText("H:")
        self.home_drive_input.setMaximumWidth(60)
        drive_layout.addWidget(self.home_drive_input)
        drive_layout.addStretch()
        home_layout.addLayout(drive_layout)
        
        # Template selection
        template_layout = QHBoxLayout()
        template_layout.addWidget(QLabel("Generate from template:"))
        self.home_template_combo = QComboBox()
        self.home_template_combo.addItem("-- Select Template --", None)
        
        # Load templates with tooltips
        template_manager = HomeDirectoryTemplateManager()
        for name, template_path in template_manager.get_all_templates().items():
            self.home_template_combo.addItem(name, template_path)
            idx = self.home_template_combo.count() - 1
            self.home_template_combo.setItemData(
                idx,
                f"<b>{name}</b><br><code>{template_path}</code>",
                Qt.ItemDataRole.ToolTipRole
            )
        self.home_template_combo.setToolTip("Hover over a template to see its path pattern")
        self.home_template_combo.currentIndexChanged.connect(self._update_template_hint)

        template_layout.addWidget(self.home_template_combo, 1)
        
        generate_home_btn = QPushButton("Generate")
        generate_home_btn.clicked.connect(self.generate_home_path)
        template_layout.addWidget(generate_home_btn)
        home_layout.addLayout(template_layout)

        # Template preview / hint
        self._template_hint_label = QLabel()
        self._template_hint_label.setStyleSheet(
            "color: #aaa; font-size: 9px; font-style: italic;"
        )
        self._template_hint_label.setWordWrap(True)
        home_layout.addWidget(self._template_hint_label)
        
        # Info about placeholders
        help_text = QLabel(
            "<i>Templates support placeholders: {first_name}, {last_name}, "
            "{username}, {class_name}, with optional part selection</i>"
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #888; font-size: 10px;")
        home_layout.addWidget(help_text)
        
        layout.addWidget(home_group)
        
        # === GROUP MEMBERSHIPS ===
        groups_group = QGroupBox("Group Memberships")
        groups_layout = QVBoxLayout(groups_group)
        
        # Current groups display
        groups_layout.addWidget(QLabel("<b>Current Groups:</b>"))
        self.current_groups_list = QListWidget()
        self.current_groups_list.setMaximumHeight(100)
        self.refresh_group_display()
        groups_layout.addWidget(self.current_groups_list)
        
        # Group selection buttons
        button_layout = QHBoxLayout()
        
        select_groups_btn = QPushButton("📋 Select Groups...")
        select_groups_btn.clicked.connect(self.select_groups)
        button_layout.addWidget(select_groups_btn)
        
        apply_template_btn = QPushButton("🗂️ Apply Template...")
        apply_template_btn.clicked.connect(self.apply_group_template)
        button_layout.addWidget(apply_template_btn)
        
        clear_groups_btn = QPushButton("❌ Clear All")
        clear_groups_btn.clicked.connect(self.clear_all_groups)
        button_layout.addWidget(clear_groups_btn)
        
        groups_layout.addLayout(button_layout)
        
        layout.addWidget(groups_group)
        
        # === ACCOUNT STATUS ===
        status_group = QGroupBox("Account Status")
        status_layout = QVBoxLayout(status_group)
        
        self.account_enabled_checkbox = QCheckBox("Account Enabled")
        self.account_enabled_checkbox.setChecked(self.person.account_enabled)
        self.account_enabled_checkbox.setToolTip(
            "Enable or disable the account. Disabled accounts cannot log in."
        )
        status_layout.addWidget(self.account_enabled_checkbox)
        
        status_info = QLabel(
            "ℹ️ <i>By default, accounts are created disabled for security. "
            "Enable only when ready for use.</i>"
        )
        status_info.setWordWrap(True)
        status_info.setStyleSheet("color: #888; font-size: 10px; margin-top: 5px;")
        status_layout.addWidget(status_info)
        
        layout.addWidget(status_group)
        
        # === PASSWORD POLICY ===
        policy_group = QGroupBox("Password Policy")
        policy_layout = QVBoxLayout(policy_group)
        
        self.change_password_checkbox = QCheckBox("User must change password at next logon")
        self.change_password_checkbox.setChecked(self.person.password_must_change)
        policy_layout.addWidget(self.change_password_checkbox)
        
        self.cannot_change_checkbox = QCheckBox("User cannot change password")
        self.cannot_change_checkbox.setChecked(self.person.password_cannot_change)
        policy_layout.addWidget(self.cannot_change_checkbox)
        
        self.never_expires_checkbox = QCheckBox("Password never expires")
        self.never_expires_checkbox.setChecked(self.person.password_never_expires)
        policy_layout.addWidget(self.never_expires_checkbox)
        
        layout.addWidget(policy_group)
        
        # === VALIDATION STATUS ===
        self.validation_text = QTextEdit()
        self.validation_text.setReadOnly(True)
        self.validation_text.setMaximumHeight(100)
        layout.addWidget(QLabel("Validation Status:"))
        layout.addWidget(self.validation_text)
        
        # === DIALOG BUTTONS ===
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | 
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_changes)
        button_box.rejected.connect(self.reject)
        outer_layout.addWidget(button_box)
        
        # Store original values and validate
        self._store_original_values()
        self.validate()
    
    # === VALIDATION HELPER METHODS ===

    def _append_success(self, message: str) -> None:
        """
        Append a green success line to the validation area.

        Uses HTML so the colour is embedded in the content and cannot be
        overridden by a later call to setStyleSheet on the widget.
        """
        current = self.validation_text.toHtml()
        # Append as a new paragraph so it does not overwrite existing lines
        self.validation_text.append(
            f'<span style="color: #4CAF50;">&#10003; {message}</span>'
        )

    def _append_error(self, message: str) -> None:
        """Append a red error line to the validation area."""
        self.validation_text.append(
            f'<span style="color: #ff6b6b;">&#10060; {message}</span>'
        )

    # === GROUP MEMBERSHIP METHODS ===
    
    def refresh_group_display(self):
        """Refresh the display of current groups"""
        self.current_groups_list.clear()
        
        for group in self.person.group_memberships:
            item = QListWidgetItem(f"{group.name} ({group.group_type})")
            item.setToolTip(group.dn)
            self.current_groups_list.addItem(item)
        
        if not self.person.group_memberships:
            item = QListWidgetItem("No groups assigned")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.current_groups_list.addItem(item)
    
    def _update_template_hint(self, index):
        """Show the template path when a template is selected"""
        template_path = self.home_template_combo.currentData()
        if template_path and hasattr(self, '_template_hint_label'):
            self._template_hint_label.setText(f"Path: {template_path}")
        elif hasattr(self, '_template_hint_label'):
            self._template_hint_label.setText("")

    def select_groups(self):
        """Show dialog to select groups"""
        if not self.available_groups:
            QMessageBox.information(
                self, "No Groups Available",
                "No groups available. Please use Group Management to add groups first."
            )
            return
        
        # Create selection dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Groups")
        dialog.setModal(True)
        dialog.setMinimumSize(500, 400)
        
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("<b>Select groups for this user:</b>"))
        
        # List with checkboxes
        list_widget = QListWidget()
        
        current_dns = set(g.dn.lower() for g in self.person.group_memberships)
        
        for group in self.available_groups:
            item = QListWidgetItem(f"{group.name} - {group.dn}")
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            
            if group.dn.lower() in current_dns:
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
            # Update person's groups
            new_groups = []
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    group = item.data(Qt.ItemDataRole.UserRole)
                    new_groups.append(group)

            self.person.group_memberships = new_groups
            self.refresh_group_display()
            self._append_success("Updated group memberships")
    
    def apply_group_template(self):
        """Show dialog to apply a group template"""
        if not self.available_templates:
            QMessageBox.information(
                self, "No Templates Available",
                "No templates available. Please use Group Management to create templates first."
            )
            return
        
        # Create template selection dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Apply Group Template")
        dialog.setModal(True)
        dialog.setMinimumSize(500, 400)
        
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("<b>Select a template to apply:</b>"))
        
        # Template list
        list_widget = QListWidget()
        
        for template in self.available_templates:
            item = QListWidgetItem(
                f"{template.name} ({len(template.groups)} groups)"
            )
            item.setData(Qt.ItemDataRole.UserRole, template)
            item.setToolTip(template.description or "No description")
            list_widget.addItem(item)
        
        layout.addWidget(list_widget)
        
        # Preview area
        preview_label = QLabel("<b>Template groups:</b>")
        layout.addWidget(preview_label)
        
        preview_text = QTextEdit()
        preview_text.setReadOnly(True)
        preview_text.setMaximumHeight(150)
        layout.addWidget(preview_text)
        
        def update_preview():
            selected = list_widget.selectedItems()
            if selected:
                template = selected[0].data(Qt.ItemDataRole.UserRole)
                groups_text = "\n".join(f"• {g.name}" for g in template.groups)
                preview_text.setText(groups_text)
            else:
                preview_text.clear()
        
        list_widget.itemSelectionChanged.connect(update_preview)
        
        # Action selection
        action_layout = QHBoxLayout()
        action_layout.addWidget(QLabel("Action:"))
        action_combo = QComboBox()
        action_combo.addItem("Replace all groups", "replace")
        action_combo.addItem("Add to existing groups", "add")
        action_layout.addWidget(action_combo)
        layout.addLayout(action_layout)
        
        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = list_widget.selectedItems()
            if not selected:
                return
            
            template = selected[0].data(Qt.ItemDataRole.UserRole)
            action = action_combo.currentData()
            
            if action == "replace":
                self.person.group_memberships = template.groups.copy()
            elif action == "add":
                # Add groups not already present
                current_dns = set(g.dn.lower() for g in self.person.group_memberships)
                for group in template.groups:
                    if group.dn.lower() not in current_dns:
                        self.person.add_to_group(group)
            
            self.refresh_group_display()
            self._append_success(f"Applied template: {template.name}")
    
    def clear_all_groups(self):
        """Clear all group memberships"""
        if not self.person.group_memberships:
            return
        
        reply = QMessageBox.question(
            self, "Clear Groups",
            "Remove all group memberships?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.person.group_memberships = []
            self.refresh_group_display()
            self._append_success("Cleared all groups")
    
    # === HOME DIRECTORY METHODS ===
    
    def generate_home_path(self):
        """Generate home directory path from selected template"""
        template = self.home_template_combo.currentData()
        
        if not template:
            QMessageBox.warning(
                self, "No Template",
                "Please select a template first"
            )
            return
        
        try:
            generator = HomeDirectoryPathGenerator()
            path = generator.generate_path(template, self.person)
            
            self.home_path_input.setText(path)
            self._append_success(f"Generated home path: {path}")

        except PlaceholderError as e:
            QMessageBox.warning(
                self, "Generation Failed",
                f"Failed to generate path:\n{str(e)}"
            )
            self._append_error(f"Path generation failed: {e}")
    
    # === EXISTING METHODS (unchanged) ===
    
    def _store_original_values(self):
        """Store original values for change detection"""
        self.original_values = {
            'first_name': self.person.first_name,
            'last_name': self.person.last_name,
            'class_name': self.person.class_name,
            'ad_username': self.person.ad_username,
            'ad_password': self.person.ad_password,
            'ad_display_name': self.person.ad_display_name,
            'ad_email': self.person.ad_email,
            'ad_description': self.person.ad_description,
            'home_directory': self.person.home_directory,
            'home_drive': self.person.home_drive,
            'group_memberships': self.person.group_memberships.copy(),
            'password_must_change': self.person.password_must_change,
            'password_cannot_change': self.person.password_cannot_change,
            'password_never_expires': self.person.password_never_expires,
            'account_enabled': self.person.account_enabled,
        }
    
    def on_name_changed(self):
        """Handle name change"""
        first = self.first_name_input.text().strip()
        last = self.last_name_input.text().strip()
        
        if first and last:
            if (first != self.original_values['first_name'] or 
                last != self.original_values['last_name']):
                
                if self.person.ad_display_name:
                    self.validation_text.append(
                        "💡 Name changed - you may want to regenerate Display Name"
                    )
        
                if self.person.ad_username:
                    self.validation_text.append(
                        "💡 Name changed - you may want to regenerate Username"
                    )
        self.validate()
    
    def on_class_changed(self):
        """Handle class change"""
        if self.class_name_input.text() != self.original_values['class_name']:
            if self.person.ad_display_name:
                self.validation_text.append(
                    "💡 Class changed - you may want to regenerate Display Name"
                )
        self.validate()
    
    def on_username_changed(self):
        """Handle username change"""
        self.validate()
    
    def generate_username(self):
        """Generate username from name"""
        first = self.first_name_input.text().strip()
        last = self.last_name_input.text().strip()
        
        if not first or not last:
            QMessageBox.warning(self, "Missing Data", "First and last name required")
            return
        
        username = generate_username(first, last, self.existing_usernames)
        self.username_input.setText(username)
        self._append_success(f"Generated username: {username}")
        self.validate()
    
    def generate_password(self):
        """Generate new password"""
        password = generate_password()
        self.password_display.setText(password)
        self._append_success("Generated new password")
        self.validate()
    
    def generate_display_name(self):
        """Generate display name from name and class"""
        first = self.first_name_input.text().strip()
        last = self.last_name_input.text().strip()
        class_name = self.class_name_input.text().strip()
        
        if not all([first, last, class_name]):
            QMessageBox.warning(self, "Missing Data", 
                              "First name, last name and class required")
            return
        
        display = generate_display_name(first, last, class_name)
        self.display_name_input.setText(display)
        self._append_success(f"Generated display name: {display}")
        self.validate()
    
    def validate(self):
        """
        Validate current field values and update the validation status area.

        Each message is coloured individually using HTML so that positive
        (✓) messages always appear green, warnings orange, and errors red —
        regardless of whether other issues exist.
        """
        # Build a temporary Person from the current UI values for validation
        temp_person = Person(
            first_name=self.first_name_input.text().strip(),
            last_name=self.last_name_input.text().strip(),
            class_name=self.class_name_input.text().strip(),
            ad_username=self.username_input.text().strip() or None,
            ad_password=self.password_display.text() or None,
            ad_display_name=self.display_name_input.text().strip() or None,
            ad_email=self.email_input.text().strip() or None,
            ad_description=self.description_input.text().strip() or None,
        )

        result = ADValidator.validate_person(temp_person)

        # Switch the widget to HTML rendering and clear old content
        self.validation_text.clear()
        # Remove any previously set global colour override so that the
        # per-message HTML colours are not masked
        self.validation_text.setStyleSheet("")

        html_lines = []

        if result.is_valid:
            html_lines.append(
                '<span style="color: #4CAF50;">&#10003; All validations passed</span>'
            )

        # Errors — always red, regardless of overall validity
        for issue in result.errors:
            html_lines.append(
                f'<span style="color: #ff6b6b;">&#10060; {issue.field}: {issue.message}</span>'
            )

        # Warnings — always orange
        for issue in result.warnings:
            html_lines.append(
                f'<span style="color: #FFA726;">&#9888; {issue.field}: {issue.message}</span>'
            )

        self.validation_text.setHtml("<br>".join(html_lines))

        return result.is_valid
    
    def accept_changes(self):
        """Apply changes to person"""
        # Final validation
        if not self.validate():
            reply = QMessageBox.question(
                self, "Validation Errors",
                "There are validation errors. Apply changes anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return
        
        # Check for required fields
        first = self.first_name_input.text().strip()
        last = self.last_name_input.text().strip()
        class_name = self.class_name_input.text().strip()
        
        if not all([first, last, class_name]):
            QMessageBox.warning(self, "Missing Data", 
                              "First name, last name and class are required")
            return
        
        # Apply changes - AUTOMATIC DIRTY TRACKING via property setters
        self.person.first_name = first
        self.person.last_name = last
        self.person.class_name = class_name
        self.person.ad_username = self.username_input.text().strip() or None
        self.person.ad_password = self.password_display.text() or None
        self.person.ad_display_name = self.display_name_input.text().strip() or None
        self.person.ad_email = self.email_input.text().strip() or None
        self.person.ad_description = self.description_input.text().strip() or None
        
        # Home directory
        self.person.home_directory = self.home_path_input.text().strip() or None
        self.person.home_drive = self.home_drive_input.text().strip() or None
        
        # Group memberships already updated through dialog interactions
        
        # Password policy
        self.person.password_must_change = self.change_password_checkbox.isChecked()
        self.person.password_cannot_change = self.cannot_change_checkbox.isChecked()
        self.person.password_never_expires = self.never_expires_checkbox.isChecked()
        
        # Account status
        self.person.account_enabled = self.account_enabled_checkbox.isChecked()
        
        logger.info(f"Updated person properties: {self.person}")
        logger.debug(f"Dirty fields: {self.person.get_dirty_fields()}")
        self.accept()
