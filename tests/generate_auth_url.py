# generate_auth_url.py
import os
from dotenv import load_dotenv
import urllib.parse # For URL encoding

# Load environment variables from .env file
load_dotenv()

# Configuration from environment variables
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI") # e.g., http://localhost:8000/oauth2callback
SCOPE = "https://www.googleapis.com/auth/calendar" # Ensure this matches your app's scope
AUTH_URL_BASE = "https://accounts.google.com/o/oauth2/auth"

def create_auth_url(user_id_for_state: str) -> str | None:
    """
    Creates the Google OAuth 2.0 authentication URL.

    Args:
        user_id_for_state: A unique identifier for the user, which will be
                           passed as the 'state' parameter and returned by Google.
                           Typically, this is your application's internal user ID.
    Returns:
        The authentication URL string, or None if configuration is missing.
    """
    if not CLIENT_ID or not REDIRECT_URI:
        print("ERROR: GOOGLE_CLIENT_ID or GOOGLE_REDIRECT_URI not found in .env file.")
        print("Please ensure these are set correctly for your local testing client ID.")
        return None

    # The 'state' parameter is crucial for security and associating the callback
    # with the user who initiated the flow.
    # For testing, you can use a simple mock user ID.
    # In your app, this would be the actual user_id.
    normalized_state = user_id_for_state.replace("@c.us", "").replace("+","") # Simple normalization

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "response_type": "code",        # We want an authorization code
        "access_type": "offline",       # To get a refresh token
        "state": normalized_state,      # To maintain state and prevent CSRF
        "prompt": "consent"             # Ensures the user sees the consent screen, useful for testing
                                        # and for ensuring refresh token is granted.
                                        # Can be changed to 'select_account consent' or just 'select_account'
                                        # once you are sure the refresh token mechanism works.
    }

    # URL encode the parameters
    encoded_params = urllib.parse.urlencode(params)
    auth_url = f"{AUTH_URL_BASE}?{encoded_params}"
    return auth_url

if __name__ == "__main__":
    print("--- Google OAuth URL Generator for Local Testing ---")

    if not CLIENT_ID or not REDIRECT_URI:
        print("\nERROR: Missing GOOGLE_CLIENT_ID or GOOGLE_REDIRECT_URI in your .env file.")
        print("Please set these to the credentials of your OAuth Client ID that is configured")
        print(f"to allow '{REDIRECT_URI}' as an Authorized redirect URI in Google Cloud Console.")
    else:
        print(f"\nUsing configuration from .env:")
        print(f"  Client ID: {CLIENT_ID[:10]}...") # Print only a part for brevity/security
        print(f"  Redirect URI: {REDIRECT_URI}")
        print(f"  Scope: {SCOPE}")

        mock_user_id = input("\nEnter a mock User ID to use for the 'state' parameter (e.g., 'localtestuser123'): ")
        if not mock_user_id.strip():
            mock_user_id = "default_test_user_state"
            print(f"No User ID entered, using default: '{mock_user_id}'")

        auth_link = create_auth_url(mock_user_id.strip())

        if auth_link:
            print("\nGenerated Authentication URL:\n")
            print(auth_link)
            print("\n--- Instructions ---")
            print("1. Ensure your WhatsTasker application (main.py) is running locally and")
            print(f"   is accessible at the redirect URI ({REDIRECT_URI}).")
            print("2. Copy the URL above and paste it into your web browser.")
            print("3. Sign in with a Google account that is either a 'test user' (if your")
            print("   OAuth consent screen is in 'Testing' mode) or any Google account (if")
            print("   your consent screen is 'In production').")
            print("4. Grant the requested permissions.")
            print("5. If successful, Google will redirect your browser back to your application's")
            print(f"   {REDIRECT_URI} endpoint with an authorization code.")
            print("6. Your application's `/oauth2callback` endpoint should then handle this code.")
        else:
            print("\nFailed to generate authentication URL. Please check your .env configuration.")