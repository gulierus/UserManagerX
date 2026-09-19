"""
Column Management Dialog
Allows users to show/hide columns, add custom columns, and delete columns

:class:`ColumnVisibilityDialog` used to live inside
``operations/ad_management.py``.  It is a plain dialog with nothing Active
Directory about it, and the Microsoft 365 operation needs exactly the same
window, so it moved here next to its sibling rather than being imported from
one operation into another.
"""

import logging
from typing import List
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QInputDialog, QMessageBox,
    QDialogButtonBox, QGroupBox
)
from PyQt6.QtCore import Qt

logger = logging.getLogger(__name__)


class ColumnManagementDialog(QDialog):
    """
    Dialog for managing table columns
    
    Features:
    - Show/hide existing columns
    - Add new custom columns
    - Delete custom columns
    - Reorder columns (future enhancement)
    """
    
    def __init__(self, all_columns: List[str], visible_columns: List[str],
                 custom_columns: List[str], parent=None):
        super().__init__(parent)
        
        self.all_columns = all_columns.copy()
        self.visible_columns = visible_columns.copy()
        self.custom_columns = custom_columns.copy()
        self.new_custom_columns: List[str] = []
        self.deleted_columns: List[str] = []
        
        self.setWindowTitle("Manage Columns")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        
        # Info
        info = QLabel(
            "<b>Column Management</b><br>"
            "Select which columns to display in the table. "
            "You can also add custom columns or delete them."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # Column visibility group
        visibility_group = QGroupBox("Column Visibility")
        visibility_layout = QVBoxLayout(visibility_group)
        
        visibility_info = QLabel("Check columns to show, uncheck to hide:")
        visibility_layout.addWidget(visibility_info)
        
        self.column_list = QListWidget()
        
        for col in self.all_columns:
            item = QListWidgetItem(col)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            
            # Check if visible
            if col in self.visible_columns:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            
            # Mark custom columns
            if col in self.custom_columns:
                item.setText(f"{col} (custom)")
            
            self.column_list.addItem(item)
        
        visibility_layout.addWidget(self.column_list)
        
        # Quick select buttons
        quick_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self.select_all)
        quick_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(self.deselect_all)
        quick_layout.addWidget(deselect_all_btn)
        
        visibility_layout.addLayout(quick_layout)
        
        layout.addWidget(visibility_group)
        
        # Custom column management
        custom_group = QGroupBox("Custom Columns")
        custom_layout = QHBoxLayout(custom_group)
        
        add_btn = QPushButton("➕ Add Custom Column")
        add_btn.clicked.connect(self.add_custom_column)
        custom_layout.addWidget(add_btn)
        
        delete_btn = QPushButton("🗑️ Delete Selected Column")
        delete_btn.clicked.connect(self.delete_selected_column)
        custom_layout.addWidget(delete_btn)
        
        layout.addWidget(custom_group)
        
        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_changes)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def select_all(self):
        """Select all columns"""
        for i in range(self.column_list.count()):
            self.column_list.item(i).setCheckState(Qt.CheckState.Checked)
            
    def deselect_all(self):
        """Deselect all columns"""
        for i in range(self.column_list.count()):
            self.column_list.item(i).setCheckState(Qt.CheckState.Unchecked)
            
    def add_custom_column(self):
        """Add a new custom column"""
        name, ok = QInputDialog.getText(
            self,
            "Add Custom Column",
            "Enter column name:"
        )
        
        if ok and name:
            # Check for duplicates
            if name in self.all_columns or name in self.new_custom_columns:
                QMessageBox.warning(
                    self, "Duplicate Name",
                    f"Column '{name}' already exists"
                )
                return
            
            # Add to new custom columns list
            self.new_custom_columns.append(name)
            
            # Add to list widget
            item = QListWidgetItem(f"{name} (custom, new)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.column_list.addItem(item)
            
            logger.info(f"Added custom column: {name}")
            
    def delete_selected_column(self):
        """Delete the selected column (only custom columns can be deleted)"""
        current_item = self.column_list.currentItem()
        if not current_item:
            QMessageBox.information(
                self, "No Selection",
                "Please select a column to delete"
            )
            return
        
        # Extract column name (remove markers like "(custom)")
        col_text = current_item.text()
        col_name = col_text.split(' (')[0] if ' (' in col_text else col_text
        
        # Check if it's a custom column
        if col_name not in self.custom_columns and col_name not in self.new_custom_columns:
            QMessageBox.warning(
                self, "Cannot Delete",
                "Only custom columns can be deleted"
            )
            return
        
        reply = QMessageBox.question(
            self, "Delete Column",
            f"Delete column '{col_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # Remove from new custom if it's there
            if col_name in self.new_custom_columns:
                self.new_custom_columns.remove(col_name)
            else:
                # Mark for deletion
                self.deleted_columns.append(col_name)
            
            # Remove from list widget
            row = self.column_list.row(current_item)
            self.column_list.takeItem(row)
            
            logger.info(f"Deleted column: {col_name}")
            
    def accept_changes(self):
        """Validate and accept changes"""
        # Collect visible columns
        visible = []
        for i in range(self.column_list.count()):
            item = self.column_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                col_text = item.text()
                col_name = col_text.split(' (')[0] if ' (' in col_text else col_text
                visible.append(col_name)
        
        # Ensure at least one column is visible
        if not visible:
            QMessageBox.warning(
                self, "No Columns",
                "At least one column must be visible"
            )
            return
        
        self.visible_columns = visible
        self.accept()
        
    def get_visible_columns(self) -> List[str]:
        """Get list of visible columns"""
        return self.visible_columns
        
    def get_new_custom_columns(self) -> List[str]:
        """Get list of newly added custom columns"""
        return self.new_custom_columns
        
    def get_deleted_columns(self) -> List[str]:
        """Get list of deleted columns"""
        return self.deleted_columns


class ColumnVisibilityDialog(QDialog):
    """Dialog for selecting visible columns"""
    
    def __init__(self, columns: list, visible_columns: list, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.visible_columns = visible_columns.copy()
        self.setWindowTitle("Select Visible Columns")
        self.setModal(True)
        self.setMinimumWidth(400)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Select which columns to display:"))
        
        self.list_widget = QListWidget()
        
        for col in self.columns:
            item = QListWidgetItem(col)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if col in self.visible_columns 
                else Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)
        
        layout.addWidget(self.list_widget)
        
        # Select/Deselect all buttons
        btn_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self.select_all)
        btn_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(self.deselect_all)
        btn_layout.addWidget(deselect_all_btn)
        
        layout.addLayout(btn_layout)
        
        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def select_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Checked)
    
    def deselect_all(self):
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)
    
    def get_visible_columns(self) -> list:
        """Get list of selected column names"""
        visible = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                visible.append(item.text())
        return visible
