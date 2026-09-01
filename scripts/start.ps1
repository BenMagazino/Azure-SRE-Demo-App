$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$appPath = Join-Path $repoRoot "app\main.py"

function Find-PythonRuntime {
  $candidates = @()
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    $candidates += [PSCustomObject]@{ Path = $launcher.Source; PrefixArgs = @("-3") }
  }

  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python -and $python.Source -notlike "*\WindowsApps\python.exe") {
    $candidates += [PSCustomObject]@{ Path = $python.Source; PrefixArgs = @() }
  }

  $commonPaths = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe")
  )
  foreach ($path in $commonPaths) {
    if (Test-Path $path) {
      $prefixArgs = if ($path.EndsWith("\py.exe")) { @("-3") } else { @() }
      $candidates += [PSCustomObject]@{ Path = $path; PrefixArgs = $prefixArgs }
    }
  }

  foreach ($candidate in $candidates) {
    $versionText = (& $candidate.Path @($candidate.PrefixArgs) --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
      continue
    }
    $match = [regex]::Match($versionText, "Python\s+(\d+)\.(\d+)\.(\d+)")
    if ($match.Success) {
      $major = [int]$match.Groups[1].Value
      $minor = [int]$match.Groups[2].Value
      $patch = [int]$match.Groups[3].Value
      if (
        $major -gt 3 -or
        ($major -eq 3 -and $minor -gt 14) -or
        ($major -eq 3 -and $minor -eq 14 -and $patch -ge 7)
      ) {
        $candidate | Add-Member -NotePropertyName Version -NotePropertyValue $versionText
        return $candidate
      }
    }
  }

  return $null
}

$runtime = Find-PythonRuntime
if (-not $runtime) {
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $winget) {
    throw @"
Python 3.14.7 or newer is required, and WinGet is unavailable.
Install Python from https://www.python.org/downloads/windows/ and rerun this script.
"@
  }

  Write-Host "Python 3.14.7 or newer was not found." -ForegroundColor Yellow
  Write-Host "Installing or updating Python 3.14.7 for the current user..."
  & $winget.Source install `
    --id Python.Python.3.14 `
    --exact `
    --version 3.14.7 `
    --scope user `
    --force `
    --disable-interactivity `
    --accept-package-agreements `
    --accept-source-agreements
  if ($LASTEXITCODE -ne 0) {
    throw "WinGet was unable to install or update Python 3.14.7."
  }

  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $env:PATH = "$userPath;$machinePath"
  $runtime = Find-PythonRuntime
  if (-not $runtime) {
    throw "Python was updated but could not be started. Open a new PowerShell window and rerun this script."
  }
}

Write-Host "Using $($runtime.Version)"
Write-Host "Starting the Azure SRE Agent onboarding wizard..." -ForegroundColor Green
$launchArgs = @($runtime.PrefixArgs) + @($appPath)
& $runtime.Path @launchArgs
exit $LASTEXITCODE
