"""
Active Directory Management Operations - New Architecture
VERSION 3 - Removed manual mark_field_dirty() calls (automatic via properties)
FIXED VERSION - Corrected imports, improved error handling, fixed edge cases
Part 1 of 2 - Main widget and helpers
"""

import logging
import re
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QDialog, QDialogButtonBox, QTextEdit,
    QGroupBox, QCheckBox, QListWidget, QListWidgetItem, QMenu, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QProgressDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QAction

from models import Person, ADStatus
from services.ad_client import ADClient
from services.ad_services import (
    ADDiscoveryService, SyncPlanner, ConflictDetector, ConflictResolver,
    PersonsSyncService, ConflictStrategy
)
from services.ad_validator import ADValidator
from ui.column_management_dialog import ColumnVisibilityDialog
from ui.property_editor import PropertyEditorDialog
from utils.ad_utils import generate_username, generate_password, generate_display_name
from ui.bulk_edit_dialog import BulkEditDialog
from ui.group_management_dialog import GroupManagementDialog, show_group_management_dialog
from ui.password_policy_dialog import PasswordPolicyDialog
from ui.username_policy_dialog import UsernamePolicyDialog
from ui.home_template_dialog import HomeDirectoryTemplateDialog
from ui.ad_analysis_dialog import ADAnalysisDialog
from utils.ad_search_scope import OU_PLACEHOLDERS, SearchScope, SearchScopeConfig
from ui.ad_difference_view import (
    ADDifferenceDelegate, combined_difference, differences_shown, mark_item,
    person_differences, set_differences_shown, toggle_button_text,
)

logger = logging.getLogger(__name__)

#: Background colour of the "Status" cell, per status.
#:
#: The three tones say how much attention a row needs, not how pretty it is:
#: green = nothing to do, amber = something is waiting or only partly done,
#: red = a problem that stops the person from being synchronised.  A status
#: without an entry keeps the table's own background.
STATUS_COLORS = {
    ADStatus.MATCHES_AD: QColor("#4CAF50"),
    ADStatus.SYNC_SUCCEEDED: QColor("#4CAF50"),
    ADStatus.DIFFERS_FROM_AD: QColor("#FFA726"),
    ADStatus.SYNC_INCOMPLETE: QColor("#FF7043"),
    ADStatus.NOT_FOUND_IN_AD: QColor("#EF5350"),
    ADStatus.MULTIPLE_AD_MATCHES: QColor("#AB47BC"),
}


class ADDiscoveryThread(QThread):
    """
    Thread for AD discovery operations.

    The completion signal is named ``discovery_finished`` on purpose: QThread
    already provides a ``finished()`` signal, and declaring another signal with
    the same name shadows it, which makes the two collide in unpredictable
    ways.
    """

    discovery_finished = pyqtSignal(bool, object, str)
    progress = pyqtSignal(str)
    
    def __init__(self, source, server, base_dn, username, password,
                 connection_options=None, scope_config=None):
        super().__init__()
        self.source = source
        self.server = server
        self.base_dn = base_dn
        self.username = username
        self.password = password
        #: TLS settings from the connection panel, applied to every client
        self.connection_options = dict(connection_options or {})
        #: Where to look for a person (see utils.ad_search_scope)
        self.scope_config = scope_config
        self._is_running = True
        
    def run(self):
        """Run discovery"""
        try:
            if not self._is_running:
                self.discovery_finished.emit(False, None, "Discovery cancelled")
                return
                
            with ADClient(self.server, self.username, self.password,
                          **self.connection_options) as client:
                if not client.connection:
                    self.discovery_finished.emit(False, None, "Failed to connect to AD")
                    return
                
                if not self._is_running:
                    self.discovery_finished.emit(False, None, "Discovery cancelled")
                    return
                
                self.progress.emit("Discovering persons in AD...")
                
                discovery_service = ADDiscoveryService(client, self.scope_config)
                persons = self.source.get_all_persons()
                
                if not persons:
                    self.discovery_finished.emit(True, [], "No persons to discover")
                    return
                
                results = discovery_service.discover_persons(persons, self.base_dn)
                
                if self._is_running:
                    self.discovery_finished.emit(True, results, f"Discovered {len(results)} persons")
                else:
                    self.discovery_finished.emit(False, None, "Discovery cancelled")
                
        except Exception as e:
            logger.exception("Error during AD discovery")
            self.discovery_finished.emit(False, None, f"Error: {str(e)}")
    
    def stop(self):
        """Stop the discovery thread"""
        self._is_running = False


class ConflictResolutionDialog(QDialog):
    """Dialog for resolving conflicts"""
    
    def __init__(self, conflict_info, parent=None):
        super().__init__(parent)
        self.conflict_info = conflict_info
        self.user_choices = {}
        self.setWindowTitle(f"Resolve Conflict - {conflict_info.person.first_name} {conflict_info.person.last_name}")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.init_ui()
        
    def init_ui(self):
        """Initialize UI"""
        layout = QVBoxLayout(self)
        
        # Person info
        person = self.conflict_info.person
        info = QLabel(f"<b>{person.first_name} {person.last_name}</b> ({person.class_name})")
        layout.addWidget(info)
        
        layout.addWidget(QLabel("The following fields have conflicting changes:"))
        
        # Store combo boxes for later access
        self.field_combos = {}
        
        # For each conflicting field, show options
        for field in self.conflict_info.conflicting_fields:
            field_group = QGroupBox(f"Field: {field}")
            field_layout = QVBoxLayout(field_group)
            
            local_value = self.conflict_info.local_changes.get(field)
            remote_value = self.conflict_info.remote_changes.get(field)
            
            field_layout.addWidget(QLabel(f"Your change: {local_value}"))
            field_layout.addWidget(QLabel(f"AD current value: {remote_value}"))
            
            # Radio buttons
            combo = QComboBox()
            combo.addItem(f"Use my value: {local_value}", "local")
            combo.addItem(f"Use AD value: {remote_value}", "remote")
            combo.addItem("Enter custom value...", "custom")
            combo.setProperty("field", field)
            combo.currentIndexChanged.connect(lambda idx, c=combo: self.on_choice_changed(c))
            field_layout.addWidget(combo)
            
            # Custom input (hidden initially)
            custom_input = QLineEdit()
            custom_input.setVisible(False)
            custom_input.setPlaceholderText("Enter custom value...")
            custom_input.setProperty("field", field)
            field_layout.addWidget(custom_input)
            
            # Store references
            combo.setProperty("custom_input", custom_input)
            self.field_combos[field] = (combo, custom_input)
            
            layout.addWidget(field_group)
        
        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_choices)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def on_choice_changed(self, combo):
        """Handle choice change"""
        custom_input = combo.property("custom_input")
        if custom_input and combo.currentData() == "custom":
            custom_input.setVisible(True)
        elif custom_input:
            custom_input.setVisible(False)
    
    def accept_choices(self):
        """Collect choices and accept"""
        # Collect all choices
        for field, (combo, custom_input) in self.field_combos.items():
            choice_type = combo.currentData()
            
            if choice_type == "local":
                self.user_choices[field] = "local"
            elif choice_type == "remote":
                self.user_choices[field] = "remote"
            elif choice_type == "custom":
                custom_value = custom_input.text().strip()
                if not custom_value:
                    QMessageBox.warning(self, "Missing Value", 
                                      f"Please enter a custom value for {field}")
                    return
                self.user_choices[field] = f"custom:{custom_value}"
        
        self.accept()


class SyncReviewDialog(QDialog):
    """Dialog for reviewing sync plan before execution"""
    
    def __init__(self, sync_plan, parent=None):
        super().__init__(parent)
        self.sync_plan = sync_plan
        self.setWindowTitle("Review Synchronization Plan")
        self.setModal(True)
        self.setMinimumSize(800, 600)
        self.init_ui()
        
    def init_ui(self):
        """Initialize UI"""
        layout = QVBoxLayout(self)
        
        # Statistics
        stats = self.sync_plan.statistics
        stats_text = (
            f"🆕 Create: {stats['creates']}\n"
            f"✏️ Update: {stats['updates']}\n"
            f"⚠️ Conflicts: {stats['conflicts']}\n"
            f"❌ Incomplete: {stats['incomplete']}"
        )
        stats_label = QLabel(stats_text)
        stats_label.setStyleSheet("background-color: #2a4a5a; padding: 10px; font-size: 14px;")
        layout.addWidget(stats_label)
        
        # Tabs for different operation types
        tabs = QTabWidget()
        
        # Create operations tab
        if self.sync_plan.create_operations:
            create_text = QTextEdit()
            create_text.setReadOnly(True)
            for op in self.sync_plan.create_operations:
                create_text.append(
                    f"Create: {op.person.first_name} {op.person.last_name} "
                    f"({op.person.class_name}) -> {op.target_ou}"
                )
            tabs.addTab(create_text, f"Create ({len(self.sync_plan.create_operations)})")
        
        # Update operations tab
        if self.sync_plan.update_operations:
            update_text = QTextEdit()
            update_text.setReadOnly(True)
            for op in self.sync_plan.update_operations:
                conflict_mark = "⚠️" if op.has_conflict else ""
                update_text.append(
                    f"{conflict_mark}Update: {op.person.first_name} {op.person.last_name} "
                    f"({op.person.class_name}) - Changes: {list(op.changes.keys())}"
                )
            tabs.addTab(update_text, f"Update ({len(self.sync_plan.update_operations)})")
        
        # Incomplete persons tab
        if self.sync_plan.incomplete_persons:
            incomplete_text = QTextEdit()
            incomplete_text.setReadOnly(True)
            incomplete_text.setStyleSheet("color: #ff6b6b;")
            for person in self.sync_plan.incomplete_persons:
                missing = ADValidator.check_missing_properties(person)
                incomplete_text.append(
                    f"❌ {person.first_name} {person.last_name} ({person.class_name}) "
                    f"- Missing: {', '.join(missing)}"
                )
            tabs.addTab(incomplete_text, f"Incomplete ({len(self.sync_plan.incomplete_persons)})")
        
        layout.addWidget(tabs)
        
        # Warning if conflicts exist
        if stats['conflicts'] > 0:
            warning = QLabel(
                f"⚠️ There are {stats['conflicts']} conflict(s) that require resolution before syncing."
            )
            warning.setStyleSheet("color: #ff6b6b; font-weight: bold; padding: 10px;")
            layout.addWidget(warning)
        
        # Buttons
        button_box = QDialogButtonBox()
        
        if stats['conflicts'] > 0:
            resolve_btn = button_box.addButton("Resolve Conflicts", QDialogButtonBox.ButtonRole.ActionRole)
            resolve_btn.clicked.connect(self.accept)
        
        if stats['conflicts'] == 0 and stats['total_operations'] > 0:
            sync_btn = button_box.addButton("Synchronize", QDialogButtonBox.ButtonRole.AcceptRole)
            sync_btn.clicked.connect(self.accept)
        
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.clicked.connect(self.reject)
        
        layout.addWidget(button_box)


class ADManagementWidget(QWidget):
    """Widget for Active Directory management operations - Enhanced Version"""
    
    # Default visible columns
    DEFAULT_COLUMNS = ["Name", "Class", "Username", "Email", "Status", "Enabled", "Dirty"]
    ALL_COLUMNS = ["Name", "Class", "Username", "Password", "Email", "Display Name",
                   "Home Directory", "Groups",
                   "Status", "Enabled", "Dirty", "Must Change Pwd", "Cannot Change Pwd",
                   "Never Expires"]

    #: Which compared fields each column shows (point 19 b).
    #:
    #: A column that is absent here is never outlined, either because Active
    #: Directory does not hold the value ("Password" - it is never given back,
    #: "Dirty" - a local flag) or because it is not an attribute at all
    #: ("Class" is a position in the tree).
    COLUMN_DIFFERENCE_FIELDS = {
        "Name": ("first_name", "last_name"),
        "Username": ("ad_username",),
        "Email": ("ad_email",),
        "Display Name": ("ad_display_name",),
        "Home Directory": ("home_drive", "home_directory"),
        "Groups": ("group_memberships",),
    }
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.current_source = None
        self.ad_client = None
        self.visible_columns = self.DEFAULT_COLUMNS.copy()
        self.discovery_thread = None
        self.available_groups = []       # populated via Manage Groups
        self.available_templates = []    # populated via Manage Groups
        
        # Connect to source manager signals for auto-refresh
        self.source_manager.source_added.connect(self.on_source_changed_external)
        self.source_manager.source_removed.connect(self.on_source_changed_external)
        self.source_manager.source_modified.connect(self.on_source_modified_external)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Info, with the collapse control on the same row
        header_row = QHBoxLayout()
        info = QLabel(
            "<b>Active Directory Management</b><br>"
            "Manage student data synchronization with Active Directory."
        )
        info.setWordWrap(True)
        header_row.addWidget(info, stretch=1)

        # One button hides BOTH the connection panel and the operations panel,
        # so the person table can use the whole tab. "Columns..." and the table
        # controls stay visible - they belong to the table.
        self.toggle_panels_button = QPushButton("⌃ Hide connection && operations")
        self.toggle_panels_button.setCheckable(True)
        self.toggle_panels_button.setChecked(False)
        self.toggle_panels_button.setToolTip(
            "Collapse the Active Directory Connection and Operations panels to "
            "see more of the person table."
        )
        self.toggle_panels_button.toggled.connect(self._on_toggle_panels)
        header_row.addWidget(self.toggle_panels_button, alignment=Qt.AlignmentFlag.AlignTop)

        layout.addLayout(header_row)
        
        # AD Connection Panel
        conn_group = QGroupBox("Active Directory Connection")
        conn_layout = QVBoxLayout(conn_group)
        
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Server:"))
        self.ad_server_input = QLineEdit()
        self.ad_server_input.setPlaceholderText("ldap://dc.example.com")
        row1.addWidget(self.ad_server_input)
        conn_layout.addLayout(row1)
        
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Base DN:"))
        self.ad_base_input = QLineEdit()
        self.ad_base_input.setPlaceholderText("OU=Students,DC=example,DC=com")
        row2.addWidget(self.ad_base_input)
        conn_layout.addLayout(row2)
        
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Username:"))
        self.ad_user_input = QLineEdit()
        self.ad_user_input.setPlaceholderText("domain\\admin")
        row3.addWidget(self.ad_user_input)
        conn_layout.addLayout(row3)
        
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Password:"))
        self.ad_pass_input = QLineEdit()
        self.ad_pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        row4.addWidget(self.ad_pass_input)
        conn_layout.addLayout(row4)

        # --- security --------------------------------------------------------
        # Active Directory REFUSES to set a password over an unencrypted
        # connection (error 53, "problem 5003 WILL_NOT_PERFORM"), so the
        # channel has to be encrypted for credentials to work at all.
        row5 = QHBoxLayout()
        self.ad_start_tls_check = QCheckBox("Encrypt connection (StartTLS)")
        self.ad_start_tls_check.setChecked(True)
        self.ad_start_tls_check.setToolTip(
            "Upgrade a plain ldap:// connection to an encrypted one.\n"
            "Active Directory only accepts password changes over an encrypted\n"
            "channel - without this, setting passwords fails with\n"
            "'unwillingToPerform'.\n"
            "\n"
            "You do NOT need an ldaps:// address for this: StartTLS encrypts\n"
            "the ordinary ldap:// connection on port 389.  An ldaps:// address\n"
            "(port 636) is the alternative - it is already encrypted, so this\n"
            "option is then ignored.\n"
            "\n"
            "The domain controller does need a server certificate for LDAP;\n"
            "without one it refuses the upgrade.  The certificate does not have\n"
            "to be trusted by this computer unless 'Verify server certificate'\n"
            "is switched on."
        )
        row5.addWidget(self.ad_start_tls_check)

        self.ad_validate_cert_check = QCheckBox("Verify server certificate")
        self.ad_validate_cert_check.setChecked(False)
        self.ad_validate_cert_check.setToolTip(
            "Verify the domain controller's certificate.\n"
            "Usually off, because school domain controllers commonly use a\n"
            "self-signed certificate.\n"
            "\n"
            "Off does not mean unencrypted: the connection is still encrypted,\n"
            "the certificate simply is not checked against a trusted authority."
        )
        row5.addWidget(self.ad_validate_cert_check)
        row5.addStretch()
        conn_layout.addLayout(row5)

        self.ad_security_hint = QLabel(
            "ℹ️ Passwords can only be set over an encrypted connection "
            "(ldaps:// or StartTLS)."
        )
        self.ad_security_hint.setWordWrap(True)
        self.ad_security_hint.setStyleSheet("color: #888; font-size: 10px;")
        conn_layout.addWidget(self.ad_security_hint)

        # --- where to search (point 07a) -----------------------------------
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Search in:"))
        self.ad_scope_combo = QComboBox()
        for scope in SearchScope:
            self.ad_scope_combo.addItem(scope.label, scope)
        self.ad_scope_combo.setToolTip(
            "Where 'Discover in AD' and the synchronisation look for a person.\n"
            "The default walks the whole subtree below the Base DN."
        )
        self.ad_scope_combo.currentIndexChanged.connect(self._on_search_scope_changed)
        scope_row.addWidget(self.ad_scope_combo, stretch=1)
        conn_layout.addLayout(scope_row)

        ou_row = QHBoxLayout()
        ou_row.addWidget(QLabel("Organisational units:"))
        self.ad_ou_input = QLineEdit()
        self.ad_ou_input.setEnabled(False)
        self.ad_ou_input.setPlaceholderText(
            "Trida-{class_name}, Rocnik-{grade}, {enrollment_year}"
        )
        self.ad_ou_input.setToolTip(
            "One or more OU names, separated by a comma or a semicolon.\n"
            "Each is searched directly inside the Base DN.\n"
            "Placeholders are resolved per person, so one entry can cover "
            "every class."
        )
        ou_row.addWidget(self.ad_ou_input, stretch=1)
        conn_layout.addLayout(ou_row)

        self.ad_ou_hint = QLabel(
            "Placeholders: " + " · ".join(
                f"<code>{{{name}}}</code> {description}"
                for name, description in OU_PLACEHOLDERS.items()
            )
        )
        self.ad_ou_hint.setWordWrap(True)
        self.ad_ou_hint.setStyleSheet("color: #888; font-size: 9px;")
        self.ad_ou_hint.setVisible(False)
        conn_layout.addWidget(self.ad_ou_hint)

        self.connection_group = conn_group
        layout.addWidget(conn_group)
        
        # Operations Panel
        ops_group = QGroupBox("Operations")
        ops_layout = QHBoxLayout(ops_group)
        
        discover_btn = QPushButton("🔍 Discover in AD")
        discover_btn.setToolTip("Check which persons exist in AD")
        discover_btn.clicked.connect(self.discover_in_ad)
        ops_layout.addWidget(discover_btn)
        
        # --- credential generation (point 09) ----------------------------
        # Four commands - {all credentials, only missing} x {all people,
        # selected people} - would be four buttons. Two of them differ only in
        # WHO they apply to, which is a property of the current selection, not
        # a different operation. So the operation stays a button and the scope
        # becomes a combo box next to it: two controls instead of four, and the
        # chosen scope is visible rather than implied by the button caption.
        # (A radio pair was the alternative; it costs more width and the scope
        # is not a mode the user keeps switching, so a combo reads better.)
        self.generate_scope_combo = QComboBox()
        self.generate_scope_combo.addItem("for all persons", "all")
        self.generate_scope_combo.addItem("for selected persons", "selected")
        self.generate_scope_combo.setToolTip(
            "Which persons the two Generate buttons apply to."
        )
        ops_layout.addWidget(QLabel("Generate:"))
        ops_layout.addWidget(self.generate_scope_combo)

        gen_all_btn = QPushButton("🔑 All Credentials")
        gen_all_btn.setToolTip(
            "Generate user name, password and display name, overwriting "
            "whatever is already there."
        )
        gen_all_btn.clicked.connect(self.generate_all_credentials)
        ops_layout.addWidget(gen_all_btn)

        gen_missing_btn = QPushButton("➕ Only Missing")
        gen_missing_btn.setToolTip(
            "Only fill in what is missing; existing credentials are kept."
        )
        gen_missing_btn.clicked.connect(self.generate_missing_credentials)
        ops_layout.addWidget(gen_missing_btn)

        analyze_btn = QPushButton("📊 Analyze Source")
        analyze_btn.clicked.connect(self.analyze_source)
        ops_layout.addWidget(analyze_btn)

        bulk_edit_btn = QPushButton("✏️ Bulk Edit...")
        bulk_edit_btn.setToolTip("Edit multiple persons at once")
        bulk_edit_btn.clicked.connect(self.bulk_edit_persons)
        ops_layout.addWidget(bulk_edit_btn)

        manage_groups_btn = QPushButton("🗂️ Manage Groups...")
        manage_groups_btn.clicked.connect(self.open_group_management)
        ops_layout.addWidget(manage_groups_btn)

        # --- format editors, grouped behind one control (point 11b) -------
        self.format_combo = QComboBox()
        self.format_combo.addItem("⚙ Formats…", None)
        self.format_combo.addItem("🔐 Password Format…", "password")
        self.format_combo.addItem("🔤 Username Format…", "username")
        self.format_combo.addItem("🏠 Home Directory Templates…", "home")
        self.format_combo.setToolTip(
            "Define how generated passwords, user names and home directory "
            "paths are built. The same rules are used for validation."
        )
        self.format_combo.activated.connect(self._on_format_chosen)
        ops_layout.addWidget(self.format_combo)

        sync_btn = QPushButton("🔄 Synchronize")
        sync_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        sync_btn.clicked.connect(self.synchronize)
        ops_layout.addWidget(sync_btn)
        
        self.operations_group = ops_group
        layout.addWidget(ops_group)
        
        # Table controls
        table_controls = QHBoxLayout()

        # "Columns..." stays here on purpose: it belongs to the table, and it
        # must remain reachable when the two panels above are collapsed.
        columns_btn = QPushButton("📋 Columns...")
        columns_btn.setToolTip("Select visible columns")
        columns_btn.clicked.connect(self.select_columns)
        table_controls.addWidget(columns_btn)

        # --- 19b: show what Active Directory currently holds ---------------
        self.show_ad_diff_button = QPushButton()
        self.show_ad_diff_button.setCheckable(True)
        # The switch is shared with the editing window and remembered between
        # sessions, so the button starts in whatever state the user left.
        self.show_ad_diff_button.setChecked(differences_shown())
        self.show_ad_diff_button.setText(
            toggle_button_text(self.show_ad_diff_button.isChecked()))
        self.show_ad_diff_button.setToolTip(
            "Outline the cells whose value differs from the one read from "
            "Active Directory by 'Discover in AD'.\n"
            "Hover over an outlined cell to see the directory's value."
        )
        self.show_ad_diff_button.toggled.connect(self._on_ad_diff_toggled)
        table_controls.addWidget(self.show_ad_diff_button)

        table_controls.addStretch()

        layout.addLayout(table_controls)
        
        # Person Table
        self.person_table = QTableWidget()
        self.person_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.person_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.person_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.person_table.setAlternatingRowColors(True)
        self.person_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.person_table.customContextMenuRequested.connect(self.show_context_menu)

        # Draws the outline around cells that differ from Active Directory.
        # It asks this widget whether the outlines are on, so toggling the
        # button only needs a repaint, not a rebuild of every row.
        self.ad_difference_delegate = ADDifferenceDelegate(
            self.ad_differences_visible, self.person_table)
        self.person_table.setItemDelegate(self.ad_difference_delegate)
        
        # Enable sorting
        self.person_table.setSortingEnabled(True)
        
        # Style
        self.person_table.setStyleSheet("""
            QTableWidget {
                gridline-color: #555;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QTableWidget::item:selected {
                background-color: #4a6fa5;
            }
            QHeaderView::section {
                padding: 5px;
                border: 1px solid #555;
                font-weight: bold;
            }
        """)
        
        layout.addWidget(self.person_table)
        
    def _on_toggle_panels(self, hidden: bool) -> None:
        """Collapse or restore the two panels above the table."""
        self.connection_group.setVisible(not hidden)
        self.operations_group.setVisible(not hidden)
        self.toggle_panels_button.setText(
            "⌄ Show connection && operations" if hidden
            else "⌃ Hide connection && operations"
        )

    def _mark_ad_difference(self, item, differences: dict, column: str) -> None:
        """
        Flag one table cell when its value differs from Active Directory.

        Args:
            item: The cell.
            differences: ``field -> FieldDifference`` for this person.
            column: The column name, which decides which fields the cell shows.
        """
        fields = self.COLUMN_DIFFERENCE_FIELDS.get(column)
        if not fields or not differences:
            return
        mark_item(item, combined_difference(
            column, [differences.get(field) for field in fields]))

    def ad_differences_visible(self) -> bool:
        """
        Whether the outlines are currently switched on.

        Read from the button while it exists, so the delegate follows the click
        immediately, and from the stored setting before the interface is built.
        """
        button = getattr(self, "show_ad_diff_button", None)
        if button is None:
            return differences_shown()
        return button.isChecked()

    def _on_ad_diff_toggled(self, checked: bool) -> None:
        """
        Remember the choice and repaint the table.

        The state is stored as a setting rather than kept in this widget, which
        is what makes it survive into the editing window and into the next
        person the user opens (point 19 b).
        """
        set_differences_shown(checked)
        self.show_ad_diff_button.setText(toggle_button_text(checked))
        self.person_table.viewport().update()

    def _on_search_scope_changed(self, _index: int) -> None:
        """Only the "named OUs" scope needs the OU list."""
        named = self.ad_scope_combo.currentData() is SearchScope.NAMED_OUS
        self.ad_ou_input.setEnabled(named)
        self.ad_ou_hint.setVisible(named)

    def get_search_scope(self) -> SearchScopeConfig:
        """
        Build the search scope from the connection panel.

        Returns:
            The configured :class:`~utils.ad_search_scope.SearchScopeConfig`.
        """
        scope = self.ad_scope_combo.currentData() or SearchScope.SUBTREE
        raw = self.ad_ou_input.text()
        templates = [part.strip() for part in re.split(r'[;,]', raw) if part.strip()]
        return SearchScopeConfig(scope=scope, ou_templates=templates)

    def get_connection_options(self) -> dict:
        """
        Return the keyword arguments every ADClient in this widget is built with.

        Keeping it in one place means the security settings cannot be applied
        to some connections and forgotten on others.
        """
        return {
            "use_start_tls": self.ad_start_tls_check.isChecked(),
            "validate_certificate": self.ad_validate_cert_check.isChecked(),
        }

    def set_source(self, source):
        """Set the current source"""
        self.current_source = source
        self.refresh_person_table()
        
    def on_source_changed_external(self, *args):
        """Handle external source changes"""
        # Don't refresh if no source selected yet
        if self.current_source:
            self.refresh_person_table()
    
    def on_source_modified_external(self, source_name):
        """Handle source modification"""
        if self.current_source and self.current_source.name == source_name:
            self.refresh_person_table()

    def select_columns(self):
        """Show column visibility dialog"""
        dialog = ColumnVisibilityDialog(self.ALL_COLUMNS, self.visible_columns, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_visible = dialog.get_visible_columns()
            # Ensure at least one column is visible
            if not new_visible:
                QMessageBox.warning(self, "No Columns", 
                                  "At least one column must be visible")
                return
            self.visible_columns = new_visible
            self.refresh_person_table()

    def refresh_person_table(self):
        """Refresh the person table with visible columns"""
        # QTableWidget.clear() removes the ITEMS but not the widgets placed with
        # setCellWidget(), so every repaint left the previous "Edit" buttons
        # behind - they accumulated and kept their captured Person alive.
        for row in range(self.person_table.rowCount()):
            for column in range(self.person_table.columnCount()):
                widget = self.person_table.cellWidget(row, column)
                if widget is not None:
                    self.person_table.removeCellWidget(row, column)
                    # removeCellWidget() (and deleteLater()) only SCHEDULE the
                    # destruction: until the event loop gets around to it the
                    # button is still a child of the viewport, still painted and
                    # still holding a reference to its Person.  A burst of
                    # repaints therefore piled the old buttons up.  Detaching the
                    # widget from the table right away is what actually drops it;
                    # deleteLater() then frees it when it is safe to do so.
                    widget.setParent(None)
                    widget.deleteLater()

        self.person_table.clear()
        self.person_table.setSortingEnabled(False)  # Disable while populating
        
        if not self.current_source:
            self.person_table.setRowCount(0)
            self.person_table.setColumnCount(0)
            return
        
        persons = self.current_source.get_all_persons()
        
        # Set up columns
        self.person_table.setColumnCount(len(self.visible_columns) + 1)  # +1 for Actions
        headers = self.visible_columns + ["Actions"]
        self.person_table.setHorizontalHeaderLabels(headers)
        
        # Set row count
        self.person_table.setRowCount(len(persons))
        
        # Populate table
        for row, person in enumerate(persons):
            col = 0
            first_item = None
            # What "Discover in AD" found differs from what the application
            # holds.  Empty for a person who has never been discovered.
            differences = person_differences(person)

            # Name
            if "Name" in self.visible_columns:
                name_item = QTableWidgetItem(f"{person.first_name} {person.last_name}")
                self._mark_ad_difference(name_item, differences, "Name")
                self.person_table.setItem(row, col, name_item)
                first_item = first_item or name_item
                col += 1
            
            # Class
            if "Class" in self.visible_columns:
                class_item = QTableWidgetItem(person.class_name)
                self.person_table.setItem(row, col, class_item)
                col += 1
            
            # Username
            if "Username" in self.visible_columns:
                username_item = QTableWidgetItem(person.ad_username or "(not set)")
                self._mark_ad_difference(username_item, differences, "Username")
                self.person_table.setItem(row, col, username_item)
                col += 1
            
            # Password
            if "Password" in self.visible_columns:
                password_item = QTableWidgetItem("•" * 8 if person.ad_password else "(not set)")
                self.person_table.setItem(row, col, password_item)
                col += 1
            
            # Email
            if "Email" in self.visible_columns:
                email_item = QTableWidgetItem(person.ad_email or "(not set)")
                self._mark_ad_difference(email_item, differences, "Email")
                self.person_table.setItem(row, col, email_item)
                col += 1
            
            # Display Name
            if "Display Name" in self.visible_columns:
                display_item = QTableWidgetItem(person.ad_display_name or "(not set)")
                self._mark_ad_difference(display_item, differences, "Display Name")
                self.person_table.setItem(row, col, display_item)
                col += 1
            
            # Home Directory
            if "Home Directory" in self.visible_columns:
                home_text = person.home_directory or "(not set)"
                if person.home_directory and person.home_drive:
                    home_text = f"{person.home_drive}  {person.home_directory}"
                home_item = QTableWidgetItem(home_text)
                home_item.setToolTip(person.home_directory or "")
                self._mark_ad_difference(home_item, differences, "Home Directory")
                self.person_table.setItem(row, col, home_item)
                col += 1

            # Groups the person belongs to
            if "Groups" in self.visible_columns:
                groups = person.group_memberships or []
                if groups:
                    names = [g.name for g in groups]
                    groups_text = ", ".join(names)
                    groups_item = QTableWidgetItem(groups_text)
                    # The DNs are what actually identifies a group; showing them
                    # in the cell would be unreadable, so they go in the tooltip.
                    groups_item.setToolTip("\n".join(g.dn for g in groups))
                else:
                    groups_item = QTableWidgetItem("(none)")
                self._mark_ad_difference(groups_item, differences, "Groups")
                self.person_table.setItem(row, col, groups_item)
                col += 1

            # Status
            if "Status" in self.visible_columns:
                # The cell shows the short label; the full sentence explaining
                # what the status means is one hover away.
                status_item = QTableWidgetItem(person.ad_status.label)
                status_item.setToolTip(person.ad_status.description)
                colour = STATUS_COLORS.get(person.ad_status)
                if colour is not None:
                    status_item.setBackground(colour)
                self.person_table.setItem(row, col, status_item)
                col += 1
            
            # Enabled
            if "Enabled" in self.visible_columns:
                enabled_item = QTableWidgetItem("✓" if person.account_enabled else "✗")
                enabled_item.setForeground(QColor("#4CAF50") if person.account_enabled else QColor("#EF5350"))
                self.person_table.setItem(row, col, enabled_item)
                col += 1
            
            # Dirty
            if "Dirty" in self.visible_columns:
                dirty_item = QTableWidgetItem("✓" if person.is_dirty() else "")
                self.person_table.setItem(row, col, dirty_item)
                col += 1
            
            # Password policy columns
            if "Must Change Pwd" in self.visible_columns:
                must_change_item = QTableWidgetItem("✓" if person.password_must_change else "")
                self.person_table.setItem(row, col, must_change_item)
                col += 1
            
            if "Cannot Change Pwd" in self.visible_columns:
                cannot_change_item = QTableWidgetItem("✓" if person.password_cannot_change else "")
                self.person_table.setItem(row, col, cannot_change_item)
                col += 1
            
            if "Never Expires" in self.visible_columns:
                never_expires_item = QTableWidgetItem("✓" if person.password_never_expires else "")
                self.person_table.setItem(row, col, never_expires_item)
                col += 1
            
            # Remember which person this row shows.  The table is sortable, so
            # the row index does NOT stay in sync with the order returned by
            # get_all_persons(); looking the person up by index would edit or
            # enable the wrong record after the user sorted a column.
            for column_index in range(self.person_table.columnCount()):
                item = self.person_table.item(row, column_index)
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, person)

            # Actions button
            edit_btn = QPushButton("✏️ Edit")
            edit_btn.clicked.connect(lambda checked, p=person: self.edit_person(p))
            self.person_table.setCellWidget(row, col, edit_btn)
        
        # Auto-resize columns
        self.person_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        
        # But allow manual resize
        for col_idx in range(self.person_table.columnCount()):
            self.person_table.horizontalHeader().setSectionResizeMode(col_idx, QHeaderView.ResizeMode.Interactive)
        
        # Re-enable sorting
        self.person_table.setSortingEnabled(True)
        
    def show_context_menu(self, position):
        """Show context menu for table"""
        menu = QMenu()
        
        edit_action = QAction("✏️ Edit", self)
        edit_action.triggered.connect(lambda: self.edit_selected_person())
        menu.addAction(edit_action)
        
        bulk_edit_action = QAction("✏️ Bulk Edit Selected", self)
        bulk_edit_action.triggered.connect(self.bulk_edit_persons)
        menu.addAction(bulk_edit_action)
        
        menu.addSeparator()
        
        enable_action = QAction("✓ Enable Selected Accounts", self)
        enable_action.triggered.connect(lambda: self.bulk_set_enabled(True))
        menu.addAction(enable_action)
        
        disable_action = QAction("✗ Disable Selected Accounts", self)
        disable_action.triggered.connect(lambda: self.bulk_set_enabled(False))
        menu.addAction(disable_action)
        
        menu.exec(self.person_table.viewport().mapToGlobal(position))
        
    def edit_person(self, person):
        """Open property editor for person"""
        if not self.current_source:
            return
        
        if self.current_source.readonly:
            QMessageBox.warning(self, "Read-Only", "This source is read-only")
            return
        
        # Identity, not equality: Person compares by value, so two students
        # with the same name, class and user name are "==".  Filtering with
        # "!=" dropped the twin's user name from the taken set and the editor
        # then happily handed out a duplicate login.
        existing_usernames = {
            p.ad_username for p in self.current_source.get_all_persons()
            if p.ad_username and p is not person
        }
        
        dialog = PropertyEditorDialog(person, existing_usernames,
                                       available_groups=self.available_groups,
                                       available_templates=self.available_templates,
                                       parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_person_table()
            self.source_manager.notify_source_modified(self.current_source.name)
    
    def _person_at_row(self, row: int):
        """
        Return the person shown in *row*.

        The person object is stored on the row itself, so this keeps working
        after the user sorted the table by any column.
        """
        for column_index in range(self.person_table.columnCount()):
            item = self.person_table.item(row, column_index)
            if item is None:
                continue
            person = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(person, Person):
                return person
        return None

    def edit_selected_person(self):
        """Edit the first selected person"""
        selected_rows = self.person_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select a person first")
            return

        person = self._person_at_row(selected_rows[0].row())
        if person is not None:
            self.edit_person(person)
    
    def bulk_set_enabled(self, enabled: bool):
        """Quickly enable/disable selected accounts"""
        selected_persons = self.get_selected_persons()
        if not selected_persons:
            QMessageBox.warning(self, "No Selection", "Please select persons first")
            return
        
        action = "enable" if enabled else "disable"
        reply = QMessageBox.question(
            self,
            f"Confirm {action.title()}",
            f"{action.title()} {len(selected_persons)} account(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            for person in selected_persons:
                person.account_enabled = enabled
            
            self.refresh_person_table()
            self.source_manager.notify_source_modified(self.current_source.name)
            
            QMessageBox.information(
                self, "Success",
                f"{action.title()}d {len(selected_persons)} account(s)"
            )

    def bulk_edit_persons(self):
        """Show bulk edit dialog"""
        selected_persons = self.get_selected_persons()
        
        if not selected_persons:
            QMessageBox.warning(self, "No Selection", "Please select persons to edit")
            return
        
        if self.current_source and self.current_source.readonly:
            QMessageBox.warning(self, "Read-Only", "This source is read-only")
            return
        
        dialog = BulkEditDialog(selected_persons,
                                 available_groups=self.available_groups,
                                 available_templates=self.available_templates,
                                 parent=self)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # BulkEditDialog.apply_changes() has already written every value to the
        # selected persons and reported the result.  Applying dialog.changes a
        # second time here used to
        #   * generate a *different* password than the one the dialog reported,
        #   * and create bogus attributes on Person (group_action, groups,
        #     home_directory_template, home_drive) because those keys are
        #     instructions, not person fields.
        # So the widget only refreshes the view.
        self.refresh_person_table()
        if self.current_source:
            self.source_manager.notify_source_modified(self.current_source.name)

    def get_selected_persons(self) -> list:
        """
        Get the list of selected persons.

        Persons are read from the rows themselves instead of being looked up by
        index, so the selection stays correct after sorting.
        """
        if not self.current_source:
            return []

        selected_rows = sorted(
            {index.row() for index in self.person_table.selectionModel().selectedRows()}
        )

        persons = []
        for row in selected_rows:
            person = self._person_at_row(row)
            if person is not None:
                persons.append(person)
        return persons
    
    def _prepare_credential_generation(self):
        """
        Validate the preconditions shared by both generation commands.

        Honours the scope chosen next to the buttons: either every person in
        the source, or only the ones selected in the table.

        Returns:
            The list of persons to work on, or ``None`` when the operation
            cannot run (a message has already been shown).
        """
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a source first")
            return None

        if self.current_source.readonly:
            QMessageBox.warning(self, "Read-Only Source",
                                "This source is read-only. Create a copy first.")
            return None

        if self.generate_scope_combo.currentData() == "selected":
            persons = self.get_selected_persons()
            if not persons:
                QMessageBox.warning(
                    self, "No Selection",
                    "The scope is set to 'for selected persons', but nothing is "
                    "selected in the table.\n\nSelect the persons you want, or "
                    "switch the scope to 'for all persons'."
                )
                return None
            return persons

        persons = self.current_source.get_all_persons()
        if not persons:
            QMessageBox.information(self, "No Persons", "No persons found in source")
            return None

        return persons

    def _on_format_chosen(self, index: int) -> None:
        """Open the format editor the user picked, then reset the combo."""
        choice = self.format_combo.itemData(index)
        # Always fall back to the caption entry so the control reads as a menu
        # rather than as a setting that is currently "Password Format".
        self.format_combo.setCurrentIndex(0)

        if choice == "password":
            self.open_password_format()
        elif choice == "username":
            self.open_username_format()
        elif choice == "home":
            self.open_home_directory_templates()

    def open_username_format(self):
        """Open the dialog that defines how generated user names look."""
        dialog = UsernamePolicyDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        policy = dialog.get_policy()
        QMessageBox.information(
            self, "Username Format Saved",
            f"New user names will be generated as:\n\n{policy.describe()}\n\n"
            "Existing user names are not changed. Use 'All Credentials' or the "
            "per-person editor to re-generate them."
        )
        self.refresh_person_table()

    def open_home_directory_templates(self):
        """Open the home directory template manager."""
        dialog = HomeDirectoryTemplateDialog(self)
        dialog.exec()

    @staticmethod
    def _describe_person(person) -> str:
        """Return a readable label for a person, even with missing data."""
        name = f"{person.first_name or ''} {person.last_name or ''}".strip()
        if not name:
            name = "(no name)"
        class_name = person.class_name or "(no class)"
        return f"{name} [{class_name}]"

    def _generate_credentials_for(self, person, existing_usernames: set,
                                  only_missing: bool = False) -> Optional[str]:
        """
        Generate user name, password and display name for a single person.

        A student without a last name (EduPage delivers single-word names) used
        to abort the whole run with
        ``ValueError: Last name cannot be empty``.  Now the person is skipped
        and the reason is reported back to the caller, so every other student
        still gets credentials.

        Args:
            person: The person to process.
            existing_usernames: User names already taken; extended in place.
            only_missing: Fill in the empty fields only and keep every value the
                person already has.  "Generate missing credentials" used to run
                the full generator, which handed a student who merely lacked a
                password a brand new login (and, because their old one is in
                *existing_usernames*, a numbered one at that) - their AD account
                and every path derived from it were orphaned by it.

        Returns:
            ``None`` on success, otherwise the reason the person was skipped.
        """
        first_name = (person.first_name or "").strip()
        last_name = (person.last_name or "").strip()

        needs_username = not (only_missing and person.ad_username)
        needs_password = not (only_missing and person.ad_password)
        needs_display_name = not (only_missing and person.ad_display_name)

        # The name is only a precondition for what is actually derived from it;
        # a password can be generated for a record with a broken name as well.
        if (needs_username or needs_display_name) and (not first_name or not last_name):
            missing = []
            if not first_name:
                missing.append("first name")
            if not last_name:
                missing.append("last name")
            return f"missing {' and '.join(missing)}"

        username = None
        if needs_username:
            try:
                username = generate_username(first_name, last_name, existing_usernames,
                                         class_name=(person.class_name or "").strip())
            except (ValueError, IndexError, RuntimeError) as exc:
                return f"user name could not be generated ({exc})"

        password = None
        if needs_password:
            try:
                password = generate_password()
            except ValueError as exc:
                # A broken password policy affects every person - report it as is
                return f"password could not be generated ({exc})"

        if needs_username:
            person.ad_username = username
            existing_usernames.add(username)
        elif person.ad_username:
            # Kept, not generated - but it is still taken, so it has to stay in
            # the set the next person's name is generated against.  (The only
            # caller that uses only_missing seeds the set up front; this keeps
            # the documented "extended in place" contract true on its own.)
            existing_usernames.add(person.ad_username)
        if needs_password:
            person.ad_password = password

        if needs_display_name:
            class_name = (person.class_name or "").strip()
            if class_name:
                try:
                    person.ad_display_name = generate_display_name(
                        first_name, last_name, class_name
                    )
                except ValueError as exc:            # pragma: no cover - defensive
                    logger.warning("Display name not generated for %s: %s",
                                   self._describe_person(person), exc)
            else:
                # Without a class the "First Last (Class)" form is impossible
                person.ad_display_name = f"{first_name} {last_name}"

        return None

    def _report_generation_result(self, generated: int, skipped: list,
                                  nothing_message: str) -> None:
        """Show a summary of a credential generation run."""
        if skipped:
            preview = "\n".join(f"• {label}: {reason}" for label, reason in skipped[:15])
            if len(skipped) > 15:
                preview += f"\n… and {len(skipped) - 15} more"

            box = QMessageBox(self)
            box.setWindowTitle("Credentials Generated With Warnings"
                               if generated else "No Credentials Generated")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                f"Generated credentials for {generated} student(s).\n"
                f"{len(skipped)} student(s) were skipped."
            )
            box.setInformativeText(
                "Skipped students keep their previous data. Fix the listed "
                "records (for example on the 'Comparison and Sync' tab) and run "
                "the command again."
            )
            box.setDetailedText(preview)
            box.exec()
            return

        if generated:
            QMessageBox.information(self, "Success",
                                    f"Generated credentials for {generated} students")
        else:
            QMessageBox.information(self, "Nothing To Do", nothing_message)

    def generate_all_credentials(self):
        """Generate credentials for all students."""
        persons = self._prepare_credential_generation()
        if persons is None:
            return

        generated = 0
        skipped = []
        existing_usernames = set()

        # AUTOMATIC DIRTY TRACKING - no manual mark_field_dirty() calls needed
        for person in persons:
            reason = self._generate_credentials_for(person, existing_usernames)
            if reason is None:
                generated += 1
            else:
                skipped.append((self._describe_person(person), reason))
                logger.warning("Skipped %s: %s", self._describe_person(person), reason)

        self.refresh_person_table()
        self.source_manager.notify_source_modified(self.current_source.name)

        self._report_generation_result(
            generated, skipped, "No credentials could be generated."
        )
        logger.info("Generated credentials for %d students (%d skipped)",
                    generated, len(skipped))

    def generate_missing_credentials(self):
        """Generate credentials only for students without them."""
        persons = self._prepare_credential_generation()
        if persons is None:
            return

        generated = 0
        skipped = []
        existing_usernames = {p.ad_username for p in persons if p.ad_username}

        # AUTOMATIC DIRTY TRACKING - no manual mark_field_dirty() calls needed
        for person in persons:
            if person.ad_username and person.ad_password:
                continue

            # Fill in the gaps only - an existing login must survive
            reason = self._generate_credentials_for(person, existing_usernames,
                                                    only_missing=True)
            if reason is None:
                generated += 1
            else:
                skipped.append((self._describe_person(person), reason))
                logger.warning("Skipped %s: %s", self._describe_person(person), reason)

        self.refresh_person_table()
        self.source_manager.notify_source_modified(self.current_source.name)

        self._report_generation_result(
            generated, skipped, "All students already have credentials"
        )
        logger.info("Generated missing credentials for %d students (%d skipped)",
                    generated, len(skipped))

    def open_password_format(self):
        """Open the dialog that defines how generated passwords look."""
        dialog = PasswordPolicyDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        policy = dialog.get_policy()
        QMessageBox.information(
            self, "Password Format Saved",
            f"New passwords will be generated as:\n\n{policy.describe()}\n\n"
            "Existing passwords are not changed. Use 'Generate All Credentials' "
            "or the per-person editor to re-generate them."
        )
        # Validation messages depend on the policy - repaint the table
        self.refresh_person_table()
    
    def analyze_source(self):
        """
        Analyse the source and show the result in a detailed, actionable window.

        This used to be a message box, which could only report *how many*
        records had problems - not which ones - and offered no way to act on
        them.
        """
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a source first")
            return

        try:
            source = self.current_source

            # Edge case: source has no get_all_persons method
            if not hasattr(source, 'get_all_persons'):
                QMessageBox.warning(self, "Analysis Error",
                    "The selected source does not support analysis.")
                return

            # Edge case: empty source
            if not source.get_all_persons():
                QMessageBox.information(self, "Source Analysis",
                    "<b>Source Analysis:</b><br><br>"
                    "The source contains no persons.<br><br>"
                    "Ready for sync: ❌ No (empty source)")
                return

            dialog = ADAnalysisDialog(source, self)
            dialog.exec()

            if dialog.changed:
                self.refresh_person_table()
                self.source_manager.notify_source_modified(source.name)

        except AttributeError as e:
            logger.error(f"Attribute error during source analysis: {e}")
            QMessageBox.critical(self, "Analysis Error",
                f"The source structure is invalid or incompatible:\n\n{str(e)}")
        except Exception as e:
            logger.exception("Error during source analysis")
            QMessageBox.critical(self, "Analysis Error",
                f"An unexpected error occurred during analysis:\n\n{str(e)}")

    def discover_in_ad(self):
        """Discover which persons exist in AD"""
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a source first")
            return
        
        server = self.ad_server_input.text().strip()
        base_dn = self.ad_base_input.text().strip()
        username = self.ad_user_input.text().strip()
        password = self.ad_pass_input.text()
        
        if not all([server, base_dn, username, password]):
            QMessageBox.warning(self, "Missing Information", 
                              "Please fill in all AD connection details")
            return
        
        # Clean up any existing thread
        if self.discovery_thread and self.discovery_thread.isRunning():
            # wait() without a timeout blocks the whole GUI until the LDAP
            # request returns, which can take a minute on an unreachable server
            self.discovery_thread.stop()
            if not self.discovery_thread.wait(5000):
                logger.warning("Previous AD discovery did not stop within 5 seconds")
        
        # Show progress
        progress = QProgressDialog("Discovering in AD...", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        
        # Start discovery thread
        self.discovery_thread = ADDiscoveryThread(
            self.current_source, server, base_dn, username, password,
            self.get_connection_options(), self.get_search_scope()
        )
        self.discovery_thread.discovery_finished.connect(
            lambda s, r, m: self.on_discovery_finished(s, r, m, progress)
        )
        progress.canceled.connect(self.discovery_thread.stop)
        self.discovery_thread.start()
    
    def on_discovery_finished(self, success, results, message, progress):
        """Handle discovery completion"""
        progress.close()
        
        if success:
            # Update table
            self.refresh_person_table()
            if self.current_source:
                self.source_manager.notify_source_modified(self.current_source.name)
            
            QMessageBox.information(self, "Discovery Complete", message)
        else:
            QMessageBox.critical(self, "Discovery Failed", message)
            
    def open_group_management(self):
        """Open group management dialog"""
       
    
        # Get credentials (may be empty strings)
        server = self.ad_server_input.text().strip()
        username = self.ad_user_input.text().strip()
        password = self.ad_pass_input.text()
        base_dn = self.ad_base_input.text().strip()
    
        # Show dialog with factory function (handles warnings)
        dialog = show_group_management_dialog(
            ad_server=server,
            ad_username=username,
            ad_password=password,
            base_dn=base_dn,
            initial_groups=self.available_groups,
            initial_templates=self.available_templates,
            parent=self
        )
    
        if dialog:
            dialog.exec()
        
            # Get updated groups/templates if needed
            self.available_groups = dialog.get_groups()
            self.available_templates = dialog.get_templates()

    def synchronize(self):
        """Start synchronization process with auto-discovery"""
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a source first")
            return
        
        server = self.ad_server_input.text().strip()
        base_dn = self.ad_base_input.text().strip()
        username = self.ad_user_input.text().strip()
        password = self.ad_pass_input.text()
        
        if not all([server, base_dn, username, password]):
            QMessageBox.warning(self, "Missing Information", 
                              "Please fill in all AD connection details")
            return
        
        # First, run discovery automatically
        reply = QMessageBox.question(
            self,
            "Auto-Discovery",
            "Before synchronizing, the system will discover current AD state.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.No:
            return
        
        # Clean up any existing thread
        if self.discovery_thread and self.discovery_thread.isRunning():
            # wait() without a timeout blocks the whole GUI until the LDAP
            # request returns, which can take a minute on an unreachable server
            self.discovery_thread.stop()
            if not self.discovery_thread.wait(5000):
                logger.warning("Previous AD discovery did not stop within 5 seconds")
        
        # Run discovery
        progress = QProgressDialog("Discovering in AD...", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        
        self.discovery_thread = ADDiscoveryThread(
            self.current_source, server, base_dn, username, password,
            self.get_connection_options(), self.get_search_scope()
        )
        self.discovery_thread.discovery_finished.connect(
            lambda s, r, m: self.on_discovery_before_sync_finished(s, r, m, progress, 
                                                                   server, base_dn, username, password)
        )
        progress.canceled.connect(self.discovery_thread.stop)
        self.discovery_thread.start()
        
    def on_discovery_before_sync_finished(self, success, results, message, progress, 
                                         server, base_dn, username, password):
        """Handle discovery completion before sync"""
        progress.close()
        
        if not success:
            QMessageBox.critical(self, "Discovery Failed", 
                               f"Cannot proceed with sync:\n{message}")
            return
        
        # Update table
        self.refresh_person_table()
        
        # Continue with sync
        self.execute_sync(server, base_dn, username, password)
    
    def execute_sync(self, server, base_dn, username, password):
        """Execute the actual synchronization (planning on UI thread, execution async)"""
        from utils.ad_sync_task import ADPlanTask, ADSyncTask
        from utils.progress_dialog import ProgressDialog

        # Step 1: Connect to AD and build sync plan asynchronously
        plan_task = ADPlanTask(self.current_source, server, base_dn, username, password,
                               self.get_connection_options())
        plan_dialog = ProgressDialog(plan_task, self)
        plan_dialog.start_task()
        plan_dialog.exec()

        if not plan_task.sync_plan:
            QMessageBox.critical(self, "Planning Failed",
                                 "Could not connect to Active Directory or build sync plan.")
            plan_task.close_client()
            return

        sync_plan = plan_task.sync_plan
        client = plan_task.client

        try:
            # Step 2: Show review dialog (UI)
            review_dialog = SyncReviewDialog(sync_plan, self)
            if review_dialog.exec() != QDialog.DialogCode.Accepted:
                return

            # Step 3: Handle conflicts (UI)
            user_conflict_choices = {}
            if sync_plan.statistics.get('conflicts', 0) > 0:
                for op in sync_plan.update_operations:
                    if op.has_conflict and op.conflict_info:
                        conflict_dialog = ConflictResolutionDialog(op.conflict_info, self)
                        if conflict_dialog.exec() == QDialog.DialogCode.Accepted:
                            person_id = str(id(op.person))
                            user_conflict_choices[person_id] = conflict_dialog.user_choices
                        else:
                            QMessageBox.information(self, "Sync Cancelled",
                                                    "Synchronization cancelled during conflict resolution")
                            return

            # Step 4: Execute sync asynchronously
            resolver = ConflictResolver()
            sync_service = PersonsSyncService(client, resolver)

            sync_task = ADSyncTask(sync_service, sync_plan, ConflictStrategy.USER_PROMPT,
                                   user_conflict_choices)
            sync_progress = ProgressDialog(sync_task, self)
            sync_progress.start_task()
            sync_progress.exec()

            result = sync_task.result
            if result is None:
                QMessageBox.warning(self, "Sync Incomplete", "Synchronization did not complete.")
                return

            # Step 5: Show detailed results
            stats = result.statistics

            partial_success_items = [
                op for op in result.successful
                if op.message and ("but" in op.message.lower() or "failed" in op.message.lower()
                                   or "warning" in op.message.lower())
            ]

            msg = (
                f"<b>Synchronization complete!</b><br><br>"
                f"✅ Successful: <b>{stats['successful']}</b><br>"
                f"❌ Failed: <b>{stats['failed']}</b><br>"
                f"⏭ Skipped: <b>{stats['skipped']}</b><br>"
                f"⏱ Duration: {result.duration:.2f}s"
            )

            if partial_success_items:
                msg += f"<br><br>⚠ <b>Partial successes ({len(partial_success_items)}):</b><br>"
                msg += "<i>(operations that completed partially — e.g., user created but password could not be set)</i><br><br>"
                for op in partial_success_items[:10]:
                    person_name = f"{op.person.first_name} {op.person.last_name}" if op.person else "Unknown"
                    msg += f"• <b>{person_name}</b>: {op.message}<br>"
                if len(partial_success_items) > 10:
                    msg += f"<i>...and {len(partial_success_items) - 10} more</i><br>"

            if result.failed:
                msg += "<br><b>Failed operations:</b><br>"
                for op in result.failed[:5]:
                    person_name = f"{op.person.first_name} {op.person.last_name}" if op.person else "Unknown"
                    msg += f"• <b>{person_name}</b>: {op.message}<br>"
                if len(result.failed) > 5:
                    msg += f"<i>...and {len(result.failed) - 5} more</i><br>"

            from PyQt6.QtWidgets import QTextBrowser
            result_dialog = QDialog(self)
            result_dialog.setWindowTitle("Sync Complete")
            result_dialog.setMinimumSize(500, 350)
            result_dialog_layout = QVBoxLayout(result_dialog)
            browser = QTextBrowser()
            browser.setHtml(msg)
            result_dialog_layout.addWidget(browser)
            ok_btn = QPushButton("OK")
            ok_btn.clicked.connect(result_dialog.accept)
            result_dialog_layout.addWidget(ok_btn)
            result_dialog.exec()

            self.refresh_person_table()
            if self.current_source:
                self.source_manager.notify_source_modified(self.current_source.name)

        except Exception as e:
            logger.exception("Error during synchronization")
            QMessageBox.critical(self, "Sync Error", f"Synchronization failed: {str(e)}")
        finally:
            plan_task.close_client()

    def closeEvent(self, event):
        """Handle widget close event"""
        # Clean up discovery thread if running
        if self.discovery_thread and self.discovery_thread.isRunning():
            # wait() without a timeout blocks the whole GUI until the LDAP
            # request returns, which can take a minute on an unreachable server
            self.discovery_thread.stop()
            if not self.discovery_thread.wait(5000):
                logger.warning("Previous AD discovery did not stop within 5 seconds")
        event.accept()

                                                      