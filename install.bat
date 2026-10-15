@echo off
rem ============================================================
rem  SparkBuddy - First-time setup (venv + dependencies)
rem  Run this ONCE after cloning. Then use start.bat every day.
rem  Requires Python 3.10+ installed and on PATH.
rem ============================================================
setlocal
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3.10+ first.
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo [1/3] Creating virtual environment ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
)

echo [2/3] Installing Python dependencies ...
"%PY%" -m pip install -r requirements.txt -i https://pypi.org/simple
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your network and retry.
    pause
    exit /b 1
)

echo [3/3] Installing Chromium for Playwright (about 150 MB, one-time) ...
"%PY%" -m playwright install chromium

echo.
echo ==============================================
echo  Setup finished! Double-click start.bat
echo  to launch SparkBuddy (http://127.0.0.1:8765)
echo ==============================================
pause
endlocal
