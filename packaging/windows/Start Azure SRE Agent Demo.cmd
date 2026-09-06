@echo off
setlocal
set "AZURE_SRE_AGENT_PORTABLE=1"
set "AZURE_SRE_DEMO_NO_BROWSER=1"
set "AZURE_SRE_DEMO_SHORTCUT=%~dp0..\Azure SRE Agent Demo.lnk"
set "AZURE_SRE_DEMO_SHORTCUT_TEMPLATE=%~dp0Azure SRE Agent Demo.link-template"

attrib.exe -R "%AZURE_SRE_DEMO_SHORTCUT%" >nul 2>&1
copy /Y "%AZURE_SRE_DEMO_SHORTCUT_TEMPLATE%" "%AZURE_SRE_DEMO_SHORTCUT%" >nul 2>&1
attrib.exe +R "%AZURE_SRE_DEMO_SHORTCUT%" >nul 2>&1

start "" /b "%~dp0python\pythonw.exe" "%~dp0main.py" %* >nul 2>&1
start "" /b powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Show-Splash.ps1" >nul 2>&1
exit /b %ERRORLEVEL%
