# tools/user_task_manager.py
# Note: This file becomes obsolete in the v0.7 architecture.
# Logic moved to services/task_manager.py

from datetime import datetime, timedelta # Make sure timedelta is imported if used
# Correct potential import path if moved
from tools.google_calendar_api import GoogleCalendarAPI # Keep original path if not moved
from tools import activity_db.list_tasks_for_user
from tools.logger import log_info, log_error, log_warning
# --- REMOVED get_agent import ---
# from users.user_manager import get_agent
import os # Keep os for path operations if needed
from typing import List, Dict, Any, Optional # For type hints

class UserTaskManager:
    def __init__(self, user_id: str, calendar_api: GoogleCalendarAPI | None = None): # Type hint calendar_api
        self.user_id = user_id
        # Assign directly, could be None if GCal isn't setup/working
        self.calendar: GoogleCalendarAPI | None = calendar_api
        # Use the is_active check which verifies self.service is not None
        calendar_connected = calendar_api.is_active() if calendar_api else False
        log_info("UserTaskManager", "__init__", f"Task manager initialized for {user_id}. Calendar connected: {calendar_connected}")

    # --- MODIFIED create_event signature and logic ---
    def create_event(self, event_data: Dict, active_tasks_context: Optional[List[Dict]] = None) -> Optional[str]:
        """
        Creates event, saves metadata, adds to provided memory context. (OLD VERSION)
        Returns Google Calendar event ID or None.
        """
        google_event_id = None
        if self.calendar and self.calendar.is_active(): # Check if active
            log_info("UserTaskManager", "create_event", f"Attempting to create event on Google Calendar for user {self.user_id}")
            # Pass event_data directly, GCalAPI handles formatting
            google_event_id = self.calendar.create_event(event_data)
            if not google_event_id:
                log_error("UserTaskManager", "create_event", f"Failed to create event on Google Calendar for {self.user_id}. Aborting metadata save.")
                return None # Abort if GCal fails
        else:
            log_warning("UserTaskManager", "create_event", f"Calendar not available/active for {self.user_id}. Cannot create GCal event.")
            return None # Abort if no GCal ID source


        # --- Prepare and Save Metadata ---
        metadata_to_save = {
            # Provide defaults for ALL fieldnames FIRST
            "event_id": google_event_id, "user_id": self.user_id, "type": None,
            "status": "pending", "project": None, "progress": None,
            "internal_reminder_minutes": None, "internal_reminder_sent": None,
            "series_id": None, "created_at": datetime.utcnow().isoformat(),
            "completed_at": None, "original_date": None, "title": None,
            "description": None, "date": None, "time": None, "duration": None,
             # Add other FIELDNAMES defaults here...

            # Overwrite with provided event_data
            **event_data,

            # Explicitly set/overwrite crucial fields again
             "event_id": google_event_id, "user_id": self.user_id,
             "status": event_data.get("status", "pending"),
             "date": event_data.get("date"), "time": event_data.get("time"),
             "title": event_data.get("title", event_data.get("description")),
             "description": event_data.get("description"),
             "type": event_data.get("type", "reminder" if not event_data.get("duration") else "task"),
             "original_date": event_data.get("date") # Keep original requested date
        }
        allowed_keys = set(metadata_store.FIELDNAMES)
        metadata_to_save_cleaned = {k: v for k, v in metadata_to_save.items() if k in allowed_keys}
        if 'event_id' not in metadata_to_save_cleaned or 'user_id' not in metadata_to_save_cleaned:
            log_error("UserTaskManager", "create_event", f"Essential keys missing after cleaning metadata for {google_event_id}. Aborting.")
            return None

        try:
            log_info("UserTaskManager", "create_event", f"Saving metadata for event {google_event_id} (user {self.user_id})")
            activity_db.add_or_update_task(metadata_to_save_cleaned)
            log_info("UserTaskManager", "create_event", f"Successfully saved metadata for event {google_event_id}")

            # --- Update Provided Memory Context ---
            if active_tasks_context is not None: # Check if context was passed
                active_tasks_context.append(metadata_to_save_cleaned)
                log_info("UserTaskManager", "create_event", f"Appended new event {google_event_id} to provided context. New size: {len(active_tasks_context)}")
            else:
                log_warning("UserTaskManager", "create_event", "No active_tasks_context provided, skipping in-memory update.")
            # --- End Memory Update ---

            return google_event_id
        except Exception as e:
            log_error("UserTaskManager", "create_event", f"Failed to save metadata for event {google_event_id}", e)
            # Cleanup GCal event if metadata save failed
            if self.calendar and self.calendar.is_active():
                 log_warning("UserTaskManager", "create_event", f"Attempting cleanup: delete orphaned GCal event {google_event_id}")
                 self.calendar.delete_event(google_event_id)
            return None

    # --- MODIFIED update_event signature and logic ---
    def update_event(self, event_id: str, updates: Dict, active_tasks_context: Optional[List[Dict]] = None) -> bool:
        """Updates metadata, optionally GCal, and provided memory context. Returns True on metadata success."""
        log_info("UserTaskManager", "update_event", f"Updating event {event_id} for user {self.user_id}")
        updated_metadata = None
        try:
            original_metadata = activity_db.get_task(event_id)
            if not original_metadata: raise ValueError(f"Metadata for {event_id} not found.")
            updated_metadata = {**original_metadata, **updates} # Apply updates
            # Ensure all required fields are still present after update if necessary
            activity_db.add_or_update_task(updated_metadata)
            log_info("UserTaskManager", "update_event", f"Successfully updated metadata for event {event_id}")

            # --- Update Provided Memory Context ---
            if active_tasks_context is not None:
                found_in_context = False
                for i, item in enumerate(active_tasks_context):
                    if item.get("event_id") == event_id:
                        active_tasks_context[i] = updated_metadata # Replace item
                        found_in_context = True
                        log_info("UserTaskManager", "update_event", f"Updated event {event_id} in provided context.")
                        break
                if not found_in_context:
                     log_warning("UserTaskManager", "update_event", f"Event {event_id} not found in provided context for update.")
                     # Optionally add if status implies it should be active?
                     if updated_metadata.get("status", "pending").lower() not in ["completed", "cancelled", "done"]:
                          active_tasks_context.append(updated_metadata)
                          log_info("UserTaskManager", "update_event", f"Added updated event {event_id} to provided context as it was missing.")
            else:
                 log_warning("UserTaskManager", "update_event", "No active_tasks_context provided, skipping in-memory update.")
            # --- End Memory Update ---

        except Exception as meta_e:
             log_error("UserTaskManager", "update_event", f"Failed to update metadata for event {event_id}", meta_e)
             return False # Signal failure if metadata update fails

        # Update Google Calendar (if connected) - Separate try/except
        if self.calendar and self.calendar.is_active():
            try:
                # Prepare GCal updates (only send relevant fields)
                gcal_updates = {k: v for k, v in updates.items() if k in ["title", "description", "start_datetime", "end_datetime", "timeZone", "date"]}
                # Add inferred fields if needed
                if "title" not in gcal_updates and "title" in updated_metadata: gcal_updates["title"] = updated_metadata["title"]
                if "description" not in gcal_updates and "description" in updated_metadata: gcal_updates["description"] = updated_metadata["description"]
                # TODO: Add logic to reconstruct start/end datetimes if only date/time/duration changed in metadata

                if gcal_updates:
                     log_info("UserTaskManager", "update_event", f"Attempting to update GCal event {event_id}")
                     update_success = self.calendar.update_event(event_id, gcal_updates)
                     if not update_success:
                          log_warning("UserTaskManager", "update_event", f"GCal update reported failure for {event_id}, but metadata was updated.")
                else:
                     log_info("UserTaskManager", "update_event", "No GCal specific fields to update.")
            except Exception as cal_e:
                log_error("UserTaskManager", "update_event", f"Failed to update GCal event {event_id} after metadata update.", cal_e)
        else:
            log_info("UserTaskManager", "update_event", f"Calendar not connected/inactive. Skipping GCal update for {event_id}")

        return True # Return True if metadata update succeeded

    # --- MODIFIED delete_event signature and logic ---
    def delete_event(self, event_id: str, active_tasks_context: Optional[List[Dict]] = None) -> bool:
        """Deletes event from GCal (if connected), metadata store, and provided memory context. Returns True on metadata success."""
        log_info("UserTaskManager", "delete_event", f"Deleting event {event_id} for user {self.user_id}")
        gcal_deleted_or_skipped = False
        # 1. Delete from Google Calendar first
        if self.calendar and self.calendar.is_active():
            try:
                gcal_deleted_or_skipped = self.calendar.delete_event(event_id) # Returns True on success or 404/410
                if not gcal_deleted_or_skipped:
                     log_warning("UserTaskManager", "delete_event", f"GCal deletion reported failure for {event_id}, proceeding metadata deletion cautiously.")
            except Exception as cal_e:
                log_error("UserTaskManager", "delete_event", f"Error deleting GCal event {event_id}. Proceeding metadata delete.", cal_e)
                gcal_deleted_or_skipped = False # Assume failed
        else:
             log_info("UserTaskManager", "delete_event", f"Calendar not connected/inactive. Skipping GCal deletion.")
             gcal_deleted_or_skipped = True # Okay to proceed

        # 2. Delete from Metadata Store (only if GCal delete was ok or skipped)
        metadata_deleted = False
        if gcal_deleted_or_skipped:
            try:
                metadata_deleted = activity_db.delete_task(event_id) # Returns True if deleted or not found
                if metadata_deleted:
                    log_info("UserTaskManager", "delete_event", f"Successfully deleted/confirmed missing metadata for event {event_id}")
                    # --- Remove from Provided Memory Context ---
                    if active_tasks_context is not None:
                         original_len = len(active_tasks_context)
                         active_tasks_context[:] = [item for item in active_tasks_context if item.get("event_id") != event_id] # Modify list in-place
                         if len(active_tasks_context) < original_len:
                              log_info("UserTaskManager", "delete_event", f"Removed event {event_id} from provided context.")
                         else:
                              log_warning("UserTaskManager", "delete_event", f"Event {event_id} not found in provided context for removal.")
                    else:
                         log_warning("UserTaskManager", "delete_event", "No active_tasks_context provided, skipping in-memory removal.")
                    # --- End Memory Update ---
                else:
                     # delete_event_metadata already logged warning if not found
                     pass
            except Exception as meta_e:
                log_error("UserTaskManager", "delete_event", f"Failed to delete metadata for event {event_id}", meta_e)
                metadata_deleted = False # Ensure failure is noted
        else:
             log_error("UserTaskManager", "delete_event", f"Skipping metadata deletion for {event_id} because GCal deletion failed critically.")

        return metadata_deleted # Return True if metadata deletion step was successful


    # --- MODIFIED mark_event_completed signature and logic ---
    def mark_event_completed(self, event_id: str, active_tasks_context: Optional[List[Dict]] = None) -> bool:
        """Marks an event completed in metadata ONLY and removes from provided memory context."""
        log_info("UserTaskManager", "mark_event_completed", f"Marking event {event_id} as completed (metadata only)")
        try:
            completion_time = datetime.utcnow().isoformat() # Use ISO format
            metadata = activity_db.get_task(event_id)
            if not metadata: raise ValueError(f"Metadata for event {event_id} not found.")

            updates = {"status": "completed", "completed_at": completion_time}
            metadata.update(updates)
            activity_db.add_or_update_task(metadata) # Save updated metadata

            # --- Remove from Provided Memory Context ---
            if active_tasks_context is not None:
                original_len = len(active_tasks_context)
                active_tasks_context[:] = [item for item in active_tasks_context if item.get("event_id") != event_id] # Modify in-place
                if len(active_tasks_context) < original_len:
                     log_info("UserTaskManager", "mark_event_completed", f"Removed completed event {event_id} from provided context.")
                else:
                     log_warning("UserTaskManager", "mark_event_completed", f"Event {event_id} not found in provided context for removal (already removed?).")
            else:
                 log_warning("UserTaskManager", "mark_event_completed", "No active_tasks_context provided, skipping in-memory removal.")
            # --- End Memory Update ---

            return True # Metadata update successful

        except Exception as e:
             log_error("UserTaskManager", "mark_event_completed", f"Failed to mark event {event_id} as completed", e)
             return False # Signal failure

    # --- get_user_events remains the same ---
    def get_user_events(self, start_date: str, end_date: str) -> List[Dict]:
        # ... (existing implementation is okay, fetches GCal then tries to enrich from metadata) ...
        log_info("UserTaskManager", "get_user_events", f"Getting events for user {self.user_id} from {start_date} to {end_date}")
        calendar_events = []
        if self.calendar and self.calendar.is_active():
            try:
                 calendar_events = self.calendar.list_events(start_date, end_date)
            except Exception as e:
                 log_error("UserTaskManager", "get_user_events", f"Failed to list GCal events for {self.user_id}", e)
                 return []
        else:
            log_warning("UserTaskManager", "get_user_events", f"Calendar not connected/inactive for {self.user_id}. Cannot fetch GCal events.")
            return [] # Return empty if calendar is the primary source

        enriched_events = []
        for event in calendar_events:
            event_id = event.get("event_id") # Using standardized key now
            if not event_id: continue

            try:
                meta = activity_db.get_task(event_id)
                if meta:
                    combined_data = {**event, **meta} # Metadata fields overwrite GCal fields if keys overlap
                    enriched_events.append(combined_data)
                else:
                    log_warning("UserTaskManager", "get_user_events", f"Metadata not found for GCal event {event_id}. Using GCal data only.")
                    enriched_events.append(event)
            except Exception as meta_e:
                 log_error("UserTaskManager", "get_user_events", f"Error fetching metadata for event {event_id}", meta_e)
                 enriched_events.append(event) # Append basic event on metadata error

        log_info("UserTaskManager", "get_user_events", f"Returning {len(enriched_events)} enriched events for {self.user_id}")
        return enriched_events

