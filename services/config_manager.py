# --- START OF FULL services/config_manager.py ---
from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_user_preferences as get_prefs_from_registry
from users.user_registry import update_preferences as update_prefs_in_registry
from typing import Dict, Any
import traceback 

# --- AgentStateManager Imports ---
AGENT_STATE_MANAGER_IMPORTED = False
_get_agent_state_mem = None 
_update_preferences_in_state_mem = None 

try:
    from services.agent_state_manager import get_agent_state, update_preferences_in_state
    _get_agent_state_mem = get_agent_state
    _update_preferences_in_state_mem = update_preferences_in_state
    AGENT_STATE_MANAGER_IMPORTED = True
    log_info("config_manager", "import", "Successfully imported AgentStateManager functions (get_agent_state, update_preferences_in_state).")
except ImportError:
    log_error("config_manager", "import", "AgentStateManager not found or functions missing. In-memory operations will be significantly impacted or unavailable.")

# --- CalendarTool Import is now done locally ---
CALENDAR_TOOL_IMPORTED = True # Assume it can be imported when needed. Actual check done in function.
    
# --- Configuration ---
ALLOWED_GCAL_STATUSES = {"not_integrated", "pending_auth", "connected", "error"}

def get_preferences(user_id: str) -> Dict | None:
    fn_name = "get_preferences" 
    if not AGENT_STATE_MANAGER_IMPORTED or _get_agent_state_mem is None:
        log_warning("config_manager", fn_name, f"AgentStateManager not available. Cannot get preferences from memory for {user_id}. This is unexpected for runtime operations.")
        return None 

    try:
        agent_state = _get_agent_state_mem(user_id)
        if agent_state and isinstance(agent_state.get("preferences"), dict):
            return agent_state["preferences"].copy() 
        elif agent_state:
            log_warning("config_manager", fn_name, f"Preferences dict missing or invalid in agent state for {user_id}")
        return None
    except Exception as e:
        log_error("config_manager", fn_name, f"Unexpected error getting preferences from memory for {user_id}", e)
        return None

def update_preferences(user_id: str, updates: Dict) -> bool:
    fn_name = "update_preferences"
    if not isinstance(updates, dict) or not updates:
        log_warning("config_manager", fn_name, f"Invalid or empty updates provided for user {user_id}.")
        return False

    registry_update_success = False
    try:
        registry_update_success = update_prefs_in_registry(user_id, updates)
        if not registry_update_success:
            log_warning("config_manager", fn_name, f"Registry file update reported as failed for {user_id} by user_registry.")
            return False
    except Exception as e_reg: 
        log_error("config_manager", fn_name, f"Exception during registry file update for {user_id}", e_reg)
        return False

    if AGENT_STATE_MANAGER_IMPORTED and _update_preferences_in_state_mem is not None:
        try:
            mem_update_success = _update_preferences_in_state_mem(user_id, updates)
            if not mem_update_success:
                log_warning("config_manager", fn_name, f"In-memory state update failed or user not found in state for {user_id} (after successful registry save).")
        except Exception as mem_e:
             log_error("config_manager", fn_name, f"Error updating in-memory state for {user_id} (after successful registry save)", mem_e)
    elif AGENT_STATE_MANAGER_IMPORTED and _update_preferences_in_state_mem is None:
        log_error("config_manager", fn_name, "AgentStateManager imported but _update_preferences_in_state_mem function is None.")
    else: 
        log_warning("config_manager", fn_name, "AgentStateManager not imported. Skipping in-memory state update for preferences.")
    return registry_update_success

def initiate_calendar_auth(user_id: str) -> Dict:
    fn_name = "initiate_calendar_auth"
    log_info("config_manager", fn_name, f"Initiating calendar auth for {user_id}")

    _check_calendar_auth_status_tool_local = None
    try:
        from tools.calendar_tool import authenticate
        _check_calendar_auth_status_tool_local = authenticate
    except ImportError as e_auth:
        log_error("config_manager", "import_debug", f"ImportError for 'authenticate' in initiate_calendar_auth: {e_auth}. Calendar auth initiation will fail.")
        return {"status": "fails", "message": "Calendar authentication component import failed."}
    except Exception as e_other: # Should already have traceback imported at module level
        log_error("config_manager", "import_debug", f"OTHER Exception importing 'authenticate' in initiate_calendar_auth: {e_other}. Trace: {traceback.format_exc()}")
        return {"status": "fails", "message": "Calendar authentication component unavailable."}

    if _check_calendar_auth_status_tool_local is None: # Should be caught by except blocks, but as a safeguard
        log_error("config_manager", fn_name, "Calendar tool (authenticate function) not available after import attempt.")
        return {"status": "fails", "message": "Calendar authentication component unavailable."}
    
    current_prefs = get_preferences(user_id)
    if not current_prefs:
        log_warning("config_manager", fn_name, f"Preferences for {user_id} not found in memory. Attempting fallback to registry for GCal auth initiation.")
        current_prefs = get_prefs_from_registry(user_id)
        if not current_prefs:
            log_error("config_manager", fn_name, f"User profile (preferences) not found for {user_id} even in registry.")
            return {"status": "fails", "message": "User profile not found."}
    
    try:
        auth_result = _check_calendar_auth_status_tool_local(user_id, current_prefs)
        
        if auth_result.get("status") == "pending":
            log_info("config_manager", fn_name, f"Calendar auth pending for {user_id}. Setting gcal_integration_status.")
            status_set_success = set_gcal_integration_status(user_id, "pending_auth")
            if not status_set_success:
                log_error("config_manager", fn_name, f"Failed to set gcal_integration_status to 'pending_auth' for {user_id}.")
        return auth_result
    except Exception as e:
        log_error("config_manager", fn_name, f"Error during calendar auth initiation for {user_id}", e)
        return {"status": "fails", "message": f"Error starting calendar authentication: {str(e)}."}

def set_user_status(user_id: str, status: str) -> bool:
    fn_name = "set_user_status"
    if not status or not isinstance(status, str):
        log_warning("config_manager", fn_name, f"Invalid status value: {status} for user {user_id}")
        return False
    return update_preferences(user_id, {"status": status})

def set_gcal_integration_status(user_id: str, status: str) -> bool:
    fn_name = "set_gcal_integration_status"
    if status not in ALLOWED_GCAL_STATUSES:
        log_error("config_manager", fn_name, f"Invalid gcal_integration_status '{status}' for user {user_id}. Allowed: {ALLOWED_GCAL_STATUSES}")
        return False
    
    log_info("config_manager", fn_name, f"Setting gcal_integration_status='{status}' for user {user_id}")
    return update_preferences(user_id, {"gcal_integration_status": status})

# --- END OF FULL services/config_manager.py ---