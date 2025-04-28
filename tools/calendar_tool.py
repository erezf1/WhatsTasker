# --- START OF FULL tools/calendar_tool.py ---

import os
import requests
import json # For logging potentially
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from tools.logger import log_info, log_error, log_warning
from tools.encryption import encrypt_data # Only need encrypt_data from here
import jwt
import requests.compat # Needed for urlencode in authenticate
from datetime import datetime # Keep if used elsewhere

# --- REMOVE service layer import attempt from module level ---
# --- Keep ONLY direct registry update as fallback ---
from users.user_registry import update_preferences as update_prefs_direct
log_warning("calendar_tool", "import", "Using direct registry update for preferences in callback.")
# ----------------------------------------------

router = APIRouter()

# --- Configuration ---
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth2callback")
SCOPE = "https://www.googleapis.com/auth/calendar"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL_BASE = "https://accounts.google.com/o/oauth2/auth"

if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
    log_error("calendar_tool", "config", "CRITICAL: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET env var not set.")

log_info("calendar_tool", "config", f"Calendar Tool loaded. Client ID starts with: {str(CLIENT_ID)[:10]}")

# --- Core Authentication Check Function ---
def authenticate(user_id: str, prefs: dict) -> dict:
    """
    Checks user's calendar auth status based on provided preferences.
    Returns status and auth URL if needed, or status/message if token exists.
    """
    log_info("calendar_tool", "authenticate", f"Checking auth status for user {user_id}")
    from tools.token_store import get_user_token # Import here
    token_data = get_user_token(user_id)
    calendar_enabled = prefs.get("Calendar_Enabled", False)

    if token_data is not None and calendar_enabled:
        log_info("calendar_tool", "authenticate", f"Token data exists and enabled flag is True for {user_id}.")
        return {"status": "token_exists", "message": "Stored calendar credentials found. Attempting to use..."}
    else:
        # ... (rest of auth URL generation logic remains the same) ...
        if token_data is not None and not calendar_enabled:
             log_info("calendar_tool", "authenticate", f"Token data exists but Calendar_Enabled=False for {user_id}. Initiating re-auth/enable.")
        else: # token_data is None
             log_info("calendar_tool", "authenticate", f"No valid token data found for {user_id}. Initiating auth.")

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
             return {"status": "pending", "message": f"Please authenticate your calendar by visiting this URL: {auth_url}"}
        except Exception as url_e:
             log_error("calendar_tool", "authenticate", f"Failed to build auth URL for {user_id}", url_e)
             return {"status": "fails", "message": "Failed to generate authentication URL."}


# --- OAuth Callback Endpoint (Corrected Scope) ---
@router.get("/oauth2callback", response_class=HTMLResponse)
async def oauth2callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """
    Handles the OAuth2 callback from Google.
    Exchanges code, saves token, updates preferences.
    """
    # --- Define flag and function placeholder INSIDE the function scope ---
    config_manager_imported_locally = False
    update_prefs_service = None
    # --- Attempt import locally ---
    try:
        from services.config_manager import update_preferences
        update_prefs_service = update_preferences
        config_manager_imported_locally = True
    except ImportError:
         # Error/warning already logged at module level, no need to repeat
         pass # Keep flag as False, function as None
    # ---------------------------------------------------------------------

    html_error_template = "<html><body><h1>Authentication Error</h1><p>Details: {details}</p><p>Please try authenticating again or contact support if the issue persists.</p></body></html>"
    html_success_template = "<html><body><h1>Authentication Successful!</h1><p>Your credentials have been saved. The connection will be fully tested when first used. You can close this window and return to the chat.</p></body></html>"

    if error:
        log_error("calendar_tool", "oauth2callback", f"OAuth error received from Google: {error}")
        return HTMLResponse(content=html_error_template.format(details=f"Google reported an error: {error}"), status_code=400)
    if not code or not state:
        log_error("calendar_tool", "oauth2callback", "Callback missing code or state.")
        return HTMLResponse(content=html_error_template.format(details="Invalid response received from Google (missing code or state)."), status_code=400)

    user_id = state
    log_info("calendar_tool", "oauth2callback", f"Callback received for user {user_id}.")

    if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
        log_error("calendar_tool", "oauth2callback", "Server configuration error: Client ID/Secret not set.")
        return HTMLResponse(content=html_error_template.format(details="Server configuration error."), status_code=500)

    try:
        # --- 1. Exchange Code for Tokens ---
        payload = {
            "code": code, "client_id": CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"
        }
        log_info("calendar_tool", "oauth2callback", f"Exchanging authorization code for tokens for user {user_id}.")
        token_response = requests.post(TOKEN_URL, data=payload)
        token_response.raise_for_status()
        tokens = token_response.json()
        log_info("calendar_tool", "oauth2callback", f"Tokens received successfully. Keys: {list(tokens.keys())}")

        if 'access_token' not in tokens:
            log_error("calendar_tool", "oauth2callback", f"Access token *NOT* received for user {user_id}.")
            return HTMLResponse(content=html_error_template.format(details="Failed to obtain access token from Google."), status_code=500)
        if 'refresh_token' not in tokens:
            log_warning("calendar_tool", "oauth2callback", f"Refresh token *NOT* received for user {user_id}. Offline access might fail later.")

        # --- 2. Extract Email ---
        email = ""
        id_token = tokens.get("id_token")
        if id_token:
            try:
                decoded = jwt.decode(id_token, options={"verify_signature": False, "verify_aud": False})
                email = decoded.get("email", "")
                log_info("calendar_tool", "oauth2callback", f"Extracted email '{email}' for user {user_id}.")
            except jwt.exceptions.DecodeError as jwt_e:
                log_warning("calendar_tool", "oauth2callback", f"Failed decode id_token for {user_id}, proceeding without email.", jwt_e)

        # --- 3. Save Tokens Encrypted ---
        from tools.token_store import save_user_token_encrypted # Import locally
        if not save_user_token_encrypted(user_id, tokens):
             log_error("calendar_tool", "oauth2callback", f"Failed to save token via token_store for {user_id}.")
             return HTMLResponse(content=html_error_template.format(details="Failed to save credentials securely."), status_code=500)
        log_info("calendar_tool", "oauth2callback", f"Tokens stored successfully via token_store for {user_id}.")

        # --- 4. Update Preferences ---
        prefs_update = { "email": email, "Calendar_Enabled": True }
        pref_update_success = False
        # --- Use locally checked flag and function ---
        if config_manager_imported_locally and update_prefs_service:
            pref_update_success = update_prefs_service(user_id, prefs_update)
            if pref_update_success:
                 log_info("calendar_tool", "oauth2callback", f"Preferences updated via ConfigManager for {user_id}: {prefs_update}")
            else:
                 log_error("calendar_tool", "oauth2callback", f"ConfigManager failed update preferences for {user_id} after token save.")
        else:
             # Fallback to direct update
             # Warning already logged at module level
             pref_update_success = update_prefs_direct(user_id, prefs_update) # Use direct update
             if pref_update_success:
                  log_info("calendar_tool", "oauth2callback", f"Preferences updated DIRECTLY for {user_id}: {prefs_update}")
             else:
                  log_error("calendar_tool", "oauth2callback", f"Direct registry update failed for {user_id} after token save.")
        # -----------------------------------------

        if pref_update_success:
             return HTMLResponse(content=html_success_template, status_code=200)
        else:
             # Tokens saved, but profile update failed
             return HTMLResponse(content=html_error_template.format(details="Credentials saved, but failed to update user profile. Contact support."), status_code=500)

    except requests.exceptions.HTTPError as http_e:
        response_text = http_e.response.text; status_code = http_e.response.status_code
        error_details = f"Error {status_code} during token exchange.";
        try: error_json = http_e.response.json(); error_details = error_json.get('error_description', error_json.get('error', f"HTTP {status_code}"))
        except ValueError: pass
        log_error("calendar_tool", "oauth2callback", f"HTTP error {status_code} during token exchange for {user_id}. Details: {error_details}", http_e)
        return HTMLResponse(content=html_error_template.format(details=f"Could not get authorization from Google: {error_details}."), status_code=status_code)
    except Exception as e:
        log_error("calendar_tool", "oauth2callback", f"Generic unexpected error during callback for {user_id}", e)
        return HTMLResponse(content=html_error_template.format(details=f"An unexpected server error occurred: {e}."), status_code=500)

# --- END OF FULL tools/calendar_tool.py ---