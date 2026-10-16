@echo off
rem ============================================================
rem  SparkBuddy - One-click launcher (silent)
rem  Preferred path: the panel process is created via WMI, so it
rem  is fully detached from this window - closing (or losing)
rem  this window can never kill the panel. If WMI is blocked,
rem  we fall back to a direct launch.
rem  The panel opens your browser by itself when it is ready;
rem  cold start after reboot can take up to about one minute.
rem  To stop the panel: click "停止面板" in the panel settings.
rem ============================================================
setlocal
cd /d "%~dp0"

set PYW=%~dp0.venv\Scripts\pythonw.exe
if not exist "%PYW%" (
    echo [ERROR] venv pythonw not found: %PYW%
    echo Please run install.bat first.
    pause
    exit /b 1
)

echo Starting SparkBuddy in background...
powershell -NoProfile -Command "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='\"%PYW%\" \"%~dp0launch_panel.py\"';CurrentDirectory='%~dp0'}; if($r.ReturnValue -ne 0){exit 1}" >nul 2>&1
if errorlevel 1 (
    echo WMI launch unavailable - using fallback...
    start "" "%PYW%" "%~dp0launch_panel.py"
)
echo Done. Your browser will open automatically when the panel is ready.
echo (After a reboot this can take up to about one minute - just wait.)
timeout /t 3 >nul
exit /b 0
