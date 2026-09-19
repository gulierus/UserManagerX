"""
First tab: Data Source Selection and Configuration
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QGroupBox, QStackedWidget, QMessageBox
)
from PyQt6.QtCore import Qt

from sources.edupage_source import EdupageSourceWidget
from sources.file_source import FileSourceWidget
from sources.ad_source import ActiveDirectorySourceWidget
from sources.m365_source import MicrosoftM365SourceWidget

logger = logging.getLogger(__name__)


class SourceSelectionTab(QWidget):
    """First tab for selecting and configuring data sources"""

    #: The source types offered, in the order of the pages in the stack.
    SOURCE_TYPES = [
        "EduPage",
        "Encrypted JSON File",
        "Active Directory",
        "Microsoft 365 From Web",
    ]

    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Title
        title = QLabel("Data Source Configuration")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)
        
        # Description
        desc = QLabel(
            "Select a data source type and configure its settings. "
            "All sources on this tab are read-only. "
            "Use the 'Comparison & Sync' tab to view and work with the loaded data."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        layout.addWidget(desc)
        
        # Source type selection
        source_group = QGroupBox("Source Type")
        source_layout = QVBoxLayout(source_group)
        
        selector_layout = QHBoxLayout()
        selector_layout.addWidget(QLabel("Select Data Source:"))
        
        self.source_combo = QComboBox()
        # The order here is the order of the pages in the stack below, and
        # SOURCE_TYPES repeats it for the log line; the three must agree.
        self.source_combo.addItems(self.SOURCE_TYPES)
        self.source_combo.currentIndexChanged.connect(self.on_source_changed)
        selector_layout.addWidget(self.source_combo)
        selector_layout.addStretch()
        
        source_layout.addLayout(selector_layout)
        layout.addWidget(source_group)
        
        # Configuration area (stacked widget for different sources)
        config_group = QGroupBox("Source Configuration")
        config_layout = QVBoxLayout(config_group)
        
        self.config_stack = QStackedWidget()
        
        # Create widgets for each source type
        self.edupage_widget = EdupageSourceWidget(self.source_manager)
        self.file_widget = FileSourceWidget(self.source_manager)
        self.ad_widget = ActiveDirectorySourceWidget(self.source_manager)
        self.m365_widget = MicrosoftM365SourceWidget(self.source_manager)

        self.config_stack.addWidget(self.edupage_widget)
        self.config_stack.addWidget(self.file_widget)
        self.config_stack.addWidget(self.ad_widget)
        self.config_stack.addWidget(self.m365_widget)
        
        config_layout.addWidget(self.config_stack)
        layout.addWidget(config_group, stretch=1)
        
        # Info label
        info = QLabel(
            "💡 Tip: After loading data from a source, switch to the 'Comparison & Sync' "
            "tab to view classes and students, or to merge multiple sources."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px; "
            "color: #8cc4ff;"
        )
        layout.addWidget(info)
        
        logger.info("SourceSelectionTab UI initialized")
        
    def on_source_changed(self, index):
        """Handle source type selection change"""
        self.config_stack.setCurrentIndex(index)
        if 0 <= index < len(self.SOURCE_TYPES):
            logger.info(f"Source type changed to: {self.SOURCE_TYPES[index]}")
