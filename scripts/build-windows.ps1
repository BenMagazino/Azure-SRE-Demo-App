$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonVersion = "3.14.7"
$runtimeArchiveName = "python-$pythonVersion-embed-amd64.zip"
$runtimeUri = "https://www.python.org/ftp/python/$pythonVersion/$runtimeArchiveName"
$runtimeSha256 = "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15"
$downloadDirectory = Join-Path $repoRoot "build\downloads"
$runtimeArchive = Join-Path $downloadDirectory $runtimeArchiveName
$staleOneFileExecutable = Join-Path $repoRoot "dist\AzureSREAgentDemo.exe"
$packageDirectory = Join-Path $repoRoot "dist\AzureSREAgentDemo"
$packageArchive = Join-Path $repoRoot "dist\AzureSREAgentDemo-portable-win-x64.zip"
$stagingRoot = Join-Path $repoRoot "build\package-output"
$stagingPackage = Join-Path $stagingRoot "AzureSREAgentDemo"
$stagingRuntime = Join-Path $stagingPackage "python"
$launcherSource = Join-Path $repoRoot "packaging\windows\Start Azure SRE Agent Demo.cmd"
$splashSource = Join-Path $repoRoot "packaging\windows\Show-Splash.ps1"
$readmeSource = Join-Path $repoRoot "packaging\windows\README.txt"

function Test-RuntimeArchive {
  if (-not (Test-Path -LiteralPath $runtimeArchive)) {
    return $false
  }
  return (Get-FileHash -LiteralPath $runtimeArchive -Algorithm SHA256).Hash -eq $runtimeSha256
}

if (Test-Path $stagingRoot) {
  Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stagingRuntime -Force | Out-Null
New-Item -ItemType Directory -Path $downloadDirectory -Force | Out-Null

if (-not (Test-RuntimeArchive)) {
  if (Test-Path -LiteralPath $runtimeArchive) {
    Remove-Item -LiteralPath $runtimeArchive -Force
  }
  $partialArchive = "$runtimeArchive.download"
  try {
    Write-Host "Downloading the official Python $pythonVersion embeddable runtime..."
    Invoke-WebRequest -Uri $runtimeUri -OutFile $partialArchive -UseBasicParsing
    if ((Get-FileHash -LiteralPath $partialArchive -Algorithm SHA256).Hash -ne $runtimeSha256) {
      throw "The downloaded Python runtime did not match its pinned SHA-256 checksum."
    }
    Move-Item -LiteralPath $partialArchive -Destination $runtimeArchive -Force
  } finally {
    if (Test-Path -LiteralPath $partialArchive) {
      Remove-Item -LiteralPath $partialArchive -Force
    }
  }
}

Expand-Archive -LiteralPath $runtimeArchive -DestinationPath $stagingRuntime
$runtimePython = Join-Path $stagingRuntime "python.exe"
$runtimeSignature = Get-AuthenticodeSignature -LiteralPath $runtimePython
if (
  $runtimeSignature.Status -ne "Valid" -or
  $runtimeSignature.SignerCertificate.Subject -notlike "*Python Software Foundation*"
) {
  throw "The embedded Python executable does not have a valid Python Software Foundation signature."
}

New-Item -ItemType Directory -Path (Join-Path $stagingPackage "app") | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "app\main.py") -Destination (Join-Path $stagingPackage "app\main.py")
Copy-Item -LiteralPath (Join-Path $repoRoot "app\static") -Destination (Join-Path $stagingPackage "app") -Recurse
New-Item -ItemType Directory -Path (Join-Path $stagingPackage "vendor") | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "vendor\starter-lab") -Destination (Join-Path $stagingPackage "vendor") -Recurse
Copy-Item -LiteralPath $launcherSource -Destination $stagingPackage
Copy-Item -LiteralPath $splashSource -Destination $stagingPackage
Copy-Item -LiteralPath $readmeSource -Destination $stagingPackage

& $runtimePython --version
if ($LASTEXITCODE -ne 0) {
  throw "The embedded Python runtime could not be started."
}

try {
  if (Test-Path -LiteralPath $staleOneFileExecutable) {
    Remove-Item -LiteralPath $staleOneFileExecutable -Force
  }
  if (-not (Test-Path $packageDirectory)) {
    New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
  }
  & robocopy `
    $stagingPackage `
    $packageDirectory `
    /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
  $robocopyExitCode = $LASTEXITCODE
  if ($robocopyExitCode -ge 8) {
    throw "Robocopy failed with exit code $robocopyExitCode."
  }
  $global:LASTEXITCODE = 0
} catch {
  throw "Unable to update the Windows package. Close any running portable demo and try again. $($_.Exception.Message)"
}

if (Test-Path -LiteralPath $packageArchive) {
  Remove-Item -LiteralPath $packageArchive -Force
}
Compress-Archive -LiteralPath $packageDirectory -DestinationPath $packageArchive -CompressionLevel Optimal

if (-not (Test-Path (Join-Path $packageDirectory "Start Azure SRE Agent Demo.cmd"))) {
  throw "The portable package could not be copied to its distribution folder."
}
if (-not (Test-Path (Join-Path $packageDirectory "Show-Splash.ps1"))) {
  throw "The startup splash could not be copied to the distribution folder."
}
if (Test-Path $stagingRoot) {
  Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

Write-Host ""
Write-Host "Portable Windows package created:" -ForegroundColor Green
Write-Host $packageDirectory
Write-Host $packageArchive
