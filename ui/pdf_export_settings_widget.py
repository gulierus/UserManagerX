"""
PDF Export Settings Widget
Settings panel for PDF export configuration (as QWidget)
VERSION 3 - Dynamic font selection using FontManager and QFontDatabase
"""

import logging
from typing import List, Dict, Any
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QGroupBox, QPushButton,
    QTabWidget, QScrollArea, QRadioButton, QButtonGroup, QFileDialog,
    QMessageBox, QColorDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from ui.encryption_selector_widget import EncryptionSelectorWidget
from ui.column_width_dialog import ColumnWidthDialog
from ui.file_path_selector_widget import FilePathSelectorWidget
from utils.font_manager import get_font_manager

logger = logging.getLogger(__name__)


class PDFExportSettingsWidget(QWidget):
    """
    PDF export settings widget (embedded in dialog)
    
    Provides all PDF generation settings organized in tabs
    Uses FontManager for dynamic font selection
    """
    
    def __init__(self, all_columns: List[str], default_columns: List[str],
                 saved_settings: Dict[str, Any] = None, parent=None):
        super().__init__(parent)
        
        self.all_columns = all_columns
        self.default_columns = default_columns
        self.saved_settings = saved_settings or {}
        
        self.selected_columns: List[str] = default_columns.copy()
        self.column_widths: Dict[str, Any] = {}
        self.header_color = QColor("#4a6fa5")
        
        # Get font manager
        self.font_manager = get_font_manager()
        
        self.init_ui()
        self._apply_settings()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Tabs for different setting categories
        tabs = QTabWidget()
        
        tabs.addTab(self._create_output_tab(), "Output")
        tabs.addTab(self._create_columns_tab(), "Columns")
        tabs.addTab(self._create_page_layout_tab(), "Page Layout")
        tabs.addTab(self._create_table_appearance_tab(), "Table")
        tabs.addTab(self._create_content_tab(), "Content")
        
        layout.addWidget(tabs)
        
    def _create_output_tab(self) -> QWidget:
        """Create output settings tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # ---- File selection — reusable FilePathSelectorWidget ----
        self.file_selector = FilePathSelectorWidget(
            label="Output File",
            auto_name_factory=lambda: (
                f"table_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            ),
            file_filter="PDF Files (*.pdf);;All Files (*)",
            default_suffix=".pdf",
            parent=self,
        )
        layout.addWidget(self.file_selector)
        
        # Encryption
        encryption_group = QGroupBox("Encryption")
        encryption_layout = QVBoxLayout(encryption_group)
        
        self.encryption_widget = EncryptionSelectorWidget(
            include_pikepdf=True,
            include_none=False,
            require_password=True,
            parent=self
        )
        encryption_layout.addWidget(self.encryption_widget)
        
        layout.addWidget(encryption_group)
        
        layout.addStretch()
        return widget
        
    def _create_columns_tab(self) -> QWidget:
        """Create column selection tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("Select columns to include in PDF:"))
        
        # Scrollable column list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        self.column_checkboxes = {}
        
        for col in self.all_columns:
            checkbox = QCheckBox(col)
            checkbox.setChecked(col in self.selected_columns)
            scroll_layout.addWidget(checkbox)
            self.column_checkboxes[col] = checkbox
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        
        layout.addWidget(scroll)
        
        # Quick select buttons
        btn_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self._select_all_columns)
        btn_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(self._deselect_all_columns)
        btn_layout.addWidget(deselect_all_btn)
        
        layout.addLayout(btn_layout)
        
        return widget
        
    def _create_page_layout_tab(self) -> QWidget:
        """Create page layout settings tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Orientation
        orientation_group = QGroupBox("Page Orientation")
        orientation_layout = QHBoxLayout(orientation_group)
        
        orientation_layout.addWidget(QLabel("Orientation:"))
        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem("Portrait", "portrait")
        self.orientation_combo.addItem("Landscape", "landscape")
        orientation_layout.addWidget(self.orientation_combo)
        orientation_layout.addStretch()
        
        layout.addWidget(orientation_group)
        
        # Margins
        margins_group = QGroupBox("Page Margins (cm)")
        margins_layout = QVBoxLayout(margins_group)
        
        # Grid layout for margins
        margin_grid = QHBoxLayout()
        
        # Top
        top_layout = QVBoxLayout()
        top_layout.addWidget(QLabel("Top:"))
        self.margin_top_spin = QDoubleSpinBox()
        self.margin_top_spin.setRange(0.0, 10.0)
        self.margin_top_spin.setSingleStep(0.5)
        self.margin_top_spin.setDecimals(1)
        self.margin_top_spin.setSuffix(" cm")
        top_layout.addWidget(self.margin_top_spin)
        margin_grid.addLayout(top_layout)
        
        # Bottom
        bottom_layout = QVBoxLayout()
        bottom_layout.addWidget(QLabel("Bottom:"))
        self.margin_bottom_spin = QDoubleSpinBox()
        self.margin_bottom_spin.setRange(0.0, 10.0)
        self.margin_bottom_spin.setSingleStep(0.5)
        self.margin_bottom_spin.setDecimals(1)
        self.margin_bottom_spin.setSuffix(" cm")
        bottom_layout.addWidget(self.margin_bottom_spin)
        margin_grid.addLayout(bottom_layout)
        
        # Left
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Left:"))
        self.margin_left_spin = QDoubleSpinBox()
        self.margin_left_spin.setRange(0.0, 10.0)
        self.margin_left_spin.setSingleStep(0.5)
        self.margin_left_spin.setDecimals(1)
        self.margin_left_spin.setSuffix(" cm")
        left_layout.addWidget(self.margin_left_spin)
        margin_grid.addLayout(left_layout)
        
        # Right
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Right:"))
        self.margin_right_spin = QDoubleSpinBox()
        self.margin_right_spin.setRange(0.0, 10.0)
        self.margin_right_spin.setSingleStep(0.5)
        self.margin_right_spin.setDecimals(1)
        self.margin_right_spin.setSuffix(" cm")
        right_layout.addWidget(self.margin_right_spin)
        margin_grid.addLayout(right_layout)
        
        margins_layout.addLayout(margin_grid)
        layout.addWidget(margins_group)
        
        layout.addStretch()
        return widget
        
    def _create_table_appearance_tab(self) -> QWidget:
        """Create table appearance settings tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Row height - FIXED with QRadioButton and QButtonGroup
        row_height_group = QGroupBox("Row Height")
        row_height_layout = QVBoxLayout(row_height_group)
        
        # Button group for exclusive selection
        self.row_height_button_group = QButtonGroup(self)
        
        self.row_height_auto_radio = QRadioButton("Automatic row height")
        self.row_height_button_group.addButton(self.row_height_auto_radio)
        row_height_layout.addWidget(self.row_height_auto_radio)
        
        manual_layout = QHBoxLayout()
        self.row_height_manual_radio = QRadioButton("Manual height:")
        self.row_height_button_group.addButton(self.row_height_manual_radio)
        manual_layout.addWidget(self.row_height_manual_radio)
        
        self.row_height_spin = QDoubleSpinBox()
        self.row_height_spin.setRange(0.3, 10.0)
        self.row_height_spin.setSingleStep(0.1)
        self.row_height_spin.setDecimals(1)
        self.row_height_spin.setSuffix(" cm")
        self.row_height_spin.setEnabled(False)
        manual_layout.addWidget(self.row_height_spin)
        manual_layout.addStretch()
        
        row_height_layout.addLayout(manual_layout)
        
        # Connect toggle signal
        self.row_height_manual_radio.toggled.connect(self.row_height_spin.setEnabled)
        
        layout.addWidget(row_height_group)
        
        # Column widths - MOVED HERE from Columns tab
        width_group = QGroupBox("Column Widths")
        width_layout = QVBoxLayout(width_group)
        
        width_info = QLabel(
            "Configure width for each column. Click 'Configure...' to set individual widths."
        )
        width_info.setWordWrap(True)
        width_info.setStyleSheet("font-size: 10px; color: #aaa;")
        width_layout.addWidget(width_info)
        
        # Only "Configure Individual Widths" button (removed "Set All to Auto")
        config_btn = QPushButton("⚙️ Configure Individual Widths...")
        config_btn.clicked.connect(self._configure_column_widths)
        width_layout.addWidget(config_btn)
        
        layout.addWidget(width_group)
        
        # Text wrapping
        wrap_group = QGroupBox("Text Wrapping")
        wrap_layout = QHBoxLayout(wrap_group)
        
        self.text_wrap_check = QCheckBox("Enable text wrapping in cells")
        wrap_layout.addWidget(self.text_wrap_check)
        wrap_layout.addStretch()
        
        layout.addWidget(wrap_group)
        
        # Alignment
        alignment_group = QGroupBox("Cell Alignment")
        alignment_layout = QVBoxLayout(alignment_group)
        
        # Horizontal alignment
        h_layout = QHBoxLayout()
        h_layout.addWidget(QLabel("Horizontal:"))
        self.h_align_combo = QComboBox()
        self.h_align_combo.addItem("Left", "LEFT")
        self.h_align_combo.addItem("Center", "CENTER")
        self.h_align_combo.addItem("Right", "RIGHT")
        h_layout.addWidget(self.h_align_combo)
        h_layout.addStretch()
        alignment_layout.addLayout(h_layout)
        
        # Vertical alignment
        v_layout = QHBoxLayout()
        v_layout.addWidget(QLabel("Vertical:"))
        self.v_align_combo = QComboBox()
        self.v_align_combo.addItem("Top", "TOP")
        self.v_align_combo.addItem("Middle", "MIDDLE")
        self.v_align_combo.addItem("Bottom", "BOTTOM")
        v_layout.addWidget(self.v_align_combo)
        v_layout.addStretch()
        alignment_layout.addLayout(v_layout)
        
        layout.addWidget(alignment_group)
        
        # Header color
        color_group = QGroupBox("Table Header")
        color_layout = QHBoxLayout(color_group)
        
        color_layout.addWidget(QLabel("Header Background:"))
        self.color_preview = QLabel("      ")
        self.color_preview.setStyleSheet(
            f"background-color: {self.header_color.name()}; border: 1px solid #555;"
        )
        color_layout.addWidget(self.color_preview)
        
        color_btn = QPushButton("Choose Color...")
        color_btn.clicked.connect(self._choose_header_color)
        color_layout.addWidget(color_btn)
        
        color_layout.addStretch()
        layout.addWidget(color_group)
        
        # Font settings - DYNAMIC using FontManager
        font_group = QGroupBox("Font")
        font_layout = QVBoxLayout(font_group)
        
        # Font family - dynamically populated
        family_layout = QHBoxLayout()
        family_layout.addWidget(QLabel("Font Family:"))
        self.font_combo = QComboBox()
        self._populate_font_combo()
        family_layout.addWidget(self.font_combo)
        family_layout.addStretch()
        font_layout.addLayout(family_layout)
        
        # Font size
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Size:"))
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 24)
        self.font_size_spin.setSuffix(" pt")
        size_layout.addWidget(self.font_size_spin)
        size_layout.addStretch()
        font_layout.addLayout(size_layout)
        
        # Font info
        self.font_info_label = QLabel()
        self.font_info_label.setStyleSheet("font-size: 10px; color: #aaa;")
        self.font_info_label.setWordWrap(True)
        font_layout.addWidget(self.font_info_label)
        
        # Update font info when selection changes
        self.font_combo.currentTextChanged.connect(self._update_font_info)
        
        layout.addWidget(font_group)
        
        layout.addStretch()
        return widget
        
    def _create_content_tab(self) -> QWidget:
        """Create content settings tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Title
        title_group = QGroupBox("Document Title")
        title_layout = QVBoxLayout(title_group)
        
        self.include_title_check = QCheckBox("Include title in document")
        self.include_title_check.toggled.connect(self._on_include_title_toggled)
        title_layout.addWidget(self.include_title_check)
        
        title_input_layout = QHBoxLayout()
        title_input_layout.addWidget(QLabel("Title Text:"))
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Enter document title...")
        self.title_input.setEnabled(False)
        title_input_layout.addWidget(self.title_input)
        title_layout.addLayout(title_input_layout)
        
        layout.addWidget(title_group)
        
        # Date
        date_group = QGroupBox("Generation Date")
        date_layout = QHBoxLayout(date_group)
        
        self.include_date_check = QCheckBox("Include generation date in document")
        date_layout.addWidget(self.include_date_check)
        date_layout.addStretch()
        
        layout.addWidget(date_group)
        
        layout.addStretch()
        return widget
        
    def _populate_font_combo(self):
        """Populate font combo box with available fonts from FontManager"""
        self.font_combo.clear()
        
        # Get available fonts from font manager
        available_fonts = self.font_manager.get_available_families()
        
        if not available_fonts:
            # Fallback if no fonts loaded
            self.font_combo.addItem("Helvetica")
            self.font_combo.addItem("Times")
            self.font_combo.addItem("Courier")
            logger.warning("No fonts loaded from FontManager, using fallback fonts")
            return
        
        # Add fonts to combo
        for font_family in available_fonts:
            self.font_combo.addItem(font_family)
        
        logger.debug(f"Populated font combo with {len(available_fonts)} fonts")
        
    def _update_font_info(self, family_name: str):
        """Update font information label"""
        if not family_name:
            self.font_info_label.clear()
            return
        
        font_info = self.font_manager.get_font_info(family_name)
        
        info_parts = []
        
        if font_info.get('is_custom'):
            info_parts.append("Custom Font (DejaVu)")
        elif font_info.get('is_builtin'):
            info_parts.append("Built-in Font")
        else:
            info_parts.append("System Font")
        
        if font_info.get('reportlab_name'):
            info_parts.append(f"ReportLab: {font_info['reportlab_name']}")
        
        self.font_info_label.setText(" | ".join(info_parts))
        
    def _apply_settings(self):
        """Apply saved settings or defaults"""
        # Output file — restore via FilePathSelectorWidget
        auto = self.saved_settings.get('auto_filename', True)
        saved_path = self.saved_settings.get('output_path', '')
        self.file_selector.set_auto_mode(auto)
        if saved_path:
            self.file_selector.set_path(saved_path)
        
        # Margins (2 cm default)
        margins = self.saved_settings.get('margins', {})
        self.margin_top_spin.setValue(margins.get('top', 2.0))
        self.margin_bottom_spin.setValue(margins.get('bottom', 2.0))
        self.margin_left_spin.setValue(margins.get('left', 2.0))
        self.margin_right_spin.setValue(margins.get('right', 2.0))
        
        # Row height
        row_height = self.saved_settings.get('row_height', {})
        if row_height.get('auto', True):
            self.row_height_auto_radio.setChecked(True)
        else:
            self.row_height_manual_radio.setChecked(True)
        self.row_height_spin.setValue(row_height.get('value', 0.8))
        
        # Orientation
        orientation = self.saved_settings.get('orientation', 'portrait')
        idx = self.orientation_combo.findData(orientation)
        if idx >= 0:
            self.orientation_combo.setCurrentIndex(idx)
        
        # Text wrapping
        self.text_wrap_check.setChecked(
            self.saved_settings.get('text_wrap', True)
        )
        
        # Alignment
        alignment = self.saved_settings.get('alignment', {})
        h_idx = self.h_align_combo.findData(alignment.get('horizontal', 'LEFT'))
        if h_idx >= 0:
            self.h_align_combo.setCurrentIndex(h_idx)
        
        v_idx = self.v_align_combo.findData(alignment.get('vertical', 'MIDDLE'))
        if v_idx >= 0:
            self.v_align_combo.setCurrentIndex(v_idx)
        
        # Font - use saved font family or default to first available
        font = self.saved_settings.get('font', {})
        saved_family = font.get('family', 'Helvetica')
        
        # Try to find saved font
        font_idx = self.font_combo.findText(saved_family)
        if font_idx >= 0:
            self.font_combo.setCurrentIndex(font_idx)
        else:
            # If saved font not found, use first available
            if self.font_combo.count() > 0:
                self.font_combo.setCurrentIndex(0)
        
        self.font_size_spin.setValue(font.get('size', 10))
        
        # Update font info
        self._update_font_info(self.font_combo.currentText())
        
        # Content
        self.include_title_check.setChecked(
            self.saved_settings.get('include_title', True)
        )
        self.title_input.setText(
            self.saved_settings.get('title_text', 'Table Export')
        )
        self.include_date_check.setChecked(
            self.saved_settings.get('include_date', True)
        )
        
        # Header color
        header_color = self.saved_settings.get('header_color', '#4a6fa5')
        self.header_color = QColor(header_color)
        self.color_preview.setStyleSheet(
            f"background-color: {self.header_color.name()}; border: 1px solid #555;"
        )
        
        # Initialize column widths to auto
        for col in self.all_columns:
            self.column_widths[col] = 'auto'
        
    def _select_all_columns(self):
        """Select all columns"""
        for checkbox in self.column_checkboxes.values():
            checkbox.setChecked(True)
        
    def _deselect_all_columns(self):
        """Deselect all columns"""
        for checkbox in self.column_checkboxes.values():
            checkbox.setChecked(False)
        
    def _configure_column_widths(self):
        """Configure individual column widths"""
        # Get currently selected columns
        selected = self._get_selected_columns()
        
        if not selected:
            QMessageBox.warning(self, "No Columns", "Please select at least one column first")
            return
        
        dialog = ColumnWidthDialog(selected, self.column_widths, self)
        if dialog.exec():
            self.column_widths = dialog.get_widths()
        
    def _on_include_title_toggled(self, checked):
        """Handle include title checkbox"""
        self.title_input.setEnabled(checked)
        
    def _choose_header_color(self):
        """Choose header background color"""
        color = QColorDialog.getColor(self.header_color, self, "Choose Header Color")
        
        if color.isValid():
            self.header_color = color
            self.color_preview.setStyleSheet(
                f"background-color: {color.name()}; border: 1px solid #555;"
            )
        
    def _get_selected_columns(self) -> List[str]:
        """Get list of selected columns"""
        selected = []
        for col, checkbox in self.column_checkboxes.items():
            if checkbox.isChecked():
                selected.append(col)
        return selected
        
    def validate_preview_settings(self) -> tuple:
        """Validate settings for preview (no encryption needed)"""
        selected_columns = self._get_selected_columns()
        
        if not selected_columns:
            return (False, "Please select at least one column")
        
        return (True, "")
        
    def validate_export_settings(self) -> tuple:
        """Validate settings for export (including encryption)"""
        # Validate columns
        is_valid, error = self.validate_preview_settings()
        if not is_valid:
            return (is_valid, error)
        
        # Validate output path
        output_path = self.file_selector.get_path().strip()
        if not output_path:
            return (False, "Please specify an output file")
        
        # Validate encryption
        is_valid, error = self.encryption_widget.validate()
        if not is_valid:
            return (False, error)
        
        return (True, "")
        
    def get_settings_for_preview(self) -> Dict[str, Any]:
        """Get settings for preview generation (no encryption)"""
        return self._get_common_settings()
        
    def get_settings_for_export(self) -> Dict[str, Any]:
        """Get settings for export (including encryption)"""
        settings = self._get_common_settings()
        
        # Add encryption settings
        settings['output_path'] = self.file_selector.get_path().strip()
        settings['encryption_method'] = self.encryption_widget.get_encryption_method()
        settings['file_format'] = self.encryption_widget.get_file_format()
        settings['password'] = self.encryption_widget.get_password()
        
        return settings
        
    def get_settings_without_password(self) -> Dict[str, Any]:
        """Get settings without password (for saving)"""
        settings = self._get_common_settings()
        settings['auto_filename'] = self.file_selector.is_auto_mode()
        settings['output_path'] = self.file_selector.get_path().strip()
        return settings
        
    def _get_common_settings(self) -> Dict[str, Any]:
        """Get common settings used by both preview and export"""
        return {
            'selected_columns': self._get_selected_columns(),
            'column_widths': self.column_widths,
            'orientation': self.orientation_combo.currentData(),
            'margins': {
                'top': self.margin_top_spin.value(),
                'bottom': self.margin_bottom_spin.value(),
                'left': self.margin_left_spin.value(),
                'right': self.margin_right_spin.value()
            },
            'row_height': {
                'auto': self.row_height_auto_radio.isChecked(),
                'value': self.row_height_spin.value()
            },
            'text_wrap': self.text_wrap_check.isChecked(),
            'alignment': {
                'horizontal': self.h_align_combo.currentData(),
                'vertical': self.v_align_combo.currentData()
            },
            'header_color': self.header_color.name(),
            'font': {
                'family': self.font_combo.currentText(),
                'size': self.font_size_spin.value()
            },
            'include_title': self.include_title_check.isChecked(),
            'title_text': self.title_input.text(),
            'include_date': self.include_date_check.isChecked(),
        }
