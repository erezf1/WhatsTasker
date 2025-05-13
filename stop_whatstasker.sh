#!/bin/bash

# stop_whatstasker.sh
# Script to stop all WhatsTasker related processes.

# === Configuration ===
# These should ideally match the names/paths used in your monitor_whatstasker.sh
# and how the processes are actually named or can be identified.
MONITOR_SCRIPT_NAME="monitor_whatstasker.sh"
PYTHON_BACKEND_SCRIPT_NAME="main.py" # The main Python script
NODE_BRIDGE_SCRIPT_NAME="wa_bridge.js"   # The Node.js bridge script

LOG_DIR_RELATIVE="logs" # Relative to project dir, if this script is in project root
STOP_LOG_FILENAME="stop_whatstasker.log" # Just the filename

# === Determine Absolute Log Path ===
# Get the directory of this script
SCRIPT_DIR_STOP="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
# Construct absolute path for the logs directory and the specific log file
LOG_DIR_ABS="$SCRIPT_DIR_STOP/$LOG_DIR_RELATIVE"
FULL_STOP_LOG_PATH="$LOG_DIR_ABS/$STOP_LOG_FILENAME"

# === Ensure Log Directory Exists ===
if [ ! -d "$LOG_DIR_ABS" ]; then
    # Attempt to create, suppress error if it already exists due to race condition
    mkdir -p "$LOG_DIR_ABS" 2>/dev/null
    if [ $? -eq 0 ]; then # Check if mkdir succeeded or directory already existed
        # Use echo to append as tee might fail if directory was just created
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Log directory created by stop script: $LOG_DIR_ABS" >> "$FULL_STOP_LOG_PATH"
    else
        # If mkdir failed and directory doesn't exist, we can't log to file
        echo "$(date '+%Y-%m-%d %H:%M:%S') [StopScript CRITICAL] - Failed to create log directory $LOG_DIR_ABS. Logging to console only."
        # Fallback: if log dir creation fails, tee will output to stdout only
        FULL_STOP_LOG_PATH="/dev/stdout" # Log to stdout if file path is problematic
    fi
fi

# === Logging Function for this Stop Script ===
log_stop_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [StopScript] - $1" | tee -a "$FULL_STOP_LOG_PATH"
}

# === Function to Find and Kill Processes ===
# $1: Script name pattern to search for
# $2: Descriptive name for logging
# $3: (Optional) Signal to send (default is TERM)
kill_processes_by_name() {
    local script_pattern="$1"
    local process_description="$2"
    local signal="${3:-TERM}" # Default to TERM if no signal specified

    log_stop_message "Attempting to stop $process_description (pattern: '$script_pattern')..."

    # Find PIDs matching the script pattern.
    # pgrep -f looks for the pattern in the full command line.
    # Exclude grep itself and this script from being killed.
    # For a more robust exclusion of this script, you could compare PIDs: pgrep -f "$script_pattern" | grep -v "^$$\$"
    local pids
    pids=$(pgrep -f "$script_pattern" | grep -Ev "(^$$|grep)") # Exclude current script PID and grep

    if [[ -z "$pids" ]]; then
        log_stop_message "$process_description not found running."
    else
        log_stop_message "Found $process_description PID(s): $pids. Sending $signal signal..."
        # Loop through each PID and kill it
        for pid in $pids; do
            if kill "-$signal" "$pid" > /dev/null 2>&1; then
                log_stop_message "Successfully sent $signal to $process_description (PID: $pid)."
                # Optional: Wait a bit and check if it's gone, then try KILL
                sleep 2 # Give process time to shut down
                if ps -p "$pid" > /dev/null; then # Check if process still exists
                    log_stop_message "$process_description (PID: $pid) still running after $signal. Attempting SIGKILL..."
                    if kill -KILL "$pid" > /dev/null 2>&1; then
                        log_stop_message "Successfully sent SIGKILL to $process_description (PID: $pid)."
                    else
                        log_stop_message "Failed to send SIGKILL to $process_description (PID: $pid). Manual check may be needed."
                    fi
                else
                    log_stop_message "$process_description (PID: $pid) terminated after $signal."
                fi
            else
                log_stop_message "Failed to send $signal to $process_description (PID: $pid). It might have already stopped or permissions issue."
            fi
        done
        log_stop_message "Finished attempting to stop $process_description."
    fi
}

# === Main Stop Logic ===
log_stop_message "--- Initiating WhatsTasker Shutdown ---"

# 1. Stop the Monitor Script First
kill_processes_by_name "$MONITOR_SCRIPT_NAME" "Monitor Script ($MONITOR_SCRIPT_NAME)"
sleep 2 # Give monitor's trap a moment (though we'll kill explicitly)

# 2. Stop the Python Backend
kill_processes_by_name "$PYTHON_BACKEND_SCRIPT_NAME" "Python Backend ($PYTHON_BACKEND_SCRIPT_NAME)"
sleep 1 # Short delay

# 3. Stop the Node.js Bridge
kill_processes_by_name "$NODE_BRIDGE_SCRIPT_NAME" "Node.js Bridge ($NODE_BRIDGE_SCRIPT_NAME)"

log_stop_message "--- WhatsTasker Shutdown Attempt Complete ---"
log_stop_message "Please verify processes are stopped using 'ps aux | grep -E \"$MONITOR_SCRIPT_NAME|$PYTHON_BACKEND_SCRIPT_NAME|$NODE_BRIDGE_SCRIPT_NAME\"' or similar commands."

exit 0