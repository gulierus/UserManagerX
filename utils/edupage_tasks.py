"""
EduPage Progress Tasks
Implements specific tasks for loading data from EduPage with 2FA support
"""

import logging
from typing import Dict, List, Optional
from PyQt6.QtCore import pyqtSignal

from utils.progress_tasks import AbstractProgressTask, LogLevel, TaskCancelledException
from models import Source, Class, Person

logger = logging.getLogger(__name__)


class EduPageBaseTask(AbstractProgressTask):
    """
    Base class for all EduPage operations.
    Provides common functionality for EduPage API interactions with proper 2FA support.
    """
    
    # Additional signals specific to EduPage
    twofa_code_needed = pyqtSignal()  # Request 2FA code from user
    login_successful = pyqtSignal()
    
    def __init__(self, username: str, password: str, subdomain: str, 
                 task_name: str, is_deterministic: bool = False):
        """
        Initialize EduPage task.
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
            task_name: Human-readable task name
            is_deterministic: Whether progress is deterministic
        """
        super().__init__(task_name, is_deterministic, can_pause=False)
        
        self.username = username
        self.password = password  
        self.subdomain = subdomain
        
        self.edupage = None
        self.twofa_login = None  # Store TwoFactorLogin object
        self.twofa_code: Optional[str] = None
        
    def _login_with_2fa(self) -> bool:
        """
        Perform login with proper 2FA support according to EduPage API.
        
        Returns:
            True if login successful, False otherwise
        """
        try:
            from edupage_api import Edupage
            from edupage_api.exceptions import BadCredentialsException, SecondFactorFailedException
            
            self.emit_log("Initializing EduPage API...", LogLevel.INFO)
            self.edupage = Edupage()
            
            self.emit_progress(-1, "Connecting to EduPage...")
            
            # Attempt login - returns None or TwoFactorLogin object
            try:
                self.twofa_login = self.edupage.login(self.username, self.password, self.subdomain)
                
                # Check if 2FA is needed
                if self.twofa_login is not None:
                    self.emit_log("Two-factor authentication required", LogLevel.WARNING)
                    
                    # Request code from user
                    self.emit_progress(-1, "Waiting for 2FA code...")
                    self.twofa_code_needed.emit()
                    
                    # Wait for code to be set (blocking)
                    while self.twofa_code is None and not self.is_cancelled():
                        self.msleep(100)

                    # Cancelling here is a user decision, not a login failure:
                    # raise the dedicated exception so run() reports the task as
                    # cancelled instead of failed.
                    self.check_cancelled()
                    
                    # Complete 2FA login using finish_with_code
                    try:
                        self.emit_progress(-1, "Verifying 2FA code...")
                        self.twofa_login.finish_with_code(self.twofa_code)
                        
                        if self.edupage.is_logged_in:
                            self.emit_log("2FA verification successful", LogLevel.SUCCESS)
                            self.login_successful.emit()
                            return True
                        else:
                            self.emit_log("2FA verification failed", LogLevel.ERROR)
                            return False
                            
                    except SecondFactorFailedException:
                        self.emit_log("Invalid 2FA code", LogLevel.ERROR)
                        return False
                    except Exception as e:
                        self.emit_log(f"2FA verification error: {str(e)}", LogLevel.ERROR)
                        return False
                else:
                    # No 2FA needed - check if logged in
                    if self.edupage.is_logged_in:
                        self.emit_log("Login successful (no 2FA required)", LogLevel.SUCCESS)
                        self.login_successful.emit()
                        return True
                    else:
                        self.emit_log("Login failed", LogLevel.ERROR)
                        return False
                        
            except BadCredentialsException:
                self.emit_log("Invalid username or password", LogLevel.ERROR)
                return False
            
        except ImportError:
            self.emit_log("edupage-api package not installed", LogLevel.ERROR)
            return False
            
        except TaskCancelledException:
            # Cancelling the 2FA dialog is a user decision, not a login failure.
            # The catch-all below used to swallow it, log it as "Login error" and
            # return False; let it travel up so run() reports the task as
            # cancelled without an error in the log.
            raise
            
        except Exception as e:
            self.emit_log(f"Login error: {str(e)}", LogLevel.ERROR)
            logger.exception("EduPage login error")
            return False
    
    def _logout(self):
        """Perform logout from EduPage. Note: logout() will be added to API."""
        if self.edupage:
            try:
                self.emit_log("Logging out from EduPage...", LogLevel.INFO)
                # Note: logout() method will be added to EduPage API
                if hasattr(self.edupage, 'logout'):
                    self.edupage.logout()
                    self.emit_log("Logged out successfully", LogLevel.SUCCESS)
                else:
                    self.emit_log("Logout method not available (session will expire)", LogLevel.INFO)
            except Exception as e:
                self.emit_log(f"Logout error (non-critical): {str(e)}", LogLevel.WARNING)
                logger.warning(f"EduPage logout error: {e}")
    
    def set_2fa_code(self, code: str):
        """Set 2FA code provided by user."""
        self.twofa_code = code
        logger.debug(f"2FA code set for task: {self.task_name}")
    
    def cleanup(self):
        """Cleanup - ensure logout."""
        self._logout()


class EduPageMode1SinglePhaseTask(EduPageBaseTask):
    """
    Mode 1 - Single Phase: Load everything in one go with intermediate class selection.
    
    Workflow (all in one task):
    1. Login with 2FA support
    2. Fetch all classes
    3. Pause and wait for user to select classes (via callback)
    4. Fetch all students from school
    5. Filter students by selected class_ids
    6. Fetch full names for each student
    7. Create Source
    8. Logout
    """
    
    def __init__(self, username: str, password: str, subdomain: str):
        """
        Initialize Mode 1 single-phase task.
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
        """
        super().__init__(
            username, password, subdomain,
            task_name="EduPage: Mode 1 - Single Phase Load",
            is_deterministic=True
        )
        
        self.all_classes = []
        self.selected_class_names: Optional[List[str]] = None
        self.result_data: Dict[str, List[Person]] = {}
    
    # Signal to request class selection from user
    class_selection_needed = pyqtSignal(list)  # Emits list of class names
    
    def set_selected_classes(self, class_names: List[str]):
        """Set selected class names from user."""
        self.selected_class_names = class_names
        logger.info(f"User selected {len(class_names)} classes")
    
    def execute(self) -> str:
        """Execute single-phase load."""
        # Step 1: Login (15%)
        self.emit_progress(0, "Logging in to EduPage...")
        if not self._login_with_2fa():
            self.check_cancelled()
            raise Exception("Login failed")
        
        self.check_cancelled()
        self.emit_progress(15, "Login successful")
        
        # Step 2: Fetch all classes (10%)
        self.emit_progress(15, "Loading class list...")
        classes = self.edupage.get_classes()
        
        if not classes:
            raise Exception("No classes found")
        
        self.all_classes = classes
        class_names = [cls.name for cls in classes]
        
        self.emit_log(f"Found {len(classes)} classes", LogLevel.SUCCESS)
        self.check_cancelled()
        self.emit_progress(25, f"Found {len(classes)} classes")
        
        # Step 3: Request class selection from user (wait for callback)
        self.emit_log("Waiting for class selection...", LogLevel.INFO)
        self.emit_progress(25, "Please select classes to load...")
        self.class_selection_needed.emit(class_names)
        
        # Wait for selection (blocking)
        while self.selected_class_names is None and not self.is_cancelled():
            self.msleep(100)

        # The user closed the class-selection dialog -> this is a cancellation,
        # not an error. TaskCancelledException makes run() emit task_cancelled
        # so the UI shows "Cancelled" instead of "Failed".
        self.check_cancelled()

        if not self.selected_class_names:
            raise Exception("No classes selected")
        
        self.emit_log(f"Loading {len(self.selected_class_names)} selected classes", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(30, f"Loading students from {len(self.selected_class_names)} classes")
        
        # Step 4: Filter to selected classes
        selected_classes = [cls for cls in classes if cls.name in self.selected_class_names]
        selected_class_ids = [cls.class_id for cls in selected_classes]
        
        # Step 5: Fetch all students (15%)
        self.emit_progress(30, "Fetching all students from school...")
        all_students = self.edupage.get_all_students()
        
        if not all_students:
            raise Exception("No students found")
        
        self.emit_log(f"Found {len(all_students)} students in total", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(45, f"Found {len(all_students)} students")
        
        # Step 6: Filter students by selected classes (5%)
        self.emit_progress(45, "Filtering students by selected classes...")
        filtered_students = [
            student for student in all_students 
            if student.class_id in selected_class_ids
        ]
        
        self.emit_log(f"Filtered to {len(filtered_students)} students in selected classes", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(50, f"Processing {len(filtered_students)} students")
        
        # Step 7: Fetch full names and organize by class (40%)
        from edupage_api.dbi import DbiHelper
        dbi_helper = DbiHelper(self.edupage)
        
        # Organize students by class
        students_by_class: Dict[str, List] = {}
        for cls in selected_classes:
            students_by_class[cls.name] = []
        
        for i, student in enumerate(filtered_students):
            self.check_cancelled()
            
            progress = 50 + int((i / len(filtered_students)) * 40)
            self.emit_progress(progress, f"Loading full names ({i+1}/{len(filtered_students)})...")
            
            try:
                # Fetch full name
                full_name = dbi_helper.fetch_student_name(student.person_id)
                
                if not full_name:
                    full_name = student.name_short
                
                # Find class name for this student
                class_name = None
                for cls in selected_classes:
                    if cls.class_id == student.class_id:
                        class_name = cls.name
                        break
                
                if class_name:
                    # Split full name into first and last name
                    name_parts = full_name.split(' ', 1)
                    if len(name_parts) == 2:
                        first_name, last_name = name_parts
                    else:
                        first_name = full_name
                        last_name = ""
                    
                    # Create Person object
                    person = Person(
                        first_name=first_name,
                        last_name=last_name,
                        class_name=class_name
                    )
                    person.metadata['edupage_id'] = student.person_id
                    person.metadata['name_short'] = student.name_short
                    
                    students_by_class[class_name].append(person)
                    
            except Exception as e:
                self.emit_log(f"Warning: Failed to load student {student.name_short}: {e}", LogLevel.WARNING)
                logger.warning(f"Failed to load student {student.person_id}: {e}")
        
        self.result_data = students_by_class
        
        # Log statistics
        for class_name, students in students_by_class.items():
            self.emit_log(f"Loaded {len(students)} students from {class_name}", LogLevel.SUCCESS)
        
        self.check_cancelled()
        self.emit_progress(90, "Finalizing...")
        
        # Step 8: Logout (10%)
        self._logout()
        self.emit_progress(100, "Complete")
        
        total_students = sum(len(students) for students in self.result_data.values())
        return f"Loaded {total_students} students from {len(self.result_data)} classes"
    
    def get_result_source(self, source_name: str) -> Source:
        """Convert loaded data to Source object."""
        source = Source(
            name=source_name,
            source_type="edupage",
            readonly=True
        )
        source.set_source_info('subdomain', self.subdomain)
        
        for class_name, persons in self.result_data.items():
            cls = Class(name=class_name)
            for person in persons:
                cls.add_person(person)
            source.add_class(cls)
        
        return source


class EduPageMode1TwoPhaseTask(EduPageBaseTask):
    """
    Mode 1 - Two Phase: Load class names, let user select, then load students from selected classes.
    
    Workflow:
    1. Login with 2FA support
    2. Fetch all classes
    3. Return class names for selection (handled by coordinator)
    4. Fetch all students from school
    5. Filter students by selected class_ids
    6. Fetch full names for each student
    7. Create Source
    8. Logout
    """
    
    def __init__(self, username: str, password: str, subdomain: str, 
                 selected_class_names: Optional[List[str]] = None):
        """
        Initialize Mode 1 two-phase task.
        
        Args:
            username: EduPage username
            password: EduPage password
            subdomain: School subdomain
            selected_class_names: Optional list of selected class names (for second phase)
        """
        super().__init__(
            username, password, subdomain,
            task_name="EduPage: Mode 1 - Two Phase Load",
            is_deterministic=True
        )
        
        self.selected_class_names = selected_class_names
        self.all_classes = []  # Store all classes
        self.result_data: Dict[str, List[Person]] = {}
    
    def execute(self) -> str:
        """Execute Mode 1 two-phase workflow."""
        # Phase 1: Login and fetch class names
        if self.selected_class_names is None:
            return self._execute_phase1()
        # Phase 2: Load students from selected classes
        else:
            return self._execute_phase2()
    
    def _execute_phase1(self) -> str:
        """Phase 1: Login and fetch class names."""
        # Step 1: Login (40%)
        self.emit_progress(0, "Logging in to EduPage...")
        if not self._login_with_2fa():
            self.check_cancelled()
            raise Exception("Login failed")
        
        self.check_cancelled()
        self.emit_progress(40, "Login successful")
        
        # Step 2: Fetch all classes (40%)
        self.emit_progress(40, "Loading class list...")
        classes = self.edupage.get_classes()
        
        if not classes:
            raise Exception("No classes found")
        
        self.all_classes = classes
        self.emit_log(f"Found {len(classes)} classes", LogLevel.SUCCESS)
        self.check_cancelled()
        self.emit_progress(80, f"Found {len(classes)} classes")
        
        # Step 3: Logout (20%)
        self._logout()
        self.emit_progress(100, "Phase 1 complete - ready for class selection")
        
        return f"Found {len(classes)} classes"
    
    def _execute_phase2(self) -> str:
        """Phase 2: Load students from selected classes."""
        # Step 1: Login again (15%)
        self.emit_progress(0, "Logging in to EduPage...")
        if not self._login_with_2fa():
            self.check_cancelled()
            raise Exception("Login failed")
        
        self.check_cancelled()
        self.emit_progress(15, "Login successful")
        
        # Step 2: Fetch all classes to get class_ids (10%)
        self.emit_progress(15, "Loading class list...")
        classes = self.edupage.get_classes()
        
        if not classes:
            raise Exception("No classes found")
        
        # Filter to selected classes
        selected_classes = [cls for cls in classes if cls.name in self.selected_class_names]
        selected_class_ids = [cls.class_id for cls in selected_classes]
        
        self.emit_log(f"Will load {len(selected_classes)} selected classes", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(25, f"Loading students from {len(selected_classes)} classes")
        
        # Step 3: Fetch all students (20%)
        self.emit_progress(25, "Fetching all students from school...")
        all_students = self.edupage.get_all_students()
        
        if not all_students:
            raise Exception("No students found")
        
        self.emit_log(f"Found {len(all_students)} students in total", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(45, f"Found {len(all_students)} students")
        
        # Step 4: Filter students by selected classes (5%)
        self.emit_progress(45, "Filtering students by selected classes...")
        filtered_students = [
            student for student in all_students 
            if student.class_id in selected_class_ids
        ]
        
        self.emit_log(f"Filtered to {len(filtered_students)} students in selected classes", LogLevel.INFO)
        self.check_cancelled()
        self.emit_progress(50, f"Processing {len(filtered_students)} students")
        
        # Step 5: Fetch full names and organize by class (35%)
        from edupage_api.dbi import DbiHelper
        dbi_helper = DbiHelper(self.edupage)
        
        # Organize students by class
        students_by_class: Dict[str, List] = {}
        for cls in selected_classes:
            students_by_class[cls.name] = []
        
        for i, student in enumerate(filtered_students):
            self.check_cancelled()
            
            progress = 50 + int((i / len(filtered_students)) * 35)
            self.emit_progress(progress, f"Loading full names ({i+1}/{len(filtered_students)})...")
            
            try:
                # Fetch full name
                full_name = dbi_helper.fetch_student_name(student.person_id)
                
                if not full_name:
                    full_name = student.name_short
                
                # Find class name for this student
                class_name = None
                for cls in selected_classes:
                    if cls.class_id == student.class_id:
                        class_name = cls.name
                        break
                
                if class_name:
                    # Split full name into first and last name
                    name_parts = full_name.split(' ', 1)
                    if len(name_parts) == 2:
                        first_name, last_name = name_parts
                    else:
                        first_name = full_name
                        last_name = ""
                    
                    # Create Person object
                    person = Person(
                        first_name=first_name,
                        last_name=last_name,
                        class_name=class_name
                    )
                    person.metadata['edupage_id'] = student.person_id
                    person.metadata['name_short'] = student.name_short
                    
                    students_by_class[class_name].append(person)
                    
            except Exception as e:
                self.emit_log(f"Warning: Failed to load student {student.name_short}: {e}", LogLevel.WARNING)
                logger.warning(f"Failed to load student {student.person_id}: {e}")
        
        self.result_data = students_by_class
        
        # Log statistics
        for class_name, students in students_by_class.items():
            self.emit_log(f"Loaded {len(students)} students from {class_name}", LogLevel.SUCCESS)
        
        self.check_cancelled()
        self.emit_progress(85, "Finalizing...")
        
        # Step 6: Logout (15%)
        self._logout()
        self.emit_progress(100, "Complete")
        
        total_students = sum(len(students) for students in self.result_data.values())
        return f"Loaded {total_students} students from {len(self.result_data)} classes"
    
    def get_class_names(self) -> List[str]:
        """Get list of class names (after Phase 1)."""
        return [cls.name for cls in self.all_classes]
    
    def get_result_source(self, source_name: str) -> Source:
        """
        Convert loaded data to Source object.
        
        Args:
            source_name: Name for the source
            
        Returns:
            Source object with all data
        """
        source = Source(
            name=source_name,
            source_type="edupage",
            readonly=True
        )
        source.set_source_info('subdomain', self.subdomain)
        
        for class_name, persons in self.result_data.items():
            cls = Class(name=class_name)
            for person in persons:
                cls.add_person(person)
            source.add_class(cls)
        
        return source


class EduPageMode2Task(EduPageBaseTask):
    """
    Mode 2: Placeholder for future implementation.
    Will be implemented by user.
    """
    
    def __init__(self, username: str, password: str, subdomain: str):
        super().__init__(
            username, password, subdomain,
            task_name="EduPage: Mode 2 - Not Implemented",
            is_deterministic=False
        )
        
        self.result_data: Dict[str, List[Person]] = {}
    
    def execute(self) -> str:
        """
        Mode 2 is not yet implemented.
        This is a placeholder for user to implement custom logic.
        """
        raise NotImplementedError(
            "Mode 2 is not yet implemented. "
            "Please implement the custom logic in this method."
        )
    
    def get_result_source(self, source_name: str) -> Source:
        """Create Source from loaded data."""
        source = Source(
            name=source_name,
            source_type="edupage",
            readonly=True
        )
        source.set_source_info('subdomain', self.subdomain)
        
        for class_name, persons in self.result_data.items():
            cls = Class(name=class_name)
            for person in persons:
                cls.add_person(person)
            source.add_class(cls)
        
        return source
