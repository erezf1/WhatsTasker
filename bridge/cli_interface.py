# bridge/cli_interface.py

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
import uuid
from threading import Lock, Thread # Import Thread
import time # Import time for sleep
from tools.logger import log_info, log_error
from bridge.request_router import handle_incoming_message, set_bridge
# Ensure calendar_tool provides the router correctly
try:
    from tools.calendar_tool import router as calendar_router
except ImportError:
    log_error("cli_interface", "import", "Could not import calendar_router from tools.calendar_tool")
    # Define a dummy router if import fails to allow server start
    from fastapi import APIRouter
    calendar_router = APIRouter()
# from datetime import datetime # Not used directly here

# Define a CLI Bridge
class CLIBridge:
    """Bridge that handles message queuing for CLI interaction."""
    def __init__(self, message_queue, lock):
        self.message_queue = message_queue
        self.lock = lock

    def send_message(self, user_id: str, message: str):
        """Adds the outgoing message to the queue instead of printing."""
        outgoing = {
            "user_id": user_id,
            "message": message,
            "message_id": str(uuid.uuid4())
        }
        with self.lock:
            self.message_queue.append(outgoing)
        log_info("CLIBridge", "send_message", f"Message for {user_id} queued (ID: {outgoing['message_id']}). Queue size: {len(self.message_queue)}")

# Global in-memory store for outgoing messages.
outgoing_messages = []
queue_lock = Lock()

# Set the global bridge in the router to use our CLI Bridge instance
# Pass the queue and lock to the bridge instance
set_bridge(CLIBridge(outgoing_messages, queue_lock))


def create_app() -> FastAPI:
    """Creates the FastAPI app."""
    app = FastAPI()
    app.include_router(calendar_router, prefix="")

    @app.post("/incoming")
    async def incoming_message(request: Request):
        """Receives message, processes it, queues response, returns ack."""
        try:
            data = await request.json()
            user_id = data.get("user_id")
            message = data.get("message")
            if not user_id or not message:
                return JSONResponse(content={"error": "Missing user_id or message"}, status_code=400)
            log_info("cli_interface", "incoming_message", f"Received message from user {user_id}: {message}")

            # Process the incoming message via the unified handler
            # handle_incoming_message now calls CLIBridge.send_message which queues the response
            handle_incoming_message(user_id, message)

            # --- REVERT HERE ---
            # Return only an acknowledgment. The actual message is queued by the bridge.
            return JSONResponse(content={"ack": True})
            # --- END REVERT ---

        except Exception as e:
            log_error("cli_interface", "incoming_message", "Error processing incoming message", e)
            return JSONResponse(content={"error": "Internal server error"}, status_code=500)

    @app.get("/outgoing")
    async def get_outgoing_messages():
        """Returns and clears the list of queued outgoing messages."""
        msgs_to_send = []
        with queue_lock:
            # Return all messages currently in the queue and clear it
            msgs_to_send = outgoing_messages[:] # Copy the list
            outgoing_messages.clear()          # Clear the original list
        if msgs_to_send:
            log_info("cli_interface", "get_outgoing_messages", f"Returning {len(msgs_to_send)} messages from queue.")
        return JSONResponse(content={"messages": msgs_to_send})

    @app.post("/ack")
    async def acknowledge_message(request: Request):
        """Receives acknowledgment (currently does nothing as queue is cleared on GET)."""
        # In a more robust system, GET /outgoing wouldn't clear the queue.
        # Messages would only be removed upon receiving an ACK for their specific message_id.
        # For this simple polling client, clearing on GET is sufficient.
        try:
            data = await request.json()
            message_id = data.get("message_id")
            if not message_id:
                return JSONResponse(content={"error": "Missing message_id"}, status_code=400)
            # Log but don't modify queue here, as GET already cleared it
            log_info("cli_interface", "acknowledge_message", f"Ack received for message {message_id} (queue already cleared by GET).")
            return JSONResponse(content={"ack": True})
        except Exception as e:
            log_error("cli_interface", "acknowledge_message", "Error processing ack", e)
            return JSONResponse(content={"error": "Internal server error"}, status_code=500)

    return app

app = create_app()

# Keep if running directly, but usually run via main.py
# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)