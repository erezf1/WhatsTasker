# llm_interface.py
import os
import openai
import instructor
import threading
from tools.logger import log_info, log_error

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_client = None
_client_lock = threading.Lock()

def get_instructor_client():
    """Initializes and returns a singleton, instructor-patched OpenAI client."""
    global _client
    if not OPENAI_API_KEY:
        log_error("llm_interface", "get_instructor_client", "OPENAI_API_KEY not found in environment.")
        return None

    with _client_lock:
        if _client is None:
            try:
                log_info("llm_interface", "get_instructor_client", "Initializing instructor-patched OpenAI client...")
                # Initialize the OpenAI client
                base_client = openai.OpenAI(api_key=OPENAI_API_KEY)
                # Patch it with Instructor
                _client = instructor.patch(base_client)
                log_info("llm_interface", "get_instructor_client", "Instructor-patched OpenAI client initialized.")
            except Exception as e:
                log_error("llm_interface", "get_instructor_client", f"Failed to initialize OpenAI client: {e}", e)
                _client = None # Ensure it remains None on failure
    return _client
