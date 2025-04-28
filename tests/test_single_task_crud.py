# tests/test_single_task_crud.py
import sys
import os
import time
from datetime import datetime, timedelta

# Add project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Core imports
from tools.logger import log_info, log_error, log_warning
from typing import Dict, List, Optional, Any
from tools import metadata_store

# --- Ensure .env is loaded ---
from dotenv import load_dotenv
dotenv_path = os.path.join(project_root, '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path=dotenv_path)
    log_info("test_single_task_crud", "main", ".env loaded.")
else:
    log_warning("test_single_task_crud", "main", ".env file not found, ensure env vars are set externally.")

# --- Modules/Functions to Test ---
# Import functions directly from the service layer
try:
    from services import task_manager, agent_state_manager
    # Import user_manager ONLY for initializing the agent state
    from users import user_manager
    MODULE_IMPORT_SUCCESS = True
except ImportError as e:
    log_error("test_single_task_crud", "imports", f"Failed to import necessary modules: {e}", e)
    MODULE_IMPORT_SUCCESS = False

# Test User Configuration
TEST_USER_ID = "123" # The pre-configured, authenticated user

# --- Assertion Helper ---
def _assert(condition: bool, message: str) -> bool:
    """Simple assertion helper that logs errors."""
    if not condition:
        log_error("test_single_task_crud", "ASSERT FAILED", message)
        return False
    log_info("test_single_task_crud", "ASSERT PASSED", message)
    return True

# --- Test Function ---
def run_create_delete_test() -> bool:
    """Tests creating one event and then deleting it."""
    log_info("test_single_task_crud", "run_create_delete_test", f"--- Starting Single Task Create/Delete Test for User {TEST_USER_ID} ---")
    overall_success = True
    created_event_id: Optional[str] = None

    try:
        # 1. Initialize Agent State (Crucial Step)
        # This loads prefs, tries to init GCal, preloads context, registers state
        log_info("test_single_task_crud", "run_create_delete_test", f"Initializing agent state for {TEST_USER_ID}...")
        agent_state = user_manager.create_and_register_agent_state(TEST_USER_ID)
        if not _assert(agent_state is not None, "Failed to create/register agent state"): return False
        # Verify GCal was initialized successfully this time
        if not _assert(agent_state.get("calendar") is not None and agent_state["calendar"].is_active(), "Google Calendar API did not initialize successfully"): return False
        log_info("test_single_task_crud", "run_create_delete_test", "Agent state initialization successful (including GCal).")


        # 2. Create Task/Reminder
        log_info("test_single_task_crud", "run_create_delete_test", "Testing CREATE...")
        today_str = datetime.now().strftime("%Y-%m-%d")
        task_data = {
            "description": f"Test Event for Delete {time.time()}", # Unique description
            "title": f"Test Event {time.time()}",
            "type": "reminder",
            "date": today_str,
            "time": "15:30" # Specific time
        }
        # Call the service function
        created_metadata = task_manager.create_task(TEST_USER_ID, task_data)
        created_event_id = created_metadata.get('event_id') if created_metadata else None

        if not _assert(created_event_id is not None, "Task creation failed or didn't return metadata"):
            overall_success = False
            # Cannot proceed to delete if creation failed
            log_info("test_single_task_crud", "run_create_delete_test", "--- Test Finished (Early due to Create Failure) ---")
            return False
        else:
            log_info("test_single_task_crud", "run_create_delete_test", f"Event created successfully. ID: {created_event_id}")
            # Verify memory update
            context = agent_state_manager.get_context(TEST_USER_ID)
            if not _assert(context and any(t.get('event_id') == created_event_id for t in context), "Created task not found in context"): overall_success = False
            # Verify persistent metadata
            meta_check = activity_db.get_task(created_event_id)
            if not _assert(meta_check and meta_check.get('event_id') == created_event_id, "Created task metadata not found"): overall_success = False

        # 3. Delete Task/Reminder
        log_info("test_single_task_crud", "run_create_delete_test", f"Testing DELETE (Event ID: {created_event_id})...")
        delete_success = task_manager.delete_task(TEST_USER_ID, created_event_id)

        if not _assert(delete_success, "Task deletion failed"):
            overall_success = False
        else:
            log_info("test_single_task_crud", "run_create_delete_test", "Task deletion successful.")
            # Verify memory update
            context_after_delete = agent_state_manager.get_context(TEST_USER_ID)
            if not _assert(context_after_delete is None or not any(t.get('event_id') == created_event_id for t in context_after_delete), "Deleted task still found in context"): overall_success = False
            # Verify persistent metadata deleted
            meta_check_after_delete = activity_db.get_task(created_event_id)
            if not _assert(not meta_check_after_delete, "Deleted task metadata still found"): overall_success = False


    except ImportError as ie:
        log_error("test_single_task_crud", "run_create_delete_test", f"Import Error - Missing Service/Module?: {ie}", ie)
        overall_success = False
    except Exception as e:
        log_error("test_single_task_crud", "run_create_delete_test", "Test crashed", e)
        overall_success = False
    finally:
        # Optional minimal cleanup: Remove from memory if test crashed mid-way
        if created_event_id: # If an ID was generated
            try:
                 if agent_state_manager.get_agent_state(TEST_USER_ID): # Check if state exists
                    agent_state_manager.remove_task_from_context(TEST_USER_ID, created_event_id)
                    log_info("test_single_task_crud", "run_create_delete_test", f"Attempted final context cleanup for {created_event_id}")
            except Exception as final_clean_e:
                 log_warning("test_single_task_crud", "run_create_delete_test", f"Error during final context cleanup: {final_clean_e}")

        log_info("test_single_task_crud", "run_create_delete_test", f"--- Finished Single Task Create/Delete Test (Success: {overall_success}) ---")

    return overall_success

# --- Main Execution Block ---
if __name__ == "__main__":
    if not MODULE_IMPORT_SUCCESS:
        print("\nERROR: Could not run test due to missing modules. Please check imports and setup.")
        sys.exit(1)

    # Initialize Agent State Manager with user_manager's dictionary
    # Ensure this matches how it's done in main.py
    try:
        from users.user_manager import _user_agents_in_memory
        from services.agent_state_manager import initialize_state_store
        initialize_state_store(_user_agents_in_memory)
    except ImportError:
        log_error("test_single_task_crud", "main", "Could not initialize AgentStateManager.")
        sys.exit(1)
    except Exception as init_e:
        log_error("test_single_task_crud", "main", f"Error initializing state manager: {init_e}")
        sys.exit(1)


    # Run the test function
    success = run_create_delete_test()

    # Exit with appropriate code for automation if needed
    sys.exit(0 if success else 1)