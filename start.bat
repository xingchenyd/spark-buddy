@echo off
rem ============================================================
rem  SparkBuddy (huohua guanjia) - One-click launcher
rem  Starts the web panel on http://127.0.0.1:8765 and opens it
rem  in the default browser. Close the console window to stop.
rem ============================================================
setlocal
cd /d "%~dp0"

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    pause
    exit /b 1
)

echo Starting SparkBuddy panel on http://127.0.0.1:8765 ...
start "SparkBuddy Panel" "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765

timeout /t 2 >nul
start "" http://127.0.0.1:8765

echo.
echo SparkBuddy is running. Keep the "SparkBuddy Panel" window open
echo while sending tasks are scheduled. Close it to stop.
timeout /t 6 >nul
endlocal
