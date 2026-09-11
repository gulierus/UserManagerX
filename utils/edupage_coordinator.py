"""
EduPage Load Coordinator
Orchestrates the EduPage data loading process with 2FA support
"""

import logging
from typing import Optional, List
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QMessageBox, QInputDialog
)
from PyQt6.QtCore import QObject, pyqtSignal

from utils.progress_dialog import ProgressDialog
from utils.edupage_tasks import EduPageMode1SinglePhaseTask, EduPageMode1TwoPhaseTask
from models import Source

logger = logging.getLogger(__name__)


class TwoFADialog(QDialog):
    """Dialog for entering 2FA code with resend support."""
    
    # Signal emitted when user requests code resend
    resend_requested = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.code = None
        self.setWindowTitle("Two-Factor Authentication")
        self.setModal(True)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Info
        info_text = "Please enter the verification code:"
        
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        
        # Code input
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Enter 6-digit code")
        self.code_input.setMaxLength(6)
        layout.addWidget(self.code_input)
        
        # Resend button
        resend_layout = QHBoxLayout()
        resend_layout.addStretch()
        
        self.resend_btn = QPushButton("Resend Code")
        self.resend_btn.setStyleSheet("color: #4CAF50;")
        self.resend_btn.clicked.connect(self.on_resend)
        resend_layout.addWidget(self.resend_btn)
        
        layout.addLayout(resend_layout)
        
        # Status label for resend feedback
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #4CAF50; font-style: italic;")
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        verify_btn = QPushButton("Verify")
        verify_btn.clicked.connect(self.on_verify)
        verify_btn.setDefault(True)
        button_layout.addWidget(verify_btn)
        
        layout.addLayout(button_layout)
    
    def on_resend(self):
        """Handle resend button click."""
        self.resend_btn.setEnabled(False)
        self.status_label.setText("Resending code...")
        self.status_label.setVisible(True)
        
        # Emit signal to coordinator
        self.resend_requested.emit()
    
    def on_resend_complete(self, success: bool):
        """Called by coordinator after resend attempt."""
        self.resend_btn.setEnabled(True)
        if success:
            self.status_label.setText("✓ Code has been resent")
            self.status_label.setStyleSheet("color: #4CAF50; font-style: italic;")
        else:
            self.status_label.setText("✗ Failed to resend code")
            self.status_label.setStyleSheet("color: #f44336; font-style: italic;")
        self.status_label.setVisible(True)
    
    def on_verify(self):
        code = self.code_input.text().strip()
        if not code:
            QMessageBox.warning(self, "Invalid Code", "Please enter the verification code")
            return
        
        self.code = code
        self.accept()


class ClassSelectionDialog(QDialog):
    """Dialog for selecting classes to load."""
    
    def __init__(self, class_names: List[str], parent=None):
        super().__init__(parent)
        self.class_names = class_names
        self.selected_classes: List[str] = []
        self.setWindowTitle("Select Classes")
        self.setModal(True)
        self.setMinimumSize(400, 500)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Info
        info = QLabel(f"Select the classes you want to load ({len(self.class_names)} available):")
        layout.addWidget(info)
        
        # Class list
        self.class_list = QListWidget()
        self.class_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.class_list.addItems(sorted(self.class_names))
        layout.addWidget(self.class_list)
        
        # Select all/none buttons
        select_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self.select_all)
        select_layout.addWidget(select_all_btn)
        
        select_none_btn = QPushButton("Clear Selection")
        select_none_btn.clicked.connect(self.select_none)
        select_layout.addWidget(select_none_btn)
        
        layout.addLayout(select_layout)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        ok_btn = QPushButton("Load Selected Classes")
        ok_btn.clicked.connect(self.on_ok)
        ok_btn.setDefault(True)
        button_layout.addWidget(ok_btn)
        
        layout.addLayout(button_layout)
    
    def select_all(self):
        self.class_list.selectAll()
    
    def select_none(self):
        self.class_list.clearSelection()
    
    def on_ok(self):
        selected_items = self.class_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one class")
            return
        
        self.selected_classes = [item.text() for item in selected_items]
        self.accept()


class EduPageLoadCoordinator(QObject):
    """
    Coordinates the EduPage data loading process.
    Manages the workflow for both loading modes with 2FA support.
    """
    
    # Signal emitted when loading is complete
    loading_complete = pyqtSignal(Source)  # Emits the loaded Source
    loading_failed = pyqtSignal(str)  # Emits error message
    
    def __init__(self, parent_widget, source_manager):
        super().__init__()
        self.parent_widget = parent_widget
        self.source_manager = source_manager
        
        # Current task and dialog
        self.current_task = None
        self.current_dialog: Optional[ProgressDialog] = None
        self.current_2fa_dialog: Optional[TwoFADialog] = None
        
        # Credentials for Mode 1 Phase 2 (two-phase mode)
        self.cached_username = None
        self.cached_password = None
        self.cached_subdomain = None
    
    def execute_mode1_single_phase(self, username: str, password: str, subdomain: str):
        """
        Execute Mode 1: Single-phase load (default).
        
        All in one task:
        Login → Fetch classes → Select classes → Load students → Create Source
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
        """
        logger.info(f"Starting EduPage Mode 1 (Single Phase) for subdomain: {subdomain}")
        
        # Import the task class
        from utils.edupage_tasks import EduPageMode1SinglePhaseTask
        
        # Create task
        self.current_task = EduPageMode1SinglePhaseTask(username, password, subdomain)
        
        # Connect signals
        self.current_task.twofa_code_needed.connect(self._handle_2fa_code_request)
        self.current_task.class_selection_needed.connect(self._handle_class_selection)
        
        # Create and show progress dialog
        self.current_dialog = ProgressDialog(self.current_task, self.parent_widget)
        self.current_dialog.task.task_finished.connect(
            lambda success, end_time, msg: self._on_single_phase_finished(success, msg, subdomain)
        )
        
        # Start task
        self.current_dialog.start_task()
        self.current_dialog.exec()
    
    def _handle_class_selection(self, class_names: List[str]):
        """Handle class selection request from single-phase task."""
        logger.info(f"Class selection requested: {len(class_names)} classes available")
        
        # Show class selection dialog
        selection_dialog = ClassSelectionDialog(class_names, self.parent_widget)
        if selection_dialog.exec() == QDialog.DialogCode.Accepted:
            selected = selection_dialog.selected_classes
            if selected:
                # Provide selection to task
                self.current_task.set_selected_classes(selected)
                logger.info(f"User selected {len(selected)} classes")
            else:
                # No selection - cancel task
                self.current_task.request_cancel()
        else:
            # User cancelled selection - cancel task
            self.current_task.request_cancel()
            logger.info("User cancelled class selection")
    
    def _was_cancelled(self) -> bool:
        """
        Report whether the current run was stopped by the user.

        A cancellation is a deliberate user action, so it must not be presented
        as a failure. The progress dialog already shows the "Cancelled" state;
        no extra error window is needed on top of it.
        """
        dialog = self.current_dialog
        if dialog is not None:
            try:
                if dialog.was_cancelled():
                    return True
            except (RuntimeError, AttributeError):
                pass

        task = self.current_task
        if task is not None:
            try:
                return bool(task.is_cancelled())
            except (RuntimeError, AttributeError):
                return False
        return False

    def _report_failure(self, message: str) -> bool:
        """
        Emit :attr:`loading_failed` unless the run was cancelled by the user.

        Returns:
            True when the failure was reported, False when it was suppressed
            because the user cancelled.
        """
        if self._was_cancelled():
            logger.info("EduPage loading cancelled by user - no error is shown")
            return False
        self.loading_failed.emit(message)
        return True

    def _on_single_phase_finished(self, success: bool, message: str, subdomain: str):
        """Handle completion of single-phase load."""
        if not success:
            self._report_failure(message)
            return
        
        # Ask for source name
        source_name = self._ask_for_source_name(f"EduPage-{subdomain}")
        if not source_name:
            return
        
        # Create source
        try:
            source = self.current_task.get_result_source(source_name)
        except Exception as exc:
            logger.exception("Could not build the source from the loaded data")
            self.loading_failed.emit(f"Could not build the source: {exc}")
            return

        # Add to source manager
        self.source_manager.add_source(source)

        # Emit success
        self.loading_complete.emit(source)

    def execute_mode1_two_phase(self, username: str, password: str, subdomain: str):
        
        """
        Execute Mode 1: Two-phase load with class selection between logins.
        
        Phase 1: Login → Fetch class names → Select classes
        Phase 2: Login → Load students from selected classes → Create Source
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
        """

        logger.info(f"Starting EduPage Mode 1 (Two-Phase) for subdomain: {subdomain}")
        
        # Cache credentials for Phase 2
        self.cached_username = username
        self.cached_password = password
        self.cached_subdomain = subdomain
        
        # Show info about two-phase loading
        QMessageBox.information(
            self.parent_widget,
            "Two-Phase Loading",
            "Mode 1 (Two-Phase) loads data in two separate phases:\n\n"
            "Phase 1: Fetch class names (requires login)\n"
            "Phase 2: Load students from selected classes (requires second login)\n\n"
            "Note: You may be asked for 2FA code twice if 2FA is enabled."
        )
        
        # Start Phase 1
        self._execute_mode1_phase1()
    
    def _execute_mode1_phase1(self):
        """Execute Mode 1 Phase 1: Fetch class names."""
        from utils.edupage_tasks import EduPageMode1TwoPhaseTask
        
        # Create task for Phase 1
        self.current_task = EduPageMode1TwoPhaseTask(
            self.cached_username,
            self.cached_password,
            self.cached_subdomain,
            selected_class_names=None  # Phase 1
        )
        
        # Connect 2FA signal
        self.current_task.twofa_code_needed.connect(self._handle_2fa_code_request)
        
        # Create and show progress dialog
        self.current_dialog = ProgressDialog(self.current_task, self.parent_widget)
        self.current_dialog.task.task_finished.connect(
            lambda success, end_time, msg: self._on_mode1_phase1_finished(success, msg)
        )
        
        # Start task
        self.current_dialog.start_task()
        self.current_dialog.exec()
    
    def _on_mode1_phase1_finished(self, success: bool, message: str):
        """Handle completion of Mode 1 Phase 1."""
        if not success:
            self._report_failure(message)
            return

        # Get class names from task
        class_names = self.current_task.get_class_names()

        if not class_names:
            QMessageBox.warning(
                self.parent_widget,
                "No Classes",
                "No classes were found in EduPage"
            )
            self.loading_failed.emit("No classes found")
            return
        
        # Show class selection dialog
        selection_dialog = ClassSelectionDialog(class_names, self.parent_widget)
        if selection_dialog.exec() != QDialog.DialogCode.Accepted:
            logger.info("User cancelled class selection")
            return
        
        selected = selection_dialog.selected_classes
        if not selected:
            return
        
        logger.info(f"User selected {len(selected)} classes")
        
        # Proceed to Phase 2 with selected classes
        self._execute_mode1_phase2(selected)
    
    def _execute_mode1_phase2(self, selected_classes: List[str]):
        """Execute Mode 1 Phase 2: Load students from selected classes."""
        logger.info(f"Starting Mode 1 Phase 2 with {len(selected_classes)} classes")
        
        from utils.edupage_tasks import EduPageMode1TwoPhaseTask
        
        # Create task for Phase 2
        self.current_task = EduPageMode1TwoPhaseTask(
            self.cached_username,
            self.cached_password,
            self.cached_subdomain,
            selected_class_names=selected_classes
        )
        
        # Connect 2FA signal
        self.current_task.twofa_code_needed.connect(self._handle_2fa_code_request)
        
        # Create and show progress dialog
        self.current_dialog = ProgressDialog(self.current_task, self.parent_widget)
        self.current_dialog.task.task_finished.connect(
            lambda success, end_time, msg: self._on_mode1_phase2_finished(success, msg)
        )
        
        # Start task
        self.current_dialog.start_task()
        self.current_dialog.exec()
    
    def _on_mode1_phase2_finished(self, success: bool, message: str):
        """Handle completion of Mode 1 Phase 2."""
        if not success:
            self._report_failure(message)
            return
        
        # Ask for source name
        source_name = self._ask_for_source_name(f"EduPage-{self.cached_subdomain}")
        if not source_name:
            return
        
        # Create source
        try:
            source = self.current_task.get_result_source(source_name)
        except Exception as exc:
            logger.exception("Could not build the source from the loaded data")
            self.loading_failed.emit(f"Could not build the source: {exc}")
            return

        # Add to source manager
        self.source_manager.add_source(source)

        # Emit success
        self.loading_complete.emit(source)

    def execute_mode2_load(self, username: str, password: str, subdomain: str):
        """
        Execute Mode 2: Not yet implemented.
        Shows information dialog to user.
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
        """
        logger.info("Mode 2 selected - showing not implemented dialog")
        
        QMessageBox.information(
            self.parent_widget,
            "Mode 2 - Not Implemented",
            "Mode 2 is not yet implemented.\n\n"
            "This mode is reserved for future custom implementation.\n"
            "Please use Mode 1 for now, or implement Mode 2 logic in edupage_tasks.py."
        )
    
    def _handle_2fa_code_request(self):
        """Handle request for 2FA code from task with resend support."""
        logger.info("2FA code requested")
        
        # Show 2FA dialog
        dialog = TwoFADialog(self.parent_widget)
        self.current_2fa_dialog = dialog
        
        # Connect resend signal
        dialog.resend_requested.connect(self._handle_2fa_resend)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Provide code to task
            self.current_task.set_2fa_code(dialog.code)
            logger.info("2FA code provided to task")
        else:
            # User cancelled - cancel task
            self.current_task.request_cancel()
            logger.info("User cancelled 2FA dialog")
        
        self.current_2fa_dialog = None
    
    def _handle_2fa_resend(self):
        """Handle 2FA code resend request."""
        logger.info("2FA code resend requested")
        
        try:
            # Get the TwoFactorLogin object from current task
            if self.current_task and hasattr(self.current_task, 'twofa_login') and self.current_task.twofa_login:
                self.current_task.twofa_login.resend_notifications()
                logger.info("2FA code resent successfully")
                
                # Notify dialog of success
                if self.current_2fa_dialog:
                    self.current_2fa_dialog.on_resend_complete(True)
            else:
                logger.warning("Cannot resend: TwoFactorLogin object not available")
                if self.current_2fa_dialog:
                    self.current_2fa_dialog.on_resend_complete(False)
                    
        except Exception as e:
            logger.exception("Failed to resend 2FA code")
            if self.current_2fa_dialog:
                self.current_2fa_dialog.on_resend_complete(False)
    
    def _ask_for_source_name(self, default_name: str) -> Optional[str]:
        """Ask user for source name."""
        while True:
            name, ok = QInputDialog.getText(
                self.parent_widget,
                "Name Source",
                "Enter name for this source:",
                QLineEdit.EchoMode.Normal,
                default_name
            )
            
            if not ok or not name:
                return None
            
            name = name.strip()
            if not name:
                QMessageBox.warning(
                    self.parent_widget,
                    "Invalid Name",
                    "Source name cannot be empty"
                )
                continue
            
            # Check for duplicates
            if self.source_manager.source_name_exists(name):
                reply = QMessageBox.question(
                    self.parent_widget,
                    "Duplicate Name",
                    f"Source '{name}' already exists. Use it anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply == QMessageBox.StandardButton.No:
                    continue
            
            return name
