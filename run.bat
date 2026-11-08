@echo off
cd /d "%~dp0"
echo Installing requirements (first time only)...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Python was not found. Install Python 3.9+ from python.org and tick "Add Python to PATH", then run this again.
  pause
  exit /b 1
)
start "" http://localhost:5000
echo Server running. Keep this window open. Press Ctrl+C to stop.
python app.py
pause
