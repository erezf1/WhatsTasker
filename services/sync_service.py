# --- START OF FILE services/sync_service.py ---
"""
Provides functionality to get a combined view of WhatsTasker-managed items
and external Google Calendar events for a specific user and time period.
Does NOT modify the persistent metadata store for external events.
"""
import traceback
from datetime import datetime, timedelta, timezone # Import timezone
from typing import List, Dict, Any # Keep Dict, List, Any for internal hints

from tools.logger import log_info, log_error, log_warning
from tools import metadata_store

# Service/Tool Imports
try:
    from users.user_manager import get_agent
except ImportError:
    log_error("sync_service", "import", "Failed to import user_manager.get_agent")
    def get_agent(*args, **kwargs): return None # Dummy

try:
    from services.agent_state_manager import update_task_in_context # For updating context after merge
    AGENT_STATE_IMPORTED = True
except ImportError:
    log_error("sync_service", "import", "Failed to import agent_state_manager functions.")
    AGENT_STATE_IMPORTED = False
    def update_task_in_context(*args, **kwargs): pass # Dummy

# We need GoogleCalendarAPI type for isinstance check, even if not used directly
try:
    from tools.google_calendar_api import GoogleCalendarAPI
except ImportError:
    GoogleCalendarAPI = None # Define as None if import fails


def get_synced_context_snapshot(user_id, start_date_str, end_date_str):
    """
    Fetches WT metadata and GCal events for a period, merges them,
    identifies external events, and returns a combined list of dictionaries.
    Does not persist external events. Updates metadata for WT items if GCal changed.
    """
    fn_name = "get_synced_context_snapshot"
    log_info("sync_service", fn_name, f"Generating synced context for user {user_id}, range: {start_date_str} to {end_date_str}")

    # 1. Get Calendar API instance
    agent_state = get_agent(user_id) if get_agent else None
    calendar_api = None
    if agent_state and GoogleCalendarAPI:
        calendar_api_maybe = agent_state.get("calendar")
        if isinstance(calendar_api_maybe, GoogleCalendarAPI) and calendar_api_maybe.is_active():
            calendar_api = calendar_api_maybe

    # 2. Fetch GCal Events
    gcal_events_list = []
    if calendar_api:
        try:
            log_info("sync_service", fn_name, f"Fetching GCal events for {user_id}...")
            gcal_events_list = calendar_api.list_events(start_date_str, end_date_str)
            log_info("sync_service", fn_name, f"Fetched {len(gcal_events_list)} GCal events for {user_id}.")
        except Exception as e:
            log_error("sync_service", fn_name, f"Error fetching GCal events for {user_id}", e)
            # Continue without GCal events, will only use metadata
    else:
        log_info("sync_service", fn_name, f"GCal API not available or inactive for {user_id}, skipping GCal fetch.")

    # 3. Fetch WT Metadata
    wt_metadata_list = []
    try:
        log_info("sync_service", fn_name, f"Fetching WT metadata for {user_id}...")
        # Fetch within the same range for comparison, but list_metadata filters by 'date' field
        # which might miss tasks scheduled via GCal sessions. Fetching all might be safer?
        # For now, stick to range based on 'date' field as implemented in list_metadata.
        wt_metadata_list = metadata_store.list_metadata(user_id, start_date_str, end_date_str)
        log_info("sync_service", fn_name, f"Fetched {len(wt_metadata_list)} WT metadata items for {user_id}.")
    except Exception as e:
        log_error("sync_service", fn_name, f"Error fetching WT metadata for {user_id}", e)
        # If metadata fails, we might still proceed with just GCal events? Or return empty?
        # Let's return empty for now if metadata fails.
        return []

    # 4. Create Maps for Efficient Lookup
    gcal_events_map = {e['event_id']: e for e in gcal_events_list if e.get('event_id')}
    wt_metadata_map = {m['event_id']: m for m in wt_metadata_list if m.get('event_id')}

    # 5. Merge & Identify Types
    aggregated_context_list: List[Dict[str, Any]] = []
    processed_wt_ids = set() # Keep track of WT items found in GCal

    # Iterate through GCal events first
    for event_id, gcal_data in gcal_events_map.items():
        if event_id in wt_metadata_map:
            # --- WT Item Found in GCal ---
            processed_wt_ids.add(event_id)
            meta_data = wt_metadata_map[event_id]
            # Merge: Start with metadata, update with latest GCal info
            merged_data = meta_data.copy()
            gcal_start = gcal_data.get("gcal_start_datetime")
            gcal_end = gcal_data.get("gcal_end_datetime")
            gcal_title = gcal_data.get("title")
            needs_meta_update = False

            # Update GCal times if they differ
            if gcal_start != merged_data.get("gcal_start_datetime"):
                merged_data["gcal_start_datetime"] = gcal_start
                needs_meta_update = True
            if gcal_end != merged_data.get("gcal_end_datetime"):
                merged_data["gcal_end_datetime"] = gcal_end
                needs_meta_update = True
            # Optionally update title if it changed in GCal? Be cautious.
            # if gcal_title and gcal_title != merged_data.get("title"):
            #    merged_data["title"] = gcal_title
            #    needs_meta_update = True

            aggregated_context_list.append(merged_data)

            # If relevant fields changed, update the persistent metadata store
            if needs_meta_update:
                log_info("sync_service", fn_name, f"GCal data changed for WT item {event_id}. Updating metadata.")
                try:
                    meta_to_save = {fn: merged_data.get(fn) for fn in metadata_store.FIELDNAMES}
                    metadata_store.save_event_metadata(meta_to_save)
                    # Also update in-memory state if possible
                    if AGENT_STATE_IMPORTED:
                        update_task_in_context(user_id, event_id, meta_to_save)
                except Exception as save_err:
                     log_error("sync_service", fn_name, f"Failed to save updated metadata for WT item {event_id} after GCal merge.", save_err)

        else:
            # --- External GCal Event ---
            external_event_data = gcal_data.copy() # Start with GCal data
            external_event_data["type"] = "external_event" # Mark its type
            external_event_data["user_id"] = user_id # Ensure user_id is present
            # Add placeholder status? Or leave as None? Let's leave it.
            # external_event_data["status"] = "calendar_event"
            aggregated_context_list.append(external_event_data)

    # 6. Add WT Items Not Found in GCal Fetch Window
    for event_id, meta_data in wt_metadata_map.items():
        if event_id not in processed_wt_ids:
            # This is a WT item (task/reminder) that wasn't in the GCal list
            # Could be outside the GCal window, deleted from GCal, or never had a GCal entry (local task)
            log_info("sync_service", fn_name, f"Including WT item {event_id} which was not found in GCal fetch window.")
            aggregated_context_list.append(meta_data) # Add the metadata as is

    log_info("sync_service", fn_name, f"Generated aggregated context with {len(aggregated_context_list)} items for {user_id}.")
    return aggregated_context_list

# --- END OF FILE services/sync_service.py ---