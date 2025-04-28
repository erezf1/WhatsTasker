# --- START OF FULL bridge/cli_interface.py ---

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
import uuid
from threading import Lock, Thread
import time
import json # Added for error handling

# Use the central logger
from tools.logger import log_info, log_error, log_warning
# Import the central router and its setter function
from bridge.request_router import handle_incoming_message, set_bridge

# Ensure calendar_tool provides the router correctly (needed for OAuth)
try:
    from tools.calendar_tool import router as calendar_router
    CALENDAR_ROUTER_IMPORTED = True
    log_info("cli_interface", "import", "Successfully imported calendar_router.")
except ImportError:
    log_error("cli_interface", "import", "Could not import calendar_router from tools.calendar_tool. OAuth callback will fail if CLI mode used.")
    CALENDAR_ROUTER_IMPORTED = False
    from fastapi import APIRouter
    calendar_router = APIRouter()


# Define a CLI Bridge
# Global in-memory store for CLI outgoing messages.
outgoing_cli_messages = []
cli_queue_lock = Lock()

class CLIBridge:
    """Bridge that handles message queuing for CLI interaction."""
    def __init__(self, message_queue, lock):
        self.message_queue = message_queue
        self.lock = lock
        log_info("CLIBridge", "__init__", "CLI Bridge initialized for queuing.")

    # --- UPDATED send_message ---
    def send_message(self, user_id: str, message: str):
        """
        Adds the outgoing message to the CLI queue.
        Does NOT log the message content here (handled by request_router).
        """
        # user_id received here is the NORMALIZED ID from request_router
        if not user_id or not message:
             log_warning("CLIBridge", "send_message", f"Attempted to queue empty message or invalid user_id for CLI: {user_id}")
             return

        outgoing = {
            "user_id": user_id, # Use the normalized ID for CLI mock
            "message": message,
            "message_id": str(uuid.uuid4()) # Generate ID, might be used by mock sender ACK
        }
        with self.lock:
            self.message_queue.append(outgoing)
        # Log the queuing action
        log_info("CLIBridge", "send_message", f"Message for CLI user {user_id} queued (ID: {outgoing['message_id']}). Queue size: {len(self.message_queue)}")
    # --- END UPDATED send_message ---

# Set the global bridge in the router to use our CLI Bridge instance
# This should only be called by main.py if CLI mode is selected
# if __name__ != "__main__": # Crude check
#     set_bridge(CLIBridge(outgoing_cli_messages, cli_queue_lock))
#     log_info("cli_interface", "init", "CLI Bridge potentially set in request_router.")

def create_cli_app() -> FastAPI:
    """Creates the FastAPI app instance for the CLI Interface."""
    app = FastAPI(
        title="WhatsTasker CLI Bridge API",
        description="Handles interaction for the CLI mock sender.",
        version="1.0.0"
    )

    # Include calendar routes if needed
    if CALENDAR_ROUTER_IMPORTED:
        app.include_router(calendar_router, prefix="", tags=["Authentication"])
        log_info("cli_interface", "create_cli_app", "Calendar router included.")
    else:
         log_warning("cli_interface", "create_cli_app", "Calendar router not included.")


    # --- API Endpoints (Adjusted for CLI mock) ---
    @app.post("/incoming", tags=["CLI Bridge"])
    async def incoming_cli_message(request: Request):
        """Receives message from CLI mock, processes it, queues response, returns ack."""
        endpoint_name = "incoming_cli_message"
        try:
            data = await request.json()
            user_id = data.get("user_id") # Expecting normalized ID from mock sender
            message = data.get("message")
            if not user_id or message is None:
                log_warning("cli_interface", endpoint_name, f"Received invalid payload: {data}")
                raise HTTPException(status_code=400, detail="Missing user_id or message")

            log_info("cli_interface", endpoint_name, f"Received message via CLI bridge from user {user_id}: '{str(message)[:50]}...'")

            # Pass normalized ID to router, router handles DB logging
            handle_incoming_message(user_id, str(message))

            # Return only an acknowledgment.
            return JSONResponse(content={"ack": True})

        except json.JSONDecodeError:
            log_error("cli_interface", endpoint_name, "Received non-JSON payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        except HTTPException as http_exc:
            raise http_exc
        except Exception as e:
            log_error("cli_interface", endpoint_name, "Error processing incoming CLI message", e)
            raise HTTPException(status_code=500, detail="Internal server error processing message")

    @app.get("/outgoing", tags=["CLI Bridge"])
    async def get_outgoing_cli_messages():
        """Returns and clears the list of queued outgoing messages for the CLI mock."""
        # This endpoint *differs* from the WhatsApp one - it clears on GET
        endpoint_name = "get_outgoing_cli_messages"
        msgs_to_send = []
        with cli_queue_lock:
            # Return all messages currently in the queue and clear it
            msgs_to_send = outgoing_cli_messages[:] # Copy the list
            outgoing_cli_messages.clear()          # Clear the original list
        if msgs_to_send:
            log_info("cli_interface", endpoint_name, f"Returning {len(msgs_to_send)} messages from CLI queue (and clearing).")
        return JSONResponse(content={"messages": msgs_to_send})

    @app.post("/ack", tags=["CLI Bridge"])
    async def acknowledge_cli_message(request: Request):
        """Receives acknowledgment (currently does nothing for CLI as queue is cleared on GET)."""
        endpoint_name = "acknowledge_cli_message"
        try:
            data = await request.json()
            message_id = data.get("message_id")
            if not message_id:
                log_warning("cli_interface", endpoint_name, f"Received ACK without message_id: {data}")
                raise HTTPException(status_code=400, detail="Missing message_id")

            # Log but don't modify queue here, as GET already cleared it for CLI mock
            log_info("cli_interface", endpoint_name, f"CLI Ack received for message {message_id} (queue already cleared by GET).")
            return JSONResponse(content={"ack_received": True, "removed": False}) # Indicate not removed by ACK
        except json.JSONDecodeError:
            log_error("cli_interface", endpoint_name, "Received non-JSON ACK payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload for ACK")
        except HTTPException as http_exc:
            raise http_exc
        except Exception as e:
            log_error("cli_interface", endpoint_name, f"Error processing CLI ACK for message_id {data.get('message_id', 'N/A')}", e)
            raise HTTPException(status_code=500, detail="Internal server error processing ACK")

    return app

# Create the FastAPI app instance for this interface
# main.py should import 'app' from here if cli mode is selected
app = create_cli_app()

# --- END OF FULL bridge/cli_interface.py ---