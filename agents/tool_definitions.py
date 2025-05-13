# --- START OF FULL agents/tool_definitions.py ---

from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Dict, List, Any, Tuple, Optional # Keep Optional for Pydantic models
import json
from datetime import datetime, timedelta, timezone # Added timezone
import re
import uuid # Added uuid
import traceback
# Import Service Layer functions & Helpers
import services.task_manager as task_manager
import services.config_manager as config_manager
import services.task_query_service as task_query_service
from services.agent_state_manager import get_agent_state, add_task_to_context, update_task_in_context # Assuming AGENT_STATE_MANAGER_IMPORTED is handled internally or true
# Import the class itself for type checking if needed, handle import error
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
     GoogleCalendarAPI = None
     GCAL_API_IMPORTED = False

# Import activity_db for type checking within tools
try:
    import tools.activity_db as activity_db # Keep for direct access if needed (e.g., type check in cancel_sessions)
    DB_IMPORTED = True
except ImportError:
    DB_IMPORTED = False
    log_error("tool_definitions", "update_item_details_tool_import", "Failed to import real activity_db. Some checks may be skipped.", None)
    # Define a dummy class with the necessary static method
    class activity_db_dummy:
        @staticmethod
        def get_task(*a, **k):
             log_warning("tool_definitions", "activity_db_dummy.get_task", "Using dummy get_task - DB not imported.")
             return None
    # Define activity_db to point to the dummy
    activity_db = activity_db_dummy

# LLM Interface (for scheduler sub-call)
from services.llm_interface import get_instructor_client
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage

# Utilities
from tools.logger import log_info, log_error, log_warning # Ensure log_warning is used correctly
import yaml
import os
import pydantic # Keep pydantic import

# --- Helper Functions --- (No changes needed in helpers _get_calendar_api_from_state, _parse_scheduler_llm_response, _load_scheduler_prompts)

# Returns GoogleCalendarAPI instance or None
def _get_calendar_api_from_state(user_id) -> Optional[GoogleCalendarAPI]:
    """Helper to retrieve the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api_from_state"
    if not GCAL_API_IMPORTED or GoogleCalendarAPI is None:
        # log_warning("tool_definitions", fn_name, "GoogleCalendarAPI class missing or not imported.") # Can be noisy
        return None
    try:
        state = get_agent_state(user_id)
        if state is not None:
            api = state.get("calendar")
            if isinstance(api, GoogleCalendarAPI) and api.is_active():
                return api
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Error getting calendar API instance for user {user_id}", e)
    return None

# Returns dict or None
def _parse_scheduler_llm_response(raw_text) -> Optional[Dict]:
    """Parses the specific JSON output from the Session Scheduler LLM."""
    fn_name = "_parse_scheduler_llm_response"
    if not raw_text:
        log_warning("tool_definitions", fn_name, "Scheduler response text is empty.")
        return None
    processed_text = None
    try:
        # Try extracting JSON block first
        match = re.search(r"```json\s*({.*?})\s*```", raw_text, re.DOTALL | re.IGNORECASE)
        if match:
             processed_text = match.group(1).strip()
        else:
             # Fallback: Assume the entire response is JSON or find first/last brace
             processed_text = raw_text.strip()
             if not processed_text.startswith("{") or not processed_text.endswith("}"):
                  start_brace = raw_text.find('{'); end_brace = raw_text.rfind('}')
                  if start_brace != -1 and end_brace > start_brace:
                      processed_text = raw_text[start_brace : end_brace + 1].strip()
                      log_warning("tool_definitions", fn_name, "Used find/rfind fallback for JSON extraction.")
                  else:
                      log_warning("tool_definitions", fn_name, "Could not extract JSON block from raw text.");
                      return None
        if not processed_text:
            raise ValueError("Processed text is empty after extraction attempts.")

        data = json.loads(processed_text)
        required_keys = ["proposed_sessions", "response_message"]
        if not isinstance(data, dict) or not all(k in data for k in required_keys):
            missing = [k for k in required_keys if k not in data]; raise ValueError(f"Parsed JSON missing required keys: {missing}")
        if not isinstance(data["proposed_sessions"], list): raise ValueError("'proposed_sessions' key must contain a list.")

        valid_sessions = []
        for i, session_dict in enumerate(data["proposed_sessions"]):
            required_session_keys = ["date", "time", "end_time"]
            if not isinstance(session_dict, dict) or not all(k in session_dict for k in required_session_keys):
                log_warning("tool_definitions", fn_name, f"Skipping invalid session structure: {session_dict}")
                continue
            try:
                 datetime.strptime(session_dict["date"], '%Y-%m-%d'); datetime.strptime(session_dict["time"], '%H:%M'); datetime.strptime(session_dict["end_time"], '%H:%M')
                 ref = session_dict.get("slot_ref");
                 session_dict["slot_ref"] = ref if isinstance(ref, int) and ref > 0 else i + 1
                 valid_sessions.append(session_dict)
            except (ValueError, TypeError) as fmt_err:
                log_warning("tool_definitions", fn_name, f"Skipping session due to format error ({fmt_err}): {session_dict}")

        data["proposed_sessions"] = valid_sessions # Replace with validated list
        log_info("tool_definitions", fn_name, f"Successfully parsed {len(valid_sessions)} valid sessions.")
        return data
    except (json.JSONDecodeError, ValueError) as parse_err:
        log_error("tool_definitions", fn_name, f"Scheduler JSON parsing failed. Error: {parse_err}. Extracted: '{processed_text or 'N/A'}' Raw: '{raw_text[:200]}'", parse_err);
        return None
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Unexpected error parsing scheduler response: {e}", e);
        return None

_SCHEDULER_PROMPTS_CACHE: Dict[str, Optional[str]] = {}
# Returns tuple (sys_prompt_str|None, human_prompt_str|None)
def _load_scheduler_prompts() -> Tuple[Optional[str], Optional[str]]:
    """Loads scheduler system and human prompts from config."""
    fn_name = "_load_scheduler_prompts"
    prompts_path = os.path.join("config", "prompts.yaml"); cache_key = prompts_path + "_scheduler"
    if cache_key in _SCHEDULER_PROMPTS_CACHE: return _SCHEDULER_PROMPTS_CACHE[cache_key] # type: ignore
    sys_prompt: Optional[str] = None
    human_prompt: Optional[str] = None
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f: all_prompts = yaml.safe_load(f)
        if not all_prompts: raise ValueError("YAML prompts file loaded as empty.")
        sys_prompt = all_prompts.get("session_scheduler_system_prompt")
        human_prompt = all_prompts.get("session_scheduler_human_prompt")
        if not sys_prompt or not human_prompt:
            log_error("tool_definitions", fn_name, "One or both scheduler prompts (system/human) are missing in prompts.yaml.")
            sys_prompt, human_prompt = None, None # Ensure both are None if one is missing
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Failed to load scheduler prompts from {prompts_path}: {e}", e)
        sys_prompt, human_prompt = None, None # Ensure None on error
    _SCHEDULER_PROMPTS_CACHE[cache_key] = (sys_prompt, human_prompt);
    return sys_prompt, human_prompt


# =====================================================
# == Pydantic Model Definitions (MUST COME BEFORE TOOLS) ==
# =====================================================

class CreateReminderParams(BaseModel):
    description: str = Field(...)
    date: str = Field(...)
    time: Optional[str] = Field(None)
    project: Optional[str] = Field(None)
    @field_validator('time')
    @classmethod
    def validate_time_format(cls, v: Optional[str]):
        if v is None or v == "": return None
        try: hour, minute = map(int, v.split(':')); return f"{hour:02d}:{minute:02d}"
        except (ValueError, TypeError): raise ValueError("Time must be in HH:MM format (e.g., '14:30') or null/empty")
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")

# Note: This model is for creating the *initial metadata* for a schedulable Task.
# The LLM might call this less often if the flow usually goes directly to propose_task_slots -> finalize_task_and_book_sessions.
class CreateTaskParams(BaseModel):
    description: str = Field(...)
    date: Optional[str] = Field(None, description="Optional: Due date for the task.")
    estimated_duration: Optional[str] = Field(None, description="Optional: Estimated total duration (e.g., '2h', '90m').")
    project: Optional[str] = Field(None)
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: Optional[str]):
        if v is None: return None
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")

# --- NEW MODEL for ToDos ---
class CreateToDoParams(BaseModel):
    description: str = Field(...)
    date: Optional[str] = Field(None, description="Optional: Due date for the ToDo.")
    project: Optional[str] = Field(None)
    estimated_duration: Optional[str] = Field(None, description="Optional: Estimated duration for the ToDo (e.g., '1h', '30m'). For user reference only.") # <-- ADDED
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: Optional[str]):
        if v is None: return None
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")
    # No specific validator needed for estimated_duration here, general string is fine.
    # The LLM will be instructed to provide it in a parseable format if it gets one.
# --- END NEW MODEL ---

class ProposeTaskSlotsParams(BaseModel):
    duration: str = Field(...)
    timeframe: str = Field(...)
    description: Optional[str] = Field(None)
    scheduling_hints: Optional[str] = Field(None, description="User's specific scheduling preferences or constraints provided in natural language, e.g., 'in the afternoon', 'not on Monday', 'needs one continuous block', 'split into sessions'.")
    num_options_to_propose: Optional[int] = Field(3)
    @field_validator('num_options_to_propose')
    @classmethod
    def check_num_options(cls, v: Optional[int]):
        if v is not None and v <= 0: raise ValueError("num_options_to_propose must be positive")
        return v

class FinalizeTaskAndBookSessionsParams(BaseModel):
    search_context: Dict = Field(...)
    approved_slots: List[Dict] = Field(..., min_length=1)
    project: Optional[str] = Field(None)
    @field_validator('approved_slots')
    @classmethod
    def validate_slots_structure(cls, v):
        if not isinstance(v, list) or not v: raise ValueError("approved_slots must be a non-empty list.")
        for i, slot in enumerate(v):
            if not isinstance(slot, dict): raise ValueError(f"Slot {i+1} is not a dict.")
            req_keys = ["date", "time", "end_time"];
            if not all(k in slot for k in req_keys): raise ValueError(f"Slot {i+1} missing required keys: {req_keys}")
            try: datetime.strptime(slot["date"], '%Y-%m-%d')
            except (ValueError, TypeError): raise ValueError(f"Slot {i+1} has invalid date format: {slot['date']}")
            try: datetime.strptime(slot["time"], '%H:%M')
            except (ValueError, TypeError): raise ValueError(f"Slot {i+1} has invalid time format: {slot['time']}")
            try: datetime.strptime(slot["end_time"], '%H:%M')
            except (ValueError, TypeError): raise ValueError(f"Slot {i+1} has invalid end_time format: {slot['end_time']}")
        return v

class UpdateItemDetailsParams(BaseModel):
    item_id: str = Field(...)
    updates: dict = Field(...)
    @field_validator('updates')
    @classmethod
    def check_allowed_keys_and_formats(cls, v: dict):
        allowed_keys = {"description", "date", "time", "estimated_duration", "project"}
        if not v: raise ValueError("Updates dictionary cannot be empty.")
        validated_updates = {}
        for key, value in v.items():
            if key not in allowed_keys: raise ValueError(f"Invalid key '{key}'. Allowed: {', '.join(allowed_keys)}")
            if key == 'date':
                if value is None: validated_updates[key] = None
                else:
                    try: validated_updates[key] = cls.validate_date_format_static(str(value))
                    except ValueError as e: raise ValueError(f"Invalid format for date '{value}': {e}")
            elif key == 'time':
                if value is None: validated_updates[key] = None
                else:
                     try: validated_updates[key] = cls.validate_time_format_static(str(value))
                     except ValueError as e: raise ValueError(f"Invalid format for time '{value}': {e}")
            elif key == 'estimated_duration':
                if value is None or (isinstance(value, str) and value.strip() == ""): validated_updates[key] = None
                elif not isinstance(value, str): raise ValueError("Estimated duration must be a string or null/empty")
                else: validated_updates[key] = value
            else: validated_updates[key] = value # description, project
        if not validated_updates: raise ValueError("Updates dictionary resulted in no valid fields.")
        return validated_updates
    @staticmethod
    def validate_date_format_static(v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")
    @staticmethod
    def validate_time_format_static(v: Optional[str]):
        if v is None: return None
        if v == "": return None
        try: hour, minute = map(int, v.split(':')); return f"{hour:02d}:{minute:02d}"
        except (ValueError, TypeError): raise ValueError("Time must be in HH:MM format (e.g., '14:30') or empty string or null")

class UpdateItemStatusParams(BaseModel):
    item_id: str = Field(...)
    new_status: str = Field(...)
    @field_validator('new_status')
    @classmethod
    def check_item_status(cls, v: str):
        allowed = {"pending", "in_progress", "completed", "cancelled"}
        v_lower = v.lower().replace(" ", "_") # Standardize to snake_case
        if v_lower not in allowed: raise ValueError(f"Status must be one of: {', '.join(allowed)}")
        return v_lower

class UpdateUserPreferencesParams(BaseModel):
    updates: dict = Field(...)
    @field_validator('updates')
    @classmethod
    def check_updates_not_empty(cls, v: dict):
        if not v: raise ValueError("Updates dictionary cannot be empty.")
        return v

class InitiateCalendarConnectionParams(BaseModel):
    pass

class CancelTaskSessionsParams(BaseModel):
    task_id: str = Field(...)
    session_ids_to_cancel: list[str] = Field(..., min_length=1)

class InterpretListReplyParams(BaseModel):
    user_reply: str = Field(...)
    list_mapping: dict = Field(...)

class GetFormattedTaskListParams(BaseModel):
    date_range: Optional[list[str]] = Field(None)
    status_filter: Optional[str] = Field('active')
    project_filter: Optional[str] = Field(None)
    @field_validator('date_range')
    @classmethod
    def validate_and_normalize_date_range(cls, v: Optional[list[str]]):
        if v is None: return v
        if not isinstance(v, list): raise ValueError("date_range must be a list of date strings.")
        if len(v) == 1:
            try:
                the_date = datetime.strptime(v[0], '%Y-%m-%d').date()
                log_warning("tool_definitions", "validate_date_range", f"Received single date {v[0]}, assuming start/end.")
                return [v[0], v[0]]
            except (ValueError, TypeError):
                raise ValueError("Single date provided is not a valid YYYY-MM-DD string.")
        elif len(v) == 2:
            try:
                start_date = datetime.strptime(v[0], '%Y-%m-%d').date()
                end_date = datetime.strptime(v[1], '%Y-%m-%d').date()
                if start_date > end_date: raise ValueError("Start date cannot be after end date.")
                return v
            except (ValueError, TypeError):
                raise ValueError("Dates must be valid YYYY-MM-DD strings.")
        else:
            raise ValueError("date_range must be a list of one or two date strings.")
    @field_validator('status_filter')
    @classmethod
    def check_status_filter(cls, v: Optional[str]):
        if v is None: return 'active'
        allowed = {'active', 'pending', 'in_progress', 'completed', 'all'}
        v_lower = v.lower().replace(" ", "_")
        if v_lower not in allowed:
            log_warning("tool_definitions", "check_status_filter", f"Invalid status_filter '{v}'. Defaulting 'active'.")
            return 'active'
        return v_lower


# =====================================================
# == Tool Function Definitions (MUST COME AFTER MODELS) ==
# =====================================================

# --- Returns dict ---
def create_reminder_tool(user_id, params: CreateReminderParams) -> Dict:
    """Creates a simple reminder, potentially adding it to Google Calendar if time is specified. Sets type='reminder'."""
    fn_name = "create_reminder_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}, desc: '{params.description[:30]}...'")
        data_dict = params.model_dump(exclude_none=True)
        data_dict["type"] = "reminder" # Explicitly set type
        saved_item = task_manager.create_task(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "reminder", "message": f"Reminder '{params.description[:30]}...' created."}
        else:
             log_error("tool_definitions", fn_name, f"Task manager failed create reminder")
             return {"success": False, "item_id": None, "message": "Failed to save reminder."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": None, "message": f"Error: {e}"}

# --- Returns dict ---
def create_task_tool(user_id, params: CreateTaskParams) -> Dict:
    """Creates metadata for a schedulable Task (type='task'). Does not schedule sessions or interact with calendar directly here."""
    fn_name = "create_task_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}, desc: '{params.description[:30]}...'")
        data_dict = params.model_dump(exclude_none=True)
        data_dict["type"] = "task" # Explicitly set type
        if "time" in data_dict: del data_dict["time"] # Tasks don't have specific start time in metadata
        # Note: 'date' might be due date, 'estimated_duration' stored for later scheduling.
        saved_item = task_manager.create_task(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "task", "estimated_duration": saved_item.get("estimated_duration"), "message": f"Task '{params.description[:30]}...' metadata created."}
        else:
             log_error("tool_definitions", fn_name, f"Task manager failed create task metadata")
             return {"success": False, "item_id": None, "message": "Failed to save task metadata."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": None, "message": f"Error: {e}"}

# --- NEW TOOL FUNCTION ---
# --- Returns dict ---
def create_todo_tool(user_id, params: CreateToDoParams) -> Dict:
    """Creates a simple ToDo item (type='todo') for tracking, without calendar scheduling."""
    fn_name = "create_todo_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}, desc: '{params.description[:30]}...'")
        data_dict = params.model_dump(exclude_none=True)
        data_dict["type"] = "todo" # Explicitly set type
        # Fields like time, duration are not relevant for ToDo creation
        saved_item = task_manager.create_task(user_id, data_dict) # Use the same underlying service function
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "todo", "message": f"ToDo '{params.description[:30]}...' added to your list."}
        else:
             log_error("tool_definitions", fn_name, f"Task manager failed create ToDo")
             return {"success": False, "item_id": None, "message": "Failed to save ToDo."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": None, "message": f"Error: {e}"}
# --- END NEW TOOL FUNCTION ---

# --- Returns dict ---
def propose_task_slots_tool(user_id, params: ProposeTaskSlotsParams) -> Dict:
    """
    Finds available work session slots for a Task based on duration/timeframe and hints.
    Uses an LLM sub-call for intelligent slot finding, considering calendar events.
    Returns proposed slots and the search context used.
    """
    # --- Function body remains the same ---
    fn_name = "propose_task_slots_tool"
    fail_result = {"success": False, "proposed_slots": None, "message": "Sorry, I encountered an issue trying to propose schedule slots.", "search_context": None}
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}. Search: duration='{params.duration}', timeframe='{params.timeframe}', hints='{params.scheduling_hints}'")
        search_context_to_return = params.model_dump() # Store validated input as basis for context

        llm_client = get_instructor_client()
        if not llm_client:
             log_error("tool_definitions", fn_name, "LLM client unavailable.")
             return {**fail_result, "message": "Scheduler resources unavailable (LLM Client)."}

        sys_prompt, human_prompt = _load_scheduler_prompts()
        if not sys_prompt or not human_prompt:
             log_error("tool_definitions", fn_name, "Scheduler prompts failed load.")
             return {**fail_result, "message": "Scheduler resources unavailable (Prompts)."}

        agent_state = get_agent_state(user_id);
        prefs = agent_state.get("preferences", {}) if agent_state else {}
        calendar_api = _get_calendar_api_from_state(user_id)
        preferred_session_str = prefs.get("Preferred_Session_Length", "60m")

        task_estimated_duration_str = params.duration;
        total_minutes = task_manager._parse_duration_to_minutes(task_estimated_duration_str)
        if total_minutes is None:
             log_error("tool_definitions", fn_name, f"Invalid duration '{task_estimated_duration_str}'")
             return {**fail_result, "message": f"Invalid duration format: '{task_estimated_duration_str}'. Use 'Xh' or 'Ym'."}

        session_minutes = task_manager._parse_duration_to_minutes(preferred_session_str) or 60
        num_slots_to_find = 1
        slot_duration_str = task_estimated_duration_str
        hints_lower = (params.scheduling_hints or "").lower()

        needs_split = False
        if "continuous" in hints_lower or "one block" in hints_lower or "one slot" in hints_lower:
             needs_split = False
             log_info("tool_definitions", fn_name, "Hint indicates continuous block required.")
        elif "split" in hints_lower or "separate" in hints_lower or "multiple sessions" in hints_lower:
             needs_split = True
             log_info("tool_definitions", fn_name, "Hint indicates split sessions required.")
        elif session_minutes > 0 and total_minutes > session_minutes:
             needs_split = True
             log_info("tool_definitions", fn_name, "Defaulting to split sessions (total > preferred, no specific hint).")

        if needs_split and session_minutes > 0:
             num_slots_to_find = (total_minutes + session_minutes - 1) // session_minutes
             if num_slots_to_find <= 0 : num_slots_to_find = 1
             slot_duration_str = preferred_session_str
             log_info("tool_definitions", fn_name, f"Calculated need for {num_slots_to_find} split sessions of duration: {slot_duration_str}")
        else:
            log_info("tool_definitions", fn_name, f"Calculated need for 1 continuous block of duration: {slot_duration_str}")

        today = datetime.now().date(); start_date = today + timedelta(days=1); due_date_for_search = None
        tf = params.timeframe.lower()
        iso_interval_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z))/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z))", params.timeframe)
        if iso_interval_match:
             try:
                  start_iso, end_iso = iso_interval_match.groups()
                  start_dt_aware = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
                  end_dt_aware = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))
                  start_date = start_dt_aware.date()
                  start_date = max(start_date, today + timedelta(days=1))
                  due_date_for_search = end_dt_aware.date()
                  log_info("tool_definitions", fn_name, f"Parsed ISO Interval: Start={start_date}, EffectiveDue={due_date_for_search}")
             except ValueError as iso_parse_err:
                  log_warning("tool_definitions", fn_name, f"Failed to parse ISO interval timeframe '{params.timeframe}': {iso_parse_err}")
        elif "tomorrow" in tf: start_date = today + timedelta(days=1); due_date_for_search = start_date
        elif "next week" in tf:
             start_of_next_week = today + timedelta(days=(7 - today.weekday()))
             start_date = start_of_next_week
             due_date_for_search = start_of_next_week + timedelta(days=6)
             log_info("tool_definitions", fn_name, f"Parsed 'next week': Start={start_date}, EffectiveDue={due_date_for_search}")
        elif "on " in tf:
            try: date_part = tf.split("on ")[1].strip(); parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date(); start_date = max(parsed_date, today + timedelta(days=1)); due_date_for_search = start_date
            except Exception: log_warning("tool_definitions", fn_name, f"Parsing timeframe date failed: '{params.timeframe}'")
        elif "by " in tf:
            try: date_part = tf.split("by ")[1].strip(); parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date(); due_date_for_search = parsed_date; start_date = max(today + timedelta(days=1), parsed_date - timedelta(days=14))
            except Exception: log_warning("tool_definitions", fn_name, f"Parsing timeframe 'by' date failed: '{params.timeframe}'")
        elif "later today" in tf or "tonight" in tf or "this afternoon" in tf:
             start_date = today
             due_date_for_search = today
             log_info("tool_definitions", fn_name, f"Parsed relative timeframe '{tf}': Start={start_date}, EffectiveDue={due_date_for_search}")
        else: log_warning("tool_definitions", fn_name, f"Unclear timeframe '{params.timeframe}', using default window.")

        default_horizon_days = 56; end_date_limit = start_date + timedelta(days=default_horizon_days - 1)
        end_date = end_date_limit
        if due_date_for_search and due_date_for_search >= start_date:
             buffer_days = 0 if start_date == due_date_for_search else 1
             end_date = min(end_date_limit, due_date_for_search - timedelta(days=buffer_days))
        end_date = max(end_date, start_date);
        start_date_str, end_date_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        log_info("tool_definitions", fn_name, f"Derived Search Range: {start_date_str} to {end_date_str}")
        search_context_to_return["effective_due_date"] = due_date_for_search.strftime("%Y-%m-%d") if due_date_for_search else None
        search_context_to_return["search_start_date"] = start_date_str
        search_context_to_return["search_end_date"] = end_date_str
        search_context_to_return["original_timeframe"] = params.timeframe

        existing_events = [];
        if calendar_api is not None:
            log_info("tool_definitions", fn_name, f"Fetching GCal events from {start_date_str} to {end_date_str}")
            if start_date <= end_date:
                try:
                    events_raw = calendar_api.list_events(start_date_str, end_date_str)
                    existing_events = [
                         {"start_datetime": ev.get("gcal_start_datetime"), "end_datetime": ev.get("gcal_end_datetime"), "summary": ev.get("title")}
                         for ev in events_raw if ev.get("gcal_start_datetime") and ev.get("gcal_end_datetime")
                    ]
                    log_info("tool_definitions", fn_name, f"Fetched {len(existing_events)} valid GCal events.")
                except Exception as e:
                     log_error("tool_definitions", fn_name, f"Fetch GCal events failed: {e}", e)
            else:
                 log_warning("tool_definitions", fn_name, f"Invalid search range ({start_date_str} > {end_date_str}). Skipping GCal fetch.")
        else:
            log_warning("tool_definitions", fn_name, "GCal API inactive or unavailable. Proposing slots without checking calendar conflicts.")

        try:
            slots_to_request_from_llm = num_slots_to_find
            prompt_data = {
                "task_description": params.description or "(No description)",
                "task_due_date": search_context_to_return["effective_due_date"] or "(No specific due date)",
                "task_estimated_duration": task_estimated_duration_str,
                "user_working_days": prefs.get("Work_Days", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
                "user_work_start_time": prefs.get("Work_Start_Time", "09:00"),
                "user_work_end_time": prefs.get("Work_End_Time", "17:00"),
                "user_session_length": slot_duration_str,
                "existing_events_json": json.dumps(existing_events),
                "current_date": today.strftime("%Y-%m-%d"),
                "num_slots_requested": slots_to_request_from_llm,
                "search_start_date": start_date_str,
                "search_end_date": end_date_str,
                "scheduling_hints": params.scheduling_hints or "None"
            }
            log_info("tool_definitions", fn_name, f"Scheduler prompt data prepared (requesting {slots_to_request_from_llm} slots, event count: {len(existing_events)}).")
        except Exception as e:
             log_error("tool_definitions", fn_name, f"Failed prepare prompt data for scheduler: {e}", e)
             return {**fail_result, "message": "Failed prepare data for scheduler."}

        raw_llm_output = None; parsed_data = None
        try:
            fmt_human = human_prompt.format(**prompt_data)
            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": fmt_human}]
            log_info("tool_definitions", fn_name, ">>> Invoking Session Scheduler LLM...")
            sched_resp = llm_client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.2, response_format={"type": "json_object"}) # type: ignore
            raw_llm_output = sched_resp.choices[0].message.content
            parsed_data = _parse_scheduler_llm_response(raw_llm_output)
            if parsed_data is None:
                log_error("tool_definitions", fn_name, "Scheduler response parse failed or returned invalid structure.")
                return {**fail_result, "message": "Received invalid proposals format from scheduler."}

            log_info("tool_definitions", f"{fn_name}_DEBUG", f"Parsed Scheduler LLM Resp: {json.dumps(parsed_data, indent=2)}")
            num_returned = len(parsed_data.get("proposed_sessions", []))
            if num_returned < num_slots_to_find:
                 log_warning("tool_definitions", fn_name, f"Sub-LLM returned only {num_returned} slots, but {num_slots_to_find} were needed.")
                 parsed_data["response_message"] += f" (Note: Only found {num_returned} of the {num_slots_to_find} required slots)."
            log_info("tool_definitions", fn_name, f"Scheduler LLM processing successful.")
            return {"success": True, "proposed_slots": parsed_data.get("proposed_sessions"), "message": parsed_data.get("response_message", "..."), "search_context": search_context_to_return}
        except Exception as e:
            tb_str = traceback.format_exc()
            log_error("tool_definitions", fn_name, f"Scheduler LLM invoke/process error: {e}. Raw: '{raw_llm_output}'. Parsed: {parsed_data}\nTraceback:\n{tb_str}", e)
            return {**fail_result, "message": "Error during slot finding process."}

    except pydantic.ValidationError as e:
        log_error("tool_definitions", fn_name, f"Validation Error: {e}");
        return {"success": False, "proposed_slots": None, "message": f"Invalid parameters: {e}", "search_context": None}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error in propose_task_slots: {e}", e)
         return fail_result

# --- Returns dict ---
def finalize_task_and_book_sessions_tool(user_id, params: FinalizeTaskAndBookSessionsParams) -> Dict:
    """Creates task metadata (type='task') AND books the approved work sessions in GCal."""
    # --- Function body remains the same ---
    fn_name = "finalize_task_and_book_sessions_tool"
    item_id: Optional[str] = None # Initialize item_id
    task_title = "(Error getting title)" # Initialize task_title
    try:
        search_context = params.search_context; approved_slots = params.approved_slots
        log_info("tool_definitions", fn_name, f"Executing finalize+book user={user_id}, desc='{search_context.get('description')}', slots={len(approved_slots)}")
        if not search_context or not approved_slots:
            log_error("tool_definitions", fn_name, "Missing search_context or approved_slots.")
            return {"success": False, "item_id": None, "booked_count": 0, "message": "Internal error: Missing context or slots to book."}

        try:
            task_metadata_payload = {
                 "description": search_context.get("description", "Untitled Task"),
                 "estimated_duration": search_context.get("duration"),
                 "type": "task", # Explicitly set type for Task
                 "project": params.project,
                 "date": approved_slots[0].get("date"),
                 "time": approved_slots[0].get("time"),
                 # Include effective due date if available in context
                 "original_date": search_context.get("effective_due_date"),
            }
            task_title = task_metadata_payload["description"]

            # Use task_manager.create_task which handles DB insertion
            created_meta = task_manager.create_task(user_id, task_metadata_payload)

            if created_meta and created_meta.get("event_id"):
                item_id = created_meta["event_id"]
                log_info("tool_definitions", fn_name, f"Task metadata created successfully via task_manager: {item_id}");
            else:
                 raise ValueError("Failed to create task metadata via task_manager.")

        except Exception as create_err:
            log_error("tool_definitions", fn_name, f"Metadata creation failed: {create_err}", create_err)
            return {"success": False, "item_id": None, "booked_count": 0, "message": "Failed to save the task details before scheduling."}

        # Proceed to booking only if metadata creation succeeded
        booking_result = task_manager.schedule_work_sessions(user_id, item_id, approved_slots)
        if booking_result.get("success"):
            log_info("tool_definitions", fn_name, f"Booked {booking_result.get('booked_count', 0)} sessions for {item_id}")
            return {
                "success": True,
                "item_id": item_id,
                "booked_count": booking_result.get("booked_count", 0),
                "message": booking_result.get("message", f"Task '{task_title[:30]}...' created & sessions booked.")
            }
        else:
            log_error("tool_definitions", fn_name, f"Metadata created ({item_id}), but booking sessions failed: {booking_result.get('message')}")
            return {
                "success": False,
                "item_id": item_id,
                "booked_count": 0,
                "message": f"Task '{task_title[:30]}...' was created, but scheduling sessions failed: {booking_result.get('message')}"
            }
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "booked_count": 0, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": item_id, "booked_count": 0, "message": f"Error: {e}"}


# --- Returns dict ---
def update_item_details_tool(user_id, params: UpdateItemDetailsParams) -> Dict:
    """Updates core details (desc, date, time, estimate, project) of ANY item type (Task, Reminder, ToDo)."""
    # --- Function body remains the same ---
    fn_name = "update_item_details_tool";
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, item={params.item_id}, updates={list(params.updates.keys())}")
        updated_item = task_manager.update_task(user_id, params.item_id, params.updates)

        if updated_item:
             return {"success": True, "message": f"Item '{params.item_id[:8]}...' updated successfully."}
        else:
             log_warning("tool_definitions", fn_name, f"Update failed for item {params.item_id} (not found or no change?).")
             item_exists = False
             if DB_IMPORTED: item_exists = activity_db.get_task(params.item_id) is not None
             if item_exists:
                  return {"success": False, "message": f"Failed to apply updates to item {params.item_id[:8]}... (perhaps no change or internal error)."}
             else:
                  return {"success": False, "message": f"Item {params.item_id[:8]}... not found."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}", e, user_id=user_id)
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e, user_id=user_id)
         return {"success": False, "message": f"Error during update: {e}."}

# --- Returns dict ---
def update_item_status_tool(user_id, params: UpdateItemStatusParams) -> Dict:
    """Changes status OR cancels/deletes ANY item type. Requires existing item_id."""
    # --- Function body remains the same ---
    fn_name = "update_item_status_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, item={params.item_id}, status={params.new_status}")
        success = False; message = ""
        if params.new_status == "cancelled":
            success = task_manager.cancel_item(user_id, params.item_id)
            message = f"Item '{params.item_id[:8]}...' cancel processed. Result: {'Success' if success else 'Failed/Not Found'}."
        else:
            updated_item = task_manager.update_task_status(user_id, params.item_id, params.new_status)
            success = updated_item is not None
            message = f"Status update to '{params.new_status}' for item '{params.item_id[:8]}...' {'succeeded' if success else 'failed/not found'}."
        return {"success": success, "message": message}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "message": f"Error updating status: {e}."}

# --- Returns dict ---
def update_user_preferences_tool(user_id, params: UpdateUserPreferencesParams) -> Dict:
    # --- Function body remains the same ---
    fn_name = "update_user_preferences_tool"
    try:
        update_keys = list(params.updates.keys()) if params.updates else []
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, updates={update_keys}")
        success = config_manager.update_preferences(user_id, params.updates)
        return {"success": success, "message": f"Preferences update {'succeeded' if success else 'failed'}."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "message": f"Error: {e}."}

# --- Returns dict ---
def initiate_calendar_connection_tool(user_id, params: InitiateCalendarConnectionParams) -> Dict:
    # --- Function body remains the same ---
    fn_name = "initiate_calendar_connection_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}")
        result_dict = config_manager.initiate_calendar_auth(user_id)
        result_dict["success"] = result_dict.get("status") in ["pending", "token_exists"]
        return result_dict
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "status": "fails", "message": f"Error: {e}."}

# --- Returns dict ---
def cancel_task_sessions_tool(user_id, params: CancelTaskSessionsParams) -> Dict:
    """Removes specific scheduled GCal sessions linked explicitly to a Task and updates Task metadata."""
    fn_name = "cancel_task_sessions_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, Task={params.task_id}, SessionIDs={params.session_ids_to_cancel}")

        # --- ADDED TYPE CHECK ---
        item_metadata = None
        if DB_IMPORTED:
            item_metadata = activity_db.get_task(params.task_id)

        if item_metadata is None:
            return {"success": False, "cancelled_count": 0, "message": f"Task {params.task_id[:8]}... not found."}
        if item_metadata.get("type") != "task":
            log_warning("tool_definitions", fn_name, f"Attempted to cancel sessions for non-task item {params.task_id} (type: {item_metadata.get('type')}).")
            return {"success": False, "cancelled_count": 0, "message": "Cannot cancel sessions for items that are not Tasks."}
        # --- END ADDED TYPE CHECK ---

        result = task_manager.cancel_sessions(user_id, params.task_id, params.session_ids_to_cancel)
        return result
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "cancelled_count": 0, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "cancelled_count": 0, "message": f"Error: {e}."}

# --- Returns dict ---
def interpret_list_reply_tool(user_id, params: InterpretListReplyParams) -> Dict:
    """Placeholder tool to interpret replies to numbered lists."""
    # --- Function body remains the same ---
    fn_name = "interpret_list_reply_tool"
    try:
        log_warning("tool_definitions", fn_name, f"Tool executed for user {user_id} - Implementation is basic.")
        extracted_numbers = [int(s) for s in re.findall(r'\b\d+\b', params.user_reply)]
        identified_item_ids = []
        if params.list_mapping:
             identified_item_ids = [params.list_mapping.get(str(num)) for num in extracted_numbers if str(num) in params.list_mapping]
             identified_item_ids = [item_id for item_id in identified_item_ids if item_id is not None]

        if identified_item_ids:
             log_info("tool_definitions", fn_name, f"Identified numbers {extracted_numbers} mapping to IDs: {identified_item_ids}")
             return { "success": True, "action": "process", "item_ids": identified_item_ids, "message": f"Identified item number(s): {', '.join(map(str, extracted_numbers))}." }
        else:
             log_info("tool_definitions", fn_name, f"No valid item numbers found in reply: '{params.user_reply}'")
             return {"success": False, "item_ids": [], "message": "Couldn't find any valid item numbers in your reply."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_ids": [], "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Error parsing list reply: {e}", e)
         return {"success": False, "item_ids": [], "message": "Sorry, I had trouble interpreting your reply."}

# --- Returns dict ---
def get_formatted_task_list_tool(user_id, params: GetFormattedTaskListParams) -> Dict:
    """Retrieves and formats a list of items (any type) based on filters."""
    # --- Function body remains the same ---
    fn_name = "get_formatted_task_list_tool"
    try:
        status_filter = params.status_filter or 'active'
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, Filter={status_filter}, Proj={params.project_filter}, Range={params.date_range}")
        date_range_tuple = tuple(params.date_range) if params.date_range else None
        list_body, list_mapping = task_query_service.get_formatted_list(
             user_id=user_id,
             date_range=date_range_tuple,
             status_filter=status_filter,
             project_filter=params.project_filter
        )
        item_count = len(list_mapping);
        message = f"Found {item_count} item(s)." if item_count > 0 else "No items found matching your criteria."
        if item_count > 0 and not list_body:
             log_warning("tool_definitions", fn_name, f"Found {item_count} items but list body is empty.")
             message = f"Found {item_count}, but there was an error formatting the list."
        return {"success": True, "list_body": list_body or "", "list_mapping": list_mapping or {}, "count": item_count, "message": message}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "list_body": "", "list_mapping": {}, "count": 0, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "list_body": "", "list_mapping": {}, "count": 0, "message": f"Sorry, an error occurred while retrieving the list: {e}."}


# =====================================================
# == Tool Dictionaries (MUST COME AFTER MODELS & FUNCS) ==
# =====================================================

AVAILABLE_TOOLS = {
    "create_reminder": create_reminder_tool,
    "create_task": create_task_tool, # For schedulable Tasks
    "create_todo": create_todo_tool,   # <-- NEW
    "propose_task_slots": propose_task_slots_tool,
    "finalize_task_and_book_sessions": finalize_task_and_book_sessions_tool,
    "update_item_details": update_item_details_tool,
    "update_item_status": update_item_status_tool,
    "update_user_preferences": update_user_preferences_tool,
    "initiate_calendar_connection": initiate_calendar_connection_tool,
    "cancel_task_sessions": cancel_task_sessions_tool, # Only for Tasks
    "interpret_list_reply": interpret_list_reply_tool,
    "get_formatted_task_list": get_formatted_task_list_tool
}

TOOL_PARAM_MODELS = {
    "create_reminder": CreateReminderParams,
    "create_task": CreateTaskParams, # For schedulable Tasks
    "create_todo": CreateToDoParams,   # <-- NEW
    "propose_task_slots": ProposeTaskSlotsParams,
    "finalize_task_and_book_sessions": FinalizeTaskAndBookSessionsParams,
    "update_item_details": UpdateItemDetailsParams,
    "update_item_status": UpdateItemStatusParams,
    "update_user_preferences": UpdateUserPreferencesParams,
    "initiate_calendar_connection": InitiateCalendarConnectionParams,
    "cancel_task_sessions": CancelTaskSessionsParams, # Only for Tasks
    "interpret_list_reply": InterpretListReplyParams,
    "get_formatted_task_list": GetFormattedTaskListParams
}

# --- END OF FULL agents/tool_definitions.py ---