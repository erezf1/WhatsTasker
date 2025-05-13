# --- START OF services/scheduler_service.py ---

from typing import Dict, List, Tuple # Added List, Tuple
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
import pytz
from tools.logger import log_info, log_error, log_warning
# --- ADD THIS IMPORT ---
from bridge.request_router import send_message
# ----------------------

# ... (Global scheduler instance, constants, _job_listener) ...
scheduler = None
DEFAULT_TIMEZONE = 'UTC'
NOTIFICATION_CHECK_INTERVAL_MINUTES = 60
ROUTINE_CHECK_INTERVAL_MINUTES = 60
DAILY_CLEANUP_HOUR_UTC = 0
DAILY_CLEANUP_MINUTE_UTC = 5

def _job_listener(event):
    # ... (function remains the same) ...
    fn_name = "_job_listener"
    job = scheduler.get_job(event.job_id) if scheduler else None
    job_name = job.name if job else event.job_id
    if event.exception:
        log_error("scheduler_service", fn_name, f"Job '{job_name}' crashed:", event.exception)
        log_error("scheduler_service", fn_name, f"Traceback: {event.traceback}")
    pass

# --- NEW FUNCTION TO WRAP ROUTINE CHECK AND SENDING ---
def _run_routine_check_and_send():
    """Wrapper function called by scheduler to run checks and send messages."""
    fn_name = "_run_routine_check_and_send"
    #log_info("scheduler_service", fn_name, "Scheduler executing routine check job...")
    try:
        # Import the check function here if not already imported globally
        from services.routine_service import check_routine_triggers
        messages_to_send = check_routine_triggers() # This now returns a list

        if messages_to_send:
            log_info("scheduler_service", fn_name, f"Routine check generated {len(messages_to_send)} messages to send.")
            for user_id, message_content in messages_to_send:
                try:
                    send_message(user_id, message_content)
                except Exception as send_err:
                    log_error("scheduler_service", fn_name, f"Error sending routine message to user {user_id}", send_err)
        else:
            log_info("scheduler_service", fn_name, "Routine check completed, no messages to send.")

    except Exception as job_err:
        # Log errors occurring within the job execution itself
        log_error("scheduler_service", fn_name, "Error during scheduled routine check execution", job_err)
# --- END OF NEW WRAPPER FUNCTION ---


def start_scheduler() -> bool:
    global scheduler
    fn_name = "start_scheduler"

    if scheduler and scheduler.running:
        log_warning("scheduler_service", fn_name, "Scheduler is already running.")
        return True

    try:
        log_info("scheduler_service", fn_name, "Initializing APScheduler...")
        executors = {'default': ThreadPoolExecutor(10)}
        job_defaults = {'coalesce': True, 'max_instances': 1}
        scheduler = BackgroundScheduler(
            executors=executors, job_defaults=job_defaults, timezone=pytz.timezone(DEFAULT_TIMEZONE)
        )

        # --- Import Job Functions ---
        check_event_notifications = None
        # We don't import check_routine_triggers here anymore, it's called in the wrapper
        daily_cleanup = None
        try:
            from services.notification_service import check_event_notifications
        except ImportError as e:
            log_error("scheduler_service", fn_name, f"Failed to import 'check_event_notifications': {e}. Notification job NOT scheduled.")
        try:
            # Keep import for daily_cleanup
            from services.routine_service import daily_cleanup
        except ImportError as e:
             log_error("scheduler_service", fn_name, f"Failed to import 'daily_cleanup': {e}. Cleanup job NOT scheduled.")
        # ----------------------------

        # --- Schedule Jobs ---
        jobs_scheduled_count = 0
        if check_event_notifications:
            scheduler.add_job( check_event_notifications, trigger='interval', minutes=NOTIFICATION_CHECK_INTERVAL_MINUTES, id='event_notification_check', name='Check Event Notifications')
            log_info("scheduler_service", fn_name, f"Scheduled 'check_event_notifications' job every {NOTIFICATION_CHECK_INTERVAL_MINUTES} minutes.")
            jobs_scheduled_count += 1
        else:
             log_warning("scheduler_service", fn_name, "'check_event_notifications' job not scheduled.")

        # --- MODIFIED: Schedule the WRAPPER function ---
        scheduler.add_job(
            _run_routine_check_and_send, # Call the wrapper
            trigger='interval',
            minutes=ROUTINE_CHECK_INTERVAL_MINUTES,
            id='routine_trigger_check',
            name='Check Routine Triggers & Send' # Updated name slightly
        )
        log_info("scheduler_service", fn_name, f"Scheduled 'Routine Trigger Check & Send' job every {ROUTINE_CHECK_INTERVAL_MINUTES} minutes.")
        jobs_scheduled_count += 1 # Assuming this job is always added
        # -------------------------------------------------

        if daily_cleanup:
            scheduler.add_job( daily_cleanup, trigger='cron', hour=DAILY_CLEANUP_HOUR_UTC, minute=DAILY_CLEANUP_MINUTE_UTC, timezone=DEFAULT_TIMEZONE, id='daily_cleanup_job', name='Daily Cleanup')
            log_info("scheduler_service", fn_name, f"Scheduled 'daily_cleanup' job daily at {DAILY_CLEANUP_HOUR_UTC:02d}:{DAILY_CLEANUP_MINUTE_UTC:02d} {DEFAULT_TIMEZONE}.")
            jobs_scheduled_count += 1
        else:
            log_warning("scheduler_service", fn_name, "'daily_cleanup' job not scheduled.")

        # ... (rest of the start_scheduler function including listener, start, return True/False) ...
        scheduler.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        scheduler.start()
        log_info("scheduler_service", fn_name, "APScheduler started successfully.")
        return True

    except Exception as e:
        log_error("scheduler_service", fn_name, f"Failed to initialize or start APScheduler: {e}", e)
        scheduler = None
        return False

# --- (shutdown_scheduler function remains the same) ---
def shutdown_scheduler():
    # ... (shutdown logic) ...
    global scheduler
    fn_name = "shutdown_scheduler"
    if scheduler and scheduler.running:
        try:
            log_info("scheduler_service", fn_name, "Attempting to shut down scheduler...")
            scheduler.shutdown(wait=False)
            log_info("scheduler_service", fn_name, "Scheduler shut down complete.")
            scheduler = None
        except Exception as e:
            log_error("scheduler_service", fn_name, f"Error during scheduler shutdown: {e}", e)
    elif scheduler:
        log_info("scheduler_service", fn_name, "Scheduler found but was not running.")
        scheduler = None
    else:
        log_info("scheduler_service", fn_name, "No active scheduler instance to shut down.")

# --- END OF services/scheduler_service.py ---