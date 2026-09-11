"""
PDF Decrypt Task
Progress task for decrypting encrypted PDF files
VERSION 2 - With USRX format support
"""

import logging
import base64

from utils.progress_tasks import AbstractProgressTask, LogLevel
from utils.encryption import decrypt_file_with_format

logger = logging.getLogger(__name__)


class PDFDecryptTask(AbstractProgressTask):
    """
    Task for decrypting encrypted PDF files
    
    Steps:
    1. Read encrypted file
    2. Decrypt using specified method (or auto-detect)
    3. Decode PDF data
    
    Supports both standard formats (.aes, .gpg) and USRX container format
    """
    
    def __init__(self, file_path: str, password: str, encryption_method: str = None):
        super().__init__(
            task_name="Decrypt PDF",
            is_deterministic=True,
            can_pause=False
        )
        
        self.file_path = file_path
        self.password = password
        self.encryption_method = encryption_method  # Can be None for auto-detect
        
        self.success = False
        self.decrypted_data: bytes = None
        
    def execute(self) -> str:
        """Execute PDF decryption"""
        try:
            total_steps = 3
            current_step = 0
            
            # Step 1: Read file
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Reading encrypted file..."
            )
            self.emit_log(f"Reading file: {self.file_path}", LogLevel.INFO)
            
            import os
            if not os.path.exists(self.file_path):
                raise FileNotFoundError(f"File not found: {self.file_path}")
            
            file_size = os.path.getsize(self.file_path)
            self.emit_log(f"File size: {file_size} bytes", LogLevel.INFO)
            
            self.check_cancelled()
            
            # Step 2: Decrypt with format auto-detection
            current_step += 1
            
            if self.encryption_method:
                self.emit_progress(
                    int((current_step / total_steps) * 100),
                    f"Decrypting with {self.encryption_method}..."
                )
                self.emit_log(f"Using encryption method: {self.encryption_method}", LogLevel.INFO)
            else:
                self.emit_progress(
                    int((current_step / total_steps) * 100),
                    "Auto-detecting format and decrypting..."
                )
                self.emit_log("Auto-detecting file format and encryption method", LogLevel.INFO)
            
            # Decrypt using encryption utils (handles both standard and USRX)
            decrypted_str = decrypt_file_with_format(
                self.file_path,
                self.password,
                encryption_method=self.encryption_method,
                file_format=None  # Auto-detect format
            )
            
            self.emit_log("Decryption successful", LogLevel.SUCCESS)
            
            self.check_cancelled()
            
            # Step 3: Decode PDF data
            current_step += 1
            self.emit_progress(
                int((current_step / total_steps) * 100),
                "Decoding PDF data..."
            )
            self.emit_log("Decoding base64 PDF data", LogLevel.INFO)
            
            # Decrypt functions return base64-encoded PDF as string
            # Decode back to bytes
            try:
                self.decrypted_data = base64.b64decode(decrypted_str)
            except Exception as e:
                # Maybe it's not base64 encoded (direct binary data)
                # Try to use it directly
                logger.warning(f"Base64 decode failed, trying direct data: {e}")
                if isinstance(decrypted_str, bytes):
                    self.decrypted_data = decrypted_str
                else:
                    self.decrypted_data = decrypted_str.encode('latin-1')
            
            self.emit_log(f"Decoded PDF: {len(self.decrypted_data)} bytes", LogLevel.INFO)
            
            # Verify it's a PDF
            if not self.decrypted_data.startswith(b'%PDF'):
                self.emit_log(
                    "Warning: Decrypted data doesn't start with PDF header",
                    LogLevel.WARNING
                )
            
            # Complete
            self.emit_progress(100, "Decryption complete")
            self.emit_log("PDF decryption completed successfully", LogLevel.SUCCESS)
            
            self.success = True
            return f"Decrypted {len(self.decrypted_data)} bytes"
            
        except Exception as e:
            self.emit_log(f"Decryption failed: {str(e)}", LogLevel.ERROR)
            raise
        
    def cleanup(self):
        """Cleanup resources"""
        pass
