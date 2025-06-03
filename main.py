# --- START OF FULL main.py ---

import os
import sys
import asyncio
import signal
import argparse
from dotenv import load_dotenv
import threading
load_dotenv() # Load .env variables first

# --- Determine Bridge Type ---
DEFAULT_BRIDGE = "whatsapp" # Default if nothing else specified
ALLOWED_BRIDGES = ["cli", "whatsapp", "twilio"] # Added "twilio"
bridge_type_env = os.getenv("BRIDGE_TYPE", "").lower()

parser = argparse.ArgumentParser(description="Run WhatsTasker Backend")
parser.add_argument("--bridge", type=str, choices=ALLOWED_BRIDGES, help=f"Specify the bridge interface ({', '.join(ALLOWED_BRIDGES)})")
args = parser.parse_args()
bridge_type_arg = args.bridge.lower() if args.bridge else None

# Priority: Argument > Environment Variable > Default
bridge_type = DEFAULT_BRIDGE
if bridge_type_env in ALLOWED_BRIDGES:
    bridge_type = bridge_type_env
if bridge_type_arg: # Command-line arg takes highest precedence
    bridge_type = bridge_type_arg

if bridge_type not in ALLOWED_BRIDGES:
    print(f"FATAL ERROR: Invalid bridge type '{bridge_type}'. Allowed: {', '.join(ALLOWED_BRIDGES)}.")
    sys.exit(1)

# --- Logger Import (must happen early) ---
try:
    from tools.logger import log_info, log_error, log_warning
    log_info("main", "init", "Logger imported successfully.")
except ImportError as log_import_err:
    print(f"FATAL ERROR: Failed to import logger: {log_import_err}")
    sys.exit(1)
except Exception as log_init_e:
    print(f"FATAL ERROR during initial logging setup: {log_init_e}")
    sys.exit(1)


# --- Dynamic Bridge and App Import ---
uvicorn_app_path: str | None = None
bridge_module_name: str | None = None
bridge_instance: any = None # Keep Any for now

try:
    if bridge_type == "cli":
        from bridge.cli_interface import app as fastapi_app, CLIBridge, outgoing_cli_messages, cli_queue_lock
        uvicorn_app_path = "bridge.cli_interface:app"
        bridge_module_name = "CLI Bridge"
        bridge_instance = CLIBridge(outgoing_cli_messages, cli_queue_lock)
    elif bridge_type == "whatsapp":
        from bridge.whatsapp_interface import app as fastapi_app, WhatsAppBridge, outgoing_whatsapp_messages, whatsapp_queue_lock
        uvicorn_app_path = "bridge.whatsapp_interface:app"
        bridge_module_name = "WhatsApp (whatsapp-web.js) Bridge"
        bridge_instance = WhatsAppBridge(outgoing_whatsapp_messages, whatsapp_queue_lock)
    elif bridge_type == "twilio":
        from bridge.twilio_interface import app as fastapi_app, TwilioBridge # Ensure TwilioBridge uses its own queue if needed or direct send
        # Twilio specific config (could also be loaded directly in twilio_interface.py)
        TWILIO_ACCOUNT_SID_MAIN = os.getenv("TWILIO_ACCOUNT_SID")
        TWILIO_AUTH_TOKEN_MAIN = os.getenv("TWILIO_AUTH_TOKEN")
        TWILIO_WHATSAPP_NUMBER_MAIN = os.getenv("TWILIO_WHATSAPP_NUMBER")

        if not all([TWILIO_ACCOUNT_SID_MAIN, TWILIO_AUTH_TOKEN_MAIN, TWILIO_WHATSAPP_NUMBER_MAIN]):
            log_error("main", "init_twilio", "Twilio credentials/number not fully configured in .env. Twilio bridge may fail.")
            # Decide if this is a fatal error if twilio is selected
            # For now, it will try to initialize, and twilio_interface will log errors.

        # TwilioBridge initialization in twilio_interface.py handles client creation.
        # Here we just pass necessary components if its constructor needs them.
        # Assuming TwilioBridge constructor in twilio_interface.py handles client init:
        from twilio.rest import Client as TwilioSdkClient # Import for type hint, actual client in bridge
        
        _twilio_client_main: TwilioSdkClient | None = None
        if TWILIO_ACCOUNT_SID_MAIN and TWILIO_AUTH_TOKEN_MAIN:
            try:
                _twilio_client_main = TwilioSdkClient(TWILIO_ACCOUNT_SID_MAIN, TWILIO_AUTH_TOKEN_MAIN)
            except Exception as e_twilio_sdk:
                log_error("main", "init_twilio_sdk", f"Failed to initialize Twilio SDK client in main: {e_twilio_sdk}")
                _twilio_client_main = None # Ensure it's None on failure

        # The TwilioBridge in twilio_interface.py doesn't use a message queue in the same way
        # as wa_bridge.js. It sends directly. So, passing dummy queue/lock.
        # Or, its constructor could be adapted. For now, assume direct send logic.
        bridge_instance = TwilioBridge(
            message_queue=[], # Dummy, as TwilioBridge sends directly
            lock=threading.Lock(), # Dummy lock
            client=_twilio_client_main, # Pass the client initialized here
            twilio_sender_number=TWILIO_WHATSAPP_NUMBER_MAIN
        )
        uvicorn_app_path = "bridge.twilio_interface:app"
        bridge_module_name = "WhatsApp (Twilio) Bridge"
    else:
        raise ValueError(f"Internal logic error determining bridge type: {bridge_type}")

    from bridge.request_router import set_bridge # Must be imported after fastapi_app might be defined
    set_bridge(bridge_instance)
    log_info("main", "init", f"WhatsTasker v0.9 starting...")
    log_info("main", "init", f"Using Bridge Interface: {bridge_module_name} (Selected: '{bridge_type}', App Path: '{uvicorn_app_path}')")

except ImportError as import_err:
    log_error("main", "init", f"Failed to import bridge module for type '{bridge_type}': {import_err}", import_err)
    sys.exit(1)
except Exception as bridge_setup_err:
    log_error("main", "init", f"Failed during bridge setup for type '{bridge_type}': {bridge_setup_err}", bridge_setup_err)
    sys.exit(1)

# --- Other Imports ---
import uvicorn
import threading # For TwilioBridge dummy lock
from typing import Any # For bridge_instance type hint
from users.user_manager import init_all_agents
import traceback

# --- Scheduler Import ---
try:
    from services.scheduler_service import start_scheduler, shutdown_scheduler
    SCHEDULER_IMPORTED = True
    log_info("main", "import", "Scheduler service imported successfully.")
except ImportError as sched_import_err:
    log_error("main", "import", f"Scheduler service not found or failed import: {sched_import_err}. Background tasks disabled.", sched_import_err)
    SCHEDULER_IMPORTED = False
    def start_scheduler(): return False # type: ignore
    def shutdown_scheduler(): pass # type: ignore
# ----------------------------


async def handle_shutdown_signal(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
    log_warning("main", "handle_shutdown_signal", f"Received signal {sig.name}. Initiating shutdown...")
    if server:
         server.should_exit = True
    # await asyncio.sleep(1) # Optional delay


server: uvicorn.Server | None = None

async def main_async():
    global server
    log_info("main", "main_async", "Initializing agent states...")
    try:
        init_all_agents()
        log_info("main", "main_async", "Agent state initialization complete.")
    except Exception as init_e:
        log_error("main", "main_async", "CRITICAL error during init_all_agents.", init_e)
        sys.exit(1)

    if SCHEDULER_IMPORTED:
        try:
            log_info("main", "main_async", "Starting scheduler service...")
            if start_scheduler():
                log_info("main", "main_async", "Scheduler service started successfully.")
            else:
                log_error("main", "main_async", "Scheduler service FAILED to start.")
        except Exception as sched_e:
            log_error("main", "main_async", "CRITICAL error starting scheduler.", sched_e)

    reload_enabled = os.getenv("APP_ENV", "production").lower() == "development"
    log_level = "debug" if reload_enabled else "info"
    server_port = int(os.getenv("PORT", "8000"))
    
    log_info("main", "main_async", f"Starting FastAPI server via Uvicorn...")
    log_info("main", "main_async", f"Target App: '{uvicorn_app_path}'") # uvicorn_app_path is now guaranteed to be set
    log_info("main", "main_async", f"Host: 0.0.0.0, Port: {server_port}")
    log_info("main", "main_async", f"Reload: {reload_enabled}, Log Level: {log_level}")

    if uvicorn_app_path is None: # Should not happen due to checks above
        log_error("main", "main_async", "uvicorn_app_path is None. Cannot start server.")
        sys.exit(1)

    config = uvicorn.Config(
        uvicorn_app_path, host="0.0.0.0", port=server_port,
        reload=reload_enabled, access_log=False, log_level=log_level, lifespan="on"
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig_name, lambda s=sig_name: asyncio.create_task(handle_shutdown_signal(s, loop)))
        except NotImplementedError: # Windows
            signal.signal(sig_name, lambda s, f: asyncio.create_task(handle_shutdown_signal(signal.Signals(s), loop))) # type: ignore

    try:
        await server.serve()
    finally:
        log_info("main", "main_async", "Server stopped. Performing final cleanup...")
        if SCHEDULER_IMPORTED:
            try:
                log_info("main", "main_async", "Shutting down scheduler...")
                shutdown_scheduler()
                log_info("main", "main_async", "Scheduler shut down.")
            except Exception as e_sched_shutdown:
                log_error("main", "main_async", "Error shutting down scheduler.", e_sched_shutdown)
        log_info("main", "main_async", "Main async process finished.")


if __name__ == "__main__":
    try: _ = log_info # Check logger
    except NameError: print("FATAL: Logger not defined."); sys.exit(1)

    try:
        asyncio.run(main_async())
    except SystemExit as se:
         log_error("main", "__main__", f"Server exited with code: {se.code}. Check previous errors (e.g., port conflict).")
         sys.exit(se.code)
    except KeyboardInterrupt:
        log_warning("main", "__main__", "KeyboardInterrupt received. Exiting.")
        if SCHEDULER_IMPORTED:
             try: shutdown_scheduler()
             except Exception: pass
        sys.exit(0)
    except Exception as e_main_run:
        log_error("main", "__main__", f"Unhandled error during server execution/shutdown.", e_main_run)
        log_error("main", "__main__", f"Traceback:\n{traceback.format_exc()}")
        if SCHEDULER_IMPORTED:
            try: shutdown_scheduler()
            except Exception as final_e: log_error("main","__main__", f"Error in final scheduler shutdown attempt: {final_e}")
        sys.exit(1)
    finally:
        log_info("main", "__main__", "Application exiting.")

# --- END OF FULL main.py ---