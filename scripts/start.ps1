$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue

if ($python) {
  & $python.Source (Join-Path $repoRoot "app\main.py")
  exit $LASTEXITCODE
}

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
  throw "Python 3.9 or newer is required. Run .\scripts\bootstrap-dev.ps1 first."
}

& $python.Source -3 (Join-Path $repoRoot "app\main.py")
exit $LASTEXITCODE
