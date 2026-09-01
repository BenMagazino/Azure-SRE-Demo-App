Azure SRE Agent Demo - Portable Windows Package
================================================

Double-click "Start Azure SRE Agent Demo.cmd" to launch the application.
The browser opens automatically at http://127.0.0.1:8765.

Keep the complete folder together. The included python directory contains the
official Python Software Foundation 3.14.7 embeddable runtime, so Python does
not need to be installed on the workstation.

The wizard requires Azure CLI 2.88.0 or newer and Azure Developer CLI 1.28.0
or newer. WinGet 1.29.280 or newer can install or update either tool. Git is
not required.

Start by choosing a lab. The initial catalog includes the Grubify Starter Lab
and its Memory Leak demo scenario.

The Configure step scans the selected subscription for compatible existing labs.
The last successful result is cached locally for offline fallback.

If Windows marks files from the downloaded ZIP as internet-origin, right-click
the ZIP before extracting it, select Properties, select Unblock, and then
extract it again.
