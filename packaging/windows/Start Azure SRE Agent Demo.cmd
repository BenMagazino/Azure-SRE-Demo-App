@echo off
setlocal
title Azure SRE Agent Demo
set "AZURE_SRE_AGENT_PORTABLE=1"

"%~dp0python\python.exe" "%~dp0app\main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
  echo.
  echo Azure SRE Agent Demo stopped with exit code %APP_EXIT_CODE%.
  echo Review the diagnostic log path shown above for details.
  pause
)

exit /b %APP_EXIT_CODE%
