# --- START OF FULL agents/tool_definitions.py ---

from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Dict, List, Any, Tuple, TYPE_CHECKING
import json
from datetime import datetime, timezone, timedelta # Added timedelta
import re
import traceback
import os # For path joining for prompts
import yaml # For loading prompts
import pytz # For timezone handling

# Import Service Layer functions & Helpers
import services.task_manager as task_manager
import services.config_manager as config_manager
import services.task_query_service as task_query_service
from services.agent_state_manager import get_agent_state # For user preferences and GCal API object

# --- GoogleCalendarAPI Import Handling ---
GCAL_API_IMPORTED = False
GoogleCalendarAPI_class_ref = None # Placeholder for the class itself
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    if GoogleCalendarAPI: # Check if the imported name is not None
        GoogleCalendarAPI_class_ref = GoogleCalendarAPI
        GCAL_API_IMPORTED = True
except ImportError:
    pass # Error already logged by google_calendar_api.py if it fails

# --- ActivityDB Import Handling ---
DB_IMPORTED = False
activity_db_module_ref = None # Placeholder for the module
try:
    import tools.activity_db as activity_db
    activity_db_module_ref = activity_db
    DB_IMPORTED = True
except ImportError:
    # Define a dummy for type checking within tools if DB fails to import
    class activity_db_dummy:
        @staticmethod
        def get_task(*args, **kwargs): return None
    activity_db_module_ref = activity_db_dummy() # Instantiate dummy

# LLM Interface (for scheduler sub-call in propose_task_slots)
from services.llm_interface import get_instructor_client
from openai import OpenAI # For type hinting if needed
# Explicitly import ChatCompletionMessageParam for type hinting messages_for_llm
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam


# Utilities
from tools.logger import log_info, log_error, log_warning
import pydantic # Keep pydantic import for ValidationError

# --- Load Standard Messages ---
_messages_tool_def = {} # For messages needed by tools, e.g., onboarding completion
try:
    messages_path_tools = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path_tools):
        with open(messages_path_tools, 'r', encoding="utf-8") as f_tools_msg:
            content_tools_msg = f_tools_msg.read()
            if content_tools_msg.strip(): # Ensure file is not empty
                f_tools_msg.seek(0) # Reset cursor
                _messages_tool_def = yaml.safe_load(f_tools_msg) or {}
            else:
                _messages_tool_def = {} # File is empty or whitespace only
    else:
        log_warning("tool_definitions", "init", f"{messages_path_tools} not found for tool messages.")
        _messages_tool_def = {}
except Exception as e_load_msg_tools:
    _messages_tool_def = {}
    log_error("tool_definitions", "init", f"Failed to load messages.yaml for tools: {e_load_msg_tools}", e_load_msg_tools)
# --- End Load Standard Messages ---


# --- Helper Functions ---

# --- Helper function to load the new single scheduler prompt ---
_COMPREHENSIVE_SCHEDULER_PROMPT_CACHE: str | None = None # Cache for the single prompt string

def _load_comprehensive_scheduler_prompt() -> str:
    global _COMPREHENSIVE_SCHEDULER_PROMPT_CACHE
    fn_name = "_load_comprehensive_scheduler_prompt"
    
    if _COMPREHENSIVE_SCHEDULER_PROMPT_CACHE is not None:
        return _COMPREHENSIVE_SCHEDULER_PROMPT_CACHE

    prompt_key = "comprehensive_task_scheduler_prompt"
    prompts_path = os.path.join("config", "prompts.yaml")
    
    prompt_text: str = ""
    try:
        if not os.path.exists(prompts_path):
            raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            all_prompts = yaml.safe_load(f)
        if not all_prompts:
            raise ValueError("YAML prompts file loaded as empty.")
        
        prompt_text_temp = all_prompts.get(prompt_key)
        if not prompt_text_temp or not prompt_text_temp.strip():
            log_error("tool_definitions", fn_name, f"Prompt '{prompt_key}' missing or empty in {prompts_path}.")
        else:
            prompt_text = prompt_text_temp
            _COMPREHENSIVE_SCHEDULER_PROMPT_CACHE = prompt_text # Cache it
            log_info("tool_definitions", fn_name, f"Successfully loaded '{prompt_key}'.")

    except Exception as e:
        log_error("tool_definitions", fn_name, f"Failed to load scheduler prompt '{prompt_key}': {e}", e)
        # Return an empty string or a default fallback if critical
        # For now, an empty string will cause the tool to fail gracefully later.
    
    return prompt_text

def _get_calendar_api_from_state(user_id: str) -> Any: # Returns GoogleCalendarAPI instance or None
    fn_name = "_get_calendar_api_from_state_tooldef"
    if not GCAL_API_IMPORTED or GoogleCalendarAPI_class_ref is None:
        # log_warning("tool_definitions", fn_name, f"GCal API library not imported or class not available for user {user_id}.")
        return None
    try:
        state = get_agent_state(user_id) # From agent_state_manager
        if state is not None:
            api = state.get("calendar")
            if isinstance(api, GoogleCalendarAPI_class_ref) and api.is_active():
                return api
        # else: log_warning if state is None (agent_state_manager should handle this)
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Error getting calendar API from state for {user_id}", e, user_id=user_id)
    return None


def _parse_comprehensive_schedule_response(raw_text: str, user_id_for_log: str = "UnknownUser") -> Dict | None:
    fn_name = "_parse_comprehensive_schedule_response"
    if not raw_text:
        log_warning("tool_definitions", fn_name, "Comprehensive scheduler LLM response text is empty.", user_id=user_id_for_log)
        return None
    
    processed_text = None
    try:
        match = re.search(r"```json\s*({.*?})\s*```", raw_text, re.DOTALL | re.IGNORECASE)
        if match: processed_text = match.group(1).strip()
        else:
            processed_text = raw_text.strip()
            if not processed_text.startswith("{") or not processed_text.endswith("}"):
                start_brace = raw_text.find('{'); end_brace = raw_text.rfind('}')
                if start_brace != -1 and end_brace > start_brace:
                    processed_text = raw_text[start_brace : end_brace + 1].strip()
                else:
                    log_error("tool_definitions", fn_name, f"Response not JSON and no ```json block. Raw: {raw_text[:200]}", user_id=user_id_for_log)
                    return None
        
        if not processed_text: raise ValueError("Processed text for JSON parsing is empty.")
        data = json.loads(processed_text)

        required_top_keys = ["proposed_sessions", "parsed_task_details_for_finalization", "response_message"]
        if not isinstance(data, dict) or not all(k in data for k in required_top_keys):
            log_error("tool_definitions", fn_name, f"Parsed JSON missing required top-level keys: {required_top_keys}. Found: {list(data.keys())}", user_id=user_id_for_log)
            return None
        
        if not isinstance(data["proposed_sessions"], list):
            log_warning("tool_definitions", fn_name, "'proposed_sessions' is not a list, returning empty.", user_id=user_id_for_log)
            data["proposed_sessions"] = []

        valid_sessions = []
        for i, session_dict in enumerate(data.get("proposed_sessions", [])): # Use .get for safety
            req_session_keys = ["date", "time", "end_time", "status"]
            if not isinstance(session_dict, dict) or not all(k in session_dict for k in req_session_keys):
                log_warning("tool_definitions", fn_name, f"Skipping invalid session structure: {session_dict}", user_id=user_id_for_log)
                continue
            try:
                datetime.strptime(session_dict["date"], '%Y-%m-%d')
                datetime.strptime(session_dict["time"], '%H:%M')
                datetime.strptime(session_dict["end_time"], '%H:%M')
                if session_dict["status"] not in ["new", "updated"]: raise ValueError("Invalid session status")
                ref = session_dict.get("slot_ref")
                session_dict["slot_ref"] = ref if isinstance(ref, int) and ref > 0 else i + 1
                valid_sessions.append(session_dict)
            except (ValueError, TypeError) as fmt_err:
                log_warning("tool_definitions", fn_name, f"Skipping session due to format/value error ({fmt_err}): {session_dict}", user_id=user_id_for_log)
        data["proposed_sessions"] = valid_sessions

        required_task_detail_keys = ["description", "estimated_total_duration"]
        task_details = data.get("parsed_task_details_for_finalization")
        if not isinstance(task_details, dict) or not all(k in task_details for k in required_task_detail_keys):
            log_warning("tool_definitions", fn_name, f"'parsed_task_details_for_finalization' invalid. Using fallbacks. Found: {task_details}", user_id=user_id_for_log)
            data["parsed_task_details_for_finalization"] = {
                "description": str(task_details.get("description", "Task (details parsing failed)") if isinstance(task_details, dict) else "Task (details parsing failed)"),
                "estimated_total_duration": str(task_details.get("estimated_total_duration", "1h") if isinstance(task_details, dict) else "1h"),
                "project": str(task_details.get("project", "") if isinstance(task_details, dict) else ""),
                "due_date": str(task_details.get("due_date", "") if isinstance(task_details, dict) else "")
            }
        data["parsed_task_details_for_finalization"].setdefault("project", "")
        data["parsed_task_details_for_finalization"].setdefault("due_date", "")


        if not isinstance(data.get("response_message"), str):
            data["response_message"] = str(data.get("response_message","Scheduler message missing."))

        return data
    except json.JSONDecodeError as parse_err:
        log_error("tool_definitions", fn_name, f"Scheduler JSON parsing error. Error: {parse_err}. Extracted: '{processed_text or 'N/A'}'", parse_err, user_id=user_id_for_log)
        return None
    except ValueError as val_err:
        log_error("tool_definitions", fn_name, f"Validation error in scheduler response: {val_err}", val_err, user_id=user_id_for_log)
        return None
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Unexpected error parsing scheduler response: {e}", e, user_id=user_id_for_log)
        return None

_SCHEDULER_PROMPTS_CACHE: Dict[str, Tuple[str, str]] = {}
def _load_scheduler_prompts() -> Tuple[str, str]: # Kept original name, content is new
    fn_name = "_load_scheduler_prompts"
    system_prompt_key = "session_scheduler_system_prompt" # Assumes this key now points to the new comprehensive prompt
    human_prompt_key = "session_scheduler_human_prompt"   # Assumes this key now points to the new comprehensive human prompt template
    
    prompts_path = os.path.join("config", "prompts.yaml")
    cache_key = prompts_path + "_" + system_prompt_key + "_" + human_prompt_key
    if cache_key in _SCHEDULER_PROMPTS_CACHE: return _SCHEDULER_PROMPTS_CACHE[cache_key]

    sys_prompt_text: str = ""; human_prompt_template_text: str = ""
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f: all_prompts = yaml.safe_load(f)
        if not all_prompts: raise ValueError("YAML prompts file loaded as empty.")
        
        sys_prompt_text_temp = all_prompts.get(system_prompt_key)
        human_prompt_template_text_temp = all_prompts.get(human_prompt_key)

        if not sys_prompt_text_temp or not sys_prompt_text_temp.strip():
            log_error("tool_definitions", fn_name, f"System prompt '{system_prompt_key}' missing/empty.")
        else: sys_prompt_text = sys_prompt_text_temp
        if not human_prompt_template_text_temp or not human_prompt_template_text_temp.strip():
            log_error("tool_definitions", fn_name, f"Human prompt template '{human_prompt_key}' missing/empty.")
        else: human_prompt_template_text = human_prompt_template_text_temp
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Failed to load scheduler prompts: {e}", e)
    
    _SCHEDULER_PROMPTS_CACHE[cache_key] = (sys_prompt_text, human_prompt_template_text)
    return sys_prompt_text, human_prompt_template_text

# =====================================================
# == Pydantic Model Definitions ==
# =====================================================
class CreateToDoParams(BaseModel):
    description: str = Field(..., description="The content or description of the ToDo item.")
    date: str = Field("", description="Optional: Due date for the ToDo in YYYY-MM-DD format. Can be empty.")
    project: str = Field("", description="Optional: Project tag for the ToDo. Can be empty.")
    estimated_duration: str = Field("", description="Optional: Estimated duration (e.g., '1h', '30m'). Can be empty.")
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str):
        if v == "": return ""
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format or empty")

class CreateReminderParams(BaseModel):
    description: str = Field(...)
    date: str = Field(...)
    time: str = Field("", description="Optional: Time in HH:MM format. Empty for all-day.")
    project: str = Field("", description="Optional: Project tag. Can be empty.")
    @field_validator('time')
    @classmethod
    def validate_time_format(cls, v: str):
        if v == "": return ""
        try: hour, minute = map(int, v.split(':')); return f"{hour:02d}:{minute:02d}"
        except (ValueError, TypeError): raise ValueError("Time must be in HH:MM format or empty")
    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v: str):
        try: datetime.strptime(v, '%Y-%m-%d'); return v
        except (ValueError, TypeError): raise ValueError("Date must be in YYYY-MM-DD format")

class ProposeTaskSlotsParams(BaseModel): # Externally, params are simple
    natural_language_scheduling_request: str = Field(...)
    item_id_to_reschedule: str = Field("", description="Optional: Task ID if rescheduling.")

class FinalizeTaskAndBookSessionsParams(BaseModel): # MODIFIED to expect richer search_context
    search_context: Dict = Field(..., description="Context from 'propose_task_slots', MUST include 'parsed_task_details_from_llm'.")
    approved_slots: List[Dict] = Field(..., min_length=1, description="List of user-approved slot dicts. Each needs 'date', 'time', 'end_time', 'status'.")
    task_description_override: str = Field("", description="Optional: Override for task description.")
    estimated_total_duration_override: str = Field("", description="Optional: Override for task total duration.")
    project_override: str = Field("", description="Optional: Override for project tag.")

    @field_validator('search_context')
    @classmethod
    def validate_search_context_structure(cls, v: Dict):
        if not isinstance(v, dict): raise ValueError("search_context must be a dictionary.")
        details = v.get("parsed_task_details_from_llm")
        if not isinstance(details, dict): raise ValueError("search_context missing valid 'parsed_task_details_from_llm' dictionary.")
        if not isinstance(details.get("description"), str) or not details.get("description").strip():
            raise ValueError("'parsed_task_details_from_llm.description' must be a non-empty string.")
        if not isinstance(details.get("estimated_total_duration"), str): # Can be empty, but must be string
             if details.get("estimated_total_duration") is None:
                  raise ValueError("'parsed_task_details_from_llm.estimated_total_duration' cannot be null if key exists, use empty string or valid duration string.")
        v["parsed_task_details_from_llm"].setdefault("project", "")
        v["parsed_task_details_from_llm"].setdefault("due_date", "")
        return v

    @field_validator('approved_slots')
    @classmethod
    def validate_slots_structure(cls, v_slots: List[Dict]):
        if not isinstance(v_slots, list) or not v_slots: raise ValueError("approved_slots non-empty list.")
        for i, slot in enumerate(v_slots):
            if not isinstance(slot, dict): raise ValueError(f"Slot {i+1} not a dict.")
            req_keys = ["date", "time", "end_time", "status"]
            if not all(k in slot for k in req_keys): raise ValueError(f"Slot {i+1} missing keys: {req_keys}. Got: {list(slot.keys())}")
            try: datetime.strptime(slot["date"], '%Y-%m-%d')
            except: raise ValueError(f"Slot {i+1} invalid date: {slot['date']}")
            try: datetime.strptime(slot["time"], '%H:%M')
            except: raise ValueError(f"Slot {i+1} invalid time: {slot['time']}")
            try: datetime.strptime(slot["end_time"], '%H:%M')
            except: raise ValueError(f"Slot {i+1} invalid end_time: {slot['end_time']}")
            if slot["status"] not in ["new", "updated"]: raise ValueError(f"Slot {i+1} invalid status: {slot['status']}")
        return v_slots

class UpdateItemDetailsParams(BaseModel):
    item_id: str = Field(...)
    updates: dict = Field(..., description="Dict of fields to update, e.g. {'description': 'new', 'status': 'completed'}.")
    @field_validator('updates') # Validation for date/time/status formats within updates
    @classmethod
    def check_allowed_keys_and_formats(cls, v: dict):
        allowed_keys = {"description", "date", "time", "estimated_duration", "project", "status"} 
        if not v: raise ValueError("Updates dictionary cannot be empty.")
        validated_updates = {}
        for key, value in v.items():
            if key not in allowed_keys: raise ValueError(f"Invalid key '{key}'. Allowed: {', '.join(allowed_keys)}")
            if key == 'date':
                if value == "" or value is None: validated_updates[key] = "" # Allow clearing date
                else:
                    try: validated_updates[key] = cls._validate_date_format_static(str(value))
                    except ValueError as e: raise ValueError(f"Invalid format for date '{value}': {e}")
            elif key == 'time':
                if value == "" or value is None: validated_updates[key] = "" # Allow clearing time
                else:
                     try: validated_updates[key] = cls._validate_time_format_static(str(value))
                     except ValueError as e: raise ValueError(f"Invalid format for time '{value}': {e}")
            elif key == 'status':
                if value is None: raise ValueError("Status cannot be null.")
                validated_updates[key] = cls._validate_status_format_static(str(value))
            else: # description, estimated_duration, project
                validated_updates[key] = value if value is not None else "" # Ensure string or empty string
        if not validated_updates: raise ValueError("Updates dictionary resulted in no valid fields after validation.")
        return validated_updates
    @staticmethod
    def _validate_date_format_static(v_date: str):
        if v_date == "": return ""
        try: datetime.strptime(v_date, '%Y-%m-%d'); return v_date
        except: raise ValueError("Date must be YYYY-MM-DD or empty")
    @staticmethod
    def _validate_time_format_static(v_time: str):
        if v_time == "": return ""
        try: h, m = map(int, v_time.split(':')); return f"{h:02d}:{m:02d}"
        except: raise ValueError("Time must be HH:MM or empty")
    @staticmethod
    def _validate_status_format_static(v_status: str):
        allowed = {"pending", "in_progress", "completed", "cancelled"}
        v_lower = v_status.lower().strip().replace(" ", "_")
        if v_lower not in allowed: raise ValueError(f"Status must be one of: {', '.join(allowed)}")
        return v_lower

class FormatListForDisplayParams(BaseModel):
    date_range: List[str] = Field([], description="Optional. [YYYY-MM-DD, YYYY-MM-DD] or [YYYY-MM-DD].")
    status_filter: str = Field("active", description="Optional. 'active', 'pending', 'in_progress', 'completed', 'all'. Default 'active'.")
    project_filter: str = Field("", description="Optional. Filter by project tag.")
    @field_validator('date_range')
    @classmethod
    def validate_and_normalize_date_range(cls, v: List[str]):
        if not v: return []
        if not isinstance(v, list): raise ValueError("date_range must be a list.")
        if len(v) == 1:
            try: datetime.strptime(v[0], '%Y-%m-%d').date(); return [v[0], v[0]]
            except: raise ValueError("Single date invalid YYYY-MM-DD.")
        elif len(v) == 2:
            try:
                s, e = datetime.strptime(v[0], '%Y-%m-%d').date(), datetime.strptime(v[1], '%Y-%m-%d').date()
                if s > e: raise ValueError("Start date after end date.")
                return v
            except: raise ValueError("Dates must be valid YYYY-MM-DD.")
        else: raise ValueError("date_range must be 0, 1, or 2 date strings.")
    @field_validator('status_filter')
    @classmethod
    def check_status_filter(cls, v: str):
        if v is None or v == "": return 'active'
        allowed = {'active', 'pending', 'in_progress', 'completed', 'all'}
        v_lower = v.lower().strip().replace(" ", "_")
        return v_lower if v_lower in allowed else 'active'

class UpdateUserPreferencesParams(BaseModel):
    updates: dict = Field(...)
    @field_validator('updates')
    @classmethod
    def check_updates_not_empty(cls, v: dict):
        if not v: raise ValueError("Updates dictionary cannot be empty.")
        return v

class InitiateCalendarConnectionParams(BaseModel): pass

class SendOnboardingCompletionMessageParams(BaseModel):
    """Tool to retrieve the standard onboarding completion message."""
    pass # No parameters needed for this tool

class InterpretListReplyParams(BaseModel): # This tool may become obsolete
    user_reply: str = Field(...)
    list_mapping: dict = Field(...)

# =====================================================
# == Tool Function Definitions ==
# =====================================================

# --- Item Creation Tools ---
def create_todo_tool(user_id: str, params: CreateToDoParams) -> Dict:
    fn_name = "create_todo_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} creating ToDo: '{params.description[:30]}...'") # Verbose
        data_dict = params.model_dump(exclude_none=False)
        data_dict["type"] = "todo"
        for key in ["date", "project", "estimated_duration"]: # Convert empty strings to None for DB
            if data_dict.get(key) == "": data_dict[key] = None
        
        saved_item = task_manager.create_item(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "todo", "message": f"ToDo '{str(params.description)[:30]}...' added."}
        else:
             log_error("tool_definitions", fn_name, f"task_manager.create_item failed for ToDo for user {user_id}")
             return {"success": False, "item_id": None, "message": "Failed to save ToDo."}
    except pydantic.ValidationError as e: return {"success": False, "item_id": None, "message": f"Invalid ToDo params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "item_id": None, "message": f"Error creating ToDo: {e}"}

def create_reminder_tool(user_id: str, params: CreateReminderParams) -> Dict:
    fn_name = "create_reminder_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} creating Reminder: '{params.description[:30]}...'") # Verbose
        data_dict = params.model_dump(exclude_none=False)
        data_dict["type"] = "reminder"
        for key in ["time", "project"]: # Convert empty strings to None
            if data_dict.get(key) == "": data_dict[key] = None
        
        saved_item = task_manager.create_item(user_id, data_dict)
        if saved_item and saved_item.get("event_id"):
            return {"success": True, "item_id": saved_item.get("event_id"), "item_type": "reminder", "message": f"Reminder '{str(params.description)[:30]}...' created."}
        else:
             log_error("tool_definitions", fn_name, f"task_manager.create_item failed for Reminder for user {user_id}")
             return {"success": False, "item_id": None, "message": "Failed to save reminder."}
    except pydantic.ValidationError as e: return {"success": False, "item_id": None, "message": f"Invalid Reminder params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "item_id": None, "message": f"Error creating Reminder: {e}"}

# --- Task Scheduling Tools (Revised) ---
# --- Revised propose_task_slots_tool ---
def propose_task_slots_tool(user_id: str, params: ProposeTaskSlotsParams) -> Dict:
    fn_name = "propose_task_slots_tool"
    log_info("tool_definitions", fn_name, f"User {user_id}, NLP Req: '{params.natural_language_scheduling_request[:60]}...', ReschedID: {params.item_id_to_reschedule or 'N/A'}")

    fail_result = { # Default failure response
        "success": False, "proposed_slots": [],
        "message": "Sorry, I encountered an issue trying to propose schedule slots.",
        "search_context": {
            "original_request": params.natural_language_scheduling_request,
            "item_id_to_reschedule": params.item_id_to_reschedule,
            "parsed_task_details_from_llm": None # Keep this structure for consistency
        }
    }

    comprehensive_prompt_template = _load_comprehensive_scheduler_prompt()
    if not comprehensive_prompt_template:
        log_error("tool_definitions", fn_name, "Comprehensive scheduler prompt not loaded. Cannot proceed.", user_id=user_id)
        fail_result["message"] = "Internal error: Scheduler prompt configuration missing."
        return fail_result

    agent_state = get_agent_state(user_id)
    if not agent_state:
        log_error("tool_definitions", fn_name, f"Agent state not found for {user_id}.", user_id=user_id)
        fail_result["message"] = "Internal error: User context unavailable for scheduling."
        return fail_result

    preferences = agent_state.get("preferences", {})
    user_tz_str = preferences.get("TimeZone", "UTC")
    try:
        user_timezone = pytz.timezone(user_tz_str)
    except pytz.UnknownTimeZoneError:
        log_warning("tool_definitions", fn_name, f"Unknown timezone '{user_tz_str}' for user {user_id}. Using UTC.", user_id=user_id)
        user_timezone = pytz.utc
        user_tz_str = "UTC" # Correct the string if it was invalid

    now_user_tz = datetime.now(user_timezone)
    # Define a reasonable search window, e.g., from today up to 4 weeks
    search_start_user_tz_date = now_user_tz.date()
    search_end_user_tz_date = search_start_user_tz_date + timedelta(weeks=4)

    live_gcal_events = []
    if preferences.get("gcal_integration_status") == "connected":
        calendar_api = _get_calendar_api_from_state(user_id) # Your existing helper
        if calendar_api:
            try:
                live_gcal_events = calendar_api.list_events(
                    search_start_user_tz_date.strftime("%Y-%m-%d"),
                    search_end_user_tz_date.strftime("%Y-%m-%d")
                )
            except Exception as e_cal:
                log_error("tool_definitions", fn_name, f"Error fetching GCal events for {user_id}", e_cal, user_id=user_id)
                # Proceed without live events, LLM will be informed list is empty
        # else: log GCal API not init was handled in the previous log snippet analysis
    
    existing_task_details_json_str = "{}" # Default empty JSON object as string
    if params.item_id_to_reschedule and DB_IMPORTED and activity_db_module_ref: # Check DB_IMPORTED
        existing_task_data = activity_db_module_ref.get_task(params.item_id_to_reschedule)
        if existing_task_data:
            relevant_details = {
                k: existing_task_data.get(k) for k in 
                ["title", "description", "estimated_duration", "date", "project", "status", "session_event_ids"]
                if existing_task_data.get(k) is not None # Only include non-null values
            }
            existing_task_details_json_str = json.dumps(relevant_details, default=str)

    # Prepare all values for formatting the prompt
    prompt_format_values = {
        "natural_language_scheduling_request": params.natural_language_scheduling_request,
        "existing_task_id": params.item_id_to_reschedule or "N/A",
        "existing_task_details_json": existing_task_details_json_str,
        "user_id": user_id,
        "user_preferred_session_length": preferences.get("Preferred_Session_Length", "1h"),
        "user_working_days": json.dumps(preferences.get("Work_Days", ["Mon", "Tue", "Wed", "Thu", "Fri"])), # Send as JSON string
        "user_work_start_time": preferences.get("Work_Start_Time", "09:00"),
        "user_work_end_time": preferences.get("Work_End_Time", "17:00"),
        "user_timezone": user_tz_str,
        "current_date_user_tz": now_user_tz.strftime("%Y-%m-%d"),
        "search_start_date_user_tz": search_start_user_tz_date.strftime("%Y-%m-%d"),
        "search_end_date_user_tz": search_end_user_tz_date.strftime("%Y-%m-%d"),
        "live_calendar_events_json": json.dumps(live_gcal_events, default=str)
    }

    try:
        formatted_prompt_for_llm = comprehensive_prompt_template.format(**prompt_format_values)
    except KeyError as e_fmt:
        log_error("tool_definitions", fn_name, f"Missing key '{e_fmt}' when formatting comprehensive scheduler prompt for {user_id}.", e_fmt, user_id=user_id)
        fail_result["message"] = "Internal error: Scheduler prompt formatting failed."
        return fail_result

    # For OpenAI API, the detailed instructions and data often go into the "user" message,
    # after a very brief system message.
    messages_for_llm: List[ChatCompletionMessageParam] = [ # type: ignore
        {"role": "system", "content": "You are an expert Internal Scheduling Assistant. Follow the instructions and use the data provided in the user message to generate your JSON response."},
        {"role": "user", "content": formatted_prompt_for_llm}
    ]
    
    client = get_instructor_client()
    if not client:
        log_error("tool_definitions", fn_name, "LLM client unavailable for scheduling sub-task.", user_id=user_id)
        fail_result["message"] = "Internal error: AI scheduling service client unavailable."
        return fail_result

    try:
        log_info("tool_definitions", fn_name, f"Invoking Scheduling LLM for {user_id} with combined prompt...")
        response = client.chat.completions.create(
            model="gpt-4o", # Or your preferred model for this sub-task
            messages=messages_for_llm,
            response_format={"type": "json_object"}, # Crucial for getting JSON back
            temperature=0.1, # Low temperature for deterministic JSON generation
        )
        llm_raw_output = response.choices[0].message.content
        if not llm_raw_output:
            raise ValueError("Scheduling LLM returned empty content.")
            
    except Exception as e_llm:
        log_error("tool_definitions", fn_name, f"Error invoking Scheduling LLM for {user_id}", e_llm, user_id=user_id)
        fail_result["message"] = "AI scheduler sub-task encountered an issue. Please try again."
        return fail_result

    # Parse the JSON output from the LLM
    parsed_llm_data = _parse_comprehensive_schedule_response(llm_raw_output, user_id) # Your existing parser
    if not parsed_llm_data:
        log_error("tool_definitions", fn_name, f"Failed to parse valid JSON from scheduling LLM for {user_id}. Raw: {llm_raw_output[:500]}")
        fail_result["message"] = "AI scheduler returned an unexpected format. Please rephrase your request."
        return fail_result

    # Construct the final result for the main orchestrator
    return {
        "success": True,
        "proposed_slots": parsed_llm_data.get("proposed_sessions", []),
        "message": parsed_llm_data.get("response_message", "Slots proposed by sub-agent."), # Message from the sub-agent
        "search_context": { # This is what the main orchestrator needs
            "original_request": params.natural_language_scheduling_request,
            "item_id_to_reschedule": params.item_id_to_reschedule,
            "parsed_task_details_from_llm": parsed_llm_data.get("parsed_task_details_for_finalization")
            # Add any other context from prompt_format_values if needed by finalize_task_and_book_sessions
            # e.g., "user_preferred_session_length": prompt_format_values["user_preferred_session_length"]
        }
    }

def finalize_task_and_book_sessions_tool(user_id: str, params: FinalizeTaskAndBookSessionsParams) -> Dict:
    fn_name = "finalize_task_and_book_sessions_tool"
    item_id_final: str | None = None
    try:
        search_context = params.search_context; approved_slots = params.approved_slots
        parsed_details = search_context.get("parsed_task_details_from_llm", {})
        original_request_nlp = search_context.get("original_request", "N/A")
        item_id_to_reschedule = search_context.get("item_id_to_reschedule")

        # log_info("tool_definitions", fn_name, f"User {user_id} finalizing task. LLM desc: '{parsed_details.get('description', 'N/A')[:30]}...'") # Verbose

        if not approved_slots: return {"success": False, "item_id": None, "booked_count": 0, "message": "No approved slots."}

        task_metadata_payload = {
            "type": "task", "status": "pending",
            "description": params.task_description_override or parsed_details.get("description", f"Task from: {original_request_nlp[:50]}"),
            "title": params.task_description_override or parsed_details.get("description", f"Task from: {original_request_nlp[:50]}"),
            "estimated_duration": params.estimated_total_duration_override or parsed_details.get("estimated_total_duration"),
            "project": params.project_override or parsed_details.get("project"),
            "date": parsed_details.get("due_date") # Due date from LLM
        }
        for key in ["project", "date", "estimated_duration"]: # Ensure None if empty
            if not task_metadata_payload[key]: task_metadata_payload[key] = None
        task_title_for_user_message = task_metadata_payload["description"]

        if item_id_to_reschedule:
            updated_item_obj = task_manager.update_item_details(user_id, item_id_to_reschedule, task_metadata_payload)
            if updated_item_obj and updated_item_obj.get("event_id"): item_id_final = updated_item_obj["event_id"]
            else: return {"success": False, "item_id": item_id_to_reschedule, "booked_count": 0, "message": f"Failed to update task '{task_title_for_user_message[:30]}'."}
        else:
            created_meta_item = task_manager.create_item(user_id, task_metadata_payload)
            if created_meta_item and created_meta_item.get("event_id"): item_id_final = created_meta_item["event_id"]
            else: return {"success": False, "item_id": None, "booked_count": 0, "message": "Failed to save new task."}

        if not item_id_final: return {"success": False, "item_id": None, "booked_count": 0, "message": "Internal error preparing task ID."}

        if item_id_to_reschedule and DB_IMPORTED and activity_db_module_ref: # Clear old sessions if rescheduling
            original_task_data = activity_db_module_ref.get_task(item_id_to_reschedule)
            if original_task_data:
                old_session_ids = original_task_data.get("session_event_ids", [])
                if isinstance(old_session_ids, list) and old_session_ids:
                    task_manager.cancel_sessions(user_id, item_id_to_reschedule, old_session_ids)
        
        booking_result = task_manager.schedule_work_sessions(user_id, item_id_final, approved_slots)
        if booking_result.get("success"):
            return {"success": True, "item_id": item_id_final, "booked_count": booking_result.get("booked_count", 0), "message": booking_result.get("message")}
        else:
            return {"success": False, "item_id": item_id_final, "booked_count": 0, "message": f"Task '{task_title_for_user_message[:30]}' saved, but scheduling failed: {booking_result.get('message')}"}
    except pydantic.ValidationError as e: return {"success": False, "item_id": None, "booked_count": 0, "message": f"Invalid params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "item_id": item_id_final, "booked_count": 0, "message": f"Error: {e}."}

# --- Item Modification & Listing Tools (Largely Unchanged) ---
def update_item_details_tool(user_id: str, params: UpdateItemDetailsParams) -> Dict:
    fn_name = "update_item_details_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} updating item {params.item_id}, updates: {list(params.updates.keys())}") # Verbose
        updates_for_details = params.updates.copy()
        status_to_set = updates_for_details.pop("status", None)
        message_parts = []; overall_success = True

        if status_to_set:
            status_update_success = False; status_update_message = ""
            if status_to_set.lower() == "cancelled":
                status_update_success = task_manager.cancel_item(user_id, params.item_id)
                status_update_message = f"Item cancellation {'succeeded' if status_update_success else 'failed'}"
            else:
                updated_item_status_obj = task_manager.update_item_status(user_id, params.item_id, status_to_set)
                status_update_success = updated_item_status_obj is not None
                status_update_message = f"Status update to '{status_to_set}' {'succeeded' if status_update_success else 'failed'}"
            message_parts.append(status_update_message)
            if not status_update_success: overall_success = False
        
        if updates_for_details and overall_success: # Only try to update details if status update (if any) succeeded
            updated_item_obj = task_manager.update_item_details(user_id, params.item_id, updates_for_details)
            if updated_item_obj: message_parts.append("Other details updated.")
            else: message_parts.append("Failed to update other details."); overall_success = False
        elif not updates_for_details and not status_to_set: # No status change and no other details
             return {"success": False, "message": "No updates provided."}

        final_message = "Item update: " + "; ".join(message_parts) if message_parts else "No specific updates."
        if not overall_success and not message_parts: # e.g. only status update was attempted and failed
            final_message = "Failed to update item status."
        return {"success": overall_success, "message": final_message}
    except pydantic.ValidationError as e: return {"success": False, "message": f"Invalid update params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "message": f"Error updating: {e}."}

def format_list_for_display_tool(user_id: str, params: FormatListForDisplayParams) -> Dict:
    fn_name = "format_list_for_display_tool"
    try:
        status_filter = params.status_filter if params.status_filter else 'active'
        date_range_tuple = tuple(params.date_range) if params.date_range else None
        # log_info("tool_definitions", fn_name, f"User {user_id} formatting list. Status={status_filter}, Proj={params.project_filter}, Range={date_range_tuple}") # Verbose
        list_body, list_mapping = task_query_service.get_formatted_list(
             user_id=user_id, date_range=date_range_tuple,
             status_filter=status_filter, project_filter=params.project_filter if params.project_filter else None
        )
        item_count = len(list_mapping)
        message = f"Found {item_count} item(s)." if item_count > 0 else "No items found matching your criteria."
        if item_count > 0 and not list_body: message = f"Found {item_count} items, but error formatting list."
        return {"success": True, "formatted_list_string": list_body or "", "list_mapping": list_mapping or {}, "count": item_count, "message": message}
    except pydantic.ValidationError as e: return {"success": False, "formatted_list_string": "", "list_mapping": {}, "count": 0, "message": f"Invalid list params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "formatted_list_string": "", "list_mapping": {}, "count": 0, "message": f"Error listing: {e}."}

# --- Preference & Auth Tools (Unchanged) ---
def update_user_preferences_tool(user_id: str, params: UpdateUserPreferencesParams) -> Dict:
    fn_name = "update_user_preferences_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} updating preferences: {list(params.updates.keys())}") # Verbose
        success = config_manager.update_preferences(user_id, params.updates)
        return {"success": success, "message": f"Preferences update {'succeeded' if success else 'failed'}."}
    except pydantic.ValidationError as e: return {"success": False, "message": f"Invalid preference params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "message": f"Error: {e}."}

def initiate_calendar_connection_tool(user_id: str, params: InitiateCalendarConnectionParams) -> Dict:
    fn_name = "initiate_calendar_connection_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} initiating GCal connection.") # Verbose
        result_dict = config_manager.initiate_calendar_auth(user_id)
        # 'success' flag based on whether an auth URL was generated or token already exists
        result_dict["success"] = result_dict.get("status") in ["pending", "token_exists"]
        return result_dict
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "status": "error", "message": f"Error initiating GCal: {e}."}

def send_onboarding_completion_message_tool(user_id: str, params: SendOnboardingCompletionMessageParams) -> Dict:
    """Retrieves the standard onboarding completion message in the user's preferred language."""
    fn_name = "send_onboarding_completion_message_tool"
    try:
        agent_state = get_agent_state(user_id)
        if not agent_state or not agent_state.get("preferences"):
            log_warning("tool_definitions", fn_name, f"Cannot get preferences for user {user_id} to determine language.")
            return {"success": False, "message_to_send": "Onboarding complete! (Could not load detailed welcome)."}

        user_lang = agent_state["preferences"].get("Preferred_Language", "en") # Default to English
        
        completion_message_structure = _messages_tool_def.get("onboarding_completion_message", {})
        message_text = completion_message_structure.get(user_lang, completion_message_structure.get("en"))

        if not message_text:
            log_error("tool_definitions", fn_name, f"Onboarding completion message not found for lang '{user_lang}' or default 'en'.")
            return {"success": False, "message_to_send": "You're all set up! Welcome to WhatsTasker."}

        return {"success": True, "message_to_send": message_text}
    except Exception as e:
        log_error("tool_definitions", fn_name, f"Error retrieving onboarding completion message: {e}", e)
        return {"success": False, "message_to_send": "Onboarding process finished. Welcome!"}


def interpret_list_reply_tool(user_id: str, params: InterpretListReplyParams) -> Dict: # Potentially obsolete
    fn_name = "interpret_list_reply_tool"
    try:
        # log_info("tool_definitions", fn_name, f"User {user_id} interpreting list reply: '{params.user_reply[:30]}...'") # Verbose
        extracted_numbers = [int(s) for s in re.findall(r'\b\d+\b', params.user_reply)]
        identified_item_ids = []
        if params.list_mapping:
             identified_item_ids = [params.list_mapping.get(str(num)) for num in extracted_numbers if str(num) in params.list_mapping]
             identified_item_ids = [item_id for item_id in identified_item_ids if item_id is not None]
        if identified_item_ids:
             return { "success": True, "item_ids": identified_item_ids, "message": f"Interpreted item(s): {', '.join(map(str, extracted_numbers))}." }
        else:
             return {"success": False, "item_ids": [], "message": "Couldn't find valid item numbers in reply."}
    except pydantic.ValidationError as e: return {"success": False, "item_ids": [], "message": f"Invalid params: {e.errors()}"}
    except Exception as e: log_error("tool_definitions", fn_name, f"Error: {e}", e); return {"success": False, "item_ids": [], "message": "Error interpreting reply."}

# =====================================================
# == Tool Dictionaries ==
# =====================================================
AVAILABLE_TOOLS = {
    "create_todo": create_todo_tool,
    "create_reminder": create_reminder_tool,
    "propose_task_slots": propose_task_slots_tool,
    "finalize_task_and_book_sessions": finalize_task_and_book_sessions_tool,
    "update_item_details": update_item_details_tool,
    "format_list_for_display": format_list_for_display_tool,
    "update_user_preferences": update_user_preferences_tool,
    "initiate_calendar_connection": initiate_calendar_connection_tool,
    "send_onboarding_completion_message": send_onboarding_completion_message_tool, # New Tool
    "interpret_list_reply": interpret_list_reply_tool,
}
TOOL_PARAM_MODELS = {
    "create_todo": CreateToDoParams,
    "create_reminder": CreateReminderParams,
    "propose_task_slots": ProposeTaskSlotsParams,
    "finalize_task_and_book_sessions": FinalizeTaskAndBookSessionsParams,
    "update_item_details": UpdateItemDetailsParams,
    "format_list_for_display": FormatListForDisplayParams,
    "update_user_preferences": UpdateUserPreferencesParams,
    "initiate_calendar_connection": InitiateCalendarConnectionParams,
    "send_onboarding_completion_message": SendOnboardingCompletionMessageParams, # New Tool
    "interpret_list_reply": InterpretListReplyParams,
}
# --- END OF FULL agents/tool_definitions.py ---