"""
Active Directory data source implementation
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QProgressDialog, QComboBox, QInputDialog
)
from PyQt6.QtCore import Qt

from models import Source, Class, Person
from utils.source_naming import suggest_ad_source_name

logger = logging.getLogger(__name__)

# Canonical spelling of the organizational units that hold a class.
CLASS_OU_PREFIX = 'Trida-'


def _attribute_text(entry, name: str) -> str:
    """
    Read one LDAP attribute of an ldap3 entry as text.

    ldap3 exposes every *requested* attribute on the entry even when the
    directory returned no value for it - ``entry.givenName.value`` is then
    ``None``.  Wrapping that in ``str()`` produced the literal string
    ``'None'``, which looks like a real name and kept nameless accounts.
    A value the directory did not return must read as an empty string.
    """
    try:
        attribute = entry[name]
    except (KeyError, AttributeError):
        # The attribute was not requested / does not exist on this entry.
        return ''

    value = attribute.value
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return ''
    return str(value)


class ActiveDirectorySourceWidget(QWidget):
    """Widget for loading data from Active Directory"""
    
    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Instructions
        info = QLabel(
            "Load student data from Windows Active Directory. "
            "The system will search for organizational units named 'Trida-*' "
            "and load users from them."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-style: italic;")
        layout.addWidget(info)
        
        # Server
        server_layout = QHBoxLayout()
        server_layout.addWidget(QLabel("Server:"))
        self.server_input = QLineEdit()
        self.server_input.setPlaceholderText("ldap://dc.example.com")
        server_layout.addWidget(self.server_input)
        layout.addLayout(server_layout)
        
        # Base DN
        base_layout = QHBoxLayout()
        base_layout.addWidget(QLabel("Base DN:"))
        self.base_dn_input = QLineEdit()
        self.base_dn_input.setPlaceholderText("DC=example,DC=com")
        base_layout.addWidget(self.base_dn_input)
        layout.addLayout(base_layout)
        
        # Search format ComboBox
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Search Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Trida-6X (uppercase X)", "uppercase")
        self.format_combo.addItem("Trida-6x (lowercase x)", "lowercase")
        self.format_combo.addItem("Both (Trida-6X and Trida-6x)", "both")
        self.format_combo.setToolTip(
            "Choose the format for class organizational units:\n"
            "• Uppercase: Trida-6A, Trida-7B\n"
            "• Lowercase: Trida-6a, Trida-7b\n"
            "• Both: Search for both formats"
        )
        format_layout.addWidget(self.format_combo)
        layout.addLayout(format_layout)
        
        # Username
        user_layout = QHBoxLayout()
        user_layout.addWidget(QLabel("Username:"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("domain\\username")
        user_layout.addWidget(self.username_input)
        layout.addLayout(user_layout)
        
        # Password
        pass_layout = QHBoxLayout()
        pass_layout.addWidget(QLabel("Password:"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("password")
        pass_layout.addWidget(self.password_input)
        layout.addLayout(pass_layout)
        
        # Load button
        load_layout = QHBoxLayout()
        self.load_btn = QPushButton("Load from Active Directory")
        self.load_btn.clicked.connect(self.on_load)
        load_layout.addWidget(self.load_btn)
        load_layout.addStretch()
        layout.addLayout(load_layout)
        
        layout.addStretch()
        
    def _ou_search_filter(self) -> str:
        """
        Build the LDAP filter for the class units in the selected spelling.

        The 'Search Format' combo box was never read - every run searched for
        'Trida-*' regardless of the choice.  The selected format now decides
        how the prefix is spelled in the filter, and 'Both' asks for either
        spelling explicitly.
        """
        search_format = self.format_combo.currentData()
        uppercase = f'(ou={CLASS_OU_PREFIX}*)'
        lowercase = f'(ou={CLASS_OU_PREFIX.lower()}*)'

        if search_format == 'lowercase':
            return lowercase
        if search_format == 'both':
            return f'(|{uppercase}{lowercase})'
        return uppercase

    def _ask_for_source_name(self, source) -> bool:
        """
        Let the user confirm or change the name of a freshly loaded source.

        The suggested name is already unique, so simply pressing OK always
        works.  A name that is taken is not silently accepted: a second source
        with the same name is unreachable through every lookup by name, so the
        user is asked whether the existing one should be replaced.

        Args:
            source: The source to name.  Renamed in place on success, and the
                replaced source (if any) is removed from the manager.

        Returns:
            ``True`` when the source is named and may be added, ``False`` when
            the user cancelled the whole load.
        """
        default_name = source.name
        replaced_source = None

        while True:
            name, ok = QInputDialog.getText(
                self,
                "Name Source",
                "Enter a name for this Active Directory source:",
                QLineEdit.EchoMode.Normal,
                default_name
            )

            if not ok:
                return False

            name = (name or "").strip()
            if not name:
                QMessageBox.warning(self, "Invalid Name",
                                    "Source name cannot be empty")
                continue

            existing = self.source_manager.get_source_by_name(name)
            if existing is not None:
                reply = QMessageBox.question(
                    self,
                    "Duplicate Name",
                    f"Source '{name}' already exists. Replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply == QMessageBox.StandardButton.No:
                    default_name = name
                    continue
                replaced_source = existing

            source.name = name
            break

        if replaced_source is not None:
            self.source_manager.remove_source(replaced_source)
            logger.info("Replaced existing source: %s", source.name)

        return True

    def on_load(self):
        """Load data from Active Directory"""
        server = self.server_input.text().strip()
        base_dn = self.base_dn_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()
        
        if not all([server, base_dn, username, password]):
            QMessageBox.warning(self, "Missing Information", 
                              "Please fill in all fields")
            return
        
        # The progress dialog only exists once the ldap3 import succeeded;
        # the error handlers below must not assume it was created.
        progress = None
        
        try:
            from ldap3 import Server, Connection, ALL, SUBTREE
            
            progress = QProgressDialog("Connecting to Active Directory...", "Cancel", 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.show()
            
            # Connect to AD
            server_obj = Server(server, get_info=ALL)
            conn = Connection(server_obj, user=username, password=password, auto_bind=True)
            
            progress.setLabelText("Searching for class organizational units...")
            
            # Search for class OUs (Trida-6X pattern).
            #
            # The name is a readable default built from the directory and the
            # branch that is being loaded ('AD school.local - Students')
            # instead of the raw connection URL, and it is already free - two
            # loads of the same directory used to produce two sources with
            # identical names, and every lookup by name then found only the
            # first of them.  The user confirms or changes it once the data is
            # actually here (see below).
            source = Source(
                name=suggest_ad_source_name(
                    server, base_dn, self.source_manager.get_source_names()),
                source_type="active_directory",
                readonly=True
            )
            
            # Search for OUs matching pattern Trida-*
            conn.search(
                search_base=base_dn,
                search_filter=self._ou_search_filter(),
                search_scope=SUBTREE,
                attributes=['ou', 'distinguishedName']
            )
            
            class_ous = []
            for entry in conn.entries:
                ou_name = _attribute_text(entry, 'ou')
                # LDAP compares 'ou' case-insensitively, so the directory also
                # returns units spelled 'trida-7b'.  A case-sensitive
                # startswith() silently dropped exactly those units.
                if ou_name and ou_name.lower().startswith(CLASS_OU_PREFIX.lower()):
                    class_ous.append({
                        'name': ou_name,
                        'dn': _attribute_text(entry, 'distinguishedName') or entry.entry_dn
                    })
            
            # Load users from each class OU
            for ou_info in class_ous:
                progress.setLabelText(f"Loading students from {ou_info['name']}...")
                
                # Strip the prefix in the spelling the directory actually used
                # ('Trida-6A' -> '6A', 'trida-7b' -> '7b').
                class_name = ou_info['name'][len(CLASS_OU_PREFIX):]
                cls = Class(name=class_name)
                
                # Search for users in this OU
                conn.search(
                    search_base=ou_info['dn'],
                    search_filter='(objectClass=user)',
                    search_scope=SUBTREE,
                    # 'distinguishedName' has to be requested explicitly - it was
                    # read from the entry below without ever being asked for.
                    attributes=['givenName', 'sn', 'displayName', 'sAMAccountName',
                                'mail', 'distinguishedName']
                )
                
                for entry in conn.entries:
                    first_name = _attribute_text(entry, 'givenName')
                    last_name = _attribute_text(entry, 'sn')
                    
                    if not first_name or not last_name:
                        continue
                    
                    person = Person(
                        first_name=first_name,
                        last_name=last_name,
                        class_name=class_name,
                        ad_username=_attribute_text(entry, 'sAMAccountName') or None,
                        ad_display_name=_attribute_text(entry, 'displayName') or None,
                        ad_email=_attribute_text(entry, 'mail') or None,
                    )
                    person.metadata['ad_dn'] = (_attribute_text(entry, 'distinguishedName')
                                                or entry.entry_dn)
                    cls.add_person(person)
                
                source.add_class(cls)
            
            # Disconnect
            conn.unbind()
            progress.close()
            
            if not source.classes:
                QMessageBox.warning(
                    self,
                    "No Data",
                    "No class organizational units found matching 'Trida-*' pattern"
                )
                return
            
            # Let the user confirm or change the name, the same way the
            # EduPage and file loaders do.  Cancelling discards the load.
            if not self._ask_for_source_name(source):
                logger.info("Active Directory load cancelled at the naming step")
                return

            self.source_manager.add_source(source)
            QMessageBox.information(
                self,
                "Success",
                f"Successfully loaded {len(source.get_all_persons())} students "
                f"from {len(source.classes)} classes into '{source.name}'"
            )
            logger.info(f"Successfully loaded AD source: {source.name}")
            
        except ImportError:
            # The import is the first statement of the try block, so the progress
            # dialog usually does not exist yet - closing it unconditionally
            # raised NameError instead of telling the user about ldap3.
            if progress is not None:
                progress.close()
            QMessageBox.critical(self, "Error", 
                               "ldap3 package is not installed")
        except Exception as e:
            if progress is not None:
                progress.close()
            logger.exception("Error loading from Active Directory")
            QMessageBox.critical(self, "Error", f"Failed to connect: {str(e)}")
