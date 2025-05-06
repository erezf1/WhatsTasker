# --- START OF FILE users/user_registry.py ---

import json
import os
from datetime import datetime
from tools.logger import log_info, log_error, log_warning

DATA_SUFFIX = os.getenv("DATA_SUFFIX", "") # Default to empty for whatsapp mode
USER_REGISTRY_PATH = f"data/users/registry{DATA_SUFFIX}.json" # Dynamic path

# --- UPDATED Default Preferences ---
DEFAULT_PREFERENCES = {
    "status": "new", # 'new', 'onboarding', 'active'
    # Time & Scheduling Preferences
    "TimeZone": None, # REQUIRED during onboarding (e.g., "Asia/Jerusalem", "America/New_York")
    "Work_Start_Time": None, # REQUIRED during onboarding (HH:MM)
    "Work_End_Time": None,   # REQUIRED during onboarding (HH:MM)
    "Work_Days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"], # Default, modifiable
    "Working_Session_Length": None, # REQUIRED during onboarding (e.g., "60m", "1.5h")
    # Routine Preferences
    "Morning_Summary_Time": None , # User local time (HH:MM), default None
    "Evening_Summary_Time": None , # User local time (HH:MM), default None
    "Enable_Morning": True, # Default enabled if time is set
    "Enable_Evening": True, # Default enabled if time is set
    "Enable_Weekly_Reflection": False, # Future use
    # Notification Preferences (NEW)
    "Notification_Lead_Time": "15m", # Default lead time for event notifications
    # Calendar Integration
    "Calendar_Enabled": False, # Flag if GCal connected
    "Calendar_Type": "", # "Google" or potentially others later
    "email": "", # User's Google email (extracted during auth)
    "token_file": None, # Path to encrypted token file
    # Internal Tracking
    "Last_Sync": "", # ISO 8601 UTC timestamp (e.g., "2025-04-22T15:30:00Z")
    "last_morning_trigger_date": "", # YYYY-MM-DD string
    "last_evening_trigger_date": "", # YYYY-MM-DD string
    # Misc/Future Use
    "Holiday_Dates": [], # List of YYYY-MM-DD strings
}
# --- END OF UPDATED DEFAULT PREFERENCES ---


# Global in-memory registry variable.
_registry = {}

def load_registry():
    """Loads the registry from disk into memory."""
    global _registry
    if os.path.exists(USER_REGISTRY_PATH):
        try:
            with open(USER_REGISTRY_PATH, "r", encoding="utf-8") as f:
                # Handle empty file case
                content = f.read()
                if not content.strip():
                    _registry = {}
                else:
                    f.seek(0) # Go back to start if not empty
                    _registry = json.load(f)
            # Ensure existing users have all default keys
            updated_registry = False
            for user_id, user_data in _registry.items():
                 if "preferences" not in user_data:
                      user_data["preferences"] = DEFAULT_PREFERENCES.copy()
                      updated_registry = True
                 else:
                      for key, default_value in DEFAULT_PREFERENCES.items():
                           if key not in user_data["preferences"]:
                                user_data["preferences"][key] = default_value
                                updated_registry = True
            if updated_registry:
                 log_info("user_registry", "load_registry", "Added missing default preference keys to existing users.")
                 save_registry() # Save immediately if defaults were added

        except (json.JSONDecodeError, IOError) as e:
            log_error("user_registry", "load_registry", f"Failed to load or parse registry file {USER_REGISTRY_PATH}", e)
            _registry = {} # Fallback to empty registry on error
    else:
        _registry = {}
    log_info("user_registry", "load_registry", f"Registry loaded with {len(_registry)} users.")
    return _registry

def get_registry():
    """Returns the in-memory registry. Loads it if not already loaded."""
    global _registry
    # Check if registry is empty dictionary, load only if file exists
    if not _registry and os.path.exists(USER_REGISTRY_PATH):
         load_registry()
    # If still empty after attempt, it's genuinely empty or failed load
    return _registry

# Compatibility alias
def load_registered_users():
    return get_registry()

def save_registry():
    """Saves the current in-memory registry to disk."""
    global _registry
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(USER_REGISTRY_PATH), exist_ok=True)
        # Use atomic write pattern
        temp_path = USER_REGISTRY_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(_registry, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, USER_REGISTRY_PATH)
        # log_info("user_registry", "save_registry", "Registry saved to disk.") # Can be noisy
    except IOError as e:
        log_error("user_registry", "save_registry", f"Failed to write registry file {USER_REGISTRY_PATH}", e)
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
    except Exception as e:
        log_error("user_registry", "save_registry", f"Unexpected error saving registry", e)
        if os.path.exists(temp_path):
             try: os.remove(temp_path)
             except OSError: pass


def register_user(user_id):
    """Registers a new user with default preferences if not already present."""
    reg = get_registry() # Ensures registry is loaded
    if user_id not in reg:
        log_info("user_registry", "register_user", f"Registering new user {user_id}...")
        # Use deep copy to avoid modifying the original DEFAULT_PREFERENCES
        reg[user_id] = {"preferences": DEFAULT_PREFERENCES.copy()}
        save_registry() # Save after adding the new user
        log_info("user_registry", "register_user", f"Registered new user {user_id} with default preferences.")
    # else: User already exists, do nothing silently


def update_preferences(user_id, new_preferences):
    """Updates preferences for a given user and saves the registry."""
    reg = get_registry()
    if user_id in reg:
        # Ensure the preferences key exists and is a dict
        if not isinstance(reg[user_id].get("preferences"), dict):
             reg[user_id]["preferences"] = DEFAULT_PREFERENCES.copy()

        # Validate keys before updating? Optional, but good practice.
        valid_updates = {k: v for k, v in new_preferences.items() if k in DEFAULT_PREFERENCES}
        invalid_keys = set(new_preferences.keys()) - set(valid_updates.keys())
        if invalid_keys:
            log_warning("user_registry", "update_preferences", f"Ignoring invalid preference keys for user {user_id}: {invalid_keys}")

        if not valid_updates:
             log_warning("user_registry", "update_preferences", f"No valid preference keys provided for update for user {user_id}.")
             return False # Or True if ignoring invalid keys is considered success? Let's say False.

        reg[user_id]["preferences"].update(valid_updates)
        save_registry() # Save after updating
        log_info("user_registry", "update_preferences", f"Updated preferences for user {user_id}: {list(valid_updates.keys())}")
        return True
    else:
        log_error("user_registry", "update_preferences", f"User {user_id} not registered, cannot update preferences.")
        return False

def get_user_preferences(user_id):
    """Gets preferences for a user, returns None if user not found."""
    reg = get_registry()
    user_data = reg.get(user_id)
    if user_data:
        # Ensure preferences key exists and return a copy with all defaults ensured
        prefs = user_data.get("preferences", {})
        if not isinstance(prefs, dict):
             prefs = {} # Reset if not a dict

        # Create a copy of defaults, update with user's saved prefs
        # This ensures all keys exist in the returned dict
        full_prefs = DEFAULT_PREFERENCES.copy()
        full_prefs.update(prefs)
        return full_prefs
    else:
        return None

# Load registry into memory on module import.
load_registry()

# (Keep __main__ block for testing if desired)

# --- END OF FILE users/user_registry.py ---