# --- START OF FILE services/task_query_service.py ---
"""Service layer for querying and formatting item (Task, ToDo, Reminder) data from the database."""
from datetime import datetime, timedelta, timezone
import json
from typing import Dict, List, Any, Tuple
import pytz
import traceback

from tools.logger import log_info, log_error, log_warning
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("task_query_service", "import", "activity_db not found. Item querying disabled.", None)
    DB_IMPORTED = False
    class activity_db: # Dummy
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
        @staticmethod
        def get_task(*args, **kwargs): return None

try:
    from services.agent_state_manager import get_agent_state
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_query_service", "import", "AgentStateManager not found.")
    AGENT_STATE_MANAGER_IMPORTED = False
    def get_agent_state(*args, **kwargs): return None

try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
     log_warning("task_query_service", "import", "GoogleCalendarAPI not imported.")
     GoogleCalendarAPI = None # Define as None if import fails
     GCAL_API_IMPORTED = False

ACTIVE_STATUSES = ["pending", "in_progress"]

def _get_calendar_api_from_state(user_id: str) -> Any: # Changed return type hint
    fn_name = "_get_calendar_api_from_state_query"
    if not AGENT_STATE_MANAGER_IMPORTED or not GCAL_API_IMPORTED or GoogleCalendarAPI is None:
        return None
    try:
        agent_state = get_agent_state(user_id)
        if agent_state is not None:
            calendar_api_maybe = agent_state.get("calendar")
            if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
                return calendar_api_maybe
    except Exception as e:
         log_error("task_query_service", fn_name, f"Error getting calendar API for {user_id}", e, user_id=user_id)
    return None

def _sort_items(item_list: List[Dict]) -> List[Dict]: # Renamed from _sort_tasks
    """Sorts item list robustly by date/time, then type, then title."""
    fn_name = "_sort_items"
    def sort_key(item):
        # Primary sort key: Effective datetime
        eff_dt = datetime.max # Default for items without date/time
        gcal_start = item.get("gcal_start_datetime")
        meta_date_str = item.get("date")
        meta_time_str = item.get("time")

        if gcal_start and isinstance(gcal_start, str):
             try:
                 if 'T' in gcal_start: eff_dt = datetime.fromisoformat(gcal_start.replace('Z', '+00:00')).replace(tzinfo=None)
                 elif len(gcal_start) == 10: eff_dt = datetime.combine(datetime.strptime(gcal_start, '%Y-%m-%d').date(), datetime.min.time())
             except ValueError: pass # Fallback
        elif meta_date_str:
            try:
                base_date = datetime.strptime(meta_date_str, "%Y-%m-%d")
                if meta_time_str:
                    time_part = meta_time_str + ':00' if len(meta_time_str.split(':')) == 2 else meta_time_str
                    eff_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else: # All-day item based on metadata date
                    eff_dt = datetime.combine(base_date.date(), datetime.min.time())
            except (ValueError, TypeError): pass
        
        # Secondary sort key: Item type (Reminder -> Task -> ToDo -> External)
        type_order = {"reminder": 0, "task": 1, "todo": 2, "external_event": 3}
        item_type_val = type_order.get(item.get("type", "todo"), 2) # Default to ToDo if type missing

        # Tertiary sort key: Creation time (if available), then title
        created_at_str = item.get("created_at", "")
        created_dt = datetime.max
        if created_at_str:
            try: created_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
            except ValueError: pass
            
        return (eff_dt, item_type_val, created_dt, item.get("title", "").lower())

    try:
        return sorted(item_list, key=sort_key)
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error during item sorting: {e}", e)
        return item_list

def _format_item_line(item_data: Dict, user_timezone_str: str = "UTC", calendar_api: Any = None) -> str: # Renamed
    """Formats a single item (Task, ToDo, Reminder, External Event) dictionary into a display string."""
    fn_name = "_format_item_line"
    try:
        user_tz = pytz.utc
        try:
            if user_timezone_str: user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError: user_timezone_str = "UTC" # Logged by pytz

        parts = []
        item_type_raw = item_data.get("type", "Item")
        item_type_display = str(item_type_raw).capitalize()
        if item_type_raw == "external_event": item_type_display = "Event"
        parts.append(f"({item_type_display})")

        title = str(item_data.get("title", "")).strip() or "(No Title)"
        parts.append(title)

        if item_type_raw == "task":
            est_duration = item_data.get("estimated_duration")
            if est_duration and str(est_duration).strip().lower() not in ['', 'none', 'nan', 'null']:
                parts.append(f"[Est: {est_duration}]")
        
        # Date/Time Formatting
        gcal_start_str = item_data.get("gcal_start_datetime")
        meta_date = item_data.get("date")
        meta_time = item_data.get("time")
        dt_display_str = ""

        if gcal_start_str:
             try:
                 if 'T' in gcal_start_str: # Datetime
                      dt_aware = datetime.fromisoformat(gcal_start_str.replace('Z', '+00:00'))
                      dt_local = dt_aware.astimezone(user_tz)
                      formatted_dt = dt_local.strftime('%a, %b %d @ %H:%M %Z')
                      dt_display_str = f" on {formatted_dt}"
                 elif len(gcal_start_str) == 10: # Date (All day)
                      dt_local = datetime.strptime(gcal_start_str, '%Y-%m-%d').date()
                      formatted_dt = dt_local.strftime('%a, %b %d (All day)')
                      dt_display_str = f" on {formatted_dt}"
             except Exception as fmt_err:
                  log_warning("task_query_service", fn_name, f"Could not format gcal_start '{gcal_start_str}': {fmt_err}")
                  dt_display_str = f" (Time Error: {gcal_start_str})"
        elif meta_date:
             dt_display_str = f" on {meta_date}"
             if meta_time: dt_display_str += f" at {meta_time}"
             elif item_type_raw == "reminder": dt_display_str += " (All day)" # Clarify for reminders
        
        if dt_display_str: parts.append(dt_display_str)

        project = item_data.get("project")
        status = item_data.get("status")
        if project: parts.append(f"{{{project}}}")
        
        # Display status for WT managed items, not external GCal events
        if item_type_raw in ["task", "todo", "reminder"] and status:
             parts.append(f"[{str(status).capitalize()}]")

        main_line = " ".join(p for p in parts if p)

        # Display Scheduled Session Details for Tasks
        session_details_lines = []
        if item_type_raw == "task" and calendar_api is not None:
            session_gcal_ids = item_data.get("session_event_ids") # Should be a list from DB
            if isinstance(session_gcal_ids, list) and session_gcal_ids:
                session_details_lines.append("    └── Scheduled Sessions:")
                session_num = 0
                for session_id_str in session_gcal_ids:
                    if not isinstance(session_id_str, str) or not session_id_str.strip(): continue
                    try:
                        # Fetch session details (could be optimized if sync_service pre-populates this)
                        session_event_data = calendar_api._get_single_event(session_id_str)
                        if session_event_data:
                            session_num += 1
                            parsed_session = calendar_api._parse_google_event(session_event_data)
                            s_start_iso = parsed_session.get("gcal_start_datetime")
                            s_end_iso = parsed_session.get("gcal_end_datetime")
                            s_info = "(Time Error)"
                            if s_start_iso and s_end_iso:
                                try:
                                    s_aware = datetime.fromisoformat(s_start_iso.replace('Z', '+00:00'))
                                    e_aware = datetime.fromisoformat(s_end_iso.replace('Z', '+00:00'))
                                    s_local, e_local = s_aware.astimezone(user_tz), e_aware.astimezone(user_tz)
                                    s_info = s_local.strftime('%Y-%m-%d %H:%M') + e_local.strftime('-%H:%M %Z')
                                except Exception as parse_err:
                                    log_warning("task_query_service", fn_name, f"Could not parse session times {s_start_iso}-{s_end_iso}: {parse_err}")
                                    s_info = f"{s_start_iso} to {s_end_iso} (parse error)"
                            session_details_lines.append(f"        {session_num}) {s_info} (ID: {session_id_str})")
                        # else: log_warning if GCal event for session ID is missing
                    except Exception as fetch_err:
                        log_error("task_query_service", fn_name, f"Error fetching/processing session {session_id_str}", fetch_err, user_id=item_data.get("user_id"))
                        session_details_lines.append(f"        - Error fetching session ID {session_id_str}")
                if session_num == 0 and session_details_lines: # No valid sessions found/fetched
                    session_details_lines.pop(0) # Remove header

        return main_line + ("\n" + "\n".join(session_details_lines) if session_details_lines else "")

    except Exception as e:
        user_ctx = item_data.get("user_id", "UnknownUser")
        item_ctx_id = item_data.get("event_id", item_data.get("item_id", "UnknownItem"))
        log_error("task_query_service", fn_name, f"General error formatting item line {item_ctx_id}. Error: {e}\n{traceback.format_exc()}", e, user_id=user_ctx)
        return f"Error displaying item: {item_ctx_id}"


def get_formatted_list(
    user_id: str,
    date_range: Tuple[str, str] | None = None,
    status_filter: str = 'active',
    project_filter: str | None = None,
    # trigger_sync: bool = False # Sync is handled by sync_service, not directly triggered here
) -> Tuple[str, Dict]:
    """
    Gets items from DB, filters, sorts, formats into numbered list string & mapping.
    Handles Tasks (with sessions), ToDos, and Reminders.
    """
    fn_name = "get_formatted_list"
    if not DB_IMPORTED:
        log_error("task_query_service", fn_name, "Database module unavailable.", user_id=user_id)
        return "Error: Could not access item data.", {}

    log_info("task_query_service", fn_name, f"User {user_id}, Status={status_filter}, Range={date_range}, Proj={project_filter}")

    query_status_list = None
    filter_lower = status_filter.lower().strip().replace(" ", "") if status_filter else 'active'
    if filter_lower == 'active': query_status_list = ACTIVE_STATUSES
    elif filter_lower == 'completed': query_status_list = ["completed"]
    elif filter_lower == 'pending': query_status_list = ["pending"]
    elif filter_lower == 'in_progress': query_status_list = ["in_progress"]
    elif filter_lower != 'all': # if 'all', query_status_list remains None (no status filter)
        log_warning("task_query_service", fn_name, f"Unknown status filter '{status_filter}'. Defaulting 'active'.", user_id=user_id)
        query_status_list = ACTIVE_STATUSES
    
    item_list_from_db = []
    try:
        # list_tasks_for_user now fetches all item types based on filters
        item_list_from_db = activity_db.list_tasks_for_user(
            user_id=user_id, status_filter=query_status_list,
            date_range=date_range, project_filter=project_filter
        )
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error fetching items from DB for {user_id}", e, user_id=user_id)
        return "Error retrieving items.", {}

    if not item_list_from_db:
        log_info("task_query_service", fn_name, f"No items found matching criteria for {user_id}.")
        return "", {} 

    calendar_api = _get_calendar_api_from_state(user_id)
    user_tz_str = "UTC"
    if AGENT_STATE_MANAGER_IMPORTED:
        agent_state = get_agent_state(user_id)
        prefs = agent_state.get("preferences", {}) if agent_state else {}
        user_tz_str = prefs.get("TimeZone", "UTC")

    sorted_items = _sort_items(item_list_from_db)
    lines, mapping = [], {}
    item_num_display = 0
    for item in sorted_items:
        item_id = item.get("event_id") or item.get("item_id") # Prefer event_id
        if not item_id: continue

        item_num_display += 1
        formatted_line = _format_item_line(item, user_tz_str, calendar_api)
        lines.append(f"{item_num_display}. {formatted_line}")
        mapping[str(item_num_display)] = item_id

    if item_num_display == 0:
        log_warning("task_query_service", fn_name, f"Formatting resulted in zero list items for {user_id}.", user_id=user_id)
        return "Error formatting the item list.", {}

    return "\n".join(lines), mapping


def get_items_for_summary( # Renamed from get_tasks_for_summary
    user_id: str, date_range: Tuple[str, str],
    status_filter: str = 'active',
    # trigger_sync: bool = False # Sync handled by sync_service
) -> List[Dict]:
    """Gets items from DB for summaries (Tasks, ToDos, Reminders), filters, sorts, returns list of dicts."""
    fn_name = "get_items_for_summary"
    if not DB_IMPORTED: return []

    log_info("task_query_service", fn_name, f"User {user_id}, Status={status_filter}, Range={date_range} for summary.")
    
    query_status_list = None
    filter_lower = status_filter.lower().strip().replace(" ", "") if status_filter else 'active'
    if filter_lower == 'active': query_status_list = ACTIVE_STATUSES
    elif filter_lower == 'completed': query_status_list = ["completed"]
    elif filter_lower == 'pending': query_status_list = ["pending"]
    elif filter_lower == 'in_progress': query_status_list = ["in_progress"]
    # if 'all', query_status_list remains None

    try:
        item_list = activity_db.list_tasks_for_user(
            user_id=user_id, status_filter=query_status_list, date_range=date_range
        )
        # Ensure all item types relevant for summaries are included by default if no specific types are filtered out by list_tasks_for_user
        sorted_items = _sort_items(item_list)
        log_info("task_query_service", fn_name, f"Returning {len(sorted_items)} items for summary {user_id}")
        return sorted_items
    except Exception as db_err:
        log_error("task_query_service", fn_name, f"Database error fetching items for summary: {user_id}", db_err, user_id=user_id)
        return []


def get_context_snapshot(user_id: str, history_weeks: int = 1, future_weeks: int = 2) -> Tuple[List[Dict], List[Dict]]:
    """
    Fetches relevant active WT items (DB: Tasks, ToDos, Reminders)
    and GCal events (API) for Orchestrator context.
    """
    fn_name = "get_context_snapshot"
    if not DB_IMPORTED: return [], [] 

    # log_info("task_query_service", fn_name, f"Get Context Snapshot: User={user_id}") # Can be verbose
    wt_items_context, calendar_events_context = [], []

    try:
        today = datetime.now(timezone.utc).date() 
        start_date = today - timedelta(weeks=history_weeks)
        end_date = today + timedelta(weeks=future_weeks)
        start_str, end_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        date_range_tuple = (start_str, end_str)

        try:
            # Fetch ALL active item types from DB within the window
            wt_items_context = activity_db.list_tasks_for_user(
                user_id=user_id, status_filter=ACTIVE_STATUSES, date_range=date_range_tuple
            )
            # log_info("task_query_service", fn_name, f"Fetched {len(wt_items_context)} active WT items from DB for snapshot.")
        except Exception as db_err:
             log_error("task_query_service", fn_name, f"Failed to fetch WT items from DB for snapshot: {db_err}", db_err, user_id=user_id)
             # Continue with empty wt_items_context

        calendar_api = _get_calendar_api_from_state(user_id)
        if calendar_api:
            try:
                calendar_events_context = calendar_api.list_events(start_str, end_str) # Already parsed by GCalAPI
                # log_info("task_query_service", fn_name, f"Fetched {len(calendar_events_context)} GCal events for snapshot.")
            except Exception as cal_e:
                log_error("task_query_service", fn_name, f"Failed fetch GCal events for snapshot: {cal_e}", cal_e, user_id=user_id)
                # Continue with empty calendar_events_context
        # else: GCal API not active, calendar_events_context remains empty

        # log_info("task_query_service", fn_name, f"Context snapshot for {user_id}: {len(wt_items_context)} WT items, {len(calendar_events_context)} GCal events.")
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error creating context snapshot for {user_id}", e, user_id=user_id)
        return [], [] 

    return wt_items_context, calendar_events_context

# --- END OF FILE services/task_query_service.py ---