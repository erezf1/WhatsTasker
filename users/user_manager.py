# --- START OF FILE users/user_manager.py ---

import os
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import traceback
from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry, register_user, get_user_preferences

# --- Database Import ---
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
    log_info("user_manager", "import", "Successfully imported activity_db.")
except ImportError:
    DB_IMPORTED = False
    class activity_db:
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
    log_error("user_manager", "import", "activity_db not found. Task preloading disabled.")

# --- State Manager Import ---
try:
    from services.agent_state_manager import (
        register_agent_instance,
        get_agent_state,
        initialize_state_store
    )
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
     log_error("user_manager", "import", "AgentStateManager not found.")
     AGENT_STATE_MANAGER_IMPORTED = False
     _user_agents_in_memory: Dict[str, Dict[str, Any]] = {}
     def register_agent_instance(uid, state): _user_agents_in_memory[uid] = state
     def get_agent_state(uid): return _user_agents_in_memory.get(uid)
     def initialize_state_store(ref): global _user_agents_in_memory; _user_agents_in_memory = ref

# --- Service/Tool Imports ---
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
    GCAL_API_IMPORTED = False
    GoogleCalendarAPI = None
    log_warning("user_manager","import", "GoogleCalendarAPI not found.")

# --- Token Store Import (NEEDED FOR CHECK) ---
try:
    from tools.token_store import get_user_token
    TOKEN_STORE_IMPORTED = True
except ImportError:
     TOKEN_STORE_IMPORTED = False
     log_error("user_manager", "import", "Failed to import token_store.get_user_token. GCalAPI check will fail.")
     def get_user_token(*args, **kwargs): return None
# ---------------------------------------------

# --- In-Memory State Dictionary Reference ---
_user_agents_in_memory: Dict[str, Dict[str, Any]] = {}
if AGENT_STATE_MANAGER_IMPORTED:
    initialize_state_store(_user_agents_in_memory)


# --- Preload Context ---
def _preload_initial_context(user_id: str) -> list[dict]:
    """Loads initial context (all tasks) for a user from the SQLite database."""
    fn_name = "_preload_initial_context"
    log_info("user_manager", fn_name, f"Preloading initial context for {user_id} from activity_db.")

    if not DB_IMPORTED:
        log_error("user_manager", fn_name, "Database module not imported. Cannot preload context.")
        return []

    try:
        task_list = activity_db.list_tasks_for_user(user_id=user_id)
        log_info("user_manager", fn_name, f"Preloaded {len(task_list)} tasks from DB for {user_id}.")
        return task_list
    except Exception as e:
        log_error("user_manager", fn_name, f"Error preloading context for {user_id} from DB", e, user_id=user_id)
        return []


# --- Agent State Creation ---
def create_and_register_agent_state(user_id: str): # REMOVED TYPE HINT
    """Creates the full agent state dictionary and registers it."""
    fn_name = "create_and_register_agent_state"
    log_info("user_manager", fn_name, f"Creating FULL agent state for {user_id}")
    norm_user_id = re.sub(r'\D', '', user_id)
    if not norm_user_id:
        log_error("user_manager", fn_name, f"Invalid user_id after normalization: '{user_id}'")
        return None

    register_user(norm_user_id)
    preferences = get_user_preferences(norm_user_id)
    if not preferences:
        log_error("user_manager", fn_name, f"Failed to get/create prefs for {norm_user_id} after registration attempt.")
        return None

    # --- Refined GCal Initialization ---
    calendar_api_instance = None
    # Check if GCal enabled in prefs AND necessary libraries/functions are loaded
    if preferences.get("Calendar_Enabled") and GCAL_API_IMPORTED and GoogleCalendarAPI is not None and TOKEN_STORE_IMPORTED:
        # --- MODIFIED CHECK: Try loading token data ---
        token_data = get_user_token(norm_user_id)
        if token_data is not None:
            # --- END MODIFIED CHECK ---
            log_info("user_manager", fn_name, f"Valid token data found for {norm_user_id}. Attempting GCalAPI init.")
            temp_cal_api = None
            try:
                temp_cal_api = GoogleCalendarAPI(norm_user_id)
                if temp_cal_api.is_active():
                    calendar_api_instance = temp_cal_api
                    log_info("user_manager", fn_name, f"GCalAPI initialized and active for {norm_user_id}")
                else:
                    log_warning("user_manager", fn_name, f"GCalAPI initialized but NOT active for {norm_user_id}. Calendar features disabled.")
                    calendar_api_instance = None
            except Exception as cal_e:
                 tb_str = traceback.format_exc()
                 log_error("user_manager", fn_name, f"Exception during GCalAPI initialization or is_active() check for {norm_user_id}. Traceback:\n{tb_str}", cal_e)
                 calendar_api_instance = None
        else:
            # Token data not found (logged by get_user_token)
            log_warning("user_manager", fn_name, f"GCal enabled for {norm_user_id} but no valid token data found via token_store.")
    elif not preferences.get("Calendar_Enabled"):
         log_info("user_manager", fn_name, f"Calendar not enabled for {norm_user_id}, skipping GCal init.")
    # Log if libs were the issue
    elif not GCAL_API_IMPORTED:
         log_warning("user_manager", fn_name, f"GoogleCalendarAPI library not imported, skipping calendar init for {norm_user_id}.")
    elif not TOKEN_STORE_IMPORTED:
         log_warning("user_manager", fn_name, f"token_store not imported, skipping calendar init for {norm_user_id}.")
    # --- End Refined GCal Initialization ---

    initial_context = _preload_initial_context(norm_user_id)

    agent_state = {
        "user_id": norm_user_id,
        "preferences": preferences,
        "active_tasks_context": initial_context,
        "calendar": calendar_api_instance,
        "conversation_history": [],
        "notified_event_ids_today": set()
    }

    try:
        register_agent_instance(norm_user_id, agent_state)
        log_info("user_manager", fn_name, f"Successfully registered agent state for {norm_user_id}")
        return agent_state
    except Exception as e:
        log_error("user_manager", fn_name, f"Failed state registration for {norm_user_id}", e)
        return None

# --- Initialize All Agents ---
def init_all_agents():
    """Initializes states for all users found in the registry."""
    fn_name = "init_all_agents"
    log_info("user_manager", fn_name, "Initializing states for all registered users...")
    registry_data = get_registry()
    registered_users = list(registry_data.keys())
    initialized_count = 0
    failed_count = 0

    if not registered_users:
        log_info("user_manager", fn_name, "No users found in registry.")
        return

    log_info("user_manager", fn_name, f"Found {len(registered_users)} users. Initializing...")
    for user_id in registered_users:
        norm_user_id = re.sub(r'\D', '', user_id)
        if not norm_user_id:
             log_warning("user_manager", fn_name, f"Skipping invalid user_id found in registry: '{user_id}'")
             failed_count += 1
             continue
        try:
            created_state = create_and_register_agent_state(norm_user_id)
            if created_state:
                initialized_count += 1
            else:
                failed_count += 1
        except Exception as e:
            log_error("user_manager", fn_name, f"Unexpected error initializing agent state for user {norm_user_id}", e)
            failed_count += 1

    log_info("user_manager", fn_name, f"Agent state initialization complete. Success: {initialized_count}, Failed: {failed_count}")


# --- Get Agent State ---
def get_agent(user_id: str) -> Optional[Dict]:
    """Retrieves or creates and registers the agent state for a user."""
    fn_name = "get_agent"
    norm_user_id = re.sub(r'\D', '', user_id)
    if not norm_user_id:
         log_error("user_manager", fn_name, f"Cannot get agent state for invalid normalized user_id from '{user_id}'")
         return None

    agent_state = None
    try:
        agent_state = get_agent_state(norm_user_id)
        if not agent_state:
            log_warning("user_manager", fn_name, f"State for {norm_user_id} not in memory. Creating now.")
            agent_state = create_and_register_agent_state(norm_user_id)
    except Exception as e:
         log_error("user_manager", fn_name, f"Error retrieving/creating agent state for {norm_user_id}", e)
         agent_state = None

    return agent_state

# --- END OF FILE users/user_manager.py ---