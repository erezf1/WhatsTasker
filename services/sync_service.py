# --- START OF FULL services/sync_service.py ---

import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import json

from tools.logger import log_info, log_error, log_warning
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
        def get_task(*args, **kwargs): return None

try:
    from users.user_manager import get_agent
    USER_MANAGER_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import user_manager.get_agent")
    USER_MANAGER_IMPORTED = False
    def get_agent(*args, **kwargs): return None

try:
    from services.agent_state_manager import update_task_in_context, get_agent_state # Added get_agent_state
    AGENT_STATE_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import agent_state_manager functions.")
    AGENT_STATE_IMPORTED = False
    def update_task_in_context(*args, **kwargs): pass
    def get_agent_state(*args, **kwargs): return None # Dummy

# --- config_manager import for setting GCal status ---
CONFIG_MANAGER_SYNC_IMPORTED = False
_set_gcal_status_func_sync = None
try:
    from services.config_manager import set_gcal_integration_status
    _set_gcal_status_func_sync = set_gcal_integration_status
    CONFIG_MANAGER_SYNC_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import config_manager.set_gcal_integration_status. GCal status updates on sync error will be skipped.")
# --- End config_manager import ---

try:
    from tools.google_calendar_api import GoogleCalendarAPI, HttpError
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    TENACITY_IMPORTED = True
    GCAL_API_IMPORTED = True
except ImportError:
    log_warning("sync_service", "import", "GoogleCalendarAPI or Tenacity not found. Sync limited/no GCal retry.")
    GoogleCalendarAPI = None
    HttpError = Exception
    GCAL_API_IMPORTED = False
    TENACITY_IMPORTED = False
    def retry(*args, **kwargs):
        def decorator(func):
            def wrapper(*f_args, **f_kwargs): return func(*f_args, **f_kwargs)
            return wrapper
        return decorator

GCAL_RETRYABLE_EXCEPTIONS_TUPLE = (HttpError, TimeoutError, ConnectionResetError, ConnectionAbortedError)
try:
    from http.client import RemoteDisconnected
    GCAL_RETRYABLE_EXCEPTIONS_TUPLE += (RemoteDisconnected,)
except ImportError:
    pass

if TENACITY_IMPORTED and GCAL_API_IMPORTED and GoogleCalendarAPI is not None :
    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(GCAL_RETRYABLE_EXCEPTIONS_TUPLE), reraise=True # Reraise True to catch it in calling function
    )
    def _fetch_gcal_events_with_retry(calendar_api_instance: GoogleCalendarAPI, start_date_str: str, end_date_str: str, user_id_for_log: str) -> List[Dict]:
        # log_info("sync_service", "_fetch_gcal_events_with_retry", f"Attempting GCal fetch for user {user_id_for_log}") # Verbose
        return calendar_api_instance.list_events(start_date_str, end_date_str)
else:
    def _fetch_gcal_events_with_retry(calendar_api_instance: Any, start_date_str: str, end_date_str: str, user_id_for_log: str) -> List[Dict]:
        fn_name = "_fetch_gcal_events_no_retry"
        if calendar_api_instance and hasattr(calendar_api_instance, 'list_events'):
            try:
                return calendar_api_instance.list_events(start_date_str, end_date_str)
            except GCAL_RETRYABLE_EXCEPTIONS_TUPLE as e:
                log_error("sync_service", fn_name, f"GCal fetch failed (no retry) for {user_id_for_log}", e, user_id=user_id_for_log)
                raise # Reraise to be caught by calling function
            except Exception as e_unexp:
                log_error("sync_service", fn_name, f"Unexpected GCal fetch error (no retry) for {user_id_for_log}", e_unexp, user_id=user_id_for_log)
                raise # Reraise
        return []


def _sort_synced_items(item_list: List[Dict]) -> List[Dict]:
    fn_name = "_sort_synced_items"
    def sort_key(item):
        eff_dt = datetime.max
        gcal_start = item.get("gcal_start_datetime")
        meta_date_str = item.get("date"); meta_time_str = item.get("time")
        if gcal_start and isinstance(gcal_start, str):
            try:
                if 'T' in gcal_start: eff_dt = datetime.fromisoformat(gcal_start.replace('Z', '+00:00')).replace(tzinfo=None)
                elif len(gcal_start) == 10: eff_dt = datetime.combine(datetime.strptime(gcal_start, '%Y-%m-%d').date(), datetime.min.time())
            except ValueError: pass
        elif meta_date_str:
            try:
                base_date = datetime.strptime(meta_date_str, "%Y-%m-%d")
                if meta_time_str:
                    time_part = meta_time_str + ':00' if len(meta_time_str.split(':')) == 2 else meta_time_str
                    eff_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else: eff_dt = datetime.combine(base_date.date(), datetime.min.time())
            except (ValueError, TypeError): pass
        type_order = {"reminder": 0, "task": 1, "todo": 2, "external_event": 3}
        item_type_val = type_order.get(item.get("type", "todo"), 2)
        created_at_str = item.get("created_at", ""); created_dt = datetime.max
        if created_at_str:
            try: created_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
            except ValueError: pass
        return (eff_dt, item_type_val, created_dt, item.get("title", "").lower())
    try: return sorted(item_list, key=sort_key)
    except Exception as e: log_error("sync_service", fn_name, f"Error during item sorting: {e}", e); return item_list


def get_synced_context_snapshot(user_id: str, start_date_str: str, end_date_str: str) -> List[Dict]:
    fn_name = "get_synced_context_snapshot"
    if not DB_IMPORTED: return []

    calendar_api_instance = None
    current_gcal_status = "not_integrated" # Default

    if USER_MANAGER_IMPORTED and GCAL_API_IMPORTED and GoogleCalendarAPI is not None and AGENT_STATE_IMPORTED:
        agent_state = get_agent_state(user_id) # Get from state manager
        if agent_state:
            api_maybe = agent_state.get("calendar")
            current_gcal_status = agent_state.get("preferences", {}).get("gcal_integration_status", "not_integrated")
            if isinstance(api_maybe, GoogleCalendarAPI) and api_maybe.is_active() and current_gcal_status == "connected":
                calendar_api_instance = api_maybe
            elif current_gcal_status == "connected" and (api_maybe is None or not api_maybe.is_active()):
                 log_warning("sync_service", fn_name, f"GCal status is 'connected' for {user_id} but API instance not active/found. Attempting re-init for sync.")
                 # Try to re-initialize it for this sync attempt if user_manager is also available
                 if get_agent: # Ensure get_agent (from user_manager) is available
                    agent_full_state_from_usermgr = get_agent(user_id)
                    if agent_full_state_from_usermgr and agent_full_state_from_usermgr.get("calendar"):
                         calendar_api_instance = agent_full_state_from_usermgr.get("calendar")
                         if not calendar_api_instance or not calendar_api_instance.is_active():
                              calendar_api_instance = None # Failed re-init
                              log_warning("sync_service", fn_name, f"Re-init of GCal API failed for {user_id} during sync.")
                              if CONFIG_MANAGER_SYNC_IMPORTED and _set_gcal_status_func_sync:
                                  _set_gcal_status_func_sync(user_id, "error")


    gcal_events_list = []
    if calendar_api_instance:
        try:
            gcal_events_list = _fetch_gcal_events_with_retry(calendar_api_instance, start_date_str, end_date_str, user_id)
        except Exception as e_gcal_fetch: # Catch if _fetch_gcal_events_with_retry re-raises after all attempts
            log_error("sync_service", fn_name, f"Final GCal event fetch error for {user_id} after retries.", e_gcal_fetch, user_id=user_id)
            if CONFIG_MANAGER_SYNC_IMPORTED and _set_gcal_status_func_sync and current_gcal_status == "connected":
                log_info("sync_service", fn_name, f"Setting GCal status to 'error' for {user_id} due to sync fetch failure.")
                _set_gcal_status_func_sync(user_id, "error")
            gcal_events_list = [] # Ensure it's empty on error

    wt_items_list_from_db = []
    try:
        wt_items_list_from_db = activity_db.list_tasks_for_user(
            user_id=user_id, date_range=(start_date_str, end_date_str)
        )
    except Exception as e_db_fetch:
        log_error("sync_service", fn_name, f"Error fetching WT items from DB for {user_id}", e_db_fetch, user_id=user_id)

    gcal_events_map = {e['event_id']: e for e in gcal_events_list if e.get('event_id')}
    wt_items_map = {t['event_id']: t for t in wt_items_list_from_db if t.get('event_id')}
    aggregated_context_list: List[Dict[str, Any]] = []
    processed_wt_item_ids = set()

    for gcal_event_id, gcal_data in gcal_events_map.items():
        if gcal_event_id in wt_items_map:
            processed_wt_item_ids.add(gcal_event_id)
            wt_item_data = wt_items_map[gcal_event_id]
            if wt_item_data.get("type") == "reminder":
                merged_data = wt_item_data.copy(); needs_db_update = False
                gcal_start = gcal_data.get("gcal_start_datetime"); gcal_end = gcal_data.get("gcal_end_datetime")
                gcal_title = gcal_data.get("title")
                if gcal_start != merged_data.get("gcal_start_datetime"):
                    merged_data["gcal_start_datetime"] = gcal_start
                    if gcal_start and 'T' in gcal_start:
                        try:
                            dt_aware = datetime.fromisoformat(gcal_start.replace('Z', '+00:00'))
                            merged_data["date"] = dt_aware.strftime("%Y-%m-%d")
                            merged_data["time"] = dt_aware.strftime("%H:%M")
                        except ValueError: pass
                    elif gcal_start and len(gcal_start) == 10:
                        merged_data["date"] = gcal_start; merged_data["time"] = None
                    needs_db_update = True
                if gcal_end != merged_data.get("gcal_end_datetime"):
                    merged_data["gcal_end_datetime"] = gcal_end; needs_db_update = True
                if gcal_title and gcal_title != merged_data.get("title"):
                    merged_data["title"] = gcal_title; merged_data["description"] = gcal_title; needs_db_update = True
                if needs_db_update:
                    # log_info("sync_service", fn_name, f"GCal data changed for WT Reminder {gcal_event_id}. Updating DB.") # Verbose
                    try:
                        update_success = activity_db.add_or_update_task(merged_data)
                        if update_success and AGENT_STATE_IMPORTED:
                            updated_data_from_db = activity_db.get_task(gcal_event_id)
                            if updated_data_from_db: update_task_in_context(user_id, gcal_event_id, updated_data_from_db)
                        elif not update_success:
                            log_error("sync_service", fn_name, f"Failed DB update for WT Reminder {gcal_event_id} after GCal merge.", user_id=user_id)
                    except Exception as save_err:
                        log_error("sync_service", fn_name, f"Error saving updated WT Reminder {gcal_event_id}", save_err, user_id=user_id)
                aggregated_context_list.append(merged_data)
            else: # Task or ToDo
                aggregated_context_list.append(wt_item_data)
        else: # External GCal event
            external_event_data = gcal_data.copy()
            external_event_data["type"] = "external_event"; external_event_data["user_id"] = user_id
            external_event_data.setdefault("status", gcal_data.get("status_gcal", "confirmed"))
            aggregated_context_list.append(external_event_data)

    for wt_event_id, wt_item_data in wt_items_map.items():
        if wt_event_id not in processed_wt_item_ids:
            if isinstance(wt_item_data.get("session_event_ids"), str):
                try: wt_item_data["session_event_ids"] = json.loads(wt_item_data["session_event_ids"])
                except: wt_item_data["session_event_ids"] = []
            aggregated_context_list.append(wt_item_data)
            
    sorted_aggregated_context = _sort_synced_items(aggregated_context_list)
    return sorted_aggregated_context

# --- END OF FULL services/sync_service.py ---