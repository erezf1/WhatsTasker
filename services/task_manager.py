# --- START OF FULL services/task_manager.py ---
import json
import traceback
import uuid
from datetime import datetime, timedelta, timezone
import re
from typing import Dict, List, Any

try:
    from tools.logger import log_info, log_error, log_warning
except ImportError:
    import logging; logging.basicConfig(level=logging.INFO)
    log_info=logging.info; log_error=logging.error; log_warning=logging.warning
    log_error("task_manager", "import", "Logger failed import.")

# --- ActivityDB Import Handling ---
DB_IMPORTED = False
activity_db_module_ref = None
try:
    import tools.activity_db as activity_db
    activity_db_module_ref = activity_db # Store the module itself
    DB_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "activity_db not found. Item management disabled.", None)
    class activity_db_dummy: # Dummy class for type hinting and preventing crashes
        @staticmethod
        def add_or_update_task(*args, **kwargs): return False
        @staticmethod
        def get_task(*args, **kwargs): return None
        @staticmethod
        def delete_task(*args, **kwargs): return False
        @staticmethod
        def list_tasks_for_user(*args, **kwargs): return []
    activity_db_module_ref = activity_db_dummy() # Assign dummy instance
# --- End ActivityDB Import ---

# --- GoogleCalendarAPI Import Handling ---
GCAL_API_IMPORTED = False
GoogleCalendarAPI_class_ref_tm = None # Placeholder for the class
try:
    from tools.google_calendar_api import GoogleCalendarAPI
    if GoogleCalendarAPI: # Check if not None
        GoogleCalendarAPI_class_ref_tm = GoogleCalendarAPI
        GCAL_API_IMPORTED = True
except ImportError:
    log_warning("task_manager", "import", "GoogleCalendarAPI not found, GCal features disabled.")
# --- End GoogleCalendarAPI Import ---

# --- AgentStateManager Import Handling ---
AGENT_STATE_MANAGER_IMPORTED = False
_get_agent_state_tm = None
_add_task_to_context_tm = None
_update_task_in_context_tm = None
_remove_task_from_context_tm = None
try:
    from services.agent_state_manager import get_agent_state, add_task_to_context, update_task_in_context, remove_task_from_context
    _get_agent_state_tm = get_agent_state
    _add_task_to_context_tm = add_task_to_context
    _update_task_in_context_tm = update_task_in_context
    _remove_task_from_context_tm = remove_task_from_context
    AGENT_STATE_MANAGER_IMPORTED = True
except ImportError:
    log_error("task_manager", "import", "AgentStateManager not found. In-memory context updates skipped.")
# --- End AgentStateManager Import ---

DEFAULT_REMINDER_DURATION_MINUTES = 15

def _get_calendar_api(user_id: str) -> Any: # Returns GoogleCalendarAPI instance or None
    fn_name = "_get_calendar_api_tm"
    if not AGENT_STATE_MANAGER_IMPORTED or not GCAL_API_IMPORTED or GoogleCalendarAPI_class_ref_tm is None or _get_agent_state_tm is None:
        return None
    agent_state = _get_agent_state_tm(user_id)
    if agent_state is not None:
        calendar_api = agent_state.get("calendar")
        if isinstance(calendar_api, GoogleCalendarAPI_class_ref_tm) and calendar_api.is_active():
            return calendar_api
    return None

def _parse_duration_to_minutes(duration_str: str | None) -> int | None:
    fn_name = "_parse_duration_to_minutes_tm"
    if not duration_str or not isinstance(duration_str, str): return None
    duration_str = duration_str.lower().replace(' ',''); total_minutes = 0.0
    try:
        hour_match = re.search(r'(\d+(\.\d+)?)\s*h', duration_str)
        minute_match = re.search(r'(\d+)\s*m', duration_str)
        if hour_match: total_minutes += float(hour_match.group(1)) * 60
        if minute_match: total_minutes += int(minute_match.group(1))
        if total_minutes == 0 and hour_match is None and minute_match is None:
             if duration_str.replace('.','',1).isdigit(): total_minutes = float(duration_str) # Assume minutes if just a number
             else: raise ValueError("Unrecognized duration format")
        return int(round(total_minutes)) if total_minutes > 0 else None
    except (ValueError, TypeError, AttributeError) as e:
        log_warning("task_manager", fn_name, f"Could not parse duration string '{duration_str}': {e}")
        return None

# ==============================================================
# Core Service Functions for Items (Tasks, ToDos, Reminders)
# ==============================================================

def create_item(user_id: str, item_params: Dict[str, Any]) -> Dict | None:
    fn_name = "create_item"
    if not DB_IMPORTED or activity_db_module_ref is None:
        log_error("task_manager", fn_name, "Database module not available. Cannot create item.")
        return None

    item_type = item_params.get("type")
    if not item_type or item_type not in ["task", "todo", "reminder"]:
        log_error("task_manager", fn_name, f"Invalid type '{item_type}' in item_params for user {user_id}.", user_id=user_id)
        return None
    # log_info("task_manager", fn_name, f"Creating item for {user_id}, type: {item_type}, desc: '{item_params.get('description', '')[:30]}...'") # Verbose

    calendar_api = _get_calendar_api(user_id)
    gcal_event_id_for_reminder: str | None = None

    try:
        item_data_to_save = {
            "user_id": user_id, "type": item_type, "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z',
            "title": item_params.get("description", f"Untitled {item_type.capitalize()}"), # Use description as title
            "description": item_params.get("description", ""),
            "project": item_params.get("project"), # None if not provided
            "date": item_params.get("date"),       # None if not provided
            "estimated_duration": None, "session_event_ids": [], "sessions_planned": 0,
            "sessions_completed": 0, "progress_percent": 0,
        }

        if item_type == "reminder":
            item_data_to_save["time"] = item_params.get("time") # Specific to reminder
            item_time = item_data_to_save.get("time")
            has_time = item_time is not None and item_time != ""
            
            if has_time and item_data_to_save.get("date") and calendar_api is not None and calendar_api.is_active():
                # log_info("task_manager", fn_name, f"Attempting GCal creation for reminder {user_id}...") # Verbose
                gcal_event_payload = {
                    "title": item_data_to_save.get("title"),
                    "description": f"Reminder: {item_data_to_save.get('description', '')}",
                    "date": item_data_to_save["date"], "time": item_time,
                    "duration": f"{DEFAULT_REMINDER_DURATION_MINUTES}m"
                }
                try:
                    created_gcal_event_id_from_api = calendar_api.create_event(gcal_event_payload)
                    if created_gcal_event_id_from_api:
                        item_data_to_save["event_id"] = created_gcal_event_id_from_api
                        gcal_event_id_for_reminder = created_gcal_event_id_from_api
                        gcal_details = calendar_api._get_single_event(created_gcal_event_id_from_api)
                        if gcal_details:
                            parsed_gcal = calendar_api._parse_google_event(gcal_details)
                            item_data_to_save["gcal_start_datetime"] = parsed_gcal.get("gcal_start_datetime")
                            item_data_to_save["gcal_end_datetime"] = parsed_gcal.get("gcal_end_datetime")
                    else: # GCal creation failed at API level
                        log_warning("task_manager", fn_name, f"GCal event creation failed (API returned None) for reminder for {user_id}. Using local ID.", user_id=user_id)
                        item_data_to_save["event_id"] = f"local_reminder_{uuid.uuid4()}"
                except Exception as gcal_err:
                    log_error("task_manager", fn_name, f"Error creating GCal event for reminder for {user_id}", gcal_err, user_id=user_id)
                    item_data_to_save["event_id"] = f"local_reminder_{uuid.uuid4()}"
            else: # No time, or no date, or no GCal API
                item_data_to_save["event_id"] = f"local_reminder_{uuid.uuid4()}"
                if has_time and not item_data_to_save.get("date"):
                    log_info("task_manager", fn_name, f"Reminder for {user_id} has time but no date. Not syncing to GCal.", user_id=user_id)
                elif has_time and (calendar_api is None or not calendar_api.is_active()):
                    log_info("task_manager", fn_name, f"Timed reminder for {user_id} will not be synced to GCal (API inactive/unavailable).", user_id=user_id)
        
        elif item_type == "task":
            item_data_to_save["estimated_duration"] = item_params.get("estimated_duration")
            item_data_to_save["event_id"] = f"local_task_{uuid.uuid4()}"
        
        elif item_type == "todo":
            item_data_to_save["estimated_duration"] = item_params.get("estimated_duration") # For user reference
            item_data_to_save["event_id"] = f"local_todo_{uuid.uuid4()}"

        if not item_data_to_save.get("event_id"): # Should be set by now
             log_error("task_manager", fn_name, "Critical: event_id not set before DB save.", user_id=user_id)
             return None

        save_success = activity_db_module_ref.add_or_update_task(item_data_to_save)
        if save_success:
            # log_info("task_manager", fn_name, f"Item {item_data_to_save['event_id']} ({item_type}) saved to DB for {user_id}") # Verbose
            saved_data_from_db = activity_db_module_ref.get_task(item_data_to_save['event_id'])
            if saved_data_from_db and AGENT_STATE_MANAGER_IMPORTED and _add_task_to_context_tm:
                _add_task_to_context_tm(user_id, saved_data_from_db)
            return saved_data_from_db if saved_data_from_db else item_data_to_save # Return what was saved/retrieved
        else: # DB save failed
            log_error("task_manager", fn_name, f"Failed to save item {item_data_to_save.get('event_id')} to DB for {user_id}.", user_id=user_id)
            if gcal_event_id_for_reminder and calendar_api: # Rollback GCal for reminder if DB save failed
                log_warning("task_manager", fn_name, f"DB save failed. Rolling back GCal event {gcal_event_id_for_reminder} for reminder.")
                try: calendar_api.delete_event(gcal_event_id_for_reminder)
                except Exception as rb_err: log_error("task_manager", fn_name, f"GCal reminder rollback failed for {gcal_event_id_for_reminder}", rb_err, user_id=user_id)
            return None
    except Exception as e:
        log_error("task_manager", fn_name, f"Unexpected error during item creation for {user_id}: {item_params}", e, user_id=user_id)
        if gcal_event_id_for_reminder and calendar_api: # Rollback on any other exception too
             try: calendar_api.delete_event(gcal_event_id_for_reminder)
             except Exception as rb_err_gen: log_error("task_manager", fn_name, f"GCal reminder rollback on general error failed for {gcal_event_id_for_reminder}", rb_err_gen, user_id=user_id)
        return None

def update_item_details(user_id: str, item_id: str, updates: Dict[str, Any]) -> Dict | None:
    fn_name = "update_item_details"
    if not DB_IMPORTED or activity_db_module_ref is None : return None
    # log_info("task_manager", fn_name, f"Updating item {item_id} for {user_id}, keys: {list(updates.keys())}") # Verbose

    existing_item = activity_db_module_ref.get_task(item_id)
    if existing_item is None:
        log_error("task_manager", fn_name, f"Item {item_id} not found for update. User: {user_id}.", user_id=user_id)
        return None
    if existing_item.get("user_id") != user_id:
        log_error("task_manager", fn_name, f"User mismatch for item {item_id}. Expected {user_id}, got {existing_item.get('user_id')}.", user_id=user_id)
        return None

    calendar_api = _get_calendar_api(user_id)
    item_type = existing_item.get("type")
    gcal_event_changed_for_reminder = False # Flag if GCal event for a reminder was modified/deleted

    # --- GCal Update for Timed Reminders ---
    if item_type == "reminder" and not item_id.startswith("local_") and calendar_api and calendar_api.is_active():
        gcal_update_payload = {}
        needs_gcal_api_call = False
        action_on_gcal = "update" # "update" or "delete"

        # Check for title/description change
        if "description" in updates and updates["description"] != existing_item.get("title"):
            gcal_update_payload["title"] = updates["description"]
            gcal_update_payload["description"] = f"Reminder: {updates['description']}" # Keep GCal desc prefixed
            needs_gcal_api_call = True
        
        # Determine new date/time, considering if keys are present in 'updates'
        new_date = updates["date"] if "date" in updates else existing_item.get("date")
        new_time = updates["time"] if "time" in updates else existing_item.get("time")

        # If date or time fields are explicitly part of the update dict
        if "date" in updates or "time" in updates:
            if new_date and new_time: # Becomes a timed event (or updates time/date of existing timed)
                if new_date != existing_item.get("date") or new_time != existing_item.get("time"):
                    gcal_update_payload["date"] = new_date
                    gcal_update_payload["time"] = new_time
                    # Duration usually fixed for reminders, GCal API handles if not provided
                    needs_gcal_api_call = True
            elif new_date and not new_time: # Becomes an all-day event on new_date
                if new_date != existing_item.get("date") or existing_item.get("time") is not None: # If date changed or it previously had a time
                    gcal_update_payload["date"] = new_date
                    gcal_update_payload["time"] = None # Signal all-day to GCal API method
                    needs_gcal_api_call = True
            elif not new_date: # Date is cleared (becomes dateless, remove from GCal)
                action_on_gcal = "delete"
                needs_gcal_api_call = True # Need to call API to delete it
        
        if needs_gcal_api_call:
            try:
                if action_on_gcal == "delete":
                    # log_info("task_manager", fn_name, f"Date cleared for GCal reminder {item_id}. Deleting from GCal.") # Verbose
                    if calendar_api.delete_event(item_id): gcal_event_changed_for_reminder = True
                elif gcal_update_payload: # Only update if there's something to update
                    # log_info("task_manager", fn_name, f"Attempting GCal update for reminder {item_id} with payload: {gcal_update_payload}") # Verbose
                    if calendar_api.update_event(item_id, gcal_update_payload): gcal_event_changed_for_reminder = True
            except Exception as gcal_err:
                 log_error("task_manager", fn_name, f"Error performing GCal {action_on_gcal} for reminder {item_id}", gcal_err, user_id=user_id)

    # --- DB Update ---
    db_update_data = existing_item.copy()
    applied_db_updates = False
    # Define valid updatable fields (excluding status, which has its own function)
    valid_detail_keys = {"description", "project", "date", "time", "estimated_duration"}

    for key, value in updates.items():
        if key in valid_detail_keys:
            # Ensure empty strings from Pydantic become None for DB consistency if field can be NULL
            db_value = None if isinstance(value, str) and value == "" and key in ["date", "time", "project", "estimated_duration"] else value
            if db_update_data.get(key) != db_value:
                db_update_data[key] = db_value
                if key == 'description': db_update_data['title'] = db_value # Keep title synced with description
                applied_db_updates = True
    
    if gcal_event_changed_for_reminder and calendar_api and not item_id.startswith("local_"):
        # Re-fetch GCal start/end datetimes as they might have changed or event deleted
        gcal_details = calendar_api._get_single_event(item_id) # Will be None if deleted
        new_gcal_start = None; new_gcal_end = None
        if gcal_details:
            parsed_gcal = calendar_api._parse_google_event(gcal_details)
            new_gcal_start = parsed_gcal.get("gcal_start_datetime")
            new_gcal_end = parsed_gcal.get("gcal_end_datetime")
        
        if db_update_data.get("gcal_start_datetime") != new_gcal_start or \
           db_update_data.get("gcal_end_datetime") != new_gcal_end:
            db_update_data["gcal_start_datetime"] = new_gcal_start
            db_update_data["gcal_end_datetime"] = new_gcal_end
            applied_db_updates = True

    if applied_db_updates:
        save_success = activity_db_module_ref.add_or_update_task(db_update_data)
        if save_success:
            # log_info("task_manager", fn_name, f"Item {item_id} ({item_type}) details updated successfully in DB.") # Verbose
            updated_item_from_db = activity_db_module_ref.get_task(item_id)
            if updated_item_from_db and AGENT_STATE_MANAGER_IMPORTED and _update_task_in_context_tm:
                 _update_task_in_context_tm(user_id, item_id, updated_item_from_db)
            return updated_item_from_db if updated_item_from_db else db_update_data
        else:
            log_error("task_manager", fn_name, f"Failed to save item {item_id} detail updates to DB.", user_id=user_id)
            return None # DB save failed
    else: # No changes were applicable for DB update
        # log_info("task_manager", fn_name, f"No applicable DB detail updates for item {item_id}.") # Verbose
        return existing_item # Return original data if no effective changes

def update_item_status(user_id: str, item_id: str, new_status: str) -> Dict | None:
    fn_name = "update_item_status"
    if not DB_IMPORTED or activity_db_module_ref is None : return None
    # log_info("task_manager", fn_name, f"Setting status='{new_status}' for item {item_id}, user {user_id}") # Verbose
    new_status_clean = new_status.lower().strip().replace(" ", "_")
    allowed_statuses = {"pending", "in_progress", "completed"} # "cancelled" is by cancel_item
    if new_status_clean == "cancelled":
        log_error("task_manager", fn_name, "Use cancel_item() function for 'cancelled' status.", user_id=user_id)
        return None
    if new_status_clean not in allowed_statuses:
         log_error("task_manager", fn_name, f"Invalid status '{new_status}' provided for item {item_id}.", user_id=user_id)
         return None

    existing_item = activity_db_module_ref.get_task(item_id)
    if existing_item is None: return None # get_task logs error
    if existing_item.get("user_id") != user_id: log_error("task_manager", fn_name, f"User mismatch for item {item_id}.", user_id=user_id); return None

    updates_dict = {"status": new_status_clean}
    now_iso_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    if new_status_clean == "completed":
        updates_dict["completed_at"] = now_iso_utc; updates_dict["progress_percent"] = 100
        if existing_item.get("type") == "task": updates_dict["sessions_completed"] = existing_item.get("sessions_planned", 0)
    elif new_status_clean == "pending":
         updates_dict["completed_at"] = None; updates_dict["progress_percent"] = 0
         if existing_item.get("type") == "task": updates_dict["sessions_completed"] = 0
    elif new_status_clean == "in_progress": updates_dict["completed_at"] = None # No change to progress/sessions_completed typically

    item_data_to_save = existing_item.copy(); item_data_to_save.update(updates_dict)
    save_success = activity_db_module_ref.add_or_update_task(item_data_to_save)
    if save_success:
        updated_item_from_db = activity_db_module_ref.get_task(item_id)
        if updated_item_from_db and AGENT_STATE_MANAGER_IMPORTED and _update_task_in_context_tm:
             _update_task_in_context_tm(user_id, item_id, updated_item_from_db)
        return updated_item_from_db if updated_item_from_db else item_data_to_save
    else: log_error("task_manager", fn_name, f"Failed to save status update for {item_id} to DB.", user_id=user_id); return None

def cancel_item(user_id: str, item_id: str) -> bool:
    fn_name = "cancel_item"
    if not DB_IMPORTED or activity_db_module_ref is None : return False
    # log_info("task_manager", fn_name, f"Processing cancellation for item {item_id}, user {user_id}") # Verbose
    item_data = activity_db_module_ref.get_task(item_id)
    if item_data is None: log_warning("task_manager", fn_name, f"Item {item_id} not found in DB for cancel.", user_id=user_id); return True # Assume handled
    if item_data.get("user_id") != user_id: log_error("task_manager", fn_name, f"User mismatch for {item_id}.", user_id=user_id); return False
    if item_data.get("status") == "cancelled": return True

    calendar_api = _get_calendar_api(user_id)
    item_type = item_data.get("type")
    if calendar_api and calendar_api.is_active():
        if item_type == "reminder" and not item_id.startswith("local_"):
            try: calendar_api.delete_event(item_id)
            except Exception as del_err: log_error("task_manager", fn_name, f"Error deleting GCal reminder {item_id}", del_err, user_id=user_id)
        elif item_type == "task":
            session_ids = item_data.get("session_event_ids", [])
            if isinstance(session_ids, list) and session_ids:
                for session_gcal_id in session_ids:
                    if isinstance(session_gcal_id, str) and session_gcal_id:
                        try: calendar_api.delete_event(session_gcal_id)
                        except Exception as sess_del_err: log_error("task_manager", fn_name, f"Error deleting GCal session {session_gcal_id}", sess_del_err, user_id=user_id)
    
    update_payload = item_data.copy(); update_payload["status"] = "cancelled"
    if item_type == "task":
        update_payload.update({"sessions_planned": 0, "sessions_completed": 0, "progress_percent": 0, "session_event_ids": []})
    
    save_success = activity_db_module_ref.add_or_update_task(update_payload)
    if save_success:
        cancelled_item_data = activity_db_module_ref.get_task(item_id)
        if cancelled_item_data and AGENT_STATE_MANAGER_IMPORTED and _update_task_in_context_tm:
             _update_task_in_context_tm(user_id, item_id, cancelled_item_data) # Update context with cancelled state
        return True
    else: log_error("task_manager", fn_name, f"Failed to save cancelled status for {item_id} to DB.", user_id=user_id); return False

# --- Scheduling Functions (Specific to Tasks) ---
def schedule_work_sessions(user_id: str, task_item_id: str, slots_to_book: List[Dict]) -> Dict:
    fn_name = "schedule_work_sessions"
    default_fail = {"success": False, "booked_count": 0, "message": "Error.", "session_ids": []}
    if not DB_IMPORTED or activity_db_module_ref is None : return {**default_fail, "message": "DB unavailable."}

    # log_info("task_manager", fn_name, f"Booking {len(slots_to_book)} sessions for task {task_item_id}, user {user_id}") # Verbose
    calendar_api = _get_calendar_api(user_id)
    if calendar_api is None or not calendar_api.is_active(): return {**default_fail, "message": "Calendar not connected/active."}

    task_metadata = activity_db_module_ref.get_task(task_item_id)
    if task_metadata is None or task_metadata.get("type") != "task":
        return {**default_fail, "message": "Parent Task not found or invalid."}
    if task_metadata.get("user_id") != user_id: return {**default_fail, "message": "Task ownership mismatch."}

    task_title = task_metadata.get("title", "Task Work"); created_gcal_ids = []; errors = []
    for i, slot in enumerate(slots_to_book):
        s_date, s_time, s_end_time = slot.get("date"), slot.get("time"), slot.get("end_time")
        if not all([s_date, s_time, s_end_time]): errors.append(f"Slot {i+1} missing details"); continue
        try:
            start_dt = datetime.strptime(f"{s_date} {s_time}", "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(f"{s_date} {s_end_time}", "%Y-%m-%d %H:%M")
            duration_m = int((end_dt - start_dt).total_seconds() / 60)
            if duration_m <= 0: raise ValueError("Session duration non-positive")
            session_event_data = {
                "title": f"Work: {task_title} [Session {i+1}/{len(slots_to_book)}]",
                "description": f"Work session for task: {task_title}\nParent Task ID: {task_item_id}",
                "date": s_date, "time": s_time, "duration": f"{duration_m}m"
            }
            session_gcal_id = calendar_api.create_event(session_event_data)
            if session_gcal_id: created_gcal_ids.append(session_gcal_id)
            else: errors.append(f"Slot {i+1} GCal creation fail (API returned None)")
        except Exception as e_sess: errors.append(f"Slot {i+1} GCal error: {type(e_sess).__name__}"); log_error(fn_name, f"Error GCal session {i+1} for task {task_item_id}", e_sess,user_id=user_id)
    
    if not created_gcal_ids:
        err_summary = "; ".join(errors) or "Unknown reason"
        return {**default_fail, "message": f"Could not schedule sessions in calendar. Errors: {err_summary}"}

    # Combine with any pre-existing session IDs if this function is called multiple times for same task (additive)
    # However, finalize_task_and_book_sessions_tool now clears old sessions first for a reschedule.
    existing_session_ids = task_metadata.get("session_event_ids", [])
    if not isinstance(existing_session_ids, list): existing_session_ids = [] # Ensure list
    all_session_ids = list(set(existing_session_ids + created_gcal_ids)) # Add new, ensure unique

    update_payload = task_metadata.copy()
    update_payload["sessions_planned"] = len(all_session_ids) # Total planned sessions
    update_payload["session_event_ids"] = all_session_ids
    # Set status to in_progress if not already completed/cancelled, as sessions are now planned
    if update_payload["status"] == "pending": update_payload["status"] = "in_progress"

    save_success = activity_db_module_ref.add_or_update_task(update_payload)
    if save_success:
        updated_task_data = activity_db_module_ref.get_task(task_item_id)
        if updated_task_data and AGENT_STATE_MANAGER_IMPORTED and _update_task_in_context_tm:
            _update_task_in_context_tm(user_id, task_item_id, updated_task_data)
        num_booked = len(created_gcal_ids); s = "s" if num_booked != 1 else ""
        msg = f"Okay, {num_booked} work session{s} scheduled for '{task_title}'."
        if errors: msg += f" (Issues with {len(errors)} other potential slots)."
        return {"success": True, "booked_count": num_booked, "message": msg, "session_ids": created_gcal_ids}
    else: # DB update failed, rollback GCal for sessions created IN THIS CALL
        log_error("task_manager", fn_name, f"Created GCal sessions for {task_item_id}, but failed DB update. Rolling back these GCal sessions.", user_id=user_id)
        if calendar_api:
            for sid in created_gcal_ids: # Only rollback what was just created
                try: calendar_api.delete_event(sid)
                except Exception as rb_err_sess: log_error(fn_name, f"GCal session rollback delete failed for {sid}", rb_err_sess, user_id=user_id)
        return {**default_fail, "message": "Scheduled sessions, but failed to link to task. Calendar changes rolled back."}

def cancel_sessions(user_id: str, task_item_id: str, session_gcal_ids_to_cancel: List[str]) -> Dict:
    fn_name = "cancel_sessions"
    default_fail = {"success": False, "cancelled_count": 0, "message": "Error."}
    if not DB_IMPORTED or activity_db_module_ref is None : return {**default_fail, "message": "DB unavailable."}
    if not session_gcal_ids_to_cancel: return {"success": True, "cancelled_count": 0, "message": "No specific sessions provided to cancel."}

    # log_info("task_manager", fn_name, f"Cancelling {len(session_gcal_ids_to_cancel)} sessions for task {task_item_id}, user {user_id}") # Verbose
    calendar_api = _get_calendar_api(user_id)
    if calendar_api is None or not calendar_api.is_active() : return {**default_fail, "message": "Calendar not connected/active."}

    task_metadata = activity_db_module_ref.get_task(task_item_id)
    if task_metadata is None or task_metadata.get("type") != "task": return {**default_fail, "message": "Parent Task not found."}
    if task_metadata.get("user_id") != user_id: return {**default_fail, "message": "Task ownership mismatch."}

    cancelled_count_api = 0; errors_api = []
    for session_gcal_id in session_gcal_ids_to_cancel:
        if not isinstance(session_gcal_id, str) or session_gcal_id.startswith("local_"): continue
        try:
            if calendar_api.delete_event(session_gcal_id): cancelled_count_api += 1
        except Exception as e_del_sess: errors_api.append(session_gcal_id); log_error(fn_name, f"Error GCal delete session {session_gcal_id}", e_del_sess, user_id=user_id)

    existing_session_ids_db = task_metadata.get("session_event_ids", [])
    if not isinstance(existing_session_ids_db, list): existing_session_ids_db = []
    
    remaining_ids_db = [sid for sid in existing_session_ids_db if sid not in set(session_gcal_ids_to_cancel)]

    update_payload = task_metadata.copy()
    update_payload["sessions_planned"] = len(remaining_ids_db)
    update_payload["session_event_ids"] = remaining_ids_db
    if not remaining_ids_db and update_payload["status"] == "in_progress": # If all sessions cancelled, revert to pending unless already completed/cancelled
        update_payload["status"] = "pending"

    save_success = activity_db_module_ref.add_or_update_task(update_payload)
    if save_success:
        updated_task_data = activity_db_module_ref.get_task(task_item_id)
        if updated_task_data and AGENT_STATE_MANAGER_IMPORTED and _update_task_in_context_tm:
            _update_task_in_context_tm(user_id, task_item_id, updated_task_data)
        msg = f"Successfully cancelled {cancelled_count_api} session(s) from calendar and updated task."
        if errors_api: msg += f" Encountered errors with {len(errors_api)} GCal deletions."
        return {"success": True, "cancelled_count": cancelled_count_api, "message": msg}
    else:
        log_error("task_manager", fn_name, f"Cancelled GCal sessions for {task_item_id}, but failed DB update.", user_id=user_id)
        return {**default_fail, "cancelled_count": cancelled_count_api, "message": "Cancelled sessions in calendar, but failed to update task link."}

# --- END OF FULL services/task_manager.py ---