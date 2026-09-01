# Azure SRE Agent Demo Setup

A Windows-first local web application that reduces the setup friction for
Microsoft's Azure SRE Agent starter lab. Version 1 focuses only on Scenario 1:
deploy Grubify, trigger its cart memory leak, and observe the SRE Agent investigate.

The app intentionally excludes GitHub integration and does not require Git,
Git Bash, Rust, MSYS2, MinGW, Node.js, or npm.

## Requirements

- Windows 11
- An Azure subscription where you can create resources and role assignments

The portable Windows package includes Python. Python 3.14.7 or newer is required
only when running directly from the source repository.

The app requires Azure CLI 2.88.0 or newer and Azure Developer CLI 1.28.0 or
newer. WinGet 1.29.280 or newer is the recommended installer. The prerequisite
screen verifies these versions and can install, repair, or update tools
sequentially with one action. Individual remediation, copy-command, and
official-documentation fallbacks remain available.

These minimums were reviewed on September 1, 2026 and should be refreshed by
December 1, 2026 to preserve the three-month tool-age policy.

## First run

From PowerShell in the repository root:

```powershell
.\scripts\start.cmd
```

The launcher uses a process-scoped PowerShell execution-policy bypass so it also
works in Windows Sandbox without changing the machine policy. The start script
checks for Python 3.14.7 or newer, installs or updates Python 3.14.7 with WinGet
when necessary, and launches the local application. It does not create a virtual
environment or download Python packages because the application uses only the
Python standard library. If both Python and WinGet are unavailable, it provides
the direct Python installation URL.

The browser opens automatically at <http://127.0.0.1:8765>.

## Windows Sandbox diagnostics

Each application launch writes a timestamped diagnostic log containing startup,
prerequisite, HTTP, job, subprocess, and authentication details. Device codes,
claims challenges, and tokens are redacted.

`AzureSREAgentDemo.wsb` maps the host `sandbox-logs` directory into the Sandbox
as `AzureSREAgentDemoLogs`. Logs therefore remain available in
`sandbox-logs\AzureSREAgentDemo-*.log` after the Sandbox is closed.

On a standard Windows or Hyper-V VM launch, logs are stored under
`%LOCALAPPDATA%\AzureSREAgentDemo\logs`. The banner at the top of the application
shows the exact active path and provides a **Download log** action in every
environment.

## Build the portable Windows package

```powershell
.\scripts\build-windows.ps1
```

The build script downloads the official Python Software Foundation 3.14.7
embeddable runtime, verifies its pinned SHA-256 checksum and Authenticode
signature, and writes both:

- `dist\AzureSREAgentDemo`
- `dist\AzureSREAgentDemo-portable-win-x64.zip`

End users do not need Python installed. After extracting the ZIP, they
double-click `Start Azure SRE Agent Demo.cmd`. There is no custom PyInstaller
executable; the launcher runs the included, signed `python.exe`. The complete
folder must remain together.

## Current wizard flow

1. Verify the minimum Azure CLI, Azure Developer CLI, and WinGet versions.
2. Run Azure CLI and azd device-code sign-in while streaming output in the browser.
3. Choose a discovered Microsoft Entra tenant and Azure subscription. The current
   Azure CLI default is shown first, and cross-tenant selections are reauthenticated
   with a tenant-scoped device-code flow when required.
4. Configure the Azure environment and region.
5. Deploy the vendored starter lab.
6. Run the Scenario 1 cart fault injection.
7. Restore the declared Bicep, application, and SRE Agent baseline when policy
   enforcement, autonomous remediation, or an outage causes configuration drift.
8. Tear down the Azure environment and return to the deployment step for a clean
   redeployment.

All wizard steps are implemented in the Python backend. Deployment uses ACR Tasks
to build Grubify remotely, so Docker Desktop and a local Grubify checkout are not
required.
