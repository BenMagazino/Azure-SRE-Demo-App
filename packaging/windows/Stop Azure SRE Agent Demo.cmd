@echo off
setlocal
title Stop Azure SRE Agent Demo

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "try { $session = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/session' -TimeoutSec 5; $headers = @{'X-SRE-Session' = $session.token}; $result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/shutdown' -Headers $headers -TimeoutSec 5; if (-not $result.shutting_down) { throw 'The backend did not accept the shutdown request.' }; Write-Host 'Azure SRE Agent Demo is stopping.' -ForegroundColor Green } catch { Write-Host 'No responsive Azure SRE Agent Demo backend was found.' -ForegroundColor Yellow; exit 1 }"
set "STOP_EXIT_CODE=%ERRORLEVEL%"

if not "%STOP_EXIT_CODE%"=="0" (
  echo.
  pause
  exit /b %STOP_EXIT_CODE%
)

ping.exe -n 3 127.0.0.1 >nul
exit /b 0
