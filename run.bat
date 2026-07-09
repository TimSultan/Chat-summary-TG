@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup - this only happens once...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create a virtual environment. Make sure Python is installed
        echo and on PATH, then run this again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Failed to install dependencies - see the error above.
        pause
        exit /b 1
    )
    echo Setup complete.
    echo.
)

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo No .env file found - created one from .env.example.
    echo Fill in TELEGRAM_API_ID, TELEGRAM_API_HASH, and OPENAI_API_KEY, then run this again.
    notepad ".env"
    pause
    exit /b 1
)

".venv\Scripts\python.exe" gui.py
if errorlevel 1 (
    echo.
    echo The app exited with an error - see above.
    pause
)

endlocal
