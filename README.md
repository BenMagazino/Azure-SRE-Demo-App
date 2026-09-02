# Azure SRE Agent Demo Setup

A Windows-first local web application that reduces setup friction for guided
Azure SRE Agent labs. The workflow is driven by a lab catalog so each lab can
declare its own dependencies and demo scenarios.

The initial catalog includes the **Grubify Starter Lab** and its **Memory Leak**
scenario: deploy Grubify, trigger cart memory pressure, and observe Azure SRE
Agent investigate.

The app intentionally excludes GitHub integration and does not require Git,
Git Bash, Rust, MSYS2, MinGW, Node.js, or npm.

## Requirements

- Windows 11
- An Azure subscription where you can create resources and role assignments

The portable Windows package includes Python. Python 3.14.7 or newer is required
only when running directly from the source repository.

The app requires Azure CLI 2.88.0 or newer and Azure Developer CLI 1.28.0 or
newer. The prerequisite screen verifies these versions and can install, repair,
or update tools sequentially with one action. Azure CLI remediation uses
Microsoft's 64-bit ZIP distribution, verifies its pinned SHA-256 checksum, and
installs version 2.90.0 under
`%LOCALAPPDATA%\AzureSREAgentDemo\tools\azure-cli`. This user-profile
installation does not require administrator approval or change the machine
`PATH`. The ZIP distribution is currently marked preview by Microsoft. WinGet
1.29.280 or newer handles Azure Developer CLI remediation. Copy-command and
official-documentation fallbacks remain available for WinGet-managed tools.

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
The launcher hands the server off to a background Python process, so its
terminal window closes after startup. A native Windows splash displays startup
progress, waits for the local health endpoint, opens the browser when the
backend is ready, and then closes itself. Microsoft Edge opens in application
mode, providing a standalone window without browser tabs or an address bar
while retaining the normal Edge profile and its authenticated sessions. If
Edge is unavailable, the launcher falls back to the default browser. If
startup times out, the splash points to the diagnostic-log directory. Use
**Back** to revisit an earlier wizard step without clearing completed state.
Use **Shutdown** to stop the local backend and close the application window;
browsers that block scripted closure display a safe-to-close confirmation
instead.

The browser sends a local heartbeat every 10 seconds. If the application
window is closed without using **Shutdown**, the backend stops after two
minutes without a heartbeat. Active installation, authentication, deployment,
recovery, scenario, and teardown jobs are allowed to finish before automatic
shutdown. The portable package also includes
`app\Stop Azure SRE Agent Demo.cmd` for an explicit graceful shutdown when the
application window is unavailable.

The package root includes a portable `Azure SRE Agent Demo.lnk` launcher.
Windows shortcut files cannot resolve relative custom-icon paths, so the
shortcut uses a built-in Windows launcher icon rather than rewriting itself to
an extraction-specific path. It targets the embedded start command in the
`app` folder and continues to work when the complete extracted folder is moved.
The shortcut is intentionally read-only so Windows cannot replace that relative
target with a build-machine or redirected-drive path. The custom shield icon is
used by the browser and splash experience. Since ZIP extraction can remove the
read-only attribute, the hidden launcher restores a pristine relative shortcut
and reapplies that protection before starting the backend.

## Windows Sandbox diagnostics

Each application launch writes a timestamped diagnostic log containing startup,
prerequisite, HTTP, job, subprocess, and authentication details. Device codes,
claims challenges, and tokens are redacted.

`AzureSREAgentDemo.wsb` maps the host `sandbox-logs` directory into the Sandbox
as `AzureSREAgentDemoLogs`. Logs therefore remain available in
`sandbox-logs\AzureSREAgentDemo-*.log` after the Sandbox is closed.

On a standard Windows or Hyper-V VM launch, logs are stored under
`%LOCALAPPDATA%\AzureSREAgentDemo\logs`. A **Download diagnostic log** action is
available in the application footer without exposing the workstation path in
the interface.

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
double-click `Azure SRE Agent Demo.lnk`. The package root contains only that
branded shortcut, `README.txt`, and the `app` folder. The application folder
contains the launch scripts, signed embedded Python runtime, web application,
and vendored lab assets. The complete extracted folder must remain together.

## Current wizard flow

1. Choose a lab from the catalog.
2. Verify the selected lab's minimum Azure CLI, Azure Developer CLI, and WinGet versions.
3. Run Azure CLI and azd device-code sign-in while streaming output in the browser.
4. Choose a discovered Microsoft Entra tenant and Azure subscription. The current
   Azure CLI default is shown first, and cross-tenant selections are reauthenticated
   with a tenant-scoped device-code flow when required.
5. Scan the selected subscription for compatible existing labs, or configure a
   new Azure environment and region. Existing labs can be connected to the
   local azd project and reconciled with the current lab definition.
6. Deploy the selected lab.
7. Choose and run a scenario supported by that lab.
8. Restore the declared Bicep, application, and SRE Agent baseline when policy
   enforcement, autonomous remediation, or an outage causes configuration drift.
9. Tear down the Azure environment and return to the deployment step for a clean
   redeployment.

All wizard steps are implemented in the Python backend. Deployment uses ACR Tasks
to build Grubify remotely, so Docker Desktop and a local Grubify checkout are not
required.

Environment discovery uses read-only Azure CLI subscription inventory as the
source of truth and `azd env list` to identify environments already known on the
workstation. The last successful scan is cached at
`%LOCALAPPDATA%\AzureSREAgentDemo\environments.json` for offline fallback.
