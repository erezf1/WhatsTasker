# --- START OF FILE services/task_query_service.py ---
"""Service layer for querying and formatting task/reminder data."""
from datetime import datetime, timedelta
import json
from typing import Dict, List, Any, Optional, Set, Tuple # Added Set/Tuple
import pytz
from tools.logger import log_info, log_error, log_warning
from tools import metadata_store

# Agent State Manager Import
try:
    from services.agent_state_manager import get_context, get_agent_state
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_query_service", "import", "AgentStateManager not found.")
    AGENT_STATE_MANAGER_IMPORTED = False
    def get_context(*args, **kwargs): return None
    def get_agent_state(*args, **kwargs): return None

# Google Calendar API Import
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
     log_warning("task_query_service", "import", "GoogleCalendarAPI not imported.")
     GoogleCalendarAPI = None
     GCAL_API_IMPORTED = False

ACTIVE_STATUSES = {"pending", "in_progress"}

# --- Internal Helper Functions ---

def _get_calendar_api_from_state(user_id):
    """Helper to retrieve the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api_from_state" # Added fn_name
    if not AGENT_STATE_MANAGER_IMPORTED or not GCAL_API_IMPORTED: return None
    try:
        agent_state = get_agent_state(user_id)
        if agent_state:
            calendar_api_maybe = agent_state.get("calendar")
            if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
                return calendar_api_maybe
    except Exception as e:
         log_error("task_query_service", fn_name, f"Error getting calendar API for {user_id}", e) # Use fn_name
    return None

def _filter_tasks_by_status(task_list, status_filter='active'):
    """Filters task list by status."""
    fn_name = "_filter_tasks_by_status" # Added fn_name
    filter_lower = status_filter.lower()
    if filter_lower == 'all': return task_list
    target_statuses = set()
    if filter_lower == 'active': target_statuses = ACTIVE_STATUSES
    elif filter_lower == 'completed': target_statuses = {"completed"}
    elif filter_lower == 'pending': target_statuses = {"pending"}
    elif filter_lower == 'in_progress': target_statuses = {"in_progress", "inprogress"} # Allow both variants
    else:
        log_warning("task_query_service", fn_name, f"Unknown status filter '{status_filter}'. Defaulting 'active'."); # Use fn_name
        target_statuses = ACTIVE_STATUSES
    return [task for task in task_list if str(task.get("status", "pending")).lower() in target_statuses]

def _filter_tasks_by_date_range(task_list, date_range):
    """Filters task list by 'date' field."""
    fn_name = "_filter_tasks_by_date_range" # Added fn_name
    if not date_range or len(date_range) != 2:
        log_warning("task_query_service", fn_name, f"Invalid date_range provided: {date_range}. Skipping filter.")
        return task_list
    try:
        start_date = datetime.strptime(date_range[0], "%Y-%m-%d").date()
        end_date = datetime.strptime(date_range[1], "%Y-%m-%d").date()
        filtered = []
        for task in task_list:
            task_date_str = task.get("date")
            if task_date_str and isinstance(task_date_str, str):
                try:
                    task_date = datetime.strptime(task_date_str, "%Y-%m-%d").date()
                    if start_date <= task_date <= end_date: filtered.append(task)
                except (ValueError, TypeError): pass # Skip tasks with unparseable dates silently
        return filtered
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error filtering by date range {date_range}: {e}", e); # Use fn_name
        return [] # Return empty list on error

def _filter_tasks_by_project(task_list, project_filter):
    """Filters task list by project tag (case-insensitive)."""
    if not project_filter: return task_list
    filter_lower = project_filter.lower()
    return [task for task in task_list if task.get("project") is not None and str(task.get("project", "")).lower() == filter_lower]

def _sort_tasks(task_list):
    """Sorts task list robustly by date/time."""
    fn_name = "_sort_tasks" # Added fn_name
    def sort_key(item):
        # Use gcal_start_datetime if available and valid, otherwise fallback
        gcal_start = item.get("gcal_start_datetime")
        if gcal_start and isinstance(gcal_start, str):
             try:
                 dt_aware = datetime.fromisoformat(gcal_start.replace('Z', '+00:00'))
                 return dt_aware.replace(tzinfo=None) # Compare naive UTC equivalent
             except ValueError:
                  log_warning("task_query_service", f"{fn_name}.sort_key", f"Could not parse gcal_start_datetime '{gcal_start}' for {item.get('event_id')}. Falling back.")

        # Fallback logic using date/time
        meta_date_str = item.get("date")
        meta_time_str = item.get("time")
        sort_dt = datetime.max # Default to max for sorting unknowns last

        if meta_date_str:
            try:
                time_part = meta_time_str if meta_time_str else "00:00:00"
                if len(time_part.split(':')) == 2: time_part += ':00'
                if len(meta_date_str) == 10 and len(time_part) == 8:
                     sort_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                elif len(meta_date_str) == 10: # All day if only date
                     sort_dt = datetime.strptime(meta_date_str, "%Y-%m-%d")
                else:
                     log_warning("task_query_service", f"{fn_name}.sort_key", f"Invalid date/time format for {item.get('event_id')}: D='{meta_date_str}' T='{meta_time_str}'.")
            except (ValueError, TypeError):
                 log_warning("task_query_service", f"{fn_name}.sort_key", f"Could not parse date/time for {item.get('event_id')}: D='{meta_date_str}' T='{meta_time_str}'.")
        return sort_dt

    try:
        # Sort primarily by datetime, secondarily by creation time, finally by title
        return sorted(task_list, key=lambda item: (sort_key(item), item.get("created_at", ""), item.get("title", "").lower()))
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error during task sorting: {e}", e)
        return task_list # Return unsorted list on error

def _format_task_line(task_data, user_timezone_str="UTC", calendar_api=None): # Added calendar_api param
    """
    Formats a single task/event dictionary into a display string, converting
    aware datetimes to the user's timezone. For tasks, it fetches and lists
    details of scheduled work sessions from Google Calendar if available.

    Args:
        task_data (dict): Dictionary containing item data.
        user_timezone_str (str): The user's Olson timezone string. Defaults to 'UTC'.
        calendar_api (GoogleCalendarAPI | None): Active GCal API instance for the user.

    Returns:
        str: A formatted string representation of the item.
    """
    fn_name = "_format_task_line"
    try:
        # Determine User Timezone Object
        user_tz = pytz.utc
        try:
            if user_timezone_str: user_tz = pytz.timezone(user_timezone_str)
        except pytz.UnknownTimeZoneError:
            log_warning("task_query_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC for formatting item {task_data.get('event_id')}.")
            user_timezone_str = "UTC"

        # --- Assemble Main Line Parts ---
        parts = []
        item_type = str(task_data.get("type", "Item")).capitalize()
        if item_type.lower() == "external_event": item_type = "Event"
        elif item_type.lower() == "task": item_type = "Task"
        elif item_type.lower() == "reminder": item_type = "Reminder"
        parts.append(f"({item_type})")

        desc = str(task_data.get("title", "")).strip() or str(task_data.get("description", "")).strip() or "(No Title)"
        parts.append(desc)

        if item_type.lower() == "task":
            duration = task_data.get("estimated_duration")
            is_empty_like = duration is None or (isinstance(duration, str) and duration.strip().lower() in ['', 'none', 'nan'])
            if duration and not is_empty_like:
                parts.append(f"[Est: {duration}]")

        gcal_start_str = task_data.get("gcal_start_datetime")
        meta_date = task_data.get("date")
        meta_time = task_data.get("time")
        dt_str = ""

        if gcal_start_str:
             try:
                 dt_aware = datetime.fromisoformat(gcal_start_str.replace('Z', '+00:00'))
                 dt_local = dt_aware.astimezone(user_tz)
                 formatted_dt = dt_local.strftime('%Y-%m-%d @ %H:%M %Z')
                 dt_str = f" on {formatted_dt}"
             except (ValueError, TypeError) as parse_err:
                 log_warning("task_query_service", fn_name, f"Could not parse/convert gcal_start_datetime '{gcal_start_str}' for {task_data.get('event_id')}. Error: {parse_err}. Falling back.")
                 dt_str = f" (Time Error: {gcal_start_str})"
        elif meta_date:
             dt_str = f" on {meta_date}"
             if meta_time: dt_str += f" at {meta_time}"
             else: dt_str += " (All day)"

        if dt_str: parts.append(dt_str)

        project = task_data.get("project")
        if project: parts.append(f"{{{project}}}")

        if item_type.lower() in ["task", "reminder"]:
            status = str(task_data.get("status", "pending")).capitalize()
            parts.append(f"[{status}]")

        main_line = " ".join(p for p in parts if p)

        # --- ADDED: Fetch and Display Scheduled Session Details ---
        session_details_lines = []
        # Check type is Task, calendar_api is valid, and session_event_ids exists
        if item_type.lower() == "task" and calendar_api and task_data.get("session_event_ids"):
            sessions_json = task_data.get("session_event_ids")
            if isinstance(sessions_json, str): # Ensure it's a string before trying to parse
                session_ids = []
                try:
                    session_ids = json.loads(sessions_json)
                    if not isinstance(session_ids, list): session_ids = [] # Ensure it parsed to a list
                except json.JSONDecodeError:
                    log_warning("task_query_service", fn_name, f"Could not parse session_event_ids JSON for task {task_data.get('event_id')}: {sessions_json}")
                    session_details_lines.append("    └── (Error reading session data)")
                except Exception as e: # Catch unexpected parsing errors
                    log_error("task_query_service", fn_name, f"Unexpected error parsing session JSON for task {task_data.get('event_id')}: {e}", e)
                    session_details_lines.append("    └── (Error reading session data)")

                if session_ids:
                    session_details_lines.append("    └── Scheduled Sessions:") # Header for sessions
                    session_num = 0
                    for session_id in session_ids:
                        # Ensure session_id is a non-empty string before proceeding
                        if not isinstance(session_id, str) or not session_id.strip():
                            log_warning("task_query_service", fn_name, f"Skipping invalid session ID: {session_id} for task {task_data.get('event_id')}")
                            continue

                        try:
                            # Fetch session details from GCal
                            session_event_data = calendar_api._get_single_event(session_id) # Use internal getter
                            if session_event_data:
                                session_num += 1
                                parsed_session = calendar_api._parse_google_event(session_event_data) # Parse it
                                session_start_str = parsed_session.get("gcal_start_datetime")
                                session_end_str = parsed_session.get("gcal_end_datetime")
                                session_time_info = "(Error parsing time)" # Default

                                # Attempt to parse and format times
                                if session_start_str and session_end_str:
                                    try:
                                        s_aware = datetime.fromisoformat(session_start_str.replace('Z', '+00:00'))
                                        e_aware = datetime.fromisoformat(session_end_str.replace('Z', '+00:00'))
                                        s_local = s_aware.astimezone(user_tz)
                                        e_local = e_aware.astimezone(user_tz)
                                        # Format: Date - Start HH:MM to End HH:MM (TZ)
                                        session_time_info = s_local.strftime('%Y-%m-%d %H:%M') + e_local.strftime('-%H:%M %Z')
                                    except (ValueError, TypeError):
                                         log_warning("task_query_service", fn_name, f"Error parsing session time for {session_id}")
                                         session_time_info = f"{session_start_str} to {session_end_str} (parse error)"

                                # Add line for this session (further indented)
                                session_details_lines.append(f"        {session_num}) {session_time_info} (ID: {session_id})")
                            else:
                                log_warning("task_query_service", fn_name, f"Could not fetch details for session ID {session_id}")
                                # Optionally add a line indicating it wasn't found
                                # session_details_lines.append(f"        - Session ID {session_id} not found in calendar.")
                        except Exception as fetch_err:
                            log_error("task_query_service", fn_name, f"Error fetching/processing session {session_id}", fetch_err)
                            session_details_lines.append(f"        - Error fetching session ID {session_id}")

                    # Only keep the header if actual sessions were found/processed
                    if session_num == 0 and session_details_lines and "Scheduled Sessions:" in session_details_lines[0]:
                         session_details_lines.pop(0) # Remove header if no sessions listed below it
        # --- END OF ADDED SECTION ---

        # Combine main line and session details
        return main_line + ("\n" + "\n".join(session_details_lines) if session_details_lines else "")

    except Exception as e:
        # Adding traceback here can help debug formatting errors
        import traceback
        log_error("task_query_service", fn_name, f"General error formatting item line {task_data.get('event_id')}. Error: {e}\n{traceback.format_exc()}", e)
        return f"Error displaying item: {task_data.get('event_id', 'Unknown ID')}"

# --- Public Service Functions ---

def get_formatted_list(user_id, date_range=None, status_filter='active', project_filter=None, trigger_sync=False): # Removed type hints
    """
    Gets tasks, filters, sorts, formats into numbered list string & mapping.
    Includes fetching and displaying details for scheduled task sessions.
    """
    fn_name = "get_formatted_list"
    log_info("task_query_service", fn_name, f"Executing for user={user_id}, Status={status_filter}, Range={date_range}, Proj={project_filter}")

    if trigger_sync:
        log_info("task_query_service", fn_name, "Sync triggered (NYI).")

    task_list = []
    calendar_api = None
    user_tz_str = "UTC" # Default timezone

    try:
        # Get task context from agent state
        task_list = get_context(user_id) or []

        # Get Calendar API instance (needed for session details)
        calendar_api = _get_calendar_api_from_state(user_id)
        if not calendar_api:
            log_info("task_query_service", fn_name, f"Calendar API not active for {user_id}, session details will not be fetched for list.")

        # Get user timezone from preferences
        agent_state = get_agent_state(user_id) # Fetch state once
        prefs = agent_state.get("preferences", {}) if agent_state else {}
        user_tz_str = prefs.get("TimeZone", "UTC") # Use default if not set

    except Exception as e:
        log_error("task_query_service", fn_name, f"Error getting context/prefs for {user_id}", e)
        # Continue with potentially empty task_list and no calendar_api

    # Apply filters (no changes needed here)
    filtered_s = _filter_tasks_by_status(task_list, status_filter)
    filtered_d = filtered_s
    if date_range:
        filtered_d = _filter_tasks_by_date_range(filtered_s, date_range)
    final_tasks = filtered_d
    if project_filter:
        final_tasks = _filter_tasks_by_project(filtered_d, project_filter)

    if not final_tasks:
        log_info("task_query_service", fn_name, f"No tasks match criteria for {user_id}.")
        return "", {}

    # Sort tasks (no changes needed here)
    sorted_tasks = _sort_tasks(final_tasks)

    # Format lines and build mapping
    lines, mapping = [], {}
    item_num = 0
    for task in sorted_tasks:
        item_id = task.get("event_id")
        if not item_id:
            log_warning("task_query_service", fn_name, f"Skipping item missing event_id: {task.get('title')}")
            continue

        item_num += 1
        # Pass API instance and timezone to formatting function
        formatted_line = _format_task_line(task, user_tz_str, calendar_api)
        lines.append(f"{item_num}. {formatted_line}")
        mapping[str(item_num)] = item_id # Use string key for mapping

    if item_num == 0:
        # This case should be rare now unless _format_task_line errors out for all items
        log_warning("task_query_service", fn_name, f"Formatting resulted in zero list items for {user_id}.")
        return "Error formatting list.", {}

    list_body = "\n".join(lines)
    log_info("task_query_service", fn_name, f"Generated list body ({len(mapping)} items, including session details) for {user_id}")
    return list_body, mapping

def get_tasks_for_summary(user_id, date_range, status_filter='active', trigger_sync=False):
    """Gets tasks, filters, sorts and returns list of dictionaries."""
    # *** CORRECTED LOGGING ***
    fn_name = "get_tasks_for_summary"
    log_info("task_query_service", fn_name, f"Executing for user={user_id}, Filter={status_filter}, Range={date_range}")
    # *************************

    if trigger_sync:
        log_info("task_query_service", fn_name, "Sync triggered (NYI).")

    try:
        task_list = get_context(user_id) or []
    except Exception as e:
        log_error("task_query_service", fn_name, f"Error getting context for {user_id}", e)
        task_list = []

    filtered_s = _filter_tasks_by_status(task_list, status_filter)
    # Ensure date_range is a tuple or list of two strings
    final_tasks = filtered_s
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        final_tasks = _filter_tasks_by_date_range(filtered_s, date_range)
    elif date_range is not None:
         log_warning("task_query_service", fn_name, f"Invalid date_range format for summary: {date_range}. Skipping date filter.")


    sorted_tasks = _sort_tasks(final_tasks)
    log_info("task_query_service", fn_name, f"Returning {len(sorted_tasks)} tasks for summary {user_id}")
    return sorted_tasks

def get_context_snapshot(user_id, history_weeks=1, future_weeks=2):
    """Fetches relevant active WT tasks and GCal events for Orchestrator context."""
    fn_name = "get_context_snapshot"
    log_info("task_query_service", fn_name, f"Get Context Snapshot: User={user_id}")
    task_context, calendar_context = [], []
    try:
        # Note: This currently only gets WT tasks from context, not external GCal events
        # To include external events, this would need to call the new sync_service function
        # For now, keeping it simple as per current structure before sync service exists.

        today = datetime.now().date()
        start_date = today - timedelta(weeks=history_weeks)
        end_date = today + timedelta(weeks=future_weeks)
        start_str, end_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
        date_range_tuple = (start_str, end_str)

        # Get WT tasks from the current in-memory context
        task_context = get_tasks_for_summary(user_id, date_range=date_range_tuple, status_filter='active')

        # Get GCal events directly from API
        calendar_api = _get_calendar_api_from_state(user_id)
        if calendar_api:
            try:
                # Fetch GCal events - list_events returns parsed dicts
                calendar_context = calendar_api.list_events(start_str, end_str)
                log_info("task_query_service", fn_name, f"Fetched {len(calendar_context)} GCal events for snapshot.")
            except Exception as cal_e:
                log_error("task_query_service", fn_name, f"Failed fetch calendar events for snapshot: {cal_e}", cal_e) # Log exception too
                # Continue without calendar context
        else:
            log_info("task_query_service", fn_name, f"Calendar API not active for {user_id}, skipping GCal fetch for snapshot.")

        log_info("task_query_service", fn_name, f"Snapshot created: {len(task_context)} tasks, {len(calendar_context)} events.")

    except Exception as e:
        # Log the error *here* within the function's context
        log_error("task_query_service", fn_name, f"Error creating context snapshot for {user_id}", e)
        # Return empty lists, the calling function will see the error log
    return task_context, calendar_context
# --- END OF FILE services/task_query_service.py ---