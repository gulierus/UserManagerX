"""
Encryption Selector Widget
Universal widget for encryption method selection with availability checking
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, 
    QLineEdit, QGroupBox
)
from PyQt6.QtCore import pyqtSignal

from utils.encryption import get_available_methods

logger = logging.getLogger(__name__)


class EncryptionSelectorWidget(QWidget):
    """
    Universal encryption selector widget
    
    Features:
    - Automatic detection of available encryption methods
    - Visual indication of availability
    - Support for .usrx format
    - Password input with confirmation
    - Emits signals when selection changes
    """
    
    # Signals
    method_changed = pyqtSignal(str)  # Emits encryption method
    
    def __init__(self, include_pikepdf: bool = False, 
                 include_none: bool = False,
                 require_password: bool = True,
                 parent=None):
        """
        Initialize encryption selector
        
        Args:
            include_pikepdf: Whether to include pikepdf option
            include_none: Whether to include "No encryption" option
            require_password: Whether password fields are shown
            parent: Parent widget
        """
        super().__init__(parent)
        
        self.include_pikepdf = include_pikepdf
        self.include_none = include_none
        self.require_password = require_password
        
        self.init_ui()
        self.update_encryption_availability()
        
    def init_ui(self):
        """Initialize user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Encryption method selection
        method_layout = QHBoxLayout()
        method_layout.addWidget(QLabel("Encryption Method:"))
        
        self.method_combo = QComboBox()
        self.method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_layout.addWidget(self.method_combo, stretch=1)
        
        layout.addLayout(method_layout)
        
        # File format selection
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("File Format:"))
        
        self.format_combo = QComboBox()
        self.format_combo.addItem("Standard Format", "standard")
        self.format_combo.addItem("USRX Container Format (.usrx)", "usrx")
        format_layout.addWidget(self.format_combo, stretch=1)
        
        layout.addLayout(format_layout)
        
        # Status label
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #aaa; font-size: 10px;")
        layout.addWidget(self.status_label)
        
        # Password fields (if required)
        if self.require_password:
            self.password_group = QGroupBox("Password")
            password_layout = QVBoxLayout(self.password_group)
            
            # Password
            pass_layout = QHBoxLayout()
            pass_layout.addWidget(QLabel("Password:"))
            self.password_input = QLineEdit()
            self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_input.setPlaceholderText("Enter encryption password")
            pass_layout.addWidget(self.password_input)
            password_layout.addLayout(pass_layout)
            
            # Confirm password
            confirm_layout = QHBoxLayout()
            confirm_layout.addWidget(QLabel("Confirm:"))
            self.password_confirm = QLineEdit()
            self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_confirm.setPlaceholderText("Confirm password")
            confirm_layout.addWidget(self.password_confirm)
            password_layout.addLayout(confirm_layout)
            
            layout.addWidget(self.password_group)
        
    def update_encryption_availability(self):
        """Update encryption method list based on availability"""
        # Save current selection
        current_method = self.method_combo.currentData()
        
        # Clear combo box
        self.method_combo.clear()
        
        # Check available methods
        available = get_available_methods()
        
        # Add "None" option if requested
        if self.include_none:
            self.method_combo.addItem("No Encryption", "none")
        
        # Add pikepdf if requested
        if self.include_pikepdf:
            self.method_combo.addItem(
                "pikepdf (PDF native, viewable in PDF readers)", 
                "pikepdf"
            )
        
        # Add AES-GCM
        if available.get("aes-gcm"):
            self.method_combo.addItem("AES-GCM (Recommended)", "aes-gcm")
        else:
            self.method_combo.addItem(
                "AES-GCM [Not Available - Install PyCryptodome]", 
                "aes-gcm-unavailable"
            )
        
        # Add GPG
        if available.get("gpg"):
            self.method_combo.addItem("GPG (OpenPGP)", "gpg")
        else:
            self.method_combo.addItem(
                "GPG [Not Available - Install GnuPG and python-gnupg]", 
                "gpg-unavailable"
            )
        
        # Update status label
        status_parts = []
        if available["aes-gcm"]:
            status_parts.append("✓ AES-GCM available")
        else:
            status_parts.append("✗ AES-GCM not available")
        
        if available["gpg"]:
            status_parts.append("✓ GPG available")
        else:
            status_parts.append("✗ GPG not available")
        
        self.status_label.setText(" | ".join(status_parts))
        
        # Restore selection if possible
        if current_method:
            index = self.method_combo.findData(current_method)
            if index >= 0:
                self.method_combo.setCurrentIndex(index)
        
        logger.debug(f"Encryption availability: {available}")
        
    def _on_method_changed(self):
        """Handle method selection change"""
        method = self.get_encryption_method()
        self.method_changed.emit(method)
        
        # Update format combo availability
        if method in ['pikepdf', 'none']:
            # pikepdf and none don't support USRX
            self.format_combo.setCurrentIndex(0)  # Standard
            self.format_combo.setEnabled(False)
        else:
            self.format_combo.setEnabled(True)
        
    def get_encryption_method(self) -> str:
        """Get selected encryption method"""
        method = self.method_combo.currentData()
        
        # Handle unavailable methods
        if method in ['aes-gcm-unavailable', 'gpg-unavailable']:
            return None
        
        return method
        
    def get_file_format(self) -> str:
        """Get selected file format (standard or usrx)"""
        return self.format_combo.currentData()
        
    def get_password(self) -> str:
        """Get password (if password fields are shown)"""
        if self.require_password:
            return self.password_input.text()
        return ""
        
    def get_password_confirm(self) -> str:
        """Get password confirmation"""
        if self.require_password:
            return self.password_confirm.text()
        return ""
        
    def set_password(self, password: str):
        """Set password (for testing or restoration)"""
        if self.require_password:
            self.password_input.setText(password)
            self.password_confirm.setText(password)
        
    def clear_password(self):
        """Clear password fields"""
        if self.require_password:
            self.password_input.clear()
            self.password_confirm.clear()
        
    def validate(self) -> tuple:
        """
        Validate encryption settings
        
        Returns:
            (is_valid, error_message) tuple
        """
        method = self.get_encryption_method()
        
        if method is None:
            return (False, "Selected encryption method is not available")
        
        if method == 'none':
            return (True, "")
        
        if self.require_password:
            password = self.get_password()
            confirm = self.get_password_confirm()
            
            if not password:
                return (False, "Please enter a password")
            
            if password != confirm:
                return (False, "Passwords do not match")
            
            if len(password) < 8:
                return (False, "Password should be at least 8 characters")
        
        return (True, "")
        
    def is_method_available(self, method: str) -> bool:
        """Check if a specific method is available"""
        available = get_available_methods()
        
        if method == 'pikepdf':
            return True  # Always available if pikepdf is installed
        
        return available.get(method, False)
