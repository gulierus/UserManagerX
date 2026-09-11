"""
PDF Viewer Window
View encrypted PDF files with decryption support
VERSION 2 - Fixed as QWidget with modal behavior, proper PDF display
"""

import logging
import os
from io import BytesIO
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QFileDialog, QMessageBox, QGroupBox,
    QCheckBox, QSizePolicy
)
from PyQt6.QtCore import Qt, QUrl, QByteArray
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

from utils.pdf_decrypt_task import PDFDecryptTask
from utils.progress_dialog import ProgressDialog
from utils.encryption import detect_file_format, get_file_info
from utils.encryption import detect_encryption_method

logger = logging.getLogger(__name__)


class PDFViewerWindow(QWidget):
    """
    PDF Viewer window for viewing encrypted PDFs
    
    Features:
    - QWidget with modal behavior
    - File selection
    - Encryption method selection (auto-detect option)
    - Password input
    - Decryption with progress
    - PDF viewing in QWebEngineView (pikepdf via URL, others via QByteArray)
    - Toggle panel visibility
    - Fixed height control panel
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Make it modal
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        
        # Buffer for decrypted PDF (for AES-GCM/GPG)
        self.decrypted_buffer: BytesIO = None
        
        self.setWindowTitle("Encrypted PDF Viewer")
        self.setMinimumSize(900, 700)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Control panel - FIXED HEIGHT
        self.control_panel = self._create_control_panel()
        self.control_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, 
            QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self.control_panel)
        
        # Web view for PDF - EXPANDABLE.  Without the optional
        # PyQt6-WebEngine component the viewer shows an explanation instead of
        # crashing the whole application at import time.
        if not WEB_ENGINE_AVAILABLE:
            self.web_view = None
            placeholder = QLabel(
                "<b>PDF viewing is not available</b><br><br>"
                "The optional component <code>PyQt6-WebEngine</code> is not "
                "installed on this computer.<br>"
                "Install it with <code>pip install PyQt6-WebEngine</code>."
            )
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #aaa; padding: 20px;")
            placeholder.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
            layout.addWidget(placeholder)
            return

        self.web_view = QWebEngineView()

        # Configure web engine settings for PDF support
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        self.web_view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )
        self.web_view.setHtml(
            "<html><body style='background: #2a2a2a; color: #aaa; "
            "display: flex; align-items: center; justify-content: center; "
            "height: 100vh; margin: 0;'>"
            "<div style='text-align: center;'>"
            "<h2>No PDF Loaded</h2>"
            "<p>Select a file and click 'Load PDF' to view</p>"
            "</div></body></html>"
        )
        layout.addWidget(self.web_view)
        
        # Bottom buttons
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(10, 5, 10, 10)
        
        self.toggle_panel_btn = QPushButton("Hide Panel")
        self.toggle_panel_btn.clicked.connect(self.toggle_panel)
        bottom_layout.addWidget(self.toggle_panel_btn)
        
        bottom_layout.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)
        
        layout.addLayout(bottom_layout)
        
    def _create_control_panel(self) -> QGroupBox:
        """Create the control panel for file selection and decryption"""
        panel = QGroupBox("File Selection & Decryption")
        layout = QVBoxLayout(panel)
        
        # File selection
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("PDF File:"))
        
        self.file_path_input = QLineEdit()
        self.file_path_input.setPlaceholderText("Select encrypted PDF file...")
        self.file_path_input.setReadOnly(True)
        file_layout.addWidget(self.file_path_input, stretch=1)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_file)
        file_layout.addWidget(browse_btn)
        
        layout.addLayout(file_layout)
        
        # Encryption settings
        encryption_layout = QHBoxLayout()
        
        # Encrypted checkbox
        self.encrypted_check = QCheckBox("File is encrypted")
        self.encrypted_check.setChecked(True)
        self.encrypted_check.toggled.connect(self._on_encrypted_toggled)
        encryption_layout.addWidget(self.encrypted_check)
        
        # Encryption method with Auto Detect option
        encryption_layout.addWidget(QLabel("Method:"))
        self.encryption_combo = QComboBox()
        self.encryption_combo.addItem("Auto Detect", "auto")
        self.encryption_combo.addItem("AES-GCM", "aes-gcm")
        self.encryption_combo.addItem("GPG", "gpg")
        self.encryption_combo.addItem("pikepdf (PDF native)", "pikepdf")
        self.encryption_combo.currentIndexChanged.connect(self._on_method_changed)
        encryption_layout.addWidget(self.encryption_combo)
        
        # Password (hidden for pikepdf since it uses built-in PDF viewer UI)
        self.password_label = QLabel("Password:")
        encryption_layout.addWidget(self.password_label)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Enter password...")
        encryption_layout.addWidget(self.password_input, stretch=1)
        
        layout.addLayout(encryption_layout)
        
        # Load button
        load_layout = QHBoxLayout()
        load_layout.addStretch()
        
        load_btn = QPushButton("Load PDF")
        load_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        load_btn.setMinimumHeight(35)
        load_btn.clicked.connect(self.load_pdf)
        load_layout.addWidget(load_btn)
        
        layout.addLayout(load_layout)
        
        return panel
        
    def browse_file(self):
        """Browse for PDF file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select PDF File",
            "",
            "Supported Files (*.pdf *.gpg *.aes *.usrx);;All Files (*.*)"
        )
        
        if file_path:
            # Validate file type
            error = self._validate_file_type(file_path)
            if error:
                QMessageBox.critical(self, "Unsupported File", error)
                return
            
            self.file_path_input.setText(file_path)
            
            # Try to detect file format (but don't auto-set method)
            try:
                file_format = detect_file_format(file_path)
                if file_format == 'usrx':
                    info = get_file_info(file_path)
                    logger.info(f"Detected USRX file: {info.get('encryption_type')}")
                else:
                    logger.info(f"Detected standard format file")
            except Exception as e:
                logger.debug(f"Could not detect file format: {e}")

    def _validate_file_type(self, file_path: str) -> str:
        """
        Validate that the file is actually a supported type.
        Returns error message or empty string if valid.
        """
        import os
        import struct

        allowed_extensions = {'.pdf', '.gpg', '.aes', '.usrx'}
        ext = os.path.splitext(file_path)[1].lower()

        if ext not in allowed_extensions:
            return (f"Unsupported file type: '{ext}'.\n\n"
                    f"Only PDF, GPG, AES, and USRX files are supported.")

        # Verify actual file content using magic bytes
        try:
            with open(file_path, 'rb') as f:
                header = f.read(16)

            if not header:
                return "File is empty."

            # PDF: starts with %PDF
            if ext == '.pdf':
                if not header.startswith(b'%PDF'):
                    return ("The file does not appear to be a valid PDF file.\n\n"
                            "Renaming a file to .pdf does not make it a PDF.")

            # GPG: starts with 0x8503 (binary GPG) or '-----BEGIN' (ASCII armor)
            elif ext == '.gpg':
                is_gpg_binary = header[0] in (0x85, 0xC1, 0x89, 0x99, 0xA3)
                is_gpg_ascii = header.startswith(b'-----BEGIN')
                if not is_gpg_binary and not is_gpg_ascii:
                    return ("The file does not appear to be a valid GPG encrypted file.")

            # AES/USRX: custom format - trust the encryption layer but check not plaintext
            elif ext in ('.aes', '.usrx'):
                # Check it's not a plain text or PDF file
                if header.startswith(b'%PDF'):
                    return "AES/USRX file appears to be a plain PDF. Please encrypt it first."
                if all(32 <= b < 127 or b in (9, 10, 13) for b in header):
                    # Entirely printable ASCII - could be plaintext, but also could be base64
                    # Only reject if it looks like an obvious text file
                    if header.startswith(b'<?xml') or header.startswith(b'<!DOCTYPE'):
                        return "The file appears to be a plain XML/text file, not an encrypted file."

        except (IOError, OSError) as e:
            return f"Cannot read file: {e}"
        except Exception as e:
            logger.warning(f"File validation error: {e}")
            # Don't block - let decryption fail with a proper error

        return ""
        
    def _on_method_changed(self, index):
        """Handle encryption method change - hide password for pikepdf"""
        method = self.encryption_combo.currentData()
        is_pikepdf = (method == 'pikepdf')
        self.password_label.setVisible(not is_pikepdf)
        self.password_input.setVisible(not is_pikepdf)
        if is_pikepdf:
            self.password_input.clear()
            # Show info about pikepdf password handling
            from PyQt6.QtWidgets import QToolTip
            QToolTip.showText(
                self.encryption_combo.mapToGlobal(self.encryption_combo.rect().bottomLeft()),
                "pikepdf-encrypted files: the PDF viewer will prompt for the password directly."
            )
        
    def _on_encrypted_toggled(self, checked):
        """Handle encrypted checkbox toggle"""
        self.encryption_combo.setEnabled(checked)
        self.password_input.setEnabled(checked)
        
    def load_pdf(self):
        """Load and decrypt PDF"""
        file_path = self.file_path_input.text().strip()
        
        if not file_path:
            QMessageBox.warning(self, "No File", "Please select a PDF file")
            return
        
        if not os.path.exists(file_path):
            QMessageBox.critical(self, "File Not Found", f"File not found:\n{file_path}")
            return
        
        # Validate file type before trying to load
        error = self._validate_file_type(file_path)
        if error:
            QMessageBox.critical(self, "Unsupported File", error)
            return
        
        # Check if encrypted
        is_encrypted = self.encrypted_check.isChecked()
        
        if not is_encrypted:
            # Not encrypted - load directly via URL (pikepdf native)
            self._load_pdf_from_file(file_path)
            return
        
        # Get encryption method
        encryption_method = self.encryption_combo.currentData()
        
        if encryption_method == 'pikepdf':
            # pikepdf encrypted - load directly via URL
            self._load_pdf_from_file(file_path)
            return
        
        # For AES-GCM/GPG - need password
        password = self.password_input.text()
        
        if not password:
            QMessageBox.warning(self, "No Password", "Please enter password")
            return
        
        # Auto-detect if requested
        if encryption_method == 'auto':
            logger.info("Auto-detecting encryption method...")
            
            try:
                # Try to detect from file
                file_format = detect_file_format(file_path)
                
                if file_format == 'usrx':
                    # Get encryption type from USRX header
                    info = get_file_info(file_path)
                    encryption_method = info.get('encryption_type', 'unknown')
                    logger.info(f"Auto-detected: {encryption_method} (USRX)")
                else:
                    # Standard format - try to detect
                    encryption_method = detect_encryption_method(file_path)
                    logger.info(f"Auto-detected: {encryption_method}")
                    
            except Exception as e:
                logger.exception("Auto-detection failed")
                QMessageBox.critical(
                    self, "Auto-Detection Failed",
                    f"Could not auto-detect encryption method:\n{str(e)}\n\n"
                    "Please select method manually."
                )
                return
        
        # Create decrypt task
        task = PDFDecryptTask(file_path, password, encryption_method)
        
        progress_dialog = ProgressDialog(task, self)
        progress_dialog.start_task()
        progress_dialog.exec()
        
        if task.success and task.decrypted_data:
            # Display decrypted PDF using QByteArray
            self._display_pdf_from_bytes(task.decrypted_data)
        
    def _load_pdf_from_file(self, file_path: str):
        """Load PDF directly from file using QUrl (for pikepdf or unencrypted)"""
        try:
            # Convert to absolute path
            abs_path = os.path.abspath(file_path)
            
            # Create file URL
            file_url = QUrl.fromLocalFile(abs_path)
            
            # Load PDF in web engine view
            self.web_view.setUrl(file_url)
            
            logger.info(f"Loaded PDF from file: {file_path}")
            
        except Exception as e:
            logger.exception("Error loading PDF from file")
            QMessageBox.critical(
                self, "Load Error",
                f"Failed to load PDF:\n{str(e)}"
            )
        
    def _display_pdf_from_bytes(self, pdf_data: bytes):
        """Display PDF data from bytes using QByteArray"""
        if not WEB_ENGINE_AVAILABLE or self.web_view is None:
            logger.info("PDF display skipped - PyQt6-WebEngine is not installed")
            return

        try:
            # Store in buffer
            if self.decrypted_buffer:
                self.decrypted_buffer.close()
            
            self.decrypted_buffer = BytesIO(pdf_data)
            
            # Reset buffer position
            self.decrypted_buffer.seek(0)
            
            # Convert BytesIO to QByteArray
            pdf_qbytes = QByteArray(self.decrypted_buffer.read())
            
            # Load into QWebEngineView using setContent (from memory - NO temp file!)
            self.web_view.setContent(pdf_qbytes, "application/pdf")
            
            logger.info(f"Displayed PDF from memory: {len(pdf_data)} bytes")
            
        except Exception as e:
            logger.exception("Error displaying PDF")
            QMessageBox.critical(
                self, "Display Error",
                f"Failed to display PDF:\n{str(e)}"
            )
        
    def toggle_panel(self):
        """Toggle control panel visibility"""
        is_visible = self.control_panel.isVisible()
        self.control_panel.setVisible(not is_visible)
        
        if is_visible:
            self.toggle_panel_btn.setText("Show Panel")
        else:
            self.toggle_panel_btn.setText("Hide Panel")
        
    def closeEvent(self, event):
        """Handle window close"""
        # Clean up buffer
        if self.decrypted_buffer:
            self.decrypted_buffer.close()
            self.decrypted_buffer = None
        
        event.accept()
