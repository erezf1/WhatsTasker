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

# Central logger
from tools.logger import log_info, log_error, log_warning

# Database access module
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "activity_db not found. Sync service disabled.", None)
    DB_IMPORTED = False
    # Dummy DB functions
    class activity_db:
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
        @staticmethod
        def add_or_update_task(*args, **kwargs): return False
# --- End DB Import ---

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
    def update_task_in_context(*args, **kwargs): pass # Dummy

# Google Calendar API (for type checking and fetching events)
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
    log_warning("sync_service", "import", "GoogleCalendarAPI not found. Sync will only show DB tasks.")
    GoogleCalendarAPI = None
    GCAL_API_IMPORTED = False


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

    # 1. Get Calendar API instance
    calendar_api = None
    if USER_MANAGER_IMPORTED and GCAL_API_IMPORTED and GoogleCalendarAPI is not None:
        agent_state = get_agent(user_id)
        if agent_state:
            calendar_api_maybe = agent_state.get("calendar")
            if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
                calendar_api = calendar_api_maybe

    # 2. Fetch GCal Events if API is available
    gcal_events_list = []
    if calendar_api:
        try:
            #log_info("sync_service", fn_name, f"Fetching GCal events for {user_id}...")
            # list_events returns parsed dicts including 'event_id', 'gcal_start_datetime' etc.
            gcal_events_list = calendar_api.list_events(start_date_str, end_date_str)
            log_info("sync_service", fn_name, f"Fetched {len(gcal_events_list)} GCal events for {user_id}.")
        except Exception as e:
            log_error("sync_service", fn_name, f"Error fetching GCal events for {user_id}", e, user_id=user_id)
            # Continue without GCal events
    else:
        log_info("sync_service", fn_name, f"GCal API not available or inactive for {user_id}, skipping GCal fetch.")

    # 3. Fetch WT Tasks from Database within the same date range
    wt_tasks_list = []
    try:
        #log_info("sync_service", fn_name, f"Fetching WT tasks from DB for {user_id}...")
        # Fetch tasks based on the 'date' column matching the range
        # We don't filter by status here; we want all potentially relevant WT items
        wt_tasks_list = activity_db.list_tasks_for_user(
            user_id=user_id,
            date_range=(start_date_str, end_date_str)
            # status_filter=None # Get all statuses within the date range
        )
        log_info("sync_service", fn_name, f"Fetched {len(wt_tasks_list)} WT tasks from DB for {user_id} in range.")
    except Exception as e:
        log_error("sync_service", fn_name, f"Error fetching WT tasks from DB for {user_id}", e, user_id=user_id)
        # If DB fails, should we proceed with only GCal events? Or return empty?
        # Let's return only GCal events if DB fails, but log error clearly.
        # Fall through, wt_tasks_list will be empty.

    # 4. Create Maps for Efficient Lookup
    gcal_events_map = {e['event_id']: e for e in gcal_events_list if e.get('event_id')}
    wt_tasks_map = {t['event_id']: t for t in wt_tasks_list if t.get('event_id')}

    # 5. Merge & Identify Types
    aggregated_context_list: List[Dict[str, Any]] = []
    processed_wt_ids = set() # Keep track of WT items found in GCal map

    # Iterate through GCal events first
    for event_id, gcal_data in gcal_events_map.items():
        if event_id in wt_tasks_map:
            # --- WT Item Found in GCal ---
            processed_wt_ids.add(event_id)
            task_data = wt_tasks_map[event_id] # The task data from our DB
            merged_data = task_data.copy() # Start with DB data
            needs_db_update = False

            # Check for differences that require updating our DB record
            gcal_start = gcal_data.get("gcal_start_datetime")
            gcal_end = gcal_data.get("gcal_end_datetime")
            gcal_title = gcal_data.get("title")
            gcal_desc = gcal_data.get("description")
            # Add GCal status if needed: gcal_status = gcal_data.get("status_gcal")

            # Update stored GCal times if they differ
            if gcal_start != merged_data.get("gcal_start_datetime"):
                merged_data["gcal_start_datetime"] = gcal_start
                needs_db_update = True
            if gcal_end != merged_data.get("gcal_end_datetime"):
                merged_data["gcal_end_datetime"] = gcal_end
                needs_db_update = True

            # Option 1: Always update title/desc from GCal if GCal link exists?
            # Option 2: Only update if DB fields are empty/default? (Safer)
            # Let's go with Option 2 for now to avoid overwriting user edits in WT potentially.
            if gcal_title and not merged_data.get("title", "").strip():
                 merged_data["title"] = gcal_title
                 needs_db_update = True
            if gcal_desc and not merged_data.get("description", "").strip():
                 merged_data["description"] = gcal_desc
                 needs_db_update = True
            # Potentially sync status? If GCal event is 'cancelled', should WT task be? Complex rule. Skip for now.

            # If the merged data differs from original DB data, update DB
            if needs_db_update:
                log_info("sync_service", fn_name, f"GCal data changed for WT item {event_id}. Updating DB.")
                try:
                    # add_or_update_task expects list for session IDs
                    if isinstance(merged_data.get("session_event_ids"), str): # Ensure it's list before saving
                        try: merged_data["session_event_ids"] = json.loads(merged_data["session_event_ids"])
                        except: merged_data["session_event_ids"] = []

                    update_success = activity_db.add_or_update_task(merged_data)
                    if update_success and AGENT_STATE_IMPORTED:
                        # Update in-memory context as well
                        updated_data_from_db = activity_db.get_task(event_id) # Re-fetch to get latest state
                        if updated_data_from_db: update_task_in_context(user_id, event_id, updated_data_from_db)
                    elif not update_success:
                         log_error("sync_service", fn_name, f"Failed DB update for WT item {event_id} after GCal merge.", user_id=user_id)

                except Exception as save_err:
                     log_error("sync_service", fn_name, f"Unexpected error saving updated metadata for WT item {event_id} after GCal merge.", save_err, user_id=user_id)

            # Add the (potentially updated) merged data to the context list
            aggregated_context_list.append(merged_data)

        else:
            # --- External GCal Event (Not in our DB) ---
            external_event_data = gcal_data.copy() # Start with GCal data
            external_event_data["type"] = "external_event" # Mark its type
            external_event_data["user_id"] = user_id # Ensure user_id is present
            # Ensure required fields for formatting have some value?
            external_event_data.setdefault("status", None) # External events don't have WT status
            aggregated_context_list.append(external_event_data)

    # 6. Add WT Tasks Not Found in GCal Fetch Window
    for event_id, task_data in wt_tasks_map.items():
        if event_id not in processed_wt_ids:
            # This is a WT item (task/reminder) that wasn't in the GCal list for this window.
            # Could be a local-only task, or GCal event outside window, or deleted from GCal.
            # We still want it in the context if it's relevant (e.g., active status).
            log_info("sync_service", fn_name, f"Including WT item {event_id} (status: {task_data.get('status')}) which was not found in GCal fetch window.")
            # Make sure session IDs are list (should be from DB layer)
            if isinstance(task_data.get("session_event_ids"), str):
                 try: task_data["session_event_ids"] = json.loads(task_data["session_event_ids"])
                 except: task_data["session_event_ids"] = []
            aggregated_context_list.append(task_data) # Add the DB data as is

    #log_info("sync_service", fn_name, f"Generated aggregated context with {len(aggregated_context_list)} items for {user_id}.")
    # Sort the final aggregated list before returning? Good for routines.
    sorted_aggregated_context = _sort_tasks(aggregated_context_list) # Use the existing sort helper
    return sorted_aggregated_context

# --- Placeholder for future full sync ---
def perform_full_sync(user_id: str):
    """(NOT IMPLEMENTED) Placeholder for a more complex two-way sync."""
    log_warning("sync_service", "perform_full_sync", f"Full two-way sync not implemented. User: {user_id}")
    # This would involve:
    # 1. Fetching *all* relevant GCal events (wider date range? or use sync tokens?)
    # 2. Fetching *all* non-cancelled WT tasks from DB.
    # 3. Complex diffing logic to identify:
    #    - New GCal events -> Create corresponding 'external_event' metadata in DB? (Optional)
    #    - New WT tasks -> Create in GCal? (Maybe only if scheduled?)
    #    - Updated GCal events -> Update corresponding WT task metadata in DB.
    #    - Updated WT tasks -> Update corresponding GCal event? (Be careful!)
    #    - Deleted GCal events -> Update status or delete WT task metadata?
    #    - Deleted WT tasks (marked cancelled) -> Delete GCal event?
    # 4. Handling conflicts gracefully.
    # 5. Updating last_sync timestamp in user preferences.
    pass

# Helper from task_query_service might be needed here if not importing that module
def _sort_tasks(task_list: List[Dict]) -> List[Dict]:
    """Sorts task list robustly by date/time."""
    fn_name = "_sort_tasks_sync" # Different name to avoid potential conflicts if imported elsewhere
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