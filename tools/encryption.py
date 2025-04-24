# tools/encryption.py

import os
import json
from cryptography.fernet import Fernet, InvalidToken
from tools.logger import log_error, log_info

# --- Key Management ---
# Load the encryption key from environment variables
# CRITICAL: This key MUST be kept secret and secure.
# Generate one using generate_key() below and set it as an environment variable.
ENCRYPTION_KEY_ENV_VAR = "ENCRYPTION_KEY"
_encryption_key = os.getenv(ENCRYPTION_KEY_ENV_VAR)

if not _encryption_key:
    log_error("encryption", "__init__",
              f"CRITICAL ERROR: Environment variable '{ENCRYPTION_KEY_ENV_VAR}' not set. Encryption disabled.")
    # You might want to raise an exception here to halt execution
    # raise ValueError(f"Environment variable '{ENCRYPTION_KEY_ENV_VAR}' is required for encryption.")
    _fernet = None
else:
    try:
        # Ensure the key is bytes
        _key_bytes = _encryption_key.encode('utf-8')
        _fernet = Fernet(_key_bytes)
        log_info("encryption", "__init__", "Fernet encryption service initialized successfully.")
    except Exception as e:
        log_error("encryption", "__init__", f"Failed to initialize Fernet. Invalid key format? Error: {e}", e)
        _fernet = None
        # raise ValueError(f"Invalid encryption key format: {e}") # Optional: Halt execution

# --- Encryption/Decryption Functions ---

def encrypt_data(data: dict) -> bytes | None:
    """
    Encrypts a dictionary using Fernet.

    Args:
        data: The dictionary to encrypt.

    Returns:
        Encrypted bytes if successful, None otherwise.
    """
    if not _fernet:
        log_error("encryption", "encrypt_data", "Encryption service not available (key missing or invalid).")
        return None
    try:
        # Serialize the dictionary to a JSON string, then encode to bytes
        data_bytes = json.dumps(data).encode('utf-8')
        encrypted_data = _fernet.encrypt(data_bytes)
        return encrypted_data
    except Exception as e:
        log_error("encryption", "encrypt_data", f"Encryption failed: {e}", e)
        return None

def decrypt_data(encrypted_data: bytes) -> dict | None:
    """
    Decrypts data encrypted with Fernet back into a dictionary.

    Args:
        encrypted_data: The encrypted bytes to decrypt.

    Returns:
        The original dictionary if successful, None otherwise.
    """
    if not _fernet:
        log_error("encryption", "decrypt_data", "Encryption service not available (key missing or invalid).")
        return None
    try:
        decrypted_bytes = _fernet.decrypt(encrypted_data)
        # Decode bytes back to JSON string, then parse into dictionary
        decrypted_json = decrypted_bytes.decode('utf-8')
        original_data = json.loads(decrypted_json)
        return original_data
    except InvalidToken:
        log_error("encryption", "decrypt_data", "Decryption failed: Invalid token (key mismatch or data corrupted).")
        return None
    except Exception as e:
        log_error("encryption", "decrypt_data", f"Decryption failed: {e}", e)
        return None

# --- Key Generation Utility ---

def generate_key() -> str:
    """Generates a new Fernet key (URL-safe base64 encoded)."""
    return Fernet.generate_key().decode('utf-8')

# Example usage for generating a key (run this file directly: python -m tools.encryption)
if __name__ == "__main__":
    new_key = generate_key()
    print("Generated Fernet Key (set this as your ENCRYPTION_KEY environment variable):")
    print(new_key)
    print("\nWARNING: Keep this key secure and secret!")