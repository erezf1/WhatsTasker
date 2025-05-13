#!/bin/bash

# === Configuration ===
PROJECT_DIR="/home/whatstasker/WhatsTasker" # <-- !!! ADJUST: Absolute path to your project
PYTHON_EXEC="$PROJECT_DIR/venv/bin/python3" # <-- !!! ADJUST: Path to python in venv
NODE_EXEC=$(which node) # Should find node if installed correctly
MAIN_PY_SCRIPT="$PROJECT_DIR/main.py"
NODE_JS_SCRIPT="$PROJECT_DIR/wa_bridge.js"

LOG_DIR="$PROJECT_DIR/logs" # Log directory within the project
PYTHON_LOG="$LOG_DIR/backend_app.log"
NODE_LOG="$LOG_DIR/whatsapp_bridge.log"
MONITOR_LOG="$LOG_DIR/monitor.log"

CHECK_INTERVAL_SECONDS=100 # Check every hour (3600 seconds) - Set back from 10 for production
# CHECK_INTERVAL_SECONDS=10 # Use 10 for quick testing

MONITOR_PID=$$ # Get the PID of this monitor script itself

# === Ensure Log Directory Exists ===
mkdir -p "$LOG_DIR"

# === Logging Function for Monitor Script ===
log_message() {
    # Include Monitor PID in logs for clarity if multiple were accidentally run
    echo "$(date '+%Y-%m-%d %H:%M:%S') [Monitor PID: $MONITOR_PID] - $1" >> "$MONITOR_LOG"
}

# === Function to Start Python Backend ===
start_python() {
    log_message "Attempting to start Python backend..."
    # Activate venv, change to project dir, run main.py, redirect output, run in background
    # Use exec to replace the subshell process with the python process? Maybe not needed here.
    ( source "$PROJECT_DIR/venv/bin/activate" && cd "$PROJECT_DIR" && "$PYTHON_EXEC" "$MAIN_PY_SCRIPT" >> "$PYTHON_LOG" 2>&1 ) &
    PYTHON_BG_PID=$! # Capture the PID of the background process *started by this function*
    log_message "Python backend start command issued (Attempted PID: $PYTHON_BG_PID)."
    # Note: This PID might not be the final Python process if it forks, pgrep is more reliable for checking.
}

# === Function to Start Node.js Bridge ===
start_node() {
    log_message "Attempting to start Node.js bridge..."
    # Change to project dir, run node script, redirect output, run in background
    ( cd "$PROJECT_DIR" && "$NODE_EXEC" "$NODE_JS_SCRIPT" >> "$NODE_LOG" 2>&1 ) &
    NODE_BG_PID=$! # Capture the PID
    log_message "Node.js bridge start command issued (Attempted PID: $NODE_BG_PID)."
}

# === Function to Check if Process is Running ===
# Returns 0 if running, 1 if not running. Also stores PID in global CHECK_PID variable if found.
CHECK_PID="" # Global variable to store found PID
check_process() {
    local script_name="$1"
    # Use pgrep -f to find the PID. Use -o to get the oldest matching process if multiple exist.
    # Using -x might be too strict if the command line has extra args later.
    CHECK_PID=$(pgrep -f -o "$script_name")
    if [[ -n "$CHECK_PID" ]]; then
        # Optionally double-check if the found PID actually contains the script name in its command line
        # cmdline=$(ps -p $CHECK_PID -o cmd=) # This can be less reliable across systems
        # if [[ "$cmdline" == *"$script_name"* ]]; then
            return 0 # Process is running
        # fi
    fi
    CHECK_PID="" # Clear if not found or check fails
    return 1 # Process is NOT running
}

# === Cleanup Function (Triggered by TRAP) ===
cleanup() {
    log_message "Termination signal received. Cleaning up managed processes..."

    # Find and kill Python backend
    if check_process "$MAIN_PY_SCRIPT" && [[ -n "$CHECK_PID" ]]; then
        log_message "Stopping Python backend (PID: $CHECK_PID)..."
        kill "$CHECK_PID" # Send TERM signal
    else
        log_message "Python backend not found running during cleanup."
    fi

    # Find and kill Node.js bridge
    if check_process "$NODE_JS_SCRIPT" && [[ -n "$CHECK_PID" ]]; then
        log_message "Stopping Node.js bridge (PID: $CHECK_PID)..."
        kill "$CHECK_PID" # Send TERM signal
    else
        log_message "Node.js bridge not found running during cleanup."
    fi

    log_message "Cleanup actions complete. Monitor script exiting."
    exit 0 # Exit the script cleanly after cleanup
}

# === Trap Signals ===
# Call the 'cleanup' function when the script receives TERM, INT, QUIT, or EXIT signals
trap cleanup TERM INT QUIT EXIT
log_message "Signal traps set for TERM, INT, QUIT, EXIT."

# === Initial Startup ===
log_message "Monitor script starting (PID: $MONITOR_PID). Performing initial process check/start."
if ! check_process "$MAIN_PY_SCRIPT"; then
    log_message "Python backend not running. Starting..."
    start_python
else
    log_message "Python backend already running (PID: $CHECK_PID)."
fi
sleep 2 # Small delay between starts

if ! check_process "$NODE_JS_SCRIPT"; then
    log_message "Node.js bridge not running. Starting..."
    start_node
else
    log_message "Node.js bridge already running (PID: $CHECK_PID)."
fi

log_message "Initial checks complete. Starting monitoring loop (Interval: $CHECK_INTERVAL_SECONDS seconds)."

# === Monitoring Loop ===
while true; do
    # Check Python Backend
    if check_process "$MAIN_PY_SCRIPT"; then
        log_message "CHECK: Python backend is running (PID: $CHECK_PID)."
    else
        log_message "ALERT: Python backend stopped. Restarting..."
        start_python
        sleep 5 # Give it a moment after restart
    fi

    # Check Node Bridge
    if check_process "$NODE_JS_SCRIPT"; then
        log_message "CHECK: Node.js bridge is running (PID: $CHECK_PID)."
    else
        log_message "ALERT: Node.js bridge stopped. Restarting..."
        start_node
        sleep 5 # Give it a moment after restart
    fi

    # Wait for the next check interval
    sleep "$CHECK_INTERVAL_SECONDS"
done