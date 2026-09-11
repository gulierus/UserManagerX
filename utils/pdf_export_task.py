"""
PDF Export Task
Progress task for exporting table data to encrypted PDF
VERSION 2 - Fixed pikepdf temp file cleanup issue
"""

import logging
import time
from typing import List, Dict, Any
from io import BytesIO

from utils.progress_tasks import AbstractProgressTask, LogLevel
from utils.pdf_generator import PDFGenerator
from utils.encryption import encrypt_file_with_format

logger = logging.getLogger(__name__)


class PDFExportTask(AbstractProgressTask):
    """
    Task for exporting table data to encrypted PDF
    
    Steps:
    1. Validate settings
    2. Generate PDF in memory
    3. Encrypt PDF
    4. Write to file
    """
    
    def __init__(self, table_data: List[Dict[str, Any]], settings: Dict[str, Any]):
        super().__init__(
            task_name="PDF Export",
            is_deterministic=True,
            can_pause=False
        )
        
        self.table_data = table_data
        self.settings = settings
        self.success = False
        
    def execute(self) -> str:
        """Execute PDF export"""
        try:
            total_steps = 4
            current_step = 0
            
            # Step 1: Validate settings
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Validating settings..."
            )
            self.emit_log("Validating export settings", LogLevel.INFO)
            
            generator = PDFGenerator(self.table_data, self.settings)
            is_valid, warnings = generator.validate_settings()
            
            if warnings:
                for warning in warnings:
                    self.emit_log(f"Warning: {warning}", LogLevel.WARNING)
            
            self.check_cancelled()
            
            # Step 2: Generate PDF
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Generating PDF document..."
            )
            self.emit_log(
                f"Generating PDF with {len(self.table_data)} rows, "
                f"{len(self.settings['selected_columns'])} columns",
                LogLevel.INFO
            )
            
            buffer = BytesIO()
            pdf_data = generator.generate_to_buffer(buffer)
            
            self.emit_log(f"Generated PDF: {len(pdf_data)} bytes", LogLevel.INFO)
            
            self.check_cancelled()
            
            # Step 3: Encrypt PDF
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Encrypting PDF..."
            )
            
            encryption_method = self.settings['encryption_method']
            file_format = self.settings.get('file_format', 'standard')
            password = self.settings['password']
            output_path = self.settings['output_path']
            
            self.emit_log(f"Encrypting with {encryption_method} ({file_format})", LogLevel.INFO)
            
            if encryption_method == 'pikepdf':
                # Use pikepdf encryption - FIXED version
                self._encrypt_with_pikepdf(pdf_data, output_path, password)
                
            else:
                # Use encryption utils for AES-GCM or GPG with optional USRX format
                import base64
                pdf_str = base64.b64encode(pdf_data).decode('ascii')
                
                encrypt_file_with_format(
                    pdf_str, output_path, password,
                    encryption_method, file_format
                )
            
            self.check_cancelled()
            
            # Step 4: Verify output
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Verifying output..."
            )
            
            import os
            if not os.path.exists(output_path):
                raise RuntimeError("Output file was not created")
            
            file_size = os.path.getsize(output_path)
            self.emit_log(f"Output file: {file_size} bytes", LogLevel.INFO)
            
            # Complete
            self.emit_progress(100, "Export complete")
            self.emit_log("PDF export completed successfully", LogLevel.SUCCESS)
            
            self.success = True
            return f"Exported {len(self.table_data)} rows to {output_path}"
            
        except Exception as e:
            self.emit_log(f"Export failed: {str(e)}", LogLevel.ERROR)
            raise
        
    def _encrypt_with_pikepdf(self, pdf_data: bytes, output_path: str, password: str):
        """
        Encrypt PDF using pikepdf
        
        FIXED: Properly closes PDF before deleting temp file to avoid Windows file locking
        """
        try:
            import pikepdf
            import tempfile
            import os
            
            # Create temporary file for unencrypted PDF
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pdf') as tmp:
                tmp.write(pdf_data)
                tmp_path = tmp.name
            
            pdf = None
            try:
                # Open PDF
                pdf = pikepdf.open(tmp_path)
                
                # Save with encryption
                pdf.save(
                    output_path,
                    encryption=pikepdf.Encryption(
                        owner=password,
                        user=password,
                        R=6,  # AES-256
                        allow=pikepdf.Permissions(
                            accessibility=True,
                            extract=False,
                            modify_annotation=False,
                            modify_assembly=False,
                            modify_form=False,
                            modify_other=False,
                            print_highres=True,
                            print_lowres=True
                        )
                    )
                )
                
                self.emit_log("Encrypted with pikepdf (print-only)", LogLevel.INFO)
                
            finally:
                # CRITICAL: Close PDF before deleting temp file
                if pdf is not None:
                    pdf.close()
                
                # Small delay to ensure file handles are released (Windows fix)
                time.sleep(0.1)
                
                # Now try to delete temp file
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except PermissionError as e:
                        # If still locked, log warning but don't fail
                        # File will be cleaned up by OS temp file cleanup eventually
                        logger.warning(f"Could not delete temp file (will be cleaned by OS): {e}")
                    
        except ImportError:
            raise RuntimeError("pikepdf library is not installed")
        
    def cleanup(self):
        """Cleanup resources"""
        pass
