# --- START OF FULL tests/mock_browser_chat.py ---
# Simplified version using only print() for errors/info

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

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
VIEWER_PORT = int(os.getenv("VIEWER_PORT", "5001"))
MAX_MESSAGES = 100

# --- Main Backend Configuration ---
MAIN_BACKEND_PORT = os.getenv("PORT", "8001") # Read from PORT env var like main.py
MAIN_BACKEND_BASE_URL = f"http://localhost:{MAIN_BACKEND_PORT}"
MAIN_BACKEND_INCOMING_URL = f"{MAIN_BACKEND_BASE_URL}/incoming"
MAIN_BACKEND_OUTGOING_URL = f"{MAIN_BACKEND_BASE_URL}/outgoing"
MAIN_BACKEND_ACK_URL = f"{MAIN_BACKEND_BASE_URL}/ack"

# --- Mock User ID (Will be updated by user input) ---
MOCK_USER_ID = "1234" # Default value

# --- In-memory message store (bot messages only) ---
message_store_bot = deque(maxlen=MAX_MESSAGES)
message_lock = threading.Lock()
_stop_polling_event = threading.Event()

# --- Flask App Setup ---
script_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(script_dir, 'templates')
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

# --- Background Polling Function (Simplified Output) ---
def poll_main_backend():
    """Polls the main backend for outgoing messages and sends ACKs. Prints only errors."""
    print(f"[Polling Thread] Started. Target: {MAIN_BACKEND_OUTGOING_URL}")
    session = requests.Session()
    connection_lost = False
    last_successful_poll = time.time()

    while not _stop_polling_event.is_set():
        try:
            res = session.get(MAIN_BACKEND_OUTGOING_URL, timeout=10)
            res.raise_for_status()
            if connection_lost: print("[Polling Thread] Connection restored."); connection_lost = False
            last_successful_poll = time.time()
            data = res.json()
            messages = data.get("messages", [])

            if messages:
                timestamp = datetime.now().strftime("%H:%M:%S")
                with message_lock:
                    # Filter messages for the MOCK_USER_ID before processing
                    user_messages = [
                        msg for msg in messages
                        if msg.get('user_id') == MOCK_USER_ID # Check if message is for our mock user
                    ]

                    if user_messages:
                        # Add only the relevant messages to the display store
                        for msg in reversed(user_messages):
                            message_content = msg.get('message', '[No message content]')
                            message_id = msg.get('message_id', f'nomockid-{time.time()}')
                            message_store_bot.append({
                                "sender": "bot", "timestamp": timestamp,
                                "content": message_content, "id": message_id
                            })
                            # Send ACK for the processed message
                            try:
                                ack_payload = {"message_id": message_id, "user_id": msg.get("user_id")}
                                ack_res = session.post(MAIN_BACKEND_ACK_URL, json=ack_payload, timeout=3)
                                if ack_res.status_code != 200:
                                    print(f"[Polling Thread WARNING] Failed ACK for {message_id}. Status: {ack_res.status_code}")
                            except Exception as ack_e:
                                print(f"[Polling Thread ERROR] Error sending ACK for {message_id}: {ack_e}")
                    # else: No messages specifically for this MOCK_USER_ID in this poll

            # Reset connection_lost flag if request was successful (even if no messages)
            if not connection_lost and res.status_code == 200:
                 pass # Already reset above if connection_lost was true

        except requests.exceptions.Timeout:
            if not connection_lost and (time.time() - last_successful_poll > 30):
                print(f"[Polling Thread WARNING] Connection lost? Repeated timeouts polling {MAIN_BACKEND_OUTGOING_URL}.")
                connection_lost = True
        except requests.exceptions.RequestException as e:
             error_msg = f"Connection error polling {MAIN_BACKEND_OUTGOING_URL}: {e}"
             if isinstance(e, requests.exceptions.ConnectionError) and "actively refused it" in str(e).lower():
                  error_msg = f"Connection error polling {MAIN_BACKEND_OUTGOING_URL}: Target refused connection. Is main backend running?"
             if not connection_lost: print(f"[Polling Thread ERROR] {error_msg}"); connection_lost = True
        except json.JSONDecodeError as e:
             response_text_snippet = res.text[:100] if 'res' in locals() else 'N/A'
             print(f"[Polling Thread ERROR] Failed JSON decode from {MAIN_BACKEND_OUTGOING_URL}. Response: {response_text_snippet}. Error: {e}")
        except Exception as e:
            print(f"[Polling Thread ERROR] Unexpected error: {e}")
            connection_lost = True
        finally:
            sleep_time = 0.5 if not connection_lost else 2.0
            # Use max to prevent negative sleep times if system clock changes
            time.sleep(max(0, sleep_time))


    print("[Polling Thread] Stopped.")


# --- Flask Routes (Simplified Output) ---
@app.route('/')
def index():
    # Use the globally set MOCK_USER_ID for the title
    return render_template('browser_chat.html', title=f"WhatsTasker Chat (User: {MOCK_USER_ID})")

@app.route('/send_message', methods=['POST'])
def send_message():
    """Receives message from browser, forwards to main backend using the current MOCK_USER_ID."""
    try:
        data = request.get_json()
        message_text = data.get('message')
        if not message_text: return jsonify({"status": "error", "message": "No message content"}), 400

        # Use the globally set MOCK_USER_ID when sending
        user_id_to_send = MOCK_USER_ID
        # print(f"Forwarding message from {user_id_to_send}: {message_text[:50]}...") # Removed info print

        backend_payload = {"user_id": user_id_to_send, "message": message_text}
        backend_timeout = 60

        try:
            response = requests.post(MAIN_BACKEND_INCOMING_URL, json=backend_payload, timeout=backend_timeout)
            response.raise_for_status()
            try:
                ack_data = response.json();
                if not ack_data.get("ack"): print(f"[Flask WARNING] Main backend response ({response.status_code}) missing ACK.")
            except ValueError: print(f"[Flask WARNING] Main backend response ({response.status_code}) not JSON.")
            return jsonify({"status": "ok", "message": "Forwarded to backend"}), 200

        except requests.exceptions.Timeout: print(f"[Flask ERROR] Timeout sending to {MAIN_BACKEND_INCOMING_URL}"); return jsonify({"status": "error", "message": f"Timeout sending to backend"}), 503
        except requests.exceptions.ConnectionError: print(f"[Flask ERROR] Conn refused by {MAIN_BACKEND_INCOMING_URL}"); return jsonify({"status": "error", "message": f"Connection refused by backend"}), 503
        except requests.exceptions.RequestException as e: print(f"[Flask ERROR] Failed forward to backend: {e}"); return jsonify({"status": "error", "message": f"Failed to forward: {e}"}), 500

    except Exception as e: print(f"[Flask ERROR] Error in /send_message: {e}"); return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_messages')
def get_messages():
    """Provides ONLY BOT messages to the frontend."""
    with message_lock:
        bot_messages = list(message_store_bot)
    sorted_bot_messages = sorted(bot_messages, key=lambda x: x.get('timestamp', ''))
    return jsonify({"messages": sorted_bot_messages})

@app.route('/clear_messages', methods=['POST'])
def clear_messages():
    """Clears the server's BOT message store."""
    with message_lock:
        message_store_bot.clear()
    print("[Flask INFO] Browser chat BOT messages cleared on server.")
    return jsonify({"status": "ok"}), 200

# --- Main Execution ---
if __name__ == '__main__':
    # --- *** ADDED User ID Prompt *** ---
    print("--- Starting Mock Browser Chat Interface ---")
    try:
        user_input_id = input(f"Enter User ID to simulate (leave blank for default '{MOCK_USER_ID}'): ")
        if user_input_id.strip():
            MOCK_USER_ID = user_input_id.strip() # Use user input if provided
        else:
            # Default is already set, no action needed, but log it
            print(f"Using default User ID: {MOCK_USER_ID}")
            pass
    except Exception as e:
        print(f"[ERROR] Failed to get user input for ID, using default '{MOCK_USER_ID}'. Error: {e}")
    # --- *** END User ID Prompt *** ---

    print(f"Serving chat UI on: http://localhost:{VIEWER_PORT}")
    print(f"Acting as User ID:  {MOCK_USER_ID}") # Display the chosen ID
    print(f"Talking to Backend: {MAIN_BACKEND_INCOMING_URL}")
    print(f"Polling Backend at: {MAIN_BACKEND_OUTGOING_URL}")
    print(f"--------------------------------------------")

    polling_thread = threading.Thread(target=poll_main_backend, daemon=True)
    polling_thread.start()

    try:
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.WARNING)
        app.run(host='0.0.0.0', port=VIEWER_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nCtrl+C received, shutting down...")
    except Exception as e:
        print(f"[ERROR] Flask server crashed: {e}")
    finally:
        _stop_polling_event.set()
        print("Waiting for polling thread to stop...")
        polling_thread.join(timeout=2)
        print("Mock browser chat server stopped.")

# --- END OF FULL tests/mock_browser_chat.py ---