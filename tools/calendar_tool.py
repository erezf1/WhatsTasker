# --- START OF FULL tools/calendar_tool.py ---

import os
import requests
import json
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from tools.logger import log_info, log_error, log_warning
from tools.encryption import encrypt_data
import jwt
import requests.compat
from datetime import datetime

# --- Import config_manager locally within functions where needed ---
# --- This avoids circular dependency issues at module load time ---

router = APIRouter()

GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
SCOPE = "https://www.googleapis.com/auth/calendar"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL_BASE = "https://accounts.google.com/o/oauth2/auth"

if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
    log_error("calendar_tool", "config", "CRITICAL: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET env var not set.")

log_info("calendar_tool", "config", f"Calendar Tool loaded. Client ID starts with: {str(CLIENT_ID)[:10]}")

def authenticate(user_id: str, prefs: dict) -> dict:
    log_info("calendar_tool", "authenticate", f"Checking auth status for user {user_id}")
    from tools.token_store import get_user_token
    token_data = get_user_token(user_id)
    calendar_enabled = prefs.get("Calendar_Enabled", False)
    gcal_status = prefs.get("gcal_integration_status", "not_integrated")

    # If already connected and enabled, no need to re-auth unless status is error
    if token_data is not None and calendar_enabled and gcal_status == "connected":
        log_info("calendar_tool", "authenticate", f"GCal token exists, enabled, and status 'connected' for {user_id}.")
        return {"status": "token_exists", "message": "Calendar already connected and credentials seem valid."}
    
    # If status is 'error', or not 'connected' but token exists and enabled, or no token: proceed to auth URL
    log_info("calendar_tool", "authenticate", f"Proceeding to generate auth URL for {user_id}. Current GCal status: {gcal_status}, Token: {'Yes' if token_data else 'No'}, Enabled: {calendar_enabled}")

    if not CLIENT_ID or not REDIRECT_URI:
        log_error("calendar_tool", "authenticate", f"Client ID or Redirect URI missing for auth URL generation.")
        return {"status": "fails", "message": "Server configuration error prevents authentication."}

    normalized_state = user_id.replace("@c.us", "").replace("+","")
    params = {
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "response_type": "code", "access_type": "offline", "state": normalized_state, "prompt": "consent"
    }
    try:
        encoded_params = requests.compat.urlencode(params)
        auth_url = f"{AUTH_URL_BASE}?{encoded_params}"
        log_info("calendar_tool", "authenticate", f"Generated auth URL for {user_id}")
        
        # --- Import config_manager locally ---
        try:
            from services.config_manager import set_gcal_integration_status
            set_gcal_integration_status(user_id, "pending_auth")
        except ImportError:
            log_error("calendar_tool", "authenticate", "Failed to import config_manager to set 'pending_auth' status.")
        except Exception as e_cfg:
            log_error("calendar_tool", "authenticate", f"Error setting 'pending_auth' status for {user_id}", e_cfg)

        return {"status": "pending", "message": f"Please authenticate your calendar by visiting this URL: {auth_url}"}
    except Exception as url_e:
        log_error("calendar_tool", "authenticate", f"Failed to build auth URL for {user_id}", url_e)
        return {"status": "fails", "message": "Failed to generate authentication URL."}


@router.get("/oauth2callback", response_class=HTMLResponse)
async def oauth2callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    config_manager_imported_locally = False
    set_gcal_status_func = None
    update_prefs_func = None # For Calendar_Enabled and email

    try:
        from services.config_manager import set_gcal_integration_status, update_preferences
        set_gcal_status_func = set_gcal_integration_status
        update_prefs_func = update_preferences
        config_manager_imported_locally = True
    except ImportError:
        # Fallback to direct registry update for Calendar_Enabled and email
        from users.user_registry import update_preferences as update_prefs_direct_registry
        update_prefs_func = update_prefs_direct_registry
        log_warning("calendar_tool", "oauth2callback", "ConfigManager not found for prefs update, using direct registry update. GCal status may not be optimally set on error.")
    except Exception as e_import:
        log_error("calendar_tool", "oauth2callback", f"Unexpected error importing config_manager: {e_import}")


    html_error_template = "<html><body><h1>Authentication Error</h1><p>Details: {details}</p><p>Please try authenticating again or contact support if the issue persists.</p></body></html>"
    html_success_template = "<html><body><h1>Authentication Successful!</h1><p>Your credentials have been saved. You can close this window and return to the chat.</p></body></html>"

    if not state: # User ID must be present in state
        log_error("calendar_tool", "oauth2callback", "Callback missing state (user_id).")
        return HTMLResponse(content=html_error_template.format(details="Invalid response received from Google (missing user identifier)."), status_code=400)
    
    user_id = state # state is the user_id

    if error:
        log_error("calendar_tool", "oauth2callback", f"OAuth error received from Google for user {user_id}: {error}")
        if set_gcal_status_func: set_gcal_status_func(user_id, "error")
        else: log_error("calendar_tool", "oauth2callback", "Cannot set GCal status to 'error' due to missing config_manager.set_gcal_integration_status function.")
        return HTMLResponse(content=html_error_template.format(details=f"Google reported an error: {error}"), status_code=400)
    
    if not code:
        log_error("calendar_tool", "oauth2callback", f"Callback missing authorization code for user {user_id}.")
        if set_gcal_status_func: set_gcal_status_func(user_id, "error")
        else: log_error("calendar_tool", "oauth2callback", "Cannot set GCal status to 'error' due to missing config_manager.set_gcal_integration_status function.")
        return HTMLResponse(content=html_error_template.format(details="Invalid response received from Google (missing authorization code)."), status_code=400)

    log_info("calendar_tool", "oauth2callback", f"Callback received for user {user_id}.")

    if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
        log_error("calendar_tool", "oauth2callback", "Server configuration error: Client ID/Secret not set.")
        if set_gcal_status_func: set_gcal_status_func(user_id, "error") # Potentially set status to error
        return HTMLResponse(content=html_error_template.format(details="Server configuration error."), status_code=500)

    try:
        payload = {
            "code": code, "client_id": CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"
        }
        log_info("calendar_tool", "oauth2callback", f"Exchanging authorization code for tokens for user {user_id}.")
        token_response = requests.post(TOKEN_URL, data=payload)
        token_response.raise_for_status()
        tokens = token_response.json()
        log_info("calendar_tool", "oauth2callback", f"Tokens received successfully for {user_id}. Keys: {list(tokens.keys())}")

        if 'access_token' not in tokens:
            log_error("calendar_tool", "oauth2callback", f"Access token *NOT* received for user {user_id}.")
            if set_gcal_status_func: set_gcal_status_func(user_id, "error")
            return HTMLResponse(content=html_error_template.format(details="Failed to obtain access token from Google."), status_code=500)
        
        email = ""
        id_token = tokens.get("id_token")
        if id_token:
            try:
                decoded = jwt.decode(id_token, options={"verify_signature": False, "verify_aud": False})
                email = decoded.get("email", "")
                log_info("calendar_tool", "oauth2callback", f"Extracted email '{email}' for user {user_id}.")
            except jwt.exceptions.DecodeError as jwt_e:
                log_warning("calendar_tool", "oauth2callback", f"Failed decode id_token for {user_id}, proceeding without email.", jwt_e)

        from tools.token_store import save_user_token_encrypted
        if not save_user_token_encrypted(user_id, tokens):
            log_error("calendar_tool", "oauth2callback", f"Failed to save token via token_store for {user_id}.")
            if set_gcal_status_func: set_gcal_status_func(user_id, "error")
            return HTMLResponse(content=html_error_template.format(details="Failed to save credentials securely."), status_code=500)
        log_info("calendar_tool", "oauth2callback", f"Tokens stored successfully via token_store for {user_id}.")

        prefs_update = { "email": email, "Calendar_Enabled": True }
        pref_update_success = False
        if update_prefs_func: # This will be either config_manager.update_preferences or the direct registry one
            pref_update_success = update_prefs_func(user_id, prefs_update)
        
        if pref_update_success:
            log_info("calendar_tool", "oauth2callback", f"Preferences updated (Calendar_Enabled, email) for {user_id}.")
            if set_gcal_status_func: 
                set_gcal_status_func(user_id, "connected") # Set status to connected
            else: 
                log_error("calendar_tool", "oauth2callback", "config_manager.set_gcal_integration_status not available to set 'connected'.")
            return HTMLResponse(content=html_success_template, status_code=200)
        else:
            log_error("calendar_tool", "oauth2callback", f"Failed to update Calendar_Enabled/email preferences for {user_id} after token save.")
            if set_gcal_status_func: set_gcal_status_func(user_id, "error") # Still an error in setup
            return HTMLResponse(content=html_error_template.format(details="Credentials saved, but failed to update user profile. Contact support."), status_code=500)

    except requests.exceptions.HTTPError as http_e:
        response_text = http_e.response.text; status_code = http_e.response.status_code
        error_details = f"Error {status_code} during token exchange."
        try: error_json = http_e.response.json(); error_details = error_json.get('error_description', error_json.get('error', f"HTTP {status_code}"))
        except ValueError: pass
        log_error("calendar_tool", "oauth2callback", f"HTTP error {status_code} during token exchange for {user_id}. Details: {error_details}", http_e)
        if set_gcal_status_func: set_gcal_status_func(user_id, "error")
        return HTMLResponse(content=html_error_template.format(details=f"Could not get authorization from Google: {error_details}."), status_code=status_code)
    except Exception as e:
        log_error("calendar_tool", "oauth2callback", f"Generic unexpected error during callback for {user_id}", e)
        if set_gcal_status_func: set_gcal_status_func(user_id, "error")
        return HTMLResponse(content=html_error_template.format(details=f"An unexpected server error occurred: {e}."), status_code=500)

# --- END OF FULL tools/calendar_tool.py ---