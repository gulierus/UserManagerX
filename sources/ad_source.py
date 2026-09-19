"""
Active Directory data source implementation
"""

import logging
import re

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QProgressDialog, QComboBox, QInputDialog
)
from PyQt6.QtCore import Qt

from models import Source, Class
from utils.ad_entry_mapping import (
    PERSON_ATTRIBUTES, attribute_text, person_from_entry,
)
from utils.ad_search_scope import (
    DEFAULT_CLASS_OU_PATTERN, OU_PLACEHOLDERS, SearchScope, SearchScopeConfig,
    build_class_ou_filter, class_name_from_ou, class_ou_search_scope,
)
from utils.source_naming import suggest_ad_source_name

logger = logging.getLogger(__name__)

# Canonical spelling of the organizational units that hold a class.
# Shared with the Operations tab so both tabs recognise the same units.
CLASS_OU_PREFIX = DEFAULT_CLASS_OU_PATTERN.rstrip('*')


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
        
        # --- where to search -------------------------------------------
        # The same two controls as the "Active Directory Management"
        # operation on the "3. Operations" tab, built from the same
        # SearchScope definitions, so the two tabs share one vocabulary.
        scope_layout = QHBoxLayout()
        scope_layout.addWidget(QLabel("Search in:"))
        self.scope_combo = QComboBox()
        for scope in SearchScope:
            self.scope_combo.addItem(scope.label, scope)
        self.scope_combo.setToolTip(
            "Where the class organisational units are looked for.\n"
            "The default walks the whole subtree below the Base DN."
        )
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_layout.addWidget(self.scope_combo, stretch=1)
        layout.addLayout(scope_layout)

        ou_layout = QHBoxLayout()
        ou_layout.addWidget(QLabel("Organisational units:"))
        self.ou_input = QLineEdit()
        self.ou_input.setEnabled(False)
        self.ou_input.setPlaceholderText(
            "Trida-{class_name}, Rocnik-{grade}, {enrollment_year}"
        )
        self.ou_input.setToolTip(
            "One or more OU names, separated by a comma or a semicolon.\n"
            "A placeholder stands for 'whatever this class is called', so\n"
            "'Trida-{class_name}' loads every Trida-... unit and 'Zaci'\n"
            "loads exactly that one."
        )
        ou_layout.addWidget(self.ou_input, stretch=1)
        layout.addLayout(ou_layout)

        self.ou_hint = QLabel(
            "Placeholders: " + " · ".join(
                f"<code>{{{name}}}</code> {description}"
                for name, description in OU_PLACEHOLDERS.items()
            )
        )
        self.ou_hint.setWordWrap(True)
        self.ou_hint.setStyleSheet("color: #888; font-size: 9px;")
        self.ou_hint.setVisible(False)
        layout.addWidget(self.ou_hint)
        
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
        
    def _on_scope_changed(self, _index: int) -> None:
        """Only the "named OUs" scope needs the OU list."""
        named = self.scope_combo.currentData() is SearchScope.NAMED_OUS
        self.ou_input.setEnabled(named)
        self.ou_hint.setVisible(named)

    def get_search_scope(self) -> SearchScopeConfig:
        """
        Build the search scope from the two controls above.

        Mirrors ``ADManagementWidget.get_search_scope()`` - the same fields,
        the same separators, the same resulting object.

        Returns:
            The configured :class:`~utils.ad_search_scope.SearchScopeConfig`.
        """
        scope = self.scope_combo.currentData() or SearchScope.SUBTREE
        raw = self.ou_input.text()
        templates = [part.strip() for part in re.split(r'[;,]', raw) if part.strip()]
        return SearchScopeConfig(scope=scope, ou_templates=templates)

    def _ou_search_filter(self) -> str:
        """
        Build the LDAP filter that finds the class organisational units.

        Delegates to the shared search-scope module, so this tab and the
        Operations tab cannot drift apart.
        """
        return build_class_ou_filter(self.get_search_scope())

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
        
        scope_config = self.get_search_scope()
        problems = scope_config.problems()
        if problems:
            QMessageBox.warning(self, "Search Scope",
                                "\n".join(problems))
            return

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
            
            # Find the class organisational units.  Which units count and how
            # deep the search goes both come from the selected scope.
            logger.info("Searching for class units: %s", scope_config.describe())
            conn.search(
                search_base=base_dn,
                search_filter=self._ou_search_filter(),
                search_scope=class_ou_search_scope(scope_config),
                attributes=['ou', 'distinguishedName']
            )

            class_ous = []
            seen_dns = set()
            for entry in conn.entries:
                ou_name = attribute_text(entry, 'ou')
                if not ou_name:
                    continue
                dn = attribute_text(entry, 'distinguishedName') or entry.entry_dn
                # Two templates can match the same unit ('Trida-{class_name}'
                # and 'Trida-6A'); it must still be loaded only once.
                key = (dn or ou_name).casefold()
                if key in seen_dns:
                    continue
                seen_dns.add(key)
                class_ous.append({'name': ou_name, 'dn': dn})
            
            # Load users from each class OU
            for ou_info in class_ous:
                progress.setLabelText(f"Loading students from {ou_info['name']}...")
                
                # 'Trida-6A' -> '6A', 'trida-7b' -> '7b'.  A unit the user
                # named explicitly and that carries no prefix keeps its name.
                class_name = class_name_from_ou(ou_info['name'], CLASS_OU_PREFIX)
                cls = Class(name=class_name)

                # Search for users in this OU.  ldap3 only fills in attributes
                # that were requested, so the list has to be complete - an
                # attribute left out here reads as empty everywhere in the
                # application, which is exactly why home directories and group
                # memberships used to arrive blank.
                conn.search(
                    search_base=ou_info['dn'],
                    search_filter='(&(objectClass=user)'
                                  '(!(objectClass=computer)))',
                    search_scope=SUBTREE,
                    attributes=PERSON_ATTRIBUTES
                )

                for entry in conn.entries:
                    person = person_from_entry(entry, class_name)
                    if person is None:
                        # No first or last name: a service account or a
                        # computer object, not a pupil.
                        continue
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
