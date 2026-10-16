@echo off
rem ============================================================
rem  SparkBuddy - One-click launcher
rem  Starts the web panel on http://127.0.0.1:8765, WAITS until
rem  it is really listening, then opens the browser.
rem  Keep the "SparkBuddy Panel" window open. Close it to stop.
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
rem cmd /c wrapper: if uvicorn crashes, the window stays open with the error
start "SparkBuddy Panel" cmd /c ""%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765 & echo. & echo [!] Panel process exited. Read the error above if any. & pause >nul"

echo Waiting for the panel to come up (first start after reboot can be slow)...
set /a TRIES=0
:waitup
set /a TRIES+=1
if %TRIES% GTR 40 (
    echo [ERROR] Panel did not start within 40 seconds.
    echo Possible causes: port 8765 already occupied, or antivirus blocked python.
    pause
    exit /b 1
)
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8765);$c.Close();exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 >nul
    goto waitup
)

echo Panel is up - opening browser...
start "" http://127.0.0.1:8765
echo.
echo SparkBuddy is running in the "SparkBuddy Panel" window.
echo Keep that window open while tasks are scheduled; close it to stop.
timeout /t 8 >nul
endlocal
