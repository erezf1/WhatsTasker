# extract_code.py
import os
import re

CODE_TXT_FILE = "code.txt" # Input file containing all code snippets
START_MARKER_PREFIX = "=== START FILE: "
END_MARKER_PREFIX = "=== END FILE: "
MARKER_SUFFIX = " ==="

def write_code_to_file(file_path, code_lines):
    """Creates directories and writes extracted code lines to a file."""
    full_path = os.path.normpath(file_path)
    try:
        # Create directory structure if it doesn't exist
        dir_path = os.path.dirname(full_path)
        if dir_path: # Only create if path includes directory separators
            os.makedirs(dir_path, exist_ok=True)
            print(f"   Ensured directory exists: {dir_path}")

        # Write the extracted code
        with open(full_path, "w", encoding="utf-8") as f:
            # Join lines, preserving original endings (usually \n)
            f.write("".join(code_lines))
        print(f"   Successfully wrote file: {full_path}")
        return True
    except IOError as e:
        print(f"   ❌ ERROR writing file {full_path}: {e}")
        return False
    except Exception as e:
        print(f"   ❌ ERROR processing file {full_path}: {e}")
        return False

def extract_files_from_codetxt(input_filename=CODE_TXT_FILE):
    """Reads the combined code file and extracts individual files."""
    current_file_path = None
    is_inside_file = False
    current_code_lines = []
    files_written = 0
    files_failed = 0

    print(f"--- Starting code extraction from '{input_filename}' ---")

    if not os.path.exists(input_filename):
        print(f"❌ ERROR: Input file '{input_filename}' not found.")
        return

    try:
        with open(input_filename, "r", encoding="utf-8") as f_in:
            for line_num, line in enumerate(f_in, 1):
                # Check for Start Marker
                if line.startswith(START_MARKER_PREFIX) and line.endswith(MARKER_SUFFIX + "\n"):
                    if is_inside_file:
                         print(f"   ⚠️ WARNING: Found new START marker on line {line_num} before finding END marker for '{current_file_path}'. Previous content might be lost.")
                    path_start_index = len(START_MARKER_PREFIX)
                    path_end_index = len(line) - len(MARKER_SUFFIX + "\n")
                    current_file_path = line[path_start_index:path_end_index].strip()
                    if not current_file_path:
                         print(f"   ⚠️ WARNING: Could not extract file path from START marker on line {line_num}. Skipping.")
                         current_file_path = None
                         is_inside_file = False
                         continue
                    is_inside_file = True
                    current_code_lines = []
                    print(f"\n>>> Found start marker for: {current_file_path}")
                    continue # Skip marker line itself

                # Check for End Marker
                elif line.startswith(END_MARKER_PREFIX) and line.endswith(MARKER_SUFFIX + "\n"):
                    if not is_inside_file:
                        print(f"   ⚠️ WARNING: Found END marker on line {line_num} but wasn't inside a file block. Ignoring.")
                        continue
                    # Optional: Verify end marker path matches current_file_path
                    expected_end_path = line[len(END_MARKER_PREFIX):len(line) - len(MARKER_SUFFIX + "\n")].strip()
                    if expected_end_path != current_file_path:
                         print(f"   ⚠️ WARNING: End marker path '{expected_end_path}' on line {line_num} doesn't match current file path '{current_file_path}'.")

                    print(f"<<< Found end marker for: {current_file_path}")
                    if write_code_to_file(current_file_path, current_code_lines):
                        files_written += 1
                    else:
                        files_failed += 1

                    # Reset state
                    current_file_path = None
                    is_inside_file = False
                    current_code_lines = []
                    continue # Skip marker line itself

                # Collect code lines
                elif is_inside_file:
                    current_code_lines.append(line)

            # Check if file ended unexpectedly while inside a block
            if is_inside_file:
                print(f"   ⚠️ WARNING: Reached end of '{input_filename}' while still inside file block for '{current_file_path}'. Writing remaining content.")
                if write_code_to_file(current_file_path, current_code_lines):
                    files_written += 1
                else:
                    files_failed += 1

    except Exception as e:
        print(f"\n❌ ERROR: An unexpected error occurred reading '{input_filename}': {e}")
        files_failed += 1 # Count as failure

    print(f"\n--- Extraction finished ---")
    print(f"   Files successfully written: {files_written}")
    print(f"   Errors encountered: {files_failed}")

if __name__ == "__main__":
    extract_files_from_codetxt() # Expects code.txt in the same directory
    # Or specify a different input file:
    # extract_files_from_codetxt("my_custom_code_dump.txt")