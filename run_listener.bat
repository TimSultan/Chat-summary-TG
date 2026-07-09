@echo off
cd /d "%~dp0"
echo Starting the listener. Leave this window open. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" listener.py
echo.
pause
