# tools/token_store.py

import os
import json
# Remove: from cryptography.fernet import Fernet # No longer needed here
from tools.encryption import decrypt_data # <-- IMPORT THIS
from tools.logger import log_info, log_error, log_warning # <-- Added log_error
from tools.encryption import decrypt_data, encrypt_data

TOKEN_DIR = "data" # Keep this if already defined

# Remove or comment out: ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY") # No longer needed here

def get_user_token(user_id: str) -> dict | None: # <-- Return type changed to dict | None
    """Loads and decrypts the user's token from the encrypted file."""
    path = os.path.join(TOKEN_DIR, f"tokens_{user_id}.json.enc")
    if not os.path.exists(path):
        # This is not necessarily an error, just means no token yet.
        log_info("token_store", "get_user_token", f"No token file found for user {user_id} at {path}")
        return None # <-- Return None if file not found

    try:
        with open(path, "rb") as f:
            encrypted_data = f.read()

        # Use the new decrypt function
        token_data = decrypt_data(encrypted_data) # <-- USE THIS

        if token_data:
            # Log a summary of the token data without exposing full details.
            log_info("token_store", "get_user_token", f"Decrypted token data keys for user {user_id}: {list(token_data.keys())}")
            return token_data
        else:
            # Decryption failed (logged within decrypt_data)
            log_error("token_store", "get_user_token", f"Failed to decrypt token for user {user_id} from {path}.")
            # Consider deleting the corrupted file? os.remove(path)
            return None

    except FileNotFoundError:
         log_info("token_store", "get_user_token", f"Token file not found for user {user_id} at {path} (race condition?).")
         return None
    except Exception as e:
        # Catch any other unexpected errors during file reading etc.
        log_error("token_store", "get_user_token", f"Unexpected error loading token for user {user_id} from {path}: {str(e)}", e)
        return None


def save_user_token_encrypted(user_id: str, token_data: dict) -> bool:
    """
    Encrypts and saves the user's token data to a file using atomic write.

    Args:
        user_id: The user's identifier.
        token_data: The dictionary containing token information. It expects keys like
                    'access_token', 'refresh_token', 'token_uri', etc. It handles if
                    the access token key is 'token' coming from the Credentials object.

    Returns:
        True if saving was successful, False otherwise.
    """
    log_info("token_store", "save_user_token_encrypted", f"Attempting to save token for user {user_id}")
    path = os.path.join(TOKEN_DIR, f"tokens_{user_id}.json.enc")
    temp_path = path + ".tmp" # Temporary file path

    try:
        # Standardize access token key to 'access_token' before saving
        data_to_save = token_data.copy()
        if 'token' in data_to_save and 'access_token' not in data_to_save:
            data_to_save['access_token'] = data_to_save.pop('token')

        # Basic validation: Check if critical tokens are present AFTER standardization
        if not data_to_save.get('access_token'):
             log_error("token_store", "save_user_token_encrypted", f"Attempted to save token data missing access_token for {user_id}.")
             return False
        if not data_to_save.get('refresh_token'):
             # Allow saving even if refresh token is missing, but log warning.
             log_warning("token_store", "save_user_token_encrypted", f"Saving token data potentially missing refresh_token for {user_id}.")

        # Encrypt the standardized data
        encrypted_tokens = encrypt_data(data_to_save)
        if not encrypted_tokens:
            # Error already logged by encrypt_data
            log_error("token_store", "save_user_token_encrypted", f"Encryption failed for {user_id}'s tokens during save.")
            return False

        # Ensure directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Write to temporary file first
        with open(temp_path, "wb") as f:
            f.write(encrypted_tokens)

        # Atomically replace original file with temp file
        os.replace(temp_path, path)

        log_info("token_store", "save_user_token_encrypted", f"Token stored successfully for {user_id} at {path}.")
        return True

    except Exception as e:
        log_error("token_store", "save_user_token_encrypted", f"Failed to write token file {path}", e)
        # Clean up temp file if it exists after an error
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError as rm_err: log_error("...", f"Failed to remove temp file {temp_path}: {rm_err}")
        return False