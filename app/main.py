from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(getattr(sys, "_MEIPASS"))
else:
    ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "AzureSREAgentDemo"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
if FROZEN:
    BUNDLED_VENDOR_DIR = ROOT / "vendor" / "starter-lab"
    VENDOR_DIR = STATE_DIR / "starter-lab"
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BUNDLED_VENDOR_DIR, VENDOR_DIR, dirs_exist_ok=True)
else:
    VENDOR_DIR = ROOT.parent / "vendor" / "starter-lab"
HOST = "127.0.0.1"
PORT = 8765
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
AUTH_RETRY_GRACE_SECONDS = 4.0
SESSION_TOKEN = uuid.uuid4().hex
LOGGER = logging.getLogger("AzureSREAgentDemo")
LOG_FILE: Optional[Path] = None


def redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(enter\s+(?:the\s+)?code\s+)([A-Z0-9-]{6,12})",
        r"\1<redacted-device-code>",
        value,
    )
    redacted = re.sub(
        r'(?i)(--claims-challenge\s+)(?:"[^"]*"|\S+)',
        r"\1<redacted-claims-challenge>",
        redacted,
    )
    redacted = re.sub(
        r'(?i)("(?:accessToken|refreshToken|clientSecret|password)"\s*:\s*)"[^"]*"',
        r'\1"<redacted>"',
        redacted,
    )
    return re.sub(
        r"(?i)(authorization:\s*bearer\s+)\S+",
        r"\1<redacted>",
        redacted,
    )


def redact_command(command: list[str]) -> list[str]:
    sensitive_options = {
        "--access-token",
        "--claims-challenge",
        "--client-secret",
        "--password",
    }
    redacted = list(command)
    for index, argument in enumerate(command[:-1]):
        if argument.lower() in sensitive_options:
            redacted[index + 1] = "<redacted>"
    return redacted


def safe_log_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sensitive_keys = {
        "access_token",
        "authorization",
        "claims_challenge",
        "client_secret",
        "code",
        "password",
        "refresh_token",
        "token",
    }

    def sanitize(key: str, value: Any) -> Any:
        if key.lower() in sensitive_keys:
            return "<redacted>"
        if key.lower() == "command" and isinstance(value, list):
            return redact_command(value)
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, list):
            return [redact_text(item) if isinstance(item, str) else item for item in value]
        if isinstance(value, dict):
            return {
                nested_key: sanitize(str(nested_key), nested_value)
                for nested_key, nested_value in value.items()
            }
        return value

    return {key: sanitize(key, value) for key, value in payload.items()}


def diagnostic_log_directory() -> Path:
    configured = os.environ.get("AZURE_SRE_AGENT_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    sandbox_directory = Path.home() / "Desktop" / "AzureSREAgentDemoLogs"
    if (
        os.name == "nt"
        and os.environ.get("USERNAME", "").lower() == "wdagutilityaccount"
        and sandbox_directory.is_dir()
    ):
        return sandbox_directory
    return STATE_DIR / "logs"


def configure_logging() -> Path:
    global LOG_FILE
    if LOG_FILE is not None:
        return LOG_FILE

    log_directory = diagnostic_log_directory()
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_directory = STATE_DIR / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    LOG_FILE = log_directory / f"AzureSREAgentDemo-{timestamp}-{os.getpid()}.log"
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    except OSError:
        log_directory = STATE_DIR / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        LOG_FILE = log_directory / f"AzureSREAgentDemo-{timestamp}-{os.getpid()}.log"
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)
    return LOG_FILE


@dataclass
class ToolStatus:
    id: str
    name: str
    installed: bool
    version: Optional[str]
    install_command: str
    install_url: str
    required: bool


TOOLS = (
    (
        "winget",
        "WinGet (recommended installer)",
        ("--version",),
        "Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery; "
        "Repair-WinGetPackageManager -Force -Latest",
        "https://learn.microsoft.com/windows/package-manager/winget/",
        False,
    ),
    ("az", "Azure CLI", ("version",),
     "winget install --id Microsoft.AzureCLI --exact --source winget "
     "--accept-source-agreements --accept-package-agreements",
     "https://learn.microsoft.com/cli/azure/install-azure-cli-windows", True),
    ("azd", "Azure Developer CLI", ("version",),
     "winget install --id Microsoft.Azd --exact --source winget "
     "--accept-source-agreements --accept-package-agreements",
     "https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd", True),
    ("git", "Git", ("--version",),
     "winget install --id Git.Git --exact --source winget "
     "--scope user --silent --disable-interactivity "
     "--accept-source-agreements --accept-package-agreements",
     "https://git-scm.com/download/win", True),
)


class Job:
    def __init__(self, command: Optional[list[str]] = None) -> None:
        self.id = str(uuid.uuid4())
        self.command = command or []
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.process: Optional[subprocess.Popen[str]] = None
        self.process_lock = threading.Lock()
        self.finish_lock = threading.Lock()
        self.finished = False

    def emit(self, event_type: str, **payload: Any) -> None:
        LOGGER.debug(
            "job=%s event=%s payload=%s",
            self.id,
            event_type,
            safe_log_payload(payload),
        )
        self.events.put({"type": event_type, **payload})

    def set_process(self, process: subprocess.Popen[str]) -> None:
        with self.process_lock:
            self.process = process
        LOGGER.debug("job=%s tracking process pid=%s", self.id, process.pid)

    def clear_process(self, process: subprocess.Popen[str]) -> None:
        with self.process_lock:
            if self.process is process:
                self.process = None
        LOGGER.debug("job=%s released process pid=%s", self.id, process.pid)

    def terminate_process(self) -> None:
        with self.process_lock:
            process = self.process
        if process and process.poll() is None:
            try:
                LOGGER.info("job=%s terminating process pid=%s", self.id, process.pid)
                process.terminate()
            except OSError:
                LOGGER.exception(
                    "job=%s could not terminate process pid=%s",
                    self.id,
                    process.pid,
                )
                return

    def finish(self, success: bool, exit_code: Optional[int]) -> None:
        with self.finish_lock:
            if self.finished:
                return
            self.finished = True
        LOGGER.info(
            "job=%s finished success=%s exit_code=%s",
            self.id,
            success,
            exit_code,
        )
        self.emit("done", success=success, exit_code=exit_code)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
INSTALL_LOCK = threading.Lock()

INSTALL_COMMANDS = {
    "winget": [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        (
            "Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 "
            "-Force | Out-Null; "
            "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; "
            "Install-Module -Name Microsoft.WinGet.Client -Scope CurrentUser "
            "-Force -AllowClobber -Repository PSGallery; "
            "Import-Module Microsoft.WinGet.Client; "
            "Repair-WinGetPackageManager -Force -Latest"
        ),
    ],
    "az": [
        "winget",
        "install",
        "--id",
        "Microsoft.AzureCLI",
        "--exact",
        "--source",
        "winget",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ],
    "azd": [
        "winget",
        "install",
        "--id",
        "Microsoft.Azd",
        "--exact",
        "--source",
        "winget",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ],
    "git": [
        "winget",
        "install",
        "--id",
        "Git.Git",
        "--exact",
        "--source",
        "winget",
        "--scope",
        "user",
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ],
}
INSTALL_ORDER = tuple(INSTALL_COMMANDS)


def command_version(executable: str, args: tuple[str, ...]) -> Optional[str]:
    resolved = shutil.which(executable)
    if resolved is None:
        LOGGER.debug("Prerequisite executable not found: %s", executable)
        return None
    LOGGER.debug("Checking prerequisite: %s resolved=%s", executable, resolved)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [resolved, *args],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        LOGGER.exception("Prerequisite check failed: %s", executable)
        return None

    text = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    version = match.group(0) if match else "installed"
    LOGGER.debug(
        "Prerequisite check complete: %s exit_code=%s version=%s duration=%.3fs",
        executable,
        result.returncode,
        version,
        time.monotonic() - started,
    )
    return version


def prerequisite_statuses() -> list[ToolStatus]:
    refresh_process_path()
    statuses = [
        ToolStatus(
            id=tool_id,
            name=name,
            installed=(version := command_version(tool_id, args)) is not None,
            version=version,
            install_command=install_command,
            install_url=install_url,
            required=required,
        )
        for tool_id, name, args, install_command, install_url, required in TOOLS
    ]
    LOGGER.info(
        "Prerequisite status: %s",
        ", ".join(
            f"{tool.id}={'ready ' + str(tool.version) if tool.installed else 'missing'}"
            for tool in statuses
        ),
    )
    return statuses


def refresh_process_path() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    registry_paths = []
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    )
    for hive, key_path in keys:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                registry_paths.extend(os.path.expandvars(value).split(os.pathsep))
        except OSError:
            continue

    if not registry_paths:
        LOGGER.debug("PATH refresh skipped because no registry PATH values were available")
        return

    current_paths = os.environ.get("PATH", "").split(os.pathsep)
    paths = []
    seen = set()
    for path in [*registry_paths, *current_paths]:
        normalized = os.path.normcase(path.strip().strip('"'))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        paths.append(path)
    os.environ["PATH"] = os.pathsep.join(paths)
    LOGGER.debug(
        "Refreshed process PATH: registry_entries=%s total_entries=%s",
        len(registry_paths),
        len(paths),
    )


def run_tool_install(job: Job, tool_id: str) -> bool:
    command = INSTALL_COMMANDS[tool_id]
    job.emit("tool_status", tool_id=tool_id, status="installing")
    job.emit("output", line=f"Installing {tool_id}...")
    success, _ = run_process(job, command)
    if not success:
        job.emit("tool_status", tool_id=tool_id, status="failed")
        return False

    tool = next(item for item in prerequisite_statuses() if item.id == tool_id)
    if tool.installed:
        job.emit(
            "tool_status",
            tool_id=tool_id,
            status="ready",
            version=tool.version,
        )
        job.emit("output", line=f"{tool.name} {tool.version or ''} is ready.")
        return True

    job.emit("tool_status", tool_id=tool_id, status="failed")
    job.emit(
        "error",
        message=(
            "The installer completed, but the tool was not detected. "
            "Restart the app and re-check prerequisites."
        ),
    )
    return False


def install_tool_worker(job: Job, tool_id: str) -> None:
    with INSTALL_LOCK:
        job.emit("started", command=job.command)
        success = run_tool_install(job, tool_id)
        job.emit("done", success=success, exit_code=0 if success else 1)


def install_all_worker(job: Job) -> None:
    with INSTALL_LOCK:
        job.emit("started", command=[])
        statuses = {tool.id: tool for tool in prerequisite_statuses()}
        missing_required = [
            tool_id
            for tool_id in INSTALL_ORDER
            if statuses[tool_id].required and not statuses[tool_id].installed
        ]
        if not missing_required:
            job.emit("output", line="All required dependencies are already installed.")
            job.emit("done", success=True, exit_code=0)
            return

        install_ids = missing_required
        if not statuses["winget"].installed:
            install_ids = ["winget", *install_ids]

        failures = []
        for tool_id in install_ids:
            if not run_tool_install(job, tool_id):
                failures.append(tool_id)
                if tool_id == "winget":
                    job.emit(
                        "error",
                        message="WinGet is required to install the remaining dependencies.",
                    )
                    break

        ready = all(
            tool.installed
            for tool in prerequisite_statuses()
            if tool.required
        )
        if ready:
            job.emit("output", line="All required dependencies are ready.")
        elif failures:
            job.emit(
                "error",
                message=f"Installation failed for: {', '.join(failures)}.",
            )
        job.emit("done", success=ready, exit_code=0 if ready else 1)


def run_process(
    job: Job,
    command: list[str],
    cwd: Optional[Path] = None,
    line_interceptor: Optional[Callable[[str], bool]] = None,
    emit_command: bool = True,
) -> tuple[bool, str]:
    if emit_command:
        job.emit("command", command=command)
    process_command = resolved_process_command(command)
    if process_command is None:
        LOGGER.error("job=%s command not found: %s", job.id, command[0])
        job.emit("error", message=f"Command not found: {command[0]}")
        return False, ""
    environment = None
    if command[:2] == ["az", "login"]:
        # Disable WAM so explicit device-code login can use any organizational account.
        environment = os.environ.copy()
        environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"
    started = time.monotonic()
    LOGGER.info(
        "job=%s starting command=%s resolved=%s cwd=%s",
        job.id,
        redact_command(command),
        redact_command(process_command),
        str(cwd) if cwd else os.getcwd(),
    )
    try:
        process = subprocess.Popen(
            process_command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
            env=environment,
        )
    except OSError as error:
        LOGGER.exception("job=%s process launch failed", job.id)
        job.emit("error", message=str(error))
        return False, ""

    job.set_process(process)
    LOGGER.info("job=%s process started pid=%s", job.id, process.pid)
    captured: list[str] = []
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            captured.append(line)
            LOGGER.debug(
                "job=%s pid=%s output=%s",
                job.id,
                process.pid,
                redact_text(line),
            )
            if line_interceptor and line_interceptor(line):
                LOGGER.info(
                    "job=%s pid=%s output interceptor requested termination",
                    job.id,
                    process.pid,
                )
                job.terminate_process()
                break
            job.emit("output", line=line)
            device = parse_device_code(line)
            if device:
                browser_opened = open_browser_url(device["verification_url"])
                job.emit(
                    "device_code",
                    **device,
                    browser_opened=browser_opened,
                )
        exit_code = process.wait()
    finally:
        job.clear_process(process)
    LOGGER.info(
        "job=%s process exited pid=%s exit_code=%s duration=%.3fs lines=%s",
        job.id,
        process.pid,
        exit_code,
        time.monotonic() - started,
        len(captured),
    )
    return exit_code == 0, "\n".join(captured)


def resolved_process_command(command: list[str]) -> Optional[list[str]]:
    resolved = shutil.which(command[0])
    if resolved is None:
        return None
    if command[0].lower() == "az" and Path(resolved).suffix.lower() == ".cmd":
        azure_python = Path(resolved).parent.parent / "python.exe"
        if azure_python.is_file():
            return [
                str(azure_python),
                "-I",
                "-B",
                "-u",
                "-m",
                "azure.cli",
                *command[1:],
            ]
    return [resolved, *command[1:]]


def stream_process(job: Job) -> None:
    job.emit("started", command=job.command)
    success, _ = run_process(job, job.command)
    exit_code = 0 if success else 1
    job.emit("done", success=exit_code == 0, exit_code=exit_code)


def parse_claims_challenge_login(line: str) -> Optional[dict[str, str]]:
    match = re.search(
        r'az login --tenant "([^"]+)" --scope "([^"]+)" '
        r'--claims-challenge "([^"]+)"',
        line,
    )
    if not match:
        return None
    return {
        "tenant": match.group(1),
        "scope": match.group(2),
        "claims_challenge": match.group(3),
    }


def cached_azure_context() -> Optional[dict[str, str]]:
    success, output = run_capture([
        "az",
        "account",
        "show",
        "--query",
        "{tenant:tenantId,subscription:id}",
        "--output",
        "json",
        "--only-show-errors",
    ])
    if not success:
        return None
    try:
        context = json.loads(output)
    except json.JSONDecodeError:
        return None
    tenant = str(context.get("tenant", ""))
    subscription = str(context.get("subscription", ""))
    guid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    if not re.fullmatch(guid, tenant) or not re.fullmatch(guid, subscription):
        return None
    return {"tenant": tenant, "subscription": subscription}


def scoped_azure_login_command(context: dict[str, str]) -> list[str]:
    return [
        "az",
        "login",
        "--tenant",
        context["tenant"],
        "--scope",
        "https://management.core.windows.net//.default",
        "--subscription",
        context["subscription"],
        "--skip-subscription-discovery",
        "--use-device-code",
    ]


def claims_challenge_login_command(
    challenge: dict[str, str],
    context: Optional[dict[str, str]] = None,
) -> list[str]:
    command = [
        "az",
        "login",
        "--tenant",
        challenge["tenant"],
        "--scope",
        challenge["scope"],
        "--claims-challenge",
        challenge["claims_challenge"],
        "--use-device-code",
    ]
    if context and context["tenant"].lower() == challenge["tenant"].lower():
        command.extend([
            "--subscription",
            context["subscription"],
            "--skip-subscription-discovery",
        ])
    return command


def azure_cli_management_authenticated() -> bool:
    success, _ = run_capture(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--output",
            "none",
            "--only-show-errors",
        ],
        timeout=10,
    )
    LOGGER.debug("Azure management token probe authenticated=%s", success)
    return success


def wait_for_management_authentication(
    authenticated: threading.Event,
) -> bool:
    return authenticated.wait(AUTH_RETRY_GRACE_SECONDS)


def azure_login_worker(job: Job) -> None:
    LOGGER.info("job=%s Azure CLI authentication worker started", job.id)
    challenge: dict[str, str] = {}
    challenge_context: dict[str, str] = {}
    discovered_context: dict[str, str] = {}
    authenticated = threading.Event()
    stop_monitor = threading.Event()

    stale_context = cached_azure_context()
    LOGGER.debug(
        "job=%s cached Azure context present=%s",
        job.id,
        stale_context is not None,
    )
    if stale_context:
        job.emit(
            "output",
            line="Clearing the previous Azure CLI account selection...",
        )
        logged_out, logout_output = run_capture(["az", "logout"])
        if not logged_out:
            job.emit(
                "error",
                message=logout_output or "Unable to clear the previous Azure CLI session.",
            )
            job.finish(success=False, exit_code=1)
            return

    def monitor_authentication() -> None:
        poll_count = 0
        while not stop_monitor.wait(1.5):
            poll_count += 1
            is_authenticated = azure_cli_management_authenticated()
            LOGGER.debug(
                "job=%s authentication monitor poll=%s authenticated=%s",
                job.id,
                poll_count,
                is_authenticated,
            )
            if is_authenticated:
                authenticated.set()
                job.emit(
                    "output",
                    line="Azure management authentication verified.",
                )
                job.finish(success=True, exit_code=0)
                job.terminate_process()
                return

    monitor = threading.Thread(target=monitor_authentication, daemon=True)
    monitor.name = f"auth-monitor-{job.id[:8]}"
    monitor.start()

    try:
        def intercept_claims_challenge(line: str) -> bool:
            parsed = parse_claims_challenge_login(line)
            if parsed:
                challenge.update(parsed)
                selected = cached_azure_context()
                if selected and selected["tenant"].lower() == parsed["tenant"].lower():
                    challenge_context.update(selected)
                return True
            if "AADSTS50076" in line:
                selected = cached_azure_context()
                if selected:
                    discovered_context.update(selected)
                    job.emit(
                        "output",
                        line=(
                            "The selected Azure account is ready. "
                            "Validating management access..."
                        ),
                    )
                    return True
            return False

        success, _ = run_process(
            job,
            job.command,
            line_interceptor=intercept_claims_challenge,
        )
        LOGGER.info(
            "job=%s initial Azure login ended success=%s authenticated=%s "
            "claims_challenge=%s discovered_context=%s",
            job.id,
            success,
            authenticated.is_set(),
            bool(challenge),
            bool(discovered_context),
        )
        grace_authenticated = authenticated.is_set()
        if not grace_authenticated and (challenge or discovered_context):
            LOGGER.info(
                "job=%s waiting %.1fs for the initial management token "
                "before starting a Conditional Access retry",
                job.id,
                AUTH_RETRY_GRACE_SECONDS,
            )
            grace_authenticated = wait_for_management_authentication(authenticated)
        if not grace_authenticated and challenge:
            job.emit(
                "auth_phase",
                message=(
                    "Your organization requires one additional device-code sign-in "
                    "to satisfy management-plane MFA. Complete the second code; "
                    "the app will then validate the selected subscription."
                ),
            )
            job.emit(
                "output",
                line=(
                    "Conditional Access requested tenant-specific MFA. "
                    "Retrying once for that tenant..."
                ),
            )
            logged_out, logout_output = run_capture(["az", "logout"])
            if not logged_out:
                job.emit("error", message=logout_output or "Unable to reset the partial Azure login.")
                job.finish(success=False, exit_code=1)
                return
            success, _ = run_process(
                job,
                claims_challenge_login_command(challenge, challenge_context or None),
                emit_command=False,
            )
        elif not grace_authenticated and discovered_context:
            job.emit(
                "auth_phase",
                message=(
                    "Your organization requires one additional device-code sign-in "
                    "to finish authentication for the selected subscription."
                ),
            )
            job.emit(
                "output",
                line=(
                    "Management access still requires MFA. "
                    "Starting one additional sign-in..."
                ),
            )
            success, _ = run_process(
                job,
                scoped_azure_login_command(discovered_context),
                emit_command=False,
            )
        success = grace_authenticated or authenticated.is_set() or (
            success and azure_cli_management_authenticated()
        )
        job.finish(success=success, exit_code=0 if success else 1)
    finally:
        stop_monitor.set()
        monitor.join(timeout=2)
        LOGGER.info("job=%s Azure CLI authentication worker stopped", job.id)


def parse_device_code(line: str) -> Optional[dict[str, str]]:
    url_match = re.search(
        r"https://(?:microsoft\.com/devicelogin|login\.microsoft(?:online)?\.com/device)\S*",
        line,
        re.IGNORECASE,
    )
    code_match = re.search(
        r"(?:enter\s+(?:the\s+)?code|code[:\s]+)\s*([A-Z0-9-]{6,12})",
        line,
        re.IGNORECASE,
    )
    if not (url_match and code_match):
        return None
    return {
        "verification_url": url_match.group(0).rstrip(".,"),
        "code": code_match.group(1).upper(),
    }


def is_device_login_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    return parsed.scheme == "https" and (
        (host == "microsoft.com" and path == "/devicelogin")
        or (host in {"login.microsoft.com", "login.microsoftonline.com"} and path == "/device")
    )


def is_windows_sandbox() -> bool:
    return (
        os.name == "nt"
        and os.environ.get("USERNAME", "").lower() == "wdagutilityaccount"
    )


def find_edge() -> Optional[Path]:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def open_browser_url(url: str) -> bool:
    LOGGER.info("Opening browser URL: %s", url)
    if is_windows_sandbox():
        edge = find_edge()
        LOGGER.debug("Windows Sandbox Edge path: %s", edge)
        if edge:
            try:
                process = subprocess.Popen(
                    [str(edge), url],
                    creationflags=CREATE_NO_WINDOW,
                )
                LOGGER.info("Opened Edge directly pid=%s", process.pid)
                return True
            except OSError:
                LOGGER.exception("Unable to open Edge directly")
    try:
        opened = webbrowser.open_new_tab(url)
        LOGGER.info("Default browser open result=%s", opened)
        return opened
    except webbrowser.Error:
        LOGGER.exception("Default browser could not open URL")
        return False


def create_job(
    command: Optional[list[str]] = None,
    worker: Optional[Any] = None,
) -> Job:
    job = Job(command)
    with JOBS_LOCK:
        JOBS[job.id] = job
    target = worker or stream_process
    LOGGER.info(
        "Created job=%s worker=%s command=%s",
        job.id,
        getattr(target, "__name__", target.__class__.__name__),
        redact_command(job.command),
    )
    threading.Thread(
        target=target,
        args=(job,),
        daemon=True,
        name=f"job-{job.id[:8]}",
    ).start()
    return job


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_capture(
    command: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 60,
) -> tuple[bool, str]:
    process_command = resolved_process_command(command)
    if process_command is None:
        LOGGER.error("Capture command not found: %s", command[0])
        return False, f"Command not found: {command[0]}"
    environment = None
    if command[0].lower() == "az":
        environment = os.environ.copy()
        environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"
    started = time.monotonic()
    LOGGER.debug(
        "Starting capture command=%s resolved=%s cwd=%s timeout=%ss",
        redact_command(command),
        redact_command(process_command),
        str(cwd) if cwd else os.getcwd(),
        timeout,
    )
    try:
        result = subprocess.run(
            process_command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        LOGGER.exception("Capture command failed: %s", redact_command(command))
        return False, str(error)
    output = (result.stdout or result.stderr).strip()
    LOGGER.debug(
        "Capture command complete command=%s exit_code=%s duration=%.3fs output=%s",
        redact_command(command),
        result.returncode,
        time.monotonic() - started,
        redact_text(output),
    )
    return result.returncode == 0, output


def authentication_statuses() -> dict[str, bool]:
    azure_cli = azure_cli_management_authenticated()
    azd_command_succeeded, azd_output = run_capture([
        "azd",
        "auth",
        "login",
        "--check-status",
        "--output",
        "json",
    ])
    azd_authenticated = False
    if azd_command_succeeded:
        try:
            azd_status = json.loads(azd_output)
            if isinstance(azd_status, dict):
                azd_authenticated = azd_status.get("status") == "success"
            else:
                LOGGER.warning(
                    "Azure Developer CLI returned an unexpected status response"
                )
        except json.JSONDecodeError as error:
            LOGGER.warning("Unable to parse Azure Developer CLI status: %s", error)
    statuses = {"azure-cli": azure_cli, "azd": azd_authenticated}
    LOGGER.info("Authentication status: %s", statuses)
    return statuses


def azd_values(environment: str) -> dict[str, str]:
    success, output = run_capture(
        ["azd", "env", "get-values", "-e", environment],
        VENDOR_DIR,
    )
    if not success:
        return {}
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def http_json(
    method: str,
    url: str,
    token: str,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urlopen(request, timeout=90) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except URLError as error:
        return 0, str(error)


def upload_knowledge_base(endpoint: str, token: str) -> tuple[int, str]:
    boundary = f"----sreagent{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])

    field("triggerIndexing", "true")
    for path in sorted((VENDOR_DIR / "knowledge-base").glob("*.md")):
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="files"; '
                f'filename="{path.name}"\r\n'
            ).encode(),
            b"Content-Type: text/plain\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    request = Request(
        f"{endpoint}/api/v1/AgentMemory/upload",
        data=b"".join(chunks),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urlopen(request, timeout=180) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except URLError as error:
        return 0, str(error)


def refresh_app_url(job: Job, app_name: str, resource_group: str) -> str:
    success, output = run_process(
        job,
        [
            "az", "containerapp", "show",
            "--name", app_name,
            "--resource-group", resource_group,
            "--query", "properties.configuration.ingress.fqdn",
            "-o", "tsv",
        ],
    )
    return f"https://{output.strip()}" if success and output.strip() else ""


def post_provision(job: Job, environment: str) -> bool:
    values = azd_values(environment)
    required = (
        "SRE_AGENT_ENDPOINT", "SRE_AGENT_NAME", "AZURE_RESOURCE_GROUP",
        "CONTAINER_APP_NAME", "FRONTEND_APP_NAME",
        "AZURE_CONTAINER_REGISTRY_NAME",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        job.emit("output", line=f"Missing azd outputs: {', '.join(missing)}")
        return False

    endpoint = values["SRE_AGENT_ENDPOINT"].rstrip("/")
    resource_group = values["AZURE_RESOURCE_GROUP"]
    api_name = values["CONTAINER_APP_NAME"]
    frontend_name = values["FRONTEND_APP_NAME"]
    acr_name = values["AZURE_CONTAINER_REGISTRY_NAME"]

    job.emit("step", name="Building Grubify API in Azure Container Registry")
    commands = [
        [
            "az", "acr", "build", "--registry", acr_name,
            "--image", "grubify-api:latest", "--file", "Dockerfile",
            "https://github.com/dm-chelupati/grubify.git#main:GrubifyApi",
            "--no-logs", "--output", "none",
        ],
        [
            "az", "containerapp", "update", "--name", api_name,
            "--resource-group", resource_group,
            "--image", f"{acr_name}.azurecr.io/grubify-api:latest",
            "--output", "none",
        ],
    ]
    for command in commands:
        success, _ = run_process(job, command)
        if not success:
            return False

    api_url = refresh_app_url(job, api_name, resource_group)
    if not api_url:
        job.emit("output", line="Could not resolve the Grubify API URL.")
        return False
    run_process(
        job,
        ["azd", "env", "set", "-e", environment, "CONTAINER_APP_URL", api_url],
        VENDOR_DIR,
    )

    job.emit("step", name="Building and deploying Grubify frontend")
    frontend_commands = [
        [
            "az", "acr", "build", "--registry", acr_name,
            "--image", "grubify-frontend:latest", "--file", "Dockerfile",
            "https://github.com/dm-chelupati/grubify.git#main:grubify-frontend",
            "--no-logs", "--output", "none",
        ],
        [
            "az", "containerapp", "update", "--name", frontend_name,
            "--resource-group", resource_group,
            "--image", f"{acr_name}.azurecr.io/grubify-frontend:latest",
            "--set-env-vars", f"REACT_APP_API_BASE_URL={api_url}/api",
            "--output", "none",
        ],
    ]
    for command in frontend_commands:
        success, _ = run_process(job, command)
        if not success:
            return False

    frontend_url = refresh_app_url(job, frontend_name, resource_group)
    if frontend_url:
        run_process(
            job,
            ["azd", "env", "set", "-e", environment, "FRONTEND_APP_URL", frontend_url],
            VENDOR_DIR,
        )
        success, _ = run_process(
            job,
            [
                "az", "containerapp", "update", "--name", api_name,
                "--resource-group", resource_group,
                "--set-env-vars", f"AllowedOrigins__0={frontend_url}",
                "--output", "none",
            ],
        )
        if not success:
            return False

    job.emit("step", name="Authenticating to the SRE Agent data plane")
    success, token = run_capture(
        [
            "az", "account", "get-access-token",
            "--resource", "https://azuresre.dev",
            "--query", "accessToken", "-o", "tsv",
        ],
    )
    if not success or not token.strip():
        return False
    token = token.strip()

    job.emit("step", name="Uploading the Scenario 1 knowledge base")
    status, response = upload_knowledge_base(endpoint, token)
    if status not in (200, 201):
        job.emit("output", line=f"Knowledge-base upload failed: HTTP {status} {response[:300]}")
        return False

    job.emit("step", name="Creating the incident-handler subagent")
    subagent = {
        "name": "incident-handler",
        "type": "ExtendedAgent",
        "tags": [],
        "owner": "",
        "properties": {
            "instructions": (
                "You are an expert in triaging and diagnosing incidents. "
                "Search the knowledge base for the relevant runbook, execute "
                "diagnostic steps, collect evidence, and provide findings. "
                "Always search memory for similar past incidents first."
            ),
            "handoffDescription": (
                "Investigates incidents using runbooks and provides findings"
            ),
            "handoffs": [],
            "tools": [
                "SearchMemory", "RunAzCliReadCommands", "RunAzCliWriteCommands",
                "GetAzCliHelp", "QueryLogAnalyticsByWorkspaceId",
                "QueryAppInsightsByResourceId", "ExecutePythonCode",
            ],
            "mcpTools": [],
            "allowParallelToolCalls": True,
            "enableSkills": True,
        },
    }
    status, response = http_json(
        "PUT",
        f"{endpoint}/api/v2/extendedAgent/agents/incident-handler",
        token,
        subagent,
    )
    if status not in (200, 201, 202, 204):
        job.emit("output", line=f"Subagent creation failed: HTTP {status} {response[:300]}")
        return False

    job.emit("step", name="Enabling Azure Monitor incident handling")
    success, subscription_id = run_process(
        job, ["az", "account", "show", "--query", "id", "-o", "tsv"]
    )
    if not success:
        return False
    resource_id = (
        f"/subscriptions/{subscription_id.strip()}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/agents/{values['SRE_AGENT_NAME']}"
    )
    monitor_body = json.dumps({
        "properties": {
            "incidentManagementConfiguration": {
                "type": "AzMonitor",
                "connectionName": "azmonitor",
            },
            "experimentalSettings": {
                "EnableWorkspaceTools": True,
                "EnableDevOpsTools": True,
                "EnablePythonTools": True,
            },
        }
    })
    success, _ = run_process(
        job,
        [
            "az", "rest", "--method", "PATCH",
            "--url",
            f"https://management.azure.com{resource_id}?api-version=2025-05-01-preview",
            "--body", monitor_body,
            "--output", "none",
        ],
    )
    if not success:
        return False

    job.emit("output", line="Waiting 30 seconds for Azure Monitor initialization...")
    time.sleep(30)
    response_plan = {
        "id": "grubify-http-errors",
        "name": "Grubify HTTP Errors",
        "priorities": ["Sev0", "Sev1", "Sev2", "Sev3", "Sev4"],
        "titleContains": "",
        "handlingAgent": "incident-handler",
        "agentMode": "autonomous",
        "maxAttempts": 3,
    }
    for attempt in range(1, 6):
        success, token = run_capture(
            [
                "az", "account", "get-access-token",
                "--resource", "https://azuresre.dev",
                "--query", "accessToken", "-o", "tsv",
            ],
        )
        if not success:
            return False
        status, response = http_json(
            "PUT",
            f"{endpoint}/api/v1/incidentPlayground/filters/grubify-http-errors",
            token.strip(),
            response_plan,
        )
        if status in (200, 201, 202, 409):
            return True
        job.emit(
            "output",
            line=f"Response plan attempt {attempt}/5 returned HTTP {status}; retrying...",
        )
        time.sleep(15)
    job.emit("output", line=f"Response-plan creation failed: {response[:300]}")
    return False


def deploy_worker(job: Job) -> None:
    state = load_state()
    environment = state.get("environment")
    if not environment:
        job.emit("error", message="Configure an environment before deploying.")
        job.emit("done", success=False, exit_code=None)
        return
    job.emit("started", command=["azd", "up"])
    job.emit("step", name="Registering the Microsoft.App resource provider")
    success, _ = run_process(
        job,
        [
            "az", "provider", "register",
            "--namespace", "Microsoft.App",
            "--wait", "--output", "none",
        ],
    )
    if not success:
        job.emit("done", success=False, exit_code=1)
        return
    job.emit("step", name="Deploying Azure infrastructure")
    success, _ = run_process(
        job,
        ["azd", "provision", "--preview", "-e", environment, "--no-prompt"],
        VENDOR_DIR,
    )
    if not success:
        job.emit("done", success=False, exit_code=1)
        return
    success, _ = run_process(
        job,
        ["azd", "up", "-e", environment, "--no-prompt"],
        VENDOR_DIR,
    )
    if success:
        success = post_provision(job, environment)
    job.emit("done", success=success, exit_code=0 if success else 1)


def teardown_worker(job: Job) -> None:
    environment = load_state().get("environment")
    if not environment:
        job.emit("error", message="No configured environment.")
        job.emit("done", success=False, exit_code=None)
        return
    job.emit("started", command=["azd", "down"])
    success, _ = run_process(
        job,
        [
            "azd", "down", "-e", environment,
            "--purge", "--force", "--no-prompt",
        ],
        VENDOR_DIR,
    )
    job.emit("done", success=success, exit_code=0 if success else 1)


def break_cart_worker(job: Job) -> None:
    environment = load_state().get("environment")
    values = azd_values(environment) if environment else {}
    app_url = values.get("CONTAINER_APP_URL", "").rstrip("/")
    if not app_url:
        job.emit("error", message="The deployed Grubify API URL is unavailable.")
        job.emit("done", success=False, exit_code=None)
        return

    job.emit("started", command=["POST", f"{app_url}/api/cart/demo-user/items"])
    successes = 0
    errors = 0
    body = json.dumps({"foodItemId": 1, "quantity": 1}).encode("utf-8")
    for index in range(1, 201):
        request = Request(
            f"{app_url}/api/cart/demo-user/items",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                if response.status in (200, 201):
                    successes += 1
                else:
                    errors += 1
        except (HTTPError, URLError, TimeoutError):
            errors += 1
        if index % 10 == 0:
            job.emit(
                "output",
                line=f"{index}/200 requests: {successes} succeeded, {errors} failed",
            )
        time.sleep(0.5)
    # Each successful request retains roughly 10 MiB in the demo API. Requiring
    # 75 successes creates enough pressure against the 1 GiB container limit to
    # make the intended alert plausible while allowing some transient failures.
    triggered = successes >= 75
    if not triggered:
        job.emit(
            "error",
            message=(
                f"Only {successes} requests succeeded; at least 75 are required "
                "to plausibly trigger the memory-pressure scenario."
            ),
        )
    job.emit(
        "done",
        success=triggered,
        exit_code=0 if triggered else 1,
        successes=successes,
        errors=errors,
    )


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info(
            "HTTP client=%s %s",
            self.client_address[0],
            format_string % args,
        )

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_diagnostic_log(self) -> None:
        if LOG_FILE is None or not LOG_FILE.is_file():
            self.send_json(
                {"error": "The diagnostic log is not available."},
                HTTPStatus.NOT_FOUND,
            )
            return
        for handler in LOGGER.handlers:
            handler.flush()
        try:
            data = LOG_FILE.read_bytes()
        except OSError as error:
            LOGGER.exception("Unable to read diagnostic log for download")
            self.send_json(
                {"error": str(error)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{LOG_FILE.name}"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        LOGGER.debug("HTTP GET path=%s", path)
        if path == "/api/health":
            self.send_json({"status": "ok"})
            return
        if path == "/api/session":
            self.send_json({"token": SESSION_TOKEN})
            return
        if path == "/api/diagnostics":
            self.send_json({
                "path": str(LOG_FILE) if LOG_FILE else "",
                "filename": LOG_FILE.name if LOG_FILE else "",
            })
            return
        if path == "/api/diagnostics/download":
            self.send_diagnostic_log()
            return
        if path == "/api/prerequisites":
            self.send_json([asdict(status) for status in prerequisite_statuses()])
            return
        if path == "/api/auth/status":
            self.send_json(authentication_statuses())
            return
        if path == "/api/summary":
            environment = load_state().get("environment")
            if not environment:
                self.send_json({"error": "No configured environment"}, HTTPStatus.CONFLICT)
                return
            values = azd_values(environment)
            self.send_json({
                "environment": environment,
                "resource_group": values.get("AZURE_RESOURCE_GROUP", ""),
                "agent_portal_url": values.get("AGENT_PORTAL_URL", "https://sre.azure.com"),
                "agent_endpoint": values.get("SRE_AGENT_ENDPOINT", ""),
                "api_url": values.get("CONTAINER_APP_URL", ""),
                "frontend_url": values.get("FRONTEND_APP_URL", ""),
            })
            return
        if path.startswith("/api/jobs/") and path.endswith("/events"):
            job_id = path.removeprefix("/api/jobs/").removesuffix("/events").strip("/")
            self.stream_job_events(job_id)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.headers.get("X-SRE-Session") != SESSION_TOKEN:
            LOGGER.warning(
                "Rejected POST with invalid session token from client=%s",
                self.client_address[0],
            )
            self.send_json({"error": "Invalid local session token"}, HTTPStatus.FORBIDDEN)
            return
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
        if origin and origin not in allowed_origins:
            LOGGER.warning(
                "Rejected POST with untrusted origin=%s client=%s",
                origin,
                self.client_address[0],
            )
            self.send_json({"error": "Untrusted request origin"}, HTTPStatus.FORBIDDEN)
            return
        path = urlparse(self.path).path
        LOGGER.info("HTTP action POST path=%s client=%s", path, self.client_address[0])
        if path == "/api/client-log":
            try:
                message = str(self.read_json().get("message", ""))[:2000]
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
                return
            LOGGER.error(
                "Browser client error client=%s message=%s",
                self.client_address[0],
                redact_text(message),
            )
            self.send_json({"logged": True})
            return
        commands = {
            "/api/auth/azure-cli": [
                "az",
                "login",
                "--scope",
                "https://management.core.windows.net//.default",
                "--use-device-code",
            ],
            "/api/auth/azd": ["azd", "auth", "login", "--use-device-code"],
        }
        if path in commands:
            worker = azure_login_worker if path == "/api/auth/azure-cli" else None
            job = create_job(commands[path], worker=worker)
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/install/all":
            job = create_job(worker=install_all_worker)
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path.startswith("/api/install/"):
            tool_id = path.removeprefix("/api/install/").strip("/")
            command = INSTALL_COMMANDS.get(tool_id)
            if not command:
                self.send_json({"error": "Unsupported installer"}, HTTPStatus.NOT_FOUND)
                return
            job = create_job(
                command,
                worker=lambda current_job: install_tool_worker(current_job, tool_id),
            )
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/open-device-login":
            try:
                verification_url = str(self.read_json().get("url", ""))
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
                return
            if not is_device_login_url(verification_url):
                self.send_json({"error": "Invalid Microsoft device-login URL"}, HTTPStatus.BAD_REQUEST)
                return
            if not open_browser_url(verification_url):
                self.send_json({"error": "Unable to open the default browser"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json({"opened": True})
            return
        if path == "/api/configure":
            self.configure_environment()
            return
        workers = {
            "/api/deploy": deploy_worker,
            "/api/break-cart": break_cart_worker,
            "/api/teardown": teardown_worker,
        }
        if path in workers:
            job = create_job(worker=workers[path])
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def configure_environment(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return

        environment = str(payload.get("environment", "")).strip()
        location = str(payload.get("location", "")).strip().lower()
        if not re.fullmatch(r"[a-zA-Z0-9-]{2,30}", environment):
            self.send_json(
                {"error": "Environment must be 2-30 letters, numbers, or hyphens."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if location not in {"eastus2", "swedencentral", "australiaeast"}:
            self.send_json({"error": "Unsupported Azure region."}, HTTPStatus.BAD_REQUEST)
            return

        success, subscription_id = run_capture(
            ["az", "account", "show", "--query", "id", "-o", "tsv"]
        )
        if not success or not subscription_id:
            self.send_json(
                {"error": "Sign in with Azure CLI before configuring the environment."},
                HTTPStatus.CONFLICT,
            )
            return

        new_command = [
            "azd", "env", "new", environment,
            "--location", location,
            "--subscription", subscription_id.strip(),
            "--no-prompt",
        ]
        created, output = run_capture(new_command, VENDOR_DIR)
        if not created:
            selected, select_output = run_capture(
                ["azd", "env", "select", environment], VENDOR_DIR
            )
            if not selected:
                self.send_json(
                    {"error": output or select_output or "Unable to configure azd environment."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        settings = (
            ("AZURE_LOCATION", location),
            ("AZURE_SUBSCRIPTION_ID", subscription_id.strip()),
        )
        for key, value in settings:
            saved, save_output = run_capture(
                ["azd", "env", "set", "-e", environment, key, value],
                VENDOR_DIR,
            )
            if not saved:
                self.send_json(
                    {"error": save_output or f"Unable to set {key}."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        state = {
            "environment": environment,
            "location": location,
            "subscription_id": subscription_id.strip(),
        }
        save_state(state)
        self.send_json(state)

    def stream_job_events(self, job_id: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            LOGGER.warning("SSE requested unknown job=%s", job_id)
            self.send_json({"error": "Unknown job"}, HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        LOGGER.info("SSE connected job=%s client=%s", job_id, self.client_address[0])

        while True:
            event = job.events.get()
            message = f"data: {json.dumps(event)}\n\n".encode("utf-8")
            try:
                self.wfile.write(message)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.warning("SSE disconnected job=%s before completion", job_id)
                return
            if event["type"] == "done":
                LOGGER.info("SSE completed job=%s", job_id)
                return


def main() -> None:
    log_file = configure_logging()

    def log_unhandled_exception(
        exception_type: type[BaseException],
        exception: BaseException,
        exception_traceback: Any,
    ) -> None:
        LOGGER.critical(
            "Unhandled application exception",
            exc_info=(exception_type, exception, exception_traceback),
        )

    def log_thread_exception(args: Any) -> None:
        LOGGER.critical(
            "Unhandled exception in thread=%s",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_unhandled_exception
    threading.excepthook = log_thread_exception
    LOGGER.info(
        "Application starting pid=%s frozen=%s executable=%s python=%s cwd=%s",
        os.getpid(),
        FROZEN,
        sys.executable,
        sys.version.replace("\n", " "),
        os.getcwd(),
    )
    LOGGER.info(
        "Paths root=%s static=%s state=%s vendor=%s diagnostics=%s",
        ROOT,
        STATIC_DIR,
        STATE_DIR,
        VENDOR_DIR,
        log_file,
    )
    LOGGER.debug(
        "Runtime environment username=%s computer=%s sandbox=%s argv=%s",
        os.environ.get("USERNAME", ""),
        os.environ.get("COMPUTERNAME", ""),
        is_windows_sandbox(),
        sys.argv,
    )
    try:
        server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    except OSError:
        LOGGER.exception("Unable to bind web server to %s:%s", HOST, PORT)
        raise
    url = f"http://{HOST}:{PORT}"
    LOGGER.info("SRE Agent onboarding wizard ready: %s", url)
    LOGGER.info("Diagnostic log: %s", log_file)
    browser_timer = threading.Timer(0.6, lambda: open_browser_url(url))
    browser_timer.name = "browser-launch"
    browser_timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping server after keyboard interrupt")
    finally:
        server.server_close()
        LOGGER.info("Application stopped")


if __name__ == "__main__":
    main()
