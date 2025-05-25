# --- START OF FULL tools/google_calendar_api.py ---

import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any, TYPE_CHECKING

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

# --- config_manager import for setting GCal status ---
CONFIG_MANAGER_IMPORTED = False
_set_gcal_status_func = None
try:
    from services.config_manager import set_gcal_integration_status
    _set_gcal_status_func = set_gcal_integration_status
    CONFIG_MANAGER_IMPORTED = True
except ImportError:
    log_error("google_calendar_api", "import", "Failed to import config_manager.set_gcal_integration_status. GCal status updates on API error will be skipped.")
# --- End config_manager import ---

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

try:
    from tools.token_store import get_user_token, save_user_token_encrypted
except ImportError as e:
    log_error("google_calendar_api", "import", f"Failed to import from token_store: {e}", e)
    def get_user_token(*args, **kwargs): return None
    def save_user_token_encrypted(*args, **kwargs): return False

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource
    if Credentials:
        from google.oauth2.credentials import Credentials

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
if not GOOGLE_CLIENT_ID: log_error("google_calendar_api", "config", "CRITICAL: GOOGLE_CLIENT_ID not set.")
if not GOOGLE_CLIENT_SECRET: log_error("google_calendar_api", "config", "CRITICAL: GOOGLE_CLIENT_SECRET not set.")
DEFAULT_TIMEZONE = "Asia/Jerusalem"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleCalendarAPI:
    def __init__(self, user_id: str):
        fn_name = "__init__"
        self.user_id = user_id
        self.service: Any = None # Use Any if Resource is not available
        self.user_timezone = DEFAULT_TIMEZONE

        # log_info("GoogleCalendarAPI", fn_name, f"Initializing for user {self.user_id}") # Verbose
        if not GOOGLE_LIBS_AVAILABLE:
            log_error("GoogleCalendarAPI", fn_name, "Google API libraries not available. Initialization skipped.")
            if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func:
                 _set_gcal_status_func(self.user_id, "error") # Cannot connect if libs missing
            return

        credentials = self._load_credentials()

        if credentials is not None:
            try:
                if build is None:
                    raise ImportError("Build function ('googleapiclient.discovery.build') not available.")
                self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
                # log_info("GoogleCalendarAPI", fn_name, f"GCal service built successfully for {self.user_id}") # Verbose
                # If service builds successfully, we assume 'connected' for now.
                # Actual API calls will test this and can set to 'error' if they fail.
                # Only set to 'connected' if status was 'pending_auth' or 'not_integrated'.
                # This check is tricky here as we don't have direct access to current status easily.
                # Let's assume that if we get this far, an attempt to connect is being made.
                # The calling function (e.g. user_manager) might set 'connected' if is_active() is true.
            except ImportError as e:
                log_error("GoogleCalendarAPI", fn_name, f"Import error during service build: {e}", e)
                self.service = None
                if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
            except Exception as e:
                log_error("GoogleCalendarAPI", fn_name, f"Failed to build GCal service: {e}", e)
                self.service = None
                if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
        else:
            # _load_credentials already logs and sets status to 'error' on failure
            log_warning("GoogleCalendarAPI", fn_name, f"Initialization incomplete for {self.user_id} due to credential failure.")
            self.service = None
            # No need to set status here again, _load_credentials handles it

    def _load_credentials(self) -> Any: # Return type Credentials | None
        fn_name = "_load_credentials"
        # log_info("GoogleCalendarAPI", fn_name, f"Attempting credentials load for {self.user_id}") # Verbose

        if not GOOGLE_LIBS_AVAILABLE or Credentials is None:
            log_error("GoogleCalendarAPI", fn_name, "Google libraries or Credentials class not available.")
            if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
            return None

        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            log_error("GoogleCalendarAPI", fn_name, "Client ID or Secret missing in environment config.")
            if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
            return None

        token_data = get_user_token(self.user_id)
        if token_data is None:
            # log_info("GoogleCalendarAPI", fn_name, f"No token data found for user {self.user_id}.") # Verbose
            # Not necessarily an error state yet, could be first time.
            return None

        if "refresh_token" not in token_data:
            log_error("GoogleCalendarAPI", fn_name, f"FATAL: refresh_token missing in stored data for {self.user_id}. Re-auth needed.")
            if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
            return None

        credential_info_for_lib = {
            'token': token_data.get('access_token'), 'refresh_token': token_data.get('refresh_token'),
            'token_uri': GOOGLE_TOKEN_URI, 'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET, 'scopes': token_data.get('scopes', [])
        }
        if isinstance(credential_info_for_lib['scopes'], str):
            credential_info_for_lib['scopes'] = credential_info_for_lib['scopes'].split()

        creds = None
        try:
            creds = Credentials.from_authorized_user_info(credential_info_for_lib)
            if not creds.valid:
                log_warning("GoogleCalendarAPI", fn_name, f"Credentials invalid/expired for {self.user_id}. Checking refresh token.")
                if creds.refresh_token:
                    # log_info("GoogleCalendarAPI", fn_name, f"Attempting explicit token refresh for {self.user_id}...") # Verbose
                    try:
                        if GoogleAuthRequest is None: raise ImportError("GoogleAuthRequest class not available for refresh.")
                        creds.refresh(GoogleAuthRequest())
                        # log_info("GoogleCalendarAPI", fn_name, f"Token refresh successful for {self.user_id}.") # Verbose
                        refreshed_token_data_to_save = {
                            'access_token': creds.token, 'refresh_token': creds.refresh_token,
                            'token_uri': creds.token_uri, 'client_id': creds.client_id,
                            'client_secret': creds.client_secret, 'scopes': creds.scopes,
                            'expiry_iso': creds.expiry.isoformat() if creds.expiry else None
                        }
                        if save_user_token_encrypted is None:
                            log_error("GoogleCalendarAPI", fn_name, "save_user_token_encrypted function not available.")
                        elif not save_user_token_encrypted(self.user_id, refreshed_token_data_to_save):
                            log_warning("GoogleCalendarAPI", fn_name, f"Failed to save refreshed token for {self.user_id}.")
                    except RefreshError as refresh_err:
                        log_error("GoogleCalendarAPI", fn_name, f"Token refresh FAILED for {self.user_id} (RefreshError): {refresh_err}. Re-authentication required.", refresh_err)
                        if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                        token_file_path = os.path.join("data", "tokens", f"tokens_{self.user_id}{os.getenv('DATA_SUFFIX', '')}.json.enc")
                        if os.path.exists(token_file_path):
                            log_warning("GoogleCalendarAPI", fn_name, f"Deleting invalid token file due to refresh failure: {token_file_path}")
                            try: os.remove(token_file_path)
                            except OSError as rm_err: log_error("GoogleCalendarAPI", fn_name, f"Failed to remove token file: {rm_err}")
                        return None
                    except ImportError as imp_err:
                        log_error("GoogleCalendarAPI", fn_name, f"Import error during refresh for {self.user_id}: {imp_err}")
                        if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                        return None
                    except Exception as refresh_err_unexp:
                        log_error("GoogleCalendarAPI", fn_name, f"Unexpected error during token refresh for {self.user_id}: {refresh_err_unexp}", refresh_err_unexp)
                        if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                        return None
                else:
                    log_error("GoogleCalendarAPI", fn_name, f"Credentials invalid for {self.user_id}, and no refresh token available. Re-authentication needed.")
                    if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                    return None

            if creds and creds.valid:
                # log_info("GoogleCalendarAPI", fn_name, f"Credentials loaded and valid for {self.user_id}.") # Verbose
                return creds
            else:
                log_error("GoogleCalendarAPI", fn_name, f"Failed to obtain valid credentials for {self.user_id} after potential refresh.")
                if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                return None
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Error creating/validating credentials object for {self.user_id}: {e}", e)
            if CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
            return None

    def is_active(self):
        return self.service is not None

    def create_event(self, event_data: Dict) -> str | None:
        fn_name = "create_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}.")
            # No status change here as it's likely already 'error' or 'not_integrated' if service isn't active
            return None
        assert self.service is not None

        try:
            # ... (event parsing logic remains the same as before) ...
            event_date = event_data.get('date'); event_time = event_data.get('time'); duration_minutes = None
            if event_data.get('duration'):
                try:
                    duration_str = str(event_data['duration']).lower().replace(' ',''); total_minutes = 0.0
                    hour_match = re.search(r'(\d+(\.\d+)?)\s*h', duration_str); minute_match = re.search(r'(\d+)\s*m', duration_str)
                    if hour_match: total_minutes += float(hour_match.group(1)) * 60
                    if minute_match: total_minutes += int(minute_match.group(1))
                    if total_minutes == 0 and hour_match is None and minute_match is None:
                        if duration_str.replace('.','',1).isdigit(): total_minutes = float(duration_str)
                        else: raise ValueError("Unrecognized duration format")
                    duration_minutes = int(round(total_minutes)) if total_minutes > 0 else None
                except (ValueError, TypeError, AttributeError): duration_minutes = 30
            if not event_date: return None
            start_obj = {}; end_obj = {}; time_zone = self.user_timezone
            if event_time:
                try:
                    start_dt = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
                    delta = timedelta(minutes=duration_minutes if duration_minutes is not None else 30)
                    end_dt = start_dt + delta
                    start_obj = {"dateTime": start_dt.isoformat(), "timeZone": time_zone}
                    end_obj = {"dateTime": end_dt.isoformat(), "timeZone": time_zone}
                except ValueError as time_err: log_error("GoogleCalendarAPI", fn_name, f"Invalid date/time format: {event_date} {event_time}", time_err); return None
            else:
                try:
                    start_dt_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                    end_date_dt = start_dt_date + timedelta(days=1)
                    start_obj = {"date": start_dt_date.strftime("%Y-%m-%d")}
                    end_obj = {"date": end_date_dt.strftime("%Y-%m-%d")}
                except ValueError as date_err: log_error("GoogleCalendarAPI", fn_name, f"Invalid date format '{event_date}' for all-day event", date_err); return None
            google_event_body = {
                "summary": event_data.get("title", event_data.get("description", "New Item")),
                "description": event_data.get("description", ""), "start": start_obj, "end": end_obj
            }
            # log_info("GoogleCalendarAPI", fn_name, f"Creating GCal event for user {self.user_id}: {google_event_body.get('summary')}") # Verbose
            created_event = self.service.events().insert(calendarId='primary', body=google_event_body).execute()
            google_event_id = created_event.get("id")
            if google_event_id: return google_event_id
            else: log_error("GoogleCalendarAPI", fn_name, f"GCal API response missing 'id'. Response: {created_event}"); return None
        except HttpError as http_err:
            log_error("GoogleCalendarAPI", fn_name, f"HTTP error creating event for user {self.user_id}: Status {http_err.resp.status}", http_err)
            if http_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: # Unauthorized or Forbidden
                _set_gcal_status_func(self.user_id, "error")
            return None
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error creating event for user {self.user_id}", e)
            return None

    def update_event(self, event_id: str, updates: Dict) -> bool:
        fn_name = "update_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot update event {event_id}.")
            return False
        assert self.service is not None
        try:
            # ... (existing logic to fetch event and prepare update_payload remains the same) ...
            try: existing_event = self.service.events().get(calendarId='primary', eventId=event_id).execute()
            except HttpError as get_err:
                if get_err.resp.status == 404: return False # Event not found
                log_error("GoogleCalendarAPI", fn_name, f"HTTP error getting event {event_id} before update: {get_err}", get_err)
                if get_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: _set_gcal_status_func(self.user_id, "error")
                return False
            update_payload = {}; needs_update = False; time_zone = self.user_timezone
            if "title" in updates: update_payload["summary"] = updates["title"]; needs_update = True
            if "description" in updates: update_payload["description"] = updates["description"]; needs_update = True
            new_date_str = updates.get("date"); new_time_str = updates.get("time")
            if new_date_str is not None or "time" in updates:
                current_start_info = existing_event.get('start', {}); current_end_info = existing_event.get('end', {})
                is_currently_all_day = 'date' in current_start_info and 'dateTime' not in current_start_info
                target_date_str = new_date_str
                if target_date_str is None:
                    if is_currently_all_day: target_date_str = current_start_info.get('date')
                    elif current_start_info.get('dateTime'):
                        try: target_date_str = datetime.fromisoformat(current_start_info['dateTime']).strftime('%Y-%m-%d')
                        except ValueError: target_date_str = None
                    else: target_date_str = None
                target_time_str = new_time_str if "time" in updates else (datetime.fromisoformat(current_start_info['dateTime']).strftime('%H:%M') if current_start_info.get('dateTime') and not is_currently_all_day else None)
                if target_date_str:
                    if target_time_str:
                        try:
                            start_dt = datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
                            duration = timedelta(minutes=30)
                            if current_start_info.get('dateTime') and current_end_info.get('dateTime'):
                                try: duration = datetime.fromisoformat(current_end_info['dateTime']) - datetime.fromisoformat(current_start_info['dateTime'])
                                except ValueError: pass
                            end_dt = start_dt + duration
                            update_payload["start"] = {"dateTime": start_dt.isoformat(), "timeZone": time_zone}
                            update_payload["end"] = {"dateTime": end_dt.isoformat(), "timeZone": time_zone}
                            needs_update = True
                        except ValueError as e: log_error("GoogleCalendarAPI", fn_name, f"Invalid date/time '{target_date_str} {target_time_str}' on update: {e}")
                    else:
                        try:
                            start_dt_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                            end_date_dt = start_dt_date + timedelta(days=1)
                            update_payload["start"] = {"date": start_dt_date.strftime("%Y-%m-%d")}
                            update_payload["end"] = {"date": end_date_dt.strftime("%Y-%m-%d")}
                            if 'dateTime' in update_payload.get("start",{}): del update_payload["start"]["dateTime"]
                            if 'dateTime' in update_payload.get("end",{}): del update_payload["end"]["dateTime"]
                            needs_update = True
                        except ValueError as e: log_error("GoogleCalendarAPI", fn_name, f"Invalid date '{target_date_str}' for all-day update: {e}")
            if not needs_update: return True
            # log_info("GoogleCalendarAPI", fn_name, f"Patching GCal event {event_id} for user {self.user_id}. Fields: {list(update_payload.keys())}") # Verbose
            self.service.events().patch(calendarId='primary', eventId=event_id, body=update_payload).execute()
            return True
        except HttpError as http_err:
            log_error("GoogleCalendarAPI", fn_name, f"HTTP error updating event {event_id}: Status {http_err.resp.status}", http_err)
            if http_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func:
                _set_gcal_status_func(self.user_id, "error")
            return False
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error updating event {event_id}", e)
            return False

    def delete_event(self, event_id: str) -> bool:
        fn_name = "delete_event"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot delete event {event_id}.")
            return False
        assert self.service is not None
        try:
            # log_info("GoogleCalendarAPI", fn_name, f"Attempting delete GCal event {event_id} for user {self.user_id}") # Verbose
            self.service.events().delete(calendarId='primary', eventId=event_id, sendNotifications=False).execute()
            return True
        except HttpError as http_err:
            if http_err.resp.status in [404, 410]:
                log_warning("GoogleCalendarAPI", fn_name, f"GCal event {event_id} not found or already gone (Status {http_err.resp.status}). Assuming deleted.")
                return True
            else:
                log_error("GoogleCalendarAPI", fn_name, f"HTTP error deleting event {event_id}: Status {http_err.resp.status}", http_err)
                if http_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func:
                    _set_gcal_status_func(self.user_id, "error")
                return False
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error deleting event {event_id}", e)
            return False

    def list_events(self, start_date: str, end_date: str) -> List[Dict]:
        fn_name = "list_events"
        if not self.is_active():
            log_error("GoogleCalendarAPI", fn_name, f"Service not active for user {self.user_id}, cannot list events.")
            return []
        assert self.service is not None
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt_exclusive = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            time_min = start_dt.isoformat() + "Z"; time_max = end_dt_exclusive.isoformat() + "Z"
        except ValueError as date_err:
            log_error("GoogleCalendarAPI", fn_name, f"Invalid date format for listing events: {start_date} / {end_date}", date_err)
            return []
        try:
            all_items = []; page_token = None
            while True:
                events_result = self.service.events().list(
                    calendarId='primary', timeMin=time_min, timeMax=time_max, maxResults=250,
                    singleEvents=True, orderBy='startTime', pageToken=page_token
                ).execute()
                items = events_result.get("items", []); all_items.extend(items)
                page_token = events_result.get('nextPageToken')
                if not page_token: break
            # log_info("GoogleCalendarAPI", fn_name, f"Found {len(all_items)} GCal events for user {self.user_id} in range.") # Verbose
            return [self._parse_google_event(e) for e in all_items]
        except HttpError as http_err:
            log_error("GoogleCalendarAPI", fn_name, f"HTTP error listing events for user {self.user_id}: Status {http_err.resp.status}", http_err)
            if http_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: # Unauthorized or Forbidden
                _set_gcal_status_func(self.user_id, "error")
            return []
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error listing events for user {self.user_id}", e)
            return []

    def _get_single_event(self, event_id: str) -> Dict | None:
        fn_name = "_get_single_event"
        if not self.is_active(): return None
        assert self.service is not None
        try:
            event = self.service.events().get(calendarId='primary', eventId=event_id).execute()
            return event
        except HttpError as http_err:
            if http_err.resp.status in [404, 410]: return None
            else:
                log_error("GoogleCalendarAPI", fn_name, f"HTTP error getting event {event_id}: Status {http_err.resp.status}", http_err)
                if http_err.resp.status in [401, 403] and CONFIG_MANAGER_IMPORTED and _set_gcal_status_func: # Unauthorized or Forbidden
                     _set_gcal_status_func(self.user_id, "error")
                return None
        except Exception as e:
            log_error("GoogleCalendarAPI", fn_name, f"Unexpected error getting event {event_id}", e)
            return None

    def _parse_google_event(self, event: Dict) -> Dict:
        start_info = event.get("start", {}); end_info = event.get("end", {})
        start_datetime_str = start_info.get("dateTime", start_info.get("date"))
        end_datetime_str = end_info.get("dateTime", end_info.get("date"))
        is_all_day = "date" in start_info and "dateTime" not in start_info
        parsed = {
            "event_id": event.get("id"), "title": event.get("summary", ""),
            "description": event.get("description", ""),
            "gcal_start_datetime": start_datetime_str, "gcal_end_datetime": end_datetime_str,
            "is_all_day": is_all_day, "gcal_link": event.get("htmlLink", ""),
            "status_gcal": event.get("status", ""), "created_gcal": event.get("created"),
            "updated_gcal": event.get("updated"),
        }
        return parsed

# --- END OF FULL tools/google_calendar_api.py ---