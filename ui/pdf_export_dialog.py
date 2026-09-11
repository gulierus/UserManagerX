"""
PDF Export Dialog - Unified Preview and Export
Combines preview and export settings in one dialog with splitter
"""

import logging
from typing import List, Dict, Any
from io import BytesIO
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QWidget, QMessageBox
)
from PyQt6.QtCore import Qt, QByteArray
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    WEB_ENGINE_AVAILABLE = True
    WEB_ENGINE_ERROR = None
except ImportError as _web_engine_error:   # pragma: no cover - environment dependent
    # PyQt6-WebEngine is an optional component.  Without it the PDF preview is
    # unavailable, but the application - including PDF *export* - must still
    # start and work.
    QWebEngineView = None
    QWebEngineSettings = None
    WEB_ENGINE_AVAILABLE = False
    WEB_ENGINE_ERROR = _web_engine_error

from ui.pdf_export_settings_widget import PDFExportSettingsWidget
from utils.pdf_generator import PDFGenerator
from utils.pdf_export_task import PDFExportTask
from utils.progress_dialog import ProgressDialog

logger = logging.getLogger(__name__)


class PDFExportDialog(QDialog):
    """
    Unified PDF export dialog with preview and settings
    
    Features:
    - Left side: PDF preview using QWebEngineView
    - Right side: Export settings (collapsible)
    - QSplitter for resizing
    - Preview button generates PDF in memory
    - Export button saves to file with encryption
    - Settings are preserved between dialog opens
    """
    
    def __init__(self, table_data: List[Dict[str, Any]],
                 column_headers: List[str],
                 visible_columns: List[str],
                 saved_settings: Dict[str, Any] = None,
                 parent=None):
        super().__init__(parent)

        self.table_data = table_data
        self.column_headers = column_headers
        self.visible_columns = visible_columns
        self.saved_settings = saved_settings or {}
        
        # Preview buffer
        self.preview_buffer: BytesIO = None
        
        self.setWindowTitle("PDF Export")
        self.setMinimumSize(1200, 700)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Main splitter
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left side - Preview
        preview_panel = self._create_preview_panel()
        main_splitter.addWidget(preview_panel)
        
        # Right side - Settings
        self.settings_panel = self._create_settings_panel()
        main_splitter.addWidget(self.settings_panel)
        
        # Set initial sizes (60% preview, 40% settings)
        main_splitter.setSizes([720, 480])
        
        layout.addWidget(main_splitter)
        
        # Bottom buttons
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(10, 5, 10, 10)
        
        # Toggle settings panel button
        self.toggle_settings_btn = QPushButton("◀ Hide Settings")
        self.toggle_settings_btn.clicked.connect(self.toggle_settings_panel)
        bottom_layout.addWidget(self.toggle_settings_btn)
        
        bottom_layout.addStretch()
        
        # Preview button
        preview_btn = QPushButton("👁️ Generate Preview")
        preview_btn.setMinimumHeight(35)
        preview_btn.setStyleSheet("background-color: #2196F3; font-weight: bold;")
        preview_btn.clicked.connect(self.generate_preview)
        bottom_layout.addWidget(preview_btn)
        
        # Export button
        export_btn = QPushButton("💾 Export to File...")
        export_btn.setMinimumHeight(35)
        export_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        export_btn.clicked.connect(self.export_to_file)
        bottom_layout.addWidget(export_btn)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(35)
        close_btn.clicked.connect(self.close_dialog)
        bottom_layout.addWidget(close_btn)
        
        layout.addLayout(bottom_layout)
        
    def _create_preview_panel(self) -> QWidget:
        """
        Create the preview panel.

        When PyQt6-WebEngine is not installed, a placeholder explaining the
        situation is shown instead of the preview; exporting keeps working.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        if not WEB_ENGINE_AVAILABLE:
            self.web_view = None
            placeholder = QLabel(
                "<b>PDF preview is not available</b><br><br>"
                "The optional component <code>PyQt6-WebEngine</code> is not "
                "installed on this computer.<br>"
                "Install it with <code>pip install PyQt6-WebEngine</code> to "
                "see the preview.<br><br>"
                "Exporting the PDF works without it."
            )
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #aaa; padding: 20px;")
            layout.addWidget(placeholder)
            return panel

        # Web view for PDF preview
        self.web_view = QWebEngineView()
        
        # Configure web engine settings for PDF support
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)        

        self.web_view.setHtml(
            "<html><body style='background: #2a2a2a; color: #aaa; "
            "display: flex; align-items: center; justify-content: center; "
            "height: 100vh; margin: 0;'>"
            "<div style='text-align: center;'>"
            "<h2>No Preview Generated</h2>"
            "<p>Click 'Generate Preview' to see PDF output</p>"
            "</div></body></html>"
        )
        layout.addWidget(self.web_view)
        
        return panel
        
    def _create_settings_panel(self) -> QWidget:
        """Create settings panel"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Title
        title = QLabel("<b>Export Settings</b>")
        title.setStyleSheet("font-size: 12px; padding: 5px;")
        layout.addWidget(title)
        
        # Settings widget
        self.settings_widget = PDFExportSettingsWidget(
            all_columns=self.column_headers,
            default_columns=self.visible_columns,
            saved_settings=self.saved_settings,
            parent=self
        )
        layout.addWidget(self.settings_widget)
        
        return panel
        
    def toggle_settings_panel(self):
        """Toggle settings panel visibility"""
        is_visible = self.settings_panel.isVisible()
        self.settings_panel.setVisible(not is_visible)
        
        if is_visible:
            self.toggle_settings_btn.setText("▶ Show Settings")
        else:
            self.toggle_settings_btn.setText("◀ Hide Settings")
        
    def generate_preview(self):
        """Generate PDF preview in memory"""
        try:
            # Get settings (don't need encryption for preview)
            settings = self.settings_widget.get_settings_for_preview()
            
            # Validate settings
            is_valid, error = self.settings_widget.validate_preview_settings()
            if not is_valid:
                QMessageBox.warning(self, "Invalid Settings", error)
                return
            
            # Create generator
            generator = PDFGenerator(
                self.table_data,
                settings,
                show_margins=True  # Show margins in preview
            )
            
            # Check for warnings
            is_valid, warnings = generator.validate_settings()
            if warnings:
                warning_text = "\n".join(warnings)
                QMessageBox.warning(
                    self, "Configuration Warnings",
                    f"Preview will be generated, but please note:\n\n{warning_text}"
                )
            
            # Generate PDF to buffer
            if self.preview_buffer:
                self.preview_buffer.close()
            
            self.preview_buffer = BytesIO()
            generator.generate_to_buffer(self.preview_buffer)
            
            # Display in web view
            self._display_preview()
            
            logger.info("PDF preview generated successfully")
            
        except Exception as e:
            logger.exception("Error generating preview")
            QMessageBox.critical(
                self, "Preview Error",
                f"Failed to generate preview:\n{str(e)}"
            )
        
    def _display_preview(self):
        """Display preview buffer in web view (no-op without WebEngine)."""
        if not self.preview_buffer:
            return

        if not WEB_ENGINE_AVAILABLE or self.web_view is None:
            logger.info("PDF preview skipped - PyQt6-WebEngine is not installed")
            return
        
        try:
            # Reset buffer position
            self.preview_buffer.seek(0)
            
            # Convert BytesIO to QByteArray
            pdf_data = QByteArray(self.preview_buffer.read())
            
            # Load into QWebEngineView using setContent (from memory - NO temp file!)
            self.web_view.setContent(pdf_data, "application/pdf")
            
            logger.debug("Preview displayed in web view")
            
        except Exception as e:
            logger.exception("Error displaying preview")
            QMessageBox.critical(
                self, "Display Error",
                f"Failed to display preview:\n{str(e)}"
            )
        
    def export_to_file(self):
        """Export PDF to file with encryption"""
        try:
            # Validate full settings (including encryption)
            is_valid, error = self.settings_widget.validate_export_settings()
            if not is_valid:
                QMessageBox.warning(self, "Invalid Settings", error)
                return
            
            # Get settings
            settings = self.settings_widget.get_settings_for_export()
            
            # Check if output file already exists
            import os
            output_path = settings.get('output_path', '')
            if output_path and os.path.exists(output_path):
                reply = QMessageBox.question(
                    self, "File Exists",
                    f"The file already exists:\n{output_path}\n\nDo you want to overwrite it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            
            # Create and run export task
            task = PDFExportTask(
                table_data=self.table_data,
                settings=settings
            )
            
            progress_dialog = ProgressDialog(task, self)
            progress_dialog.start_task()
            progress_dialog.exec()
            
                
        except Exception as e:
            logger.exception("Error during export")
            QMessageBox.critical(
                self, "Export Error",
                f"Failed to export PDF:\n{str(e)}"
            )
        
    def close_dialog(self):
        """Close dialog and cleanup"""
        # Save settings before closing
        self.done(QDialog.DialogCode.Accepted)
        
    def get_settings_without_password(self) -> Dict[str, Any]:
        """Get settings without password (for saving)"""
        return self.settings_widget.get_settings_without_password()
        
    def closeEvent(self, event):
        """Handle dialog close"""
        # Clean up preview buffer
        if self.preview_buffer:
            self.preview_buffer.close()
            self.preview_buffer = None
        
        event.accept()
