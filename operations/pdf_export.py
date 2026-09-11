"""
PDF Export Widget - Advanced Table Export with Encryption
Main widget for managing table data and PDF export operations
VERSION 2 - UI improvements and warning dialogs
"""

import logging
from typing import List, Dict, Any, Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QCheckBox, QListWidget, QMessageBox, QMenu, QInputDialog,
    QAbstractItemView, QSplitter
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QAction

from models import Source
from ui.column_management_dialog import ColumnManagementDialog
from ui.bulk_edit_table_dialog import BulkEditTableDialog
from ui.pdf_export_dialog import PDFExportDialog
from ui.pdf_viewer_window import PDFViewerWindow

logger = logging.getLogger(__name__)


class PDFExportWidget(QWidget):
    """
    Widget for advanced table-based PDF export
    
    Features:
    - Interactive table with source data
    - Column visibility management
    - Custom column addition/removal
    - Bulk editing capabilities
    - Multiple encryption methods (pikepdf, AES-GCM, GPG)
    - Advanced PDF generation with full customization
    - Encrypted PDF viewer
    """
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.current_source: Optional[Source] = None
        self.selected_classes: List[str] = []
        
        # Table data management
        self.table_data: List[Dict[str, Any]] = []
        self.column_headers: List[str] = []
        self.visible_columns: List[str] = []
        self.custom_columns: List[str] = []  # Track user-added columns
        
        # Saved settings for PDF export dialog
        self.saved_export_settings: Dict[str, Any] = {}
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Compact title and description
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        
        title = QLabel("<b>PDF Table Export</b>")
        title.setStyleSheet("font-size: 13px;")
        header_layout.addWidget(title)
        
        desc = QLabel("Create and export customizable tables to encrypted PDF files.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; font-size: 10px;")
        header_layout.addWidget(desc)
        
        layout.addLayout(header_layout)
        
        # Create splitter for class selection and main content
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left panel - Class selection
        class_panel = self._create_class_selection_panel()
        splitter.addWidget(class_panel)
        
        # Right panel - Table and controls
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # Table controls
        table_controls = self._create_table_controls()
        right_layout.addLayout(table_controls)
        
        # Table
        self.table = self._create_table()
        right_layout.addWidget(self.table)
        
        # Export controls
        export_controls = self._create_export_controls()
        right_layout.addLayout(export_controls)
        
        splitter.addWidget(right_panel)
        
        # Set splitter sizes (20% for classes, 80% for table)
        splitter.setSizes([200, 800])
        
        layout.addWidget(splitter)
        layout.setStretch(1, 1)
        
    def _create_class_selection_panel(self) -> QWidget:
        """Create class selection panel"""
        panel = QGroupBox("Select Classes")
        layout = QVBoxLayout(panel)
        
        info = QLabel("Select classes to include:")
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 10px; color: #aaa;")
        layout.addWidget(info)
        
        self.class_list = QListWidget()
        self.class_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.class_list)
        
        # Select all/none buttons
        btn_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self.select_all_classes)
        btn_layout.addWidget(select_all_btn)
        
        select_none_btn = QPushButton("Deselect All")
        select_none_btn.clicked.connect(self.select_no_classes)
        btn_layout.addWidget(select_none_btn)
        
        layout.addLayout(btn_layout)
        
        # Load button
        load_btn = QPushButton("🔄 Load Selected Classes")
        load_btn.setStyleSheet("background-color: #2196F3; font-weight: bold;")
        load_btn.clicked.connect(self.load_table_from_source)
        layout.addWidget(load_btn)
        
        return panel
        
    def _create_table_controls(self) -> QHBoxLayout:
        """Create table control buttons"""
        controls = QHBoxLayout()
        
        controls.addWidget(QLabel("<b>Table Data:</b>"))
        
        # Column management
        columns_btn = QPushButton("📋 Manage Columns...")
        columns_btn.setToolTip("Show/hide columns, add custom columns")
        columns_btn.clicked.connect(self.manage_columns)
        controls.addWidget(columns_btn)
        
        # Bulk edit
        bulk_edit_btn = QPushButton("✏️ Bulk Edit...")
        bulk_edit_btn.setToolTip("Edit multiple rows at once")
        bulk_edit_btn.clicked.connect(self.bulk_edit_rows)
        controls.addWidget(bulk_edit_btn)
        
        # Clear table
        clear_btn = QPushButton("🗑️ Clear Table")
        clear_btn.clicked.connect(self.clear_table)
        controls.addWidget(clear_btn)
        
        controls.addStretch()
        
        # Row count label
        self.row_count_label = QLabel("Rows: 0")
        self.row_count_label.setStyleSheet("color: #aaa;")
        controls.addWidget(self.row_count_label)
        
        return controls
        
    def _create_table(self) -> QTableWidget:
        """Create the main data table"""
        table = QTableWidget()
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self.show_table_context_menu)
        

        # Enable editing
        table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | 
                             QTableWidget.EditTrigger.EditKeyPressed)
        
        # Style
        table.setStyleSheet("""
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
        
        # Allow column resizing
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        
        # Context menu for header
        table.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.horizontalHeader().customContextMenuRequested.connect(self.show_header_context_menu)
        
        return table
        
    def _create_export_controls(self) -> QHBoxLayout:
        """Create export control buttons"""
        controls = QHBoxLayout()
        
        # Export button (unified preview + export)
        export_btn = QPushButton("📄 Export to PDF...")
        export_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        export_btn.setMinimumHeight(35)
        export_btn.clicked.connect(self.export_to_pdf)
        controls.addWidget(export_btn)
        
        # View encrypted PDF button
        view_btn = QPushButton("🔓 View Encrypted PDF...")
        view_btn.setMinimumHeight(35)
        view_btn.clicked.connect(self.view_encrypted_pdf)
        controls.addWidget(view_btn)
        
        controls.addStretch()
        
        return controls
        
    def set_source(self, source: Optional[Source]):
        """Set the current data source"""
        self.current_source = source
        self.populate_class_list()
        
    def populate_class_list(self):
        """Populate the class selection list"""
        self.class_list.clear()
        
        if not self.current_source:
            return
        
        for cls in self.current_source.classes:
            self.class_list.addItem(cls.name)
            
    def select_all_classes(self):
        """Select all classes in the list"""
        for i in range(self.class_list.count()):
            self.class_list.item(i).setSelected(True)
            
    def select_no_classes(self):
        """Deselect all classes"""
        self.class_list.clearSelection()
        
    def load_table_from_source(self):
        """Load data from source into table based on selected classes"""
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a data source first")
            return
        
        # Get selected classes
        selected_items = self.class_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Classes", "Please select at least one class")
            return
        
        # Warning if table has data
        if self.table_data:
            reply = QMessageBox.question(
                self, "Clear Table Data",
                "Loading new data will clear the current table.\n\n"
                "All unsaved changes will be lost. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.No:
                return
        
        self.selected_classes = [item.text() for item in selected_items]
        
        # Clear existing data
        self.table_data = []
        self.column_headers = []
        self.visible_columns = []
        self.custom_columns = []
        
        # Collect data from selected classes
        for cls in self.current_source.classes:
            if cls.name in self.selected_classes:
                for person in cls.persons:
                    row_data = self._person_to_dict(person)
                    self.table_data.append(row_data)
        
        if not self.table_data:
            QMessageBox.information(self, "No Data", "Selected classes contain no persons")
            return
        
        # Determine columns from first row
        if self.table_data:
            self.column_headers = list(self.table_data[0].keys())
            self.visible_columns = self.column_headers.copy()
        
        # Populate table
        self._populate_table()
        
        logger.info(f"Loaded {len(self.table_data)} rows from {len(self.selected_classes)} classes")
        
    def _person_to_dict(self, person) -> Dict[str, Any]:
        """Convert Person object to dictionary with string values"""
        return {
            'First Name': str(person.first_name),
            'Last Name': str(person.last_name),
            'Class': str(person.class_name),
            'Username': str(person.ad_username or ''),
            'Password': str(person.ad_password or ''),
            'Email': str(person.ad_email or ''),
            'Display Name': str(person.ad_display_name or ''),
            'Enabled': 'Yes' if person.account_enabled else 'No',
            'Must Change Password': 'Yes' if person.password_must_change else 'No',
            'Cannot Change Password': 'Yes' if person.password_cannot_change else 'No',
            'Password Never Expires': 'Yes' if person.password_never_expires else 'No',
            'AD Status': str(person.ad_status.value),
        }
        
    def _populate_table(self):
        """Populate table with current data"""
        self.table.setSortingEnabled(False)
        self.table.clear()
        
        # Set up columns
        self.table.setColumnCount(len(self.visible_columns))
        self.table.setHorizontalHeaderLabels(self.visible_columns)
        
        # Set up rows
        self.table.setRowCount(len(self.table_data))
        
        # Populate cells.  Every item remembers WHICH record it shows, because
        # the table is sortable: after the user sorts a column, the visual row
        # number no longer matches the position in self.table_data.
        for row_idx, row_data in enumerate(self.table_data):
            for col_idx, col_name in enumerate(self.visible_columns):
                value = row_data.get(col_name, '')
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row_idx)
                self.table.setItem(row_idx, col_idx, item)
        
        # Enable sorting
        self.table.setSortingEnabled(True)
        
        # Update row count
        self.row_count_label.setText(f"Rows: {len(self.table_data)}")
        
    def manage_columns(self):
        """Show column management dialog"""
        if not self.column_headers:
            QMessageBox.information(self, "No Data", "Load data from source first")
            return
        
        dialog = ColumnManagementDialog(
            self.column_headers,
            self.visible_columns,
            self.custom_columns,
            self
        )
        
        if dialog.exec():
            self.visible_columns = dialog.get_visible_columns()
            new_custom = dialog.get_new_custom_columns()
            
            # Add new custom columns to data
            for col_name in new_custom:
                if col_name not in self.column_headers:
                    self.column_headers.append(col_name)
                    self.custom_columns.append(col_name)
                    # Add empty values to all rows
                    for row in self.table_data:
                        row[col_name] = ''
            
            # Remove deleted custom columns
            deleted = dialog.get_deleted_columns()
            for col_name in deleted:
                if col_name in self.column_headers:
                    self.column_headers.remove(col_name)
                if col_name in self.custom_columns:
                    self.custom_columns.remove(col_name)
                if col_name in self.visible_columns:
                    self.visible_columns.remove(col_name)
                # Remove from data
                for row in self.table_data:
                    row.pop(col_name, None)
            
            self._populate_table()
            
    def bulk_edit_rows(self):
        """Bulk edit selected rows"""
        if not self.table_data:
            QMessageBox.information(self, "No Data", "Table is empty")
            return
        
        selected_rows = sorted(set(index.row() for index in self.table.selectedIndexes()))

        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select rows to edit")
            return

        # Translate the visual rows into record indices (the table is sortable)
        selected_indices = []
        for row in selected_rows:
            data_index = self._data_index_for_row(row)
            if data_index is not None and data_index not in selected_indices:
                selected_indices.append(data_index)

        if not selected_indices:
            QMessageBox.warning(self, "No Selection", "The selected rows could not be matched")
            return
        
        dialog = BulkEditTableDialog(
            selected_indices,
            self.column_headers,
            self.table_data,
            self
        )

        if dialog.exec():
            changes = dialog.get_changes()

            # Apply changes to the records the selected rows really show
            for data_index in selected_indices:
                for col_name, value in changes.items():
                    self.table_data[data_index][col_name] = value

            self._populate_table()

            QMessageBox.information(
                self, "Success",
                f"Applied changes to {len(selected_indices)} row(s)"
            )
            
    def clear_table(self):
        """Clear all table data"""
        if not self.table_data:
            return
        
        reply = QMessageBox.question(
            self, "Clear Table",
            "Clear all table data? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.table_data = []
            self.column_headers = []
            self.visible_columns = []
            self.custom_columns = []
            self.table.clear()
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.row_count_label.setText("Rows: 0")
            
    def show_table_context_menu(self, position):
        """Show context menu for table"""
        menu = QMenu()
        
        edit_action = QAction("✏️ Edit Cell", self)
        edit_action.triggered.connect(lambda: self.table.editItem(self.table.currentItem()))
        menu.addAction(edit_action)
        
        menu.addSeparator()
        
        bulk_action = QAction("✏️ Bulk Edit Selected", self)
        bulk_action.triggered.connect(self.bulk_edit_rows)
        menu.addAction(bulk_action)
        
        menu.exec(self.table.viewport().mapToGlobal(position))
        
    def show_header_context_menu(self, position):
        """Show context menu for table header"""
        col_idx = self.table.horizontalHeader().logicalIndexAt(position)
        if col_idx < 0:
            return
        
        col_name = self.visible_columns[col_idx]
        
        menu = QMenu()
        
        # Rename header
        rename_action = QAction("✏️ Rename Header", self)
        rename_action.triggered.connect(lambda: self.rename_column(col_idx, col_name))
        menu.addAction(rename_action)
        
        # Delete column (only for custom columns)
        if col_name in self.custom_columns:
            delete_action = QAction("🗑️ Delete Column", self)
            delete_action.triggered.connect(lambda: self.delete_column(col_name))
            menu.addAction(delete_action)
        
        menu.exec(self.table.horizontalHeader().mapToGlobal(position))
        
    def rename_column(self, col_idx: int, old_name: str):
        """Rename a column header"""
        new_name, ok = QInputDialog.getText(
            self, "Rename Column",
            f"Enter new name for column '{old_name}':",
            text=old_name
        )
        
        if ok and new_name and new_name != old_name:
            if new_name in self.column_headers:
                QMessageBox.warning(self, "Duplicate Name", 
                                  f"Column '{new_name}' already exists")
                return
            
            # Update headers
            idx = self.column_headers.index(old_name)
            self.column_headers[idx] = new_name
            
            idx = self.visible_columns.index(old_name)
            self.visible_columns[idx] = new_name
            
            if old_name in self.custom_columns:
                idx = self.custom_columns.index(old_name)
                self.custom_columns[idx] = new_name
            
            # Update data
            for row in self.table_data:
                if old_name in row:
                    row[new_name] = row.pop(old_name)
            
            # Refresh table
            self._populate_table()
            
    def delete_column(self, col_name: str):
        """Delete a custom column"""
        reply = QMessageBox.question(
            self, "Delete Column",
            f"Delete column '{col_name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if col_name in self.column_headers:
                self.column_headers.remove(col_name)
            if col_name in self.visible_columns:
                self.visible_columns.remove(col_name)
            if col_name in self.custom_columns:
                self.custom_columns.remove(col_name)
            
            # Remove from data
            for row in self.table_data:
                row.pop(col_name, None)
            
            self._populate_table()
            
    def export_to_pdf(self):
        """Export table to PDF with unified preview and export dialog"""
        if not self.table_data:
            QMessageBox.warning(self, "No Data", "Table is empty. Load data first.")
            return
        
        # Sync table data with current table content (in case user edited)
        self._sync_table_data()
        
        # Keep reference on self to prevent garbage collection
        self._pdf_export_dialog = PDFExportDialog(
            table_data=self.table_data,
            column_headers=self.column_headers,
            visible_columns=self.visible_columns,
            saved_settings=self.saved_export_settings,
            parent=self
        )
        
        self._pdf_export_dialog.exec()
        
        # Save settings for next time (but not password)
        self.saved_export_settings = self._pdf_export_dialog.get_settings_without_password()
            
    def view_encrypted_pdf(self):
        """Open encrypted PDF viewer"""
        self.viewer = PDFViewerWindow()
        self.viewer.show()
        
    def _data_index_for_row(self, row: int) -> Optional[int]:
        """
        Return the index in ``self.table_data`` that the given table row shows.

        The table can be sorted by the user, so the visual row number is not a
        valid index into the data list.  Each cell therefore carries the index
        of its record.

        Args:
            row: Visual row number in the table.

        Returns:
            The index into ``self.table_data``, or ``None`` when it cannot be
            determined.
        """
        for col_idx in range(self.table.columnCount()):
            item = self.table.item(row, col_idx)
            if item is None:
                continue
            index = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(index, int) and 0 <= index < len(self.table_data):
                return index
        return None

    def _sync_table_data(self):
        """
        Synchronize table_data with the current table content.

        Edited cells are written back to the record the row actually shows.
        Mapping the visual row number straight onto ``self.table_data`` wrote
        the values into the wrong students as soon as the table had been
        sorted - the exported PDF then mixed up user names and passwords.
        """
        for row in range(self.table.rowCount()):
            data_index = self._data_index_for_row(row)
            if data_index is None:
                logger.warning("Row %d could not be matched to a record - skipped", row)
                continue

            for col_idx, col_name in enumerate(self.visible_columns):
                item = self.table.item(row, col_idx)
                if item:
                    self.table_data[data_index][col_name] = item.text()
