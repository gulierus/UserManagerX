"""
Advanced Logging Configuration System
Provides rotating file handlers and configurable logging
"""

import logging
import logging.handlers
import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Optional
import json
from datetime import datetime
from html import escape  # FIX 1: Import for HTML escape
from PyQt6.QtCore import QTimer  # FIX 2: Import for thread safety
from PyQt6.QtGui import QTextCursor  # FIX 3: Import for proper cursor access

# FIX 11: Constants instead of magic values
MB_TO_BYTES = 1024 * 1024
DEFAULT_MAX_LOG_SIZE_MB = 10
DEFAULT_BACKUP_COUNT = 5
SEPARATOR_LENGTH = 80


# FIX 4: Valid log levels
VALID_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

# Log level colors (shared constant for consistent coloring)
LOG_COLORS = {
    'ERROR': '#EF5350',      # Red
    'CRITICAL': '#EF5350',   # Red
    'WARNING': '#FFA726',    # Orange
    'INFO': '#4CAF50',       # Green
    'DEBUG': '#888888',      # Gray
    'DEFAULT': '#d4d4d4'     # Light gray
}


class LoggingConfig:
    """Configuration for application logging"""
    
    # Default configuration
    DEFAULT_CONFIG = {
        'log_dir': 'logs',
        'log_file': 'student_management.log',
        'max_bytes': DEFAULT_MAX_LOG_SIZE_MB * MB_TO_BYTES,
        'backup_count': DEFAULT_BACKUP_COUNT,
        'log_level': 'INFO',
        'console_enabled': True,
        'file_enabled': True,
        'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        'date_format': '%Y-%m-%d %H:%M:%S'
    }
    
    def __init__(self, config_file: str = 'logging_config.json'):
        self.config_file = config_file
        self.config = self.load_config()
        self.logger = None
        self.file_handler = None
        self.console_handler = None
    
    def load_config(self) -> dict:
        """FIX 5: Load configuration from file or use defaults s lepším error handlingem"""
        if not os.path.exists(self.config_file):
            return self.DEFAULT_CONFIG.copy()
        
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Validate config is a dict
            if not isinstance(config, dict):
                print(f"Warning: Config file contains invalid format, using defaults", 
                      file=sys.stderr)
                return self.DEFAULT_CONFIG.copy()
            
            # FIX 4: Validate log_level if present
            if 'log_level' in config and config['log_level'] not in VALID_LOG_LEVELS:
                print(f"Warning: Invalid log level '{config['log_level']}', using INFO", 
                      file=sys.stderr)
                config['log_level'] = 'INFO'
            
            # FIX 15: Validate paths
            if 'log_dir' in config and not self.validate_path(config['log_dir']):
                print(f"Warning: Invalid log_dir path, using default", file=sys.stderr)
                config['log_dir'] = self.DEFAULT_CONFIG['log_dir']
            
            if 'log_file' in config and not self.validate_path(config['log_file']):
                print(f"Warning: Invalid log_file path, using default", file=sys.stderr)
                config['log_file'] = self.DEFAULT_CONFIG['log_file']
            
            # Merge with defaults for any missing keys
            return {**self.DEFAULT_CONFIG, **config}
            
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}", file=sys.stderr)
            return self.DEFAULT_CONFIG.copy()
        except PermissionError:
            print(f"Error: Permission denied reading config file", file=sys.stderr)
            return self.DEFAULT_CONFIG.copy()
        except Exception as e:
            print(f"Error loading logging config: {e}", file=sys.stderr)
            return self.DEFAULT_CONFIG.copy()
    
    def save_config(self) -> bool:
        """FIX 6: Save current configuration to file s atomic write"""
        try:
            # FIX 6: Write to temporary file first (atomic write)
            config_dir = os.path.dirname(self.config_file)
            if not config_dir:
                config_dir = '.'
            
            # Ensure config directory exists
            if config_dir != '.':
                try:
                    os.makedirs(config_dir, exist_ok=True)
                except (PermissionError, OSError):
                    pass  # Will fail later with proper error
            
            temp_fd, temp_path = tempfile.mkstemp(
                suffix='.json',
                dir=config_dir,
                text=True
            )
            
            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=2)
                
                # FIX 6: Atomic replace
                shutil.move(temp_path, self.config_file)
                return True
                
            except Exception:
                # Clean up temp file if something went wrong
                try:
                    os.unlink(temp_path)
                except:
                    pass
                raise
                
        except PermissionError:
            print(f"Error: Permission denied writing config file", file=sys.stderr)
            return False
        except OSError as e:
            if e.errno == 28:  # No space left on device
                print(f"Error: No space left on device", file=sys.stderr)
            else:
                print(f"Error: Cannot write config file: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Error saving logging config: {e}", file=sys.stderr)
            return False
    
    def get_log_dir(self) -> Path:
        """Get log directory path"""
        return Path(self.config['log_dir'])
    
    def get_log_file_path(self) -> Path:
        """Get full log file path"""
        return self.get_log_dir() / self.config['log_file']
    
    def ensure_log_dir(self) -> bool:
        """FIX 7: Ensure log directory exists s error handlingem"""
        try:
            log_dir = self.get_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            return True
        except PermissionError:
            print(f"Error: Permission denied creating log directory: {log_dir}", 
                  file=sys.stderr)
            return False
        except OSError as e:
            print(f"Error: Cannot create log directory: {e}", file=sys.stderr)
            return False
    
    def get_log_level(self, level_name: str) -> int:
        """FIX 4: Get logging level from name with validation"""
        if level_name not in VALID_LOG_LEVELS:
            print(f"Warning: Invalid log level '{level_name}', using INFO", file=sys.stderr)
            level_name = 'INFO'
        return getattr(logging, level_name)
    
    def validate_path(self, path: str) -> bool:
        """
        Validate that a configured path is usable.

        The previous version rejected every absolute path (anything starting
        with ``/`` or ``\\``, and anything containing ``:``).  Since the user
        can pick the log folder with a "Browse..." button, that made every
        chosen folder - ``C:\\Logs`` as well as ``/var/log/app`` - invalid, and
        update_config() refused the change without any visible error.

        Args:
            path: The value configured for ``log_dir`` or ``log_file``.

        Returns:
            True when the value can be used as a path.
        """
        if not path or not isinstance(path, str):
            return False

        candidate = path.strip()
        if not candidate or '\0' in candidate:
            return False

        # Path traversal is rejected, but only as a complete segment so a
        # folder called "my..logs" stays valid.
        segments = candidate.replace('\\', '/').split('/')
        if any(segment == '..' for segment in segments):
            return False

        # Characters that are invalid on Windows.  A colon is allowed only as
        # the drive separator ("C:\\...").
        if any(c in candidate for c in '<>"|?*'):
            return False

        colon_index = candidate.find(':')
        if colon_index != -1 and (colon_index != 1 or not candidate[0].isalpha()):
            return False
        if candidate.count(':') > 1:
            return False

        return True
    
    def setup_logging(self) -> bool:
        """FIX 8: Set up logging with rotating file handler s comprehensive error handlingem"""
        # FIX 7: Ensure log directory exists
        if not self.ensure_log_dir():
            print("Warning: Could not create log directory, file logging disabled", 
                  file=sys.stderr)
            self.config['file_enabled'] = False
        
        try:
            # Get root logger
            root_logger = logging.getLogger()
            root_logger.setLevel(self.get_log_level(self.config['log_level']))
            
            # Remove existing handlers to avoid duplicates
            root_logger.handlers.clear()
            
            # Create formatter
            formatter = logging.Formatter(
                self.config['format'],
                datefmt=self.config['date_format']
            )
            
            # File handler with rotation
            if self.config['file_enabled']:
                try:
                    self.file_handler = logging.handlers.RotatingFileHandler(
                        str(self.get_log_file_path()),
                        maxBytes=self.config['max_bytes'],
                        backupCount=self.config['backup_count'],
                        encoding='utf-8'
                    )
                    self.file_handler.setFormatter(formatter)
                    self.file_handler.setLevel(self.get_log_level(self.config['log_level']))
                    root_logger.addHandler(self.file_handler)
                except (PermissionError, OSError) as e:
                    print(f"Warning: Could not create file handler: {e}", file=sys.stderr)
                    self.config['file_enabled'] = False
            
            # Console handler
            if self.config['console_enabled']:
                try:
                    self.console_handler = logging.StreamHandler()
                    self.console_handler.setFormatter(formatter)
                    self.console_handler.setLevel(self.get_log_level(self.config['log_level']))
                    root_logger.addHandler(self.console_handler)
                except Exception as e:
                    print(f"Warning: Could not create console handler: {e}", file=sys.stderr)
            
            # Log startup message (only if we have at least one handler)
            if root_logger.handlers:
                logging.info("=" * SEPARATOR_LENGTH)
                logging.info(f"Logging initialized at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                if self.config['file_enabled']:
                    logging.info(f"Log file: {self.get_log_file_path()}")
                logging.info(f"Log level: {self.config['log_level']}")
                logging.info("=" * SEPARATOR_LENGTH)
            else:
                print("Warning: No logging handlers could be initialized", file=sys.stderr)
            
            return True
            
        except Exception as e:
            print(f"Error setting up logging: {e}", file=sys.stderr)
            return False
    
    def update_config(self, **kwargs) -> bool:
        """FIX 10: Update configuration with validation"""
        # Validate parameters
        valid_keys = set(self.DEFAULT_CONFIG.keys())
        invalid_keys = set(kwargs.keys()) - valid_keys
        
        if invalid_keys:
            logging.warning(f"Invalid config keys ignored: {invalid_keys}")
            kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}
        
        if not kwargs:
            logging.warning("No valid parameters to update")
            return False
        
        # FIX 10: Validate specific parameters
        if 'log_level' in kwargs:
            if kwargs['log_level'] not in VALID_LOG_LEVELS:
                logging.error(f"Invalid log level: {kwargs['log_level']}")
                return False
        
        # isinstance(True, int) is True in Python, so a boolean slipped through
        # and became maxBytes=1 - rotating the log file on every single record.
        if 'max_bytes' in kwargs:
            value = kwargs['max_bytes']
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                logging.error(f"Invalid max_bytes: {value}")
                return False

        if 'backup_count' in kwargs:
            value = kwargs['backup_count']
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                logging.error(f"Invalid backup_count: {value}")
                return False
        
        # FIX 15: Validate paths
        if 'log_dir' in kwargs and not self.validate_path(kwargs['log_dir']):
            logging.error(f"Invalid log_dir path: {kwargs['log_dir']}")
            return False
        
        if 'log_file' in kwargs and not self.validate_path(kwargs['log_file']):
            logging.error(f"Invalid log_file path: {kwargs['log_file']}")
            return False
        
        # Update config with rollback capability
        old_config = self.config.copy()
        self.config.update(kwargs)
        
        # Save config
        if not self.save_config():
            # Revert on save failure
            self.config = old_config
            logging.error("Failed to save config, changes reverted")
            return False
        
        # Reapply logging configuration
        if not self.setup_logging():
            # Revert on setup failure
            self.config = old_config
            self.save_config()
            self.setup_logging()
            logging.error("Failed to apply logging config, changes reverted")
            return False
        
        logging.info("Configuration updated successfully")
        return True
    
    def get_log_files(self) -> list:
        """FIX 9: Get list of all log files (current + rotated) s error handlingem"""
        log_dir = self.get_log_dir()
        if not log_dir.exists():
            return []
        
        log_file_name = self.config['log_file']
        log_files = []
        
        def add_log_file(file_path: Path, is_current: bool) -> None:
            """Helper to safely add log file info"""
            try:
                if not file_path.exists():
                    return
                
                stat_info = file_path.stat()
                log_files.append({
                    'path': file_path,
                    'name': file_path.name,
                    'size': stat_info.st_size,
                    'modified': datetime.fromtimestamp(stat_info.st_mtime),
                    'is_current': is_current
                })
            except (PermissionError, OSError) as e:
                # Skip files we can't access
                logging.debug(f"Could not access log file {file_path}: {e}")
            except ValueError as e:
                # Invalid timestamp
                logging.debug(f"Invalid timestamp for {file_path}: {e}")
            except Exception as e:
                logging.warning(f"Unexpected error accessing {file_path}: {e}")
        
        # Current log file
        current_log = log_dir / log_file_name
        add_log_file(current_log, True)
        
        # Rotated log files.  The value comes from a JSON file the user can
        # edit, so it may be a string; range() would then raise TypeError and
        # the whole Logs tab would come up empty.
        try:
            backup_count = int(self.config.get('backup_count', DEFAULT_BACKUP_COUNT))
        except (TypeError, ValueError):
            logging.warning("Invalid backup_count %r in the logging configuration",
                            self.config.get('backup_count'))
            backup_count = DEFAULT_BACKUP_COUNT

        for i in range(1, max(0, backup_count) + 1):
            rotated_log = log_dir / f"{log_file_name}.{i}"
            add_log_file(rotated_log, False)
        
        # Sort by modification time (newest first)
        try:
            log_files.sort(key=lambda x: x['modified'], reverse=True)
        except (TypeError, KeyError) as e:
            logging.warning(f"Could not sort log files: {e}")
        
        return log_files

# Global logging config instance
_logging_config = None

def get_logging_config() -> Optional[LoggingConfig]:
    """FIX 13: Get global logging configuration instance s error handlingem"""
    global _logging_config
    if _logging_config is None:
        try:
            _logging_config = LoggingConfig()
        except Exception as e:
            print(f"Fatal: Could not initialize logging config: {e}", file=sys.stderr)
            return None
    return _logging_config


def setup_application_logging() -> Optional[LoggingConfig]:
    """Set up application-wide logging"""
    config = get_logging_config()
    if config:
        config.setup_logging()
    return config
