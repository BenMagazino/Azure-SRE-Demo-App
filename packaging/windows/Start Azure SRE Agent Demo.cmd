@echo off
setlocal
set "AZURE_SRE_AGENT_PORTABLE=1"
set "AZURE_SRE_DEMO_NO_BROWSER=1"

start "" /b "%~dp0python\pythonw.exe" "%~dp0app\main.py" >nul 2>&1
start "" /b powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Show-Splash.ps1" >nul 2>&1
exit /b %ERRORLEVEL%
