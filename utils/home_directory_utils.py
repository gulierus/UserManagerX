"""
Home Directory Path Generation Utilities
Provides functionality for generating home directory paths with placeholders
"""

import logging
import re
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from models import Person

logger = logging.getLogger(__name__)


@dataclass
class NamePartSpec:
    """
    Specification for extracting part of a name
    
    Attributes:
        part_index: Which part to use (1=first, 2=second, -1=last,
            0=all parts joined together, None=the whole name unchanged)
        char_count: How many characters to use (None=all)
    """
    part_index: Optional[int] = 1  # Default: use first part
    char_count: Optional[int] = None  # Default: use all characters
    
    def __repr__(self):
        return f"NamePartSpec(part={self.part_index}, chars={self.char_count})"


class PlaceholderError(Exception):
    """Exception raised for placeholder-related errors"""
    pass


class HomeDirectoryPathGenerator:
    """
    Generator for home directory paths with placeholder support
    
    Supported placeholders:
    - {first_name} - Full first name
    - {last_name} - Full last name
    - {first_name:1} - First part of first name
    - {first_name:1:3} - First 3 chars of first part of first name
    - {last_name:-1} - Last part of last name
    - {username} - AD username
    - {class_name} - Class name
    
    Examples:
    - "\\\\server\\users\\{last_name}\\{first_name:1:1}" 
      -> "\\\\server\\users\\Novak\\J"
    - "\\\\server\\home\\{username}"
      -> "\\\\server\\home\\novakj"
    """
    
    # Placeholder pattern: {field_name} or {field_name:part} or {field_name:part:chars}
    PLACEHOLDER_PATTERN = re.compile(r'\{([a-z_]+)(?::(-?\d+))?(?::(\d+))?\}')
    
    # Available placeholders
    AVAILABLE_PLACEHOLDERS = {
        'first_name': 'First name (supports part selection)',
        'last_name': 'Last name (supports part selection)',
        'username': 'AD username',
        'class_name': 'Class name',
    }
    
    @classmethod
    def generate_path(cls, template: str, person: Person, 
                     default_first_spec: Optional[NamePartSpec] = None,
                     default_last_spec: Optional[NamePartSpec] = None) -> str:
        """
        Generate home directory path from template
        
        Args:
            template: Path template with placeholders
            person: Person object with data
            default_first_spec: Default specification for first_name parts
            default_last_spec: Default specification for last_name parts
            
        Returns:
            Generated path
            
        Raises:
            PlaceholderError: If template contains invalid placeholders
        """
        if not template:
            raise PlaceholderError("Template cannot be empty")
        
        # Set defaults
        # A bare "{first_name}" / "{last_name}" is documented as the *full*
        # name.  The defaults used to be "first part" / "last part", which
        # silently dropped half of a compound name such as
        # "Novakova Svobodova"; part_index=None keeps the name whole.  A caller
        # that wants a single part still says so with its own spec, and a
        # template that spells the part out (e.g. "{last_name:-1}") still wins.
        if default_first_spec is None:
            default_first_spec = NamePartSpec(part_index=None, char_count=None)
        if default_last_spec is None:
            default_last_spec = NamePartSpec(part_index=None, char_count=None)
        
        result = template
        
        # Find all placeholders
        matches = list(cls.PLACEHOLDER_PATTERN.finditer(template))
        
        if not matches:
            logger.warning("Template contains no placeholders")
            return template
        
        # Process placeholders in reverse order to preserve positions
        for match in reversed(matches):
            full_match = match.group(0)
            field_name = match.group(1)
            part_index_str = match.group(2)
            char_count_str = match.group(3)
            
            # Parse part index
            part_index = int(part_index_str) if part_index_str else None
            char_count = int(char_count_str) if char_count_str else None
            
            # Generate replacement value
            try:
                value = cls._get_placeholder_value(
                    field_name, person, part_index, char_count,
                    default_first_spec, default_last_spec
                )
                
                # Replace placeholder with value
                start, end = match.span()
                result = result[:start] + value + result[end:]
                
            except Exception as e:
                logger.error(f"Error processing placeholder {full_match}: {e}")
                raise PlaceholderError(f"Invalid placeholder {full_match}: {str(e)}")
        
        logger.debug(f"Generated path: {result} from template: {template}")
        return result
    
    @classmethod
    def _get_placeholder_value(cls, field_name: str, person: Person,
                               part_index: Optional[int], char_count: Optional[int],
                               default_first_spec: NamePartSpec,
                               default_last_spec: NamePartSpec) -> str:
        """
        Get value for a specific placeholder
        
        Args:
            field_name: Name of the field
            person: Person object
            part_index: Which part to use (or None for default)
            char_count: How many characters to use (or None for all)
            default_first_spec: Default spec for first name
            default_last_spec: Default spec for last name
            
        Returns:
            Resolved placeholder value
            
        Raises:
            PlaceholderError: If field is invalid or value cannot be extracted
        """
        if field_name == 'first_name':
            if not person.first_name:
                raise PlaceholderError("First name is empty")
            
            spec = NamePartSpec(
                part_index=part_index if part_index is not None else default_first_spec.part_index,
                char_count=char_count if char_count is not None else default_first_spec.char_count
            )
            return cls._extract_name_part(person.first_name, spec)
        
        elif field_name == 'last_name':
            if not person.last_name:
                raise PlaceholderError("Last name is empty")
            
            spec = NamePartSpec(
                part_index=part_index if part_index is not None else default_last_spec.part_index,
                char_count=char_count if char_count is not None else default_last_spec.char_count
            )
            return cls._extract_name_part(person.last_name, spec)
        
        elif field_name == 'username':
            if not person.ad_username:
                raise PlaceholderError("Username is empty")
            return person.ad_username
        
        elif field_name == 'class_name':
            if not person.class_name:
                raise PlaceholderError("Class name is empty")
            return person.class_name
        
        else:
            raise PlaceholderError(f"Unknown placeholder: {field_name}")
    
    @classmethod
    def _extract_name_part(cls, name: str, spec: NamePartSpec) -> str:
        """
        Extract specific part of a name according to specification
        
        Args:
            name: Full name (may contain multiple parts)
            spec: Specification for extraction
            
        Returns:
            Extracted name part
            
        Raises:
            PlaceholderError: If part cannot be extracted
        """
        if not name or not name.strip():
            raise PlaceholderError("Name is empty")
        
        # Split name into parts
        parts = name.strip().split()
        
        if not parts:
            raise PlaceholderError("Name has no parts")
        
        # Select part
        if spec.part_index is None:
            # Whole name - only its whitespace is normalised
            selected = ' '.join(parts)
        elif spec.part_index == 0:
            # Use all parts (joined)
            selected = ''.join(parts)
        elif spec.part_index == -1:
            # Use last part
            selected = parts[-1]
        else:
            # Use specific part (1-based index)
            if spec.part_index < 1 or spec.part_index > len(parts):
                raise PlaceholderError(
                    f"Part index {spec.part_index} out of range (name has {len(parts)} parts)"
                )
            selected = parts[spec.part_index - 1]
        
        # Apply character limit
        if spec.char_count is not None:
            if spec.char_count < 1:
                raise PlaceholderError("Character count must be at least 1")
            selected = selected[:spec.char_count]
        
        return selected
    
    @classmethod
    def validate_template(cls, template: str) -> List[str]:
        """
        Validate a template and return list of issues
        
        Args:
            template: Template to validate
            
        Returns:
            List of validation issues (empty if valid)
        """
        issues = []
        
        if not template:
            issues.append("Template is empty")
            return issues
        
        if not template.strip():
            issues.append("Template contains only whitespace")
        
        # Find all placeholders
        matches = list(cls.PLACEHOLDER_PATTERN.finditer(template))
        
        if not matches:
            issues.append("Template contains no placeholders")
        
        for match in matches:
            field_name = match.group(1)
            part_index_str = match.group(2)
            char_count_str = match.group(3)
            
            # Check if field name is valid
            if field_name not in cls.AVAILABLE_PLACEHOLDERS:
                issues.append(f"Unknown placeholder: {{{field_name}}}")
                continue
            
            # Validate part index if specified
            if part_index_str:
                try:
                    part_index = int(part_index_str)
                    # 0 = all, -1 = last, positive = specific part
                    if part_index < -1:
                        issues.append(f"Invalid part index in {match.group(0)}: must be -1, 0, or positive")
                except ValueError:
                    issues.append(f"Invalid part index in {match.group(0)}: must be integer")
            
            # Validate character count if specified
            if char_count_str:
                try:
                    char_count = int(char_count_str)
                    if char_count < 1:
                        issues.append(f"Invalid character count in {match.group(0)}: must be positive")
                except ValueError:
                    issues.append(f"Invalid character count in {match.group(0)}: must be integer")
        
        return issues
    
    @classmethod
    def get_available_placeholders(cls) -> Dict[str, str]:
        """Get dictionary of available placeholders with descriptions"""
        return cls.AVAILABLE_PLACEHOLDERS.copy()
    
    @classmethod
    def get_example_templates(cls) -> List[Dict[str, str]]:
        """
        Get list of example templates
        
        Returns:
            List of dictionaries with 'template' and 'description' keys
        """
        return [
            {
                'template': '\\\\server\\users\\{last_name}\\{first_name}',
                'description': 'Full name hierarchy (e.g., \\\\server\\users\\Novak\\Jan)'
            },
            {
                'template': '\\\\server\\home\\{username}',
                'description': 'Username-based (e.g., \\\\server\\home\\novakj)'
            },
            {
                'template': '\\\\server\\students\\{class_name}\\{last_name}',
                'description': 'Class and last name (e.g., \\\\server\\students\\9A\\Novak)'
            },
            {
                'template': '\\\\server\\users\\{last_name:1:1}{first_name:1:1}',
                'description': 'Initials (e.g., \\\\server\\users\\NJ)'
            },
            {
                'template': '\\\\server\\home\\{last_name}_{first_name:1}',
                'description': 'Last name + first part of first name (e.g., \\\\server\\home\\Novak_Jan)'
            },
            {
                'template': 'H:\\{username}',
                'description': 'Drive letter with username (e.g., H:\\novakj)'
            }
        ]


class HomeDirectoryTemplateManager:
    """Manager for home directory templates"""
    
    #: Settings category the user's own templates are stored in.
    SETTINGS_CATEGORY = "home_directory_templates"

    def __init__(self, load_saved: bool = True):
        """Initialize template manager"""
        self.templates: Dict[str, str] = {}
        self._load_default_templates()
        #: names that ship with the application and cannot be deleted
        self.builtin_names = set(self.templates)
        if load_saved:
            self.load()
        logger.info("HomeDirectoryTemplateManager initialized")
    
    def _load_default_templates(self):
        """Load default templates"""
        examples = HomeDirectoryPathGenerator.get_example_templates()
        for idx, example in enumerate(examples, 1):
            template_name = f"Template {idx}"
            self.templates[template_name] = example['template']
    
    def add_template(self, name: str, template: str) -> bool:
        """
        Add a template
        
        Args:
            name: Template name
            template: Template path pattern
            
        Returns:
            True if added, False if name already exists
        """
        if name in self.templates:
            logger.warning(f"Template '{name}' already exists")
            return False
        
        # Validate template
        issues = HomeDirectoryPathGenerator.validate_template(template)
        if issues:
            logger.warning(f"Template validation failed: {issues}")
            return False
        
        self.templates[name] = template
        logger.info(f"Added home directory template: {name}")
        return True
    
    def remove_template(self, name: str) -> bool:
        """Remove a template"""
        if name in self.templates:
            del self.templates[name]
            logger.info(f"Removed template: {name}")
            return True
        return False
    
    def get_template(self, name: str) -> Optional[str]:
        """Get a template by name"""
        return self.templates.get(name)
    
    def get_all_templates(self) -> Dict[str, str]:
        """Get all templates"""
        return self.templates.copy()
    
    def is_builtin(self, name: str) -> bool:
        """True for a template that ships with the application."""
        return name in self.builtin_names

    def update_template(self, name: str, template: str) -> bool:
        """
        Replace the body of an existing template.

        Args:
            name: Template name.
            template: The new path pattern.

        Returns:
            True when the template existed and was replaced.
        """
        if name not in self.templates:
            return False
        self.templates[name] = template
        logger.info("Updated home directory template: %s", name)
        return True

    def rename_template(self, old_name: str, new_name: str) -> bool:
        """
        Rename a template, keeping its body.

        Returns:
            True when the rename happened.
        """
        if old_name not in self.templates or not new_name:
            return False
        if new_name in self.templates:
            return False
        self.templates[new_name] = self.templates.pop(old_name)
        logger.info("Renamed home directory template: %s -> %s", old_name, new_name)
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        Merge the user's own templates over the built-in ones.

        Without this the manager was rebuilt from the built-in examples on
        every construction, so a template the user created only survived as
        long as the dialog that created it.
        """
        try:
            from utils.settings_manager import get_settings
            stored = get_settings().get_category(self.SETTINGS_CATEGORY)
        except Exception:
            logger.exception("Could not load the home directory templates")
            return

        for name, template in (stored or {}).items():
            if isinstance(name, str) and isinstance(template, str) and template:
                self.templates[name] = template

    def save(self) -> bool:
        """
        Persist the templates the user added or changed.

        Built-in templates are only stored when their body was edited, so a
        later change to the shipped defaults still reaches existing users.

        Returns:
            True when the settings file was written.
        """
        try:
            from utils.settings_manager import get_settings
            defaults = {}
            probe = HomeDirectoryTemplateManager.__new__(HomeDirectoryTemplateManager)
            probe.templates = {}
            probe._load_default_templates()
            defaults = probe.templates

            custom = {
                name: body for name, body in self.templates.items()
                if defaults.get(name) != body
            }
            return get_settings().set_category(self.SETTINGS_CATEGORY, custom)
        except Exception:
            logger.exception("Could not save the home directory templates")
            return False

    def template_exists(self, name: str) -> bool:
        """Check if template exists"""
        return name in self.templates
