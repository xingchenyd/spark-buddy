@echo off
rem ============================================================
rem  SparkBuddy - One-click launcher (silent)
rem  Runs the panel hidden in background (pythonw, no window),
rem  waits until the port is really up, opens the browser, then
rem  this launcher window closes by itself.
rem  To stop the panel: click "停止面板" in the panel settings.
rem ============================================================
setlocal
cd /d "%~dp0"

set PYW=%~dp0.venv\Scripts\pythonw.exe
if not exist "%PYW%" (
    echo [ERROR] venv pythonw not found: %PYW%
    pause
    exit /b 1
)

echo Starting SparkBuddy panel in background...
rem pythonw = no console window; start = don't block this launcher.
rem If the panel is already running, this new instance fails to
rem bind port 8765 and exits silently - harmless.
start "" "%PYW%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765

echo Waiting for the panel to come up (first start can be slow)...
set /a TRIES=0
:waitup
set /a TRIES+=1
if %TRIES% GTR 40 (
    echo [ERROR] Panel did not start within 40 seconds.
    echo Possible causes: antivirus blocked python, or install.bat was not run.
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
exit /b 0
