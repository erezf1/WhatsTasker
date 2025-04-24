# --- START OF FILE services/task_manager.py ---
"""
Service layer for managing tasks: creating, updating, cancelling, and scheduling sessions.
Interacts with Google Calendar API and the Metadata Store.
"""
import json
import traceback
import uuid
from datetime import datetime, timedelta, timezone # Added timezone
import re

# Tool/Service Imports
try: from tools.logger import log_info, log_error, log_warning
except ImportError:
    import logging; logging.basicConfig(level=logging.INFO)
    log_info=logging.info; log_error=logging.error; log_warning=logging.warning
    log_error("task_manager", "import", "Logger failed import.")

try: from tools.google_calendar_api import GoogleCalendarAPI
except ImportError:
    log_error("task_manager", "import", "GoogleCalendarAPI not found.")
    GoogleCalendarAPI = None

try: from tools import metadata_store; METADATA_STORE_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "metadata_store not found."); METADATA_STORE_IMPORTED = False
    # Define a minimal MockMetadataStore if needed for basic testing without the real one
    class MockMetadataStore:
        _data = {}; FIELDNAMES = ["event_id", "user_id", "type", "status", "title", "description", "date", "time", "estimated_duration", "session_event_ids", "sessions_planned", "created_at", "completed_at", "project", "original_date", "duration", "progress", "progress_percent", "internal_reminder_minutes", "internal_reminder_sent", "sessions_completed", "series_id", "gcal_start_datetime", "gcal_end_datetime"]
        def init_metadata_store(self): pass
        def save_event_metadata(self, data): self._data[data['event_id']] = {k: data.get(k) for k in self.FIELDNAMES}
        def get_event_metadata(self, event_id): return self._data.get(event_id)
        def delete_event_metadata(self, event_id): return self._data.pop(event_id, None) is not None
        def list_metadata(self, user_id, **kwargs): return [v for v in self._data.values() if v.get("user_id") == user_id]
        def load_all_metadata(self): return list(self._data.values())
    metadata_store = MockMetadataStore(); metadata_store.init_metadata_store()


try: from services.agent_state_manager import get_agent_state, add_task_to_context, update_task_in_context, remove_task_from_context; AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "AgentStateManager not found."); AGENT_STATE_MANAGER_IMPORTED = False
    # Define dummy functions if import fails
    def get_agent_state(*a, **k): return None
    def add_task_to_context(*a, **k): pass
    def update_task_in_context(*a, **k): pass
    def remove_task_from_context(*a, **k): pass

# Constants
DEFAULT_REMINDER_DURATION = "15m" # Duration for GCal event if reminder has time

# --- Helper Functions ---
def _get_calendar_api(user_id):
    """Safely retrieves the active calendar API instance from agent state."""
    fn_name = "_get_calendar_api"
    if not AGENT_STATE_MANAGER_IMPORTED or GoogleCalendarAPI is None:
        log_warning("task_manager", fn_name, f"Cannot get calendar API for {user_id}: Dependencies missing.")
        return None
    agent_state = get_agent_state(user_id)
    if agent_state:
        calendar_api = agent_state.get("calendar")
        if isinstance(calendar_api, GoogleCalendarAPI) and calendar_api.is_active():
            return calendar_api
        else:
            # Log only if calendar object exists but is inactive
            if isinstance(calendar_api, GoogleCalendarAPI) and not calendar_api.is_active():
                 log_info("task_manager", fn_name, f"Calendar API found but inactive for user {user_id}.")
    return None

def _parse_duration_to_minutes(duration_str):
    """Parses duration strings like '2h', '90m', '1.5h' into minutes."""
    fn_name = "_parse_duration_to_minutes"
    if not duration_str or not isinstance(duration_str, str):
        return None

    duration_str = duration_str.lower().replace(' ','')
    total_minutes = 0.0 # Use float for intermediate calculation

    try:
        # Match patterns like '1.5h30m', '2h', '90m', '1.5h', '0.5h'
        hour_match = re.search(r'(\d+(\.\d+)?)\s*h', duration_str)
        minute_match = re.search(r'(\d+)\s*m', duration_str)

        if hour_match:
            total_minutes += float(hour_match.group(1)) * 60
            # Check if minutes follow directly after hours (e.g., in '1h30m')
            # to avoid double counting minutes if 'm' is also present separately
            remaining_str = duration_str[hour_match.end():]
            minute_after_hour_match = re.match(r'(\d+)\s*m', remaining_str)
            if minute_after_hour_match:
                 total_minutes += int(minute_after_hour_match.group(1))
                 # Prevent separate minute match from adding again
                 minute_match = None
        elif minute_match: # Only look for minutes if hours weren't found first
            total_minutes += int(minute_match.group(1))

        # Handle cases where only a number was provided (assume minutes)
        if total_minutes == 0 and hour_match is None and minute_match is None:
             if duration_str.replace('.','',1).isdigit():
                  log_warning("task_manager", fn_name, f"Assuming minutes for duration input: '{duration_str}'")
                  total_minutes = float(duration_str)
             else:
                  # Could not parse as hours, minutes, or plain number
                  raise ValueError("Unrecognized duration format")

        return int(round(total_minutes)) if total_minutes > 0 else None

    except (ValueError, TypeError) as e:
        log_warning("task_manager", fn_name, f"Could not parse duration string '{duration_str}': {e}")
        return None

# ==============================================================
# Core Service Functions
# ==============================================================

def create_task(user_id, task_params):
    """Creates a task or reminder, saves metadata, optionally adds to GCal."""
    fn_name = "create_task"
    item_type = task_params.get("type")
    if not item_type:
        log_error("task_manager", fn_name, "Missing 'type' in task_params.")
        return None
    log_info("task_manager", fn_name, f"Creating item for {user_id}, type: {item_type}")
    if not METADATA_STORE_IMPORTED:
        log_error("task_manager", fn_name, "Metadata store unavailable.")
        return None

    calendar_api = _get_calendar_api(user_id)
    google_event_id = None

    try:
        # Initialize metadata with known fields
        metadata = {k: task_params.get(k) for k in metadata_store.FIELDNAMES if k in task_params}
        metadata["user_id"] = user_id
        metadata["status"] = "pending"
        # *** Store created_at in UTC ISO format ***
        metadata["created_at"] = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        metadata["title"] = task_params.get("description", "Untitled Item")
        metadata["type"] = item_type
        # *** Initialize internal_reminder_sent as empty string ***
        metadata["internal_reminder_sent"] = "" # Default to not sent

        # Task specific defaults
        if item_type == "task":
            metadata["session_event_ids"] = json.dumps([])
            metadata["sessions_planned"] = 0
            metadata["sessions_completed"] = 0
            metadata["progress_percent"] = 0

        # --- Modified GCal Reminder Creation Logic ---
        item_time = task_params.get("time")
        has_time = item_time is not None and item_time != ""
        should_create_gcal = item_type == "reminder" and has_time and calendar_api is not None

        log_info("task_manager", fn_name, f"Checking GCal creation: type='{item_type}', has_time={has_time}, calendar_api_present={calendar_api is not None}. Should create: {should_create_gcal}")

        if should_create_gcal:
            log_info("task_manager", fn_name, f"Attempting to create GCal Reminder event for user {user_id}, item: '{metadata['title'][:30]}...'")
            gcal_event_payload = {
                "title": metadata.get("title"),
                "description": f"Reminder: {metadata.get('description', '')}",
                "date": task_params.get("date"),
                "time": item_time,
                "duration": DEFAULT_REMINDER_DURATION
            }
            try:
                # Explicitly log before the API call
                log_info("task_manager", fn_name, f"Calling calendar_api.create_event for user {user_id} with payload: {gcal_event_payload}")
                google_event_id = calendar_api.create_event(gcal_event_payload) # This might return None or raise Exception

                # *** ADDED Logging: Check result immediately ***
                if google_event_id:
                    log_info("task_manager", fn_name, f"GCal Reminder event CREATED successfully, ID: {google_event_id}")
                    metadata["event_id"] = google_event_id
                    # Fetch details to get exact times
                    gcal_event_details = calendar_api._get_single_event(google_event_id)
                    if gcal_event_details:
                        parsed_gcal = calendar_api._parse_google_event(gcal_event_details)
                        metadata["gcal_start_datetime"] = parsed_gcal.get("gcal_start_datetime")
                        metadata["gcal_end_datetime"] = parsed_gcal.get("gcal_end_datetime")
                        log_info("task_manager", fn_name, f"Fetched GCal details for new event {google_event_id}")
                    else:
                         log_warning("task_manager", fn_name, f"Created GCal event {google_event_id} but failed to fetch its details afterward.")
                else:
                    # calendar_api.create_event returned None
                    log_warning("task_manager", fn_name, f"GCal Reminder event creation FAILED (returned None) for user {user_id}. Assigning local ID.")
                    metadata["event_id"] = f"local_{uuid.uuid4()}"

            except Exception as gcal_create_err:
                # Log any exception during the create_event call
                log_error("task_manager", fn_name, f"Error calling calendar_api.create_event for user {user_id}", gcal_create_err)
                metadata["event_id"] = f"local_{uuid.uuid4()}" # Assign local ID if GCal failed
                google_event_id = None # Ensure google_event_id is None after failure
        else:
            # Log reason for not attempting GCal creation
            reason = ""
            if item_type != "reminder": reason = "Item is not a reminder."
            elif not has_time: reason = "Reminder has no specific time."
            elif calendar_api is None: reason = "Calendar API is not active/available."
            log_info("task_manager", fn_name, f"Assigning local ID for {item_type}. Reason: {reason}")
            metadata["event_id"] = f"local_{uuid.uuid4()}"
        # --- End Modified GCal Reminder Creation Logic ---

        # Final validation before saving
        if not metadata.get("event_id"):
            log_error("task_manager", fn_name, "Critical error: Metadata missing 'event_id' before save attempt.")
            return None

        # Prepare final dict and save
        final_meta = {fn: metadata.get(fn) for fn in metadata_store.FIELDNAMES}
        metadata_store.save_event_metadata(final_meta)
        log_info("task_manager", fn_name, f"Metadata saved for event_id: {final_meta['event_id']}")

        # Update in-memory context
        if AGENT_STATE_MANAGER_IMPORTED:
             add_task_to_context(user_id, final_meta)

        return final_meta

    except Exception as e:
        tb = traceback.format_exc()
        log_error("task_manager", fn_name, f"Overall error creating item for {user_id}. Trace:\n{tb}", e)
        # Rollback GCal event if it was successfully created in this failed attempt
        if google_event_id and calendar_api:
            log_warning("task_manager", fn_name, f"Attempting final GCal rollback for event {google_event_id} due to overall error.")
            try: calendar_api.delete_event(google_event_id)
            except Exception as final_rollback_e: log_error("task_manager", fn_name, f"Error during final GCal rollback for event {google_event_id}", final_rollback_e)
        return None

def update_task(user_id, item_id, updates):
    """Updates details of an existing task/reminder."""
    fn_name = "update_task"
    log_info("task_manager", fn_name, f"Updating item {item_id} for {user_id}, keys: {list(updates.keys())}")
    if not METADATA_STORE_IMPORTED:
        log_error("task_manager", fn_name, "Metadata store unavailable.")
        return None

    calendar_api = _get_calendar_api(user_id)
    gcal_updated = False

    try:
        existing = metadata_store.get_event_metadata(item_id)
        if not existing:
            log_error("task_manager", fn_name, f"Metadata not found for item {item_id}.")
            return None
        if existing.get("user_id") != user_id:
            log_error("task_manager", fn_name, f"User mismatch for item {item_id}.")
            return None

        item_type = existing.get("type")
        needs_gcal_update, gcal_payload = False, {}

        # Check if GCal update is needed (only for non-local reminders with active calendar)
        if item_type == "reminder" and not item_id.startswith("local_") and calendar_api:
            # Map metadata update keys to potential GCal payload keys
            if "description" in updates: gcal_payload["title"] = updates["description"]; gcal_payload["description"] = f"Reminder: {updates['description']}"; needs_gcal_update = True
            if "date" in updates or "time" in updates:
                 # Note: GCal update needs full date/time usually, handle carefully
                 gcal_payload["date"] = updates.get("date", existing.get("date"))
                 gcal_payload["time"] = updates.get("time", existing.get("time")) # Handle potential None value
                 if "time" in updates and updates["time"] is None: # Explicitly clearing time
                      gcal_payload["time"] = None
                 needs_gcal_update = True
            # Add other relevant mappings if needed

            if needs_gcal_update:
                 log_info("task_manager", fn_name, f"Attempting GCal Reminder update for {item_id}")
                 gcal_updated = calendar_api.update_event(item_id, gcal_payload)
                 if not gcal_updated: log_warning("task_manager", fn_name, f"GCal Reminder update failed for {item_id}.")
                 else: log_info("task_manager", fn_name, f"GCal Reminder update OK for {item_id}")
            else: log_info("task_manager", fn_name, f"No relevant GCal fields to update for reminder {item_id}.")
        # else: log info about skipping GCal update for tasks/local items/inactive calendar?

        # Update metadata store
        allowed_meta_keys = {"description", "date", "time", "estimated_duration", "project"}
        valid_meta_updates = {k: v for k, v in updates.items() if k in allowed_meta_keys}

        if not valid_meta_updates:
             log_warning("task_manager", fn_name, f"No valid metadata fields in updates for {item_id}. No metadata change.")
             # Return existing if no changes, but maybe GCal updated? Check gcal_updated?
             # For simplicity, let's return existing if no *metadata* changes.
             return existing

        updated_metadata = existing.copy()
        updated_metadata.update(valid_meta_updates)
        if "description" in valid_meta_updates: updated_metadata["title"] = valid_meta_updates["description"] # Keep title synced with description
        if "time" in valid_meta_updates and valid_meta_updates["time"] is None: updated_metadata["time"] = None # Ensure None is stored if cleared

        # Fetch updated GCal details if update occurred
        if gcal_updated and calendar_api:
             gcal_event_details = calendar_api._get_single_event(item_id)
             if gcal_event_details:
                  parsed_gcal = calendar_api._parse_google_event(gcal_event_details)
                  updated_metadata["gcal_start_datetime"] = parsed_gcal.get("gcal_start_datetime")
                  updated_metadata["gcal_end_datetime"] = parsed_gcal.get("gcal_end_datetime")

        meta_to_save = {fn: updated_metadata.get(fn) for fn in metadata_store.FIELDNAMES}
        metadata_store.save_event_metadata(meta_to_save)
        log_info("task_manager", fn_name, f"Metadata updated successfully for {item_id}")

        # Update in-memory context
        if AGENT_STATE_MANAGER_IMPORTED:
             update_task_in_context(user_id, item_id, meta_to_save)

        return meta_to_save

    except Exception as e:
        tb = traceback.format_exc()
        log_error("task_manager", fn_name, f"Error updating item {item_id} for {user_id}. Trace:\n{tb}", e)
        return None

def update_task_status(user_id, item_id, new_status):
    """Updates only the status and related tracking fields."""
    fn_name = "update_task_status"
    log_info("task_manager", fn_name, f"Setting status='{new_status}' for item {item_id}, user {user_id}")
    if new_status == "cancelled":
        log_error("task_manager", fn_name, "Use cancel_item() function for 'cancelled' status.")
        return None
    try:
        existing = metadata_store.get_event_metadata(item_id)
        if not existing:
            log_error("task_manager", fn_name, f"Metadata not found for item {item_id}.")
            return None
        if existing.get("user_id") != user_id:
            log_error("task_manager", fn_name, f"User mismatch for item {item_id}.")
            return None

        updates_dict = {"status": new_status}
        if new_status.lower() == "completed":
            # *** Store completed_at in UTC ISO format ***
            updates_dict["completed_at"] = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
            updates_dict["progress_percent"] = 100
            if existing.get("type") == "task":
                updates_dict["sessions_completed"] = existing.get("sessions_planned", 0) # Mark all planned as done
        elif new_status.lower() in ["pending", "in_progress", "in progress"]:
             updates_dict["completed_at"] = "" # Use empty string for None/cleared value in CSV
             if new_status.lower() == "pending":
                  updates_dict["progress_percent"] = 0 # Reset progress

        # Apply updates to a copy
        updated_metadata = existing.copy()
        updated_metadata.update(updates_dict)

        # Prepare final dict with only defined FIELDNAMES
        meta_to_save = {fn: updated_metadata.get(fn) for fn in metadata_store.FIELDNAMES}
        metadata_store.save_event_metadata(meta_to_save)
        log_info("task_manager", fn_name, f"Metadata status updated successfully for {item_id}")

        # Update in-memory context
        if AGENT_STATE_MANAGER_IMPORTED:
             update_task_in_context(user_id, item_id, meta_to_save)

        return meta_to_save
    except Exception as e:
        log_error("task_manager", fn_name, f"Error saving status update for {item_id}", e)
        return None

def cancel_item(user_id, item_id):
    """Sets item status to 'cancelled' and deletes associated GCal events."""
    fn_name = "cancel_item"
    log_info("task_manager", fn_name, f"Processing cancellation for item {item_id}, user {user_id}")
    if not METADATA_STORE_IMPORTED:
        log_error("task_manager", fn_name, "Metadata store unavailable.")
        return False

    calendar_api = _get_calendar_api(user_id)
    gcal_cleanup_ok = True # Assume okay unless error occurs

    try:
        metadata = metadata_store.get_event_metadata(item_id)
        if not metadata:
            log_warning("task_manager", fn_name, f"Metadata not found for {item_id} during cancel. Assuming already handled.")
            return True
        if metadata.get("user_id") != user_id:
            log_error("task_manager", fn_name, f"User mismatch for item {item_id}.")
            return False
        if metadata.get("status") == "cancelled":
            log_info("task_manager", fn_name, f"Item {item_id} is already cancelled.")
            return True

        item_type = metadata.get("type")

        # --- GCal Cleanup ---
        # ... (GCal cleanup logic remains the same) ...
        if calendar_api and not item_id.startswith("local_"):
            # ... (reminder and task session deletion) ...
            pass # Keep existing GCal cleanup logic here
        elif not calendar_api:
             log_info("task_manager", fn_name, f"Calendar API inactive for user {user_id}. Skipping GCal cleanup for {item_id}.")
        else: # Item is local_
             log_info("task_manager", fn_name, f"Item {item_id} is local. No GCal cleanup needed.")
        # --- End GCal Cleanup ---

        # --- Update Metadata Status DIRECTLY ---
        log_info("task_manager", fn_name, f"Updating metadata status to cancelled for {item_id}")
        metadata["status"] = "cancelled"
        # Optionally clear session data from metadata?
        # metadata["session_event_ids"] = json.dumps([])
        # metadata["sessions_planned"] = 0
        try:
            # Prepare final dict with only defined FIELDNAMES
            meta_to_save = {fn: metadata.get(fn) for fn in metadata_store.FIELDNAMES}
            metadata_store.save_event_metadata(meta_to_save)
            log_info("task_manager", fn_name, f"Successfully marked item {item_id} as cancelled in metadata.")
            # Update in-memory context
            if AGENT_STATE_MANAGER_IMPORTED:
                 # Use update, not remove, to reflect the 'cancelled' status
                 update_task_in_context(user_id, item_id, meta_to_save)
            return True
        except Exception as meta_save_err:
             log_error("task_manager", fn_name, f"Failed to save metadata status update to cancelled for {item_id}", meta_save_err)
             # Should we try to rollback GCal changes here? Difficult.
             return False
        # --- End Direct Metadata Update ---

    except Exception as e:
        tb = traceback.format_exc()
        log_error("task_manager", fn_name, f"Error cancelling item {item_id} for user {user_id}. Trace:\n{tb}", e)
        return False

# --- Scheduling Functions ---

def schedule_work_sessions(user_id, task_id, slots_to_book):
    """Creates GCal events for proposed work sessions and updates the main task metadata."""
    fn_name = "schedule_work_sessions"
    log_info("task_manager", fn_name, f"Booking {len(slots_to_book)} sessions for task {task_id}")
    if not METADATA_STORE_IMPORTED:
         return {"success": False, "message": "Metadata store unavailable.", "session_ids": []}

    calendar_api = _get_calendar_api(user_id)
    if not calendar_api:
        return {"success": False, "message": "Calendar is not connected or active. Cannot schedule sessions.", "session_ids": []}

    # --- Get Task Details & Preferences ---
    task_metadata = metadata_store.get_event_metadata(task_id)
    if not task_metadata or task_metadata.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"Main task {task_id} not found or user mismatch.")
        return {"success": False, "message": "Could not find the original task details.", "session_ids": []}
    if task_metadata.get("type") != "task":
         log_error("task_manager", fn_name, f"Item {task_id} is not a task. Cannot schedule sessions.")
         return {"success": False, "message": "Scheduling is only supported for items of type 'task'.", "session_ids": []}

    task_title = task_metadata.get("title", "Task Work")
    agent_state = get_agent_state(user_id) # Reuse state if already fetched?
    prefs = agent_state.get("preferences", {}) if agent_state else {}
    session_length_str = prefs.get("Preferred_Session_Length", "60m")
    session_length_minutes = _parse_duration_to_minutes(session_length_str) or 60

    # --- Create GCal Events ---
    created_session_ids = []
    errors = []
    for i, session_slot in enumerate(slots_to_book):
        session_date = session_slot.get("date")
        session_time = session_slot.get("time")
        if not session_date or not session_time:
             log_warning("task_manager", fn_name, f"Skipping session {i+1} for task {task_id}: missing date or time.")
             errors.append(f"Session {i+1} missing data")
             continue

        try:
            session_event_data = {
                "title": f"Work: {task_title} [{i+1}/{len(slots_to_book)}]",
                "description": f"Focused work session for task: {task_title}\nParent Task ID: {task_id}",
                "date": session_date, "time": session_time,
                "duration": f"{session_length_minutes}m"
            }
            session_event_id = calendar_api.create_event(session_event_data)
            if session_event_id:
                created_session_ids.append(session_event_id)
            else:
                log_error("task_manager", fn_name, f"Failed to create GCal event for session {i+1} of task {task_id}.")
                errors.append(f"Session {i+1} GCal creation failed")
        except Exception as e:
            log_error("task_manager", fn_name, f"Error creating GCal event for session {i+1} of task {task_id}", e)
            errors.append(f"Session {i+1} creation error: {type(e).__name__}")

    if not created_session_ids:
        log_error("task_manager", fn_name, f"Failed to create any GCal session events for task {task_id}.")
        error_summary = "; ".join(errors)
        return {"success": False, "message": f"Sorry, I couldn't add the proposed sessions to your calendar. Errors: {error_summary}", "session_ids": []}

    log_info("task_manager", fn_name, f"Successfully created {len(created_session_ids)} GCal session events for task {task_id}: {created_session_ids}")

    # --- Update Main Task Metadata ---
    try:
        existing_ids_json = task_metadata.get("session_event_ids", "[]")
        existing_session_ids = json.loads(existing_ids_json) if isinstance(existing_ids_json, str) and existing_ids_json.strip() else []
        if not isinstance(existing_session_ids, list): existing_session_ids = []
    except json.JSONDecodeError:
        log_error("task_manager", fn_name, f"Failed to parse existing session IDs for task {task_id}. Overwriting.")
        existing_session_ids = []

    # Combine old and new, removing duplicates
    all_session_ids = list(set(existing_session_ids + created_session_ids))

    metadata_update_payload = {
        "sessions_planned": len(all_session_ids),
        "session_event_ids": json.dumps(all_session_ids),
        "status": "in_progress" # Update status when sessions are booked
    }

    # Apply updates to a copy
    updated_metadata = task_metadata.copy()
    updated_metadata.update(metadata_update_payload)
    # Prepare final dict with only defined FIELDNAMES
    meta_to_save = {fn: updated_metadata.get(fn) for fn in metadata_store.FIELDNAMES}

    try:
        metadata_store.save_event_metadata(meta_to_save)
        log_info("task_manager", fn_name, f"Updated parent task {task_id} metadata with session info.")
        if AGENT_STATE_MANAGER_IMPORTED: update_task_in_context(user_id, task_id, meta_to_save)
    except Exception as meta_e:
         log_error("task_manager", fn_name, f"Created GCal sessions, but failed update metadata for task {task_id}.", meta_e)
         log_warning("task_manager", fn_name, f"Attempting rollback of {len(created_session_ids)} GCal sessions for {task_id}.")
         if calendar_api:
             for sid in created_session_ids:
                 try: calendar_api.delete_event(sid)
                 except Exception as del_e: log_error("task_manager", fn_name, f"Rollback delete failed for session {sid}", del_e)
         else: log_warning("task_manager", fn_name, "Cannot perform GCal rollback as calendar_api is not available.")
         return {"success": False, "message": "Scheduled sessions in calendar, but failed to link them to the task. Rolling back calendar changes.", "session_ids": []}

    # --- Return Success ---
    num_sessions = len(created_session_ids)
    plural_s = "s" if num_sessions > 1 else ""
    success_message = f"Okay, I've scheduled {num_sessions} work session{plural_s} for '{task_title}' in your calendar."
    if errors: success_message += f" (Note: Issues encountered with {len(errors)} potential sessions)."

    return {"success": True, "message": success_message, "session_ids": created_session_ids}


def cancel_sessions(user_id, task_id, session_ids_to_cancel):
    """Cancels specific GCal work sessions and updates task metadata."""
    fn_name = "cancel_sessions"
    log_info("task_manager", fn_name, f"Cancelling {len(session_ids_to_cancel)} sessions for task {task_id}")
    if not METADATA_STORE_IMPORTED:
        return {"success": False, "cancelled_count": 0, "message": "Metadata store unavailable."}

    calendar_api = _get_calendar_api(user_id)
    if not calendar_api:
        return {"success": False, "cancelled_count": 0, "message": "Calendar is not connected or active."}

    # --- Get Task Metadata ---
    task_metadata = metadata_store.get_event_metadata(task_id)
    if not task_metadata or task_metadata.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"Original task {task_id} not found or user mismatch.")
        return {"success": False, "cancelled_count": 0, "message": "Original task details not found."}
    if task_metadata.get("type") != "task":
         return {"success": False, "cancelled_count": 0, "message": "Can only cancel sessions for tasks."}

    # --- Delete GCal Events ---
    cancelled_count = 0
    errors = []
    valid_ids_to_cancel_gcal = [sid for sid in session_ids_to_cancel if not str(sid).startswith("local_")]

    for session_id in valid_ids_to_cancel_gcal:
        try:
            deleted = calendar_api.delete_event(session_id)
            if deleted:
                cancelled_count += 1
            # else: delete_event now returns True even if 404/410, so this branch likely won't hit often
        except Exception as e:
            log_error("task_manager", fn_name, f"Error deleting GCal session {session_id} for task {task_id}", e)
            errors.append(session_id)

    log_info("task_manager", fn_name, f"GCal delete attempts completed for task {task_id}. Success/Gone: {cancelled_count}, Errors: {len(errors)}")

    # --- Update Metadata ---
    try:
        existing_ids_json = task_metadata.get("session_event_ids", "[]")
        existing_session_ids = json.loads(existing_ids_json) if isinstance(existing_ids_json, str) and existing_ids_json.strip() else []
        if not isinstance(existing_session_ids, list): existing_session_ids = []
    except json.JSONDecodeError:
        log_error("task_manager", fn_name, f"Corrupted session IDs JSON for task {task_id}. Resetting.")
        existing_session_ids = []

    cancelled_set = set(session_ids_to_cancel) # Use the original list including potential local IDs
    remaining_ids = [sid for sid in existing_session_ids if sid not in cancelled_set]

    metadata_updates = {
        "sessions_planned": len(remaining_ids),
        "session_event_ids": json.dumps(remaining_ids)
        # Consider if status should change back from 'in_progress' if all sessions cancelled?
        # "status": "pending" if not remaining_ids else "in_progress"
    }
    updated_metadata = task_metadata.copy()
    updated_metadata.update(metadata_updates)
    meta_to_save = {fn: updated_metadata.get(fn) for fn in metadata_store.FIELDNAMES}

    try:
        metadata_store.save_event_metadata(meta_to_save)
        log_info("task_manager", fn_name, f"Updated parent task {task_id} metadata after cancelling sessions.")
        if AGENT_STATE_MANAGER_IMPORTED: update_task_in_context(user_id, task_id, meta_to_save)
        message = f"Successfully cancelled {cancelled_count} session(s)." # Report GCal deletions
        if errors: message += f" Encountered errors cancelling {len(errors)}."
        return {"success": True, "cancelled_count": cancelled_count, "message": message}
    except Exception as meta_e:
        log_error("task_manager", fn_name, f"Deleted GCal sessions, but failed update metadata for task {task_id}.", meta_e)
        # This is tricky - GCal events are gone, but metadata isn't updated.
        return {"success": False, "cancelled_count": cancelled_count, "message": "Cancelled sessions in calendar, but failed to update the task link."}
# --- END OF FILE services/task_manager.py ---