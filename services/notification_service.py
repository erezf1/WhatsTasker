# --- START OF FILE services/notification_service.py ---

import traceback
from datetime import datetime, timedelta, timezone
import pytz
from typing import Dict, List # <--- ADD Dict and List HERE (List might be needed elsewhere)

from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry
from services.sync_service import get_synced_context_snapshot # To get current items
from services.agent_state_manager import get_notified_event_ids, add_notified_event_id
from bridge.request_router import send_message 
from services.task_manager import _parse_duration_to_minutes # Re-use for lead time
import re # <--- ADDED FOR TITLE PARSING IN NOTIFICATION

try:
    from tools.activity_db import update_task_fields # For marking WT items as notified
    DB_IMPORTED = True
except ImportError:
    DB_IMPORTED = False
    def update_task_fields(*args, **kwargs): return False 
    log_error("notification_service", "import", "Failed to import activity_db.update_task_fields. WT item notification status won't be saved.")

# Minimal translations for notification messages
NOTIFICATION_TRANSLATIONS = {
    "en": {
        "reminder_starts_soon": "🔔 Reminder: '{title}' is starting soon at {time_str}.",
        "session_starts_soon": "🔔 Heads up: Your work session for '{title}' starts soon at {time_str}."
    },
    "he": {
        "reminder_starts_soon": "🔔 תזכורת: '{title}' מתחיל/ה בקרוב בשעה {time_str}.",
        "session_starts_soon": "🔔 שים/י לב: זמן העבודה שלך עבור '{title}' מתחיל בקרוב בשעה {time_str}."
    }
}

def _get_notification_translation(lang: str, key: str, default_lang: str = "en") -> str:
    """Fetches a translation string for notifications."""
    return NOTIFICATION_TRANSLATIONS.get(lang, {}).get(key) or \
           NOTIFICATION_TRANSLATIONS.get(default_lang, {}).get(key, key)


DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES = 15

def generate_event_notification_message(event_data: Dict, user_timezone_str: str, user_lang: str) -> str | None: # Corrected: Dict
    """
    Formats a notification message for an event, in the user's language and timezone.
    Differentiates message for Reminders vs. Task sessions.
    """
    fn_name = "generate_event_notification_message"
    title = event_data.get('title', '(No Title)')
    start_time_iso = event_data.get('gcal_start_datetime')
    item_type = event_data.get('type', 'event') 

    if not start_time_iso or 'T' not in start_time_iso : 
        # log_warning("notification_service", fn_name, f"Cannot generate notification for '{title}' - missing specific start time.") # Can be verbose
        return None
    try:
        user_tz = pytz.utc
        try:
            if user_timezone_str: user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError:
            log_warning("notification_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC for notification for '{title}'.")
            user_timezone_str = "UTC" 
        
        dt_aware = datetime.fromisoformat(start_time_iso.replace('Z', '+00:00'))
        dt_local = dt_aware.astimezone(user_tz)
        time_str_local = dt_local.strftime('%H:%M %Z') 

        template_key = "reminder_starts_soon" 
        base_title = title
        if item_type == "task": 
            template_key = "session_starts_soon"
            match = re.match(r"Work: (.*?) \[\d+/\d+\]", title)
            if match: base_title = match.group(1).strip()
        
        message_template = _get_notification_translation(user_lang, template_key)
        return message_template.format(title=base_title, time_str=time_str_local)

    except (ValueError, TypeError) as parse_err:
        log_error("notification_service", fn_name, f"Error parsing/converting start time '{start_time_iso}' for '{title}'. Error: {parse_err}", parse_err)
        return None
    except Exception as e:
        log_error("notification_service", fn_name, f"General error formatting notification for '{title}': {e}", e)
        return None

def check_event_notifications():
    """
    Scheduled job: checks users for upcoming events needing notification.
    Sends notifications in user's language and updates DB for WT items.
    """
    fn_name = "check_event_notifications"
    now_utc = datetime.now(timezone.utc)

    try:
        registry = get_registry() # Still need this to get the list of all user IDs
        if not registry:
            # log_info("notification_service", fn_name, "User registry is empty. No users to check for notifications.") # Can be verbose
            return
        user_ids = list(registry.keys())

        # log_info("notification_service", fn_name, f"Checking notifications for {len(user_ids)} users.") # Can be verbose

        for user_id in user_ids:
            prefs = None # Initialize prefs to None for each user iteration
            try:
                # --- MODIFIED SECTION START ---
                agent_state = get_agent_state(user_id) # Get full agent state from memory
                if not agent_state:
                    # log_warning("notification_service", fn_name, f"Agent state not found in memory for {user_id}, skipping notifications.") # Verbose
                    continue
                
                prefs = agent_state.get("preferences") # Get preferences from the in-memory agent state
                # --- MODIFIED SECTION END ---

                if not prefs or prefs.get("status") != "active" or not prefs.get("Calendar_Enabled"):
                    # log_info("notification_service", fn_name, f"Skipping notifications for {user_id} (inactive, GCal disabled, or no prefs).") # Verbose
                    continue

                user_lang = prefs.get("Preferred_Language", "en")
                user_tz_str = prefs.get("TimeZone", "UTC")
                # ... rest of the function remains the same, using the 'prefs' dictionary obtained from agent_state ...
                
                lead_time_str = prefs.get("Notification_Lead_Time", f"{DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES}m")
                
                lead_time_minutes = _parse_duration_to_minutes(lead_time_str)
                if lead_time_minutes is None:
                    log_warning("notification_service", fn_name, f"Invalid Notification_Lead_Time '{lead_time_str}' for {user_id}. Using default.", user_id=user_id)
                    lead_time_minutes = DEFAULT_NOTIFICATION_LEAD_TIME_MINUTES
                
                notification_window_start_utc = now_utc 
                notification_window_end_utc = now_utc + timedelta(minutes=lead_time_minutes) 
                
                today_utc_str = now_utc.strftime("%Y-%m-%d")
                tomorrow_utc_str = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
                
                # Assuming sync_service.get_synced_context_snapshot also uses in-memory GCal API if available
                aggregated_context = sync_service.get_synced_context_snapshot(user_id, today_utc_str, tomorrow_utc_str)
                if not aggregated_context: continue

                notified_today_set = get_notified_event_ids(user_id) 

                for item in aggregated_context:
                    event_id = item.get("event_id")
                    start_time_iso = item.get("gcal_start_datetime")

                    if not event_id or not start_time_iso or 'T' not in start_time_iso: continue 
                    if event_id in notified_today_set: continue 

                    try:
                        start_dt_aware_utc = datetime.fromisoformat(start_time_iso.replace('Z', '+00:00'))
                        
                        if notification_window_start_utc < start_dt_aware_utc <= notification_window_end_utc:
                            notification_message = generate_event_notification_message(item, user_tz_str, user_lang)
                            if notification_message:
                                send_message(user_id, notification_message)
                                add_notified_event_id(user_id, event_id) 
                                
                                if item.get("type") in ["task", "reminder"] and DB_IMPORTED and not str(item.get("event_id","")).startswith("local_"):
                                    sent_time_utc_str = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
                                    db_update_success = update_task_fields(event_id, {"internal_reminder_sent": sent_time_utc_str})
                                    if not db_update_success: # Only log if it failed
                                        log_warning("notification_service", fn_name, f"Failed to update internal_reminder_sent in DB for WT item {event_id}", user_id=user_id)
                            else:
                                log_warning("notification_service", fn_name, f"Failed to generate notification message for event {event_id}.", user_id=user_id)
                    
                    except ValueError:
                        log_warning("notification_service", fn_name, f"Could not parse start time '{start_time_iso}' for event {event_id} of user {user_id}. Skipping.", user_id=user_id)
                    except Exception as item_err:
                        log_error("notification_service", fn_name, f"Error processing item {event_id} for user {user_id}", item_err, user_id=user_id)
            
            except Exception as user_err:
                user_context = user_id if prefs else "UnknownUser (no prefs)" # Use updated prefs for context
                log_error("notification_service", fn_name, f"Error processing notifications for user {user_id}", user_err, user_id=user_context)
    
    except Exception as main_err:
        log_error("notification_service", fn_name, f"General error during notification check run", main_err)

# --- END OF FILE services/notification_service.py ---