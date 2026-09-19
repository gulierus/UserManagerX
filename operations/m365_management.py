"""
Microsoft 365 Management
========================

Version 26, point 21 e.  The counterpart of "Active Directory Management", and
deliberately built to look and behave like it: a connection panel, an
operations panel, and a table of people with an Edit button on every row.

What is different, and why
--------------------------
The table shows **Microsoft 365** fields.  It does not show the Active
Directory display name, the home directory or "Cannot Change Pwd", because
those live in the other directory and a person can be in both with different
values - showing them here would invite exactly the confusion this separation
exists to prevent.

The synchronisation maps a class onto a **group**, not onto an organisational
unit, and the group name is a template the user controls.  A team is attached
only when the user switches it on.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from models import Source
from models_m365 import M365Status
from services.graph_compat import GRAPH_AVAILABLE, graph_unavailable_message
from services.m365_auth import M365Credentials
from services.m365_services import (
    DEFAULT_DISPLAY_NAME_TEMPLATE, DEFAULT_GROUP_TEMPLATE, DEFAULT_UPN_TEMPLATE,
    M365SyncConfig,
)
from ui.column_management_dialog import ColumnVisibilityDialog
from ui.m365_connection_widget import M365ConnectionWidget

logger = logging.getLogger(__name__)

#: Background colour of the "Status" cell, per status.  The same three tones as
#: the Active Directory table: green = nothing to do, amber = something
#: outstanding, red = a problem.
STATUS_COLORS = {
    M365Status.MATCHES_M365: QColor("#4CAF50"),
    M365Status.SYNC_SUCCEEDED: QColor("#4CAF50"),
    M365Status.DIFFERS_FROM_M365: QColor("#FFA726"),
    M365Status.SYNC_INCOMPLETE: QColor("#FF7043"),
    M365Status.NOT_FOUND_IN_M365: QColor("#EF5350"),
    M365Status.MULTIPLE_M365_MATCHES: QColor("#AB47BC"),
}


class M365ManagementWidget(QWidget):
    """The "Microsoft 365 Management" operation."""

    #: Every column the table can show.
    #:
    #: Only Microsoft 365 fields: the Active Directory display name, the home
    #: directory and "Cannot Change Pwd" belong to the other operation.
    ALL_COLUMNS = [
        "Name", "Class", "Sign-in name", "Display name", "Alias", "Password",
        "Usage location", "Group", "Status", "Enabled", "Dirty",
    ]

    #: Shown until the user chooses otherwise.
    DEFAULT_COLUMNS = ["Name", "Class", "Sign-in name", "Status", "Enabled",
                       "Dirty"]

    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.current_source: Optional[Source] = None
        self.visible_columns = list(self.DEFAULT_COLUMNS)

        #: Set once a connection has been made, so later operations can reuse
        #: the credentials without asking again.  Never holds a secret beyond
        #: what the user typed into the panel.
        self._credentials: Optional[M365Credentials] = None
        self._tenant_domain: str = ""

        self.init_ui()

    # -- construction -----------------------------------------------------

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        title = QLabel("Microsoft 365 Management")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        self._build_connection_panel(layout)
        self._build_operations_panel(layout)
        self._build_table(layout)

        if not GRAPH_AVAILABLE:
            self._disable_with_reason(graph_unavailable_message())

    def _build_connection_panel(self, layout: QVBoxLayout) -> None:
        """The sign-in panel, shared with the "from web" source."""
        self.connection = M365ConnectionWidget()

        connection_layout = self.connection.layout()

        row = QHBoxLayout()
        self.connect_button = QPushButton("🔌 Connect")
        self.connect_button.setToolTip(
            "Sign in and check that the credentials and the permissions work.")
        self.connect_button.clicked.connect(self.connect_to_m365)
        row.addWidget(self.connect_button)

        self.connection_status = QLabel("Not connected")
        self.connection_status.setStyleSheet("color: #888; font-size: 11px;")
        row.addWidget(self.connection_status, stretch=1)
        connection_layout.addLayout(row)

        self.connection_group = self.connection
        layout.addWidget(self.connection)

    def _build_operations_panel(self, layout: QVBoxLayout) -> None:
        """The buttons and the settings that drive them."""
        group = QGroupBox("Operations")
        group_layout = QVBoxLayout(group)

        buttons = QHBoxLayout()

        discover = QPushButton("🔍 Discover in Microsoft 365")
        discover.setToolTip(
            "Look every person up by their sign-in name and record what "
            "Microsoft 365 holds for them.")
        discover.clicked.connect(self.discover_in_m365)
        buttons.addWidget(discover)

        bulk_edit = QPushButton("✏️ Bulk Edit...")
        bulk_edit.setToolTip(
            "Set Microsoft 365 fields - including the password - for several "
            "people at once.")
        bulk_edit.clicked.connect(self.bulk_edit)
        buttons.addWidget(bulk_edit)

        self.format_combo = QComboBox()
        self.format_combo.addItem("⚙ Formats…", None)
        self.format_combo.addItem("✉ Sign-in name format…", "upn")
        self.format_combo.addItem("🏷 Display name format…", "display_name")
        self.format_combo.addItem("🔐 Password format…", "password")
        self.format_combo.addItem("👥 Group name format…", "group")
        self.format_combo.setToolTip(
            "Define how the sign-in name, the display name, the password and "
            "the group name are built. All of them support placeholders.")
        self.format_combo.activated.connect(self._on_format_chosen)
        buttons.addWidget(self.format_combo)

        self.sync_button = QPushButton("🔄 Synchronize")
        self.sync_button.setStyleSheet(
            "background-color: #4CAF50; font-weight: bold;")
        self.sync_button.setToolTip(
            "Create the missing accounts and groups, and bring the existing "
            "ones into line.")
        self.sync_button.clicked.connect(self.synchronize)
        buttons.addWidget(self.sync_button)
        buttons.addStretch()
        group_layout.addLayout(buttons)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.owners_input = QLineEdit()
        self.owners_input.setPlaceholderText(
            "teacher@skola.cz, head@skola.cz — separated by a comma")
        self.owners_input.setToolTip(
            "Who owns the groups this application creates.\n"
            "App-only sign-in has no signed-in user to become the owner, so "
            "without this the group is created ownerless and nobody can "
            "manage it in the portal.\n"
            "A team cannot be created on an ownerless group at all."
        )
        form.addRow("Group owners:", self.owners_input)
        group_layout.addLayout(form)

        self.create_teams_check = QCheckBox(
            "Also create a Microsoft Team for each group")
        self.create_teams_check.setToolTip(
            "Off by default. A team is never created automatically — only "
            "when this is switched on.\n"
            "A team needs the group to have at least one owner."
        )
        group_layout.addWidget(self.create_teams_check)

        self.operations_group = group
        layout.addWidget(group)

    def _build_table(self, layout: QVBoxLayout) -> None:
        """The person table and its controls."""
        controls = QHBoxLayout()

        columns_button = QPushButton("📋 Columns...")
        columns_button.setToolTip("Choose which columns are visible")
        columns_button.clicked.connect(self.select_columns)
        controls.addWidget(columns_button)

        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: #888; font-size: 11px;")
        controls.addWidget(self.summary_label)
        controls.addStretch()
        layout.addLayout(controls)

        self.person_table = QTableWidget()
        self.person_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.person_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.person_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.person_table.setAlternatingRowColors(True)
        self.person_table.setSortingEnabled(True)
        layout.addWidget(self.person_table, stretch=1)

    def _disable_with_reason(self, reason: str) -> None:
        """Switch the operation off and say why."""
        self.connect_button.setEnabled(False)
        self.sync_button.setEnabled(False)
        self.connection_status.setStyleSheet("color: #e6a93c; font-size: 11px;")
        self.connection_status.setText(reason)

    # -- source -----------------------------------------------------------

    def set_source(self, source) -> None:
        """Show a different source."""
        self.current_source = source
        self.refresh_person_table()

    def on_source_changed_external(self, *_args) -> None:
        """A source was added or removed elsewhere."""
        if self.current_source:
            self.refresh_person_table()

    def on_source_modified_external(self, source_name: str) -> None:
        """This source was edited elsewhere."""
        if self.current_source and self.current_source.name == source_name:
            self.refresh_person_table()

    def persons(self) -> List:
        """Everyone in the current source, or an empty list."""
        if not self.current_source:
            return []
        return self.current_source.get_all_persons()

    def selected_persons(self) -> List:
        """
        The people whose rows are selected, or everybody when none are.

        The same rule the Active Directory table uses, so a command means the
        same thing in both.
        """
        rows = {index.row() for index in
                self.person_table.selectionModel().selectedRows()}
        everybody = self.persons()
        if not rows:
            return everybody
        return [everybody[row] for row in sorted(rows) if row < len(everybody)]

    # -- the table --------------------------------------------------------

    def select_columns(self) -> None:
        """Let the user choose which columns are visible."""
        dialog = ColumnVisibilityDialog(self.ALL_COLUMNS, self.visible_columns,
                                        self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.get_visible_columns()
        if not chosen:
            QMessageBox.warning(self, "No Columns",
                                "At least one column must be visible")
            return
        self.visible_columns = chosen
        self.refresh_person_table()

    def _cell_for(self, person, column: str) -> QTableWidgetItem:
        """Build one cell of the table."""
        if column == "Name":
            return QTableWidgetItem(
                f"{person.first_name} {person.last_name}")
        if column == "Class":
            return QTableWidgetItem(person.class_name)
        if column == "Sign-in name":
            return QTableWidgetItem(
                person.m365_user_principal_name or "(not set)")
        if column == "Display name":
            return QTableWidgetItem(person.m365_display_name or "(not set)")
        if column == "Alias":
            return QTableWidgetItem(person.m365_mail_nickname or "(not set)")
        if column == "Password":
            # Never the password itself: the table is the thing people take
            # screenshots of.
            return QTableWidgetItem("•" * 8 if person.m365_password
                                    else "(not set)")
        if column == "Usage location":
            return QTableWidgetItem(person.m365_usage_location or "(not set)")
        if column == "Group":
            return QTableWidgetItem(self._group_name_for(person))
        if column == "Status":
            item = QTableWidgetItem(person.m365_status.label)
            item.setToolTip(person.m365_status.description)
            colour = STATUS_COLORS.get(person.m365_status)
            if colour is not None:
                item.setBackground(colour)
            return item
        if column == "Enabled":
            item = QTableWidgetItem("✓" if person.account_enabled else "✗")
            item.setForeground(QColor("#4CAF50") if person.account_enabled
                               else QColor("#EF5350"))
            return item
        if column == "Dirty":
            item = QTableWidgetItem("✓" if person.is_dirty() else "")
            if person.is_dirty():
                item.setToolTip(
                    "Edited since the last synchronisation: "
                    + ", ".join(sorted(person.get_dirty_fields())))
            return item
        return QTableWidgetItem("")

    def _group_name_for(self, person) -> str:
        """
        The group this person's class would map to.

        Rendered with the current template rather than stored, so changing the
        template updates the column immediately.
        """
        from utils.name_templates import TemplateError, class_values, render

        if not self.current_source:
            return ""
        for school_class in self.current_source.classes:
            if school_class.name != person.class_name:
                continue
            try:
                return render(self.group_template, class_values(school_class))
            except TemplateError:
                return "(template error)"
        return ""

    def refresh_person_table(self) -> None:
        """Redraw the table from the current source."""
        # Remove the Edit buttons before clearing: removeCellWidget only
        # schedules the destruction, so a burst of repaints piles them up and
        # each one keeps its captured person alive.
        for row in range(self.person_table.rowCount()):
            for column in range(self.person_table.columnCount()):
                widget = self.person_table.cellWidget(row, column)
                if widget is not None:
                    self.person_table.removeCellWidget(row, column)
                    widget.setParent(None)
                    widget.deleteLater()

        self.person_table.clear()
        self.person_table.setSortingEnabled(False)

        people = self.persons()
        if not people:
            self.person_table.setRowCount(0)
            self.person_table.setColumnCount(0)
            self.summary_label.setText("")
            return

        headers = self.visible_columns + ["Actions"]
        self.person_table.setColumnCount(len(headers))
        self.person_table.setHorizontalHeaderLabels(headers)
        self.person_table.setRowCount(len(people))

        for row, person in enumerate(people):
            for column_index, column in enumerate(self.visible_columns):
                self.person_table.setItem(row, column_index,
                                          self._cell_for(person, column))

            edit_button = QPushButton("Edit")
            edit_button.setToolTip(
                "Edit every Microsoft 365 field of this person")
            edit_button.clicked.connect(
                lambda _checked=False, chosen=person: self.edit_person(chosen))
            self.person_table.setCellWidget(row, len(self.visible_columns),
                                            edit_button)

        self.person_table.setSortingEnabled(True)
        header = self.person_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.person_table.resizeColumnsToContents()

        self._update_summary(people)

    def _update_summary(self, people: List) -> None:
        """Say how many people are in which state."""
        counts: Dict[M365Status, int] = {}
        for person in people:
            counts[person.m365_status] = counts.get(person.m365_status, 0) + 1
        parts = [f"{count} {status.label.lower()}"
                 for status, count in sorted(counts.items(),
                                             key=lambda item: item[0].value)]
        self.summary_label.setText(f"{len(people)} person(s) · "
                                   + ", ".join(parts))

    # -- settings ---------------------------------------------------------

    @property
    def group_template(self) -> str:
        """The current class-to-group name template."""
        return getattr(self, "_group_template", DEFAULT_GROUP_TEMPLATE)

    @property
    def upn_template(self) -> str:
        """The current sign-in name template."""
        return getattr(self, "_upn_template", DEFAULT_UPN_TEMPLATE)

    @property
    def display_name_template(self) -> str:
        """The current display name template."""
        return getattr(self, "_display_name_template",
                       DEFAULT_DISPLAY_NAME_TEMPLATE)

    def sync_config(self) -> M365SyncConfig:
        """Everything the user has chosen, as one object."""
        import re

        owners = [part.strip() for part in
                  re.split(r'[;,]', self.owners_input.text()) if part.strip()]

        return M365SyncConfig(
            group_name_template=self.group_template,
            upn_template=self.upn_template,
            display_name_template=self.display_name_template,
            domain=self._tenant_domain,
            create_teams=self.create_teams_check.isChecked(),
            owner_user_principal_names=owners,
        )

    def _on_format_chosen(self, index: int) -> None:
        """Open the settings window the combo names, then reset it."""
        what = self.format_combo.itemData(index)
        self.format_combo.setCurrentIndex(0)
        if what is None:
            return

        if what == "password":
            self._open_password_format()
            return
        self._open_name_format(what)

    def _open_password_format(self) -> None:
        """Reuse the application's own password policy dialog."""
        from ui.password_policy_dialog import PasswordPolicyDialog

        dialog = PasswordPolicyDialog(self)
        dialog.exec()

    def _open_name_format(self, what: str) -> None:
        """Open the template editor for one of the three name formats."""
        from ui.m365_format_dialog import M365NameFormatDialog

        settings = {
            'upn': ("Sign-in Name Format", self.upn_template, "person",
                    "The sign-in name a new account is created with, for "
                    "example novakj@skola.cz."),
            'display_name': ("Display Name Format", self.display_name_template,
                             "person",
                             "The name Microsoft 365 shows for the person."),
            'group': ("Group Name Format", self.group_template, "class",
                      "The Microsoft 365 group each class is mapped onto."),
        }[what]

        title, current, kind, explanation = settings
        dialog = M365NameFormatDialog(title, current, kind, explanation,
                                      self._tenant_domain, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        template = dialog.template()
        if what == 'upn':
            self._upn_template = template
        elif what == 'display_name':
            self._display_name_template = template
        else:
            self._group_template = template
        self.refresh_person_table()

    # -- actions ----------------------------------------------------------

    def _require_credentials(self) -> Optional[M365Credentials]:
        """
        The credentials to work with, or ``None`` after telling the user why.

        The panel is checked before anything is sent, so a missing field comes
        back as a sentence rather than a Microsoft error code.
        """
        credentials = self.connection.credentials()
        problems = credentials.problems()
        if problems:
            QMessageBox.warning(self, "Missing Information",
                                "\n".join(problems))
            return None
        return credentials

    def _require_source(self) -> bool:
        """Say so when there is nothing to work on."""
        if self.current_source and self.persons():
            return True
        QMessageBox.warning(
            self, "No Source",
            "Select a source with at least one person first.")
        return False

    def connect_to_m365(self) -> None:
        """Sign in and remember the tenant, so the templates can use it."""
        from utils.m365_tasks import M365ConnectTask
        from utils.progress_dialog import ProgressDialog

        credentials = self._require_credentials()
        if credentials is None:
            return

        task = M365ConnectTask(credentials)
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

        client = getattr(task, "client", None)
        if client is None:
            self.connection_status.setText("Not connected")
            return

        # The client belongs to the worker thread, which has finished; only
        # the facts it learned are of use here.
        self._credentials = credentials
        self._tenant_domain = task.tenant_domain or ""
        who = task.signed_in_as or "the application"
        where = task.tenant_domain or task.tenant_id or "Microsoft 365"
        self.connection_status.setStyleSheet(
            "color: #4CAF50; font-size: 11px;")
        self.connection_status.setText(f"Connected to {where} as {who}")
        client.close()
        task.client = None

    def discover_in_m365(self) -> None:
        """Look everybody up and record what Microsoft 365 holds."""
        from utils.m365_tasks import M365DiscoverTask
        from utils.progress_dialog import ProgressDialog

        if not self._require_source():
            return
        credentials = self._credentials or self._require_credentials()
        if credentials is None:
            return

        task = M365DiscoverTask(credentials, self.persons(),
                                self.sync_config())
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

        self.refresh_person_table()

        if task.errors:
            QMessageBox.warning(
                self, "Some People Could Not Be Looked Up",
                "These people were left as 'Not checked' rather than being "
                "reported as missing, so synchronising cannot create a "
                "duplicate for them:\n\n" + "\n".join(task.errors[:10])
                + ("\n..." if len(task.errors) > 10 else "")
            )

    def bulk_edit(self) -> None:
        """Set Microsoft 365 fields for the selected people."""
        from ui.m365_bulk_edit_dialog import M365BulkEditDialog

        if not self._require_source():
            return

        chosen = self.selected_persons()
        dialog = M365BulkEditDialog(chosen, self.sync_config(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_person_table()

    def edit_person(self, person) -> None:
        """Edit one person's Microsoft 365 fields."""
        from ui.m365_person_dialog import M365PersonDialog

        dialog = M365PersonDialog(person, self.sync_config(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_person_table()
            if self.current_source:
                self.source_manager.notify_source_modified(
                    self.current_source.name)

    def synchronize(self) -> None:
        """Create the missing accounts and groups, then bring them into line."""
        from utils.m365_tasks import M365SyncTask
        from utils.progress_dialog import ProgressDialog

        if not self._require_source():
            return
        credentials = self._credentials or self._require_credentials()
        if credentials is None:
            return

        config = self.sync_config()
        problems = config.problems()
        if problems:
            QMessageBox.warning(self, "Check the Settings",
                                "\n".join(problems))
            return

        task = M365SyncTask(credentials, self.current_source, config)
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

        self.refresh_person_table()
        if self.current_source:
            self.source_manager.notify_source_modified(
                self.current_source.name)

        result = task.sync_result
        if result is None:
            return

        if result.succeeded:
            QMessageBox.information(self, "Synchronisation Complete",
                                    result.summary())
        else:
            QMessageBox.warning(
                self, "Synchronisation Finished With Problems",
                result.summary() + "\n\n" + "\n".join(result.failed[:10])
                + ("\n..." if len(result.failed) > 10 else "")
            )
