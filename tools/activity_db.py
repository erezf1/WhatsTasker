# --- START OF FULL tools/activity_db.py ---

import sqlite3
import os
import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any, Tuple

try:
    from tools.logger import log_info, log_error, log_warning
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] [%(module)s:%(funcName)s] %(message)s')
    log_info = logging.info; log_error = logging.error; log_warning = logging.warning
    log_error("activity_db", "import", "Project logger not found, using basic logging.")

DATA_SUFFIX = os.getenv("DATA_SUFFIX", "")
DB_DIR = "data"
DB_FILE = os.path.join(DB_DIR, f"whatstasker_activity{DATA_SUFFIX}.db")
DB_LOCK = threading.Lock()

def init_db():
    fn_name = "init_db"
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor()

            # === users_tasks Table (No changes from your last correct version) ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users_tasks (
                event_id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('task', 'todo', 'reminder', 'external_event')),
                status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'completed', 'cancelled')),
                title TEXT NOT NULL, description TEXT, date TEXT, time TEXT,
                estimated_duration TEXT, sessions_planned INTEGER DEFAULT 0,
                sessions_completed INTEGER DEFAULT 0, progress_percent INTEGER DEFAULT 0,
                session_event_ids TEXT DEFAULT '[]', project TEXT, series_id TEXT,
                gcal_start_datetime TEXT, gcal_end_datetime TEXT, duration TEXT,
                created_at TEXT NOT NULL, completed_at TEXT, internal_reminder_sent TEXT,
                original_date TEXT, progress TEXT
            )
            """)
            # Indexes for users_tasks (ensure they cover common queries)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON users_tasks (user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id_status ON users_tasks (user_id, status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id_meta_date ON users_tasks (user_id, date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON users_tasks (project)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON users_tasks (status)")


            # === messages Table (REVISED SCHEMA for Richer Logging) ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,                  -- ISO 8601 UTC for when the event was logged by the system
                logged_at_iso TEXT NOT NULL,              -- Explicit column for system log time
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool', 'system_internal')),
                message_type TEXT NOT NULL,               -- e.g., 'user_text', 'agent_text_response',
                                                          -- 'agent_tool_call_request', 'tool_execution_result',
                                                          -- 'system_routine_payload', 'system_error_to_user'
                content_text TEXT,                        -- User's text, Agent's textual reply (can be NULL if assistant only makes tool_calls)
                                                          -- For tool role, this will be the JSON string of the tool's result.
                tool_calls_json TEXT,                     -- For role='assistant', message_type='agent_tool_call_request': JSON string of tool_calls array
                tool_name TEXT,                           -- For role='tool', message_type='tool_execution_result': Name of the tool
                associated_tool_call_id TEXT,             -- For role='tool': the tool_call_id this result corresponds to
                raw_user_id TEXT,                         -- Original user ID from bridge, if different
                bridge_message_id TEXT                    -- Message ID from the bridge, if any
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp)") # User's message timestamp
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_logged_at ON messages (logged_at_iso)") # System log timestamp
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id_role ON messages (user_id, role)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_message_type ON messages (message_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_assoc_tool_call_id ON messages (associated_tool_call_id)")


            # === llm_activity Table (Keeping for now as a dedicated log for specific tool interaction analysis if needed) ===
            # If the new 'messages' table proves sufficient, this could be deprecated later.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, user_id TEXT NOT NULL,
                agent_type TEXT NOT NULL, activity_type TEXT NOT NULL, tool_name TEXT,
                tool_call_id TEXT, content_summary TEXT, details_json TEXT
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_activity_timestamp ON llm_activity (timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_activity_user_id ON llm_activity (user_id)")

            # === system_logs Table (No changes from your last version) ===
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, level TEXT NOT NULL,
                module TEXT NOT NULL, function TEXT NOT NULL, message TEXT NOT NULL,
                traceback TEXT, user_id_context TEXT
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_timestamp ON system_logs (timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs (level)")

            conn.commit()
    except sqlite3.Error as e:
        log_error("activity_db", fn_name, f"Database initialization failed: {e}", e)
        raise

TASK_FIELDS = [
    "event_id", "user_id", "type", "status", "title", "description", "date", "time",
    "estimated_duration", "sessions_planned", "sessions_completed", "progress_percent",
    "session_event_ids", "project", "series_id", "gcal_start_datetime",
    "gcal_end_datetime", "duration", "created_at", "completed_at",
    "internal_reminder_sent", "original_date", "progress"
]

def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict:
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

def add_or_update_task(task_data: Dict) -> bool:
    fn_name = "add_or_update_task"
    event_id = task_data.get("event_id")
    if not event_id: log_error("activity_db", fn_name, "Missing 'event_id'."); return False
    db_params = []; columns_for_insert = []; placeholders_for_insert = []; updates_for_conflict = []
    for field in TASK_FIELDS:
        columns_for_insert.append(field); placeholders_for_insert.append("?")
        if field != 'event_id': updates_for_conflict.append(f'{field}=excluded.{field}')
        value = task_data.get(field)
        if field == "session_event_ids": value = json.dumps(value) if isinstance(value, (list, tuple)) else (value if isinstance(value, str) and value.startswith('[') else '[]')
        elif field in ["sessions_planned", "sessions_completed", "progress_percent"]:
            try: value = int(value) if value is not None else 0
            except (ValueError, TypeError): value = 0
        elif field in ["type", "status"]: value = str(value).lower() if value is not None else None
        if field == "created_at" and value is None: value = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
        if field in ["user_id", "type", "status", "title", "created_at"] and value is None:
            log_error("activity_db", fn_name, f"Required field '{field}' is missing for task {event_id}.")
            return False
        db_params.append(value)
    sql = f"INSERT INTO users_tasks ({', '.join(columns_for_insert)}) VALUES ({', '.join(placeholders_for_insert)}) ON CONFLICT(event_id) DO UPDATE SET {', '.join(updates_for_conflict)}"
    try:
        with DB_LOCK, sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor(); cursor.execute(sql, db_params); conn.commit()
        return True
    except sqlite3.Error as e: log_error("activity_db", fn_name, f"DB error saving task {event_id}: {e}", e); return False
    except Exception as e_unexp: log_error("activity_db", fn_name, f"Unexpected error task {event_id}: {e_unexp}", e_unexp); return False

def get_task(event_id: str) -> Dict | None:
    sql = f"SELECT * FROM users_tasks WHERE event_id = ?"
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            conn.row_factory = _dict_factory
            cursor = conn.cursor(); cursor.execute(sql, (event_id,)); row = cursor.fetchone()
            if row and 'session_event_ids' in row:
                try: row['session_event_ids'] = json.loads(row['session_event_ids']) if isinstance(row['session_event_ids'], str) else (row['session_event_ids'] or [])
                except: row['session_event_ids'] = []
            return row
    except sqlite3.Error as e: log_error("activity_db", "get_task", f"DB error get task {event_id}: {e}", e); return None

def delete_task(event_id: str) -> bool:
    sql = "DELETE FROM users_tasks WHERE event_id = ?"
    try:
        with DB_LOCK, sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor(); cursor.execute(sql, (event_id,)); conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e: log_error("activity_db", "delete_task", f"DB error delete task {event_id}: {e}", e); return False

def list_tasks_for_user(user_id: str, status_filter: List[str] | None = None, date_range: Tuple[str, str] | None = None, project_filter: str | None = None, type_filter: List[str] | None = None) -> List[Dict]:
    sql = "SELECT * FROM users_tasks WHERE user_id = ?"; params: List[Any] = [user_id]; conditions = []
    if status_filter:
        clean_statuses = [s.lower().strip() for s in status_filter if isinstance(s, str) and s.strip()]
        if clean_statuses: conditions.append(f"status IN ({','.join('?'*len(clean_statuses))})"); params.extend(clean_statuses)
    if type_filter:
        clean_types = [t.lower().strip() for t in type_filter if isinstance(t, str) and t.strip()]
        if clean_types: conditions.append(f"type IN ({','.join('?'*len(clean_types))})"); params.extend(clean_types)
    if date_range and len(date_range) == 2 and date_range[0] and date_range[1]:
        conditions.append("COALESCE(CASE WHEN gcal_start_datetime IS NOT NULL AND LENGTH(gcal_start_datetime) >= 10 THEN SUBSTR(gcal_start_datetime, 1, 10) END, date) BETWEEN ? AND ?")
        params.extend([date_range[0], date_range[1]])
    if project_filter and project_filter.strip(): conditions.append("LOWER(project) = LOWER(?)"); params.append(project_filter.strip())
    if conditions: sql += " AND " + " AND ".join(conditions)
    sql += " ORDER BY COALESCE(CASE WHEN gcal_start_datetime IS NOT NULL AND LENGTH(gcal_start_datetime) >= 10 THEN SUBSTR(gcal_start_datetime, 1, 10) END, date) ASC, COALESCE(CASE WHEN gcal_start_datetime IS NOT NULL AND INSTR(gcal_start_datetime, 'T') > 0 THEN SUBSTR(gcal_start_datetime, INSTR(gcal_start_datetime, 'T') + 1, 5) END, time) ASC, created_at ASC"
    results = []
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            conn.row_factory = _dict_factory; cursor = conn.cursor(); cursor.execute(sql, params)
            for row in cursor.fetchall():
                if 'session_event_ids' in row:
                    try: row['session_event_ids'] = json.loads(row['session_event_ids']) if isinstance(row['session_event_ids'], str) else (row['session_event_ids'] or [])
                    except: row['session_event_ids'] = []
                results.append(row)
        return results
    except sqlite3.Error as e: log_error("activity_db", "list_tasks_for_user", f"DB error list items for {user_id}: {e}", e); return []

def update_task_fields(event_id: str, updates: Dict[str, Any]) -> bool:
    if not event_id or not updates: return False
    allowed_fields = {f for f in TASK_FIELDS if f != 'event_id'}; update_cols_setters = []; update_params_values = []
    for key, value in updates.items():
        if key in allowed_fields:
            update_cols_setters.append(f"{key} = ?")
            if key == "session_event_ids": update_params_values.append(json.dumps(value) if isinstance(value, list) else (value if isinstance(value, str) else '[]'))
            elif key in ["type", "status"]: update_params_values.append(str(value).lower() if value is not None else None)
            else: update_params_values.append(value)
    if not update_cols_setters: return False
    update_params_values.append(event_id)
    sql = f"UPDATE users_tasks SET {', '.join(update_cols_setters)} WHERE event_id = ?"
    try:
        with DB_LOCK, sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor(); cursor.execute(sql, update_params_values); conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e: log_error("activity_db", "update_task_fields", f"DB error update task {event_id}: {e}", e); return False
    except Exception as e_unexp: log_error("activity_db", "update_task_fields", f"Unexpected error task {event_id}: {e_unexp}", e_unexp); return False


# --- REVISED: log_message_db for Richer Logging ---
def log_message_db(
    user_id: str,
    role: str, # 'user', 'assistant', 'tool', 'system_internal'
    message_type: str,
    # For 'user' role or 'assistant' simple text response: the text.
    # For 'tool' role (result): JSON string of the tool's full result dictionary.
    # Can be None for 'assistant' role if it only issues tool_calls.
    content_text: str | None = None,
    # For 'assistant' role, message_type='agent_tool_call_request': JSON string of tool_calls array.
    tool_calls_json: str | None = None,
    # For 'tool' role, message_type='tool_execution_result': Name of the tool.
    tool_name: str | None = None,
    # For 'tool' role: the tool_call_id this result corresponds to.
    associated_tool_call_id: str | None = None,
    # User's actual message timestamp (if available, e.g. from WhatsApp)
    # or the system's timestamp when user message was received by backend.
    user_message_timestamp_iso: str | None = None,
    raw_user_id: str | None = None,
    bridge_message_id: str | None = None
):
    fn_name = "log_message_db_rich"
    
    valid_roles = ('user', 'assistant', 'tool', 'system_internal')
    if role not in valid_roles:
        log_error("activity_db", fn_name, f"Invalid role '{role}' for DB log. User: {user_id}")
        return

    # System's timestamp for when this log event is recorded
    logged_at_iso_val = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    # Use user_message_timestamp_iso if provided for 'timestamp' column, else use logged_at
    timestamp_val = user_message_timestamp_iso if user_message_timestamp_iso else logged_at_iso_val

    sql = """INSERT INTO messages (timestamp, logged_at_iso, user_id, role, message_type, content_text,
                                 tool_calls_json, tool_name, associated_tool_call_id,
                                 raw_user_id, bridge_message_id)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    
    params = (
        timestamp_val, logged_at_iso_val, user_id, role, message_type, content_text,
        tool_calls_json, tool_name, associated_tool_call_id,
        raw_user_id, bridge_message_id
    )
    try:
        with DB_LOCK:
            with sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
    except sqlite3.Error as e:
        print(f"CRITICAL DB LOG ERROR (messages table): {e} | User: {user_id}, Role: {role}, Type: {message_type}, Content: {str(content_text)[:50]}")
        log_error("activity_db", fn_name, f"Database error logging rich message: {e}", e)
    except Exception as ex_unexp:
        print(f"CRITICAL UNEXPECTED DB LOG ERROR (messages table): {ex_unexp} | User: {user_id}, Role: {role}")
        log_error("activity_db", fn_name, f"Unexpected error logging rich message: {ex_unexp}", ex_unexp)


# --- System Logs (remains same) ---
def log_system_event(level: str, module: str, function: str, message: str, traceback_str: str | None = None, user_id_context: str | None = None, timestamp: str | None = None):
    sql = "INSERT INTO system_logs (timestamp, level, module, function, message, traceback, user_id_context) VALUES (?, ?, ?, ?, ?, ?, ?)"
    ts = timestamp or datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    params = (ts, level.upper(), module, function, message, traceback_str, user_id_context)
    try:
        with DB_LOCK, sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor(); cursor.execute(sql, params); conn.commit()
    except sqlite3.Error as e: print(f"CRITICAL DB LOG ERROR (system_logs): {e} | Msg: {message[:100]}")

# --- llm_activity Table & Logging Function (Keeping for now, review for deprecation later) ---
def log_llm_activity_db(user_id: str, agent_type: str, activity_type: str, tool_name: str | None = None, tool_call_id: str | None = None, content_summary: str | None = None, details: Dict | List | None = None):
    fn_name = "log_llm_activity_db"
    # Consider if this is still needed if the main 'messages' table captures enough detail.
    # log_info("activity_db", fn_name, f"Logging LLM activity: {activity_type} for {user_id}, tool: {tool_name or 'N/A'}")
    sql = "INSERT INTO llm_activity (timestamp, user_id, agent_type, activity_type, tool_name, tool_call_id, content_summary, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    ts = datetime.now(timezone.utc).isoformat(timespec='seconds')+'Z'
    details_str = None
    if isinstance(details, (dict, list)):
        try: details_str = json.dumps(details)
        except TypeError: details_str = json.dumps({"error": "Serialization failed", "original_type": str(type(details))})
    elif isinstance(details, str): details_str = details # Assume already JSON string
    
    params = (ts, user_id, agent_type, activity_type, tool_name, tool_call_id, content_summary, details_str)
    try:
        with DB_LOCK, sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10) as conn:
            cursor = conn.cursor(); cursor.execute(sql, params); conn.commit()
    except sqlite3.Error as e: print(f"CRITICAL DB LOG ERROR (llm_activity): {e} | Activity: {activity_type}, Tool: {tool_name}")

init_db()
# --- END OF FULL tools/activity_db.py ---