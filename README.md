# Azure SRE Agent Demo Setup

A Windows-first local web application that reduces the setup friction for
Microsoft's Azure SRE Agent starter lab. Version 1 focuses only on Scenario 1:
deploy Grubify, trigger its cart memory leak, and observe the SRE Agent investigate.

The app intentionally excludes GitHub integration and does not require Git Bash,
Rust, MSYS2, MinGW, Node.js, or npm.

## Requirements

- Windows 11
- Python 3.9 or newer
- An Azure subscription where you can create resources and role assignments

Azure CLI, Azure Developer CLI, and Git are checked inside the app. If one is
missing, the prerequisite screen provides its exact `winget install` command and
official installation documentation.

## First run

From PowerShell in the repository root:

```powershell
.\scripts\start.cmd
```

The launcher uses a process-scoped PowerShell execution-policy bypass so it also
works in Windows Sandbox without changing the machine policy. The start script
checks for Python 3.9 or newer, installs Python 3.12 with WinGet when necessary,
and launches the local application. It does not create a virtual environment or
download Python packages because the application uses only the Python standard
library. If both Python and WinGet are unavailable, it provides the direct
Python installation URL.

The browser opens automatically at <http://127.0.0.1:8765>.

## Build the Windows executable

```powershell
.\scripts\build-windows.ps1
```

The build script creates an isolated `.build-venv`, installs PyInstaller only
inside it, and writes `dist\AzureSREAgentDemo\AzureSREAgentDemo.exe`. The
application uses PyInstaller's directory-based package because enterprise
Windows Application Control policies commonly block one-file executables from
extracting a Python DLL into `%TEMP%`. End users do not need Python installed;
distribute the complete `dist\AzureSREAgentDemo` directory.

## Current wizard flow

1. Check Azure CLI, Azure Developer CLI, and Git.
2. Run Azure CLI and azd device-code sign-in while streaming output in the browser.
3. Configure the Azure environment and region.
4. Deploy the vendored starter lab.
5. Run the Scenario 1 cart fault injection.
6. Tear down the Azure environment.

All wizard steps are implemented in the Python backend. Deployment uses ACR Tasks
to build Grubify remotely, so Docker Desktop and a local Grubify checkout are not
required.
