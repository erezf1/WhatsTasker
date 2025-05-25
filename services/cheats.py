# --- START OF FILE services/cheats.py ---

import json
from typing import List, Dict, Any
from datetime import datetime, timedelta
import pytz # Added for timezone formatting in cheats

# Service Imports
from services import task_query_service
from services import task_manager
from services import agent_state_manager
from services import sync_service
from services import routine_service # Import the module
from users.user_registry import get_user_preferences

try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    print("[ERROR] [cheats:import] activity_db not found. /clear command may fail.")
    DB_IMPORTED = False
    class activity_db:
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []

from tools.logger import log_info, log_error, log_warning

ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14

# --- Private Handler Functions ---

def _handle_help() -> str:
    return """Available Cheat Commands:
/help - Show this help message
/list [status] - List items (status: active*, pending, completed, all)
/memory - Show summary of current agent in-memory state
/clear - !! DANGER !! Mark all user's items as cancelled
/morning - Generate and show today's morning summary data (raw structured output)
/evening - Generate and show today's evening review data (raw structured output)"""


def _handle_list(user_id: str, args: List[str]) -> str:
    fn_name = "_handle_list_cheat"
    status_filter = args[0].lower() if args else 'active'
    allowed_statuses = ['active', 'pending', 'in_progress', 'completed', 'all']

    if status_filter not in allowed_statuses:
        return f"Invalid status '{status_filter}'. Use one of: {', '.join(allowed_statuses)}"
    try:
        prefs = get_user_preferences(user_id) or {}
        user_tz_str = prefs.get("TimeZone", "UTC")
        list_body, mapping = task_query_service.get_formatted_list(
            user_id=user_id, status_filter=status_filter,
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
    # (No changes needed for this function)
    fn_name = "_handle_memory_cheat"
    try:
        agent_state = agent_state_manager.get_agent_state(user_id)
        if agent_state:
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
    # (No changes needed for this function)
    fn_name = "_handle_clear_cheat"
    log_warning("cheats", fn_name, f"!! Initiating /clear command for user {user_id} !!")
    cancelled_count = 0; failed_count = 0; errors = []
    if not DB_IMPORTED: return "Error: Cannot access task database to perform clear."
    try:
        items_to_clear_dicts = activity_db.list_tasks_for_user(user_id=user_id, status_filter=["pending", "in_progress", "completed"])
        if not items_to_clear_dicts: return "No items found in a clearable state."
        item_ids_to_clear = [item.get("event_id") for item in items_to_clear_dicts if item.get("event_id")]
        for item_id in item_ids_to_clear:
            try:
                if task_manager.cancel_item(user_id, item_id): cancelled_count += 1
                else: failed_count += 1; errors.append(f"Failed: {item_id[:8]}")
            except Exception as cancel_e: failed_count += 1; errors.append(f"Error: {item_id[:8]} ({type(cancel_e).__name__})")
        response = f"Clear op finished. Cancelled: {cancelled_count}, Failed/Skipped: {failed_count}"
        if errors: response += "\nFailures:\n" + "\n".join(errors[:5])
        return response
    except Exception as e:
        log_error("cheats", fn_name, f"Critical error during /clear for {user_id}", e, user_id=user_id)
        return "A critical error occurred during the clear operation."


def _format_routine_data_for_cheat_display(routine_data: Dict | None, routine_type: str, user_tz_str: str) -> str:
    """Simple formatter for routine data for cheat code display."""
    if not routine_data:
        return f"No data generated for {routine_type}."

    user_tz = pytz.timezone(user_tz_str) if user_tz_str else pytz.utc
    lines = [f"--- {routine_type.replace('_data','').replace('_', ' ').title()} Data (User TZ: {user_tz_str}) ---"]

    if routine_type == "morning_summary_data":
        for key, item_list in routine_data.items():
            lines.append(f"\n**{key.replace('_',' ').title()}:**")
            if not item_list: lines.append("  (None)")
            for item in item_list:
                title = item.get('title', 'N/A')
                time_info = ""
                if item.get('is_all_day'): time_info = "(All Day)"
                elif item.get('gcal_start_datetime'):
                    try:
                        dt_aware = datetime.fromisoformat(item['gcal_start_datetime'].replace('Z', '+00:00'))
                        time_info = dt_aware.astimezone(user_tz).strftime('%H:%M')
                        if item.get('gcal_end_datetime'):
                             dt_end_aware = datetime.fromisoformat(item['gcal_end_datetime'].replace('Z', '+00:00'))
                             time_info += dt_end_aware.astimezone(user_tz).strftime('-%H:%M')
                    except: time_info = "(Time Error)"
                elif item.get('time'): time_info = item.get('time')

                lines.append(f"  - {title} @ {time_info if time_info else item.get('date','No Date')} [Type: {item.get('type','N/A')}, Status: {item.get('status','N/A')}]")
    
    elif routine_type == "evening_review_data":
        items = routine_data.get("items_for_review", [])
        if not items: lines.append("  (No items for review)")
        for i, item in enumerate(items):
            title = item.get('title', 'N/A')
            time_info = ""
            if item.get('is_all_day'): time_info = "(All Day)"
            elif item.get('gcal_start_datetime'):
                try:
                    dt_aware = datetime.fromisoformat(item['gcal_start_datetime'].replace('Z', '+00:00'))
                    time_info = dt_aware.astimezone(user_tz).strftime('%H:%M')
                    if item.get('gcal_end_datetime'):
                         dt_end_aware = datetime.fromisoformat(item['gcal_end_datetime'].replace('Z', '+00:00'))
                         time_info += dt_end_aware.astimezone(user_tz).strftime('-%H:%M')
                except: time_info = "(Time Error)"
            elif item.get('time'): time_info = item.get('time')

            incomplete_flag = " [INCOMPLETE TASK]" if item.get('is_incomplete_task') else ""
            lines.append(f"  {i+1}. {title} @ {time_info if time_info else item.get('date','No Date')} [Type: {item.get('type','N/A')}, Status: {item.get('status','N/A')}]{incomplete_flag}")
            
    return "\n".join(lines)


def _handle_morning(user_id: str) -> str:
    fn_name = "_handle_morning_cheat"
    log_info("cheats", fn_name, f"Executing /morning cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs: return "Error: Could not retrieve user preferences."
        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service._get_local_time(user_tz_str) # Use helper from routine_service
        
        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")

        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
        
        # Call the new data generation function
        summary_data = routine_service.generate_morning_summary_data(user_id, aggregated_context)
        
        # Format this data for cheat display
        return _format_routine_data_for_cheat_display(summary_data, "morning_summary_data", user_tz_str)

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating morning summary data via cheat for {user_id}", e, user_id=user_id)
        return "An error occurred while generating the morning summary data."


def _handle_evening(user_id: str) -> str:
    fn_name = "_handle_evening_cheat"
    log_info("cheats", fn_name, f"Executing /evening cheat for {user_id}")
    try:
        prefs = get_user_preferences(user_id)
        if not prefs: return "Error: Could not retrieve user preferences."
        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service._get_local_time(user_tz_str) # Use helper from routine_service

        context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
        context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")
        
        aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
        
        # Call the new data generation function
        review_data = routine_service.generate_evening_review_data(user_id, aggregated_context)
        
        # Format this data for cheat display
        return _format_routine_data_for_cheat_display(review_data, "evening_review_data", user_tz_str)

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating evening review data via cheat for {user_id}", e, user_id=user_id)
        return "An error occurred while generating the evening review data."


# --- Main Dispatcher ---
def handle_cheat_command(user_id: str, command: str, args: List[str]) -> str:
    command = command.lower()
    if command == "/help": return _handle_help()
    elif command == "/list": return _handle_list(user_id, args)
    elif command == "/memory": return _handle_memory(user_id)
    elif command == "/clear": return _handle_clear(user_id)
    elif command == "/morning": return _handle_morning(user_id)
    elif command == "/evening": return _handle_evening(user_id)
    else: return f"Unknown command: '{command}'. Try /help."

# --- END OF FILE services/cheats.py ---