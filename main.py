"""
Student Management System
Main application file with PyQt6 GUI and Advanced Logging
VERSION 2 - With Font Manager initialization
"""

import sys
import logging
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QMessageBox, QStyleFactory
)
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt

# CRITICAL: QtWebEngine (used by the PDF preview) requires the
# AA_ShareOpenGLContexts attribute to be set before the QApplication instance
# exists.  Setting it here - before any module that pulls in QtWebEngine is
# imported - keeps the PDF preview working no matter in which order the UI
# modules are loaded.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

# CRITICAL: Import resources BEFORE creating QApplication
# This loads fonts and other resources into Qt's resource system
try:
    import resources.resources_rc  # Generated from resources.qrc
    RESOURCES_LOADED = True
except ImportError:
    RESOURCES_LOADED = False
    print("WARNING: resources_rc not found - custom fonts will not be available")
    print("Run: pyrcc6 resources.qrc -o resources_rc.py")

from ui.source_selection_tab import SourceSelectionTab
from ui.comparison_tab import ComparisonTab
from ui.operations_tab import OperationsTab
from ui.settings_tab import SettingsTab
from ui.log_viewer_tab import LogViewerTab
from ui.source_manager_tab import SourceManagerTab
from ui.equal_width_tab_bar import EqualWidthTabBar
from models import SourceManager
from utils.logging_config import setup_application_logging
from utils.font_manager import initialize_fonts, get_font_manager

# Set up logging BEFORE creating any loggers
logging_config = setup_application_logging()

logger = logging.getLogger(__name__)


class StudentManagementSystem(QMainWindow):
    """Main application window for Student Management System"""
    
    def __init__(self):
        super().__init__()
        logger.info("=" * 80)
        logger.info("Initializing Student Management System")
        logger.info("=" * 80)
        
        self.source_manager = SourceManager()
        self.init_ui()
        
        logger.info("Application initialization complete")
        
    def init_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle("Student Management System")
        self.setMinimumSize(1200, 800)
        
        logger.debug("Applying application style")
        self.apply_modern_style()
        
        # Create central widget with tab widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create tab widget - all tabs share the same width
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabBar(EqualWidthTabBar(self.tab_widget))
        self.tab_widget.setElideMode(Qt.TextElideMode.ElideNone)
        self.tab_widget.setUsesScrollButtons(False)
        layout.addWidget(self.tab_widget)
        
        logger.debug("Creating application tabs")
        
        # Create tabs
        try:
            self.source_selection_tab = SourceSelectionTab(self.source_manager)
            self.comparison_tab = ComparisonTab(self.source_manager)
            self.operations_tab = OperationsTab(self.source_manager)
            self.settings_tab = SettingsTab()
            self.log_viewer_tab = LogViewerTab()
            self.source_manager_tab = SourceManagerTab(self.source_manager)
            
            # Add tabs
            self.tab_widget.addTab(self.source_selection_tab, "1. Data Sources")
            self.tab_widget.addTab(self.comparison_tab, "2. Comparison and Sync")
            self.tab_widget.addTab(self.operations_tab, "3. Operations")
            self.tab_widget.addTab(self.settings_tab, "4. Settings")
            self.tab_widget.addTab(self.log_viewer_tab, "5. Logs")
            # Appended rather than inserted next to "1. Data Sources" so the
            # numbering the rest of the application (and its documentation)
            # refers to stays stable.
            self.tab_widget.addTab(self.source_manager_tab, "6. Source Manager")
            
            logger.info("All tabs created successfully")

            # Only now does settings_tab exist. Applying the saved theme before
            # the tabs were built made _apply_saved_theme() hit its
            # hasattr(self, 'settings_tab') guard every single time, so the
            # theme the user had chosen was silently never restored at start-up.
            self._apply_saved_theme()
            
        except Exception as e:
            logger.exception("Error creating tabs")
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Failed to create application tabs: {str(e)}"
            )
            raise
        
        # Resolve the current year in the background. Loading a source
        # calculates enrollment years and needs it; doing the lookup at that
        # moment would block the window, so it is warmed up here instead.
        self._start_year_warmup()

        logger.debug("UI initialization complete")

    def _start_year_warmup(self):
        """Resolve the current year in a worker thread, without any UI."""
        try:
            from utils.enrollment_service import is_year_cached
            from utils.enrollment_task import YearWarmupTask

            if is_year_cached():
                logger.debug("Year already resolved - no warm-up needed")
                return

            self._year_warmup = YearWarmupTask()
            self._year_warmup.task_finished.connect(
                lambda success, _end, message: logger.info(
                    "Year warm-up %s: %s", "ok" if success else "failed", message
                )
            )
            self._year_warmup.start()
        except Exception:
            logger.exception("Could not start the year warm-up")
        
    def _apply_saved_theme(self):
        """Apply the theme that was saved in SettingsManager."""
        try:
            from utils.settings_manager import get_settings
            settings = get_settings()
            theme_name = settings.get_str("theme", "Dark", category="general")
            # Delegate to settings_tab if it exists and knows the theme
            if hasattr(self, 'settings_tab') and hasattr(self.settings_tab, '_apply_theme'):
                if theme_name in self.settings_tab.THEMES:
                    self.settings_tab._apply_theme(theme_name)
        except Exception as e:
            logger.warning(f"Could not apply saved theme: {e}")

    def apply_modern_style(self):
        """Apply modern dark theme to the application"""
        # Use Fusion style for modern look
        QApplication.setStyle(QStyleFactory.create('Fusion'))
        
        # Dark palette
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(25, 25, 25))
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
        dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
        
        # Apply palette (optional - can be enabled/disabled)
        # QApplication.setPalette(dark_palette)
        
    def closeEvent(self, event):
        """Handle application close event"""
        logger.info("=" * 80)
        logger.info("Closing Student Management System")
        logger.info("=" * 80)

        # Qt delivers the close event to the window only, never to the widgets
        # inside it, so the tabs never got the chance to clean up after
        # themselves: LogViewerTab kept its RealTimeLogHandler on the root
        # logger, its 5 s refresh timer running and its loader/filter threads
        # unjoined.  Closing every page runs that page's own closeEvent.
        #
        # The event stops there: a page's *children* (the AD management widget
        # inside the Operations tab, for instance) still get no close event, so
        # their own cleanup is a separate problem.  The guard below only catches
        # a page that is already gone - PyQt aborts the process on an exception
        # raised inside a closeEvent, so that cannot be caught from here.
        try:
            for index in range(self.tab_widget.count()):
                tab = self.tab_widget.widget(index)
                try:
                    tab.close()
                except Exception as e:
                    logger.warning(f"Error closing tab {index}: {e}")
        except Exception as e:
            logger.warning(f"Error closing application tabs: {e}")

        # Stop the year warm-up if it is still running
        warmup = getattr(self, "_year_warmup", None)
        if warmup is not None and warmup.isRunning():
            warmup.request_cancel()
            if not warmup.wait(2000):
                logger.warning("Year warm-up did not stop in time")

        # Cleanup font manager temporary files
        try:
            font_manager = get_font_manager()
            font_manager.cleanup()
            logger.debug("Font manager cleanup complete")
        except Exception as e:
            logger.warning(f"Error during font manager cleanup: {e}")
        
        event.accept()


def main():
    
    """Main entry point"""
    try:
        logger.info("Starting application")
        
        app = QApplication(sys.argv)
        app.setApplicationName("Student Management System")
        
        # Initialize font system AFTER creating QApplication
        logger.info("Initializing font system...")
        
        if RESOURCES_LOADED:
            initialize_fonts(load_custom_fonts=True)
            font_manager = get_font_manager()
            available_fonts = font_manager.get_available_families()
            logger.info(f"Font system initialized: {len(available_fonts)} fonts available")
            
            # Log custom fonts
            custom_fonts = font_manager.get_custom_fonts()
            if custom_fonts:
                logger.info(f"Custom fonts loaded: {', '.join(custom_fonts)}")
            else:
                logger.warning("No custom fonts loaded - check resources")
        else:
            # Resources not loaded - use only built-in fonts
            logger.warning("Resources not loaded - initializing with built-in fonts only")
            initialize_fonts(load_custom_fonts=False)
            font_manager = get_font_manager()
            logger.info(f"Using built-in fonts only: {', '.join(font_manager.get_available_families())}")
        
        logger.info("Creating main window")
        window = StudentManagementSystem()
        window.show()
        
        logger.info("Application ready - entering event loop")
        
        exit_code = app.exec()
        
        logger.info(f"Application exiting with code: {exit_code}")
        sys.exit(exit_code)
        
    except Exception as e:
        logger.exception("Fatal error in main application")
        try:
            QMessageBox.critical(None, "Fatal Error", f"Application crashed: {str(e)}")
        except Exception:
            # A bare "except:" here also swallowed KeyboardInterrupt/SystemExit
            print(f"FATAL ERROR: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
