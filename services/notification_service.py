# --- START OF FULL services/notification_service.py ---
"""
Handles checking for upcoming events and sending notifications.
Uses an in-memory set within agent_state to track sent notifications for the day.
"""
import traceback
from datetime import datetime, timedelta, timezone
import pytz

from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry, get_user_preferences
from services.sync_service import get_synced_context_snapshot
from services.agent_state_manager import get_notified_event_ids, add_notified_event_id
from bridge.request_router import send_message # Direct import for sending
from services.task_manager import _parse_duration_to_minutes # For parsing lead time

try:
    from tools.activity_db import update_task_fields
    DB_IMPORTED = True
except ImportError:
    DB_IMPORTED = False
    def update_task_fields(*args, **kwargs): return False # Dummy
    log_error("notification_service", "import", "Failed to import activity_db.update_task_fields")
# --- End Imports ---

NOTIFICATION_CHECK_INTERVAL_MINUTES = 5
DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES = 15

def generate_event_notification_message(event_data, user_timezone_str="UTC"):
    """
    Formats a simple notification message for an event, converting the start time
    to the user's local timezone.
    """
    fn_name = "generate_event_notification_message"
    title = event_data.get('title', '(No Title)')
    start_time_str = event_data.get('gcal_start_datetime')
    if not start_time_str:
        log_warning("notification_service", fn_name, f"Cannot generate notification for event '{title}' - missing gcal_start_datetime.")
        return None
    try:
        user_tz = pytz.utc
        try:
            if user_timezone_str: user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError:
            log_warning("notification_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC for notification message for event '{title}'.")
            user_timezone_str = "UTC"
        dt_aware = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        dt_local = dt_aware.astimezone(user_tz)
        time_str = dt_local.strftime('%H:%M %Z')
        return f"🔔 Reminder: '{title}' is starting soon at {time_str}."
    except (ValueError, TypeError) as parse_err:
        log_error("notification_service", fn_name, f"Error parsing/converting start time '{start_time_str}' for event '{title}'. Error: {parse_err}", parse_err)
        return None
    except Exception as e:
        log_error("notification_service", fn_name, f"General error formatting notification for event '{title}': {e}", e)
        return None

def check_event_notifications():
    """
    Scheduled job function. Checks all users for upcoming events needing notification.
    Updates 'internal_reminder_sent' in the database for notified WT items.
    """
    fn_name = "check_event_notifications"
    log_info("notification_service", fn_name, "Running scheduled check for event notifications...")
    now_utc = datetime.now(timezone.utc)

    # --- Outermost Try ---
    try:
        registry = get_registry()
        if not registry:
            log_warning("notification_service", fn_name, "User registry is empty. Skipping check.")
            return

        user_ids = list(registry.keys())
        log_info("notification_service", fn_name, f"Checking notifications for {len(user_ids)} users.")

        for user_id in user_ids:
            prefs = None
            # --- Inner Try (User Level) ---
            try:
                prefs = get_user_preferences(user_id)
                if not prefs or prefs.get("status") != "active" or not prefs.get("Calendar_Enabled"):
                    continue

                lead_time_str = prefs.get("Notification_Lead_Time", "15m")
                lead_time_minutes = _parse_duration_to_minutes(lead_time_str)
                if lead_time_minutes is None:
                    log_warning("notification_service", fn_name, f"Invalid Notification_Lead_Time '{lead_time_str}' for user {user_id}. Using default.", user_id=user_id)
                    lead_time_minutes = DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES

                notification_window_end_utc = now_utc + timedelta(minutes=lead_time_minutes)
                today_date_str = now_utc.strftime("%Y-%m-%d")
                aggregated_context = get_synced_context_snapshot(user_id, today_date_str, today_date_str)
                if not aggregated_context: continue

                notified_today_set = get_notified_event_ids(user_id)

                for item in aggregated_context:
                    event_id = item.get("event_id")
                    start_time_iso = item.get("gcal_start_datetime")

                    if not event_id or not start_time_iso or 'T' not in start_time_iso: continue
                    if event_id in notified_today_set: continue

                    # --- Innermost Try (Item Level) ---
                    try:
                        start_dt_aware = datetime.fromisoformat(start_time_iso.replace('Z', '+00:00'))

                        # Corrected check logic
                        if start_dt_aware > now_utc and start_dt_aware <= notification_window_end_utc:
                            log_info("notification_service", fn_name, f"Triggering notification for user {user_id}, event: {event_id} ('{item.get('title')}')")

                            notification_message = generate_event_notification_message(item, prefs.get("TimeZone", "UTC"))

                            if notification_message:
                                send_message(user_id, notification_message)
                                add_notified_event_id(user_id, event_id)
                                log_info("notification_service", fn_name, f"Sent notification and marked as notified (memory) for event {event_id}, user {user_id}")

                                if item.get("type") in ["task", "reminder"] and DB_IMPORTED:
                                    sent_time_utc_str = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
                                    update_payload = {"internal_reminder_sent": sent_time_utc_str}
                                    db_update_success = update_task_fields(event_id, update_payload)
                                    if db_update_success:
                                        log_info("notification_service", fn_name, f"Updated internal_reminder_sent in DB for WT item {event_id}")
                                    else:
                                        log_warning("notification_service", fn_name, f"Failed to update internal_reminder_sent in DB for WT item {event_id}", user_id=user_id)
                            else:
                                log_warning("notification_service", fn_name, f"Failed to generate notification message for event {event_id}.", user_id=user_id)

                    except ValueError:
                        log_warning("notification_service", fn_name, f"Could not parse start time '{start_time_iso}' for event {event_id}. Skipping.", user_id=user_id)
                    except Exception as item_err:
                        log_error("notification_service", fn_name, f"Error processing item {event_id} for user {user_id}", item_err, user_id=user_id)
                    # --- End Innermost Try ---

            except Exception as user_err: # <-- Correctly indented for Inner Try
                user_context = user_id if prefs else f"Unknown User (Error before prefs load)"
                log_error("notification_service", fn_name, f"Error processing notifications for user {user_id}", user_err, user_id=user_context)
            # --- End Inner Try ---

    # --- <<< CORRECTED INDENTATION FOR FINAL EXCEPT >>> ---
    except Exception as main_err: # <-- Correctly indented for Outermost Try
        log_error("notification_service", fn_name, f"General error during notification check run", main_err)
    # --- <<< END CORRECTION >>> ---

    # --- Correctly indented final log ---
    log_info("notification_service", fn_name, "Finished scheduled check for event notifications.")

# --- END OF FILE services/notification_service.py ---