# --- START OF REFACTORED services/cheats.py ---

"""
Service layer for handling direct 'cheat code' commands, bypassing the LLM orchestrator.
Used primarily for testing, debugging, and direct actions. Interacts with DB via services.
"""
import json
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

# Service Imports
from services import task_query_service # For /list
from services import task_manager # For /clear (cancel_item)
from services import agent_state_manager # For /memory
from services import sync_service # For /morning, /evening
from services import routine_service # For /morning, /evening helpers
from users.user_registry import get_user_preferences

# --- Database Import (Needed for /clear) ---
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("cheats", "import", "activity_db not found. /clear command may fail.", None)
    DB_IMPORTED = False
    class activity_db: # Dummy class
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
# --- End DB Import ---

# Utilities
from tools.logger import log_info, log_error, log_warning

# Define constants used by routines (mirroring routine_service)
ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14

# --- Private Handler Functions ---

def _handle_help() -> str:
    """Provides help text for available cheat commands."""
    # (No changes needed)
    return """Available Cheat Commands:
/help - Show this help message
/list [status] - List items (status: active*, pending, completed, all)
/memory - Show summary of current agent in-memory state
/clear - !! DANGER !! Mark all user's items as cancelled
/morning - Generate and show today's morning summary
/evening - Generate and show today's evening review"""


def _handle_list(user_id: str, args: List[str]) -> str:
    """Handles the /list command by calling the refactored task_query_service."""
    # (No changes needed - relies on task_query_service using the DB)
    fn_name = "_handle_list"
    status_filter = args[0].lower() if args else 'active'
    allowed_statuses = ['active', 'pending', 'in_progress', 'completed', 'all']

    if status_filter not in allowed_statuses:
        return f"Invalid status '{status_filter}'. Use one of: {', '.join(allowed_statuses)}"

    try:
        # Get timezone for display context
        prefs = get_user_preferences(user_id) or {}
        user_tz_str = prefs.get("TimeZone", "UTC")

        # Call the query service function (which now uses the DB)
        list_body, mapping = task_query_service.get_formatted_list(
            user_id=user_id,
            status_filter=status_filter,
            # No project/date filter for basic /list cheat
        )
        if list_body:
            list_intro = f"Items with status '{status_filter}' (Times relative to {user_tz_str}):\n---\n"
            return list_intro + list_body
        else:
            return f"No items found with status '{status_filter}'."
    except Exception as e:
        log_error("cheats", fn_name, f"Error calling get_formatted_list: {e}", e, user_id=user_id)
        return "Error retrieving list."


def _handle_memory(user_id: str) -> str:
    """Handles the /memory command."""
    # (No changes needed)
    fn_name = "_handle_memory"
    try:
        agent_state = agent_state_manager.get_agent_state(user_id)
        if agent_state:
            # Create a serializable summary (avoiding non-JSON types like sets)
            state_summary = {
                "user_id": agent_state.get("user_id"),
                "preferences_keys": list(agent_state.get("preferences", {}).keys()),
                "history_count": len(agent_state.get("conversation_history", [])),
                "context_item_count": len(agent_state.get("active_tasks_context", [])),
                "calendar_object_present": agent_state.get("calendar") is not None,
                "notified_ids_today_count": len(agent_state.get("notified_event_ids_today", set()))
            }
            return f"Agent Memory Summary:\n```json\n{json.dumps(state_summary, indent=2)}\n```"
        else:
            return "Error: Agent state not found in memory."
    except Exception as e:
        log_error("cheats", fn_name, f"Error retrieving agent state: {e}", e, user_id=user_id)
        return "Error retrieving agent memory state."


def _handle_clear(user_id: str) -> str:
    """Handles the /clear command. Finds non-cancelled items in DB and attempts cancellation."""
    # --- REFACTORED ---
    fn_name = "_handle_clear"
    log_warning("cheats", fn_name, f"!! Initiating /clear command for user {user_id} !!")
    cancelled_count = 0
    failed_count = 0
    errors = []

    if not DB_IMPORTED:
        log_error("cheats", fn_name, "Database module not available, cannot perform clear.", user_id=user_id)
        return "Error: Cannot access task database to perform clear."

    try:
        # Fetch only tasks/reminders that are NOT already cancelled from DB
        statuses_to_clear = ["pending", "in_progress", "completed"] # Include completed to clear them too
        items_to_clear_dicts = activity_db.list_tasks_for_user(user_id=user_id, status_filter=statuses_to_clear)

        if not items_to_clear_dicts:
            return "No items found in a clearable state (pending, in_progress, completed)."

        item_ids_to_clear = [item.get("event_id") for item in items_to_clear_dicts if item.get("event_id")]

        log_info("cheats", fn_name, f"Found {len(item_ids_to_clear)} items in DB to attempt cancellation for user {user_id}.")

        for item_id in item_ids_to_clear:
            try:
                # Call task_manager.cancel_item (which now updates DB and handles GCal)
                success = task_manager.cancel_item(user_id, item_id)
                if success:
                    cancelled_count += 1
                else:
                    failed_count += 1
                    errors.append(f"Failed cancel: {item_id[:8]}...")
                    log_warning("cheats", fn_name, f"task_manager.cancel_item failed for {item_id}", user_id=user_id)
            except Exception as cancel_e:
                failed_count += 1
                errors.append(f"Error cancel: {item_id[:8]}... ({type(cancel_e).__name__})")
                log_error("cheats", fn_name, f"Exception during cancel_item for {item_id}", cancel_e, user_id=user_id)

        response = f"Clear operation finished.\nSuccessfully cancelled: {cancelled_count}\nFailed/Skipped: {failed_count}"
        if errors:
            response += "\nFailures:\n" + "\n".join(errors[:5]) # Show first 5 errors
            if len(errors) > 5: response += "\n..."

        return response

    except Exception as e:
        log_error("cheats", fn_name, f"Critical error during /clear setup or execution for {user_id}", e, user_id=user_id)
        return "A critical error occurred during the clear operation."
    # --- END REFACTORED ---

def _handle_morning(user_id: str) -> str:
    """Handles the /morning command by generating the summary."""
    # (No changes needed - relies on sync_service using the DB)
    fn_name = "_handle_morning"
    log_info("cheats", fn_name, f"Executing /morning cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs: return "Error: Could not retrieve user preferences."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service.get_local_time(user_tz_str)
        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")

        log_info("cheats", fn_name, f"Getting synced context for morning summary (User: {user_id})...")
        # sync_service now uses the DB
        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
        summary_msg = routine_service.generate_morning_summary(user_id, aggregated_context)

        return summary_msg if summary_msg else "Could not generate morning summary."

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating morning summary via cheat code for {user_id}", e, user_id=user_id)
        return "An error occurred while generating the morning summary."


def _handle_evening(user_id: str) -> str:
    """Handles the /evening command by generating the review."""
    # (No changes needed - relies on sync_service using the DB)
    fn_name = "_handle_evening"
    log_info("cheats", fn_name, f"Executing /evening cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs: return "Error: Could not retrieve user preferences."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service.get_local_time(user_tz_str)
        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")

        log_info("cheats", fn_name, f"Getting synced context for evening review (User: {user_id})...")
        # sync_service now uses the DB
        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
        review_msg = routine_service.generate_evening_review(user_id, aggregated_context)

        return review_msg if review_msg else "Could not generate evening review."

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating evening review via cheat code for {user_id}", e, user_id=user_id)
        return "An error occurred while generating the evening review."


# --- Main Dispatcher ---

def handle_cheat_command(user_id: str, command: str, args: List[str]) -> str:
    """
    Dispatches cheat commands to the appropriate handler.
    """
    # (No changes needed in dispatcher logic)
    command = command.lower()

    if command == "/help": return _handle_help()
    elif command == "/list": return _handle_list(user_id, args)
    elif command == "/memory": return _handle_memory(user_id)
    elif command == "/clear": return _handle_clear(user_id)
    elif command == "/morning": return _handle_morning(user_id)
    elif command == "/evening": return _handle_evening(user_id)
    else: return f"Unknown command: '{command}'. Try /help."

# --- END OF REFACTORED services/cheats.py ---