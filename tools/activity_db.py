# --- START OF tools/activity_db.py ---

import sqlite3
import os
import json
import threading
from datetime import datetime, timezone # Use timezone-aware datetime
from tools.logger import log_info, log_error, log_warning

# --- Configuration ---
DB_DIR = "data"
DB_FILE = os.path.join(DB_DIR, "whatstasker_activity.db")
DB_LOCK = threading.Lock() # Lock for thread-safe writes if needed later

# --- Initialization ---
def init_db():
    """Initializes the database: creates directory, file, tables, and indexes if they don't exist."""
    fn_name = "init_db"
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        log_info("activity_db", fn_name, f"Connecting to database: {DB_FILE}")
        # Use 'with' statement for automatic connection management
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn: # Allow access from different threads if needed by scheduler/web server
            cursor = conn.cursor()
            log_info("activity_db", fn_name, "Ensuring tables and indexes exist...")

            # === Create users_tasks Table ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users_tasks (
                event_id TEXT PRIMARY KEY NOT NULL,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                date TEXT,
                time TEXT,
                estimated_duration TEXT,
                sessions_planned INTEGER DEFAULT 0,
                sessions_completed INTEGER DEFAULT 0,
                progress_percent INTEGER DEFAULT 0,
                session_event_ids TEXT DEFAULT '[]',
                project TEXT,
                series_id TEXT,
                gcal_start_datetime TEXT,
                gcal_end_datetime TEXT,
                duration TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                internal_reminder_sent TEXT,
                original_date TEXT,
                progress TEXT
            )
            """)
            # === Create Indexes for users_tasks ===
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON users_tasks (user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id_status ON users_tasks (user_id, status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id_date ON users_tasks (user_id, date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON users_tasks (project)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON users_tasks (status)")

            # === Create messages Table ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                content TEXT NOT NULL,
                raw_user_id TEXT,
                bridge_message_id TEXT
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id)")

            # === Create llm_activity Table ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_type TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                tool_name TEXT,
                tool_call_id TEXT,
                content_summary TEXT,
                details_json TEXT
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_activity_timestamp ON llm_activity (timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_activity_user_id ON llm_activity (user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_activity_type ON llm_activity (activity_type)")

            # === Create system_logs Table ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                module TEXT NOT NULL,
                function TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT,
                user_id_context TEXT
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_timestamp ON system_logs (timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs (level)")

            conn.commit() # Commit table/index creations
            log_info("activity_db", fn_name, "Database initialization check complete.")

    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database initialization failed: {e}", e)
        raise # Re-raise critical error

# --- users_tasks Table Functions ---

# Define the expected fields for consistency (matches FIELDNAMES from metadata_store)
TASK_FIELDS = [
    "event_id", "user_id", "type", "status", "title", "description", "date", "time",
    "estimated_duration", "sessions_planned", "sessions_completed", "progress_percent",
    "session_event_ids", "project", "series_id", "gcal_start_datetime",
    "gcal_end_datetime", "duration", "created_at", "completed_at",
    "internal_reminder_sent", "original_date", "progress"
]

def _dict_factory(cursor, row):
    """Converts DB rows into dictionaries."""
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

def add_or_update_task(task_data: dict) -> bool:
    """Adds a new task or updates an existing one based on event_id."""
    fn_name = "add_or_update_task"
    event_id = task_data.get("event_id")
    if not event_id:
        log_error("activity_db", fn_name, "Cannot save task: 'event_id' is missing.")
        return False

    # Prepare data: ensure only valid fields, handle JSON, default numerics
    db_params = []
    placeholders = []
    columns = []
    for field in TASK_FIELDS:
        columns.append(field)
        value = task_data.get(field)
        if field == "session_event_ids":
            # Ensure it's a JSON string, default to '[]'
            if isinstance(value, (list, tuple)):
                value = json.dumps(value)
            elif not isinstance(value, str) or not value.strip():
                value = '[]'
        elif field in ["sessions_planned", "sessions_completed", "progress_percent"]:
            # Ensure it's an integer, default to 0
            try:
                value = int(value) if value is not None else 0
            except (ValueError, TypeError):
                value = 0
        # Convert None to NULL for DB, handle other types simply
        db_params.append(value)
        placeholders.append("?")

    sql = f"""
    INSERT INTO users_tasks ({', '.join(columns)})
    VALUES ({', '.join(placeholders)})
    ON CONFLICT(event_id) DO UPDATE SET
    {', '.join([f'{col}=excluded.{col}' for col in columns if col != 'event_id'])}
    """
    # Note: UPSERT syntax `ON CONFLICT...` requires SQLite 3.24.0+

    try:
        with DB_LOCK: # Use lock for write operations if needed later
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute(sql, db_params)
                conn.commit()
        log_info("activity_db", fn_name, f"Successfully added/updated task {event_id}")
        return True
    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database error saving task {event_id}: {e}", e)
        return False
    except Exception as e: # Catch other potential errors like JSON issues
        log_error("activity_db", fn_name, f"Unexpected error saving task {event_id}: {e}", e)
        return False

def get_task(event_id: str) -> dict | None:
    """Retrieves a single task by event_id."""
    fn_name = "get_task"
    sql = "SELECT * FROM users_tasks WHERE event_id = ?"
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            conn.row_factory = _dict_factory # Return rows as dicts
            cursor = conn.cursor()
            cursor.execute(sql, (event_id,))
            row = cursor.fetchone()
            if row:
                # Decode JSON field
                try:
                    row['session_event_ids'] = json.loads(row.get('session_event_ids', '[]') or '[]')
                except (json.JSONDecodeError, TypeError):
                    log_warning("activity_db", fn_name, f"Failed to decode session_event_ids JSON for task {event_id}. Using empty list.")
                    row['session_event_ids'] = []
            return row # Returns dict or None if not found
    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database error getting task {event_id}: {e}", e)
        return None

def delete_task(event_id: str) -> bool:
    """Deletes a task by event_id."""
    fn_name = "delete_task"
    sql = "DELETE FROM users_tasks WHERE event_id = ?"
    try:
        with DB_LOCK:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (event_id,))
                conn.commit()
                if cursor.rowcount > 0:
                    log_info("activity_db", fn_name, f"Successfully deleted task {event_id}")
                    return True
                else:
                    log_warning("activity_db", fn_name, f"Task {event_id} not found for deletion.")
                    return False # Return False if not found
    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database error deleting task {event_id}: {e}", e)
        return False

def list_tasks_for_user(
    user_id: str,
    status_filter: list[str] | None = None, # Allow filtering by multiple statuses
    date_range: tuple[str, str] | None = None,
    project_filter: str | None = None
) -> list[dict]:
    """Lists tasks for a user, optionally filtering by status, date range, and project."""
    fn_name = "list_tasks_for_user"
    sql = "SELECT * FROM users_tasks WHERE user_id = ?"
    params = [user_id]
    conditions = []

    if status_filter:
        # Ensure status_filter is a list or tuple
        if isinstance(status_filter, str):
            status_filter = [status_filter]
        if isinstance(status_filter, (list, tuple)) and status_filter:
            placeholders = ','.join('?' * len(status_filter))
            conditions.append(f"status IN ({placeholders})")
            params.extend(status_filter)

    if date_range and len(date_range) == 2:
        conditions.append("date BETWEEN ? AND ?")
        params.extend(date_range)

    if project_filter:
        conditions.append("LOWER(project) = LOWER(?)") # Case-insensitive project filter
        params.append(project_filter)

    if conditions:
        sql += " AND " + " AND ".join(conditions)

    sql += " ORDER BY date, time" # Add default sorting

    results = []
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            conn.row_factory = _dict_factory
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            for row in rows:
                # Decode JSON field
                try:
                    row['session_event_ids'] = json.loads(row.get('session_event_ids', '[]') or '[]')
                except (json.JSONDecodeError, TypeError):
                    log_warning("activity_db", fn_name, f"Failed to decode session_event_ids JSON for task {row.get('event_id')}. Using empty list.")
                    row['session_event_ids'] = []
                results.append(row)
        return results
    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database error listing tasks for user {user_id}: {e}", e)
        return []

# --- Logging Table Functions ---

def log_system_event(level: str, module: str, function: str, message: str, traceback_str: str | None = None, user_id_context: str | None = None):
    """Logs errors and warnings to the system_logs table."""
    fn_name = "log_system_event"
    sql = """
    INSERT INTO system_logs (timestamp, level, module, function, message, traceback, user_id_context)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    params = (ts, level, module, function, message, traceback_str, user_id_context)
    try:
        with DB_LOCK:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
        # Avoid logging the log success itself to prevent loops
    except sqlite3.Error as e:
        # Fallback to print if DB logging fails
        print(f"CRITICAL DB LOG ERROR: {e} while logging: {params}")

def log_message_db(direction: str, user_id: str, content: str, raw_user_id: str | None = None, bridge_message_id: str | None = None):
    """Logs incoming/outgoing messages to the messages table."""
    fn_name = "log_message_db"
    sql = """
    INSERT INTO messages (timestamp, user_id, direction, content, raw_user_id, bridge_message_id)
    VALUES (?, ?, ?, ?, ?, ?)
    """
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    params = (ts, user_id, direction, content, raw_user_id, bridge_message_id)
    try:
        with DB_LOCK:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
    except sqlite3.Error as e:
        print(f"CRITICAL DB LOG ERROR: {e} while logging message: {params}")

def log_llm_activity_db(user_id: str, agent_type: str, activity_type: str, tool_name: str | None = None, tool_call_id: str | None = None, content_summary: str | None = None, details: dict | list | None = None):
    """Logs LLM interactions to the llm_activity table."""
    fn_name = "log_llm_activity_db"
    sql = """
    INSERT INTO llm_activity
    (timestamp, user_id, agent_type, activity_type, tool_name, tool_call_id, content_summary, details_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    details_str = None
    if isinstance(details, (dict, list)):
        try:
            details_str = json.dumps(details)
        except TypeError as json_err:
            log_warning("activity_db", fn_name, f"Could not serialize details to JSON for LLM activity: {json_err}. Details: {details}")
            details_str = json.dumps({"error": "Serialization failed", "original_type": str(type(details))})
    elif isinstance(details, str): # Allow passing pre-serialized JSON
        details_str = details

    params = (ts, user_id, agent_type, activity_type, tool_name, tool_call_id, content_summary, details_str)
    try:
        with DB_LOCK:
             with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                 cursor = conn.cursor()
                 cursor.execute(sql, params)
                 conn.commit()
    except sqlite3.Error as e:
        print(f"CRITICAL DB LOG ERROR: {e} while logging LLM activity: {params}")


# --- Initialize DB on module load ---
init_db()

# --- END OF tools/activity_db.py ---