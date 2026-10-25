@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "TAVERN_START_EXIT=%ERRORLEVEL%"
if not "%TAVERN_START_EXIT%"=="0" pause
exit /b %TAVERN_START_EXIT%
