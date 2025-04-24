# --- START OF UPDATED services/cheats.py ---

"""
Service layer for handling direct 'cheat code' commands, bypassing the LLM orchestrator.
Used primarily for testing, debugging, and direct actions.
"""
import json
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta # Added datetime imports

# Service Imports
from services import task_query_service, task_manager, agent_state_manager
# --- ADDED IMPORTS for morning/evening cheats ---
from services import sync_service
from services import routine_service
from users.user_registry import get_user_preferences
# ----------------------------------------------
from tools import metadata_store # Needed to list all items for /clear

# Utilities
from tools.logger import log_info, log_error, log_warning

# Define constants used by routines (mirroring routine_service)
ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14

# --- Private Handler Functions ---

def _handle_help() -> str:
    """Provides help text for available cheat commands."""
    return """Available Cheat Commands:
/help - Show this help message
/list [status] - List items (status: active*, pending, completed, all)
/memory - Show summary of current agent in-memory state
/clear - !! DANGER !! Mark all user's items as cancelled (removes from GCal)
/morning - Generate and show today's morning summary
/evening - Generate and show today's evening review"""


def _handle_list(user_id: str, args: List[str]) -> str:
    """Handles the /list command."""
    status_filter = args[0].lower() if args else 'active'
    allowed_statuses = ['active', 'pending', 'in_progress', 'completed', 'all']

    if status_filter not in allowed_statuses:
        return f"Invalid status '{status_filter}'. Use one of: {', '.join(allowed_statuses)}"

    try:
        # Pass user's timezone to formatting function if available
        prefs = get_user_preferences(user_id) or {}
        user_tz_str = prefs.get("TimeZone", "UTC")

        list_body, _ = task_query_service.get_formatted_list(
            user_id=user_id,
            status_filter=status_filter,
            # Pass timezone for formatting (needs get_formatted_list to accept/pass it down)
            # For now, assuming _format_task_line called within get_formatted_list handles it
            # If not, this needs adjustment or get_formatted_list modification
        )
        if list_body:
            # Add user timezone info explicitly to the list output for clarity
            list_intro = f"Items with status '{status_filter}' (Times displayed relative to {user_tz_str}):\n"
            return list_intro + list_body
        else:
            return f"No items found with status '{status_filter}'."
    except Exception as e:
        log_error("cheats", "_handle_list", f"Error calling get_formatted_list: {e}", e)
        return "Error retrieving list."


def _handle_memory(user_id: str) -> str:
    """Handles the /memory command."""
    try:
        agent_state = agent_state_manager.get_agent_state(user_id)
        if agent_state:
            state_summary = {
                "user_id": agent_state.get("user_id"),
                "preferences_keys": list(agent_state.get("preferences", {}).keys()),
                "history_count": len(agent_state.get("conversation_history", [])),
                "context_item_count": len(agent_state.get("active_tasks_context", [])),
                "calendar_object_present": agent_state.get("calendar") is not None
            }
            return f"Agent Memory Summary:\n```json\n{json.dumps(state_summary, indent=2, default=str)}\n```"
        else:
            return "Error: Agent state not found in memory."
    except Exception as e:
        log_error("cheats", "_handle_memory", f"Error retrieving agent state: {e}", e)
        return "Error retrieving agent memory state."


def _handle_clear(user_id: str) -> str:
    """Handles the /clear command. Marks all items as cancelled."""
    log_warning("cheats", "_handle_clear", f"!! Initiating /clear command for user {user_id} !!")
    cancelled_count = 0
    failed_count = 0
    errors = []

    try:
        all_metadata = metadata_store.list_metadata(user_id=user_id)
        item_ids_to_clear = [item.get("event_id") for item in all_metadata if item.get("event_id") and item.get("status") != "cancelled"]

        if not item_ids_to_clear:
            return "No active items found to clear."

        log_info("cheats", "_handle_clear", f"Attempting to cancel {len(item_ids_to_clear)} items for user {user_id}.")

        for item_id in item_ids_to_clear:
            try:
                success = task_manager.cancel_item(user_id, item_id)
                if success:
                    cancelled_count += 1
                else:
                    failed_count += 1
                    errors.append(f"Failed cancel: {item_id[:8]}...")
                    log_warning("cheats", "_handle_clear", f"task_manager.cancel_item failed for {item_id}")
            except Exception as cancel_e:
                failed_count += 1
                errors.append(f"Error cancel: {item_id[:8]}... ({type(cancel_e).__name__})")
                log_error("cheats", "_handle_clear", f"Exception during cancel_item for {item_id}", cancel_e)

        response = f"Clear operation finished.\nSuccessfully cancelled: {cancelled_count}\nFailed/Skipped: {failed_count}"
        if errors:
            response += "\nFailures:\n" + "\n".join(errors[:5])
            if len(errors) > 5: response += "\n..."

        return response

    except Exception as e:
        log_error("cheats", "_handle_clear", f"Critical error during /clear setup or execution for {user_id}", e)
        return "A critical error occurred during the clear operation."


def _handle_morning(user_id: str) -> str:
    """Handles the /morning command by generating the summary."""
    fn_name = "_handle_morning"
    log_info("cheats", fn_name, f"Executing /morning cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs:
            return "Error: Could not retrieve user preferences."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service.get_local_time(user_tz_str) # Use helper from routine_service

        # Calculate date range for context
        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")

        log_info("cheats", fn_name, f"Getting synced context for morning summary (User: {user_id})...")
        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)

        # Generate the summary using the function from routine_service
        summary_msg = routine_service.generate_morning_summary(user_id, aggregated_context)

        return summary_msg if summary_msg else "Could not generate morning summary."

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating morning summary via cheat code for {user_id}", e)
        return "An error occurred while generating the morning summary."


def _handle_evening(user_id: str) -> str:
    """Handles the /evening command by generating the review."""
    fn_name = "_handle_evening"
    log_info("cheats", fn_name, f"Executing /evening cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs:
            return "Error: Could not retrieve user preferences."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service.get_local_time(user_tz_str) # Use helper

        # Calculate date range for context
        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")

        log_info("cheats", fn_name, f"Getting synced context for evening review (User: {user_id})...")
        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)

        # Generate the review using the function from routine_service
        review_msg = routine_service.generate_evening_review(user_id, aggregated_context)

        return review_msg if review_msg else "Could not generate evening review."

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating evening review via cheat code for {user_id}", e)
        return "An error occurred while generating the evening review."


# --- Main Dispatcher ---

def handle_cheat_command(user_id: str, command: str, args: List[str]) -> str:
    """
    Dispatches cheat commands to the appropriate handler.
    """
    command = command.lower() # Ensure case-insensitivity

    if command == "/help":
        return _handle_help()
    elif command == "/list":
        return _handle_list(user_id, args)
    elif command == "/memory":
        return _handle_memory(user_id)
    elif command == "/clear":
        return _handle_clear(user_id)
    elif command == "/morning":
        return _handle_morning(user_id) # Calls implemented handler
    elif command == "/evening":
        return _handle_evening(user_id) # Calls implemented handler
    else:
        return f"Unknown command: '{command}'. Try /help."

# --- END OF UPDATED services/cheats.py ---