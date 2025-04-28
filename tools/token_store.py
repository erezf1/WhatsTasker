# --- START OF FULL tools/token_store.py ---

import os
import json
from tools.encryption import decrypt_data
from tools.logger import log_info, log_error, log_warning

TOKEN_DIR = "data"

def get_user_token(user_id: str) -> dict | None:
    """Loads and decrypts the user's token from the encrypted file."""
    fn_name = "get_user_token"
    relative_path = os.path.join(TOKEN_DIR, f"tokens_{user_id}.json.enc")

    try:
        cwd = os.getcwd()
        # --- Generate Absolute Path FIRST ---
        absolute_path = os.path.abspath(relative_path)
        # --- Log Both ---
        log_info("token_store", fn_name, f"DEBUG: Checking for token file. User: {user_id}, Relative Path: '{relative_path}', CWD: '{cwd}', Absolute Path Check: '{absolute_path}'")
    except Exception as debug_e:
        log_warning("token_store", fn_name, f"DEBUG: Error getting CWD/abspath: {debug_e}")
        absolute_path = relative_path # Fallback to relative if abspath fails

    # --- MODIFY: Check existence using the generated ABSOLUTE path ---
    file_exists = os.path.exists(absolute_path)
    # ------------------------------------------------------------------

    if not file_exists:
        log_info("token_store", fn_name, f"Result: os.path.exists returned FALSE for path '{absolute_path}'")
        return None

    log_info("token_store", fn_name, f"Result: os.path.exists returned TRUE for path '{absolute_path}'. Attempting to read.")

    try:
        # --- Use the absolute path to open the file too ---
        with open(absolute_path, "rb") as f:
        # -------------------------------------------------
            encrypted_data = f.read()

        token_data = decrypt_data(encrypted_data)

        if token_data:
            log_info("token_store", fn_name, f"Decrypted token data keys for user {user_id}: {list(token_data.keys())}")
            return token_data
        else:
            log_error("token_store", fn_name, f"Failed to decrypt token for user {user_id} from {absolute_path}.")
            return None

    except FileNotFoundError:
         # This shouldn't happen if os.path.exists was True, but good to keep
         log_info("token_store", fn_name, f"Token file not found for user {user_id} at {absolute_path} (race condition?).")
         return None
    except PermissionError as pe:
         log_error("token_store", fn_name, f"Permission denied reading token file for user {user_id} from {absolute_path}: {pe}", pe)
         return None
    except Exception as e:
        log_error("token_store", fn_name, f"Unexpected error loading token for user {user_id} from {absolute_path}: {str(e)}", e)
        return None


def save_user_token_encrypted(user_id: str, token_data: dict) -> bool:
    """
    Encrypts and saves the user's token data to a file using atomic write.
    """
    log_info("token_store", "save_user_token_encrypted", f"Attempting to save token for user {user_id}")
    # Use absolute path for saving too for consistency
    relative_path = os.path.join(TOKEN_DIR, f"tokens_{user_id}.json.enc")
    path = os.path.abspath(relative_path)
    temp_path = path + ".tmp"

    try:
        data_to_save = token_data.copy()
        if 'token' in data_to_save and 'access_token' not in data_to_save:
            data_to_save['access_token'] = data_to_save.pop('token')

        if not data_to_save.get('access_token'):
            log_error("token_store", "save_user_token_encrypted", f"Attempted to save token data missing access_token for {user_id}.")
            return False
        if not data_to_save.get('refresh_token'):
            log_warning("token_store", "save_user_token_encrypted", f"Saving token data potentially missing refresh_token for {user_id}.")

        from tools.encryption import encrypt_data
        encrypted_tokens = encrypt_data(data_to_save)
        if not encrypted_tokens:
            log_error("token_store", "save_user_token_encrypted", f"Encryption failed for {user_id}'s tokens during save.")
            return False

        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(temp_path, "wb") as f:
            f.write(encrypted_tokens)

        os.replace(temp_path, path)

        log_info("token_store", "save_user_token_encrypted", f"Token stored successfully for {user_id} at {path}.")
        return True

    except Exception as e:
        log_error("token_store", "save_user_token_encrypted", f"Failed to write token file {path}", e)
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError as rm_err: log_error("token_store", "save_user_token_encrypted", f"Failed to remove temp file {temp_path}: {rm_err}")
        return False
# --- END OF FULL tools/token_store.py ---