# --- START OF FULL services/routine_service.py ---

import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Tuple
import pytz
import re

from tools.logger import log_info, log_error, log_warning
# from users.user_registry import get_registry, get_user_preferences # REMOVE get_user_preferences
from users.user_registry import get_registry # Keep get_registry
from services import sync_service
from services.config_manager import update_preferences # To update last trigger date

# --- MODIFIED IMPORT: Get preferences from agent_state_manager ---
from services.agent_state_manager import get_agent_state, clear_notified_event_ids # Added get_agent_state

ROUTINE_CONTEXT_HISTORY_DAYS = 1
ROUTINE_CONTEXT_FUTURE_DAYS = 14

def _get_local_time(user_timezone_str: str) -> datetime:
    fn_name = "_get_local_time_routine"
    if not user_timezone_str: user_timezone_str = 'UTC'
    try:
        user_tz = pytz.timezone(user_timezone_str)
        return datetime.now(user_tz)
    except pytz.UnknownTimeZoneError:
        log_warning("routine_service", fn_name, f"Unknown timezone '{user_timezone_str}'. Using UTC.")
        return datetime.now(pytz.utc)
    except Exception as e:
        log_error("routine_service", fn_name, f"Error getting local time for tz '{user_timezone_str}'", e)
        return datetime.now(pytz.utc)

def _get_item_local_date_str(item: Dict, user_tz: pytz.BaseTzInfo) -> str | None:
    start_dt_str = item.get("gcal_start_datetime")
    item_date_local_str = None
    if start_dt_str:
        try:
            if 'T' in start_dt_str: dt_aware = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00')); item_date_local_str = dt_aware.astimezone(user_tz).strftime("%Y-%m-%d")
            elif len(start_dt_str) == 10: item_date_local_str = start_dt_str
        except (ValueError, TypeError): pass
    elif item.get("date"): item_date_local_str = item.get("date")
    return item_date_local_str

def _sort_routine_items(items: List[Dict]) -> List[Dict]:
    def sort_key(item):
        eff_dt = datetime.max
        gcal_start = item.get("gcal_start_datetime")
        meta_date_str = item.get("date"); meta_time_str = item.get("time")
        if gcal_start and isinstance(gcal_start, str):
            try:
                if 'T' in gcal_start: eff_dt = datetime.fromisoformat(gcal_start.replace('Z', '+00:00')).replace(tzinfo=None)
                elif len(gcal_start) == 10: eff_dt = datetime.combine(datetime.strptime(gcal_start, '%Y-%m-%d').date(), datetime.min.time())
            except ValueError: pass
        elif meta_date_str:
            try:
                base_date = datetime.strptime(meta_date_str, "%Y-%m-%d")
                if meta_time_str:
                    time_part = meta_time_str + ':00' if len(meta_time_str.split(':')) == 2 else meta_time_str
                    eff_dt = datetime.strptime(f"{meta_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else: eff_dt = datetime.combine(base_date.date(), datetime.min.time())
            except (ValueError, TypeError): pass
        return eff_dt
    return sorted(items, key=sort_key)

def generate_morning_summary_data(user_id: str, context: List[Dict]) -> Dict[str, List[Dict]] | None:
    fn_name = "generate_morning_summary_data"
    
    # --- MODIFIED: Get prefs from agent_state ---
    agent_state = get_agent_state(user_id)
    if not agent_state:
        log_warning("routine_service", fn_name, f"Agent state not found for user {user_id}, cannot generate summary data.")
        return None
    prefs = agent_state.get("preferences")
    # --- END MODIFICATION ---

    if not prefs:
        log_warning("routine_service", fn_name, f"Preferences not found in agent state for user {user_id}, cannot generate summary data.")
        return None
        
    user_tz_str = prefs.get("TimeZone", "UTC")
    user_tz = pytz.timezone(user_tz_str) if user_tz_str else pytz.utc
    now_local = _get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    # log_info("routine_service", fn_name, f"Preparing morning summary data for user {user_id}") # Verbose

    events_and_reminders_today: List[Dict] = []
    sessions_today: List[Dict] = []
    todos_today: List[Dict] = []

    for item in context:
        item_local_date = _get_item_local_date_str(item, user_tz)
        item_type = item.get("type")
        item_status = item.get("status")

        if item_local_date == today_local_str:
            if item_type in ["task", "reminder", "todo"] and item_status in ["completed", "cancelled"]:
                continue

            clean_item = {
                "item_id": item.get("event_id") or item.get("item_id"), "type": item_type,
                "title": item.get("title"), "description": item.get("description"),
                "date": item.get("date"), "time": item.get("time"),
                "gcal_start_datetime": item.get("gcal_start_datetime"), "gcal_end_datetime": item.get("gcal_end_datetime"),
                "is_all_day": item.get('is_all_day', False) or (item.get('gcal_start_datetime') and 'T' not in item.get('gcal_start_datetime')),
                "status": item_status, "project": item.get("project"),
            }
            if item_type == "task":
                clean_item["estimated_duration"] = item.get("estimated_duration")
                match = re.match(r"Work: (.*?) \[\d+/\d+\]", clean_item["title"] or "")
                if match: clean_item["base_task_title"] = match.group(1).strip()

            if item_type == "reminder" or item_type == "external_event":
                events_and_reminders_today.append(clean_item)
            elif item_type == "task":
                if clean_item.get("gcal_start_datetime") and 'T' in clean_item["gcal_start_datetime"]:
                    sessions_today.append(clean_item)
            elif item_type == "todo":
                todos_today.append(clean_item)

    if not events_and_reminders_today and not sessions_today and not todos_today:
        # log_info("routine_service", fn_name, f"No items for morning summary for user {user_id}.") # Verbose
        return None

    return {
        "events_and_reminders_today": _sort_routine_items(events_and_reminders_today),
        "sessions_today": _sort_routine_items(sessions_today),
        "todos_today": sorted(todos_today, key=lambda x: (x.get("date") is None, x.get("title", "").lower()))
    }

def generate_evening_review_data(user_id: str, context: List[Dict]) -> Dict[str, List[Dict]] | None:
    fn_name = "generate_evening_review_data"

    # --- MODIFIED: Get prefs from agent_state ---
    agent_state = get_agent_state(user_id)
    if not agent_state:
        log_warning("routine_service", fn_name, f"Agent state not found for user {user_id}, cannot generate review data.")
        return None
    prefs = agent_state.get("preferences")
    # --- END MODIFICATION ---

    if not prefs:
        log_warning("routine_service", fn_name, f"Preferences not found in agent state for user {user_id}, cannot generate review data.")
        return None

    user_tz_str = prefs.get("TimeZone", "UTC")
    user_tz = pytz.timezone(user_tz_str) if user_tz_str else pytz.utc
    now_local = _get_local_time(user_tz_str)
    today_local_str = now_local.strftime("%Y-%m-%d")

    # log_info("routine_service", fn_name, f"Preparing evening review data for user {user_id}") # Verbose

    items_for_review_data: List[Dict] = []
    raw_items_today = []

    for item in context:
        item_local_date = _get_item_local_date_str(item, user_tz)
        if item_local_date == today_local_str:
            item_type = item.get("type"); item_status = item.get("status")
            clean_item = {
                "item_id": item.get("event_id") or item.get("item_id"), "type": item_type,
                "title": item.get("title"), "description": item.get("description"),
                "date": item.get("date"), "time": item.get("time"),
                "gcal_start_datetime": item.get("gcal_start_datetime"), "gcal_end_datetime": item.get("gcal_end_datetime"),
                "is_all_day": item.get('is_all_day', False) or (item.get('gcal_start_datetime') and 'T' not in item.get('gcal_start_datetime')),
                "status": item_status, "project": item.get("project"), "is_incomplete_task": False
            }
            if item_type == "task": clean_item["estimated_duration"] = item.get("estimated_duration")

            if item_type == "reminder" or item_type == "external_event":
                if item.get("status_gcal") != "cancelled": raw_items_today.append(clean_item)
            elif item_type == "task":
                if clean_item.get("gcal_start_datetime") and 'T' in clean_item["gcal_start_datetime"]:
                    if item_status in ["pending", "in_progress"]: clean_item["is_incomplete_task"] = True
                    raw_items_today.append(clean_item)
            elif item_type == "todo":
                if item_status in ["pending", "in_progress"]: raw_items_today.append(clean_item)
    
    if not raw_items_today:
        # log_info("routine_service", fn_name, f"No items for evening review for user {user_id}.") # Verbose
        return None

    items_for_review_data = _sort_routine_items(raw_items_today)
    return {"items_for_review": items_for_review_data}

def check_routine_triggers() -> List[Dict[str, Any]]:
    fn_name = "check_routine_triggers"
    routine_jobs: List[Dict[str, Any]] = []

    try:
        registry = get_registry()
        if not registry: return []
        user_ids = list(registry.keys())

        for user_id in user_ids:
            # --- MODIFIED: Get prefs from agent_state ---
            agent_state = get_agent_state(user_id)
            if not agent_state:
                # log_warning("routine_service", fn_name, f"Agent state not found for user {user_id} during trigger check.") # Verbose
                continue
            prefs = agent_state.get("preferences")
            # --- END MODIFICATION ---

            if not prefs or prefs.get("status") != "active": continue

            user_tz_str = prefs.get("TimeZone")
            if not user_tz_str: continue

            now_local = _get_local_time(user_tz_str)
            today_local_str = now_local.strftime("%Y-%m-%d")
            current_local_hm = now_local.strftime("%H:%M")
            
            aggregated_context = None; context_fetched_for_user = False
            def fetch_context_if_needed():
                nonlocal aggregated_context, context_fetched_for_user
                if not context_fetched_for_user:
                    context_start_date = (now_local - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
                    context_end_date = (now_local + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")
                    aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
                    context_fetched_for_user = True
                return aggregated_context

            morning_time_str = prefs.get("Morning_Summary_Time")
            if prefs.get("Enable_Morning") and morning_time_str:
                if current_local_hm >= morning_time_str and prefs.get("last_morning_trigger_date") != today_local_str:
                    # log_info("routine_service", fn_name, f"Identified Morning Summary trigger for {user_id}") # Verbose
                    current_context = fetch_context_if_needed()
                    summary_data = generate_morning_summary_data(user_id, current_context or [])
                    if summary_data:
                        routine_jobs.append({
                            "user_id": user_id, "routine_type": "morning_summary_data",
                            "data_for_llm": summary_data
                        })
                    update_preferences(user_id, {"last_morning_trigger_date": today_local_str})

            evening_time_str = prefs.get("Evening_Summary_Time")
            if prefs.get("Enable_Evening") and evening_time_str:
                if current_local_hm >= evening_time_str and prefs.get("last_evening_trigger_date") != today_local_str:
                    # log_info("routine_service", fn_name, f"Identified Evening Review trigger for {user_id}") # Verbose
                    current_context = fetch_context_if_needed()
                    review_data = generate_evening_review_data(user_id, current_context or [])
                    if review_data:
                        routine_jobs.append({
                            "user_id": user_id, "routine_type": "evening_review_data",
                            "data_for_llm": review_data
                        })
                    update_preferences(user_id, {"last_evening_trigger_date": today_local_str})
            
            if len(user_ids) > 20 and (prefs.get("Enable_Morning") or prefs.get("Enable_Evening")):
                import time; time.sleep(0.05)
    except Exception as main_err:
        log_error("routine_service", fn_name, f"General error during routine trigger check", main_err)
        traceback.print_exc()
    
    # if routine_jobs: log_info("routine_service", fn_name, f"Routine checks found {len(routine_jobs)} jobs to queue.") # Verbose
    return routine_jobs

def daily_cleanup():
    fn_name = "daily_cleanup"
    log_info("routine_service", fn_name, "Running daily cleanup job...")
    try:
        registry = get_registry()
        if not registry: return
        user_ids = list(registry.keys())
        
        cleared_count = 0
        for user_id in user_ids:
            try:
                if get_agent_state(user_id): # Check if agent state exists before trying to clear
                   clear_notified_event_ids(user_id)
                   cleared_count += 1
            except Exception as e:
                 log_error("routine_service", fn_name, f"Error during daily cleanup for user {user_id}", e, user_id=user_id)
        log_info("routine_service", fn_name, f"Daily cleanup finished. Cleared notification sets for {cleared_count} users.")
    except Exception as e:
        log_error("routine_service", fn_name, "Error during daily cleanup main loop", e)

# --- END OF FULL services/routine_service.py ---