# --- START OF FILE services/notification_service.py ---
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
from tools import metadata_store
NOTIFICATION_CHECK_INTERVAL_MINUTES = 5

DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES = 15

def generate_event_notification_message(event_data, user_timezone_str="UTC"):
    """
    Formats a simple notification message for an event, converting the start time
    to the user's local timezone.

    Args:
        event_data (dict): Dictionary containing item data, expects 'gcal_start_datetime' and 'title'.
        user_timezone_str (str): The user's Olson timezone string (e.g., 'America/New_York').
                                 Defaults to 'UTC' if not provided or invalid.

    Returns:
        str: Formatted notification string or None if formatting fails.
    """
    fn_name = "generate_event_notification_message"
    title = event_data.get('title', '(No Title)')
    start_time_str = event_data.get('gcal_start_datetime')

    if not start_time_str:
        log_warning("notification_service", fn_name, f"Cannot generate notification for event '{title}' - missing gcal_start_datetime.")
        return None

    try:
        # Determine User Timezone Object
        user_tz = pytz.utc # Default to UTC
        try:
            if user_timezone_str:
                 user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError:
            log_warning("notification_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC for notification message for event '{title}'.")
            user_timezone_str = "UTC" # Update string for consistency

        # Parse the aware datetime string from GCal
        dt_aware = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))

        # Convert to user's local timezone
        dt_local = dt_aware.astimezone(user_tz)

        # Format the time string using local time and timezone abbreviation
        time_str = dt_local.strftime('%H:%M %Z') # e.g., 10:00 EDT

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
    """
    fn_name = "check_event_notifications"
    log_info("notification_service", fn_name, "Running scheduled check for event notifications...")
    now_utc = datetime.now(timezone.utc)

    try:
        registry = get_registry()
        if not registry:
            log_warning("notification_service", fn_name, "User registry is empty. Skipping check.")
            return

        user_ids = list(registry.keys())
        log_info("notification_service", fn_name, f"Checking notifications for {len(user_ids)} users.")

        for user_id in user_ids:
            try:
                prefs = get_user_preferences(user_id)
                if not prefs or not prefs.get("Calendar_Enabled"):
                    # log_info("notification_service", fn_name, f"Skipping user {user_id}: Calendar not enabled.")
                    continue

                lead_time_str = prefs.get("Notification_Lead_Time", "15m")
                lead_time_minutes = _parse_duration_to_minutes(lead_time_str)
                if lead_time_minutes is None:
                     log_warning("notification_service", fn_name, f"Invalid Notification_Lead_Time '{lead_time_str}' for user {user_id}. Using default.")
                     lead_time_minutes = DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES

                # Define the window to check: from now up to lead_time + buffer (e.g., scheduler interval)
                # Fetch slightly ahead to avoid race conditions with scheduler timing
                check_end_utc = now_utc + timedelta(minutes=lead_time_minutes + NOTIFICATION_CHECK_INTERVAL_MINUTES)
                start_date_str = now_utc.strftime("%Y-%m-%d")
                end_date_str = check_end_utc.strftime("%Y-%m-%d")

                # Get combined context (includes external events)
                # Note: sync_service currently filters metadata by 'date' field, which might miss GCal events
                # scheduled far out if the 'date' field wasn't set. Need refinement?
                # For notifications, fetching a narrow window directly from GCal might be better.
                # Let's stick to sync_service for now.
                aggregated_context = get_synced_context_snapshot(user_id, start_date_str, end_date_str)
                if not aggregated_context:
                    # log_info("notification_service", fn_name, f"No context found for user {user_id} in window.")
                    continue

                notified_today_set = get_notified_event_ids(user_id) # Get set of IDs already notified today
                # log_info("notification_service", fn_name, f"User {user_id} notified set size: {len(notified_today_set)}")

                for item in aggregated_context:
                    event_id = item.get("event_id")
                    start_time_iso = item.get("gcal_start_datetime")

                    if not event_id or not start_time_iso:
                        # log_warning("notification_service", fn_name, f"Skipping item for user {user_id} due to missing ID or start time: {item.get('title')}")
                        continue

                    # Check if already notified today
                    if event_id in notified_today_set:
                        # log_info("notification_service", fn_name, f"Event {event_id} already notified today for user {user_id}.")
                        continue

                    try:
                        # Parse the start time (should be timezone-aware)
                        start_dt_aware = datetime.fromisoformat(start_time_iso.replace('Z', '+00:00'))

                        # Calculate notification trigger time (UTC)
                        notification_trigger_time = start_dt_aware - timedelta(minutes=lead_time_minutes)

                        # Check if the notification time is now or in the past
                        if notification_trigger_time <= now_utc:
                             log_info("notification_service", fn_name, f"Triggering notification for user {user_id}, event: {event_id} ('{item.get('title')}')")

                             # Add user timezone to item data for formatting
                             item['user_timezone'] = prefs.get("TimeZone", "UTC")
                             notification_message = generate_event_notification_message(item)

                             if notification_message:
                                 send_message(user_id, notification_message)
                                 # Mark as notified for today in memory
                                 add_notified_event_id(user_id, event_id)
                                 log_info("notification_service", fn_name, f"Sent notification and marked as notified for event {event_id}, user {user_id}")

                                 # Mark WT items as notified in metadata store as well (for persistence)
                                 if item.get("type") in ["task", "reminder"]:
                                     try:
                                         sent_time_utc = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
                                         metadata_update = {"internal_reminder_sent": sent_time_utc}
                                         # Use metadata_store directly - TaskManager doesn't have a direct update method for this
                                         current_meta = metadata_store.get_event_metadata(event_id)
                                         if current_meta:
                                             current_meta.update(metadata_update)
                                             meta_to_save = {fn: current_meta.get(fn) for fn in metadata_store.FIELDNAMES}
                                             metadata_store.save_event_metadata(meta_to_save)
                                             log_info("notification_service", fn_name, f"Updated internal_reminder_sent in metadata for WT item {event_id}")
                                         else:
                                              log_warning("notification_service", fn_name, f"Could not find metadata for WT item {event_id} to update sent status.")
                                     except Exception as meta_update_err:
                                          log_error("notification_service", fn_name, f"Failed to update metadata sent status for {event_id}", meta_update_err)

                             else:
                                 log_warning("notification_service", fn_name, f"Failed to generate notification message for event {event_id}.")

                    except ValueError:
                        log_warning("notification_service", fn_name, f"Could not parse start time '{start_time_iso}' for event {event_id}. Skipping notification.")
                    except Exception as item_err:
                        log_error("notification_service", fn_name, f"Error processing item {event_id} for user {user_id}", item_err)

            except Exception as user_err:
                 log_error("notification_service", fn_name, f"Error processing notifications for user {user_id}", user_err)
                 # Continue to the next user

    except Exception as main_err:
        log_error("notification_service", fn_name, f"General error during notification check run", main_err)

    log_info("notification_service", fn_name, "Finished scheduled check for event notifications.")

# --- END OF FILE services/notification_service.py ---