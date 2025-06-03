# --- START OF FULL agents/orchestrator_agent.py (Consolidated & Controllable LLM File Logging) ---

import json
import os
import traceback
from typing import Dict, List, Any
from datetime import datetime, timezone
import pytz
import threading # For file logging lock

from services.llm_interface import get_instructor_client
from openai import OpenAI
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function as ToolCallFunction
from openai.types.shared_params import FunctionDefinition

from .tool_definitions import AVAILABLE_TOOLS, TOOL_PARAM_MODELS
from tools.logger import log_info, log_error, log_warning
import yaml
import pydantic

# --- Configuration for Detailed LLM Call Logging ---
LOG_DETAILED_LLM_CALLS_TO_FILE = True 
LLM_CALL_LOG_FILE_PATH = "logs/llm_calls_orchestrator_log.json" 
_llm_call_log_lock = threading.Lock() 
# --- End Configuration ---

# Ensure log directory exists
if LOG_DETAILED_LLM_CALLS_TO_FILE:
    try:
        os.makedirs(os.path.dirname(LLM_CALL_LOG_FILE_PATH), exist_ok=True)
    except OSError as e:
        log_error("orchestrator_agent", "init_log_dir", f"Could not create directory for LLM call logs: {e}")
        LOG_DETAILED_LLM_CALLS_TO_FILE = False 

# --- Prompt Cache ---
_ORCH_PROMPT_CACHE: Dict[str, str] = {} # <<< --- ADDED THIS LINE ---

# --- Load Standard Messages ---
_messages_orchestrator = {}
try:
    messages_path_orch = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path_orch):
        with open(messages_path_orch, 'r', encoding="utf-8") as f_orch_msg:
            _yaml_content_orch = f_orch_msg.read()
            if _yaml_content_orch.strip():
                f_orch_msg.seek(0)
                loaded_messages_orch = yaml.safe_load(f_orch_msg)
                if isinstance(loaded_messages_orch, dict):
                    _messages_orchestrator = loaded_messages_orch
    if not _messages_orchestrator: # Fallback if file empty or not dict
        log_warning("orchestrator_agent", "init", "Messages.yaml empty or invalid format. Using default error message.")
except Exception as e_load_msg_orch:
    log_error("orchestrator_agent", "init", f"Failed to load messages.yaml: {e_load_msg_orch}", e_load_msg_orch)
GENERIC_ERROR_MSG_ORCH = _messages_orchestrator.get("generic_error_message", "Sorry, an unexpected error occurred. Please try again.")
# --- End Load Standard Messages ---


def _log_llm_call_details(user_id: str, call_identifier: str, messages: List[ChatCompletionMessageParam], tools: List[Dict[str, Any]] | None, tool_choice: Any | None):
    """Appends the details of an LLM API call to a central JSON log file if enabled."""
    if not LOG_DETAILED_LLM_CALLS_TO_FILE:
        return

    serializable_messages = []
    for msg in messages:
        entry_to_log = {}
        if isinstance(msg, dict):
            entry_to_log = msg.copy()
        elif hasattr(msg, 'model_dump'):
            entry_to_log = msg.model_dump(exclude_unset=True, by_alias=True)
        else:
            entry_to_log = {"error": "Unknown message type", "original_message": str(msg)}
        serializable_messages.append(entry_to_log)

    log_entry = {
        "log_timestamp_utc_iso": datetime.now(timezone.utc).isoformat(), 
        "user_id": user_id,
        "call_identifier": call_identifier, 
        "messages_sent_to_llm": serializable_messages,
        "tools_parameter_sent_to_llm": tools,
        "tool_choice_parameter_sent_to_llm": str(tool_choice) if tool_choice is not None else None
    }
    
    with _llm_call_log_lock: 
        try:
            json_data = []
            if os.path.exists(LLM_CALL_LOG_FILE_PATH):
                with open(LLM_CALL_LOG_FILE_PATH, 'r', encoding='utf-8') as f:
                    try:
                        content = f.read()
                        if content.strip(): 
                           json_data = json.loads(content)
                        if not isinstance(json_data, list): 
                            log_warning("orchestrator_agent", "_log_llm_call_details", f"LLM call log file {LLM_CALL_LOG_FILE_PATH} was not a list. Resetting.")
                            json_data = []
                    except json.JSONDecodeError:
                        log_warning("orchestrator_agent", "_log_llm_call_details", f"LLM call log file {LLM_CALL_LOG_FILE_PATH} corrupted. Resetting.")
                        json_data = [] 
            
            json_data.append(log_entry)
            
            with open(LLM_CALL_LOG_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            log_error("orchestrator_agent", "_log_llm_call_details", f"User [{user_id}] FAILED to append LLM Call '{call_identifier}' to file: {e}", e)

# --- Agent State Manager Import for history updates ---
try:
    from services.agent_state_manager import add_message_to_user_history as add_to_persistent_history
    AGENT_STATE_MANAGER_HISTORY_AVAILABLE = True
except ImportError:
    AGENT_STATE_MANAGER_HISTORY_AVAILABLE = False
    log_error("orchestrator_agent", "import", "add_message_to_user_history from AgentStateManager not found.")
    def add_to_persistent_history(*args, **kwargs): pass # type: ignore
# --- End Import ---

_log_message_db_orch = None
try:
    from tools.activity_db import log_message_db as log_message_db_activity
    _log_message_db_orch = log_message_db_activity
except ImportError:
    log_error("orchestrator_agent", "import", "activity_db.log_message_db not found.")


def load_orchestrator_prompt() -> str:
    prompts_path = os.path.join("config", "prompts.yaml"); cache_key = prompts_path + "_orchestrator_agent_system_prompt"
    if cache_key in _ORCH_PROMPT_CACHE: return _ORCH_PROMPT_CACHE[cache_key] # Now _ORCH_PROMPT_CACHE is defined
    prompt_text_result: str = ""
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")
            prompt_text = all_prompts.get("orchestrator_agent_system_prompt")
            if not prompt_text or not prompt_text.strip():
                log_error("orchestrator_agent", "load_prompt", "Orchestrator prompt 'orchestrator_agent_system_prompt' missing or empty.")
            else: prompt_text_result = prompt_text
    except Exception as e_load:
        log_error("orchestrator_agent", "load_prompt", f"CRITICAL: Failed load orchestrator prompt: {e_load}", e_load)
    _ORCH_PROMPT_CACHE[cache_key] = prompt_text_result; return prompt_text_result

def _reconstruct_llm_history_from_rich_state(
    conversation_history_from_state: List[Dict[str, Any]]
) -> List[ChatCompletionMessageParam]:
    llm_api_history: List[ChatCompletionMessageParam] = []
    for entry in conversation_history_from_state:
        role = entry.get("role")
        content = entry.get("content")
        msg_for_api: Dict[str, Any] = {"role": role}
        if role == "user":
            msg_for_api["content"] = content if content is not None else ""
        elif role == "assistant":
            msg_for_api["content"] = content
            tool_calls_json_str = entry.get("tool_calls_json_str") 
            if tool_calls_json_str:
                try:
                    msg_for_api["tool_calls"] = json.loads(tool_calls_json_str)
                except json.JSONDecodeError:
                    log_error("orchestrator_agent", "_reconstruct_history", f"Failed to parse tool_calls_json_str from history: {tool_calls_json_str}")
            if msg_for_api.get("content") is None and "tool_calls" not in msg_for_api:
                msg_for_api["content"] = "" # Ensure content is not missing if no tool calls
        elif role == "tool":
            msg_for_api["tool_call_id"] = entry.get("tool_call_id") 
            msg_for_api["name"] = entry.get("name") 
            msg_for_api["content"] = content 
        else: # Skip unknown roles
            continue
        llm_api_history.append(msg_for_api) # type: ignore
    return llm_api_history

def handle_user_request(
    user_id: str, message: str, history: List[Dict[str, Any]], preferences: Dict,
    task_context: List[Dict], calendar_context: List[Dict]
) -> str:
    fn_name = "handle_user_request"
    log_info("orchestrator_agent", fn_name, f"User [{user_id}] Orchestrator received message: '{message[:150]}...'")

    orchestrator_system_prompt = load_orchestrator_prompt()
    if not orchestrator_system_prompt: return GENERIC_ERROR_MSG_ORCH 
    client: OpenAI | None = get_instructor_client()
    if not client: return GENERIC_ERROR_MSG_ORCH 

    try:
        user_timezone_str = preferences.get("TimeZone", "UTC")
        user_timezone = pytz.timezone(user_timezone_str) if user_timezone_str else pytz.utc
        now = datetime.now(user_timezone)
        current_date_str, current_time_str, current_day_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.strftime("%A")
        llm_context_history_for_api = _reconstruct_llm_history_from_rich_state(history)
        def prepare_item_for_llm(item_dict: Dict) -> Dict:
             essential_fields = ['item_id', 'type', 'title', 'description', 'date', 'time', 'status', 'estimated_duration', 'project', 'gcal_start_datetime']
             prepared = {k: item_dict.get(k) for k in essential_fields if item_dict.get(k) is not None}
             if 'event_id' in item_dict and 'item_id' not in prepared: prepared['item_id'] = item_dict['event_id']
             return prepared
        active_items_str_for_summary = json.dumps([prepare_item_for_llm(item) for item in task_context[:15]], indent=2, default=str, ensure_ascii=False)
        calendar_events_str_for_summary = json.dumps(calendar_context[:15], indent=2, default=str, ensure_ascii=False)
        user_prefs_str_for_summary = json.dumps(preferences, indent=2, default=str, ensure_ascii=False)
    except Exception as e_ctx_prep: 
        log_error("orchestrator_agent", fn_name, f"Error preparing context for {user_id}: {e_ctx_prep}", e_ctx_prep,user_id=user_id); return GENERIC_ERROR_MSG_ORCH

    system_context_summary_content = f"User State Context Summary (user {user_id}):\n" \
                                     f"1. Current Time/Date ({user_timezone_str}): {current_date_str}, {current_day_str}, {current_time_str}.\n" \
                                     f"2. User Preferences: {user_prefs_str_for_summary}\n" \
                                     f"3. Active Items (DB Snapshot): {active_items_str_for_summary}\n" \
                                     f"4. Calendar Events (Live GCal if connected): {calendar_events_str_for_summary}\n" \
                                     f"Full conversation history (including your tool use) follows."
    
    messages_for_api: List[ChatCompletionMessageParam] = [ 
        {"role": "system", "content": orchestrator_system_prompt}, # type: ignore
        {"role": "system", "content": system_context_summary_content}, # type: ignore
        *llm_context_history_for_api,
        {"role": "user", "content": message} # type: ignore
    ]

    tools_for_llm_list: List[Dict[str, Any]] = []
    if AVAILABLE_TOOLS and TOOL_PARAM_MODELS:
        for tool_name_iter, model_class in TOOL_PARAM_MODELS.items():
            tool_func = AVAILABLE_TOOLS.get(tool_name_iter)
            if not tool_func: continue
            description = tool_func.__doc__.strip() if tool_func.__doc__ else f"Executes {tool_name_iter}"
            try:
                params_schema = model_class.model_json_schema()
                if not params_schema.get('properties') and not model_class.model_fields: params_schema = {} 
                func_def: FunctionDefinition = {"name": tool_name_iter, "description": description, "parameters": params_schema}
                tools_for_llm_list.append({"type": "function", "function": func_def})
            except Exception as e_schema: log_error("orchestrator_agent", fn_name, f"Schema gen error tool {tool_name_iter}: {e_schema}", e_schema,user_id=user_id)
    final_tools_for_llm = tools_for_llm_list if tools_for_llm_list else None
    tool_choice_val: Any = "auto" if final_tools_for_llm else None

    try:
        _log_llm_call_details(user_id, "1st_call_orchestrator", messages_for_api, final_tools_for_llm, tool_choice_val)
        log_info("orchestrator_agent", fn_name, f"User [{user_id}] Invoking LLM (1st call)... Tools available: {bool(final_tools_for_llm)}")
        response = client.chat.completions.create(
            model="gpt-4o", messages=messages_for_api,
            tools=final_tools_for_llm, tool_choice=tool_choice_val, # type: ignore
            temperature=0.1,
        )
        response_message = response.choices[0].message
        llm_text_content = response_message.content
        tool_calls_from_llm: List[ChatCompletionMessageToolCall] | None = response_message.tool_calls

        if tool_calls_from_llm:
            log_info("orchestrator_agent", fn_name, f"User [{user_id}] LLM (1st call) DECIDED TO CALL TOOL(S): {json.dumps([tc.model_dump(exclude_none=True) for tc in tool_calls_from_llm], indent=2, ensure_ascii=False)}")
            if llm_text_content: log_info("orchestrator_agent", fn_name, f"User [{user_id}] LLM (1st call) also provided interim text: '{llm_text_content[:100]}...'")
        elif llm_text_content:
            log_info("orchestrator_agent", fn_name, f"User [{user_id}] LLM (1st call) RESPONDED WITH TEXT directly: '{llm_text_content[:100]}...'")
        else:
            log_warning("orchestrator_agent", fn_name, f"User [{user_id}] LLM (1st call) returned no text and no tool calls.")

        if _log_message_db_orch and tool_calls_from_llm: 
            tool_calls_json_str_for_db = None
            try: tool_calls_json_str_for_db = json.dumps([tc.model_dump() for tc in tool_calls_from_llm])
            except Exception as e_tc_json: log_error("orchestrator_agent", fn_name, f"Error serializing tool_calls for DB log: {e_tc_json}", e_tc_json, user_id=user_id)
            _log_message_db_orch(
                user_id=user_id, role="assistant",
                message_type="agent_tool_call_request",
                content_text=llm_text_content, 
                tool_calls_json=tool_calls_json_str_for_db
            )
        
        if AGENT_STATE_MANAGER_HISTORY_AVAILABLE and tool_calls_from_llm:
            assistant_tool_calls_for_history = [tc.model_dump(exclude_unset=True, by_alias=True) for tc in tool_calls_from_llm]
            add_to_persistent_history(
                user_id=user_id, role="assistant", message_type="agent_tool_call_request_orch",
                content=llm_text_content, tool_calls_obj=assistant_tool_calls_for_history
            )

        if not tool_calls_from_llm:
            if llm_text_content:
                log_info("orchestrator_agent", fn_name, f"User [{user_id}] Orchestrator returning direct text: '{llm_text_content[:500]}'")
                return llm_text_content
            else:
                log_warning("orchestrator_agent", fn_name, f"LLM returned no text and no tool calls for user {user_id}. Using generic error.", user_id=user_id)
                return GENERIC_ERROR_MSG_ORCH # Return generic error if no text and no tools

        current_turn_assistant_message_for_api: ChatCompletionMessageParam = {"role": "assistant", "content": llm_text_content if llm_text_content is not None else ""} # type: ignore
        if tool_calls_from_llm:
            current_turn_assistant_message_for_api["tool_calls"] = [tc.model_dump(exclude_unset=True, by_alias=True) for tc in tool_calls_from_llm] # type: ignore
        messages_for_api.append(current_turn_assistant_message_for_api)
        
        tool_results_for_llm_api_list: List[ChatCompletionMessageParam] = []

        for tool_call_obj in tool_calls_from_llm:
            tool_name = tool_call_obj.function.name
            tool_call_id = tool_call_obj.id
            tool_args_str = tool_call_obj.function.arguments
            tool_result_dict_from_py: Dict = {"success": False, "message": GENERIC_ERROR_MSG_ORCH}
            log_info("orchestrator_agent", fn_name, f"User [{user_id}] EXECUTING TOOL: {tool_name}, Call ID: {tool_call_id}, Args: {tool_args_str[:200]}")
            if tool_name not in AVAILABLE_TOOLS:
                tool_result_dict_from_py = {"success": False, "message": f"Error: Unknown action '{tool_name}'."}
            else:
                tool_func_py = AVAILABLE_TOOLS[tool_name]
                param_model_py = TOOL_PARAM_MODELS[tool_name]
                try:
                    tool_args_dict_py = {}
                    if tool_args_str and tool_args_str.strip() and tool_args_str.strip() != '{}':
                        tool_args_dict_py = json.loads(tool_args_str)
                    elif not param_model_py.model_fields: tool_args_dict_py = {}
                    validated_params_py = param_model_py(**tool_args_dict_py)
                    tool_result_dict_from_py = tool_func_py(user_id, validated_params_py)
                except json.JSONDecodeError as e_json_dec:
                    err_msg = f"Error: Invalid JSON arguments format for tool {tool_name}. Args: '{tool_args_str}'. Error: {e_json_dec}"
                    tool_result_dict_from_py = {"success": False, "message": err_msg}; log_error("orchestrator_agent", fn_name, err_msg, e_json_dec, user_id=user_id)
                except pydantic.ValidationError as e_val:
                    err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e_val.errors()])
                    err_msg = f"Error: Invalid parameters for tool {tool_name}: {err_summary}. Args: {tool_args_str}"
                    tool_result_dict_from_py = {"success": False, "message": err_msg}; log_error("orchestrator_agent", fn_name, err_msg, e_val, user_id=user_id)
                except Exception as e_tool_exec:
                    tb_str_tool = traceback.format_exc()
                    err_msg = f"Error performing action {tool_name}: {str(e_tool_exec)[:100]}"
                    tool_result_dict_from_py = {"success": False, "message": err_msg}; log_error("orchestrator_agent", fn_name, f"Error executing tool {tool_name} (ID: {tool_call_id}). Trace:\n{tb_str_tool}", e_tool_exec, user_id=user_id)

            log_info("orchestrator_agent", fn_name, f"User [{user_id}] TOOL '{tool_name}' (Call ID: {tool_call_id}) EXECUTION RESULT: {json.dumps(tool_result_dict_from_py, default=str, ensure_ascii=False)}")
            
            tool_result_json_str_for_api = json.dumps(tool_result_dict_from_py, default=str)
            if _log_message_db_orch: 
                _log_message_db_orch(
                    user_id=user_id, role="tool", message_type="tool_execution_result",
                    content_text=tool_result_json_str_for_api,
                    tool_name=tool_name, associated_tool_call_id=tool_call_id
                )
            if AGENT_STATE_MANAGER_HISTORY_AVAILABLE: 
                add_to_persistent_history(
                    user_id=user_id, role="tool", message_type="tool_execution_result_orch",
                    content=tool_result_json_str_for_api, tool_name=tool_name, tool_call_id=tool_call_id
                )
            tool_results_for_llm_api_list.append({ # type: ignore
                "tool_call_id": tool_call_id, "role": "tool",
                "name": tool_name, "content": tool_result_json_str_for_api,
            })

        messages_for_api.extend(tool_results_for_llm_api_list)
        
        _log_llm_call_details(user_id, "2nd_call_after_tools_orchestrator", messages_for_api, None, None)
        log_info("orchestrator_agent", fn_name, f"User [{user_id}] Invoking LLM (2nd call, after tool execution)...")
        second_response = client.chat.completions.create(
            model="gpt-4o", messages=messages_for_api, temperature=0.1,
        )
        final_text_response = second_response.choices[0].message.content

        if final_text_response:
            log_info("orchestrator_agent", fn_name, f"User [{user_id}] Orchestrator returning final text: '{final_text_response[:500]}'")
            return final_text_response
        else:
            log_warning("orchestrator_agent", fn_name, f"User [{user_id}] LLM (2nd call) returned no text. Fallback.", user_id=user_id)
            try:
                if tool_results_for_llm_api_list:
                    first_tool_result_parsed = json.loads(tool_results_for_llm_api_list[0]['content']) # type: ignore
                    return first_tool_result_parsed.get("message", GENERIC_ERROR_MSG_ORCH)
                return GENERIC_ERROR_MSG_ORCH
            except: return GENERIC_ERROR_MSG_ORCH

    except Exception as e_outer_loop:
        tb_str = traceback.format_exc()
        log_error("orchestrator_agent", fn_name, f"Core error in orchestrator for {user_id}. Trace:\n{tb_str}", e_outer_loop, user_id=user_id)
        if _log_message_db_orch: _log_message_db_orch(user_id=user_id, role="system_internal", message_type="system_error_to_user", content_text=GENERIC_ERROR_MSG_ORCH)
        return GENERIC_ERROR_MSG_ORCH

# --- END OF FULL agents/orchestrator_agent.py (Consolidated & Controllable LLM File Logging) ---