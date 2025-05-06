# --- START OF FULL agents/onboarding_agent.py ---
import json
import os
import traceback
from typing import Dict, List, Optional, Any # Retain Optional for type hints as Pydantic/OpenAI models use it
from datetime import datetime
import pytz

# Instructor/LLM Imports
from services.llm_interface import get_instructor_client
from openai import OpenAI # Base client for types if needed
from openai.types.chat import ChatCompletionMessage, ChatCompletionToolParam
from openai.types.chat.chat_completion_message_tool_call import Function as ToolFunctionCall # Renamed to avoid conflict
from openai.types.shared_params import FunctionDefinition

# Tool Imports (Limited Set for Onboarding)
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

# --- Load Standard Messages (Mainly for GENERIC_ERROR_MSG) ---
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
# SETUP_START_MSG no longer used directly by Python, LLM will handle greetings

# --- Function to Load Onboarding Prompt ---
_ONBOARDING_PROMPT_CACHE: Dict[str, Optional[str]] = {} # Specify cache type
_ONBOARDING_HUMAN_PROMPT_CACHE: Dict[str, Optional[str]] = {}

def load_onboarding_prompts() -> tuple[Optional[str], Optional[str]]:
    """Loads the onboarding system and human prompts from the YAML file, with caching."""
    prompts_path = os.path.join("config", "prompts.yaml")
    system_cache_key = prompts_path + "_onboarding_system"
    human_cache_key = prompts_path + "_onboarding_human"

    cached_system_prompt = _ONBOARDING_PROMPT_CACHE.get(system_cache_key)
    cached_human_prompt = _ONBOARDING_HUMAN_PROMPT_CACHE.get(human_cache_key)

    if cached_system_prompt is not None and cached_human_prompt is not None:
        return cached_system_prompt, cached_human_prompt

    system_prompt_text: Optional[str] = None
    human_prompt_text: Optional[str] = None
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip(): raise ValueError("Prompts file is empty.")
            f.seek(0); all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")

            system_prompt_text = all_prompts.get("onboarding_agent_system_prompt")
            if not system_prompt_text or not system_prompt_text.strip():
                log_error("onboarding_agent", "load_onboarding_prompts", "Key 'onboarding_agent_system_prompt' NOT FOUND or EMPTY.")
                system_prompt_text = None
            else:
                log_info("onboarding_agent", "load_onboarding_prompts", "Onboarding system prompt loaded successfully.")

            human_prompt_text = all_prompts.get("onboarding_agent_human_prompt")
            if not human_prompt_text or not human_prompt_text.strip():
                log_error("onboarding_agent", "load_onboarding_prompts", "Key 'onboarding_agent_human_prompt' NOT FOUND or EMPTY.")
                human_prompt_text = None
            else:
                log_info("onboarding_agent", "load_onboarding_prompts", "Onboarding human prompt loaded successfully.")

    except Exception as e:
        log_error("onboarding_agent", "load_onboarding_prompts", f"CRITICAL: Failed load/parse onboarding prompts: {e}", e)
        system_prompt_text = None
        human_prompt_text = None

    _ONBOARDING_PROMPT_CACHE[system_cache_key] = system_prompt_text
    _ONBOARDING_HUMAN_PROMPT_CACHE[human_cache_key] = human_prompt_text
    return system_prompt_text, human_prompt_text

# --- Main Onboarding Handler Function ---
def handle_onboarding_request(
    user_id: str,
    message: str,
    history: List[Dict],
    preferences: Dict # Current preferences passed in
) -> str:
    """Handles the user's request during the onboarding phase using pure LLM flow."""
    fn_name = "handle_onboarding_request"
    log_info("onboarding_agent", fn_name, f"Handling onboarding request for {user_id}: '{message[:50]}...'")

    onboarding_system_prompt, onboarding_human_prompt = load_onboarding_prompts()
    if not onboarding_system_prompt or not onboarding_human_prompt:
         log_error("onboarding_agent", fn_name, "Onboarding system or human prompt could not be loaded.")
         return GENERIC_ERROR_MSG

    client: Optional[OpenAI] = get_instructor_client()
    if not client:
        log_error("onboarding_agent", fn_name, "LLM client unavailable.")
        return GENERIC_ERROR_MSG

    # --- Prepare Context for Prompt ---
    try:
        history_limit = 20 # Keep more history for onboarding to better detect language
        limited_history = history[-(history_limit*2):] # Each turn is 2 entries (user, assistant/tool)
        history_str = "\n".join([f"{m['sender'].capitalize()}: {m['content']}" for m in limited_history])
        prefs_str = json.dumps(preferences, indent=2, default=str)
    except Exception as e:
        log_error("onboarding_agent", fn_name, f"Error preparing context strings for onboarding: {e}", e)
        return GENERIC_ERROR_MSG

    # --- Construct Initial Messages for LLM ---
    # Human prompt is now dynamic with placeholders
    formatted_human_prompt = onboarding_human_prompt.format(
        current_preferences_json=prefs_str,
        conversation_history=history_str,
        message=message # The latest user message
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": onboarding_system_prompt},
        # System messages with context are now part of the human prompt's preamble for this agent
        {"role": "user", "content": formatted_human_prompt} # The user message is part of the formatted human prompt
    ]
    # Remove the logic that inserted SETUP_START_MSG; LLM will handle greeting.

    # --- Define Tools AVAILABLE for Onboarding ---
    # (Tool definition logic is similar to orchestrator_agent)
    onboarding_tools_for_llm_map = {
        "update_user_preferences": {
            "function": AVAILABLE_TOOLS.get("update_user_preferences"),
            "model": UpdateUserPreferencesParams
        },
        "initiate_calendar_connection": {
            "function": AVAILABLE_TOOLS.get("initiate_calendar_connection"),
            "model": InitiateCalendarConnectionParams
        },
    }
    # Filter out tools that might have failed to load
    onboarding_tools_for_llm_map = {
        name: data for name, data in onboarding_tools_for_llm_map.items() if data["function"] and data["model"]
    }

    tools_for_llm: List[ChatCompletionToolParam] = []
    for tool_name, tool_data in onboarding_tools_for_llm_map.items():
        func = tool_data["function"]
        model = tool_data["model"]
        description = func.__doc__.strip() if func and func.__doc__ else f"Executes {tool_name}"
        try:
            params_schema = model.model_json_schema()
            # Ensure 'properties' is not empty for tools that expect params
            if not params_schema.get('properties') and model.model_fields:
                # If model has fields but schema has no properties, it might be an issue.
                # For empty models like InitiateCalendarConnectionParams, schema might be {}
                pass # Allow empty schema for tools with no params
            elif not params_schema.get('properties'):
                 params_schema = {} # For tools with truly no parameters

            func_def: FunctionDefinition = {"name": tool_name, "description": description, "parameters": params_schema}
            tool_param: ChatCompletionToolParam = {"type": "function", "function": func_def}
            tools_for_llm.append(tool_param)
        except Exception as e:
            log_error("onboarding_agent", fn_name, f"Schema error for onboarding tool {tool_name}: {e}", e)

    # --- Interaction Loop (Two-Step LLM Call) ---
    try:
        log_info("onboarding_agent", fn_name, f"Invoking Onboarding LLM for {user_id} (Initial Turn)...")
        response = client.chat.completions.create(
            model="gpt-4o", # Or a suitable model like gpt-3.5-turbo
            messages=messages, # type: ignore
            tools=tools_for_llm if tools_for_llm else None, # Pass None if no tools
            tool_choice="auto" if tools_for_llm else None,
            temperature=0.2, # Slightly higher temp for more natural onboarding
        )
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        # --- Scenario 1: LLM Responds Directly (Asks question, gives info) ---
        if not tool_calls:
            if response_message.content:
                log_info("onboarding_agent", fn_name, "Onboarding LLM responded directly.")
                return response_message.content
            else:
                log_warning("onboarding_agent", fn_name, "Onboarding LLM response had no tool calls and no content.")
                return "I'm sorry, I'm having a little trouble at the moment. Could you try rephrasing?"

        # --- Scenario 2: LLM Calls One or More Onboarding Tools ---
        # Onboarding agent typically calls one tool at a time.
        log_info("onboarding_agent", fn_name, f"Onboarding LLM requested {len(tool_calls)} tool call(s).")
        messages.append(response_message.model_dump(exclude_unset=True)) # Add assistant's tool call request
        tool_results_messages = []

        for tool_call in tool_calls: # Loop although expecting one for onboarding
            tool_name = tool_call.function.name
            tool_call_id = tool_call.id
            tool_args_str = tool_call.function.arguments
            tool_result_content = GENERIC_ERROR_MSG # Default in case of error

            log_info("onboarding_agent", fn_name, f"Processing Tool Call ID: {tool_call_id}, Name: {tool_name}, Args: {tool_args_str[:150]}...")

            if tool_name not in onboarding_tools_for_llm_map:
                log_warning("onboarding_agent", fn_name, f"Onboarding LLM tried unknown/disallowed tool: {tool_name}. Sending error result back.")
                tool_result_content = json.dumps({"success": False, "message": f"Error: Unknown action '{tool_name}' requested during setup."})
            else:
                tool_func = onboarding_tools_for_llm_map[tool_name]["function"]
                param_model = onboarding_tools_for_llm_map[tool_name]["model"]
                try:
                    tool_args_dict = {}
                    if tool_args_str and tool_args_str.strip() != '{}': tool_args_dict = json.loads(tool_args_str)
                    elif not param_model.model_fields : tool_args_dict = {} # Handle no-arg tools

                    validated_params = param_model(**tool_args_dict)
                    tool_result_dict = tool_func(user_id, validated_params) # Call the tool
                    log_info("onboarding_agent", fn_name, f"Onboarding Tool {tool_name} (ID: {tool_call_id}) executed. Result: {tool_result_dict}")
                    tool_result_content = json.dumps(tool_result_dict)

                except json.JSONDecodeError:
                    log_error("onboarding_agent", fn_name, f"Failed parse JSON args for onboarding tool {tool_name} (ID: {tool_call_id}): {tool_args_str}");
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid arguments format for {tool_name}."})
                except pydantic.ValidationError as e:
                    log_error("onboarding_agent", fn_name, f"Arg validation failed for onboarding tool {tool_name} (ID: {tool_call_id}). Err: {e.errors()}. Args: {tool_args_str}", e)
                    err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e.errors()])
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid parameters for {tool_name} - {err_summary}"})
                except Exception as e:
                    log_error("onboarding_agent", fn_name, f"Error executing onboarding tool {tool_name} (ID: {tool_call_id}). Trace:\n{traceback.format_exc()}", e);
                    tool_result_content = json.dumps({"success": False, "message": f"Error performing action {tool_name}."})

            tool_results_messages.append({
                "tool_call_id": tool_call_id, "role": "tool",
                "name": tool_name, "content": tool_result_content,
            })

        messages.extend(tool_results_messages) # Add all tool results

        # --- Make SECOND LLM call with ALL tool results ---
        log_info("onboarding_agent", fn_name, f"Invoking Onboarding LLM again for {user_id} with {len(tool_results_messages)} tool result(s)...")
        second_response = client.chat.completions.create(
            model="gpt-4o", # Or gpt-3.5-turbo
            messages=messages, # type: ignore
            # No tools needed here, LLM should just generate response based on results
        )
        second_response_message = second_response.choices[0].message

        if second_response_message.content:
            log_info("onboarding_agent", fn_name, "Onboarding LLM generated final response after processing tool result(s).")
            # Check if the LAST action was setting status to active (optional, LLM should handle the final message)
            if tool_calls and tool_calls[-1].function.name == "update_user_preferences":
                 try:
                      last_tool_args_str = tool_calls[-1].function.arguments
                      update_data = json.loads(last_tool_args_str) if last_tool_args_str and last_tool_args_str.strip() != '{}' else {}
                      if isinstance(update_data.get("updates"), dict) and update_data["updates"].get("status") == "active":
                           log_info("onboarding_agent", fn_name, f"Onboarding likely completed for user {user_id} (status set to active).")
                 except Exception as parse_err:
                      log_warning("onboarding_agent", fn_name, f"Could not parse last tool args to check for status update: {parse_err}")
            return second_response_message.content
        else:
            log_warning("onboarding_agent", fn_name, "Onboarding LLM provided no content after processing tool result(s).")
            # Try to construct a fallback from the first tool's message if possible
            try: fallback_msg = json.loads(tool_results_messages[0]['content']).get("message", GENERIC_ERROR_MSG)
            except: fallback_msg = GENERIC_ERROR_MSG
            return fallback_msg

    # --- Outer Exception Handling ---
    except Exception as e:
        tb_str = traceback.format_exc()
        log_error("onboarding_agent", fn_name, f"Core error in onboarding LLM logic for {user_id}. Traceback:\n{tb_str}", e)
        return GENERIC_ERROR_MSG
# --- END OF FULL agents/onboarding_agent.py ---