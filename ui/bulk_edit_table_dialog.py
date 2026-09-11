"""
Bulk Edit Table Dialog
Allows users to edit multiple rows at once by setting column values
"""

import logging
from typing import List, Dict, Any
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QMessageBox, QDialogButtonBox,
    QGroupBox, QScrollArea, QWidget, QCheckBox
)
from PyQt6.QtCore import Qt

logger = logging.getLogger(__name__)


class BulkEditTableDialog(QDialog):
    """
    Dialog for bulk editing table rows
    
    Allows user to:
    - Select a column
    - Set a value for that column
    - Apply the value to all selected rows
    """
    
    def __init__(self, selected_rows: List[int], columns: List[str],
                 table_data: List[Dict[str, Any]], parent=None):
        super().__init__(parent)
        
        self.selected_rows = selected_rows
        self.columns = columns
        self.table_data = table_data
        self.changes: Dict[str, str] = {}
        self.edit_widgets: List[tuple] = []  # (column_combo, value_input, apply_checkbox)
        
        self.setWindowTitle(f"Bulk Edit - {len(selected_rows)} rows")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        
        # Info
        info = QLabel(
            f"<b>Bulk Edit {len(self.selected_rows)} Selected Rows</b><br>"
            "Select columns and set values to apply to all selected rows."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # Scroll area for column editors
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(300)
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        # Add initial edit rows
        for _ in range(5):  # Start with 5 edit rows
            self._add_edit_row(scroll_layout)
        
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)
        
        # Add more button
        add_btn = QPushButton("➕ Add Another Column")
        add_btn.clicked.connect(lambda: self._add_edit_row(scroll_layout))
        layout.addWidget(add_btn)
        
        # Preview
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        
        self.preview_label = QLabel("<i>No changes selected</i>")
        self.preview_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label)
        
        update_preview_btn = QPushButton("🔄 Update Preview")
        update_preview_btn.clicked.connect(self.update_preview)
        preview_layout.addWidget(update_preview_btn)
        
        layout.addWidget(preview_group)
        
        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_changes)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def _add_edit_row(self, parent_layout):
        """Add a new edit row"""
        row_group = QGroupBox()
        row_layout = QHBoxLayout(row_group)
        
        # Column selection
        row_layout.addWidget(QLabel("Column:"))
        column_combo = QComboBox()
        column_combo.addItem("(Select Column)")
        column_combo.addItems(self.columns)
        row_layout.addWidget(column_combo, stretch=1)
        
        # Value input
        row_layout.addWidget(QLabel("Value:"))
        value_input = QLineEdit()
        value_input.setPlaceholderText("Enter value (or check 'Set empty')...")
        row_layout.addWidget(value_input, stretch=2)

        # Checkbox to explicitly set to empty string
        set_empty_cb = QCheckBox("Set empty")
        set_empty_cb.setToolTip("Set the column value to an empty string")
        def _on_set_empty_toggled(checked, vi=value_input):
            vi.setEnabled(not checked)
            vi.setPlaceholderText("(will be set to empty string)" if checked else "Enter value (or check 'Set empty')...")
        set_empty_cb.toggled.connect(_on_set_empty_toggled)
        row_layout.addWidget(set_empty_cb)

        # Track this widget group (column_combo, value_input, set_empty_cb)
        self.edit_widgets.append((column_combo, value_input, set_empty_cb))

        parent_layout.addWidget(row_group)

    def update_preview(self):
        """Update preview of changes"""
        # Collect current selections
        temp_changes = {}

        for column_combo, value_input, set_empty_cb in self.edit_widgets:
            column = column_combo.currentText()
            set_empty = set_empty_cb.isChecked()
            value = "" if set_empty else value_input.text()

            if column != "(Select Column)" and (value or set_empty):
                temp_changes[column] = value
        
        if not temp_changes:
            self.preview_label.setText("<i>No changes selected</i>")
            return
        
        # Build preview text
        preview_text = f"<b>Changes to apply to {len(self.selected_rows)} rows:</b><br><br>"
        
        for col, val in temp_changes.items():
            preview_text += f"• <b>{col}</b>: {val}<br>"
        
        self.preview_label.setText(preview_text)
        
    def accept_changes(self):
        """Validate and accept changes"""
        # Collect changes
        self.changes = {}

        for column_combo, value_input, set_empty_cb in self.edit_widgets:
            column = column_combo.currentText()
            set_empty = set_empty_cb.isChecked()
            value = "" if set_empty else value_input.text()

            if column != "(Select Column)" and (value != "" or set_empty):
                # Check for duplicates
                if column in self.changes:
                    QMessageBox.warning(
                        self, "Duplicate Column",
                        f"Column '{column}' is selected multiple times. "
                        "Please select each column only once."
                    )
                    return

                self.changes[column] = value
        
        if not self.changes:
            QMessageBox.warning(
                self, "No Changes",
                "Please select at least one column and value"
            )
            return
        
        # Confirm
        reply = QMessageBox.question(
            self, "Confirm Bulk Edit",
            f"Apply changes to {len(self.selected_rows)} rows?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.accept()
        
    def get_changes(self) -> Dict[str, str]:
        """Get the changes to apply"""
        return self.changes
