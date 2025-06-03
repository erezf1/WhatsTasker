# --- START OF FULL tools/calendar_tool.py (Corrected SyntaxError) ---
# tools/calendar_tool.py
import os
import requests 
import json     
import yaml     
from fastapi import APIRouter, Request 
from fastapi.responses import HTMLResponse 
from tools.logger import log_info, log_error, log_warning
import jwt 
import requests.compat 

from tools.token_store import save_user_token_encrypted, get_user_token 
from services.agent_state_manager import update_agent_state_key, get_agent_state 
from tools.google_calendar_api import GoogleCalendarAPI 
from bridge.request_router import send_message as send_chat_message 
from typing import Dict

log_info("calendar_tool", "module_load", "Starting module load.")

router = APIRouter()

_calendar_tool_messages: Dict = {}

def _load_messages_from_yaml():
    global _calendar_tool_messages 
    fn_name = "_load_messages_from_yaml_calendar_tool"
    try:
        _messages_path = os.path.join("config", "messages.yaml")
        if os.path.exists(_messages_path):
            with open(_messages_path, 'r', encoding="utf-8") as f_msg:
                _yaml_content = f_msg.read()
                if _yaml_content.strip():
                    f_msg.seek(0)
                    loaded_messages = yaml.safe_load(f_msg)
                    if isinstance(loaded_messages, dict):
                        _calendar_tool_messages = loaded_messages
                        log_info("calendar_tool", fn_name, "Successfully loaded messages from messages.yaml.")
                        return 
                    else:
                        log_warning("calendar_tool", fn_name, "messages.yaml content is not a dictionary.")
                else:
                    log_warning("calendar_tool", fn_name, "messages.yaml is empty.")
        else:
            log_warning("calendar_tool", fn_name, f"messages.yaml not found at {_messages_path}.")
    except Exception as e_msg_load:
        log_error("calendar_tool", fn_name, f"Failed to load messages.yaml: {e_msg_load}", e_msg_load)
    
    _calendar_tool_messages = {} 
    log_warning("calendar_tool", fn_name, "Using empty messages for calendar_tool due to loading issue.")

_load_messages_from_yaml()

def _get_message(message_key: str, user_lang: str = "en", **kwargs) -> str:
    default_lang = "en"
    message_obj = _calendar_tool_messages.get(message_key, {}) 
    
    if isinstance(message_obj, dict):
        message_template = message_obj.get(user_lang, message_obj.get(default_lang, f"MsgKeyNotFound: {message_key}"))
    elif isinstance(message_obj, str):
        message_template = message_obj
    else:
        message_template = f"MsgKeyNotFoundOrInvalid: {message_key} (Type: {type(message_obj)})"
        log_warning("calendar_tool", "_get_message", f"Message object for key '{message_key}' is not dict or str.")

    try:
        return message_template.format(**kwargs) if kwargs else message_template
    except KeyError as e_format:
        log_warning("calendar_tool", "_get_message", f"Missing key '{e_format}' for formatting msg_key '{message_key}'")
        return message_template
    except Exception as e_general_format:
        log_error("calendar_tool", "_get_message", f"General error formatting msg_key '{message_key}': {e_general_format}")
        return message_template

def _get_html_response_page(page_title_key: str, message_body_key: str, user_lang: str = "en", **kwargs) -> str:
    page_title = _get_message(page_title_key, user_lang) 
    body_content = _get_message(message_body_key, user_lang, **kwargs)
    return f"""
    <html>
    <head><title>{page_title}</title></head>
    <body>
        <h1>{page_title}</h1>
        <p>{body_content}</p>
    </body>
    </html>
    """

GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
REDIRECT_URI_BASE_ENV = os.getenv("GOOGLE_REDIRECT_URI")
SCOPE = "https://www.googleapis.com/auth/calendar.events"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL_BASE = "https://accounts.google.com/o/oauth2/auth"

if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
    log_error("calendar_tool", "config_check", "CRITICAL: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET env var not set.")


def authenticate(user_id: str, prefs: dict) -> dict:
    from services.config_manager import set_gcal_integration_status 
    fn_name = "authenticate"
    log_info("calendar_tool", fn_name, f"Checking auth status for user {user_id}")
    
    actual_redirect_uri = REDIRECT_URI_BASE_ENV
    if not actual_redirect_uri:
        log_error("calendar_tool", fn_name, "GOOGLE_REDIRECT_URI not configured for this instance.")
        return {"status": "fails", "message": "Server configuration error (redirect URI missing)."}

    token_data = get_user_token(user_id)
    calendar_enabled = prefs.get("Calendar_Enabled", False)
    gcal_status = prefs.get("gcal_integration_status", "not_integrated")

    if token_data is not None and calendar_enabled and gcal_status == "connected":
        return {"status": "token_exists", "message": "Calendar already connected."}
    
    log_info("calendar_tool", fn_name, f"Proceeding to generate auth URL for {user_id}. GCal status: {gcal_status}, Redirect: {actual_redirect_uri}")

    if not CLIENT_ID:
        log_error("calendar_tool", fn_name, f"Client ID missing.")
        return {"status": "fails", "message": "Server configuration error."}

    params = {
        "client_id": CLIENT_ID, "redirect_uri": actual_redirect_uri, "scope": SCOPE,
        "response_type": "code", "access_type": "offline", "state": user_id, "prompt": "consent"
    }
    try:
        encoded_params = requests.compat.urlencode(params)
        auth_url = f"{AUTH_URL_BASE}?{encoded_params}"
        log_info("calendar_tool", fn_name, f"Generated auth URL for {user_id}")
        set_gcal_integration_status(user_id, "pending_auth") 
        return {"status": "pending", "message": f"Please authenticate by visiting: {auth_url}"}
    except Exception as url_e:
        log_error("calendar_tool", fn_name, f"Failed to build auth URL for {user_id}", url_e)
        set_gcal_integration_status(user_id, "error")
        return {"status": "fails", "message": "Failed to generate authentication URL."}


@router.get("/oauth2callback", response_class=HTMLResponse)
async def oauth2callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    from services.config_manager import set_gcal_integration_status, update_preferences
    fn_name = "oauth2callback"
    user_id_from_state = state
    user_lang = "en" 

    if user_id_from_state:
        agent_s = get_agent_state(user_id_from_state)
        if agent_s and agent_s.get("preferences"):
            user_lang = agent_s.get("preferences").get("Preferred_Language", "en")

    current_instance_redirect_uri = REDIRECT_URI_BASE_ENV
    if not current_instance_redirect_uri:
        log_error("calendar_tool", fn_name, "FATAL: GOOGLE_REDIRECT_URI not set. Cannot process OAuth callback.")
        return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title", "oauth_browser_error_message", user_lang, details="Server Misconfiguration"), status_code=500)

    if not user_id_from_state:
        log_error("calendar_tool", fn_name, "Callback missing state (user_id).")
        return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title", "oauth_browser_error_message", user_lang, details="Invalid response from Google (missing user identifier)"), status_code=400)

    log_info("calendar_tool", fn_name, f"Callback received for user_id: {user_id_from_state}. Code: {'Present' if code else 'Missing'}. Error: {error or 'None'}.")

    failure_chat_msg = _get_message("oauth_failure_chat_message_retry", user_lang)
    browser_error_html = _get_html_response_page("oauth_browser_page_title", "oauth_browser_error_message", user_lang, details="Authentication process could not be completed.")

    if error:
        log_error("calendar_tool", fn_name, f"Google OAuth error for user {user_id_from_state}: {error}")
        set_gcal_integration_status(user_id_from_state, "error")
        try: send_chat_message(user_id_from_state, failure_chat_msg)
        except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
        return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title", "oauth_browser_error_message", user_lang, details=f"Google reported an error: {error}"), status_code=400)
    
    if not code:
        log_error("calendar_tool", fn_name, f"Callback missing authorization code for {user_id_from_state}.")
        set_gcal_integration_status(user_id_from_state, "error") 
        try: send_chat_message(user_id_from_state, failure_chat_msg)
        except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
        return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title","oauth_browser_error_message", user_lang, details="Invalid response from Google (missing authorization code)"), status_code=400)

    if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
        log_error("calendar_tool", fn_name, "Server config error: Client ID/Secret not set for token exchange.")
        set_gcal_integration_status(user_id_from_state, "error") 
        return HTMLResponse(content=browser_error_html, status_code=500)

    try:
        payload = {
            "code": code, "client_id": CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": current_instance_redirect_uri, 
            "grant_type": "authorization_code"
        }
        token_response = requests.post(TOKEN_URL, data=payload)
        token_response.raise_for_status() 
        tokens = token_response.json()

        if 'access_token' not in tokens:
            log_error("calendar_tool", fn_name, f"Access token NOT received for {user_id_from_state}. Response: {tokens}")
            set_gcal_integration_status(user_id_from_state, "error") 
            try: send_chat_message(user_id_from_state, failure_chat_msg)
            except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
            return HTMLResponse(content=browser_error_html, status_code=500)
        
        user_email_from_google = ""
        id_token = tokens.get("id_token")
        if id_token:
            try:
                decoded_id_token = jwt.decode(id_token, options={"verify_signature": False, "verify_aud": False})
                user_email_from_google = decoded_id_token.get("email", "")
            except jwt.exceptions.DecodeError as jwt_e:
                log_warning("calendar_tool", fn_name, f"Failed to decode id_token for {user_id_from_state}, no email. Error: {jwt_e}")
        
        if not save_user_token_encrypted(user_id_from_state, tokens):
            log_error("calendar_tool", fn_name, f"Failed to save token securely for {user_id_from_state}.")
            set_gcal_integration_status(user_id_from_state, "error") 
            try: send_chat_message(user_id_from_state, failure_chat_msg)
            except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
            return HTMLResponse(content=browser_error_html, status_code=500)

        prefs_to_update = {"Calendar_Enabled": True, "gcal_integration_status": "connected"}
        if user_email_from_google: prefs_to_update["email"] = user_email_from_google
        update_preferences(user_id_from_state, prefs_to_update) 
        log_info("calendar_tool", fn_name, f"Preferences updated for {user_id_from_state}: GCal connected, email set.")

        gcal_api_instance = GoogleCalendarAPI(user_id_from_state) 
        if gcal_api_instance.is_active():
            update_agent_state_key(user_id_from_state, "calendar", gcal_api_instance) 
            success_chat_msg = _get_message("oauth_success_chat_message", user_lang)
            try: send_chat_message(user_id_from_state, success_chat_msg)
            except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send success chat msg to {user_id_from_state}", send_e)
            return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title","oauth_browser_processing_message", user_lang), status_code=200)
        else:
            log_error("calendar_tool", fn_name, f"GCal API instance NOT active for {user_id_from_state} post-OAuth.")
            set_gcal_integration_status(user_id_from_state, "error") 
            try: send_chat_message(user_id_from_state, failure_chat_msg)
            except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
            return HTMLResponse(content=browser_error_html, status_code=500)

    except requests.exceptions.HTTPError as http_e:
        error_details_google = f"Error {http_e.response.status_code if http_e.response else 'N/A'} from Google during token exchange."
        log_error("calendar_tool", fn_name, f"HTTP error token exchange for {user_id_from_state}. Details: {error_details_google}", http_e)
        set_gcal_integration_status(user_id_from_state, "error") 
        try: send_chat_message(user_id_from_state, failure_chat_msg)
        except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
        return HTMLResponse(content=_get_html_response_page("oauth_browser_page_title","oauth_browser_error_message", user_lang, details=error_details_google), status_code=http_e.response.status_code if http_e.response else 500)
    except Exception as e:
        import traceback 
        log_error("calendar_tool", fn_name, f"Generic unexpected error during callback for {user_id_from_state}. Trace: {traceback.format_exc()}", e)
        set_gcal_integration_status(user_id_from_state, "error") 
        try: send_chat_message(user_id_from_state, failure_chat_msg)
        except Exception as send_e: log_error("calendar_tool", fn_name, f"Failed to send chat error to {user_id_from_state}", send_e)
        return HTMLResponse(content=browser_error_html, status_code=500)

log_info("calendar_tool", "module_load", "Finished module load.")
# --- END OF FULL tools/calendar_tool.py (Corrected SyntaxError) ---