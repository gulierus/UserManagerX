"""
Active Directory Data Validator
FIXED VERSION - Improved validation logic and error handling
"""

import logging
import re
from typing import List, Dict, Tuple, Set, Optional
from dataclasses import dataclass

from models import Person

from utils.ad_utils import (
    remove_diacritics, 
    validate_username, 
    validate_password, 
    validate_email,
    ValidationIssue
)

logger = logging.getLogger(__name__)

@dataclass
class ValidationResult:
    """Result of validation"""
    is_valid: bool
    issues: List[ValidationIssue]
    
    @property
    def errors(self) -> List[ValidationIssue]:
        """Get only errors"""
        return [i for i in self.issues if i.severity == 'error']
    
    @property
    def warnings(self) -> List[ValidationIssue]:
        """Get only warnings"""
        return [i for i in self.issues if i.severity == 'warning']


class ADValidator:
    """Validator for Active Directory data"""
    
    @classmethod
    def validate_person(cls, person: Person, check_duplicates: bool = False, 
                       existing_usernames: Optional[Set[str]] = None) -> ValidationResult:
        """
        Validate a single person for AD compliance
        
        Args:
            person: Person to validate
            check_duplicates: Whether to check for duplicate usernames
            existing_usernames: Set of existing usernames to check against
            
        Returns:
            ValidationResult with any issues found
        """
        issues = []
        
        # Check required fields
        if not person.first_name or not person.first_name.strip():
            issues.append(ValidationIssue(
                person=person,
                field='first_name',
                issue_type='missing',
                message="First name is required",
                severity='error'
            ))
        
        if not person.last_name or not person.last_name.strip():
            issues.append(ValidationIssue(
                person=person,
                field='last_name',
                issue_type='missing',
                message="Last name is required",
                severity='error'
            ))
        
        if not person.class_name or not person.class_name.strip():
            issues.append(ValidationIssue(
                person=person,
                field='class_name',
                issue_type='missing',
                message="Class name is required",
                severity='error'
            ))
        
        # Validate username
        if person.ad_username:
            username_issues = validate_username(person)
            issues.extend(username_issues)
            
            # Check for duplicates
            if check_duplicates and existing_usernames is not None:
                if person.ad_username in existing_usernames:
                    issues.append(ValidationIssue(
                        person=person,
                        field='ad_username',
                        issue_type='duplicate',
                        message=f"Username '{person.ad_username}' already exists",
                        severity='error'
                    ))
        else:
            issues.append(ValidationIssue(
                person=person,
                field='ad_username',
                issue_type='missing',
                message="Username is required for AD",
                severity='error'
            ))
        
        # Validate password
        if person.ad_password:
            password_issues = validate_password(person)
            issues.extend(password_issues)
        else:
            issues.append(ValidationIssue(
                person=person,
                field='ad_password',
                issue_type='missing',
                message="Password is required for AD",
                severity='error'
            ))
        
        # Validate email if present
        if person.ad_email:
            email_issues = validate_email(person)
            issues.extend(email_issues)
        
        # Validate display name
        if not person.ad_display_name:
            issues.append(ValidationIssue(
                person=person,
                field='ad_display_name',
                issue_type='missing',
                message="Display name is recommended",
                severity='warning'
            ))
        
        is_valid = not any(i.severity == 'error' for i in issues)
        return ValidationResult(is_valid=is_valid, issues=issues)
    

    

    @classmethod
    def validate_source(cls, source) -> ValidationResult:
        """
        Validate all persons in a source
        
        Args:
            source: Source to validate
            
        Returns:
            Combined ValidationResult for all persons
        """
        all_issues = []
        all_usernames = set()
        
        # First pass: collect all usernames
        for cls_obj in source.classes:
            for person in cls_obj.persons:
                if person.ad_username:
                    all_usernames.add(person.ad_username)
        
        # Second pass: validate each person
        seen_usernames = set()
        for cls_obj in source.classes:
            for person in cls_obj.persons:
                # For duplicate checking, use usernames seen so far
                result = cls.validate_person(
                    person, 
                    check_duplicates=True, 
                    existing_usernames=seen_usernames
                )
                all_issues.extend(result.issues)
                
                # Add username to seen set after validation
                if person.ad_username:
                    seen_usernames.add(person.ad_username)
        
        is_valid = not any(i.severity == 'error' for i in all_issues)
        return ValidationResult(is_valid=is_valid, issues=all_issues)
    
    @classmethod
    def check_missing_properties(cls, person: Person) -> List[str]:
        """
        Check which AD properties are missing for a person
        
        Returns:
            List of missing property names
        """
        missing = []
        
        if not person.ad_username:
            missing.append('ad_username')
        if not person.ad_password:
            missing.append('ad_password')
        if not person.ad_display_name:
            missing.append('ad_display_name')
        if not person.ad_email:
            missing.append('ad_email')
        
        return missing
    
    @staticmethod
    def _count_distinct_persons(issues: List[ValidationIssue]) -> int:
        """
        Count how many different persons the given issues belong to.

        ``Person`` is a dataclass with a generated ``__eq__``, which means
        Python sets ``__hash__`` to ``None`` - putting Person objects into a
        set raises ``TypeError: unhashable type: 'Person'``.  Two students can
        also legitimately carry identical field values, so identity is the only
        correct grouping key here.

        Args:
            issues: Validation issues to group.

        Returns:
            Number of distinct person objects.
        """
        return len({id(issue.person) for issue in issues if issue.person is not None})

    @classmethod
    def analyze_source(cls, source) -> Dict:
        """
        Perform comprehensive analysis of source

        Returns:
            Dictionary with statistics and issues
        """
        validation = cls.validate_source(source)

        all_persons = source.get_all_persons()
        total_persons = len(all_persons)
        persons_with_issues = cls._count_distinct_persons(validation.issues)
        persons_with_errors = cls._count_distinct_persons(validation.errors)
        
        # Count missing properties
        missing_username = sum(1 for p in all_persons if not p.ad_username)
        missing_password = sum(1 for p in all_persons if not p.ad_password)
        missing_email = sum(1 for p in all_persons if not p.ad_email)
        
        return {
            'total_persons': total_persons,
            'persons_with_issues': persons_with_issues,
            'persons_with_errors': persons_with_errors,
            'total_errors': len(validation.errors),
            'total_warnings': len(validation.warnings),
            'missing_username': missing_username,
            'missing_password': missing_password,
            'missing_email': missing_email,
            'is_ready_for_sync': validation.is_valid,
            'validation_result': validation
        }
