### 📁 `tools/excel_handler.py`
import os
import pandas as pd
from datetime import datetime
from tools.logger import log_info, log_error

EXCEL_FOLDER = "data"
DEFAULT_SHEET = "Tasks"

COLUMNS = [
    "id", "type", "description", "duration", "date", "time",
    "status", "created_at", "updated_at"
]

def _get_file_path(user_id):
    return os.path.join(EXCEL_FOLDER, f"user_{user_id}_tasks.xlsx")

def init_user_excel_file(user_id: str):
    os.makedirs(EXCEL_FOLDER, exist_ok=True)
    path = _get_file_path(user_id)
    if not os.path.exists(path):
        df = pd.DataFrame(columns=COLUMNS)
        df.to_excel(path, sheet_name=DEFAULT_SHEET, index=False)

def add_task_or_reminder(user_id: str, record: dict):
    path = _get_file_path(user_id)
    # Initialize file if not exists
    if not os.path.exists(path):
        init_user_excel_file(user_id)
    df = pd.read_excel(path, sheet_name=DEFAULT_SHEET)
    df = pd.concat([df, pd.DataFrame([record])], ignore_index=True)
    df.to_excel(path, sheet_name=DEFAULT_SHEET, index=False)

def update_task_status(user_id: str, task_id: str, new_status: str):
    path = _get_file_path(user_id)
    df = pd.read_excel(path, sheet_name=DEFAULT_SHEET)
    current_status = df.loc[df['id'] == task_id, 'status'].values
    log_info("excel_handler", "update_task_status", f"Before update, task {task_id} status: {current_status}")
    df.loc[df['id'] == task_id, ['status', 'updated_at']] = [new_status.strip().lower(), datetime.now().isoformat()]
    df.to_excel(path, sheet_name=DEFAULT_SHEET, index=False)
    # Re-read and log to verify update.
    df_updated = pd.read_excel(path, sheet_name=DEFAULT_SHEET)
    updated_status = df_updated.loc[df_updated['id'] == task_id, 'status'].values
    log_info("excel_handler", "update_task_status", f"After update, task {task_id} status: {updated_status}")

def load_user_tasks(user_id: str):
    path = _get_file_path(user_id)
    df = pd.read_excel(path, sheet_name=DEFAULT_SHEET)
    return df.to_dict(orient="records")

def get_all_active_items(user_id: str):
    tasks = load_user_tasks(user_id)
    # Normalize status to compare properly.
    return [
        t for t in tasks
        if str(t.get("status", "")).lower().strip() not in ["cancelled", "completed"]
    ]

def task_exists(user_id: str, task_id: str):
    tasks = load_user_tasks(user_id)
    return any(t["id"] == task_id for t in tasks)