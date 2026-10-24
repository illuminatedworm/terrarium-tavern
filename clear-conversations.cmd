@echo off
setlocal
set "TAVERN_CLEAR_PYTHON=%~dp0runtime\python\python.exe"
if not exist "%TAVERN_CLEAR_PYTHON%" set "TAVERN_CLEAR_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TAVERN_CLEAR_PYTHON%" (
    echo Please run setup.ps1 to prepare the project's Python environment.
    if "%~1"=="" pause
    exit /b 1
)
"%TAVERN_CLEAR_PYTHON%" -X utf8 "%~dp0clear-conversations.py" %*
set "TAVERN_CLEAR_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %TAVERN_CLEAR_EXIT%
