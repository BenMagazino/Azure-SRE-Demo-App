param(
  [switch]$Launch
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "Azure SRE Agent demo - developer bootstrap" -ForegroundColor Cyan

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
  $python = Get-Command python -ErrorAction SilentlyContinue
}

if (-not $python) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Python is missing and winget is unavailable. Install Python 3.11 or newer, then rerun this script."
  }

  Write-Host "Python is missing. Installing Python 3.12 for the current user..."
  winget install --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  Write-Host "Python was installed. Open a new PowerShell window and rerun this script."
  exit 0
}

if ($python.Name -eq "py.exe") {
  $version = & $python.Source -3 --version
  $runArgs = @("-3", (Join-Path $repoRoot "app\main.py"))
} else {
  $version = & $python.Source --version
  $runArgs = @((Join-Path $repoRoot "app\main.py"))
}

Write-Host "Using $version"
Write-Host "No virtual environment or package installation is required."
Write-Host "Bootstrap complete." -ForegroundColor Green

if ($Launch) {
  Write-Host "Starting the local onboarding wizard..."
  & $python.Source @runArgs
} else {
  Write-Host "Run .\scripts\start.ps1 to launch the app."
}
