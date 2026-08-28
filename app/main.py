from __future__ import annotations

import json
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
from typing import Any, Optional
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
SESSION_TOKEN = uuid.uuid4().hex


@dataclass
class ToolStatus:
    id: str
    name: str
    installed: bool
    version: Optional[str]
    install_command: str
    install_url: str


TOOLS = (
    ("az", "Azure CLI", ("version",), "winget install --id Microsoft.AzureCLI",
     "https://learn.microsoft.com/cli/azure/install-azure-cli-windows"),
    ("azd", "Azure Developer CLI", ("version",), "winget install --id Microsoft.Azd",
     "https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd"),
    ("git", "Git", ("--version",), "winget install --id Git.Git",
     "https://git-scm.com/download/win"),
)


class Job:
    def __init__(self, command: Optional[list[str]] = None) -> None:
        self.id = str(uuid.uuid4())
        self.command = command or []
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()

    def emit(self, event_type: str, **payload: Any) -> None:
        self.events.put({"type": event_type, **payload})


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def command_version(executable: str, args: tuple[str, ...]) -> Optional[str]:
    resolved = shutil.which(executable)
    if resolved is None:
        return None
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
        return None

    text = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return match.group(0) if match else "installed"


def prerequisite_statuses() -> list[ToolStatus]:
    return [
        ToolStatus(
            id=tool_id,
            name=name,
            installed=(version := command_version(tool_id, args)) is not None,
            version=version,
            install_command=install_command,
            install_url=install_url,
        )
        for tool_id, name, args, install_command, install_url in TOOLS
    ]


def run_process(job: Job, command: list[str], cwd: Optional[Path] = None) -> tuple[bool, str]:
    job.emit("command", command=command)
    resolved = shutil.which(command[0])
    if resolved is None:
        job.emit("error", message=f"Command not found: {command[0]}")
        return False, ""
    try:
        process = subprocess.Popen(
            [resolved, *command[1:]],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as error:
        job.emit("error", message=str(error))
        return False, ""

    captured: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        captured.append(line)
        job.emit("output", line=line)
        device = parse_device_code(line)
        if device:
            job.emit("device_code", **device)

    exit_code = process.wait()
    return exit_code == 0, "\n".join(captured)


def stream_process(job: Job) -> None:
    job.emit("started", command=job.command)
    success, _ = run_process(job, job.command)
    exit_code = 0 if success else 1
    job.emit("done", success=exit_code == 0, exit_code=exit_code)


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


def create_job(
    command: Optional[list[str]] = None,
    worker: Optional[Any] = None,
) -> Job:
    job = Job(command)
    with JOBS_LOCK:
        JOBS[job.id] = job
    target = worker or stream_process
    threading.Thread(target=target, args=(job,), daemon=True).start()
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


def run_capture(command: list[str], cwd: Optional[Path] = None) -> tuple[bool, str]:
    resolved = shutil.which(command[0])
    if resolved is None:
        return False, f"Command not found: {command[0]}"
    try:
        result = subprocess.run(
            [resolved, *command[1:]],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


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
        print(f"[web] {format_string % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        if path == "/api/health":
            self.send_json({"status": "ok"})
            return
        if path == "/api/session":
            self.send_json({"token": SESSION_TOKEN})
            return
        if path == "/api/prerequisites":
            self.send_json([asdict(status) for status in prerequisite_statuses()])
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
            self.send_json({"error": "Invalid local session token"}, HTTPStatus.FORBIDDEN)
            return
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
        if origin and origin not in allowed_origins:
            self.send_json({"error": "Untrusted request origin"}, HTTPStatus.FORBIDDEN)
            return
        path = urlparse(self.path).path
        commands = {
            "/api/auth/azure-cli": [
                "az",
                "login",
                "--scope",
                "https://management.core.windows.net//.default",
            ],
            "/api/auth/azd": ["azd", "auth", "login", "--use-device-code"],
        }
        if path in commands:
            job = create_job(commands[path])
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
            if not webbrowser.open_new_tab(verification_url):
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
            self.send_json({"error": "Unknown job"}, HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        while True:
            event = job.events.get()
            message = f"data: {json.dumps(event)}\n\n".encode("utf-8")
            try:
                self.wfile.write(message)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if event["type"] == "done":
                return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"SRE Agent onboarding wizard: {url}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
