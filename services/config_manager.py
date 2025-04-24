# services/config_manager.py
"""Service layer for managing user configuration and preferences."""
from tools.logger import log_info, log_error, log_warning
# Import registry functions for persistence
from users.user_registry import get_user_preferences as get_prefs_from_registry
from users.user_registry import update_preferences as update_prefs_in_registry # This writes to file
# Import state manager for memory updates
try:
    # This function updates the live agent state dictionary
    from services.agent_state_manager import update_preferences_in_state
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("config_manager", "import", "AgentStateManager not found. In-memory preference updates skipped.")
    AGENT_STATE_MANAGER_IMPORTED = False
    def update_preferences_in_state(*args, **kwargs): return False # Dummy

# Import calendar tool auth check
try:
    from tools.calendar_tool import authenticate as check_calendar_auth_status
    CALENDAR_TOOL_IMPORTED = True
except ImportError:
     log_error("config_manager", "import", "calendar_tool not found. Calendar auth initiation fails.")
     CALENDAR_TOOL_IMPORTED = False
     def check_calendar_auth_status(*args, **kwargs): return {"status": "fails", "message": "Calendar tool unavailable."}

from typing import Dict, Any, Optional

def get_preferences(user_id: str) -> Optional[Dict]:
    """Gets user preferences from the persistent registry."""
    try:
        prefs = get_prefs_from_registry(user_id)
        return prefs # Returns None if not found
    except Exception as e:
        log_error("config_manager", "get_preferences", f"Error reading preferences for {user_id}", e)
        return None

def update_preferences(user_id: str, updates: Dict) -> bool:
    """
    Updates preferences in persistent registry AND in-memory agent state.
    Returns True on success (based on registry update), False otherwise.
    """
    log_info("config_manager", "update_preferences", f"Updating preferences for {user_id}: {list(updates.keys())}")
    if not isinstance(updates, dict) or not updates:
        log_warning("config_manager", "update_preferences", "Invalid or empty updates provided.")
        return False

    # 1. Update Persistent Store (Registry File)
    registry_update_success = False
    try:
        update_prefs_in_registry(user_id, updates) # Writes to registry.json
        log_info("config_manager", "update_preferences", f"Registry file update requested for {user_id}")
        registry_update_success = True
    except Exception as e:
        log_error("config_manager", "update_preferences", f"Registry file update failed for {user_id}", e)
        return False # Don't proceed if persistence fails

    # 2. Update In-Memory State via AgentStateManager (If persistence succeeded)
    if registry_update_success and AGENT_STATE_MANAGER_IMPORTED:
        try:
            mem_update_success = update_preferences_in_state(user_id, updates) # Updates live _AGENT_STATE_STORE
            if not mem_update_success:
                log_warning("config_manager", "update_preferences", f"In-memory state update failed or user not found in state for {user_id}.")
                # Should we revert registry? For now, proceed but warn.
        except Exception as mem_e:
             log_error("config_manager", "update_preferences", f"Error updating in-memory state for {user_id}", mem_e)
             # Log error, but persistence succeeded, so arguably return True

    elif registry_update_success: # Log if manager wasn't imported
        log_warning("config_manager", "update_preferences", "AgentStateManager not imported. Skipping in-memory state update.")

    return registry_update_success # Return success based on registry write

def initiate_calendar_auth(user_id: str) -> Dict:
    """Initiates calendar auth flow via calendar_tool."""
    log_info("config_manager", "initiate_calendar_auth", f"Initiating calendar auth for {user_id}")
    if not CALENDAR_TOOL_IMPORTED:
         return {"status": "fails", "message": "Calendar auth component unavailable."}
    current_prefs = get_preferences(user_id) # Use service getter
    if not current_prefs:
        log_error("config_manager", "initiate_calendar_auth", f"Prefs not found for {user_id}")
        return {"status": "fails", "message": "User profile not found."}
    try:
        # Pass current prefs needed by authenticate function
        auth_result = check_calendar_auth_status(user_id, current_prefs)
        return auth_result
    except Exception as e:
        log_error("config_manager", "initiate_calendar_auth", f"Error during calendar auth init: {e}", e)
        return {"status": "fails", "message": "Error starting calendar auth."}

def set_user_status(user_id: str, status: str) -> bool:
    """Helper to specifically update user status in registry and memory."""
    log_info("config_manager", "set_user_status", f"Setting status='{status}' for {user_id}")
    if not status or not isinstance(status, str):
        log_warning("config_manager", "set_user_status", f"Invalid status value: {status}")
        return False
    # Calls the main update function which handles both registry and memory state
    return update_preferences(user_id, {"status": status})