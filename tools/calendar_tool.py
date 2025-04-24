# tools/calendar_tool.py

import os
import requests
import json # For logging potentially
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from tools.logger import log_info, log_error, log_warning
from tools.encryption import encrypt_data # Need encrypt
import jwt
import requests.compat # Needed for urlencode in authenticate
from datetime import datetime # Keep if used elsewhere

# --- TEMPORARY PHASE 1 IMPORTS ---
# This direct import is specific to Phase 1 testing.
# Later phases might use a service (ConfigManager) to update preferences.
from users.user_registry import update_preferences as update_prefs_direct
# ----------------------------------

router = APIRouter()

# --- Configuration ---
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth2callback")
SCOPE = "https://www.googleapis.com/auth/calendar"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL_BASE = "https://accounts.google.com/o/oauth2/auth"

# Check essential config on load
if not GOOGLE_CLIENT_SECRET or not CLIENT_ID:
    log_error("calendar_tool", "config", "CRITICAL: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET env var not set.")

log_info("calendar_tool", "config", f"Calendar Tool loaded. Client ID starts with: {str(CLIENT_ID)[:10]}")


# --- Core Authentication Check Function (Phase 1) ---
def authenticate(user_id: str, prefs: dict) -> dict:
    """
    Checks user's calendar auth status based on provided preferences (Phase 1).
    Returns status and auth URL if needed, or status/message if token exists.
    Does NOT attempt to load/test the token here.
    """
    log_info("calendar_tool", "authenticate", f"Checking auth status for user {user_id}")
    token_file_path = prefs.get("token_file")
    calendar_enabled = prefs.get("Calendar_Enabled", False)

    # Scenario 1: Token file exists and system thinks it's enabled
    if calendar_enabled and token_file_path and os.path.exists(token_file_path):
        log_info("calendar_tool", "authenticate", f"Token file exists and enabled flag is True for {user_id}.")
        # Report that token exists, actual validation happens when GCalAPI is initialized
        return {"status": "token_exists", "message": "Stored calendar credentials found. Attempting to use..."}

    # Scenario 2: Initiate new authorization
    else:
        log_info("calendar_tool", "authenticate", f"No valid token file found or not enabled for {user_id}. Initiating auth.")
        # Check essential config needed to generate URL
        if not CLIENT_ID or not REDIRECT_URI:
             log_error("calendar_tool", "authenticate", f"Client ID or Redirect URI missing for auth URL generation.")
             return {"status": "fails", "message": "Server configuration error prevents authentication."}

        normalized_state = user_id.replace("@c.us", "").replace("+","") # Basic normalization
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "response_type": "code",
            "access_type": "offline", # Crucial for refresh token
            "state": normalized_state,
            "prompt": "consent" # Force consent screen for refresh token reliability
        }
        try:
             encoded_params = requests.compat.urlencode(params)
             auth_url = f"{AUTH_URL_BASE}?{encoded_params}"
             log_info("calendar_tool", "authenticate", f"Generated auth URL for {user_id}")
             return {"status": "pending", "message": f"Please authenticate your calendar by visiting this URL: {auth_url}"}
        except Exception as url_e:
             log_error("calendar_tool", "authenticate", f"Failed to build auth URL for {user_id}", url_e)
             return {"status": "fails", "message": "Failed to generate authentication URL."}


# --- OAuth Callback Endpoint (Cleaned "OLD STYLE" Logic) ---
@router.get("/oauth2callback", response_class=HTMLResponse)
async def oauth2callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """
    Handles the OAuth2 callback from Google.
    Exchanges code, saves token (WITHOUT immediate verification), updates registry.
    """
    html_error_template = "<html><body><h1>Authentication Error</h1><p>Details: {details}</p><p>Please try authenticating again or contact support if the issue persists.</p></body></html>"
    # Simple success message assuming token *will* work later
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

        # --- Basic Token Checks ---
        if 'access_token' not in tokens:
             log_error("calendar_tool", "oauth2callback", f"Access token *NOT* received for user {user_id}.")
             return HTMLResponse(content=html_error_template.format(details="Failed to obtain access token from Google."), status_code=500)
        if 'refresh_token' not in tokens:
             log_warning("calendar_tool", "oauth2callback", f"Refresh token *NOT* received for user {user_id}. Offline access might fail later.")
        # --- End Basic Checks ---

        # --- Verification Block REMOVED ---

        # --- 3. Extract Email ---
        email = ""
        id_token = tokens.get("id_token")
        if id_token:
            try:
                decoded = jwt.decode(id_token, options={"verify_signature": False})
                email = decoded.get("email", "")
                log_info("calendar_tool", "oauth2callback", f"Extracted email '{email}' for user {user_id}.")
            except jwt.exceptions.DecodeError as jwt_e:
                 log_warning("calendar_tool", "oauth2callback", f"Failed to decode id_token for {user_id}, proceeding without email.", jwt_e)

        # --- 4. Encrypt and Save Tokens ---
        log_info("calendar_tool", "oauth2callback", f"Proceeding to encrypt and save token for {user_id} (no immediate verification).")
        encrypted_tokens = encrypt_data(tokens)
        if not encrypted_tokens:
             log_error("calendar_tool", "oauth2callback", f"Encryption failed for {user_id}'s tokens.")
             return HTMLResponse(content=html_error_template.format(details="Failed to secure credentials."), status_code=500)

        token_file_path = os.path.join("data", f"tokens_{user_id}.json.enc")
        try:
            os.makedirs(os.path.dirname(token_file_path), exist_ok=True)
            with open(token_file_path, "wb") as f: f.write(encrypted_tokens)
            log_info("calendar_tool", "oauth2callback", f"Tokens stored successfully for {user_id}.")

            # --- 5. Update Preferences (DIRECTLY in Registry - Phase 1 Only) ---
            prefs_update = {
                "email": email,
                "token_file": token_file_path,
                "Calendar_Enabled": True,
                "status": "active" # Set user active after successful save for Phase 1
            }
            try:
                update_prefs_direct(user_id, prefs_update)
                log_info("calendar_tool", "oauth2callback", f"Preferences updated DIRECTLY for {user_id}: {prefs_update}")
                # Return simple SUCCESS response to user
                return HTMLResponse(content=html_success_template, status_code=200)
            except Exception as pref_e:
                log_error("calendar_tool", "oauth2callback", f"Failed to update preferences registry for {user_id} after token save.", pref_e)
                return HTMLResponse(content=html_error_template.format(details="Credentials saved, but failed to update user profile. Contact support."), status_code=500)

        except IOError as io_e:
             log_error("calendar_tool", "oauth2callback", f"Failed to write token file {token_file_path}", io_e)
             return HTMLResponse(content=html_error_template.format(details="Failed to save credentials locally."), status_code=500)

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