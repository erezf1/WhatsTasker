# --- START OF FILE tools/metadata_store.py ---

import csv
import os
from datetime import datetime
from tools.logger import log_info, log_error, log_warning

# --- Configuration ---
METADATA_DIR = "data"
METADATA_FILE = os.path.join(METADATA_DIR, "events_metadata.csv")

# --- UPDATED FIELDNAMES (Phase 1 - Scheduler Prep) ---
FIELDNAMES = [
    # Core Identifiers & Status
    "event_id",                 # Primary Key (Matches Google Calendar Event ID if applicable, otherwise local_uuid)
    "user_id",                  # User identifier
    "type",                     # 'task', 'reminder', 'event' (event type added later by sync if needed)
    "status",                   # 'pending', 'in progress', 'completed', 'cancelled'

    # Core Content
    "title",                    # Task/Reminder/Event Title (potentially synced from GCal)
    "description",              # Task/Reminder/Event Description (potentially synced from GCal)
    "date",                     # User's intended local Due Date / Reminder Date (YYYY-MM-DD)
    "time",                     # User's intended local Due Time / Reminder Time (HH:MM or None)

    # Task Specific Fields
    "estimated_duration",       # User's estimate of work time (e.g., "4h", "90m")
    "sessions_planned",         # Integer: Total work sessions needed/scheduled based on estimate/prefs
    "sessions_completed",       # Integer: Work sessions marked as completed
    "progress_percent",         # Integer: 0-100 calculated progress (primarily for tasks)
    "session_event_ids",        # String: JSON encoded list of GCal IDs for work sessions ["id1", "id2"]

    # Scheduling & Sync Related (Some potentially updated by GCal)
    "project",                  # Associated project tag (or None)
    "series_id",                # ID for recurring events (if implemented later)
    "gcal_start_datetime",      # Full ISO 8601 Aware Timestamp from GCal (e.g., 2023-10-27T10:00:00+03:00)
    "gcal_end_datetime",        # Full ISO 8601 Aware Timestamp from GCal
    "duration",                 # Original GCal event duration string (e.g., PT1H) - informational

    # Internal Tracking & Metadata
    "created_at",               # ISO 8601 UTC timestamp ('Z') when metadata record was created
    "completed_at",             # ISO 8601 UTC timestamp ('Z') when task was completed (or None)
    "internal_reminder_sent",   # ISO 8601 UTC timestamp ('Z') when reminder was sent, or "" if not sent
    "original_date",            # Original user input for date (e.g., 'tomorrow') - informational
    "progress",                 # Qualitative progress notes (Potentially deprecated - keep for now)
    "internal_reminder_minutes",# (DEPRECATED - use Notification_Lead_Time pref instead) - Keep field for backward compat? Or remove? Let's remove.
]
# --- END OF UPDATED FIELDNAMES ---

# --- Initialization ---
def init_metadata_store():
    """Ensures the data directory and metadata CSV file exist with headers."""
    fn_name = "init_metadata_store"
    try:
        os.makedirs(METADATA_DIR, exist_ok=True)
        header_correct = False
        file_exists = os.path.exists(METADATA_FILE)

        if file_exists:
             try:
                 with open(METADATA_FILE, "r", newline="", encoding="utf-8") as f_check:
                      first_line = f_check.readline()
                      if first_line:
                           # Handle potential BOM (Byte Order Mark)
                           if first_line.startswith('\ufeff'):
                               first_line = first_line[1:]
                           reader = csv.reader([first_line])
                           try:
                               header = next(reader)
                               header_correct = (header == FIELDNAMES)
                           except StopIteration: # Empty file after BOM or similar
                               header_correct = False
                      else: # File exists but is empty
                           header_correct = False
             except Exception as read_err:
                 log_warning("metadata_store", fn_name, f"Could not read or parse existing header: {read_err}")
                 header_correct = False # Assume incorrect if read fails

        if not file_exists or not header_correct:
            action = "Overwriting" if file_exists else "Creating"
            log_warning("metadata_store", fn_name, f"{action} {METADATA_FILE} with new headers (Required: {len(FIELDNAMES)}, Found Correct: {header_correct}).")
            # Optional: Backup existing file before overwriting
            # if file_exists and not header_correct:
            #     backup_path = METADATA_FILE + f".bak_{datetime.now():%Y%m%d_%H%M%S}"
            #     try:
            #         os.rename(METADATA_FILE, backup_path)
            #         log_info("metadata_store", fn_name, f"Backed up existing file to {backup_path}")
            #     except OSError as bak_err:
            #         log_error("metadata_store", fn_name, f"Could not back up existing metadata file: {bak_err}")

            try:
                with open(METADATA_FILE, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    writer.writeheader()
                log_info("metadata_store", fn_name, f"Successfully wrote headers to {METADATA_FILE}.")
            except IOError as write_err:
                 log_error("metadata_store", fn_name, f"Failed to write headers to {METADATA_FILE}", write_err)
                 raise # Reraise critical error

    except IOError as e:
        log_error("metadata_store", fn_name, f"Failed to initialize metadata store directory/file at {METADATA_FILE}", e)
        raise


# --- Core CRUD Functions ---

def save_event_metadata(event_metadata: dict):
    """
    Saves or updates a single event's metadata using atomic write.
    Ensures only fields defined in FIELDNAMES are saved.
    """
    fn_name = "save_event_metadata"
    init_metadata_store() # Ensure file exists with correct header

    event_id = event_metadata.get("event_id")
    if not event_id:
        log_error("metadata_store", fn_name, "Cannot save metadata: 'event_id' is missing.")
        raise KeyError("'event_id' is required in event_metadata dictionary.")

    all_events = []
    updated = False

    # Read existing data safely
    try:
        all_events = load_all_metadata()
    except Exception as load_e:
         log_error("metadata_store", fn_name, f"Error loading existing metadata before save: {load_e}. Proceeding cautiously.", load_e)
         all_events = [] # Start fresh if load fails

    # Prepare the row to be saved, ensuring only valid fields are included
    # And convert values to string for CSV compatibility (handle None)
    row_to_save = {}
    for field in FIELDNAMES:
        value = event_metadata.get(field)
        # Convert non-string/non-None values to string. Keep None as None (will become empty string in CSV).
        if value is not None and not isinstance(value, str):
            row_to_save[field] = str(value)
        else:
            row_to_save[field] = value # Keep strings and None as they are

    # Update or append
    found_index = -1
    for i, existing_event in enumerate(all_events):
        if existing_event.get("event_id") == event_id:
            found_index = i
            break

    if found_index != -1:
        log_info("metadata_store", fn_name, f"Updating existing metadata for event {event_id}")
        all_events[found_index] = row_to_save
        updated = True
    else:
        log_info("metadata_store", fn_name, f"Adding new metadata for event {event_id}")
        all_events.append(row_to_save)

    # Write data back safely using atomic write
    temp_file_path = METADATA_FILE + ".tmp"
    try:
        with open(temp_file_path, "w", newline="", encoding="utf-8") as f:
            # Use extrasaction='ignore' - shouldn't be needed due to explicit field loop, but safe.
            # Handle None values by writing empty strings using DictWriter's default behavior.
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_events) # Pass the list of dictionaries
        os.replace(temp_file_path, METADATA_FILE)
        log_info("metadata_store", fn_name, f"Successfully saved metadata. Total records: {len(all_events)}")
    except (IOError, csv.Error) as e: # Catch specific CSV/IO errors
         log_error("metadata_store", fn_name, f"Error writing metadata file {METADATA_FILE}: {e}", e)
         if os.path.exists(temp_file_path):
             try: os.remove(temp_file_path)
             except OSError: pass
         raise # Reraise after logging
    except Exception as e:
         log_error("metadata_store", fn_name, f"Unexpected error writing metadata file {METADATA_FILE}", e)
         if os.path.exists(temp_file_path):
             try: os.remove(temp_file_path)
             except OSError: pass
         raise

def get_event_metadata(event_id: str) -> dict:
    """
    Retrieves metadata for a specific event ID. Returns empty dict if not found/error.
    """
    fn_name = "get_event_metadata"
    init_metadata_store()
    try:
        with open(METADATA_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("event_id") == event_id:
                    # Convert numeric/json fields back if needed, or handle downstream
                    # Example: row['sessions_planned'] = int(row['sessions_planned'] or 0)
                    return row
        return {} # Not found
    except FileNotFoundError:
        log_info("metadata_store", fn_name, f"Metadata file not found when getting {event_id}.")
        return {}
    except Exception as e:
        log_error("metadata_store", fn_name, f"Error reading metadata file for event {event_id}", e)
        return {}


def delete_event_metadata(event_id: str) -> bool:
    """Deletes metadata for a specific event ID. Rewrites the file."""
    fn_name = "delete_event_metadata"
    init_metadata_store()
    if not event_id:
        log_warning("metadata_store", fn_name, "Attempted to delete metadata with empty event_id.")
        return False

    all_events = []
    found = False
    try:
        all_events = load_all_metadata() # Read all current data
        original_count = len(all_events)
        # Filter out the event to delete
        filtered_events = [e for e in all_events if e.get("event_id") != event_id]
        found = len(filtered_events) < original_count

        if not found:
             log_warning("metadata_store", fn_name, f"Event ID {event_id} not found in metadata. No deletion performed.")
             return True # Consider it success if it wasn't there to delete

        all_events = filtered_events # Use the filtered list for saving

    except Exception as load_e:
         log_error("metadata_store", fn_name, f"Error loading metadata before delete operation for {event_id}", load_e)
         return False # Fail if we couldn't load existing data

    # Write the filtered data back using atomic write
    temp_file_path = METADATA_FILE + ".tmp"
    try:
        with open(temp_file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_events)
        os.replace(temp_file_path, METADATA_FILE)
        log_info("metadata_store", fn_name, f"Successfully deleted metadata for {event_id}. Remaining records: {len(all_events)}")
        return True
    except Exception as e:
        log_error("metadata_store", fn_name, f"Error writing metadata after delete operation for {event_id}", e)
        if os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except OSError: pass
        return False


def list_metadata(user_id: str, start_date_str: str | None = None, end_date_str: str | None = None) -> list[dict]:
    """Lists metadata for a user, optionally filtered by 'date' field."""
    fn_name = "list_metadata"
    init_metadata_store()
    results = []
    try:
        with open(METADATA_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("user_id") == user_id:
                    include = True
                    # Date filtering logic (uses 'date' field, not gcal_start_datetime)
                    row_date_str = row.get("date")
                    if row_date_str and (start_date_str or end_date_str):
                        try:
                            row_date = datetime.strptime(row_date_str, "%Y-%m-%d").date()
                            if start_date_str:
                                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                                if row_date < start_date: include = False
                            if include and end_date_str:
                                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                                if row_date > end_date: include = False
                        except (ValueError, TypeError):
                             # Ignore rows with invalid date format if filtering is active
                             include = False
                    if include:
                        results.append(row)
        return results
    except FileNotFoundError:
        log_info("metadata_store", fn_name, "Metadata file not found.")
        return []
    except Exception as e:
        log_error("metadata_store", fn_name, f"Error reading metadata for user {user_id}", e)
        return []


def load_all_metadata() -> list[dict]:
    """Loads all records from the metadata CSV file."""
    fn_name = "load_all_metadata"
    init_metadata_store()
    try:
        with open(METADATA_FILE, "r", newline="", encoding="utf-8") as f:
            # Check if file is empty before trying to read
            first_char = f.read(1)
            if not first_char:
                return [] # Return empty list for empty file
            f.seek(0) # Reset cursor
            reader = csv.DictReader(f)
            data = list(reader) # Read all rows into a list
            return data
    except FileNotFoundError:
        log_info("metadata_store", fn_name, "Metadata file not found.")
        return []
    except Exception as e:
        log_error("metadata_store", fn_name, f"Error reading all metadata: {e}", e)
        return []


# --- Ensure store is initialized when module is loaded ---
init_metadata_store()

# --- END OF FILE tools/metadata_store.py ---