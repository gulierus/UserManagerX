"""
Universal Progress Dialog
Displays progress and status for long-running tasks

Usage:
    # Create task
    task = MyCustomTask(params)
    
    # Create dialog
    dialog = ProgressDialog(task, parent=self)
    
    # IMPORTANT: Must call start_task() before exec()
    dialog.start_task()
    
    # Show dialog (blocks until task completes)
    dialog.exec()
"""

import logging
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QGroupBox
)
from PyQt6.QtCore import Qt, QDateTime
from PyQt6.QtGui import QFont, QTextCursor

from utils.progress_tasks import AbstractProgressTask, LogLevel

logger = logging.getLogger(__name__)


class ProgressDialog(QDialog):
    """
    Universal progress dialog for displaying task progress.
    
    Features:
    - Top panel: Task info and status
    - Progress bar: Deterministic or indeterminate
    - Control buttons: Cancel, Pause/Resume
    - Log panel: Scrollable log with timestamps
    - Bottom panel: Duration and completion stats
    
    IMPORTANT: You must call start_task() explicitly before exec() to begin execution.
    The dialog does not auto-start the task to allow for proper initialization.
    
    Example:
        task = MyTask()
        dialog = ProgressDialog(task, parent=self)
        dialog.start_task()  # Required!
        dialog.exec()
    """
    
    def __init__(self, task: AbstractProgressTask, parent=None, modal: bool = True):
        """
        Initialize progress dialog.
        
        Args:
            task: Task instance to execute
            parent: Parent widget
            modal: Whether dialog is modal
            
        Note:
            The task is NOT started automatically. You must call start_task()
            explicitly before calling exec().
        """
        super().__init__(parent)
        
        self.task = task
        self.setModal(modal)
        self.setWindowTitle(f"Progress: {task.task_name}")
        self.setMinimumSize(700, 500)
        
        # State tracking
        self._is_running = False
        self._is_paused = False
        self._is_finished = False
        self._success = False
        self._was_cancelled = False
        
        self.init_ui()
        self.connect_signals()
        
        logger.info(f"Created progress dialog for task: {task.task_name}")
    
    def init_ui(self):
        """Initialize user interface."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # === TOP PANEL: Task Info ===
        self._create_info_panel(layout)
        
        # === PROGRESS BAR ===
        self._create_progress_bar(layout)
        
        # === CONTROL BUTTONS ===
        self._create_control_buttons(layout)
        
        # === LOG PANEL ===
        self._create_log_panel(layout)
        
        # === BOTTOM PANEL: Statistics (hidden initially) ===
        self._create_stats_panel(layout)
        
        # Initial state
        self.stats_panel.setVisible(False)
    
    def _create_info_panel(self, parent_layout):
        """Create top information panel."""
        info_group = QGroupBox("Task Information")
        info_layout = QVBoxLayout(info_group)
        
        # Task name
        self.task_name_label = QLabel(f"<b>{self.task.task_name}</b>")
        font = QFont()
        font.setPointSize(11)
        self.task_name_label.setFont(font)
        info_layout.addWidget(self.task_name_label)
        
        # Status - Changed from "Initializing..." to "Ready"
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("Status:"))
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #2196F3; font-weight: bold;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        info_layout.addLayout(status_layout)
        
        # Current operation - Changed to reflect waiting state
        self.operation_label = QLabel("Waiting to start...")
        self.operation_label.setStyleSheet("color: #aaa; font-style: italic;")
        info_layout.addWidget(self.operation_label)
        
        parent_layout.addWidget(info_group)
    
    def _create_progress_bar(self, parent_layout):
        """Create progress bar."""
        progress_group = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_group)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        
        # Set indeterminate mode if task is not deterministic
        if not self.task.is_deterministic:
            self.progress_bar.setMinimum(0)
            self.progress_bar.setMaximum(0)  # Indeterminate mode
            self.progress_bar.setFormat("Processing...")  # Set default format for indeterminate
        
        progress_layout.addWidget(self.progress_bar)
        parent_layout.addWidget(progress_group)
    
    def _create_control_buttons(self, parent_layout):
        """Create control buttons."""
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        # Cancel button (always available)
        self.cancel_button = QPushButton("\U0001F6D1 Cancel")
        self.cancel_button.setMinimumHeight(35)
        self.cancel_button.clicked.connect(self.on_cancel_clicked)
        button_layout.addWidget(self.cancel_button)
        
        # Pause/Resume button (only if task supports it)
        if self.task.can_pause:
            self.pause_resume_button = QPushButton("\u23F8\uFE0F Pause")
            self.pause_resume_button.setMinimumHeight(35)
            self.pause_resume_button.clicked.connect(self.on_pause_resume_clicked)
            button_layout.addWidget(self.pause_resume_button)
        else:
            self.pause_resume_button = None
        
        button_layout.addStretch()
        
        # Close button (disabled until finished)
        self.close_button = QPushButton("\u2713 Close")
        self.close_button.setMinimumHeight(35)
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)
        
        parent_layout.addLayout(button_layout)
    
    def _create_log_panel(self, parent_layout):
        """Create scrollable log panel."""
        log_group = QGroupBox("Operation Log")
        log_layout = QVBoxLayout(log_group)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #2a2a2a;
                color: #e0e0e0;
                border: 1px solid #555;
            }
        """)
        
        log_layout.addWidget(self.log_text)
        parent_layout.addWidget(log_group)
    
    def _create_stats_panel(self, parent_layout):
        """Create bottom statistics panel."""
        self.stats_panel = QGroupBox("Completion Statistics")
        stats_layout = QVBoxLayout(self.stats_panel)
        
        # Create labels for stats
        self.start_time_label = QLabel()
        self.end_time_label = QLabel()
        self.duration_label = QLabel()
        self.result_label = QLabel()
        
        stats_layout.addWidget(self.start_time_label)
        stats_layout.addWidget(self.end_time_label)
        stats_layout.addWidget(self.duration_label)
        stats_layout.addWidget(self.result_label)
        
        parent_layout.addWidget(self.stats_panel)
    
    def connect_signals(self):
        """Connect task signals to dialog slots."""
        self.task.task_started.connect(self.on_task_started)
        self.task.progress_changed.connect(self.on_progress_changed)
        self.task.log_message.connect(self.on_log_message)
        self.task.task_finished.connect(self.on_task_finished)
        self.task.task_cancelled.connect(self.on_task_cancelled)
        self.task.task_paused.connect(self.on_task_paused)
        self.task.task_resumed.connect(self.on_task_resumed)
        self.task.task_error.connect(self.on_task_error)
    
    def start_task(self):
        """
        Start executing the task.
        
        MUST be called before exec() to begin task execution.
        The task does not start automatically to allow proper initialization.
        """
        if self._is_running:
            logger.warning("Task is already running")
            return
        
        # Check if thread is already running
        if self.task.isRunning():
            logger.warning("Task thread is already running")
            return
        
        self._is_running = True
        self.status_label.setText("Running")
        self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        
        logger.info(f"Starting task: {self.task.task_name}")
        self.task.start()
    
    def on_task_started(self, start_time: QDateTime):
        """Handle task start."""
        self.status_label.setText("Running")
        self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        logger.debug(f"Task started at: {start_time.toString()}")
    
    def on_progress_changed(self, value: int, message: str):
        """
        Handle progress update.

        Updates that arrive after the task was cancelled are ignored so the
        progress bar cannot fall back to a stale "in progress" text.
        """
        if self._was_cancelled:
            return

        # Ensure message is not None
        if message is None:
            message = ""
        
        if self.task.is_deterministic and value >= 0:
            self.progress_bar.setValue(value)
            if message:
                self.progress_bar.setFormat(f"{value}% - {message}")
            else:
                self.progress_bar.setFormat(f"{value}%")
        else:
            # Indeterminate mode - just show message
            if message:
                self.progress_bar.setFormat(message)
        
        if message:
            self.operation_label.setText(message)
    
    def on_log_message(self, message: str, level: LogLevel, timestamp: QDateTime):
        """Handle log message."""
        # Format message with color based on level
        colors = {
            LogLevel.DEBUG: "#888",
            LogLevel.INFO: "#e0e0e0",
            LogLevel.WARNING: "#FFA726",
            LogLevel.ERROR: "#EF5350",
            LogLevel.SUCCESS: "#4CAF50"
        }
        color = colors.get(level, "#e0e0e0")
        
        # Format timestamp
        time_str = timestamp.toString("hh:mm:ss")
        
        # Add to log
        formatted = f'<span style="color: #888;">[{time_str}]</span> '
        formatted += f'<span style="color: {color};">{message}</span>'
        
        self.log_text.append(formatted)
        
        # Auto-scroll to bottom
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)
    
    def on_task_finished(self, success: bool, end_time: QDateTime, result_message: str):
        """
        Handle task completion.

        A cancelled task is *not* reported as "Failed": the task emits
        ``task_cancelled`` before ``task_finished(False, ...)``, and a cancelled
        run is a deliberate user action, not an error.
        """
        self._is_finished = True
        self._is_running = False  # Reset running state
        self._success = success

        cancelled = self._was_cancelled or self.was_cancelled()

        # Update status
        if success:
            self.status_label.setText("Completed Successfully")
            self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
            # Set progress to 100% for deterministic tasks
            if self.task.is_deterministic:
                self.progress_bar.setValue(100)
                self.progress_bar.setFormat("100% - Complete")
        elif cancelled:
            self._show_cancelled_state()
        else:
            self.status_label.setText("Failed")
            self.status_label.setStyleSheet("color: #EF5350; font-weight: bold;")
            self._set_progress_text("Failed")
        
        # Show statistics
        self._show_statistics(success, result_message)
        
        # Update buttons
        self.cancel_button.setEnabled(False)
        if self.pause_resume_button:
            self.pause_resume_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.close_button.setFocus()  # Auto-focus close button
        
        logger.info(f"Task finished: {result_message}")
    
    def on_task_cancelled(self):
        """Handle task cancellation."""
        self._show_cancelled_state()

    def _show_cancelled_state(self):
        """
        Put every status widget into the "cancelled" state.

        Without this the progress bar kept the text of the last progress update
        (for example "25% - Please select classes to load...") even though the
        task had already been stopped, which is exactly the opposite of what the
        user just did.
        """
        self._was_cancelled = True
        self.status_label.setText("Cancelled")
        self.status_label.setStyleSheet("color: #FF9800; font-weight: bold;")
        self.operation_label.setText("Task cancelled by user")
        self._set_progress_text("Cancelled")

    def _set_progress_text(self, state: str):
        """
        Overwrite the progress-bar text with the given state.

        Deterministic bars keep their numeric value so the user can still see
        how far the task got before it stopped.
        """
        try:
            if self.task.is_deterministic:
                self.progress_bar.setFormat(f"{self.progress_bar.value()}% - {state}")
            else:
                # Leave indeterminate mode so the bar stops animating
                self.progress_bar.setMaximum(100)
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat(state)
        except RuntimeError:  # pragma: no cover - widget already destroyed
            logger.debug("Progress bar no longer available")

    def was_cancelled(self) -> bool:
        """
        Return True when the task was cancelled by the user.

        Callers use this to distinguish "the user stopped it" from "it failed",
        so they can suppress error dialogs after a cancellation.
        """
        if self._was_cancelled:
            return True
        try:
            return bool(self.task.is_cancelled())
        except RuntimeError:  # pragma: no cover - task already destroyed
            return self._was_cancelled
    
    def on_task_paused(self):
        """Handle task pause."""
        self._is_paused = True
        self.status_label.setText("Paused")
        self.status_label.setStyleSheet("color: #FF9800; font-weight: bold;")
        
        if self.pause_resume_button:
            self.pause_resume_button.setText("\u25B6\uFE0F Resume")
    
    def on_task_resumed(self):
        """Handle task resume."""
        self._is_paused = False
        self.status_label.setText("Running")
        self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
        
        if self.pause_resume_button:
            self.pause_resume_button.setText("\u23F8\uFE0F Pause")
    
    def on_task_error(self, error_message: str):
        """Handle task error."""
        logger.error(f"Task error: {error_message}")
    
    def on_cancel_clicked(self):
        """Handle cancel button click."""
        # Validate task is running
        if not self._is_running:
            logger.warning("Cancel clicked but task is not running")
            return
        
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")
        self.operation_label.setText("Cancelling task...")  # User feedback
        self.task.request_cancel()
        logger.info("User requested task cancellation")
    
    def on_pause_resume_clicked(self):
        """Handle pause/resume button click."""
        # Validate task is running
        if not self._is_running:
            logger.warning("Pause/Resume clicked but task is not running")
            return
        
        if self._is_paused:
            self.operation_label.setText("Resuming task...")  # User feedback
            self.task.request_resume()
        else:
            self.operation_label.setText("Pausing task...")  # User feedback
            self.task.request_pause()
    
    def _show_statistics(self, success: bool, result_message: str):
        """Show completion statistics in bottom panel."""
        # Validate times are available
        if not self.task.start_time or not self.task.end_time:
            logger.warning("Cannot show statistics - start_time or end_time is None")
            return
        
        # Calculate duration
        start = self.task.start_time
        end = self.task.end_time
        duration_ms = start.msecsTo(end)
        
        # Check for negative duration (system time change)
        if duration_ms < 0:
            logger.warning(f"Negative duration detected: {duration_ms}ms - possible system time change")
            duration_str = "Invalid duration (system time changed)"
        else:
            duration_sec = duration_ms / 1000.0
            
            # Format duration
            if duration_sec < 60:
                duration_str = f"{duration_sec:.1f} seconds"
            elif duration_sec < 3600:
                minutes = int(duration_sec / 60)
                seconds = duration_sec % 60
                duration_str = f"{minutes} min {seconds:.0f} sec"
            else:
                hours = int(duration_sec / 3600)
                minutes = int((duration_sec % 3600) / 60)
                duration_str = f"{hours} hr {minutes} min"
        
        # Update labels
        self.start_time_label.setText(f"<b>Started:</b> {start.toString('yyyy-MM-dd hh:mm:ss')}")
        self.end_time_label.setText(f"<b>Completed:</b> {end.toString('yyyy-MM-dd hh:mm:ss')}")
        self.duration_label.setText(f"<b>Duration:</b> {duration_str}")
        
        # Result with color
        result_color = "#4CAF50" if success else "#EF5350"
        self.result_label.setText(f'<b>Result:</b> <span style="color: {result_color};">{result_message}</span>')
        
        # Show panel
        self.stats_panel.setVisible(True)
    
    def closeEvent(self, event):
        """Handle dialog close event."""
        if self._is_running and not self._is_finished:
            # Task still running - cancel it
            logger.info("Dialog closing with running task - requesting cancellation")
            self.task.request_cancel()
            self.task.wait(5000)  # Wait up to 5 seconds
            
            if self.task.isRunning():
                logger.warning(
                    "Task did not stop in time, forcing termination. "
                    "This may cause resource leaks or data corruption. "
                    "Task implementation should respond to cancellation requests promptly."
                )
                self.task.terminate()
        
        event.accept()
