@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Please run setup.ps1 to prepare the project's Python environment.
    if "%~1"=="" pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0clear-conversations.py" %*
set "TAVERN_CLEAR_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %TAVERN_CLEAR_EXIT%
