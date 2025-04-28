# --- START OF REFACTORED services/task_query_service.py ---
"""Service layer for querying and formatting task/reminder data from the database."""
from datetime import datetime, timedelta, timezone # Added timezone
import json
from typing import Dict, List, Any, Set, Tuple # Keep required types
import pytz
import traceback # Keep for detailed error logging

from tools.logger import log_info, log_error, log_warning
# Import the database utility module
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("task_query_service", "import", "activity_db not found. Task querying disabled.", None)
    DB_IMPORTED = False
    # Dummy DB functions
    class activity_db:
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
        @staticmethod
        def get_task(*args, **kwargs): return None
    # Consider halting application if DB is critical

# Agent State Manager Import (still needed for preferences/calendar API instance)
try:
    from services.agent_state_manager import get_agent_state
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_query_service", "import", "AgentStateManager not found.")
    AGENT_STATE_MANAGER_IMPORTED = False
    def get_agent_state(*args, **kwargs): return None # Dummy function

# Google Calendar API Import (needed for checking type and formatting sessions)
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
     log_warning("task_query_service", "import", "GoogleCalendarAPI not imported.")
     GoogleCalendarAPI = None
     GCAL_API_IMPORTED = False

# Define active statuses consistently
ACTIVE_STATUSES = ["pending", "in_progress"] # Use list for DB query IN clause

# --- Internal Helper Functions ---

# Keep this function as it retrieves the API instance needed for _format_task_line
def _get_calendar_api_from_state(user_id):
    """Helper to retrieve the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api_from_state"
    if not AGENT_STATE_MANAGER_IMPORTED or not GCAL_API_IMPORTED or GoogleCalendarAPI is None:
        return None
    try:
        agent_state = get_agent_state(user_id) # Returns dict or None
        if agent_state is not None:
            calendar_api_maybe = agent_state.get("calendar")
            if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
                return calendar_api_maybe # Return active instance
    except Exception as e:
         log_error("task_query_service", fn_name, f"Error getting calendar API for {user_id}", e, user_id=user_id)
    return None # Return None if not found, not active, or error

# Keep sorting logic, operates on list of dictionaries
def _sort_tasks(task_list: List[Dict]) -> List[Dict]:
    """Sorts task list robustly by date/time."""
    # (Sorting logic remains the same as previous version - operates on dicts)
    fn_name = "_sort_tasks"
    def sort_key(item):
        gcal_start = item.get("gcal_start_datetime")
        if gcal_start and isinstance(gcal_start, str):
             try:
                 if 'T' in gcal_start: # Datetime format
                      dt_aware = datetime.fromisoformat(gcal_start.replace('Z', '+00:00'))
                      return dt_aware.replace(tzinfo=None) # Compare naive UTC equivalent
                 elif len(gcal_start) == 10: # Date format (all-day)
                      dt_date = datetime.strptime(gcal_start, '%Y-%m-%d').date()
                      # Sort all-day events as start of the day
                      return datetime.combine(dt_date, datetime.min.time())
             except ValueError:
                  pass # Fallback to metadata if parse fails

        # Fallback logic using metadata date/time
        meta_date_str = item.get("date")
        meta_time_str = item.get("time")
        sort_dt = datetime.max # Default to max for sorting unknowns last

        if meta_date_str:
            try:
                if meta_time_str: # Timed item
                    time_part = meta_time_str
                    if len(time_part.split(':')) == 2: time_part += ':00' # Add seconds if missing
                    sort_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else: # All day item based on metadata date
                    sort_dt = datetime.strptime(meta_date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                 # Log warning? Maybe too verbose for sorting fallback
                 pass # Use default max time
        return sort_dt

    try:
        # Sort primarily by datetime, secondarily by creation time (if available), finally by title
        return sorted(task_list, key=lambda item: (sort_key(item), item.get("created_at", ""), item.get("title", "").lower()))
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error during task sorting: {e}", e)
        return task_list # Return unsorted list on error

# Keep formatting logic, operates on list of dictionaries
# Ensure it correctly handles data types from DB (e.g., sessions_planned is INT)
# And decodes session_event_ids if needed (activity_db functions handle this now)
def _format_task_line(task_data: Dict, user_timezone_str: str = "UTC", calendar_api = None) -> str:
    """Formats a single task/event dictionary into a display string."""
    fn_name = "_format_task_line"
    try:
        # Determine User Timezone Object
        user_tz = pytz.utc
        try:
            if user_timezone_str: user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError: user_timezone_str = "UTC"

        # --- Assemble Main Line Parts (Largely same logic as before) ---
        parts = []
        item_type_raw = task_data.get("type", "Item")
        item_type = str(item_type_raw).capitalize()
        if item_type_raw == "external_event": item_type = "Event" # Handle external events from sync
        parts.append(f"({item_type})")
        desc = str(task_data.get("title", "")).strip() or "(No Title)"
        parts.append(desc)
        if item_type_raw == "task":
            duration = task_data.get("estimated_duration")
            # Check for various empty-like values
            if duration and not str(duration).strip().lower() in ['', 'none', 'nan', 'null']:
                parts.append(f"[Est: {duration}]")

        # Date/Time Formatting (Prioritize GCal, fallback to metadata)
        gcal_start_str = task_data.get("gcal_start_datetime"); meta_date = task_data.get("date"); meta_time = task_data.get("time")
        dt_str = ""
        if gcal_start_str:
             try: # Format GCal time
                 if 'T' in gcal_start_str: # Datetime
                      dt_aware = datetime.fromisoformat(gcal_start_str.replace('Z', '+00:00'))
                      dt_local = dt_aware.astimezone(user_tz)
                      # Use a clear format like: Tue, Apr 25 @ 10:00 EDT
                      formatted_dt = dt_local.strftime('%a, %b %d @ %H:%M %Z')
                      dt_str = f" on {formatted_dt}"
                 elif len(gcal_start_str) == 10: # Date (All day)
                      dt_local = datetime.strptime(gcal_start_str, '%Y-%m-%d').date()
                      formatted_dt = dt_local.strftime('%a, %b %d (All day)')
                      dt_str = f" on {formatted_dt}"
             except Exception as fmt_err:
                  log_warning("task_query_service", fn_name, f"Could not format gcal_start '{gcal_start_str}': {fmt_err}")
                  dt_str = f" (Time Error: {gcal_start_str})" # Show raw on error
        elif meta_date: # Fallback to metadata date/time
             dt_str = f" on {meta_date}"
             if meta_time: dt_str += f" at {meta_time}"
             else: dt_str += " (All day)"
        if dt_str: parts.append(dt_str)

        project = task_data.get("project"); status = task_data.get("status")
        if project: parts.append(f"{{{project}}}")
        if item_type_raw in ["task", "reminder"] and status:
             parts.append(f"[{str(status).capitalize()}]")

        main_line = " ".join(p for p in parts if p)

        # --- Display Scheduled Session Details ---
        session_details_lines = []
        # session_event_ids should be a list from the DB access layer now
        session_ids = task_data.get("session_event_ids")
        if item_type_raw == "task" and calendar_api is not None and isinstance(session_ids, list) and session_ids:
            session_details_lines.append("    └── Scheduled Sessions:")
            session_num = 0
            for session_id in session_ids:
                if not isinstance(session_id, str) or not session_id.strip(): continue # Skip invalid IDs
                try:
                    session_event_data = calendar_api._get_single_event(session_id) # Returns dict or None
                    if session_event_data:
                        session_num += 1
                        parsed_session = calendar_api._parse_google_event(session_event_data) # Returns dict
                        s_start = parsed_session.get("gcal_start_datetime")
                        s_end = parsed_session.get("gcal_end_datetime")
                        s_info = "(Time Error)" # Default
                        if s_start and s_end:
                            try:
                                s_aware = datetime.fromisoformat(s_start.replace('Z', '+00:00'))
                                e_aware = datetime.fromisoformat(s_end.replace('Z', '+00:00'))
                                s_local, e_local = s_aware.astimezone(user_tz), e_aware.astimezone(user_tz)
                                # Format: YYYY-MM-DD HH:MM-HH:MM TZN
                                s_info = s_local.strftime('%Y-%m-%d %H:%M') + e_local.strftime('-%H:%M %Z')
                            except Exception as parse_err:
                                log_warning("task_query_service", fn_name, f"Could not parse session times {s_start}-{s_end}: {parse_err}")
                                s_info = f"{s_start} to {s_end} (parse error)"
                        session_details_lines.append(f"        {session_num}) {s_info} (ID: {session_id})")
                    # else: log_warning(f"Session ID {session_id} not found in GCal") # Optional: log if GCal event missing
                except Exception as fetch_err:
                    log_error("task_query_service", fn_name, f"Error fetching/processing session {session_id}", fetch_err, user_id=task_data.get("user_id"))
                    session_details_lines.append(f"        - Error fetching session ID {session_id}")
            # Remove header if no actual sessions were listed
            if session_num == 0 and session_details_lines:
                 session_details_lines.pop(0)

        # Combine main line and session details
        return main_line + ("\n" + "\n".join(session_details_lines) if session_details_lines else "")

    except Exception as e:
        # Log error with user context if available
        user_ctx = task_data.get("user_id", "Unknown")
        log_error("task_query_service", fn_name, f"General error formatting item line {task_data.get('event_id')}. Error: {e}\n{traceback.format_exc()}", e, user_id=user_ctx)
        return f"Error displaying item: {task_data.get('event_id', 'Unknown ID')}"


# --- Public Service Functions ---

# Returns Tuple[str, Dict]
def get_formatted_list(
    user_id: str,
    date_range: Tuple[str, str] | None = None,
    status_filter: str = 'active', # Provide default
    project_filter: str | None = None,
    trigger_sync: bool = False # Keep trigger_sync param, though sync isn't fully implemented
) -> Tuple[str, Dict]:
    """
    Gets tasks from DB, filters (optional), sorts, formats into numbered list string & mapping.
    Includes fetching and displaying details for scheduled task sessions if GCal is active.
    """
    fn_name = "get_formatted_list"
    if not DB_IMPORTED:
        log_error("task_query_service", fn_name, "Database module unavailable.", user_id=user_id)
        return "Error: Could not access task data.", {}

    log_info("task_query_service", fn_name, f"Executing for user={user_id}, Status={status_filter}, Range={date_range}, Proj={project_filter}")

    if trigger_sync:
        log_info("task_query_service", fn_name, "Sync triggered (Not Implemented Yet).", user_id=user_id)
        # Future: Call sync_service.perform_full_sync(user_id) here?

    # Determine status list for DB query
    query_status_list = None
    filter_lower = status_filter.lower().replace(" ", "") if status_filter else 'active' # Default if None
    if filter_lower == 'active': query_status_list = ACTIVE_STATUSES
    elif filter_lower == 'completed': query_status_list = ["completed"]
    elif filter_lower == 'pending': query_status_list = ["pending"]
    elif filter_lower == 'in_progress': query_status_list = ["in_progress"]
    elif filter_lower == 'all': query_status_list = None # No status filter for DB
    else:
        log_warning("task_query_service", fn_name, f"Unknown status filter '{status_filter}'. Defaulting 'active'.", user_id=user_id)
        query_status_list = ACTIVE_STATUSES

    log_info("task_query_service", fn_name, f"Querying DB for user={user_id}, Status={query_status_list}, Range={date_range}, Proj={project_filter}")

    # Fetch data from DB, applying filters available in the DB function
    task_list = []
    try:
        task_list = activity_db.list_tasks_for_user(
            user_id=user_id,
            status_filter=query_status_list, # Pass list of statuses or None
            date_range=date_range,         # Pass tuple or None
            project_filter=project_filter  # Pass string or None
        )
        log_info("task_query_service", fn_name, f"Fetched {len(task_list)} tasks from DB for {user_id} with filters.")
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error fetching tasks from DB for {user_id}", e, user_id=user_id)
        return "Error retrieving tasks.", {}

    # No need for python filtering now as DB function handles it

    if not task_list:
        log_info("task_query_service", fn_name, f"No tasks found matching criteria for {user_id} after DB query.")
        return "", {} # Return empty string and dict if no tasks found

    # Get Calendar API and User Timezone for formatting
    calendar_api = _get_calendar_api_from_state(user_id)
    user_tz_str = "UTC"
    if AGENT_STATE_MANAGER_IMPORTED:
        agent_state = get_agent_state(user_id)
        prefs = agent_state.get("preferences", {}) if agent_state else {}
        user_tz_str = prefs.get("TimeZone", "UTC")

    # Sort tasks
    sorted_tasks = _sort_tasks(task_list)

    # Format lines and build mapping
    lines, mapping = [], {}
    item_num = 0
    for task in sorted_tasks:
        item_id = task.get("event_id")
        if not item_id: continue # Skip items missing id

        item_num += 1
        formatted_line = _format_task_line(task, user_tz_str, calendar_api)
        lines.append(f"{item_num}. {formatted_line}")
        mapping[str(item_num)] = item_id # Use string key

    if item_num == 0: # Should only happen if formatting fails for all items
        log_warning("task_query_service", fn_name, f"Formatting resulted in zero list items for {user_id}.", user_id=user_id)
        return "Error formatting the task list.", {}

    list_body = "\n".join(lines)
    log_info("task_query_service", fn_name, f"Generated list body ({len(mapping)} items) for {user_id}")
    return list_body, mapping


# Returns List[Dict]
def get_tasks_for_summary(
    user_id: str,
    date_range: Tuple[str, str], # Date range is typically required for summaries
    status_filter: str = 'active',
    trigger_sync: bool = False
) -> List[Dict]:
    """Gets tasks from DB for summaries, filters, sorts, returns list of dictionaries."""
    fn_name = "get_tasks_for_summary"
    if not DB_IMPORTED:
        log_error("task_query_service", fn_name, "Database module unavailable.", user_id=user_id)
        return []

    log_info("task_query_service", fn_name, f"Executing for user={user_id}, Filter={status_filter}, Range={date_range}")

    if trigger_sync:
        log_info("task_query_service", fn_name, "Sync triggered (Not Implemented Yet).", user_id=user_id)

    # Determine status list for DB query
    query_status_list = None
    filter_lower = status_filter.lower().replace(" ", "") if status_filter else 'active'
    if filter_lower == 'active': query_status_list = ACTIVE_STATUSES
    elif filter_lower == 'completed': query_status_list = ["completed"]
    elif filter_lower == 'pending': query_status_list = ["pending"]
    elif filter_lower == 'in_progress': query_status_list = ["in_progress"]
    # 'all' means query_status_list remains None

    try:
        # Fetch directly using the specific filters required for summaries
        task_list = activity_db.list_tasks_for_user(
            user_id=user_id,
            status_filter=query_status_list, # Pass list of statuses or None
            date_range=date_range
            # No project filter typically needed for summaries
        )
        # Sort results after fetching
        sorted_tasks = _sort_tasks(task_list)
        log_info("task_query_service", fn_name, f"Returning {len(sorted_tasks)} tasks for summary {user_id}")
        return sorted_tasks
    except Exception as db_err:
        log_error("task_query_service", fn_name, f"Database error fetching tasks for summary: {user_id}", db_err, user_id=user_id)
        return []


# Returns Tuple[List[Dict], List[Dict]]
def get_context_snapshot(user_id: str, history_weeks: int = 1, future_weeks: int = 2) -> Tuple[List[Dict], List[Dict]]:
    """Fetches relevant active WT tasks (DB) and GCal events (API) for Orchestrator context."""
    fn_name = "get_context_snapshot"
    if not DB_IMPORTED:
        log_error("task_query_service", fn_name, "Database module unavailable.", user_id=user_id)
        return [], [] # Return empty lists

    log_info("task_query_service", fn_name, f"Get Context Snapshot: User={user_id}")
    task_context, calendar_context = [], []

    try:
        # 1. Calculate date range
        today = datetime.now(timezone.utc).date() # Use UTC date for consistency
        start_date = today - timedelta(weeks=history_weeks)
        end_date = today + timedelta(weeks=future_weeks)
        start_str, end_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        date_range_tuple = (start_str, end_str)

        # 2. Get WT tasks from DB (only active ones within the window)
        try:
            task_context = activity_db.list_tasks_for_user(
                user_id=user_id,
                status_filter=ACTIVE_STATUSES, # Fetch only active tasks
                date_range=date_range_tuple     # Apply date range
            )
            log_info("task_query_service", fn_name, f"Fetched {len(task_context)} active WT tasks from DB for snapshot.")
        except Exception as db_err:
             log_error("task_query_service", fn_name, f"Failed to fetch tasks from DB for snapshot: {db_err}", db_err, user_id=user_id)
             task_context = [] # Continue without tasks if DB fails

        # 3. Get GCal events directly from API
        calendar_api = _get_calendar_api_from_state(user_id)
        if calendar_api:
            try:
                # Fetch GCal events - list_events returns parsed dicts
                calendar_context = calendar_api.list_events(start_str, end_str)
                log_info("task_query_service", fn_name, f"Fetched {len(calendar_context)} GCal events for snapshot.")
            except Exception as cal_e:
                log_error("task_query_service", fn_name, f"Failed fetch calendar events for snapshot: {cal_e}", cal_e, user_id=user_id)
                calendar_context = [] # Continue without calendar events
        else:
            log_info("task_query_service", fn_name, f"Calendar API not active for {user_id}, skipping GCal fetch for snapshot.")

        log_info("task_query_service", fn_name, f"Snapshot created for {user_id}: {len(task_context)} tasks, {len(calendar_context)} external events.")

    except Exception as e:
        log_error("task_query_service", fn_name, f"Error creating context snapshot for {user_id}", e, user_id=user_id)
        return [], [] # Return empty lists on error

    return task_context, calendar_context

# --- END OF REFACTORED services/task_query_service.py ---