# --- START OF FULL agents/orchestrator_agent.py ---

import json
import os
import traceback
from typing import Dict, List, Any
from datetime import datetime, timezone # Added timezone
import pytz

from services.llm_interface import get_instructor_client
from openai import OpenAI
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function as ToolCallFunction
from openai.types.shared_params import FunctionDefinition # Keep this for tool definition

from .tool_definitions import AVAILABLE_TOOLS, TOOL_PARAM_MODELS
from tools.logger import log_info, log_error, log_warning
import yaml
import pydantic

# --- Database Logging Import for Orchestrator ---
# We will use the revised log_message_db from activity_db
_log_message_db_orch = None
try:
    from tools.activity_db import log_message_db as log_message_db_activity
    _log_message_db_orch = log_message_db_activity
    log_info("orchestrator_agent", "import", "Successfully linked activity_db.log_message_db for rich message logging.")
except ImportError:
    log_error("orchestrator_agent", "import", "activity_db.log_message_db not found. Rich message logging to DB will be skipped.")

_messages_orchestrator = {}
try:
    messages_path_orch = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path_orch):
        with open(messages_path_orch, 'r', encoding="utf-8") as f_orch:
            content_orch = f_orch.read()
            _messages_orchestrator = yaml.safe_load(f_orch.seek(0) or f_orch) if content_orch.strip() else {}
    else: _messages_orchestrator = {}
except Exception as e_orch_msg_load:
    _messages_orchestrator = {}; log_error("orchestrator_agent", "init", f"Failed to load messages.yaml: {e_orch_msg_load}", e_orch_msg_load)
GENERIC_ERROR_MSG_ORCH = _messages_orchestrator.get("generic_error_message", "Sorry, an unexpected error occurred.")

_ORCH_PROMPT_CACHE: Dict[str, str] = {}
def load_orchestrator_prompt() -> str:
    prompts_path = os.path.join("config", "prompts.yaml"); cache_key = prompts_path + "_orchestrator_v099" # Ensure new prompt is loaded
    if cache_key in _ORCH_PROMPT_CACHE: return _ORCH_PROMPT_CACHE[cache_key]
    prompt_text_result: str = ""
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")
            prompt_text = all_prompts.get("orchestrator_agent_system_prompt") # Use V0.9.9
            if not prompt_text or not prompt_text.strip(): 
                log_error("orchestrator_agent", "load_prompt", "Orchestrator prompt missing or empty.")
            else: prompt_text_result = prompt_text
    except Exception as e_load: 
        log_error("orchestrator_agent", "load_prompt", f"CRITICAL: Failed load orchestrator prompt: {e_load}", e_load)
    _ORCH_PROMPT_CACHE[cache_key] = prompt_text_result; return prompt_text_result

def _reconstruct_llm_history_from_rich_state(
    conversation_history_from_state: List[Dict[str, Any]]
) -> List[ChatCompletionMessageParam]:
    """
    Reconstructs history for LLM API from the rich AgentStateManager history.
    Parses JSON strings in 'content' (for tool results) or 'tool_calls_json_str' (for assistant tool requests)
    back into Python objects for the OpenAI API.
    """
    llm_api_history: List[ChatCompletionMessageParam] = []
    for entry in conversation_history_from_state:
        role = entry.get("role")
        content = entry.get("content") # For user text, assistant text, or JSON string of tool result
        
        msg_for_api: Dict[str, Any] = {"role": role}

        if role == "user":
            msg_for_api["content"] = content if content is not None else ""
        elif role == "assistant":
            msg_for_api["content"] = content # Can be None if only tool_calls
            tool_calls_json_str = entry.get("tool_calls_json_str")
            if tool_calls_json_str:
                try:
                    tool_calls_list = json.loads(tool_calls_json_str)
                    # Ensure it's a list of dicts as expected by OpenAI API for tool_calls
                    if isinstance(tool_calls_list, list) and all(isinstance(tc, dict) for tc in tool_calls_list):
                        msg_for_api["tool_calls"] = tool_calls_list
                    else:
                        log_warning("orchestrator_agent", "_reconstruct_history", f"Parsed tool_calls_json_str is not a list of dicts: {tool_calls_list}")
                except json.JSONDecodeError:
                    log_error("orchestrator_agent", "_reconstruct_history", f"Failed to parse tool_calls_json_str: {tool_calls_json_str}")
            # If content is None and there are no tool_calls, ensure content is at least an empty string for some models
            if msg_for_api.get("content") is None and "tool_calls" not in msg_for_api:
                msg_for_api["content"] = ""

        elif role == "tool":
            msg_for_api["tool_call_id"] = entry.get("tool_call_id")
            msg_for_api["name"] = entry.get("name")
            msg_for_api["content"] = content # This is already the JSON string of the tool result
        
        else: # Should not happen if roles are validated upstream
            log_warning("orchestrator_agent", "_reconstruct_history", f"Unknown role in history entry: {role}")
            continue
            
        llm_api_history.append(msg_for_api) # type: ignore
    return llm_api_history


def handle_user_request(
    user_id: str, message: str, history: List[Dict[str, Any]], preferences: Dict, # history is now rich
    task_context: List[Dict], calendar_context: List[Dict]
) -> str:
    fn_name = "handle_user_request"
    orchestrator_system_prompt = load_orchestrator_prompt()
    if not orchestrator_system_prompt: return GENERIC_ERROR_MSG_ORCH

    client: OpenAI | None = get_instructor_client()
    if not client: return GENERIC_ERROR_MSG_ORCH

    # Log incoming user message to DB
    if _log_message_db_orch:
        _log_message_db_orch(
            user_id=user_id, role="user", message_type="user_text",
            content_text=message, # user_message_timestamp_iso can be added if bridge provides it
        )

    try:
        user_timezone_str = preferences.get("TimeZone", "UTC")
        user_timezone = pytz.timezone(user_timezone_str) if user_timezone_str else pytz.utc
        now = datetime.now(user_timezone)
        current_date_str, current_time_str, current_day_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.strftime("%A")
        
        # History from AgentStateManager is already limited and contains rich objects
        # We need to reconstruct it for the LLM API call
        llm_context_history = _reconstruct_llm_history_from_rich_state(history)
        
        def prepare_item_for_llm(item_dict: Dict) -> Dict:
             essential_fields = ['item_id', 'type', 'title', 'description', 'date', 'time', 'status', 'estimated_duration', 'project', 'gcal_start_datetime']
             prepared = {k: item_dict.get(k) for k in essential_fields if item_dict.get(k) is not None}
             if 'event_id' in item_dict and 'item_id' not in prepared: prepared['item_id'] = item_dict['event_id']
             return prepared
        active_items_str = json.dumps([prepare_item_for_llm(item) for item in task_context[:25]], indent=2, default=str)
        calendar_events_str = json.dumps(calendar_context[:25], indent=2, default=str)
        user_prefs_str = json.dumps(preferences, indent=2, default=str)
    except Exception as e_ctx_prep: 
        log_error("orchestrator_agent", fn_name, f"Error preparing context for {user_id}: {e_ctx_prep}", e_ctx_prep,user_id=user_id); return GENERIC_ERROR_MSG_ORCH

    system_context_summary_msg = f"User State Context Summary (user {user_id}):\n" \
                                 f"1. Current Time/Date ({user_timezone_str}): {current_date_str}, {current_day_str}, {current_time_str}.\n" \
                                 f"2. User Preferences: {user_prefs_str}\n" \
                                 f"3. Active Items (DB Snapshot): {active_items_str}\n" \
                                 f"4. Calendar Events (Live GCal if connected): {calendar_events_str}\n" \
                                 f"Full conversation history (including your tool use) follows."

    messages_for_api: List[ChatCompletionMessageParam] = [ # type: ignore
        {"role": "system", "content": orchestrator_system_prompt},
        {"role": "system", "content": system_context_summary_msg},
        *llm_context_history, # Unpack the reconstructed rich history
        {"role": "user", "content": message}
    ]

    tools_for_llm_list: List[Dict[str, Any]] = []
    if AVAILABLE_TOOLS and TOOL_PARAM_MODELS:
        for tool_name, model_class in TOOL_PARAM_MODELS.items():
            tool_func = AVAILABLE_TOOLS.get(tool_name)
            if not tool_func: continue
            description = tool_func.__doc__.strip() if tool_func.__doc__ else f"Executes {tool_name}"
            try:
                params_schema = model_class.model_json_schema()
                if not params_schema.get('properties') and not model_class.model_fields: params_schema = {} 
                func_def: FunctionDefinition = {"name": tool_name, "description": description, "parameters": params_schema}
                tools_for_llm_list.append({"type": "function", "function": func_def})
            except Exception as e_schema: log_error("orchestrator_agent", fn_name, f"Schema gen error tool {tool_name}: {e_schema}", e_schema,user_id=user_id)
    
    final_tools_for_llm = tools_for_llm_list if tools_for_llm_list else None
    tool_choice_val: str | None = "auto" if final_tools_for_llm else None

    try:
        response = client.chat.completions.create(
            model="gpt-4o", messages=messages_for_api,
            tools=final_tools_for_llm, tool_choice=tool_choice_val, # type: ignore
            temperature=0.1,
        )
        response_message = response.choices[0].message
        llm_text_content = response_message.content # Text part of LLM's response
        tool_calls_from_llm: List[ChatCompletionMessageToolCall] | None = response_message.tool_calls

        # Log LLM's response (assistant's turn) to DB
        if _log_message_db_orch:
            tool_calls_json_str_for_db = None
            if tool_calls_from_llm:
                try: tool_calls_json_str_for_db = json.dumps([tc.model_dump() for tc in tool_calls_from_llm])
                except Exception as e_tc_json: log_error("orchestrator_agent", fn_name, f"Error serializing tool_calls for DB log: {e_tc_json}", e_tc_json)
            
            _log_message_db_orch(
                user_id=user_id, role="assistant",
                message_type="agent_tool_call_request" if tool_calls_from_llm else "agent_text_response",
                content_text=llm_text_content,
                tool_calls_json=tool_calls_json_str_for_db
            )

        if not tool_calls_from_llm: # LLM responded directly with text
            if llm_text_content: return llm_text_content
            else: return "I'm not sure how to respond to that. Can you try rephrasing?" # Fallback

        # --- Tool Execution Loop ---
        # Add assistant's message (with tool_calls) to messages list for the next LLM call
        current_turn_assistant_message: ChatCompletionMessageParam = {"role": "assistant", "content": llm_text_content} # type: ignore
        if tool_calls_from_llm: # Ensure tool_calls is present if not None
            current_turn_assistant_message["tool_calls"] = [tc.model_dump(exclude_unset=True) for tc in tool_calls_from_llm]
        messages_for_api.append(current_turn_assistant_message)
        
        tool_results_for_llm_api: List[ChatCompletionMessageParam] = [] # type: ignore

        for tool_call in tool_calls_from_llm:
            tool_name = tool_call.function.name
            tool_call_id = tool_call.id
            tool_args_str = tool_call.function.arguments
            tool_result_dict_from_py: Dict = {"success": False, "message": GENERIC_ERROR_MSG_ORCH} # Default

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
                except json.JSONDecodeError:
                    tool_result_dict_from_py = {"success": False, "message": f"Error: Invalid arguments format for {tool_name}."}
                except pydantic.ValidationError as e_val:
                    err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e_val.errors()])
                    tool_result_dict_from_py = {"success": False, "message": f"Error: Invalid parameters for {tool_name}: {err_summary}"}
                except Exception as e_tool_exec:
                    log_error("orchestrator_agent", fn_name, f"Error executing tool {tool_name} (ID: {tool_call_id}). Trace:\n{traceback.format_exc()}", e_tool_exec, user_id=user_id)
                    tool_result_dict_from_py = {"success": False, "message": f"Error performing action {tool_name}: {str(e_tool_exec)[:100]}"} # Truncate long errors

            # Log tool execution result to DB
            tool_result_json_str_for_db = json.dumps(tool_result_dict_from_py, default=str)
            if _log_message_db_orch:
                _log_message_db_orch(
                    user_id=user_id, role="tool", message_type="tool_execution_result",
                    content_text=tool_result_json_str_for_db, # Store full JSON result as content_text for role 'tool'
                    tool_name=tool_name, associated_tool_call_id=tool_call_id
                )
            
            tool_results_for_llm_api.append({
                "tool_call_id": tool_call_id, "role": "tool",
                "name": tool_name, "content": tool_result_json_str_for_db, # Pass JSON string to LLM
            })

        messages_for_api.extend(tool_results_for_llm_api)

        second_response = client.chat.completions.create(
            model="gpt-4o", messages=messages_for_api, temperature=0.1,
        )
        final_text_response = second_response.choices[0].message.content

        # Log agent's final text response (after tool use) to DB
        if _log_message_db_orch and final_text_response:
            _log_message_db_orch(
                user_id=user_id, role="assistant", message_type="agent_text_response",
                content_text=final_text_response
            )

        if final_text_response: 
            log_info("orchestrator_agent", fn_name, f"USER_ID [{user_id}] FINAL RESPONSE TO BE SENT: '{str(final_text_response)[:500]}'") 
            return final_text_response
        else: # Fallback if LLM gives no text after tool
            try:
                first_tool_result_parsed = json.loads(tool_results_for_llm_api[0]['content']) # type: ignore
                return first_tool_result_parsed.get("message", GENERIC_ERROR_MSG_ORCH)
            except: return GENERIC_ERROR_MSG_ORCH

    except Exception as e_outer_loop:
        tb_str = traceback.format_exc()
        log_error("orchestrator_agent", fn_name, f"Core error in orchestrator for {user_id}. Trace:\n{tb_str}", e_outer_loop, user_id=user_id)
        # Log this system-level error to user in DB
        if _log_message_db_orch:
            _log_message_db_orch(user_id=user_id, role="system_internal", message_type="system_error_to_user", content_text=GENERIC_ERROR_MSG_ORCH)
        return GENERIC_ERROR_MSG_ORCH

# --- END OF FULL agents/orchestrator_agent.py ---