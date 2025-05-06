# --- START OF FULL tools/token_store.py ---

import os
import json
from dotenv import load_dotenv # Added for .env loading
from tools.encryption import decrypt_data, encrypt_data # Ensure both are imported
from tools.logger import log_info, log_error, log_warning

# --- Load Environment Variables ---
# Load variables from .env file in the current directory or parent directories
load_dotenv()

# --- Configuration ---
# Get the data suffix (_cli or empty) from environment variables
DATA_SUFFIX = os.getenv("DATA_SUFFIX", "") # Default to empty string if not set

# Define the specific subdirectory for tokens
TOKEN_BASE_DIR = "data"
TOKEN_SUB_DIR = "tokens" # New subdirectory name
TOKEN_DIR_PATH = os.path.join(TOKEN_BASE_DIR, TOKEN_SUB_DIR)

# --- Helper Function to Construct Full Path ---
def _get_token_path(user_id: str) -> str:
    """Constructs the absolute path for a user's token file including the suffix."""
    # Construct filename with suffix: e.g., tokens_1234_cli.json.enc or tokens_1234.json.enc
    filename = f"tokens_{user_id}{DATA_SUFFIX}.json.enc"
    # Construct the relative path including the subdirectory
    relative_path = os.path.join(TOKEN_DIR_PATH, filename)
    # Return the absolute path
    return os.path.abspath(relative_path)

# --- Core Functions ---

def get_user_token(user_id: str) -> dict | None:
    """Loads and decrypts the user's token from the encrypted file in data/tokens."""
    fn_name = "get_user_token"
    absolute_path = _get_token_path(user_id) # Get the full path

    # Log the path being checked
    log_info("token_store", fn_name, f"Attempting to load token from: '{absolute_path}'")

    # Check if the file exists
    if not os.path.exists(absolute_path):
        log_info("token_store", fn_name, f"Token file not found for user {user_id} at '{absolute_path}'.")
        return None

    # Proceed if file exists
    log_info("token_store", fn_name, f"Token file found at '{absolute_path}'. Attempting to read and decrypt.")
    try:
        with open(absolute_path, "rb") as f:
            encrypted_data = f.read()

        # Decrypt the data
        token_data = decrypt_data(encrypted_data) # Assumes decrypt_data returns dict or None

        if token_data:
            log_info("token_store", fn_name, f"Successfully decrypted token data for user {user_id}. Keys: {list(token_data.keys())}")
            return token_data
        else:
            # decrypt_data should log its own errors
            log_error("token_store", fn_name, f"Decryption failed for token file: '{absolute_path}'. Check encryption logs.")
            return None

    except FileNotFoundError:
         # Should ideally not happen after os.path.exists, but handles race conditions
         log_info("token_store", fn_name, f"Token file disappeared before reading for user {user_id} at '{absolute_path}'.")
         return None
    except PermissionError as pe:
         log_error("token_store", fn_name, f"Permission denied reading token file '{absolute_path}'", pe)
         return None
    except Exception as e:
        log_error("token_store", fn_name, f"Unexpected error loading token from '{absolute_path}'", e)
        return None


def save_user_token_encrypted(user_id: str, token_data: dict) -> bool:
    """
    Encrypts and saves the user's token data to a file in data/tokens using atomic write.
    """
    fn_name = "save_user_token_encrypted"
    path = _get_token_path(user_id) # Get the full target path
    temp_path = path + ".tmp"      # Temporary file for atomic write

    log_info("token_store", fn_name, f"Attempting to save encrypted token for user {user_id} to '{path}'")

    try:
        # --- Data Validation ---
        # Ensure necessary tokens are present
        data_to_save = token_data.copy()
        # Handle potential 'token' key from oauth callback -> 'access_token'
        if 'token' in data_to_save and 'access_token' not in data_to_save:
            data_to_save['access_token'] = data_to_save.pop('token')

        if not data_to_save.get('access_token'):
            log_error("token_store", fn_name, f"Cannot save token: Missing 'access_token' for user {user_id}.")
            return False
        # It's critical for Google OAuth to have a refresh token for offline access
        if not data_to_save.get('refresh_token'):
            log_warning("token_store", fn_name, f"Saving token data missing 'refresh_token' for user {user_id}. Offline access/refresh will fail.")
        # -----------------------

        # Encrypt the validated data
        encrypted_tokens = encrypt_data(data_to_save)
        if not encrypted_tokens:
            # encrypt_data should log its own errors
            log_error("token_store", fn_name, f"Encryption failed for user {user_id}'s tokens during save.")
            return False

        # --- Atomic Write ---
        # Ensure the target directory exists (data/tokens/)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Write to temporary file first
        with open(temp_path, "wb") as f:
            f.write(encrypted_tokens)

        # Atomically replace the old file with the new one
        os.replace(temp_path, path)
        # --------------------

        log_info("token_store", fn_name, f"Token stored successfully for {user_id} at '{path}'.")
        return True

    except PermissionError as pe:
        log_error("token_store", fn_name, f"Permission denied writing token file to '{path}' or temp file '{temp_path}'.", pe)
        return False
    except Exception as e:
        log_error("token_store", fn_name, f"Unexpected error saving token file to '{path}'", e)
        # Clean up temporary file if it exists after an error
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                log_info("token_store", fn_name, f"Removed temporary token file '{temp_path}' after error.")
            except OSError as rm_err:
                log_error("token_store", fn_name, f"Failed to remove temporary token file '{temp_path}' after error: {rm_err}")
        return False

# --- END OF FULL tools/token_store.py ---