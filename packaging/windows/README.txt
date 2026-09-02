Azure SRE Agent Demo - Portable Windows Package
================================================

Double-click "Start Azure SRE Agent Demo.cmd" to launch the application.
The browser opens automatically at http://127.0.0.1:8765.
The startup terminal closes after handing the application to a background
process. A startup splash remains visible until the local backend is ready and
Microsoft Edge opens in a standalone application window using the normal Edge
profile and its authenticated sessions. If Edge is unavailable, the default
browser opens instead. Use the Shutdown button to stop the local process.

Keep the complete folder together. The included python directory contains the
official Python Software Foundation 3.14.7 embeddable runtime, so Python does
not need to be installed on the workstation.

The wizard requires Azure CLI 2.88.0 or newer and Azure Developer CLI 1.28.0
or newer. The wizard installs Azure CLI 2.90.0 privately in your local app-data
folder from Microsoft's checksum-verified ZIP package, without a UAC prompt or
machine PATH change. Microsoft currently marks the ZIP distribution as preview.
WinGet 1.29.280 or newer installs or updates Azure Developer CLI. Git is not
required.

Start by choosing a lab. The initial catalog includes the Grubify Starter Lab
and its Memory Leak demo scenario.

The Configure step scans the selected subscription for compatible existing labs.
The last successful result is cached locally for offline fallback.

If Windows marks files from the downloaded ZIP as internet-origin, right-click
the ZIP before extracting it, select Properties, select Unblock, and then
extract it again.
