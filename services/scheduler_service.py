# --- START OF FILE services/scheduler_service.py ---

from typing import Dict, List, Any # Keep Any for job data
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
import pytz
from tools.logger import log_info, log_error, log_warning

# --- MODIFIED: Import request_router function ---
from bridge.request_router import handle_internal_system_event # NEWLY IMPORTED

scheduler = None
DEFAULT_TIMEZONE = 'UTC' # APScheduler's internal timezone for scheduling
NOTIFICATION_CHECK_INTERVAL_MINUTES = 60 # Check every hour
ROUTINE_CHECK_INTERVAL_MINUTES = 60    # Check every hour
DAILY_CLEANUP_HOUR_UTC = 0             # Midnight UTC for daily cleanup
DAILY_CLEANUP_MINUTE_UTC = 5           # A few minutes past midnight

def _job_listener(event):
    fn_name = "_job_listener_scheduler" # Unique name
    job = scheduler.get_job(event.job_id) if scheduler else None
    job_name = job.name if job else event.job_id
    if event.exception:
        log_error("scheduler_service", fn_name, f"Job '{job_name}' crashed:", event.exception)
        log_error("scheduler_service", fn_name, f"Traceback: {event.traceback}")
    # else:
    #     log_info("scheduler_service", fn_name, f"Job '{job_name}' executed successfully.") # Can be verbose

def _dispatch_routine_jobs():
    """
    Wrapper function called by scheduler.
    Gets structured routine job data from routine_service and dispatches it
    to the request_router as an internal system event.
    """
    fn_name = "_dispatch_routine_jobs"
    # log_info("scheduler_service", fn_name, "Scheduler executing routine job dispatch...") # Can be verbose
    try:
        from services.routine_service import check_routine_triggers # Import locally
        
        # This now returns List[Dict[str, Any]]
        # Each dict is like: {"user_id": "X", "routine_type": "morning_summary_data", "data_for_llm": {...}}
        routine_jobs_to_dispatch = check_routine_triggers() 

        if routine_jobs_to_dispatch:
            log_info("scheduler_service", fn_name, f"Routine check generated {len(routine_jobs_to_dispatch)} internal events to dispatch.")
            for job_data in routine_jobs_to_dispatch:
                user_id = job_data.get("user_id")
                routine_type = job_data.get("routine_type")
                if not user_id or not routine_type:
                    log_warning("scheduler_service", fn_name, f"Skipping invalid routine job data: {job_data}")
                    continue
                try:
                    # Call the new router function to handle this internal event
                    handle_internal_system_event(job_data)
                    log_info("scheduler_service", fn_name, f"Dispatched internal event '{routine_type}' for user {user_id} to router.")
                except Exception as dispatch_err:
                    log_error("scheduler_service", fn_name, f"Error dispatching internal event '{routine_type}' for user {user_id} via router", dispatch_err)
        # else:
            # log_info("scheduler_service", fn_name, "Routine check completed, no internal events to dispatch.") # Can be verbose

    except ImportError:
        log_error("scheduler_service", fn_name, "Failed to import routine_service.check_routine_triggers. Routine jobs skipped.")
    except Exception as job_err:
        log_error("scheduler_service", fn_name, "Error during scheduled routine job dispatch execution", job_err)


def start_scheduler() -> bool:
    global scheduler
    fn_name = "start_scheduler"

    if scheduler and scheduler.running:
        log_warning("scheduler_service", fn_name, "Scheduler is already running.")
        return True

    try:
        log_info("scheduler_service", fn_name, "Initializing APScheduler...")
        executors = {'default': ThreadPoolExecutor(10)} # Max 10 concurrent jobs
        job_defaults = {'coalesce': True, 'max_instances': 1} # Prevent job run overlap
        scheduler = BackgroundScheduler(
            executors=executors, job_defaults=job_defaults, timezone=pytz.timezone(DEFAULT_TIMEZONE)
        )

        # Import Job Functions
        check_event_notifications_func = None
        daily_cleanup_func = None
        try:
            from services.notification_service import check_event_notifications
            check_event_notifications_func = check_event_notifications
        except ImportError as e:
            log_error("scheduler_service", fn_name, f"Import 'check_event_notifications' failed: {e}. Notification job NOT scheduled.")
        try:
            from services.routine_service import daily_cleanup
            daily_cleanup_func = daily_cleanup
        except ImportError as e:
             log_error("scheduler_service", fn_name, f"Import 'daily_cleanup' failed: {e}. Cleanup job NOT scheduled.")
        
        jobs_scheduled_count = 0
        if check_event_notifications_func:
            scheduler.add_job(
                check_event_notifications_func, 
                trigger='interval', minutes=NOTIFICATION_CHECK_INTERVAL_MINUTES, 
                id='event_notification_check', name='Check Event Notifications'
            )
            jobs_scheduled_count += 1
        
        # Schedule the new routine dispatcher
        scheduler.add_job(
            _dispatch_routine_jobs, 
            trigger='interval', minutes=ROUTINE_CHECK_INTERVAL_MINUTES, 
            id='routine_job_dispatch', name='Dispatch Routine Jobs'
        )
        jobs_scheduled_count += 1

        if daily_cleanup_func:
            scheduler.add_job(
                daily_cleanup_func, 
                trigger='cron', hour=DAILY_CLEANUP_HOUR_UTC, minute=DAILY_CLEANUP_MINUTE_UTC, 
                id='daily_cleanup_job', name='Daily Cleanup'
            )
            jobs_scheduled_count += 1
        
        if jobs_scheduled_count == 0:
            log_warning("scheduler_service", fn_name, "No jobs were scheduled. Scheduler might not be useful.")

        scheduler.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        scheduler.start()
        log_info("scheduler_service", fn_name, f"APScheduler started successfully with {jobs_scheduled_count} job(s).")
        return True

    except Exception as e:
        log_error("scheduler_service", fn_name, f"Failed to initialize or start APScheduler: {e}", e)
        scheduler = None
        return False

def shutdown_scheduler():
    global scheduler
    fn_name = "shutdown_scheduler"
    if scheduler and scheduler.running:
        try:
            log_info("scheduler_service", fn_name, "Attempting to shut down scheduler...")
            scheduler.shutdown(wait=False) # Don't wait for jobs to complete
            log_info("scheduler_service", fn_name, "Scheduler shut down initiated.")
            scheduler = None
        except Exception as e:
            log_error("scheduler_service", fn_name, f"Error during scheduler shutdown: {e}", e)
    elif scheduler: # Exists but not running
        log_info("scheduler_service", fn_name, "Scheduler found but was not running.")
        scheduler = None
    else: # No instance
        log_info("scheduler_service", fn_name, "No active scheduler instance to shut down.")

# --- END OF FILE services/scheduler_service.py ---