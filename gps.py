# --- START OF UPDATED DUMP SCRIPT ---

import os
from datetime import datetime

# List of project files for v0.8 Orchestrator structure + Scheduler/Notifications
project_files = [
    # ================== DOCUMENTATION & SETUP ==================
    "README.md",                    # Should be updated for v0.8
    "WhatsTasker_PRD_08.txt",       # v0.8 Product Requirements
    "WhatsTasker_SRS_08.txt",       # v0.8 Software Requirements/Architecture
    "requirements.txt",             # Includes APScheduler now

    # ================== CONFIGURATION ==================
    "config/prompts.yaml",          # Includes Orchestrator, Onboarding, Scheduler prompts
    "config/messages.yaml",         # Includes welcome message and others
    "config/settings.yaml",         # General app settings (if used)
    # ".env",                       # Secrets - DO NOT DUMP/COMMIT

    # ================== API & ENTRY POINT ==================
    "main.py",                      # Main entry point (imports & starts scheduler)

    # ================== BRIDGE ==================
    "bridge/request_router.py",     # Handles routing based on status
    "bridge/cli_interface.py",      # FastAPI endpoints for CLI bridge

    # ================== AGENT LAYER (v0.8) ==================
    "agents/orchestrator_agent.py", # Main agent logic (pure LLM flow)
    "agents/onboarding_agent.py",   # Handles 'onboarding' status
    "agents/tool_definitions.py",   # Pydantic models & tool functions

    # ================== SERVICES (Business Logic) ==================
    "services/task_manager.py",     # Core task CRUD, GCal interaction, timestamp logic
    "services/task_query_service.py",# Listing, context snapshot (needs formatting update later)
    "services/config_manager.py",   # Preference management, calendar auth start, status setting
    "services/agent_state_manager.py",# Core in-memory state management (with notification tracking)
    "services/cheats.py",           # Handles cheat code commands
    "services/llm_interface.py",    # Initializes Instructor-patched OpenAI client
    # --- NEW/Updated Services ---
    "services/scheduler_service.py", # NEW: Initializes APScheduler and jobs
    "services/sync_service.py",     # UPDATED: Contains get_synced_context_snapshot
    "services/notification_service.py", # NEW: Contains check_event_notifications logic
    "services/routine_service.py",  # NEW: Contains check_routine_triggers & summary generation

    # ================== TOOLS (Utilities & API Wrappers) ==================
    "tools/google_calendar_api.py", # Wrapper for GCal API (with is_active, updated parsing)
    "tools/calendar_tool.py",       # OAuth callback endpoint, core auth check
    "tools/metadata_store.py",      # Metadata persistence (CSV - updated FIELDNAMES)
    "tools/token_store.py",         # Encrypted token storage
    "tools/encryption.py",          # Encryption utilities
    "tools/logger.py",              # Logging setup

    # ================== USERS (State & Preferences) ==================
    "users/user_manager.py",        # Initializes agent states (with notification set)
    "users/user_registry.py",       # Persistent user preferences store (JSON - updated defaults)

    # ================== DATA (Example - DO NOT DUMP SENSITIVE DATA) ==================
    # "data/users/registry.json",       # Example structure, maybe exclude content
    # "data/events_metadata.csv",       # Example structure, maybe exclude content
    # "data/tokens_..."               # Exclude token files!

    # ================== TESTS ==================
    "tests/mock_sender.py",         # Simulates user input
    # Add new tests for Agents, Tools, Services
]

def write_full_code_dump(output_filename="project_v0.8_dump.txt"):
    """Creates a single text file containing the content of specified project files."""
    dump_count = 0
    missing_files = []
    processed_files = set() # Use a set for faster checking

    print(f"Starting project dump to '{output_filename}'...")

    try:
        with open(output_filename, "w", encoding="utf-8") as f_out:
            f_out.write(f"# WhatsTasker Project Code Dump (v0.8 Architecture Target + Scheduler)\n") # Updated header
            f_out.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            for item_path_str in project_files:
                item_path_str = item_path_str.strip()
                if item_path_str.startswith("#"):
                    # Write section headers for better structure
                    if item_path_str.startswith("# ===") or item_path_str.startswith("# ---"):
                         f_out.write(f"\n{item_path_str}\n\n")
                    continue # Skip other comment lines

                item_path = os.path.normpath(item_path_str)

                if item_path in processed_files:
                    continue
                processed_files.add(item_path)

                f_out.write("=" * 80 + "\n")
                f_out.write(f"📄 {item_path}\n")
                f_out.write("=" * 80 + "\n\n")

                if os.path.exists(item_path):
                    try:
                        with open(item_path, "r", encoding="utf-8") as f_in:
                            content = f_in.read()
                            f_out.write(f"# --- START OF FILE {item_path} ---\n")
                            f_out.write(content)
                            f_out.write(f"\n# --- END OF FILE {item_path} ---\n")
                        dump_count += 1
                        print(f"  ✅ Dumped: {item_path}")
                    except Exception as e:
                        f_out.write(f"# ⚠️ Error reading file: {e}\n")
                        print(f"  ❌ Error reading: {item_path} - {e}")
                        missing_files.append(f"{item_path} (Read Error)")
                else:
                    f_out.write("# ⚠️ File does not exist.\n")
                    print(f"  ⚠️ Missing: {item_path}")
                    missing_files.append(f"{item_path} (Not Found)")

                f_out.write("\n\n")

        print("-" * 80)
        print(f"📦 Project dump complete.")
        actual_file_count = len([p for p in project_files if not p.strip().startswith('#')])
        print(f"   Total files listed for dump: {actual_file_count}")
        print(f"   Files successfully dumped: {dump_count}")
        if missing_files:
            print(f"   ⚠️ Missing or unreadable files ({len(missing_files)}):")
            for missing in missing_files:
                print(f"      - {missing}")
        print(f"   Output written to: {output_filename}")
        print("-" * 80)

    except IOError as e:
        print(f"\n❌ ERROR: Could not write to output file '{output_filename}': {e}")
    except Exception as e:
        print(f"\n❌ ERROR: An unexpected error occurred during dump: {e}")


if __name__ == "__main__":
    # Update output filename if desired
    write_full_code_dump(output_filename="project_v0.8_dump.txt")

# --- END OF UPDATED DUMP SCRIPT ---