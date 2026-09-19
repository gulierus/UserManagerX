"""
Encrypted file data source implementation
Supports both GPG and AES-GCM encryption methods
"""

import logging
import json
from pathlib import Path
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QInputDialog, QComboBox,
    QGroupBox
)
from PyQt6.QtCore import Qt

from models import Source, Class, Person
from utils.source_serialization import source_from_dict
from utils.encryption import (
    decrypt_file, get_available_methods, detect_encryption_method,
    get_encryption_info
)

logger = logging.getLogger(__name__)


class FileSourceWidget(QWidget):
    """Widget for loading data from encrypted files"""
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.detected_method = None
        self.init_ui()
        self.update_encryption_availability()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Instructions
        info = QLabel(
            "Load student data from an encrypted JSON file. "
            "Supports both GPG and AES-GCM encryption methods."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(info)
        
        # Encryption method group
        method_group = QGroupBox("Encryption Method")
        method_layout = QVBoxLayout()
        
        method_select_layout = QHBoxLayout()
        method_select_layout.addWidget(QLabel("Method:"))
        
        self.method_combo = QComboBox()
        self.method_combo.addItem("Auto-detect", "auto")
        self.method_combo.addItem("GPG (OpenPGP)", "gpg")
        self.method_combo.addItem("AES-GCM", "aes-gcm")
        self.method_combo.currentIndexChanged.connect(self.on_method_changed)
        method_select_layout.addWidget(self.method_combo, stretch=1)
        
        method_layout.addLayout(method_select_layout)
        
        # Status label for encryption availability
        self.encryption_status_label = QLabel()
        self.encryption_status_label.setWordWrap(True)
        self.encryption_status_label.setStyleSheet("color: #aaa; font-size: 10px;")
        method_layout.addWidget(self.encryption_status_label)
        
        method_group.setLayout(method_layout)
        layout.addWidget(method_group)
        
        # File selection
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("File:"))
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("Select encrypted JSON file...")
        self.file_input.setReadOnly(True)
        self.file_input.textChanged.connect(self.on_file_changed)
        file_layout.addWidget(self.file_input, stretch=1)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.on_browse)
        file_layout.addWidget(browse_btn)
        layout.addLayout(file_layout)
        
        # File info label
        self.file_info_label = QLabel()
        self.file_info_label.setWordWrap(True)
        self.file_info_label.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
        layout.addWidget(self.file_info_label)
        
        # Password
        pass_layout = QHBoxLayout()
        pass_layout.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Enter decryption password")
        pass_layout.addWidget(self.password_input)
        layout.addLayout(pass_layout)
        
        # Load button
        load_layout = QHBoxLayout()
        self.load_btn = QPushButton("Load Data")
        self.load_btn.clicked.connect(self.on_load)
        load_layout.addWidget(self.load_btn)
        load_layout.addStretch()
        layout.addLayout(load_layout)
        
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
            
            # Disable method combo items if not available
            for i in range(self.method_combo.count()):
                method = self.method_combo.itemData(i)
                if method == "auto":
                    continue
                enabled = available.get(method, False)
                
                # Update item text with availability indicator
                if method == "gpg":
                    text = "GPG (OpenPGP)"
                elif method == "aes-gcm":
                    text = "AES-GCM"
                else:
                    text = method
                
                if not enabled:
                    text += " [Not Available]"
                
                self.method_combo.setItemText(i, text)
                
                # Note: QComboBox doesn't support disabling individual items easily
                # So we'll check availability when user tries to load
            
            logger.info(f"Encryption availability: GPG={available['gpg']}, AES-GCM={available['aes-gcm']}")
            
        except Exception as e:
            logger.exception("Error checking encryption availability")
            self.encryption_status_label.setText("Error checking encryption libraries")
    
    def on_method_changed(self, index):
        """Handle encryption method selection change"""
        method = self.method_combo.currentData()
        logger.debug(f"Encryption method changed to: {method}")
        
        # Update file filter if browsing
        if method == "gpg":
            self.file_input.setPlaceholderText("Select GPG encrypted file (*.gpg)...")
        elif method == "aes-gcm":
            self.file_input.setPlaceholderText("Select AES-GCM encrypted file (*.aes, *.json)...")
        else:
            self.file_input.setPlaceholderText("Select encrypted JSON file...")
    
    def on_file_changed(self, file_path):
        """Handle file path change - try to detect encryption method"""
        if not file_path:
            self.file_info_label.clear()
            self.detected_method = None
            return
        
        try:
            # Try to get file info
            info = get_encryption_info(file_path)
            
            self.detected_method = info.get("method")
            
            info_text = f"Detected: {info.get('method', 'unknown').upper()}"
            if "algorithm" in info:
                info_text += f" ({info['algorithm']})"
            
            file_size = info.get("file_size", 0)
            if file_size > 0:
                if file_size < 1024:
                    size_str = f"{file_size} bytes"
                elif file_size < 1024 * 1024:
                    size_str = f"{file_size / 1024:.1f} KB"
                else:
                    size_str = f"{file_size / (1024 * 1024):.1f} MB"
                info_text += f" | Size: {size_str}"
            
            self.file_info_label.setText(info_text)
            
            # If auto-detect is selected, update the label
            if self.method_combo.currentData() == "auto":
                logger.info(f"Auto-detected encryption method: {self.detected_method}")
            
        except Exception as e:
            logger.debug(f"Could not detect file encryption info: {e}")
            self.file_info_label.setText("Could not detect encryption method")
            self.detected_method = None
        
    def on_browse(self):
        """Browse for encrypted file"""
        # Determine file filter based on selected method
        method = self.method_combo.currentData()
        
        # ".json" is deliberately NOT offered. An AES-GCM file is internally a
        # JSON envelope, so the extension looks plausible - but the application
        # only ever writes ".aes" and ".gpg", and a PLAINTEXT JSON export
        # cannot be loaded at all: detect_encryption_method() rejects it with
        # "Cannot detect encryption method from file format". Offering ".json"
        # invited users to pick an unencrypted file that can never work.
        # "All Files (*)" is still there for a file that was renamed.
        if method == "gpg":
            filter_str = "GPG Encrypted Files (*.gpg);;All Files (*)"
        elif method == "aes-gcm":
            filter_str = "AES Encrypted Files (*.aes);;All Files (*)"
        else:
            filter_str = "Encrypted Files (*.gpg *.aes);;All Files (*)"
        
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Encrypted JSON File",
            "",
            filter_str
        )
        
        if file_path:
            self.file_input.setText(file_path)
            
    def on_load(self):
        """Load data from encrypted file"""
        file_path = self.file_input.text().strip()
        password = self.password_input.text()
        
        if not file_path:
            QMessageBox.warning(self, "No File", "Please select a file to load")
            return
            
        if not password:
            QMessageBox.warning(self, "No Password", "Please enter the decryption password")
            return
        
        # Get selected method
        selected_method = self.method_combo.currentData()
        
        # Determine which method to use
        if selected_method == "auto":
            method = None  # Auto-detect
            method_name = "auto-detected"
        else:
            method = selected_method
            method_name = selected_method.upper()
        
        # Check if selected method is available
        if method is not None:
            available = get_available_methods()
            if not available.get(method, False):
                QMessageBox.critical(
                    self,
                    "Method Not Available",
                    f"The selected encryption method ({method_name}) is not available.\n\n"
                    f"{'Install GnuPG and python-gnupg library for GPG support.' if method == 'gpg' else 'Install PyCryptodome library for AES-GCM support.'}"
                )
                return
        
        try:
            logger.info(f"Attempting to decrypt file: {file_path} using method: {method_name}")
            
            # Decrypt and load file
            decrypted_data = decrypt_file(file_path, password, method=method)
            
            # Parse JSON data
            try:
                data = json.loads(decrypted_data)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON: {e}")
                QMessageBox.critical(
                    self,
                    "Invalid Data",
                    f"Decrypted data is not valid JSON.\n\n"
                    f"The file may be corrupted or not in the expected format."
                )
                return
            
            # Parse JSON data into Source object
            source = self.parse_json_to_source(data, Path(file_path).stem)
            
            # Ask user for source name
            default_name = source.name
            replaced_source = None

            while True:
                name, ok = QInputDialog.getText(
                    self,
                    "Name Source",
                    "Enter name for this source:",
                    QLineEdit.EchoMode.Normal,
                    default_name
                )
                
                if not ok or not name:
                    return
                
                name = name.strip()
                if not name:
                    QMessageBox.warning(self, "Invalid Name", "Source name cannot be empty")
                    continue
                
                # Check for duplicates
                if self.source_manager.source_name_exists(name):
                    reply = QMessageBox.question(
                        self,
                        "Duplicate Name",
                        f"Source '{name}' already exists. Replace it?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if reply == QMessageBox.StandardButton.No:
                        default_name = name
                        continue

                    # The question promises a replacement - actually perform it.
                    # Simply adding another source with the same name produced
                    # two indistinguishable entries and every lookup by name
                    # returned the old one, so the freshly loaded data was
                    # unreachable.
                    replaced_source = self.source_manager.get_source_by_name(name)

                source.name = name
                break

            if replaced_source is not None:
                self.source_manager.remove_source(replaced_source)
                logger.info(f"Replaced existing source: {name}")

            self.source_manager.add_source(source)
            
            person_count = len(source.get_all_persons())
            class_count = len(source.classes)
            
            QMessageBox.information(
                self,
                "Success",
                f"Successfully loaded {person_count} student(s) from {class_count} class(es)\n\n"
                f"Source: {name}\n"
                f"Encryption: {method_name}"
            )
            
            logger.info(f"Successfully loaded file source: {source.name} "
                       f"({person_count} persons, {class_count} classes)")
            
            # Clear password for security
            self.password_input.clear()
            
        except FileNotFoundError as e:
            logger.error(f"File not found: {e}")
            QMessageBox.critical(
                self,
                "File Not Found",
                f"The selected file could not be found:\n{file_path}"
            )
            
        except ValueError as e:
            logger.error(f"Validation error: {e}")
            QMessageBox.critical(
                self,
                "Invalid File",
                f"The file format is invalid or unsupported:\n\n{str(e)}"
            )
            
        except RuntimeError as e:
            error_msg = str(e)
            logger.error(f"Decryption failed: {error_msg}")
            
            if "incorrect password" in error_msg.lower():
                QMessageBox.critical(
                    self,
                    "Incorrect Password",
                    "Decryption failed. Please check your password and try again."
                )
            elif "not available" in error_msg.lower():
                QMessageBox.critical(
                    self,
                    "Encryption Method Not Available",
                    error_msg
                )
            else:
                QMessageBox.critical(
                    self,
                    "Decryption Failed",
                    f"Failed to decrypt the file:\n\n{error_msg}"
                )
        
        except Exception as e:
            logger.exception("Unexpected error loading encrypted file")
            QMessageBox.critical(
                self,
                "Error",
                f"An unexpected error occurred:\n\n{str(e)}"
            )
            
    def parse_json_to_source(self, data: dict, filename: str) -> Source:
        """
        Parse JSON data into a Source object.

        Args:
            data: Dictionary with source data.
            filename: Filename used as the default source name.

        Returns:
            Source object.

        Raises:
            ValueError: If the data format is invalid.
        """
        try:
            # Shared with the exporter, so everything that was written is read
            # back: home directory, group memberships, password policy flags
            # and the account status included.
            return source_from_dict(data, default_name=filename, readonly=True)
        except ValueError:
            raise
        except Exception as e:
            logger.exception("Error parsing JSON to source")
            raise ValueError(f"Failed to parse JSON data: {str(e)}")
