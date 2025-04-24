# tests/run_all_tests_simple.py
import sys
import os
import time
from datetime import datetime, timedelta

# Add project root to sys.path if necessary for imports within tests
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Core imports
from tools.logger import log_info, log_error, log_warning
from typing import Dict, List, Optional, Any

# --- Modules/Functions to Test ---
# Import functions directly from the service layer
try:
    from services import task_manager, task_query_service, config_manager, agent_state_manager
    from users import user_manager, user_registry
    from tools import metadata_store # Keep direct import for cleanup verification
    MODULE_IMPORT_SUCCESS = True
except ImportError as e:
    log_error("run_all_tests_simple", "imports", f"Failed to import necessary modules: {e}", e)
    MODULE_IMPORT_SUCCESS = False

# Test User Configuration
TEST_USER_ID = "123"
TEST_USER_EMAIL = "test_user_123@example.com"

# --- Helper Functions ---
def _assert(condition: bool, message: str):
    """Simple assertion helper that logs errors."""
    if not condition:
        log_error("run_all_tests_simple", "ASSERT FAILED", message)
        return False
    # log_info("run_all_tests_simple", "ASSERT PASSED", message) # Optional
    return True

def _cleanup_user_data(user_id: str):
    """Attempts to clean up test data for a user."""
    log_info("run_all_tests_simple", "_cleanup_user_data", f"--- Starting Cleanup for {user_id} ---")
    overall_cleanup_success = True

    # 1. Clean Metadata Store
    log_info("run_all_tests_simple", "_cleanup_user_data", "Cleaning metadata store...")
    try:
        # Load all metadata to find relevant IDs
        all_meta_before = metadata_store.load_all_metadata()
        user_meta_ids = [row['event_id'] for row in all_meta_before if row.get('user_id') == user_id and row.get('event_id')]
        log_info("run_all_tests_simple", "_cleanup_user_data", f"Found {len(user_meta_ids)} metadata entries for {user_id} to potentially delete.")

        if user_meta_ids:
            deleted_count = 0
            # Delete using the service function for consistency if possible,
            # but direct metadata_store call is okay for cleanup script
            for event_id in user_meta_ids:
                if metadata_store.delete_event_metadata(event_id): # Call tool directly
                     deleted_count += 1
                else:
                     log_warning("run_all_tests_simple", "_cleanup_user_data", f"Metadata store reported failure deleting {event_id}")
                     # overall_cleanup_success = False # Decide if this constitutes failure

            log_info("run_all_tests_simple", "_cleanup_user_data", f"Attempted deletion of {deleted_count}/{len(user_meta_ids)} metadata entries.")
            # Verify by reloading
            all_meta_after = metadata_store.load_all_metadata()
            user_meta_after = [row for row in all_meta_after if row.get('user_id') == user_id]
            if not user_meta_after:
                 log_info("run_all_tests_simple", "_cleanup_user_data", "Metadata cleanup verified (no user entries found).")
            else:
                 log_error("run_all_tests_simple", "_cleanup_user_data", f"Metadata cleanup FAILED, {len(user_meta_after)} entries still present.")
                 overall_cleanup_success = False
        else:
            log_info("run_all_tests_simple", "_cleanup_user_data", "No metadata entries found for user, skipping deletion.")

    except Exception as e:
        log_error("run_all_tests_simple", "_cleanup_user_data", "Error during metadata cleanup", e)
        overall_cleanup_success = False

    # 2. Clean User Registry
    log_info("run_all_tests_simple", "_cleanup_user_data", "Cleaning user registry...")
    try:
        reg = user_registry.get_registry()
        if user_id in reg:
            del reg[user_id]
            # Rewrite the registry file
            with open(user_registry.USER_REGISTRY_PATH, "w", encoding="utf-8") as f:
                 import json
                 json.dump(reg, f, indent=2)
            log_info("run_all_tests_simple", "_cleanup_user_data", f"Removed user {user_id} from registry.")
        else:
            log_info("run_all_tests_simple", "_cleanup_user_data", f"User {user_id} not found in registry.")
    except Exception as e:
        log_error("run_all_tests_simple", "_cleanup_user_data", "Error during registry cleanup", e)
        overall_cleanup_success = False

    # 3. Clean In-Memory State
    log_info("run_all_tests_simple", "_cleanup_user_data", "Cleaning in-memory agent state...")
    try:
        # Access state via AgentStateManager if possible, else directly
        state_manager_available = "agent_state_manager" in sys.modules
        if state_manager_available:
            with agent_state_manager._state_lock: # Direct lock access (less ideal but needed for direct dict manipulation)
                 if user_id in agent_state_manager._AGENT_STATE_STORE:
                     del agent_state_manager._AGENT_STATE_STORE[user_id]
                     log_info("run_all_tests_simple", "_cleanup_user_data", f"Removed user {user_id} from AgentStateManager.")
                 else:
                     log_info("run_all_tests_simple", "_cleanup_user_data", f"User {user_id} not found in AgentStateManager.")
        else:
             log_warning("run_all_tests_simple", "_cleanup_user_data", "AgentStateManager not imported, cannot clean memory directly.")
             # Rely on restart to clear memory

    except Exception as e:
        log_error("run_all_tests_simple", "_cleanup_user_data", "Error during in-memory state cleanup", e)
        # Don't necessarily fail overall cleanup for memory issues if persistence cleaned
        # overall_cleanup_success = False

    # 4. Clean GCal Events (Placeholder)
    log_warning("run_all_tests_simple", "_cleanup_user_data", "Google Calendar cleanup NOT implemented.")

    log_info("run_all_tests_simple", "_cleanup_user_data", f"--- Finished Cleanup for {user_id}. Overall Success (Persistence): {overall_cleanup_success} ---")
    return overall_cleanup_success


def _setup_test_user(user_id: str) -> bool:
    """Ensures test user exists and has basic state initialized."""
    log_info("run_all_tests_simple", "_setup_test_user", f"Setting up test user {user_id}")
    try:
        # Call the function responsible for creating/registering state
        state = user_manager.create_and_register_agent_state(user_id)
        return state is not None
    except Exception as e:
        log_error("run_all_tests_simple", "_setup_test_user", f"Failed to setup test user {user_id}", e)
        return False

# --- Test Suite Functions ---

def run_task_crud_tests() -> bool:
    """Tests create, list, update, delete via TaskManagerService & TaskQueryService."""
    log_info("run_all_tests_simple", "run_task_crud_tests", "--- Starting Task CRUD Tests ---")
    overall_success = True
    created_event_ids = [] # Keep track of IDs created in this test run

    try:
        if not _setup_test_user(TEST_USER_ID): return False

        today_str = datetime.now().strftime("%Y-%m-%d")
        tomorrow_dt = datetime.now().date() + timedelta(days=1)
        tomorrow_str = tomorrow_dt.strftime("%Y-%m-%d")

        # 1. Create Task
        log_info("run_all_tests_simple", "run_task_crud_tests", "Testing CREATE Task...")
        task_data_1 = { "description": "Test Task Alpha (CRUD)", "type": "task", "date": today_str, "time": "14:00", "duration": "1h" }
        created_task_1 = task_manager.create_task(TEST_USER_ID, task_data_1)
        task_1_id = created_task_1.get('event_id') if created_task_1 else None
        if not _assert(task_1_id is not None, "Task 1 creation failed or didn't return metadata"): return False
        created_event_ids.append(task_1_id)
        log_info("run_all_tests_simple", "run_task_crud_tests", f"Task 1 created ID: {task_1_id}")
        context = agent_state_manager.get_context(TEST_USER_ID) # Verify context
        if not _assert(context and any(t.get('event_id') == task_1_id for t in context), "Task 1 not added to context"): overall_success = False

        # 2. Create Reminder
        log_info("run_all_tests_simple", "run_task_crud_tests", "Testing CREATE Reminder...")
        task_data_2 = { "description": "Test Reminder Bravo (CRUD)", "type": "reminder", "date": tomorrow_str }
        created_task_2 = task_manager.create_task(TEST_USER_ID, task_data_2)
        task_2_id = created_task_2.get('event_id') if created_task_2 else None
        if not _assert(task_2_id is not None, "Reminder 2 creation failed"): overall_success = False; task_2_id = None # Prevent use later
        if task_2_id: created_event_ids.append(task_2_id)
        log_info("run_all_tests_simple", "run_task_crud_tests", f"Reminder 2 created ID: {task_2_id}")
        context = agent_state_manager.get_context(TEST_USER_ID) # Verify context
        if not _assert(context and any(t.get('event_id') == task_2_id for t in context), "Reminder 2 not added to context"): overall_success = False

        # 3. List Tasks (using TaskQueryService)
        log_info("run_all_tests_simple", "run_task_crud_tests", "Testing LIST (Query Service)...")
        list_str, mapping = task_query_service.get_formatted_list(TEST_USER_ID, status_filter='active')
        log_info("run_all_tests_simple", "run_task_crud_tests", f"Formatted List Output:\n{list_str}")
        log_info("run_all_tests_simple", "run_task_crud_tests", f"List Mapping: {mapping}")
        current_listed_ids = set(mapping.values())
        if task_1_id and not _assert(task_1_id in current_listed_ids, "Task 1 not found in list"): overall_success = False
        if task_2_id and not _assert(task_2_id in current_listed_ids, "Reminder 2 not found in list"): overall_success = False
        if not _assert(len(mapping) >= 2, f"List expected >= 2 items, got {len(mapping)}"): overall_success = False

        # 4. Update Task Status
        if task_1_id:
            log_info("run_all_tests_simple", "run_task_crud_tests", f"Testing UPDATE Status (Task 1: {task_1_id}) to completed...")
            updated_task = task_manager.update_task_status(TEST_USER_ID, task_1_id, "completed")
            if not _assert(updated_task and updated_task.get('status') == 'completed', "Task 1 status update failed"): overall_success = False
            else:
                 log_info("run_all_tests_simple", "run_task_crud_tests", "Task 1 status updated.")
                 context = agent_state_manager.get_context(TEST_USER_ID) # Verify context update
                 if not _assert(context is None or not any(t.get('event_id') == task_1_id for t in context), "Task 1 not removed from context after completion"): overall_success = False

        # 5. Delete Reminder
        if task_2_id:
            log_info("run_all_tests_simple", "run_task_crud_tests", f"Testing DELETE (Reminder 2: {task_2_id})...")
            delete_success = task_manager.delete_task(TEST_USER_ID, task_2_id)
            if not _assert(delete_success, "Reminder 2 deletion failed"): overall_success = False
            else:
                 created_event_ids.remove(task_2_id) # Remove from list needing cleanup
                 log_info("run_all_tests_simple", "run_task_crud_tests", "Reminder 2 deleted.")
                 context = agent_state_manager.get_context(TEST_USER_ID) # Verify context update
                 if not _assert(context is None or not any(t.get('event_id') == task_2_id for t in context), "Reminder 2 not removed from context"): overall_success = False
                 # --- Verify metadata deletion using get ---
                 meta_check = metadata_store.get_event_metadata(task_2_id) # Use correct function
                 if not _assert(not meta_check, "Reminder 2 metadata check found data after delete"): overall_success = False
                 else: log_info("run_all_tests_simple", "run_task_crud_tests", "Reminder 2 metadata deletion verified.")

    except ImportError as ie:
        log_error("run_all_tests_simple", "run_task_crud_tests", f"Import Error - Missing Service/Module?: {ie}", ie)
        overall_success = False
    except Exception as e:
        log_error("run_all_tests_simple", "run_task_crud_tests", "Test suite crashed", e)
        overall_success = False
    finally:
        # --- Cleanup ---
        log_info("run_all_tests_simple", "run_task_crud_tests", "Attempting cleanup of remaining created test events...")
        for event_id in created_event_ids: # Only cleanup items confirmed created and not deleted
             try: task_manager.delete_task(TEST_USER_ID, event_id)
             except Exception as clean_e: log_warning("...", f"Cleanup error for {event_id}: {clean_e}")
        # --- End Cleanup ---
        log_info("run_all_tests_simple", "run_task_crud_tests", f"--- Finished Task CRUD Tests (Success: {overall_success}) ---")
    return overall_success


# (Keep run_config_tests function as previously provided)
def run_config_tests() -> bool:
    """Tests get/update preferences via ConfigManagerService."""
    log_info("run_all_tests_simple", "run_config_tests", "--- Starting Config Manager Tests ---")
    overall_success = True
    original_prefs = {}
    try:
        # Ensure user exists
        if not _setup_test_user(TEST_USER_ID): return False

        # 1. Get Initial Prefs
        prefs = config_manager.get_preferences(TEST_USER_ID)
        if not _assert(prefs is not None and prefs.get('status') == 'new', "Failed to get initial default prefs"): return False
        original_prefs = prefs.copy() # Store for restoration
        log_info("run_all_tests_simple", "run_config_tests", f"Initial Prefs: {prefs}")

        # 2. Update Prefs
        updates = {"Morning_Summary_Time": "08:30", "Calendar_Enabled": True, "status": "initiating"}
        success = config_manager.update_preferences(TEST_USER_ID, updates)
        if not _assert(success, "update_preferences call failed"): return False

        # 3. Verify Persistent Update
        prefs_after = user_registry.get_user_preferences(TEST_USER_ID) # Read registry directly
        if not _assert(prefs_after.get("Morning_Summary_Time") == "08:30", "Morning time not updated in registry"): overall_success = False
        if not _assert(prefs_after.get("Calendar_Enabled") is True, "Calendar enabled not updated in registry"): overall_success = False
        if not _assert(prefs_after.get("status") == "initiating", "Status not updated in registry"): overall_success = False

        # 4. Verify In-Memory Update (via AgentStateManager)
        mem_prefs = agent_state_manager.get_preferences_from_state(TEST_USER_ID)
        if not _assert(mem_prefs and mem_prefs.get("Morning_Summary_Time") == "08:30", "Morning time not updated in memory"): overall_success = False
        if not _assert(mem_prefs and mem_prefs.get("Calendar_Enabled") is True, "Calendar enabled not updated in memory"): overall_success = False
        if not _assert(mem_prefs and mem_prefs.get("status") == "initiating", "Status not updated in memory"): overall_success = False

        # 5. Test set_user_status helper
        success = config_manager.set_user_status(TEST_USER_ID, "active")
        if not _assert(success, "set_user_status call failed"): return False
        prefs_final = user_registry.get_user_preferences(TEST_USER_ID)
        mem_prefs_final = agent_state_manager.get_preferences_from_state(TEST_USER_ID)
        if not _assert(prefs_final.get("status") == "active" and mem_prefs_final.get("status") == "active", "set_user_status failed to update state"): overall_success = False

        # 6. Test initiate_calendar_auth (without actually clicking link)
        auth_result = config_manager.initiate_calendar_auth(TEST_USER_ID)
        if not _assert(auth_result and isinstance(auth_result, dict), "initiate_calendar_auth failed"): return False
        log_info("run_all_tests_simple", "run_config_tests", f"Initiate Auth Result: {auth_result}")
        # Check status based on current prefs (should be 'pending' if token file doesn't exist)
        token_file_exists = os.path.exists(prefs_final.get("token_file","")) if prefs_final.get("token_file") else False
        expected_status = "token_exists" if token_file_exists else "pending"
        if not _assert(auth_result.get("status") == expected_status, f"Auth status mismatch: Expected {expected_status}, Got {auth_result.get('status')}"): overall_success = False

    except ImportError as ie:
        log_error("run_all_tests_simple", "run_config_tests", f"Import Error - Missing Service/Module?: {ie}", ie)
        overall_success = False
    except Exception as e:
        log_error("run_all_tests_simple", "run_config_tests", "Test suite crashed", e)
        overall_success = False
    finally:
        # Restore original prefs if possible
        if original_prefs and 'status' in original_prefs: # Check if we stored original
            log_info("run_all_tests_simple", "run_config_tests", "Attempting to restore original preferences...")
            config_manager.update_preferences(TEST_USER_ID, original_prefs)
        log_info("run_all_tests_simple", "run_config_tests", f"--- Finished Config Manager Tests (Success: {overall_success}) ---")
    return overall_success


# --- Main Test Runner Function ---
# (Keep run_all function as previously provided - it calls the test suites)
def run_all():
    """Runs all configured test functions sequentially."""
    if not MODULE_IMPORT_SUCCESS:
        log_error("run_all_tests_simple", "run_all", "Cannot run tests due to initial import errors.")
        return False

    log_info("run_all_tests_simple", "run_all", "*** === Running All Phase 2 Service Tests === ***")
    start_time = time.time()
    overall_success = True
    tests_run = 0
    tests_failed = 0

    # --- List of test functions to run ---
    test_suites_to_run = [
        run_config_tests, # Test config/prefs first
        run_task_crud_tests, # Test task CRUD after
        # Add other service tests here (e.g., TaskQueryService specific tests)
    ]
    # -------------------------------------

    for run_suite_func in test_suites_to_run:
        tests_run += 1
        suite_name = run_suite_func.__name__
        log_info("run_all_tests_simple", "run_all", f"--- Running Suite: {suite_name} ---")
        suite_success = False
        try:
            suite_success = run_suite_func() # Check boolean return
            if not suite_success:
                overall_success = False
                tests_failed += 1
                # Error should be logged within the test function itself
                log_error("run_all_tests_simple", "run_all", f"!!! Test Suite Failed: {suite_name} !!!")
        except Exception as e:
            overall_success = False
            tests_failed += 1
            log_error("run_all_tests_simple", "run_all", f"!!! Test Suite Crashed: {suite_name} !!!", e)

        # No need for else block, failure logged above or within suite
        log_info("run_all_tests_simple", "run_all", f"--- Finished Suite: {suite_name} (Success: {suite_success}) ---")


    # --- Final Cleanup ---
    _cleanup_user_data(TEST_USER_ID)
    # --------------------

    end_time = time.time()
    duration = end_time - start_time
    log_info("run_all_tests_simple", "run_all", "*** === Test Run Summary === ***")
    log_info("run_all_tests_simple", "run_all", f"Duration: {duration:.2f} seconds")
    log_info("run_all_tests_simple", "run_all", f"Total Suites Executed: {tests_run}")
    log_info("run_all_tests_simple", "run_all", f"Overall Result: {'PASS' if overall_success else 'FAIL'} ({tests_failed} failed)")
    log_info("run_all_tests_simple", "run_all", "*** ======================== ***")

    return overall_success # Return overall success status


if __name__ == "__main__":
    # Allow running directly
    from dotenv import load_dotenv
    dotenv_path = os.path.join(project_root, '.env')
    if os.path.exists(dotenv_path): load_dotenv(dotenv_path=dotenv_path)
    else: log_warning("run_all_tests_simple", "main", ".env file not found.")

    # Run the tests only if imports were successful
    if MODULE_IMPORT_SUCCESS:
        run_all()
    else:
        print("\nERROR: Could not run tests due to missing modules. Please check imports.")
        sys.exit(1)