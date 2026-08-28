$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repoRoot ".build-venv"
$python = Get-Command python -ErrorAction SilentlyContinue
$staleOneFileExecutable = Join-Path $repoRoot "dist\AzureSREAgentDemo.exe"
$packageDirectory = Join-Path $repoRoot "dist\AzureSREAgentDemo"
$stagingRoot = Join-Path $repoRoot "build\package-output"
$stagingPackage = Join-Path $stagingRoot "AzureSREAgentDemo"

# Remove the obsolete one-file artifact created by earlier builds. The
# supported package is the complete dist\AzureSREAgentDemo directory.
if (Test-Path $staleOneFileExecutable) {
  Remove-Item $staleOneFileExecutable -Force
}
if (Test-Path $stagingRoot) {
  Remove-Item $stagingRoot -Recurse -Force
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
if ($LASTEXITCODE -ne 0) {
  throw "Unable to create the packaging environment."
}

$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip pyinstaller
if ($LASTEXITCODE -ne 0) {
  throw "Unable to install the packaging tools."
}

Push-Location $repoRoot
try {
  & $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name AzureSREAgentDemo `
    --distpath $stagingRoot `
    --add-data "app\static;static" `
    --add-data "vendor\starter-lab;vendor\starter-lab" `
    app\main.py
  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to create the Windows package."
  }
} finally {
  Pop-Location
}

$stagedExecutable = Join-Path $stagingPackage "AzureSREAgentDemo.exe"
if (-not (Test-Path $stagedExecutable)) {
  throw "Packaging completed without producing the expected executable."
}

try {
  if (Test-Path $packageDirectory) {
    Get-ChildItem -LiteralPath $packageDirectory -Force | ForEach-Object {
      Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
  } else {
    New-Item -ItemType Directory -Path $packageDirectory | Out-Null
  }
  Get-ChildItem -LiteralPath $stagingPackage -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packageDirectory -Recurse -Force
  }
} catch {
  throw "Unable to update the Windows package. Close any running AzureSREAgentDemo executable and try again. $($_.Exception.Message)"
} finally {
  if (Test-Path $stagingRoot) {
    Remove-Item $stagingRoot -Recurse -Force
  }
}

$executable = Join-Path $packageDirectory "AzureSREAgentDemo.exe"
if (-not (Test-Path $executable)) {
  throw "The Windows package could not be copied to its distribution folder."
}

Write-Host ""
Write-Host "Windows executable created:" -ForegroundColor Green
Write-Host $executable
