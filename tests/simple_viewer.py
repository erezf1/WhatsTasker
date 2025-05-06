# simple_viewer.py
import os
from flask import Flask, render_template, request, jsonify
from collections import deque
import threading
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables for port configuration
load_dotenv()

# Basic configuration
VIEWER_PORT = int(os.getenv("VIEWER_PORT", "5001")) # Use port 5001 by default
MAX_MESSAGES = 100 # Max messages to keep in memory

# --- In-memory message store ---
# deque is efficient for appending and limiting size
message_store = deque(maxlen=MAX_MESSAGES)
message_lock = threading.Lock() # Protect access if Flask uses threads

script_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(script_dir, 'templates')
print(f"Viewer attempting to use template folder: {template_dir}") # Debug print


# --- Create Flask App ---
app = Flask(__name__, template_folder=template_dir)

# --- Routes ---
@app.route('/')
def index():
    """Serves the main HTML viewer page."""
    # No need to pass messages here, JavaScript will fetch them
    return render_template('viewer.html', title="WhatsTasker Bot Viewer")

@app.route('/log_message', methods=['POST'])
def log_message():
    """Receives messages from mock_sender."""
    try:
        data = request.get_json()
        if not data or 'content' not in data:
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        message_content = data.get('content')
        timestamp = datetime.now().strftime("%H:%M:%S")
        message_id = data.get('message_id', 'N/A') # Get message_id if sent

        with message_lock:
            message_store.append({
                "timestamp": timestamp,
                "id": message_id,
                "content": message_content
            })

        # print(f"Viewer received: {message_content[:50]}...") # Optional: Log to viewer console
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Error in /log_message: {e}") # Log errors in viewer console
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_messages')
def get_messages():
    """Provides messages to the frontend JavaScript."""
    with message_lock:
        # Return messages as a list (deque needs conversion)
        messages = list(message_store)
    return jsonify({"messages": messages})

@app.route('/clear_messages', methods=['POST'])
def clear_messages():
    """Allows clearing the displayed messages."""
    with message_lock:
        message_store.clear()
    print("Viewer messages cleared.")
    return jsonify({"status": "ok"}), 200


# --- Main Execution ---
if __name__ == '__main__':
    print(f"Starting simple viewer server on http://localhost:{VIEWER_PORT}")
    # Turn off Flask's default logging unless debugging the viewer itself
    # Also turn off reloader for stability
    app.run(host='0.0.0.0', port=VIEWER_PORT, debug=False, use_reloader=False)