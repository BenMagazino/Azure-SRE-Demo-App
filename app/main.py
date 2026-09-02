from __future__ import annotations

import hashlib
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
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


FROZEN = bool(getattr(sys, "frozen", False))
PORTABLE = os.environ.get("AZURE_SRE_AGENT_PORTABLE", "").strip() == "1"
if FROZEN:
    ROOT = Path(getattr(sys, "_MEIPASS"))
    BUNDLED_VENDOR_DIR = ROOT / "vendor" / "starter-lab"
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLED_VENDOR_DIR = (
        ROOT / "vendor" / "starter-lab"
        if PORTABLE
        else ROOT.parent / "vendor" / "starter-lab"
    )
STATIC_DIR = ROOT / "static"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "AzureSREAgentDemo"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
ENVIRONMENT_CACHE_FILE = STATE_DIR / "environments.json"
MANAGED_TOOLS_DIR = STATE_DIR / "tools"
AZURE_CLI_VERSION = "2.90.0"
AZURE_CLI_URL = (
    "https://azcliprod.blob.core.windows.net/zip/"
    f"azure-cli-{AZURE_CLI_VERSION}-x64.zip"
)
AZURE_CLI_SHA256 = (
    "C4EF59B14F0EDD074427FD9981E57B0780965CCDCF6191C033FDF4B4361F33D7"
)
AZURE_CLI_DIR = MANAGED_TOOLS_DIR / "azure-cli"
AZURE_CLI_DOCS_URL = (
    "https://learn.microsoft.com/cli/azure/install-azure-cli-windows"
    "?view=azure-cli-latest#zip-package"
)
AZD_VERSION = "1.32.0"
AZD_URL = (
    "https://github.com/Azure/azure-dev/releases/download/"
    f"azure-dev-cli_{AZD_VERSION}/azd-windows-amd64.zip"
)
AZD_SHA256 = (
    "EA71A4B1CF7E67B766553108507E66E37510AE7F2ED59C02BC160AC3F8A87B8A"
)
AZD_DIR = MANAGED_TOOLS_DIR / "azd"
AZD_DOCS_URL = (
    "https://learn.microsoft.com/azure/developer/"
    "azure-developer-cli/install-azd"
)
if FROZEN or PORTABLE:
    VENDOR_DIR = STATE_DIR / "starter-lab"
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BUNDLED_VENDOR_DIR, VENDOR_DIR, dirs_exist_ok=True)
else:
    VENDOR_DIR = ROOT.parent / "vendor" / "starter-lab"
HOST = "127.0.0.1"
PORT = 8765
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
AUTH_RETRY_GRACE_SECONDS = 4.0
CLIENT_LEASE_TIMEOUT_SECONDS = 120.0
CLIENT_LEASE_CHECK_SECONDS = 10.0
CLIENT_LAUNCH_FALLBACK_SECONDS = 5.0
SESSION_TOKEN = uuid.uuid4().hex
LOGGER = logging.getLogger("AzureSREAgentDemo")
LOG_FILE: Optional[Path] = None
AZURE_DEVICE_LOGIN_URL = "https://microsoft.com/devicelogin"
AZURE_GUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
SRE_AGENT_REGIONS = frozenset({
    "australiaeast",
    "canadacentral",
    "centralus",
    "eastasia",
    "eastus2",
    "francecentral",
    "italynorth",
    "japaneast",
    "koreacentral",
    "northcentralus",
    "southafricanorth",
    "southeastasia",
    "spaincentral",
    "swedencentral",
    "uksouth",
    "westcentralus",
    "westus2",
    "westus3",
})


def redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(enter\s+(?:the\s+)?code\s+)([A-Z0-9-]{6,12})",
        r"\1<redacted-device-code>",
        value,
    )
    redacted = re.sub(
        r"(?i)(start by copying the next code:\s*)([A-Z0-9-]{6,12})",
        r"\1<redacted-device-code>",
        redacted,
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
    minimum_version: str
    ready: bool
    state: str
    install_command: str
    install_url: str
    required: bool


@dataclass(frozen=True)
class ScenarioDefinition:
    id: str
    name: str
    description: str
    action_label: str
    confirmation: str
    investigation_delay_seconds: int


@dataclass(frozen=True)
class LabDefinition:
    id: str
    name: str
    description: str
    resource_count: int
    estimated_turnaround: str
    dependency_ids: tuple[str, ...]
    scenarios: tuple[ScenarioDefinition, ...]


TOOLS = (
    ("az", "Azure CLI", ("version",), "2.88.0",
     AZURE_CLI_DOCS_URL, True),
    ("azd", "Azure Developer CLI", ("version",), "1.28.0",
     AZD_DOCS_URL, True),
)
LABS = (
    LabDefinition(
        id="grubify-starter-lab",
        name="Grubify Starter Lab",
        description=(
            "Deploy Grubify to Azure Container Apps and observe Azure SRE Agent "
            "investigate an application incident."
        ),
        resource_count=17,
        estimated_turnaround="10-23 min",
        dependency_ids=("az", "azd"),
        scenarios=(
            ScenarioDefinition(
                id="memory-leak",
                name="Memory Leak",
                description=(
                    "Apply sustained cart allocations until the API experiences "
                    "managed memory pressure and HTTP failures."
                ),
                action_label="Run Memory Leak",
                confirmation=(
                    "Send cart requests to trigger the Grubify memory leak?"
                ),
                investigation_delay_seconds=300,
            ),
        ),
    ),
)
LABS_BY_ID = {lab.id: lab for lab in LABS}
LAB_ID_TAG = "azure-sre-agent-lab-id"
LAB_ENVIRONMENT_TAG = "azure-sre-agent-environment"


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
AZURE_CONTEXT_LOCK = threading.Lock()
CLIENT_HEARTBEAT_LOCK = threading.Lock()
LAST_CLIENT_HEARTBEAT: Optional[float] = None

INSTALL_COMMANDS = {
    "az": ["app-managed", "azure-cli", AZURE_CLI_VERSION],
    "azd": ["app-managed", "azure-developer-cli", AZD_VERSION],
}
UPDATE_COMMANDS = {
    "az": INSTALL_COMMANDS["az"],
    "azd": INSTALL_COMMANDS["azd"],
}
REPAIR_COMMANDS = {
    "az": INSTALL_COMMANDS["az"],
    "azd": INSTALL_COMMANDS["azd"],
}
INSTALL_ORDER = tuple(INSTALL_COMMANDS)
MINIMUM_VERSIONS = {
    tool_id: minimum_version
    for tool_id, _name, _args, minimum_version, _install_url, _required in TOOLS
}


def parse_version(value: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def version_meets_minimum(version: str, minimum_version: str) -> bool:
    parsed = parse_version(version)
    minimum = parse_version(minimum_version)
    return parsed is not None and minimum is not None and parsed >= minimum


def azure_resource_group_portal_url(
    tenant_id: str,
    subscription_id: str,
    resource_group: str,
) -> str:
    if not subscription_id or not resource_group:
        return ""
    tenant_segment = f"@{quote(tenant_id, safe='')}" if tenant_id else ""
    return (
        f"https://portal.azure.com/#{tenant_segment}/resource/subscriptions/"
        f"{quote(subscription_id, safe='')}/resourceGroups/"
        f"{quote(resource_group, safe='')}/overview"
    )


def command_version(executable: str, args: tuple[str, ...]) -> Optional[str]:
    command = resolved_process_command([executable, *args])
    if command is None:
        LOGGER.debug("Prerequisite executable not found: %s", executable)
        return None
    LOGGER.debug("Checking prerequisite: %s resolved=%s", executable, command[0])
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
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
    if result.returncode != 0:
        LOGGER.warning(
            "Prerequisite version check failed: %s exit_code=%s output=%s",
            executable,
            result.returncode,
            redact_text(text.strip()),
        )
        return None
    parsed = parse_version(text)
    if parsed is None:
        LOGGER.warning(
            "Prerequisite version check returned no version: %s output=%s",
            executable,
            redact_text(text.strip()),
        )
        return None
    version = ".".join(str(part) for part in parsed)
    LOGGER.debug(
        "Prerequisite check complete: %s exit_code=%s version=%s duration=%.3fs",
        executable,
        result.returncode,
        version,
        time.monotonic() - started,
    )
    return version


def prerequisite_statuses(
    tool_ids: Optional[tuple[str, ...]] = None,
) -> list[ToolStatus]:
    refresh_process_path()
    selected_ids = (
        tool_ids
        if tool_ids is not None
        else tuple(MINIMUM_VERSIONS)
    )
    unknown_ids = set(selected_ids).difference(MINIMUM_VERSIONS)
    if unknown_ids:
        raise ValueError(
            f"Unknown prerequisite tool IDs: {', '.join(sorted(unknown_ids))}"
        )
    statuses = []
    for tool_id, name, args, minimum_version, install_url, required in TOOLS:
        if tool_id not in selected_ids:
            continue
        installed = shutil.which(tool_id) is not None
        version = command_version(tool_id, args) if installed else None
        if not installed:
            state = "missing"
        elif version is None:
            state = "invalid"
        elif version_meets_minimum(version, minimum_version):
            state = "ready"
        else:
            state = "outdated"
        remediation = {
            "missing": INSTALL_COMMANDS,
            "invalid": REPAIR_COMMANDS,
            "outdated": UPDATE_COMMANDS,
            "ready": INSTALL_COMMANDS,
        }[state][tool_id]
        statuses.append(
            ToolStatus(
                id=tool_id,
                name=name,
                installed=installed,
                version=version,
                minimum_version=minimum_version,
                ready=state == "ready",
                state=state,
                install_command=subprocess.list2cmdline(remediation),
                install_url=install_url,
                required=required,
            )
        )
    LOGGER.info(
        "Prerequisite status: %s",
        ", ".join(
            f"{tool.id}={tool.state} version={tool.version or 'unknown'} "
            f"minimum={tool.minimum_version}"
            for tool in statuses
        ),
    )
    return statuses


def selected_lab(state: Optional[dict[str, Any]] = None) -> Optional[LabDefinition]:
    current_state = state if state is not None else load_state()
    return LABS_BY_ID.get(str(current_state.get("lab_id", "")))


def lab_catalog_payload(state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    current_state = state if state is not None else load_state()
    active_lab = selected_lab(current_state)
    return {
        "labs": [asdict(lab) for lab in LABS],
        "selected_lab_id": active_lab.id if active_lab else "",
        "selected_scenario_id": (
            str(current_state.get("scenario_id", "")) if active_lab else ""
        ),
    }


def parse_json_records(output: str) -> Optional[list[dict[str, Any]]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def local_azd_environment_names() -> set[str]:
    success, output = run_capture(
        ["azd", "env", "list", "--output", "json"],
        VENDOR_DIR,
    )
    if not success:
        return set()
    records = parse_json_records(output)
    if records is None:
        return set()
    names = set()
    for record in records:
        name = str(record.get("name") or record.get("Name") or "").strip()
        if name:
            names.add(name.casefold())
    return names


def build_existing_environment_catalog(
    groups: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    container_apps: list[dict[str, Any]],
    local_environment_names: set[str],
    lab_id: str,
) -> list[dict[str, Any]]:
    agent_groups = {
        str(resource.get("resourceGroup") or "").casefold()
        for resource in agents
        if resource.get("resourceGroup")
    }
    apps_by_group: dict[str, set[str]] = {}
    for resource in container_apps:
        resource_group = str(resource.get("resourceGroup") or "").casefold()
        name = str(resource.get("name") or "").casefold()
        if resource_group and name:
            apps_by_group.setdefault(resource_group, set()).add(name)

    environments = []
    for group in groups:
        resource_group = str(group.get("name") or "").strip()
        resource_group_key = resource_group.casefold()
        if not resource_group:
            continue
        tags = {
            str(key).casefold(): str(value).strip()
            for key, value in (group.get("tags") or {}).items()
        }
        tagged_lab_id = tags.get(LAB_ID_TAG.casefold(), "")
        if tagged_lab_id:
            if tagged_lab_id.casefold() != lab_id.casefold():
                continue
            detection = "managed"
        else:
            app_names = apps_by_group.get(resource_group_key, set())
            is_grubify_lab = (
                resource_group_key.startswith("rg-")
                and resource_group_key in agent_groups
                and any(
                    name.startswith("ca-grubify-")
                    and not name.startswith("ca-grubify-fe-")
                    for name in app_names
                )
                and any(name.startswith("ca-grubify-fe-") for name in app_names)
            )
            if not is_grubify_lab:
                continue
            detection = "legacy"

        environment = (
            tags.get(LAB_ENVIRONMENT_TAG.casefold(), "")
            or resource_group.removeprefix("rg-")
        )
        location = str(group.get("location") or "").lower()
        if (
            not re.fullmatch(r"[a-zA-Z0-9-]{2,30}", environment)
            or location not in SRE_AGENT_REGIONS
        ):
            continue
        environments.append({
            "environment": environment,
            "resource_group": resource_group,
            "location": location,
            "detection": detection,
            "local": environment.casefold() in local_environment_names,
        })
    return sorted(
        environments,
        key=lambda item: (not item["local"], item["environment"].casefold()),
    )


def load_environment_cache(
    subscription_id: str,
    lab_id: str,
) -> list[dict[str, Any]]:
    if not ENVIRONMENT_CACHE_FILE.is_file():
        return []
    try:
        payload = json.loads(ENVIRONMENT_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        payload.get("subscription_id") != subscription_id
        or payload.get("lab_id") != lab_id
        or not isinstance(payload.get("environments"), list)
    ):
        return []
    return [
        item
        for item in payload["environments"]
        if isinstance(item, dict)
    ]


def save_environment_cache(
    subscription_id: str,
    lab_id: str,
    environments: list[dict[str, Any]],
) -> None:
    try:
        ENVIRONMENT_CACHE_FILE.write_text(
            json.dumps({
                "subscription_id": subscription_id,
                "lab_id": lab_id,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "environments": environments,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        LOGGER.exception("Unable to save the environment discovery cache")


def discover_existing_environments(
    subscription_id: str,
    lab_id: str,
) -> dict[str, Any]:
    local_names = local_azd_environment_names()
    commands = (
        [
            "az", "group", "list",
            "--subscription", subscription_id,
            "--query", "[].{name:name,location:location,tags:tags}",
            "--output", "json",
        ],
        [
            "az", "resource", "list",
            "--subscription", subscription_id,
            "--resource-type", "Microsoft.App/agents",
            "--query", "[].{name:name,resourceGroup:resourceGroup,location:location}",
            "--output", "json",
        ],
        [
            "az", "resource", "list",
            "--subscription", subscription_id,
            "--resource-type", "Microsoft.App/containerApps",
            "--query", "[].{name:name,resourceGroup:resourceGroup,location:location}",
            "--output", "json",
        ],
    )
    records = []
    for command in commands:
        success, output = run_capture(command, timeout=60)
        parsed = parse_json_records(output) if success else None
        if parsed is None:
            cached = load_environment_cache(subscription_id, lab_id)
            for item in cached:
                environment = str(item.get("environment") or "")
                item["local"] = environment.casefold() in local_names
            return {
                "environments": cached,
                "source": "cache",
                "stale": True,
                "warning": (
                    "Azure discovery is unavailable. Showing the last successful "
                    "subscription scan."
                    if cached
                    else "Azure discovery is unavailable and no offline cache exists."
                ),
            }
        records.append(parsed)

    environments = build_existing_environment_catalog(
        records[0],
        records[1],
        records[2],
        local_names,
        lab_id,
    )
    save_environment_cache(subscription_id, lab_id, environments)
    return {
        "environments": environments,
        "source": "azure",
        "stale": False,
        "warning": "",
    }


def validate_existing_lab(
    subscription_id: str,
    environment: dict[str, Any],
) -> dict[str, Any]:
    resource_group = str(environment.get("resource_group") or "").strip()
    commands = (
        [
            "az", "resource", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query",
            "[].{id:id,name:name,type:type,location:location,"
            "provisioningState:provisioningState}",
            "--output", "json",
        ],
        [
            "az", "containerapp", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query",
            "[].{id:id,name:name,image:properties.template.containers[0].image,"
            "fqdn:properties.configuration.ingress.fqdn,"
            "provisioningState:properties.provisioningState}",
            "--output", "json",
        ],
    )
    parsed_records: list[list[dict[str, Any]]] = []
    for command in commands:
        success, output = run_capture(command, timeout=30)
        records = parse_json_records(output) if success else None
        if records is None:
            return {
                "ready": False,
                "issues": [
                    output or "Azure did not return resource validation data."
                ],
                "values": {},
            }
        parsed_records.append(records)

    resources, container_apps = parsed_records
    resources_by_type: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        resource_type = str(resource.get("type") or "").casefold()
        if resource_type:
            resources_by_type.setdefault(resource_type, []).append(resource)

    required_types = {
        "microsoft.app/agents": "Azure SRE Agent",
        "microsoft.app/managedenvironments": "Container Apps environment",
        "microsoft.containerregistry/registries": "Azure Container Registry",
        "microsoft.operationalinsights/workspaces": "Log Analytics workspace",
        "microsoft.insights/components": "Application Insights",
        "microsoft.insights/metricalerts": "metric alert",
    }
    issues = [
        f"Missing {label}."
        for resource_type, label in required_types.items()
        if not resources_by_type.get(resource_type)
    ]
    for resource in resources:
        provisioning_state = str(
            resource.get("provisioningState") or ""
        ).strip()
        if provisioning_state and provisioning_state.casefold() != "succeeded":
            issues.append(
                f"{resource.get('name') or 'A resource'} is {provisioning_state}."
            )

    api_app = next(
        (
            app for app in container_apps
            if str(app.get("name") or "").casefold().startswith("ca-grubify-")
            and not str(app.get("name") or "").casefold().startswith(
                "ca-grubify-fe-"
            )
        ),
        None,
    )
    frontend_app = next(
        (
            app for app in container_apps
            if str(app.get("name") or "").casefold().startswith(
                "ca-grubify-fe-"
            )
        ),
        None,
    )
    for app, label, image_name in (
        (api_app, "Grubify API", "grubify-api"),
        (frontend_app, "Grubify frontend", "grubify-frontend"),
    ):
        if app is None:
            issues.append(f"Missing {label} Container App.")
            continue
        provisioning_state = str(app.get("provisioningState") or "").strip()
        if provisioning_state.casefold() != "succeeded":
            issues.append(
                f"{label} provisioning state is "
                f"{provisioning_state or 'unknown'}."
            )
        if not str(app.get("fqdn") or "").strip():
            issues.append(f"{label} has no application endpoint.")
        if image_name not in str(app.get("image") or "").casefold():
            issues.append(f"{label} is not running the current lab image.")

    agents = resources_by_type.get("microsoft.app/agents", [])
    agent_details: dict[str, Any] = {}
    if agents:
        success, output = run_capture(
            [
                "az", "resource", "show",
                "--ids", str(agents[0].get("id") or ""),
                "--api-version", "2025-05-01-preview",
                "--query",
                "{name:name,endpoint:properties.agentEndpoint,"
                "incidentType:properties.incidentManagementConfiguration.type,"
                "provisioningState:properties.provisioningState}",
                "--output", "json",
            ],
            timeout=30,
        )
        if success:
            try:
                payload = json.loads(output)
                if isinstance(payload, dict):
                    agent_details = payload
            except json.JSONDecodeError:
                pass
        if not agent_details:
            issues.append("Unable to read the Azure SRE Agent status.")
        else:
            provisioning_state = str(
                agent_details.get("provisioningState") or ""
            ).strip()
            if (
                provisioning_state
                and provisioning_state.casefold() != "succeeded"
            ):
                issues.append(
                    f"Azure SRE Agent provisioning state is {provisioning_state}."
                )
            if not str(agent_details.get("endpoint") or "").strip():
                issues.append("Azure SRE Agent has no service endpoint.")
            if (
                str(agent_details.get("incidentType") or "").casefold()
                != "azmonitor"
            ):
                issues.append(
                    "Azure SRE Agent incident handling is not configured."
                )

    if issues:
        return {"ready": False, "issues": issues, "values": {}}

    success, token = run_capture([
        "az", "account", "get-access-token",
        "--resource", "https://azuresre.dev",
        "--query", "accessToken",
        "--output", "tsv",
    ], timeout=30)
    if not success or not token.strip():
        return {
            "ready": False,
            "issues": ["Unable to authenticate to the Azure SRE Agent service."],
            "values": {},
        }
    agent_endpoint = str(agent_details["endpoint"]).rstrip("/")
    data_plane_checks = (
        (
            f"{agent_endpoint}/api/v2/extendedAgent/agents/incident-handler",
            "The incident-handler subagent is not configured.",
        ),
        (
            f"{agent_endpoint}/api/v1/incidentPlayground/filters/"
            "grubify-http-errors",
            "The Grubify incident response plan is not configured.",
        ),
    )
    data_plane_issues = []
    for url, issue in data_plane_checks:
        status, _ = http_json("GET", url, token.strip())
        if status != HTTPStatus.OK:
            data_plane_issues.append(issue)
    if data_plane_issues:
        return {
            "ready": False,
            "issues": data_plane_issues,
            "values": {},
        }

    registry = resources_by_type["microsoft.containerregistry/registries"][0]
    managed_environment = resources_by_type[
        "microsoft.app/managedenvironments"
    ][0]
    workspace = resources_by_type[
        "microsoft.operationalinsights/workspaces"
    ][0]
    registry_name = str(registry.get("name") or "")
    location = str(environment.get("location") or "").lower()
    return {
        "ready": True,
        "issues": [],
        "values": {
            "AZURE_LOCATION": location,
            "AZURE_SUBSCRIPTION_ID": subscription_id,
            "AZURE_RESOURCE_GROUP": resource_group,
            "AZURE_CONTAINER_REGISTRY_NAME": registry_name,
            "AZURE_CONTAINER_REGISTRY_ENDPOINT": (
                f"{registry_name}.azurecr.io"
            ),
            "AZURE_CONTAINER_APPS_ENVIRONMENT_NAME": str(
                managed_environment.get("name") or ""
            ),
            "AZURE_CONTAINER_APPS_ENVIRONMENT_ID": str(
                managed_environment.get("id") or ""
            ),
            "SRE_AGENT_NAME": str(agent_details.get("name") or ""),
            "SRE_AGENT_ENDPOINT": agent_endpoint,
            "AGENT_PORTAL_URL": "https://sre.azure.com",
            "CONTAINER_APP_NAME": str(api_app.get("name") or ""),
            "CONTAINER_APP_URL": (
                f"https://{str(api_app.get('fqdn') or '')}"
            ),
            "FRONTEND_APP_NAME": str(frontend_app.get("name") or ""),
            "FRONTEND_APP_URL": (
                f"https://{str(frontend_app.get('fqdn') or '')}"
            ),
            "LOG_ANALYTICS_WORKSPACE_ID": str(workspace.get("id") or ""),
        },
    }


def refresh_process_path() -> None:
    if os.name != "nt":
        return

    managed_paths = []
    managed_azure_cli_bin = AZURE_CLI_DIR / "bin"
    if (managed_azure_cli_bin / "az.cmd").is_file():
        managed_paths.append(str(managed_azure_cli_bin))
    if (AZD_DIR / "azd.exe").is_file():
        managed_paths.append(str(AZD_DIR))

    registry_paths = []
    try:
        import winreg
    except ImportError:
        LOGGER.debug("Windows registry is unavailable during PATH refresh")
    else:
        keys = (
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        )
        for hive, key_path in keys:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
                    registry_paths.extend(os.path.expandvars(value).split(os.pathsep))
            except OSError:
                continue

    if not managed_paths and not registry_paths:
        LOGGER.debug("PATH refresh skipped because no updated PATH values were available")
        return

    current_paths = os.environ.get("PATH", "").split(os.pathsep)
    paths = []
    seen = set()
    for path in [*managed_paths, *registry_paths, *current_paths]:
        normalized = os.path.normcase(path.strip().strip('"'))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        paths.append(path)
    os.environ["PATH"] = os.pathsep.join(paths)
    LOGGER.debug(
        "Refreshed process PATH: managed_entries=%s registry_entries=%s total_entries=%s",
        len(managed_paths),
        len(registry_paths),
        len(paths),
    )


def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination_root = destination.resolve()
    for member in archive.infolist():
        member_path = PurePosixPath(member.filename.replace("\\", "/"))
        if (
            member_path.is_absolute()
            or ".." in member_path.parts
            or any(part.endswith(":") for part in member_path.parts)
        ):
            raise ValueError(f"Unsafe ZIP entry: {member.filename}")
        target = (destination / Path(*member_path.parts)).resolve()
        try:
            target.relative_to(destination_root)
        except ValueError as error:
            raise ValueError(f"Unsafe ZIP entry: {member.filename}") from error
    archive.extractall(destination)


def install_managed_zip_tool(
    job: Job,
    *,
    display_name: str,
    slug: str,
    version: str,
    url: str,
    expected_sha256: str,
    install_dir: Path,
    archive_executable: Path,
    installed_executable: Path,
) -> bool:
    MANAGED_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = MANAGED_TOOLS_DIR / f"{slug}-{version}.zip"
    staging_dir = MANAGED_TOOLS_DIR / f"{slug}-staging"
    backup_dir = MANAGED_TOOLS_DIR / f"{slug}-backup"

    try:
        archive_path.unlink(missing_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if backup_dir.exists():
            if not install_dir.exists():
                backup_dir.replace(install_dir)
            else:
                shutil.rmtree(backup_dir)
    except OSError as error:
        LOGGER.exception("Managed %s staging cleanup failed", display_name)
        job.emit(
            "output",
            line=(
                f"{display_name} installation could not prepare its staging "
                f"folder: {error}"
            ),
        )
        return False

    job.emit(
        "output",
        line=(
            f"Downloading {display_name} {version} to the current user "
            "profile (no administrator approval required)..."
        ),
    )
    digest = hashlib.sha256()
    downloaded = 0
    last_reported_megabytes = 0
    request = Request(
        url,
        headers={"User-Agent": "AzureSREAgentDemo/1.0"},
    )
    try:
        with urlopen(request, timeout=180) as response, archive_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                downloaded_megabytes = downloaded // (10 * 1024 * 1024) * 10
                if downloaded_megabytes > last_reported_megabytes:
                    last_reported_megabytes = downloaded_megabytes
                    job.emit(
                        "output",
                        line=f"Downloaded {downloaded_megabytes} MB...",
                    )

        actual_hash = digest.hexdigest().upper()
        if actual_hash != expected_sha256:
            raise ValueError(
                f"{display_name} download checksum mismatch. "
                f"Expected {expected_sha256}, received {actual_hash}."
            )

        job.emit("output", line=f"Checksum verified. Extracting {display_name}...")
        staging_dir.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            safe_extract_zip(archive, staging_dir)
        archive_target = staging_dir / archive_executable
        if not archive_target.is_file():
            raise ValueError(
                f"{display_name} archive does not contain {archive_executable}."
            )
        installed_target = staging_dir / installed_executable
        if archive_target != installed_target:
            installed_target.parent.mkdir(parents=True, exist_ok=True)
            archive_target.replace(installed_target)

        if install_dir.exists():
            install_dir.replace(backup_dir)
        try:
            staging_dir.replace(install_dir)
        except OSError:
            if backup_dir.exists() and not install_dir.exists():
                backup_dir.replace(install_dir)
            raise
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

        refresh_process_path()
        job.emit(
            "output",
            line=f"{display_name} installed privately at {install_dir}.",
        )
        return True
    except (OSError, URLError, ValueError, zipfile.BadZipFile) as error:
        LOGGER.exception("Managed %s installation failed", display_name)
        job.emit("output", line=f"{display_name} installation failed: {error}")
        return False
    finally:
        try:
            archive_path.unlink(missing_ok=True)
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
        except OSError as cleanup_error:
            LOGGER.warning(
                "Managed %s temporary-file cleanup failed: %s",
                display_name,
                cleanup_error,
            )
            job.emit(
                "output",
                line=f"Temporary-file cleanup needs attention: {cleanup_error}",
            )


def install_managed_azure_cli(job: Job) -> bool:
    return install_managed_zip_tool(
        job,
        display_name="Azure CLI",
        slug="azure-cli",
        version=AZURE_CLI_VERSION,
        url=AZURE_CLI_URL,
        expected_sha256=AZURE_CLI_SHA256,
        install_dir=AZURE_CLI_DIR,
        archive_executable=Path("bin") / "az.cmd",
        installed_executable=Path("bin") / "az.cmd",
    )


def install_managed_azd(job: Job) -> bool:
    return install_managed_zip_tool(
        job,
        display_name="Azure Developer CLI",
        slug="azd",
        version=AZD_VERSION,
        url=AZD_URL,
        expected_sha256=AZD_SHA256,
        install_dir=AZD_DIR,
        archive_executable=Path("azd-windows-amd64.exe"),
        installed_executable=Path("azd.exe"),
    )


def run_tool_install(job: Job, tool_id: str) -> bool:
    current = next(item for item in prerequisite_statuses() if item.id == tool_id)
    if current.ready:
        job.emit(
            "tool_status",
            tool_id=tool_id,
            status="ready",
            version=current.version,
        )
        job.emit("output", line=f"{current.name} {current.version} is already ready.")
        return True

    action, command = {
        "missing": ("Installing", INSTALL_COMMANDS[tool_id]),
        "outdated": ("Updating", UPDATE_COMMANDS[tool_id]),
        "invalid": ("Repairing", REPAIR_COMMANDS[tool_id]),
    }[current.state]
    event_status = action.removesuffix("ing").lower() + "ing"
    job.emit("tool_status", tool_id=tool_id, status=event_status)
    job.emit(
        "output",
        line=(
            f"{action} {current.name}; required version is "
            f"{current.minimum_version} or newer..."
        ),
    )
    if tool_id == "az":
        success = install_managed_azure_cli(job)
    elif tool_id == "azd":
        success = install_managed_azd(job)
    else:
        success, _ = run_process(job, command)
    if not success:
        job.emit("tool_status", tool_id=tool_id, status="failed")
        return False

    tool = next(item for item in prerequisite_statuses() if item.id == tool_id)
    if tool.ready:
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
            f"The update completed, but {tool.name} "
            f"{tool.version or 'could not report a version'} does not meet "
            f"the required minimum {tool.minimum_version}. "
            "Restart the app and retry the update."
        ),
    )
    return False


def install_tool_worker(
    job: Job,
    tool_id: str,
) -> None:
    with INSTALL_LOCK:
        job.emit("started", command=job.command)
        success = run_tool_install(job, tool_id)
        job.emit("done", success=success, exit_code=0 if success else 1)


def install_all_worker(job: Job, lab_id: Optional[str] = None) -> None:
    with INSTALL_LOCK:
        job.emit("started", command=[])
        lab = LABS_BY_ID.get(lab_id) if lab_id else None
        tool_ids = lab.dependency_ids if lab else None
        statuses = {
            tool.id: tool
            for tool in (
                prerequisite_statuses(tool_ids)
                if tool_ids
                else prerequisite_statuses()
            )
        }
        unresolved_required = [
            tool_id
            for tool_id in INSTALL_ORDER
            if tool_id in statuses
            and statuses[tool_id].required
            and not statuses[tool_id].ready
        ]
        if not unresolved_required:
            job.emit("output", line="All required dependencies meet their minimum versions.")
            job.emit("done", success=True, exit_code=0)
            return

        failures = []
        for tool_id in unresolved_required:
            if not run_tool_install(job, tool_id):
                failures.append(tool_id)

        ready = all(
            tool.ready
            for tool in (
                prerequisite_statuses(tool_ids)
                if tool_ids
                else prerequisite_statuses()
            )
            if tool.required
        )
        if ready:
            job.emit("output", line="All required dependencies are ready.")
        elif failures:
            job.emit(
                "error",
                message=f"Installation or update failed for: {', '.join(failures)}.",
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
        managed_azure_cli = AZURE_CLI_DIR / "bin" / "az.cmd"
        if os.path.normcase(os.path.abspath(resolved)) == os.path.normcase(
            os.path.abspath(managed_azure_cli)
        ):
            return [resolved, *command[1:]]
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
    if not is_azure_guid(tenant) or not is_azure_guid(subscription):
        return None
    return {"tenant": tenant, "subscription": subscription}


def is_azure_guid(value: str) -> bool:
    return AZURE_GUID_PATTERN.fullmatch(value) is not None


def build_azure_context_catalog(
    accounts: list[Any],
    active: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    tenants: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        tenant_id = str(account.get("tenantId") or "").strip()
        subscription_id = str(account.get("id") or "").strip()
        if not is_azure_guid(tenant_id) or not is_azure_guid(subscription_id):
            continue

        tenant_name = str(
            account.get("tenantDisplayName")
            or account.get("tenantDefaultDomain")
            or tenant_id
        ).strip()
        tenant = tenants.setdefault(
            tenant_id.lower(),
            {
                "id": tenant_id,
                "name": tenant_name,
                "subscriptions": [],
            },
        )
        if tenant["name"] == tenant["id"] and tenant_name != tenant_id:
            tenant["name"] = tenant_name
        if any(
            subscription["id"].lower() == subscription_id.lower()
            for subscription in tenant["subscriptions"]
        ):
            continue
        tenant["subscriptions"].append({
            "id": subscription_id,
            "name": str(account.get("name") or subscription_id).strip(),
            "is_default": account.get("isDefault") is True,
            "state": str(account.get("state") or ""),
        })

    active_tenant = (active or {}).get("tenant", "").lower()
    active_subscription = (active or {}).get("subscription", "").lower()
    catalog_tenants = list(tenants.values())
    for tenant in catalog_tenants:
        tenant["subscriptions"].sort(
            key=lambda subscription: (
                not subscription["is_default"],
                subscription["id"].lower() != active_subscription,
                subscription["name"].casefold(),
                subscription["id"].lower(),
            )
        )
    catalog_tenants.sort(
        key=lambda tenant: (
            not any(
                subscription["is_default"]
                for subscription in tenant["subscriptions"]
            ),
            tenant["id"].lower() != active_tenant,
            tenant["name"].casefold(),
            tenant["id"].lower(),
        )
    )
    return {
        "tenants": catalog_tenants,
        "active": active,
    }


def azure_context_catalog() -> tuple[Optional[dict[str, Any]], str]:
    success, output = run_capture([
        "az",
        "account",
        "list",
        "--query",
        (
            "[].{id:id,name:name,tenantId:tenantId,"
            "tenantDisplayName:tenantDisplayName,"
            "tenantDefaultDomain:tenantDefaultDomain,"
            "isDefault:isDefault,state:state}"
        ),
        "--output",
        "json",
        "--only-show-errors",
    ])
    if not success:
        return None, output or "Unable to list Azure subscriptions."
    try:
        accounts = json.loads(output)
    except json.JSONDecodeError:
        return None, "Azure CLI returned an invalid subscription list."
    if not isinstance(accounts, list):
        return None, "Azure CLI returned an unexpected subscription list."

    catalog = build_azure_context_catalog(accounts, cached_azure_context())
    if not catalog["tenants"]:
        return None, "No Azure subscriptions were discovered for this sign-in."
    return catalog, ""


def azure_context_is_available(
    catalog: dict[str, Any],
    tenant_id: str,
    subscription_id: str,
) -> bool:
    return any(
        tenant["id"].lower() == tenant_id.lower()
        and any(
            subscription["id"].lower() == subscription_id.lower()
            for subscription in tenant["subscriptions"]
        )
        for tenant in catalog["tenants"]
    )


def activate_azure_context(
    tenant_id: str,
    subscription_id: str,
) -> tuple[bool, str, bool, Optional[dict[str, str]]]:
    if not is_azure_guid(tenant_id) or not is_azure_guid(subscription_id):
        return False, "Tenant and subscription IDs must be valid GUIDs.", False, None

    with AZURE_CONTEXT_LOCK:
        catalog, error = azure_context_catalog()
        if catalog is None:
            return False, error, False, None
        if not azure_context_is_available(catalog, tenant_id, subscription_id):
            return (
                False,
                "The selected subscription was not discovered under that tenant.",
                False,
                None,
            )

        selected, output = run_capture([
            "az",
            "account",
            "set",
            "--subscription",
            subscription_id,
        ])
        if not selected:
            return False, output or "Unable to select the Azure subscription.", False, None

        active = cached_azure_context()
        if (
            active is None
            or active["tenant"].lower() != tenant_id.lower()
            or active["subscription"].lower() != subscription_id.lower()
        ):
            return False, "Azure CLI did not activate the requested tenant and subscription.", False, active
        if not azure_cli_management_authenticated():
            return (
                False,
                "The selected tenant requires an additional device-code sign-in.",
                True,
                active,
            )
        return True, "", False, active


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


def azd_login_command(
    context: Optional[dict[str, str]] = None,
) -> list[str]:
    command = [
        "azd",
        "auth",
        "login",
        "--use-device-code",
        "--no-prompt",
    ]
    if context:
        command.extend(["--tenant-id", context["tenant"]])
    return command


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
    scoped_request = "--tenant" in job.command
    if stale_context and not scoped_request:
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
    azd_code_match = re.search(
        r"start by copying the next code:\s*([A-Z0-9-]{6,12})",
        line,
        re.IGNORECASE,
    )
    if azd_code_match:
        return {
            "verification_url": AZURE_DEVICE_LOGIN_URL,
            "code": azd_code_match.group(1).upper(),
        }

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


def discover_edge_profiles(user_data_dir: Optional[Path] = None) -> list[dict[str, str]]:
    if user_data_dir is None:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            return []
        user_data_dir = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    local_state = user_data_dir / "Local State"
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.info("Microsoft Edge profile catalog was not found")
        return []
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Microsoft Edge profile catalog could not be read")
        return []

    info_cache = payload.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        LOGGER.warning("Microsoft Edge profile catalog has an invalid info_cache")
        return []

    try:
        resolved_user_data_dir = user_data_dir.resolve()
    except OSError:
        LOGGER.exception("Microsoft Edge user-data directory could not be resolved")
        return []

    profiles = []
    for directory, metadata in info_cache.items():
        if not isinstance(directory, str):
            continue
        profile_path = user_data_dir / directory
        try:
            profile_is_safe = (
                profile_path.resolve().is_relative_to(resolved_user_data_dir)
            )
        except OSError:
            profile_is_safe = False
        if (
            not re.fullmatch(r"(?:Default|Profile \d+)", directory)
            or not isinstance(metadata, dict)
            or not profile_is_safe
            or not profile_path.is_dir()
        ):
            continue
        profiles.append({
            "id": directory,
            "name": str(metadata.get("name") or directory).strip(),
            "email": str(metadata.get("user_name") or "").strip(),
        })
    return sorted(
        profiles,
        key=lambda profile: (
            profile["id"] != "Default",
            profile["name"].casefold(),
        ),
    )


def open_edge_profile_url(url: str, profile_directory: str) -> bool:
    edge = find_edge()
    available_profiles = {
        profile["id"]
        for profile in discover_edge_profiles()
    }
    if edge is None or profile_directory not in available_profiles:
        return False
    try:
        process = subprocess.Popen(
            [
                str(edge),
                f"--profile-directory={profile_directory}",
                "--new-window",
                url,
            ],
            creationflags=CREATE_NO_WINDOW,
        )
        LOGGER.info(
            "Opened URL in Edge profile=%s pid=%s",
            profile_directory,
            process.pid,
        )
        return True
    except OSError:
        LOGGER.exception(
            "Unable to open URL in Edge profile=%s",
            profile_directory,
        )
        return False


def demo_external_urls(
    state: Optional[dict[str, Any]] = None,
    values: Optional[dict[str, str]] = None,
) -> set[str]:
    state = state if state is not None else load_state()
    environment = str(state.get("environment") or "")
    values = values if values is not None else (
        azd_values(environment) if environment else {}
    )
    resource_group = values.get("AZURE_RESOURCE_GROUP", "")
    return {
        url
        for url in (
            values.get("AGENT_PORTAL_URL", "https://sre.azure.com"),
            values.get("CONTAINER_APP_URL", ""),
            values.get("FRONTEND_APP_URL", ""),
            azure_resource_group_portal_url(
                str(state.get("tenant_id") or ""),
                str(state.get("subscription_id") or ""),
                resource_group,
            ),
        )
        if url
    }


def is_allowed_demo_external_url(
    url: str,
    state: Optional[dict[str, Any]] = None,
    values: Optional[dict[str, str]] = None,
) -> bool:
    return (
        urlparse(url).scheme == "https"
        and url in demo_external_urls(state, values)
    )


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


def should_open_browser() -> bool:
    return os.environ.get("AZURE_SRE_DEMO_NO_BROWSER", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def should_fallback_open_client() -> bool:
    return os.environ.get(
        "AZURE_SRE_DEMO_CLIENT_FALLBACK", ""
    ).strip().lower() in {"1", "true", "yes"}


def open_application_window(url: str) -> bool:
    edge = find_edge() if os.name == "nt" else None
    if edge:
        try:
            process = subprocess.Popen(
                [str(edge), f"--app={url}", "--start-maximized"],
                creationflags=CREATE_NO_WINDOW,
            )
            LOGGER.info("Opened Edge application window pid=%s", process.pid)
            return True
        except OSError:
            LOGGER.exception("Unable to open Edge application window")
    return open_browser_url(url)


def launch_client_if_unclaimed(
    server: ThreadingHTTPServer,
    url: str,
) -> None:
    shutdown_event = getattr(server, "shutdown_event")
    if shutdown_event.wait(CLIENT_LAUNCH_FALLBACK_SECONDS):
        return
    with CLIENT_HEARTBEAT_LOCK:
        heartbeat_received = LAST_CLIENT_HEARTBEAT is not None
    if heartbeat_received:
        LOGGER.info("Client heartbeat received; browser fallback is not needed")
        return
    LOGGER.warning(
        "No client heartbeat after %.1f seconds; opening the application directly",
        CLIENT_LAUNCH_FALLBACK_SECONDS,
    )
    open_application_window(url)


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


def shutdown_application(server: ThreadingHTTPServer) -> None:
    LOGGER.info("Local application shutdown requested")
    shutdown_event = getattr(server, "shutdown_event", None)
    if shutdown_event is not None:
        shutdown_event.set()
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        job.terminate_process()
    server.shutdown()


def record_client_heartbeat() -> None:
    global LAST_CLIENT_HEARTBEAT
    with CLIENT_HEARTBEAT_LOCK:
        LAST_CLIENT_HEARTBEAT = time.monotonic()


def client_lease_expired(started_at: float, now: Optional[float] = None) -> bool:
    current_time = time.monotonic() if now is None else now
    with CLIENT_HEARTBEAT_LOCK:
        last_heartbeat = LAST_CLIENT_HEARTBEAT
    lease_reference = last_heartbeat if last_heartbeat is not None else started_at
    return current_time - lease_reference >= CLIENT_LEASE_TIMEOUT_SECONDS


def active_jobs_running() -> bool:
    with JOBS_LOCK:
        return any(not job.finished for job in JOBS.values())


def monitor_client_lease(server: ThreadingHTTPServer, started_at: float) -> None:
    shutdown_event = getattr(server, "shutdown_event")
    while not shutdown_event.wait(CLIENT_LEASE_CHECK_SECONDS):
        if not client_lease_expired(started_at):
            continue
        if active_jobs_running():
            LOGGER.info(
                "Client lease expired, but active jobs are still running"
            )
            continue
        LOGGER.info(
            "No browser heartbeat received for %.0f seconds; stopping local backend",
            CLIENT_LEASE_TIMEOUT_SECONDS,
        )
        shutdown_application(server)
        return


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


def set_azd_values(
    environment: str,
    values: dict[str, str],
) -> tuple[bool, str]:
    for key, value in values.items():
        success, output = run_capture(
            ["azd", "env", "set", "-e", environment, key, value],
            VENDOR_DIR,
        )
        if not success:
            return False, output or f"Unable to save {key}."
    return True, ""


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
            "Accept": "application/json",
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


def response_plan_payload(
    environment: str,
    subscription_id: str,
    resource_group: str,
) -> dict[str, Any]:
    alert_name = f"alert-http-5xx-{environment}"
    alert_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Insights/metricAlerts/{alert_name}"
    )
    return {
        "id": "grubify-http-errors",
        "name": "Grubify HTTP Errors",
        "priorities": ["Sev0", "Sev1", "Sev2", "Sev3", "Sev4"],
        "alertId": alert_id,
        "titleContains": "",
        "titleNotContains": [],
        "handlingAgent": "incident-handler",
        "agentMode": "autonomous",
    }


def upsert_response_plan(
    url: str,
    token: str,
    payload: dict[str, Any],
) -> tuple[int, str]:
    status, response = http_json("PUT", url, token, payload)
    if status == 409:
        return http_json("POST", url, token, payload)
    return status, response


def response_plan_status_is_retryable(status: int) -> bool:
    return status in {0, 404, 405, 408, 425, 429, 500, 502, 503, 504}


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

    job.emit("step", name="Uploading the Grubify lab knowledge base")
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
    response_plan = response_plan_payload(
        environment,
        subscription_id.strip(),
        resource_group,
    )
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
        status, response = upsert_response_plan(
            f"{endpoint}/api/v1/incidentPlayground/filters/grubify-http-errors",
            token.strip(),
            response_plan,
        )
        if status in (200, 201, 202, 204):
            return True
        if not response_plan_status_is_retryable(status):
            job.emit(
                "output",
                line=f"Response-plan creation failed: HTTP {status} {response[:300]}",
            )
            return False
        if attempt == 5:
            break
        job.emit(
            "output",
            line=f"Response plan attempt {attempt}/5 returned HTTP {status}; retrying...",
        )
        time.sleep(15)
    job.emit(
        "output",
        line=f"Response-plan creation failed: HTTP {status} {response[:300]}",
    )
    return False


def restore_container_baseline(job: Job, environment: str) -> bool:
    values = azd_values(environment)
    required = "AZURE_RESOURCE_GROUP", "CONTAINER_APP_NAME", "FRONTEND_APP_NAME"
    missing = [key for key in required if not values.get(key)]
    if missing:
        job.emit("output", line=f"Missing azd outputs: {', '.join(missing)}")
        return False

    resource_group = values["AZURE_RESOURCE_GROUP"]
    baselines = (
        (values["CONTAINER_APP_NAME"], "0.5", "1Gi"),
        (values["FRONTEND_APP_NAME"], "0.25", "0.5Gi"),
    )
    job.emit("step", name="Restoring Container App CPU and memory baselines")
    for app_name, cpu, memory in baselines:
        success, _ = run_process(
            job,
            [
                "az", "containerapp", "update",
                "--name", app_name,
                "--resource-group", resource_group,
                "--cpu", cpu,
                "--memory", memory,
                "--output", "none",
            ],
        )
        if not success:
            return False
    return True


def reconcile_demo(job: Job, restoring: bool = False) -> None:
    state = load_state()
    environment = state.get("environment")
    if not environment:
        job.emit("error", message="Configure an environment before deploying.")
        job.emit("done", success=False, exit_code=None)
        return
    job.emit(
        "started",
        command=["azd", "up", "-e", environment, "--no-prompt"],
    )
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
    job.emit(
        "step",
        name=(
            "Previewing baseline infrastructure reconciliation"
            if restoring
            else "Deploying Azure infrastructure"
        ),
    )
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
    if success and restoring:
        success = restore_container_baseline(job, environment)
    if success:
        success = post_provision(job, environment)
    if success:
        state["deployment_active"] = True
        save_state(state)
    job.emit("done", success=success, exit_code=0 if success else 1)


def deploy_worker(job: Job) -> None:
    reconcile_demo(job)


def restore_baseline_worker(job: Job) -> None:
    reconcile_demo(job, restoring=True)


def teardown_worker(job: Job) -> None:
    state = load_state()
    environment = state.get("environment")
    if not environment:
        job.emit("error", message="No configured environment.")
        job.emit("done", success=False, exit_code=None)
        return
    if (
        state.get("existing_environment")
        and state.get("existing_environment_detection") == "legacy"
    ):
        job.emit(
            "error",
            message=(
                "This compatible legacy lab was not created by this application. "
                "Delete it through its original deployment workflow."
            ),
        )
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
    if success:
        state["deployment_active"] = False
        state.pop("scenario_id", None)
        save_state(state)
    job.emit("done", success=success, exit_code=0 if success else 1)


def memory_pressure_observed(
    successes: int,
    max_consecutive_service_failures: int,
) -> bool:
    return successes >= 75 or (
        successes >= 50 and max_consecutive_service_failures >= 20
    )


def request_metrics_have_data(raw_metrics: str) -> bool:
    try:
        payload = json.loads(raw_metrics)
    except (json.JSONDecodeError, TypeError):
        return False
    for metric in payload.get("value", []):
        for timeseries in metric.get("timeseries", []):
            for sample in timeseries.get("data", []):
                total = sample.get("total")
                if isinstance(total, (int, float)) and total > 0:
                    return True
    return False


def wait_for_request_metrics(
    job: Job,
    app_url: str,
    resource_id: str,
    attempts: int = 24,
    delay_seconds: float = 15,
) -> bool:
    start_time = (
        datetime.now(timezone.utc) - timedelta(minutes=15)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    metric_command = [
        "az", "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", "Requests",
        "--interval", "PT1M",
        "--start-time", start_time,
        "--aggregation", "Total",
        "--output", "json",
    ]
    job.emit(
        "output",
        line="Checking Azure Monitor request metrics before fault injection...",
    )
    success, output = run_capture(metric_command)
    if success and request_metrics_have_data(output):
        job.emit("output", line="Azure Monitor request metrics are already ready.")
        return True

    job.emit("step", name="Waiting for Azure Monitor request metrics")
    job.emit(
        "output",
        line=(
            "No recent request metric is visible yet. Sending a harmless "
            f"readiness probe every {delay_seconds:g} seconds "
            f"(up to {attempts} attempts)."
        ),
    )
    for attempt in range(1, attempts + 1):
        probe = Request(
            f"{app_url}/api/cart/monitor-probe-{uuid.uuid4().hex[:8]}",
            method="GET",
        )
        try:
            with urlopen(probe, timeout=15) as response:
                if response.status != HTTPStatus.OK:
                    return False
        except (HTTPError, URLError, TimeoutError):
            return False

        job.emit(
            "output",
            line=(
                f"Metrics warm-up attempt {attempt}/{attempts}: probe accepted; "
                f"checking Azure Monitor in {delay_seconds:g} seconds..."
            ),
        )
        time.sleep(delay_seconds)
        success, output = run_capture(metric_command)
        if success and request_metrics_have_data(output):
            job.emit("output", line="Azure Monitor request metrics are ready.")
            return True
    job.emit(
        "output",
        line=f"Request metrics did not appear after {attempts} readiness probes.",
    )
    return False


def break_cart_worker(job: Job) -> None:
    state = load_state()
    environment = state.get("environment")
    values = azd_values(environment) if environment else {}
    app_url = values.get("CONTAINER_APP_URL", "").rstrip("/")
    if not app_url:
        job.emit("error", message="The deployed Grubify API URL is unavailable.")
        job.emit("done", success=False, exit_code=None)
        return

    job.emit("started", command=["POST", f"{app_url}/api/cart/demo-user/items"])
    subscription_id = state.get("subscription_id", "")
    resource_group = values.get("AZURE_RESOURCE_GROUP", "")
    app_name = values.get("CONTAINER_APP_NAME", "")
    if not subscription_id or not resource_group or not app_name:
        job.emit("error", message="The deployed Container App identity is unavailable.")
        job.emit("done", success=False, exit_code=None)
        return
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/containerApps/{app_name}"
    )
    if not wait_for_request_metrics(job, app_url, resource_id):
        job.emit(
            "error",
            message=(
                "Azure Monitor request metrics are not ready. Break Cart was "
                "not started because its alert could not be observed reliably."
            ),
        )
        job.emit("done", success=False, exit_code=1)
        return

    successes = 0
    errors = 0
    consecutive_service_failures = 0
    max_consecutive_service_failures = 0
    countdown_started = False
    scenario = LABS_BY_ID["grubify-starter-lab"].scenarios[0]

    def start_investigation_countdown() -> None:
        nonlocal countdown_started
        if countdown_started:
            return
        countdown_started = True
        job.emit(
            "output",
            line=(
                "First alert-triggering request failure detected. "
                "Starting the expected SRE Agent response window."
            ),
        )
        job.emit(
            "investigation_countdown",
            scenario_id=scenario.id,
            seconds=scenario.investigation_delay_seconds,
            started_at=time.time(),
        )

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
                    consecutive_service_failures = 0
                else:
                    errors += 1
                    if response.status >= 500:
                        consecutive_service_failures += 1
                        start_investigation_countdown()
                    else:
                        consecutive_service_failures = 0
        except HTTPError as error:
            errors += 1
            if error.code >= 500:
                consecutive_service_failures += 1
                start_investigation_countdown()
            else:
                consecutive_service_failures = 0
        except (URLError, TimeoutError):
            errors += 1
            consecutive_service_failures += 1
            start_investigation_countdown()
        max_consecutive_service_failures = max(
            max_consecutive_service_failures,
            consecutive_service_failures,
        )
        if index % 10 == 0:
            job.emit(
                "output",
                line=f"{index}/200 requests: {successes} succeeded, {errors} failed",
            )
        time.sleep(0.5)
    # Each success retains roughly 10 MiB. Either enough allocations or a
    # sustained service failure after substantial allocation proves the break.
    triggered = memory_pressure_observed(
        successes,
        max_consecutive_service_failures,
    )
    if triggered and max_consecutive_service_failures >= 20:
        job.emit(
            "output",
            line=(
                "Memory pressure observed: the service stopped accepting "
                f"requests after {successes} successful allocations."
            ),
        )
    if not triggered:
        job.emit(
            "error",
            message=(
                f"Memory pressure was not confirmed: {successes} requests "
                "succeeded and the longest service-failure streak was "
                f"{max_consecutive_service_failures}."
            ),
        )
    job.emit(
        "done",
        success=triggered,
        exit_code=0 if triggered else 1,
        successes=successes,
        errors=errors,
        max_consecutive_service_failures=max_consecutive_service_failures,
    )


SCENARIO_WORKERS: dict[tuple[str, str], Callable[[Job], None]] = {
    ("grubify-starter-lab", "memory-leak"): break_cart_worker,
}


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format_string: str, *args: Any) -> None:
        path = urlparse(self.path).path
        log = LOGGER.debug if path in {"/api/health", "/api/heartbeat"} else LOGGER.info
        log(
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
        if path == "/api/edge-profiles":
            self.send_json({
                "edge_available": find_edge() is not None,
                "profiles": discover_edge_profiles(),
            })
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
        if path == "/api/labs":
            self.send_json(lab_catalog_payload())
            return
        if path == "/api/prerequisites":
            lab = selected_lab()
            if lab is None:
                self.send_json(
                    {"error": "Select a lab before checking prerequisites."},
                    HTTPStatus.CONFLICT,
                )
                return
            self.send_json([
                asdict(status)
                for status in prerequisite_statuses(lab.dependency_ids)
            ])
            return
        if path == "/api/auth/status":
            self.send_json(authentication_statuses())
            return
        if path == "/api/azure-context":
            catalog, error = azure_context_catalog()
            if catalog is None:
                self.send_json({"error": error}, HTTPStatus.CONFLICT)
                return
            self.send_json(catalog)
            return
        if path == "/api/environments":
            state = load_state()
            lab = selected_lab(state)
            context = cached_azure_context()
            if lab is None:
                self.send_json({"error": "Select a lab first."}, HTTPStatus.CONFLICT)
                return
            if context is None or not azure_cli_management_authenticated():
                self.send_json(
                    {"error": "Authenticate the selected Azure subscription first."},
                    HTTPStatus.CONFLICT,
                )
                return
            self.send_json(discover_existing_environments(
                context["subscription"],
                lab.id,
            ))
            return
        if path == "/api/summary":
            state = load_state()
            environment = state.get("environment")
            if not environment:
                self.send_json({"error": "No configured environment"}, HTTPStatus.CONFLICT)
                return
            if not state.get("deployment_active"):
                self.send_json({"error": "Demo is not deployed"}, HTTPStatus.CONFLICT)
                return
            values = azd_values(environment)
            lab = selected_lab(state)
            resource_group = values.get("AZURE_RESOURCE_GROUP", "")
            self.send_json({
                "lab_id": lab.id if lab else "",
                "lab_name": lab.name if lab else "",
                "environment": environment,
                "existing_environment": bool(state.get("existing_environment")),
                "environment_detection": state.get(
                    "existing_environment_detection",
                    "",
                ),
                "resource_group": resource_group,
                "resource_group_portal_url": azure_resource_group_portal_url(
                    str(state.get("tenant_id") or ""),
                    str(state.get("subscription_id") or ""),
                    resource_group,
                ),
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
        log = LOGGER.debug if path == "/api/heartbeat" else LOGGER.info
        log("HTTP action POST path=%s client=%s", path, self.client_address[0])
        if path == "/api/shutdown":
            self.send_json({"shutting_down": True})
            threading.Thread(
                target=shutdown_application,
                args=(self.server,),
                daemon=True,
                name="application-shutdown",
            ).start()
            return
        if path == "/api/heartbeat":
            record_client_heartbeat()
            self.send_json({"active": True})
            return
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
        if path == "/api/lab":
            self.select_lab()
            return
        if path == "/api/auth/azure-cli":
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
                return
            tenant_id = str(payload.get("tenant_id", "")).strip()
            subscription_id = str(payload.get("subscription_id", "")).strip()
            if tenant_id or subscription_id:
                if not is_azure_guid(tenant_id) or not is_azure_guid(subscription_id):
                    self.send_json(
                        {"error": "Tenant and subscription IDs must be valid GUIDs."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                catalog, error = azure_context_catalog()
                if (
                    catalog is None
                    or not azure_context_is_available(
                        catalog,
                        tenant_id,
                        subscription_id,
                    )
                ):
                    self.send_json(
                        {
                            "error": error
                            or "The selected Azure context is not available."
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                command = scoped_azure_login_command({
                    "tenant": tenant_id,
                    "subscription": subscription_id,
                })
            else:
                command = [
                    "az",
                    "login",
                    "--scope",
                    "https://management.core.windows.net//.default",
                    "--use-device-code",
                ]
            job = create_job(command, worker=azure_login_worker)
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/auth/azd":
            job = create_job(
                azd_login_command(cached_azure_context()),
            )
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/install/all":
            lab = selected_lab()
            if lab is None:
                self.send_json({"error": "Select a lab first."}, HTTPStatus.CONFLICT)
                return
            job = create_job(
                worker=lambda current_job: install_all_worker(current_job, lab.id)
            )
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        if path.startswith("/api/install/"):
            tool_id = path.removeprefix("/api/install/").strip("/")
            lab = selected_lab()
            if lab is None:
                self.send_json({"error": "Select a lab first."}, HTTPStatus.CONFLICT)
                return
            if tool_id not in lab.dependency_ids or tool_id not in INSTALL_COMMANDS:
                self.send_json({"error": "Unsupported installer"}, HTTPStatus.NOT_FOUND)
                return
            job = create_job(
                worker=lambda current_job: install_tool_worker(
                    current_job,
                    tool_id,
                ),
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
        if path == "/api/open-edge-link":
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
                return
            url = str(payload.get("url") or "").strip()
            profile = str(payload.get("profile") or "").strip()
            state = load_state()
            environment = str(state.get("environment") or "")
            values = azd_values(environment) if environment else {}
            if not is_allowed_demo_external_url(url, state, values):
                self.send_json(
                    {"error": "This URL is not part of the active demo."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not open_edge_profile_url(url, profile):
                self.send_json(
                    {"error": "The selected Microsoft Edge profile is unavailable."},
                    HTTPStatus.CONFLICT,
                )
                return
            self.send_json({"opened": True})
            return
        if path == "/api/azure-context":
            try:
                payload = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
                return
            tenant_id = str(payload.get("tenant_id", "")).strip()
            subscription_id = str(payload.get("subscription_id", "")).strip()
            success, error, requires_auth, active = activate_azure_context(
                tenant_id,
                subscription_id,
            )
            if not success:
                status = (
                    HTTPStatus.UNAUTHORIZED
                    if requires_auth
                    else HTTPStatus.CONFLICT
                )
                self.send_json(
                    {
                        "error": error,
                        "requires_auth": requires_auth,
                        "active": active,
                    },
                    status,
                )
                return
            self.send_json({"active": active})
            return
        if path == "/api/configure":
            self.configure_environment()
            return
        if path == "/api/environments/validate":
            self.validate_environment()
            return
        if path == "/api/scenarios/run":
            self.run_scenario()
            return
        workers = {
            "/api/deploy": deploy_worker,
            "/api/restore-baseline": restore_baseline_worker,
            "/api/teardown": teardown_worker,
        }
        if path in workers:
            if path != "/api/teardown" and selected_lab() is None:
                self.send_json(
                    {"error": "Select a supported lab before deploying."},
                    HTTPStatus.CONFLICT,
                )
                return
            job = create_job(worker=workers[path])
            self.send_json({"job_id": job.id}, HTTPStatus.ACCEPTED)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def select_lab(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return
        lab_id = str(payload.get("lab_id", "")).strip()
        lab = LABS_BY_ID.get(lab_id)
        if lab is None:
            self.send_json({"error": "Unsupported lab."}, HTTPStatus.BAD_REQUEST)
            return

        state = load_state()
        previous_lab_id = str(state.get("lab_id", ""))
        if (
            previous_lab_id
            and previous_lab_id != lab.id
            and state.get("deployment_active")
            and not state.get("existing_environment")
        ):
            self.send_json(
                {"error": "Tear down the active lab before selecting another lab."},
                HTTPStatus.CONFLICT,
            )
            return
        if not previous_lab_id:
            state["lab_id"] = lab.id
        elif previous_lab_id != lab.id:
            state = {
                "lab_id": lab.id,
                "deployment_active": False,
            }
        else:
            state["lab_id"] = lab.id
        save_state(state)
        self.send_json({"lab": asdict(lab), "state": state})

    def run_scenario(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return

        state = load_state()
        lab = selected_lab(state)
        if lab is None:
            self.send_json({"error": "Select a lab first."}, HTTPStatus.CONFLICT)
            return
        if not state.get("deployment_active"):
            self.send_json(
                {"error": "Deploy the lab before running a scenario."},
                HTTPStatus.CONFLICT,
            )
            return
        scenario_id = str(payload.get("scenario_id", "")).strip()
        scenario = next(
            (item for item in lab.scenarios if item.id == scenario_id),
            None,
        )
        worker = SCENARIO_WORKERS.get((lab.id, scenario_id))
        if scenario is None or worker is None:
            self.send_json(
                {"error": "Unsupported scenario for this lab."},
                HTTPStatus.BAD_REQUEST,
            )
            return

        state["scenario_id"] = scenario.id
        save_state(state)
        job = create_job(worker=worker)
        self.send_json(
            {"job_id": job.id, "scenario": asdict(scenario)},
            HTTPStatus.ACCEPTED,
        )

    def configure_environment(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return

        state = load_state()
        lab = selected_lab(state)
        if lab is None:
            self.send_json(
                {"error": "Select a supported lab before configuring Azure."},
                HTTPStatus.CONFLICT,
            )
            return

        environment = str(payload.get("environment", "")).strip()
        location = str(payload.get("location", "")).strip().lower()
        existing_environment = payload.get("existing_environment") is True
        if not re.fullmatch(r"[a-zA-Z0-9-]{2,30}", environment):
            self.send_json(
                {"error": "Environment must be 2-30 letters, numbers, or hyphens."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if location not in SRE_AGENT_REGIONS:
            self.send_json({"error": "Unsupported Azure region."}, HTTPStatus.BAD_REQUEST)
            return

        context = cached_azure_context()
        if context is None or not azure_cli_management_authenticated():
            self.send_json(
                {
                    "error": (
                        "Select and authenticate an Azure tenant and subscription "
                        "before configuring the environment."
                    )
                },
                HTTPStatus.CONFLICT,
            )
            return
        subscription_id = context["subscription"]

        new_command = [
            "azd", "env", "new", environment,
            "--location", location,
            "--subscription", subscription_id,
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
            ("AZURE_SUBSCRIPTION_ID", subscription_id),
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

        state.update({
            "lab_id": lab.id,
            "environment": environment,
            "location": location,
            "tenant_id": context["tenant"],
            "subscription_id": subscription_id,
            "deployment_active": False,
            "existing_environment": existing_environment,
        })
        state.pop("scenario_id", None)
        state.pop("validated_at", None)
        state.pop("validation_issues", None)
        if not existing_environment:
            state.pop("existing_environment_detection", None)
        save_state(state)
        self.send_json(state)

    def validate_environment(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return

        state = load_state()
        lab = selected_lab(state)
        context = cached_azure_context()
        environment_name = str(payload.get("environment") or "").strip()
        resource_group = str(payload.get("resource_group") or "").strip()
        if lab is None or context is None:
            self.send_json(
                {"error": "Select a lab and Azure subscription first."},
                HTTPStatus.CONFLICT,
            )
            return
        if (
            not state.get("existing_environment")
            or state.get("environment") != environment_name
            or state.get("subscription_id") != context["subscription"]
        ):
            self.send_json(
                {"error": "Save the selected existing lab before validating it."},
                HTTPStatus.CONFLICT,
            )
            return

        candidate = next(
            (
                item
                for item in load_environment_cache(
                    context["subscription"],
                    lab.id,
                )
                if item.get("environment") == environment_name
                and item.get("resource_group") == resource_group
            ),
            None,
        )
        if candidate is None:
            self.send_json(
                {
                    "error": (
                        "The selected lab is not in the latest subscription scan. "
                        "Scan the subscription again."
                    )
                },
                HTTPStatus.CONFLICT,
            )
            return

        result = validate_existing_lab(context["subscription"], candidate)
        state["deployment_active"] = False
        state["existing_environment_detection"] = candidate.get("detection", "")
        state["validation_issues"] = result["issues"]
        state.pop("validated_at", None)
        if not result["ready"]:
            save_state(state)
            self.send_json({
                "ready": False,
                "environment": environment_name,
                "issues": result["issues"],
            })
            return

        saved, error = set_azd_values(environment_name, result["values"])
        if not saved:
            save_state(state)
            self.send_json(
                {"error": error},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        state["deployment_active"] = True
        state["validation_issues"] = []
        state["validated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        self.send_json({
            "ready": True,
            "environment": environment_name,
            "issues": [],
        })

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
        "Application starting pid=%s frozen=%s portable=%s executable=%s python=%s cwd=%s",
        os.getpid(),
        FROZEN,
        PORTABLE,
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
        server.daemon_threads = True
        server.shutdown_event = threading.Event()
    except OSError:
        LOGGER.exception("Unable to bind web server to %s:%s", HOST, PORT)
        raise
    url = f"http://{HOST}:{PORT}"
    LOGGER.info("SRE Agent onboarding wizard ready: %s", url)
    LOGGER.info("Diagnostic log: %s", log_file)
    if should_open_browser():
        browser_timer = threading.Timer(0.6, lambda: open_browser_url(url))
        browser_timer.name = "browser-launch"
        browser_timer.start()
    else:
        LOGGER.info("Automatic browser launch disabled by environment")
    if should_fallback_open_client():
        threading.Thread(
            target=launch_client_if_unclaimed,
            args=(server, url),
            daemon=True,
            name="client-launch-fallback",
        ).start()
    lease_started_at = time.monotonic()
    threading.Thread(
        target=monitor_client_lease,
        args=(server, lease_started_at),
        daemon=True,
        name="client-lease-monitor",
    ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping server after keyboard interrupt")
    finally:
        server.shutdown_event.set()
        server.server_close()
        LOGGER.info("Application stopped")


if __name__ == "__main__":
    main()
