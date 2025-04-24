@echo off
REM Batch script to apply Phase 1 infrastructure changes for WhatsTasker v0.8
REM IMPORTANT: Run this script from the ROOT of your project_v0.8_dev directory!
REM Recommended: Activate your Python virtual environment BEFORE running this script.

echo.
echo [Phase 1] Applying WhatsTasker v0.8 Infrastructure Changes...
echo =============================================================
echo.

REM --- Step 1: Update Python Dependencies ---
echo [1/4] Updating Python dependencies...
echo      Uninstalling old Langchain packages...
pip uninstall langchain langchain-core langchain-openai -y
echo.
echo      Installing Instructor and updated OpenAI...
pip install instructor "openai>=1.0"
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install required packages. Please check pip and your internet connection.
    pause
    exit /b %ERRORLEVEL%
) ELSE (
    echo      Dependencies updated successfully.
)
echo.

REM --- Step 2: Delete Obsolete v0.7 Agent Files ---
echo [2/4] Deleting obsolete v0.7 agent files...
IF EXIST "agents\intention_agent.py" ( del /Q "agents\intention_agent.py" && echo      Deleted agents\intention_agent.py ) ELSE ( echo      agents\intention_agent.py not found. )
IF EXIST "agents\task_agent.py"      ( del /Q "agents\task_agent.py"      && echo      Deleted agents\task_agent.py      ) ELSE ( echo      agents\task_agent.py not found.      )
IF EXIST "agents\config_agent.py"    ( del /Q "agents\config_agent.py"    && echo      Deleted agents\config_agent.py    ) ELSE ( echo      agents\config_agent.py not found.    )
IF EXIST "agents\scheduler_agent.py" ( del /Q "agents\scheduler_agent.py" && echo      Deleted agents\scheduler_agent.py ) ELSE ( echo      agents\scheduler_agent.py not found. )
echo      Obsolete agent files deletion step complete.
echo.

REM --- Step 3: Delete Obsolete Langchain Chains Directory ---
echo [3/4] Deleting obsolete langchain_chains directory...
IF EXIST "langchain_chains" (
    rmdir /S /Q "langchain_chains"
    IF %ERRORLEVEL% NEQ 0 (
        echo WARNING: Could not delete langchain_chains directory completely. Please check permissions or delete manually.
    ) ELSE (
        echo      Deleted langchain_chains directory.
    )
) ELSE (
    echo      langchain_chains directory not found.
)
echo.

REM --- Step 4: Create New v0.8 File Placeholders ---
echo [4/4] Creating new v0.8 file placeholders...

REM Check if agents directory exists, create if not (should exist, but safety check)
IF NOT EXIST "agents" (
    mkdir "agents"
    echo      Created agents directory.
)

REM Create new agent/tool files
echo. > "agents\orchestrator_agent.py" && echo      Created agents\orchestrator_agent.py
echo. > "agents\tool_definitions.py"  && echo      Created agents\tool_definitions.py
echo. > "agents\scheduling_logic.py"  && echo      Created agents\scheduling_logic.py
echo. > "agents\list_reply_logic.py"  && echo      Created agents\list_reply_logic.py

REM Create new LLM interface file (in root for simplicity here)
echo. > "llm_interface.py"            && echo      Created llm_interface.py

echo      Placeholder files created.
echo.

echo =============================================================
echo [Phase 1] Infrastructure changes applied successfully!
echo You can now start implementing the new Orchestrator and Tools.
echo =============================================================
echo.
pause