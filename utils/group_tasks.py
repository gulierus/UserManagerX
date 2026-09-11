"""
Progress Tasks for Group Management Operations
Background tasks for group discovery, export/import operations
"""

import logging
import json
from typing import List, Dict, Any, Optional
from datetime import datetime

from utils.progress_tasks import AbstractProgressTask, LogLevel
from models import ADGroup, GroupTemplate
from services.ad_client import ADClient
from services.ad_group_service import ADGroupService
from utils.encryption import encrypt_file_with_format, decrypt_file_with_format

logger = logging.getLogger(__name__)


class GroupDiscoveryTask(AbstractProgressTask):
    """
    Task for discovering groups in Active Directory
    
    Discovers all groups in a specified OU and reports progress
    """
    
    def __init__(self, server: str, username: str, password: str, 
                 ou_dn: str, task_name: str = "Group Discovery"):
        """
        Initialize group discovery task
        
        Args:
            server: LDAP server URL
            username: AD admin username
            password: AD admin password
            ou_dn: Distinguished Name of OU to search
            task_name: Custom task name
        """
        super().__init__(task_name, is_deterministic=False, can_pause=False)
        self.server = server
        self.username = username
        self.password = password
        self.ou_dn = ou_dn
        self.discovered_groups: List[ADGroup] = []
        self.errors: List[str] = []
    
    def execute(self) -> str:
        """
        Execute group discovery
        
        Returns:
            Result message
            
        Raises:
            Exception: If discovery fails
        """
        self.emit_log(f"Starting group discovery in: {self.ou_dn}", LogLevel.INFO)
        self.emit_progress(-1, "Connecting to Active Directory...")
        
        try:
            # Connect to AD
            with ADClient(self.server, self.username, self.password) as client:
                self.check_cancelled()
                
                if not client.connection:
                    raise RuntimeError("Failed to connect to Active Directory")
                
                self.emit_log("Connected to AD successfully", LogLevel.SUCCESS)
                self.emit_progress(-1, "Searching for groups...")
                
                # Create group service
                group_service = ADGroupService(client)
                
                # Discover groups
                self.emit_log(f"Searching OU: {self.ou_dn}", LogLevel.INFO)
                result = group_service.discover_groups_in_ou(self.ou_dn)
                
                self.check_cancelled()
                
                # Store results
                self.discovered_groups = result.groups
                self.errors = result.errors
                
                # Log results
                self.emit_log(f"Found {len(self.discovered_groups)} groups", LogLevel.SUCCESS)
                
                if self.errors:
                    for error in self.errors:
                        self.emit_log(f"Warning: {error}", LogLevel.WARNING)
                
                # Log group details
                for group in self.discovered_groups:
                    self.emit_log(
                        f"  - {group.name} ({group.group_type}, {group.members_count} members)",
                        LogLevel.INFO
                    )
                
                return f"Discovered {len(self.discovered_groups)} groups"
        
        except Exception as e:
            self.emit_log(f"Discovery failed: {str(e)}", LogLevel.ERROR)
            raise
    
    def cleanup(self):
        """Cleanup resources"""
        pass
    
    def get_discovered_groups(self) -> List[ADGroup]:
        """Get list of discovered groups"""
        return self.discovered_groups
    
    def get_errors(self) -> List[str]:
        """Get list of errors encountered"""
        return self.errors


class GroupExportTask(AbstractProgressTask):
    """
    Task for exporting groups to encrypted file
    
    Exports groups with encryption and USRX format
    """
    
    def __init__(self, groups: List[ADGroup], output_path: str, 
                 password: str, encryption_method: str,
                 task_name: str = "Export Groups"):
        """
        Initialize export task
        
        Args:
            groups: List of ADGroup objects to export
            output_path: Path to output file
            password: Encryption password
            encryption_method: 'aes-gcm' or 'gpg'
            task_name: Custom task name
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.groups = groups
        self.output_path = output_path
        self.password = password
        self.encryption_method = encryption_method
    
    def execute(self) -> str:
        """
        Execute export operation
        
        Returns:
            Result message
        """
        total_groups = len(self.groups)
        
        self.emit_log(f"Exporting {total_groups} groups", LogLevel.INFO)
        self.emit_progress(10, "Preparing data...")
        
        try:
            # Convert groups to dictionary format
            groups_data = []
            
            for idx, group in enumerate(self.groups):
                self.check_cancelled()
                
                group_dict = {
                    'name': group.name,
                    'dn': group.dn,
                    'description': group.description,
                    'group_type': group.group_type,
                    'members_count': group.members_count,
                    'metadata': group.metadata
                }
                groups_data.append(group_dict)
                
                progress = 10 + int((idx / total_groups) * 30)
                self.emit_progress(progress, f"Processing group {idx + 1}/{total_groups}")
            
            # Create export data structure
            export_data = {
                'version': '1.0',
                'export_type': 'groups',
                'export_date': datetime.now().isoformat(),
                'total_groups': total_groups,
                'groups': groups_data
            }
            
            self.emit_progress(50, "Serializing data...")
            
            # Convert to JSON
            json_data = json.dumps(export_data, indent=2, ensure_ascii=False)
            
            self.emit_progress(60, "Encrypting data...")
            self.emit_log(f"Using {self.encryption_method} encryption", LogLevel.INFO)
            
            # Encrypt and save with USRX format
            metadata = {
                'content_type': 'ad_groups',
                'total_groups': total_groups,
                'export_date': datetime.now().isoformat()
            }
            
            encrypt_file_with_format(
                data=json_data,
                output_path=self.output_path,
                password=self.password,
                encryption_method=self.encryption_method,
                file_format='usrx',
                metadata=metadata
            )
            
            self.emit_progress(100, "Export complete")
            self.emit_log(f"Groups exported to: {self.output_path}", LogLevel.SUCCESS)
            
            return f"Exported {total_groups} groups successfully"
        
        except Exception as e:
            self.emit_log(f"Export failed: {str(e)}", LogLevel.ERROR)
            raise
    
    def cleanup(self):
        """Cleanup resources"""
        pass


class GroupImportTask(AbstractProgressTask):
    """
    Task for importing groups from encrypted file
    
    Imports groups with decryption and validation
    """
    
    def __init__(self, input_path: str, password: str,
                 encryption_method: Optional[str] = None,
                 task_name: str = "Import Groups"):
        """
        Initialize import task
        
        Args:
            input_path: Path to input file
            password: Decryption password
            encryption_method: 'aes-gcm' or 'gpg' (auto-detect if None)
            task_name: Custom task name
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.input_path = input_path
        self.password = password
        self.encryption_method = encryption_method
        self.imported_groups: List[ADGroup] = []
    
    def execute(self) -> str:
        """
        Execute import operation
        
        Returns:
            Result message
        """
        self.emit_log(f"Importing groups from: {self.input_path}", LogLevel.INFO)
        self.emit_progress(10, "Reading file...")
        
        try:
            # Decrypt file
            self.emit_progress(20, "Decrypting data...")
            
            json_data = decrypt_file_with_format(
                input_path=self.input_path,
                password=self.password,
                encryption_method=self.encryption_method
            )
            
            self.emit_progress(50, "Parsing data...")
            
            # Parse JSON
            data = json.loads(json_data)
            
            # Validate structure
            if data.get('export_type') != 'groups':
                raise ValueError("Invalid file format - not a groups export file")
            
            groups_data = data.get('groups', [])
            total_groups = len(groups_data)
            
            self.emit_log(f"Found {total_groups} groups in file", LogLevel.INFO)
            self.emit_progress(60, "Converting groups...")
            
            # Convert to ADGroup objects
            for idx, group_dict in enumerate(groups_data):
                self.check_cancelled()
                
                try:
                    group = ADGroup(
                        name=group_dict['name'],
                        dn=group_dict['dn'],
                        description=group_dict.get('description'),
                        group_type=group_dict.get('group_type'),
                        members_count=group_dict.get('members_count', 0),
                        metadata=group_dict.get('metadata', {})
                    )
                    self.imported_groups.append(group)
                    
                except Exception as e:
                    self.emit_log(
                        f"Warning: Failed to import group '{group_dict.get('name')}': {e}",
                        LogLevel.WARNING
                    )
                
                progress = 60 + int((idx / total_groups) * 30)
                self.emit_progress(progress, f"Processing group {idx + 1}/{total_groups}")
            
            self.emit_progress(100, "Import complete")
            self.emit_log(
                f"Successfully imported {len(self.imported_groups)} groups",
                LogLevel.SUCCESS
            )
            
            return f"Imported {len(self.imported_groups)} groups successfully"
        
        except Exception as e:
            self.emit_log(f"Import failed: {str(e)}", LogLevel.ERROR)
            raise
    
    def cleanup(self):
        """Cleanup resources"""
        pass
    
    def get_imported_groups(self) -> List[ADGroup]:
        """Get list of imported groups"""
        return self.imported_groups


class TemplateExportTask(AbstractProgressTask):
    """
    Task for exporting group templates to encrypted file
    """
    
    def __init__(self, templates: List[GroupTemplate], output_path: str,
                 password: str, encryption_method: str,
                 task_name: str = "Export Templates"):
        """
        Initialize template export task
        
        Args:
            templates: List of GroupTemplate objects to export
            output_path: Path to output file
            password: Encryption password
            encryption_method: 'aes-gcm' or 'gpg'
            task_name: Custom task name
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.templates = templates
        self.output_path = output_path
        self.password = password
        self.encryption_method = encryption_method
    
    def execute(self) -> str:
        """Execute template export"""
        total_templates = len(self.templates)
        
        self.emit_log(f"Exporting {total_templates} templates", LogLevel.INFO)
        self.emit_progress(10, "Preparing data...")
        
        try:
            # Convert templates to dictionary format
            templates_data = []
            
            for idx, template in enumerate(self.templates):
                self.check_cancelled()
                
                # Convert groups in template
                groups_data = []
                for group in template.groups:
                    group_dict = {
                        'name': group.name,
                        'dn': group.dn,
                        'description': group.description,
                        'group_type': group.group_type,
                        'members_count': group.members_count,
                        'metadata': group.metadata
                    }
                    groups_data.append(group_dict)
                
                template_dict = {
                    'name': template.name,
                    'description': template.description,
                    'groups': groups_data,
                    'metadata': template.metadata,
                    'created_date': template.created_date.isoformat() if template.created_date else None,
                    'modified_date': template.modified_date.isoformat() if template.modified_date else None
                }
                templates_data.append(template_dict)
                
                progress = 10 + int((idx / total_templates) * 30)
                self.emit_progress(progress, f"Processing template {idx + 1}/{total_templates}")
            
            # Create export data structure
            export_data = {
                'version': '1.0',
                'export_type': 'group_templates',
                'export_date': datetime.now().isoformat(),
                'total_templates': total_templates,
                'templates': templates_data
            }
            
            self.emit_progress(50, "Serializing data...")
            json_data = json.dumps(export_data, indent=2, ensure_ascii=False)
            
            self.emit_progress(60, "Encrypting data...")
            
            # Encrypt and save
            metadata = {
                'content_type': 'group_templates',
                'total_templates': total_templates,
                'export_date': datetime.now().isoformat()
            }
            
            encrypt_file_with_format(
                data=json_data,
                output_path=self.output_path,
                password=self.password,
                encryption_method=self.encryption_method,
                file_format='usrx',
                metadata=metadata
            )
            
            self.emit_progress(100, "Export complete")
            self.emit_log(f"Templates exported to: {self.output_path}", LogLevel.SUCCESS)
            
            return f"Exported {total_templates} templates successfully"
        
        except Exception as e:
            self.emit_log(f"Export failed: {str(e)}", LogLevel.ERROR)
            raise
    
    def cleanup(self):
        """Cleanup resources"""
        pass


class TemplateImportTask(AbstractProgressTask):
    """
    Task for importing group templates from encrypted file
    """
    
    def __init__(self, input_path: str, password: str,
                 encryption_method: Optional[str] = None,
                 task_name: str = "Import Templates"):
        """
        Initialize template import task
        
        Args:
            input_path: Path to input file
            password: Decryption password
            encryption_method: 'aes-gcm' or 'gpg' (auto-detect if None)
            task_name: Custom task name
        """
        super().__init__(task_name, is_deterministic=True, can_pause=False)
        self.input_path = input_path
        self.password = password
        self.encryption_method = encryption_method
        self.imported_templates: List[GroupTemplate] = []
    
    def execute(self) -> str:
        """Execute template import"""
        self.emit_log(f"Importing templates from: {self.input_path}", LogLevel.INFO)
        self.emit_progress(10, "Reading file...")
        
        try:
            # Decrypt file
            self.emit_progress(20, "Decrypting data...")
            
            json_data = decrypt_file_with_format(
                input_path=self.input_path,
                password=self.password,
                encryption_method=self.encryption_method
            )
            
            self.emit_progress(50, "Parsing data...")
            
            # Parse JSON
            data = json.loads(json_data)
            
            # Validate structure
            if data.get('export_type') != 'group_templates':
                raise ValueError("Invalid file format - not a templates export file")
            
            templates_data = data.get('templates', [])
            total_templates = len(templates_data)
            
            self.emit_log(f"Found {total_templates} templates in file", LogLevel.INFO)
            self.emit_progress(60, "Converting templates...")
            
            # Convert to GroupTemplate objects
            for idx, template_dict in enumerate(templates_data):
                self.check_cancelled()
                
                try:
                    # Convert groups
                    groups = []
                    for group_dict in template_dict.get('groups', []):
                        group = ADGroup(
                            name=group_dict['name'],
                            dn=group_dict['dn'],
                            description=group_dict.get('description'),
                            group_type=group_dict.get('group_type'),
                            members_count=group_dict.get('members_count', 0),
                            metadata=group_dict.get('metadata', {})
                        )
                        groups.append(group)
                    
                    # Create template
                    created_date = None
                    if template_dict.get('created_date'):
                        created_date = datetime.fromisoformat(template_dict['created_date'])
                    
                    modified_date = None
                    if template_dict.get('modified_date'):
                        modified_date = datetime.fromisoformat(template_dict['modified_date'])
                    
                    template = GroupTemplate(
                        name=template_dict['name'],
                        description=template_dict.get('description', ''),
                        groups=groups,
                        metadata=template_dict.get('metadata', {}),
                        created_date=created_date,
                        modified_date=modified_date
                    )
                    self.imported_templates.append(template)
                    
                except Exception as e:
                    self.emit_log(
                        f"Warning: Failed to import template '{template_dict.get('name')}': {e}",
                        LogLevel.WARNING
                    )
                
                progress = 60 + int((idx / total_templates) * 30)
                self.emit_progress(progress, f"Processing template {idx + 1}/{total_templates}")
            
            self.emit_progress(100, "Import complete")
            self.emit_log(
                f"Successfully imported {len(self.imported_templates)} templates",
                LogLevel.SUCCESS
            )
            
            return f"Imported {len(self.imported_templates)} templates successfully"
        
        except Exception as e:
            self.emit_log(f"Import failed: {str(e)}", LogLevel.ERROR)
            raise
    
    def cleanup(self):
        """Cleanup resources"""
        pass
    
    def get_imported_templates(self) -> List[GroupTemplate]:
        """Get list of imported templates"""
        return self.imported_templates
