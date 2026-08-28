$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repoRoot ".build-venv"
$python = Get-Command python -ErrorAction SilentlyContinue
$staleOneFileExecutable = Join-Path $repoRoot "dist\AzureSREAgentDemo.exe"

# Remove the obsolete one-file artifact created by earlier builds. The
# supported package is the complete dist\AzureSREAgentDemo directory.
if (Test-Path $staleOneFileExecutable) {
  Remove-Item $staleOneFileExecutable -Force
}

if (-not $python) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if (-not $launcher) {
    throw "Python is required. Run .\scripts\bootstrap-dev.ps1 first."
  }
  & $launcher.Source -3 -m venv $venv
} else {
  & $python.Source -m venv $venv
}

$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip pyinstaller

Push-Location $repoRoot
try {
  & $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name AzureSREAgentDemo `
    --add-data "app\static;static" `
    --add-data "vendor\starter-lab;vendor\starter-lab" `
    app\main.py
} finally {
  Pop-Location
}

Write-Host ""
Write-Host "Windows executable created:" -ForegroundColor Green
Write-Host (Join-Path $repoRoot "dist\AzureSREAgentDemo\AzureSREAgentDemo.exe")
