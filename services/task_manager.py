# --- START OF REFACTORED services/task_manager.py ---
"""
Service layer for managing tasks: creating, updating, cancelling, and scheduling sessions.
Interacts with Google Calendar API and the SQLite database via activity_db.
"""
import json
import traceback
import uuid
from datetime import datetime, timedelta, timezone
import re
from typing import Dict, List, Any # Keep typing for internal use

# Tool/Service Imports
try:
    from tools.logger import log_info, log_error, log_warning
except ImportError:
    # Basic fallback logger
    import logging; logging.basicConfig(level=logging.INFO)
    log_info=logging.info; log_error=logging.error; log_warning=logging.warning
    log_error("task_manager", "import", "Logger failed import.")

# Database Utility Import (Primary Data Source Now)
try:
    import tools.activity_db as activity_db
    DB_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "activity_db not found. Task management disabled.", None)
    DB_IMPORTED = False
    # Define dummy DB functions if import fails to prevent crashes later
    class activity_db:
        @staticmethod
        def add_or_update_task(*args, **kwargs): return False
        @staticmethod
        def get_task(*args, **kwargs): return None
        @staticmethod
        def delete_task(*args, **kwargs): return False
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
    # TODO: Consider if the application should halt if the DB module can't be imported

# GCal API Import
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    GCAL_API_IMPORTED = True
except ImportError:
    log_warning("task_manager", "import", "GoogleCalendarAPI not found, GCal features disabled.")
    GoogleCalendarAPI = None
    GCAL_API_IMPORTED = False

# Agent State Manager Import (for updating in-memory context)
try:
    from services.agent_state_manager import get_agent_state, add_task_to_context, update_task_in_context, remove_task_from_context
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "AgentStateManager not found. In-memory context updates skipped.")
    AGENT_STATE_MANAGER_IMPORTED = False
    # Dummy functions
    def get_agent_state(*a, **k): return None
    def add_task_to_context(*a, **k): pass
    def update_task_in_context(*a, **k): pass
    def remove_task_from_context(*a, **k): pass

# Constants
DEFAULT_REMINDER_DURATION = "15m"

# --- Helper Functions (Keep these as they are useful internally) ---
# Returns GoogleCalendarAPI instance or None
def _get_calendar_api(user_id):
    """Safely retrieves the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api"
    if not AGENT_STATE_MANAGER_IMPORTED or not GCAL_API_IMPORTED or GoogleCalendarAPI is None:
        # log_warning("task_manager", fn_name, f"Cannot get calendar API for {user_id}: Dependencies missing.")
        return None
    agent_state = get_agent_state(user_id)
    if agent_state is not None:
        calendar_api = agent_state.get("calendar")
        if isinstance(calendar_api, GoogleCalendarAPI) and calendar_api.is_active():
            return calendar_api
        # Optionally log if inactive:
        # elif isinstance(calendar_api, GoogleCalendarAPI) and not calendar_api.is_active():
        #      log_info("task_manager", fn_name, f"Calendar API found but inactive for user {user_id}.")
    # else: log_warning("task_manager", fn_name, f"Agent state not found for user {user_id}.")
    return None

# Returns int (minutes) or None
def _parse_duration_to_minutes(duration_str):
    """Parses duration strings like '2h', '90m', '1.5h' into minutes."""
    fn_name = "_parse_duration_to_minutes"
    if not duration_str or not isinstance(duration_str, str): return None
    duration_str = duration_str.lower().replace(' ',''); total_minutes = 0.0
    try:
        hour_match = re.search(r'(\d+(\.\d+)?)\s*h', duration_str)
        minute_match = re.search(r'(\d+)\s*m', duration_str)
        if hour_match: total_minutes += float(hour_match.group(1)) * 60
        if minute_match: total_minutes += int(minute_match.group(1))
        if total_minutes == 0 and hour_match is None and minute_match is None:
             if duration_str.replace('.','',1).isdigit(): total_minutes = float(duration_str)
             else: raise ValueError("Unrecognized duration format")
        return int(round(total_minutes)) if total_minutes > 0 else None
    except (ValueError, TypeError, AttributeError) as e:
        log_warning("task_manager", fn_name, f"Could not parse duration string '{duration_str}': {e}")
        return None

# ==============================================================
# Core Service Functions (Refactored for SQLite via activity_db)
# ==============================================================

# Returns dict (saved item) or None
def create_task(user_id: str, task_params: Dict[str, Any]) -> Dict | None:
    """
    Creates a task or reminder, saves metadata to DB, optionally adds to GCal.
    Returns the dictionary representing the saved task/reminder, or None on failure.
    """
    fn_name = "create_task"
    if not DB_IMPORTED:
        log_error("task_manager", fn_name, "Database module not imported. Cannot create task.")
        return None

    item_type = task_params.get("type")
    if not item_type:
        log_error("task_manager", fn_name, "Missing 'type' in task_params.", user_id=user_id)
        return None
    log_info("task_manager", fn_name, f"Creating item for {user_id}, type: {item_type}")

    calendar_api = _get_calendar_api(user_id)
    google_event_id = None # Track GCal ID for potential rollback

    try:
        # --- Prepare Core Metadata ---
        task_data_to_save = {}
        task_data_to_save["user_id"] = user_id
        task_data_to_save["type"] = item_type
        task_data_to_save["status"] = "pending" # Default status
        task_data_to_save["created_at"] = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
        task_data_to_save["title"] = task_params.get("description", "Untitled Item") # Use description as title
        # Copy other relevant fields from input params if they exist in TASK_FIELDS
        for field in activity_db.TASK_FIELDS:
             if field in task_params and field not in task_data_to_save:
                 task_data_to_save[field] = task_params[field]

        # --- Handle GCal Integration (Reminders with time) ---
        item_time = task_params.get("time")
        has_time = item_time is not None and item_time != ""
        should_create_gcal = item_type == "reminder" and has_time and calendar_api is not None

        if should_create_gcal:
            log_info("task_manager", fn_name, f"Attempting GCal creation for reminder {user_id}...")
            gcal_event_payload = { # Simplified payload for GCalAPI
                "title": task_data_to_save.get("title"),
                "description": f"Reminder: {task_data_to_save.get('description', '')}",
                "date": task_params.get("date"),
                "time": item_time,
                "duration": DEFAULT_REMINDER_DURATION
            }
            try:
                # create_event returns event ID string or None
                created_event_id = calendar_api.create_event(gcal_event_payload)
                if created_event_id is not None:
                    log_info("task_manager", fn_name, f"GCal Reminder created, ID: {created_event_id}")
                    task_data_to_save["event_id"] = created_event_id # Use GCal ID as primary key
                    google_event_id = created_event_id # Track for rollback
                    # Fetch GCal details to store exact times
                    gcal_details = calendar_api._get_single_event(created_event_id)
                    if gcal_details:
                        parsed = calendar_api._parse_google_event(gcal_details)
                        task_data_to_save["gcal_start_datetime"] = parsed.get("gcal_start_datetime")
                        task_data_to_save["gcal_end_datetime"] = parsed.get("gcal_end_datetime")
                    else: log_warning("task_manager", fn_name, f"Failed to fetch details for GCal event {created_event_id}")
                else:
                    log_warning("task_manager", fn_name, f"GCal event creation failed (returned None) for {user_id}. Using local ID.")
                    task_data_to_save["event_id"] = f"local_{uuid.uuid4()}"
            except Exception as gcal_err:
                log_error("task_manager", fn_name, f"Error creating GCal event for {user_id}", gcal_err, user_id=user_id)
                task_data_to_save["event_id"] = f"local_{uuid.uuid4()}" # Fallback to local ID
        else:
            # Assign local ID for tasks or reminders without time/API
            task_data_to_save["event_id"] = f"local_{uuid.uuid4()}"
            if item_type == "reminder" and has_time and calendar_api is None:
                 log_info("task_manager", fn_name, f"Assigning local ID for reminder {user_id}. Reason: GCal API inactive.")

        # --- Save to Database ---
        if not task_data_to_save.get("event_id"): # Should not happen, but safety check
             log_error("task_manager", fn_name, "Failed to assign event_id before DB save.", user_id=user_id)
             return None

        # Fill missing defaults before saving (optional, add_or_update_task handles some)
        task_data_to_save.setdefault('sessions_planned', 0)
        task_data_to_save.setdefault('sessions_completed', 0)
        task_data_to_save.setdefault('progress_percent', 0)
        task_data_to_save.setdefault('session_event_ids', '[]')

        save_success = activity_db.add_or_update_task(task_data_to_save)

        if save_success:
            log_info("task_manager", fn_name, f"Task {task_data_to_save['event_id']} saved to DB for {user_id}")
            # Fetch the potentially updated data from DB to return it
            saved_data = activity_db.get_task(task_data_to_save['event_id'])
            if saved_data and AGENT_STATE_MANAGER_IMPORTED:
                add_task_to_context(user_id, saved_data) # Update memory context
            return saved_data if saved_data else task_data_to_save # Return DB data if possible
        else:
            log_error("task_manager", fn_name, f"Failed to save task {task_data_to_save.get('event_id')} to DB for {user_id}.", user_id=user_id)
            # Attempt GCal rollback if DB save failed *after* GCal success
            if google_event_id is not None and calendar_api is not None:
                log_warning("task_manager", fn_name, f"DB save failed. Rolling back GCal event {google_event_id}")
                try: calendar_api.delete_event(google_event_id)
                except Exception: log_error("task_manager", fn_name, f"GCal rollback failed for {google_event_id}", user_id=user_id)
            return None

    except Exception as e:
        # Catch any unexpected errors during preparation
        log_error("task_manager", fn_name, f"Unexpected error during task creation for {user_id}", e, user_id=user_id)
        # Rollback GCal if it was created before the unexpected error
        if google_event_id is not None and calendar_api is not None:
             log_warning("task_manager", fn_name, f"Unexpected error. Rolling back GCal event {google_event_id}")
             try: calendar_api.delete_event(google_event_id)
             except Exception: log_error("task_manager", fn_name, f"GCal rollback failed for {google_event_id}", user_id=user_id)
        return None

# Returns dict (updated item) or None
def update_task(user_id: str, item_id: str, updates: Dict[str, Any]) -> Dict | None:
    """Updates details of an existing task/reminder in the DB and GCal."""
    fn_name = "update_task"
    if not DB_IMPORTED:
        log_error("task_manager", fn_name, "Database module not imported. Cannot update task.")
        return None

    log_info("task_manager", fn_name, f"Updating item {item_id} for {user_id}, keys: {list(updates.keys())}")

    # 1. Get existing task data from DB
    existing_task = activity_db.get_task(item_id) # Returns dict or None
    if existing_task is None:
        log_error("task_manager", fn_name, f"Task {item_id} not found in DB.", user_id=user_id)
        return None
    if existing_task.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"User mismatch for task {item_id}.", user_id=user_id)
        return None

    calendar_api = _get_calendar_api(user_id)
    item_type = existing_task.get("type")
    gcal_updated = False # Track GCal update success

    # 2. Handle GCal Update (Reminders only)
    if item_type == "reminder" and not item_id.startswith("local_") and calendar_api is not None:
        gcal_payload = {}
        needs_gcal_update = False
        if "description" in updates:
            gcal_payload["title"] = updates["description"]
            gcal_payload["description"] = f"Reminder: {updates['description']}"
            needs_gcal_update = True
        if "date" in updates or "time" in updates:
            gcal_payload["date"] = updates.get("date", existing_task.get("date"))
            gcal_payload["time"] = updates.get("time") if "time" in updates else existing_task.get("time")
            if gcal_payload["date"] is not None: needs_gcal_update = True

        if needs_gcal_update:
            log_info("task_manager", fn_name, f"Attempting GCal update for reminder {item_id}")
            try:
                update_success = calendar_api.update_event(item_id, gcal_payload)
                if update_success:
                    gcal_updated = True
                    log_info("task_manager", fn_name, f"GCal reminder {item_id} updated successfully.")
                else: log_warning("task_manager", fn_name, f"GCal reminder {item_id} update failed (API returned False).")
            except Exception as gcal_err:
                 log_error("task_manager", fn_name, f"Error updating GCal reminder {item_id}", gcal_err, user_id=user_id)
        else: log_info("task_manager", fn_name, f"No relevant fields for GCal reminder {item_id} update.")

    # 3. Prepare DB Updates
    db_update_data = existing_task.copy() # Start with existing data
    # Apply valid updates from the input 'updates' dictionary
    allowed_meta_keys = {"description", "date", "time", "estimated_duration", "project"}
    applied_db_updates = False
    for key, value in updates.items():
        if key in allowed_meta_keys:
            db_update_data[key] = value
            if key == 'description': db_update_data['title'] = value # Keep title synced
            applied_db_updates = True

    # 4. Refresh GCal Timestamps if GCal was updated
    if gcal_updated and calendar_api is not None:
        gcal_details = calendar_api._get_single_event(item_id)
        if gcal_details:
            parsed = calendar_api._parse_google_event(gcal_details)
            db_update_data["gcal_start_datetime"] = parsed.get("gcal_start_datetime")
            db_update_data["gcal_end_datetime"] = parsed.get("gcal_end_datetime")
            applied_db_updates = True # Mark as updated even if only GCal times changed
            log_info("task_manager", fn_name, f"Refreshed GCal times in task data for {item_id}")
        else:
            log_warning("task_manager", fn_name, f"GCal update ok, but failed to re-fetch details for {item_id}")

    # 5. Save to DB if changes were applied or GCal timestamps were refreshed
    if applied_db_updates:
        save_success = activity_db.add_or_update_task(db_update_data)
        if save_success:
            log_info("task_manager", fn_name, f"Task {item_id} updated successfully in DB.")
            # Fetch final state from DB
            updated_task_from_db = activity_db.get_task(item_id)
            if updated_task_from_db and AGENT_STATE_MANAGER_IMPORTED:
                 update_task_in_context(user_id, item_id, updated_task_from_db) # Update memory
            return updated_task_from_db if updated_task_from_db else db_update_data
        else:
            log_error("task_manager", fn_name, f"Failed to save task {item_id} updates to DB.", user_id=user_id)
            return None # DB save failed
    else:
        log_info("task_manager", fn_name, f"No applicable DB updates or GCal timestamp changes for task {item_id}.")
        return existing_task # Return original data if no changes were made

# Returns dict (updated item) or None
def update_task_status(user_id: str, item_id: str, new_status: str) -> Dict | None:
    """Updates only the status and related tracking fields in the DB."""
    fn_name = "update_task_status"
    if not DB_IMPORTED:
        log_error("task_manager", fn_name, "Database module not imported.")
        return None

    log_info("task_manager", fn_name, f"Setting status='{new_status}' for item {item_id}, user {user_id}")
    # Validate and clean status
    new_status_clean = new_status.lower().replace(" ", "")
    allowed_statuses = {"pending", "in_progress", "completed"}
    if new_status_clean == "cancelled":
        log_error("task_manager", fn_name, "Use cancel_item() function for 'cancelled' status.", user_id=user_id)
        return None
    if new_status_clean not in allowed_statuses:
         log_error("task_manager", fn_name, f"Invalid status '{new_status}' provided.", user_id=user_id)
         return None

    # 1. Get existing task data
    existing_task = activity_db.get_task(item_id)
    if existing_task is None:
        log_error("task_manager", fn_name, f"Task {item_id} not found in DB.", user_id=user_id)
        return None
    if existing_task.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"User mismatch for task {item_id}.", user_id=user_id)
        return None

    # 2. Prepare update dictionary
    updates_dict = {"status": new_status_clean}
    now_iso_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'

    if new_status_clean == "completed":
        updates_dict["completed_at"] = now_iso_utc
        updates_dict["progress_percent"] = 100
        if existing_task.get("type") == "task":
            updates_dict["sessions_completed"] = existing_task.get("sessions_planned", 0)
    elif new_status_clean == "pending":
         updates_dict["completed_at"] = None # Use None for DB
         updates_dict["progress_percent"] = 0
         updates_dict["sessions_completed"] = 0
    elif new_status_clean == "in_progress":
         updates_dict["completed_at"] = None # Use None for DB

    # 3. Apply updates to a copy and save
    task_data_to_save = existing_task.copy()
    task_data_to_save.update(updates_dict)

    save_success = activity_db.add_or_update_task(task_data_to_save)

    if save_success:
        log_info("task_manager", fn_name, f"Task {item_id} status updated to {new_status_clean} in DB.")
        updated_task_from_db = activity_db.get_task(item_id) # Re-fetch to get final state
        if updated_task_from_db and AGENT_STATE_MANAGER_IMPORTED:
             update_task_in_context(user_id, item_id, updated_task_from_db)
        return updated_task_from_db if updated_task_from_db else task_data_to_save
    else:
        log_error("task_manager", fn_name, f"Failed to save status update for task {item_id} to DB.", user_id=user_id)
        return None

# Returns bool
def cancel_item(user_id: str, item_id: str) -> bool:
    """Sets item status to 'cancelled' in DB and deletes associated GCal events."""
    fn_name = "cancel_item"
    if not DB_IMPORTED:
        log_error("task_manager", fn_name, "Database module not imported.")
        return False

    log_info("task_manager", fn_name, f"Processing cancellation for item {item_id}, user {user_id}")

    # 1. Get task data from DB
    task_data = activity_db.get_task(item_id)
    if task_data is None:
        log_warning("task_manager", fn_name, f"Task {item_id} not found in DB during cancel. Assuming handled.")
        return True # Not found -> already gone? Success.
    if task_data.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"User mismatch for item {item_id}.", user_id=user_id)
        return False
    if task_data.get("status") == "cancelled":
        log_info("task_manager", fn_name, f"Item {item_id} is already cancelled.")
        return True

    # 2. GCal Cleanup
    calendar_api = _get_calendar_api(user_id)
    item_type = task_data.get("type")
    gcal_cleanup_errors = []

    if calendar_api is not None and not item_id.startswith("local_"):
        log_info("task_manager", fn_name, f"Performing GCal cleanup for {item_type} {item_id}")
        # Delete main event if it's a Reminder on GCal
        if item_type == "reminder":
            try:
                deleted = calendar_api.delete_event(item_id)
                if not deleted: log_warning("task_manager", fn_name, f"GCal delete failed/not found for reminder {item_id}")
            except Exception as del_err:
                 log_error("task_manager", fn_name, f"Error deleting GCal reminder {item_id}", del_err, user_id=user_id)
                 gcal_cleanup_errors.append(f"Main event {item_id}")
        # Delete session events if it's a Task
        elif item_type == "task":
            session_ids = task_data.get("session_event_ids", []) # Already decoded list from get_task
            if isinstance(session_ids, list) and session_ids:
                log_info("task_manager", fn_name, f"Deleting {len(session_ids)} GCal sessions for task {item_id}")
                for session_id in session_ids:
                    if not isinstance(session_id, str) or not session_id: continue
                    try:
                        deleted = calendar_api.delete_event(session_id)
                        if not deleted: log_warning("task_manager", fn_name, f"GCal delete failed/not found for session {session_id}")
                    except Exception as sess_del_err:
                         log_error("task_manager", fn_name, f"Error deleting GCal session {session_id}", sess_del_err, user_id=user_id)
                         gcal_cleanup_errors.append(f"Session {session_id}")
            else: log_info("task_manager", fn_name, f"No GCal session IDs to delete for task {item_id}")
    # else: log reason for skipping GCal cleanup

    # 3. Update DB Status
    update_payload = task_data.copy()
    update_payload["status"] = "cancelled"
    # Reset task-specific fields on cancel
    if item_type == "task":
        update_payload["sessions_planned"] = 0
        update_payload["sessions_completed"] = 0
        update_payload["progress_percent"] = 0
        update_payload["session_event_ids"] = [] # Store empty list, will be JSON '[]'

    save_success = activity_db.add_or_update_task(update_payload)

    if save_success:
        log_info("task_manager", fn_name, f"Successfully marked item {item_id} as cancelled in DB.")
        cancelled_task_data = activity_db.get_task(item_id) # Get final state
        if cancelled_task_data and AGENT_STATE_MANAGER_IMPORTED:
             update_task_in_context(user_id, item_id, cancelled_task_data) # Update memory context
        if gcal_cleanup_errors:
             log_warning("task_manager", fn_name, f"Cancel successful for {item_id}, but GCal cleanup errors: {gcal_cleanup_errors}", user_id=user_id)
        return True
    else:
        log_error("task_manager", fn_name, f"Failed to save cancelled status for {item_id} to DB.", user_id=user_id)
        # State is inconsistent: GCal might be cleaned, DB not updated.
        return False

# --- Scheduling Functions ---

# Returns dict with 'success', 'message', 'booked_count', 'session_ids'
def schedule_work_sessions(user_id: str, task_id: str, slots_to_book: List[Dict]) -> Dict:
    """Creates GCal events for proposed work sessions and updates the parent task in DB."""
    fn_name = "schedule_work_sessions"
    default_fail_result = {"success": False, "booked_count": 0, "message": "An unexpected error occurred.", "session_ids": []}
    if not DB_IMPORTED:
        return {**default_fail_result, "message": "Database module not available."}

    log_info("task_manager", fn_name, f"Booking {len(slots_to_book)} sessions for task {task_id}")

    calendar_api = _get_calendar_api(user_id)
    if calendar_api is None:
        return {**default_fail_result, "message": "Calendar is not connected or active."}

    # 1. Get Parent Task Details from DB
    task_metadata = activity_db.get_task(task_id)
    if task_metadata is None:
        log_error("task_manager", fn_name, f"Parent task {task_id} not found in DB.", user_id=user_id)
        return {**default_fail_result, "message": "Original task details not found."}
    if task_metadata.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"User mismatch for task {task_id}.", user_id=user_id)
        return {**default_fail_result, "message": "Task ownership mismatch."}
    if task_metadata.get("type") != "task":
         log_error("task_manager", fn_name, f"Item {task_id} is not a task.", user_id=user_id)
         return {**default_fail_result, "message": "Scheduling only supported for tasks."}

    task_title = task_metadata.get("title", "Task Work")

    # 2. Create GCal Events
    created_session_ids = []
    errors = []
    for i, session_slot in enumerate(slots_to_book):
        session_date = session_slot.get("date")
        session_time = session_slot.get("time")
        session_end_time = session_slot.get("end_time")
        if not all([session_date, session_time, session_end_time]):
             msg = f"Session {i+1} missing date/time/end_time"
             log_warning("task_manager", fn_name, msg + f" for task {task_id}")
             errors.append(msg); continue
        try:
            start_dt = datetime.strptime(f"{session_date} {session_time}", "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(f"{session_date} {session_end_time}", "%Y-%m-%d %H:%M")
            duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
            if duration_minutes <= 0: raise ValueError("Duration must be positive")

            session_event_data = {
                "title": f"Work: {task_title} [{i+1}/{len(slots_to_book)}]",
                "description": f"Focused work session for task: {task_title}\nParent Task ID: {task_id}",
                "date": session_date, "time": session_time,
                "duration": f"{duration_minutes}m"
            }
            # create_event returns ID string or None
            session_event_id = calendar_api.create_event(session_event_data)
            if session_event_id is not None:
                created_session_ids.append(session_event_id)
            else:
                msg = f"Session {i+1} GCal creation failed (API returned None)"
                log_error("task_manager", fn_name, msg + f" for task {task_id}.", user_id=user_id)
                errors.append(msg)
        except Exception as e:
            msg = f"Session {i+1} creation error: {type(e).__name__}"
            log_error("task_manager", fn_name, f"Error creating GCal session {i+1} for task {task_id}", e, user_id=user_id)
            errors.append(msg)

    if not created_session_ids:
        err_summary = "; ".join(errors) if errors else "Unknown reason"
        log_error("task_manager", fn_name, f"Failed to create any GCal sessions for task {task_id}. Errors: {err_summary}", user_id=user_id)
        return {**default_fail_result, "message": f"Sorry, couldn't add sessions to calendar. Errors: {err_summary}"}

    log_info("task_manager", fn_name, f"Created {len(created_session_ids)} GCal sessions for task {task_id}: {created_session_ids}")

    # 3. Update Parent Task in DB
    # Combine existing and new session IDs
    existing_session_ids = task_metadata.get("session_event_ids", []) # Already decoded list
    if not isinstance(existing_session_ids, list): existing_session_ids = []
    all_session_ids = list(set(existing_session_ids + created_session_ids))

    update_payload = task_metadata.copy()
    update_payload["sessions_planned"] = len(all_session_ids)
    update_payload["session_event_ids"] = all_session_ids # Store list, add_or_update handles JSON
    update_payload["status"] = "in_progress" # Mark task as in progress

    save_success = activity_db.add_or_update_task(update_payload)

    if save_success:
        log_info("task_manager", fn_name, f"Parent task {task_id} updated in DB with session info.")
        updated_task_data = activity_db.get_task(task_id) # Get final state
        if updated_task_data and AGENT_STATE_MANAGER_IMPORTED:
            update_task_in_context(user_id, task_id, updated_task_data) # Update memory

        num_booked = len(created_session_ids)
        plural_s = "s" if num_booked > 1 else ""
        msg = f"Okay, I've scheduled {num_booked} work session{plural_s} for '{task_title}' in your calendar."
        if errors: msg += f" (Issues with {len(errors)} other slots)."
        return {"success": True, "booked_count": num_booked, "message": msg, "session_ids": created_session_ids}
    else:
        log_error("task_manager", fn_name, f"Created GCal sessions for {task_id}, but failed DB update.", user_id=user_id)
        # Rollback GCal changes
        log_warning("task_manager", fn_name, f"Attempting GCal rollback for {len(created_session_ids)} sessions (Task ID: {task_id}).")
        if calendar_api is not None:
            for sid in created_session_ids:
                try: calendar_api.delete_event(sid)
                except Exception: log_error("task_manager", fn_name, f"GCal rollback delete failed for session {sid}", user_id=user_id)
        return {**default_fail_result, "message": "Scheduled sessions, but failed to link to task. Calendar changes rolled back."}

# Returns dict with 'success', 'cancelled_count', 'message'
def cancel_sessions(user_id: str, task_id: str, session_ids_to_cancel: List[str]) -> Dict:
    """Cancels specific GCal work sessions and updates task metadata in DB."""
    fn_name = "cancel_sessions"
    default_fail_result = {"success": False, "cancelled_count": 0, "message": "An unexpected error occurred."}
    if not DB_IMPORTED:
        return {**default_fail_result, "message": "Database module not available."}

    log_info("task_manager", fn_name, f"Cancelling {len(session_ids_to_cancel)} sessions for task {task_id}")

    calendar_api = _get_calendar_api(user_id)
    if calendar_api is None:
        return {**default_fail_result, "message": "Calendar is not connected or active."}

    # 1. Get Parent Task from DB
    task_metadata = activity_db.get_task(task_id)
    if task_metadata is None:
        log_error("task_manager", fn_name, f"Parent task {task_id} not found.", user_id=user_id)
        return {**default_fail_result, "message": "Original task details not found."}
    if task_metadata.get("user_id") != user_id: # Check ownership
         log_error("task_manager", fn_name, f"User mismatch for task {task_id}.", user_id=user_id)
         return {**default_fail_result, "message": "Task ownership mismatch."}
    if task_metadata.get("type") != "task":
         return {**default_fail_result, "message": "Can only cancel sessions for tasks."}

    # 2. Delete GCal Events
    cancelled_count = 0
    errors = []
    valid_gcal_ids_to_cancel = [sid for sid in session_ids_to_cancel if isinstance(sid, str) and not sid.startswith("local_")]

    for session_id in valid_gcal_ids_to_cancel:
        try:
            deleted = calendar_api.delete_event(session_id) # Returns bool
            if deleted: cancelled_count += 1
            # else: delete_event logs warning if not found/failed
        except Exception as e:
            log_error("task_manager", fn_name, f"Error deleting GCal session {session_id} for task {task_id}", e, user_id=user_id)
            errors.append(session_id)
    log_info("task_manager", fn_name, f"GCal delete attempts for task {task_id}: Success/Gone: {cancelled_count}, Errors: {len(errors)}")

    # 3. Update Parent Task in DB
    existing_session_ids = task_metadata.get("session_event_ids", []) # Already list from get_task
    if not isinstance(existing_session_ids, list): existing_session_ids = []

    cancelled_set = set(session_ids_to_cancel) # Use original list (might include local IDs intended for removal)
    remaining_ids = [sid for sid in existing_session_ids if sid not in cancelled_set]

    update_payload = task_metadata.copy()
    update_payload["sessions_planned"] = len(remaining_ids)
    update_payload["session_event_ids"] = remaining_ids # Store list for DB function
    # Only change status to pending if NO sessions remain, otherwise keep current status
    if not remaining_ids:
        update_payload["status"] = "pending"

    save_success = activity_db.add_or_update_task(update_payload)

    if save_success:
        log_info("task_manager", fn_name, f"Parent task {task_id} updated in DB after session cancellation.")
        updated_task_data = activity_db.get_task(task_id) # Get final state
        if updated_task_data and AGENT_STATE_MANAGER_IMPORTED:
            update_task_in_context(user_id, task_id, updated_task_data) # Update memory

        msg = f"Successfully cancelled {cancelled_count} session(s) from your calendar."
        if errors: msg += f" Encountered errors cancelling {len(errors)}."
        return {"success": True, "cancelled_count": cancelled_count, "message": msg}
    else:
        log_error("task_manager", fn_name, f"Deleted GCal sessions for {task_id}, but failed DB update.", user_id=user_id)
        # Inconsistent state: GCal events gone, DB still references them. Hard to roll back GCal deletes.
        return {**default_fail_result, "cancelled_count": cancelled_count, "message": "Cancelled sessions in calendar, but failed to update the task link."}

# --- END OF REFACTORED services/task_manager.py ---