# --- START OF FULL agents/tool_definitions.py ---

from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Dict, List, Any, Tuple # Removed Optional
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
from tools import metadata_store
# Import the class itself for type checking if needed, handle import error
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
     GoogleCalendarAPI = None
     GCAL_API_IMPORTED = False


# LLM Interface (for scheduler sub-call)
from services.llm_interface import get_instructor_client
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage

# Utilities
from tools.logger import log_info, log_error, log_warning # Ensure log_warning is used correctly
import yaml
import os
import pydantic # Keep pydantic import

# --- Helper Functions ---

# Returns GoogleCalendarAPI instance or None
def _get_calendar_api_from_state(user_id):
    """Helper to retrieve the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api_from_state"
    if not GCAL_API_IMPORTED or GoogleCalendarAPI is None:
        log_warning("tool_definitions", fn_name, "GoogleCalendarAPI class missing or not imported.")
        return None
    try:
        # Assume get_agent_state is available and returns a dict or None
        state = get_agent_state(user_id)
        if state is not None: # Check if state exists
            api = state.get("calendar")
            # Check if it's the right type AND active
            if isinstance(api, GoogleCalendarAPI) and api.is_active():
                return api # Return the active API instance
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Error getting calendar API instance for user {user_id}", e)
    # Return None if state is None, calendar key missing, not GCalAPI type, or not active
    return None

# Returns dict or None
def _parse_scheduler_llm_response(raw_text):
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

        # Validate individual session formats
        valid_sessions = []
        for i, session_dict in enumerate(data["proposed_sessions"]):
            required_session_keys = ["date", "time", "end_time"]
            if not isinstance(session_dict, dict) or not all(k in session_dict for k in required_session_keys):
                log_warning("tool_definitions", fn_name, f"Skipping invalid session structure: {session_dict}")
                continue
            try:
                 # Validate formats strictly
                 datetime.strptime(session_dict["date"], '%Y-%m-%d'); datetime.strptime(session_dict["time"], '%H:%M'); datetime.strptime(session_dict["end_time"], '%H:%M')
                 # Ensure slot_ref is a positive integer, assign if missing/invalid
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

_SCHEDULER_PROMPTS_CACHE = {}
# Returns tuple (sys_prompt_str|None, human_prompt_str|None)
def _load_scheduler_prompts():
    """Loads scheduler system and human prompts from config."""
    fn_name = "_load_scheduler_prompts"
    prompts_path = os.path.join("config", "prompts.yaml"); cache_key = prompts_path + "_scheduler"
    if cache_key in _SCHEDULER_PROMPTS_CACHE: return _SCHEDULER_PROMPTS_CACHE[cache_key]
    sys_prompt, human_prompt = None, None
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
# --- NOTE: Pydantic models still use typing hints including '| None' implicitly ---
# --- This is standard for Pydantic and necessary for optional fields ---
# --- The user request was mainly about FUNCTION return type hints ---

class CreateReminderParams(BaseModel):
    description: str = Field(...)
    date: str = Field(...)
    time: str | None = Field(None) # Pydantic handles Optional fields like this
    project: str | None = Field(None)
    @field_validator('time')
    @classmethod
    def validate_time_format(cls, v: str | None):
        if v is None or v == "": return None
        try: hour, minute = map(int, v.split(':')); return f"{hour:02d}:{minute:02d}"
        except (ValueError, TypeError): raise ValueError("Time must be in HH:MM format (e.g., '14:30') or null/empty")
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")

class CreateTaskParams(BaseModel):
    description: str = Field(...)
    date: str = Field(...)
    estimated_duration: str | None = Field(None)
    project: str | None = Field(None)
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")

class ProposeTaskSlotsParams(BaseModel):
    duration: str = Field(...)
    timeframe: str = Field(...)
    description: str | None = Field(None)
    split_preference: str | None = Field(None)
    num_options_to_propose: int | None = Field(3) # Allow None, default applied in code if needed
    @field_validator('num_options_to_propose')
    @classmethod
    def check_num_options(cls, v: int | None):
        if v is not None and v <= 0: raise ValueError("num_options_to_propose must be positive")
        return v

class FinalizeTaskAndBookSessionsParams(BaseModel):
    search_context: Dict = Field(...)
    approved_slots: List[Dict] = Field(..., min_length=1)
    project: str | None = Field(None)
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
                # Allow time to be explicitly set to None
                if value is None: validated_updates[key] = None
                else:
                     try: validated_updates[key] = cls.validate_time_format_static(str(value)) # Use static method
                     except ValueError as e: raise ValueError(f"Invalid format for time '{value}': {e}")
            elif key == 'estimated_duration':
                if value is None or (isinstance(value, str) and value.strip() == ""): validated_updates[key] = None
                elif not isinstance(value, str): raise ValueError("Estimated duration must be a string or null/empty")
                else: validated_updates[key] = value
            else: validated_updates[key] = value # description, project
        if not validated_updates: raise ValueError("Updates dictionary resulted in no valid fields.") # Check after processing
        return validated_updates
    @staticmethod
    def validate_date_format_static(v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")
    @staticmethod
    def validate_time_format_static(v: str): # Made non-optional as None handled above
        if v == "": return None # Treat empty string as clearing time
        try: hour, minute = map(int, v.split(':')); return f"{hour:02d}:{minute:02d}"
        except (ValueError, TypeError): raise ValueError("Time must be in HH:MM format (e.g., '14:30') or empty string")

class UpdateItemStatusParams(BaseModel):
    item_id: str = Field(...)
    new_status: str = Field(...)
    @field_validator('new_status')
    @classmethod
    def check_item_status(cls, v: str):
        allowed = {"pending", "in_progress", "completed", "cancelled"} # Removed "in progress" with space
        v_lower = v.lower().replace(" ", "") # Standardize to remove space if present
        if v_lower not in allowed: raise ValueError(f"Status must be one of: {', '.join(allowed)}")
        return v_lower # Return standardized status

class UpdateUserPreferencesParams(BaseModel):
    updates: dict = Field(...)
    @field_validator('updates')
    @classmethod
    def check_updates_not_empty(cls, v: dict):
        if not v: raise ValueError("Updates dictionary cannot be empty.")
        # Add more specific validation based on user_registry.DEFAULT_PREFERENCES keys/formats if needed
        return v

class InitiateCalendarConnectionParams(BaseModel):
    pass # No parameters needed

class CancelTaskSessionsParams(BaseModel):
    task_id: str = Field(...)
    session_ids_to_cancel: list[str] = Field(..., min_length=1)

class InterpretListReplyParams(BaseModel):
    user_reply: str = Field(...)
    list_mapping: dict = Field(...)

class GetFormattedTaskListParams(BaseModel):
    date_range: list[str] | None = Field(None)
    status_filter: str | None = Field('active') # Allow None, default in code
    project_filter: str | None = Field(None)
    @field_validator('date_range')
    @classmethod
    def validate_date_range(cls, v: list[str] | None):
        if v is None: return v
        if not isinstance(v, list) or len(v) != 2: raise ValueError("date_range must be a list of two date strings.")
        try:
            start_date = datetime.strptime(v[0], '%Y-%m-%d').date(); end_date = datetime.strptime(v[1], '%Y-%m-%d').date()
            if start_date > end_date: raise ValueError("Start date cannot be after end date.")
            return v
        except (ValueError, TypeError): raise ValueError("Dates must be valid YYYY-MM-DD strings.")
    @field_validator('status_filter')
    @classmethod
    def check_status_filter(cls, v: str | None):
        if v is None: return 'active' # Default if None
        allowed = {'active', 'pending', 'in_progress', 'completed', 'all'}
        v_lower = v.lower().replace(" ", "")
        if v_lower not in allowed:
            log_warning("tool_definitions", "check_status_filter", f"Invalid status_filter '{v}'. Defaulting 'active'.")
            return 'active'
        return v_lower


# =====================================================
# == Tool Function Definitions (MUST COME AFTER MODELS) ==
# =====================================================

# Returns dict
def create_reminder_tool(user_id, params: CreateReminderParams):
    """Creates a simple reminder, potentially adding it to Google Calendar if time is specified."""
    fn_name = "create_reminder_tool"
    # Pydantic validation happens implicitly when type hint is used
    # We still keep the try-except block for runtime safety but remove explicit re-validation
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}, desc: '{params.description[:30]}...'")
        data_dict = params.model_dump(exclude_none=True); data_dict["type"] = "reminder"
        # Call service layer function
        saved_item = task_manager.create_task(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "reminder", "message": f"Reminder '{params.description[:30]}...' created."}
        else:
             log_error("tool_definitions", fn_name, f"Task manager failed create reminder")
             return {"success": False, "item_id": None, "message": "Failed to save reminder."}
    except pydantic.ValidationError as e: # Catch validation errors if they bypass type hint somehow
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": None, "message": f"Error: {e}"}

# Returns dict
def create_task_tool(user_id, params: CreateTaskParams):
    """Creates task metadata ONLY. Does not schedule sessions or interact with calendar."""
    fn_name = "create_task_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}, desc: '{params.description[:30]}...'")
        data_dict = params.model_dump(exclude_none=True); data_dict["type"] = "task"
        if "time" in data_dict: del data_dict["time"] # Tasks don't have specific start time in metadata usually
        saved_item = task_manager.create_task(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            # Return duration if available, useful for orchestrator to ask about scheduling
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "task", "estimated_duration": saved_item.get("estimated_duration"), "message": f"Task '{params.description[:30]}...' created (metadata only)."}
        else:
             log_error("tool_definitions", fn_name, f"Task manager failed create task metadata")
             return {"success": False, "item_id": None, "message": "Failed to save task."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "item_id": None, "message": f"Error: {e}"}

# Returns dict
def propose_task_slots_tool(user_id, params: ProposeTaskSlotsParams):
    """
    Finds available work session slots based on duration/timeframe BEFORE task creation.
    Uses an LLM sub-call for intelligent slot finding, considering calendar events.
    Returns proposed slots and the search context used.
    """
    fn_name = "propose_task_slots_tool"
    fail_result = {"success": False, "proposed_slots": None, "message": "Sorry, I encountered an issue trying to propose schedule slots.", "search_context": None}
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}. Search: duration='{params.duration}', timeframe='{params.timeframe}'...")
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
        calendar_api = _get_calendar_api_from_state(user_id) # Returns instance or None
        preferred_session_str = prefs.get("Preferred_Session_Length", "60m")

        task_estimated_duration_str = params.duration;
        total_minutes = task_manager._parse_duration_to_minutes(task_estimated_duration_str)
        if total_minutes is None:
             log_error("tool_definitions", fn_name, f"Invalid duration '{task_estimated_duration_str}'")
             return {**fail_result, "message": f"Invalid duration format: '{task_estimated_duration_str}'. Use 'Xh' or 'Ym'."}

        # Determine slot duration and count
        session_minutes = task_manager._parse_duration_to_minutes(preferred_session_str) or 60
        num_slots_to_find = 1;
        slot_duration_str = task_estimated_duration_str # Default: find one continuous block
        split_pref = params.split_preference.lower() if params.split_preference else None

        # Split if requested OR if task > preferred session and preference not set to continuous
        if split_pref == 'separate' or (split_pref is None and total_minutes > session_minutes):
            if session_minutes > 0:
                num_slots_to_find = (total_minutes + session_minutes - 1) // session_minutes # Ceiling division
            else: num_slots_to_find = 1 # Avoid division by zero if session_minutes is bad
            if num_slots_to_find <= 0 : num_slots_to_find = 1 # Ensure at least one slot
            slot_duration_str = preferred_session_str
            log_info("tool_definitions", fn_name, f"Requesting {num_slots_to_find} split sessions of duration: {slot_duration_str}")
        elif split_pref == 'continuous':
            log_info("tool_definitions", fn_name, f"Requesting 1 continuous block of duration: {slot_duration_str}")
        else: # Default case (task <= preferred session, or error in logic)
            log_info("tool_definitions", fn_name, f"Requesting 1 block of duration: {slot_duration_str}")

        # Determine search date range based on timeframe
        # --- This logic seems complex but reasonable for interpreting common phrases ---
        # --- Keeping it as is for now ---
        today = datetime.now().date(); start_date = today + timedelta(days=1); due_date_for_search = None
        tf = params.timeframe.lower()
        if "tomorrow" in tf: start_date = today + timedelta(days=1); due_date_for_search = start_date
        elif "next week" in tf: start_of_next_week = today + timedelta(days=(7 - today.weekday())); start_date = start_of_next_week; due_date_for_search = start_of_next_week + timedelta(days=6)
        elif "on " in tf:
            try: date_part = tf.split("on ")[1].strip(); parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date(); start_date = max(parsed_date, today + timedelta(days=1)); due_date_for_search = start_date
            except Exception: log_warning("tool_definitions", fn_name, f"Parsing timeframe date failed: '{params.timeframe}'")
        elif "by " in tf:
            try: date_part = tf.split("by ")[1].strip(); parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date(); due_date_for_search = parsed_date; start_date = max(today + timedelta(days=1), parsed_date - timedelta(days=14)) # Search up to 2 weeks before 'by' date
            except Exception: log_warning("tool_definitions", fn_name, f"Parsing timeframe 'by' date failed: '{params.timeframe}'")
        else: log_warning("tool_definitions", fn_name, f"Unclear timeframe '{params.timeframe}', using default window.")

        # Calculate end date, ensuring buffer before due date
        default_horizon_days = 56; end_date_limit = start_date + timedelta(days=default_horizon_days - 1)
        # Set end_date to 1 day before due date if due date exists and is after start_date
        end_date = end_date_limit
        if due_date_for_search and due_date_for_search > start_date:
             end_date = min(end_date_limit, due_date_for_search - timedelta(days=1))
        end_date = max(end_date, start_date); # Ensure end is not before start
        start_date_str, end_date_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        log_info("tool_definitions", fn_name, f"Derived Search Range: {start_date_str} to {end_date_str}")
        search_context_to_return["effective_due_date"] = due_date_for_search.strftime("%Y-%m-%d") if due_date_for_search else None
        search_context_to_return["search_start_date"] = start_date_str # Add for clarity
        search_context_to_return["search_end_date"] = end_date_str   # Add for clarity

        # Fetch existing calendar events
        existing_events = [];
        if calendar_api is not None: # Check if API instance exists
            log_info("tool_definitions", fn_name, f"Fetching GCal events from {start_date_str} to {end_date_str}")
            if start_date <= end_date:
                try:
                    events_raw = calendar_api.list_events(start_date_str, end_date_str) # Returns list of dicts or []
                    # Ensure necessary keys exist before adding
                    existing_events = [
                         {"start_datetime": ev.get("gcal_start_datetime"), "end_datetime": ev.get("gcal_end_datetime"), "summary": ev.get("title")}
                         for ev in events_raw if ev.get("gcal_start_datetime") and ev.get("gcal_end_datetime")
                    ]
                    log_info("tool_definitions", fn_name, f"Fetched {len(existing_events)} valid GCal events.")
                    # log_info("tool_definitions", f"{fn_name}_DEBUG", f"Existing Events Sent: {json.dumps(existing_events, indent=2)}")
                except Exception as e:
                     log_error("tool_definitions", fn_name, f"Fetch GCal events failed: {e}", e)
            else:
                 log_warning("tool_definitions", fn_name, f"Invalid search range ({start_date_str} > {end_date_str}). Skipping GCal fetch.")
        else:
            log_warning("tool_definitions", fn_name, "GCal API inactive or unavailable. Proposing slots without checking calendar conflicts.")

        # Prepare data for LLM scheduler sub-call
        try:
            num_options = params.num_options_to_propose if params.num_options_to_propose is not None else 3
            prompt_data = {
                "task_description": params.description or "(No description)",
                "task_due_date": search_context_to_return["effective_due_date"] or "(No specific due date)",
                "task_estimated_duration": task_estimated_duration_str, # Total duration
                "user_working_days": prefs.get("Work_Days", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
                "user_work_start_time": prefs.get("Work_Start_Time", "09:00"),
                "user_work_end_time": prefs.get("Work_End_Time", "17:00"),
                "user_session_length": slot_duration_str, # Duration of EACH slot to find
                "existing_events_json": json.dumps(existing_events),
                "current_date": today.strftime("%Y-%m-%d"),
                "num_slots_requested": num_options # Ask LLM for N options
            }
            # log_info("tool_definitions", f"{fn_name}_DEBUG", f"Data Sent to Scheduler LLM: {json.dumps(prompt_data, indent=2)}")
            log_info("tool_definitions", fn_name, f"Scheduler prompt data prepared (event count: {len(existing_events)}).")
        except Exception as e:
             log_error("tool_definitions", fn_name, f"Failed prepare prompt data for scheduler: {e}", e)
             return {**fail_result, "message": "Failed prepare data for scheduler."}

        # Call LLM and parse response
        raw_llm_output = None; parsed_data = None
        try:
            fmt_human = human_prompt.format(**prompt_data)
            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": fmt_human}]
            log_info("tool_definitions", fn_name, ">>> Invoking Session Scheduler LLM...")
            # Expect JSON object directly from GPT-4o
            sched_resp = llm_client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.2, response_format={"type": "json_object"})
            raw_llm_output = sched_resp.choices[0].message.content
            # log_info("tool_definitions", fn_name, f"<<< Scheduler LLM Raw Resp: {raw_llm_output[:300]}...")
            # log_info("tool_definitions", f"{fn_name}_DEBUG", f"Full Raw LLM Resp: {raw_llm_output}")
            parsed_data = _parse_scheduler_llm_response(raw_llm_output) # Use helper to parse and validate
            if parsed_data is None: # Check parse result
                log_error("tool_definitions", fn_name, "Scheduler response parse failed or returned invalid structure.")
                return {**fail_result, "message": "Received invalid proposals format from scheduler."}

            log_info("tool_definitions", f"{fn_name}_DEBUG", f"Parsed Scheduler LLM Resp: {json.dumps(parsed_data, indent=2)}")
            log_info("tool_definitions", fn_name, f"Scheduler LLM processing successful.")
            # Return success with parsed slots and the context used
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

# Returns dict
def finalize_task_and_book_sessions_tool(user_id, params: FinalizeTaskAndBookSessionsParams):
    """Creates a task metadata record AND books the approved work sessions in GCal."""
    fn_name = "finalize_task_and_book_sessions_tool"
    try:
        search_context = params.search_context; approved_slots = params.approved_slots
        log_info("tool_definitions", fn_name, f"Executing finalize+book user={user_id}, desc='{search_context.get('description')}', slots={len(approved_slots)}")
        if not search_context or not approved_slots:
            log_error("tool_definitions", fn_name, "Missing search_context or approved_slots.")
            return {"success": False, "item_id": None, "booked_count": 0, "message": "Internal error: Missing context or slots to book."}

        item_id = None; task_title = "(Error getting title)"
        try:
            # Prepare metadata payload from search context and parameters
            task_metadata_payload = {
                 "description": search_context.get("description", "Untitled Task"),
                 "estimated_duration": search_context.get("duration"), # Get duration from context
                 "type": "task",
                 "project": params.project, # Project tag comes from this tool's params
                 "date": approved_slots[0].get("date"), # Use first slot's date as initial task date
                 "time": approved_slots[0].get("time"), # Use first slot's time as initial task time (maybe None)
            }
            task_title = task_metadata_payload["description"] # Use the description as title

            # Create metadata record FIRST using task_manager service
            # task_manager handles setting defaults like status, created_at etc.
            created_meta = task_manager.create_task(user_id, task_metadata_payload)

            if created_meta and created_meta.get("event_id"):
                item_id = created_meta["event_id"]
                log_info("tool_definitions", fn_name, f"Task metadata created successfully via task_manager: {item_id}");
            else:
                 raise ValueError("Failed to create task metadata via task_manager.") # Raise error if creation failed

        except Exception as create_err:
            log_error("tool_definitions", fn_name, f"Metadata creation failed: {create_err}", create_err)
            return {"success": False, "item_id": None, "booked_count": 0, "message": "Failed to save the task details before scheduling."}

        # If metadata created successfully, proceed to book sessions
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
            # Optional: Attempt to mark the created task as pending/error or cancel?
            # For now, just report the partial success/failure.
            # task_manager.update_task_status(user_id, item_id, "pending") # Or custom error status?
            return {
                "success": False, # Overall operation failed if booking failed
                "item_id": item_id, # Return ID of created metadata
                "booked_count": 0,
                "message": f"Task '{task_title[:30]}...' was created, but scheduling sessions failed: {booking_result.get('message')}"
            }
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "item_id": None, "booked_count": 0, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         # Attempt to clean up created metadata if booking wasn't even attempted? Difficult state.
         return {"success": False, "item_id": item_id, "booked_count": 0, "message": f"Error: {e}"}


# Returns dict
def update_item_details_tool(user_id, params: UpdateItemDetailsParams):
    fn_name = "update_item_details_tool";
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, item={params.item_id}, updates={list(params.updates.keys())}")
        updated_item = task_manager.update_task(user_id, params.item_id, params.updates) # Pass validated updates
        if updated_item:
             return {"success": True, "message": f"Item '{params.item_id[:8]}...' updated successfully."}
        else:
             log_warning("tool_definitions", fn_name, f"Update failed for item {params.item_id} (not found or no change?).")
             # Check if item exists to provide better message
             if metadata_store.get_event_metadata(params.item_id):
                  return {"success": False, "message": f"Failed to apply updates to item {params.item_id[:8]}... (perhaps no change or internal error)."}
             else:
                  return {"success": False, "message": f"Item {params.item_id[:8]}... not found."}
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "message": f"Error during update: {e}."}

# Returns dict
def update_item_status_tool(user_id, params: UpdateItemStatusParams):
    """Changes status OR cancels/deletes item. Requires existing item_id."""
    fn_name = "update_item_status_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, item={params.item_id}, status={params.new_status}")
        success = False; message = ""
        if params.new_status == "cancelled":
            # Call cancel_item service which handles GCal cleanup + metadata status
            success = task_manager.cancel_item(user_id, params.item_id)
            message = f"Item '{params.item_id[:8]}...' cancel processed. Result: {'Success' if success else 'Failed/Not Found'}."
        else:
            # Call update_task_status for other statuses
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

# Returns dict
def update_user_preferences_tool(user_id, params: UpdateUserPreferencesParams):
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

# Returns dict
def initiate_calendar_connection_tool(user_id, params: InitiateCalendarConnectionParams):
    fn_name = "initiate_calendar_connection_tool"
    try:
        # No params to validate beyond the empty model
        log_info("tool_definitions", fn_name, f"Executing for user {user_id}")
        result_dict = config_manager.initiate_calendar_auth(user_id)
        # Ensure 'success' key is present based on status
        result_dict["success"] = result_dict.get("status") in ["pending", "token_exists"]
        return result_dict
    except pydantic.ValidationError as e: # Should not happen for empty model
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "status": "fails", "message": f"Error: {e}."}

# Returns dict
def cancel_task_sessions_tool(user_id, params: CancelTaskSessionsParams):
    fn_name = "cancel_task_sessions_tool"
    try:
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, Task={params.task_id}, SessionIDs={params.session_ids_to_cancel}")
        result = task_manager.cancel_sessions(user_id, params.task_id, params.session_ids_to_cancel)
        return result # cancel_sessions service should return dict with 'success', 'cancelled_count', 'message'
    except pydantic.ValidationError as e:
         log_error("tool_definitions", fn_name, f"Validation Error: {e}");
         return {"success": False, "cancelled_count": 0, "message": f"Invalid parameters: {e}"}
    except Exception as e:
         log_error("tool_definitions", fn_name, f"Unexpected error: {e}", e)
         return {"success": False, "cancelled_count": 0, "message": f"Error: {e}."}

# Returns dict
def interpret_list_reply_tool(user_id, params: InterpretListReplyParams):
    """Placeholder tool to interpret replies to numbered lists."""
    fn_name = "interpret_list_reply_tool"
    # This tool remains largely a placeholder or basic implementation
    # A robust version might need more context or LLM assistance itself
    try:
        log_warning("tool_definitions", fn_name, f"Tool executed for user {user_id} - Implementation is basic.")
        # Basic number extraction
        extracted_numbers = [int(s) for s in re.findall(r'\b\d+\b', params.user_reply)]
        identified_item_ids = []
        if params.list_mapping: # Check if mapping exists
             identified_item_ids = [params.list_mapping.get(str(num)) for num in extracted_numbers if str(num) in params.list_mapping]
             identified_item_ids = [item_id for item_id in identified_item_ids if item_id is not None] # Filter out None values

        if identified_item_ids:
             log_info("tool_definitions", fn_name, f"Identified numbers {extracted_numbers} mapping to IDs: {identified_item_ids}")
             # The 'action' key is just an example, might need refinement based on LLM needs
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

# Returns dict
def get_formatted_task_list_tool(user_id, params: GetFormattedTaskListParams):
    fn_name = "get_formatted_task_list_tool"
    try:
        status_filter = params.status_filter or 'active' # Apply default if None
        log_info("tool_definitions", fn_name, f"Executing user={user_id}, Filter={status_filter}, Proj={params.project_filter}, Range={params.date_range}")
        # Ensure date_range is a tuple if not None
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
    "create_task": create_task_tool,
    "propose_task_slots": propose_task_slots_tool,
    "finalize_task_and_book_sessions": finalize_task_and_book_sessions_tool,
    "update_item_details": update_item_details_tool,
    "update_item_status": update_item_status_tool,
    "update_user_preferences": update_user_preferences_tool,
    "initiate_calendar_connection": initiate_calendar_connection_tool,
    "cancel_task_sessions": cancel_task_sessions_tool,
    "interpret_list_reply": interpret_list_reply_tool,
    "get_formatted_task_list": get_formatted_task_list_tool
}

TOOL_PARAM_MODELS = {
    "create_reminder": CreateReminderParams,
    "create_task": CreateTaskParams,
    "propose_task_slots": ProposeTaskSlotsParams,
    "finalize_task_and_book_sessions": FinalizeTaskAndBookSessionsParams,
    "update_item_details": UpdateItemDetailsParams,
    "update_item_status": UpdateItemStatusParams,
    "update_user_preferences": UpdateUserPreferencesParams,
    "initiate_calendar_connection": InitiateCalendarConnectionParams,
    "cancel_task_sessions": CancelTaskSessionsParams,
    "interpret_list_reply": InterpretListReplyParams,
    "get_formatted_task_list": GetFormattedTaskListParams
}

# --- END OF FULL agents/tool_definitions.py ---