# --- START OF FULL services/agent_state_manager.py ---
from tools.logger import log_info, log_error, log_warning
from typing import Dict, List, Any, Set 
import threading
import copy 
from datetime import datetime, timezone # Ensure timezone is imported
import json # For stringifying tool_calls if they are passed as objects

_AGENT_STATE_STORE: Dict[str, Dict[str, Any]] | None = None 
_state_lock = threading.Lock()

MAX_CONVERSATION_HISTORY_MESSAGES = 50 # Your new limit

def initialize_state_store(agent_dict_ref: Dict[str, Dict[str, Any]]):
    global _AGENT_STATE_STORE
    if _AGENT_STATE_STORE is not None: return # Already initialized
    if isinstance(agent_dict_ref, dict):
        _AGENT_STATE_STORE = agent_dict_ref
        log_info("AgentStateManager", "initialize_state_store", f"State store initialized (ID: {id(_AGENT_STATE_STORE)}).")
    else:
        log_error("AgentStateManager", "initialize_state_store", "Invalid dict reference for state store.")
        _AGENT_STATE_STORE = {}

def _is_initialized() -> bool:
    if _AGENT_STATE_STORE is None:
        log_error("AgentStateManager", "_is_initialized", "CRITICAL: State store not initialized.")
        return False
    return True

def register_agent_instance(user_id: str, agent_state: Dict[str, Any]):
    if not _is_initialized(): return
    if not isinstance(agent_state, dict):
         log_error("AgentStateManager", "register_agent_instance", f"Invalid agent_state type for {user_id}.")
         return
    with _state_lock:
        _AGENT_STATE_STORE[user_id] = agent_state # type: ignore

def update_preferences_in_state(user_id: str, prefs_updates: Dict) -> bool:
    if not _is_initialized(): return False
    updated = False
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id) # type: ignore
        if state and isinstance(state.get("preferences"), dict):
            state["preferences"].update(prefs_updates)
            updated = True
    return updated

# --- REVISED: add_message_to_user_history ---
def add_message_to_user_history(
    user_id: str,
    role: str,
    message_type: str,
    content: str | None, # For user/assistant text, or JSON string of tool result for 'tool' role.
                         # Can be None for 'assistant' role if it only issues tool_calls.
    tool_calls_obj: List[Dict] | None = None, # For 'assistant' role when requesting tool calls (actual Python list of dicts)
    tool_name: str | None = None,           # For 'tool' role (result)
    tool_call_id: str | None = None         # For 'tool' role (result, links to assistant's tool_call_id)
):
    """
    Appends a rich message object to the user's conversation history for LLM context.
    Manages history size. Includes a timestamp.
    'tool_calls_obj' is stored as a JSON string under 'tool_calls_json_str'.
    'content' for tool results is expected to be already a JSON string from the caller.
    """
    fn_name = "add_message_to_user_history_rich" # Indicate it's the revised one
    if not _is_initialized(): return
    
    valid_roles = {'user', 'assistant', 'tool', 'system'} # 'system' can be used for system notes in history if needed
    if role not in valid_roles:
        log_error("AgentStateManager", fn_name, f"Invalid role '{role}' for history message for {user_id}.")
        return

    history_entry: Dict[str, Any] = {
        "role": role,
        "message_type": message_type, # Store your semantic message type
        "timestamp_utc_iso": datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    }

    # Content:
    # - For 'user': their text.
    # - For 'assistant' (textual response): agent's text.
    # - For 'assistant' (tool call request): can be None or accompanying text.
    # - For 'tool' (result): should be the JSON string of the tool's output dict.
    if content is not None:
        history_entry["content"] = content
    elif role == "assistant" and tool_calls_obj: # Assistant message with only tool calls
        history_entry["content"] = None # Explicitly set to None or an empty string if OpenAI API requires it
    elif role == "user": # User content should not be None typically
        history_entry["content"] = "" # Default to empty string if None for user
        log_warning("AgentStateManager", fn_name, f"User message content was None for {user_id}. Storing empty string.")


    # Tool Calls (for assistant role, when it decides to call tools)
    if role == "assistant" and tool_calls_obj:
        if isinstance(tool_calls_obj, list) and all(isinstance(tc, dict) for tc in tool_calls_obj):
            try:
                history_entry["tool_calls_json_str"] = json.dumps(tool_calls_obj)
            except TypeError as te:
                log_error("AgentStateManager", fn_name, f"TypeError serializing tool_calls_obj for {user_id}: {te}. Storing as raw string.", te)
                history_entry["tool_calls_json_str"] = str(tool_calls_obj) # Fallback
        else:
            log_error("AgentStateManager", fn_name, f"tool_calls_obj for assistant role was not a list of dicts for {user_id}. Storing as raw string.")
            history_entry["tool_calls_json_str"] = str(tool_calls_obj) # Fallback
    
    # Tool Name and Tool Call ID (for tool role, when providing results)
    if role == "tool":
        if tool_name: history_entry["name"] = tool_name
        if tool_call_id: history_entry["tool_call_id"] = tool_call_id
        # The 'content' for role 'tool' is expected to be the JSON string of the tool result,
        # already set from the 'content' parameter.

    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id) # type: ignore
        if not state:
            log_warning("AgentStateManager", fn_name, f"State missing for {user_id}, cannot add message to history.")
            return

        if not isinstance(state.get("conversation_history"), list):
            state["conversation_history"] = []

        history_list: List[Dict[str, Any]] = state["conversation_history"]
        history_list.append(history_entry)
        
        if len(history_list) > MAX_CONVERSATION_HISTORY_MESSAGES:
            state["conversation_history"] = history_list[-MAX_CONVERSATION_HISTORY_MESSAGES:]
        else:
            state["conversation_history"] = history_list
        
        # log_info("AgentStateManager", fn_name, f"Added '{role}' (type: {message_type}) to history for {user_id}. Len: {len(state['conversation_history'])}.") # Verbose

# --- Other functions remain the same as your last correct version ---
def update_agent_state_key(user_id: str, key: str, value: Any) -> bool:
    if not _is_initialized(): return False
    updated = False
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state:
            if value is None and key in state: state.pop(key, None)
            else: state[key] = value
            updated = True
    return updated

def add_task_to_context(user_id: str, task_data: Dict):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state:
            if not isinstance(state.get("active_tasks_context"), list): state["active_tasks_context"] = []
            context = state["active_tasks_context"]; event_id = task_data.get("event_id"); found_idx = -1
            if event_id:
                for i, item in enumerate(context):
                    if item.get("event_id") == event_id: found_idx = i; break
            if found_idx != -1: context[found_idx] = task_data
            else: context.append(task_data)

def update_task_in_context(user_id: str, event_id: str, updated_task_data: Dict):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state and isinstance(state.get("active_tasks_context"), list):
            context = state["active_tasks_context"]; found = False
            for i, item in enumerate(context):
                if item.get("event_id") == event_id: context[i] = updated_task_data; found = True; break
            if not found and updated_task_data.get("status", "pending").lower() in ["pending", "in_progress"]:
                 context.append(updated_task_data)

def remove_task_from_context(user_id: str, event_id: str):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state and isinstance(state.get("active_tasks_context"), list):
            state["active_tasks_context"][:] = [item for item in state["active_tasks_context"] if item.get("event_id") != event_id]

def update_full_context(user_id: str, new_context: List[Dict]):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state: state["active_tasks_context"] = new_context if isinstance(new_context, list) else []

def add_notified_event_id(user_id: str, event_id: str):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state:
            if not isinstance(state.get("notified_event_ids_today"), set): state["notified_event_ids_today"] = set()
            state["notified_event_ids_today"].add(event_id)

def get_notified_event_ids(user_id: str) -> Set[str]:
    if not _is_initialized(): return set()
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state and isinstance(state.get("notified_event_ids_today"), set):
            return state["notified_event_ids_today"].copy()
    return set()

def clear_notified_event_ids(user_id: str):
    if not _is_initialized(): return
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state: state["notified_event_ids_today"] = set()

def get_agent_state(user_id: str) -> Dict | None:
    if not _is_initialized(): return None
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id) # type: ignore
        return state.copy() if state else None

def get_context(user_id: str) -> List[Dict] | None:
    if not _is_initialized(): return [] # Or None
    with _state_lock:
        state = _AGENT_STATE_STORE.get(user_id); # type: ignore
        if state and isinstance(state.get("active_tasks_context"), list):
            return copy.deepcopy(state["active_tasks_context"])
    return []

# --- END OF FULL services/agent_state_manager.py ---