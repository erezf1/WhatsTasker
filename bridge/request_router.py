# --- START OF FILE bridge/request_router.py ---

import re
import os
import yaml
import traceback
import json
from typing import Optional, Tuple, List
from tools.logger import log_info, log_error, log_warning

# State manager imports
from services.agent_state_manager import get_agent_state, add_message_to_user_history

# User/Agent Management
from users.user_manager import get_agent

# Orchestrator Import
try:
    from agents.orchestrator_agent import handle_user_request as route_to_orchestrator
    ORCHESTRATOR_IMPORTED = True
    log_info("request_router", "import", "Successfully imported OrchestratorAgent handler.")
except ImportError as e:
    ORCHESTRATOR_IMPORTED = False; log_error("request_router", "import", f"OrchestratorAgent import failed: {e}", e); route_to_orchestrator = None

# Onboarding Agent Import
try:
    from agents.onboarding_agent import handle_onboarding_request
    ONBOARDING_AGENT_IMPORTED = True
    log_info("request_router", "import", "Successfully imported OnboardingAgent handler.")
except ImportError as e:
     ONBOARDING_AGENT_IMPORTED = False; log_error("request_router", "import", f"OnboardingAgent import failed: {e}", e); handle_onboarding_request = None

# Task Query Service Import
try:
    from services.task_query_service import get_context_snapshot
    QUERY_SERVICE_IMPORTED = True
except ImportError as e:
     QUERY_SERVICE_IMPORTED = False; log_error("request_router", "import", f"TaskQueryService import failed: {e}", e); get_context_snapshot = None

# Cheat Service Import
try:
    from services.cheats import handle_cheat_command
    CHEATS_IMPORTED = True
    log_info("request_router", "import", "Successfully imported Cheats service.")
except ImportError as e:
     CHEATS_IMPORTED = False; log_error("request_router", "import", f"Cheats service import failed: {e}", e); handle_cheat_command = None

# ConfigManager Import
try:
    from services.config_manager import set_user_status
    CONFIG_MANAGER_IMPORTED = True
except ImportError as e:
     CONFIG_MANAGER_IMPORTED = False; log_error("request_router", "import", f"ConfigManager import failed: {e}", e); set_user_status = None


# Load standard messages
_messages = {}
try:
    messages_path = os.path.join("config", "messages.yaml")
    if os.path.exists(messages_path):
        with open(messages_path, 'r', encoding="utf-8") as f:
             content = f.read();
             if content.strip(): f.seek(0); _messages = yaml.safe_load(f) or {}
             else: _messages = {}
    else: log_warning("request_router", "import", f"{messages_path} not found."); _messages = {}
except Exception as e: log_error("request_router", "import", f"Failed to load messages.yaml: {e}", e); _messages = {}

GENERIC_ERROR_MSG = _messages.get("generic_error_message", "Sorry, an unexpected error occurred.")
WELCOME_MESSAGE = _messages.get("welcome_confirmation_message", "Hello! Welcome to WhatsTasker.")


# Global bridge instance
current_bridge = None

def normalize_user_id(user_id: str) -> str:
    return re.sub(r'\D', '', user_id) if user_id else ""

def set_bridge(bridge_instance):
    global current_bridge; current_bridge = bridge_instance
    log_info("request_router", "set_bridge", f"Bridge set to: {type(bridge_instance).__name__}")

def send_message(user_id: str, message: str):
    """Adds agent message to history and sends via configured bridge."""
    if user_id and message: add_message_to_user_history(user_id, "agent", message)
    else: log_warning("request_router", "send_message", f"Attempted add empty msg/invalid user_id ({user_id}) to history.")
    log_info("request_router", "send_message", f"Sending to {user_id}: '{message[:100]}...'")
    if current_bridge:
        try: current_bridge.send_message(user_id, message)
        except Exception as e: log_error("request_router", "send_message", f"Bridge error sending to {user_id}: {e}", e)
    else: log_error("request_router", "send_message", "No bridge configured.")


# --- Main Handler ---
def handle_incoming_message(user_id: str, message: str) -> str:
    """
    Routes incoming messages based on user status (new, onboarding, active) or cheat codes.
    """
    final_response_message = GENERIC_ERROR_MSG

    try:
        log_info("request_router", "handle_incoming_message", f"Received raw: {user_id} msg: '{message[:50]}...'")
        norm_user_id = normalize_user_id(user_id)
        if not norm_user_id: log_error("request_router", "handle_incoming_message", f"Invalid User ID: {user_id}"); return "Error: Invalid User ID."

        # --- Ensure agent state exists ---
        agent_state = get_agent(norm_user_id)
        if not agent_state: log_error("request_router", "handle_incoming_message", f"CRITICAL: Failed get/create agent state for {norm_user_id}."); return GENERIC_ERROR_MSG

        current_status = agent_state.get("preferences", {}).get("status")
        log_info("request_router", "handle_incoming_message", f"User {norm_user_id} status: {current_status}")

        # --- Routing Logic ---

        # 1. New User: Send welcome, set status to onboarding
        if current_status == "new":
            # *** CORRECTED LOG CALL ***
            log_info("request_router", "handle_incoming_message", f"New user ({norm_user_id}). Sending welcome, setting status to onboarding.")
            send_message(norm_user_id, WELCOME_MESSAGE)
            if CONFIG_MANAGER_IMPORTED and set_user_status:
                if not set_user_status(norm_user_id, 'onboarding'): # Set status to onboarding
                     log_error("request_router", "handle_incoming_message", f"Failed update status from 'new' to 'onboarding' for {norm_user_id}")
            else: log_error("request_router", "handle_incoming_message", "ConfigManager unavailable, cannot update user status after welcome.")
            return WELCOME_MESSAGE # End processing for this turn

        # 2. Cheat Codes (Check before onboarding/active routing)
        message_stripped = message.strip()
        if message_stripped.startswith('/') and CHEATS_IMPORTED and handle_cheat_command:
            parts = message_stripped.split(); command = parts[0].lower(); args = parts[1:]
            log_info("request_router", "handle_incoming_message", f"Detected command '{command}' for {norm_user_id}. Routing to Cheats.")
            try: command_response = handle_cheat_command(norm_user_id, command, args)
            except Exception as e: log_error("request_router", "handle_incoming_message", f"Error executing cheat '{command}': {e}", e); command_response = "Error processing cheat."
            send_message(norm_user_id, command_response); return command_response # Bypass other handlers

        elif message_stripped.startswith('/') and not CHEATS_IMPORTED:
             log_error("request_router", "handle_incoming_message", "Cheat command detected, but Cheats service failed import.")
             send_message(norm_user_id, "Error: Command processor unavailable."); return "Error: Command processor unavailable."

        # 3. Onboarding User: Route to onboarding agent
        elif current_status == "onboarding":
            log_info("request_router", "handle_incoming_message", f"User {norm_user_id} is onboarding. Routing to onboarding agent.")
            add_message_to_user_history(norm_user_id, "user", message) # Add user reply to history first
            if ONBOARDING_AGENT_IMPORTED and handle_onboarding_request:
                try:
                     history = agent_state.get("conversation_history", [])
                     preferences = agent_state.get("preferences", {})
                     final_response_message = handle_onboarding_request(norm_user_id, message, history, preferences)
                except Exception as onboard_e:
                     tb_str = traceback.format_exc()
                     log_error("request_router", "handle_incoming_message", f"Error calling OnboardingAgent for {norm_user_id}. Traceback:\n{tb_str}", onboard_e)
                     final_response_message = GENERIC_ERROR_MSG
            else:
                log_error("request_router", "handle_incoming_message", f"Onboarding required for {norm_user_id}, but onboarding agent not imported.")
                final_response_message = "Sorry, there's an issue with the setup process right now."

        # 4. Active User: Route to main orchestrator
        elif current_status == "active":
            log_info("request_router", "handle_incoming_message", f"User {norm_user_id} is active. Routing to orchestrator.")
            add_message_to_user_history(norm_user_id, "user", message)
            if ORCHESTRATOR_IMPORTED and route_to_orchestrator and QUERY_SERVICE_IMPORTED and get_context_snapshot:
                try:
                    history = agent_state.get("conversation_history", [])
                    preferences = agent_state.get("preferences", {})
                    task_context, calendar_context = get_context_snapshot(norm_user_id)
                    final_response_message = route_to_orchestrator(
                        user_id=norm_user_id, message=message, history=history,
                        preferences=preferences, task_context=task_context, calendar_context=calendar_context)
                except Exception as orch_e:
                     tb_str = traceback.format_exc()
                     log_error("request_router", "handle_incoming_message", f"Error calling OrchestratorAgent for {norm_user_id}. Traceback:\n{tb_str}", orch_e)
                     final_response_message = GENERIC_ERROR_MSG
            else:
                 log_error("request_router", "handle_incoming_message", f"Core components missing for active user {norm_user_id} (Orchestrator/QueryService).")
                 final_response_message = "Sorry, I can't process your request right now due to a system issue."

        # 5. Unknown Status: Log error and give generic response
        else:
            log_error("request_router", "handle_incoming_message", f"User {norm_user_id} has unknown status: '{current_status}'. Sending generic error.")
            final_response_message = GENERIC_ERROR_MSG


        # Send the final response (unless handled earlier)
        if final_response_message:
             send_message(norm_user_id, final_response_message)
        else:
             log_warning("request_router", "handle_incoming_message", f"Final response message was empty for {norm_user_id}. Sending generic error.")
             send_message(norm_user_id, GENERIC_ERROR_MSG)
             final_response_message = GENERIC_ERROR_MSG

        return final_response_message

    except Exception as outer_e:
        tb_str = traceback.format_exc()
        log_error("request_router", "handle_incoming_message", f"Unexpected outer error processing message for {user_id}. Traceback:\n{tb_str}", outer_e)
        if 'norm_user_id' in locals() and norm_user_id:
            try: send_message(norm_user_id, GENERIC_ERROR_MSG)
            except: pass
        return GENERIC_ERROR_MSG
# --- END OF FILE bridge/request_router.py ---