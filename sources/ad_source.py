"""
Active Directory data source implementation
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QProgressDialog, QComboBox
)
from PyQt6.QtCore import Qt

from models import Source, Class, Person

logger = logging.getLogger(__name__)


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
        
        try:
            from ldap3 import Server, Connection, ALL, SUBTREE
            
            progress = QProgressDialog("Connecting to Active Directory...", "Cancel", 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.show()
            
            # Connect to AD
            server_obj = Server(server, get_info=ALL)
            conn = Connection(server_obj, user=username, password=password, auto_bind=True)
            
            progress.setLabelText("Searching for class organizational units...")
            
            # Search for class OUs (Trida-6X pattern)
            source = Source(
                name=f"AD-{server}",
                source_type="active_directory",
                readonly=True
            )
            
            # Search for OUs matching pattern Trida-*
            conn.search(
                search_base=base_dn,
                search_filter='(ou=Trida-*)',
                search_scope=SUBTREE,
                attributes=['ou', 'distinguishedName']
            )
            
            class_ous = []
            for entry in conn.entries:
                ou_name = str(entry.ou.value) if hasattr(entry, 'ou') else None
                if ou_name and ou_name.startswith('Trida-'):
                    class_ous.append({
                        'name': ou_name,
                        'dn': str(entry.distinguishedName)
                    })
            
            # Load users from each class OU
            for ou_info in class_ous:
                progress.setLabelText(f"Loading students from {ou_info['name']}...")
                
                class_name = ou_info['name'].replace('Trida-', '')  # e.g., "6A"
                cls = Class(name=class_name)
                
                # Search for users in this OU
                conn.search(
                    search_base=ou_info['dn'],
                    search_filter='(objectClass=user)',
                    search_scope=SUBTREE,
                    attributes=['givenName', 'sn', 'displayName', 'sAMAccountName', 'mail']
                )
                
                for entry in conn.entries:
                    first_name = str(entry.givenName.value) if hasattr(entry, 'givenName') else ''
                    last_name = str(entry.sn.value) if hasattr(entry, 'sn') else ''
                    
                    if not first_name or not last_name:
                        continue
                    
                    person = Person(
                        first_name=first_name,
                        last_name=last_name,
                        class_name=class_name,
                        ad_username=str(entry.sAMAccountName.value) if hasattr(entry, 'sAMAccountName') else None,
                        ad_display_name=str(entry.displayName.value) if hasattr(entry, 'displayName') else None,
                        ad_email=str(entry.mail.value) if hasattr(entry, 'mail') else None,
                    )
                    person.metadata['ad_dn'] = str(entry.distinguishedName)
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
            
            self.source_manager.add_source(source)
            QMessageBox.information(
                self,
                "Success",
                f"Successfully loaded {len(source.get_all_persons())} students "
                f"from {len(source.classes)} classes"
            )
            logger.info(f"Successfully loaded AD source: {source.name}")
            
        except ImportError:
            progress.close()
            QMessageBox.critical(self, "Error", 
                               "ldap3 package is not installed")
        except Exception as e:
            if 'progress' in locals():
                progress.close()
            logger.exception("Error loading from Active Directory")
            QMessageBox.critical(self, "Error", f"Failed to connect: {str(e)}")
