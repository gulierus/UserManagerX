"""
Log Viewer Tab - Advanced log viewing with real-time and historical logs
"""

import logging
import os
import re
import subprocess
import platform
from datetime import datetime, timedelta
from pathlib import Path
from html import escape  # FIX 10: Import for HTML escape
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QComboBox, QGroupBox, QTabWidget, QListWidget,
    QListWidgetItem, QLineEdit, QCheckBox, QDateEdit, QMessageBox,
    QFileDialog, QSplitter, QDialog
)
from PyQt6.QtCore import (  # FIX 11: Added QThread
    Qt, QDate, QObject, QTimer, QThread, pyqtSignal,
)
from PyQt6.QtGui import QFont, QTextCursor

from utils.logging_config import get_logging_config, MB_TO_BYTES, LOG_COLORS

logger = logging.getLogger(__name__)

# FIX 17: Constants instead of magic values
LOG_REFRESH_INTERVAL_MS = 5000
LOG_FONT_FAMILY = "Consolas"
LOG_FONT_SIZE = 9
MAX_LOG_FILE_SIZE_MB = 10
MAX_REAL_TIME_LOG_LINES = 10000
SUBPROCESS_TIMEOUT = 5

#: Maximum number of lines rendered into the historical view at once.
#: Rendering is linear in the number of lines, but a QTextEdit holding
#: hundreds of thousands of styled lines still costs a lot of memory and makes
#: scrolling sluggish, so older lines are trimmed and the user is told about it.
MAX_DISPLAYED_LOG_LINES = 5000

#: How long the UI waits for a previous load to stop before giving up on it.
LOADER_STOP_TIMEOUT_MS = 2000


class LogLoaderThread(QThread):
    """
    Thread for asynchronous log file loading.

    The completion signal is deliberately NOT called ``finished``: QThread
    already defines a ``finished()`` signal, and shadowing it makes the two
    signals collide.
    """

    load_finished = pyqtSignal(str, dict)  # content, log_file
    error = pyqtSignal(str)

    def __init__(self, file_path, log_file_data):
        super().__init__()
        self.file_path = file_path
        self.log_file_data = log_file_data

    def run(self):
        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            if self.isInterruptionRequested():
                return
            self.load_finished.emit(content, self.log_file_data)
        except FileNotFoundError:
            self.error.emit("File was deleted or moved")
        except PermissionError:
            self.error.emit("Permission denied to read file")
        except MemoryError:
            self.error.emit("File too large to load into memory")
        except OSError as e:
            self.error.emit(f"Could not read the file: {e.strerror or e}")
        except Exception as e:
            self.error.emit(f"Unexpected error: {str(e)}")


class RealTimeFilterThread(QThread):
    """Thread for asynchronous real-time log filtering"""
    filter_finished = pyqtSignal(list, int)  # filtered_logs, total_count
    progress = pyqtSignal(int, int)  # current, total
    
    def __init__(self, logs, level_filter, search_text):
        super().__init__()
        self.logs = logs
        self.level_filter = level_filter
        self.search_text = search_text.lower()
    
    def run(self):
        try:
            filtered = []
            total = len(self.logs)
            
            for i, log_entry in enumerate(self.logs):
                # Check for interruption request
                if self.isInterruptionRequested():
                    return
                
                # Report progress every 100 items
                if i % 100 == 0:
                    self.progress.emit(i, total)
                
                # Apply filters
                if self._matches_filter(log_entry):
                    filtered.append(log_entry)
            
            # Only emit if not interrupted
            if not self.isInterruptionRequested():
                self.filter_finished.emit(filtered, total)

        except Exception as e:
            logging.error(f"Error in real-time filter thread: {e}")
    
    def _matches_filter(self, log_entry):
        """Check if log entry matches filter criteria"""
        msg = log_entry['msg']
        level = log_entry['level']
        
        # Level filter
        if self.level_filter != 'ALL':
            level_name = logging.getLevelName(level)
            if level_name != self.level_filter:
                return False
        
        # Text search
        if self.search_text and self.search_text not in msg.lower():
            return False
        
        return True


class HistoricalFilterThread(QThread):
    """Thread for asynchronous historical log filtering"""
    filter_finished = pyqtSignal(list)  # filtered_lines
    progress = pyqtSignal(int, int)  # current, total
    
    def __init__(self, lines, level_filter, date_filter, date_filter_enabled, search_text):
        super().__init__()
        self.lines = lines
        self.level_filter = level_filter
        self.date_filter = date_filter
        self.date_filter_enabled = date_filter_enabled
        self.search_text = search_text.lower()
    
    def run(self):
        try:
            filtered = []
            total = len(self.lines)
            
            for i, line in enumerate(self.lines):
                # Check for interruption request
                if self.isInterruptionRequested():
                    return
                
                # Report progress every 500 items
                if i % 500 == 0:
                    self.progress.emit(i, total)
                
                # Apply filters
                if self._line_matches_filters(line):
                    filtered.append(line)
            
            # Only emit if not interrupted
            if not self.isInterruptionRequested():
                self.filter_finished.emit(filtered)

        except Exception as e:
            logging.error(f"Error in historical filter thread: {e}")
    
    def _line_matches_filters(self, line):
        """Check if line matches all filter criteria"""
        if not line.strip():
            return False
        
        # Level filter
        if self.level_filter != 'ALL' and self.level_filter not in line:
            return False
        
        # Date filter
        if self.date_filter_enabled and self.date_filter:
            import re
            from datetime import datetime
            date_match = re.search(r'^(\d{4}-\d{2}-\d{2})', line)
            if date_match:
                try:
                    line_date = datetime.strptime(date_match.group(1), '%Y-%m-%d').date()
                    if line_date != self.date_filter:
                        return False
                except ValueError:
                    return False
            else:
                return False
        
        # Text search
        if self.search_text and self.search_text not in line.lower():
            return False
        
        return True


class RealTimeLogHandler(QObject, logging.Handler):
    """
    Logging handler that feeds the real-time view.

    **This handler is called from whatever thread logged the message**, and
    the worker threads (``AbstractProgressTask.run`` and everything it calls)
    log a great deal. The previous version invoked the callback directly, so a
    background thread ended up manipulating the QTextDocument of the real-time
    view while the GUI thread was doing the same:

        File "ui/log_viewer_tab.py", line 736 in append_colored_line
        ...
        File "utils/progress_tasks.py", line 141 in run     <- worker thread
        Windows fatal exception: access violation

    Qt widgets and QTextDocument may only be touched from the GUI thread.
    Doing it from another one corrupts memory, which is why the crash was
    intermittent - it depended on what the GUI thread happened to be doing at
    that moment, and it could take the process down before the traceback was
    even flushed.

    The record is now handed over with a Qt signal. The handler object lives in
    the GUI thread, so Qt's automatic connection becomes a *queued* one for
    anything emitted from a worker: the text is appended later, by the GUI
    thread, on its own event loop.
    """

    #: Carries ``(formatted_message, levelno, record)`` to the GUI thread.
    #: The record travels along so a failing callback can still be reported
    #: through ``logging.Handler.handleError`` the way the logging module
    #: expects.
    record_logged = pyqtSignal(str, int, object)

    def __init__(self, callback):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self._callback = callback
        # Automatic connection: direct within the GUI thread, queued from a
        # worker thread. Either way the slot runs in the GUI thread, because
        # this object was created there.
        self.record_logged.connect(self._deliver)

    def _deliver(self, msg: str, level: int, record) -> None:
        """
        Hand one record to the view, in the GUI thread.

        The callback is guarded here rather than around ``emit()``: with a
        queued connection the callback runs inside Qt's event loop, and PyQt6
        turns an unhandled exception in a slot into ``qFatal()`` - it would
        abort the whole process instead of being reported as a logging error.
        """
        try:
            self._callback(msg, level)
        except Exception:
            self.handleError(record)

    def emit(self, record):
        """Format the record and hand it to the GUI thread."""
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.record_logged.emit(msg, record.levelno, record)


class LogConfigDialog(QDialog):
    """Dialog for configuring logging settings"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        # FIX 2: Validation of return value
        self.config = get_logging_config()
        if not self.config:
            raise RuntimeError("Failed to initialize logging configuration")
        
        self.setWindowTitle("Logging Configuration")
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Log Level
        level_group = QGroupBox("Log Level")
        level_layout = QHBoxLayout(level_group)
        level_layout.addWidget(QLabel("Level:"))
        
        self.level_combo = QComboBox()
        self.level_combo.addItems(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        self.level_combo.setCurrentText(self.config.config['log_level'])
        level_layout.addWidget(self.level_combo)
        layout.addWidget(level_group)
        
        # File Rotation
        rotation_group = QGroupBox("File Rotation")
        rotation_layout = QVBoxLayout(rotation_group)
        
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Max File Size (MB):"))
        self.size_input = QLineEdit(str(self.config.config['max_bytes'] // MB_TO_BYTES))
        size_layout.addWidget(self.size_input)
        rotation_layout.addLayout(size_layout)
        
        backup_layout = QHBoxLayout()
        backup_layout.addWidget(QLabel("Backup Count:"))
        self.backup_input = QLineEdit(str(self.config.config['backup_count']))
        backup_layout.addWidget(self.backup_input)
        rotation_layout.addLayout(backup_layout)
        
        layout.addWidget(rotation_group)
        
        # Output Options
        output_group = QGroupBox("Output Options")
        output_layout = QVBoxLayout(output_group)
        
        self.console_check = QCheckBox("Enable Console Output")
        self.console_check.setChecked(self.config.config['console_enabled'])
        output_layout.addWidget(self.console_check)
        
        self.file_check = QCheckBox("Enable File Output")
        self.file_check.setChecked(self.config.config['file_enabled'])
        output_layout.addWidget(self.file_check)
        
        layout.addWidget(output_group)
        
        # Apply button
        apply_btn = QPushButton("Apply Configuration")
        apply_btn.clicked.connect(self.apply_config)
        layout.addWidget(apply_btn)
        
        layout.addStretch()
    
    def apply_config(self):
        """Apply configuration changes"""
        # FIX 1: Better input validation
        try:
            # Validace a konverze max_bytes
            size_text = self.size_input.text().strip()
            if not size_text:
                raise ValueError("Max file size cannot be empty")
            
            max_bytes = int(size_text) * MB_TO_BYTES
            if max_bytes <= 0:
                raise ValueError("Max file size must be positive")
            
            # Validace a konverze backup_count
            backup_text = self.backup_input.text().strip()
            if not backup_text:
                raise ValueError("Backup count cannot be empty")
            
            backup_count = int(backup_text)
            if backup_count < 0:
                raise ValueError("Backup count cannot be negative")
            
            self.config.update_config(
                log_level=self.level_combo.currentText(),
                max_bytes=max_bytes,
                backup_count=backup_count,
                console_enabled=self.console_check.isChecked(),
                file_enabled=self.file_check.isChecked()
            )
            
            QMessageBox.information(self, "Success", "Logging configuration updated")
            logger.info("Logging configuration updated")
            
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Input", f"Please enter valid numbers:\n{str(e)}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update configuration:\n{str(e)}")
            logger.error(f"Failed to update logging configuration: {e}")


class LogViewerTab(QWidget):
    """Tab for viewing logs with real-time and historical views"""
    
    def __init__(self):
        super().__init__()
        # FIX 2: Validation of return value
        self.config = get_logging_config()
        if not self.config:
            raise RuntimeError("Failed to initialize logging configuration")
        
        self.real_time_handler = None
        self.current_filter_level = "ALL"
        self.current_filter_date = None
        self.current_filter_text = ""
        
        # FIX 6: Initialize current_log_content
        self.current_log_content = ""
        
        # FIX 11: Thread for loading files
        self.loader_thread = None
        
        # Filter threads for async operations
        self.rt_filter_thread = None
        self.hist_filter_thread = None
        self._is_filtering = False
        
        # Debounce timers for search fields to avoid excessive filtering
        self.rt_search_debounce_timer = QTimer()
        self.rt_search_debounce_timer.setSingleShot(True)
        self.rt_search_debounce_timer.timeout.connect(self._perform_rt_filter)
        
        self.hist_search_debounce_timer = QTimer()
        self.hist_search_debounce_timer.setSingleShot(True)
        self.hist_search_debounce_timer.timeout.connect(self._perform_hist_filter)

        # FIX 18: List to store all real-time log messages
        self.real_time_logs = []
        
        self.init_ui()
        
        # Set up real-time logging
        self.setup_real_time_logging()
        
        # FIX 4: Auto-refresh with visibility check
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_log_files)
        self.refresh_timer.start(LOG_REFRESH_INTERVAL_MS)
    
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Title and controls
        header_layout = QHBoxLayout()
        
        title = QLabel("<b>Application Logs</b>")
        title.setStyleSheet("font-size: 16px;")
        header_layout.addWidget(title)
        
        header_layout.addStretch()
        
        # Open log folder button
        open_folder_btn = QPushButton("📁 Open Log Folder")
        open_folder_btn.clicked.connect(self.open_log_folder)
        header_layout.addWidget(open_folder_btn)
        
        # Open external folder button
        open_external_btn = QPushButton("📂 Open External Folder")
        open_external_btn.clicked.connect(self.open_external_folder)
        header_layout.addWidget(open_external_btn)
        
        # Configuration button
        config_btn = QPushButton("⚙️ Logging Settings")
        config_btn.setToolTip("Open the Settings tab → Logging category to configure logging")
        config_btn.clicked.connect(self.show_config)
        header_layout.addWidget(config_btn)
        
        layout.addLayout(header_layout)
        
        # Info label
        info_label = QLabel(f"📂 Log folder: {self.config.get_log_dir().absolute()}")
        info_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(info_label)
        
        # Main tab widget
        self.tab_widget = QTabWidget()
        
        # Tab 1: Real-time logs
        self.real_time_tab = self.create_real_time_tab()
        self.tab_widget.addTab(self.real_time_tab, "🔴 Real-Time Logs")
        
        # Tab 2: Historical logs
        self.historical_tab = self.create_historical_tab()
        self.tab_widget.addTab(self.historical_tab, "📚 Historical Logs")
        
        layout.addWidget(self.tab_widget)
    
    def create_real_time_tab(self) -> QWidget:
        """Create real-time log viewer tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Filter controls
        filter_layout = QHBoxLayout()
        
        # Level filter
        filter_layout.addWidget(QLabel("Level:"))
        self.rt_level_combo = QComboBox()
        self.rt_level_combo.addItems(['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        self.rt_level_combo.currentTextChanged.connect(self.update_real_time_filter)
        filter_layout.addWidget(self.rt_level_combo)
        
        # Text filter
        filter_layout.addWidget(QLabel("Search:"))
        self.rt_search_input = QLineEdit()
        self.rt_search_input.setPlaceholderText("Filter by text...")
        self.rt_search_input.textChanged.connect(self._on_rt_search_changed)
        filter_layout.addWidget(self.rt_search_input, stretch=1)
        
        # Clear button
        clear_btn = QPushButton("🗑️ Clear")
        clear_btn.clicked.connect(self.clear_real_time_log)
        filter_layout.addWidget(clear_btn)
        
        # Auto-scroll checkbox
        self.auto_scroll_check = QCheckBox("Auto-scroll")
        self.auto_scroll_check.setChecked(True)
        filter_layout.addWidget(self.auto_scroll_check)
        
        layout.addLayout(filter_layout)
        
        # Log display
        self.real_time_text = QTextEdit()
        self.real_time_text.setReadOnly(True)
        self.real_time_text.setFont(QFont(LOG_FONT_FAMILY, LOG_FONT_SIZE))
        self.real_time_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #555;
            }
        """)
        layout.addWidget(self.real_time_text)
        
        # Stats
        self.rt_stats_label = QLabel("Lines: 0")
        self.rt_stats_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.rt_stats_label)
        
        return widget
    
    def create_historical_tab(self) -> QWidget:
        """Create historical log viewer tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Split view: file list on left, content on right
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left side: File list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        
        left_layout.addWidget(QLabel("<b>Log Files</b>"))
        
        # Refresh button
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_log_files)
        left_layout.addWidget(refresh_btn)
        
        # File list
        self.file_list = QListWidget()
        self.file_list.itemClicked.connect(self.load_log_file)
        left_layout.addWidget(self.file_list)
        
        splitter.addWidget(left_widget)
        
        # Right side: Log content
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        
        # Filter controls
        filter_layout = QHBoxLayout()
        
        # Level filter
        filter_layout.addWidget(QLabel("Level:"))
        self.hist_level_combo = QComboBox()
        self.hist_level_combo.addItems(['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
        self.hist_level_combo.currentTextChanged.connect(self.apply_historical_filters)
        filter_layout.addWidget(self.hist_level_combo)
        
        # Date filter
        self.hist_date_check = QCheckBox("Filter by date:")
        self.hist_date_check.stateChanged.connect(self.apply_historical_filters)
        filter_layout.addWidget(self.hist_date_check)
        
        self.hist_date_filter = QDateEdit()
        self.hist_date_filter.setDate(QDate.currentDate())
        self.hist_date_filter.setCalendarPopup(True)
        self.hist_date_filter.dateChanged.connect(self.apply_historical_filters)
        filter_layout.addWidget(self.hist_date_filter)
        
        # Text search
        filter_layout.addWidget(QLabel("Search:"))
        self.hist_search_input = QLineEdit()
        self.hist_search_input.setPlaceholderText("Filter by text...")
        self.hist_search_input.textChanged.connect(self._on_hist_search_changed)
        filter_layout.addWidget(self.hist_search_input, stretch=1)
        
        right_layout.addLayout(filter_layout)
        
        # Log display
        self.historical_text = QTextEdit()
        self.historical_text.setReadOnly(True)
        self.historical_text.setFont(QFont(LOG_FONT_FAMILY, LOG_FONT_SIZE))
        self.historical_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #555;
            }
        """)
        right_layout.addWidget(self.historical_text)
        
        # Stats
        self.hist_stats_label = QLabel("Select a log file to view")
        self.hist_stats_label.setStyleSheet("color: #888; font-size: 10px;")
        right_layout.addWidget(self.hist_stats_label)
        
        splitter.addWidget(right_widget)
        
        # Set initial splitter sizes
        splitter.setSizes([300, 700])
        
        layout.addWidget(splitter)
        
        # Initial load of file list
        self.refresh_log_files()
        
        return widget
    
    def setup_real_time_logging(self):
        """Set up real-time log handler"""
        self.real_time_handler = RealTimeLogHandler(self.add_real_time_log)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        self.real_time_handler.setFormatter(formatter)
        
        # Add to root logger
        logging.getLogger().addHandler(self.real_time_handler)
        
        logger.info("Real-time log viewer initialized")
    
    def _set_filtering_state(self, is_filtering, tab_name="real-time"):
        """Enable/disable UI during filtering - but preserve search field focus"""
        self._is_filtering = is_filtering
        
        if tab_name == "real-time":
            # Disable level combo but keep search field enabled for continuous typing
            self.rt_level_combo.setEnabled(not is_filtering)
            # Search field stays enabled to preserve focus and allow typing
            # Clear button should also stay enabled
        else:  # historical
            # For historical, only disable controls that aren't the search field
            self.hist_level_combo.setEnabled(not is_filtering)
            self.hist_date_filter.setEnabled(not is_filtering)
            self.hist_date_check.setEnabled(not is_filtering)
            # Search field stays enabled to preserve focus

    def _on_rt_search_changed(self):
        """Handle real-time search text change with debouncing"""
        # Stop the debounce timer
        self.rt_search_debounce_timer.stop()
        
        # Cancel any running filter
        if self.rt_filter_thread and self.rt_filter_thread.isRunning():
            self.rt_filter_thread.requestInterruption()
            # Don't wait - just let it finish in background
        
        # Start debounce timer (300ms delay)
        self.rt_search_debounce_timer.start(300)
    
    def _on_hist_search_changed(self):
        """Handle historical search text change with debouncing"""
        # Stop the debounce timer
        self.hist_search_debounce_timer.stop()
        
        # Cancel any running filter
        if self.hist_filter_thread and self.hist_filter_thread.isRunning():
            self.hist_filter_thread.requestInterruption()
            # Don't wait - just let it finish in background
        
        # Start debounce timer (300ms delay)
        self.hist_search_debounce_timer.start(300)
    
    def _perform_rt_filter(self):
        """Perform the actual real-time filtering after debounce delay"""
        self.update_real_time_filter()
    
    def _perform_hist_filter(self):
        """Perform the actual historical filtering after debounce delay"""
        self.apply_historical_filters()    

    def update_real_time_filter(self):
        """FIX 18: Asynchronous implementation of functional real-time filtering using stored messages"""
        # If no messages, do nothing - but still release the UI.  The controls
        # are disabled while a filter runs; returning early without resetting
        # the filtering state left the level combo disabled forever when the
        # buffer was cleared between the keystroke and the debounced filter.
        if not self.real_time_logs:
            self.rt_stats_label.setText("Lines: 0")
            self._set_filtering_state(False, "real-time")
            return
        
        # Cancel existing filter thread if running
        if self.rt_filter_thread and self.rt_filter_thread.isRunning():
            self.rt_filter_thread.requestInterruption()
            # Wait briefly for it to finish
            self.rt_filter_thread.wait(100)
        
        # Set filtering state (but search field stays enabled)
        self._set_filtering_state(True, "real-time")
        
        # Get current cursor position in search field to restore later
        search_field_had_focus = self.rt_search_input.hasFocus()
        cursor_position = self.rt_search_input.cursorPosition() if search_field_had_focus else 0
        
        # Create and start filter thread
        level_filter = self.rt_level_combo.currentText()
        search_text = self.rt_search_input.text()
        
        self.rt_filter_thread = RealTimeFilterThread(
            self.real_time_logs.copy(),  # Pass copy to avoid race conditions
            level_filter,
            search_text
        )
        
        # Store focus info for restoration
        self.rt_filter_thread._restore_focus = search_field_had_focus
        self.rt_filter_thread._cursor_position = cursor_position
        
        # Connect signals
        self.rt_filter_thread.filter_finished.connect(self._on_rt_filter_finished)
        self.rt_filter_thread.progress.connect(self._on_rt_filter_progress)
        
        # Start filtering
        self.rt_filter_thread.start()
    
    def _on_rt_filter_progress(self, current, total):
        """Update progress during real-time filtering"""
        self.rt_stats_label.setText(f"Filtering... {current}/{total}")
    
    def _on_rt_filter_finished(self, filtered_logs, total_count):
        """Handle completion of real-time filtering"""
        # Check if this is the current thread (not an old interrupted one)
        if self.sender() != self.rt_filter_thread:
            return
        
        # Save auto-scroll state
        auto_scroll = self.auto_scroll_check.isChecked()
        self.auto_scroll_check.setChecked(False)
        
        # Clear and display filtered logs
        self.real_time_text.clear()
        for log_entry in filtered_logs:
            self.append_colored_line(log_entry['msg'])
        
        # Restore auto-scroll
        self.auto_scroll_check.setChecked(auto_scroll)
        if auto_scroll:
            self.real_time_text.moveCursor(QTextCursor.MoveOperation.End)
        
        # Update stats
        self.rt_stats_label.setText(f"Lines: {len(filtered_logs)} / {total_count}")
        
        # Re-enable UI
        self._set_filtering_state(False, "real-time")
        
        # Restore focus and cursor position if search field had focus
        if hasattr(self.rt_filter_thread, '_restore_focus') and self.rt_filter_thread._restore_focus:
            self.rt_search_input.setFocus()
            if hasattr(self.rt_filter_thread, '_cursor_position'):
                self.rt_search_input.setCursorPosition(self.rt_filter_thread._cursor_position)

    def append_colored_line(self, line: str):
        """Append a colored line to real-time display"""
        # FIX 10: HTML escape
        escaped_line = escape(line)
        
        # Determine color based on log level using LOG_COLORS
        color = LOG_COLORS['DEFAULT']
        for level_name in ['ERROR', 'CRITICAL', 'WARNING', 'INFO', 'DEBUG']:
            if level_name in line:
                color = LOG_COLORS[level_name]
                break
        
        # Every message becomes its own paragraph (block).  The previous
        # version appended '<span>...</span><br>' with insertHtml(), which kept
        # all messages inside a single block: blockCount()/lineCount() stayed 1,
        # so the cap below never trimmed anything, and every insert re-laid out
        # the whole growing paragraph (a thousand lines blocked the GUI for
        # seconds, and the cost per line kept rising).
        doc = self.real_time_text.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(f'<span style="color: {color};">{escaped_line}</span>')
        
        # FIX 4 & 20: Omezit počet řádků - drop the oldest blocks
        excess = doc.blockCount() - MAX_REAL_TIME_LOG_LINES
        if excess > 0:
            trim = QTextCursor(doc)
            trim.movePosition(QTextCursor.MoveOperation.Start)
            trim.movePosition(QTextCursor.MoveOperation.NextBlock,
                              QTextCursor.MoveMode.KeepAnchor,
                              excess)
            trim.removeSelectedText()
        
        # Auto-scroll
        if self.auto_scroll_check.isChecked():
            self.real_time_text.moveCursor(QTextCursor.MoveOperation.End)
    
    def add_real_time_log(self, msg: str, level: int):
        """FIX 18: Callback to add log message to list and display"""
        # Store in list with level
        self.real_time_logs.append({'msg': msg, 'level': level})
        
        # Limit list size
        if len(self.real_time_logs) > MAX_REAL_TIME_LOG_LINES:
            self.real_time_logs.pop(0)
        
        # Display if passes filter (the duplicated inline filter code used the
        # same rules as the helper, so the helper is the single source of truth)
        if self._log_matches_filter({'msg': msg, 'level': level}):
            self.append_colored_line(msg)
        
        # Update stats.  This used to sit behind the early returns above, so a
        # message the filter hides never reached it and the "visible / stored"
        # counter kept showing a stale total even though the record was stored.
        visible_count = len([log for log in self.real_time_logs 
                            if self._log_matches_filter(log)])
        self.rt_stats_label.setText(f"Lines: {visible_count} / {len(self.real_time_logs)}")
    
    def _log_matches_filter(self, log_entry: dict) -> bool:
        """Helper to check if log entry matches current filter"""
        msg = log_entry['msg']
        level = log_entry['level']
        
        level_filter = self.rt_level_combo.currentText()
        search_text = self.rt_search_input.text().lower()
        
        # Level filter
        if level_filter != 'ALL':
            level_name = logging.getLevelName(level)
            if level_name != level_filter:
                return False
        
        # Text search
        if search_text and search_text not in msg.lower():
            return False
        
        return True
    
    def clear_real_time_log(self):
        """Clear real-time log display"""
        reply = QMessageBox.question(
            self,
            "Clear Logs",
            "Clear the real-time log display?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.real_time_text.clear()
            self.real_time_logs.clear()  # FIX 18: Also clear stored messages
            self.rt_stats_label.setText("Lines: 0")
            logger.info("Real-time log display cleared")
    
    def refresh_log_files(self):
        """FIX 4 & 13: Refresh list of log files s with visibility check and selection preservation"""
        # Visibility check
        if not self.isVisible():
            return
        
        # FIX 13: Uložit aktuální výběr
        current_selection = self.file_list.currentItem()
        current_file_name = None
        if current_selection:
            log_file_data = current_selection.data(Qt.ItemDataRole.UserRole)
            if log_file_data and isinstance(log_file_data, dict):
                current_file_name = log_file_data.get('name')
        
        self.file_list.clear()
        
        try:
            log_files = self.config.get_log_files()
        except Exception as e:
            logger.error(f"Failed to get log files: {e}")
            return
        
        for log_file in log_files:
            # Format size
            size_mb = log_file['size'] / MB_TO_BYTES
            
            # Format modified time
            modified_str = log_file['modified'].strftime('%Y-%m-%d %H:%M:%S')
            
            # Create item text
            if log_file['is_current']:
                item_text = f"🟢 {log_file['name']} (Current) - {size_mb:.2f} MB"
            else:
                item_text = f"📄 {log_file['name']} - {size_mb:.2f} MB"
            
            item_text += f"\n   Modified: {modified_str}"
            
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, log_file)
            self.file_list.addItem(item)
        
        # FIX 13: Restore selection
        if current_file_name:
            for i in range(self.file_list.count()):
                item = self.file_list.item(i)
                log_file_data = item.data(Qt.ItemDataRole.UserRole)
                if log_file_data and log_file_data.get('name') == current_file_name:
                    self.file_list.setCurrentItem(item)
                    break
    
    def load_log_file(self, item: QListWidgetItem):
        """FIX 5, 11, 14: Load and display selected log file s validací a asynchronním načítáním"""
        # FIX 14: Validace dat
        log_file = item.data(Qt.ItemDataRole.UserRole)
        if not log_file or not isinstance(log_file, dict):
            QMessageBox.warning(self, "Error", "Invalid log file data")
            return
        
        if 'path' not in log_file:
            QMessageBox.warning(self, "Error", "Log file path not found")
            return
        
        # FIX 14: 'size' is required by the large-file check below; a truncated
        # entry used to raise KeyError inside this slot instead of warning.
        if not isinstance(log_file.get('size'), (int, float)):
            QMessageBox.warning(self, "Error", "Log file size not found")
            return
        
        # FIX 5: Kontrola velikosti souboru před načtením
        if log_file['size'] > MAX_LOG_FILE_SIZE_MB * MB_TO_BYTES:
            reply = QMessageBox.question(
                self,
                "Large File",
                f"File is large ({log_file['size'] / MB_TO_BYTES:.2f} MB).\n"
                "Loading may take time and consume memory.\n"
                "Do you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
        # Show the loading state
        self.historical_text.setPlainText("Loading...")
        self.hist_stats_label.setText("Loading file...")

        # Stop a previous load.  wait() without a timeout blocks the GUI
        # thread indefinitely, which was one of the reasons the application
        # appeared to freeze; the old thread is asked to stop and abandoned if
        # it does not react in time.
        previous = self.loader_thread
        if previous is not None and previous.isRunning():
            previous.requestInterruption()
            try:
                previous.load_finished.disconnect(self.on_log_file_loaded)
                previous.error.disconnect(self.on_log_file_error)
            except TypeError:
                # Already disconnected - nothing to do
                pass
            if not previous.wait(LOADER_STOP_TIMEOUT_MS):
                logger.warning("Previous log loader did not stop in time")

        self.loader_thread = LogLoaderThread(log_file['path'], log_file)
        self.loader_thread.load_finished.connect(self.on_log_file_loaded)
        self.loader_thread.error.connect(self.on_log_file_error)
        self.loader_thread.start()
    
    def on_log_file_loaded(self, content: str, log_file: dict):
        """FIX 11: Callback pro úspěšné načtení souboru"""
        try:
            # Store full content for filtering
            self.current_log_content = content
            
            # Apply filters
            self.apply_historical_filters()
            
            # Update stats
            line_count = len(content.split('\n'))
            size_mb = log_file['size'] / MB_TO_BYTES
            self.hist_stats_label.setText(
                f"File: {log_file['name']} | Size: {size_mb:.2f} MB | Lines: {line_count}"
            )
            
            logger.info(f"Loaded log file: {log_file['name']}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to process log file: {str(e)}")
            logger.error(f"Failed to process log file: {e}")
    
    def on_log_file_error(self, error_msg: str):
        """FIX 11: Callback pro chybu při načítání"""
        self.historical_text.clear()
        self.hist_stats_label.setText("Error loading file")
        QMessageBox.critical(self, "Error", f"Failed to load log file:\n{error_msg}")
        logger.error(f"Failed to load log file: {error_msg}")
        
        # Refresh file list v případě že soubor byl smazán
        if "deleted" in error_msg.lower():
            self.refresh_log_files()
    
    def apply_historical_filters(self):
        """Apply filters to historical logs asynchronously"""
        if not self.current_log_content:
            return
        
        # Cancel existing filter thread if running
        if self.hist_filter_thread and self.hist_filter_thread.isRunning():
            self.hist_filter_thread.requestInterruption()
            # Wait briefly for it to finish
            self.hist_filter_thread.wait(100)
        
        # Set filtering state (but search field stays enabled)
        self._set_filtering_state(True, "historical")
        
        # Get current cursor position in search field to restore later
        search_field_had_focus = self.hist_search_input.hasFocus()
        cursor_position = self.hist_search_input.cursorPosition() if search_field_had_focus else 0
        
        # Get filter parameters
        level_filter = self.hist_level_combo.currentText()
        search_text = self.hist_search_input.text()
        date_filter_enabled = self.hist_date_check.isChecked()
        filter_date = None
        if date_filter_enabled:
            q_date = self.hist_date_filter.date()
            filter_date = datetime(q_date.year(), q_date.month(), q_date.day()).date()
        
        # Split content into lines
        lines = self.current_log_content.split('\n')
        
        # Create and start filter thread
        self.hist_filter_thread = HistoricalFilterThread(
            lines,
            level_filter,
            filter_date,
            date_filter_enabled,
            search_text
        )
        
        # Store focus info for restoration
        self.hist_filter_thread._restore_focus = search_field_had_focus
        self.hist_filter_thread._cursor_position = cursor_position
        
        # Connect signals
        self.hist_filter_thread.filter_finished.connect(self._on_hist_filter_finished)
        self.hist_filter_thread.progress.connect(self._on_hist_filter_progress)
        
        # Start filtering
        self.hist_filter_thread.start()
    
    def _on_hist_filter_progress(self, current, total):
        """Update progress during historical filtering"""
        current_stats = self.hist_stats_label.text()
        # Drop the counter of the previous update, whichever kind it was: only
        # " | Filtered:" was stripped, so consecutive progress updates piled up
        # as "... | Filtering... 0/1000 | Filtering... 500/1000 | ...".
        for suffix in (" | Filtering...", " | Filtered:"):
            if suffix in current_stats:
                current_stats = current_stats.split(suffix)[0]
        self.hist_stats_label.setText(current_stats + f" | Filtering... {current}/{total}")
    
    def _on_hist_filter_finished(self, filtered_lines):
        """Handle completion of historical filtering"""
        # Check if this is the current thread (not an old interrupted one)
        if self.sender() != self.hist_filter_thread:
            return
        
        # Display filtered content with coloring
        self.display_colored_log(filtered_lines)
        
        # Update stats
        current_stats = self.hist_stats_label.text()
        # Remove previous "Filtering..." or "Filtered" part if exists
        if " | Filtering..." in current_stats:
            current_stats = current_stats.split(" | Filtering...")[0]
        elif " | Filtered:" in current_stats:
            current_stats = current_stats.split(" | Filtered:")[0]
        
        self.hist_stats_label.setText(
            current_stats + f" | Filtered: {len(filtered_lines)} lines"
        )
        
        # Re-enable UI
        self._set_filtering_state(False, "historical")
        
        # Restore focus and cursor position if search field had focus
        if hasattr(self.hist_filter_thread, '_restore_focus') and self.hist_filter_thread._restore_focus:
            self.hist_search_input.setFocus()
            if hasattr(self.hist_filter_thread, '_cursor_position'):
                self.hist_search_input.setCursorPosition(self.hist_filter_thread._cursor_position)


    @staticmethod
    def _line_color(line: str) -> str:
        """Return the display colour for a log line based on its level."""
        if 'ERROR' in line or 'CRITICAL' in line:
            return '#EF5350'
        if 'WARNING' in line:
            return '#FFA726'
        if 'INFO' in line:
            return '#4CAF50'
        if 'DEBUG' in line:
            return '#888888'
        return '#d4d4d4'

    def display_colored_log(self, lines: list):
        """
        Display log lines with colour coding and HTML escaping.

        The whole document is built as a single HTML string and handed to the
        widget in one call.  The previous implementation called
        ``insertHtml()`` once per line, which re-laid out the entire document
        every time: opening a log file with a few thousand lines took minutes
        and looked like the application had frozen at "Loading...".

        Very long files are trimmed to :data:`MAX_DISPLAYED_LOG_LINES` lines so
        the view stays responsive; the user is told when that happens.
        """
        total = len(lines)
        truncated = total > MAX_DISPLAYED_LOG_LINES
        visible = lines[-MAX_DISPLAYED_LOG_LINES:] if truncated else lines

        parts = []
        if truncated:
            parts.append(
                '<span style="color: #FFA726;">— showing the last '
                f'{MAX_DISPLAYED_LOG_LINES} of {total} lines. Use the filters '
                'above to narrow the result. —</span><br>'
            )

        for line in visible:
            parts.append(
                f'<span style="color: {self._line_color(line)};">'
                f'{escape(line)}</span><br>'
            )

        # One single update instead of one per line
        self.historical_text.setUpdatesEnabled(False)
        try:
            self.historical_text.clear()
            self.historical_text.setHtml(''.join(parts))
        finally:
            self.historical_text.setUpdatesEnabled(True)
    
    def open_log_folder(self):
        """FIX 7: Open default log folder in file manager with better error handling"""
        log_dir = self.config.get_log_dir()
        
        if not log_dir.exists():
            QMessageBox.warning(self, "Not Found", "Log folder does not exist yet")
            return
        
        try:
            if platform.system() == 'Windows':
                os.startfile(log_dir)
            elif platform.system() == 'Darwin':  # macOS
                result = subprocess.run(
                    ['open', str(log_dir)],
                    timeout=SUBPROCESS_TIMEOUT,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        result.returncode, 'open', 
                        output=result.stdout, 
                        stderr=result.stderr
                    )
            else:  # Linux
                result = subprocess.run(
                    ['xdg-open', str(log_dir)],
                    timeout=SUBPROCESS_TIMEOUT,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        result.returncode, 'xdg-open',
                        output=result.stdout,
                        stderr=result.stderr
                    )
            
            logger.info(f"Opened log folder: {log_dir}")
            
        except subprocess.TimeoutExpired:
            QMessageBox.warning(
                self, 
                "Timeout", 
                "Opening folder took too long. Please open it manually."
            )
            logger.warning(f"Timeout opening log folder: {log_dir}")
        except subprocess.CalledProcessError as e:
            error_msg = f"Failed to open folder: {str(e)}"
            if e.stderr:
                error_msg += f"\n{e.stderr}"
            QMessageBox.critical(self, "Error", error_msg)
            logger.error(f"Failed to open log folder: {e}")
        except FileNotFoundError as e:
            QMessageBox.critical(
                self, 
                "Error", 
                f"File manager not found. Please open the folder manually:\n{log_dir}"
            )
            logger.error(f"File manager not found: {e}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Unexpected error: {str(e)}")
            logger.error(f"Unexpected error opening log folder: {e}")
    
    def open_external_folder(self):
        """FIX 3: Open external folder and load logs from there with try-finally"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Log Folder",
            str(self.config.get_log_dir())
        )
        
        if folder:
            # FIX 3: Use try-finally to ensure restoration of original value
            original_log_dir = self.config.config['log_dir']
            try:
                # Temporarily switch to external folder
                self.config.config['log_dir'] = folder
                
                # Refresh file list
                self.refresh_log_files()
                
                logger.info(f"Loaded logs from external folder: {folder}")
                
                QMessageBox.information(
                    self,
                    "External Folder",
                    f"Loaded logs from:\n{folder}\n\n"
                    "Note: This is temporary. Default folder will be restored on next refresh."
                )
            finally:
                # FIX 3: Always restore original log dir
                self.config.config['log_dir'] = original_log_dir
    
    def show_config(self):
        """Redirect to Settings tab for logging configuration"""
        QMessageBox.information(
            self,
            "Logging Configuration",
            "Logging configuration has been moved to the Settings tab.\n\n"
            "Please go to the '4.Settings' tab → 'Logging' category to configure logging."
        )
    
    def closeEvent(self, event):
        """FIX 16: Clean up on close with better error handling"""
        # Stop timer
        try:
            if hasattr(self, 'refresh_timer') and self.refresh_timer:
                self.refresh_timer.stop()
        except Exception as e:
            logger.error(f"Error stopping timer: {e}")
        
        # Wait for loader thread
        try:
            if hasattr(self, 'loader_thread') and self.loader_thread:
                if self.loader_thread.isRunning():
                    self.loader_thread.wait(1000)  # Wait max 1 second
        except Exception as e:
            logger.error(f"Error stopping loader thread: {e}")
        
        # Stop filter threads
        try:
            if hasattr(self, 'rt_search_debounce_timer') and self.rt_search_debounce_timer:
                self.rt_search_debounce_timer.stop()
        except Exception as e:
            logger.error(f"Error stopping RT debounce timer: {e}")
        
        try:
            if hasattr(self, 'hist_search_debounce_timer') and self.hist_search_debounce_timer:
                self.hist_search_debounce_timer.stop()
        except Exception as e:
            logger.error(f"Error stopping historical debounce timer: {e}")
        
        try:
            if hasattr(self, 'rt_filter_thread') and self.rt_filter_thread:
                if self.rt_filter_thread.isRunning():
                    self.rt_filter_thread.requestInterruption()
                    self.rt_filter_thread.wait(1000)
        except Exception as e:
            logger.error(f"Error stopping real-time filter thread: {e}")
        
        try:
            if hasattr(self, 'hist_filter_thread') and self.hist_filter_thread:
                if self.hist_filter_thread.isRunning():
                    self.hist_filter_thread.requestInterruption()
                    self.hist_filter_thread.wait(1000)
        except Exception as e:
            logger.error(f"Error stopping historical filter thread: {e}")

        # Remove real-time handler
        try:
            if self.real_time_handler:
                root_logger = logging.getLogger()
                if self.real_time_handler in root_logger.handlers:
                    root_logger.removeHandler(self.real_time_handler)
                    logger.info("Real-time log handler removed")
        except Exception as e:
            logger.error(f"Error removing handler: {e}")
        
        event.accept()
