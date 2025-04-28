# --- START OF FULL tools/google_calendar_api.py ---

import os
import re  # <--- ADD THIS IMPORT

from datetime import datetime, timedelta
from typing import Dict, List, Any, TYPE_CHECKING # Removed Optional

# --- Try importing Google libraries ---
Credentials = None
build = None
HttpError = Exception
GoogleAuthRequest = None
RefreshError = Exception
GOOGLE_LIBS_AVAILABLE = False

try:
    from tools.logger import log_info, log_error, log_warning
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:google_calendar_api:%(message)s')
    log_info = logging.info; log_error = logging.error; log_warning = logging.warning
    log_error("google_calendar_api", "import", "Failed to import project logger.")

try:
    from google.oauth2.credentials import Credentials as ImportedCredentials
    from googleapiclient.discovery import build as imported_build
    from googleapiclient.errors import HttpError as ImportedHttpError
    from google.auth.transport.requests import Request as ImportedGoogleAuthRequest
    from google.auth.exceptions import RefreshError as ImportedRefreshError

    Credentials = ImportedCredentials
    build = imported_build
    HttpError = ImportedHttpError
    GoogleAuthRequest = ImportedGoogleAuthRequest
    RefreshError = ImportedRefreshError
    GOOGLE_LIBS_AVAILABLE = True
    log_info("google_calendar_api", "import", "Successfully imported Google API libraries.")
except ImportError as import_error_exception:
    log_error("google_calendar_api", "import", f"Failed to import one or more Google API libraries: {import_error_exception}. GoogleCalendarAPI will be non-functional.", import_error_exception)

# --- Other Local Project Imports ---
try:
    from tools.token_store import get_user_token, save_user_token_encrypted
except ImportError as e:
     log_error("google_calendar_api", "import", f"Failed to import from token_store: {e}", e)
     # Define dummy functions if import fails
     def get_user_token(*args, **kwargs): return None
     def save_user_token_encrypted(*args, **kwargs): return False

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource
    if Credentials:
         from google.oauth2.credentials import Credentials

# --- Configuration ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
if not GOOGLE_CLIENT_ID: log_error("google_calendar_api", "config", "CRITICAL: GOOGLE_CLIENT_ID not set.")
if not GOOGLE_CLIENT_SECRET: log_error("google_calendar_api", "config", "CRITICAL: GOOGLE_CLIENT_SECRET not set.")
DEFAULT_TIMEZONE = "Asia/Jerusalem" # TODO: Make user-specific via preferences
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleCalendarAPI:
    """Handles interactions with the Google Calendar API for a specific user."""
    def __init__(self, user_id: str):
        fn_name = "__init__"
        self.user_id = user_id
        self.service = None # Initialize service to None
        self.user_timezone = DEFAULT_TIMEZONE # TODO: Load from user prefs eventually

        log_info("GoogleCalendarAPI", fn_name, f"Initializing for user {self.user_id}")
        if not GOOGLE_LIBS_AVAILABLE:
            log_error("GoogleCalendarAPI", fn_name, "Google API libraries not available. Initialization skipped.")
            return

        credentials = self._load_credentials() # This now returns Credentials or None

        if credentials is not None: # Explicit check for None
            try:
                if build is None:
                    raise ImportError("Build function ('googleapiclient.discovery.build') not available.")
                # Assign the built service to self.service
                self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
                log_info("GoogleCalendarAPI", fn_name, f"GCal service built successfully for {self.user_id}")
            except ImportError as e:
                 log_error("GoogleCalendarAPI", fn_name, f"Import error during service build: {e}", e)
                 self.service = None # Ensure service is None on error
            except Exception as e:
                log_error("GoogleCalendarAPI", fn_name, f"Failed to build GCal service: {e}", e)
                self.service = None # Ensure service is None on error
        else:
            log_warning("GoogleCalendarAPI", fn_name, f"Initialization incomplete for {self.user_id} due to credential failure.")
            self.service = None # Ensure service is None if creds fail


    # Return type is now 'Credentials | None', but we remove the hint as requested
    # The function still returns None on failure.
    def _load_credentials(self):
        fn_name = "_load_credentials"
        log_info("GoogleCalendarAPI", fn_name, f"Attempting credentials load for {self.user_id}")

        if not GOOGLE_LIBS_AVAILABLE or Credentials is None:
            log_error("GoogleCalendarAPI", fn_name, "Google libraries or Credentials class not available.")
            return None

        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
             log_error("GoogleCalendarAPI", fn_name, "Client ID or Secret missing in environment config.")
             return None

        token_data = get_user_token(self.user_id)
        if token_data is None: # Explicit check
             log_info("GoogleCalendarAPI", fn_name, f"No token data found for user {self.user_id}.")
             return None

        if "refresh_token" not in token_data:
             log_error("GoogleCalendarAPI", fn_name, f"FATAL: refresh_token missing in stored data for {self.user_id}. Re-auth needed.")
             # Consider deleting the bad token file here?
             # delete_token_file(self.user_id) # Hypothetical function
             return None

        credential_info_for_lib = {
            'token': token_data.get('access_token'),
            'refresh_token': token_data.get('refresh_token'),
            'token_uri': GOOGLE_TOKEN_URI,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'scopes': token_data.get('scopes', [])
        }
        # Ensure scopes is a list
        if isinstance(credential_info_for_lib['scopes'], str):
            credential_info_for_lib['scopes'] = credential_info_for_lib['scopes'].split()

        creds = None
        try:
            creds = Credentials.from_authorized_user_info(credential_info_for_lib)

            if not creds.valid:
                log_warning("GoogleCalendarAPI", fn_name, f"Credentials invalid/expired for {self.user_id}. Checking refresh token.")
                if creds.refresh_token:
                    log_info("GoogleCalendarAPI", fn_name, f"Attempting explicit token refresh for {self.user_id}...")
                    try:
                        if GoogleAuthRequest is None: raise ImportError("GoogleAuthRequest class not available for refresh.")
                        creds.refresh(GoogleAuthRequest())
                        log_info("GoogleCalendarAPI", fn_name, f"Token refresh successful for {self.user_id}.")
                        # Prepare data for saving (including expiry)
                        refreshed_token_data_to_save = {
                            'access_token': creds.token,
                            'refresh_token': creds.refresh_token,
                            'token_uri': creds.token_uri,
                            'client_id': creds.client_id,
                            'client_secret': creds.client_secret,
                            'scopes': creds.scopes,
                            'expiry_iso': creds.expiry.isoformat() if creds.expiry else None
                        }
                        # Attempt to save the refreshed token
                        if save_user_token_encrypted is None:
                            log_error("GoogleCalendarAPI", fn_name, "save_user_token_encrypted function not available.")
                        elif not save_user_token_encrypted(self.user_id, refreshed_token_data_to_save):
                            log_warning("GoogleCalendarAPI", fn_name, f"Failed to save refreshed token for {self.user_id}.")
                        # else: Saved successfully (no log needed unless verbose)

                    except RefreshError as refresh_err:
                        log_error("GoogleCalendarAPI", fn_name, f"Token refresh FAILED for {self.user_id} (RefreshError): {refresh_err}. Re-authentication required.", refresh_err)
                        token_file_path = os.path.join("data", f"tokens_{self.user_id}.json.enc")
                        if os.path.exists(token_file_path):
                            log_warning("GoogleCalendarAPI", fn_name, f"Deleting invalid token file due to refresh failure: {token_file_path}")
                            try: os.remove(token_file_path)
                            except OSError as rm_err: log_error("GoogleCalendarAPI", fn_name, f"Failed to remove token file: {rm_err}")
                        return None # Return None as refresh failed
                    except ImportError as imp_err:
                         log_error("GoogleCalendarAPI", fn_name, f"Import error during refresh for {self.user_id}: {imp_err}")
                         return None
                    except Exception as refresh_err:
                        log_error("GoogleCalendarAPI", fn_name, f"Unexpected error during token refresh for {self.user_id}: {refresh_err}", refresh_err)
                        return None # Return None on unexpected error
                else:
                     log_error("GoogleCalendarAPI", fn_name, f"Credentials invalid for {self.user_id}, and no refresh token available. Re-authentication needed.")
                     return None # Return None as creds invalid and no refresh possible

            # Final check after potential refresh attempt
            if creds and creds.valid:
                 log_info("GoogleCalendarAPI", fn_name, f"Credentials loaded and valid for {self.user_id}.")
                 return creds # Return the valid credentials object
            else:
                 log_error("GoogleCalendarAPI", fn_name, f"Failed to obtain valid credentials for {self.user_id} after potential refresh.")
                 return None # Return None as still not valid
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Error creating/validating credentials object for {self.user_id}: {e}", e)
            return None # Return None on error


    def is_active(self):
        """Checks if the Google Calendar service object was successfully initialized."""
        return self.service is not None

    # Returns event ID string or None
    def create_event(self, event_data: Dict):
        fn_name = "create_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}.")
            return None
        assert self.service is not None # Should be active if is_active() passed

        try:
            event_date = event_data.get('date')
            event_time = event_data.get('time')
            duration_minutes = None
            if event_data.get('duration'):
                try:
                    # Improved duration parsing logic
                    duration_str = str(event_data['duration']).lower().replace(' ','')
                    total_minutes = 0.0
                    hour_match = re.search(r'(\d+(\.\d+)?)\s*h', duration_str)
                    minute_match = re.search(r'(\d+)\s*m', duration_str)
                    if hour_match: total_minutes += float(hour_match.group(1)) * 60
                    if minute_match: total_minutes += int(minute_match.group(1))
                    # Handle plain numbers (assume minutes) only if no h/m found
                    if total_minutes == 0 and hour_match is None and minute_match is None:
                         if duration_str.replace('.','',1).isdigit():
                              total_minutes = float(duration_str)
                         else: raise ValueError("Unrecognized duration format")
                    duration_minutes = int(round(total_minutes)) if total_minutes > 0 else None
                except (ValueError, TypeError, AttributeError):
                    log_warning("GoogleCalendarAPI", fn_name, f"Could not parse duration: {event_data.get('duration')}, using default.")
                    duration_minutes = 30 # Default to 30 mins if parse fails

            if not event_date:
                 log_error("GoogleCalendarAPI", fn_name, f"Missing mandatory 'date' field.")
                 return None

            start_obj = {}
            end_obj = {}
            time_zone = self.user_timezone # Use the instance timezone

            if event_time:
                 # Handle timed event
                 try:
                     start_dt = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
                     # Use parsed duration or default
                     delta = timedelta(minutes=duration_minutes if duration_minutes is not None else 30)
                     end_dt = start_dt + delta
                     start_obj = {"dateTime": start_dt.isoformat(), "timeZone": time_zone}
                     end_obj = {"dateTime": end_dt.isoformat(), "timeZone": time_zone}
                 except ValueError as time_err:
                      log_error("GoogleCalendarAPI", fn_name, f"Invalid date/time format: {event_date} {event_time}", time_err)
                      return None
            else: # Handle all-day event
                try:
                    start_dt_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                    # All-day events end on the *next* day according to GCal API
                    end_date_dt = start_dt_date + timedelta(days=1)
                    start_obj = {"date": start_dt_date.strftime("%Y-%m-%d")}
                    end_obj = {"date": end_date_dt.strftime("%Y-%m-%d")}
                except ValueError as date_err:
                     log_error("GoogleCalendarAPI", fn_name, f"Invalid date format '{event_date}' for all-day event", date_err)
                     return None

            # Construct the event body
            google_event_body = {
                "summary": event_data.get("title", event_data.get("description", "New Item")),
                "description": event_data.get("description", ""),
                "start": start_obj,
                "end": end_obj
            }

            log_info("GoogleCalendarAPI", fn_name, f"Creating GCal event for user {self.user_id}: {google_event_body.get('summary')}")
            created_event = self.service.events().insert(calendarId='primary', body=google_event_body).execute()
            google_event_id = created_event.get("id")

            if google_event_id:
                log_info("GoogleCalendarAPI", fn_name, f"Successfully created GCal event ID: {google_event_id}")
                return google_event_id # Return the ID string
            else:
                log_error("GoogleCalendarAPI", fn_name, f"GCal API response missing 'id'. Response: {created_event}")
                return None # Return None on failure
        except HttpError as http_err:
             log_error("GoogleCalendarAPI", fn_name, f"HTTP error creating event for user {self.user_id}: Status {http_err.resp.status}", http_err)
             return None
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error creating event for user {self.user_id}", e)
            return None

    # Returns bool
    def update_event(self, event_id: str, updates: Dict):
        fn_name = "update_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot update event {event_id}.")
            return False
        assert self.service is not None

        try:
            # Get the existing event first to determine current times/duration if needed
            try:
                  existing_event = self.service.events().get(calendarId='primary', eventId=event_id).execute()
            except HttpError as get_err:
                 # If event not found, cannot update
                 if get_err.resp.status == 404:
                     log_warning("GoogleCalendarAPI", fn_name, f"Cannot update event {event_id}: Not found.")
                     return False
                 else:
                      log_error("GoogleCalendarAPI", fn_name, f"HTTP error getting event {event_id} before update: {get_err}", get_err)
                      return False

            update_payload = {}
            needs_update = False
            time_zone = self.user_timezone

            # Update simple fields
            if "title" in updates: update_payload["summary"] = updates["title"]; needs_update = True
            if "description" in updates: update_payload["description"] = updates["description"]; needs_update = True

            # Handle date/time updates carefully
            new_date_str = updates.get("date")
            new_time_str = updates.get("time") # Can be None if time is cleared

            # Check if date or time is being explicitly modified
            if new_date_str is not None or "time" in updates:
                 current_start_info = existing_event.get('start', {})
                 current_end_info = existing_event.get('end', {})
                 is_currently_all_day = 'date' in current_start_info and 'dateTime' not in current_start_info

                 # Determine the target date
                 target_date_str = new_date_str
                 if target_date_str is None: # Date not provided in update, use existing
                      if is_currently_all_day:
                           target_date_str = current_start_info.get('date')
                      elif current_start_info.get('dateTime'):
                           try: target_date_str = datetime.fromisoformat(current_start_info['dateTime']).strftime('%Y-%m-%d')
                           except ValueError: target_date_str = None # Fallback if parse fails
                      else: target_date_str = None # Cannot determine existing date

                 # Determine the target time (can be None)
                 target_time_str = new_time_str if "time" in updates else (datetime.fromisoformat(current_start_info['dateTime']).strftime('%H:%M') if current_start_info.get('dateTime') and not is_currently_all_day else None)

                 if target_date_str:
                      if target_time_str: # Update to a timed event
                           try:
                               start_dt = datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
                               # Preserve duration if possible
                               duration = timedelta(minutes=30) # Default fallback
                               if current_start_info.get('dateTime') and current_end_info.get('dateTime'):
                                    try: duration = datetime.fromisoformat(current_end_info['dateTime']) - datetime.fromisoformat(current_start_info['dateTime'])
                                    except ValueError: pass # Use default if parse fails
                               end_dt = start_dt + duration
                               update_payload["start"] = {"dateTime": start_dt.isoformat(), "timeZone": time_zone}
                               update_payload["end"] = {"dateTime": end_dt.isoformat(), "timeZone": time_zone}
                               needs_update = True
                           except ValueError as e:
                                log_error("GoogleCalendarAPI", fn_name, f"Invalid date/time '{target_date_str} {target_time_str}' on update: {e}")
                                # Don't proceed with this part of the update if format is bad
                      else: # Update to an all-day event
                           try:
                               start_dt_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                               end_date_dt = start_dt_date + timedelta(days=1)
                               update_payload["start"] = {"date": start_dt_date.strftime("%Y-%m-%d")}
                               update_payload["end"] = {"date": end_date_dt.strftime("%Y-%m-%d")}
                               # Clear any existing dateTime fields if changing to all-day
                               if 'dateTime' in update_payload.get("start",{}): del update_payload["start"]["dateTime"]
                               if 'dateTime' in update_payload.get("end",{}): del update_payload["end"]["dateTime"]
                               needs_update = True
                           except ValueError as e:
                                log_error("GoogleCalendarAPI", fn_name, f"Invalid date '{target_date_str}' for all-day update: {e}")

            if not needs_update:
                 log_info("GoogleCalendarAPI", fn_name, f"No fields require patching for GCal event {event_id}")
                 return True # No change needed, considered success

            log_info("GoogleCalendarAPI", fn_name, f"Patching GCal event {event_id} for user {self.user_id}. Fields: {list(update_payload.keys())}")
            self.service.events().patch(calendarId='primary', eventId=event_id, body=update_payload).execute()
            log_info("GoogleCalendarAPI", fn_name, f"Successfully updated GCal event {event_id}")
            return True # Return True on success
        except HttpError as http_err:
             log_error("GoogleCalendarAPI", fn_name, f"HTTP error updating event {event_id}: Status {http_err.resp.status}", http_err)
             return False
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error updating event {event_id}", e)
            return False

    # Returns bool
    def delete_event(self, event_id: str):
        fn_name = "delete_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot delete event {event_id}.")
            return False
        assert self.service is not None
        try:
            log_info("GoogleCalendarAPI", fn_name, f"Attempting delete GCal event {event_id} for user {self.user_id}")
            # sendNotifications=False might be useful if you handle reminders internally
            self.service.events().delete(calendarId='primary', eventId=event_id, sendNotifications=False).execute()
            log_info("GoogleCalendarAPI", fn_name, f"Successfully deleted GCal event {event_id}.")
            return True # Return True on success
        except HttpError as http_err:
            if http_err.resp.status in [404, 410]: # Not Found or Gone
                log_warning("GoogleCalendarAPI", fn_name, f"GCal event {event_id} not found or already gone (Status {http_err.resp.status}). Assuming deleted.")
                return True # Consider deletion successful if it's already gone
            else:
                 log_error("GoogleCalendarAPI", fn_name, f"HTTP error deleting event {event_id}: Status {http_err.resp.status}", http_err)
                 return False # Return False on other HTTP errors
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error deleting event {event_id}", e)
            return False

    # Returns list of dicts or empty list
    def list_events(self, start_date: str, end_date: str):
        fn_name = "list_events"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot list events.")
            return []
        assert self.service is not None

        try:
            # Format dates for API (inclusive start, exclusive end)
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt_exclusive = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            # Use UTC 'Z' for timeMin/timeMax as recommended by Google API
            time_min = start_dt.isoformat() + "Z"
            time_max = end_dt_exclusive.isoformat() + "Z"
            log_info("GoogleCalendarAPI", fn_name, f"Listing GCal events for user {self.user_id} from {time_min} to {time_max}")
        except ValueError as date_err:
            log_error("GoogleCalendarAPI", fn_name, f"Invalid date format for listing events: {start_date} / {end_date}", date_err)
            return [] # Return empty list on bad date format

        try:
            all_items = []
            page_token = None
            while True:
                events_result = self.service.events().list(
                    calendarId='primary',
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=250, # Max allowed by API per page
                    singleEvents=True, # Expand recurring events
                    orderBy='startTime',
                    pageToken=page_token
                ).execute()
                items = events_result.get("items", [])
                all_items.extend(items)
                page_token = events_result.get('nextPageToken')
                if not page_token: break # Exit loop when no more pages
            log_info("GoogleCalendarAPI", fn_name, f"Found {len(all_items)} GCal events for user {self.user_id} in range.")
            # Parse *after* collecting all items
            return [self._parse_google_event(e) for e in all_items]
        except HttpError as http_err:
             log_error("GoogleCalendarAPI", fn_name, f"HTTP error listing events for user {self.user_id}: Status {http_err.resp.status}", http_err)
             return []
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error listing events for user {self.user_id}", e)
            return []

    # Returns dict or None
    def _get_single_event(self, event_id: str):
        fn_name = "_get_single_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}.")
            return None
        assert self.service is not None
        try:
            event = self.service.events().get(calendarId='primary', eventId=event_id).execute()
            return event # Return the raw GCal event dictionary
        except HttpError as http_err:
             if http_err.resp.status in [404, 410]: # Not Found or Gone
                  log_warning("GoogleCalendarAPI", fn_name, f"Event {event_id} not found (Status {http_err.resp.status}).")
                  return None # Return None if not found
             else:
                  log_error("GoogleCalendarAPI", fn_name, f"HTTP error getting event {event_id}: Status {http_err.resp.status}", http_err)
                  return None # Return None on other errors
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error getting event {event_id}", e)
            return None

    # Returns dict
    def _parse_google_event(self, event: Dict):
        # This function parses the raw GCal event into our standard format
        start_info = event.get("start", {})
        end_info = event.get("end", {})
        # Get dateTime first, fallback to date for all-day events
        start_datetime_str = start_info.get("dateTime", start_info.get("date"))
        end_datetime_str = end_info.get("dateTime", end_info.get("date"))
        # Determine if it's an all-day event
        is_all_day = "date" in start_info and "dateTime" not in start_info

        # Basic parsing
        parsed = {
            "event_id": event.get("id"),
            "title": event.get("summary", ""),
            "description": event.get("description", ""),
            "gcal_start_datetime": start_datetime_str, # Store the full string
            "gcal_end_datetime": end_datetime_str,     # Store the full string
            "is_all_day": is_all_day,
            "gcal_link": event.get("htmlLink", ""),
            "status_gcal": event.get("status", ""), # e.g., 'confirmed', 'tentative', 'cancelled'
            "created_gcal": event.get("created"), # ISO timestamp
            "updated_gcal": event.get("updated"), # ISO timestamp
            # Add any other relevant fields we might want later
        }
        return parsed

# --- END OF CLASS GoogleCalendarAPI ---

# --- END OF FULL tools/google_calendar_api.py ---