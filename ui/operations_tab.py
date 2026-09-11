"""
Third tab: Operations on single source
VERSION 2 - With automatic source detection (no refresh button)
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QListWidget, QStackedWidget, QGroupBox
)
from PyQt6.QtCore import Qt

from operations.ad_management import ADManagementWidget
from operations.pdf_export import PDFExportWidget
from operations.json_export import JSONExportWidget

logger = logging.getLogger(__name__)


class OperationsTab(QWidget):
    """Third tab for performing operations on a single source"""
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.current_source = None
        
        # Connect to source manager signals for automatic updates
        self.source_manager.source_added.connect(self.on_sources_changed)
        self.source_manager.source_removed.connect(self.on_sources_changed)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)
        
        # Title
        title = QLabel("Operations on Data Source")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)
        
        # Description
        desc = QLabel(
            "Select a data source and choose an operation to perform. "
            "Operations include Active Directory management, PDF export, and data backup."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        layout.addWidget(desc)
        
        # Source selection
        source_group = QGroupBox("Select Data Source")
        source_layout = QHBoxLayout(source_group)
        
        source_layout.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("(Select Source)")
        self.source_combo.currentTextChanged.connect(self.on_source_changed)
        source_layout.addWidget(self.source_combo, stretch=1)
        
        # Info label instead of refresh button
        auto_info = QLabel("🔄 <i>Auto-updated</i>")
        auto_info.setStyleSheet("color: #4CAF50; font-size: 10px;")
        auto_info.setToolTip("Source list updates automatically when sources are added or removed")
        source_layout.addWidget(auto_info)
        
        layout.addWidget(source_group)
        
        # Main content area - horizontal split
        content_layout = QHBoxLayout()
        
        # Left side - operation list
        operations_group = QGroupBox("Available Operations")
        operations_layout = QVBoxLayout(operations_group)
        
        self.operation_list = QListWidget()
        self.operation_list.addItems([
            "Active Directory Management",
            "Export to Encrypted PDF",
            "Export to Encrypted JSON"
        ])
        self.operation_list.currentRowChanged.connect(self.on_operation_changed)
        operations_layout.addWidget(self.operation_list)
        
        content_layout.addWidget(operations_group, stretch=1)
        
        # Right side - operation-specific widget
        self.operation_stack = QStackedWidget()
        
        # Create operation widgets
        self.ad_widget = ADManagementWidget(self.source_manager)
        self.pdf_widget = PDFExportWidget(self.source_manager)
        self.json_widget = JSONExportWidget(self.source_manager)
        
        self.operation_stack.addWidget(self.ad_widget)
        self.operation_stack.addWidget(self.pdf_widget)
        self.operation_stack.addWidget(self.json_widget)
        
        content_layout.addWidget(self.operation_stack, stretch=3)
        
        layout.addLayout(content_layout, stretch=1)
        
        # Initial population
        self.populate_sources()
        
    def on_sources_changed(self, *args):
        """Handle source list changes (automatic refresh)"""
        current_selection = self.source_combo.currentText()
        self.populate_sources()
        
        # Try to restore selection if possible
        index = self.source_combo.findText(current_selection)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        
        logger.debug("Source list automatically refreshed")
    
    def populate_sources(self):
        """Populate source combo box"""
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("(Select Source)")
        self.source_combo.addItems(self.source_manager.get_source_names())
        self.source_combo.blockSignals(False)
            
    def on_source_changed(self, source_name):
        """Handle source selection change"""
        if source_name == "(Select Source)":
            self.current_source = None
        else:
            self.current_source = self.source_manager.get_source_by_name(source_name)
            
        # Update all operation widgets
        self.ad_widget.set_source(self.current_source)
        self.pdf_widget.set_source(self.current_source)
        self.json_widget.set_source(self.current_source)
        
        logger.info(f"Selected source for operations: {source_name}")
        
    def on_operation_changed(self, index):
        """Handle operation selection change"""
        if index >= 0:
            self.operation_stack.setCurrentIndex(index)
            operations = ["AD Management", "PDF Export", "JSON Export"]
            logger.info(f"Selected operation: {operations[index]}")
