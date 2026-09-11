"""
Column Width Configuration Dialog
Allows users to set individual column widths for PDF export
"""

import logging
from typing import List, Dict, Any
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QRadioButton, QDoubleSpinBox,
    QDialogButtonBox, QGroupBox, QButtonGroup
)
from PyQt6.QtCore import Qt

logger = logging.getLogger(__name__)


class ColumnWidthDialog(QDialog):
    """
    Dialog for configuring individual column widths
    
    For each column, user can choose:
    - Automatic width
    - Manual width (in cm)
    """
    
    def __init__(self, columns: List[str], current_widths: Dict[str, Any], parent=None):
        super().__init__(parent)
        
        self.columns = columns
        self.widths = current_widths.copy()
        self.width_widgets: Dict[str, tuple] = {}  # column -> (auto_radio, manual_radio, spin)
        
        self.setWindowTitle("Configure Column Widths")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        
        # Info
        info = QLabel(
            "<b>Column Width Configuration</b><br>"
            "Set the width for each column in the PDF table. "
            "Choose automatic width or specify a manual width in centimeters."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # Scroll area for columns
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        # Create width settings for each column
        for col in self.columns:
            col_group = self._create_column_widget(col)
            scroll_layout.addWidget(col_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        
        layout.addWidget(scroll)
        
        # Quick action buttons
        quick_layout = QHBoxLayout()
        
        all_auto_btn = QPushButton("Set All to Auto")
        all_auto_btn.clicked.connect(self._set_all_auto)
        quick_layout.addWidget(all_auto_btn)
        
        quick_layout.addStretch()
        layout.addLayout(quick_layout)
        
        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept_widths)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def _create_column_widget(self, column: str) -> QWidget:
        """Create width configuration widget for a column"""
        group = QGroupBox(column)
        layout = QHBoxLayout(group)
        
        # Radio buttons
        auto_radio = QRadioButton("Automatic")
        layout.addWidget(auto_radio)
        
        manual_radio = QRadioButton("Manual:")
        layout.addWidget(manual_radio)
        
        # Spin box for manual width
        width_spin = QDoubleSpinBox()
        width_spin.setRange(1.0, 20.0)
        width_spin.setSingleStep(0.5)
        width_spin.setDecimals(1)
        width_spin.setSuffix(" cm")
        width_spin.setEnabled(False)
        layout.addWidget(width_spin)
        
        layout.addStretch()
        
        # Connect signals
        manual_radio.toggled.connect(width_spin.setEnabled)
        
        # Button group to ensure exclusivity
        btn_group = QButtonGroup(group)
        btn_group.addButton(auto_radio)
        btn_group.addButton(manual_radio)
        
        # Set initial state
        current_width = self.widths.get(column, 'auto')
        if current_width == 'auto':
            auto_radio.setChecked(True)
            width_spin.setValue(3.0)  # Default manual value
        else:
            manual_radio.setChecked(True)
            width_spin.setValue(float(current_width))
        
        # Store widgets
        self.width_widgets[column] = (auto_radio, manual_radio, width_spin)
        
        return group
        
    def _set_all_auto(self):
        """Set all columns to automatic width"""
        for auto_radio, manual_radio, width_spin in self.width_widgets.values():
            auto_radio.setChecked(True)
        
    def accept_widths(self):
        """Accept and save width settings"""
        # Collect all settings
        for column, (auto_radio, manual_radio, width_spin) in self.width_widgets.items():
            if auto_radio.isChecked():
                self.widths[column] = 'auto'
            else:
                self.widths[column] = width_spin.value()
        
        self.accept()
        
    def get_widths(self) -> Dict[str, Any]:
        """Get the configured widths"""
        return self.widths
