$ErrorActionPreference = "Stop"

$shortcutPath = [IO.Path]::GetFullPath(
  (Join-Path $PSScriptRoot "..\Azure SRE Agent Demo.lnk")
)
$launcherPath = Join-Path $PSScriptRoot "Start Azure SRE Agent Demo.cmd"
$iconPath = Join-Path $PSScriptRoot "Azure SRE Agent Demo.ico"

foreach ($requiredPath in @($shortcutPath, $launcherPath, $iconPath)) {
  if (-not (Test-Path -LiteralPath $requiredPath)) {
    throw "Required shortcut resource was not found: $requiredPath"
  }
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $null
try {
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = $launcherPath
  $shortcut.IconLocation = "$iconPath,0"
  $shortcut.Description = "Azure SRE Agent Demo"
  $shortcut.WindowStyle = 7
  $shortcut.Save()
} finally {
  if ($shortcut) {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
  }
  [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
