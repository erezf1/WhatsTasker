# --- START of tests/mock_sender.py ---
import requests
import sys
import os
import json
import time
import threading
import re # <-- Added for regex check
from datetime import datetime # <-- Added for timestamp in log file
from dotenv import load_dotenv # <-- Added for .env loading

# --- Load Environment Variables ---
load_dotenv() # Load .env file content

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Use the project logger, but primarily for errors/warnings in this script
try:
    from tools.logger import log_info, log_error, log_warning
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    log_info=logging.info; log_error=logging.error; log_warning=logging.warning
    log_error("mock_sender", "import", "Failed to import main logger.")

# --- Configuration ---
DEFAULT_USER = "1234" # Example default user for prompt

# Read PORT from .env, default to 8001 for CLI testing if not set
CLI_PORT = os.getenv("PORT", "8001") # Default port for CLI dev environment
BASE_URL = f"http://localhost:{CLI_PORT}" # Construct BASE_URL using the port

INCOMING_URL = f"{BASE_URL}/incoming"
OUTGOING_URL = f"{BASE_URL}/outgoing"
ACK_URL = f"{BASE_URL}/ack"

# --- Log file for raw bot responses ---
LOG_DIR = "logs"
BOT_RESPONSE_LOG_FILE = os.path.join(LOG_DIR, "mock_sender_bot_responses.log")

# Ensure log directory exists
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError as e:
    print(f"WARNING: Could not create log directory '{LOG_DIR}'. Error: {e}")

# --- Global State ---
_stop_polling = threading.Event()

# --- Function to log raw bot messages ---
def log_bot_response(user_id: str, message_id: str, message_content: str):
    """Appends raw bot message details to a log file."""
    try:
        timestamp = datetime.now().isoformat()
        log_entry = {
            "timestamp": timestamp,
            "user_id_received_for": user_id,
            "message_id": message_id,
            "content": message_content
        }
        with open(BOT_RESPONSE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log_warning("mock_sender", "log_bot_response", f"Failed to write bot response to log file: {e}")

# --- Polling Function ---
def poll_for_messages(user_id_raw: str):
    log_info("mock_sender", "poll_thread", f"Polling thread started for user {user_id_raw}. Target: {BASE_URL}")
    session = requests.Session()
    connection_lost = False

    while not _stop_polling.is_set():
        try:
            res = session.get(OUTGOING_URL, timeout=10)
            res.raise_for_status()

            if connection_lost:
                print("[SYSTEM]: Connection to server restored.")
                log_warning("mock_sender", "poll_thread", "Connection restored.")
                connection_lost = False

            data = res.json()
            messages = data.get("messages", [])

            if messages:
                for msg in messages:
                    bot_message_content = msg.get('message', '[No message content]')
                    bot_message_id = msg.get('message_id', 'UNKNOWN_ID')
                    log_bot_response(user_id_raw, bot_message_id, bot_message_content)

                    is_hebrew = bool(re.search(r'[\u0590-\u05FF]', bot_message_content))
                    rtl_mark = '\u202B'
                    display_prefix = "\n[BOT]: "
                    display_message = f"{rtl_mark}{bot_message_content}" if is_hebrew else bot_message_content
                    print(f"{display_prefix}{display_message}")

                    try:
                        ack_payload = {"message_id": bot_message_id, "user_id": msg.get("user_id")}
                        ack_res = session.post(ACK_URL, json=ack_payload, timeout=5)
                        if ack_res.status_code != 200:
                             log_warning("mock_sender", "poll_thread", f"Failed ACK for {bot_message_id}. Status: {ack_res.status_code}")
                    except Exception as ack_e:
                         log_warning("mock_sender", "poll_thread", f"Error sending ACK for {bot_message_id}: {ack_e}")

                print(f"[YOU]: ", end="", flush=True)

        except requests.exceptions.Timeout:
             if not connection_lost:
                 log_warning("mock_sender", "poll_thread", f"Polling request timed out ({OUTGOING_URL}).")
                 print("[SYSTEM]: Polling timed out...")
                 connection_lost = True
             time.sleep(2)
        except requests.exceptions.RequestException as e:
             if not connection_lost:
                 error_msg = f"Connection error polling {OUTGOING_URL}: {e}"
                 if isinstance(e, requests.exceptions.ConnectionError) and "actively refused it" in str(e).lower():
                      error_msg = f"Connection error polling {OUTGOING_URL}: Target actively refused connection. Is the server running on {BASE_URL}?"
                 log_error("mock_sender", "poll_thread", error_msg)
                 print(f"[SYSTEM]: {error_msg}")
                 connection_lost = True
             time.sleep(5)
        except json.JSONDecodeError as e:
             log_error("mock_sender", "poll_thread", f"Failed to decode JSON response from {OUTGOING_URL}: {e}. Response text: {res.text[:100] if 'res' in locals() else 'N/A'}")
             print("[SYSTEM]: Received invalid response from server.")
             time.sleep(2)
        except Exception as e:
             log_error("mock_sender", "poll_thread", f"Unexpected error in polling thread: {e}", e)
             if not connection_lost:
                 print(f"[SYSTEM]: Unexpected polling error: {e}")
                 connection_lost = True
             time.sleep(5)

        if not connection_lost:
            time.sleep(0.3)
        else:
            time.sleep(1.0)

    log_info("mock_sender", "poll_thread", "Polling thread stopped.")

# --- Sending Function ---
def send_mock_message(user_id_raw: str, message: str):
    """Sends message, expects only ACK back directly."""
    payload = {"user_id": user_id_raw, "message": message}
    ack_timeout = 60

    try:
        res = requests.post(INCOMING_URL, json=payload, timeout=ack_timeout)
        if res.status_code >= 400:
             log_error("mock_sender", "send_mock_message", f"Server returned error status {res.status_code} from {INCOMING_URL}. Response: {res.text[:200]}")
             print(f"[SYSTEM ERROR]: Server returned status {res.status_code}. Check backend logs.")
             return
        response_data = res.json()
        if not response_data.get("ack"):
             log_warning("mock_sender", "send_mock_message", f"Server response OK status ({res.status_code}) but ACK missing from {INCOMING_URL}: {response_data}")
             print(f"[SYSTEM WARNING]: Server ACK missing in response.")
    except requests.exceptions.ReadTimeout:
        log_warning("mock_sender", "send_mock_message", f"ACK response from {INCOMING_URL} timed out after {ack_timeout}s (message likely sent, server processing).")
        print(f"[SYSTEM]: Server took too long to acknowledge message (>{ack_timeout}s), but it was likely sent. Check for BOT response.")
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Connection error sending to {INCOMING_URL}: {e}"
        if "actively refused it" in str(e).lower():
             error_msg = f"Connection error sending to {INCOMING_URL}: Target actively refused connection. Is the server running on {BASE_URL}?"
        log_error("mock_sender", "send_mock_message", error_msg, e)
        print(f"[SYSTEM]: {error_msg}")
    except requests.exceptions.RequestException as e:
        log_error("mock_sender", "send_mock_message", f"Failed to send message to {INCOMING_URL} due to request error", e)
        print(f"[SYSTEM]: Error sending message: {e}")
    except json.JSONDecodeError:
        log_error("mock_sender", "send_mock_message", f"Received non-JSON ACK response from {INCOMING_URL} (Status: {res.status_code}): {res.text[:200] if 'res' in locals() else 'N/A'}")
        print(f"[SYSTEM ERROR]: Received invalid ACK response from server.")
    except Exception as e:
        log_error("mock_sender", "send_mock_message", f"Unexpected error sending message to {INCOMING_URL}", e)
        print(f"[SYSTEM]: Unexpected error sending message: {e}")

# --- Main Execution ---
def main():
    default_display_user = DEFAULT_USER
    user_input_raw = input(f"Enter user ID (default: {default_display_user}): ").strip()
    user_id_to_send = user_input_raw if user_input_raw else default_display_user

    print(f"--- Mock Sender for User: {user_id_to_send} ---")
    print(f"--- Target Backend URL: {BASE_URL} ---") # Display target URL
    print(f"--- Bot responses also logged to: {BOT_RESPONSE_LOG_FILE} ---")
    print("Polling for messages... Type your message. Use :exit to quit.")

    polling_thread = threading.Thread(target=poll_for_messages, args=(user_id_to_send,), daemon=True)
    polling_thread.start()

    while True:
        try:
            msg = input(f"[YOU]: ")
            if msg.strip().lower() == ":exit": break
            if msg.strip() == "": continue
            send_mock_message(user_id_to_send, msg)
        except (EOFError, KeyboardInterrupt):
            print("\nCtrl+C or EOF detected.")
            break
        except Exception as loop_err:
             log_error("mock_sender", "main_loop", f"Error in main input loop: {loop_err}", loop_err)
             print(f"[SYSTEM ERROR]: An error occurred in the input loop: {loop_err}")
             time.sleep(1)

    print("\nStopping polling thread...")
    _stop_polling.set()
    polling_thread.join(timeout=2)
    print("Mock chat ended.")

if __name__ == "__main__":
     try: _ = log_info
     except NameError: print("[FATAL ERROR] Logger not initialized. Exiting."); sys.exit(1)
     log_info("mock_sender", "main", f"Starting mock sender. Target URL: {BASE_URL}")
     main()
# --- END of tests/mock_sender.py ---