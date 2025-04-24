# --- START OF FILE services/routine_service.py ---
"""
Handles scheduled generation of Morning and Evening summaries/reviews.
Includes timezone handling and daily cleanup tasks.
"""

import traceback
from datetime import datetime, timedelta, timezone
import pytz # For timezone handling

from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry, get_user_preferences
from services import sync_service # Import the whole module
from services.config_manager import update_preferences # To update last trigger date
from services.agent_state_manager import clear_notified_event_ids, get_agent_state # For daily cleanup
from services.task_query_service import _format_task_line, _sort_tasks # Use helper for formatting

# Define time window for fetching context for routines (e.g., yesterday to 14 days ahead)
ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14

def get_local_time(user_timezone_str):
    """Gets the current time in the user's specified timezone."""
    fn_name = "get_local_time"
    if not user_timezone_str: user_timezone_str = 'UTC' # Default if None/empty
    try:
        user_tz = pytz.timezone(user_timezone_str)
        return datetime.now(user_tz)
    except pytz.UnknownTimeZoneError:
        log_warning("routine_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC.")
        return datetime.now(pytz.utc)
    except Exception as e:
        log_error("routine_service", fn_name, f"Error getting local time for tz '{user_timezone_str}'", e)
        return datetime.now(pytz.utc) # Default to UTC on error


def generate_morning_summary(user_id, context):
    """Generates the morning summary message including GCal events and WT tasks."""
    fn_name = "generate_morning_summary"
    prefs = get_user_preferences(user_id)
    user_tz_str = prefs.get("TimeZone", "UTC") if prefs else "UTC"
    user_tz = pytz.timezone(user_tz_str) # Get timezone object
    now_local = get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    log_info("routine_service", fn_name, f"Generating morning summary for user {user_id} for date {today_local_str}")

    items_today = []
    for item in context:
        item_date_local_str = None
        start_dt_str = item.get("gcal_start_datetime")
        is_all_day = item.get("is_all_day", False)

        if start_dt_str: # Prioritize GCal time
             try:
                 # Parse ISO string (can be date or datetime, potentially with Z or offset)
                 if 'T' in start_dt_str: # It's likely a datetime
                      dt_aware = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                      item_date_local_str = dt_aware.astimezone(user_tz).strftime("%Y-%m-%d")
                 elif len(start_dt_str) == 10: # It's likely just a date (all-day event)
                      item_date_local_str = start_dt_str
             except ValueError:
                 log_warning("routine_service", fn_name, f"Could not parse gcal_start_datetime '{start_dt_str}' for item {item.get('event_id')}")
                 pass # Ignore parse errors for this item's date check
        elif item.get("date"): # Fallback to WT date field
             item_date_local_str = item.get("date")

        # Check if the derived local date matches today
        if item_date_local_str == today_local_str:
             # Include tasks, reminders, and external events if they are not completed/cancelled
             current_status = item.get("status", "pending") # Default WT items to pending
             item_type = item.get("type")
             # Include external events, or WT items not completed/cancelled
             if item_type == "external_event" or current_status not in ["completed", "cancelled"]:
                items_today.append(item)

    if not items_today:
        return f"Good morning! ☀️ Looks like a clear schedule today ({today_local_str}). Anything you'd like to add?"

    # Sort items for display
    sorted_items = _sort_tasks(items_today) # Use the existing sort helper

    message_lines = [f"Good morning! ☀️ Here's your overview for today, {today_local_str}:"]
    for item in sorted_items:
        # Pass user_tz_str for potential use in formatting (though _format_task_line needs update)
        # Add timezone info to item temporarily for formatting function
        item['_user_timezone_for_display'] = user_tz_str
        formatted_line = _format_task_line(item)
        message_lines.append(f"- {formatted_line}")
        item.pop('_user_timezone_for_display', None) # Clean up temporary key

    message_lines.append("\nHave a productive day!")
    return "\n".join(message_lines)


def generate_evening_review(user_id, context):
    """Generates the evening review message, listing only active WT items for the day."""
    fn_name = "generate_evening_review"
    prefs = get_user_preferences(user_id)
    user_tz_str = prefs.get("TimeZone", "UTC") if prefs else "UTC"
    user_tz = pytz.timezone(user_tz_str)
    now_local = get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    log_info("routine_service", fn_name, f"Generating evening review for user {user_id} for date {today_local_str}")

    wt_items_today_active = []
    for item in context:
        item_type = item.get("type")
        # *** Filter for WT items ONLY ***
        if item_type in ["task", "reminder"]:
            item_date_local_str = None
            start_dt_str = item.get("gcal_start_datetime")
            is_all_day = item.get("is_all_day", False)

            if start_dt_str:
                 try:
                     if 'T' in start_dt_str:
                          dt_aware = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                          item_date_local_str = dt_aware.astimezone(user_tz).strftime("%Y-%m-%d")
                     elif len(start_dt_str) == 10:
                          item_date_local_str = start_dt_str
                 except ValueError: pass
            elif item.get("date"):
                 item_date_local_str = item.get("date")

            # Include if it's for today AND its status is pending or in progress
            if item_date_local_str == today_local_str and item.get("status") in ["pending", "in_progress", "in progress"]:
                 wt_items_today_active.append(item)

    if not wt_items_today_active:
        return f"Good evening! 👋 No active tasks or reminders were scheduled for today ({today_local_str}). Time to relax or plan for tomorrow?"

    # Sort items for display
    sorted_items = _sort_tasks(wt_items_today_active)

    message_lines = [f"Good evening! 👋 Let's review your day ({today_local_str}). Here are the tasks/reminders still marked as active:"]
    for i, item in enumerate(sorted_items):
        # Pass user_tz_str for potential use in formatting
        item['_user_timezone_for_display'] = user_tz_str
        formatted_line = _format_task_line(item)
        message_lines.append(f"{i+1}. {formatted_line}")
        item.pop('_user_timezone_for_display', None)

    message_lines.append("\nHow did it go? You can update items by replying (e.g., 'complete 1', 'cancel 2') or add new ones for tomorrow.")
    return "\n".join(message_lines)


def check_routine_triggers(): # Remove -> List[Tuple[str, str]] hint if not allowed
    """
    Scheduled job function. Checks all users if morning/evening routines should run.
    Calls sync service before generating summaries.
    Returns a list of (user_id, message_content) tuples for messages to be sent.
    """
    fn_name = "check_routine_triggers"
    log_info("routine_service", fn_name, "Running scheduled check for routine triggers...")
    messages_to_send = [] # Initialize list to store messages

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

                now_local = get_local_time(user_tz_str)
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
                            # --- REPLACE send_message WITH append ---
                            messages_to_send.append((user_id, summary_msg))
                            # ----------------------------------------
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
                            # --- REPLACE send_message WITH append ---
                            messages_to_send.append((user_id, review_msg))
                            # ----------------------------------------
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
    return messages_to_send # Return the list

def daily_cleanup():
    """Scheduled job to perform daily cleanup tasks (e.g., reset notification tracker)."""
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
                    # Check if state exists before trying to clear
                    if get_agent_state(user_id): # Use get_agent_state to check existence
                       clear_notified_event_ids(user_id)
                       cleared_count += 1
                    # else: No state in memory, nothing to clear
                # else: Cannot clear if state manager not imported
            except Exception as e:
                 log_error("routine_service", fn_name, f"Error during daily cleanup for user {user_id}", e)

        log_info("routine_service", fn_name, f"Daily cleanup finished. Cleared notification sets for {cleared_count} users.")

    except Exception as e:
        log_error("routine_service", fn_name, "Error during daily cleanup main loop", e)

# --- END OF FILE services/routine_service.py ---