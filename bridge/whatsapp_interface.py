# --- START OF FULL bridge/whatsapp_interface.py ---

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks # <--- ADD BackgroundTasks
from fastapi.responses import JSONResponse
# import uvicorn # Keep for potential direct running/debugging, though main.py handles it
import uuid
from threading import Lock
import json
import re

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
    from fastapi import APIRouter
    calendar_router = APIRouter()

# --- Bridge Definition ---
outgoing_whatsapp_messages = []
whatsapp_queue_lock = Lock()

class WhatsAppBridge:
    def __init__(self, message_queue, lock):
        self.message_queue = message_queue
        self.lock = lock
        log_info("WhatsAppBridge", "__init__", "WhatsApp Bridge initialized for queuing.")

    def send_message(self, user_id: str, message: str):
        if not user_id or not message:
             log_warning("WhatsAppBridge", "send_message", f"Attempted to queue empty message or invalid user_id for WhatsApp: {user_id}")
             return

        formatted_user_id = user_id
        if re.match(r'^\d+$', user_id):
            formatted_user_id = f"{user_id}@c.us"
        elif '@' not in user_id:
             log_warning("WhatsAppBridge", "send_message", f"User ID '{user_id}' lacks '@' suffix and is not digits. Sending as is, may fail in whatsapp-web.js.")

        outgoing = {
            "user_id": formatted_user_id,
            "message": message,
            "message_id": str(uuid.uuid4())
        }
        with self.lock:
            self.message_queue.append(outgoing)
        log_info("WhatsAppBridge", "send_message", f"Message for WA user {formatted_user_id} queued (ID: {outgoing['message_id']}). Queue size: {len(self.message_queue)}")

# --- Helper for Background Task ---
async def process_incoming_message_background(user_id_from_bridge: str, message_body_from_bridge: str):
    """
    This function runs in the background, processing the message.
    It calls the main handler which might take time (LLM calls, etc.).
    Errors within handle_incoming_message should be logged by it or its sub-components.
    """
    fn_name = "process_incoming_message_background"
    try:
        log_info("whatsapp_interface", fn_name, f"Background task started for user {user_id_from_bridge}")
        # The actual processing logic
        # handle_incoming_message itself handles logging its own errors and sending responses
        handle_incoming_message(user_id_from_bridge, message_body_from_bridge)
        log_info("whatsapp_interface", fn_name, f"Background task finished for user {user_id_from_bridge}")
    except Exception as e:
        # Log any unexpected error during the background task execution itself
        # This is a catch-all; specific errors should be handled deeper in the call stack.
        log_error("whatsapp_interface", fn_name, f"Unhandled exception in background message processing for {user_id_from_bridge}", e, user_id=user_id_from_bridge)


def create_whatsapp_app() -> FastAPI:
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

    @app.post("/incoming", tags=["WhatsApp Bridge"])
    async def incoming_whatsapp_message(request: Request, background_tasks: BackgroundTasks): # <--- Inject BackgroundTasks
        endpoint_name = "incoming_whatsapp_message"
        try:
            data = await request.json()
            user_id = data.get("user_id")
            message_body = data.get("message")

            if not user_id or message_body is None:
                log_warning("whatsapp_interface", endpoint_name, f"Received invalid payload: {data}")
                raise HTTPException(status_code=400, detail="Missing user_id or message")

            # --- Offload the actual processing to a background task ---
            background_tasks.add_task(process_incoming_message_background, user_id, str(message_body))
            # ----------------------------------------------------------

            # Return immediate ACK
            log_info("whatsapp_interface", endpoint_name, f"ACK for incoming from {user_id}. Processing in background. Msg: '{str(message_body)[:30]}...'")
            return JSONResponse(content={"ack": True}, status_code=200)

        except json.JSONDecodeError:
            log_error("whatsapp_interface", endpoint_name, "Received non-JSON payload.")
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        except HTTPException as http_exc:
            # Re-raise HTTPExceptions directly
            raise http_exc
        except Exception as e:
            # Catch-all for unexpected errors during initial request handling (before background task)
            log_error("whatsapp_interface", endpoint_name, "Error processing incoming WhatsApp message (before background task)", e)
            raise HTTPException(status_code=500, detail="Internal server error processing message")

    @app.get("/outgoing", tags=["WhatsApp Bridge"])
    async def get_outgoing_whatsapp_messages():
        endpoint_name = "get_outgoing_whatsapp_messages"
        msgs_to_send = []
        with whatsapp_queue_lock:
            msgs_to_send = outgoing_whatsapp_messages[:]
        if msgs_to_send:
            # Reduce chattiness of this log unless debugging
            # log_info("whatsapp_interface", endpoint_name, f"Returning {len(msgs_to_send)} messages from WA queue (without clearing).")
            pass
        return JSONResponse(content={"messages": msgs_to_send})

    @app.post("/ack", tags=["WhatsApp Bridge"])
    async def acknowledge_whatsapp_message(request: Request):
        endpoint_name = "acknowledge_whatsapp_message"
        message_id = None
        try:
            data = await request.json()
            message_id = data.get("message_id")
            user_id_from_ack = data.get("user_id") # Get user_id from ACK for better logging

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
                    log_info("whatsapp_interface", endpoint_name, f"WA ACK received and message removed for ID: {message_id}. User: {user_id_from_ack or removed_msg.get('user_id')}. Queue size: {len(outgoing_whatsapp_messages)}")
                else:
                    log_warning("whatsapp_interface", endpoint_name, f"WA ACK for unknown/already removed message ID: {message_id}. User: {user_id_from_ack}")
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

app = create_whatsapp_app()

# --- END OF FULL bridge/whatsapp_interface.py ---