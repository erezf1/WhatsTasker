# --- START OF FULL bridge/twilio_interface.py ---

from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
from fastapi.responses import Response as FastAPIResponse # Use a more generic name to avoid conflict
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
import os
from typing import Dict, List, Any # Keep for type hints
import uuid
from threading import Lock

from tools.logger import log_info, log_error, log_warning
from bridge.request_router import handle_incoming_message, set_bridge # Assuming set_bridge can handle multiple

# --- Twilio Configuration ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # Your Twilio WhatsApp sender number e.g., "whatsapp:+14155238886"

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
    log_error("twilio_interface", "config", "Twilio credentials or WhatsApp number missing from environment. Twilio bridge will not function.")
    # Optionally, raise an error or prevent app creation if Twilio is the selected bridge type.

# Initialize Twilio client and validator if credentials are provided
twilio_client: TwilioClient | None = None
twilio_validator: RequestValidator | None = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
        log_info("twilio_interface", "init", "Twilio client and validator initialized.")
    except Exception as e_twilio_init:
        log_error("twilio_interface", "init", "Failed to initialize Twilio client/validator.", e_twilio_init)
        twilio_client = None
        twilio_validator = None

# Global in-memory store for Twilio outgoing messages (similar to other bridges)
outgoing_twilio_messages: List[Dict[str, Any]] = []
twilio_queue_lock = Lock()

class TwilioBridge:
    """Bridge for Twilio WhatsApp interactions."""
    def __init__(self, message_queue: List[Dict[str, Any]], lock: Lock, client: TwilioClient | None, twilio_sender_number: str | None):
        self.message_queue = message_queue # Not directly used by Twilio for sending, but kept for consistency
        self.lock = lock
        self.client = client
        self.twilio_sender_number = twilio_sender_number
        log_info("TwilioBridge", "__init__", "Twilio Bridge instance initialized.")

    def send_message(self, user_id: str, message_body: str):
        """
        Sends a message via Twilio to the user.
        user_id here is expected to be normalized by request_router.
        We need to format it back to Twilio's 'whatsapp:+<number>' format.
        """
        fn_name = "send_message_twilio"
        if not self.client or not self.twilio_sender_number:
            log_error("twilio_interface", fn_name, "Twilio client or sender number not configured. Cannot send message.")
            return

        if not user_id or not message_body:
            log_warning("twilio_interface", fn_name, f"Attempted to send empty message or invalid user_id via Twilio: {user_id}")
            return

        # Ensure user_id is in Twilio's format (e.g., whatsapp:+1234567890)
        # request_router should provide a normalized number. Add prefix back.
        # Assuming normalized_user_id is just the number part e.g. "1234567890"
        if not user_id.startswith("whatsapp:"):
            twilio_recipient_id = f"whatsapp:+{user_id}"
        else:
            twilio_recipient_id = user_id # Already in correct format

        try:
            log_info("twilio_interface", fn_name, f"Sending Twilio message from {self.twilio_sender_number} to {twilio_recipient_id}: '{message_body[:50]}...'")
            message_instance = self.client.messages.create(
                from_=self.twilio_sender_number,
                body=message_body,
                to=twilio_recipient_id
            )
            log_info("twilio_interface", fn_name, f"Twilio message sent. SID: {message_instance.sid}")
            # Note: Twilio doesn't use our internal message_id for ACKs in the same way wa_bridge.js does.
            # The ACK for Twilio is handled by the HTTP response to their webhook.
        except Exception as e:
            log_error("twilio_interface", fn_name, f"Error sending Twilio message to {twilio_recipient_id}", e)

# --- Helper for Background Task ---
async def process_incoming_twilio_message_background(user_id_from_bridge: str, message_body_from_bridge: str):
    fn_name = "process_incoming_twilio_message_background"
    try:
        # log_info("twilio_interface", fn_name, f"Twilio background task started for user {user_id_from_bridge}") # Verbose
        handle_incoming_message(user_id_from_bridge, message_body_from_bridge)
        # log_info("twilio_interface", fn_name, f"Twilio background task finished for user {user_id_from_bridge}") # Verbose
    except Exception as e:
        log_error("twilio_interface", fn_name, f"Unhandled exception in Twilio background message processing for {user_id_from_bridge}", e)


def create_twilio_app() -> FastAPI:
    """Creates the FastAPI app instance for the Twilio Interface."""
    app_instance = FastAPI(
        title="WhatsTasker Twilio Bridge API",
        description="Handles incoming WhatsApp messages from Twilio and integrates with the backend.",
        version="1.0.0"
    )

    # Include calendar routes if needed (same as other interfaces)
    try:
        from tools.calendar_tool import router as calendar_router
        app_instance.include_router(calendar_router, prefix="", tags=["Authentication"])
        log_info("twilio_interface", "create_twilio_app", "Calendar router included.")
    except ImportError:
        log_warning("twilio_interface", "create_twilio_app", "Calendar router not imported, OAuth callback might fail if Twilio bridge is primary.")


    @app_instance.post("/twilio/incoming", tags=["Twilio Bridge"])
    async def incoming_twilio_message(request: Request, background_tasks: BackgroundTasks, From: str = Form(...), Body: str = Form(...)):
        """
        Receives incoming WhatsApp messages from Twilio via webhook.
        Twilio sends data as application/x-www-form-urlencoded.
        """
        endpoint_name = "incoming_twilio_message"

        # Validate Twilio signature (optional but recommended for production)
        if twilio_validator:
            twilio_signature = request.headers.get("X-Twilio-Signature")
            # Construct full URL correctly, FastAPI request.url includes query params if any
            # For POST, Twilio usually doesn't append query params to the webhook URL itself
            form_params = await request.form() # Get form parameters
            
            # Convert ImmutableMultiDict to a regular dict for the validator
            # The validator expects a dictionary of the POST parameters.
            post_vars_dict = {key: value for key, value in form_params.items()}

            if not twilio_signature or not twilio_validator.validate(
                str(request.url), # Full URL as Twilio sees it
                post_vars_dict,    # The POST parameters
                twilio_signature
            ):
                log_warning("twilio_interface", endpoint_name, "Twilio signature validation FAILED. Rejecting request.")
                raise HTTPException(status_code=403, detail="Twilio signature validation failed.")
            # log_info("twilio_interface", endpoint_name, "Twilio signature validation successful.") # Verbose
        else:
            log_warning("twilio_interface", endpoint_name, "Twilio validator not initialized. Skipping signature validation (NOT RECOMMENDED FOR PRODUCTION).")

        user_id_from_twilio = From  # e.g., "whatsapp:+1234567890"
        message_body = Body

        if not user_id_from_twilio or message_body is None: # Body can be empty (e.g. media message with no caption)
            log_warning("twilio_interface", endpoint_name, f"Received invalid payload from Twilio. From: {user_id_from_twilio}, Body: {message_body}")
            # Twilio expects an empty TwiML response for success, or an error.
            # Just returning 400 might be enough, or an empty TwiML <Response/>
            return FastAPIResponse(content="<Response/>", media_type="application/xml", status_code=400)

        # Offload actual processing to a background task
        background_tasks.add_task(process_incoming_twilio_message_background, user_id_from_twilio, str(message_body))

        log_info("twilio_interface", endpoint_name, f"ACK for Twilio incoming from {user_id_from_twilio}. Processing in background. Msg: '{str(message_body)[:30]}...'")
        # Twilio expects an empty TwiML response to acknowledge receipt and stop retrying.
        return FastAPIResponse(content="<Response/>", media_type="application/xml")

    # No /outgoing or /ack needed for Twilio as send_message calls Twilio API directly
    # and Twilio's ACK mechanism is the HTTP 200 response to its webhook.

    return app_instance

# Create the FastAPI app instance for this interface
# main.py should import 'app' from here if Twilio mode is selected
app = create_twilio_app()

# --- END OF FULL bridge/twilio_interface.py ---