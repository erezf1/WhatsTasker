# --- START OF FULL bridge/request_router.py ---

import re
import os
import yaml
import traceback
import json
from typing import Optional, Tuple, List # Keep Optional if used elsewhere internally
from tools.logger import log_info, log_error, log_warning # Keep logger for operational logs

# --- Database Logging Import ---
try:
    import tools.activity_db as activity_db
    ACTIVITY_DB_IMPORTED = True
except ImportError:
    log_error("request_router", "import", "Failed to import activity_db. Message DB logging disabled.", None)
    ACTIVITY_DB_IMPORTED = False
    # Define a dummy function if import fails
    class activity_db:
        @staticmethod
        def log_message_db(*args, **kwargs): pass
# --- End DB Import ---

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
    """Removes non-digit characters. Specific to phone number IDs."""
    # Consider if other ID types might exist later.
    # For WhatsApp, it might receive 'number@c.us'. We want just the number part usually.
    if user_id and '@' in user_id:
        user_id = user_id.split('@')[0]
    return re.sub(r'\D', '', user_id) if user_id else ""

def set_bridge(bridge_instance):
    """Sets the active bridge instance for sending messages."""
    global current_bridge
    if current_bridge is None:
        current_bridge = bridge_instance
        log_info("request_router", "set_bridge", f"Bridge set to: {type(bridge_instance).__name__}")
    else:
        log_warning("request_router", "set_bridge", "Attempted to set bridge when one is already configured.")


def send_message(user_id: str, message: str):
    """Adds agent message to history, logs it to DB, and sends via configured bridge."""
    fn_name = "send_message"
    # Note: user_id received here is expected to be the *normalized* ID

    # 1. Add to conversation history in memory
    if user_id and message:
        add_message_to_user_history(user_id, "agent", message)
    else:
        log_warning("request_router", fn_name, f"Attempted add empty msg/invalid user_id ({user_id}) to history.")
        # Don't proceed if invalid
        return

    # 2. Log outgoing message to Database
    if ACTIVITY_DB_IMPORTED:
        activity_db.log_message_db(
            direction='OUT',
            user_id=user_id, # Log with normalized ID
            content=message,
            raw_user_id=None, # Raw ID isn't typically available here
            bridge_message_id=None # Bridge ID generated later by the bridge instance
        )
    # else: DB logging disabled or failed import

    # 3. Send via Bridge
    log_info("request_router", fn_name, f"Queuing OUT message for {user_id}: '{message[:100]}...'")
    if current_bridge:
        try:
            # The bridge's send_message method handles formatting (like adding @c.us)
            # and generating the unique bridge_message_id for ACK
            current_bridge.send_message(user_id, message)
        except Exception as e:
            # Log error with user_id context
            log_error("request_router", fn_name, f"Bridge error sending message to {user_id}: {e}", e, user_id=user_id)
    else:
        log_error("request_router", fn_name, "No bridge configured. Cannot send message.")


# --- Main Handler ---
def handle_incoming_message(user_id: str, message: str) -> str:
    """
    Routes incoming messages based on user status (new, onboarding, active) or cheat codes.
    Logs incoming message to DB.
    """
    fn_name = "handle_incoming_message"
    final_response_message = GENERIC_ERROR_MSG
    # Keep the raw user_id received from the bridge for logging/potential use
    raw_user_id = user_id

    try:
        log_info("request_router", fn_name, f"Received raw: {raw_user_id} msg: '{message[:50]}...'")
        norm_user_id = normalize_user_id(raw_user_id)

        if not norm_user_id:
            log_error("request_router", fn_name, f"Invalid User ID after normalization: {raw_user_id}", user_id=raw_user_id) # Log with raw ID context
            # Don't send response here, let the caller handle HTTP error
            return "Error: Invalid User ID." # Or raise exception?

        # --- Log Incoming Message to DB ---
        if ACTIVITY_DB_IMPORTED:
            activity_db.log_message_db(
                direction='IN',
                user_id=norm_user_id, # Log with normalized ID
                content=message,
                raw_user_id=raw_user_id # Store original ID from bridge
            )
        # ---------------------------------

        # --- Ensure agent state exists ---
        agent_state = get_agent(norm_user_id) # Uses normalized ID
        if not agent_state:
            # Log error with normalized ID context
            log_error("request_router", fn_name, f"CRITICAL: Failed get/create agent state for {norm_user_id}.", user_id=norm_user_id)
            # Send generic error back
            send_message(norm_user_id, GENERIC_ERROR_MSG)
            return GENERIC_ERROR_MSG

        current_status = agent_state.get("preferences", {}).get("status")
        log_info("request_router", fn_name, f"User {norm_user_id} status: {current_status}")

        # --- Routing Logic (Uses norm_user_id internally) ---

        # 1. New User: Send welcome, set status to onboarding
        if current_status == "new":
            log_info("request_router", fn_name, f"New user ({norm_user_id}). Sending welcome, setting status to onboarding.")
            send_message(norm_user_id, WELCOME_MESSAGE) # Sends via bridge
            if CONFIG_MANAGER_IMPORTED and set_user_status:
                if not set_user_status(norm_user_id, 'onboarding'):
                     log_error("request_router", fn_name, f"Failed update status from 'new' to 'onboarding' for {norm_user_id}", user_id=norm_user_id)
            else: log_error("request_router", fn_name, "ConfigManager unavailable, cannot update user status after welcome.", user_id=norm_user_id)
            # No further processing needed this turn; the ack is handled by the caller (FastAPI endpoint)
            # We return the message mainly for potential testing/logging in the caller, though the primary action is send_message
            return WELCOME_MESSAGE

        # 2. Cheat Codes (Check before onboarding/active routing)
        message_stripped = message.strip()
        if message_stripped.startswith('/') and CHEATS_IMPORTED and handle_cheat_command:
            parts = message_stripped.split(); command = parts[0].lower(); args = parts[1:]
            log_info("request_router", fn_name, f"Detected command '{command}' for {norm_user_id}. Routing to Cheats.")
            try:
                command_response = handle_cheat_command(norm_user_id, command, args)
            except Exception as e:
                log_error("request_router", fn_name, f"Error executing cheat '{command}': {e}", e, user_id=norm_user_id)
                command_response = "Error processing cheat."
            send_message(norm_user_id, command_response) # Send result back
            return command_response # Return result for caller

        elif message_stripped.startswith('/') and not CHEATS_IMPORTED:
             log_error("request_router", fn_name, "Cheat command detected, but Cheats service failed import.", user_id=norm_user_id)
             err_msg = "Error: Command processor unavailable."
             send_message(norm_user_id, err_msg)
             return err_msg

        # 3. Onboarding User: Route to onboarding agent
        elif current_status == "onboarding":
            log_info("request_router", fn_name, f"User {norm_user_id} is onboarding. Routing to onboarding agent.")
            add_message_to_user_history(norm_user_id, "user", message) # Add user reply to memory history first
            if ONBOARDING_AGENT_IMPORTED and handle_onboarding_request:
                try:
                     history = agent_state.get("conversation_history", [])
                     preferences = agent_state.get("preferences", {})
                     # This function is expected to return the response message string
                     final_response_message = handle_onboarding_request(norm_user_id, message, history, preferences)
                except Exception as onboard_e:
                     tb_str = traceback.format_exc()
                     # Log error with user context
                     log_error("request_router", fn_name, f"Error calling OnboardingAgent for {norm_user_id}. Traceback:\n{tb_str}", onboard_e, user_id=norm_user_id)
                     final_response_message = GENERIC_ERROR_MSG
            else:
                # Log error with user context
                log_error("request_router", fn_name, f"Onboarding required for {norm_user_id}, but onboarding agent not imported.", user_id=norm_user_id)
                final_response_message = "Sorry, there's an issue with the setup process right now."

        # 4. Active User: Route to main orchestrator
        elif current_status == "active":
            log_info("request_router", fn_name, f"User {norm_user_id} is active. Routing to orchestrator.")
            add_message_to_user_history(norm_user_id, "user", message)
            if ORCHESTRATOR_IMPORTED and route_to_orchestrator and QUERY_SERVICE_IMPORTED and get_context_snapshot:
                try:
                    history = agent_state.get("conversation_history", [])
                    preferences = agent_state.get("preferences", {})
                    task_context, calendar_context = get_context_snapshot(norm_user_id)
                    # This function returns the response string
                    final_response_message = route_to_orchestrator(
                        user_id=norm_user_id, message=message, history=history,
                        preferences=preferences, task_context=task_context, calendar_context=calendar_context)
                except Exception as orch_e:
                     tb_str = traceback.format_exc()
                     log_error("request_router", fn_name, f"Error calling OrchestratorAgent for {norm_user_id}. Traceback:\n{tb_str}", orch_e, user_id=norm_user_id)
                     final_response_message = GENERIC_ERROR_MSG
            else:
                 # Log error with user context
                 log_error("request_router", fn_name, f"Core components missing for active user {norm_user_id} (Orchestrator={ORCHESTRATOR_IMPORTED}, QueryService={QUERY_SERVICE_IMPORTED}).", user_id=norm_user_id)
                 final_response_message = "Sorry, I can't process your request right now due to a system issue."

        # 5. Unknown Status: Log error and give generic response
        else:
            log_error("request_router", fn_name, f"User {norm_user_id} has unknown status: '{current_status}'. Sending generic error.", user_id=norm_user_id)
            final_response_message = GENERIC_ERROR_MSG

        # --- Send the final response (if not handled earlier, like for welcome/cheat) ---
        # The agent functions (onboarding/orchestrator) return the message string.
        # We need to send it using our central send_message function.
        if final_response_message:
             send_message(norm_user_id, final_response_message)
        else:
             # This case means the agent function returned None or empty string, which is unexpected.
             log_warning("request_router", fn_name, f"Agent function returned empty response for {norm_user_id}. Sending generic error.", user_id=norm_user_id)
             send_message(norm_user_id, GENERIC_ERROR_MSG)
             final_response_message = GENERIC_ERROR_MSG # Ensure we return something

        # Return the message primarily for testing/ack purposes in the caller API
        return final_response_message

    except Exception as outer_e:
        tb_str = traceback.format_exc()
        # Try to include user ID in log if possible
        user_context = raw_user_id if 'raw_user_id' in locals() else "Unknown"
        log_error("request_router", fn_name, f"Unexpected outer error processing message for {user_context}. Traceback:\n{tb_str}", outer_e, user_id=user_context)
        # Attempt to send generic error if possible
        if 'norm_user_id' in locals() and norm_user_id:
            try: send_message(norm_user_id, GENERIC_ERROR_MSG)
            except: pass # Avoid errors during error handling
        return GENERIC_ERROR_MSG # Return generic error message

# --- END OF FULL bridge/request_router.py ---