# --- START OF FILE services/agent_state_manager.py ---
# services/agent_state_manager.py
"""
Manages the in-memory state of user agents.
Provides thread-safe functions to access and modify the global agent state dictionary.
Requires initialization via initialize_state_store.
"""
from tools.logger import log_info, log_error, log_warning
from typing import Dict, List, Any, Optional, Set # Added Set
import threading
import copy
from datetime import datetime

# --- Module Level State ---
_AGENT_STATE_STORE: Optional[Dict[str, Dict[str, Any]]] = None
_state_lock = threading.Lock()

def initialize_state_store(agent_dict_ref: Dict):
    """Initializes the state manager with a reference to the global agent state dictionary."""
    global _AGENT_STATE_STORE
    if _AGENT_STATE_STORE is not None:
        log_warning("AgentStateManager", "initialize_state_store", "State store already initialized.")
        return
    if isinstance(agent_dict_ref, dict):
        _AGENT_STATE_STORE = agent_dict_ref
        log_info("AgentStateManager", "initialize_state_store", f"State store initialized with reference (ID: {id(_AGENT_STATE_STORE)}).")
    else:
        log_error("AgentStateManager", "initialize_state_store", "Invalid dictionary reference passed.")
        _AGENT_STATE_STORE = {} # Initialize to empty dict if invalid ref passed

def _is_initialized() -> bool:
    """Checks if the state store has been initialized."""
    if _AGENT_STATE_STORE is None:
        log_error("AgentStateManager", "_is_initialized", "CRITICAL: State store accessed before initialization.")
        return False
    return True

# --- Modifier Functions ---

def register_agent_instance(user_id: str, agent_state: Dict):
    """Adds or replaces the entire state dictionary for a user."""
    if not _is_initialized(): return
    if not isinstance(agent_state, dict):
         log_error("AgentStateManager", "register_agent_instance", f"Invalid agent_state type for {user_id}")
         return
    log_info("AgentStateManager", "register_agent_instance", f"Registering/updating state for user {user_id}")
    with _state_lock:
        _AGENT_STATE_STORE[user_id] = agent_state

def update_preferences_in_state(user_id: str, prefs_updates: Dict) -> bool:
    """Updates the preferences dictionary within the user's in-memory state."""
    if not _is_initialized(): return False
    updated = False
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state and isinstance(state.get("preferences"), dict):
            state["preferences"].update(prefs_updates)
            log_info("AgentStateManager", "update_preferences_in_state", f"Updated in-memory preferences for {user_id}: {list(prefs_updates.keys())}")
            updated = True
        else:
            log_warning("AgentStateManager", "update_preferences_in_state", f"Cannot update prefs: State or prefs dict missing/invalid for {user_id}")
    return updated

def add_task_to_context(user_id: str, task_data: Dict):
    """Appends or updates a task dictionary in the user's in-memory context list."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state:
            # Ensure 'active_tasks_context' exists and is a list
            if not isinstance(state.get("active_tasks_context"), list):
                 state["active_tasks_context"] = []

            context = state["active_tasks_context"]
            event_id = task_data.get("event_id") # Use event_id as primary key
            found_idx = -1
            if event_id:
                for i, item in enumerate(context):
                    # Check using event_id which should be unique
                    if item.get("event_id") == event_id:
                        found_idx = i
                        break

            if found_idx != -1:
                 log_info("AgentStateManager", "add_task_to_context", f"Updating task {event_id} in context for {user_id}.")
                 context[found_idx] = task_data # Replace existing entry
            else:
                 context.append(task_data) # Add as new entry
                 log_info("AgentStateManager", "add_task_to_context", f"Added task {event_id} to context for {user_id}. New size: {len(context)}")
        else:
            log_warning("AgentStateManager", "add_task_to_context", f"State missing for {user_id}.")

def update_task_in_context(user_id: str, event_id: str, updated_task_data: Dict):
    """Finds a task by event_id in the context list and replaces it."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state and isinstance(state.get("active_tasks_context"), list):
            context = state["active_tasks_context"]
            found = False
            for i, item in enumerate(context):
                if item.get("event_id") == event_id:
                    context[i] = updated_task_data # Replace with new data
                    found = True
                    log_info("AgentStateManager", "update_task_in_context", f"Updated task {event_id} in context for {user_id}")
                    break
            if not found:
                 log_warning("AgentStateManager", "update_task_in_context", f"Task {event_id} not found for update. Adding if active.")
                 # Add only if it seems active (optional, depends on desired behavior)
                 if updated_task_data.get("status", "pending").lower() in ["pending", "in_progress", "in progress"]:
                      context.append(updated_task_data)
        else:
             log_warning("AgentStateManager", "update_task_in_context", f"State or active_tasks_context list invalid for {user_id}")

def remove_task_from_context(user_id: str, event_id: str):
    """Removes a task by event_id from the user's in-memory context list."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state and isinstance(state.get("active_tasks_context"), list):
            original_len = len(state["active_tasks_context"])
            # Use list comprehension for potentially better performance on large lists
            state["active_tasks_context"][:] = [
                item for item in state["active_tasks_context"] if item.get("event_id") != event_id
            ]
            if len(state["active_tasks_context"]) < original_len:
                log_info("AgentStateManager", "remove_task_from_context", f"Removed task {event_id} from context for {user_id}")
            # else: No warning needed if not found, just means it wasn't there
        else:
             log_warning("AgentStateManager", "remove_task_from_context", f"State or active_tasks_context list invalid for {user_id}")

def update_full_context(user_id: str, new_context: List[Dict]):
    """Replaces the entire active_tasks_context list (e.g., after sync)."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state:
            state["active_tasks_context"] = new_context if isinstance(new_context, list) else []
            log_info("AgentStateManager", "update_full_context", f"Replaced context for {user_id} with {len(state['active_tasks_context'])} items.")
        else:
            log_warning("AgentStateManager", "update_full_context", f"Cannot replace context: State missing for {user_id}")

def add_message_to_user_history(user_id: str, sender: str, message: str):
    """
    Appends a detailed message to the user's conversation history list. Keeps last 50.
    """
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if not state:
            log_warning("AgentStateManager", "add_message_to_user_history", f"Cannot add message: State missing for {user_id}")
            return

        if not isinstance(state.get("conversation_history"), list):
            log_warning("AgentStateManager", "add_message_to_user_history", f"conversation_history invalid for {user_id}, initializing.")
            state["conversation_history"] = []

        history_list = state["conversation_history"]
        timestamp = datetime.now().isoformat()
        entry = { "sender": sender, "timestamp": timestamp, "content": message }
        history_list.append(entry)
        state["conversation_history"] = history_list[-50:] # Limit size

def update_agent_state_key(user_id: str, key: str, value: Any) -> bool:
    """
    Updates or adds/removes a specific key-value pair in the user's agent state.
    """
    if not _is_initialized(): return False
    updated = False
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state:
            if value is None:
                if state.pop(key, None) is not None:
                    log_info("AgentStateManager", "update_agent_state_key", f"Removed key '{key}' from state for {user_id}")
            else:
                state[key] = value
                log_info("AgentStateManager", "update_agent_state_key", f"Updated key '{key}' in state for {user_id}")
            updated = True
        else:
            log_warning("AgentStateManager", "update_agent_state_key", f"Cannot update key '{key}': State missing for {user_id}")
    return updated

    # --- Notification Tracking Functions ---
def add_notified_event_id(user_id: str, event_id: str):
    """Adds an event ID to the set of notified events for today."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state:
            # Ensure the key exists and is a set
            if not isinstance(state.get("notified_event_ids_today"), set):
                state["notified_event_ids_today"] = set()
            state["notified_event_ids_today"].add(event_id)
            # log_info("AgentStateManager", "add_notified_event_id", f"Added {event_id} to notified set for {user_id}") # Maybe too verbose
        else:
            log_warning("AgentStateManager", "add_notified_event_id", f"State missing for {user_id}, cannot add notified event.")

def get_notified_event_ids(user_id: str) -> Set[str]:
    """Gets a copy of the set of notified event IDs for today."""
    if not _is_initialized(): return set()
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state and isinstance(state.get("notified_event_ids_today"), set):
            return state["notified_event_ids_today"].copy() # Return a copy
    return set() # Return empty set if user or set not found

def clear_notified_event_ids(user_id: str):
    """Clears the set of notified event IDs for the user."""
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state:
            # Reset to an empty set, even if key didn't exist before
            state["notified_event_ids_today"] = set()
            log_info("AgentStateManager", "clear_notified_event_ids", f"Cleared notified events set for {user_id}")
        else:
            log_warning("AgentStateManager", "clear_notified_event_ids", f"State missing for {user_id}, cannot clear notified events.")
    # --- End Notification Tracking Functions ---

def get_agent_state(user_id: str) -> Optional[Dict]:
    """
    Safely gets a SHALLOW copy of the full state dictionary for a user.
    """
    if not _is_initialized(): return None
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        return state.copy() if state else None

def get_context(user_id: str) -> Optional[List[Dict]]:
    """Gets a deep copy of the active_tasks_context list for a user."""
    if not _is_initialized(): return None
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id)
        if state and isinstance(state.get("active_tasks_context"), list):
            return copy.deepcopy(state["active_tasks_context"])
    return [] # Return empty list if user or context list not found/invalid
# --- END OF FILE services/agent_state_manager.py ---