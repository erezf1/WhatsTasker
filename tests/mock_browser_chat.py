# --- START OF FULL tests/mock_browser_chat.py (SIMPLIFIED & MORE ROBUST POLLING) ---

import os
import requests
import json
import time
import threading
from flask import Flask, render_template, request, jsonify
from collections import deque
from datetime import datetime
from dotenv import load_dotenv
import logging # Keep logging import
import traceback # For detailed error logging

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
VIEWER_PORT = int(os.getenv("VIEWER_PORT", "5001"))
MAX_MESSAGES = 100

# --- Main Backend Configuration ---
MAIN_BACKEND_PORT = os.getenv("PORT", "8001")
MAIN_BACKEND_BASE_URL = f"http://localhost:{MAIN_BACKEND_PORT}"
MAIN_BACKEND_INCOMING_URL = f"{MAIN_BACKEND_BASE_URL}/incoming"
MAIN_BACKEND_OUTGOING_URL = f"{MAIN_BACKEND_BASE_URL}/outgoing"
MAIN_BACKEND_ACK_URL = f"{MAIN_BACKEND_BASE_URL}/ack"

MOCK_USER_ID = "1234" # Default value, overridden at startup

message_store_bot = deque(maxlen=MAX_MESSAGES)
message_lock = threading.Lock()
_stop_polling_event = threading.Event()

script_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(script_dir, 'templates')
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

def mock_log(level, component, message, error=None, exc_info=False):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    log_entry = f"[{timestamp}] [{level.upper()}] [MockChat:{component}] {message}"
    if error:
        log_entry += f" | Error: {str(error)}"
        if exc_info:
            log_entry += f"\n{traceback.format_exc()}" # Log full traceback
    print(log_entry)

# --- Background Polling Function (REVISED FOR SIMPLICITY & ROBUSTNESS) ---
def poll_main_backend():
    component_name = "PollingThread"
    mock_log("info", component_name, f"STARTED. Target: {MAIN_BACKEND_OUTGOING_URL}, For User: {MOCK_USER_ID}")
    session = requests.Session()
    
    poll_interval_seconds = 0.75 # Simple fixed interval for testing

    while not _stop_polling_event.is_set():
        try:
            #mock_log("debug", component_name, f"Polling {MAIN_BACKEND_OUTGOING_URL}...")
            
            res = session.get(MAIN_BACKEND_OUTGOING_URL, timeout=10) # Increased timeout slightly for GET
            
            # Log basic response info regardless of status for debugging
            #mock_log("debug", component_name, f"Poll response status: {res.status_code}. Raw text (first 200): '{res.text[:200]}'")

            if res.status_code == 200:
                try:
                    data = res.json()
                    all_backend_messages = data.get("messages", [])

                    if all_backend_messages:
                        mock_log("info", component_name, f"Received {len(all_backend_messages)} message(s) in total from backend's queue.")
                        
                        user_specific_messages = [
                            msg for msg in all_backend_messages
                            if msg.get('user_id') == MOCK_USER_ID
                        ]

                        if user_specific_messages:
                            mock_log("info", component_name, f"Found {len(user_specific_messages)} message(s) FOR MOCK_USER_ID '{MOCK_USER_ID}'.")
                            
                            for msg_data in reversed(user_specific_messages):
                                message_content = msg_data.get('message', '[No message content]')
                                message_id = msg_data.get('message_id', f'mock-fallbackid-{time.time_ns()}')
                                
                                with message_lock:
                                    message_store_bot.appendleft({
                                        "sender": "bot",
                                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                                        "content": message_content,
                                        "id": message_id
                                    })
                                mock_log("info", component_name, f"Added to UI store: MsgID {message_id}, Content: '{message_content[:60]}...'")
                                
                                # Send ACK
                                try:
                                    ack_payload = {"message_id": message_id, "user_id": msg_data.get("user_id")}
                                    mock_log("debug", component_name, f"Sending ACK for MsgID: {message_id} to {MAIN_BACKEND_ACK_URL}")
                                    session.post(MAIN_BACKEND_ACK_URL, json=ack_payload, timeout=5) # ACK timeout
                                except Exception as ack_e:
                                    mock_log("error", component_name, f"Error sending ACK for {message_id}", error=ack_e) # Log ACK error but continue
                        elif all_backend_messages : # messages present, but not for this user
                             mock_log("debug", component_name, f"Messages in queue, but none for user {MOCK_USER_ID}.")
                    # else: No messages in queue (data.get("messages") was empty or not present)
                    #      mock_log("debug", component_name, "No messages in backend queue this poll.")

                except json.JSONDecodeError as e_json:
                    mock_log("error", component_name, f"JSONDecodeError parsing response from {MAIN_BACKEND_OUTGOING_URL}. Status was {res.status_code}.", error=e_json)
                except Exception as e_proc: # Catch other errors during processing of successful response
                    mock_log("error", component_name, f"Error processing successful poll response (status {res.status_code})", error=e_proc, exc_info=True)
            
            elif res.status_code >= 400: # Handle HTTP errors explicitly
                 mock_log("warning", component_name, f"Poll returned HTTP error: {res.status_code}. Response: {res.text[:200]}")

        except requests.exceptions.RequestException as e_req: # Covers Timeout, ConnectionError, etc.
            mock_log("warning", component_name, f"RequestException during poll to {MAIN_BACKEND_OUTGOING_URL}", error=e_req)
        except Exception as e_outer: # Catch-all for any other unexpected error in the loop
            # This is crucial: if an unexpected error happens, log it and continue polling
            # instead of letting the thread die silently.
            mock_log("critical", component_name, "UNEXPECTED CRITICAL ERROR in polling loop, will try to continue.", error=e_outer, exc_info=True)
        
        # Wait before next poll, stoppable by the event
        if _stop_polling_event.wait(timeout=poll_interval_seconds):
            break # Event was set, stop polling

    mock_log("info", component_name, "STOPPED.")


# --- Flask Routes (largely unchanged, but /send_message timeout increased) ---
@app.route('/')
def index():
    return render_template('browser_chat.html', title=f"WhatsTasker Mock Chat (User: {MOCK_USER_ID})")

@app.route('/send_message', methods=['POST'])
def send_message_route():
    component_name = "FlaskRoute"
    try:
        data = request.get_json()
        if not data :
             mock_log("warning", component_name, "/send_message: Received empty JSON payload.")
             return jsonify({"status": "error", "message": "No JSON data"}), 400
        message_text = data.get('message')
        if not message_text: 
            mock_log("warning", component_name, "/send_message: Received empty message content.")
            return jsonify({"status": "error", "message": "No message content"}), 400

        user_id_to_send = MOCK_USER_ID 
        mock_log("info", component_name, f"/send_message: Forwarding from '{user_id_to_send}': '{message_text[:50]}...' to {MAIN_BACKEND_INCOMING_URL}")

        backend_payload = {"user_id": user_id_to_send, "message": message_text}
        backend_timeout_seconds = 120 # Increased timeout to 2 minutes for backend processing

        try:
            response = requests.post(MAIN_BACKEND_INCOMING_URL, json=backend_payload, timeout=backend_timeout_seconds)
            
            # Log backend response details even if it's not 2xx
            mock_log("debug", component_name, f"/send_message: Backend response Status: {response.status_code}, Text: {response.text[:200]}")
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            
            # Optional: Check for specific "ack" in JSON if backend guarantees it on 200 OK
            # try:
            #     ack_data = response.json()
            #     if not ack_data.get("ack"): 
            #         mock_log("warning", component_name, f"/send_message: Main backend success response missing 'ack'.")
            # except json.JSONDecodeError:
            #     mock_log("warning", component_name, f"/send_message: Main backend success response not valid JSON.")
            
            return jsonify({"status": "ok", "message": "Forwarded to backend"}), 200

        except requests.exceptions.Timeout: 
            mock_log("error", component_name, f"/send_message: Timeout ({backend_timeout_seconds}s) sending to {MAIN_BACKEND_INCOMING_URL}"); 
            return jsonify({"status": "error", "message": f"Timeout sending to backend. It might be busy."}), 504 # Gateway Timeout
        except requests.exceptions.ConnectionError: 
            mock_log("error", component_name, f"/send_message: Connection refused by {MAIN_BACKEND_INCOMING_URL}. Is backend running?"); 
            return jsonify({"status": "error", "message": f"Connection refused by backend."}), 503 # Service Unavailable
        except requests.exceptions.HTTPError as e_http_send:
             mock_log("error", component_name, f"/send_message: Backend returned HTTP error.", error=e_http_send); 
             return jsonify({"status": "error", "message": f"Backend error: {e_http_send.response.status_code}"}), e_http_send.response.status_code
        except requests.exceptions.RequestException as e_req: 
            mock_log("error", component_name, f"/send_message: RequestException forwarding to backend", error=e_req, exc_info=True); 
            return jsonify({"status": "error", "message": f"Network error: {str(e_req)}"}), 500

    except Exception as e_flask: 
        mock_log("error", component_name, "/send_message: Unexpected error in route", error=e_flask, exc_info=True); 
        return jsonify({"status": "error", "message": "Internal mock server error"}), 500

@app.route('/get_messages')
def get_messages_route():
    with message_lock:
        bot_messages_for_display = list(message_store_bot) 
    mock_log("debug", "FlaskRoute", f"/get_messages: Returning {len(bot_messages_for_display)} bot messages to UI.")
    return jsonify({"messages": bot_messages_for_display})

@app.route('/clear_messages', methods=['POST'])
def clear_messages_route():
    with message_lock:
        message_store_bot.clear()
    mock_log("info", "FlaskRoute", "/clear_messages: Browser chat BOT messages cleared on server.")
    return jsonify({"status": "ok"}), 200

# --- Main Execution ---
if __name__ == '__main__':
    mock_log("info", "Main", "--- Starting WhatsTasker Mock Browser Chat Interface ---")
    try:
        user_input_id_raw = input(f"Enter User ID to simulate (leave blank for default '{MOCK_USER_ID}'): ").strip()
        if user_input_id_raw:
            MOCK_USER_ID = user_input_id_raw
        mock_log("info", "Main", f"Simulating as User ID: {MOCK_USER_ID}")
    except Exception as e_input_main:
        mock_log("error", "Main", f"Failed to get user input for ID, using default '{MOCK_USER_ID}'. Error: {e_input_main}")

    mock_log("info", "Main", f"Serving chat UI on: http://localhost:{VIEWER_PORT} (for User ID: {MOCK_USER_ID})")
    mock_log("info", "Main", f"Communicating with Main Backend at: {MAIN_BACKEND_BASE_URL}")
    mock_log("info", "Main", "-----------------------------------------------------")

    polling_thread = threading.Thread(target=poll_main_backend, name="MockChatPollingThread", daemon=True)
    polling_thread.start()

    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    try:
        app.run(host='0.0.0.0', port=VIEWER_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        mock_log("info", "Main","Ctrl+C received, initiating shutdown...")
    except Exception as e_flask_run_main:
        mock_log("critical", "Main", "Flask server crashed or failed to start", error=e_flask_run_main, exc_info=True)
    finally:
        mock_log("info", "Main", "Signaling polling thread to stop...")
        _stop_polling_event.set()
        polling_thread.join(timeout=3) # Give thread a moment to exit
        if polling_thread.is_alive():
            mock_log("warning", "Main", "Polling thread did not stop cleanly.")
        else:
            mock_log("info", "Main", "Polling thread stopped.")
        mock_log("info", "Main", "Mock browser chat server has shut down.")

# --- END OF FULL tests/mock_browser_chat.py (SIMPLIFIED & MORE ROBUST POLLING) ---