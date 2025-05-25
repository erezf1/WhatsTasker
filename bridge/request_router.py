# --- START OF FULL bridge/request_router.py ---

import re
import os
import yaml
import traceback
import json
from typing import Dict, List, Any, Literal

from tools.logger import log_info, log_error, log_warning

# --- Database Import ---
ACTIVITY_DB_IMPORTED = False
_activity_db_log_func = None # For log_message_db
try:
    from tools.activity_db import log_message_db as activity_db_log_message_db_func
    # No need to import the whole module if only one function is used here for logging
    _activity_db_log_func = activity_db_log_message_db_func
    ACTIVITY_DB_IMPORTED = True
except ImportError:
    log_error("request_router", "import", "activity_db.log_message_db not found. Rich DB logging for messages disabled.")
# --- End Database Import ---

from services.agent_state_manager import get_agent_state, add_message_to_user_history
from users.user_manager import get_agent
from services.config_manager import set_user_status # For setting status after welcome

# Agent handlers
ORCHESTRATOR_IMPORTED = False
_route_to_orchestrator_func = None
try:
    from agents.orchestrator_agent import handle_user_request
    _route_to_orchestrator_func = handle_user_request
    ORCHESTRATOR_IMPORTED = True
except ImportError as e:
    log_error("request_router", "import", f"OrchestratorAgent import failed: {e}. Orchestrator routing disabled.", e)

ONBOARDING_AGENT_IMPORTED = False
_handle_onboarding_request_func = None
try:
    from agents.onboarding_agent import handle_onboarding_request
    _handle_onboarding_request_func = handle_onboarding_request
    ONBOARDING_AGENT_IMPORTED = True
except ImportError as e:
    log_error("request_router", "import", f"OnboardingAgent import failed: {e}. Onboarding routing disabled.", e)

CONTEXT_SERVICE_IMPORTED = False
_get_context_snapshot_func = None
try:
    from services.task_query_service import get_context_snapshot
    _get_context_snapshot_func = get_context_snapshot
    CONTEXT_SERVICE_IMPORTED = True
except ImportError as e:
    log_error("request_router", "import", f"TaskQueryService (get_context_snapshot) import failed: {e}. Context for agent will be limited.", e)

CHEATS_IMPORTED = False
_handle_cheat_command_func = None
try:
    from services.cheats import handle_cheat_command
    _handle_cheat_command_func = handle_cheat_command
    CHEATS_IMPORTED = True
except ImportError:
    log_info("request_router", "import", "Cheats module not found. Cheat commands disabled.")


_messages_router = {}
try:
    messages_path_router = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path_router):
        with open(messages_path_router, 'r', encoding="utf-8") as f_router:
            _messages_router = yaml.safe_load(f_router) or {}
    else: _messages_router = {}
except Exception as e_msg_load:
    _messages_router = {}; log_error("request_router", "load_messages", f"Failed to load messages.yaml: {e_msg_load}", e_msg_load)

GENERIC_ERROR_MSG_ROUTER = _messages_router.get("generic_error_message", "Sorry, an unexpected error occurred.")
WELCOME_MSG_ROUTER = _messages_router.get("welcome_confirmation_message", "Hello! Welcome to WhatsTasker.")
USER_REGISTERED_MSG_ROUTER = _messages_router.get("user_registered_already_message", "Welcome back!")

current_bridge_router: Any = None
current_bridge_type_router: Literal["cli", "whatsapp", "twilio"] | None = None

def normalize_user_id(user_id_from_bridge: str, bridge_type: Literal["cli", "whatsapp", "twilio"] | None) -> str:
    if not user_id_from_bridge: return ""
    if bridge_type == "whatsapp":
        if '@c.us' in user_id_from_bridge: user_id_from_bridge = user_id_from_bridge.split('@c.us')[0]
        return re.sub(r'\D', '', user_id_from_bridge)
    elif bridge_type == "twilio":
        if user_id_from_bridge.startswith("whatsapp:+"): return re.sub(r'\D', '', user_id_from_bridge.replace("whatsapp:+", ""))
        else: log_warning("request_router", "normalize_user_id", f"Unexpected Twilio user_id: {user_id_from_bridge}. Normalizing as plain number."); return re.sub(r'\D', '', user_id_from_bridge)
    elif bridge_type == "cli": return re.sub(r'\D', '', user_id_from_bridge)
    else:
        log_warning("request_router", "normalize_user_id", f"Unknown bridge_type '{bridge_type}'. Generic normalization for {user_id_from_bridge}.")
        temp_id = user_id_from_bridge
        if '@' in temp_id: temp_id = temp_id.split('@')[0]
        if temp_id.startswith("whatsapp:+"): temp_id = temp_id.replace("whatsapp:+", "")
        return re.sub(r'\D', '', temp_id)

def set_bridge(bridge_instance: Any):
    global current_bridge_router, current_bridge_type_router
    if current_bridge_router is None:
        current_bridge_router = bridge_instance
        class_name = type(bridge_instance).__name__
        if "CLIB" in class_name: current_bridge_type_router = "cli"
        elif "WhatsAppB" in class_name: current_bridge_type_router = "whatsapp"
        elif "TwilioB" in class_name: current_bridge_type_router = "twilio"
        else: current_bridge_type_router = None; log_warning("request_router", "set_bridge", f"Could not infer bridge type from: {class_name}")
        log_info("request_router", "set_bridge", f"Bridge set to: {class_name}, inferred type: {current_bridge_type_router}")

def send_message(user_id: str, message_body: str): # user_id is normalized
    fn_name = "send_message_router"
    if not user_id or not message_body:
        log_warning("request_router", fn_name, f"Empty message or invalid user_id ({user_id}). Not sending.")
        return

    # Add agent's final text response to in-memory history
    # The OrchestratorAgent itself handles logging its detailed actions (tool calls, results)
    # to the DB before returning this final text.
    add_message_to_user_history(
        user_id=user_id,
        role="assistant", # This is an assistant's textual reply
        message_type="agent_text_response",
        content=message_body, # Stored as string
        # No tool_calls_json, tool_name, or associated_tool_call_id for a simple text response
    )
    
    # Log this outgoing text message to DB
    if ACTIVITY_DB_IMPORTED and _activity_db_log_func:
        try:
            _activity_db_log_func(
                user_id=user_id,
                role="assistant", # Consistent with in-memory history
                message_type="agent_text_response",
                content_text=message_body
                # Other fields like tool_calls_json are None for a simple text out
            )
        except Exception as e_db_log_send:
            log_error("request_router", fn_name, 
                      f"Failed to log outgoing text message to DB for user {user_id}", e_db_log_send, user_id=user_id)
    
    if current_bridge_router:
        try:
            current_bridge_router.send_message(user_id, message_body)
        except Exception as e_bridge_send:
            log_error("request_router", fn_name, f"Bridge error sending message to {user_id}", e_bridge_send, user_id=user_id)
    else:
        log_error("request_router", fn_name, "No bridge configured. Cannot send message.")


def handle_incoming_message(user_id_from_bridge: str, message_text: str) -> str:
    fn_name = "handle_incoming_message"
    
    norm_user_id = normalize_user_id(user_id_from_bridge, current_bridge_type_router)
    if not norm_user_id:
        log_error("request_router", fn_name, f"Invalid User ID after normalization: {user_id_from_bridge} (Bridge: {current_bridge_type_router})", user_id=user_id_from_bridge)
        return GENERIC_ERROR_MSG_ROUTER

    # Log incoming user message to DB
    if ACTIVITY_DB_IMPORTED and _activity_db_log_func:
        try:
            _activity_db_log_func(
                user_id=norm_user_id,
                role="user",
                message_type="user_text",
                content_text=message_text,
                raw_user_id=user_id_from_bridge
                # Consider adding user_message_timestamp_iso if bridge provides it
            )
        except Exception as e_db_log_in:
            log_error("request_router", fn_name, f"Failed to log incoming message to DB for user {norm_user_id}", e_db_log_in, user_id=norm_user_id)

    agent_state = get_agent(norm_user_id) # Creates if not exists, loads from registry
    if not agent_state:
        log_error("request_router", fn_name, f"CRITICAL: Failed to get/create agent state for {norm_user_id}.", user_id=norm_user_id)
        send_message(norm_user_id, GENERIC_ERROR_MSG_ROUTER) # send_message will log its own DB entry
        return GENERIC_ERROR_MSG_ROUTER

    current_status = agent_state.get("preferences", {}).get("status")
    final_response_message_str = GENERIC_ERROR_MSG_ROUTER

    if current_status == "new":
        send_message(norm_user_id, WELCOME_MSG_ROUTER) # Logs an 'OUT' message
        if not set_user_status(norm_user_id, 'onboarding'):
             log_error("request_router", fn_name, f"Failed to update status to 'onboarding' for {norm_user_id}", user_id=norm_user_id)
        return WELCOME_MSG_ROUTER # ACK for bridge

    message_stripped = message_text.strip()
    if message_stripped.startswith('/') and CHEATS_IMPORTED and _handle_cheat_command_func:
        parts = message_stripped.split(); command = parts[0].lower(); args = parts[1:]
        # Add user's cheat command to in-memory history first
        add_message_to_user_history(norm_user_id, role="user", message_type="user_cheat_command", content=message_text)
        try:
            command_response = _handle_cheat_command_func(norm_user_id, command, args)
            send_message(norm_user_id, command_response) # Logs an 'OUT' message
            return command_response # ACK for bridge
        except Exception as e_cheat:
            log_error("request_router", fn_name, f"Error executing cheat '{command}' for {norm_user_id}", e_cheat, user_id=norm_user_id)
            err_msg_cheat = "Error processing cheat command."
            send_message(norm_user_id, err_msg_cheat)
            return err_msg_cheat
    
    # Add regular user message to in-memory history (DB log already done above)
    add_message_to_user_history(norm_user_id, role="user", message_type="user_text", content=message_text)

    if current_status == "onboarding":
        if ONBOARDING_AGENT_IMPORTED and _handle_onboarding_request_func:
            try:
                 history = agent_state.get("conversation_history", [])
                 preferences = agent_state.get("preferences", {})
                 final_response_message_str = _handle_onboarding_request_func(norm_user_id, message_text, history, preferences)
            except Exception as e_onboard:
                 log_error("request_router", fn_name, f"Error from OnboardingAgent for {norm_user_id}", e_onboard, user_id=norm_user_id)
                 final_response_message_str = GENERIC_ERROR_MSG_ROUTER
        else: log_error("request_router", fn_name, f"Onboarding for {norm_user_id}, but OnboardingAgent missing.", user_id=norm_user_id)

    elif current_status == "active":
        if ORCHESTRATOR_IMPORTED and _route_to_orchestrator_func and \
           CONTEXT_SERVICE_IMPORTED and _get_context_snapshot_func:
            try:
                history = agent_state.get("conversation_history", [])
                preferences = agent_state.get("preferences", {})
                wt_items_ctx, gcal_events_ctx = _get_context_snapshot_func(norm_user_id)
                final_response_message_str = _route_to_orchestrator_func(
                    user_id=norm_user_id, message=message_text, history=history,
                    preferences=preferences, task_context=wt_items_ctx, calendar_context=gcal_events_ctx
                )
            except Exception as e_orch:
                 log_error("request_router", fn_name, f"Error from OrchestratorAgent for {norm_user_id}", e_orch, user_id=norm_user_id)
                 final_response_message_str = GENERIC_ERROR_MSG_ROUTER
        else: log_error("request_router", fn_name, f"Active user {norm_user_id}, but core components for orchestrator missing.", user_id=norm_user_id)
    
    else: # Unknown status
        log_error("request_router", fn_name, f"User {norm_user_id} has unknown status: '{current_status}'.", user_id=norm_user_id)
        msg_unknown_status = "There seems to be an issue with your account setup. Please contact support."
        send_message(norm_user_id, msg_unknown_status)
        return msg_unknown_status # ACK for bridge

    # Send the final response from agent (onboarding or orchestrator)
    if final_response_message_str:
         send_message(norm_user_id, final_response_message_str) # This logs the 'OUT' message
    else:
         log_warning("request_router", fn_name, f"Agent returned empty/None response for {norm_user_id}. Sending generic error.", user_id=norm_user_id)
         send_message(norm_user_id, GENERIC_ERROR_MSG_ROUTER)
         final_response_message_str = GENERIC_ERROR_MSG_ROUTER # Ensure ACK has content
    
    return final_response_message_str # Return text for bridge ACK


def handle_internal_system_event(event_data: Dict): # event_data from scheduler_service
    fn_name = "handle_internal_system_event"
    user_id = event_data.get("user_id") # Normalized ID expected
    routine_type = event_data.get("routine_type")
    payload_for_llm = event_data.get("data_for_llm")

    if not user_id or not routine_type or payload_for_llm is None:
        log_error("request_router", fn_name, f"Invalid internal system event data: {event_data}")
        return

    agent_state = get_agent(user_id)
    if not agent_state:
        log_error("request_router", fn_name, f"Agent state not found for {user_id} during internal event.", user_id=user_id)
        return

    preferences = agent_state.get("preferences", {})
    if preferences.get("status") != "active":
        return # Silently skip for non-active users

    if ORCHESTRATOR_IMPORTED and _route_to_orchestrator_func and \
       CONTEXT_SERVICE_IMPORTED and _get_context_snapshot_func:
        try:
            system_trigger_input_for_llm = {"trigger_type": routine_type, "payload": payload_for_llm}
            # The message for the LLM is the JSON string of this trigger
            message_for_llm = json.dumps(system_trigger_input_for_llm)

            # Log this system trigger event to DB messages table
            if ACTIVITY_DB_IMPORTED and _activity_db_log_func:
                _activity_db_log_func(
                    user_id=user_id, role="system_internal",
                    message_type=f"system_routine_{routine_type}", # e.g., system_routine_morning_summary_data
                    content_text=message_for_llm # Store the payload JSON as content for this type
                )

            # History for routine generation is typically empty or not directly used for the first message
            history_for_routine: List[Dict[str, Any]] = []
            wt_items_ctx, gcal_events_ctx = _get_context_snapshot_func(user_id)

            response_message_str = _route_to_orchestrator_func(
                user_id=user_id, message=message_for_llm,
                history=history_for_routine, preferences=preferences,
                task_context=wt_items_ctx, calendar_context=gcal_events_ctx
            )

            if response_message_str:
                send_message(user_id, response_message_str) # This will log it as 'OUT'/'assistant'
            else:
                log_warning("request_router", fn_name, f"Orchestrator empty response for internal event '{routine_type}' for {user_id}.", user_id=user_id)
        except Exception as e_orch_internal:
            log_error("request_router", fn_name, f"Error routing internal event '{routine_type}' to Orchestrator for {user_id}", e_orch_internal, user_id=user_id)
    else:
        log_error("request_router", fn_name, f"Cannot process internal event '{routine_type}' for {user_id}: Core components missing.", user_id=user_id)

# --- END OF FULL bridge/request_router.py ---