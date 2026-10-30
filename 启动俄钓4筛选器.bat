@echo off
setlocal EnableExtensions
title RF4 Local Recommendation Tool

cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 goto :no_python

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating local Python environment...
    python.exe -m venv ".venv"
    if errorlevel 1 goto :venv_failed
)

echo [2/4] Checking Python packages...
".venv\Scripts\python.exe" -c "import streamlit,pandas,numpy,requests,bs4,lxml,plotly,playwright" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages. This can take several minutes...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
    if errorlevel 1 goto :install_failed
)

echo [3/4] Local cache is ready. Online refresh is available inside the app.

powershell.exe -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8501/_stcore/health' -TimeoutSec 2; if ($r.Content -eq 'ok') { exit 0 } } catch {}; exit 1" >nul 2>nul
if not errorlevel 1 goto :already_running

echo [4/4] Starting the app at http://localhost:8501
echo Keep this window open while using the app.
".venv\Scripts\python.exe" -m streamlit run "app.py" --server.port 8501 --server.showEmailPrompt false --browser.gatherUsageStats false
goto :finished

:already_running
echo [4/4] The app is already running at http://localhost:8501
start "" "http://localhost:8501"
goto :finished

:no_python
echo ERROR: Python was not found in PATH.
echo Install Python 3.11 or newer, then run this file again.
goto :failed

:venv_failed
echo ERROR: Could not create the local Python environment.
goto :failed

:install_failed
echo ERROR: Required Python packages could not be installed.
echo Check your network connection, then run this file again.
goto :failed

:failed
echo.
echo Startup failed. Review the error message above.
pause
exit /b 1

:finished
echo.
echo The app has stopped.
pause

