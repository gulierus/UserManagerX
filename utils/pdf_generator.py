"""
PDF Generator
Advanced PDF generation with full customization support using ReportLab
VERSION 2 - Dynamic font support using FontManager
"""

import logging
from typing import List, Dict, Any, Union
from datetime import datetime
from io import BytesIO

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)
from reportlab.pdfgen import canvas

from PyQt6.QtCore import QBuffer, QIODevice
from utils.font_manager import get_font_manager

logger = logging.getLogger(__name__)


class PageMarginDrawer:
    """Helper class to draw page margins with dashed lines"""
    
    def __init__(self, margins: Dict[str, float]):
        self.margins = margins
        
    def draw_margins(self, canvas_obj, doc):
        """Draw margin indicators on the page"""
        canvas_obj.saveState()
        
        # Set dash pattern for margin lines
        canvas_obj.setDash(3, 3)
        canvas_obj.setStrokeColor(colors.grey)
        canvas_obj.setLineWidth(0.5)
        
        # Get page dimensions
        width, height = doc.pagesize
        
        # Convert margins from cm to points
        top = self.margins['top'] * cm
        bottom = self.margins['bottom'] * cm
        left = self.margins['left'] * cm
        right = self.margins['right'] * cm
        
        # Draw top margin line
        canvas_obj.line(0, height - top, width, height - top)
        
        # Draw bottom margin line
        canvas_obj.line(0, bottom, width, bottom)
        
        # Draw left margin line
        canvas_obj.line(left, 0, left, height)
        
        # Draw right margin line
        canvas_obj.line(width - right, 0, width - right, height)
        
        canvas_obj.restoreState()


class PDFGenerator:
    """
    Advanced PDF table generator with full customization
    
    Features:
    - Multiple page orientations
    - Configurable margins
    - Custom table styling
    - Text wrapping
    - Custom alignment
    - Header/footer support
    - Dynamic font support via FontManager
    """
    
    def __init__(self, table_data: List[Dict[str, Any]], 
                 settings: Dict[str, Any],
                 show_margins: bool = False):
        """
        Initialize PDF generator
        
        Args:
            table_data: List of row dictionaries
            settings: PDF generation settings
            show_margins: Whether to show margin indicators (for preview)
        """
        self.table_data = table_data
        self.settings = settings
        self.show_margins = show_margins
        
        # Get font manager
        self.font_manager = get_font_manager()
        
        # Parse settings
        self.selected_columns = settings['selected_columns']
        self.column_widths = settings['column_widths']
        self.orientation = settings['orientation']
        self.margins = settings['margins']
        self.row_height = settings['row_height']
        self.text_wrap = settings['text_wrap']
        self.alignment = settings['alignment']
        self.header_color = settings['header_color']
        self.font = settings['font']
        self.include_title = settings['include_title']
        self.title_text = settings.get('title_text', '')
        self.include_date = settings['include_date']
        
        # Get ReportLab font name
        self.reportlab_font = self._get_reportlab_font_name()
        
    def _get_reportlab_font_name(self) -> str:
        """
        Get ReportLab font name from Qt font family
        
        Returns:
            ReportLab font name
        """
        qt_family = self.font['family']
        rl_font = self.font_manager.get_reportlab_font_name(qt_family)
        
        logger.debug(f"Font mapping: {qt_family} -> {rl_font}")
        
        return rl_font
        
    def generate_to_buffer(self, buffer: Union[QBuffer, BytesIO]) -> bytes:
        """
        Generate PDF to a buffer
        
        Args:
            buffer: QBuffer or BytesIO to write PDF to
            
        Returns:
            PDF data as bytes
        """
        # Ensure buffer is open for writing
        if isinstance(buffer, QBuffer):
            if not buffer.isOpen():
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            # Create BytesIO wrapper for ReportLab
            bytes_buffer = BytesIO()
        else:
            bytes_buffer = buffer
        
        try:
            # Generate PDF
            self._generate_pdf(bytes_buffer)
            
            # Get data
            pdf_data = bytes_buffer.getvalue()
            
            # Write to QBuffer if needed
            if isinstance(buffer, QBuffer):
                buffer.write(pdf_data)
            
            logger.info(f"Generated PDF: {len(pdf_data)} bytes")
            return pdf_data
            
        except Exception as e:
            logger.exception("Error generating PDF")
            raise
        
    def generate_to_file(self, file_path: str) -> bytes:
        """
        Generate PDF to a file
        
        Args:
            file_path: Path to output file
            
        Returns:
            PDF data as bytes
        """
        with open(file_path, 'wb') as f:
            buffer = BytesIO()
            pdf_data = self.generate_to_buffer(buffer)
            f.write(pdf_data)
            return pdf_data
        
    def _generate_pdf(self, buffer: BytesIO):
        """Internal PDF generation logic"""
        # Determine page size
        if self.orientation == 'landscape':
            pagesize = landscape(A4)
        else:
            pagesize = A4
        
        # Create document with margins
        doc = SimpleDocTemplate(
            buffer,
            pagesize=pagesize,
            rightMargin=self.margins['right'] * cm,
            leftMargin=self.margins['left'] * cm,
            topMargin=self.margins['top'] * cm,
            bottomMargin=self.margins['bottom'] * cm
        )
        
        # Story - container for flowable content
        story = []
        
        # Add title if requested
        if self.include_title and self.title_text:
            story.extend(self._create_title())
        
        # Add date if requested
        if self.include_date:
            story.extend(self._create_date())
        
        # Add table
        story.extend(self._create_table())
        
        # Build PDF with optional margin drawer
        if self.show_margins:
            margin_drawer = PageMarginDrawer(self.margins)
            doc.build(story, onFirstPage=margin_drawer.draw_margins,
                     onLaterPages=margin_drawer.draw_margins)
        else:
            doc.build(story)
        
    def _create_title(self) -> List:
        """Create title elements"""
        elements = []
        
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=16,
            textColor=colors.HexColor('#1a1a1a'),
            spaceAfter=20,
            alignment=TA_CENTER,
            fontName=self.reportlab_font
        )
        
        title = Paragraph(self.title_text, title_style)
        elements.append(title)
        elements.append(Spacer(1, 0.5 * cm))
        
        return elements
        
    def _create_date(self) -> List:
        """Create date element"""
        elements = []
        
        styles = getSampleStyleSheet()
        date_style = ParagraphStyle(
            'DateStyle',
            parent=styles['Normal'],
            fontSize=10,
            textColor=colors.grey,
            spaceAfter=10,
            alignment=TA_CENTER,
            fontName=self.reportlab_font
        )
        
        date_text = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        date_para = Paragraph(date_text, date_style)
        elements.append(date_para)
        elements.append(Spacer(1, 0.3 * cm))
        
        return elements
        
    def _create_table(self) -> List:
        """Create table elements"""
        elements = []
        
        # Calculate column widths
        col_widths = self._calculate_column_widths()
        
        # Prepare table data
        table_data = self._prepare_table_data()
        
        # Calculate row heights
        row_heights = self._calculate_row_heights(len(table_data))
        
        # Create table
        table = Table(
            table_data,
            colWidths=col_widths,
            rowHeights=row_heights,
            repeatRows=1  # Repeat header on each page
        )
        
        # Apply table style
        table.setStyle(self._create_table_style())
        
        elements.append(table)
        
        return elements
        
    def _calculate_column_widths(self) -> List[float]:
        """Calculate column widths based on settings, always respecting page margins"""
        # Determine total available width (page width minus margins)
        if self.orientation == 'landscape':
            from reportlab.lib.pagesizes import landscape, A4
            page_width = landscape(A4)[0]
        else:
            from reportlab.lib.pagesizes import A4
            page_width = A4[0]

        available_width = (page_width
                           - self.margins['left'] * cm
                           - self.margins['right'] * cm)

        manual_widths = {}
        auto_cols = []

        for col in self.selected_columns:
            width_setting = self.column_widths.get(col, 'auto')
            if width_setting == 'auto':
                auto_cols.append(col)
            else:
                manual_widths[col] = float(width_setting) * cm

        # Clamp total manual widths to available width
        total_manual = sum(manual_widths.values())
        if total_manual > available_width and manual_widths:
            scale = available_width / total_manual
            manual_widths = {col: w * scale for col, w in manual_widths.items()}
            total_manual = available_width

        # Distribute remaining width equally among auto columns
        remaining = max(0, available_width - total_manual)
        auto_width = (remaining / len(auto_cols)) if auto_cols else 0

        widths = []
        for col in self.selected_columns:
            if col in manual_widths:
                widths.append(manual_widths[col])
            else:
                widths.append(auto_width)

        return widths
        
    def _calculate_row_heights(self, num_rows: int) -> List[float]:
        """Calculate row heights based on settings"""
        if self.row_height['auto']:
            # Auto height - let ReportLab decide
            return None
        else:
            # Manual height
            height = self.row_height['value'] * cm
            # First row is header, might want different height
            return [height] * num_rows
        
    def _prepare_table_data(self) -> List[List]:
        """Prepare table data for ReportLab"""
        data = []
        
        # Header row
        header = [col for col in self.selected_columns]
        data.append(header)
        
        # Data rows
        for row_dict in self.table_data:
            row = []
            for col in self.selected_columns:
                value = row_dict.get(col, '')
                
                # Handle text wrapping
                if self.text_wrap:
                    # Use Paragraph for wrapping
                    cell_style = self._get_cell_style()
                    cell = Paragraph(str(value), cell_style)
                else:
                    # Plain text
                    cell = str(value)
                
                row.append(cell)
            
            data.append(row)
        
        return data
        
    def _get_cell_style(self) -> ParagraphStyle:
        """Get paragraph style for table cells"""
        styles = getSampleStyleSheet()
        
        # Map alignment
        h_align = TA_LEFT
        if self.alignment['horizontal'] == 'CENTER':
            h_align = TA_CENTER
        elif self.alignment['horizontal'] == 'RIGHT':
            h_align = TA_RIGHT
        
        cell_style = ParagraphStyle(
            'CellStyle',
            parent=styles['Normal'],
            fontSize=self.font['size'],
            fontName=self.reportlab_font,
            alignment=h_align,
            wordWrap='CJK'  # Enable word wrapping
        )
        
        return cell_style
        
    def _create_table_style(self) -> TableStyle:
        """Create table styling"""
        # Map vertical alignment
        v_align = 'TOP'
        if self.alignment['vertical'] == 'MIDDLE':
            v_align = 'MIDDLE'
        elif self.alignment['vertical'] == 'BOTTOM':
            v_align = 'BOTTOM'
        
        # Map horizontal alignment for non-wrapped text
        h_align = 'LEFT'
        if self.alignment['horizontal'] == 'CENTER':
            h_align = 'CENTER'
        elif self.alignment['horizontal'] == 'RIGHT':
            h_align = 'RIGHT'
        
        # Try to use bold variant of font for header
        header_font = self._get_bold_font_variant()
        
        style_commands = [
            # Header styling
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(self.header_color)),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), header_font),
            ('FONTSIZE', (0, 0), (-1, 0), self.font['size']),
            ('ALIGN', (0, 0), (-1, 0), h_align),
            ('VALIGN', (0, 0), (-1, 0), v_align),
            
            # Data rows styling
            ('FONTNAME', (0, 1), (-1, -1), self.reportlab_font),
            ('FONTSIZE', (0, 1), (-1, -1), self.font['size']),
            ('ALIGN', (0, 1), (-1, -1), h_align),
            ('VALIGN', (0, 1), (-1, -1), v_align),
            
            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
            
            # Padding
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            
            # Alternating row colors for better readability
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')]),
        ]
        
        return TableStyle(style_commands)
        
    def _get_bold_font_variant(self) -> str:
        """
        Try to get bold variant of current font
        
        Returns:
            Font name (bold variant if available, otherwise regular)
        """
        # For built-in fonts
        if self.reportlab_font == 'Helvetica':
            return 'Helvetica-Bold'
        elif self.reportlab_font == 'Times-Roman':
            return 'Times-Bold'
        elif self.reportlab_font == 'Courier':
            return 'Courier-Bold'
        
        # For custom fonts, try to find bold variant
        # DejaVu fonts follow naming pattern
        qt_family = self.font['family']
        
        if 'DejaVu' in qt_family:
            # Try to register bold variant if not already
            bold_family = qt_family  # For DejaVu, the family includes the style
            
            # Try common patterns
            if 'Sans' in qt_family and 'Bold' not in qt_family:
                bold_family = qt_family.replace('Sans', 'Sans Bold')
            elif 'Serif' in qt_family and 'Bold' not in qt_family:
                bold_family = qt_family.replace('Serif', 'Serif Bold')
            elif 'Mono' in qt_family and 'Bold' not in qt_family:
                bold_family = qt_family.replace('Mono', 'Mono Bold')
            
            # Check if bold variant exists
            if self.font_manager.is_font_available(bold_family):
                return self.font_manager.get_reportlab_font_name(bold_family)
        
        # Fallback to regular font
        return self.reportlab_font
        
    def validate_settings(self) -> tuple:
        """
        Validate settings for potential issues
        
        Returns:
            (is_valid, warnings) tuple
        """
        warnings = []
        
        # Check if columns fit on page
        total_width = 0
        page_width = A4[0] if self.orientation == 'portrait' else A4[1]
        available_width = page_width - (self.margins['left'] + self.margins['right']) * cm
        
        for col in self.selected_columns:
            width_setting = self.column_widths.get(col, 'auto')
            if width_setting != 'auto':
                total_width += float(width_setting) * cm
        
        # If we have manual widths that exceed page width
        if total_width > available_width:
            warnings.append(
                f"Manual column widths ({total_width/cm:.1f} cm) exceed "
                f"available page width ({available_width/cm:.1f} cm). "
                "Columns will be clipped."
            )
        
        # Check if too many columns for auto width
        if len(self.selected_columns) > 15 and all(
            self.column_widths.get(col) == 'auto' for col in self.selected_columns
        ):
            warnings.append(
                f"Large number of columns ({len(self.selected_columns)}) with auto width. "
                "Consider using landscape orientation or reducing columns."
            )
        
        # Check if font is available
        if not self.font_manager.is_font_available(self.font['family']):
            warnings.append(
                f"Font '{self.font['family']}' may not be available. "
                "PDF may use fallback font."
            )
        
        return (True, warnings)
