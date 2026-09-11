"""
JSON export functionality with encryption support
Exports source data to encrypted JSON files with both GPG and AES-GCM support
"""

import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QComboBox, QGroupBox,
    QCheckBox
)
from PyQt6.QtCore import Qt

from models import Source, Class, Person
from ui.file_path_selector_widget import FilePathSelectorWidget
from utils.encryption import encrypt_file, get_available_methods
from utils.json_export_task import JSONExportTask
from utils.source_serialization import source_to_dict
from utils.progress_dialog import ProgressDialog

logger = logging.getLogger(__name__)


class JSONExportWidget(QWidget):
    """Widget for exporting data to encrypted JSON files"""
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.current_source = None  # Set by the operations tab
        self.init_ui()
        self.update_encryption_availability()
        
    def _make_auto_name(self) -> str:
        """Generate just the filename part for auto-generate mode."""
        method = self.method_combo.currentData() if hasattr(self, "method_combo") else "aes"
        ext = "gpg" if method == "gpg" else ("aes" if method == "aes-gcm" else "enc")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.current_source:
            safe = "".join(
                c for c in self.current_source.name if c.isalnum() or c in (" ", "-", "_")
            ).replace(" ", "_")
        else:
            safe = "export"
        return f"{safe}_{timestamp}.{ext}"

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Instructions
        info = QLabel(
            "Export source data to an encrypted JSON file. "
            "Choose an encryption method and provide a password. "
            "The source to export is selected in the panel above."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(info)
        
        # Source info label (read-only, updated from external source selection)
        self.source_info_label = QLabel("<i>No source selected</i>")
        self.source_info_label.setWordWrap(True)
        self.source_info_label.setStyleSheet(
            "color: #888; font-size: 10px; font-style: italic; padding: 4px;"
            " border: 1px solid #444; border-radius: 3px;"
        )
        layout.addWidget(self.source_info_label)
        
        # Encryption method group
        method_group = QGroupBox("Encryption Method")
        method_layout = QVBoxLayout()
        
        method_select_layout = QHBoxLayout()
        method_select_layout.addWidget(QLabel("Method:"))
        
        self.method_combo = QComboBox()
        self.method_combo.addItem("AES-GCM (Recommended)", "aes-gcm")
        self.method_combo.addItem("GPG (OpenPGP)", "gpg")
        self.method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_select_layout.addWidget(self.method_combo, stretch=1)
        
        method_layout.addLayout(method_select_layout)
        
        # Status label for encryption availability
        self.encryption_status_label = QLabel()
        self.encryption_status_label.setWordWrap(True)
        self.encryption_status_label.setStyleSheet("color: #aaa; font-size: 10px;")
        method_layout.addWidget(self.encryption_status_label)
        
        method_group.setLayout(method_layout)
        layout.addWidget(method_group)
        
        # ---- Output file — reusable FilePathSelectorWidget ----
        self.file_selector = FilePathSelectorWidget(
            label="Output File",
            auto_name_factory=self._make_auto_name,
            file_filter="AES Encrypted Files (*.aes);;GPG Encrypted Files (*.gpg);;All Files (*)",
            default_suffix=".aes",
            parent=self,
        )
        layout.addWidget(self.file_selector)
        
        # Password
        pass_group = QGroupBox("Encryption Password")
        pass_layout = QVBoxLayout()
        
        pass_input_layout = QHBoxLayout()
        pass_input_layout.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Enter encryption password (min 8 characters)")
        pass_input_layout.addWidget(self.password_input)
        pass_layout.addLayout(pass_input_layout)
        
        confirm_pass_layout = QHBoxLayout()
        confirm_pass_layout.addWidget(QLabel("Confirm:"))
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirm.setPlaceholderText("Confirm encryption password")
        confirm_pass_layout.addWidget(self.password_confirm)
        pass_layout.addLayout(confirm_pass_layout)
        
        pass_group.setLayout(pass_layout)
        layout.addWidget(pass_group)
        
        # Export button
        export_layout = QHBoxLayout()
        self.export_btn = QPushButton("Export Data")
        self.export_btn.clicked.connect(self.on_export)
        self.export_btn.setEnabled(False)
        export_layout.addWidget(self.export_btn)
        export_layout.addStretch()
        layout.addLayout(export_layout)
        
        layout.addStretch()
    
    def update_encryption_availability(self):
        """Update UI to show which encryption methods are available"""
        try:
            available = get_available_methods()
            
            status_parts = []
            if available["gpg"]:
                status_parts.append("✓ GPG available")
            else:
                status_parts.append("✗ GPG not available (install GnuPG and python-gnupg)")
            
            if available["aes-gcm"]:
                status_parts.append("✓ AES-GCM available")
            else:
                status_parts.append("✗ AES-GCM not available (install PyCryptodome)")
            
            self.encryption_status_label.setText(" | ".join(status_parts))
            
            # Update combo box items
            for i in range(self.method_combo.count()):
                method = self.method_combo.itemData(i)
                enabled = available.get(method, False)
                
                if method == "gpg":
                    text = "GPG (OpenPGP)"
                elif method == "aes-gcm":
                    text = "AES-GCM (Recommended)"
                else:
                    text = method
                
                if not enabled:
                    text += " [Not Available]"
                
                self.method_combo.setItemText(i, text)
            
            # Select first available method
            for i in range(self.method_combo.count()):
                method = self.method_combo.itemData(i)
                if available.get(method, False):
                    self.method_combo.setCurrentIndex(i)
                    break
            
            logger.info(
                f"Encryption availability: GPG={available['gpg']}, AES-GCM={available['aes-gcm']}"
            )
            
        except Exception as e:
            logger.exception("Error checking encryption availability")
            self.encryption_status_label.setText("Error checking encryption libraries")
    
    def set_source(self, source):
        """Set the current source (called from the operations tab)"""
        self.current_source = source
        
        if source is None:
            self.source_info_label.setText("<i>No source selected</i>")
            self.export_btn.setEnabled(False)
            return
        
        try:
            stats = source.get_statistics()
            info_text = (
                f"<b>{source.name}</b> | "
                f"Type: {source.source_type} | "
                f"Classes: {stats['total_classes']} | "
                f"Students: {stats['total_persons']}"
            )
            if source.readonly:
                info_text += " | <i>Read-only</i>"
            self.source_info_label.setText(info_text)
        except Exception:
            self.source_info_label.setText(f"<b>{source.name}</b>")
        
        self.export_btn.setEnabled(True)
        # Refresh auto-generated filename now that source is known
        self.file_selector.refresh_auto_name()

    def _on_method_changed(self, _index: int):
        """Refresh auto filename when method changes (extension changes)."""
        self.file_selector.refresh_auto_name()
        
        method = self.method_combo.currentData()
        logger.debug(f"Encryption method changed to: {method}")
       
    def on_export(self):
        """Export source data to encrypted JSON file (asynchronously)"""
        source = self.current_source
        if source is None:
            QMessageBox.warning(self, "No Source", "Please select a source in the panel above")
            return

        output_path = self.file_selector.get_path().strip()
        if not output_path:
            QMessageBox.warning(
                self, "No File",
                "Please specify an output file or select a folder for auto-generated filename."
            )
            return

        password = self.password_input.text()
        password_confirm = self.password_confirm.text()

        if not password:
            QMessageBox.warning(self, "No Password", "Please enter an encryption password")
            return

        if len(password) < 8:
            reply = QMessageBox.question(
                self, "Weak Password",
                "Password is less than 8 characters. This is not recommended for security.\n\n"
                "Do you want to continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        if password != password_confirm:
            QMessageBox.warning(
                self, "Password Mismatch",
                "Password and confirmation do not match. Please re-enter."
            )
            return

        method = self.method_combo.currentData()
        method_name = method.upper() if method else "unknown"

        available = get_available_methods()
        if not available.get(method, False):
            QMessageBox.critical(
                self, "Method Not Available",
                f"The selected encryption method ({method_name}) is not available.\n\n"
                f"{'Install GnuPG and python-gnupg library for GPG support.' if method == 'gpg' else 'Install PyCryptodome library for AES-GCM support.'}"
            )
            return

        if Path(output_path).exists():
            reply = QMessageBox.question(
                self, "File Exists",
                f"File already exists:\n{output_path}\n\nOverwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        logger.info(f"Exporting source '{source.name}' to {output_path} using {method_name}")

        task = JSONExportTask(source, output_path, password, method)
        progress_dialog = ProgressDialog(task, self)
        progress_dialog.start_task()
        progress_dialog.exec()

        if task.success:
            logger.info(f"Successfully exported source to: {output_path}")
            self.password_input.clear()
            self.password_confirm.clear()
            # Refresh auto-filename for next export
            self.file_selector.refresh_auto_name()

    def source_to_json(self, source: Source) -> Dict[str, Any]:
        """
        Convert a Source object to a JSON-serialisable dictionary.

        Args:
            source: Source object to convert.

        Returns:
            Dictionary representation of the source.
        """
        # The previous local copy of this conversion never returned its result
        # and had drifted away from the one used by the export task.
        return source_to_dict(source)
