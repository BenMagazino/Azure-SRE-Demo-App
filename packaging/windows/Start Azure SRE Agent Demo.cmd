@echo off
setlocal
set "AZURE_SRE_AGENT_PORTABLE=1"

start "" /b "%~dp0python\pythonw.exe" "%~dp0app\main.py" >nul 2>&1
exit /b %ERRORLEVEL%
