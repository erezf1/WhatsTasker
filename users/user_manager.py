# --- START OF FILE users/user_manager.py ---

import os
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import traceback
from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry, register_user, get_user_preferences

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
     # Fallback local dictionary (not recommended for production)
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
    GoogleCalendarAPI = None # Define as None if import fails
    log_warning("user_manager","import", "GoogleCalendarAPI not found.")
try:
    from tools.metadata_store import list_metadata
    METADATA_STORE_IMPORTED = True
    log_info("user_manager", "import", "Successfully imported metadata_store.")
except ImportError:
    METADATA_STORE_IMPORTED = False
    def list_metadata(*args, **kwargs): return [] # Dummy function if import fails
    log_error("user_manager", "import", "metadata_store not found.")


# --- In-Memory State Dictionary Reference ---
_user_agents_in_memory: Dict[str, Dict[str, Any]] = {}
if AGENT_STATE_MANAGER_IMPORTED:
    # Initialize the state store via the manager
    initialize_state_store(_user_agents_in_memory)


# --- Preload Context ---
def _preload_initial_context(user_id: str) -> list:
    """Loads initial context ONLY from the metadata store during startup."""
    fn_name = "_preload_initial_context"
    log_info("user_manager", fn_name, f"Preloading initial context for {user_id} from metadata store.")
    if not METADATA_STORE_IMPORTED:
        log_error("user_manager", fn_name, "Metadata store not imported. Cannot preload context.")
        return []
    try:
        # Fetch metadata without date filters initially
        metadata_list = list_metadata(user_id=user_id)
        log_info("user_manager", fn_name, f"Preloaded {len(metadata_list)} items from metadata store for {user_id}.")
        return metadata_list
    except Exception as e:
        log_error("user_manager", fn_name, f"Error preloading context for {user_id} from metadata", e)
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
    calendar_api_instance = None # Default to None
    if GCAL_API_IMPORTED and GoogleCalendarAPI is not None and preferences.get("Calendar_Enabled"):
        token_file = preferences.get("token_file")
        if token_file and os.path.exists(token_file):
            log_info("user_manager", fn_name, f"Attempting GCalAPI init for {norm_user_id}")
            temp_cal_api = None # Initialize temporary variable
            try:
                # Step 1: Create the instance (this calls __init__ which loads creds and builds service)
                temp_cal_api = GoogleCalendarAPI(norm_user_id)
                # Step 2: Explicitly check if it's active *after* __init__ completes
                if temp_cal_api.is_active():
                    calendar_api_instance = temp_cal_api # Assign the created instance
                    log_info("user_manager", fn_name, f"GCalAPI initialized and active for {norm_user_id}")
                else:
                    # This case means __init__ completed but self.service was None or is_active() returned False
                    log_warning("user_manager", fn_name, f"GCalAPI initialized but NOT active for {norm_user_id}. Calendar features disabled.")
                    calendar_api_instance = None
            except Exception as cal_e:
                 # *** ADDED TRACEBACK LOGGING ***
                 tb_str = traceback.format_exc()
                 log_error("user_manager", fn_name, f"Exception during GCalAPI initialization or is_active() check for {norm_user_id}. Traceback:\n{tb_str}", cal_e)
                 # *******************************
                 calendar_api_instance = None # Ensure None on any exception
        else:
            if preferences.get("Calendar_Enabled"):
                 log_warning("user_manager", fn_name, f"GCal enabled for {norm_user_id} but token file missing/invalid path: {token_file}")
    elif not GCAL_API_IMPORTED:
         log_warning("user_manager", fn_name, f"GoogleCalendarAPI library not imported, skipping calendar init for {norm_user_id}.")
    elif not preferences.get("Calendar_Enabled"):
         log_info("user_manager", fn_name, f"Calendar not enabled for {norm_user_id}, skipping GCal init.")
    # --- End Refined GCal Initialization ---

    # Preload initial task context
    initial_context = _preload_initial_context(norm_user_id)

    # Assemble the full agent state dictionary
    agent_state = {
        "user_id": norm_user_id,
        "preferences": preferences,
        "active_tasks_context": initial_context,
        "calendar": calendar_api_instance, # Store the final instance (or None)
        "conversation_history": [],
        "notified_event_ids_today": set()
    }

    # Register state using AgentStateManager
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
        # Normalize ID just in case registry contains non-normalized ones
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
                # Error already logged by create_and_register_agent_state
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
        # Use state manager function (or local fallback)
        agent_state = get_agent_state(norm_user_id)
        if not agent_state:
            log_warning("user_manager", fn_name, f"State for {norm_user_id} not in memory. Creating now.")
            agent_state = create_and_register_agent_state(norm_user_id)
    except Exception as e:
         log_error("user_manager", fn_name, f"Error retrieving/creating agent state for {norm_user_id}", e)
         agent_state = None # Ensure None is returned on error

    return agent_state

# --- END OF FILE users/user_manager.py ---