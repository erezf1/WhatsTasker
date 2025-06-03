# --- START OF FULL services/routine_service.py (Revised Evening Review Payload) ---

import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Tuple
import pytz
import re

from tools.logger import log_info, log_error, log_warning
from users.user_registry import get_registry
from services import sync_service
from services.config_manager import update_preferences

from services.agent_state_manager import get_agent_state, clear_notified_event_ids

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
            if 'T' in start_dt_str:
                dt_aware_utc = datetime.fromisoformat(start_dt_str.replace('Z', '+00:00'))
                if dt_aware_utc.tzinfo is None: dt_aware_utc = pytz.utc.localize(dt_aware_utc)
                item_date_local_str = dt_aware_utc.astimezone(user_tz).strftime("%Y-%m-%d")
            elif len(start_dt_str) == 10:
                item_date_local_str = start_dt_str
        except (ValueError, TypeError) as e:
            log_warning("routine_service", "_get_item_local_date_str", f"Error parsing gcal_start_datetime '{start_dt_str}': {e}")
            pass
    if item_date_local_str is None and item.get("date"):
        item_date_local_str = item.get("date")
    return item_date_local_str

def _format_time_info_for_payload(item: Dict, user_tz: pytz.BaseTzInfo) -> str:
    """Helper to format time information for the payload."""
    gcal_start_str = item.get("gcal_start_datetime")
    meta_time = item.get("time")
    is_all_day = item.get('is_all_day', False) or \
                 (gcal_start_str and 'T' not in gcal_start_str) or \
                 (item.get("date") and not meta_time and not gcal_start_str)


    if is_all_day:
        return "All-day"

    time_str = ""
    if gcal_start_str and 'T' in gcal_start_str:
        try:
            dt_aware_utc = datetime.fromisoformat(gcal_start_str.replace('Z', '+00:00'))
            if dt_aware_utc.tzinfo is None: dt_aware_utc = pytz.utc.localize(dt_aware_utc)
            dt_local_start = dt_aware_utc.astimezone(user_tz)
            time_str = dt_local_start.strftime('%H:%M')

            gcal_end_str = item.get("gcal_end_datetime")
            if gcal_end_str and 'T' in gcal_end_str:
                dt_end_aware_utc = datetime.fromisoformat(gcal_end_str.replace('Z', '+00:00'))
                if dt_end_aware_utc.tzinfo is None: dt_end_aware_utc = pytz.utc.localize(dt_end_aware_utc)
                dt_local_end = dt_end_aware_utc.astimezone(user_tz)
                if dt_local_end.date() == dt_local_start.date(): # Only show end time if on the same day
                    time_str += f" - {dt_local_end.strftime('%H:%M')}"
            return time_str
        except ValueError:
            pass # Fall through
    
    if meta_time:
        return meta_time
        
    return "No specific time"


def _sort_routine_items(items: List[Dict]) -> List[Dict]:
    def sort_key(item):
        eff_dt = datetime.max.replace(tzinfo=pytz.utc)
        item_date_str = item.get("date")
        item_time_str = item.get("time")
        gcal_start_str = item.get("gcal_start_datetime")
        if gcal_start_str:
            try:
                dt_obj = datetime.fromisoformat(gcal_start_str.replace('Z', '+00:00'))
                eff_dt = dt_obj
            except ValueError: pass
        elif item_date_str:
            try:
                base_date = datetime.strptime(item_date_str, "%Y-%m-%d")
                if item_time_str:
                    time_part = item_time_str + ':00' if len(item_time_str.split(':')) == 2 else item_time_str
                    dt_naive = datetime.strptime(f"{item_date_str} {time_part}", "%Y-%m-%d %H:%M:%S")
                else:
                    dt_naive = datetime.combine(base_date.date(), datetime.min.time())
                eff_dt = pytz.utc.localize(dt_naive) if dt_naive.tzinfo is None else dt_naive
            except (ValueError, TypeError): pass
        type_order = {"reminder": 0, "task_session": 1, "task_due": 2, "todo": 3, "external_event": 4}
        item_type = item.get("type", "todo")
        is_task_session = False
        if item_type == "task":
            if gcal_start_str and 'T' in gcal_start_str:
                 if item.get("title","").lower().startswith("work:") and ("session" in item.get("title","").lower() or re.match(r".*\[\d+/\d+\]", item.get("title",""))):
                    is_task_session = True
        item_type_for_sort = "task_session" if is_task_session else ("task_due" if item_type == "task" else item_type)
        item_type_val = type_order.get(item_type_for_sort, 3)
        return (eff_dt, item_type_val, item.get("title", "").lower())
    return sorted(items, key=sort_key)

def generate_morning_summary_data(user_id: str, context: List[Dict]) -> Dict[str, List[Dict]] | None:
    fn_name = "generate_morning_summary_data"
    agent_state = get_agent_state(user_id)
    if not agent_state:
        log_warning("routine_service", fn_name, f"Agent state not found for user {user_id}, cannot generate summary data.")
        return None
    prefs = agent_state.get("preferences")
    if not prefs:
        log_warning("routine_service", fn_name, f"Preferences not found for user {user_id}, cannot generate summary.")
        return None

    user_tz_str = prefs.get("TimeZone", "UTC")
    user_tz = pytz.timezone(user_tz_str) if user_tz_str else pytz.utc
    now_local = _get_local_time(user_tz_str)
    today_local_date = now_local.date()
    today_local_str = today_local_date.strftime("%Y-%m-%d")
    one_week_from_now_date = today_local_date + timedelta(days=7)

    events_and_reminders_today: List[Dict] = []
    sessions_today: List[Dict] = []
    todos_for_summary: List[Dict] = [] # Renamed for clarity

    for item in context:
        item_status = item.get("status")
        if item_status in ["completed", "cancelled"]:
            continue

        item_type = item.get("type")
        item_title = item.get("title", "Untitled")
        item_date_str_from_meta = item.get("date")
        effective_item_date_str = _get_item_local_date_str(item, user_tz)

        clean_item = {
            "item_id": item.get("event_id") or item.get("item_id"), "type": item_type,
            "title": item_title, "description": item.get("description"),
            "date": item_date_str_from_meta, "time": item.get("time"),
            "gcal_start_datetime": item.get("gcal_start_datetime"), 
            "gcal_end_datetime": item.get("gcal_end_datetime"),
            "is_all_day": item.get('is_all_day', False) or (item.get('gcal_start_datetime') and 'T' not in item.get('gcal_start_datetime','')),
            "status": item_status, "project": item.get("project"),
            "time_info": _format_time_info_for_payload(item, user_tz) # Added for morning too
        }

        if effective_item_date_str == today_local_str:
            if item_type == "task":
                clean_item["estimated_duration"] = item.get("estimated_duration")
                is_work_session = clean_item.get("title","").lower().startswith("work:") and \
                                  ("session" in clean_item.get("title","").lower() or re.match(r".*\[\d+/\d+\]", clean_item.get("title","")))
                
                if is_work_session and clean_item.get("gcal_start_datetime") and 'T' in clean_item.get("gcal_start_datetime",""):
                    match = re.match(r"Work: (.*?) (?:\[\d+/\d+\])?", clean_item["title"] or "", re.IGNORECASE)
                    if match: clean_item["base_task_title"] = match.group(1).strip()
                    elif clean_item.get("title","").lower().startswith("work: "):
                        clean_item["base_task_title"] = clean_item["title"][len("work: "):].strip()
                    else: clean_item["base_task_title"] = clean_item["title"] # Fallback
                    sessions_today.append(clean_item)
                else: 
                    events_and_reminders_today.append(clean_item) # Task due today, not a specific session
            elif item_type == "reminder" or item_type == "external_event":
                events_and_reminders_today.append(clean_item)
            # ToDos specifically dated for today will also be included in events_and_reminders_today if desired,
            # or handled by the upcoming_or_dateless_todos logic. Let's ensure they are distinct.
            elif item_type == "todo": # Add ToDos for today to general events/reminders list
                events_and_reminders_today.append(clean_item)


        if item_type == "todo":
            include_todo_in_upcoming = False
            if not item_date_str_from_meta: # Dateless ToDo
                include_todo_in_upcoming = True
            else: # ToDo has a date
                try:
                    todo_date_obj = datetime.strptime(item_date_str_from_meta, "%Y-%m-%d").date()
                    # Include if date is > today AND < today+7days
                    if today_local_date < todo_date_obj < one_week_from_now_date:
                        include_todo_in_upcoming = True
                except ValueError:
                    pass # Already logged if format is bad
            
            if include_todo_in_upcoming:
                # Ensure it's not also listed as "today's ToDo" if it happened to be dated today by mistake in this logic branch
                if effective_item_date_str != today_local_str:
                    todos_for_summary.append(clean_item)


    if not events_and_reminders_today and not sessions_today and not todos_for_summary:
        return { # Still return structure so LLM can say "all clear for morning"
            "message_key_for_llm": "morning_summary_all_clear",
            "events_and_reminders_today": [],
            "sessions_today": [],
            "upcoming_or_dateless_todos": []
        }

    return {
        "message_key_for_llm": "morning_summary_data_available",
        "events_and_reminders_today": _sort_routine_items(events_and_reminders_today),
        "sessions_today": _sort_routine_items(sessions_today),
        "upcoming_or_dateless_todos": sorted(todos_for_summary, key=lambda x: (x.get("date") is None, x.get("date", ""), x.get("title", "").lower()))
    }

# --- REVISED generate_evening_review_data ---
def generate_evening_review_data(user_id: str, context: List[Dict]) -> Dict[str, Any] | None:
    fn_name = "generate_evening_review_data_payload_v2"
    agent_state = get_agent_state(user_id)
    if not agent_state:
        log_warning("routine_service", fn_name, f"Agent state not found for user {user_id}, cannot generate review data.")
        return None
    prefs = agent_state.get("preferences")
    if not prefs:
        log_warning("routine_service", fn_name, f"Preferences not found for user {user_id}, cannot generate review.")
        return None

    user_tz_str = prefs.get("TimeZone", "UTC")
    user_tz = pytz.timezone(user_tz_str) if user_tz_str else pytz.utc
    now_local = _get_local_time(user_tz_str)
    today_local_date = now_local.date()
    today_local_str = today_local_date.strftime("%Y-%m-%d")
    tomorrow_local_date = today_local_date + timedelta(days=1)
    tomorrow_local_str = tomorrow_local_date.strftime("%Y-%m-%d")
    # For "upcoming ToDos" look from tomorrow up to 7 days from *tomorrow*
    one_week_from_tomorrow_date = tomorrow_local_date + timedelta(days=7)


    items_for_today_review_list: List[Dict] = []
    items_for_tomorrow_preview_list: List[Dict] = []
    relevant_todos_list: List[Dict] = []

    for item in context:
        item_status = item.get("status")
        item_type = item.get("type")
        item_title = item.get("title", "Untitled")
        item_date_str_from_meta = item.get("date") # This is the 'date' field from the item's own DB record
        
        # Determine the item's effective local date using GCal or metadata date
        effective_item_date_str = _get_item_local_date_str(item, user_tz)

        # Prepare a clean representation of the item for the payload
        clean_item = {
            "item_id": item.get("event_id") or item.get("item_id"),
            "type": item_type,
            "title": item_title,
            # "description": item.get("description"), # Optional: LLM might not need full desc for summary
            "status": item_status,
            "project": item.get("project"),
            "time_info": _format_time_info_for_payload(item, user_tz), # Formatted time string
            "due_date_info": item_date_str_from_meta if item_date_str_from_meta else "No due date" # For ToDos primarily
        }

        if item_type == "task":
            clean_item["estimated_duration"] = item.get("estimated_duration")
            is_gcal_work_session = item.get("title","").lower().startswith("work:") and \
                                   ("session" in item.get("title","").lower() or re.match(r".*\[\d+/\d+\]", item.get("title",""))) and \
                                   item.get("gcal_start_datetime") and 'T' in item.get("gcal_start_datetime","")
            if is_gcal_work_session and item_status in ["pending", "in_progress"] and effective_item_date_str == today_local_str:
                clean_item["is_incomplete_task_session"] = True


        # 1. Items for Today's Review (active or completed today)
        if effective_item_date_str == today_local_str:
            if item_status != "cancelled": # Show active and completed_today items
                 if item_type == "external_event" and item.get("status_gcal", "confirmed") == "cancelled":
                     pass # Don't include cancelled GCal external events
                 else:
                    items_for_today_review_list.append(clean_item)

        # 2. Items for Tomorrow's Preview (active ones)
        elif effective_item_date_str == tomorrow_local_str:
            if item_status in ["pending", "in_progress"]:
                items_for_tomorrow_preview_list.append(clean_item)

        # 3. Relevant ToDos (dateless or due tomorrow through next week)
        if item_type == "todo" and item_status in ["pending", "in_progress"]:
            include_todo = False
            if not item_date_str_from_meta: # Dateless
                include_todo = True
            else: # Dated ToDo
                try:
                    todo_date_obj = datetime.strptime(item_date_str_from_meta, "%Y-%m-%d").date()
                    # If due from tomorrow up to < 1 week from tomorrow
                    if tomorrow_local_date <= todo_date_obj < one_week_from_tomorrow_date:
                        include_todo = True
                except ValueError: pass
            
            if include_todo:
                # Avoid adding if it's already in tomorrow's preview (if it happened to be dated for tomorrow)
                is_already_in_tomorrow_preview = any(
                    td["item_id"] == clean_item["item_id"] for td in items_for_tomorrow_preview_list if td["type"] == "todo"
                )
                if not is_already_in_tomorrow_preview:
                    relevant_todos_list.append(clean_item)

    # --- Construct the final payload ---
    evening_review_sections = []
    all_empty = True

    if items_for_today_review_list:
        all_empty = False
        evening_review_sections.append({
            "section_type": "today_summary_and_checkup",
            "section_title_suggestion_key": "evening_review_today_title", # Key for LLM to get title like "Today's Review"
            "items": _sort_routine_items(items_for_today_review_list),
            # LLM should be prompted to ask about completion for items not 'completed'
            "user_prompt_suggestion_key": "evening_review_ask_today_completion_status_prompt"
        })
    
    if items_for_tomorrow_preview_list:
        all_empty = False
        evening_review_sections.append({
            "section_type": "tomorrow_preview",
            "section_title_suggestion_key": "evening_review_tomorrow_title",
            "items": _sort_routine_items(items_for_tomorrow_preview_list),
            "user_prompt_suggestion_key": None # It's informational
        })

    if relevant_todos_list:
        all_empty = False
        evening_review_sections.append({
            "section_type": "todo_scheduling_suggestions",
            "section_title_suggestion_key": "evening_review_todos_title",
            "items": sorted(relevant_todos_list, key=lambda x: (x.get("date") is None, x.get("due_date_info", ""), x.get("title", "").lower())),
            "user_prompt_suggestion_key": "evening_review_ask_schedule_todos_prompt"
        })

    if all_empty:
        return { # Payload indicating nothing to review or preview
            "message_key_for_llm": "evening_review_all_clear_v2", # New key
            "evening_review_sections": []
        }

    return {
        "message_key_for_llm": "evening_review_data_available_v2", # New key
        "evening_review_sections": evening_review_sections,
        "overall_closing_suggestion_key": "evening_review_generic_closing_prompt"
    }


def check_routine_triggers() -> List[Dict[str, Any]]:
    fn_name = "check_routine_triggers"
    routine_jobs: List[Dict[str, Any]] = []
    try:
        registry = get_registry()
        if not registry: return []
        user_ids = list(registry.keys())

        for user_id in user_ids:
            agent_state = get_agent_state(user_id)
            if not agent_state:
                continue
            prefs = agent_state.get("preferences")
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
                    context_start_date = (now_local.date() - timedelta(days=ROUTINE_CONTEXT_HISTORY_DAYS)).strftime("%Y-%m-%d")
                    context_end_date = (now_local.date() + timedelta(days=ROUTINE_CONTEXT_FUTURE_DAYS)).strftime("%Y-%m-%d")
                    aggregated_context = sync_service.get_synced_context_snapshot(user_id, context_start_date, context_end_date)
                    context_fetched_for_user = True
                return aggregated_context

            morning_time_str = prefs.get("Morning_Summary_Time")
            if prefs.get("Enable_Morning") and morning_time_str:
                if current_local_hm >= morning_time_str and prefs.get("last_morning_trigger_date") != today_local_str:
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
                    current_context = fetch_context_if_needed()
                    review_data = generate_evening_review_data(user_id, current_context or [])
                    if review_data : 
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
                if get_agent_state(user_id):
                   clear_notified_event_ids(user_id)
                   update_preferences(user_id, {
                       "last_morning_trigger_date": "",
                       "last_evening_trigger_date": ""
                   })
                   cleared_count += 1
            except Exception as e:
                 log_error("routine_service", fn_name, f"Error during daily cleanup for user {user_id}", e, user_id=user_id)
        log_info("routine_service", fn_name, f"Daily cleanup finished. Processed {cleared_count} users.")
    except Exception as e:
        log_error("routine_service", fn_name, "Error during daily cleanup main loop", e)

# --- END OF FULL services/routine_service.py (Revised Evening Review Payload) ---