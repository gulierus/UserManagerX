"""
Encryption utilities with support for both GPG and AES-GCM
Provides maximum security encryption with proper error handling and large file support
"""

import logging
import tempfile
import os
import json
import base64
import binascii
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, Literal
from contextlib import contextmanager
import shutil

from utils.usrx_format import (
    write_usrx_file, read_usrx_file, is_usrx_file,
    get_usrx_info, USRXFormatError
)

logger = logging.getLogger(__name__)

# Chunk size for streaming large files (1MB)
CHUNK_SIZE = 1024 * 1024

# Encryption method type
EncryptionMethod = Literal["gpg", "aes-gcm"]


def check_gpg_available() -> bool:
    """
    Check if GPG is available
    
    Returns:
        True if python-gnupg is available and GnuPG is installed
    """
    try:
        import gnupg
        gpg = gnupg.GPG()
        # Try to get version to verify GPG is actually working
        version = gpg.version
        return version is not None
    except (ImportError, Exception) as e:
        logger.debug(f"GPG not available: {e}")
        return False


def check_aes_available() -> bool:
    """
    Check if PyCryptodome is available for AES-GCM encryption
    
    Returns:
        True if PyCryptodome is available
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        from Crypto.Protocol.KDF import scrypt
        return True
    except ImportError as e:
        logger.debug(f"PyCryptodome not available: {e}")
        return False


def get_available_methods() -> Dict[str, bool]:
    """
    Get dictionary of available encryption methods
    
    Returns:
        Dictionary with method names as keys and availability as values
    """
    return {
        "gpg": check_gpg_available(),
        "aes-gcm": check_aes_available()
    }


@contextmanager
def _temp_gpg_home():
    """
    Context manager for temporary GPG home directory
    Ensures proper cleanup after use
    """
    gpg_home = tempfile.mkdtemp(prefix='gpg_')
    try:
        yield gpg_home
    finally:
        try:
            shutil.rmtree(gpg_home, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to cleanup GPG home directory: {e}")


def _get_gpg_instance():
    """
    Create GPG instance with maximum security settings
    
    Returns:
        Tuple of (GPG instance, GPG home directory)
        
    Note:
        Caller is responsible for cleaning up the GPG home directory
    """
    try:
        import gnupg
    except ImportError:
        raise RuntimeError("python-gnupg library is not installed. Install it with: pip install python-gnupg")
    
    # Use temporary directory for GPG home
    gpg_home = tempfile.mkdtemp(prefix='gpg_')
    
    try:
        gpg = gnupg.GPG(gnupghome=gpg_home)
        gpg.encoding = 'utf-8'
        return gpg, gpg_home
    except Exception as e:
        # Cleanup on error
        try:
            shutil.rmtree(gpg_home, ignore_errors=True)
        except OSError as cleanup_error:
            logger.warning("Could not remove the temporary GPG home %s: %s",
                           gpg_home, cleanup_error)
        raise RuntimeError(f"Failed to initialize GPG: {e}")


def _validate_password(password: str, min_length: int = 8) -> None:
    """
    Validate password strength
    
    Args:
        password: Password to validate
        min_length: Minimum password length
        
    Raises:
        ValueError: If password is invalid
    """
    if not password:
        raise ValueError("Password cannot be empty")
    
    if len(password) < min_length:
        logger.warning(f"Password length is less than {min_length} characters")


def _validate_file_path(file_path: str, must_exist: bool = False) -> Path:
    """
    Validate and convert file path
    
    Args:
        file_path: Path to validate
        must_exist: Whether the file must exist
        
    Returns:
        Path object
        
    Raises:
        ValueError: If path is invalid
        FileNotFoundError: If file must exist but doesn't
    """
    if not file_path:
        raise ValueError("File path cannot be empty")
    
    path = Path(file_path)
    
    if must_exist and not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    return path


# ==================== GPG Encryption ====================

def encrypt_file_gpg(data: str, output_path: str, password: str, 
                     sign: bool = False, sign_password: Optional[str] = None) -> None:
    """
    Encrypt data to a file using GPG with symmetric encryption
    
    Args:
        data: String data to encrypt
        output_path: Path to output encrypted file
        password: Encryption password (passphrase)
        sign: Whether to sign the data before encryption (default: False)
        sign_password: Password for signing key (if different from encryption password)
    
    Raises:
        RuntimeError: If encryption fails
        ValueError: If inputs are invalid
    
    Note:
        Uses AES256 cipher with SHA512 digest for maximum security.
        GPG home directory is automatically cleaned up.
    """
    _validate_password(password)
    output_path_obj = _validate_file_path(output_path)
    
    if not data:
        raise ValueError("Data to encrypt cannot be empty")
    
    gpg = None
    gpg_home = None
    
    try:
        gpg, gpg_home = _get_gpg_instance()
        
        # Configure encryption with maximum security parameters
        extra_args = [
            '--cipher-algo', 'AES256',
            '--digest-algo', 'SHA512',
            '--cert-digest-algo', 'SHA512',
            '--compress-algo', 'ZLIB',
            '--s2k-mode', '3',  # Iterated and salted S2K
            '--s2k-digest-algo', 'SHA512',
            '--s2k-count', '65011712',  # Maximum iteration count for key derivation
            '--force-mdc',  # Force modification detection code
        ]
        
        if sign:
            logger.warning("Signing requires GPG key pair to be configured. "
                         "Proceeding with encryption only.")
        
        # Encrypt with symmetric cipher (password-based)
        encrypted_data = gpg.encrypt(
            data,
            recipients=None,  # Required for symmetric encryption
            symmetric='AES256',
            passphrase=password,
            armor=True,  # ASCII armored output
            extra_args=extra_args
        )
        
        if not encrypted_data.ok:
            raise RuntimeError(f"GPG encryption failed: {encrypted_data.status}")
        
        # Ensure output directory exists
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)

        # Write encrypted data to file
        with open(output_path_obj, 'w', encoding='utf-8') as f:
            f.write(str(encrypted_data))
        
        logger.info(f"GPG encrypted file saved to: {output_path}")
        
    except Exception as e:
        logger.exception("Error during GPG encryption")
        raise RuntimeError(f"GPG encryption failed: {str(e)}")
    
    finally:
        # Cleanup GPG home directory
        if gpg_home:
            try:
                shutil.rmtree(gpg_home, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to cleanup GPG home: {e}")


def decrypt_file_gpg(input_path: str, password: str) -> str:
    """
    Decrypt a GPG encrypted file
    
    Args:
        input_path: Path to encrypted file
        password: Decryption password (passphrase)
        
    Returns:
        Decrypted string data
        
    Raises:
        RuntimeError: If decryption fails
        ValueError: If inputs are invalid
        FileNotFoundError: If input file doesn't exist
    """
    _validate_password(password)
    input_path_obj = _validate_file_path(input_path, must_exist=True)
    
    gpg = None
    gpg_home = None
    
    try:
        gpg, gpg_home = _get_gpg_instance()
        
        # Read encrypted data
        with open(input_path_obj, 'r', encoding='utf-8') as f:
            encrypted_data = f.read()
        
        if not encrypted_data:
            raise ValueError("Encrypted file is empty")
        
        # Decrypt with passphrase
        decrypted_data = gpg.decrypt(
            encrypted_data,
            passphrase=password,
            extra_args=[
                '--cipher-algo', 'AES256',
                '--digest-algo', 'SHA512',
            ]
        )
        
        if not decrypted_data.ok:
            logger.error(f"GPG decryption failed: {decrypted_data.status}")
            raise RuntimeError("GPG decryption failed - incorrect password or corrupted file")
        
        logger.info(f"GPG decrypted file: {input_path}")
        
        return str(decrypted_data)
        
    except Exception as e:
        logger.exception("Error during GPG decryption")
        if "incorrect password" in str(e).lower() or "bad passphrase" in str(e).lower():
            raise RuntimeError("GPG decryption failed - incorrect password")
        raise RuntimeError(f"GPG decryption failed: {str(e)}")
    
    finally:
        # Cleanup GPG home directory
        if gpg_home:
            try:
                shutil.rmtree(gpg_home, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to cleanup GPG home: {e}")


# ==================== AES-GCM Encryption ====================

def _derive_key_aes(password: str, salt: bytes) -> bytes:
    """
    Derive encryption key from password using Argon2, Scrypt, or PBKDF2
    
    Args:
        password: Password to derive key from
        salt: Salt for key derivation (16 bytes recommended)
        
    Returns:
        Derived 256-bit (32 byte) key
        
    Note:
        Tries Argon2 first (best), falls back to Scrypt, then PBKDF2
    """
    try:
        # Try Argon2 first (most secure)
        from Crypto.Protocol.KDF import Argon2
        
        key = Argon2(
            password=password.encode('utf-8'),
            salt=salt,
            dkLen=32,  # 256-bit key
            n=2,  # Number of iterations
            r=8,  # Block size
            p=1,  # Parallelization
        )
        logger.debug("Using Argon2 for key derivation")
        return key
        
    except (ImportError, AttributeError):
        logger.debug("Argon2 not available, falling back to Scrypt")
        
        try:
            # Fall back to Scrypt (good security)
            from Crypto.Protocol.KDF import scrypt
            
            key = scrypt(
                password=password.encode('utf-8'),
                salt=salt,
                key_len=32,  # 256-bit key
                N=2**14,  # CPU/memory cost (16384)
                r=8,  # Block size
                p=1,  # Parallelization
            )
            logger.debug("Using Scrypt for key derivation")
            return key
            
        except (ImportError, AttributeError):
            logger.debug("Scrypt not available, falling back to PBKDF2")
            
            # Last resort: PBKDF2 (acceptable but slower)
            from Crypto.Protocol.KDF import PBKDF2
            from Crypto.Hash import SHA256
            
            key = PBKDF2(
                password=password.encode('utf-8'),
                salt=salt,
                dkLen=32,  # 256-bit key
                count=100000,  # Iterations
                hmac_hash_module=SHA256
            )
            logger.debug("Using PBKDF2 for key derivation")
            return key


def encrypt_file_aes_gcm(data: str, output_path: str, password: str) -> None:
    """
    Encrypt data to a file using AES-GCM with maximum security
    
    Args:
        data: String data to encrypt
        output_path: Path to output encrypted file
        password: Encryption password
        
    Raises:
        RuntimeError: If encryption fails
        ValueError: If inputs are invalid
    
    Note:
        Uses AES-256-GCM with 96-bit nonce (NIST recommendation).
        Key is derived using Argon2/Scrypt/PBKDF2 with random salt.
        Output format is JSON with base64-encoded components.
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
    except ImportError:
        raise RuntimeError("PyCryptodome library is not installed. Install it with: pip install pycryptodome")
    
    _validate_password(password)
    output_path_obj = _validate_file_path(output_path)
    
    if not data:
        raise ValueError("Data to encrypt cannot be empty")
    
    try:
        # Generate random salt for key derivation (16 bytes)
        salt = get_random_bytes(16)
        
        # Derive encryption key from password
        key = _derive_key_aes(password, salt)
        
        # Generate random 96-bit nonce (NIST recommendation for GCM)
        nonce = get_random_bytes(12)
        
        # Create cipher
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        
        # Encrypt data
        data_bytes = data.encode('utf-8')
        ciphertext, tag = cipher.encrypt_and_digest(data_bytes)
        
        # Create output structure with all components
        output_data = {
            "version": "1.0",
            "algorithm": "AES-256-GCM",
            "kdf": "auto",  # Argon2/Scrypt/PBKDF2
            "salt": base64.b64encode(salt).decode('ascii'),
            "nonce": base64.b64encode(nonce).decode('ascii'),
            "tag": base64.b64encode(tag).decode('ascii'),
            "ciphertext": base64.b64encode(ciphertext).decode('ascii')
        }
        
        # Ensure output directory exists
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to file as JSON
        with open(output_path_obj, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2)
        
        logger.info(f"AES-GCM encrypted file saved to: {output_path}")
        
    except Exception as e:
        logger.exception("Error during AES-GCM encryption")
        raise RuntimeError(f"AES-GCM encryption failed: {str(e)}")


def decrypt_file_aes_gcm(input_path: str, password: str) -> str:
    """
    Decrypt an AES-GCM encrypted file
    
    Args:
        input_path: Path to encrypted file
        password: Decryption password
        
    Returns:
        Decrypted string data
        
    Raises:
        RuntimeError: If decryption fails
        ValueError: If inputs are invalid or file format is invalid
        FileNotFoundError: If input file doesn't exist
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError("PyCryptodome library is not installed. Install it with: pip install pycryptodome")
    
    _validate_password(password)
    input_path_obj = _validate_file_path(input_path, must_exist=True)
    
    try:
        # Read encrypted file
        with open(input_path_obj, 'r', encoding='utf-8') as f:
            encrypted_data = json.load(f)
        
        # Validate file format
        required_fields = ["version", "algorithm", "salt", "nonce", "tag", "ciphertext"]
        for field in required_fields:
            if field not in encrypted_data:
                raise ValueError(f"Invalid encrypted file format: missing '{field}' field")
        
        # Check algorithm
        if encrypted_data["algorithm"] != "AES-256-GCM":
            raise ValueError(f"Unsupported encryption algorithm: {encrypted_data['algorithm']}")
        
        # Decode components
        salt = base64.b64decode(encrypted_data["salt"])
        nonce = base64.b64decode(encrypted_data["nonce"])
        tag = base64.b64decode(encrypted_data["tag"])
        ciphertext = base64.b64decode(encrypted_data["ciphertext"])
        
        # Validate component sizes
        if len(salt) != 16:
            raise ValueError(f"Invalid salt size: expected 16 bytes, got {len(salt)}")
        if len(nonce) != 12:
            raise ValueError(f"Invalid nonce size: expected 12 bytes, got {len(nonce)}")
        if len(tag) != 16:
            raise ValueError(f"Invalid tag size: expected 16 bytes, got {len(tag)}")
        
        # Derive key from password
        key = _derive_key_aes(password, salt)
        
        # Create cipher
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        
        # Decrypt and verify
        try:
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError as e:
            # This typically means wrong password or corrupted data
            raise RuntimeError("AES-GCM decryption failed - incorrect password or corrupted file")
        
        # Decode to string
        decrypted_text = plaintext.decode('utf-8')
        
        logger.info(f"AES-GCM decrypted file: {input_path}")
        
        return decrypted_text
        
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format in encrypted file: {e}")
        raise ValueError("Invalid encrypted file format - not a valid JSON file")

    except UnicodeDecodeError as e:
        # UnicodeDecodeError is a subclass of ValueError, so it must be handled
        # BEFORE the clause below - a binary file is not a "format" error the
        # caller can act on, it is simply not an encrypted text file.
        logger.error(f"Encrypted file is not valid UTF-8 text: {e}")
        raise RuntimeError(
            "AES-GCM decryption failed: the file is not a valid encrypted "
            "text file"
        )

    except ValueError:
        # A malformed file (missing field, unsupported algorithm, wrong
        # component size) is documented as ValueError. The catch-all below used
        # to swallow it and re-raise a RuntimeError, so callers that handle the
        # documented type never saw it.
        raise

    except RuntimeError:
        # The inner handler already produced the precise message
        # ("incorrect password or corrupted file"). Re-wrapping it here matched
        # on "incorrect password" and dropped the "or corrupted file" half, so
        # a tampered file was always blamed on the user's password.
        raise

    except Exception as e:
        logger.exception("Error during AES-GCM decryption")
        raise RuntimeError(f"AES-GCM decryption failed: {str(e)}")


# ==================== Unified Interface ====================

def encrypt_file(data: str, output_path: str, password: str, 
                method: EncryptionMethod = "aes-gcm") -> None:
    """
    Encrypt data to a file using specified method
    
    Args:
        data: String data to encrypt
        output_path: Path to output encrypted file
        password: Encryption password
        method: Encryption method ("gpg" or "aes-gcm")
        
    Raises:
        RuntimeError: If encryption fails or method not available
        ValueError: If inputs are invalid or method is unknown
    """
    if method == "gpg":
        if not check_gpg_available():
            raise RuntimeError("GPG encryption is not available. "
                             "Install GnuPG and python-gnupg library.")
        encrypt_file_gpg(data, output_path, password)
        
    elif method == "aes-gcm":
        if not check_aes_available():
            raise RuntimeError("AES-GCM encryption is not available. "
                             "Install PyCryptodome library: pip install pycryptodome")
        encrypt_file_aes_gcm(data, output_path, password)
        
    else:
        raise ValueError(f"Unknown encryption method: {method}. "
                        "Supported methods: 'gpg', 'aes-gcm'")


def decrypt_file(input_path: str, password: str, 
                method: Optional[EncryptionMethod] = None) -> str:
    """
    Decrypt a file using specified or auto-detected method
    
    Args:
        input_path: Path to encrypted file
        password: Decryption password
        method: Encryption method ("gpg" or "aes-gcm"), or None to auto-detect
        
    Returns:
        Decrypted string data
        
    Raises:
        RuntimeError: If decryption fails or method not available
        ValueError: If inputs are invalid or method is unknown
        FileNotFoundError: If input file doesn't exist
    """
    input_path_obj = _validate_file_path(input_path, must_exist=True)
    
    # Auto-detect method if not specified
    if method is None:
        method = detect_encryption_method(input_path)
        logger.info(f"Auto-detected encryption method: {method}")
    
    if method == "gpg":
        if not check_gpg_available():
            raise RuntimeError("GPG decryption is not available. "
                             "Install GnuPG and python-gnupg library.")
        return decrypt_file_gpg(input_path, password)
        
    elif method == "aes-gcm":
        if not check_aes_available():
            raise RuntimeError("AES-GCM decryption is not available. "
                             "Install PyCryptodome library: pip install pycryptodome")
        return decrypt_file_aes_gcm(input_path, password)
        
    else:
        raise ValueError(f"Unknown encryption method: {method}. "
                        "Supported methods: 'gpg', 'aes-gcm'")


def detect_encryption_method(file_path: str) -> EncryptionMethod:
    """
    Auto-detect encryption method from file content
    
    Args:
        file_path: Path to encrypted file
        
    Returns:
        Detected encryption method
        
    Raises:
        ValueError: If method cannot be detected
        FileNotFoundError: If file doesn't exist
    """
    file_path_obj = _validate_file_path(file_path, must_exist=True)
    
    try:
        # Read first few bytes to detect format
        with open(file_path_obj, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        
        # GPG files start with -----BEGIN PGP MESSAGE-----
        if first_line.startswith("-----BEGIN PGP"):
            return "gpg"
        
        # AES-GCM files are JSON
        if first_line.startswith("{"):
            try:
                with open(file_path_obj, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if "algorithm" in data and "AES" in data.get("algorithm", ""):
                    return "aes-gcm"
            except json.JSONDecodeError:
                pass
        
        # A plaintext JSON document is the most common wrong file to pick:
        # it looks like an export, but this source only reads ENCRYPTED files.
        if first_line.startswith("{") or first_line.startswith("["):
            raise ValueError(
                "This looks like a plain, unencrypted JSON file. Only encrypted "
                "files can be loaded here - export the data again with "
                "'Export to Encrypted JSON' on the Operations tab."
            )

        raise ValueError("Cannot detect encryption method from file format")
        
    except Exception as e:
        logger.error(f"Failed to detect encryption method: {e}")
        raise ValueError(f"Cannot detect encryption method: {str(e)}")


def get_encryption_info(file_path: str) -> Dict[str, Any]:
    """
    Get information about an encrypted file
    
    Args:
        file_path: Path to encrypted file
        
    Returns:
        Dictionary with encryption information (method, algorithm, version, etc.)
        
    Raises:
        ValueError: If file format is invalid
        FileNotFoundError: If file doesn't exist
    """
    file_path_obj = _validate_file_path(file_path, must_exist=True)
    
    try:
        method = detect_encryption_method(file_path)
        
        info = {
            "method": method,
            "file_size": file_path_obj.stat().st_size
        }
        
        if method == "aes-gcm":
            with open(file_path_obj, 'r', encoding='utf-8') as f:
                data = json.load(f)
            info.update({
                "algorithm": data.get("algorithm"),
                "version": data.get("version"),
                "kdf": data.get("kdf")
            })
        elif method == "gpg":
            info["algorithm"] = "GPG/OpenPGP"
        
        return info
        
    except Exception as e:
        logger.error(f"Failed to get encryption info: {e}")
        raise ValueError(f"Cannot read encryption info: {str(e)}")

def encrypt_file_with_format(data: str, output_path: str, password: str,
                             encryption_method: str, file_format: str = 'standard',
                             metadata: Dict[str, Any] = None) -> None:
    """
    Encrypt data to file with specified format
    
    Args:
        data: String data to encrypt
        output_path: Output file path
        password: Encryption password
        encryption_method: 'aes-gcm' or 'gpg'
        file_format: 'standard' or 'usrx'
        metadata: Optional metadata for USRX format
        
    Raises:
        RuntimeError: If encryption fails
        ValueError: If parameters are invalid
    """
    try:
        if file_format == 'usrx':
            # USRX format - create container
            _encrypt_to_usrx(data, output_path, password, encryption_method, metadata)
        else:
            # Standard format - use existing functions
            if encryption_method == 'aes-gcm':
                encrypt_file_aes_gcm(data, output_path, password)
            elif encryption_method == 'gpg':
                encrypt_file_gpg(data, output_path, password)
            else:
                raise ValueError(f"Unsupported encryption method: {encryption_method}")
                
        logger.info(f"Encrypted file: {output_path} ({encryption_method}, {file_format})")
        
    except Exception as e:
        logger.exception("Error encrypting file")
        raise


def decrypt_file_with_format(input_path: str, password: str,
                             encryption_method: Optional[str] = None,
                             file_format: Optional[str] = None) -> str:
    """
    Decrypt file with auto-detection or specified format
    
    Args:
        input_path: Input file path
        password: Decryption password
        encryption_method: Optional encryption method (auto-detect if None)
        file_format: Optional file format (auto-detect if None)
        
    Returns:
        Decrypted string data
        
    Raises:
        RuntimeError: If decryption fails
        ValueError: If file format is invalid
    """
    try:
        # Auto-detect format if not specified
        if file_format is None:
            if is_usrx_file(input_path):
                file_format = 'usrx'
            else:
                file_format = 'standard'
        
        logger.info(f"Decrypting file: {input_path} (format: {file_format})")
        
        if file_format == 'usrx':
            # USRX format
            return _decrypt_from_usrx(input_path, password, encryption_method)
        else:
            # Standard format - use existing functions
            if encryption_method is None:
                # Try to detect
                from utils.encryption import detect_encryption_method
                encryption_method = detect_encryption_method(input_path)
            
            if encryption_method == 'aes-gcm':
                return decrypt_file_aes_gcm(input_path, password)
            elif encryption_method == 'gpg':
                return decrypt_file_gpg(input_path, password)
            else:
                raise ValueError(f"Unsupported encryption method: {encryption_method}")
                
    except Exception as e:
        logger.exception("Error decrypting file")
        raise


def _encrypt_to_usrx(data: str, output_path: str, password: str,
                     encryption_method: str, metadata: Dict[str, Any] = None):
    """
    Encrypt data and write it into a USRX container.

    The same input validation the standard-format path performs is applied
    here. Without it the container path happily accepted an empty password and
    an empty document, so a user who left the password field blank got a file
    that looked encrypted but was protected by nothing.
    """
    _validate_password(password)

    if not data:
        raise ValueError("Data to encrypt cannot be empty")

    # Never mutate the caller's dictionary
    metadata = dict(metadata) if metadata else {}

    if encryption_method == 'aes-gcm':
        # For AES-GCM, we need to store the encryption parameters
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        from utils.encryption import _derive_key_aes
        
        # Generate salt and nonce
        salt = get_random_bytes(16)
        nonce = get_random_bytes(12)
        
        # Derive key
        key = _derive_key_aes(password, salt)
        
        # Encrypt
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        data_bytes = data.encode('utf-8')
        ciphertext, tag = cipher.encrypt_and_digest(data_bytes)
        
        # Create metadata with AES-GCM parameters
        metadata.update({
            'algorithm': 'AES-256-GCM',
            'kdf': 'auto',
            'salt': base64.b64encode(salt).decode('ascii'),
            'nonce': base64.b64encode(nonce).decode('ascii'),
            'tag': base64.b64encode(tag).decode('ascii')
        })
        
        # Write to USRX
        write_usrx_file(output_path, ciphertext, 'aes-gcm', metadata)
        
    elif encryption_method == 'gpg':
        # For GPG, encrypt first then store
        import tempfile
        import os
        
        # Encrypt to temporary file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.gpg') as tmp:
            tmp_path = tmp.name
        
        try:
            encrypt_file_gpg(data, tmp_path, password)
            
            # Read encrypted data
            with open(tmp_path, 'rb') as f:
                encrypted_data = f.read()
            
            # Write to USRX
            if metadata is None:
                metadata = {}
            metadata['algorithm'] = 'GPG/OpenPGP'
            
            write_usrx_file(output_path, encrypted_data, 'gpg', metadata)
            
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as exc:
                    logger.warning("Could not delete the temporary file %s: %s",
                                   tmp_path, exc)
    else:
        raise ValueError(f"Unsupported encryption method: {encryption_method}")


def _decrypt_from_usrx(input_path: str, password: str,
                       encryption_method: Optional[str] = None) -> str:
    """Decrypt data from USRX container"""
    # Read USRX file
    data, detected_method, metadata = read_usrx_file(input_path)
    
    # Use detected method if not specified
    if encryption_method is None:
        encryption_method = detected_method
    
    logger.info(f"Decrypting USRX: method={encryption_method}")
    
    if encryption_method == 'aes-gcm':
        # Extract AES-GCM parameters from metadata
        from Crypto.Cipher import AES
        from utils.encryption import _derive_key_aes

        # Direct indexing raised a bare KeyError for a truncated or foreign
        # container, which reached the user as "KeyError: 'salt'".
        missing = [key for key in ('salt', 'nonce', 'tag') if key not in metadata]
        if missing:
            raise RuntimeError(
                "The USRX file is damaged or was not written by this "
                f"application - missing encryption parameter(s): {', '.join(missing)}"
            )

        try:
            # validate=True makes base64 reject garbage instead of silently
            # skipping the characters it does not understand - which produced
            # an empty nonce and the baffling "Nonce cannot be empty" from
            # PyCryptodome further down.
            salt = base64.b64decode(metadata['salt'], validate=True)
            nonce = base64.b64decode(metadata['nonce'], validate=True)
            tag = base64.b64decode(metadata['tag'], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise RuntimeError(
                f"The USRX file contains invalid encryption parameters: {exc}"
            )

        if not salt or not nonce or not tag:
            raise RuntimeError(
                "The USRX file contains empty encryption parameters - "
                "the container is damaged"
            )
        
        # Derive key
        key = _derive_key_aes(password, salt)
        
        # Decrypt
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        
        try:
            plaintext = cipher.decrypt_and_verify(data, tag)
        except ValueError:
            raise RuntimeError("Decryption failed - incorrect password or corrupted file")
        
        return plaintext.decode('utf-8')
        
    elif encryption_method == 'gpg':
        # Write to temporary file and decrypt
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.gpg') as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        
        try:
            decrypted = decrypt_file_gpg(tmp_path, password)
            return decrypted
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as exc:
                    logger.warning("Could not delete the temporary file %s: %s",
                                   tmp_path, exc)
    else:
        raise ValueError(f"Unsupported encryption method: {encryption_method}")


def get_file_info(file_path: str) -> Dict[str, Any]:
    """
    Get information about encrypted file
    
    Args:
        file_path: File path
        
    Returns:
        Dictionary with file information
    """
    try:
        # Check if USRX
        if is_usrx_file(file_path):
            info = get_usrx_info(file_path)
            info['format'] = 'usrx'
            return info
        else:
            # Try standard detection
            from utils.encryption import get_encryption_info
            info = get_encryption_info(file_path)
            info['format'] = 'standard'
            return info
    except Exception as e:
        logger.debug(f"Error getting file info: {e}")
        return {'format': 'unknown', 'error': str(e)}


def detect_file_format(file_path: str) -> str:
    """
    Detect file format
    
    Args:
        file_path: File path
        
    Returns:
        'usrx' or 'standard'
    """
    if is_usrx_file(file_path):
        return 'usrx'
    return 'standard'