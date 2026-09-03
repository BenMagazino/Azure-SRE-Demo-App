Azure SRE Agent Demo
====================

1. Minimum requirements
-----------------------

- 64-bit Windows 11
- Internet access
- An Azure subscription where you can create resources and role assignments
- Azure CLI 2.88.0 or newer
- Azure Developer CLI (azd) 1.28.0 or newer
- PowerShell 7.6.5 or newer for the Zava Learning lab

Python is included in this package. Git, Docker, Node.js, and azmcp are not
required for the core labs. The prerequisite step checks the tools required by
the selected lab and can install private copies of Azure CLI, azd, and
PowerShell when needed.

2. Install
----------

1. Download the portable ZIP.
2. If Windows blocks the download, right-click the ZIP, select Properties,
   select Unblock, and select OK.
3. Extract the entire ZIP to a local folder. Do not run the app from inside
   the ZIP.
4. Keep "Azure SRE Agent Demo.lnk", "README.txt", "LICENSE",
   "THIRD-PARTY-NOTICES.txt", and the "app" folder together.

No installer or administrator setup is required.

3. Run
------

Double-click "Azure SRE Agent Demo.lnk". If the shortcut cannot be opened,
run "app\Start Azure SRE Agent Demo.cmd".

The application opens in Microsoft Edge or the default browser. Follow the
wizard to check prerequisites, sign in to Azure, choose a subscription, reuse
or deploy a lab, and run the demo. Use the same shortcut for later sessions.

4. Shut down
------------

Use Shutdown in the application and wait for any active operation to finish.
If the application window is unavailable, run
"app\Stop Azure SRE Agent Demo.cmd".

Closing the browser without using Shutdown also stops the local application
after approximately five minutes, once active operations have finished.

5. Test mode
------------

Test mode is off by default. To enable its explicit, confirmed "Skip
validation" action for an existing lab, create:

%LOCALAPPDATA%\AzureSREAgentDemo\config.ini

with:

[application]
test_mode = true

For one run, use:

"app\Start Azure SRE Agent Demo.cmd" --test-mode

The command-line option overrides the INI setting. Use --no-test-mode to
disable a configured test mode. The UI and diagnostic log identify test mode,
and skipped validation is recorded as skipped rather than successful.

6. Remove Azure resources
-------------------------

Before uninstalling, open the deployed demo and use Tear down in Step 6 to
delete resources created and managed by this application.

If Tear down is disabled for a reused environment, the application does not
own that environment. Remove it through its original deployment process or
with help from its Azure owner.

7. Uninstall
------------

1. Shut down the application.
2. Delete the extracted Azure SRE Agent Demo folder.
3. Delete "%LOCALAPPDATA%\AzureSREAgentDemo" to remove private tool
   installations, writable lab copies, cached application state, and logs.

8. Legal and support notices
----------------------------

This is an independent personal project. It is not an official Microsoft
product and is not approved, owned, endorsed, or supported by Microsoft.
The demo creates billable Azure resources; you are responsible for their
security, cost, and cleanup.

Read "LICENSE" for the license covering project-owned material and
"THIRD-PARTY-NOTICES.txt" for the separate terms and attributions that apply
to redistributed components. Community support is best-effort through the
repository's GitHub Issues and has no SLA or Microsoft support coverage.
