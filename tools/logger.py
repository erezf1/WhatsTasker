# --- START OF FULL tools/logger.py ---
import os
import pytz # Make sure pytz is imported
from datetime import datetime, timezone # timezone from datetime is for UTC specifically
import traceback
import json

# --- Database Logging Import ---
ACTIVITY_DB_IMPORTED = False
_activity_db_log_func = None

try:
    pass
except ImportError:
    # Use direct print as logger isn't ready
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'}] [ERROR] [logger:import_check] Failed initial check for activity_db module. DB logging disabled.")


# === Config ===
DEBUG_MODE = os.getenv("DEBUG_MODE", "True").lower() in ('true', '1', 't', 'yes', 'y')
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "whats_tasker.log") # For file logging if DEBUG_MODE is False

# --- *** CHANGE THIS LINE *** ---
LOG_TIMEZONE_STR = "Asia/Jerusalem" # Timezone for general log timestamps
try:
    LOG_TIMEZONE_PYTZ = pytz.timezone(LOG_TIMEZONE_STR)
except pytz.UnknownTimeZoneError:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'}] [ERROR] [logger:init] Unknown LOG_TIMEZONE_STR '{LOG_TIMEZONE_STR}'. Defaulting to UTC.")
    LOG_TIMEZONE_PYTZ = pytz.utc
# --- *** END CHANGE *** ---


try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError as e:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'}] [ERROR] [logger:init] Failed to create log directory '{LOG_DIR}': {e}")

# === Helpers ===
def _timestamp_utc_iso(): # Renamed to clarify its UTC and ISO format
    """Returns current time in UTC ISO format for DB logging or specific needs."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def _format_log_entry(level: str, module: str, func: str, message: str):
    """Formats a log entry with the configured LOG_TIMEZONE_PYTZ."""
    # --- *** USES THE NEW LOG_TIMEZONE_PYTZ *** ---
    ts_aware = datetime.now(LOG_TIMEZONE_PYTZ)
    # Format: YYYY-MM-DD HH:MM:SS TZN (e.g., 2025-05-07 10:30:00 IDT)
    ts_formatted = ts_aware.strftime("%Y-%m-%d %H:%M:%S %Z")
    # --- *** END CHANGE *** ---
    return f"[{ts_formatted}] [{level.upper()}] [{module}:{func}] {message}"

# === Log functions ===
def log_info(module: str, func: str, message: str):
    """Logs informational messages. Prints only if DEBUG_MODE is True."""
    entry = _format_log_entry("INFO", module, func, message)
    if DEBUG_MODE:
        print(entry)
    # Optionally write INFO to file log even in production
    # else:
    #     try:
    #         with open(LOG_FILE, "a", encoding="utf-8") as f:
    #             f.write(entry + "\n")
    #     except Exception: pass


def _try_log_to_db(level: str, module: str, function: str, message: str, traceback_str: str | None = None, user_id_context: str | None = None, timestamp_utc_iso: str | None = None): # Ensure timestamp is UTC for DB
    """Internal helper to attempt DB logging dynamically. Timestamp for DB should be UTC."""
    global _activity_db_log_func, ACTIVITY_DB_IMPORTED

    if not ACTIVITY_DB_IMPORTED and _activity_db_log_func is None:
        try:
            from tools.activity_db import log_system_event
            _activity_db_log_func = log_system_event
            ACTIVITY_DB_IMPORTED = True
            print(f"[{_timestamp_utc_iso()}] [INFO] [logger:_try_log_to_db] Successfully linked activity_db.log_system_event.")
        except ImportError:
            _activity_db_log_func = None
            ACTIVITY_DB_IMPORTED = False
        except Exception as e:
             print(f"[{_timestamp_utc_iso()}] [ERROR] [logger:_try_log_to_db] Unexpected error linking activity_db.log_system_event: {e}")
             _activity_db_log_func = None
             ACTIVITY_DB_IMPORTED = False

    if _activity_db_log_func:
        try:
            # For DB logging, always use UTC ISO timestamp
            db_ts = timestamp_utc_iso or _timestamp_utc_iso()
            _activity_db_log_func(
                level=level.upper(),
                module=module,
                function=function,
                message=message,
                traceback_str=traceback_str,
                user_id_context=user_id_context,
                timestamp=db_ts
            )
        except Exception as db_log_err:
             ts_iso_fallback = timestamp_utc_iso or _timestamp_utc_iso()
             print(f"[{ts_iso_fallback}] [CRITICAL DB LOG FAIL] [{level.upper()}] [{module}:{function}] DB log failed: {db_log_err} | Original Msg: {message}")
    # else:
        # If DB logging is not available, we don't print a "DB_LOG_SKIP" here for every console log,
        # as console logs (INFO/ERROR/WARNING) will still print to console/file anyway.
        # The DB log attempt is silent if _activity_db_log_func is None.


def log_error(module: str, func: str, message: str, exception: Exception = None, user_id: str | None = None):
    """Logs error messages. Prints/logs to file AND attempts to log to database."""
    level = "ERROR"
    traceback_str = None
    if exception:
        traceback_str = traceback.format_exc()

    # Log to Console/File (uses LOG_TIMEZONE_PYTZ via _format_log_entry)
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
            # Use direct print for critical file log failure
            print(f"[{datetime.now(LOG_TIMEZONE_PYTZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] [CRITICAL] [logger:log_error] Failed to write ERROR to log file {LOG_FILE}: {file_log_e}")

    # Attempt to log to Database (with UTC ISO timestamp)
    _try_log_to_db(level, module, func, message, traceback_str, user_id, _timestamp_utc_iso())


def log_warning(module: str, func: str, message: str, exception: Exception = None, user_id: str | None = None):
    """Logs warning messages. Prints/logs to file AND attempts to log to database."""
    level = "WARNING"
    traceback_str = None
    if exception:
        traceback_str = traceback.format_exc()

    # Log to Console/File (uses LOG_TIMEZONE_PYTZ via _format_log_entry)
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
            print(f"[{datetime.now(LOG_TIMEZONE_PYTZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] [CRITICAL] [logger:log_warning] Failed to write WARNING to log file {LOG_FILE}: {file_log_e}")

    # Attempt to log to Database (with UTC ISO timestamp)
    _try_log_to_db(level, module, func, message, traceback_str, user_id, _timestamp_utc_iso())

# --- END OF FULL tools/logger.py ---