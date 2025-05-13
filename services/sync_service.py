# --- START OF REFACTORED services/sync_service.py ---
"""
Provides functionality to get a combined view of WhatsTasker-managed items (DB)
and external Google Calendar events (API) for a specific user and time period.
Does NOT modify the persistent DB for external events found only in GCal.
Updates DB records for WT items if GCal data has changed for that item.
"""
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import json # Added for ensuring session_event_ids is list

# Central logger
from tools.logger import log_info, log_error, log_warning

# Database access module
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "activity_db not found. Sync service disabled.", None)
    DB_IMPORTED = False
    class activity_db:
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
        @staticmethod
        def add_or_update_task(*args, **kwargs): return False
        @staticmethod
        def get_task(*args, **kwargs): return None # Added for retry logic

# User Manager (to get agent state for Calendar API)
try:
    from users.user_manager import get_agent
    USER_MANAGER_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import user_manager.get_agent")
    USER_MANAGER_IMPORTED = False
    def get_agent(*args, **kwargs): return None

# Agent State Manager (to update in-memory context after DB update)
try:
    from services.agent_state_manager import update_task_in_context
    AGENT_STATE_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import agent_state_manager functions.")
    AGENT_STATE_IMPORTED = False
    def update_task_in_context(*args, **kwargs): pass

# Google Calendar API (for type checking and fetching events)
try:
    from tools.google_calendar_api import GoogleCalendarAPI, HttpError # <-- Import HttpError
    # Import other specific exceptions if needed, e.g., from httplib2 or ssl
    # from httplib2 import ServerNotFoundError # Example
    # import ssl # Example
    GCAL_API_IMPORTED = True
except ImportError:
    log_warning("sync_service", "import", "GoogleCalendarAPI not found. Sync will only show DB tasks.")
    GoogleCalendarAPI = None
    HttpError = Exception # Fallback
    GCAL_API_IMPORTED = False

# --- Tenacity for Retries ---
try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    TENACITY_IMPORTED = True
except ImportError:
    log_warning("sync_service", "import", "Tenacity library not found. GCal fetch retries will be disabled.")
    TENACITY_IMPORTED = False
    # Dummy decorator if tenacity is not available
    def retry(*args, **kwargs):
        def decorator(func):
            def wrapper(*f_args, **f_kwargs):
                return func(*f_args, **f_kwargs)
            return wrapper
        return decorator
    # Dummy exception types for the decorator
    class ServerNotFoundError(Exception): pass # Dummy
    class SSLError(Exception): pass # Dummy
    # HttpError is already defined as Exception if google libs fail

# --- Retryable Exceptions for GCal ---
# Define a tuple of exceptions that should trigger a retry
# This might need adjustment based on the exact exceptions seen from google-api-python-client
# and its underlying httplib2 or requests library.
GCAL_RETRYABLE_EXCEPTIONS = (
    HttpError, # General Google API HTTP errors (check status codes if needed)
    TimeoutError, # Standard Python Timeout
    # ServerNotFoundError, # If using httplib2 directly and importing its specific error
    # ssl.SSLError, # If SSL errors are common and potentially transient
    ConnectionResetError, # Can be transient
    ConnectionAbortedError, # Can be transient
    # Add other transient network-related exceptions here
)
if 'RemoteDisconnected' in __builtins__: # http.client.RemoteDisconnected
    GCAL_RETRYABLE_EXCEPTIONS += (getattr(__builtins__, 'RemoteDisconnected'),)
elif 'http' in globals() and 'client' in dir(globals()['http']) and 'RemoteDisconnected' in dir(globals()['http'].client):
    GCAL_RETRYABLE_EXCEPTIONS += (globals()['http'].client.RemoteDisconnected,)


# --- Retry-enabled GCal Fetch ---
if TENACITY_IMPORTED:
    @retry(
        stop=stop_after_attempt(3), # Try up to 3 times
        wait=wait_exponential(multiplier=1, min=2, max=10), # Exponential backoff: 2s, 4s, 8s... max 10s
        retry=retry_if_exception_type(GCAL_RETRYABLE_EXCEPTIONS),
        reraise=False # If all retries fail, log error and return default (empty list)
    )
    def _fetch_gcal_events_with_retry(calendar_api_instance: GoogleCalendarAPI, start_date_str: str, end_date_str: str, user_id_for_log: str) -> List[Dict]:
        fn_name = "_fetch_gcal_events_with_retry"
        # Log attempt number if tenacity context is available (optional)
        # current_attempt = getattr(retry_state, 'attempt_number', 1) if 'retry_state' in locals() else 1
        # log_info("sync_service", fn_name, f"Attempting GCal event fetch (try if tenacity available) for user {user_id_for_log}")
        if calendar_api_instance:
            return calendar_api_instance.list_events(start_date_str, end_date_str)
        return []
else: # Fallback if tenacity is not installed
    def _fetch_gcal_events_with_retry(calendar_api_instance: GoogleCalendarAPI, start_date_str: str, end_date_str: str, user_id_for_log: str) -> List[Dict]:
        fn_name = "_fetch_gcal_events_with_retry_no_tenacity"
        log_info("sync_service", fn_name, f"Attempting GCal event fetch (no tenacity retry) for user {user_id_for_log}")
        if calendar_api_instance:
            try:
                return calendar_api_instance.list_events(start_date_str, end_date_str)
            except GCAL_RETRYABLE_EXCEPTIONS as e: # Catch the same exceptions tenacity would
                log_error("sync_service", fn_name, f"GCal fetch failed (no retry) for {user_id_for_log}", e, user_id=user_id_for_log)
                return [] # Return empty on error
            except Exception as e: # Catch any other unexpected error
                log_error("sync_service", fn_name, f"Unexpected GCal fetch error (no retry) for {user_id_for_log}", e, user_id=user_id_for_log)
                return []
        return []


def get_synced_context_snapshot(user_id: str, start_date_str: str, end_date_str: str) -> List[Dict]:
    """
    Fetches WT tasks (DB) and GCal events (API) for a period, merges them,
    identifies external events, and returns a combined list of dictionaries.
    Updates the DB record for a WT item if its corresponding GCal event changed.
    Does not persist external events found only in GCal into the tasks table.
    """
    fn_name = "get_synced_context_snapshot"
    #log_info("sync_service", fn_name, f"Generating synced context for user {user_id}, range: {start_date_str} to {end_date_str}")

    if not DB_IMPORTED:
        log_error("sync_service", fn_name, "Database module not available.", user_id=user_id)
        return []

    calendar_api = None
    if USER_MANAGER_IMPORTED and GCAL_API_IMPORTED and GoogleCalendarAPI is not None:
        agent_state = get_agent(user_id)
        if agent_state:
            calendar_api_maybe = agent_state.get("calendar")
            if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
                calendar_api = calendar_api_maybe

    gcal_events_list = []
    if calendar_api:
        try:
            # --- USE THE RETRY-ENABLED FETCH FUNCTION ---
            gcal_events_list = _fetch_gcal_events_with_retry(calendar_api, start_date_str, end_date_str, user_id)
            # --------------------------------------------
            log_info("sync_service", fn_name, f"Fetched {len(gcal_events_list)} GCal events for {user_id} (after retries if any).")
        except Exception as e: # This catch is if _fetch_gcal_events_with_retry has reraise=True or unforeseen error
            log_error("sync_service", fn_name, f"Final unhandled error fetching GCal events for {user_id}", e, user_id=user_id)
            # Continue without GCal events if fetch ultimately fails
    else:
        log_info("sync_service", fn_name, f"GCal API not available or inactive for {user_id}, skipping GCal fetch.")

    wt_tasks_list = []
    try:
        wt_tasks_list = activity_db.list_tasks_for_user(
            user_id=user_id,
            date_range=(start_date_str, end_date_str)
        )
        log_info("sync_service", fn_name, f"Fetched {len(wt_tasks_list)} WT tasks from DB for {user_id} in range.")
    except Exception as e:
        log_error("sync_service", fn_name, f"Error fetching WT tasks from DB for {user_id}", e, user_id=user_id)

    gcal_events_map = {e['event_id']: e for e in gcal_events_list if e.get('event_id')}
    wt_tasks_map = {t['event_id']: t for t in wt_tasks_list if t.get('event_id')}

    aggregated_context_list: List[Dict[str, Any]] = []
    processed_wt_ids = set()

    for event_id, gcal_data in gcal_events_map.items():
        if event_id in wt_tasks_map:
            processed_wt_ids.add(event_id)
            task_data = wt_tasks_map[event_id]
            merged_data = task_data.copy()
            needs_db_update = False

            gcal_start = gcal_data.get("gcal_start_datetime")
            gcal_end = gcal_data.get("gcal_end_datetime")
            gcal_title = gcal_data.get("title")
            gcal_desc = gcal_data.get("description")

            if gcal_start != merged_data.get("gcal_start_datetime"):
                merged_data["gcal_start_datetime"] = gcal_start
                needs_db_update = True
            if gcal_end != merged_data.get("gcal_end_datetime"):
                merged_data["gcal_end_datetime"] = gcal_end
                needs_db_update = True
            if gcal_title and not merged_data.get("title", "").strip():
                 merged_data["title"] = gcal_title
                 needs_db_update = True
            if gcal_desc and not merged_data.get("description", "").strip():
                 merged_data["description"] = gcal_desc
                 needs_db_update = True

            if needs_db_update:
                log_info("sync_service", fn_name, f"GCal data changed for WT item {event_id}. Updating DB.")
                try:
                    if isinstance(merged_data.get("session_event_ids"), str):
                        try: merged_data["session_event_ids"] = json.loads(merged_data["session_event_ids"])
                        except: merged_data["session_event_ids"] = []

                    update_success = activity_db.add_or_update_task(merged_data)
                    if update_success and AGENT_STATE_IMPORTED:
                        updated_data_from_db = activity_db.get_task(event_id)
                        if updated_data_from_db: update_task_in_context(user_id, event_id, updated_data_from_db)
                    elif not update_success:
                         log_error("sync_service", fn_name, f"Failed DB update for WT item {event_id} after GCal merge.", user_id=user_id)
                except Exception as save_err:
                     log_error("sync_service", fn_name, f"Unexpected error saving updated metadata for WT item {event_id}", save_err, user_id=user_id)
            aggregated_context_list.append(merged_data)
        else:
            external_event_data = gcal_data.copy()
            external_event_data["type"] = "external_event"
            external_event_data["user_id"] = user_id
            external_event_data.setdefault("status", None)
            aggregated_context_list.append(external_event_data)

    for event_id, task_data in wt_tasks_map.items():
        if event_id not in processed_wt_ids:
            #log_info("sync_service", fn_name, f"Including WT item {event_id} (status: {task_data.get('status')}) not in GCal window.")
            if isinstance(task_data.get("session_event_ids"), str):
                 try: task_data["session_event_ids"] = json.loads(task_data["session_event_ids"])
                 except: task_data["session_event_ids"] = []
            aggregated_context_list.append(task_data)

    sorted_aggregated_context = _sort_tasks(aggregated_context_list)
    return sorted_aggregated_context

def perform_full_sync(user_id: str):
    log_warning("sync_service", "perform_full_sync", f"Full two-way sync not implemented. User: {user_id}")
    pass

def _sort_tasks(task_list: List[Dict]) -> List[Dict]:
    fn_name = "_sort_tasks_sync"
    def sort_key(item):
        gcal_start = item.get("gcal_start_datetime")
        if gcal_start and isinstance(gcal_start, str):
             try:
                 if 'T' in gcal_start: dt_aware = datetime.fromisoformat(gcal_start.replace('Z', '+00:00')); return dt_aware.replace(tzinfo=None)
                 elif len(gcal_start) == 10: dt_date = datetime.strptime(gcal_start, '%Y-%m-%d').date(); return datetime.combine(dt_date, datetime.min.time())
             except ValueError: pass
        meta_date_str = item.get("date"); meta_time_str = item.get("time")
        sort_dt = datetime.max
        if meta_date_str:
            try:
                if meta_time_str:
                    time_part = meta_time_str + ':00' if len(meta_time_str.split(':')) == 2 else meta_time_str
                    sort_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else: sort_dt = datetime.strptime(meta_date_str, "%Y-%m-%d")
            except (ValueError, TypeError): pass
        return sort_dt
    try:
        return sorted(task_list, key=lambda item: (sort_key(item), item.get("created_at", ""), item.get("title", "").lower()))
    except Exception as e:
        log_error("sync_service", fn_name, f"Error during task sorting: {e}", e)
        return task_list
# --- END OF REFACTORED services/sync_service.py ---