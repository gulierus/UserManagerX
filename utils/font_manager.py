"""
Font Manager
Manages fonts for PDF export - loads system fonts and custom fonts from resources
Registers fonts with both Qt and ReportLab
"""

import logging
import os
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtCore import QFile, QIODevice

logger = logging.getLogger(__name__)


class FontManager:
    """
    Singleton font manager for handling fonts across the application
    
    Features:
    - Loads system fonts via QFontDatabase
    - Loads custom fonts from application resources
    - Registers fonts with ReportLab for PDF generation
    - Provides font family lists for UI
    - Maps Qt font names to ReportLab font names
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FontManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self.font_database = QFontDatabase
        
        # Maps Qt font family name -> ReportLab font name
        self.qt_to_reportlab_map: Dict[str, str] = {}
        
        # Maps ReportLab font name -> font file path (for custom fonts)
        self.reportlab_font_files: Dict[str, str] = {}
        
        # List of available font families for UI
        self.available_families: List[str] = []
        
        # Default fallback fonts (built-in ReportLab fonts)
        self._register_builtin_fonts()
        
        logger.info("FontManager initialized")
        
    def _register_builtin_fonts(self):
        """Register built-in ReportLab fonts as fallbacks"""
        builtin_fonts = {
            'Helvetica': 'Helvetica',
            'Times': 'Times-Roman',
            'Courier': 'Courier',
        }
        
        for qt_name, rl_name in builtin_fonts.items():
            self.qt_to_reportlab_map[qt_name] = rl_name
            self.available_families.append(qt_name)
        
        logger.debug(f"Registered {len(builtin_fonts)} built-in fonts")
        
    def load_custom_fonts_from_resources(self, resource_prefix: str = ":/fonts"):
        """
        Load custom fonts from Qt resources
        
        Args:
            resource_prefix: Qt resource prefix where fonts are stored
            
        Expected resource structure:
            :/fonts/DejaVuSans.ttf
            :/fonts/DejaVuSans-Bold.ttf
            :/fonts/DejaVuSerif.ttf
            etc.
        """
        try:
            # List of DejaVu font files to load
            dejavu_fonts = [
                "DejaVuSans.ttf",
                "DejaVuSans-Bold.ttf",
                "DejaVuSans-Oblique.ttf",
                "DejaVuSans-BoldOblique.ttf",
                "DejaVuSerif.ttf",
                "DejaVuSerif-Bold.ttf",
                "DejaVuSerif-Italic.ttf",
                "DejaVuSerif-BoldItalic.ttf",
                "DejaVuSansMono.ttf",
                "DejaVuSansMono-Bold.ttf",
                "DejaVuSansMono-Oblique.ttf",
                "DejaVuSansMono-BoldOblique.ttf",
            ]
            
            loaded_count = 0
            
            for font_file in dejavu_fonts:
                resource_path = f"{resource_prefix}/{font_file}"
                
                # Try to load from Qt resources
                if self._load_font_from_resource(resource_path):
                    loaded_count += 1
                else:
                    logger.warning(f"Could not load font from resource: {resource_path}")
            
            logger.info(f"Loaded {loaded_count}/{len(dejavu_fonts)} custom fonts from resources")
            
        except Exception as e:
            logger.exception("Error loading custom fonts from resources")
            
    def _load_font_from_resource(self, resource_path: str) -> bool:
        """
        Load a single font from Qt resource
        
        Args:
            resource_path: Qt resource path (e.g., ":/fonts/DejaVuSans.ttf")
            
        Returns:
            True if font was loaded successfully
        """
        try:
            # Open resource file
            resource_file = QFile(resource_path)
            
            if not resource_file.exists():
                logger.debug(f"Resource does not exist: {resource_path}")
                return False
            
            if not resource_file.open(QIODevice.OpenModeFlag.ReadOnly):
                logger.warning(f"Could not open resource: {resource_path}")
                return False
            
            # Read font data
            font_data = resource_file.readAll()
            resource_file.close()
            
            if font_data.isEmpty():
                logger.warning(f"Resource is empty: {resource_path}")
                return False
            
            # Add font to Qt font database
            font_id = self.font_database.addApplicationFontFromData(font_data)
            
            if font_id < 0:
                logger.warning(f"Failed to add font to database: {resource_path}")
                return False
            
            # Get font families added
            families = self.font_database.applicationFontFamilies(font_id)
            
            if not families:
                logger.warning(f"No families found for font: {resource_path}")
                return False
            
            # Register with ReportLab
            for family in families:
                if family not in self.available_families:
                    self.available_families.append(family)
                
                # Try to register with ReportLab
                self._register_font_with_reportlab(family, font_data.data(), resource_path)
            
            logger.debug(f"Loaded font: {resource_path} -> {families}")
            return True
            
        except Exception as e:
            logger.exception(f"Error loading font from resource: {resource_path}")
            return False
            
    def _register_font_with_reportlab(self, family_name: str, font_data: bytes, 
                                     resource_path: str):
        """
        Register font with ReportLab for PDF generation
        
        Args:
            family_name: Qt font family name
            font_data: Raw font file data
            resource_path: Original resource path (for logging)
        """
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            import tempfile
            
            # ReportLab needs a file path, so write to temp file
            # We'll keep these temp files for the lifetime of the application
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, 
                                            suffix='.ttf', prefix='font_') as tmp:
                tmp.write(font_data)
                tmp_path = tmp.name
            
            # Register with ReportLab
            # Use family name as ReportLab font name for simplicity
            rl_font_name = family_name.replace(' ', '')  # Remove spaces for ReportLab
            
            try:
                pdfmetrics.registerFont(TTFont(rl_font_name, tmp_path))
                
                # Store mapping
                self.qt_to_reportlab_map[family_name] = rl_font_name
                self.reportlab_font_files[rl_font_name] = tmp_path
                
                logger.debug(f"Registered font with ReportLab: {family_name} -> {rl_font_name}")
                
            except Exception as e:
                logger.warning(f"Could not register font with ReportLab: {family_name}: {e}")
                # Clean up temp file if registration failed
                try:
                    os.unlink(tmp_path)
                except:
                    pass
                    
        except Exception as e:
            logger.exception(f"Error registering font with ReportLab: {family_name}")
            
    def get_available_families(self) -> List[str]:
        """
        Get list of available font families for UI
        
        Returns:
            List of font family names
        """
        return sorted(self.available_families)
        
    def get_reportlab_font_name(self, qt_family_name: str) -> str:
        """
        Get ReportLab font name for a Qt font family
        
        Args:
            qt_family_name: Qt font family name
            
        Returns:
            ReportLab font name (falls back to Helvetica if not found)
        """
        return self.qt_to_reportlab_map.get(qt_family_name, 'Helvetica')
        
    def get_system_fonts(self) -> List[str]:
        """
        Get list of system fonts (excluding custom fonts)
        
        Returns:
            List of system font family names
        """
        # Get all families from Qt
        all_families = self.font_database.families()
        
        # Filter out custom fonts (DejaVu)
        system_fonts = [f for f in all_families 
                       if not f.startswith('DejaVu') and f in self.available_families]
        
        return sorted(system_fonts)
        
    def get_custom_fonts(self) -> List[str]:
        """
        Get list of custom fonts (loaded from resources)
        
        Returns:
            List of custom font family names
        """
        custom_fonts = [f for f in self.available_families if f.startswith('DejaVu')]
        return sorted(custom_fonts)
        
    def is_font_available(self, family_name: str) -> bool:
        """
        Check if a font family is available
        
        Args:
            family_name: Font family name to check
            
        Returns:
            True if font is available
        """
        return family_name in self.available_families
        
    def get_font_info(self, family_name: str) -> Dict[str, any]:
        """
        Get information about a font
        
        Args:
            family_name: Font family name
            
        Returns:
            Dictionary with font information
        """
        info = {
            'family': family_name,
            'available': family_name in self.available_families,
            'reportlab_name': self.qt_to_reportlab_map.get(family_name),
            'is_custom': family_name.startswith('DejaVu'),
            'is_builtin': family_name in ['Helvetica', 'Times', 'Courier'],
        }
        
        # Get styles available for this font
        if family_name in self.available_families:
            styles = self.font_database.styles(family_name)
            info['styles'] = styles
        
        return info
        
    def cleanup(self):
        """Cleanup temporary font files"""
        for font_file in self.reportlab_font_files.values():
            try:
                if os.path.exists(font_file):
                    os.unlink(font_file)
            except Exception as e:
                logger.debug(f"Could not delete temp font file: {e}")


# Global font manager instance
_font_manager: Optional[FontManager] = None


def get_font_manager() -> FontManager:
    """
    Get the global font manager instance
    
    Returns:
        FontManager instance
    """
    global _font_manager
    
    if _font_manager is None:
        _font_manager = FontManager()
    
    return _font_manager


def initialize_fonts(load_custom_fonts: bool = True):
    """
    Initialize font system
    
    Should be called early in application startup
    
    Args:
        load_custom_fonts: Whether to load custom fonts from resources
    """
    manager = get_font_manager()
    
    if load_custom_fonts:
        manager.load_custom_fonts_from_resources(":/fonts")
    
    logger.info(f"Font system initialized: {len(manager.get_available_families())} fonts available")
