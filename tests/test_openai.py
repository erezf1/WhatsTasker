import os
from dotenv import load_dotenv
try:
    from langchain_openai import ChatOpenAI
    print("Import OK")
except ImportError as e:
    print(f"Import Failed: {e}")
    exit()

load_dotenv() # Load .env file
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("API Key not found in environment!")
    exit()
print("API Key loaded.")

try:
    print("Attempting ChatOpenAI initialization...")
    # Use the same params as your chain_builders
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0.0,
        openai_api_key=api_key,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    print("ChatOpenAI initialized SUCCESSFULLY.")
    # Optional: Try a simple invoke
    # print("Attempting invoke...")
    # response = llm.invoke("Test prompt")
    # print(f"Invoke response: {response}")

except Exception as e:
    print(f"ERROR during ChatOpenAI initialization: {e}")
    import traceback
    traceback.print_exc() # Print full traceback

print("Test script finished.")