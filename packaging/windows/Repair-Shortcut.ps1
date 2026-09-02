$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ShortcutIconRefresh
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern void SHChangeNotify(
        uint eventId,
        uint flags,
        string item1,
        IntPtr item2);

    public static void UpdateItem(string path)
    {
        SHChangeNotify(0x00002000, 0x0005, path, IntPtr.Zero);
    }
}
'@

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

[ShortcutIconRefresh]::UpdateItem($shortcutPath)
