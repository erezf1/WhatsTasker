# --- START OF FULL main.py ---

import os
import sys
import asyncio
import signal
import argparse # Added for command-line arguments
from dotenv import load_dotenv
load_dotenv() # Load environment variables early

# --- Determine Bridge Type ---
DEFAULT_BRIDGE = "whatsapp" # Default to CLI if nothing specified
ALLOWED_BRIDGES = ["cli", "whatsapp"]

# 1. Check Environment Variable
bridge_type_env = os.getenv("BRIDGE_TYPE", "").lower()

# 2. Check Command Line Argument
parser = argparse.ArgumentParser(description="Run WhatsTasker Backend")
parser.add_argument(
    "--bridge",
    type=str,
    choices=ALLOWED_BRIDGES,
    help=f"Specify the bridge interface to use ({', '.join(ALLOWED_BRIDGES)})"
)
args = parser.parse_args()
bridge_type_arg = args.bridge.lower() if args.bridge else None

# 3. Determine final bridge type (Env Var > Arg > Default)
bridge_type = DEFAULT_BRIDGE # Start with default
if bridge_type_arg:
    bridge_type = bridge_type_arg
if bridge_type_env in ALLOWED_BRIDGES:
    bridge_type = bridge_type_env # Environment variable takes precedence

if bridge_type not in ALLOWED_BRIDGES:
    print(f"ERROR: Invalid bridge type '{bridge_type}'. Must be one of: {', '.join(ALLOWED_BRIDGES)}")
    sys.exit(1)

# 4. Construct the app path string for Uvicorn
if bridge_type == "cli":
    uvicorn_app_path = "bridge.cli_interface:app"
    bridge_module_name = "CLI Bridge"
elif bridge_type == "whatsapp":
    uvicorn_app_path = "bridge.whatsapp_bridge:app"
    bridge_module_name = "WhatsApp Bridge"
else:
    # This case should be caught by the earlier check, but included for safety
    print(f"FATAL ERROR: Logic error determining bridge type. Selected: {bridge_type}")
    sys.exit(1)

# --- Now import other modules ---
# Logger needs to be available early, before other imports potentially log
try:
    from tools.logger import log_info, log_error, log_warning
    # Log the chosen bridge *after* logger is imported
    log_info("main", "init", f"WhatsTasker v0.8 starting...")
    log_info("main", "init", f"Using Bridge Interface: {bridge_module_name} (Selected: '{bridge_type}', Path: '{uvicorn_app_path}')")
except NameError:
     print("FATAL ERROR: Logger failed to initialize early.")
     sys.exit(1)
except Exception as log_init_e:
     print(f"FATAL ERROR during initial logging setup: {log_init_e}")
     sys.exit(1)

# Other imports (Uvicorn, user_manager, scheduler_service, etc.)
import uvicorn
from users.user_manager import init_all_agents
import traceback

# --- Import Scheduler Service ---
try:
    from services.scheduler_service import start_scheduler, shutdown_scheduler
    SCHEDULER_IMPORTED = True
except ImportError:
    log_error("main", "import", "Scheduler service not found. Background tasks disabled.")
    SCHEDULER_IMPORTED = False
    # Define dummy functions if import fails
    def start_scheduler(): return False # Return False on dummy start
    def shutdown_scheduler(): pass
# ----------------------------

def main():
    log_info("main", "main", "Initializing agent states...")
    try:
        init_all_agents()
        log_info("main", "main", "Agent state initialization complete.")
    except Exception as init_e:
        log_error("main", "main", "CRITICAL error during init_all_agents.", init_e)
        sys.exit(1) # Exit if agent init fails

    # --- Start Scheduler ---
    if SCHEDULER_IMPORTED:
        try:
            log_info("main", "main", "Starting scheduler service...")
            scheduler_started = start_scheduler() # Assumes returns True/False
            if scheduler_started:
                log_info("main", "main", "Scheduler service started successfully.")
            else:
                log_error("main", "main", "Scheduler service FAILED to start. Background tasks disabled.")
                # Decide if failure is critical
                # sys.exit(1)
        except Exception as sched_e:
            log_error("main", "main", "CRITICAL error starting scheduler service.", sched_e)
            # sys.exit(1) # Optional exit
    # -----------------------

    reload_enabled = os.getenv("APP_ENV", "production").lower() == "development"
    log_level = "debug" if reload_enabled else "info"

    log_info("main", "main", f"Starting FastAPI server via Uvicorn...")
    log_info("main", "main", f"Target App: '{uvicorn_app_path}'") # Log the target path
    log_info("main", "main", f"Reload: {reload_enabled}, Log Level: {log_level}")

    # --- Use the dynamically determined app path ---
    config = uvicorn.Config(
        uvicorn_app_path, # <--- Use the variable here
        host="0.0.0.0",
        port=8000,
        reload=reload_enabled,
        access_log=False, # Keep access log off for cleaner output
        log_level=log_level
    )
    server = uvicorn.Server(config)

    # --- Graceful shutdown handling (remains the same) ---
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def handle_signal(sig, frame):
        log_warning("main", "handle_signal", f"Received signal {sig}. Initiating shutdown...")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
             loop.add_signal_handler(sig, lambda s=sig: handle_signal(s, None))
        except NotImplementedError:
             # Fallback for environments where add_signal_handler isn't available
             signal.signal(sig, handle_signal)

    async def main_server_task():
        try:
            await server.serve()
        finally:
            if not stop_event.is_set():
                 stop_event.set()

    async def shutdown_manager():
         await stop_event.wait()
         log_info("main", "shutdown_manager", "Stop event received, initiating graceful shutdown...")

         # Shutdown Scheduler
         if SCHEDULER_IMPORTED:
             try:
                 log_info("main", "shutdown_manager", "Shutting down scheduler service...")
                 shutdown_scheduler()
                 log_info("main", "shutdown_manager", "Scheduler service shut down.")
             except Exception as sched_down_e:
                 log_error("main", "shutdown_manager", "Error shutting down scheduler.", sched_down_e)

         # Uvicorn handles its own server shutdown based on the signal
         log_info("main", "shutdown_manager", "Graceful shutdown process complete.")

    # --- Run server and shutdown manager (remains the same) ---
    try:
         async def run_all():
              server_task = asyncio.create_task(main_server_task())
              shutdown_task = asyncio.create_task(shutdown_manager())
              await asyncio.gather(server_task, shutdown_task)
         asyncio.run(run_all())

    except Exception as e:
         log_error("main", "main", f"Error during server execution or shutdown management.", e)
         log_error("main", "main", f"Runtime Exception Traceback:\n{traceback.format_exc()}")
         if SCHEDULER_IMPORTED:
             try: shutdown_scheduler()
             except Exception as final_sched_down: log_error("main","main", f"Error in final scheduler shutdown attempt: {final_sched_down}")
         sys.exit(1)
    finally:
         log_info("main", "main", "Main process finished.")


if __name__ == "__main__":
    # Logger should be initialized above after determining bridge type
    # Check if logger initialization failed
    try:
        _ = log_info # Check if function exists
    except NameError:
        print("FATAL: Logger name 'log_info' not defined. Exiting.")
        sys.exit(1)

    main()

# --- END OF FULL main.py ---