import os
import pytz
from datetime import datetime
import traceback

# === Config ===
DEBUG_MODE = True  # Set to False in production
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "whats_tasker.log")

# Ensure logs directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# === Helpers ===
def _timestamp():
    tz = pytz.timezone("Asia/Jerusalem")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

def _format_log(level: str, module: str, func: str, message: str):
    return f"[{_timestamp()}] [{level}] [{module}:{func}] {message}"

# === Log functions ===
def log_info(module: str, func: str, message: str):
    entry = _format_log("INFO", module, func, message)
    if DEBUG_MODE:
        print(entry)

def log_error(module: str, func: str, message: str, exception: Exception = None):
    entry = _format_log("ERROR", module, func, message)
    if DEBUG_MODE:
        print(entry)
        # --- ADD THIS PART ---
        if exception:
            print(traceback.format_exc()) # Print traceback to console in debug mode
        # ---------------------
    else:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
            if exception:
                f.write(traceback.format_exc())
                
def log_warning(module: str, func: str, message: str, exception: Exception = None):
    entry = _format_log("WARNING", module, func, message)
    if DEBUG_MODE:
        print(entry)
    else:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
            if exception:
                f.write(traceback.format_exc())