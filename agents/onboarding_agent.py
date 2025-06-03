# --- START OF FILE agents/onboarding_agent.py ---

import json
import os
import traceback
from typing import Dict, List, Any # Retain for type hints
# Removed Optional as per user request, ensure Pydantic models don't require it if not truly optional.

# Instructor/LLM Imports
from services.llm_interface import get_instructor_client
from openai import OpenAI # Base client for types if needed
from openai.types.chat import ChatCompletionMessage, ChatCompletionToolParam
from openai.types.chat.chat_completion_message_tool_call import Function as ToolFunctionCall # Renamed
from openai.types.shared_params import FunctionDefinition

# Tool Imports (Limited Set for Onboarding)
from .tool_definitions import (
    AVAILABLE_TOOLS,
    TOOL_PARAM_MODELS,
    UpdateUserPreferencesParams,
    InitiateCalendarConnectionParams,
    SendOnboardingCompletionMessageParams
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

# --- Function to Load Onboarding Prompts ---
_ONBOARDING_PROMPT_CACHE: Dict[str, str] = {}
_ONBOARDING_HUMAN_PROMPT_CACHE: Dict[str, str] = {}

def load_onboarding_prompts() -> tuple[str, str]:
    """Loads the onboarding system and human prompts from the YAML file, with caching."""
    prompts_path = os.path.join("config", "prompts.yaml")
    system_cache_key = prompts_path + "_onboarding_system"
    human_cache_key = prompts_path + "_onboarding_human"

    # Check cache first
    cached_system_prompt = _ONBOARDING_PROMPT_CACHE.get(system_cache_key)
    cached_human_prompt = _ONBOARDING_HUMAN_PROMPT_CACHE.get(human_cache_key)

    if cached_system_prompt is not None and cached_human_prompt is not None:
        return cached_system_prompt, cached_human_prompt

    system_prompt_text: str = "" # Initialize as empty string
    human_prompt_text: str = ""  # Initialize as empty string
    prompts_loaded_successfully = False
    try:
        if not os.path.exists(prompts_path): raise FileNotFoundError(f"{prompts_path} not found.")
        with open(prompts_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip(): raise ValueError("Prompts file is empty.")
            f.seek(0); all_prompts = yaml.safe_load(f)
            if not all_prompts: raise ValueError("YAML parsing resulted in empty prompts.")

            system_prompt_text_temp = all_prompts.get("onboarding_agent_system_prompt")
            if not system_prompt_text_temp or not system_prompt_text_temp.strip():
                log_error("onboarding_agent", "load_onboarding_prompts", "Key 'onboarding_agent_system_prompt' NOT FOUND or EMPTY.")
            else:
                system_prompt_text = system_prompt_text_temp # Assign if valid
                log_info("onboarding_agent", "load_onboarding_prompts", "Onboarding system prompt loaded successfully.")

            human_prompt_text_temp = all_prompts.get("onboarding_agent_human_prompt")
            if not human_prompt_text_temp or not human_prompt_text_temp.strip():
                log_error("onboarding_agent", "load_onboarding_prompts", "Key 'onboarding_agent_human_prompt' NOT FOUND or EMPTY.")
            else:
                human_prompt_text = human_prompt_text_temp # Assign if valid
                log_info("onboarding_agent", "load_onboarding_prompts", "Onboarding human prompt loaded successfully.")
            
            if system_prompt_text and human_prompt_text:
                prompts_loaded_successfully = True

    except Exception as e:
        log_error("onboarding_agent", "load_onboarding_prompts", f"CRITICAL: Failed load/parse onboarding prompts: {e}", e)
        # system_prompt_text and human_prompt_text remain empty string on error
    
    if prompts_loaded_successfully:
        _ONBOARDING_PROMPT_CACHE[system_cache_key] = system_prompt_text
        _ONBOARDING_HUMAN_PROMPT_CACHE[human_cache_key] = human_prompt_text
    
    # Return even if loading failed, the calling function will handle empty prompts
    return system_prompt_text, human_prompt_text


# --- Main Onboarding Handler Function ---
def handle_onboarding_request(
    user_id: str,
    message: str,
    history: List[Dict],
    preferences: Dict # Current preferences passed in
) -> str:
    fn_name = "handle_onboarding_request"
    log_info("onboarding_agent", fn_name, f"Handling onboarding request for {user_id}: '{message[:50]}...'")

    onboarding_system_prompt, onboarding_human_prompt_template = load_onboarding_prompts()
    if not onboarding_system_prompt or not onboarding_human_prompt_template:
         log_error("onboarding_agent", fn_name, "Onboarding system or human prompt could not be loaded.")
         return GENERIC_ERROR_MSG

    client: OpenAI = get_instructor_client() # Type hint for clarity
    if not client:
        log_error("onboarding_agent", fn_name, "LLM client unavailable.")
        return GENERIC_ERROR_MSG

    # --- Prepare Context for Prompt ---
    try:
        history_limit = 20 
        limited_history = history[-(history_limit*2):] 
        # Simplified history string for the prompt placeholder
        history_str_parts = []
        for m_entry in limited_history:
            role = m_entry.get("role")
            content = m_entry.get("content")
            tool_calls_json_str = m_entry.get("tool_calls_json_str")
            tool_name = m_entry.get("name") # for tool role

            if role == "user":
                history_str_parts.append(f"User: {content}")
            elif role == "assistant":
                if content:
                    history_str_parts.append(f"Assistant: {content}")
                if tool_calls_json_str:
                    try:
                        tool_calls_list = json.loads(tool_calls_json_str)
                        for tc in tool_calls_list:
                            history_str_parts.append(f"Assistant (tool_call): Requesting {tc.get('function',{}).get('name')}({tc.get('function',{}).get('arguments')})")
                    except:
                         history_str_parts.append(f"Assistant (tool_call): {tool_calls_json_str}") # raw if parse fails
            elif role == "tool":
                history_str_parts.append(f"Tool ({tool_name}): {content}") # Content is JSON string of result
        history_str = "\n".join(history_str_parts)
        prefs_str = json.dumps(preferences, indent=2, default=str)
    except Exception as e:
        log_error("onboarding_agent", fn_name, f"Error preparing context strings for onboarding: {e}", e)
        return GENERIC_ERROR_MSG

    # --- Construct Initial Messages for LLM ---
    formatted_human_prompt = onboarding_human_prompt_template.format(
        current_preferences_json=prefs_str,
        conversation_history=history_str, # Use the new history_str
        message=message 
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": onboarding_system_prompt},
        {"role": "user", "content": formatted_human_prompt}
    ]

    # --- Define Tools AVAILABLE for Onboarding ---
    onboarding_tools_for_llm_map = {
        "update_user_preferences": {
            "function": AVAILABLE_TOOLS.get("update_user_preferences"),
            "model": UpdateUserPreferencesParams
        },
        "initiate_calendar_connection": {
            "function": AVAILABLE_TOOLS.get("initiate_calendar_connection"),
            "model": InitiateCalendarConnectionParams
        },
        # --- ADD THE NEW TOOL HERE ---
        "send_onboarding_completion_message": {
            "function": AVAILABLE_TOOLS.get("send_onboarding_completion_message"),
            "model": SendOnboardingCompletionMessageParams # From tool_definitions
        },
        # ---------------------------
    }
    # Filter out any tools that might have failed to load (function or model is None)
    onboarding_tools_for_llm_map = {
        name: data for name, data in onboarding_tools_for_llm_map.items() if data["function"] and data["model"]
    }

    tools_for_llm: List[ChatCompletionToolParam] = []
    if onboarding_tools_for_llm_map: 
        for tool_name, tool_data in onboarding_tools_for_llm_map.items():
            func = tool_data["function"]
            model = tool_data["model"]
            description = func.__doc__.strip() if func and func.__doc__ else f"Executes {tool_name}"
            try:
                params_schema = model.model_json_schema()
                # Ensure 'type' is 'object' for top-level schema if Pydantic doesn't add it & it has properties
                if "properties" in params_schema and "type" not in params_schema :
                     params_schema["type"] = "object"
                elif not params_schema.get('properties') and not model.model_fields: # No params for this tool
                     params_schema = {} # Empty schema for no-param tools

                func_def: FunctionDefinition = {"name": tool_name, "description": description, "parameters": params_schema}
                tool_param: ChatCompletionToolParam = {"type": "function", "function": func_def}
                tools_for_llm.append(tool_param)
            except Exception as e:
                log_error("onboarding_agent", fn_name, f"Schema error for onboarding tool {tool_name}: {e}", e)
    
    final_tools_for_llm = tools_for_llm if tools_for_llm else None

    # --- Interaction Loop (Two-Step LLM Call) ---
    try:
        log_info("onboarding_agent", fn_name, f"Invoking Onboarding LLM for {user_id} (Initial Turn).\n {messages}  \n .{final_tools_for_llm}.")
        response = client.chat.completions.create(
            model="gpt-4o", 
            messages=messages, # type: ignore
            tools=final_tools_for_llm,
            tool_choice="auto" if final_tools_for_llm else None,
            temperature=0.1, 
        )
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        if not tool_calls:
            if response_message.content:
                log_info("onboarding_agent", fn_name, "Onboarding LLM responded directly.")
                return response_message.content
            else:
                log_warning("onboarding_agent", fn_name, "Onboarding LLM response had no tool calls and no content.")
                return "I'm sorry, I'm having a little trouble at the moment. Could you try rephrasing?"

        log_info("onboarding_agent", fn_name, f"Onboarding LLM requested {len(tool_calls)} tool call(s).")
        # Prepare the assistant's message (including tool calls) for the history
        assistant_message_for_history = response_message.model_dump(exclude_unset=True)
        if "content" not in assistant_message_for_history or assistant_message_for_history["content"] is None:
             assistant_message_for_history["content"] = "" # Ensure content key exists, even if empty

        messages.append(assistant_message_for_history) # type: ignore
        tool_results_messages = []

        for tool_call in tool_calls: 
            tool_name = tool_call.function.name
            tool_call_id = tool_call.id
            tool_args_str = tool_call.function.arguments
            tool_result_content = GENERIC_ERROR_MSG 

            log_info("onboarding_agent", fn_name, f"Processing Tool Call ID: {tool_call_id}, Name: {tool_name}, Args: {tool_args_str[:150]}...")

            if tool_name not in onboarding_tools_for_llm_map:
                log_warning("onboarding_agent", fn_name, f"Onboarding LLM tried unknown/disallowed tool: {tool_name}.")
                tool_result_content = json.dumps({"success": False, "message": f"Error: Action '{tool_name}' not allowed during setup."})
            else:
                tool_func = onboarding_tools_for_llm_map[tool_name]["function"]
                param_model = onboarding_tools_for_llm_map[tool_name]["model"]
                try:
                    tool_args_dict = {}
                    # Check if arguments are expected; if not (empty schema), don't parse
                    if param_model.model_fields: # Check if Pydantic model expects fields
                        if tool_args_str and tool_args_str.strip() != '{}':
                            tool_args_dict = json.loads(tool_args_str)
                        # If args string is empty/{} but model expects fields, it might be an error or defaults are used
                    # Else (no fields expected by model), tool_args_dict remains {}

                    validated_params = param_model(**tool_args_dict)
                    tool_result_dict = tool_func(user_id, validated_params) 
                    log_info("onboarding_agent", fn_name, f"Onboarding Tool {tool_name} (ID: {tool_call_id}) executed. Result keys: {list(tool_result_dict.keys()) if isinstance(tool_result_dict, dict) else 'Non-dict result'}")
                    tool_result_content = json.dumps(tool_result_dict)

                except json.JSONDecodeError:
                    log_error("onboarding_agent", fn_name, f"Failed parse JSON args for tool {tool_name} (ID: {tool_call_id}): {tool_args_str}");
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid arguments format for {tool_name}."})
                except pydantic.ValidationError as e:
                    log_error("onboarding_agent", fn_name, f"Arg validation failed for tool {tool_name} (ID: {tool_call_id}). Err: {e.errors()}. Args: {tool_args_str}", e)
                    err_summary = "; ".join([f"{err['loc'][0] if err.get('loc') else 'param'}: {err['msg']}" for err in e.errors()])
                    tool_result_content = json.dumps({"success": False, "message": f"Error: Invalid parameters for {tool_name} - {err_summary}"})
                except Exception as e:
                    log_error("onboarding_agent", fn_name, f"Error executing tool {tool_name} (ID: {tool_call_id}). Trace:\n{traceback.format_exc()}", e);
                    tool_result_content = json.dumps({"success": False, "message": f"Error performing action {tool_name}."})

            tool_results_messages.append({
                "tool_call_id": tool_call_id, "role": "tool",
                "name": tool_name, "content": tool_result_content,
            })

        messages.extend(tool_results_messages) # type: ignore

        log_info("onboarding_agent", fn_name, f"Invoking Onboarding LLM again for {user_id} with {len(tool_results_messages)} tool result(s)...")
        second_response = client.chat.completions.create(
            model="gpt-4o", 
            messages=messages, # type: ignore
        )
        second_response_message = second_response.choices[0].message

        if second_response_message.content:
            log_info("onboarding_agent", fn_name, "Onboarding LLM generated final response after tool result(s).")
            return second_response_message.content
        else:
            log_warning("onboarding_agent", fn_name, "Onboarding LLM provided no content after tool result(s).")
            try: # Try to return the message from the tool if LLM fails to generate text (e.g. from send_onboarding_completion_message)
                last_tool_result_parsed = json.loads(tool_results_messages[-1]['content'])
                if "message_to_send" in last_tool_result_parsed:
                    return last_tool_result_parsed["message_to_send"]
                fallback_msg = last_tool_result_parsed.get("message", GENERIC_ERROR_MSG)
            except: fallback_msg = GENERIC_ERROR_MSG
            return fallback_msg

    except Exception as e:
        tb_str = traceback.format_exc()
        log_error("onboarding_agent", fn_name, f"Core error in onboarding LLM logic for {user_id}. Traceback:\n{tb_str}", e)
        return GENERIC_ERROR_MSG

# --- END OF FILE agents/onboarding_agent.py ---