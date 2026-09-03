from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import ssl
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
    BUNDLED_VENDOR_ROOT = ROOT / "vendor"
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLED_VENDOR_ROOT = (
        ROOT / "vendor"
        if PORTABLE
        else ROOT.parent / "vendor"
    )
STATIC_DIR = ROOT / "static"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "AzureSREAgentDemo"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
ENVIRONMENT_CACHE_FILE = STATE_DIR / "environments.json"
CONFIG_FILE = STATE_DIR / "config.ini"
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
POWERSHELL_VERSION = "7.6.5"
POWERSHELL_URL = (
    "https://github.com/PowerShell/PowerShell/releases/download/"
    f"v{POWERSHELL_VERSION}/PowerShell-{POWERSHELL_VERSION}-win-x64.zip"
)
POWERSHELL_SHA256 = (
    "32EB8F6CDCE08F86E987D625A2733E54AC3E289AE7E1621B14C0B5BCEC2434EA"
)
POWERSHELL_DIR = MANAGED_TOOLS_DIR / "powershell"
POWERSHELL_DOCS_URL = (
    "https://learn.microsoft.com/powershell/scripting/install/"
    "installing-powershell-on-windows"
)
if FROZEN or PORTABLE:
    VENDOR_ROOT = STATE_DIR / "labs"
    VENDOR_ROOT.mkdir(parents=True, exist_ok=True)
    for bundled_lab in BUNDLED_VENDOR_ROOT.iterdir():
        if bundled_lab.is_dir():
            shutil.copytree(
                bundled_lab,
                VENDOR_ROOT / bundled_lab.name,
                dirs_exist_ok=True,
            )
else:
    VENDOR_ROOT = ROOT.parent / "vendor"
VENDOR_DIR = VENDOR_ROOT / "starter-lab"
HOST = "127.0.0.1"
PORT = 8765
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
AUTH_RETRY_GRACE_SECONDS = 4.0
CLIENT_LEASE_TIMEOUT_SECONDS = 300.0
CLIENT_LEASE_CHECK_SECONDS = 10.0
CLIENT_LAUNCH_FALLBACK_SECONDS = 5.0
SESSION_TOKEN = uuid.uuid4().hex
LOGGER = logging.getLogger("AzureSREAgentDemo")
LOG_FILE: Optional[Path] = None
AZURE_DEVICE_LOGIN_URL = "https://microsoft.com/devicelogin"
AZURE_GUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|[P^_].*?\x1B\\|"
    r"[@-_][0-?]*[ -/]*[@-~])"
)
CLI_SPINNER_PATTERN = re.compile(r"^[|/\\-]\s+Running\s+\.*$")
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


@dataclass(frozen=True)
class RuntimeOptions:
    config_file: Path
    test_mode: bool = False


@dataclass(frozen=True)
class DelayDefinition:
    seconds: float
    description: str
    production_message: str


NONESSENTIAL_DELAYS = {
    "azure_monitor_initialization": DelayDefinition(
        seconds=30,
        description="Azure Monitor initialization stabilization",
        production_message="Waiting 30 seconds for Azure Monitor initialization...",
    ),
}
RUNTIME_OPTIONS = RuntimeOptions(config_file=CONFIG_FILE)


def load_test_mode_config(config_file: Path) -> bool:
    if not config_file.is_file():
        return False
    parser = configparser.ConfigParser()
    try:
        with config_file.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        return parser.getboolean("application", "test_mode", fallback=False)
    except (OSError, configparser.Error, ValueError) as error:
        raise ValueError(
            f"Unable to read test_mode from {config_file}: {error}"
        ) from error


def parse_runtime_options(
    argv: Optional[list[str]] = None,
    default_config_file: Path = CONFIG_FILE,
) -> RuntimeOptions:
    parser = argparse.ArgumentParser(description="Azure SRE Agent Demo")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_file,
        help="Path to the local INI configuration file.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--test-mode",
        action="store_true",
        default=None,
        help="Enable explicit test-only controls and nonessential wait bypasses.",
    )
    mode.add_argument(
        "--no-test-mode",
        action="store_false",
        dest="test_mode",
        help="Disable test mode even when the INI configuration enables it.",
    )
    arguments = parser.parse_args(argv)
    config_file = arguments.config.expanduser()
    configured_test_mode = load_test_mode_config(config_file)
    return RuntimeOptions(
        config_file=config_file,
        test_mode=(
            configured_test_mode
            if arguments.test_mode is None
            else arguments.test_mode
        ),
    )


def set_runtime_options(options: RuntimeOptions) -> None:
    global RUNTIME_OPTIONS
    RUNTIME_OPTIONS = options


def is_test_mode() -> bool:
    return RUNTIME_OPTIONS.test_mode


def wait_for_nonessential_delay(
    name: str,
    notify: Optional[Callable[[str], None]] = None,
) -> None:
    delay = NONESSENTIAL_DELAYS[name]
    if is_test_mode():
        message = f"Test mode: skipped {delay.description} wait."
        LOGGER.warning(message)
        if notify:
            notify(message)
        return
    if notify:
        notify(delay.production_message)
    time.sleep(delay.seconds)


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


def sanitize_terminal_output(value: str) -> str:
    without_ansi = ANSI_ESCAPE_PATTERN.sub("", value)
    return "".join(
        character
        for character in without_ansi
        if character in "\t"
        or ord(character) >= 32
    ).strip()


def is_transient_cli_spinner(value: str) -> bool:
    return bool(CLI_SPINNER_PATTERN.fullmatch(value))


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
    script_id: str = ""
    lane_port: Optional[int] = None
    probe_path: str = "/"
    web_health: bool = True


@dataclass(frozen=True)
class RegionDefinition:
    id: str
    name: str
    default: str
    allowed_values: tuple[str, ...]


@dataclass(frozen=True)
class LabDefinition:
    id: str
    name: str
    description: str
    resource_count: int
    estimated_turnaround: str
    dependency_ids: tuple[str, ...]
    scenarios: tuple[ScenarioDefinition, ...]
    vendor_directory: str = "starter-lab"
    default_environment: str = "sre-lab"
    regions: tuple[RegionDefinition, ...] = ()


TOOLS = (
    ("az", "Azure CLI", ("version",), "2.88.0",
     AZURE_CLI_DOCS_URL, True),
    ("azd", "Azure Developer CLI", ("version",), "1.28.0",
     AZD_DOCS_URL, True),
    ("pwsh", "PowerShell", ("--version",), "7.6.5",
     POWERSHELL_DOCS_URL, True),
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
                investigation_delay_seconds=240,
                script_id="memory-leak",
            ),
        ),
        regions=(
            RegionDefinition(
                id="location",
                name="Azure region for new resources",
                default="eastus2",
                allowed_values=tuple(sorted(SRE_AGENT_REGIONS)),
            ),
        ),
    ),
    LabDefinition(
        id="zava-learning",
        name="Zava Learning Lab",
        description=(
            "Deploy a multi-tier online learning platform and observe Azure SRE "
            "Agent diagnose and remediate network, application, database, secret, "
            "and virtual-machine incidents."
        ),
        resource_count=66,
        estimated_turnaround="25-45 min",
        dependency_ids=("az", "azd", "pwsh"),
        vendor_directory="zava-learning",
        default_environment="demo",
        regions=(
            RegionDefinition(
                id="location",
                name="Workload region",
                default="southcentralus",
                allowed_values=(
                    "centralus",
                    "eastus2",
                    "southcentralus",
                    "westus2",
                    "westus3",
                ),
            ),
            RegionDefinition(
                id="db_location",
                name="PostgreSQL region",
                default="westus3",
                allowed_values=(
                    "centralus",
                    "eastus2",
                    "southcentralus",
                    "westus2",
                    "westus3",
                ),
            ),
            RegionDefinition(
                id="agent_location",
                name="SRE Agent region",
                default="eastus2",
                allowed_values=tuple(sorted(SRE_AGENT_REGIONS)),
            ),
        ),
        scenarios=(
            ScenarioDefinition(
                id="nsg",
                name="Quiz Connectivity",
                description=(
                    "Inject an NSG priority inversion that blocks the quiz lane."
                ),
                action_label="Run Quiz Connectivity",
                confirmation="Inject the Zava quiz connectivity fault?",
                investigation_delay_seconds=900,
                script_id="nsg",
                lane_port=8081,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="appgw",
                name="Portal 502 Errors",
                description=(
                    "Point the Application Gateway health probe at an invalid path."
                ),
                action_label="Run Portal 502 Errors",
                confirmation="Inject the Zava Application Gateway fault?",
                investigation_delay_seconds=300,
                script_id="appgw",
                lane_port=8082,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="app",
                name="Quiz Service Unavailable",
                description="Scale the quiz application lane to zero active replicas.",
                action_label="Run Quiz Service Unavailable",
                confirmation="Take the Zava quiz application lane offline?",
                investigation_delay_seconds=600,
                script_id="app",
                lane_port=8083,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="perf",
                name="Slow Quiz Release",
                description="Deploy a deliberately slow quiz-service release.",
                action_label="Run Slow Quiz Release",
                confirmation="Deploy the slow Zava quiz-service release?",
                investigation_delay_seconds=480,
                script_id="perf",
                lane_port=8084,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="query",
                name="Slow Database Query",
                description="Remove the question-bank index and force full scans.",
                action_label="Run Slow Database Query",
                confirmation="Inject the Zava database query fault?",
                investigation_delay_seconds=600,
                script_id="query",
                lane_port=8085,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="pool",
                name="Connection Exhaustion",
                description="Clamp the quiz database role connection limit.",
                action_label="Run Connection Exhaustion",
                confirmation="Inject the Zava database connection-pool fault?",
                investigation_delay_seconds=600,
                script_id="pool",
                lane_port=8086,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="secret",
                name="Invalid Database Secret",
                description="Rotate the quiz lane database credential to an invalid value.",
                action_label="Run Invalid Database Secret",
                confirmation="Inject the Zava database secret fault?",
                investigation_delay_seconds=720,
                script_id="secret",
                lane_port=8087,
                probe_path="/quiz/BIO-101",
            ),
            ScenarioDefinition(
                id="disk",
                name="Reporting Disk Pressure",
                description="Fill the reporting worker data disk until exports fail.",
                action_label="Run Reporting Disk Pressure",
                confirmation="Fill the Zava reporting worker data disk?",
                investigation_delay_seconds=900,
                script_id="disk",
                web_health=False,
            ),
        ),
    ),
)
LABS_BY_ID = {lab.id: lab for lab in LABS}
LAB_ID_TAG = "sre-agent-demo-lab-id"
LAB_ENVIRONMENT_TAG = "sre-agent-demo-environment"
ZAVA_SECRET_NAMES = frozenset({
    "db-password",
    "db-pool-password",
    "vm-admin-password",
})
ZAVA_REQUIRED_SECRET_NAMES = (
    "db-password",
    "db-pool-password",
    "vm-admin-password",
)
ZAVA_CONTAINER_APPS = frozenset({
    "learner-portal",
    "course-api",
    "assessment-api",
    "gradebook-api",
    "quiz-nsg",
    "quiz-appgw",
    "quiz-app",
    "quiz-perf",
    "quiz-query",
    "quiz-pool",
    "quiz-secret",
})
ZAVA_LANE_PORTS = tuple(range(8081, 8088))
ZAVA_CORE_AGENTS = (
    "zava-cost-analyst",
    "zava-incident-responder",
    "zava-nsg-auditor",
    "zava-rbac-auditor",
)
ZAVA_CORE_SKILLS = (
    "connectivity-triage",
    "cost-analysis",
    "evidence-before-after",
    "nsg-audit",
    "performance-investigation",
    "rbac-audit",
    "rca-analysis",
    "recommendations-next-steps",
    "redaction-guard",
    "zava-audit-report",
    "zava-reporting",
)
ZAVA_CORE_CONFIG_VERSION = "1"
ZAVA_OPTIONAL_SKILLS = {
    "pagerduty": "pagerduty-incident-update",
    "servicenow": "servicenow-change-management",
    "github": "pr-delivery",
}
ZAVA_PAGERDUTY_TOOLS = (
    "pagerduty_get_incident",
    "pagerduty_list_incidents",
    "pagerduty_manage_incidents",
    "pagerduty_add_note_to_incident",
)
_INTEGRATION_STORE: dict[tuple[str, str], dict[str, str]] = {}
_INTEGRATION_STORE_LOCK = threading.RLock()


def integration_store_key(lab_id: str, environment: str) -> tuple[str, str]:
    return lab_id, environment.casefold()


def get_in_memory_secrets(lab_id: str, environment: str) -> dict[str, str]:
    with _INTEGRATION_STORE_LOCK:
        return dict(_INTEGRATION_STORE.get(
            integration_store_key(lab_id, environment),
            {},
        ))


def update_in_memory_secrets(
    lab_id: str,
    environment: str,
    values: dict[str, str],
) -> None:
    with _INTEGRATION_STORE_LOCK:
        key = integration_store_key(lab_id, environment)
        current = _INTEGRATION_STORE.setdefault(key, {})
        current.update({name: value for name, value in values.items() if value})


def replace_in_memory_secrets(
    lab_id: str,
    environment: str,
    values: dict[str, str],
) -> None:
    """Atomically replace transient deployment data, removing omitted values."""
    replacement = {name: value for name, value in values.items() if value}
    with _INTEGRATION_STORE_LOCK:
        key = integration_store_key(lab_id, environment)
        if replacement:
            _INTEGRATION_STORE[key] = replacement
        else:
            _INTEGRATION_STORE.pop(key, None)


def clear_in_memory_secrets(lab_id: str, environment: str) -> None:
    with _INTEGRATION_STORE_LOCK:
        _INTEGRATION_STORE.pop(integration_store_key(lab_id, environment), None)


def generate_deployment_password(length: int = 36) -> str:
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "abcdefghijkmnopqrstuvwxyz"
        "23456789"
        "!@#%_-+="
    )
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(character.isupper() for character in value)
            and any(character.islower() for character in value)
            and any(character.isdigit() for character in value)
            and any(character in "!@#%_-+=" for character in value)
        ):
            return value


def new_zava_deployment_secrets() -> dict[str, str]:
    return {
        "POSTGRES_ADMIN_PASSWORD": generate_deployment_password(),
        "POSTGRES_POOL_PASSWORD": generate_deployment_password(),
        "VM_ADMIN_PASSWORD": generate_deployment_password(),
    }


def zava_environment_suffix(environment: str) -> str:
    prefix = "zava-learning-"
    normalized = environment.strip()
    if normalized.casefold().startswith(prefix):
        normalized = normalized[len(prefix):]
    elif normalized.casefold() == "zava-learning":
        normalized = ""
    return normalized or "demo"


def zava_resource_group_name(environment: str) -> str:
    return f"rg-zava-learning-{zava_environment_suffix(environment)}"


def zava_agent_name(environment: str) -> str:
    return f"sre-zava-learning-{zava_environment_suffix(environment)}"


def normalize_azure_location(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def validate_lab_regions(
    lab: LabDefinition,
    payload: dict[str, Any],
) -> tuple[dict[str, str], Optional[str]]:
    values: dict[str, str] = {}
    for region in lab.regions:
        value = str(payload.get(region.id, "")).strip().lower()
        if value not in region.allowed_values:
            return {}, f"Unsupported {region.name.lower()}."
        values[region.id] = value
    return values, None


def parse_zava_integrations(payload: Any) -> tuple[dict[str, str], Optional[str]]:
    if payload is None:
        return {}, None
    if not isinstance(payload, dict):
        return {}, "Integrations must be an object."
    allowed = {
        "pagerduty_api_token",
        "pagerduty_webhook_url",
        "pagerduty_service_id",
        "pagerduty_obo_email",
        "servicenow_url",
        "servicenow_user",
        "servicenow_password",
        "github_repo",
        "github_repository",
        "github_token",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        return {}, f"Unsupported integration fields: {', '.join(unknown)}."
    result: dict[str, str] = {}
    for key, raw_value in payload.items():
        if not isinstance(raw_value, str):
            return {}, f"Integration field {key} must be text."
        value = raw_value.strip()
        if len(value) > 4096:
            return {}, f"Integration field {key} is too long."
        if value:
            result[key] = value
    for url_key in ("pagerduty_webhook_url", "servicenow_url"):
        if url_key in result and urlparse(result[url_key]).scheme != "https":
            return {}, f"Integration field {url_key} must use HTTPS."
    if "github_repository" in result:
        result["github_repo"] = result.pop("github_repository")
    if "github_repo" in result and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        result["github_repo"],
    ):
        return {}, "github_repo must use owner/repository format."
    requested_pagerduty = any(
        result.get(name)
        for name in (
            "pagerduty_api_token",
            "pagerduty_webhook_url",
            "pagerduty_service_id",
            "pagerduty_obo_email",
        )
    )
    if requested_pagerduty:
        missing = [
            name
            for name in (
                "pagerduty_api_token",
                "pagerduty_webhook_url",
                "pagerduty_obo_email",
            )
            if not result.get(name)
        ]
        if missing:
            return {}, (
                "PagerDuty setup requires pagerduty_api_token, "
                "pagerduty_webhook_url, and pagerduty_obo_email."
            )
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", result["pagerduty_obo_email"]):
            return {}, "pagerduty_obo_email must be a valid email address."
    requested_servicenow = any(
        result.get(name)
        for name in ("servicenow_url", "servicenow_user", "servicenow_password")
    )
    if requested_servicenow and not all(
        result.get(name)
        for name in ("servicenow_url", "servicenow_user", "servicenow_password")
    ):
        return {}, (
            "ServiceNow setup requires servicenow_url, servicenow_user, "
            "and servicenow_password."
        )
    if result.get("github_repo") or result.get("github_token"):
        return {}, (
            "GitHub integration cannot be configured from the supplied settings. "
            "Connect GitHub in the SRE Agent portal after deployment."
        )
    return result, None


def vendor_dir_for_lab(lab: LabDefinition) -> Path:
    return VENDOR_ROOT / lab.vendor_directory


def selected_vendor_dir(
    state: Optional[dict[str, Any]] = None,
) -> Path:
    lab = selected_lab(state)
    return vendor_dir_for_lab(lab) if lab else VENDOR_DIR


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
ACTIVE_SCENARIO_LOCK = threading.Lock()
LAST_CLIENT_HEARTBEAT: Optional[float] = None

INSTALL_COMMANDS = {
    "az": ["app-managed", "azure-cli", AZURE_CLI_VERSION],
    "azd": ["app-managed", "azure-developer-cli", AZD_VERSION],
    "pwsh": ["app-managed", "powershell", POWERSHELL_VERSION],
}
UPDATE_COMMANDS = {
    "az": INSTALL_COMMANDS["az"],
    "azd": INSTALL_COMMANDS["azd"],
    "pwsh": INSTALL_COMMANDS["pwsh"],
}
REPAIR_COMMANDS = {
    "az": INSTALL_COMMANDS["az"],
    "azd": INSTALL_COMMANDS["azd"],
    "pwsh": INSTALL_COMMANDS["pwsh"],
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


def sre_agent_portal_url(
    subscription_id: str,
    resource_group: str,
    agent_name: str,
) -> str:
    if not subscription_id or not resource_group or not agent_name:
        return ""
    return (
        "https://sre.azure.com/agents/subscriptions/"
        f"{quote(subscription_id, safe='')}/resourceGroups/"
        f"{quote(resource_group, safe='')}/providers/Microsoft.App/agents/"
        f"{quote(agent_name, safe='')}"
    )


def resolved_sre_agent_portal_url(
    state: dict[str, Any],
    values: dict[str, str],
) -> str:
    deep_link = sre_agent_portal_url(
        str(
            state.get("subscription_id")
            or values.get("AZURE_SUBSCRIPTION_ID")
            or ""
        ),
        values.get("AZURE_RESOURCE_GROUP", ""),
        values.get("SRE_AGENT_NAME", ""),
    )
    return deep_link or values.get("AGENT_PORTAL_URL") or "https://sre.azure.com"


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
    labs = []
    for lab in LABS:
        payload = asdict(lab)
        payload["configuration_schema"] = {
            "regions": [asdict(region) for region in lab.regions],
            "supports_integrations": lab.id == "zava-learning",
            "integrations": (
                [
                    {"id": "pagerduty_api_token", "secret": True},
                    {"id": "pagerduty_webhook_url", "secret": True},
                    {"id": "pagerduty_service_id", "secret": False},
                    {"id": "pagerduty_obo_email", "secret": False},
                    {"id": "servicenow_url", "secret": False},
                    {"id": "servicenow_user", "secret": False},
                    {"id": "servicenow_password", "secret": True},
                    {"id": "github_repository", "secret": False},
                    {"id": "github_token", "secret": True},
                ]
                if lab.id == "zava-learning"
                else []
            ),
        }
        labs.append(payload)
    return {
        "labs": labs,
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


def local_azd_environment_names(lab: Optional[LabDefinition] = None) -> set[str]:
    active_lab = lab or selected_lab()
    vendor_dir = vendor_dir_for_lab(active_lab) if active_lab else VENDOR_DIR
    success, output = run_capture(
        ["azd", "env", "list", "--output", "json"],
        vendor_dir,
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
    public_ips: Optional[list[dict[str, Any]]] = None,
    postgres_servers: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    agents_by_group = {
        str(resource.get("resourceGroup") or "").casefold(): resource
        for resource in agents
        if resource.get("resourceGroup")
    }
    apps_by_group: dict[str, list[dict[str, Any]]] = {}
    for resource in container_apps:
        resource_group = str(resource.get("resourceGroup") or "").casefold()
        if resource_group and resource.get("name"):
            apps_by_group.setdefault(resource_group, []).append(resource)
    public_ips_by_group: dict[str, list[dict[str, Any]]] = {}
    for resource in public_ips or []:
        resource_group = str(resource.get("resourceGroup") or "").casefold()
        if resource_group:
            public_ips_by_group.setdefault(resource_group, []).append(resource)
    postgres_by_group = {
        str(resource.get("resourceGroup") or "").casefold(): resource
        for resource in postgres_servers or []
        if resource.get("resourceGroup")
    }

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
        app_names = {
            str(app.get("name") or "").casefold()
            for app in apps_by_group.get(resource_group_key, [])
        }
        tagged_lab_id = tags.get(LAB_ID_TAG.casefold(), "")
        if tagged_lab_id:
            if tagged_lab_id.casefold() != lab_id.casefold():
                continue
            if (
                lab_id == "zava-learning"
                and resource_group_key not in agents_by_group
                and not {"learner-portal", "assessment-api"}.issubset(app_names)
            ):
                continue
            detection = "managed"
        else:
            is_grubify_lab = lab_id == "grubify-starter-lab" and (
                resource_group_key.startswith("rg-")
                and resource_group_key in agents_by_group
                and any(
                    name.startswith("ca-grubify-")
                    and not name.startswith("ca-grubify-fe-")
                    for name in app_names
                )
                and any(name.startswith("ca-grubify-fe-") for name in app_names)
            )
            is_zava_lab = lab_id == "zava-learning" and (
                tags.get("solution", "").casefold() == "zava-learning"
                or (
                    resource_group_key in agents_by_group
                    and {"learner-portal", "quiz-nsg", "quiz-appgw"}.issubset(
                        app_names
                    )
                )
            )
            if not is_grubify_lab and not is_zava_lab:
                continue
            detection = "legacy"

        environment = (
            tags.get(LAB_ENVIRONMENT_TAG.casefold(), "")
            or resource_group.removeprefix("rg-")
        )
        location = normalize_azure_location(group.get("location"))
        allowed_workload_regions = LABS_BY_ID[lab_id].regions[0].allowed_values
        if (
            not re.fullmatch(r"[a-zA-Z0-9-]{2,30}", environment)
            or location not in allowed_workload_regions
        ):
            continue
        group_apps = apps_by_group.get(resource_group_key, [])
        api_app = next(
            (
                app for app in group_apps
                if str(app.get("name") or "").casefold().startswith(
                    "ca-grubify-"
                )
                and not str(app.get("name") or "").casefold().startswith(
                    "ca-grubify-fe-"
                )
            ),
            {},
        )
        frontend_app = next(
            (
                app for app in group_apps
                if str(app.get("name") or "").casefold().startswith(
                    "ca-grubify-fe-"
                )
            ),
            {},
        )
        agent = agents_by_group.get(resource_group_key, {})
        if lab_id == "zava-learning":
            frontend_app = next(
                (
                    app for app in group_apps
                    if str(app.get("name") or "").casefold() == "learner-portal"
                ),
                {},
            )
            api_app = next(
                (
                    app for app in group_apps
                    if str(app.get("name") or "").casefold() == "assessment-api"
                ),
                {},
            )
        public_ip = next(
            (
                item for item in public_ips_by_group.get(resource_group_key, [])
                if item.get("fqdn") or item.get("ipAddress")
            ),
            {},
        )
        app_gateway_host = str(
            public_ip.get("fqdn") or public_ip.get("ipAddress") or ""
        )
        runtime_values = {
            "AZURE_RESOURCE_GROUP": resource_group,
            "CONTAINER_APP_NAME": str(api_app.get("name") or ""),
            "CONTAINER_APP_URL": (
                f"https://{api_app['fqdn']}" if api_app.get("fqdn") else ""
            ),
            "FRONTEND_APP_NAME": str(frontend_app.get("name") or ""),
            "FRONTEND_APP_URL": (
                f"https://{frontend_app['fqdn']}"
                if frontend_app.get("fqdn")
                else ""
            ),
            "SRE_AGENT_NAME": str(agent.get("name") or ""),
            "SRE_AGENT_ENDPOINT": str(agent.get("endpoint") or ""),
        }
        if lab_id == "zava-learning":
            postgres = postgres_by_group.get(resource_group_key, {})
            runtime_values.update({
                "AZURE_LOCATION": location,
                "AZURE_DB_LOCATION": normalize_azure_location(
                    postgres.get("location")
                ),
                "AZURE_AGENT_LOCATION": normalize_azure_location(
                    agent.get("location")
                ),
                "APPGW_PUBLIC_FQDN": app_gateway_host,
                "ZAVA_PORTAL_URL": (
                    f"http://{app_gateway_host}" if app_gateway_host else ""
                ),
            })
            for port in ZAVA_LANE_PORTS:
                runtime_values[f"ZAVA_LANE_{port}_URL"] = (
                    f"http://{app_gateway_host}:{port}"
                    if app_gateway_host
                    else ""
                )
        environments.append({
            "lab_id": lab_id,
            "environment": environment,
            "resource_group": resource_group,
            "location": location,
            "db_location": (
                normalize_azure_location(
                    postgres_by_group.get(resource_group_key, {}).get("location")
                )
                if lab_id == "zava-learning"
                else ""
            ),
            "agent_location": (
                normalize_azure_location(agent.get("location"))
                if lab_id == "zava-learning"
                else ""
            ),
            "detection": detection,
            "local": environment.casefold() in local_environment_names,
            "runtime_values": runtime_values,
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
    catalogs = payload.get("catalogs")
    if isinstance(catalogs, dict):
        catalog = catalogs.get(f"{subscription_id}:{lab_id}", {})
        if not isinstance(catalog, dict):
            return []
        environments = catalog.get("environments")
    else:
        if (
            payload.get("subscription_id") != subscription_id
            or payload.get("lab_id") != lab_id
        ):
            return []
        environments = payload.get("environments")
    if not isinstance(environments, list):
        return []
    return [
        item
        for item in environments
        if isinstance(item, dict)
    ]


def save_environment_cache(
    subscription_id: str,
    lab_id: str,
    environments: list[dict[str, Any]],
) -> None:
    try:
        payload: dict[str, Any] = {"catalogs": {}}
        if ENVIRONMENT_CACHE_FILE.is_file():
            try:
                existing = json.loads(
                    ENVIRONMENT_CACHE_FILE.read_text(encoding="utf-8")
                )
                if isinstance(existing.get("catalogs"), dict):
                    payload["catalogs"] = existing["catalogs"]
                elif all(
                    key in existing
                    for key in ("subscription_id", "lab_id", "environments")
                ):
                    legacy_key = (
                        f"{existing['subscription_id']}:{existing['lab_id']}"
                    )
                    payload["catalogs"][legacy_key] = {
                        "discovered_at": existing.get("discovered_at", ""),
                        "environments": existing["environments"],
                    }
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        payload["catalogs"][f"{subscription_id}:{lab_id}"] = {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "environments": environments,
        }
        ENVIRONMENT_CACHE_FILE.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError:
        LOGGER.exception("Unable to save the environment discovery cache")


def probe_http_endpoint(url: str, path: str) -> tuple[bool, str]:
    endpoint = f"{url.rstrip('/')}/{path.lstrip('/')}"
    request = Request(
        endpoint,
        method="GET",
        headers={"User-Agent": "AzureSREAgentDemo/availability-validation"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            if 200 <= response.status < 400:
                return True, ""
            return False, f"HTTP {response.status}"
    except HTTPError as error:
        return False, f"HTTP {error.code}"
    except (URLError, TimeoutError) as error:
        detail = error.reason if isinstance(error, URLError) else error
        return False, str(detail)


def validate_container_app_availability(
    apps: dict[str, Optional[dict[str, Any]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    issues = []
    checks = []
    for role, label, endpoint_path in (
        ("api", "Grubify API", "/health"),
        ("frontend", "Grubify frontend", "/"),
    ):
        app = apps.get(role)
        if app is None:
            continue
        running_status = str(app.get("runningStatus") or "").strip()
        started = running_status.casefold() in {"running", "ready"}
        latest_revision = str(app.get("latestRevisionName") or "").strip()
        ready_revision = str(app.get("latestReadyRevisionName") or "").strip()
        revision_ready = bool(
            latest_revision
            and ready_revision
            and latest_revision == ready_revision
        )
        available = False
        detail = ""
        if not started:
            detail = f"running status: {running_status or 'unknown'}"
            issues.append(
                f"{label} Container App is not started ({detail}). "
                "Start the app before reusing this lab."
            )
        elif not revision_ready:
            detail = (
                "latest revision is not ready"
                if ready_revision
                else "no ready revision"
            )
            issues.append(
                f"{label} Container App is started but its latest revision "
                "is not ready."
            )
        else:
            fqdn = str(app.get("fqdn") or "").strip()
            if fqdn:
                available, detail = probe_http_endpoint(
                    f"https://{fqdn}",
                    endpoint_path,
                )
                if not available:
                    issues.append(
                        f"{label} Container App is started but its endpoint "
                        f"{endpoint_path} is unavailable "
                        f"({detail or 'no response'})."
                    )
            else:
                detail = "no ingress endpoint"
        checks.append({
            "resource_type": "Microsoft.App/containerApps",
            "name": str(app.get("name") or ""),
            "provisioned": (
                str(app.get("provisioningState") or "").casefold()
                == "succeeded"
            ),
            "started": started,
            "available": available,
            "detail": detail,
        })
    return issues, checks


def validate_metric_alert_availability(
    alerts: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    issues = []
    checks = []
    for alert in alerts:
        enabled = alert.get("enabled")
        if enabled is False:
            issues.append(
                f"Metric alert {alert.get('name') or '<unnamed>'} is disabled. "
                "Enable it before reusing this lab."
            )
        checks.append({
            "resource_type": "Microsoft.Insights/metricAlerts",
            "name": str(alert.get("name") or ""),
            "provisioned": (
                str(alert.get("provisioningState") or "").casefold()
                == "succeeded"
            ),
            "started": None,
            "available": enabled is not False,
            "detail": "enabled" if enabled is not False else "disabled",
        })
    return issues, checks


RESOURCE_AVAILABILITY_VALIDATORS: dict[
    str,
    Callable[[Any], tuple[list[str], list[dict[str, Any]]]],
] = {
    "microsoft.app/containerapps": validate_container_app_availability,
    "microsoft.insights/metricalerts": validate_metric_alert_availability,
}


def validate_resource_availability(
    resources_by_type: dict[str, list[dict[str, Any]]],
    container_apps: dict[str, Optional[dict[str, Any]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    resources: dict[str, Any] = dict(resources_by_type)
    resources["microsoft.app/containerapps"] = container_apps
    issues = []
    checks = []
    for resource_type, validator in RESOURCE_AVAILABILITY_VALIDATORS.items():
        relevant_resources = resources.get(resource_type)
        if not relevant_resources:
            continue
        validator_issues, validator_checks = validator(relevant_resources)
        issues.extend(validator_issues)
        checks.extend(validator_checks)
    return issues, checks


def discover_existing_environments(
    subscription_id: str,
    lab_id: str,
) -> dict[str, Any]:
    lab = LABS_BY_ID[lab_id]
    local_names = local_azd_environment_names(lab)
    commands = [
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
            "--query",
            "[].{name:name,resourceGroup:resourceGroup,location:location,"
            "endpoint:properties.agentEndpoint}",
            "--output", "json",
        ],
        [
            "az", "containerapp", "list",
            "--subscription", subscription_id,
            "--query",
            "[].{name:name,resourceGroup:resourceGroup,location:location,"
            "fqdn:properties.configuration.ingress.fqdn}",
            "--output", "json",
        ],
    ]
    if lab_id == "zava-learning":
        commands.extend([
            [
                "az", "network", "public-ip", "list",
                "--subscription", subscription_id,
                "--query",
                "[].{name:name,resourceGroup:resourceGroup,"
                "fqdn:dnsSettings.fqdn,ipAddress:ipAddress}",
                "--output", "json",
            ],
            [
                "az", "postgres", "flexible-server", "list",
                "--subscription", subscription_id,
                "--query",
                "[].{name:name,resourceGroup:resourceGroup,location:location}",
                "--output", "json",
            ],
        ])
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
        records[3] if len(records) > 3 else None,
        records[4] if len(records) > 4 else None,
    )
    if lab_id == "zava-learning":
        for item in environments:
            if not item.get("local"):
                continue
            local_values = azd_values(str(item["environment"]), lab)
            version = local_values.get("ZAVA_CORE_CONFIG_VERSION", "")
            if version:
                item.setdefault("runtime_values", {})[
                    "ZAVA_CORE_CONFIG_VERSION"
                ] = version
    save_environment_cache(subscription_id, lab_id, environments)
    return {
        "environments": environments,
        "source": "azure",
        "stale": False,
        "warning": "",
    }


def validate_grubify_existing_lab(
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
            "provisioningState:provisioningState,enabled:properties.enabled}",
            "--output", "json",
        ],
        [
            "az", "containerapp", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query",
            "[].{id:id,name:name,image:properties.template.containers[0].image,"
            "fqdn:properties.configuration.ingress.fqdn,"
            "provisioningState:properties.provisioningState,"
            "runningStatus:properties.runningStatus,"
            "latestRevisionName:properties.latestRevisionName,"
            "latestReadyRevisionName:properties.latestReadyRevisionName}",
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

    availability_issues, availability_checks = validate_resource_availability(
        resources_by_type,
        {"api": api_app, "frontend": frontend_app},
    )
    issues.extend(availability_issues)

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
                "modelProvider:properties.defaultModel.provider,"
                "modelName:properties.defaultModel.name,"
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
        return {
            "ready": False,
            "issues": issues,
            "values": {},
            "availability_checks": availability_checks,
        }

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
            "availability_checks": availability_checks,
        }
    agent_endpoint = str(agent_details["endpoint"]).rstrip("/")
    data_plane_issues = []
    status, _ = http_json(
        "GET",
        f"{agent_endpoint}/api/v2/extendedAgent/agents/incident-handler",
        token.strip(),
    )
    if status != HTTPStatus.OK:
        data_plane_issues.append(
            "The incident-handler subagent is not configured."
        )
    status, response = http_json(
        "GET",
        f"{agent_endpoint}/api/v1/incidentPlayground/filters/"
        "grubify-http-errors",
        token.strip(),
    )
    if status != HTTPStatus.OK:
        data_plane_issues.append(
            "The Grubify incident response plan is not configured."
        )
    elif not response_plan_is_scoped(
        response,
        str(environment.get("environment") or ""),
    ):
        data_plane_issues.append(
            "The Grubify incident response plan is not isolated to this lab."
        )
    if data_plane_issues:
        return {
            "ready": False,
            "issues": data_plane_issues,
            "values": {},
            "availability_checks": availability_checks,
        }

    registry = resources_by_type["microsoft.containerregistry/registries"][0]
    managed_environment = resources_by_type[
        "microsoft.app/managedenvironments"
    ][0]
    workspace = resources_by_type[
        "microsoft.operationalinsights/workspaces"
    ][0]
    registry_name = str(registry.get("name") or "")
    location = normalize_azure_location(environment.get("location"))
    return {
        "ready": True,
        "issues": [],
        "availability_checks": availability_checks,
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
            "AGENT_PORTAL_URL": sre_agent_portal_url(
                subscription_id,
                resource_group,
                str(agent_details.get("name") or ""),
            ),
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


def _first_resource(
    resources_by_type: dict[str, list[dict[str, Any]]],
    resource_type: str,
) -> dict[str, Any]:
    resources = resources_by_type.get(resource_type.casefold(), [])
    return resources[0] if resources else {}


def validate_zava_existing_lab(
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
            "provisioningState:properties.provisioningState,"
            "enabled:properties.enabled,"
            "publicNetworkAccess:properties.publicNetworkAccess}",
            "--output", "json",
        ],
        [
            "az", "containerapp", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query",
            "[].{id:id,name:name,image:properties.template.containers[0].image,"
            "fqdn:properties.configuration.ingress.fqdn,"
            "provisioningState:properties.provisioningState,"
            "runningStatus:properties.runningStatus,"
            "latestRevisionName:properties.latestRevisionName,"
            "latestReadyRevisionName:properties.latestReadyRevisionName}",
            "--output", "json",
        ],
        [
            "az", "network", "public-ip", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{name:name,fqdn:dnsSettings.fqdn,ipAddress:ipAddress,"
            "provisioningState:provisioningState}",
            "--output", "json",
        ],
        [
            "az", "network", "application-gateway", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{name:name,provisioningState:provisioningState,"
            "operationalState:operationalState,listeners:length(httpListeners)}",
            "--output", "json",
        ],
        [
            "az", "vm", "list", "--show-details",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{id:id,name:name,powerState:powerState,"
            "provisioningState:provisioningState}",
            "--output", "json",
        ],
        [
            "az", "postgres", "flexible-server", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{name:name,state:state,"
            "fqdn:fullyQualifiedDomainName,location:location}",
            "--output", "json",
        ],
        [
            "az", "role", "assignment", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{principalId:principalId,role:roleDefinitionName}",
            "--output", "json",
        ],
        [
            "az", "keyvault", "list",
            "--subscription", subscription_id,
            "--resource-group", resource_group,
            "--query", "[].{name:name,"
            "publicNetworkAccess:properties.publicNetworkAccess,"
            "enablePurgeProtection:properties.enablePurgeProtection}",
            "--output", "json",
        ],
    )
    parsed_records: list[list[dict[str, Any]]] = []
    for command in commands:
        success, output = run_capture(command, timeout=60)
        records = parse_json_records(output) if success else None
        if records is None:
            return {
                "ready": False,
                "issues": ["Azure did not return Zava topology validation data."],
                "values": {},
                "availability_checks": [],
            }
        parsed_records.append(records)
    (
        resources,
        apps,
        public_ips,
        gateways,
        virtual_machines,
        postgres_servers,
        role_assignments,
        key_vaults,
    ) = parsed_records
    resources_by_type: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        resource_type = str(resource.get("type") or "").casefold()
        if resource_type:
            resources_by_type.setdefault(resource_type, []).append(resource)

    required_types = {
        "microsoft.app/agents": ("Azure SRE Agent", 1),
        "microsoft.app/managedenvironments": ("Container Apps environments", 2),
        "microsoft.containerregistry/registries": ("Azure Container Registry", 1),
        "microsoft.managedidentity/userassignedidentities": (
            "managed identities",
            2,
        ),
        "microsoft.network/virtualnetworks": ("virtual network", 1),
        "microsoft.network/networksecuritygroups": ("network security groups", 2),
        "microsoft.network/applicationgateways": ("Application Gateway", 1),
        "microsoft.network/publicipaddresses": ("public IP", 1),
        "microsoft.network/privatednszones": ("private DNS zones", 1),
        "microsoft.network/privateendpoints": ("Key Vault private endpoint", 1),
        "microsoft.keyvault/vaults": ("private Key Vault", 1),
        "microsoft.dbforpostgresql/flexibleservers": (
            "PostgreSQL Flexible Server",
            1,
        ),
        "microsoft.compute/virtualmachines": ("reporting virtual machine", 1),
        "microsoft.compute/virtualmachines/extensions": (
            "reporting VM extension",
            1,
        ),
        "microsoft.operationalinsights/workspaces": (
            "Log Analytics workspace",
            1,
        ),
        "microsoft.insights/components": ("Application Insights", 1),
        "microsoft.insights/datacollectionrules": (
            "VM data collection rule",
            1,
        ),
        "microsoft.insights/scheduledqueryrules": ("Zava symptom alerts", 4),
    }
    issues = []
    if (
        str((environment.get("runtime_values") or {}).get(
            "ZAVA_CORE_CONFIG_VERSION",
            "",
        ))
        != ZAVA_CORE_CONFIG_VERSION
    ):
        issues.append(
            "Zava knowledge/configuration provenance cannot be verified; run "
            "reconciliation before using this lab."
        )
    for resource_type, (label, minimum) in required_types.items():
        count = len(resources_by_type.get(resource_type, []))
        if count < minimum:
            issues.append(f"Missing {label} (expected at least {minimum}, found {count}).")
    for resource in resources:
        provisioning_state = str(resource.get("provisioningState") or "").strip()
        if provisioning_state and provisioning_state.casefold() != "succeeded":
            issues.append(
                f"{resource.get('name') or 'A resource'} is {provisioning_state}."
            )
    required_roles = {
        "Reader",
        "Monitoring Reader",
        "Network Contributor",
        "Container Apps Contributor",
        "Virtual Machine Contributor",
        "Managed Identity Operator",
    }
    assigned_roles = {
        str(assignment.get("role") or "")
        for assignment in role_assignments
    }
    missing_roles = sorted(required_roles - assigned_roles)
    if missing_roles:
        issues.append(
            "Zava managed-identity RBAC is missing: "
            + ", ".join(missing_roles)
            + "."
        )

    apps_by_name = {
        str(app.get("name") or "").casefold(): app
        for app in apps
        if app.get("name")
    }
    missing_apps = sorted(ZAVA_CONTAINER_APPS - apps_by_name.keys())
    if missing_apps:
        issues.append(
            "Missing Zava Container Apps: " + ", ".join(missing_apps) + "."
        )
    if len(apps) != 11:
        issues.append(f"Zava requires exactly 11 Container Apps; found {len(apps)}.")
    availability_checks: list[dict[str, Any]] = []
    for name in sorted(ZAVA_CONTAINER_APPS):
        app = apps_by_name.get(name)
        if app is None:
            continue
        provisioned = (
            str(app.get("provisioningState") or "").casefold() == "succeeded"
        )
        started = str(app.get("runningStatus") or "").casefold() in {
            "running",
            "ready",
        }
        latest_ready = bool(app.get("latestReadyRevisionName")) and (
            app.get("latestRevisionName") == app.get("latestReadyRevisionName")
        )
        available = provisioned and started and latest_ready
        availability_checks.append({
            "resource": name,
            "kind": "container-app",
            "provisioned": provisioned,
            "started": started,
            "available": available,
            "detail": "ready" if available else "not ready",
        })
        if not available:
            issues.append(f"Zava Container App {name} is not ready.")
    alert_issues, alert_checks = validate_metric_alert_availability(
        resources_by_type.get("microsoft.insights/scheduledqueryrules", [])
    )
    issues.extend(alert_issues)
    availability_checks.extend(alert_checks)
    for name, app in apps_by_name.items():
        image = str(app.get("image") or "").casefold()
        expected_image = (
            name if not name.startswith("quiz-") else "quiz-service"
        )
        if expected_image not in image:
            issues.append(f"{name} is not running the current Zava lab image.")

    if len(gateways) != 1:
        issues.append(f"Zava requires one Application Gateway; found {len(gateways)}.")
    elif (
        str(gateways[0].get("provisioningState") or "").casefold() != "succeeded"
        or str(gateways[0].get("operationalState") or "").casefold() != "running"
    ):
        issues.append("The Zava Application Gateway is not ready and running.")
    elif int(gateways[0].get("listeners") or 0) < 8:
        issues.append("The Zava Application Gateway is missing lane listeners.")
    if len(virtual_machines) != 1:
        issues.append(
            f"Zava requires one reporting VM; found {len(virtual_machines)}."
        )
    elif str(virtual_machines[0].get("powerState") or "").casefold() != "vm running":
        issues.append("The Zava reporting VM is not running.")
    elif virtual_machines[0].get("id"):
        success, output = run_capture(
            [
                "az", "rest", "--method", "GET",
                "--url",
                "https://management.azure.com"
                f"{virtual_machines[0]['id']}/providers/Microsoft.Insights/"
                "dataCollectionRuleAssociations?api-version=2022-06-01",
                "--query", "length(value)",
                "--output", "tsv",
            ],
            timeout=60,
        )
        if not success or not output.strip().isdigit() or int(output) < 1:
            issues.append("The reporting VM data collection association is missing.")
    if len(postgres_servers) != 1:
        issues.append(
            f"Zava requires one PostgreSQL server; found {len(postgres_servers)}."
        )
    elif (
        str(postgres_servers[0].get("state") or "").casefold() != "ready"
        or not postgres_servers[0].get("fqdn")
    ):
        issues.append("The Zava PostgreSQL server is not ready.")
    else:
        success, output = run_capture(
            [
                "az", "postgres", "flexible-server", "db", "list",
                "--subscription", subscription_id,
                "--resource-group", resource_group,
                "--server-name", str(postgres_servers[0]["name"]),
                "--query", "[].name",
                "--output", "json",
            ],
            timeout=60,
        )
        try:
            database_names = set(json.loads(output)) if success else set()
        except (json.JSONDecodeError, TypeError):
            database_names = set()
        if not {"zava", "zava_query"}.issubset(database_names):
            issues.append("The Zava PostgreSQL databases are incomplete.")
    if len(key_vaults) != 1:
        issues.append(f"Zava requires one private Key Vault; found {len(key_vaults)}.")
    elif (
        str(key_vaults[0].get("publicNetworkAccess") or "").casefold()
        != "disabled"
        or key_vaults[0].get("enablePurgeProtection") is not True
    ):
        issues.append(
            "The Zava Key Vault must disable public access and enable purge protection."
        )

    public_ip = next(
        (item for item in public_ips if item.get("fqdn") or item.get("ipAddress")),
        {},
    )
    app_gateway_host = str(
        public_ip.get("fqdn") or public_ip.get("ipAddress") or ""
    ).strip()
    if not app_gateway_host:
        issues.append("The Zava Application Gateway has no public address.")
    else:
        endpoints = [f"http://{app_gateway_host}"] + [
            f"http://{app_gateway_host}:{port}" for port in ZAVA_LANE_PORTS
        ]
        for index, endpoint in enumerate(endpoints):
            probe_path = "/health" if index == 0 else "/quiz/BIO-101"
            available, detail = probe_http_endpoint(endpoint, probe_path)
            availability_checks.append({
                "resource": "learner-portal" if index == 0 else f"lane-{index}",
                "kind": "application-gateway",
                "provisioned": True,
                "started": True,
                "available": available,
                "detail": detail,
            })
            if not available:
                issues.append(
                    f"Zava endpoint {endpoint}{probe_path} is unavailable: {detail}."
                )

    agent = _first_resource(resources_by_type, "microsoft.app/agents")
    agent_details: dict[str, Any] = {}
    if agent:
        success, output = run_capture(
            [
                "az", "resource", "show",
                "--ids", str(agent.get("id") or ""),
                "--api-version", "2025-05-01-preview",
                "--query",
                "{name:name,endpoint:properties.agentEndpoint,"
                "incidentType:properties.incidentManagementConfiguration.type,"
                "modelProvider:properties.defaultModel.provider,"
                "modelName:properties.defaultModel.name,"
                "provisioningState:properties.provisioningState}",
                "--output", "json",
            ],
            timeout=30,
        )
        if success:
            try:
                candidate = json.loads(output)
                if isinstance(candidate, dict):
                    agent_details = candidate
            except json.JSONDecodeError:
                pass
    if not agent_details:
        issues.append("Unable to read the Zava Azure SRE Agent status.")
    else:
        if str(agent_details.get("incidentType") or "").casefold() not in {
            "azmonitor",
            "pagerduty",
        }:
            issues.append("The Zava Azure SRE Agent incident platform is unsupported.")
        if (
            agent_details.get("modelProvider") != "Anthropic"
            or agent_details.get("modelName") != "Automatic"
        ):
            issues.append(
                "The Zava Azure SRE Agent model must be Anthropic / Automatic."
            )
        if not str(agent_details.get("endpoint") or ""):
            issues.append("The Zava Azure SRE Agent has no data-plane endpoint.")

    endpoint = str(agent_details.get("endpoint") or "").rstrip("/")
    integration_status: dict[str, str] = {
        "pagerduty": "not_configured",
        "servicenow": "not_configured",
        "github": "not_configured",
    }
    if endpoint:
        success, token = run_secret_capture(
            [
                "az", "account", "get-access-token",
                "--resource", "https://azuresre.dev",
                "--query", "accessToken",
                "--output", "tsv",
            ],
            timeout=30,
        )
        if not success or not token:
            issues.append("Unable to authenticate to the Azure SRE Agent service.")
        else:
            missing_configuration: dict[str, list[str]] = {}
            for kind, names in (
                ("agents", ZAVA_CORE_AGENTS),
                ("skills", ZAVA_CORE_SKILLS),
            ):
                missing_names = []
                for name in names:
                    status, _ = http_json(
                        "GET",
                        f"{endpoint}/api/v2/extendedAgent/{kind}/{quote(name)}",
                        token,
                    )
                    if status != HTTPStatus.OK:
                        missing_names.append(name)
                if missing_names:
                    missing_configuration[kind] = missing_names
            for kind, missing_names in missing_configuration.items():
                issues.append(
                    f"Zava agent configuration is missing {len(missing_names)} "
                    f"required {kind}: {', '.join(missing_names)}."
                )
            if str(agent_details.get("incidentType") or "").casefold() == "pagerduty":
                action_groups = resources_by_type.get(
                    "microsoft.insights/actiongroups",
                    [],
                )
                action_group = next(
                    (
                        item for item in action_groups
                        if str(item.get("name") or "").startswith(
                            "ag-zava-pagerduty-"
                        )
                    ),
                    {},
                )
                receiver_ready = False
                if action_group.get("name"):
                    receiver_ok, receiver_output = run_capture(
                        [
                            "az", "monitor", "action-group", "show",
                            "--resource-group", resource_group,
                            "--name", str(action_group["name"]),
                            "--query", "length(webhookReceivers)",
                            "--output", "tsv",
                        ],
                        timeout=60,
                    )
                    receiver_ready = (
                        receiver_ok
                        and receiver_output.strip().isdigit()
                        and int(receiver_output) > 0
                    )
                status, response = http_json(
                    "POST",
                    f"{endpoint}/api/v2/extendedAgent/connectors/"
                    "pagerduty/testconnection",
                    token,
                    {},
                )
                try:
                    connection = json.loads(response)
                except json.JSONDecodeError:
                    connection = {}
                if (
                    status not in (200, 201, 202, 204)
                    or connection.get("success") is not True
                    or not receiver_ready
                ):
                    integration_status["pagerduty"] = "reconnect_required"
                    issues.append(
                        "PagerDuty is configured but unhealthy; reconnect it in "
                        "the SRE Agent portal."
                    )
                else:
                    integration_status["pagerduty"] = "healthy"
            servicenow_presence = []
            for tool_name in (
                "CreateServiceNowChangeRequest",
                "UploadServiceNowAttachment",
            ):
                status, _ = http_json(
                    "GET",
                    f"{endpoint}/api/v2/extendedAgent/tools/{quote(tool_name)}",
                    token,
                )
                servicenow_presence.append(status == HTTPStatus.OK)
            if all(servicenow_presence):
                integration_status["servicenow"] = "present"
            elif any(servicenow_presence):
                integration_status["servicenow"] = "reconnect_required"
            github_status, _ = http_json(
                "GET",
                f"{endpoint}/api/v2/extendedAgent/connectors/github",
                token,
            )
            if github_status == HTTPStatus.OK:
                status, response = http_json(
                    "POST",
                    f"{endpoint}/api/v2/extendedAgent/connectors/"
                    "github/testconnection",
                    token,
                    {},
                )
                try:
                    github_connection = json.loads(response)
                except json.JSONDecodeError:
                    github_connection = {}
                if (
                    status in (200, 201, 202, 204)
                    and github_connection.get("success") is True
                ):
                    integration_status["github"] = "healthy"
                else:
                    integration_status["github"] = "reconnect_required"
            status, response = http_json(
                "GET",
                f"{endpoint}/api/v1/incidentPlayground/filters/"
                "zava-learning-response",
                token,
            )
            if status != HTTPStatus.OK:
                issues.append(
                    "The zava-learning-response configuration is missing."
                )
            else:
                try:
                    filter_payload = json.loads(response)
                except json.JSONDecodeError:
                    filter_payload = {}
                if (
                    filter_payload.get("titleContains") != "Zava"
                    or filter_payload.get("handlingAgent")
                    != "zava-incident-responder"
                    or filter_payload.get("agentMode") != "autonomous"
                    or filter_payload.get("isEnabled", True) is not True
                ):
                    issues.append(
                        "The zava-learning-response configuration is not autonomous "
                        "or correctly scoped."
                    )

    registry = _first_resource(
        resources_by_type,
        "microsoft.containerregistry/registries",
    )
    environments = resources_by_type.get("microsoft.app/managedenvironments", [])
    workspace = _first_resource(
        resources_by_type,
        "microsoft.operationalinsights/workspaces",
    )
    vault = _first_resource(resources_by_type, "microsoft.keyvault/vaults")
    vm = _first_resource(resources_by_type, "microsoft.compute/virtualmachines")
    postgres = _first_resource(
        resources_by_type,
        "microsoft.dbforpostgresql/flexibleservers",
    )
    workspace_customer_id = ""
    if workspace.get("id"):
        success, output = run_capture(
            [
                "az", "monitor", "log-analytics", "workspace", "show",
                "--ids", str(workspace["id"]),
                "--query", "customerId",
                "--output", "tsv",
            ],
            timeout=60,
        )
        if success:
            workspace_customer_id = output.strip()
        if not workspace_customer_id:
            issues.append("The Zava Log Analytics workspace ID is unavailable.")
    location = str(environment.get("location") or "").lower()
    values = {
        "AZURE_LOCATION": location,
        "AZURE_DB_LOCATION": normalize_azure_location(
            postgres_servers[0].get("location") if postgres_servers else ""
        ),
        "AZURE_AGENT_LOCATION": normalize_azure_location(agent.get("location")),
        "AZURE_SUBSCRIPTION_ID": subscription_id,
        "AZURE_RESOURCE_GROUP": resource_group,
        "AZURE_CONTAINER_REGISTRY_NAME": str(registry.get("name") or ""),
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": (
            f"{registry.get('name')}.azurecr.io" if registry.get("name") else ""
        ),
        "AZURE_CONTAINER_ENVIRONMENT_NAME": str(
            environments[0].get("name") if environments else ""
        ),
        "SRE_AGENT_NAME": str(agent_details.get("name") or ""),
        "SRE_AGENT_ENDPOINT": endpoint,
        "AGENT_PORTAL_URL": sre_agent_portal_url(
            subscription_id,
            resource_group,
            str(agent_details.get("name") or ""),
        ),
        "APPGW_PUBLIC_FQDN": app_gateway_host,
        "ZAVA_PORTAL_URL": (
            f"http://{app_gateway_host}" if app_gateway_host else ""
        ),
        "KEY_VAULT_NAME": str(vault.get("name") or ""),
        "REPORTING_VM_NAME": str(vm.get("name") or ""),
        "POSTGRES_SERVER_NAME": str(postgres.get("name") or ""),
        "LOG_ANALYTICS_WORKSPACE_ID": workspace_customer_id,
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": str(workspace.get("id") or ""),
    }
    if all(values.get(name) for name in (
        "KEY_VAULT_NAME",
        "REPORTING_VM_NAME",
        "AZURE_RESOURCE_GROUP",
    )):
        try:
            _recovered, missing_secrets = rehydrate_zava_secrets(
                str(environment.get("environment") or ""),
                values,
            )
        except ValueError:
            missing_secrets = list(ZAVA_REQUIRED_SECRET_NAMES)
        if "db-password" in missing_secrets or "db-pool-password" in missing_secrets:
            issues.append("Required Zava database secrets are unavailable.")
        if "vm-admin-password" in missing_secrets:
            issues.append(
                "The legacy lab has no VM credential secret; reconciliation will "
                "rotate and store one."
            )
    else:
        issues.append("Zava private Key Vault bridge resources are incomplete.")

    return {
        "ready": not issues,
        "issues": issues,
        "availability_checks": availability_checks,
        "values": values,
        "integration_status": integration_status,
    }


def validate_existing_lab(
    subscription_id: str,
    environment: dict[str, Any],
    lab: Optional[LabDefinition] = None,
) -> dict[str, Any]:
    active_lab = lab
    if active_lab is None:
        active_lab = LABS_BY_ID.get(str(environment.get("lab_id") or ""))
    if active_lab and active_lab.id == "zava-learning":
        return validate_zava_existing_lab(subscription_id, environment)
    return validate_grubify_existing_lab(subscription_id, environment)


def refresh_process_path() -> None:
    if os.name != "nt":
        return

    managed_paths = []
    managed_azure_cli_bin = AZURE_CLI_DIR / "bin"
    if (managed_azure_cli_bin / "az.cmd").is_file():
        managed_paths.append(str(managed_azure_cli_bin))
    if (AZD_DIR / "azd.exe").is_file():
        managed_paths.append(str(AZD_DIR))
    if (POWERSHELL_DIR / "pwsh.exe").is_file():
        managed_paths.append(str(POWERSHELL_DIR))

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


def download_with_windows_certificate_store(url: str, destination: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if os.name != "nt" or powershell is None:
        raise OSError("Windows certificate-store download is unavailable.")

    environment = os.environ.copy()
    environment["AZURE_SRE_DOWNLOAD_URL"] = url
    environment["AZURE_SRE_DOWNLOAD_PATH"] = str(destination)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$ProgressPreference = 'SilentlyContinue'; "
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.SecurityProtocolType]::Tls12; "
        "Invoke-WebRequest -UseBasicParsing "
        "-Uri $env:AZURE_SRE_DOWNLOAD_URL "
        "-OutFile $env:AZURE_SRE_DOWNLOAD_PATH"
    )
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=240,
        creationflags=CREATE_NO_WINDOW,
        env=environment,
    )
    if result.returncode != 0 or not destination.is_file():
        detail = redact_text(result.stderr or result.stdout).strip()
        raise OSError(
            "Windows certificate-store download failed"
            + (f": {detail}" if detail else ".")
        )


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
    downloaded = 0
    last_reported_megabytes = 0
    request = Request(
        url,
        headers={"User-Agent": "AzureSREAgentDemo/1.0"},
    )
    try:
        try:
            with urlopen(request, timeout=180) as response, archive_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    downloaded += len(chunk)
                    downloaded_megabytes = downloaded // (10 * 1024 * 1024) * 10
                    if downloaded_megabytes > last_reported_megabytes:
                        last_reported_megabytes = downloaded_megabytes
                        job.emit(
                            "output",
                            line=f"Downloaded {downloaded_megabytes} MB...",
                        )
        except URLError as error:
            if not isinstance(error.reason, ssl.SSLCertVerificationError):
                raise
            archive_path.unlink(missing_ok=True)
            job.emit(
                "output",
                line=(
                    "The bundled downloader could not validate the server "
                    "certificate. Retrying with the Windows trusted "
                    "certificate store..."
                ),
            )
            download_with_windows_certificate_store(url, archive_path)

        actual_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
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


def install_managed_powershell(job: Job) -> bool:
    return install_managed_zip_tool(
        job,
        display_name="PowerShell",
        slug="powershell",
        version=POWERSHELL_VERSION,
        url=POWERSHELL_URL,
        expected_sha256=POWERSHELL_SHA256,
        install_dir=POWERSHELL_DIR,
        archive_executable=Path("pwsh.exe"),
        installed_executable=Path("pwsh.exe"),
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
    elif tool_id == "pwsh":
        success = install_managed_powershell(job)
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
    environment_overrides: Optional[dict[str, str]] = None,
    no_log_output: bool = False,
) -> tuple[bool, str]:
    if emit_command:
        job.emit("command", command=command)
    process_command = resolved_process_command(command)
    if process_command is None:
        LOGGER.error("job=%s command not found: %s", job.id, command[0])
        job.emit("error", message=f"Command not found: {command[0]}")
        return False, ""
    needs_environment = bool(environment_overrides) or command[:2] == ["az", "login"]
    environment = os.environ.copy() if needs_environment else None
    if command[:2] == ["az", "login"]:
        # Disable WAM so explicit device-code login can use any organizational account.
        assert environment is not None
        environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"
    if environment_overrides:
        assert environment is not None
        environment.update(environment_overrides)
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
            safe_line = sanitize_terminal_output(redact_text(line))
            for sensitive_value in (environment_overrides or {}).values():
                if sensitive_value:
                    safe_line = safe_line.replace(
                        sensitive_value,
                        "<redacted-environment-value>",
                    )
            if not safe_line or is_transient_cli_spinner(safe_line):
                continue
            if not no_log_output:
                captured.append(safe_line)
                LOGGER.debug(
                    "job=%s pid=%s output=%s",
                    job.id,
                    process.pid,
                    safe_line,
                )
            if line_interceptor and line_interceptor(line):
                LOGGER.info(
                    "job=%s pid=%s output interceptor requested termination",
                    job.id,
                    process.pid,
                )
                job.terminate_process()
                break
            if no_log_output:
                continue
            job.emit("output", line=safe_line)
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
    return {
        str(link["url"])
        for link in runtime_summary_links(state, values)
        if link.get("url")
    }


def runtime_summary_links(
    state: dict[str, Any],
    values: dict[str, str],
) -> list[dict[str, str]]:
    def display(links: list[dict[str, str]]) -> list[dict[str, str]]:
        return [
            {**link, "value": link["url"]}
            for link in links
            if link.get("url")
        ]

    resource_group = values.get("AZURE_RESOURCE_GROUP", "")
    lab = selected_lab(state)
    common = [
        {
            "id": "resource-group",
            "label": "Azure resource group",
            "url": azure_resource_group_portal_url(
                str(state.get("tenant_id") or ""),
                str(state.get("subscription_id") or ""),
                resource_group,
            ),
        },
        {
            "id": "sre-agent",
            "label": "SRE Agent portal",
            "url": resolved_sre_agent_portal_url(state, values),
        },
    ]
    if lab and lab.id == "zava-learning":
        host = values.get("APPGW_PUBLIC_FQDN", "").strip()
        if not re.fullmatch(
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|"
            r"(?:\d{1,3}\.){3}\d{1,3})",
            host,
        ):
            host = ""
        portal = f"http://{host}" if host else ""
        links = [
            {"id": "portal", "label": "Zava learning portal", "url": portal},
        ]
        for port in ZAVA_LANE_PORTS:
            links.append({
                "id": f"lane-{port}",
                "label": f"Scenario lane {port}",
                "url": (
                    f"http://{host}:{port}" if host else ""
                ),
            })
        return display([*links, *common])
    return display([
            {
                "id": "api",
                "label": "Grubify API",
                "url": values.get("CONTAINER_APP_URL", ""),
            },
            {
                "id": "frontend",
                "label": "Grubify application",
                "url": values.get("FRONTEND_APP_URL", ""),
            },
            *common,
        ])


def is_allowed_demo_external_url(
    url: str,
    state: Optional[dict[str, Any]] = None,
    values: Optional[dict[str, str]] = None,
) -> bool:
    current_state = state if state is not None else load_state()
    resolved_values = values
    if resolved_values is None:
        environment = str(current_state.get("environment") or "")
        resolved_values = (
            azd_values(environment, selected_lab(current_state))
            if environment
            else {}
        )
    if url not in demo_external_urls(current_state, resolved_values):
        return False
    scheme = urlparse(url).scheme
    lab = selected_lab(current_state)
    return scheme == "https" or (
        scheme == "http"
        and lab is not None
        and lab.id == "zava-learning"
        and url in {
            str(link["url"])
            for link in runtime_summary_links(
                current_state,
                resolved_values,
            )
            if str(link.get("id", "")).startswith(("portal", "lane-"))
        }
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
    forbidden = re.compile(
        r"(?:password|secret|token|api[_-]?key|webhook[_-]?url|"
        r"authorization|credential|connection[_-]?string|private[_-]?key)$",
        re.IGNORECASE,
    )

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if not forbidden.search(str(key))
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    STATE_FILE.write_text(json.dumps(sanitize(state), indent=2), encoding="utf-8")


def run_capture(
    command: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 60,
    environment_overrides: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    process_command = resolved_process_command(command)
    if process_command is None:
        LOGGER.error("Capture command not found: %s", command[0])
        return False, f"Command not found: {command[0]}"
    needs_environment = bool(environment_overrides) or command[0].lower() == "az"
    environment = os.environ.copy() if needs_environment else None
    if command[0].lower() == "az":
        assert environment is not None
        environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"
    if environment_overrides:
        assert environment is not None
        environment.update(environment_overrides)
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


def run_secret_capture(
    command: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 120,
    environment_overrides: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """Capture a secret-bearing operation without logging its command or output."""
    process_command = resolved_process_command(command)
    if process_command is None:
        return False, ""
    environment = os.environ.copy()
    if environment_overrides:
        environment.update(environment_overrides)
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
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return result.returncode == 0, (result.stdout or "").strip()


def _zava_safe_resource_name(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,90}", value):
        raise ValueError(f"Invalid {label}.")
    return value


def zava_vm_secret_bridge(
    resource_group: str,
    vm_name: str,
    vault_name: str,
    secret_name: str,
    value: Optional[str] = None,
) -> tuple[bool, str]:
    """Read or write one allowlisted private-vault secret without diagnostics."""
    if secret_name not in ZAVA_SECRET_NAMES:
        raise ValueError("Secret name is not allowlisted.")
    resource_group = _zava_safe_resource_name(resource_group, "resource group")
    vm_name = _zava_safe_resource_name(vm_name, "virtual machine name")
    vault_name = _zava_safe_resource_name(vault_name, "Key Vault name")
    vault_uri = f"https://{vault_name}.vault.azure.net/"
    metadata = (
        "http://169.254.169.254/metadata/identity/oauth2/token"
        "?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net"
    )
    authorization_header = "Author" + "ization: " + "Bear" + "er $token"
    if value is None:
        script = (
            "set -eu; "
            f"token=$(curl -fsS -H Metadata:true '{metadata}' | "
            "python3 -c 'import json,sys;print(json.load(sys.stdin)"
            "[\"access_token\"])'); "
            f"curl -fsS -H \"{authorization_header}\" "
            f"'{vault_uri}secrets/{secret_name}?api-version=7.4' | "
            "python3 -c 'import base64,json,sys;"
            "print(base64.b64encode(json.load(sys.stdin)[\"value\"].encode())"
            ".decode())'"
        )
    else:
        encoded = __import__("base64").b64encode(value.encode("utf-8")).decode("ascii")
        script = (
            "set -eu; "
            f"token=$(curl -fsS -H Metadata:true '{metadata}' | "
            "python3 -c 'import json,sys;print(json.load(sys.stdin)"
            "[\"access_token\"])'); "
            f"payload=$(ZAVA_SECRET_B64='{encoded}' python3 -c "
            "'import base64,json,os;print(json.dumps({\"value\":"
            "base64.b64decode(os.environ[\"ZAVA_SECRET_B64\"]).decode()}))'); "
            f"curl -fsS -X PUT -H \"{authorization_header}\" "
            "-H 'Content-Type: application/json' --data \"$payload\" "
            f"'{vault_uri}secrets/{secret_name}?api-version=7.4' >/dev/null; "
            "echo ZAVA_SECRET_STORED"
        )
    success, output = run_secret_capture(
        [
            "az", "vm", "run-command", "invoke",
            "--resource-group", resource_group,
            "--name", vm_name,
            "--command-id", "RunShellScript",
            "--scripts", script,
            "--query", "value[0].message",
            "--output", "tsv",
            "--only-show-errors",
        ],
        timeout=300,
    )
    if not success:
        return False, ""
    if value is not None:
        return "ZAVA_SECRET_STORED" in output, ""
    candidates = [
        line.strip()
        for line in output.splitlines()
        if re.fullmatch(r"[A-Za-z0-9+/=]{8,}", line.strip())
    ]
    if not candidates:
        return False, ""
    try:
        decoded = __import__("base64").b64decode(
            candidates[-1],
            validate=True,
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, ""
    return bool(decoded), decoded


def zava_secret_resource_names(
    values: dict[str, str],
) -> tuple[str, str, str]:
    resource_group = values.get("AZURE_RESOURCE_GROUP", "")
    vm_name = values.get("REPORTING_VM_NAME", "")
    vault_name = values.get("KEY_VAULT_NAME", "")
    if not all((resource_group, vm_name, vault_name)):
        raise ValueError(
            "Zava private-secret bridge metadata is incomplete; reconcile the lab."
        )
    return resource_group, vm_name, vault_name


def rehydrate_zava_secrets(
    environment: str,
    values: dict[str, str],
    names: tuple[str, ...] = ZAVA_REQUIRED_SECRET_NAMES,
) -> tuple[dict[str, str], list[str]]:
    resource_group, vm_name, vault_name = zava_secret_resource_names(values)
    key_map = {
        "db-password": "POSTGRES_ADMIN_PASSWORD",
        "db-pool-password": "POSTGRES_POOL_PASSWORD",
        "vm-admin-password": "VM_ADMIN_PASSWORD",
    }
    recovered: dict[str, str] = {}
    missing = []
    for name in names:
        if name not in ZAVA_SECRET_NAMES:
            raise ValueError("Secret name is not allowlisted.")
        success, value = zava_vm_secret_bridge(
            resource_group,
            vm_name,
            vault_name,
            name,
        )
        if success:
            recovered[key_map[name]] = value
        else:
            missing.append(name)
    if recovered:
        update_in_memory_secrets("zava-learning", environment, recovered)
    return recovered, missing


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


def azd_values(
    environment: str,
    lab: Optional[LabDefinition] = None,
) -> dict[str, str]:
    active_lab = lab or selected_lab()
    vendor_dir = vendor_dir_for_lab(active_lab) if active_lab else VENDOR_DIR
    success, output = run_capture(
        ["azd", "env", "get-values", "-e", environment],
        vendor_dir,
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
    lab: Optional[LabDefinition] = None,
) -> tuple[bool, str]:
    active_lab = lab or selected_lab()
    vendor_dir = vendor_dir_for_lab(active_lab) if active_lab else VENDOR_DIR
    for key, value in values.items():
        success, output = run_capture(
            ["azd", "env", "set", "-e", environment, key, value],
            vendor_dir,
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


def secret_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, str]:
    """Perform a credential-bearing request without logging request or response data."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=90) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except (OSError, URLError):
        return 0, ""


def upload_knowledge_base(
    endpoint: str,
    token: str,
    knowledge_directory: Optional[Path] = None,
) -> tuple[int, str]:
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
    source_directory = knowledge_directory or (VENDOR_DIR / "knowledge-base")
    for path in sorted(source_directory.glob("*.md")):
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


def parse_zava_skill_manifest(path: Path, expected_name: str) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Skill {expected_name} has no YAML frontmatter.")
    lines = match.group(1).splitlines()

    def scalar(key: str) -> str:
        line = next((item for item in lines if item.startswith(f"{key}:")), "")
        return line.split(":", 1)[1].strip().strip("'\"") if line else ""

    name = scalar("name")
    description = scalar("description")
    if name != expected_name or not description:
        raise ValueError(f"Skill manifest identity is invalid: {expected_name}.")
    tools = []
    in_tools = False
    for line in lines:
        if line == "tools:":
            in_tools = True
            continue
        if in_tools:
            item = re.fullmatch(r"\s{2}-\s+([A-Za-z0-9_.-]+)", line)
            if item:
                tool = item.group(1)
                if not tool.startswith("microsoft-learn_"):
                    tools.append(tool)
            elif line.strip():
                break
    return {
        "name": name,
        "description": description,
        "tools": tools,
        "skillContent": raw,
        "additionalFiles": [],
    }


def _zava_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if text in {"true", "false"}:
        return text == "true"
    if text in {"null", "~"}:
        return None
    if text == "[]":
        return []
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return float(text) if "." in text else int(text)
    return text.strip("'\"")


def parse_zava_python_tool_manifest(
    path: Path,
    expected_name: str,
    substitutions: dict[str, str],
) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    for placeholder, value in substitutions.items():
        quoted_placeholder = f'"@@{placeholder}@@"'
        if quoted_placeholder in raw:
            raw = raw.replace(quoted_placeholder, json.dumps(value))
        else:
            raw = raw.replace(f"@@{placeholder}@@", value)
    lines = raw.splitlines()
    name_line = next(
        (line for line in lines if re.fullmatch(r"\s{2}name:\s*.+", line)),
        "",
    )
    name = name_line.split(":", 1)[1].strip().strip("'\"") if name_line else ""
    type_line = next(
        (line for line in lines if re.fullmatch(r"\s{2}type:\s*.+", line)),
        "",
    )
    tool_type = type_line.split(":", 1)[1].strip().strip("'\"") if type_line else ""
    if name != expected_name or tool_type != "PythonFunctionTool":
        raise ValueError(f"Tool manifest identity is invalid: {expected_name}.")

    def block(name: str) -> str:
        marker = next(
            (
                index
                for index, line in enumerate(lines)
                if re.fullmatch(rf"\s{{2}}{re.escape(name)}:\s*\|[-+]?", line)
            ),
            -1,
        )
        if marker < 0:
            raise ValueError(f"Tool {expected_name} has no {name}.")
        content = []
        for line in lines[marker + 1:]:
            if line.strip() and not line.startswith("    "):
                break
            content.append(line[4:] if line.startswith("    ") else "")
        return "\n".join(content)

    timeout_line = next(
        (line for line in lines if re.fullmatch(r"\s{2}timeoutSeconds:\s*\d+", line)),
        "",
    )
    if not timeout_line:
        raise ValueError(f"Tool {expected_name} has no valid timeout.")
    parameters: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    in_parameters = False
    for line in lines:
        if line == "  parameters:":
            in_parameters = True
            continue
        if not in_parameters:
            continue
        start = re.fullmatch(r"\s{2}- name:\s*(.+)", line)
        if start:
            if current:
                parameters.append(current)
            current = {"name": _zava_yaml_scalar(start.group(1))}
            continue
        field = re.fullmatch(
            r"\s{4}(type|description|required):\s*(.*)",
            line,
        )
        if field and current is not None:
            current[field.group(1)] = _zava_yaml_scalar(field.group(2))
    if current:
        parameters.append(current)
    properties = {
        "type": "PythonFunctionTool",
        "description": block("description"),
        "functionCode": block("functionCode"),
        "timeoutSeconds": int(timeout_line.split(":", 1)[1].strip()),
        "parameters": parameters,
        "authEnabled": False,
        "authScopes": [],
    }
    if "@@SERVICENOW_" in json.dumps(properties):
        raise ValueError(f"Tool {expected_name} contains unresolved settings.")
    return properties


def parse_zava_agent_manifest(
    path: Path,
    expected_name: str,
    substitutions: dict[str, str],
    enabled_optional_skills: tuple[str, ...] = (),
) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    for placeholder, value in substitutions.items():
        raw = raw.replace(f"@@{placeholder}@@", value)
    name_match = re.search(r"^metadata:\s*\n\s{2}name:\s*(\S+)\s*$", raw, re.MULTILINE)
    kind_match = re.search(r"^kind:\s*(\S+)\s*$", raw, re.MULTILINE)
    if (
        not name_match
        or name_match.group(1).strip("'\"") != expected_name
        or not kind_match
        or kind_match.group(1) != "ExtendedAgent"
    ):
        raise ValueError(f"Agent manifest identity is invalid: {expected_name}.")
    lines = raw.splitlines()
    try:
        spec_index = lines.index("spec:")
    except ValueError as error:
        raise ValueError(f"Agent {expected_name} has no spec.") from error
    properties: dict[str, Any] = {}
    index = spec_index + 1
    while index < len(lines):
        field = re.fullmatch(r"\s{2}([A-Za-z][A-Za-z0-9]*):\s*(.*)", lines[index])
        if not field:
            index += 1
            continue
        key, raw_value = field.groups()
        if raw_value in {"|-", "|", "|+"}:
            block = []
            index += 1
            while index < len(lines):
                line = lines[index]
                if line.strip() and not line.startswith("    "):
                    break
                block.append(line[4:] if line.startswith("    ") else "")
                index += 1
            properties[key] = "\n".join(block)
            continue
        if raw_value:
            properties[key] = _zava_yaml_scalar(raw_value)
            index += 1
            continue
        items = []
        index += 1
        while index < len(lines):
            item = re.fullmatch(r"\s{2}-\s+(.+)", lines[index])
            if not item:
                break
            items.append(_zava_yaml_scalar(item.group(1)))
            index += 1
        properties[key] = items
    if not isinstance(properties.get("instructions"), str):
        raise ValueError(f"Agent {expected_name} has no instructions.")
    if expected_name == "zava-incident-responder":
        optional_text = (
            " Enabled optional workflows: "
            + ", ".join(enabled_optional_skills)
            + "."
            if enabled_optional_skills
            else ""
        )
        properties["instructions"] = (
            "You are the autonomous Zava Learning Azure Monitor incident responder "
            f"for resource group {substitutions['RG']}. Diagnose from Azure Monitor, "
            "Application Insights, Log Analytics, and live Azure configuration. "
            "Apply the smallest safe recovery, verify the affected public lane is "
            "healthy, produce evidence-backed root cause and recommendations, and "
            "never read, display, or log secrets."
            + optional_text
        )
        properties["mcpTools"] = (
            list(ZAVA_PAGERDUTY_TOOLS)
            if "pagerduty-incident-update" in enabled_optional_skills
            else []
        )
        if isinstance(properties.get("allowedSkills"), list):
            properties["allowedSkills"] = [
                skill
                for skill in properties["allowedSkills"]
                if skill in (*ZAVA_CORE_SKILLS, *enabled_optional_skills)
            ]
    if "@@" in json.dumps(properties):
        raise ValueError(f"Agent {expected_name} contains unresolved placeholders.")
    return properties


def configure_zava_agent_core(
    job: Job,
    environment: str,
    values: dict[str, str],
    preserve_incident_configuration: bool = False,
    enabled_optional_skills: tuple[str, ...] = (),
) -> bool:
    required = (
        "SRE_AGENT_ENDPOINT",
        "SRE_AGENT_NAME",
        "AZURE_RESOURCE_GROUP",
        "AZURE_SUBSCRIPTION_ID",
        "LOG_ANALYTICS_WORKSPACE_NAME",
        "LOG_ANALYTICS_WORKSPACE_ID",
        "APPLICATIONINSIGHTS_NAME",
        "APPLICATIONINSIGHTS_APP_ID",
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_CONTAINER_ENVIRONMENT_NAME",
    )
    missing = [name for name in required if not values.get(name)]
    if missing:
        job.emit("error", message=f"Missing Zava agent outputs: {', '.join(missing)}.")
        return False
    success, token = run_secret_capture(
        [
            "az", "account", "get-access-token",
            "--resource", "https://azuresre.dev",
            "--query", "accessToken",
            "--output", "tsv",
        ],
        timeout=30,
    )
    if not success or not token:
        job.emit("error", message="Unable to authenticate to the SRE Agent data plane.")
        return False
    endpoint = values["SRE_AGENT_ENDPOINT"].rstrip("/")
    vendor_dir = vendor_dir_for_lab(LABS_BY_ID["zava-learning"])
    config_root = vendor_dir / "sre-config"
    substitutions = {
        "RG": values["AZURE_RESOURCE_GROUP"],
        "REPO": "",
        "LAW_NAME": values.get("LOG_ANALYTICS_WORKSPACE_NAME", ""),
        "LAW_GUID": values.get("LOG_ANALYTICS_WORKSPACE_ID", ""),
        "APPINSIGHTS_NAME": values.get("APPLICATIONINSIGHTS_NAME", ""),
        "APPINSIGHTS_APPID": values.get("APPLICATIONINSIGHTS_APP_ID", ""),
        "ACR_NAME": values.get("AZURE_CONTAINER_REGISTRY_NAME", ""),
        "CAE_NAME": values.get("AZURE_CONTAINER_ENVIRONMENT_NAME", ""),
    }
    try:
        skills = [
            (
                name,
                parse_zava_skill_manifest(
                    config_root / "agent-config" / "skills" / name / "SKILL.md",
                    name,
                ),
            )
            for name in (*ZAVA_CORE_SKILLS, *enabled_optional_skills)
        ]
        agents = [
            (
                name,
                parse_zava_agent_manifest(
                    config_root / "agent-config" / "agents" / name / f"{name}.yaml",
                    name,
                    substitutions,
                    enabled_optional_skills,
                ),
            )
            for name in ZAVA_CORE_AGENTS
        ]
    except (OSError, ValueError) as error:
        job.emit("error", message=f"Invalid controlled Zava manifest: {error}")
        return False
    for kind, entities, entity_type in (
        ("skills", skills, "Skill"),
        ("agents", agents, "ExtendedAgent"),
    ):
        for name, properties in entities:
            job.emit("step", name=f"Applying Zava {kind[:-1]} {name}")
            status, response = http_json(
                "PUT",
                f"{endpoint}/api/v2/extendedAgent/{kind}/{quote(name)}",
                token,
                {
                    "name": name,
                    "type": entity_type,
                    "tags": [],
                    "properties": properties,
                },
            )
            if status not in (200, 201, 202, 204):
                job.emit(
                    "error",
                    message=f"Unable to apply required Zava {kind[:-1]} {name} "
                    f"(HTTP {status}).",
                )
                return False

    knowledge_batches = (
        (
            config_root / "knowledge-base",
            ("zava-learning-architecture.md",),
        ),
        (
            config_root / "templates",
            (
                "zava-audit-report.md",
                "zava-brand.md",
                "zava-redaction.md",
                "zava-report-template.md",
            ),
        ),
    )
    for directory, expected_files in knowledge_batches:
        missing_files = [
            name for name in expected_files if not (directory / name).is_file()
        ]
        if missing_files:
            job.emit(
                "error",
                message="Required Zava knowledge is missing: "
                + ", ".join(missing_files)
                + ".",
            )
            return False
        job.emit("step", name=f"Uploading Zava knowledge from {directory.name}")
        status, _ = upload_knowledge_base(endpoint, token, directory)
        if status not in (200, 201, 202, 204):
            job.emit(
                "error",
                message=f"Unable to upload required Zava knowledge (HTTP {status}).",
            )
            return False

    response_plan = {
        "incidentPlatform": "AzMonitor",
        "titleContains": "Zava",
        "handlingAgent": "zava-incident-responder",
        "agentMode": "autonomous",
        "maxAutomatedInvestigationAttempts": 3,
        "isEnabled": True,
    }
    success, management_token = run_secret_capture(
        [
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com/",
            "--query", "accessToken",
            "--output", "tsv",
        ],
        timeout=30,
    )
    if not success or not management_token:
        job.emit("error", message="Unable to authenticate for Zava agent configuration.")
        return False
    agent_url = (
        "https://management.azure.com/subscriptions/"
        f"{quote(values['AZURE_SUBSCRIPTION_ID'])}/resourceGroups/"
        f"{quote(values['AZURE_RESOURCE_GROUP'])}/providers/Microsoft.App/agents/"
        f"{quote(values['SRE_AGENT_NAME'])}"
    )
    if not preserve_incident_configuration:
        status, _ = http_json(
            "PATCH",
            f"{agent_url}?api-version=2025-05-01-preview",
            management_token,
            {
                "properties": {
                    "incidentManagementConfiguration": {
                        "type": "AzMonitor",
                        "connectionName": "azmonitor",
                    }
                }
            },
        )
        if status not in (200, 201, 202, 204):
            job.emit(
                "error",
                message=f"Unable to enforce Azure Monitor core mode (HTTP {status}).",
            )
            return False
        encoded_filter = __import__("base64").b64encode(
            json.dumps(response_plan, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        job.emit("step", name="Applying zava-learning-response")
        status, _ = http_json(
            "PUT",
            f"{agent_url}/incidentFilters/zava-learning-response"
            "?api-version=2025-05-01-preview",
            management_token,
            {"properties": {"value": encoded_filter}},
        )
        if status not in (200, 201, 202, 204):
            job.emit(
                "error",
                message=f"Unable to apply zava-learning-response (HTTP {status}).",
            )
            return False
    saved, error = set_azd_values(
        environment,
        {"ZAVA_CORE_CONFIG_VERSION": ZAVA_CORE_CONFIG_VERSION},
        LABS_BY_ID["zava-learning"],
    )
    if not saved:
        job.emit("error", message=error or "Unable to record Zava core configuration.")
        return False
    return True


def configure_zava_optional_integrations(
    job: Job,
    environment: str,
    values: dict[str, str],
    settings: dict[str, str],
) -> tuple[bool, dict[str, str]]:
    requested = {
        "pagerduty": bool(settings.get("pagerduty_api_token")),
        "servicenow": bool(settings.get("servicenow_url")),
    }
    if not any(requested.values()):
        return True, {}
    success, data_token = run_secret_capture(
        [
            "az", "account", "get-access-token",
            "--resource", "https://azuresre.dev",
            "--query", "accessToken",
            "--output", "tsv",
        ],
        timeout=30,
    )
    if not success or not data_token:
        job.emit("error", message="Unable to authenticate for optional SRE Agent setup.")
        return False, {}
    success, management_token = run_secret_capture(
        [
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com/",
            "--query", "accessToken",
            "--output", "tsv",
        ],
        timeout=30,
    )
    if not success or not management_token:
        job.emit("error", message="Unable to authenticate for optional agent configuration.")
        return False, {}

    endpoint = values["SRE_AGENT_ENDPOINT"].rstrip("/")
    agent_url = (
        "https://management.azure.com/subscriptions/"
        f"{quote(values['AZURE_SUBSCRIPTION_ID'])}/resourceGroups/"
        f"{quote(values['AZURE_RESOURCE_GROUP'])}/providers/Microsoft.App/agents/"
        f"{quote(values['SRE_AGENT_NAME'])}"
    )
    vendor_dir = vendor_dir_for_lab(LABS_BY_ID["zava-learning"])
    config_root = vendor_dir / "sre-config"
    enabled_skills: list[str] = []
    status_by_integration: dict[str, str] = {}

    if requested["pagerduty"]:
        connector_properties = {
            "dataConnectorType": "Mcp",
            "dataSource": "https://mcp.pagerduty.com",
            "identity": "",
            "endpoint": "https://mcp.pagerduty.com/mcp",
            "source": "Agent",
            "extendedProperties": {
                "type": "http",
                "endpoint": "https://mcp.pagerduty.com/mcp",
                "authType": "CustomHeaders",
                "Authorization": (
                    "Token token=" + settings["pagerduty_api_token"]
                ),
                "selectedTools": list(ZAVA_PAGERDUTY_TOOLS),
                "toolsVisibleToMetaAgent": list(ZAVA_PAGERDUTY_TOOLS),
            },
        }
        connector_body = {
            "name": "pagerduty",
            "type": "AgentConnector",
            "tags": [],
            "properties": connector_properties,
        }
        job.emit("step", name="Configuring and testing PagerDuty")
        code, _ = http_json(
            "PUT",
            f"{endpoint}/api/v2/extendedAgent/connectors/pagerduty",
            data_token,
            connector_body,
        )
        if code not in (200, 201, 202, 204):
            job.emit("error", message=f"PagerDuty connector setup failed (HTTP {code}).")
            return False, status_by_integration
        code, response = http_json(
            "POST",
            f"{endpoint}/api/v2/extendedAgent/connectors/pagerduty/testconnection",
            data_token,
            connector_body,
        )
        try:
            connector_test = json.loads(response)
        except json.JSONDecodeError:
            connector_test = {}
        if code not in (200, 201, 202, 204) or connector_test.get("success") is not True:
            job.emit("error", message=f"PagerDuty connection test failed (HTTP {code}).")
            return False, status_by_integration
        action_group_name = values.get("ZAVA_PAGERDUTY_ACTION_GROUP_NAME", "")
        if not action_group_name:
            job.emit(
                "error",
                message="PagerDuty Azure Monitor delivery metadata is unavailable.",
            )
            return False, status_by_integration
        code_ok, receiver_count = run_capture(
            [
                "az", "monitor", "action-group", "show",
                "--resource-group", values["AZURE_RESOURCE_GROUP"],
                "--name", action_group_name,
                "--query", "length(webhookReceivers)",
                "--output", "tsv",
            ],
            timeout=60,
        )
        if not code_ok or not receiver_count.strip().isdigit() or int(receiver_count) < 1:
            job.emit(
                "error",
                message="PagerDuty Azure Monitor delivery is not configured.",
            )
            return False, status_by_integration
        code, _ = http_json(
            "PATCH",
            f"{agent_url}?api-version=2025-05-01-preview",
            management_token,
            {
                "properties": {
                    "incidentManagementConfiguration": {
                        "type": "PagerDuty",
                        "connectionName": "pagerduty",
                        "connectionKey": settings["pagerduty_api_token"],
                        "oboUser": settings["pagerduty_obo_email"],
                    }
                }
            },
        )
        if code not in (200, 201, 202, 204):
            job.emit("error", message=f"PagerDuty platform setup failed (HTTP {code}).")
            return False, status_by_integration
        platform_ready = False
        for attempt in range(30):
            code, response = http_json(
                "GET",
                f"{endpoint}/api/v1/incidentplayground/incidentPlatformType",
                data_token,
            )
            try:
                platform = json.loads(response)
            except json.JSONDecodeError:
                platform = {}
            if code == HTTPStatus.OK and platform.get("incidentPlatformType") == "PagerDuty":
                platform_ready = True
                break
            if attempt < 29:
                time.sleep(10)
        if not platform_ready:
            job.emit("error", message="PagerDuty platform did not become ready.")
            return False, status_by_integration
        enabled_skills.append(ZAVA_OPTIONAL_SKILLS["pagerduty"])
        status_by_integration["pagerduty"] = "healthy"

    if requested["servicenow"]:
        auth = __import__("base64").b64encode(
            (
                settings["servicenow_user"]
                + ":"
                + settings["servicenow_password"]
            ).encode("utf-8")
        ).decode("ascii")
        test_url = (
            settings["servicenow_url"].rstrip("/")
            + "/api/now/table/sys_user?sysparm_limit=1&sysparm_fields=sys_id"
        )
        job.emit("step", name="Testing and configuring ServiceNow tools")
        code, _ = secret_http_request(
            "GET",
            test_url,
            {
                "Authorization": "Basic " + auth,
                "Accept": "application/json",
            },
        )
        if code != HTTPStatus.OK:
            job.emit("error", message=f"ServiceNow authentication failed (HTTP {code}).")
            return False, status_by_integration
        substitutions = {
            "SERVICENOW_URL": settings["servicenow_url"].rstrip("/"),
            "SERVICENOW_USER": settings["servicenow_user"],
            "SERVICENOW_PASS": settings["servicenow_password"],
        }
        for name in ("CreateServiceNowChangeRequest", "UploadServiceNowAttachment"):
            try:
                properties = parse_zava_python_tool_manifest(
                    config_root / "tools" / name / f"{name}.yaml",
                    name,
                    substitutions,
                )
            except (OSError, ValueError) as error:
                job.emit("error", message=f"Invalid controlled ServiceNow tool: {error}")
                return False, status_by_integration
            code, _ = http_json(
                "PUT",
                f"{endpoint}/api/v2/extendedAgent/tools/{quote(name)}",
                data_token,
                {
                    "name": name,
                    "type": "ExtendedAgentTool",
                    "tags": [],
                    "properties": properties,
                },
            )
            if code not in (200, 201, 202, 204):
                job.emit("error", message=f"ServiceNow tool setup failed (HTTP {code}).")
                return False, status_by_integration
        enabled_skills.append(ZAVA_OPTIONAL_SKILLS["servicenow"])
        status_by_integration["servicenow"] = "healthy"

    for name in enabled_skills:
        try:
            properties = parse_zava_skill_manifest(
                config_root / "agent-config" / "skills" / name / "SKILL.md",
                name,
            )
        except (OSError, ValueError) as error:
            job.emit("error", message=f"Invalid optional Zava skill: {error}")
            return False, status_by_integration
        code, _ = http_json(
            "PUT",
            f"{endpoint}/api/v2/extendedAgent/skills/{quote(name)}",
            data_token,
            {"name": name, "type": "Skill", "tags": [], "properties": properties},
        )
        if code not in (200, 201, 202, 204):
            job.emit("error", message=f"Optional Zava skill setup failed (HTTP {code}).")
            return False, status_by_integration

    substitutions = {
        "RG": values["AZURE_RESOURCE_GROUP"],
        "REPO": "",
        "LAW_NAME": values.get("LOG_ANALYTICS_WORKSPACE_NAME", ""),
        "LAW_GUID": values.get("LOG_ANALYTICS_WORKSPACE_ID", ""),
        "APPINSIGHTS_NAME": values.get("APPLICATIONINSIGHTS_NAME", ""),
        "APPINSIGHTS_APPID": values.get("APPLICATIONINSIGHTS_APP_ID", ""),
        "ACR_NAME": values.get("AZURE_CONTAINER_REGISTRY_NAME", ""),
        "CAE_NAME": values.get("AZURE_CONTAINER_ENVIRONMENT_NAME", ""),
    }
    try:
        responder = parse_zava_agent_manifest(
            config_root / "agent-config" / "agents" / "zava-incident-responder"
            / "zava-incident-responder.yaml",
            "zava-incident-responder",
            substitutions,
            tuple(enabled_skills),
        )
    except (OSError, ValueError) as error:
        job.emit("error", message=f"Invalid Zava responder manifest: {error}")
        return False, status_by_integration
    code, _ = http_json(
        "PUT",
        f"{endpoint}/api/v2/extendedAgent/agents/zava-incident-responder",
        data_token,
        {
            "name": "zava-incident-responder",
            "type": "ExtendedAgent",
            "tags": [],
            "properties": responder,
        },
    )
    if code not in (200, 201, 202, 204):
        job.emit("error", message=f"Zava responder update failed (HTTP {code}).")
        return False, status_by_integration

    if requested["pagerduty"]:
        response_plan = {
            "incidentPlatform": "PagerDuty",
            "titleContains": "Zava",
            "handlingAgent": "zava-incident-responder",
            "agentMode": "autonomous",
            "maxAutomatedInvestigationAttempts": 3,
            "isEnabled": True,
        }
        encoded = __import__("base64").b64encode(
            json.dumps(response_plan, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        code, _ = http_json(
            "PUT",
            f"{agent_url}/incidentFilters/zava-learning-response"
            "?api-version=2025-05-01-preview",
            management_token,
            {"properties": {"value": encoded}},
        )
        if code not in (200, 201, 202, 204):
            job.emit("error", message=f"PagerDuty response plan failed (HTTP {code}).")
            return False, status_by_integration
    return True, status_by_integration


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
        "titleContains": alert_name,
        "titleContainsAll": [],
        "titleContainsAny": [],
        "titleNotContains": [],
        "handlingAgent": "incident-handler",
        "agentMode": "autonomous",
        "isEnabled": True,
    }


def response_plan_is_scoped(response: str, environment: str) -> bool:
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("titleContains")
        == f"alert-http-5xx-{environment}"
        and payload.get("isEnabled", True) is True
    )


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

    wait_for_nonessential_delay(
        "azure_monitor_initialization",
        lambda line: job.emit("output", line=line),
    )
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


def discover_zava_secure_resource_names(
    resource_group: str,
    current_values: dict[str, str],
) -> dict[str, str]:
    values = dict(current_values)
    queries = (
        (
            "KEY_VAULT_NAME",
            [
                "az", "keyvault", "list", "--resource-group", resource_group,
                "--query", "[0].name", "--output", "tsv",
            ],
        ),
        (
            "REPORTING_VM_NAME",
            [
                "az", "vm", "list", "--resource-group", resource_group,
                "--query", "[?starts_with(name,'vm-zava-reporting-')].name | [0]",
                "--output", "tsv",
            ],
        ),
    )
    for key, command in queries:
        if values.get(key):
            continue
        success, output = run_capture(command, timeout=60)
        if success and output.strip():
            values[key] = output.strip()
    values["AZURE_RESOURCE_GROUP"] = resource_group
    return values


def azure_resource_group_exists(resource_group: str) -> bool:
    if not resource_group:
        return False
    success, output = run_capture(
        ["az", "group", "exists", "--name", resource_group],
        timeout=30,
    )
    return success and output.strip().casefold() == "true"


def discover_zava_agent_names(resource_group: str) -> list[str]:
    if not resource_group:
        return []
    success, output = run_capture(
        [
            "az", "resource", "list",
            "--resource-group", resource_group,
            "--resource-type", "Microsoft.App/agents",
            "--query", "[].name",
            "--output", "json",
        ],
        timeout=60,
    )
    if not success:
        return []
    try:
        names = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(names, list):
        return []
    return [
        str(name)
        for name in names
        if isinstance(name, str) and name.strip()
    ]


def zava_process_environment(
    state: dict[str, Any],
    environment: str,
    values: dict[str, str],
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    stored = get_in_memory_secrets("zava-learning", environment)
    resource_group = (
        values.get("AZURE_RESOURCE_GROUP")
        or str(state.get("resource_group") or "")
    )
    existing = bool(
        state.get("existing_environment") or state.get("deployment_active")
        or azure_resource_group_exists(resource_group)
    )
    if existing:
        bridge_values = discover_zava_secure_resource_names(resource_group, values)
        missing_keys = [
            key
            for key in (
                "POSTGRES_ADMIN_PASSWORD",
                "POSTGRES_POOL_PASSWORD",
                "VM_ADMIN_PASSWORD",
            )
            if not stored.get(key)
        ]
        names_for_keys = {
            "POSTGRES_ADMIN_PASSWORD": "db-password",
            "POSTGRES_POOL_PASSWORD": "db-pool-password",
            "VM_ADMIN_PASSWORD": "vm-admin-password",
        }
        if missing_keys:
            try:
                recovered, missing_names = rehydrate_zava_secrets(
                    environment,
                    bridge_values,
                    tuple(names_for_keys[key] for key in missing_keys),
                )
            except ValueError:
                return None, (
                    "The private Key Vault bridge is unavailable. Existing Zava "
                    "operational deployment credentials cannot be rehydrated safely."
                )
            stored.update(recovered)
            if "db-password" in missing_names or "db-pool-password" in missing_names:
                return None, (
                    "Required database credentials are unavailable through the "
                    "reporting VM managed identity."
                )
            if "vm-admin-password" in missing_names:
                stored["VM_ADMIN_PASSWORD"] = generate_deployment_password()
                update_in_memory_secrets(
                    "zava-learning",
                    environment,
                    {"VM_ADMIN_PASSWORD": stored["VM_ADMIN_PASSWORD"]},
                )
                state["vm_credential_migration_pending"] = True
                save_state(state)
    else:
        required = (
            "POSTGRES_ADMIN_PASSWORD",
            "POSTGRES_POOL_PASSWORD",
            "VM_ADMIN_PASSWORD",
        )
        if any(not stored.get(key) for key in required):
            generated = new_zava_deployment_secrets()
            update_in_memory_secrets("zava-learning", environment, generated)
            stored.update(generated)
    required = (
        "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_POOL_PASSWORD",
        "VM_ADMIN_PASSWORD",
    )
    if any(not stored.get(key) for key in required):
        return None, "Secure Zava deployment parameters are unavailable."
    process_environment = {key: stored[key] for key in required}
    process_environment["PAGERDUTY_WEBHOOK_URL"] = stored.get(
        "pagerduty_webhook_url",
        "",
    )
    integration_status = state.get("integration_status")
    pagerduty_configured = (
        isinstance(integration_status, dict)
        and integration_status.get("pagerduty") in (True, "healthy", "configured")
    )
    process_environment["ZAVA_PAGERDUTY_CONFIGURED"] = (
        "true" if pagerduty_configured else "false"
    )
    return process_environment, None


def hydrate_zava_runtime_outputs(
    job: Job,
    environment: str,
    state: dict[str, Any],
    expected_agent_name: str = "",
    attempts: int = 30,
    delay_seconds: float = 10,
) -> Optional[dict[str, str]]:
    lab = LABS_BY_ID["zava-learning"]
    values = azd_values(environment, lab)
    resource_group = (
        values.get("AZURE_RESOURCE_GROUP")
        or str(state.get("resource_group") or "")
        or zava_resource_group_name(environment)
    )
    agent_query = (
        f"[?name=='{expected_agent_name}'] | [0]."
        "{name:name,endpoint:properties.agentEndpoint,location:location}"
        if expected_agent_name
        else "[0].{name:name,endpoint:properties.agentEndpoint,location:location}"
    )
    agent: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        success, output = run_capture([
            "az", "resource", "list",
            "--resource-group", resource_group,
            "--resource-type", "Microsoft.App/agents",
            "--query", agent_query,
            "--output", "json",
        ], timeout=60)
        try:
            candidate = json.loads(output) if success else {}
        except json.JSONDecodeError:
            candidate = {}
        if (
            isinstance(candidate, dict)
            and candidate.get("name")
            and candidate.get("endpoint")
        ):
            agent = candidate
            break
        if attempt == 1:
            job.emit(
                "step",
                name="Waiting for the Zava SRE Agent endpoint",
            )
            job.emit(
                "output",
                line=(
                    "The SRE Agent resource succeeded. Azure is still publishing "
                    "its data-plane endpoint."
                ),
            )
        if attempt < attempts:
            time.sleep(delay_seconds)
    if not isinstance(agent, dict) or not agent.get("name") or not agent.get("endpoint"):
        job.emit(
            "error",
            message=(
                "Azure did not publish the deployed Zava SRE Agent endpoint "
                f"after {attempts} readiness checks."
            ),
        )
        return None
    values.update({
        "AZURE_RESOURCE_GROUP": resource_group,
        "AZURE_SUBSCRIPTION_ID": str(state.get("subscription_id") or ""),
        "SRE_AGENT_NAME": str(agent["name"]),
        "SRE_AGENT_ENDPOINT": str(agent["endpoint"]),
        "AZURE_AGENT_LOCATION": str(
            agent.get("location") or values.get("AZURE_AGENT_LOCATION") or ""
        ).lower(),
        "AGENT_PORTAL_URL": sre_agent_portal_url(
            str(state.get("subscription_id") or ""),
            resource_group,
            str(agent["name"]),
        ),
    })
    workspace_resource_id = values.get("LOG_ANALYTICS_WORKSPACE_ID", "")
    if workspace_resource_id:
        success, output = run_capture(
            [
                "az", "monitor", "log-analytics", "workspace", "show",
                "--ids", workspace_resource_id,
                "--query", "{name:name,customerId:customerId}",
                "--output", "json",
            ],
            timeout=60,
        )
        try:
            workspace = json.loads(output) if success else {}
        except json.JSONDecodeError:
            workspace = {}
        if isinstance(workspace, dict) and workspace.get("customerId"):
            values["LOG_ANALYTICS_WORKSPACE_RESOURCE_ID"] = workspace_resource_id
            values["LOG_ANALYTICS_WORKSPACE_ID"] = str(workspace["customerId"])
            values["LOG_ANALYTICS_WORKSPACE_NAME"] = str(
                workspace.get("name") or ""
            )
    app_insights_name = values.get("APPLICATIONINSIGHTS_NAME", "")
    if app_insights_name:
        success, output = run_capture(
            [
                "az", "monitor", "app-insights", "component", "show",
                "--app", app_insights_name,
                "--resource-group", resource_group,
                "--query", "appId",
                "--output", "tsv",
            ],
            timeout=60,
        )
        if success and output.strip():
            values["APPLICATIONINSIGHTS_APP_ID"] = output.strip()
    host = values.get("APPGW_PUBLIC_FQDN", "")
    values["ZAVA_PORTAL_URL"] = f"http://{host}" if host else ""
    for port in ZAVA_LANE_PORTS:
        values[f"ZAVA_LANE_{port}_URL"] = (
            f"http://{host}:{port}" if host else ""
        )
    saved, error = set_azd_values(environment, values, lab)
    if not saved:
        job.emit("error", message=error or "Unable to save Zava runtime outputs.")
        return None
    return values


def reconcile_zava(job: Job, restoring: bool = False) -> None:
    state = load_state()
    environment = str(state.get("environment") or "")
    if not environment:
        job.emit("error", message="Configure an environment before deploying.")
        job.finish(False, None)
        return
    lab = LABS_BY_ID["zava-learning"]
    vendor_dir = vendor_dir_for_lab(lab)
    values = azd_values(environment, lab)
    region_values = {
        "location": normalize_azure_location(
            values.get("AZURE_LOCATION") or state.get("location")
        ),
        "db_location": normalize_azure_location(
            values.get("AZURE_DB_LOCATION") or state.get("db_location") or ""
        ),
        "agent_location": normalize_azure_location(
            values.get("AZURE_AGENT_LOCATION") or state.get("agent_location") or ""
        ),
    }
    invalid_regions = [
        definition.name
        for definition in lab.regions
        if region_values.get(definition.id) not in definition.allowed_values
    ]
    if invalid_regions:
        job.emit(
            "error",
            message=(
                "Zava reconciliation is blocked because immutable Azure regions "
                "could not be discovered for: " + ", ".join(invalid_regions) + "."
            ),
        )
        job.finish(False, 1)
        return
    saved, region_error = set_azd_values(
        environment,
        {
            "AZURE_LOCATION": region_values["location"],
            "AZURE_DB_LOCATION": region_values["db_location"],
            "AZURE_AGENT_LOCATION": region_values["agent_location"],
        },
        lab,
    )
    if not saved:
        job.emit("error", message=region_error or "Unable to preserve Zava regions.")
        job.finish(False, 1)
        return
    process_environment, error = zava_process_environment(
        state,
        environment,
        values,
    )
    if process_environment is None:
        job.emit("error", message=error or "Secure Zava configuration failed.")
        job.finish(False, 1)
        return
    resource_group = (
        values.get("AZURE_RESOURCE_GROUP")
        or str(state.get("resource_group") or "")
        or zava_resource_group_name(environment)
    )
    agent_location = region_values["agent_location"]
    configured_agent_name = str(values.get("SRE_AGENT_NAME") or "")
    discovered_agent_names = discover_zava_agent_names(resource_group)
    if configured_agent_name:
        agent_name = configured_agent_name
    elif len(discovered_agent_names) == 1:
        agent_name = discovered_agent_names[0]
        job.emit(
            "output",
            line=f"Resuming the existing Zava SRE Agent {agent_name}.",
        )
    elif len(discovered_agent_names) > 1:
        job.emit(
            "error",
            message=(
                "Multiple Zava SRE Agents exist in the resource group. "
                "Select an existing managed environment before reconciling."
            ),
        )
        job.finish(False, 1)
        return
    else:
        agent_name = zava_agent_name(environment)
    preserve_agent_configuration = bool(
        values.get("SRE_AGENT_NAME") and values.get("SRE_AGENT_ENDPOINT")
    )
    job.emit("started", command=["azd", "provision", "-e", environment])
    if restoring:
        reset_script = vendor_dir / "chaos" / "reset.ps1"
        job.emit("step", name="Resetting all eight Zava scenario lanes")
        reset_ok, _ = run_process(
            job,
            [
                "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-File", str(reset_script),
                "-Scenario", "all",
                "-ResourceGroup", resource_group,
            ],
            vendor_dir,
        )
        if not reset_ok:
            job.emit("error", message="Zava lane reset failed; reconciliation stopped.")
            job.finish(False, 1)
            return
    job.emit("step", name="Previewing Zava infrastructure reconciliation")
    success, _ = run_process(
        job,
        ["azd", "provision", "--preview", "-e", environment, "--no-prompt"],
        vendor_dir,
        environment_overrides=process_environment,
    )
    if not success:
        job.emit("error", message="Zava infrastructure preview failed.")
        job.finish(False, 1)
        return
    job.emit("step", name="Provisioning Zava infrastructure without local Docker")
    success, _ = run_process(
        job,
        ["azd", "provision", "-e", environment, "--no-prompt"],
        vendor_dir,
        environment_overrides=process_environment,
    )
    if not success:
        job.emit("error", message="Zava infrastructure provisioning failed.")
        job.finish(False, 1)
        return
    job.emit("step", name="Building and deploying Zava services through ACR")
    success, _ = run_process(
        job,
        [
            "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-File", str(vendor_dir / "scripts" / "post-provision.ps1"),
            "-ResourceGroup", resource_group,
        ],
        vendor_dir,
        environment_overrides=process_environment,
    )
    if not success:
        job.emit("error", message="Zava ACR build or post-provisioning failed.")
        job.finish(False, 1)
        return
    if preserve_agent_configuration:
        job.emit(
            "step",
            name="Preserving the existing Zava SRE Agent connector configuration",
        )
    else:
        job.emit("step", name="Deploying the Zava SRE Agent in its selected region")
        success, _ = run_process(
            job,
            [
                "pwsh", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-File", str(vendor_dir / "scripts" / "deploy-sre-agent.ps1"),
                "-ResourceGroup", resource_group,
                "-Location", agent_location,
                "-AgentName", agent_name,
                "-EnvironmentName", environment,
                "-IncidentPlatform", "AzMonitor",
                "-ModelProvider", "Anthropic",
                "-ModelName", "Automatic",
            ],
            vendor_dir,
        )
        if not success:
            job.emit("error", message="Zava SRE Agent deployment failed.")
            job.finish(False, 1)
            return
    values = hydrate_zava_runtime_outputs(
        job,
        environment,
        state,
        expected_agent_name=agent_name,
    )
    if values is None:
        job.finish(False, 1)
        return
    values = discover_zava_secure_resource_names(resource_group, values)
    integration_status = state.get("integration_status")
    enabled_optional_skills = tuple(
        skill
        for name, skill in ZAVA_OPTIONAL_SKILLS.items()
        if isinstance(integration_status, dict)
        and integration_status.get(name) in ("healthy", "present", "configured")
    )
    if not configure_zava_agent_core(
        job,
        environment,
        values,
        preserve_incident_configuration=preserve_agent_configuration,
        enabled_optional_skills=enabled_optional_skills,
    ):
        job.finish(False, 1)
        return
    transient_settings = get_in_memory_secrets("zava-learning", environment)
    configured, configured_status = configure_zava_optional_integrations(
        job,
        environment,
        values,
        transient_settings,
    )
    if not configured:
        job.emit(
            "error",
            message=(
                "Requested integration setup failed. One-time settings remain "
                "in memory for a safe retry and were not persisted by the app."
            ),
        )
        job.finish(False, 1)
        return
    if configured_status:
        state["integration_status"] = {
            "pagerduty": configured_status.get("pagerduty", "not_configured"),
            "servicenow": configured_status.get("servicenow", "not_configured"),
            "github": "not_configured",
        }
    state["deployment_active"] = True
    state["resource_group"] = resource_group
    state.update(region_values)
    state["validation_status"] = "reconciled"
    state.pop("vm_credential_migration_pending", None)
    save_state(state)
    clear_in_memory_secrets("zava-learning", environment)
    job.finish(True, 0)


def reconcile_demo(job: Job, restoring: bool = False) -> None:
    state = load_state()
    lab = selected_lab(state)
    if lab and lab.id == "zava-learning":
        reconcile_zava(job, restoring)
        return
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
    lab = selected_lab(state)
    if lab and lab.id == "zava-learning":
        values = azd_values(str(environment), lab)
        resource_group = (
            values.get("AZURE_RESOURCE_GROUP")
            or str(state.get("resource_group") or "")
        )
        success, output = run_capture(
            [
                "az", "group", "show",
                "--name", resource_group,
                "--query", "tags",
                "--output", "json",
            ],
            timeout=30,
        )
        try:
            tags = json.loads(output) if success else {}
        except json.JSONDecodeError:
            tags = {}
        normalized_tags = {
            str(key).casefold(): str(value)
            for key, value in tags.items()
        } if isinstance(tags, dict) else {}
        if (
            normalized_tags.get(LAB_ID_TAG.casefold()) != lab.id
            or normalized_tags.get(LAB_ENVIRONMENT_TAG.casefold())
            != str(environment)
        ):
            job.emit(
                "error",
                message=(
                    "Zava teardown is blocked because stable application ownership "
                    "tags could not be verified."
                ),
            )
            job.finish(False, None)
            return
    job.emit("started", command=["azd", "down"])
    vendor_dir = vendor_dir_for_lab(lab) if lab else VENDOR_DIR
    success, _ = run_process(
        job,
        [
            "azd", "down", "-e", environment,
            "--purge", "--force", "--no-prompt",
        ],
        vendor_dir,
    )
    if success:
        state["deployment_active"] = False
        state.pop("scenario_id", None)
        save_state(state)
        if lab:
            clear_in_memory_secrets(lab.id, str(environment))
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


def zava_scenario_worker(job: Job) -> None:
    if not ACTIVE_SCENARIO_LOCK.acquire(blocking=False):
        job.emit("error", message="Another scenario is already running.")
        job.finish(False, None)
        return
    try:
        _run_zava_scenario(job)
    finally:
        ACTIVE_SCENARIO_LOCK.release()


def generate_zava_scenario_traffic(
    scenario: ScenarioDefinition,
    scenario_url: str,
) -> tuple[bool, str]:
    endpoint = f"{scenario_url.rstrip('/')}/{scenario.probe_path.lstrip('/')}"
    attempts = 30 if scenario.id == "pool" else 12
    results: list[tuple[bool, float, str]] = []
    result_lock = threading.Lock()

    def request_once() -> None:
        started = time.monotonic()
        available, detail = probe_http_endpoint(scenario_url, scenario.probe_path)
        with result_lock:
            results.append((available, time.monotonic() - started, detail))

    if scenario.id == "pool":
        threads = [threading.Thread(target=request_once) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        for _ in range(attempts):
            request_once()
            time.sleep(0.25)
    failures = sum(not available for available, _elapsed, _detail in results)
    slow = sum(elapsed >= 0.5 for _available, elapsed, _detail in results)
    if scenario.id in {"perf", "query"}:
        return slow >= 3, f"{slow}/{len(results)} requests exceeded 500 ms"
    required_failures = 3 if scenario.id != "pool" else 2
    return (
        failures >= required_failures,
        f"{failures}/{len(results)} requests returned a failure",
    )


def zava_scenario_signal_query(
    scenario_id: str,
    injected_at: datetime,
) -> str:
    timestamp = injected_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if scenario_id == "disk":
        source = (
            "Syslog "
            f"| where TimeGenerated >= datetime({timestamp}) "
            '| where ProcessName == "zava-export" and SyslogMessage has "FAILED"'
        )
    elif scenario_id in {"perf", "query"}:
        source = (
            "ContainerAppConsoleLogs_CL "
            f"| where TimeGenerated >= datetime({timestamp}) "
            f'| where ContainerAppName_s == "quiz-{scenario_id}" '
            '| extend ms=toint(extract(@"ms=(\\d+)", 1, Log_s)) '
            "| where ms > 500"
        )
    else:
        source = (
            "AzureDiagnostics "
            f"| where TimeGenerated >= datetime({timestamp}) "
            '| where ResourceType == "APPLICATIONGATEWAYS" '
            'and Category == "ApplicationGatewayAccessLog" '
            f'| where listenerName_s == "quiz-{scenario_id}-listener" '
            "| where toint(httpStatus_d) >= 500"
        )
    return source + " | summarize Count=count()"


def wait_for_zava_monitor_signal(
    workspace: str,
    scenario_id: str,
    injected_at: datetime,
    timeout_seconds: int = 600,
) -> tuple[bool, int]:
    query = zava_scenario_signal_query(scenario_id, injected_at)
    deadline = time.monotonic() + timeout_seconds
    while True:
        success, output = run_capture(
            [
                "az", "monitor", "log-analytics", "query",
                "--workspace", workspace,
                "--analytics-query", query,
                "--timespan", "PT15M",
                "--output", "json",
            ],
            timeout=90,
        )
        count = 0
        if success:
            try:
                rows = json.loads(output)
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    count = int(rows[0].get("Count") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                count = 0
        if count > 0:
            return True, count
        if time.monotonic() >= deadline:
            return False, 0
        time.sleep(20)


def _run_zava_scenario(job: Job) -> None:
    state = load_state()
    lab = selected_lab(state)
    scenario_id = str(state.get("scenario_id") or "")
    scenario = next(
        (
            item for item in (lab.scenarios if lab else ())
            if item.id == scenario_id
        ),
        None,
    )
    runtime_values = azd_values(str(state.get("environment") or ""), lab)
    resource_group = str(
        runtime_values.get(
            "AZURE_RESOURCE_GROUP",
            "",
        )
        or state.get("resource_group")
        or ""
    )
    if lab is None or lab.id != "zava-learning" or scenario is None:
        job.emit("error", message="The selected Zava scenario is unavailable.")
        job.finish(False, None)
        return
    if not resource_group:
        job.emit("error", message="The Zava resource group is unavailable.")
        job.finish(False, None)
        return

    script = vendor_dir_for_lab(lab) / "chaos" / f"break-{scenario.script_id}.ps1"
    if not script.is_file():
        job.emit("error", message=f"Scenario script is missing: {script.name}")
        job.finish(False, None)
        return

    command = [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        "-ResourceGroup",
        resource_group,
    ]
    job.emit("started", command=command)
    job.emit("phase", name="preflight", scenario_id=scenario.id)
    if scenario.web_health:
        host = runtime_values.get("APPGW_PUBLIC_FQDN", "")
        scenario_url = (
            f"http://{host}:{scenario.lane_port}"
            if host and scenario.lane_port
            else ""
        )
        if not scenario_url:
            job.emit("error", message="The selected Zava lane endpoint is unavailable.")
            job.finish(False, 1)
            return
        available, detail = probe_http_endpoint(scenario_url, scenario.probe_path)
        if not available:
            job.emit(
                "error",
                message=(
                    "The selected Zava lane is not healthy before injection: "
                    f"{detail}. Restore the baseline first."
                ),
            )
            job.finish(False, 1)
            return
    injected_at = datetime.now(timezone.utc)
    job.emit("phase", name="injecting", scenario_id=scenario.id)
    job.emit("step", name=f"Injecting {scenario.name}")
    success, _ = run_process(job, command, vendor_dir_for_lab(lab))
    if not success:
        job.emit(
            "error",
            message=(
                f"{scenario.name} was not confirmed live. "
                "Review the scenario output and restore the baseline before retrying."
            ),
        )
        job.finish(False, 1)
        return
    job.emit("phase", name="generating_telemetry", scenario_id=scenario.id)
    if scenario.web_health:
        impact, impact_detail = generate_zava_scenario_traffic(
            scenario,
            scenario_url,
        )
        job.emit("output", line=f"Post-injection impact: {impact_detail}.")
        if not impact:
            job.emit(
                "error",
                message=(
                    "The fault script completed, but its expected customer impact "
                    "was not observed."
                ),
            )
            job.finish(False, 1)
            return
    workspace = str(runtime_values.get("LOG_ANALYTICS_WORKSPACE_ID") or "")
    if not workspace:
        job.emit(
            "error",
            message="The Log Analytics workspace is unavailable for signal validation.",
        )
        job.finish(False, 1)
        return
    signal_found, signal_count = wait_for_zava_monitor_signal(
        workspace,
        scenario.id,
        injected_at,
    )
    if not signal_found:
        job.emit(
            "error",
            message=(
                "The expected post-injection Azure Monitor signal was not ingested. "
                "The scenario remains failed rather than reporting a false success."
            ),
        )
        job.finish(False, 1)
        return
    job.emit("phase", name="impact_confirmed", scenario_id=scenario.id)
    job.emit(
        "step",
        name=f"Fault impact and Azure Monitor signal confirmed ({signal_count})",
    )
    job.emit(
        "investigation_countdown",
        scenario_id=scenario.id,
        seconds=scenario.investigation_delay_seconds,
        started_at=time.time(),
    )
    job.emit(
        "output",
        line=(
            "The fault is live and matching Azure Monitor telemetry was validated. "
            "Watch Azure Monitor and the SRE Agent portal for autonomous response."
        ),
    )
    job.finish(True, 0)


SCENARIO_WORKERS: dict[tuple[str, str], Callable[[Job], None]] = {
    ("grubify-starter-lab", "memory-leak"): break_cart_worker,
    **{
        ("zava-learning", scenario.id): zava_scenario_worker
        for scenario in LABS_BY_ID["zava-learning"].scenarios
    },
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
            self.send_json({
                "token": SESSION_TOKEN,
                "test_mode": is_test_mode(),
            })
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
            lab = selected_lab(state)
            values = azd_values(environment, lab)
            resource_group = (
                values.get("AZURE_RESOURCE_GROUP", "")
                or str(state.get("resource_group") or "")
            )
            links = runtime_summary_links(state, values)
            self.send_json({
                "lab_id": lab.id if lab else "",
                "lab_name": lab.name if lab else "",
                "environment": environment,
                "existing_environment": bool(state.get("existing_environment")),
                "environment_detection": state.get(
                    "existing_environment_detection",
                    "",
                ),
                "validation_status": state.get("validation_status", ""),
                "validation_issues": state.get("validation_issues", []),
                "availability_checks": state.get("availability_checks", []),
                "resource_group": resource_group,
                "resource_group_portal_url": azure_resource_group_portal_url(
                    str(state.get("tenant_id") or ""),
                    str(state.get("subscription_id") or ""),
                    resource_group,
                ),
                "agent_portal_url": resolved_sre_agent_portal_url(state, values),
                "agent_name": values.get("SRE_AGENT_NAME", ""),
                "agent_endpoint": values.get("SRE_AGENT_ENDPOINT", ""),
                "api_url": values.get("CONTAINER_APP_URL", ""),
                "frontend_url": values.get("FRONTEND_APP_URL", ""),
                "links": links,
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
        if path == "/api/environments/skip-validation":
            self.skip_environment_validation()
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
        existing_environment = payload.get("existing_environment") is True
        if not re.fullmatch(r"[a-zA-Z0-9-]{2,30}", environment):
            self.send_json(
                {"error": "Environment must be 2-30 letters, numbers, or hyphens."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if lab.id == "zava-learning" and existing_environment:
            region_values: dict[str, str] = {}
            location = ""
        else:
            region_values, region_error = validate_lab_regions(lab, payload)
            if region_error:
                self.send_json({"error": region_error}, HTTPStatus.BAD_REQUEST)
                return
            location = region_values["location"]
        if payload.get("integrations") and lab.id != "zava-learning":
            self.send_json(
                {"error": "Integrations are supported only by new Zava labs."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if payload.get("integrations") and existing_environment:
            self.send_json(
                {
                    "error": (
                        "Integration credentials cannot be accepted while selecting "
                        "an existing lab. Reconnect integrations in the SRE Agent portal."
                    )
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        integration_values: dict[str, str] = {}
        if lab.id == "zava-learning":
            integration_values, integration_error = parse_zava_integrations(
                payload.get("integrations")
            )
            if integration_error:
                self.send_json({"error": integration_error}, HTTPStatus.BAD_REQUEST)
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
        selected_resource_group = zava_resource_group_name(environment)
        if lab.id == "zava-learning" and existing_environment:
            requested_resource_group = str(
                payload.get("resource_group") or ""
            ).strip()
            candidate = next(
                (
                    item
                    for item in load_environment_cache(subscription_id, lab.id)
                    if item.get("environment") == environment
                    and (
                        not requested_resource_group
                        or item.get("resource_group") == requested_resource_group
                    )
                ),
                None,
            )
            if candidate is None:
                self.send_json(
                    {
                        "error": (
                            "The selected existing Zava lab is not in the latest "
                            "subscription scan."
                        )
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            selected_resource_group = str(candidate.get("resource_group") or "")
            discovered_regions = {
                "location": normalize_azure_location(candidate.get("location")),
                "db_location": normalize_azure_location(
                    candidate.get("db_location")
                ),
                "agent_location": normalize_azure_location(
                    candidate.get("agent_location")
                ),
            }
            invalid_regions = [
                definition.name
                for definition in lab.regions
                if discovered_regions.get(definition.id) not in definition.allowed_values
            ]
            if invalid_regions:
                self.send_json(
                    {
                        "error": (
                            "Cannot safely reconcile this Zava lab because Azure "
                            "did not return supported immutable regions for: "
                            + ", ".join(invalid_regions)
                            + "."
                        )
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            region_values = discovered_regions
            location = discovered_regions["location"]

        vendor_dir = vendor_dir_for_lab(lab)
        new_command = [
            "azd", "env", "new", environment,
            "--location", location,
            "--subscription", subscription_id,
            "--no-prompt",
        ]
        created, output = run_capture(new_command, vendor_dir)
        if not created:
            selected, select_output = run_capture(
                ["azd", "env", "select", environment], vendor_dir
            )
            if not selected:
                self.send_json(
                    {"error": output or select_output or "Unable to configure azd environment."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        settings = {
            "AZURE_LOCATION": location,
            "AZURE_SUBSCRIPTION_ID": subscription_id,
        }
        if lab.id == "zava-learning":
            settings.update({
                "AZURE_DB_LOCATION": region_values["db_location"],
                "AZURE_AGENT_LOCATION": region_values["agent_location"],
                "AZURE_RESOURCE_GROUP": selected_resource_group,
            })
        for key, value in settings.items():
            saved, save_output = run_capture(
                ["azd", "env", "set", "-e", environment, key, value],
                vendor_dir,
            )
            if not saved:
                self.send_json(
                    {"error": save_output or f"Unable to set {key}."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        if lab.id == "zava-learning" and not existing_environment:
            transient_values = new_zava_deployment_secrets()
            transient_values.update(integration_values)
            replace_in_memory_secrets(lab.id, environment, transient_values)

        state.update({
            "lab_id": lab.id,
            "environment": environment,
            "location": location,
            "tenant_id": context["tenant"],
            "subscription_id": subscription_id,
            "deployment_active": False,
            "existing_environment": existing_environment,
        })
        if lab.id == "zava-learning":
            state["resource_group"] = selected_resource_group
        for region_id, value in region_values.items():
            state[region_id] = value
        if lab.id == "zava-learning":
            if existing_environment:
                state["integration_status"] = {
                    "pagerduty": "unknown",
                    "servicenow": "unknown",
                    "github": "unknown",
                }
            else:
                state["integration_status"] = {
                    "pagerduty": "requested" if (
                        integration_values.get("pagerduty_api_token")
                        or integration_values.get("pagerduty_webhook_url")
                    ) else "not_configured",
                    "servicenow": "requested" if (
                        integration_values.get("servicenow_url")
                        and integration_values.get("servicenow_user")
                        and integration_values.get("servicenow_password")
                    ) else "not_configured",
                    "github": "not_configured",
                }
        state.pop("scenario_id", None)
        state.pop("validated_at", None)
        state.pop("validation_skipped_at", None)
        state.pop("validation_status", None)
        state.pop("validation_issues", None)
        state.pop("availability_checks", None)
        if not existing_environment:
            state.pop("existing_environment_detection", None)
        save_state(state)
        self.send_json(state)

    def existing_environment_candidate(
        self,
        payload: dict[str, Any],
    ) -> Optional[
        tuple[dict[str, Any], LabDefinition, dict[str, str], dict[str, Any]]
    ]:
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
            return None
        if (
            not state.get("existing_environment")
            or state.get("environment") != environment_name
            or state.get("subscription_id") != context["subscription"]
        ):
            self.send_json(
                {"error": "Save the selected existing lab before using it."},
                HTTPStatus.CONFLICT,
            )
            return None
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
            return None
        return state, lab, context, candidate

    def validate_environment(self) -> None:
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return

        selection = self.existing_environment_candidate(payload)
        if selection is None:
            return
        state, _lab, context, candidate = selection
        environment_name = str(candidate["environment"])

        result = (
            validate_existing_lab(context["subscription"], candidate, _lab)
            if _lab.id == "zava-learning"
            else validate_existing_lab(context["subscription"], candidate)
        )
        availability_checks = result.get("availability_checks", [])
        LOGGER.info(
            "Existing lab validation environment=%s ready=%s "
            "availability_checks=%s issues=%s",
            environment_name,
            result["ready"],
            safe_log_payload({"checks": availability_checks})["checks"],
            result["issues"],
        )
        state["deployment_active"] = False
        state["existing_environment_detection"] = candidate.get("detection", "")
        state["validation_issues"] = result["issues"]
        state["availability_checks"] = availability_checks
        state["validation_status"] = "failed"
        state.pop("validated_at", None)
        state.pop("validation_skipped_at", None)
        if _lab.id == "zava-learning":
            discovered_values = result.get("values", {})
            for state_key, value_key in (
                ("location", "AZURE_LOCATION"),
                ("db_location", "AZURE_DB_LOCATION"),
                ("agent_location", "AZURE_AGENT_LOCATION"),
            ):
                if discovered_values.get(value_key):
                    state[state_key] = discovered_values[value_key]
            state["integration_status"] = result.get("integration_status", {})
            if discovered_values:
                saved, error = set_azd_values(
                    environment_name,
                    discovered_values,
                    _lab,
                )
                if not saved:
                    save_state(state)
                    self.send_json({"error": error}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
        if not result["ready"]:
            save_state(state)
            self.send_json({
                "ready": False,
                "environment": environment_name,
                "issues": result["issues"],
                "availability_checks": availability_checks,
                "values": result.get("values", {}),
                "integration_status": result.get("integration_status", {}),
            })
            return

        saved, error = (
            set_azd_values(environment_name, result["values"], _lab)
            if _lab.id == "zava-learning"
            else set_azd_values(environment_name, result["values"])
        )
        if not saved:
            save_state(state)
            self.send_json(
                {"error": error},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        state["deployment_active"] = True
        state["resource_group"] = candidate.get("resource_group", "")
        state["validation_issues"] = []
        state["validation_status"] = "validated"
        state["validated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        self.send_json({
            "ready": True,
            "environment": environment_name,
            "issues": [],
            "availability_checks": availability_checks,
        })

    def skip_environment_validation(self) -> None:
        client = getattr(self, "client_address", ("<unknown>",))[0]
        if not is_test_mode():
            LOGGER.warning(
                "Rejected validation skip because test mode is disabled client=%s",
                client,
            )
            self.send_json(
                {"error": "Validation skip is available only in test mode."},
                HTTPStatus.FORBIDDEN,
            )
            return
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid JSON request"}, HTTPStatus.BAD_REQUEST)
            return
        if payload.get("acknowledge_risk") is not True:
            self.send_json(
                {"error": "Explicit risk acknowledgement is required."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        selection = self.existing_environment_candidate(payload)
        if selection is None:
            return
        state, _lab, context, candidate = selection
        environment_name = str(candidate["environment"])
        runtime_values = {
            str(key): str(value)
            for key, value in (candidate.get("runtime_values") or {}).items()
            if value
        }
        required_values = {
            "AZURE_RESOURCE_GROUP",
            "CONTAINER_APP_NAME",
            "CONTAINER_APP_URL",
            "FRONTEND_APP_NAME",
            "FRONTEND_APP_URL",
            "SRE_AGENT_NAME",
        }
        missing_values = sorted(required_values - runtime_values.keys())
        if missing_values:
            self.send_json(
                {
                    "error": (
                        "The latest subscription scan did not return enough "
                        "runtime metadata to skip validation. Scan again or "
                        "validate normally."
                    ),
                    "missing_values": missing_values,
                },
                HTTPStatus.CONFLICT,
            )
            return
        saved, error = set_azd_values(environment_name, runtime_values)
        if not saved:
            self.send_json(
                {"error": error},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        issue = (
            "Validation was explicitly skipped in test mode; resource readiness "
            "and endpoint availability are unknown."
        )
        state["deployment_active"] = True
        state["existing_environment_detection"] = candidate.get("detection", "")
        state["resource_group"] = candidate.get("resource_group", "")
        state["validation_status"] = "skipped"
        state["validation_issues"] = [issue]
        state["availability_checks"] = []
        state["validation_skipped_at"] = datetime.now(timezone.utc).isoformat()
        state.pop("validated_at", None)
        save_state(state)
        LOGGER.warning(
            "TEST MODE validation skipped environment=%s resource_group=%s "
            "subscription=%s client=%s",
            environment_name,
            candidate.get("resource_group", ""),
            context["subscription"],
            client,
        )
        self.send_json({
            "ready": False,
            "proceed": True,
            "skipped": True,
            "environment": environment_name,
            "issues": [issue],
            "validation_status": "skipped",
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


def main(argv: Optional[list[str]] = None) -> None:
    options = parse_runtime_options(argv)
    set_runtime_options(options)
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
    LOGGER.log(
        logging.WARNING if options.test_mode else logging.INFO,
        "Runtime mode test_mode=%s config=%s",
        options.test_mode,
        options.config_file,
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
