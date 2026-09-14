"""
Third tab: Operations on single source
VERSION 2 - With automatic source detection (no refresh button)
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QFrame,
    QPushButton, QListWidget, QScrollArea, QSplitter, QStackedWidget, QGroupBox
)
from PyQt6.QtCore import Qt

from ui.settings_tab import (
    CATEGORY_CAPTION_STYLE, CATEGORY_LIST_STYLE, CATEGORY_PANEL_STYLE,
)

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
        # Editing a source on the comparison tab only emits source_modified;
        # without this connection the operation widgets kept showing the class
        # list and the statistics of the source as it looked when it was picked.
        self.source_manager.source_modified.connect(self.on_source_modified)

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
        
        # Main content area - a draggable split between the operation list and
        # the selected operation, so a wide form (AD management) can be given
        # room without the list eating a fixed share of the window.
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left side - operation chooser.  Styled exactly like the category
        # chooser on the Settings tab (same module-level constants), so the two
        # "pick a section" sidebars of the application look identical.
        operations_container = QFrame()
        operations_container.setFrameShape(QFrame.Shape.StyledPanel)
        operations_container.setObjectName("categoryContainer")
        operations_container.setStyleSheet(CATEGORY_PANEL_STYLE)

        operations_layout = QVBoxLayout(operations_container)
        operations_layout.setContentsMargins(6, 6, 6, 6)
        operations_layout.setSpacing(4)

        operations_caption = QLabel("OPERATIONS")
        operations_caption.setStyleSheet(CATEGORY_CAPTION_STYLE)
        operations_layout.addWidget(operations_caption)

        self.operation_list = QListWidget()
        self.operation_list.setStyleSheet(CATEGORY_LIST_STYLE)
        self.operation_list.addItems([
            "Active Directory Management",
            "Export to Encrypted PDF",
            "Export to Encrypted JSON"
        ])
        self.operation_list.currentRowChanged.connect(self.on_operation_changed)
        operations_layout.addWidget(self.operation_list, stretch=1)

        operations_container.setMinimumWidth(170)
        splitter.addWidget(operations_container)

        # Right side - operation-specific widget
        operation_container = QFrame()
        operation_container.setFrameShape(QFrame.Shape.StyledPanel)
        operation_container.setObjectName("panelContainer")
        operation_container.setStyleSheet(
            "QFrame#panelContainer {"
            "  border: 1px solid rgba(122, 162, 224, 70);"
            "  border-radius: 6px;"
            "}"
        )
        operation_layout = QVBoxLayout(operation_container)
        operation_layout.setContentsMargins(2, 2, 2, 2)

        self.operation_stack = QStackedWidget()

        # Create operation widgets
        self.ad_widget = ADManagementWidget(self.source_manager)
        self.pdf_widget = PDFExportWidget(self.source_manager)
        self.json_widget = JSONExportWidget(self.source_manager)

        self.operation_stack.addWidget(self.ad_widget)
        self.operation_stack.addWidget(self.pdf_widget)
        self.operation_stack.addWidget(self.json_widget)

        # The AD management form is ~1065 px wide at its minimum, which would
        # pin the splitter and make it undraggable.  Scrolling the operation
        # panel instead lets the user give the list as much room as they like.
        operation_scroll = QScrollArea()
        operation_scroll.setWidgetResizable(True)
        operation_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        operation_scroll.setWidget(self.operation_stack)
        operation_layout.addWidget(operation_scroll, stretch=1)

        operation_container.setMinimumWidth(260)
        splitter.addWidget(operation_container)

        splitter.setSizes([220, 780])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)
        self.content_splitter = splitter

        layout.addWidget(splitter, stretch=1)

        # The stack shows page 0 from the start, but the list had no current
        # row, so the visible operation was not the highlighted one.  Selecting
        # row 0 now (the stack exists, so on_operation_changed can run) makes
        # the two agree.
        self.operation_list.setCurrentRow(0)

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
        else:
            # The selected source is gone.  populate_sources() rebuilds the
            # combo with its signals blocked, so the combo silently fell back
            # to "(Select Source)" while current_source and the three operation
            # widgets still held - and happily operated on - the deleted
            # source.  Run the selection handler by hand to disarm them.
            self.on_source_changed(self.source_combo.currentText())

        logger.debug("Source list automatically refreshed")

    def on_source_modified(self, source_name):
        """Re-read the selected source after it was edited elsewhere."""
        if self.current_source and self.current_source.name == source_name:
            # Push the same source through again: every operation widget
            # rebuilds its view from it in set_source().
            self.ad_widget.set_source(self.current_source)
            self.pdf_widget.set_source(self.current_source)
            self.json_widget.set_source(self.current_source)
            logger.debug(f"Operation widgets refreshed after edit of: {source_name}")

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
