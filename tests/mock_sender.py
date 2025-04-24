# --- START of tests/mock_sender.py ---
import requests
import sys
import os
import json
import time
import threading

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Use the project logger, but primarily for errors/warnings in this script
from tools.logger import log_info, log_error, log_warning

DEFAULT_USER = "972547778005" # Example default
BASE_URL = "http://localhost:8000"
INCOMING_URL = f"{BASE_URL}/incoming"
OUTGOING_URL = f"{BASE_URL}/outgoing"
ACK_URL = f"{BASE_URL}/ack"

_stop_polling = threading.Event()

def poll_for_messages(user_id_raw: str):
    # ... (polling function remains the same) ...
    log_info("mock_sender", "poll_thread", f"Polling thread started for user {user_id_raw}.") # Keep start message
    session = requests.Session()
    connection_lost = False # Flag to track connection state

    while not _stop_polling.is_set():
        try:
            res = session.get(OUTGOING_URL, timeout=10)
            res.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            if connection_lost:
                print("[SYSTEM]: Connection to server restored.")
                log_warning("mock_sender", "poll_thread", "Connection restored.") # Use warning for visibility
                connection_lost = False

            data = res.json()
            messages = data.get("messages", [])

            if messages:
                for msg in messages:
                    print(f"\n[BOT]: {msg.get('message', '[No message content]')}")
                    try:
                        ack_payload = {"message_id": msg.get("message_id"), "user_id": msg.get("user_id")}
                        ack_res = session.post(ACK_URL, json=ack_payload, timeout=5)
                        if ack_res.status_code != 200:
                             log_warning("mock_sender", "poll_thread", f"Failed ACK for {msg.get('message_id')}. Status: {ack_res.status_code}")
                    except Exception as ack_e:
                         log_warning("mock_sender", "poll_thread", f"Error sending ACK for {msg.get('message_id')}: {ack_e}")
                print(f"[YOU]: ", end="", flush=True)

        except requests.exceptions.Timeout:
             if not connection_lost: # Log only the first time timeout happens after connection was okay
                 log_warning("mock_sender", "poll_thread", "Polling request timed out.")
                 print("[SYSTEM]: Polling timed out...")
                 connection_lost = True # Assume connection might be shaky
             time.sleep(2) # Wait longer on timeout
        except requests.exceptions.RequestException as e:
             if not connection_lost: # Only log the first time connection fails
                 # Check for connection refused specifically
                 if "actively refused it" in str(e):
                      error_msg = "Connection error: Target machine actively refused connection. Is the server running?"
                 else:
                      error_msg = f"Connection error: {e}"
                 log_error("mock_sender", "poll_thread", error_msg)
                 print(f"[SYSTEM]: {error_msg}")
                 connection_lost = True
             time.sleep(5) # Wait significantly longer if connection refused/error
        except json.JSONDecodeError as e:
             log_error("mock_sender", "poll_thread", f"Failed to decode JSON response: {e}. Response text: {res.text[:100]}")
             print("[SYSTEM]: Received invalid response from server.")
             time.sleep(2)
        except Exception as e:
             log_error("mock_sender", "poll_thread", f"Unexpected error in polling thread: {e}", e)
             if not connection_lost:
                 print(f"[SYSTEM]: Unexpected polling error: {e}")
                 connection_lost = True
             time.sleep(5)

        if not connection_lost:
            time.sleep(0.5)

    log_info("mock_sender", "poll_thread", "Polling thread stopped.")


# --- UPDATED send_mock_message ---
def send_mock_message(user_id_raw: str, message: str):
    """Sends message, expects only ACK back directly."""
    payload = {"user_id": user_id_raw, "message": message}
    # Increase timeout significantly for LLM-heavy operations
    # Try 60 or 90 seconds
    ack_timeout = 60
    try:
        # Use the increased timeout here
        res = requests.post(INCOMING_URL, json=payload, timeout=ack_timeout)
        res.raise_for_status()
        response_data = res.json()
        if not response_data.get("ack"):
             log_warning("mock_sender", "send_mock_message", f"Server response did not contain expected ack: {response_data}")

    except requests.exceptions.ReadTimeout:
        # Log specifically that the ACK timed out, but the message was likely sent
        log_warning("mock_sender", "send_mock_message", f"ACK response timed out after {ack_timeout}s (message likely sent, server processing).")
        print(f"[SYSTEM]: Server took too long to acknowledge message (>{ack_timeout}s), but it was likely sent. Check for BOT response.")
    except requests.exceptions.RequestException as e:
        log_error("mock_sender", "send_mock_message", f"Failed to send message", e)
        print(f"[SYSTEM]: Error connecting to server to send message: {e}")
    except json.JSONDecodeError:
        log_error("mock_sender", "send_mock_message", f"Received non-JSON ACK response: {res.text}")
        print(f"[SYSTEM ERROR]: Received invalid ACK response from server.")
    except Exception as e:
        log_error("mock_sender", "send_mock_message", f"Unexpected error sending message", e)
        print(f"[SYSTEM]: Unexpected error sending message: {e}")

# --- (main function remains the same) ---
def main():
    default_display_user = DEFAULT_USER
    user_input_raw = input(f"Enter user ID (default: {default_display_user}): ").strip()
    user_id_to_send = user_input_raw if user_input_raw else default_display_user

    print(f"--- Mock Sender for User: {user_id_to_send} ---")
    print("Polling for messages... Type your message. Use :exit to quit.")

    polling_thread = threading.Thread(target=poll_for_messages, args=(user_id_to_send,), daemon=True)
    polling_thread.start()

    while True:
        try:
            msg = input(f"[YOU]: ")
            if msg.strip().lower() == ":exit":
                break
            if msg.strip() == "": continue
            send_mock_message(user_id_to_send, msg)
        except (EOFError, KeyboardInterrupt):
            print("\nCtrl+C or EOF detected.")
            break

    print("\nStopping polling thread...")
    _stop_polling.set()
    polling_thread.join(timeout=2)
    print("Mock chat ended.")

if __name__ == "__main__":
     try: log_info("mock_sender", "main", "Starting mock sender...")
     except NameError: print("FATAL ERROR: Logger not loaded."); sys.exit(1)
     except Exception as e: print(f"FATAL ERROR during logging setup: {e}"); sys.exit(1)
     main()
# --- END of tests/mock_sender.py ---