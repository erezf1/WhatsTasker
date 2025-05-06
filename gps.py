# gps.py - Generate Project Snapshot
import os
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
# Files/folders to include in the dump relative to the script's location (project root)
FILES_TO_DUMP = [
    "README.md",
    "WhatsTasker_PRD_08.txt",
    "WhatsTasker_SRS_08.txt",
    "requirements.txt",
    "package.json", # Include package.json for Node dependencies
    ".env.example", # Include example environment file
    ".gitignore",   # Include gitignore configuration
    "wa_bridge.js",
    "monitor_whatstasker.sh",
    "config/prompts.yaml",
    "config/messages.yaml",
    "config/settings.yaml", # Include even if empty
    "main.py",
    "bridge/request_router.py",
    "bridge/cli_interface.py",
    "bridge/whatsapp_interface.py",
    "agents/orchestrator_agent.py",
    "agents/onboarding_agent.py",
    "agents/tool_definitions.py",
    "services/task_manager.py",
    "services/task_query_service.py",
    "services/config_manager.py",
    "services/agent_state_manager.py",
    "services/cheats.py",
    "services/llm_interface.py",
    "services/scheduler_service.py",
    "services/sync_service.py",
    "services/notification_service.py",
    "services/routine_service.py",
    "tools/google_calendar_api.py",
    "tools/calendar_tool.py",
    "tools/token_store.py",
    "tools/encryption.py",
    "tools/logger.py",
    "tools/activity_db.py",
    "users/user_manager.py",
    "users/user_registry.py",
    "tests/mock_browser_chat.py",         # New browser chat app
    "tests/templates/browser_chat.html",  # New browser chat HTML
    "tests/test_smtp.py",                 # SMTP Test script
    # --- Obsolete/Replaced ---
    # "tests/mock_sender.py",     # Replaced by mock_browser_chat for UI
    # "tests/simple_viewer.py",   # Replaced by mock_browser_chat
    # "tests/mock_chat.py",       # Replaced by mock_browser_chat
    # "tools/metadata_store.py", # Obsolete based on SRS
]

# Output filename pattern
OUTPUT_FILENAME_PATTERN = "project_v0.8_dump_{timestamp}.txt"

# Separator
SEPARATOR = "=" * 80
# --- End Configuration ---

def generate_dump(output_filename: str, files_to_include: list):
    """Generates the project dump file."""
    project_root = Path(__file__).parent # Assumes gps.py is in the project root
    dump_content = []
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Header for the dump file
    dump_content.append(f"# WhatsTasker Project Code Dump (v0.8 Target - Browser Chat)")
    dump_content.append(f"# Generated: {timestamp_str}")
    dump_content.append("\n")

    processed_files = 0
    missing_files = []

    for relative_path_str in files_to_include:
        relative_path = Path(relative_path_str)
        full_path = project_root / relative_path

        if full_path.is_file():
            try:
                content = full_path.read_text(encoding='utf-8', errors='replace')
                dump_content.append(SEPARATOR)
                # Use platform-independent path separator for header
                header_path = relative_path.as_posix()
                dump_content.append(f"📄 {header_path}")
                dump_content.append(SEPARATOR)
                dump_content.append(f"\n# --- START OF FILE {header_path} ---\n")
                dump_content.append(content)
                dump_content.append(f"\n# --- END OF FILE {header_path} ---")
                dump_content.append("\n\n")
                processed_files += 1
                print(f"✅ Included: {header_path}")
            except Exception as e:
                print(f"❌ Error reading {relative_path_str}: {e}")
                missing_files.append(f"{relative_path_str} (Read Error: {e})")
        else:
            print(f"⚠️ File not found: {relative_path_str}")
            missing_files.append(f"{relative_path_str} (Not Found)")

    # --- Add Note about package-lock.json and node_modules ---
    dump_content.append(SEPARATOR)
    dump_content.append("📦 Node.js Dependencies Note")
    dump_content.append(SEPARATOR)
    dump_content.append("\n# The 'package.json' file lists Node.js dependencies.")
    dump_content.append("# The 'package-lock.json' file (not included) locks specific versions.")
    dump_content.append("# Run 'npm install' in the project root to install these dependencies (including whatsapp-web.js, axios, qrcode-terminal, dotenv, nodemailer).")
    dump_content.append("# The 'node_modules/' directory containing the installed packages is NOT included in this dump.\n\n")
    # ----------------------------------------------------------

    try:
        output_path = project_root / output_filename
        output_path.write_text("\n".join(dump_content), encoding='utf-8')
        print("-" * 30)
        print(f"✅ Dump generated successfully: {output_filename}")
        print(f"   Files included: {processed_files}")
        if missing_files:
            print(f"   ⚠️ Files skipped/missing: {len(missing_files)}")
            for missing in missing_files:
                print(f"      - {missing}")
    except Exception as e:
        print("-" * 30)
        print(f"❌ Error writing dump file {output_filename}: {e}")

if __name__ == "__main__":
    # Format the timestamp for the filename
    timestamp_file_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_FILENAME_PATTERN.format(timestamp=timestamp_file_str)
    generate_dump(output_file, FILES_TO_DUMP)