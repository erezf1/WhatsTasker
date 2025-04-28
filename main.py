# --- START OF FULL main.py ---

import os
import sys
import asyncio
import signal
import argparse
from dotenv import load_dotenv
load_dotenv()

# --- Determine Bridge Type ---
DEFAULT_BRIDGE = "whatsapp"
ALLOWED_BRIDGES = ["cli", "whatsapp"]
bridge_type_env = os.getenv("BRIDGE_TYPE", "").lower()
parser = argparse.ArgumentParser(description="Run WhatsTasker Backend")
parser.add_argument("--bridge", type=str, choices=ALLOWED_BRIDGES, help=f"Specify the bridge interface ({', '.join(ALLOWED_BRIDGES)})")
args = parser.parse_args()
bridge_type_arg = args.bridge.lower() if args.bridge else None
bridge_type = DEFAULT_BRIDGE
if bridge_type_arg: bridge_type = bridge_type_arg
if bridge_type_env in ALLOWED_BRIDGES: bridge_type = bridge_type_env
if bridge_type not in ALLOWED_BRIDGES: print(f"ERROR: Invalid bridge type '{bridge_type}'."); sys.exit(1)

# --- Logger Import (must happen early) ---
# Use a try-except block for robust logger initialization
try:
    from tools.logger import log_info, log_error, log_warning
    # Test log after import attempt
    log_info("main", "init", "Logger imported successfully.")
except ImportError as log_import_err:
    # Fallback if logger import fails catastrophically
    print(f"FATAL ERROR: Failed to import logger: {log_import_err}")
    sys.exit(1)
except Exception as log_init_e:
    print(f"FATAL ERROR during initial logging setup: {log_init_e}")
    sys.exit(1)


# --- Dynamic Bridge and App Import ---
uvicorn_app_path = None
bridge_module_name = None
bridge_instance = None

try:
    if bridge_type == "cli":
        from bridge.cli_interface import app as fastapi_app, CLIBridge, outgoing_cli_messages, cli_queue_lock
        uvicorn_app_path = "bridge.cli_interface:app"
        bridge_module_name = "CLI Bridge"
        bridge_instance = CLIBridge(outgoing_cli_messages, cli_queue_lock)
    elif bridge_type == "whatsapp":
        from bridge.whatsapp_interface import app as fastapi_app, WhatsAppBridge, outgoing_whatsapp_messages, whatsapp_queue_lock
        uvicorn_app_path = "bridge.whatsapp_interface:app"
        bridge_module_name = "WhatsApp Bridge"
        bridge_instance = WhatsAppBridge(outgoing_whatsapp_messages, whatsapp_queue_lock)
    else:
        # This should not happen due to initial checks, but keeps linters happy
        raise ValueError(f"Internal logic error determining bridge type: {bridge_type}")

    # --- Set the Bridge in the Router ---
    from bridge.request_router import set_bridge
    set_bridge(bridge_instance) # <-- EXPLICITLY SET THE BRIDGE HERE
    # ------------------------------------

    log_info("main", "init", f"WhatsTasker v0.8 starting...")
    log_info("main", "init", f"Using Bridge Interface: {bridge_module_name} (Selected: '{bridge_type}', Path: '{uvicorn_app_path}')")

except ImportError as import_err:
    log_error("main", "init", f"Failed to import bridge module for type '{bridge_type}': {import_err}", import_err)
    sys.exit(1)
except Exception as bridge_setup_err:
    log_error("main", "init", f"Failed during bridge setup for type '{bridge_type}': {bridge_setup_err}", bridge_setup_err)
    sys.exit(1)


# --- Other Imports ---
import uvicorn
from users.user_manager import init_all_agents
import traceback # Keep traceback import

# --- Scheduler Import ---
try:
    from services.scheduler_service import start_scheduler, shutdown_scheduler
    SCHEDULER_IMPORTED = True
    log_info("main", "import", "Scheduler service imported successfully.")
except ImportError as sched_import_err:
    log_error("main", "import", f"Scheduler service not found or failed import: {sched_import_err}. Background tasks disabled.", sched_import_err)
    SCHEDULER_IMPORTED = False
    # Define dummy functions to prevent crashes
    def start_scheduler(): return False
    def shutdown_scheduler(): pass
# ----------------------------


async def handle_shutdown_signal(sig, loop):
    """Async signal handler helper."""
    log_warning("main", "handle_shutdown_signal", f"Received signal {sig.name}. Initiating shutdown...")
    # Signal the main tasks to stop (implementation depends on how server/tasks are managed)
    # For Uvicorn, we can tell the server instance to exit
    if server: # Check if server object exists
         server.should_exit = True
    # Additionally, cancel other background tasks if necessary
    # Example: Cancel a long-running task
    # if some_background_task and not some_background_task.done():
    #    some_background_task.cancel()

    # Give tasks a moment to finish cleanup
    await asyncio.sleep(1)

    # Optionally force stop loop if tasks don't exit gracefully
    # loop.stop()


server: uvicorn.Server | None = None # Define server variable in outer scope

async def main_async():
    global server # Allow modification of the global server variable
    # Agent state init
    log_info("main", "main_async", "Initializing agent states...")
    try:
        init_all_agents()
        log_info("main", "main_async", "Agent state initialization complete.")
    except Exception as init_e:
        log_error("main", "main_async", "CRITICAL error during init_all_agents.", init_e)
        sys.exit(1) # Exit if agent init fails

    # Scheduler start
    if SCHEDULER_IMPORTED:
        try:
            log_info("main", "main_async", "Starting scheduler service...")
            if start_scheduler():
                log_info("main", "main_async", "Scheduler service started successfully.")
            else:
                # Error should be logged by start_scheduler if it returns False
                log_error("main", "main_async", "Scheduler service FAILED to start.")
        except Exception as sched_e:
            log_error("main", "main_async", "CRITICAL error starting scheduler.", sched_e)
            # Decide if this is fatal - potentially continue without scheduler?
            # For now, let's log and continue

    # Uvicorn config
    reload_enabled = os.getenv("APP_ENV", "production").lower() == "development"
    log_level = "debug" if reload_enabled else "info"
    # --- Define Port ---
    server_port = int(os.getenv("PORT", "8000")) # Read from env or default to 8000
    # -------------------
    log_info("main", "main_async", f"Starting FastAPI server via Uvicorn...")
    log_info("main", "main_async", f"Target App: '{uvicorn_app_path}'")
    log_info("main", "main_async", f"Host: 0.0.0.0, Port: {server_port}") # Log the port
    log_info("main", "main_async", f"Reload: {reload_enabled}, Log Level: {log_level}")

    config = uvicorn.Config(
        uvicorn_app_path,
        host="0.0.0.0",
        port=server_port, # Use the variable
        reload=reload_enabled,
        access_log=False, # Keep access log off unless needed for debugging
        log_level=log_level,
        lifespan="on" # Recommended for modern FastAPI startup/shutdown events
    )
    server = uvicorn.Server(config)

    # --- Graceful shutdown setup (Modern asyncio) ---
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            # Use loop.create_task for the handler to run it in the loop
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(handle_shutdown_signal(s, loop)))
        except NotImplementedError:
            # Fallback for systems like Windows that might not support add_signal_handler
            signal.signal(sig, lambda s, f: asyncio.create_task(handle_shutdown_signal(signal.Signals(s), loop)))
    # --- End shutdown setup ---

    try:
        await server.serve()
    finally:
        log_info("main", "main_async", "Server stopped. Performing final cleanup...")
        if SCHEDULER_IMPORTED:
            try:
                log_info("main", "main_async", "Shutting down scheduler...")
                shutdown_scheduler()
                log_info("main", "main_async", "Scheduler shut down.")
            except Exception as e:
                log_error("main", "main_async", "Error shutting down scheduler.", e)
        log_info("main", "main_async", "Main async process finished.")


if __name__ == "__main__":
    try:
        # Check logger exists before trying to use it
        _ = log_info
    except NameError:
        print("FATAL: Logger not defined or failed import.")
        sys.exit(1)

    try:
        asyncio.run(main_async())
    except SystemExit as se: # Catch SystemExit from Uvicorn startup failure
         log_error("main", "__main__", f"Server exited with code: {se.code}. Check previous errors (e.g., port conflict).")
         sys.exit(se.code) # Propagate the exit code
    except KeyboardInterrupt:
        log_warning("main", "__main__", "KeyboardInterrupt received. Exiting.")
        # Perform minimal cleanup if needed
        if SCHEDULER_IMPORTED:
             try: shutdown_scheduler()
             except Exception: pass
        sys.exit(0)
    except Exception as e:
        log_error("main", "__main__", f"Unhandled error during server execution/shutdown.", e)
        log_error("main", "__main__", f"Traceback:\n{traceback.format_exc()}")
        # Final attempt to shutdown scheduler
        if SCHEDULER_IMPORTED:
            try: shutdown_scheduler()
            except Exception as final_e: log_error("main","main", f"Error in final shutdown attempt: {final_e}")
        sys.exit(1)
    finally:
        log_info("main", "__main__", "Application exiting.")

# --- END OF FULL main.py ---