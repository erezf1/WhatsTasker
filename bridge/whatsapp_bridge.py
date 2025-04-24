# --- START OF FULL bridge/whatsapp_interface.py ---

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn # Keep for potential direct running/debugging
import uuid
from threading import Lock
import json # Import json for error handling
import re # Import re for checking format

from tools.logger import log_info, log_error, log_warning
from bridge.request_router import handle_incoming_message, set_bridge

# Try importing the calendar router (necessary for OAuth callback)
try:
    from tools.calendar_tool import router as calendar_router
    CALENDAR_ROUTER_IMPORTED = True
    log_info("whatsapp_interface", "import", "Successfully imported calendar_router.")
except ImportError:
    log_error("whatsapp_interface", "import", "Could not import calendar_router from tools.calendar_tool. OAuth callback will fail.")
    CALENDAR_ROUTER_IMPORTED = False
    # Define a dummy router if import fails to allow server start
    from fastapi import APIRouter
    calendar_router = APIRouter()

# --- Bridge Definition ---
# Global in-memory store for outgoing messages destined for WhatsApp and its lock.
outgoing_whatsapp_messages = []
whatsapp_queue_lock = Lock()

class WhatsAppBridge:
    """Bridge that handles message queuing for the Node.js WhatsApp Poller."""
    def __init__(self, message_queue, lock):
        self.message_queue = message_queue
        self.lock = lock
        log_info("WhatsAppBridge", "__init__", "WhatsApp Bridge initialized for queuing.")

    def send_message(self, user_id: str, message: str):
        """
        Adds the outgoing message with a unique ID to the shared WhatsApp queue.
        Ensures the user_id has the correct '@c.us' suffix.
        """
        if not user_id or not message:
             log_warning("WhatsAppBridge", "send_message", f"Attempted to queue empty message or invalid user_id for WhatsApp: {user_id}")
             return

        # --- ADD User ID Formatting ---
        formatted_user_id = user_id
        # Basic check: if it's likely a phone number and doesn't have @c.us or @g.us
        if re.match(r'^\d+$', user_id.split('@')[0]) and '@' not in user_id:
            formatted_user_id = f"{user_id}@c.us"
            log_info("WhatsAppBridge", "send_message", f"Appended @c.us to user_id {user_id} -> {formatted_user_id}")
        elif '@' not in user_id:
             # If it's not clearly a phone number and has no @, log a warning but use it as is? Or error?
             # For now, log a warning. It might be a group ID missing @g.us, which will likely fail later.
             log_warning("WhatsAppBridge", "send_message", f"User ID '{user_id}' lacks '@' suffix. Sending as is, may fail.")
        # --- END User ID Formatting ---


        outgoing = {
            "user_id": formatted_user_id, # Use the potentially formatted ID
            "message": message,
            "message_id": str(uuid.uuid4()) # Unique ID for ACK tracking
        }
        with self.lock:
            self.message_queue.append(outgoing)
        log_info("WhatsAppBridge", "send_message", f"Message for WhatsApp user {formatted_user_id} queued (ID: {outgoing['message_id']}). Queue size: {len(self.message_queue)}")

# Set the global bridge in the request router to use our WhatsApp Bridge instance
# Pass the shared queue and lock to the bridge instance
set_bridge(WhatsAppBridge(outgoing_whatsapp_messages, whatsapp_queue_lock))
log_info("whatsapp_interface", "init", "WhatsApp Bridge set in request_router.")
# --- End Bridge Definition ---


def create_whatsapp_app() -> FastAPI:
    """Creates the FastAPI application instance for the WhatsApp Interface."""
    app = FastAPI(
        title="WhatsTasker WhatsApp Bridge API",
        description="Handles incoming messages from and outgoing messages to the WhatsApp Web JS bridge.",
        version="1.0.0" # Example version
    )

    # Include calendar routes if successfully imported (needed for OAuth callback)
    if CALENDAR_ROUTER_IMPORTED:
        app.include_router(calendar_router, prefix="", tags=["Authentication"])
        log_info("whatsapp_interface", "create_app", "Calendar router included.")
    else:
         log_warning("whatsapp_interface", "create_app", "Calendar router not included due to import failure.")

    # --- API Endpoints ---

    @app.post("/incoming", tags=["WhatsApp Bridge"])
    async def incoming_whatsapp_message(request: Request):
        """
        Receives incoming messages forwarded from the Node.js WhatsApp bridge.
        Processes the message asynchronously via the request_router and returns an immediate ACK.
        """
        endpoint_name = "incoming_whatsapp_message"
        try:
            data = await request.json()
            user_id = data.get("user_id") # Expected format: number@c.us from WA
            message_body = data.get("message") # Key is 'message' in JS script

            if not user_id or message_body is None: # Allow empty messages
                log_warning("whatsapp_interface", endpoint_name, f"Received invalid payload (missing user_id or message): {data}")
                raise HTTPException(status_code=400, detail="Missing user_id or message")

            log_info("whatsapp_interface", endpoint_name, f"Received message via bridge from WA user {user_id}: '{str(message_body)[:50]}...'")

            # Pass the raw user_id (e.g., '972...@c.us') and message to the central router
            # The router should ideally normalize this ID (remove @c.us) for internal use,
            # and the bridge should add it back when sending.
            handle_incoming_message(user_id, str(message_body)) # Ensure message is string

            # Return only an acknowledgment, as expected by the Node.js bridge.
            return JSONResponse(content={"ack": True}, status_code=200)

        except json.JSONDecodeError:
            log_error("whatsapp_interface", endpoint_name, "Received non-JSON payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        except HTTPException as http_exc:
            # Re-raise validation errors etc.
            raise http_exc
        except Exception as e:
            log_error("whatsapp_interface", endpoint_name, "Error processing incoming WhatsApp message", e)
            raise HTTPException(status_code=500, detail="Internal server error processing message")

    @app.get("/outgoing", tags=["WhatsApp Bridge"])
    async def get_outgoing_whatsapp_messages():
        """
        Returns a list of currently queued outgoing messages destined for WhatsApp.
        **Does NOT remove** messages from the queue. Removal happens via /ack.
        """
        endpoint_name = "get_outgoing_whatsapp_messages"
        msgs_to_send = []
        with whatsapp_queue_lock:
            # Return a *copy* of all messages currently in the queue
            msgs_to_send = outgoing_whatsapp_messages[:]
            # DO NOT CLEAR the queue here
        if msgs_to_send:
            log_info("whatsapp_interface", endpoint_name, f"Returning {len(msgs_to_send)} messages from WA queue (without clearing).")
        # else: # Reduce log noise for empty queue
        #    log_info("whatsapp_interface", endpoint_name, "Outgoing WA queue is empty.")
        return JSONResponse(content={"messages": msgs_to_send})

    @app.post("/ack", tags=["WhatsApp Bridge"])
    async def acknowledge_whatsapp_message(request: Request):
        """
        Receives acknowledgment from the Node.js WhatsApp bridge that a message
        with a specific message_id has been successfully sent to the user via WhatsApp.
        Removes the acknowledged message from the outgoing queue.
        """
        endpoint_name = "acknowledge_whatsapp_message"
        message_id = None # Initialize for logging in case of error
        try:
            data = await request.json()
            message_id = data.get("message_id")
            # user_id = data.get("user_id") # Could be useful for logging context

            if not message_id:
                log_warning("whatsapp_interface", endpoint_name, f"Received ACK without message_id: {data}")
                raise HTTPException(status_code=400, detail="Missing message_id in ACK payload")

            removed = False
            with whatsapp_queue_lock:
                index_to_remove = -1
                for i, msg in enumerate(outgoing_whatsapp_messages):
                    if msg.get("message_id") == message_id:
                        index_to_remove = i
                        break

                if index_to_remove != -1:
                    removed_msg = outgoing_whatsapp_messages.pop(index_to_remove)
                    removed = True
                    log_info("whatsapp_interface", endpoint_name, f"WA ACK received and message removed for ID: {message_id}. User: {removed_msg.get('user_id')}. Queue size: {len(outgoing_whatsapp_messages)}")
                else:
                    log_warning("whatsapp_interface", endpoint_name, f"WA ACK received for unknown/already removed message ID: {message_id}")
                    # Still return success to the bridge.

            return JSONResponse(content={"ack_received": True, "removed": removed})

        except json.JSONDecodeError:
            log_error("whatsapp_interface", endpoint_name, "Received non-JSON ACK payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload for ACK")
        except HTTPException as http_exc:
            raise http_exc
        except Exception as e:
            log_error("whatsapp_interface", endpoint_name, f"Error processing WA ACK for message_id {message_id or 'N/A'}", e)
            raise HTTPException(status_code=500, detail="Internal server error processing ACK")

    return app

# Create the FastAPI app instance for this interface
app = create_whatsapp_app()

# --- Direct Run / Debugging ---
# if __name__ == "__main__":
#    log_info("whatsapp_interface", "__main__", "Starting FastAPI server directly for WhatsApp Interface debugging...")
#    print("INFO: Run this interface using 'python main.py' after updating main.py to use 'bridge.whatsapp_interface:app'")

# --- END OF bridge/whatsapp_interface.py ---