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
$stagingApplication = Join-Path $stagingPackage "app"
$stagingRuntime = Join-Path $stagingApplication "python"
$stagingVendor = Join-Path $stagingApplication "vendor"
$hiddenLauncherSource = Join-Path $repoRoot "packaging\windows\Launch.vbs"
$launcherSource = Join-Path $repoRoot "packaging\windows\Start Azure SRE Agent Demo.cmd"
$stopLauncherSource = Join-Path $repoRoot "packaging\windows\Stop Azure SRE Agent Demo.cmd"
$splashSource = Join-Path $repoRoot "packaging\windows\Show-Splash.ps1"
$iconSource = Join-Path $repoRoot "app\static\favicon.ico"
$readmeSource = Join-Path $repoRoot "packaging\windows\README.txt"
$shortcutName = "Azure SRE Agent Demo.lnk"
$shortcutTemplateName = "Azure SRE Agent Demo.link-template"

function Write-FixedShortcutString {
  param(
    [IO.BinaryWriter]$Writer,
    [string]$Value,
    [int]$ByteCount,
    [Text.Encoding]$Encoding
  )

  $encoded = $Encoding.GetBytes($Value + [char]0)
  if ($encoded.Length -gt $ByteCount) {
    throw "Shortcut value exceeds its fixed buffer."
  }
  $Writer.Write($encoded)
  $Writer.Write([byte[]]::new($ByteCount - $encoded.Length))
}

function Write-ShortcutEnvironmentBlock {
  param(
    [IO.BinaryWriter]$Writer,
    [uint32]$Signature,
    [string]$Value
  )

  $Writer.Write([uint32]0x314)
  $Writer.Write($Signature)
  Write-FixedShortcutString $Writer $Value 260 ([Text.Encoding]::ASCII)
  Write-FixedShortcutString $Writer $Value 520 ([Text.Encoding]::Unicode)
}

function New-RelativeShortcut {
  param(
    [string]$ShortcutPath,
    [string]$RelativeTarget,
    [string]$IconPath
  )

  $stream = [IO.File]::Create($ShortcutPath)
  $writer = [IO.BinaryWriter]::new($stream)
  try {
    # MS-SHLLINK header with relative/environment paths and no absolute LinkInfo.
    $writer.Write([uint32]0x4C)
    $writer.Write(
      ([Guid]"00021401-0000-0000-C000-000000000046").ToByteArray()
    )
    $writer.Write([uint32]0x000043CC)
    $writer.Write([uint32]0)
    1..3 | ForEach-Object { $writer.Write([uint64]0) }
    $writer.Write([uint32]0)
    $writer.Write([int32]0)
    $writer.Write([uint32]7)
    $writer.Write([uint16]0)
    $writer.Write([uint16]0)
    $writer.Write([uint32]0)
    $writer.Write([uint32]0)

    foreach ($value in @(
      "Azure SRE Agent Demo",
      $RelativeTarget,
      $IconPath
    )) {
      $writer.Write([uint16]$value.Length)
      $writer.Write([Text.Encoding]::Unicode.GetBytes($value))
    }

    Write-ShortcutEnvironmentBlock `
      $writer `
      ([Convert]::ToUInt32("A0000001", 16)) `
      $RelativeTarget
    Write-ShortcutEnvironmentBlock `
      $writer `
      ([Convert]::ToUInt32("A0000007", 16)) `
      $IconPath
    $writer.Write([uint32]0)
  } finally {
    $writer.Dispose()
    $stream.Dispose()
  }

  (Get-Item -LiteralPath $ShortcutPath).IsReadOnly = $true
}

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

Copy-Item -LiteralPath (Join-Path $repoRoot "app\main.py") -Destination (Join-Path $stagingApplication "main.py")
Copy-Item -LiteralPath (Join-Path $repoRoot "app\static") -Destination $stagingApplication -Recurse
New-Item -ItemType Directory -Path $stagingVendor | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "vendor\starter-lab") -Destination $stagingVendor -Recurse
Copy-Item -LiteralPath $hiddenLauncherSource -Destination $stagingApplication
Copy-Item -LiteralPath $launcherSource -Destination $stagingApplication
Copy-Item -LiteralPath $stopLauncherSource -Destination $stagingApplication
Copy-Item -LiteralPath $splashSource -Destination $stagingApplication
Copy-Item -LiteralPath $iconSource -Destination (Join-Path $stagingApplication "Azure SRE Agent Demo.ico")
Copy-Item -LiteralPath $readmeSource -Destination $stagingPackage
$relativeLauncher = "app\Launch.vbs"
$fallbackIcon = "%SystemRoot%\System32\cmd.exe"
New-RelativeShortcut `
  (Join-Path $stagingPackage $shortcutName) `
  $relativeLauncher `
  $fallbackIcon
Copy-Item `
  -LiteralPath (Join-Path $stagingPackage $shortcutName) `
  -Destination (Join-Path $stagingApplication $shortcutTemplateName)

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
  $existingShortcut = Join-Path $packageDirectory $shortcutName
  if (Test-Path -LiteralPath $existingShortcut) {
    (Get-Item -LiteralPath $existingShortcut).IsReadOnly = $false
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

if (Test-Path $stagingRoot) {
  Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}

$packagedApplication = Join-Path $packageDirectory "app"
if (-not (Test-Path (Join-Path $packageDirectory $shortcutName))) {
  throw "The portable package could not be copied to its distribution folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "Start Azure SRE Agent Demo.cmd"))) {
  throw "The start launcher could not be copied to the application folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "Launch.vbs"))) {
  throw "The hidden launcher could not be copied to the application folder."
}
if (-not (Test-Path (Join-Path $packagedApplication $shortcutTemplateName))) {
  throw "The relative shortcut template could not be copied to the application folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "Stop Azure SRE Agent Demo.cmd"))) {
  throw "The stop launcher could not be copied to the distribution folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "Show-Splash.ps1"))) {
  throw "The startup splash could not be copied to the distribution folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "Azure SRE Agent Demo.ico"))) {
  throw "The application icon could not be copied to the distribution folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "python\pythonw.exe"))) {
  throw "The embedded Python runtime could not be copied to the application folder."
}
if (-not (Test-Path (Join-Path $packagedApplication "vendor\starter-lab"))) {
  throw "The vendored lab could not be copied to the application folder."
}

$expectedRootItems = @("app", $shortcutName, "README.txt") | Sort-Object
$actualRootItems = @(
  Get-ChildItem -LiteralPath $packageDirectory |
    Select-Object -ExpandProperty Name |
    Sort-Object
)
if (Compare-Object $expectedRootItems $actualRootItems) {
  throw "The portable package root contains unexpected files or folders."
}

$shortcutPath = Join-Path $packageDirectory $shortcutName
$shortcutFile = Get-Item -LiteralPath $shortcutPath
if (-not $shortcutFile.IsReadOnly) {
  throw "The relative shortcut must be read-only to prevent path rewriting."
}
$shortcutBytes = [IO.File]::ReadAllBytes($shortcutPath)
$shortcutUnicode = [Text.Encoding]::Unicode.GetString($shortcutBytes)
$shortcutAnsi = [Text.Encoding]::ASCII.GetString($shortcutBytes)
if (-not $shortcutUnicode.Contains($relativeLauncher)) {
  throw "The portable shortcut does not contain its relative launcher path."
}
if (
  $shortcutUnicode.Contains($repoRoot) -or
  $shortcutAnsi.Contains($repoRoot) -or
  $shortcutUnicode.Contains("package-output") -or
  $shortcutAnsi.Contains("package-output")
) {
  throw "The portable shortcut contains a build-machine path."
}

if (Test-Path -LiteralPath $packageArchive) {
  Remove-Item -LiteralPath $packageArchive -Force
}
Compress-Archive -LiteralPath $packageDirectory -DestinationPath $packageArchive -CompressionLevel Optimal

Write-Host ""
Write-Host "Portable Windows package created:" -ForegroundColor Green
Write-Host $packageDirectory
Write-Host $packageArchive
