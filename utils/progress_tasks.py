"""
Universal Progress Task System
Provides abstract base classes for long-running tasks with progress reporting

Thread Safety:
- All control methods (cancel, pause, resume) are thread-safe
- State queries (is_cancelled, is_paused) are thread-safe
- Signals can be emitted from the worker thread
- Tasks should NOT be reused - create a new instance for each execution

Usage:
    class MyTask(AbstractProgressTask):
        def execute(self) -> str:
            for i in range(100):
                self.check_cancelled()  # Raises TaskCancelledException if cancelled
                self.wait_if_paused()   # Blocks if paused
                self.emit_progress(i, f"Processing {i}/100")
            return "Completed successfully"
        
        def cleanup(self):
            # Release resources here
            pass
    
    task = MyTask("My Task", is_deterministic=True, can_pause=True)
    task.start()
"""

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal, QDateTime, QMutex, QWaitCondition

logger = logging.getLogger(__name__)


class LogLevel(Enum):
    """Log message severity levels"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class QThreadABCMeta(type(QThread), type(ABC)):
    """
    Metaclass that combines QThread and ABC metaclasses.
    Required to create abstract classes that inherit from QThread.
    """
    pass


class AbstractProgressTask(QThread, ABC, metaclass=QThreadABCMeta):
    """
    Abstract base class for all long-running tasks with progress reporting.
    
    Provides:
    - Progress reporting (deterministic and indeterminate modes)
    - Pause/resume support (optional)
    - Cancellation support
    - Structured logging with timestamps
    - Automatic timing and statistics
    - Exception handling
    
    Thread Safety:
    - All public methods are thread-safe and can be called from any thread
    - Signals are emitted from the worker thread
    - Control flags are protected by mutex
    
    Important Notes:
    - Tasks should NOT be reused - create a new instance for each execution
    - Subclasses should call check_cancelled() or is_cancelled() regularly
    - check_cancelled() raises TaskCancelledException for exception-based control flow
    
    Subclasses must implement:
    - execute() - main task logic
    - cleanup() - cleanup operations
    """
    
    # Signals for communication with UI
    progress_changed = pyqtSignal(int, str)  # value (0-100 or -1), message
    log_message = pyqtSignal(str, LogLevel, QDateTime)  # message, level, timestamp
    task_started = pyqtSignal(QDateTime)  # start time
    task_finished = pyqtSignal(bool, QDateTime, str)  # success, end time, result message
    task_cancelled = pyqtSignal()
    task_paused = pyqtSignal()
    task_resumed = pyqtSignal()
    task_error = pyqtSignal(str)  # error message
    
    def __init__(self, task_name: str, is_deterministic: bool = True, can_pause: bool = False):
        """
        Initialize the progress task.
        
        Args:
            task_name: Human-readable name of the task
            is_deterministic: True if progress can be measured (0-100%), False for indeterminate
            can_pause: True if task supports pause/resume
        """
        super().__init__()
        
        self._task_name = task_name
        self._is_deterministic = is_deterministic
        self._can_pause = can_pause
        
        # Control flags - protected by mutex for thread safety
        self._control_mutex = QMutex()
        self._cancel_requested = False
        self._pause_requested = False
        
        # Timing
        self._start_time: Optional[QDateTime] = None
        self._end_time: Optional[QDateTime] = None
        
        # Thread synchronization for pause
        self._pause_mutex = QMutex()
        self._pause_condition = QWaitCondition()
        
        logger.debug(f"Created task: {task_name} (deterministic={is_deterministic}, pausable={can_pause})")
    
    def run(self):
        """
        Main thread execution - DO NOT OVERRIDE.
        Handles exception catching, timing, and calls execute().
        """
        try:
            self._start_time = QDateTime.currentDateTime()
            self.task_started.emit(self._start_time)
            self.emit_log(f"Task '{self._task_name}' started", LogLevel.INFO)
            
            # Execute main task logic
            result_message = self.execute()
            
            # Check if cancelled during execution (thread-safe check)
            if self.is_cancelled():
                self._handle_cancellation()
                return
            
            # Task completed successfully
            self._end_time = QDateTime.currentDateTime()
            self.emit_log(f"Task completed successfully: {result_message}", LogLevel.SUCCESS)
            self.task_finished.emit(True, self._end_time, result_message)
            
        except TaskCancelledException:
            # Handle cancellation exception gracefully
            self._handle_cancellation()
            
        except Exception as e:
            logger.exception(f"Error in task '{self._task_name}'")
            self._end_time = QDateTime.currentDateTime()
            error_msg = str(e)
            self.emit_log(f"Task failed with error: {error_msg}", LogLevel.ERROR)
            self.task_error.emit(error_msg)
            self.task_finished.emit(False, self._end_time, f"Error: {error_msg}")
        
        finally:
            # Always cleanup
            try:
                self.cleanup()
            except Exception as e:
                logger.exception(f"Error during cleanup of task '{self._task_name}'")
                self.emit_log(f"Cleanup failed: {str(e)}", LogLevel.ERROR)
    
    @abstractmethod
    def execute(self) -> str:
        """
        Execute the main task logic.
        
        Implementation should:
        - Periodically call check_cancelled() to honor cancellation requests
          (raises TaskCancelledException)
        - OR use if self.is_cancelled(): break pattern
        - Call wait_if_paused() at safe points to support pausing
        - Use emit_progress() to report progress
        - Use emit_log() for status messages
        
        Thread Safety:
        - This method runs in a separate worker thread
        - All provided methods (emit_progress, emit_log, check_cancelled, etc.) are thread-safe
        
        Returns:
            Result message describing what was accomplished
            
        Raises:
            TaskCancelledException: If task is cancelled (when using check_cancelled())
            Exception: If task fails
        """
        pass
    
    @abstractmethod
    def cleanup(self):
        """
        Cleanup operations after task completion (success or failure).
        Called automatically in finally block.
        
        Should release any resources (files, connections, etc.)
        Exceptions are logged but don't prevent task completion signal.
        """
        pass
    
    def request_cancel(self):
        """
        Request task cancellation. Thread-safe.
        Task should check is_cancelled() periodically or use check_cancelled().
        """
        self._control_mutex.lock()
        self._cancel_requested = True
        self._control_mutex.unlock()
        
        self.emit_log("Cancellation requested", LogLevel.WARNING)
        
        # Wake up thread if paused
        self._pause_mutex.lock()
        if self._pause_requested:
            self._pause_requested = False
            self._pause_condition.wakeAll()
        self._pause_mutex.unlock()
    
    def request_pause(self):
        """Request task pause (only if can_pause is True). Thread-safe."""
        if not self._can_pause:
            logger.warning(f"Task '{self._task_name}' does not support pausing")
            return
        
        self._control_mutex.lock()
        self._pause_requested = True
        self._control_mutex.unlock()
        
        self.emit_log("Pause requested", LogLevel.INFO)
        self.task_paused.emit()
    
    def request_resume(self):
        """Resume paused task. Thread-safe."""
        if not self._can_pause:
            return
        
        self._control_mutex.lock()
        self._pause_requested = False
        self._control_mutex.unlock()
        
        self._pause_mutex.lock()
        self._pause_condition.wakeAll()
        self._pause_mutex.unlock()
        
        self.emit_log("Resuming task", LogLevel.INFO)
        self.task_resumed.emit()
    
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested. Thread-safe."""
        self._control_mutex.lock()
        cancelled = self._cancel_requested
        self._control_mutex.unlock()
        return cancelled
    
    def is_paused(self) -> bool:
        """Check if task is currently paused. Thread-safe."""
        self._control_mutex.lock()
        paused = self._pause_requested
        self._control_mutex.unlock()
        return paused
    
    def wait_if_paused(self):
        """
        Block execution if pause was requested. Thread-safe.
        Task should call this at safe pause points.
        """
        if not self._can_pause:
            return
        
        self._pause_mutex.lock()
        
        # Check control flags in a thread-safe way
        self._control_mutex.lock()
        should_wait = self._pause_requested and not self._cancel_requested
        self._control_mutex.unlock()
        
        while should_wait:
            self._pause_condition.wait(self._pause_mutex)
            
            # Re-check after waking up
            self._control_mutex.lock()
            should_wait = self._pause_requested and not self._cancel_requested
            self._control_mutex.unlock()
        
        self._pause_mutex.unlock()
    
    def check_cancelled(self):
        """
        Check if task was cancelled and raise exception if so.
        Thread-safe convenience method for tasks that want to use exceptions for cancellation.
        
        Raises:
            TaskCancelledException: If cancellation was requested
        """
        if self.is_cancelled():
            raise TaskCancelledException("Task was cancelled by user")
    
    def emit_progress(self, value: int, message: str = ""):
        """
        Report progress update. Thread-safe.
        
        Args:
            value: Progress value (0-100 for deterministic, -1 for indeterminate)
            message: Optional status message
        """
        self.progress_changed.emit(value, message)
    
    def emit_log(self, message: str, level: LogLevel = LogLevel.INFO):
        """
        Log a message with timestamp. Thread-safe.
        
        Args:
            message: Log message
            level: Severity level
        """
        timestamp = QDateTime.currentDateTime()
        self.log_message.emit(message, level, timestamp)
        
        # Map SUCCESS to INFO for Python logger since logger doesn't have success() method
        log_level_map = {
            LogLevel.DEBUG: logger.debug,
            LogLevel.INFO: logger.info,
            LogLevel.WARNING: logger.warning,
            LogLevel.ERROR: logger.error,
            LogLevel.SUCCESS: logger.info  # Map SUCCESS to info
        }
        
        log_method = log_level_map.get(level, logger.info)
        log_method(f"[{self._task_name}] {message}")
    
    def _handle_cancellation(self):
        """Handle task cancellation."""
        self._end_time = QDateTime.currentDateTime()
        self.emit_log("Task cancelled by user", LogLevel.WARNING)
        self.task_cancelled.emit()
        self.task_finished.emit(False, self._end_time, "Cancelled by user")
    
    @property
    def task_name(self) -> str:
        """Get task name."""
        return self._task_name
    
    @property
    def is_deterministic(self) -> bool:
        """Check if task has deterministic progress."""
        return self._is_deterministic
    
    @property
    def can_pause(self) -> bool:
        """Check if task supports pausing."""
        return self._can_pause
    
    @property
    def start_time(self) -> Optional[QDateTime]:
        """Get task start time."""
        return self._start_time
    
    @property
    def end_time(self) -> Optional[QDateTime]:
        """Get task end time."""
        return self._end_time


class TaskCancelledException(Exception):
    """Exception raised when task is cancelled."""
    pass
