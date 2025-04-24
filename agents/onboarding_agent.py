# --- START OF FILE agents/onboarding_agent.py ---
import json
import os
import traceback
from typing import Dict, List, Optional, Any
from datetime import datetime
import pytz

# Instructor/LLM Imports
from services.llm_interface import get_instructor_client
from openai import OpenAI
from openai.types.chat import ChatCompletionMessage, ChatCompletionToolParam, ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.shared_params import FunctionDefinition

# Tool Imports (Limited Set)
from .tool_definitions import (
    AVAILABLE_TOOLS,
    TOOL_PARAM_MODELS,
    UpdateUserPreferencesParams,
    InitiateCalendarConnectionParams,
)

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
    else: log_warning("onboarding_agent", "init", f"{messages_path} not found."); _messages = {}
except Exception as e: log_error("onboarding_agent", "init", f"Failed to load messages.yaml: {e}", e); _messages = {}
GENERIC_ERROR_MSG = _messages.get("generic_error_message", "Sorry, an unexpected error occurred.")
SETUP_START_MSG = _messages.get("setup_starting_message", "Great! Let's set things up.")

# --- Function to Load Onboarding Prompt ---
_ONBOARDING_PROMPT_CACHE = {}
def load_onboarding_prompt() -> Optional[str]:
    """Loads the onboarding system prompt from the YAML file, with caching."""
    prompts_path = os.path.join("config", "prompts.yaml")
    cache_key = prompts_path + "_onboarding"
    if cache_key in _ONBOARDING_PROMPT_CACHE:
        return _ONBOARDING_PROMPT_CACHE[cache_key]

    prompt_text_result: Optional[str] = None
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            content = f.read();
            if not content.strip(): raise ValueError("Prompts file is empty.")
            f.seek(0); all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")
            prompt_text = all_prompts.get("onboarding_agent_system_prompt")
            if not prompt_text or not prompt_text.strip():
                log_error("onboarding_agent", "load_onboarding_prompt", "Key 'onboarding_agent_system_prompt' NOT FOUND or EMPTY.")
                prompt_text_result = None
            else:
                log_info("onboarding_agent", "load_onboarding_prompt", "Onboarding prompt loaded successfully.")
                prompt_text_result = prompt_text
    except Exception as e:
        log_error("onboarding_agent", "load_onboarding_prompt", f"CRITICAL: Failed load/parse onboarding prompt: {e}", e)
        prompt_text_result = None

    _ONBOARDING_PROMPT_CACHE[cache_key] = prompt_text_result
    return prompt_text_result

# --- REMOVED Onboarding State Helpers ---
# The pure LLM flow relies on history and current prefs in context

# --- Helper for LLM Clarification (If needed for onboarding - keep for now) ---
def _get_clarification_from_llm(client: OpenAI, question: str, user_reply: str, expected_choices: List[str]) -> str:
    """Uses LLM to interpret user reply against expected choices."""
    # This helper might still be useful if the LLM asks a yes/no for calendar
    log_info("onboarding_agent", "_get_clarification_from_llm", f"Asking LLM to clarify: Q='{question}' Reply='{user_reply}' Choices={expected_choices}")
    system_message = f"""
You are helping a user interact with a task assistant during setup.
The assistant asked the user a question, and the user replied.
Your task is to determine which of the expected choices the user's reply corresponds to.
The original question was: "{question}"
The user's reply was: "{user_reply}"
The expected choices are: {expected_choices}

Analyze the user's reply and determine the choice.
Respond ONLY with one of the following exact strings: {', '.join([f"'{choice}'" for choice in expected_choices])} or 'unclear'.
"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo", # Use a cheaper/faster model for clarification
            messages=[{"role": "system", "content": system_message}],
            temperature=0.0,
            max_tokens=10
        )
        llm_choice = response.choices[0].message.content.strip().lower().replace("'", "")
        log_info("onboarding_agent", "_get_clarification_from_llm", f"LLM clarification result: '{llm_choice}'")
        if llm_choice in expected_choices or llm_choice == 'unclear':
            return llm_choice
        else:
            log_warning("onboarding_agent", "_get_clarification_from_llm", f"LLM returned unexpected clarification: '{llm_choice}'")
            return 'unclear'
    except Exception as e:
        log_error("onboarding_agent", "_get_clarification_from_llm", f"Error during LLM clarification call: {e}", e)
        return 'unclear'

# --- Main Onboarding Handler Function ---
def handle_onboarding_request(
    user_id: str,
    message: str,
    history: List[Dict],
    preferences: Dict # Current preferences passed in
) -> str:
    """Handles the user's request during the onboarding phase using pure LLM flow."""
    log_info("onboarding_agent", "handle_onboarding_request", f"Handling onboarding request for {user_id}: '{message[:50]}...'")
    fn_name = "handle_onboarding_request" # Use function name for logging

    onboarding_system_prompt = load_onboarding_prompt()
    if not onboarding_system_prompt:
         log_error(fn_name, "load_onboarding_prompt", "Onboarding system prompt could not be loaded.") # Corrected log identifier
         return GENERIC_ERROR_MSG

    client: OpenAI = get_instructor_client()
    if not client:
        log_error(fn_name, "get_instructor_client", "LLM client unavailable.") # Corrected log identifier
        return GENERIC_ERROR_MSG

    # --- Prepare Context (Simpler than main orchestrator, focuses on prefs) ---
    try:
        user_timezone_str = preferences.get("TimeZone", "UTC"); user_timezone = pytz.utc
        try:
            user_timezone = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError:
            log_warning(fn_name, "prepare_context", f"Unknown timezone '{user_timezone_str}'. Using UTC.") # Corrected log identifier
        now = datetime.now(user_timezone); current_date_str = now.strftime("%Y-%m-%d"); current_time_str = now.strftime("%H:%M"); current_day_str = now.strftime("%A")

        history_limit = 10
        limited_history = history[-(history_limit*2):]
        history_str = "\n".join([f"{m['sender'].capitalize()}: {m['content']}" for m in limited_history])

        prefs_str = json.dumps(preferences, indent=2, default=str)

        initial_interaction = len(history) <= 1 # If only user message is present
        intro_message = SETUP_START_MSG if initial_interaction else ""

    except Exception as e:
        log_error(fn_name, "prepare_context", f"Error preparing context: {e}", e) # Corrected log identifier
        return GENERIC_ERROR_MSG

    # --- Construct Messages for Onboarding LLM ---
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": onboarding_system_prompt},
        {"role": "system", "content": f"Current User Preferences:\n```json\n{prefs_str}\n```"},
        {"role": "system", "content": f"Conversation History:\n{history_str}"},
        {"role": "user", "content": message}
    ]
    if intro_message and initial_interaction:
         messages.insert(-1, {"role": "assistant", "content": intro_message})


    # --- Define Tools AVAILABLE for Onboarding ---
    onboarding_tools_available = {
        "update_user_preferences": AVAILABLE_TOOLS.get("update_user_preferences"),
        "initiate_calendar_connection": AVAILABLE_TOOLS.get("initiate_calendar_connection"),
    }
    onboarding_tool_models = {
        "update_user_preferences": UpdateUserPreferencesParams,
        "initiate_calendar_connection": InitiateCalendarConnectionParams,
    }
    onboarding_tools_available = {k:v for k,v in onboarding_tools_available.items() if v is not None}

    tools_for_llm: List[ChatCompletionToolParam] = []
    for tool_name, model in onboarding_tool_models.items():
        if tool_name not in onboarding_tools_available: continue
        func = onboarding_tools_available.get(tool_name)
        description = func.__doc__.strip() if func and func.__doc__ else f"Executes {tool_name}"
        try:
            params_schema = model.model_json_schema();
            if not params_schema.get('properties'): params_schema = {}
            func_def: FunctionDefinition = {"name": tool_name, "description": description, "parameters": params_schema}
            tool_param: ChatCompletionToolParam = {"type": "function", "function": func_def}; tools_for_llm.append(tool_param)
        except Exception as e: log_error(fn_name, "define_tools", f"Schema error for onboarding tool {tool_name}: {e}", e) # Corrected log identifier
    # It's okay if tools_for_llm is empty if LLM decides to just talk


    # --- Interact with LLM (using same two-step logic as orchestrator) ---
    try:
        log_info(fn_name, "LLM_call_1", f"Invoking Onboarding LLM for {user_id}...") # Corrected log identifier
        response = client.chat.completions.create(
            model="gpt-4o", # Or gpt-3.5-turbo
            messages=messages,
            tools=tools_for_llm if tools_for_llm else None,
            tool_choice="auto" if tools_for_llm else None,
            temperature=0.1,
        )
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        # --- Scenario 1: LLM Responds Directly (Asks question, gives info) ---
        if not tool_calls:
            if response_message.content:
                log_info(fn_name, "LLM_call_1", "Onboarding LLM responded directly.") # Corrected log identifier
                return response_message.content
            else:
                log_warning(fn_name, "LLM_call_1", "Onboarding LLM response had no tool calls and no content.") # Corrected log identifier
                return "Sorry, I got stuck. Can you tell me what we were discussing?"

        # --- Scenario 2: LLM Calls an Onboarding Tool ---
        tool_call: ChatCompletionMessageToolCall = tool_calls[0]
        tool_name = tool_call.function.name
        tool_call_id = tool_call.id

        if tool_name not in onboarding_tools_available:
            log_warning(fn_name, "tool_call_check", f"Onboarding LLM tried unknown/disallowed tool: {tool_name}") # Corrected log identifier
            return f"Sorry, I tried an action ('{tool_name}') that isn't available during setup."

        log_info(fn_name, "tool_call_exec", f"Onboarding LLM requested Tool: {tool_name} with args: {tool_call.function.arguments[:150]}...") # Corrected log identifier
        tool_func = onboarding_tools_available[tool_name]
        param_model = onboarding_tool_models[tool_name]
        tool_result_content = GENERIC_ERROR_MSG

        try:
            # Parse and validate arguments
            tool_args_dict = {}
            tool_args_str = tool_call.function.arguments
            if tool_args_str and tool_args_str.strip() != '{}': tool_args_dict = json.loads(tool_args_str)
            elif not param_model.model_fields: tool_args_dict = {}

            validated_params = param_model(**tool_args_dict)

            # Execute the tool function
            tool_result_dict = tool_func(user_id, validated_params)
            log_info(fn_name, "tool_call_exec", f"Onboarding Tool {tool_name} executed. Result: {tool_result_dict}") # Corrected log identifier
            tool_result_content = json.dumps(tool_result_dict)

        # Handle errors during tool execution
        except json.JSONDecodeError:
             log_error(fn_name, "tool_call_exec", f"Failed parse JSON args for onboarding tool {tool_name}: {tool_args_str}"); # Corrected log identifier
             tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid arguments for {tool_name}."})
        except pydantic.ValidationError as e:
             log_error(fn_name, "tool_call_exec", f"Arg validation failed for onboarding tool {tool_name}. Err: {e.errors()}. Args: {tool_args_str}", e) # Corrected log identifier
             err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e.errors()])
             tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid parameters for {tool_name} - {err_summary}"})
        except Exception as e:
             log_error(fn_name, "tool_call_exec", f"Error executing onboarding tool {tool_name}. Trace:\n{traceback.format_exc()}", e); # Corrected log identifier
             tool_result_content = json.dumps({"success": False, "message": f"Error performing action {tool_name}."})

        # --- Make SECOND LLM call with the tool result ---
        # *** FIXED: Convert response_message object to dict before appending ***
        # Append the assistant's first message (the tool call request) as a dict
        messages.append(response_message.model_dump(exclude_unset=True))
        # Append the tool execution result
        messages.append({
            "tool_call_id": tool_call_id, "role": "tool",
            "name": tool_name, "content": tool_result_content,
        })

        log_info(fn_name, "LLM_call_2", f"Invoking Onboarding LLM again for {user_id} with tool result...") # Corrected log identifier
        second_response = client.chat.completions.create(
            model="gpt-4o", messages=messages, # No tools needed here
        )
        second_response_message = second_response.choices[0].message

        if second_response_message.content:
            log_info(fn_name, "LLM_call_2", "Onboarding LLM generated final response after processing tool result.") # Corrected log identifier
            # Check if the LAST action was setting status to active
            if tool_name == "update_user_preferences":
                 try:
                      # Ensure tool_args_str is valid JSON before parsing
                      update_data = json.loads(tool_args_str) if tool_args_str and tool_args_str.strip() != '{}' else {}
                      # Check if the 'updates' dictionary exists and contains 'status'
                      if isinstance(update_data.get("updates"), dict) and update_data["updates"].get("status") == "active":
                           log_info(fn_name, "status_check", f"Onboarding completed for user {user_id} (status set to active).") # Corrected log identifier
                 except Exception as parse_err:
                      log_warning(fn_name, "status_check", f"Could not parse tool args to check for status update: {parse_err}") # Corrected log identifier

            return second_response_message.content
        else:
            log_warning(fn_name, "LLM_call_2", "Onboarding LLM provided no content after processing tool result.") # Corrected log identifier
            try: fallback_msg = json.loads(tool_result_content).get("message", GENERIC_ERROR_MSG)
            except: fallback_msg = GENERIC_ERROR_MSG
            return fallback_msg

    # --- Outer Exception Handling ---
    except Exception as e:
        tb_str = traceback.format_exc();
        log_error(fn_name, "outer_exception", f"Core error in onboarding logic for {user_id}. Traceback:\n{tb_str}", e) # Corrected log identifier
        return GENERIC_ERROR_MSG
# --- END OF FILE agents/onboarding_agent.py ---