# --- START OF FULL bridge/whatsapp_interface.py ---

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn # Keep for potential direct running/debugging
import uuid
from threading import Lock
import json # Import json for error handling
import re # Import re for checking format

# Use the central logger
from tools.logger import log_info, log_error, log_warning
# Import the central router and its setter function
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

    # --- UPDATED send_message ---
    def send_message(self, user_id: str, message: str):
        """
        Adds the outgoing message with a unique ID to the shared WhatsApp queue.
        Ensures the user_id has the correct '@c.us' suffix for WhatsApp.
        Does NOT log the message content here (handled by request_router).
        """
        # user_id received here is the NORMALIZED ID from request_router
        if not user_id or not message:
             log_warning("WhatsAppBridge", "send_message", f"Attempted to queue empty message or invalid user_id for WhatsApp: {user_id}")
             return

        # --- Format User ID for WhatsApp Web JS ---
        formatted_user_id = user_id
        # Assume normalized ID is digits only. Add @c.us suffix.
        # (Add @g.us logic later if groups are supported)
        if re.match(r'^\d+$', user_id):
            formatted_user_id = f"{user_id}@c.us"
            # Log the formatting action, but not the content
            # log_info("WhatsAppBridge", "send_message", f"Formatted user_id {user_id} -> {formatted_user_id}")
        elif '@' not in user_id:
             # If it's not clearly a phone number and has no @, log a warning.
             log_warning("WhatsAppBridge", "send_message", f"User ID '{user_id}' lacks '@' suffix and is not digits. Sending as is, may fail in whatsapp-web.js.")
        # --- END User ID Formatting ---

        outgoing = {
            "user_id": formatted_user_id, # Use the WhatsApp-formatted ID
            "message": message,
            "message_id": str(uuid.uuid4()) # Unique ID for ACK tracking
        }
        with self.lock:
            self.message_queue.append(outgoing)
        # Log the queuing action itself, including the message ID for traceability
        log_info("WhatsAppBridge", "send_message", f"Message for WA user {formatted_user_id} queued (ID: {outgoing['message_id']}). Queue size: {len(self.message_queue)}")
    # --- END UPDATED send_message ---

# Set the global bridge in the request router to use our WhatsApp Bridge instance
# Pass the shared queue and lock to the bridge instance
# Ensure this happens only if the WhatsApp bridge is selected in main.py
# This initialization logic might need refinement depending on how main.py sets the bridge.
# Assuming main.py calls set_bridge after importing the correct module based on selection:
# if __name__ != "__main__": # Crude check to avoid running this if module imported by mistake?
#    set_bridge(WhatsAppBridge(outgoing_whatsapp_messages, whatsapp_queue_lock))
#    log_info("whatsapp_interface", "init", "WhatsApp Bridge potentially set in request_router.")
# A better approach is for main.py to explicitly call set_bridge after determining the mode.
# Let's assume main.py handles calling set_bridge(WhatsAppBridge(...))

def create_whatsapp_app() -> FastAPI:
    """Creates the FastAPI application instance for the WhatsApp Interface."""
    app = FastAPI(
        title="WhatsTasker WhatsApp Bridge API",
        description="Handles incoming messages from and outgoing messages to the WhatsApp Web JS bridge.",
        version="1.0.0"
    )

    if CALENDAR_ROUTER_IMPORTED:
        app.include_router(calendar_router, prefix="", tags=["Authentication"])
        log_info("whatsapp_interface", "create_app", "Calendar router included.")
    else:
         log_warning("whatsapp_interface", "create_app", "Calendar router not included.")

    # --- API Endpoints (Keep as they are) ---
    @app.post("/incoming", tags=["WhatsApp Bridge"])
    async def incoming_whatsapp_message(request: Request):
        endpoint_name = "incoming_whatsapp_message"
        try:
            data = await request.json()
            user_id = data.get("user_id")
            message_body = data.get("message")
            if not user_id or message_body is None:
                log_warning("whatsapp_interface", endpoint_name, f"Received invalid payload: {data}")
                raise HTTPException(status_code=400, detail="Missing user_id or message")
            # Pass raw ID to router, router handles normalization and DB logging
            handle_incoming_message(user_id, str(message_body))
            return JSONResponse(content={"ack": True}, status_code=200)
        except json.JSONDecodeError: # Renamed variable
            log_error("whatsapp_interface", endpoint_name, "Received non-JSON payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        except HTTPException as http_exc:
            raise http_exc
        except Exception as e:
            log_error("whatsapp_interface", endpoint_name, "Error processing incoming WhatsApp message", e)
            raise HTTPException(status_code=500, detail="Internal server error processing message")

    @app.get("/outgoing", tags=["WhatsApp Bridge"])
    async def get_outgoing_whatsapp_messages():
        endpoint_name = "get_outgoing_whatsapp_messages"
        msgs_to_send = []
        with whatsapp_queue_lock:
            msgs_to_send = outgoing_whatsapp_messages[:]
        if msgs_to_send:
            log_info("whatsapp_interface", endpoint_name, f"Returning {len(msgs_to_send)} messages from WA queue (without clearing).")
        return JSONResponse(content={"messages": msgs_to_send})

    @app.post("/ack", tags=["WhatsApp Bridge"])
    async def acknowledge_whatsapp_message(request: Request):
        endpoint_name = "acknowledge_whatsapp_message"
        message_id = None
        try:
            data = await request.json()
            message_id = data.get("message_id")
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
# main.py should import 'app' from here if whatsapp mode is selected
app = create_whatsapp_app()

# --- END OF FULL bridge/whatsapp_interface.py ---