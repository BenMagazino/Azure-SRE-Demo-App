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
$launcherSource = Join-Path $repoRoot "packaging\windows\Start Azure SRE Agent Demo.cmd"
$stopLauncherSource = Join-Path $repoRoot "packaging\windows\Stop Azure SRE Agent Demo.cmd"
$splashSource = Join-Path $repoRoot "packaging\windows\Show-Splash.ps1"
$iconSource = Join-Path $repoRoot "app\static\favicon.ico"
$readmeSource = Join-Path $repoRoot "packaging\windows\README.txt"
$shortcutName = "Azure SRE Agent Demo.lnk"

$shortcutSource = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace AzureSreAgentDemoPackaging
{
    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLinkClass
    {
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct Win32FindData
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint Reserved0;
        public uint Reserved1;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string FileName;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]
        public string AlternateFileName;
    }

    [ComImport]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW
    {
        void GetPath(
            [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file,
            int maxPath,
            out Win32FindData data,
            uint flags);
        void GetIDList(out IntPtr itemIdList);
        void SetIDList(IntPtr itemIdList);
        void GetDescription(
            [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name,
            int maxName);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory(
            [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory,
            int maxPath);
        void SetWorkingDirectory(
            [MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments(
            [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments,
            int maxPath);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation(
            [Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath,
            int maxPath,
            out int iconIndex);
        void SetIconLocation(
            [MarshalAs(UnmanagedType.LPWStr)] string iconPath,
            int iconIndex);
        void SetRelativePath(
            [MarshalAs(UnmanagedType.LPWStr)] string shortcutPath,
            uint reserved);
        void Resolve(IntPtr windowHandle, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    public static class PortableShortcut
    {
        public static void Create(
            string shortcutPath,
            string targetPath,
            string iconPath,
            int iconIndex)
        {
            IShellLinkW link = (IShellLinkW)new ShellLinkClass();
            try
            {
                link.SetPath(targetPath);
                link.SetDescription("Azure SRE Agent Demo");
                link.SetIconLocation(iconPath, iconIndex);
                link.SetShowCmd(7);
                link.SetRelativePath(shortcutPath, 0);
                ((IPersistFile)link).Save(shortcutPath, true);
            }
            finally
            {
                Marshal.FinalReleaseComObject(link);
            }
        }

        public static string ResolveTarget(string shortcutPath)
        {
            IShellLinkW link = (IShellLinkW)new ShellLinkClass();
            try
            {
                ((IPersistFile)link).Load(shortcutPath, 0);
                link.Resolve(IntPtr.Zero, 1);
                StringBuilder targetPath = new StringBuilder(32768);
                Win32FindData data;
                link.GetPath(targetPath, targetPath.Capacity, out data, 0);
                return targetPath.ToString();
            }
            finally
            {
                Marshal.FinalReleaseComObject(link);
            }
        }
    }
}
'@
Add-Type -TypeDefinition $shortcutSource -Language CSharp

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
Copy-Item -LiteralPath $launcherSource -Destination $stagingApplication
Copy-Item -LiteralPath $stopLauncherSource -Destination $stagingApplication
Copy-Item -LiteralPath $splashSource -Destination $stagingApplication
Copy-Item -LiteralPath $iconSource -Destination (Join-Path $stagingApplication "Azure SRE Agent Demo.ico")
Copy-Item -LiteralPath $readmeSource -Destination $stagingPackage
$fallbackIcon = Join-Path $env:SystemRoot "System32\cmd.exe"
[AzureSreAgentDemoPackaging.PortableShortcut]::Create(
  (Join-Path $stagingPackage $shortcutName),
  (Join-Path $stagingApplication "Start Azure SRE Agent Demo.cmd"),
  $fallbackIcon,
  0
)

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

$shell = New-Object -ComObject WScript.Shell
$shortcut = $null
try {
  $shortcutPath = Join-Path $packageDirectory $shortcutName
  $resolvedTarget = [AzureSreAgentDemoPackaging.PortableShortcut]::ResolveTarget(
    $shortcutPath
  )
  $expectedTarget = Join-Path $packagedApplication "Start Azure SRE Agent Demo.cmd"
  if ($resolvedTarget -ne $expectedTarget) {
    throw "The portable shortcut target could not be resolved after relocation."
  }
  $shortcut = $shell.CreateShortcut($shortcutPath)
  if ($shortcut.IconLocation -ne "$fallbackIcon,0") {
    throw "The portable shortcut fallback icon is not configured correctly."
  }
} finally {
  if ($shortcut) {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
  }
  [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}

if (Test-Path -LiteralPath $packageArchive) {
  Remove-Item -LiteralPath $packageArchive -Force
}
Compress-Archive -LiteralPath $packageDirectory -DestinationPath $packageArchive -CompressionLevel Optimal

Write-Host ""
Write-Host "Portable Windows package created:" -ForegroundColor Green
Write-Host $packageDirectory
Write-Host $packageArchive
