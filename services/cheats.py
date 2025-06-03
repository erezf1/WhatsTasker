# --- START OF FULL services/cheats.py ---

import json
from typing import List, Dict, Any
from datetime import datetime, timedelta
import pytz

# Service Imports
from services import task_query_service # For context snapshot
from services import task_manager       # Though not directly used in these modified cheats
from services import agent_state_manager
from services import sync_service
from services import routine_service
from users.user_registry import get_user_preferences

# --- NEW IMPORTS ---
from agents.orchestrator_agent import handle_user_request as call_orchestrator_agent
# We need get_context_snapshot to provide the same context the orchestrator would get
from services.task_query_service import get_context_snapshot
# --- END NEW IMPORTS ---

try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    print("[ERROR] [cheats:import] activity_db not found. /clear command may fail.")
    DB_IMPORTED = False
    class activity_db: # Dummy
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
        @staticmethod
        def get_task(*args, **kwargs): return None


from tools.logger import log_info, log_error, log_warning

# These constants are used by the cheat handlers when they call get_synced_context_snapshot
ROUTINE_CONTEXT_HISTORY_DAYS_CHEAT = 1
ROUTINE_CONTEXT_FUTURE_DAYS_CHEAT = 14 # Ensure this matches or is wider than routine_service's own context fetch if it differs

# --- Private Handler Functions ---

def _handle_help() -> str:
    return """Available Cheat Commands:
/help - Show this help message
/list [status] - List items (status: active*, pending, completed, all)
/memory - Show summary of current agent in-memory state
/clear - !! DANGER !! Mark all user's items as cancelled
/morning - Trigger morning summary generation (LLM creates user message)
/evening - Trigger evening review generation (LLM creates user message)"""


def _handle_list(user_id: str, args: List[str]) -> str:
    fn_name = "_handle_list_cheat"
    status_filter = args[0].lower() if args else 'active'
    allowed_statuses = ['active', 'pending', 'in_progress', 'completed', 'all']

    if status_filter not in allowed_statuses:
        return f"Invalid status '{status_filter}'. Use one of: {', '.join(allowed_statuses)}"
    try:
        prefs = get_user_preferences(user_id) or {}
        # user_tz_str = prefs.get("TimeZone", "UTC") # Not directly used in the message here
        list_body, mapping = task_query_service.get_formatted_list(
            user_id=user_id, status_filter=status_filter,
        )
        if list_body:
            list_intro = f"Items with status '{status_filter}':\n---\n" # Timezone info removed as it's in item lines
            return list_intro + list_body
        else:
            return f"No items found with status '{status_filter}'."
    except Exception as e:
        log_error("cheats", fn_name, f"Error calling get_formatted_list: {e}", e, user_id=user_id)
        return "Error retrieving list."


def _handle_memory(user_id: str) -> str:
    fn_name = "_handle_memory_cheat"
    try:
        agent_state = agent_state_manager.get_agent_state(user_id)
        if agent_state:
            # Make a copy for modification and sensitive data removal if needed
            state_copy = json.loads(json.dumps(agent_state, default=str)) # Deep copy & ensure serializable
            if "calendar" in state_copy: # Don't dump full calendar object
                state_copy["calendar_object_present"] = state_copy["calendar"] is not None
                del state_copy["calendar"]
            if "conversation_history" in state_copy:
                state_copy["conversation_history_count"] = len(state_copy.pop("conversation_history"))
            if "active_tasks_context" in state_copy:
                state_copy["active_tasks_context_count"] = len(state_copy.pop("active_tasks_context"))
            if "notified_event_ids_today" in state_copy:
                state_copy["notified_event_ids_today_count"] = len(state_copy.pop("notified_event_ids_today"))

            return f"Agent Memory Summary (User: {user_id}):\n```json\n{json.dumps(state_copy, indent=2)}\n```"
        else:
            return "Error: Agent state not found in memory."
    except Exception as e:
        log_error("cheats", fn_name, f"Error retrieving agent state: {e}", e, user_id=user_id)
        return "Error retrieving agent memory state."


def _handle_clear(user_id: str) -> str:
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

# --- MODIFIED _handle_morning ---
def _handle_morning(user_id: str) -> str:
    fn_name = "_handle_morning_cheat"
    log_info("cheats", fn_name, f"Executing /morning cheat for {user_id} (LLM will generate final message)")
    try:
        agent_full_state = agent_state_manager.get_agent_state(user_id)
        if not agent_full_state: return "Error: Could not retrieve full agent state."
        prefs = agent_full_state.get("preferences")
        if not prefs: return "Error: Could not retrieve user preferences from agent state."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service._get_local_time(user_tz_str)

        context_start_date_cheat = (now_local.date() - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS_CHEAT)).strftime("%Y-%m-%d")
        context_end_date_cheat = (now_local.date() + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS_CHEAT)).strftime("%Y-%m-%d")
        
        # 1. Get the aggregated context (DB items + GCal items)
        # This context is what routine_service functions use.
        aggregated_context_for_routine_fn = sync_service.get_synced_context_snapshot(user_id, context_start_date_cheat, context_end_date_cheat)
        
        # 2. Generate the raw data payload using the actual routine service function
        summary_data_payload = routine_service.generate_morning_summary_data(user_id, aggregated_context_for_routine_fn)
        
        if not summary_data_payload:
            return "No data generated by routine_service.generate_morning_summary_data. Orchestrator would not be triggered."

        # 3. Prepare the system trigger message for the Orchestrator
        system_trigger_input_for_llm = {
            "trigger_type": "morning_summary_data", # This matches what scheduler_service would send
            "payload": summary_data_payload
        }
        message_for_orchestrator_llm = json.dumps(system_trigger_input_for_llm)

        # 4. Get the current full context for the Orchestrator agent
        #    (as if the Orchestrator was being called normally)
        #    The history_weeks and future_weeks here define the Orchestrator's general view,
        #    which might be different from the routine's specific data payload context window.
        #    Let's use the Orchestrator's default context window.
        wt_items_ctx_for_orch, gcal_events_ctx_for_orch = get_context_snapshot(user_id) # Uses default history/future weeks
        
        # For a cheat code triggering a routine, the history fed to orchestrator is usually minimal/empty,
        # as it's a system-initiated event, not a direct continuation of user chat.
        history_for_orchestrator: List[Dict[str, Any]] = [] 

        # 5. Call the Orchestrator Agent
        log_info("cheats", fn_name, f"Calling orchestrator agent for user {user_id} with morning summary payload.")
        user_facing_message = call_orchestrator_agent(
            user_id=user_id,
            message=message_for_orchestrator_llm, # The system trigger JSON
            history=history_for_orchestrator,
            preferences=prefs,
            task_context=wt_items_ctx_for_orch,
            calendar_context=gcal_events_ctx_for_orch
        )
        return f"--- Morning Summary (as user would see it) ---\n{user_facing_message}"

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating morning summary via cheat (LLM path) for {user_id}", e, user_id=user_id)
        return f"An error occurred while generating the morning summary via LLM: {str(e)}"

# --- MODIFIED _handle_evening ---
def _handle_evening(user_id: str) -> str:
    fn_name = "_handle_evening_cheat"
    log_info("cheats", fn_name, f"Executing /evening cheat for {user_id} (LLM will generate final message)")
    try:
        agent_full_state = agent_state_manager.get_agent_state(user_id)
        if not agent_full_state: return "Error: Could not retrieve full agent state."
        prefs = agent_full_state.get("preferences")
        if not prefs: return "Error: Could not retrieve user preferences from agent state."

        user_tz_str = prefs.get("TimeZone", "UTC")
        now_local = routine_service._get_local_time(user_tz_str)

        context_start_date_cheat = (now_local.date() - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS_CHEAT)).strftime("%Y-%m-%d")
        context_end_date_cheat = (now_local.date() + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS_CHEAT)).strftime("%Y-%m-%d")

        aggregated_context_for_routine_fn = sync_service.get_synced_context_snapshot(user_id, context_start_date_cheat, context_end_date_cheat)
        
        review_data_payload = routine_service.generate_evening_review_data(user_id, aggregated_context_for_routine_fn)

        if not review_data_payload:
            return "No data generated by routine_service.generate_evening_review_data. Orchestrator would not be triggered."

        system_trigger_input_for_llm = {
            "trigger_type": "evening_review_data",
            "payload": review_data_payload
        }
        message_for_orchestrator_llm = json.dumps(system_trigger_input_for_llm)

        wt_items_ctx_for_orch, gcal_events_ctx_for_orch = get_context_snapshot(user_id)
        history_for_orchestrator: List[Dict[str, Any]] = []

        log_info("cheats", fn_name, f"Calling orchestrator agent for user {user_id} with evening review payload.")
        user_facing_message = call_orchestrator_agent(
            user_id=user_id,
            message=message_for_orchestrator_llm,
            history=history_for_orchestrator,
            preferences=prefs,
            task_context=wt_items_ctx_for_orch,
            calendar_context=gcal_events_ctx_for_orch
        )
        return f"--- Evening Review (as user would see it) ---\n{user_facing_message}"

    except Exception as e:
        log_error("cheats", fn_name, f"Error generating evening review via cheat (LLM path) for {user_id}", e, user_id=user_id)
        return f"An error occurred while generating the evening review via LLM: {str(e)}"


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

# --- END OF FULL services/cheats.py ---