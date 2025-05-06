# --- START OF FULL agents/orchestrator_agent.py ---
import json
import os
import traceback
from typing import Dict, List, Optional, Any # Retain Optional for Pydantic/OpenAI models
from datetime import datetime
import pytz
import re

# Instructor/LLM Imports
from services.llm_interface import get_instructor_client
from openai import OpenAI # Base client for types if needed
from openai.types.chat import ChatCompletionMessage, ChatCompletionToolParam, ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.shared_params import FunctionDefinition

# Tool Imports & Helpers
# --- Uses the UPDATED tools from tool_definitions ---
from .tool_definitions import AVAILABLE_TOOLS, TOOL_PARAM_MODELS
# --------------------------------------------------
import services.task_manager as task_manager # For parsing duration used in prompt prep

# Utilities
from tools.logger import log_info, log_error, log_warning
import yaml
import pydantic

# --- Load Standard Messages ---
_messages = {}
try:
    messages_path = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path):
        with open(messages_path, 'r', encoding="utf-8") as f:
             content = f.read()
             if content.strip(): f.seek(0); _messages = yaml.safe_load(f) or {}
             else: _messages = {}
    else: log_warning("orchestrator_agent", "init", f"{messages_path} not found."); _messages = {}
except Exception as e: log_error("orchestrator_agent", "init", f"Failed to load messages.yaml: {e}", e); _messages = {}
GENERIC_ERROR_MSG = _messages.get("generic_error_message", "Sorry, an unexpected error occurred.")


# --- Function to Load Prompt ---
_PROMPT_CACHE: Dict[str, Optional[str]] = {}
def load_orchestrator_prompt() -> Optional[str]:
    """Loads the orchestrator system prompt from the YAML file, with simple caching."""
    # This function remains the same, it just loads whatever prompt is in the file
    prompts_path = os.path.join("config", "prompts.yaml"); cache_key = prompts_path
    if cache_key in _PROMPT_CACHE: return _PROMPT_CACHE[cache_key]
    prompt_text_result: Optional[str] = None
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            content = f.read();
            if not content.strip(): raise ValueError("Prompts file is empty.")
            f.seek(0); all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")
            prompt_text = all_prompts.get("orchestrator_agent_system_prompt")
            if not prompt_text or not prompt_text.strip(): log_error("orchestrator_agent", "load_orchestrator_prompt", "Key 'orchestrator_agent_system_prompt' NOT FOUND or EMPTY."); prompt_text_result = None
            else: log_info("orchestrator_agent", "load_orchestrator_prompt", "Orchestrator prompt loaded successfully."); prompt_text_result = prompt_text
    except Exception as e: log_error("orchestrator_agent", "load_orchestrator_prompt", f"CRITICAL: Failed to load/parse orchestrator prompt: {e}", e); prompt_text_result = None
    _PROMPT_CACHE[cache_key] = prompt_text_result; return prompt_text_result


# --- Main Handler Function (Core logic remains the same) ---
def handle_user_request(
    user_id: str, message: str, history: List[Dict], preferences: Dict,
    task_context: List[Dict], calendar_context: List[Dict]
) -> str:
    """
    Handles the user's request using the Orchestrator pattern with Instructor/Tool Use.
    Relies purely on the LLM for conversational flow and tool invocation decisions.
    Handles multiple tool calls requested by the LLM in a single turn.
    """
    fn_name = "handle_user_request"
    log_info("orchestrator_agent", fn_name, f"Processing request for {user_id}: '{message[:50]}...'")

    orchestrator_system_prompt = load_orchestrator_prompt()
    if not orchestrator_system_prompt:
         log_error("orchestrator_agent", fn_name, "Orchestrator system prompt could not be loaded.")
         return GENERIC_ERROR_MSG

    client: Optional[OpenAI] = get_instructor_client()
    if not client:
        log_error("orchestrator_agent", fn_name, "LLM client unavailable.")
        return GENERIC_ERROR_MSG

    # --- Prepare Context for Prompt (No change needed here) ---
    try:
        user_timezone_str = preferences.get("TimeZone", "UTC"); user_timezone = pytz.utc
        try: user_timezone = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError: log_warning("orchestrator_agent", fn_name, f"Unknown timezone '{user_timezone_str}'. Defaulting UTC.")
        now = datetime.now(user_timezone)
        current_date_str, current_time_str, current_day_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.strftime("%A")
        history_limit, item_context_limit, calendar_context_limit = 30, 20, 20
        limited_history = history[-(history_limit*2):]; history_str = "\n".join([f"{m['sender'].capitalize()}: {m['content']}" for m in limited_history])
        def prepare_item(item):
             key_fields = ['event_id', 'item_id', 'title', 'description', 'date', 'time', 'type', 'status', 'estimated_duration', 'project']
             prep = {k: item.get(k) for k in key_fields if item.get(k) is not None}; prep['item_id'] = item.get('item_id', item.get('event_id'))
             if 'event_id' in prep and prep['event_id'] == prep['item_id']: del prep['event_id']
             return prep
        item_context_str = json.dumps([prepare_item(item) for item in task_context[:item_context_limit]], indent=2, default=str)
        calendar_context_str = json.dumps(calendar_context[:calendar_context_limit], indent=2, default=str)
        prefs_str = json.dumps(preferences, indent=2, default=str)
    except Exception as e: log_error("orchestrator_agent", fn_name, f"Error preparing context strings: {e}", e); return GENERIC_ERROR_MSG

    # --- Construct Initial Messages for LLM (No change needed here) ---
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": orchestrator_system_prompt},
        {"role": "system", "content": f"Current Reference Time ({user_timezone_str}): Date: {current_date_str}, Day: {current_day_str}, Time: {current_time_str}. Use this."},
        {"role": "system", "content": f"User Preferences:\n{prefs_str}"},
        {"role": "system", "content": f"History:\n{history_str}"},
        {"role": "system", "content": f"Active Items:\n{item_context_str}"},
        {"role": "system", "content": f"Calendar Events:\n{calendar_context_str}"},
        {"role": "user", "content": message}
    ]

    # --- Define Tools (Uses the UPDATED tools from tool_definitions) ---
    tools_for_llm: List[ChatCompletionToolParam] = []
    for tool_name, model in TOOL_PARAM_MODELS.items():
        if tool_name not in AVAILABLE_TOOLS: continue # Check against updated AVAILABLE_TOOLS
        func = AVAILABLE_TOOLS.get(tool_name)
        description = func.__doc__.strip() if func and func.__doc__ else f"Executes {tool_name}"
        try:
            params_schema = model.model_json_schema();
            # Allow empty schema for tools with no params
            if not params_schema.get('properties') and not model.model_fields: params_schema = {}
            elif not params_schema.get('properties'):
                 log_warning("orchestrator_agent", fn_name, f"Model {model.__name__} for tool {tool_name} has fields but generated empty properties schema.")
                 params_schema = {} # Default to empty if unexpected

            func_def: FunctionDefinition = {"name": tool_name, "description": description, "parameters": params_schema}
            tool_param: ChatCompletionToolParam = {"type": "function", "function": func_def}; tools_for_llm.append(tool_param)
        except Exception as e: log_error("orchestrator_agent", fn_name, f"Schema error for tool {tool_name}: {e}", e)
    if not tools_for_llm: log_error("orchestrator_agent", fn_name, "No tools defined for LLM."); return GENERIC_ERROR_MSG

    # --- Interaction Loop (No structural change needed) ---
    try:
        log_info("orchestrator_agent", fn_name, f"Invoking LLM for {user_id} (Initial Turn)...")
        response = client.chat.completions.create(
            model="gpt-4o", # Or other capable model
            messages=messages, # type: ignore
            tools=tools_for_llm,
            tool_choice="auto",
            temperature=0.1,
        )
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        # --- Scenario 1: LLM Responds Directly ---
        if not tool_calls:
            if response_message.content:
                log_info("orchestrator_agent", fn_name, "LLM responded directly (no tool call).")
                return response_message.content
            else:
                log_warning("orchestrator_agent", fn_name, "LLM response had no tool calls and no content.")
                return "Sorry, I couldn't process that request fully. Could you try rephrasing?"

        # --- Scenario 2: LLM Calls One or More Tools ---
        log_info("orchestrator_agent", fn_name, f"LLM requested {len(tool_calls)} tool call(s).")
        messages.append(response_message.model_dump(exclude_unset=True)) # Add assistant's tool call request
        tool_results_messages = []

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            tool_call_id = tool_call.id
            tool_args_str = tool_call.function.arguments
            tool_result_content = GENERIC_ERROR_MSG # Default

            log_info("orchestrator_agent", fn_name, f"Processing Tool Call ID: {tool_call_id}, Name: {tool_name}, Args: {tool_args_str[:150]}...")

            # Use updated AVAILABLE_TOOLS and TOOL_PARAM_MODELS here
            if tool_name not in AVAILABLE_TOOLS:
                log_warning("orchestrator_agent", fn_name, f"LLM tried unknown tool: {tool_name}. Sending error result back.")
                tool_result_content = json.dumps({"success": False, "message": f"Error: Unknown action '{tool_name}' requested."})
            else:
                tool_func = AVAILABLE_TOOLS[tool_name]
                param_model = TOOL_PARAM_MODELS[tool_name]
                try:
                    tool_args_dict = {}
                    if tool_args_str and tool_args_str.strip() != '{}': tool_args_dict = json.loads(tool_args_str)
                    elif not param_model.model_fields: tool_args_dict = {} # Handle no-arg tools

                    validated_params = param_model(**tool_args_dict)
                    tool_result_dict = tool_func(user_id, validated_params) # Call the tool
                    log_info("orchestrator_agent", fn_name, f"Tool {tool_name} (ID: {tool_call_id}) executed. Result: {tool_result_dict}")
                    tool_result_content = json.dumps(tool_result_dict)

                except json.JSONDecodeError:
                    log_error("orchestrator_agent", fn_name, f"Failed parse JSON args for {tool_name} (ID: {tool_call_id}): {tool_args_str}");
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid arguments provided for {tool_name}."})
                except pydantic.ValidationError as e:
                    log_error("orchestrator_agent", fn_name, f"Arg validation failed for {tool_name} (ID: {tool_call_id}). Err: {e.errors()}. Args: {tool_args_str}", e)
                    err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e.errors()])
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid parameters for {tool_name} - {err_summary}"})
                except Exception as e:
                    log_error("orchestrator_agent", fn_name, f"Error executing tool {tool_name} (ID: {tool_call_id}). Trace:\n{traceback.format_exc()}", e);
                    tool_result_content = json.dumps({"success": False, "message": f"Error performing action {tool_name}."})

            tool_results_messages.append({
                "tool_call_id": tool_call_id, "role": "tool",
                "name": tool_name, "content": tool_result_content,
            })

        messages.extend(tool_results_messages) # Add all tool results

        # --- Make SECOND LLM call with ALL tool results ---
        log_info("orchestrator_agent", fn_name, f"Invoking LLM again for {user_id} with {len(tool_results_messages)} tool result(s)...")
        second_response = client.chat.completions.create(
            model="gpt-4o", # Use the same model
            messages=messages, # type: ignore
            # No tools needed here
        )
        second_response_message = second_response.choices[0].message

        if second_response_message.content:
            log_info("orchestrator_agent", fn_name, "LLM generated final response after processing tool result(s).")
            return second_response_message.content
        else:
            log_warning("orchestrator_agent", fn_name, "LLM provided no content after processing tool result(s).")
            try: fallback_msg = json.loads(tool_results_messages[0]['content']).get("message", GENERIC_ERROR_MSG)
            except: fallback_msg = GENERIC_ERROR_MSG
            return fallback_msg

    # --- Outer Exception Handling ---
    except Exception as e:
        tb_str = traceback.format_exc()
        log_error("orchestrator_agent", fn_name, f"Core error in orchestrator logic for {user_id}. Traceback:\n{tb_str}", e)
        return GENERIC_ERROR_MSG
# --- END OF FULL agents/orchestrator_agent.py ---