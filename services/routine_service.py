# --- START OF FULL services/routine_service.py ---
"""
Handles scheduled generation of Morning and Evening summaries/reviews.
Includes timezone handling and daily cleanup tasks.
"""

import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Tuple
import pytz # For timezone handling
import re # Added for title matching

from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry, get_user_preferences
from services import sync_service # Import the whole module
from services.config_manager import update_preferences # To update last trigger date
from services.agent_state_manager import clear_notified_event_ids, get_agent_state # For daily cleanup

# Define time window for fetching context for routines
ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14 # Check tasks/events up to 2 weeks ahead for context

# --- Time Formatting Helpers ---

def _get_local_time(user_timezone_str: str) -> datetime:
    """Gets the current time in the user's specified timezone."""
    # (No changes needed)
    fn_name = "get_local_time"
    if not user_timezone_str: user_timezone_str = 'UTC'
    try:
        user_tz = pytz.timezone(user_timezone_str)
        return datetime.now(user_tz)
    except pytz.UnknownTimeZoneError:
        log_warning("routine_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC.")
        return datetime.now(pytz.utc)
    except Exception as e:
        log_error("routine_service", fn_name, f"Error getting local time for tz '{user_timezone_str}'", e)
        return datetime.now(pytz.utc)

def _format_time_local(iso_utc_str: str | None, user_tz: pytz.BaseTzInfo, default_time="??:??") -> str:
    """Formats an ISO UTC datetime string to local HH:MM."""
    # (No changes needed)
    if not iso_utc_str or 'T' not in iso_utc_str:
        return default_time
    try:
        dt_aware = datetime.fromisoformat(iso_utc_str.replace('Z', '+00:00'))
        dt_local = dt_aware.astimezone(user_tz)
        return dt_local.strftime('%H:%M')
    except (ValueError, TypeError):
        log_warning("routine_service", "_format_time_local", f"Could not parse/format time: {iso_utc_str}")
        return default_time

def _format_time_range_local(start_iso: str | None, end_iso: str | None, user_tz: pytz.BaseTzInfo, default_range="??:?? - ??:??") -> str:
    """Formats ISO UTC start/end strings to local HH:MM - HH:MM."""
    # (No changes needed)
    if not start_iso or not end_iso or 'T' not in start_iso or 'T' not in end_iso:
        return default_range
    try:
        start_aware = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
        end_aware = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))
        start_local = start_aware.astimezone(user_tz)
        end_local = end_aware.astimezone(user_tz)
        return f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')}"
    except (ValueError, TypeError):
         log_warning("routine_service", "_format_time_range_local", f"Could not parse/format time range: {start_iso} / {end_iso}")
         return default_range

def _get_item_local_date_str(item: Dict, user_tz: pytz.BaseTzInfo) -> str | None:
     """Gets the effective local date string (YYYY-MM-DD) for an item."""
     # (No changes needed)
     start_dt_str = item.get("gcal_start_datetime")
     item_date_local_str = None
     if start_dt_str:
         try:
             if 'T' in start_dt_str: # Datetime
                  dt_aware = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                  item_date_local_str = dt_aware.astimezone(user_tz).strftime("%Y-%m-%d")
             elif len(start_dt_str) == 10: # Date (All day)
                  item_date_local_str = start_dt_str # Already YYYY-MM-DD
         except (ValueError, TypeError): pass
     elif item.get("date"): # Fallback to WT date field
         item_date_local_str = item.get("date")
     return item_date_local_str

# --- Sorting Helper (Simplified from task_query_service) ---
def _sort_routine_items(items: List[Dict], user_tz: pytz.BaseTzInfo) -> List[Dict]:
    """Sorts items for routine display (by time primarily)."""
    # (No changes needed)
    def sort_key(item):
        start_dt_str = item.get("gcal_start_datetime")
        if start_dt_str and 'T' in start_dt_str:
             try:
                 dt_aware = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                 return dt_aware.replace(tzinfo=None)
             except ValueError: pass
        meta_date = item.get("date")
        meta_time = item.get("time")
        if meta_date:
             try:
                 day_dt = datetime.strptime(meta_date, "%Y-%m-%d")
                 if meta_time:
                      try:
                           hour, minute = map(int, meta_time.split(':'))
                           return day_dt.replace(hour=hour, minute=minute)
                      except (ValueError, TypeError): pass
                 return day_dt
             except ValueError: pass
        return datetime.max

    return sorted(items, key=sort_key)


# --- Routine Generation Functions (UPDATED FORMATTING) ---

def generate_morning_summary(user_id: str, context: List[Dict]) -> str:
    """Generates the morning summary message including GCal events and WT items."""
    fn_name = "generate_morning_summary"
    prefs = get_user_preferences(user_id)
    user_name = prefs.get("name", "") if prefs else ""
    user_name_greet = f" {user_name}" if user_name else ""
    user_tz_str = prefs.get("TimeZone", "UTC") if prefs else "UTC"
    user_tz = pytz.timezone(user_tz_str)
    now_local = _get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    log_info("routine_service", fn_name, f"Generating morning summary for user {user_id}")

    reminders_today = []
    sessions_today = []
    todos_today = []

    for item in context:
        item_local_date = _get_item_local_date_str(item, user_tz)
        item_type = item.get("type")
        item_status = item.get("status")

        if item_local_date == today_local_str:
            if item_type in ["task", "reminder", "todo"] and item_status in ["completed", "cancelled"]:
                continue

            if item_type == "reminder" or item_type == "external_event":
                reminders_today.append(item)
            elif item_type == "task":
                 gcal_start = item.get("gcal_start_datetime")
                 # Only include Tasks if they represent a session with time today
                 if gcal_start and 'T' in gcal_start:
                     sessions_today.append(item)
            elif item_type == "todo":
                todos_today.append(item)

    # Sort items within categories
    reminders_today = _sort_routine_items(reminders_today, user_tz)
    sessions_today = _sort_routine_items(sessions_today, user_tz)
    todos_today.sort(key=lambda x: (x.get("date") is None, x.get("title", "").lower()))

    # Build Message
    message_lines = [f"Good morning{user_name_greet}! ☀️ Here's your plan for today:"]
    items_found = False

    if reminders_today:
        items_found = True
        message_lines.append("\n**Reminders:**")
        for item in reminders_today:
            title = item.get("title", "(Untitled Reminder)")
            start_time_iso = item.get('gcal_start_datetime')
            is_all_day = item.get('is_all_day', False) or (start_time_iso and 'T' not in start_time_iso) # Check if all-day

            # --- **FORMATTING CHANGE HERE** ---
            if is_all_day:
                message_lines.append(f"- {title} (All Day)")
            else:
                time_str = _format_time_local(start_time_iso, user_tz)
                message_lines.append(f"- {title} @ {time_str}")
            # --- **END FORMATTING CHANGE** ---

    if sessions_today:
        items_found = True
        message_lines.append("\n**Work Sessions:**")
        for item in sessions_today:
             time_range_str = _format_time_range_local(item.get('gcal_start_datetime'), item.get('gcal_end_datetime'), user_tz)
             title = item.get("title", "(Untitled Task)")
             match = re.match(r"Work: (.*?) \[\d+/\d+\]", title)
             display_title = match.group(1).strip() if match else title
             message_lines.append(f"- {display_title}: {time_range_str}")

    if todos_today:
        items_found = True
        message_lines.append("\n**Active ToDos:**")
        for item in todos_today:
             title = item.get("title", "(Untitled ToDo)")
             due_date_str = item.get("date")
             due_suffix = " (Due Today)" if due_date_str == today_local_str else ""
             message_lines.append(f"- {title}{due_suffix}")

    if not items_found:
        return f"Good morning{user_name_greet}! ☀️ Looks like a clear schedule today. Anything you'd like to add?"

    message_lines.append("\n👉 Consider tackling one of your ToDos today if you have spare moments!")
    message_lines.append("\nHave a productive day!")
    return "\n".join(message_lines)


def generate_evening_review(user_id: str, context: List[Dict]) -> str:
    """Generates the evening review message, listing scheduled items and active ToDos for the day."""
    fn_name = "generate_evening_review"
    prefs = get_user_preferences(user_id)
    user_name = prefs.get("name", "") if prefs else ""
    user_name_greet = f" {user_name}" if user_name else ""
    user_tz_str = prefs.get("TimeZone", "UTC") if prefs else "UTC"
    user_tz = pytz.timezone(user_tz_str)
    now_local = _get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    log_info("routine_service", fn_name, f"Generating evening review for user {user_id}")

    items_for_review = []

    for item in context:
        item_local_date = _get_item_local_date_str(item, user_tz)
        item_type = item.get("type")
        item_status = item.get("status")

        if item_local_date == today_local_str:
            if item_type == "reminder" or item_type == "external_event":
                 if item.get("status_gcal") != "cancelled":
                     items_for_review.append(item)
            elif item_type == "task":
                 gcal_start = item.get("gcal_start_datetime")
                 # Include if it has a specific time scheduled today
                 if gcal_start and 'T' in gcal_start:
                    items_for_review.append(item)
            elif item_type == "todo" and item_status in ["pending", "in_progress"]:
                 items_for_review.append(item)

    if not items_for_review:
        return f"Good evening{user_name_greet}! 👋 No active tasks, reminders, or ToDos were scheduled for today. Time to relax or plan for tomorrow?"

    # Sort all items together for the numbered list
    sorted_items = _sort_routine_items(items_for_review, user_tz)

    message_lines = [f"Good evening{user_name_greet}! 👋 Let's review today's items:"]
    scheduled_item_indices = []
    todo_item_indices = []

    for i, item in enumerate(sorted_items):
        item_num = i + 1
        item_type = item.get("type")
        title = item.get("title", "(Untitled Item)")
        line = f"{item_num}. "
        start_time_iso = item.get('gcal_start_datetime')
        is_all_day = item.get('is_all_day', False) or (start_time_iso and 'T' not in start_time_iso)

        if item_type == "reminder" or item_type == "external_event":
            # --- **FORMATTING CHANGE HERE** ---
            if is_all_day:
                line += f"(Reminder) {title} (All Day)"
            else:
                time_str = _format_time_local(start_time_iso, user_tz)
                line += f"(Reminder) {title} @ {time_str}"
            # --- **END FORMATTING CHANGE** ---
            scheduled_item_indices.append(str(item_num))
        elif item_type == "task": # Representing a session
            time_range_str = _format_time_range_local(start_time_iso, item.get('gcal_end_datetime'), user_tz)
            match = re.match(r"Work: (.*?) \[\d+/\d+\]", title)
            display_title = match.group(1).strip() if match else title
            line += f"(Session) {display_title}: {time_range_str}"
            scheduled_item_indices.append(str(item_num))
        elif item_type == "todo":
            status_str = str(item.get('status', 'Pending')).capitalize()
            due_date_str = item.get("date")
            due_suffix = " (Due Today)" if due_date_str == today_local_str else ""
            line += f"(ToDo) {title} [{status_str}]{due_suffix}"
            todo_item_indices.append(str(item_num))
        else:
            line += f"(Unknown) {title}"

        message_lines.append(line)

    # Build dynamic footer
    footer_parts = []
    if scheduled_item_indices:
        # Construct range string e.g., "1-3", "1", "1, 3"
        range_str = ""
        if len(scheduled_item_indices) == 1:
            range_str = scheduled_item_indices[0]
        elif len(scheduled_item_indices) > 1:
             # Check if consecutive
             nums = [int(x) for x in scheduled_item_indices]
             is_consecutive = all(nums[j] == nums[0] + j for j in range(len(nums)))
             if is_consecutive:
                 range_str = f"{nums[0]}-{nums[-1]}"
             else:
                 range_str = ", ".join(scheduled_item_indices)

        footer_parts.append(f"Are the scheduled items ({range_str}) complete, or do any need rescheduling?")

    if todo_item_indices:
        range_str = ""
        if len(todo_item_indices) == 1:
             range_str = todo_item_indices[0]
        elif len(todo_item_indices) > 1:
             nums = [int(x) for x in todo_item_indices]
             is_consecutive = all(nums[j] == nums[0] + j for j in range(len(nums)))
             if is_consecutive:
                  range_str = f"{nums[0]}-{nums[-1]}"
             else:
                  range_str = ", ".join(todo_item_indices)

        footer_parts.append(f"You can also update ToDo status (e.g., 'complete {range_str}', 'cancel {range_str}').")

    if footer_parts:
         message_lines.append("\n" + "\n".join(footer_parts))

    return "\n".join(message_lines)


def check_routine_triggers() -> List[Tuple[str, str]]:
    """
    Scheduled job function. Checks all users if morning/evening routines should run.
    Calls sync service before generating summaries.
    Returns a list of (user_id, message_content) tuples for messages to be sent.
    """
    # (No changes needed in this function's logic)
    fn_name = "check_routine_triggers"
    log_info("routine_service", fn_name, "Running scheduled check for routine triggers...")
    messages_to_send: List[Tuple[str, str]] = []

    try:
        registry = get_registry()
        if not registry:
            log_warning("routine_service", fn_name, "User registry is empty. Skipping check.")
            return []

        user_ids = list(registry.keys())
        log_info("routine_service", fn_name, f"Checking routines for {len(user_ids)} users.")

        for user_id in user_ids:
            prefs = None
            try:
                prefs = get_user_preferences(user_id)
                if not prefs or prefs.get("status") != "active":
                    continue

                user_tz_str = prefs.get("TimeZone")
                if not user_tz_str:
                    continue

                now_local = _get_local_time(user_tz_str)
                today_local_str = now_local.strftime("%Y-%m-%d")
                current_local_hm = now_local.strftime("%H:%M")

                aggregated_context = None
                context_fetched = False

                # --- Check Morning Routine ---
                morning_time_str = prefs.get("Morning_Summary_Time")
                if prefs.get("Enable_Morning") and morning_time_str:
                    last_triggered_morning = prefs.get("last_morning_trigger_date")
                    if current_local_hm >= morning_time_str and last_triggered_morning != today_local_str:
                        log_info("routine_service", fn_name, f"Triggering Morning Summary for user {user_id} at {current_local_hm} {user_tz_str}")
                        if not context_fetched:
                            context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
                            context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")
                            log_info("routine_service", fn_name, f"Getting synced context for routines (User: {user_id})...")
                            aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
                            context_fetched = True

                        summary_msg = generate_morning_summary(user_id, aggregated_context or [])
                        if summary_msg:
                            messages_to_send.append((user_id, summary_msg))
                            update_success = update_preferences(user_id, {"last_morning_trigger_date": today_local_str})
                            if not update_success: log_error("routine_service", fn_name, f"Failed to update last_morning_trigger_date for {user_id}")
                        else:
                            log_warning("routine_service", fn_name, f"Morning summary generated empty message for {user_id}")

                # --- Check Evening Routine ---
                evening_time_str = prefs.get("Evening_Summary_Time")
                if prefs.get("Enable_Evening") and evening_time_str:
                    last_triggered_evening = prefs.get("last_evening_trigger_date")
                    if current_local_hm >= evening_time_str and last_triggered_evening != today_local_str:
                        log_info("routine_service", fn_name, f"Triggering Evening Review for user {user_id} at {current_local_hm} {user_tz_str}")
                        if not context_fetched:
                            context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
                            context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")
                            log_info("routine_service", fn_name, f"Getting synced context for routines (User: {user_id})...")
                            aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
                            context_fetched = True

                        review_msg = generate_evening_review(user_id, aggregated_context or [])
                        if review_msg:
                            messages_to_send.append((user_id, review_msg))
                            update_success = update_preferences(user_id, {"last_evening_trigger_date": today_local_str})
                            if not update_success: log_error("routine_service", fn_name, f"Failed to update last_evening_trigger_date for {user_id}")
                        else:
                            log_warning("routine_service", fn_name, f"Evening review generated empty message for {user_id}")

            except Exception as user_err:
                 log_error("routine_service", fn_name, f"Error processing routines for user {user_id}. Prefs: {prefs}", user_err)
                 traceback.print_exc()

    except Exception as main_err:
        log_error("routine_service", fn_name, f"General error during routine check run", main_err)
        traceback.print_exc()

    log_info("routine_service", fn_name, f"Finished scheduled check for routine triggers. Found {len(messages_to_send)} messages to send.")
    return messages_to_send


def daily_cleanup():
    """Scheduled job to perform daily cleanup tasks (e.g., reset notification tracker)."""
    # (No changes needed)
    fn_name = "daily_cleanup"
    log_info("routine_service", fn_name, "Running daily cleanup job...")

    try:
        registry = get_registry()
        user_ids = list(registry.keys())
        if not user_ids:
            log_info("routine_service", fn_name, "No users found for daily cleanup.")
            return

        log_info("routine_service", fn_name, f"Performing daily cleanup for {len(user_ids)} users...")
        cleared_count = 0
        for user_id in user_ids:
            try:
                if AGENT_STATE_IMPORTED:
                    if get_agent_state(user_id):
                       clear_notified_event_ids(user_id)
                       cleared_count += 1
            except Exception as e:
                 log_error("routine_service", fn_name, f"Error during daily cleanup for user {user_id}", e)

        log_info("routine_service", fn_name, f"Daily cleanup finished. Cleared notification sets for {cleared_count} users.")

    except Exception as e:
        log_error("routine_service", fn_name, "Error during daily cleanup main loop", e)

# --- END OF FULL services/routine_service.py ---