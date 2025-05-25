# --- START OF FULL services/config_manager.py ---
from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_user_preferences as get_prefs_from_registry
from users.user_registry import update_preferences as update_prefs_in_registry
from typing import Dict, Any

# --- AgentStateManager Imports ---
AGENT_STATE_MANAGER_IMPORTED = False
_get_agent_state_mem = None # Function placeholder
_update_preferences_in_state_mem = None # Function placeholder

try:
    from services.agent_state_manager import get_agent_state, update_preferences_in_state
    _get_agent_state_mem = get_agent_state
    _update_preferences_in_state_mem = update_preferences_in_state
    AGENT_STATE_MANAGER_IMPORTED = True
    log_info("config_manager", "import", "Successfully imported AgentStateManager functions (get_agent_state, update_preferences_in_state).")
except ImportError:
    log_error("config_manager", "import", "AgentStateManager not found or functions missing. In-memory operations will be significantly impacted or unavailable.")
    # No need for dummy functions here; functions that rely on these will check AGENT_STATE_MANAGER_IMPORTED

# --- CalendarTool Import ---
CALENDAR_TOOL_IMPORTED = False
_check_calendar_auth_status_tool = None # Function placeholder
try:
    from tools.calendar_tool import authenticate
    _check_calendar_auth_status_tool = authenticate
    CALENDAR_TOOL_IMPORTED = True
except ImportError:
    log_error("config_manager", "import", "calendar_tool.authenticate not found. Calendar auth initiation will fail.")

# --- Configuration ---
ALLOWED_GCAL_STATUSES = {"not_integrated", "pending_auth", "connected", "error"}

def get_preferences(user_id: str) -> Dict | None:
    """
    Gets user preferences from the IN-MEMORY agent state.
    Returns a copy of the preferences dictionary or None if not found/error.
    """
    fn_name = "get_preferences" # Standardized name for the function
    if not AGENT_STATE_MANAGER_IMPORTED or _get_agent_state_mem is None:
        log_warning("config_manager", fn_name, f"AgentStateManager not available. Cannot get preferences from memory for {user_id}. This is unexpected for runtime operations.")
        # As a last resort for critical system functions that NEED prefs and can't wait for agent state,
        # they might call get_prefs_from_registry directly. But general runtime should use memory.
        return None # Indicate failure to get from memory

    try:
        agent_state = _get_agent_state_mem(user_id)
        if agent_state and isinstance(agent_state.get("preferences"), dict):
            return agent_state["preferences"].copy() # Return a copy
        elif agent_state:
            log_warning("config_manager", fn_name, f"Preferences dict missing or invalid in agent state for {user_id}")
        # else: agent_state not found, _get_agent_state_mem should log this if it's an issue
        return None
    except Exception as e:
        log_error("config_manager", fn_name, f"Unexpected error getting preferences from memory for {user_id}", e)
        return None

def update_preferences(user_id: str, updates: Dict) -> bool:
    """
    Updates preferences in persistent registry AND then in-memory agent state.
    Returns True on success (based on registry update), False otherwise.
    """
    fn_name = "update_preferences"
    # log_info("config_manager", fn_name, f"Updating preferences for {user_id}: {list(updates.keys())}") # Can be verbose

    if not isinstance(updates, dict) or not updates:
        log_warning("config_manager", fn_name, f"Invalid or empty updates provided for user {user_id}.")
        return False

    # 1. Update Persistent Store (Registry File)
    registry_update_success = False
    try:
        # update_prefs_in_registry is an alias for user_registry.update_preferences
        registry_update_success = update_prefs_in_registry(user_id, updates)
        if registry_update_success:
            # log_info("config_manager", fn_name, f"Registry file update successful for {user_id}") # Can be verbose
            pass
        else:
            # user_registry.update_preferences logs errors if user not found or save fails
            log_warning("config_manager", fn_name, f"Registry file update reported as failed for {user_id} by user_registry.")
            return False # Don't proceed if persistence fails as reported by the registry module
    except Exception as e_reg: # Catch any unexpected error from registry update
        log_error("config_manager", fn_name, f"Exception during registry file update for {user_id}", e_reg)
        return False

    # 2. Update In-Memory State via AgentStateManager (If persistence succeeded)
    if AGENT_STATE_MANAGER_IMPORTED and _update_preferences_in_state_mem is not None:
        try:
            mem_update_success = _update_preferences_in_state_mem(user_id, updates)
            if not mem_update_success:
                log_warning("config_manager", fn_name, f"In-memory state update failed or user not found in state for {user_id} (after successful registry save).")
                # This indicates a potential desync, but registry is the source of truth.
        except Exception as mem_e:
             log_error("config_manager", fn_name, f"Error updating in-memory state for {user_id} (after successful registry save)", mem_e)
    elif AGENT_STATE_MANAGER_IMPORTED and _update_preferences_in_state_mem is None:
        log_error("config_manager", fn_name, "AgentStateManager imported but _update_preferences_in_state_mem function is None.")
    else: # AGENT_STATE_MANAGER_IMPORTED is False
        log_warning("config_manager", fn_name, "AgentStateManager not imported. Skipping in-memory state update for preferences.")

    return registry_update_success # Success is primarily defined by successful persistence

def initiate_calendar_auth(user_id: str) -> Dict:
    """Initiates calendar auth flow via calendar_tool and sets status to pending_auth."""
    fn_name = "initiate_calendar_auth"
    log_info("config_manager", fn_name, f"Initiating calendar auth for {user_id}")

    if not CALENDAR_TOOL_IMPORTED or _check_calendar_auth_status_tool is None:
        log_error("config_manager", fn_name, "Calendar tool (authenticate function) not available.")
        return {"status": "fails", "message": "Calendar authentication component unavailable."}
    
    # Get current preferences FROM MEMORY to pass to calendar_tool.authenticate
    # This ensures calendar_tool.authenticate has the most up-to-date gcal_integration_status
    current_prefs = get_preferences(user_id)
    if not current_prefs:
        # If not in memory, try from registry as a fallback for this specific setup step
        log_warning("config_manager", fn_name, f"Preferences for {user_id} not found in memory. Attempting fallback to registry for GCal auth initiation.")
        current_prefs = get_prefs_from_registry(user_id)
        if not current_prefs:
            log_error("config_manager", fn_name, f"User profile (preferences) not found for {user_id} even in registry.")
            return {"status": "fails", "message": "User profile not found."}
    
    try:
        auth_result = _check_calendar_auth_status_tool(user_id, current_prefs)
        
        # If auth flow is being initiated (status becomes 'pending' from calendar_tool.authenticate)
        # This means an auth URL was generated.
        if auth_result.get("status") == "pending":
            log_info("config_manager", fn_name, f"Calendar auth pending for {user_id}. Setting gcal_integration_status.")
            status_set_success = set_gcal_integration_status(user_id, "pending_auth")
            if not status_set_success:
                log_error("config_manager", fn_name, f"Failed to set gcal_integration_status to 'pending_auth' for {user_id}.")
                # Potentially alter auth_result or return a different error?
                # For now, auth_result (with URL) is still returned.
        return auth_result
    except Exception as e:
        log_error("config_manager", fn_name, f"Error during calendar auth initiation for {user_id}", e)
        return {"status": "fails", "message": f"Error starting calendar authentication: {str(e)}."}

def set_user_status(user_id: str, status: str) -> bool:
    """Helper to specifically update user status."""
    fn_name = "set_user_status"
    # log_info("config_manager", fn_name, f"Setting status='{status}' for {user_id}") # Can be verbose
    if not status or not isinstance(status, str):
        log_warning("config_manager", fn_name, f"Invalid status value: {status} for user {user_id}")
        return False
    return update_preferences(user_id, {"status": status})

def set_gcal_integration_status(user_id: str, status: str) -> bool:
    """
    Specifically updates the gcal_integration_status for a user.
    Ensures status is one of the allowed values.
    """
    fn_name = "set_gcal_integration_status"
    if status not in ALLOWED_GCAL_STATUSES:
        log_error("config_manager", fn_name, f"Invalid gcal_integration_status '{status}' for user {user_id}. Allowed: {ALLOWED_GCAL_STATUSES}")
        return False
    
    log_info("config_manager", fn_name, f"Setting gcal_integration_status='{status}' for user {user_id}")
    return update_preferences(user_id, {"gcal_integration_status": status})

# --- END OF FULL services/config_manager.py ---