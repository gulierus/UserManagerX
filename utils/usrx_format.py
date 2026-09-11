"""
USRX File Format Handler
Binary container format for User Manager X encrypted data

Format Structure:
- Magic bytes (4 bytes): "USRX"
- Version (2 bytes): Major.Minor (e.g. 1.0)
- Encryption type (1 byte): 0=None, 1=AES-GCM, 2=GPG
- Flags (1 byte): Reserved for future use
- Header size (4 bytes): Size of header in bytes
- Data size (8 bytes): Size of encrypted data in bytes
- Metadata section (variable): JSON metadata
- Data section (variable): Encrypted data or encryption-specific parameters
"""

import logging
import struct
import json
from typing import Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

# Constants
MAGIC_BYTES = b'USRX'
VERSION_MAJOR = 1
VERSION_MINOR = 0

# Encryption types
ENCRYPTION_NONE = 0
ENCRYPTION_AES_GCM = 1
ENCRYPTION_GPG = 2

ENCRYPTION_TYPE_MAP = {
    'none': ENCRYPTION_NONE,
    'aes-gcm': ENCRYPTION_AES_GCM,
    'gpg': ENCRYPTION_GPG
}

ENCRYPTION_TYPE_REVERSE = {v: k for k, v in ENCRYPTION_TYPE_MAP.items()}


class USRXFormatError(Exception):
    """Exception raised for USRX format errors"""
    pass


class USRXFile:
    """
    USRX file format handler
    
    Provides reading and writing of .usrx container files
    """
    
    def __init__(self):
        self.version_major = VERSION_MAJOR
        self.version_minor = VERSION_MINOR
        self.encryption_type = ENCRYPTION_NONE
        self.flags = 0
        self.metadata: Dict[str, Any] = {}
        self.data: bytes = b''
        
    @staticmethod
    def detect_format(file_path: str) -> bool:
        """
        Detect if file is in USRX format
        
        Args:
            file_path: Path to file
            
        Returns:
            True if file is USRX format
        """
        try:
            with open(file_path, 'rb') as f:
                magic = f.read(4)
                return magic == MAGIC_BYTES
        except Exception as e:
            logger.debug(f"Error detecting USRX format: {e}")
            return False
            
    @staticmethod
    def read_header(file_path: str) -> Dict[str, Any]:
        """
        Read only the header from USRX file (efficient for large files)
        
        Args:
            file_path: Path to file
            
        Returns:
            Dictionary with header information
            
        Raises:
            USRXFormatError: If file is not valid USRX format
        """
        try:
            with open(file_path, 'rb') as f:
                # Read magic bytes
                magic = f.read(4)
                if magic != MAGIC_BYTES:
                    raise USRXFormatError("Invalid magic bytes - not a USRX file")
                
                # Read version
                version_data = f.read(2)
                major, minor = struct.unpack('BB', version_data)
                
                # Read encryption type
                encryption_type = struct.unpack('B', f.read(1))[0]
                
                # Read flags
                flags = struct.unpack('B', f.read(1))[0]
                
                # Read header size
                header_size = struct.unpack('I', f.read(4))[0]
                
                # Read data size
                data_size = struct.unpack('Q', f.read(8))[0]
                
                # Read metadata
                metadata_size = header_size - 20  # Total header - fixed fields
                if metadata_size > 0:
                    metadata_bytes = f.read(metadata_size)
                    metadata = json.loads(metadata_bytes.decode('utf-8'))
                else:
                    metadata = {}
                
                return {
                    'version_major': major,
                    'version_minor': minor,
                    'encryption_type': ENCRYPTION_TYPE_REVERSE.get(encryption_type, 'unknown'),
                    'flags': flags,
                    'header_size': header_size,
                    'data_size': data_size,
                    'metadata': metadata
                }
                
        except Exception as e:
            logger.exception("Error reading USRX header")
            raise USRXFormatError(f"Failed to read USRX header: {str(e)}")
            
    def write(self, file_path: str, data: bytes, encryption_type: str, 
              metadata: Dict[str, Any] = None):
        """
        Write data to USRX file
        
        Args:
            file_path: Output file path
            data: Data to write (encrypted or plain)
            encryption_type: Type of encryption ('none', 'aes-gcm', 'gpg')
            metadata: Additional metadata to store
        """
        # An unrecognised label used to fall back to ENCRYPTION_NONE, which
        # stamped ciphertext as plaintext: the reader then handed the raw
        # encrypted bytes back as if they were the document.
        if encryption_type not in ENCRYPTION_TYPE_MAP:
            raise USRXFormatError(
                f"Unknown encryption type '{encryption_type}' "
                f"(expected one of: {', '.join(sorted(ENCRYPTION_TYPE_MAP))})"
            )

        try:
            # Work on a copy - write() used to inject 'created_by' into the
            # dictionary the caller passed in, so the caller's own metadata
            # grew a foreign key it never asked for.
            metadata = dict(metadata) if metadata else {}

            # Add standard metadata
            metadata['created_by'] = f'User Manager X {VERSION_MAJOR}.{VERSION_MINOR}'

            metadata_bytes = json.dumps(metadata, ensure_ascii=False).encode('utf-8')

            # Calculate sizes
            header_size = 20 + len(metadata_bytes)  # Fixed fields + metadata
            data_size = len(data)

            # Get encryption type code
            enc_type_code = ENCRYPTION_TYPE_MAP[encryption_type]
            
            # Write file
            with open(file_path, 'wb') as f:
                # Magic bytes
                f.write(MAGIC_BYTES)
                
                # Version
                f.write(struct.pack('BB', VERSION_MAJOR, VERSION_MINOR))
                
                # Encryption type
                f.write(struct.pack('B', enc_type_code))
                
                # Flags
                f.write(struct.pack('B', 0))
                
                # Header size
                f.write(struct.pack('I', header_size))
                
                # Data size
                f.write(struct.pack('Q', data_size))
                
                # Metadata
                f.write(metadata_bytes)
                
                # Data
                f.write(data)
            
            logger.info(f"Wrote USRX file: {file_path} ({data_size} bytes)")
            
        except Exception as e:
            logger.exception("Error writing USRX file")
            raise USRXFormatError(f"Failed to write USRX file: {str(e)}")
            
    def read(self, file_path: str) -> Tuple[bytes, str, Dict[str, Any]]:
        """
        Read data from USRX file
        
        Args:
            file_path: Input file path
            
        Returns:
            Tuple of (data, encryption_type, metadata)
            
        Raises:
            USRXFormatError: If file is invalid
        """
        try:
            # Read header
            header = self.read_header(file_path)
            
            # Read data section
            with open(file_path, 'rb') as f:
                # Skip to data section
                f.seek(header['header_size'])

                # Read data
                data = f.read(header['data_size'])

            # A truncated file used to yield a short data section that was then
            # handed to the decrypter, which blamed the user's password for the
            # resulting failure. Report the real cause instead.
            expected = header['data_size']
            if len(data) != expected:
                raise USRXFormatError(
                    f"File is truncated: the header declares {expected} byte(s) "
                    f"of data but only {len(data)} are present"
                )

            return (data, header['encryption_type'], header['metadata'])
            
        except USRXFormatError:
            raise
        except Exception as e:
            logger.exception("Error reading USRX file")
            raise USRXFormatError(f"Failed to read USRX file: {str(e)}")


def write_usrx_file(file_path: str, data: bytes, encryption_type: str,
                    metadata: Dict[str, Any] = None):
    """
    Convenience function to write USRX file
    
    Args:
        file_path: Output file path
        data: Data to write
        encryption_type: Encryption type
        metadata: Optional metadata
    """
    usrx = USRXFile()
    usrx.write(file_path, data, encryption_type, metadata)
    

def read_usrx_file(file_path: str) -> Tuple[bytes, str, Dict[str, Any]]:
    """
    Convenience function to read USRX file
    
    Args:
        file_path: Input file path
        
    Returns:
        Tuple of (data, encryption_type, metadata)
    """
    usrx = USRXFile()
    return usrx.read(file_path)


def is_usrx_file(file_path: str) -> bool:
    """
    Check if file is in USRX format
    
    Args:
        file_path: File path to check
        
    Returns:
        True if file is USRX format
    """
    return USRXFile.detect_format(file_path)


def get_usrx_info(file_path: str) -> Dict[str, Any]:
    """
    Get information about USRX file without reading all data
    
    Args:
        file_path: File path
        
    Returns:
        Dictionary with file information
    """
    return USRXFile.read_header(file_path)
