# --- START OF FULL tools/logger.py ---
import os
import pytz
from datetime import datetime, timezone
import traceback
import json

# --- Database Logging Import ---
# REMOVE the direct import attempt from here
# import tools.activity_db as activity_db # REMOVE THIS LINE
ACTIVITY_DB_IMPORTED = False # Assume not imported initially
_activity_db_log_func = None # Placeholder for the function

# Try to import ONLY the function we need, later, within the logging calls
try:
    # This import happens when the logger *module* is loaded.
    # We want to defer accessing the function until it's actually called.
    pass # We will import inside the function call instead
except ImportError:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'}] [ERROR] [logger:import_check] Failed initial check for activity_db module. DB logging disabled.")


# === Config ===
DEBUG_MODE = os.getenv("DEBUG_MODE", "True").lower() in ('true', '1', 't', 'yes', 'y')
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "whats_tasker.log")
LOG_TIMEZONE = pytz.utc

try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError as e:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'}] [ERROR] [logger:init] Failed to create log directory '{LOG_DIR}': {e}")

# === Helpers ===
def _timestamp():
    return datetime.now(LOG_TIMEZONE).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _format_log_entry(level: str, module: str, func: str, message: str):
    ts = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"[{ts}] [{level}] [{module}:{func}] {message}"

# === Log functions ===
def log_info(module: str, func: str, message: str):
    """Logs informational messages. Prints only if DEBUG_MODE is True."""
    if DEBUG_MODE:
        entry = _format_log_entry("INFO", module, func, message)
        print(entry)
    # Optionally write INFO to file log even in production?
    # else:
    #     try:
    #         with open(LOG_FILE, "a", encoding="utf-8") as f:
    #             f.write(entry + "\n")
    #     except Exception: pass # Avoid errors during info logging

def _try_log_to_db(level: str, module: str, function: str, message: str, traceback_str: str | None = None, user_id_context: str | None = None, timestamp: str | None = None):
    """Internal helper to attempt DB logging dynamically."""
    global _activity_db_log_func, ACTIVITY_DB_IMPORTED

    # Try to import and get the function only once if not already done
    if not ACTIVITY_DB_IMPORTED and _activity_db_log_func is None:
        try:
            from tools.activity_db import log_system_event
            _activity_db_log_func = log_system_event
            ACTIVITY_DB_IMPORTED = True
            # Use internal print as logger might not be fully ready
            print(f"[{_timestamp()}] [INFO] [logger:_try_log_to_db] Successfully linked activity_db.log_system_event.")
        except ImportError:
            # This is expected if called before activity_db is importable
            # print(f"[{_timestamp()}] [INFO] [logger:_try_log_to_db] activity_db not yet available for import.")
            _activity_db_log_func = None # Ensure it's None
            ACTIVITY_DB_IMPORTED = False # Ensure flag is false
        except Exception as e:
            # Catch other potential import errors
             print(f"[{_timestamp()}] [ERROR] [logger:_try_log_to_db] Unexpected error linking activity_db.log_system_event: {e}")
             _activity_db_log_func = None
             ACTIVITY_DB_IMPORTED = False


    # If the function is available, call it
    if _activity_db_log_func:
        try:
            _activity_db_log_func(
                level=level,
                module=module,
                function=function,
                message=message,
                traceback_str=traceback_str,
                user_id_context=user_id_context,
                timestamp=timestamp # Use provided or let DB func generate
            )
        except Exception as db_log_err:
             # Fallback to print if the DB call fails *after* import succeeded
             ts_iso = timestamp or _timestamp()
             print(f"[{ts_iso}] [CRITICAL DB LOG FAIL] [{level}] [{module}:{function}] DB log failed: {db_log_err} | Original Msg: {message}")
    else:
        # Fallback print if DB import failed or function link failed
        ts_iso = timestamp or _timestamp()
        print(f"[{ts_iso}] [DB_LOG_SKIP] [{level}] [{module}:{function}] {message}")


def log_error(module: str, func: str, message: str, exception: Exception = None, user_id: str | None = None):
    """Logs error messages. Prints/logs to file AND attempts to log to database."""
    level = "ERROR"
    ts_iso = _timestamp()
    traceback_str = None
    if exception:
        traceback_str = traceback.format_exc()

    # Log to Console/File
    entry = _format_log_entry(level, module, func, message)
    if DEBUG_MODE:
        print(entry)
        if traceback_str: print(traceback_str)
    else:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
                if traceback_str: f.write(traceback_str + "\n")
        except Exception as file_log_e:
            print(f"CRITICAL: Failed to write ERROR to log file {LOG_FILE}: {file_log_e}")

    # Attempt to log to Database
    _try_log_to_db(level, module, func, message, traceback_str, user_id, ts_iso)


def log_warning(module: str, func: str, message: str, exception: Exception = None, user_id: str | None = None):
    """Logs warning messages. Prints/logs to file AND attempts to log to database."""
    level = "WARNING"
    ts_iso = _timestamp()
    traceback_str = None
    if exception:
        traceback_str = traceback.format_exc()

    # Log to Console/File
    entry = _format_log_entry(level, module, func, message)
    if DEBUG_MODE:
        print(entry)
        if traceback_str and exception: print(f"Warning Exception Info:\n{traceback_str}")
    else:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
                if traceback_str and exception: f.write(f"Warning Exception Info:\n{traceback_str}\n")
        except Exception as file_log_e:
            print(f"CRITICAL: Failed to write WARNING to log file {LOG_FILE}: {file_log_e}")

    # Attempt to log to Database
    _try_log_to_db(level, module, func, message, traceback_str, user_id, ts_iso)

# --- END OF FULL tools/logger.py ---