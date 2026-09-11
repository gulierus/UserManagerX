"""
EduPage data source implementation with 2FA and dual mode support
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QRadioButton, QButtonGroup, QMessageBox,
    QScrollArea
)
from PyQt6.QtCore import Qt

from utils.edupage_coordinator import EduPageLoadCoordinator

logger = logging.getLogger(__name__)


class EdupageSourceWidget(QWidget):
    """
    Widget for configuring EduPage data source with 2FA support.
    
    Supports two loading modes:
    1. Selective Load: Load class names, select which to load, then load students
    2. Reserved for future implementation
    """
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.coordinator = EduPageLoadCoordinator(self, source_manager)
        
        # Connect coordinator signals
        self.coordinator.loading_complete.connect(self.on_loading_complete)
        self.coordinator.loading_failed.connect(self.on_loading_failed)
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
       
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)       

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)
        
        # === Instructions ===
        info = QLabel(
            "Connect to EduPage to load student data. "
            "This source supports two-factor authentication (2FA). "
            "Choose a loading mode below."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(info)
        
        # === Credentials Section ===
        cred_group = QGroupBox("EduPage Credentials")
        cred_layout = QVBoxLayout(cred_group)
        
        # Subdomain
        subdomain_layout = QHBoxLayout()
        subdomain_layout.addWidget(QLabel("Subdomain:"))
        self.subdomain_input = QLineEdit()
        self.subdomain_input.setPlaceholderText("yourschool")
        self.subdomain_input.setToolTip("Your school's EduPage subdomain (e.g., 'yourschool' for yourschool.edupage.org)")
        subdomain_layout.addWidget(self.subdomain_input)
        cred_layout.addLayout(subdomain_layout)
        
        # Username
        user_layout = QHBoxLayout()
        user_layout.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("your.email@example.com")
        user_layout.addWidget(self.username_input)
        cred_layout.addLayout(user_layout)
        
        # Password
        pass_layout = QHBoxLayout()
        pass_layout.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("your password")
        pass_layout.addWidget(self.password_input)
        cred_layout.addLayout(pass_layout)
        
        layout.addWidget(cred_group)
        
        # === Loading Mode Selection ===
        mode_group = QGroupBox("Loading Mode")
        mode_main_layout = QVBoxLayout(mode_group)

        mode_info = QLabel(
            "Choose how to load data from EduPage:"
        )
        mode_info.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        mode_main_layout.addWidget(mode_info)

        # Radio buttons for mode selection
        self.mode_button_group = QButtonGroup()

        # Horizontal layout for both modes side by side
        modes_h_layout = QHBoxLayout()

        # Mode 1: Selective Load
        mode1_container = QWidget()
        mode1_layout = QVBoxLayout(mode1_container)
        mode1_layout.setContentsMargins(5, 5, 5, 5)

        self.mode1_radio = QRadioButton("Mode 1: Selective Load (Recommended)")
        self.mode1_radio.setChecked(True)
        self.mode_button_group.addButton(self.mode1_radio, 1)
        mode1_layout.addWidget(self.mode1_radio)

        mode1_desc = QLabel(
            "• Loads class names, you select, then loads students\n"
            "• Choose between single-phase or two-phase loading below"
        )
        mode1_desc.setStyleSheet("color: #aaa; margin-left: 10px; font-size: 10px;")
        mode1_desc.setWordWrap(True)
        mode1_layout.addWidget(mode1_desc)
        
        # Phase selection for Mode 1
        phase_container = QWidget()
        phase_layout = QVBoxLayout(phase_container)
        phase_layout.setContentsMargins(15, 5, 5, 5)
        
        self.phase_button_group = QButtonGroup()
        
        self.single_phase_radio = QRadioButton("Single Phase (Default)")
        self.single_phase_radio.setChecked(True)
        self.phase_button_group.addButton(self.single_phase_radio, 1)
        phase_layout.addWidget(self.single_phase_radio)
        
        single_phase_desc = QLabel(
            "One login, one task - class selection happens during loading"
        )
        single_phase_desc.setStyleSheet("color: #888; margin-left: 20px; font-size: 9px;")
        single_phase_desc.setWordWrap(True)
        phase_layout.addWidget(single_phase_desc)
        
        self.two_phase_radio = QRadioButton("Two Phase")
        self.phase_button_group.addButton(self.two_phase_radio, 2)
        phase_layout.addWidget(self.two_phase_radio)
        
        two_phase_desc = QLabel(
            "Two logins, two tasks - complete logout between phases (may need 2FA twice)"
        )
        two_phase_desc.setStyleSheet("color: #888; margin-left: 20px; font-size: 9px;")
        two_phase_desc.setWordWrap(True)
        phase_layout.addWidget(two_phase_desc)
        
        mode1_layout.addWidget(phase_container)
        mode1_layout.addStretch()

        # Mode 2: Not Implemented
        mode2_container = QWidget()
        mode2_layout = QVBoxLayout(mode2_container)
        mode2_layout.setContentsMargins(5, 5, 5, 5)

        self.mode2_radio = QRadioButton("Mode 2: Custom Implementation")
        self.mode_button_group.addButton(self.mode2_radio, 2)
        mode2_layout.addWidget(self.mode2_radio)

        mode2_desc = QLabel(
            "• Reserved for future custom implementation\n"
            "• Not yet implemented\n"
            "• Selecting this mode will show an info message\n"
            "• Implementation can be added in edupage_tasks.py"
        )
        mode2_desc.setStyleSheet("color: #888; margin-left: 10px; font-size: 10px; font-style: italic;")
        mode2_desc.setWordWrap(True)
        mode2_layout.addWidget(mode2_desc)
        mode2_layout.addStretch()

        modes_h_layout.addWidget(mode1_container)
        modes_h_layout.addWidget(mode2_container)
        modes_h_layout.addStretch()

        mode_main_layout.addLayout(modes_h_layout)

        layout.addWidget(mode_group)
        
        # === Load Button ===
        # Styled exactly like the "Load Data" button of the Encrypted File
        # source: a plain push button followed by a stretch, so both data
        # sources look and behave the same.
        load_layout = QHBoxLayout()
        self.load_btn = QPushButton("Connect and Load Data")
        self.load_btn.clicked.connect(self.on_load_clicked)
        load_layout.addWidget(self.load_btn)
        load_layout.addStretch()
        layout.addLayout(load_layout)
        
        layout.addStretch()
       
        # Set scroll area
        scroll.setWidget(container)
    
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)
            
    def on_load_clicked(self):
        """Handle load button click."""
        # Validate inputs
        subdomain = self.subdomain_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()
        
        if not all([subdomain, username, password]):
            QMessageBox.warning(
                self,
                "Missing Information",
                "Please fill in all fields (subdomain, username, and password)"
            )
            return
        
        # Check which mode is selected
        selected_mode = self.mode_button_group.checkedId()
        
        logger.info(f"Starting EduPage load with Mode {selected_mode}")
        
        try:
            if selected_mode == 1:
                # Mode 1: Selective Load - check phase
                selected_phase = self.phase_button_group.checkedId()
                
                if selected_phase == 1:
                    # Single Phase (default)
                    logger.info("Using single-phase loading")
                    self.coordinator.execute_mode1_single_phase(username, password, subdomain)
                else:
                    # Two Phase
                    logger.info("Using two-phase loading")
                    self.coordinator.execute_mode1_two_phase(username, password, subdomain)
                    
            elif selected_mode == 2:
                # Mode 2: Not Implemented
                self.coordinator.execute_mode2_load(username, password, subdomain)
            else:
                QMessageBox.warning(
                    self,
                    "Invalid Mode",
                    "Please select a loading mode"
                )
                
        except Exception as e:
            logger.exception("Error starting EduPage load")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to start loading: {str(e)}"
            )
    
    def on_loading_complete(self, source):
        """Handle successful loading completion."""
        logger.info(f"Successfully loaded EduPage source: {source.name}")
        
        QMessageBox.information(
            self,
            "Success",
            f"Successfully loaded source '{source.name}' from EduPage.\n\n"
            f"Classes: {len(source.classes)}\n"
            f"Students: {len(source.get_all_persons())}"
        )
    
    def on_loading_failed(self, error_message: str):
        """Handle loading failure."""
        logger.error(f"EduPage loading failed: {error_message}")
        
        QMessageBox.critical(
            self,
            "Loading Failed",
            f"Failed to load data from EduPage:\n\n{error_message}"
        )


# For backward compatibility - in case other parts of the code reference the old thread
# This is a placeholder that logs a warning
class EdupageLoadThread:
    """
    DEPRECATED: Old thread-based loader.
    Use EduPageLoadCoordinator with progress tasks instead.
    """
    def __init__(self, *args, **kwargs):
        logger.warning(
            "EdupageLoadThread is deprecated. "
            "Use EduPageLoadCoordinator with progress tasks instead."
        )
        raise NotImplementedError(
            "EdupageLoadThread is deprecated. "
            "Use the new EdupageSourceWidget which handles everything automatically."
        )
