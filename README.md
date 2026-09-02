# Azure SRE Agent Demo Setup

A Windows-first local web application for deploying and running an
[Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/) demonstration without asking the
presenter to assemble a development toolchain or run the starter lab's Bash scripts.

The app packages the
[Grubify starter lab](https://github.com/microsoft/sre-agent/tree/main/labs/starter-lab) into a
guided six-step experience. It resolves its own Azure command-line dependencies, guides device-code
authentication, deploys or reuses a lab, starts the demo incident, and restores or removes the
environment.

## What is included

| Capability | Current behavior |
| --- | --- |
| Supported host | Windows 11 |
| User interface | Local web app at `http://127.0.0.1:8765` |
| Runtime | Python standard library; Python 3.14.7 is bundled in the portable package |
| Azure tools | Private, per-user Azure CLI 2.90.0 and Azure Developer CLI 1.32.0 installs |
| Current lab | Grubify Starter Lab: 17 Azure resources, 2 dependencies, 1 demo scenario |
| Typical turnaround | Approximately 10-23 minutes for a new deployment |
| Demo scenario | Memory Leak, with a four-minute expected-response countdown |

Git, Git Bash, Node.js, Rust, Docker Desktop, and administrator access are not required to run the
portable package.

## Requirements

- Windows 11 with PowerShell and internet access.
- An Azure subscription where the signed-in user can create resources and role assignments. The
  **Owner** role at subscription scope is the simplest supported configuration.
- Access to Azure SRE Agent in the selected subscription and deployment region.
- Microsoft Edge is optional, but required to use the work/personal profile selector for Azure links.

The deployment creates billable Azure resources. Tear down the lab when it is no longer needed.

## Run the portable app

1. Obtain `AzureSREAgentDemo-portable-win-x64.zip` from a trusted build of this repository.
2. Extract the entire ZIP to a writable local folder. Do not run it from inside the ZIP.
3. Double-click **Start Azure SRE Agent Demo.cmd**.
4. Use **Shutdown** in the app when finished. If the browser is closed without using Shutdown, the
   backend exits automatically after its client timeout once active work has completed.

The portable package contains Python and the vendored lab. On the Prerequisites step, **Resolve all
dependencies** downloads pinned Azure CLI and azd ZIPs, verifies their SHA-256 hashes, and installs
them under:

```text
%LOCALAPPDATA%\AzureSREAgentDemo\tools
```

These private installs do not change the machine PATH and do not require elevation. If Python cannot
validate GitHub's certificate chain while downloading azd, the app retries through PowerShell so
Windows' trusted certificate store is used; certificate validation is never disabled.

## The six-step workflow

1. **Lab Picker** — Select the Grubify Starter Lab and review its dependency, resource, scenario, and
   timing metadata.
2. **Prerequisites** — Detect Azure CLI and azd, then install or update only the missing dependencies.
   The minimum supported versions are Azure CLI 2.88.0 and azd 1.28.0.
3. **Sign in** — Complete device-code authentication for both CLIs, then select the Azure tenant and
   subscription to use.
4. **Configure** — Scan for an existing compatible lab or choose a name and supported region for a
   new environment. Validation is performed only for the selected existing lab.
5. **Deploy** — Reuse a healthy environment, update an incomplete one, or deploy all infrastructure
   and post-provision configuration for a new lab. Progress and command output stream in the app.
6. **Demo** — Open Grubify, its API, the resource group, or the deployed SRE Agent; run the Memory
   Leak scenario; restore the baseline; or tear down the environment.

Device codes are copied and their verification pages are opened automatically. The app never asks
for Azure passwords.

### Existing environments

The app discovers current environments through the resource-group tags
`sre-agent-demo-lab-id` and `sre-agent-demo-environment`. It can also structurally recognize a
compatible older starter-lab deployment that predates those tags.

- A healthy, app-managed environment can be reused without redeployment and remains eligible for
  teardown.
- An incomplete or drifted managed environment is routed through an update.
- A structurally discovered legacy environment can be validated and reused, but app-assisted
  teardown is disabled because the app cannot prove ownership.

### Demo and recovery

The Memory Leak scenario sends sustained cart requests until Grubify produces the alert-triggering
HTTP or connection failures. The four-minute countdown starts only after that failure is confirmed.

Azure Monitor incident ingestion can span agents in the same subscription. To keep parallel labs
isolated, each response plan filters on that environment's exact HTTP 5xx alert title. Environments
created with an older build should run **Restore baseline** once to repair a broad or stale response
plan.

**Restore baseline** reapplies the declared infrastructure, application images, SRE Agent
configuration, knowledge base, incident-handler subagent, and response plan. Use it after a demo or
when validation reports drift. It does not delete the environment.

Step 6 can open Azure links in the current browser context or in a selected local Microsoft Edge
profile. Profiles are displayed by name and account email, while the resource group and SRE Agent
links display their resource names rather than long URLs.

## What the lab deploys

The vendored Scenario 1 infrastructure includes:

- Azure Container Registry
- Azure Container Apps environment
- Grubify frontend and API container apps
- Log Analytics workspace and Application Insights
- Managed identity and required role assignments
- Azure SRE Agent
- Environment-specific HTTP 5xx alert rule
- Grubify runbook and architecture knowledge
- Incident-handler subagent and isolated response plan

Post-provision operations that the upstream lab performs in shell scripts are implemented directly
in `app\main.py`, including container image builds, CORS configuration, knowledge upload, subagent
creation, and response-plan creation.

## Diagnostics, cleanup, and uninstall

Use **Download diagnostic log** in the app footer when troubleshooting. Logs are also written to:

```text
%LOCALAPPDATA%\AzureSREAgentDemo\logs
```

The logs redact authentication tokens and other sensitive command output covered by the app's
diagnostic filters.

To remove a managed Azure environment, use **Tear down Azure resources** before uninstalling the
app. Deleting the local package does not delete anything in Azure.

To uninstall locally:

1. Shut down the app.
2. Delete the extracted portable-package folder.
3. Delete `%LOCALAPPDATA%\AzureSREAgentDemo` to remove managed CLIs, cached environment metadata,
   logs, and local application state.

## Run from source

Source development requires Git and Python 3.14.7 or newer. The application itself has no third-party
Python packages.

```powershell
git clone https://github.com/BenMagazino/Azure-SRE-Demo-App.git
cd Azure-SRE-Demo-App
.\scripts\start.cmd
```

If the required Python runtime is missing, `scripts\start.ps1` uses WinGet to install Python 3.14.7
for the current user. Azure CLI and azd are still managed through the app's Prerequisites step.

Run the automated tests:

```powershell
python -m unittest discover -s app\tests -p "test_*.py"
node --check app\static\app.js
```

Build the self-contained Windows package:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1
```

The build output is:

```text
dist\AzureSREAgentDemo-portable-win-x64.zip
```

## Project structure

```text
app\
  main.py                  Local HTTP server and Azure workflow
  static\                  Dependency-free HTML, CSS, and JavaScript UI
  tests\test_main.py       Backend and workflow test suite
packaging\windows\         Portable launchers, splash screen, and end-user notes
scripts\
  start.cmd / start.ps1    Source launcher and Python bootstrap
  build-windows.ps1        Portable ZIP builder
vendor\starter-lab\        Vendored Grubify application, Bicep, and SRE configuration
```

The application binds only to loopback. Azure operations run as child processes using the selected
Azure CLI and azd sessions; no hosted control plane or separate application account is involved.
