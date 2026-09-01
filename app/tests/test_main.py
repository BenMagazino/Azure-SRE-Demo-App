import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.main import (
    INSTALL_COMMANDS,
    INSTALL_ORDER,
    Job,
    SRE_AGENT_REGIONS,
    STATIC_DIR,
    ToolStatus,
    VENDOR_DIR,
    activate_azure_context,
    authentication_statuses,
    azd_login_command,
    azure_context_is_available,
    azure_cli_management_authenticated,
    azure_login_worker,
    break_cart_worker,
    build_azure_context_catalog,
    claims_challenge_login_command,
    command_version,
    deploy_worker,
    http_json,
    install_all_worker,
    is_device_login_url,
    memory_pressure_observed,
    open_browser_url,
    parse_claims_challenge_login,
    parse_device_code,
    prerequisite_statuses,
    redact_command,
    redact_text,
    request_metrics_have_data,
    response_plan_payload,
    response_plan_status_is_retryable,
    restore_baseline_worker,
    restore_container_baseline,
    resolved_process_command,
    run_capture,
    run_process,
    safe_log_payload,
    scoped_azure_login_command,
    should_open_browser,
    teardown_worker,
    upsert_response_plan,
    wait_for_request_metrics,
)


TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "00000000-0000-0000-0000-000000000002"
SUBSCRIPTION_A = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_B = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_C = "33333333-3333-3333-3333-333333333333"


class DeviceCodeTests(unittest.TestCase):
    def test_parses_azure_cli_device_code(self) -> None:
        line = (
            "To sign in, use a web browser to open the page "
            "https://microsoft.com/devicelogin and enter the code ABCD-EFGH "
            "to authenticate."
        )
        self.assertEqual(
            parse_device_code(line),
            {
                "verification_url": "https://microsoft.com/devicelogin",
                "code": "ABCD-EFGH",
            },
        )

    def test_ignores_unrelated_output(self) -> None:
        self.assertIsNone(parse_device_code("Retrieving subscriptions..."))

    def test_parses_new_device_login_url(self) -> None:
        line = (
            "To sign in, use a web browser to open the page "
            "https://login.microsoft.com/device and enter the code BZ4MVCCE8 "
            "to authenticate."
        )
        self.assertEqual(
            parse_device_code(line),
            {
                "verification_url": "https://login.microsoft.com/device",
                "code": "BZ4MVCCE8",
            },
        )

    def test_parses_azd_device_code_without_url(self) -> None:
        self.assertEqual(
            parse_device_code("Start by copying the next code: FD9EAW26Z"),
            {
                "verification_url": "https://microsoft.com/devicelogin",
                "code": "FD9EAW26Z",
            },
        )

    def test_only_allows_known_device_login_urls(self) -> None:
        self.assertTrue(is_device_login_url("https://login.microsoft.com/device"))
        self.assertTrue(is_device_login_url("https://microsoft.com/devicelogin"))
        self.assertFalse(is_device_login_url("https://example.com/device"))


class DiagnosticRedactionTests(unittest.TestCase):
    def test_redacts_device_code_from_process_output(self) -> None:
        line = (
            "Open https://microsoft.com/devicelogin and enter the code "
            "ABCD-EFGH to authenticate."
        )

        redacted = redact_text(line)

        self.assertNotIn("ABCD-EFGH", redacted)
        self.assertIn("<redacted-device-code>", redacted)

    def test_redacts_azd_device_code_from_process_output(self) -> None:
        redacted = redact_text("Start by copying the next code: FD9EAW26Z")

        self.assertNotIn("FD9EAW26Z", redacted)
        self.assertIn("<redacted-device-code>", redacted)

    def test_redacts_claims_challenge_from_commands(self) -> None:
        command = [
            "az",
            "login",
            "--claims-challenge",
            "sensitive-claims-value",
            "--use-device-code",
        ]

        self.assertEqual(
            redact_command(command),
            [
                "az",
                "login",
                "--claims-challenge",
                "<redacted>",
                "--use-device-code",
            ],
        )
        self.assertEqual(
            safe_log_payload({"command": command})["command"][3],
            "<redacted>",
        )


class ClaimsChallengeTests(unittest.TestCase):
    def test_parses_tenant_scoped_login_command(self) -> None:
        line = (
            'az login --tenant "00000000-0000-0000-0000-000000000000" '
            '--scope "https://management.core.windows.net//.default" '
            '--claims-challenge "encoded-claims-value"'
        )

        self.assertEqual(
            parse_claims_challenge_login(line),
            {
                "tenant": "00000000-0000-0000-0000-000000000000",
                "scope": "https://management.core.windows.net//.default",
                "claims_challenge": "encoded-claims-value",
            },
        )

    def test_builds_single_subscription_login(self) -> None:
        command = scoped_azure_login_command({
            "tenant": "00000000-0000-0000-0000-000000000000",
            "subscription": "11111111-1111-1111-1111-111111111111",
        })

        self.assertEqual(command[0:2], ["az", "login"])
        self.assertIn("--tenant", command)
        self.assertIn("--subscription", command)
        self.assertIn("--skip-subscription-discovery", command)
        self.assertIn("--use-device-code", command)

    def test_scopes_claims_retry_to_selected_subscription(self) -> None:
        command = claims_challenge_login_command(
            {
                "tenant": "00000000-0000-0000-0000-000000000000",
                "scope": "https://management.core.windows.net//.default",
                "claims_challenge": "encoded-claims-value",
            },
            {
                "tenant": "00000000-0000-0000-0000-000000000000",
                "subscription": "11111111-1111-1111-1111-111111111111",
            },
        )

        self.assertIn("--claims-challenge", command)
        self.assertIn("--subscription", command)
        self.assertIn("--skip-subscription-discovery", command)
        self.assertIn("--use-device-code", command)

    @patch("app.main.azure_cli_management_authenticated", return_value=False)
    @patch("app.main.wait_for_management_authentication", return_value=False)
    @patch("app.main.run_capture")
    @patch("app.main.run_process")
    @patch("app.main.cached_azure_context")
    def test_explains_conditional_access_retry(
        self,
        cached_azure_context,
        run_process,
        run_capture,
        wait_for_management_authentication,
        azure_cli_management_authenticated,
    ) -> None:
        cached_azure_context.side_effect = [
            None,
            {
                "tenant": "00000000-0000-0000-0000-000000000000",
                "subscription": "11111111-1111-1111-1111-111111111111",
            },
        ]

        def login(job, _command, **kwargs):
            interceptor = kwargs.get("line_interceptor")
            if interceptor:
                interceptor(
                    'az login --tenant "00000000-0000-0000-0000-000000000000" '
                    '--scope "https://management.core.windows.net//.default" '
                    '--claims-challenge "encoded-claims-value"'
                )
                return False, ""
            return True, ""

        run_process.side_effect = login
        run_capture.return_value = True, ""
        job = Job(["az", "login"])

        azure_login_worker(job)

        phases = [
            event
            for event in job.events.queue
            if event["type"] == "auth_phase"
        ]
        self.assertEqual(len(phases), 1)
        self.assertIn("one additional device-code sign-in", phases[0]["message"])
        retry_command = run_process.call_args_list[-1].args[1]
        self.assertIn("--skip-subscription-discovery", retry_command)

    @patch("app.main.azure_cli_management_authenticated", return_value=False)
    @patch("app.main.wait_for_management_authentication", return_value=True)
    @patch("app.main.run_capture")
    @patch("app.main.run_process")
    @patch("app.main.cached_azure_context")
    def test_skips_retry_when_initial_management_token_becomes_ready(
        self,
        cached_azure_context,
        run_process,
        run_capture,
        wait_for_management_authentication,
        azure_cli_management_authenticated,
    ) -> None:
        cached_azure_context.side_effect = [
            None,
            {
                "tenant": "00000000-0000-0000-0000-000000000000",
                "subscription": "11111111-1111-1111-1111-111111111111",
            },
        ]

        def login(_job, _command, **kwargs):
            interceptor = kwargs.get("line_interceptor")
            if interceptor:
                interceptor("AADSTS50076: Additional authentication is required.")
            return True, ""

        run_process.side_effect = login
        run_capture.return_value = True, ""
        job = Job(["az", "login"])

        azure_login_worker(job)

        self.assertEqual(run_process.call_count, 1)
        done = next(event for event in job.events.queue if event["type"] == "done")
        self.assertTrue(done["success"])

    @patch("app.main.run_capture")
    def test_verifies_management_token_with_short_timeout(self, run_capture) -> None:
        run_capture.return_value = True, ""

        self.assertTrue(azure_cli_management_authenticated())

        command = run_capture.call_args.args[0]
        self.assertIn("get-access-token", command)
        self.assertEqual(run_capture.call_args.kwargs["timeout"], 10)

    @patch("app.main.azure_cli_management_authenticated", return_value=False)
    @patch("app.main.run_capture")
    @patch("app.main.run_process")
    @patch("app.main.cached_azure_context")
    def test_clears_stale_context_before_account_selection(
        self,
        cached_azure_context,
        run_process,
        run_capture,
        azure_cli_management_authenticated,
    ) -> None:
        cached_azure_context.return_value = {
            "tenant": "00000000-0000-0000-0000-000000000000",
            "subscription": "11111111-1111-1111-1111-111111111111",
        }
        run_capture.return_value = True, ""
        run_process.return_value = False, ""
        command = ["az", "login", "--use-device-code"]

        azure_login_worker(Job(command))

        self.assertEqual(run_capture.call_args_list[0].args[0], ["az", "logout"])
        self.assertEqual(run_process.call_args_list[0].args[1], command)


class AzureContextTests(unittest.TestCase):
    def test_groups_tenants_and_sorts_default_subscription_first(self) -> None:
        accounts = [
            {
                "id": SUBSCRIPTION_A,
                "name": "Alpha",
                "tenantId": TENANT_A,
                "tenantDisplayName": "Tenant A",
                "isDefault": False,
                "state": "Enabled",
            },
            {
                "id": SUBSCRIPTION_C,
                "name": "Charlie",
                "tenantId": TENANT_B,
                "tenantDisplayName": "Tenant B",
                "isDefault": False,
                "state": "Enabled",
            },
            {
                "id": SUBSCRIPTION_B,
                "name": "Beta",
                "tenantId": TENANT_B,
                "tenantDisplayName": "Tenant B",
                "isDefault": True,
                "state": "Enabled",
            },
        ]

        catalog = build_azure_context_catalog(
            accounts,
            {"tenant": TENANT_B, "subscription": SUBSCRIPTION_B},
        )

        self.assertEqual(
            [tenant["id"] for tenant in catalog["tenants"]],
            [TENANT_B, TENANT_A],
        )
        self.assertEqual(
            [
                subscription["id"]
                for subscription in catalog["tenants"][0]["subscriptions"]
            ],
            [SUBSCRIPTION_B, SUBSCRIPTION_C],
        )
        self.assertTrue(
            catalog["tenants"][0]["subscriptions"][0]["is_default"]
        )

    def test_ignores_invalid_and_duplicate_subscription_records(self) -> None:
        valid = {
            "id": SUBSCRIPTION_A,
            "name": "Alpha",
            "tenantId": TENANT_A,
            "tenantDefaultDomain": "tenant.example",
            "isDefault": True,
        }

        catalog = build_azure_context_catalog([
            valid,
            valid.copy(),
            {"id": "not-a-guid", "tenantId": TENANT_A},
            "unexpected",
        ])

        self.assertEqual(len(catalog["tenants"]), 1)
        self.assertEqual(len(catalog["tenants"][0]["subscriptions"]), 1)
        self.assertEqual(catalog["tenants"][0]["name"], "tenant.example")
        self.assertTrue(
            azure_context_is_available(catalog, TENANT_A, SUBSCRIPTION_A)
        )
        self.assertFalse(
            azure_context_is_available(catalog, TENANT_B, SUBSCRIPTION_A)
        )

    @patch("app.main.azure_cli_management_authenticated", return_value=True)
    @patch("app.main.cached_azure_context")
    @patch("app.main.run_capture")
    @patch("app.main.azure_context_catalog")
    def test_activates_and_verifies_selected_context(
        self,
        azure_context_catalog,
        run_capture,
        cached_azure_context,
        azure_cli_management_authenticated,
    ) -> None:
        catalog = build_azure_context_catalog([{
            "id": SUBSCRIPTION_A,
            "name": "Alpha",
            "tenantId": TENANT_A,
            "isDefault": True,
        }])
        azure_context_catalog.return_value = catalog, ""
        run_capture.return_value = True, ""
        cached_azure_context.return_value = {
            "tenant": TENANT_A,
            "subscription": SUBSCRIPTION_A,
        }

        success, error, requires_auth, active = activate_azure_context(
            TENANT_A,
            SUBSCRIPTION_A,
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertFalse(requires_auth)
        self.assertEqual(active["subscription"], SUBSCRIPTION_A)
        self.assertEqual(
            run_capture.call_args.args[0],
            ["az", "account", "set", "--subscription", SUBSCRIPTION_A],
        )

    @patch("app.main.azure_cli_management_authenticated", return_value=False)
    @patch("app.main.cached_azure_context")
    @patch("app.main.run_capture")
    @patch("app.main.azure_context_catalog")
    def test_requests_scoped_auth_when_selected_tenant_needs_token(
        self,
        azure_context_catalog,
        run_capture,
        cached_azure_context,
        azure_cli_management_authenticated,
    ) -> None:
        catalog = build_azure_context_catalog([{
            "id": SUBSCRIPTION_A,
            "name": "Alpha",
            "tenantId": TENANT_A,
            "isDefault": True,
        }])
        azure_context_catalog.return_value = catalog, ""
        run_capture.return_value = True, ""
        cached_azure_context.return_value = {
            "tenant": TENANT_A,
            "subscription": SUBSCRIPTION_A,
        }

        success, error, requires_auth, active = activate_azure_context(
            TENANT_A,
            SUBSCRIPTION_A,
        )

        self.assertFalse(success)
        self.assertIn("additional device-code sign-in", error)
        self.assertTrue(requires_auth)
        self.assertEqual(active["tenant"], TENANT_A)

    @patch("app.main.azure_context_catalog")
    def test_rejects_invalid_context_ids_before_discovery(
        self,
        azure_context_catalog,
    ) -> None:
        success, error, requires_auth, active = activate_azure_context(
            "not-a-tenant",
            SUBSCRIPTION_A,
        )

        self.assertFalse(success)
        self.assertIn("valid GUIDs", error)
        self.assertFalse(requires_auth)
        self.assertIsNone(active)
        azure_context_catalog.assert_not_called()

    def test_scopes_noninteractive_azd_login_to_selected_tenant(self) -> None:
        command = azd_login_command({
            "tenant": TENANT_A,
            "subscription": SUBSCRIPTION_A,
        })

        self.assertEqual(command[0:3], ["azd", "auth", "login"])
        self.assertIn("--use-device-code", command)
        self.assertIn("--no-prompt", command)
        self.assertEqual(command[command.index("--tenant-id") + 1], TENANT_A)


class PrerequisiteTests(unittest.TestCase):
    @patch("app.main.refresh_process_path")
    @patch("app.main.command_version")
    def test_reports_each_configured_tool(
        self,
        command_version,
        refresh_process_path,
    ) -> None:
        command_version.return_value = "1.2.3"
        statuses = prerequisite_statuses()
        refresh_process_path.assert_called_once_with()
        self.assertEqual([item.id for item in statuses], ["winget", "az", "azd", "git"])
        self.assertTrue(all(item.installed for item in statuses))
        self.assertFalse(statuses[0].required)
        self.assertTrue(all(item.required for item in statuses[1:]))
        for item in statuses[1:]:
            self.assertIn("--source winget", item.install_command)
            self.assertIn("--accept-source-agreements", item.install_command)
            self.assertIn("--accept-package-agreements", item.install_command)

    def test_install_commands_are_allowlisted_and_noninteractive(self) -> None:
        self.assertEqual(set(INSTALL_COMMANDS), {"winget", "az", "azd", "git"})
        self.assertEqual(INSTALL_ORDER, ("winget", "az", "azd", "git"))
        for tool_id in ("az", "azd", "git"):
            command = INSTALL_COMMANDS[tool_id]
            self.assertIn("--source", command)
            self.assertIn("winget", command)
            self.assertIn("--disable-interactivity", command)
        git_command = INSTALL_COMMANDS["git"]
        self.assertIn("--silent", git_command)
        self.assertEqual(
            git_command[git_command.index("--scope") + 1],
            "user",
        )

    @patch("app.main.run_process")
    @patch("app.main.prerequisite_statuses")
    def test_install_all_runs_missing_tools_sequentially(
        self,
        prerequisite_statuses,
        run_process,
    ) -> None:
        installed = set()

        def statuses():
            return [
                ToolStatus(
                    id=tool_id,
                    name=tool_id,
                    installed=tool_id in installed,
                    version="1.0" if tool_id in installed else None,
                    install_command="",
                    install_url="",
                    required=tool_id != "winget",
                )
                for tool_id in INSTALL_ORDER
            ]

        def install(_job, command):
            installed.add(next(
                tool_id
                for tool_id, configured_command in INSTALL_COMMANDS.items()
                if configured_command == command
            ))
            return True, ""

        prerequisite_statuses.side_effect = statuses
        run_process.side_effect = install
        job = Job()

        install_all_worker(job)

        self.assertEqual(
            [call.args[1] for call in run_process.call_args_list],
            [INSTALL_COMMANDS[tool_id] for tool_id in INSTALL_ORDER],
        )
        events = list(job.events.queue)
        tool_events = [
            event
            for event in events
            if event["type"] == "tool_status"
        ]
        self.assertEqual(
            [
                (event["tool_id"], event["status"])
                for event in tool_events
            ],
            [
                pair
                for tool_id in INSTALL_ORDER
                for pair in ((tool_id, "installing"), (tool_id, "ready"))
            ],
        )
        self.assertTrue(events[-1]["success"])

    @patch("app.main.subprocess.run")
    @patch("app.main.shutil.which")
    def test_runs_resolved_windows_command(self, which, run) -> None:
        which.return_value = r"C:\Tools\az.cmd"
        run.return_value = MagicMock(stdout='{"azure-cli":"2.89.1"}', stderr="")
        self.assertEqual(command_version("az", ("version",)), "2.89.1")
        self.assertEqual(run.call_args.args[0][0], r"C:\Tools\az.cmd")


class ProcessTests(unittest.TestCase):
    @patch.dict("os.environ", {"AZURE_SRE_DEMO_NO_BROWSER": "true"})
    def test_can_disable_automatic_browser_launch(self) -> None:
        self.assertFalse(should_open_browser())

    @patch.dict("os.environ", {}, clear=True)
    def test_opens_browser_by_default(self) -> None:
        self.assertTrue(should_open_browser())

    @patch("app.main.open_browser_url", return_value=True)
    @patch("app.main.subprocess.Popen")
    @patch("app.main.resolved_process_command")
    def test_device_code_event_opens_browser_from_backend(
        self,
        resolved_process_command,
        popen,
        open_browser_url,
    ) -> None:
        resolved_process_command.return_value = ["az", "login", "--use-device-code"]
        process = popen.return_value
        process.pid = 1234
        process.stdout = [
            "Open https://login.microsoft.com/device and enter the code ABCD-EFGH\n"
        ]
        process.wait.return_value = 0
        job = Job()

        success, _ = run_process(job, ["az", "login", "--use-device-code"])

        self.assertTrue(success)
        open_browser_url.assert_called_once_with("https://login.microsoft.com/device")
        events = list(job.events.queue)
        device_event = next(event for event in events if event["type"] == "device_code")
        self.assertEqual(device_event["code"], "ABCD-EFGH")
        self.assertTrue(device_event["browser_opened"])

    @patch("app.main.open_browser_url", return_value=True)
    @patch("app.main.subprocess.Popen")
    @patch("app.main.resolved_process_command")
    def test_azd_code_event_opens_browser_without_terminal_input(
        self,
        resolved_process_command,
        popen,
        open_browser_url,
    ) -> None:
        resolved_process_command.return_value = azd_login_command()
        process = popen.return_value
        process.pid = 1234
        process.stdout = ["Start by copying the next code: FD9EAW26Z\n"]
        process.wait.return_value = 0
        job = Job()

        success, _ = run_process(job, azd_login_command())

        self.assertTrue(success)
        open_browser_url.assert_called_once_with(
            "https://microsoft.com/devicelogin"
        )
        device_event = next(
            event
            for event in job.events.queue
            if event["type"] == "device_code"
        )
        self.assertEqual(device_event["code"], "FD9EAW26Z")

    @patch("app.main.Path.is_file")
    @patch("app.main.shutil.which")
    def test_resolves_azure_cli_to_its_python_process(self, which, is_file) -> None:
        which.return_value = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
        is_file.return_value = True

        command = resolved_process_command(["az", "login"])

        self.assertIsNotNone(command)
        assert command is not None
        self.assertTrue(command[0].endswith(r"CLI2\python.exe"))
        self.assertEqual(
            command[1:],
            ["-I", "-B", "-u", "-m", "azure.cli", "login"],
        )

    @patch("app.main.subprocess.run")
    @patch("app.main.resolved_process_command")
    def test_capture_disables_windows_broker_for_status_checks(
        self,
        resolved_process_command,
        run,
    ) -> None:
        resolved_process_command.return_value = [
            r"C:\AzureCLI\python.exe",
            "-IBm",
            "azure.cli",
            "account",
            "get-access-token",
        ]
        run.return_value = MagicMock(returncode=1, stdout="", stderr="not logged in")

        success, _ = run_capture(["az", "account", "get-access-token"])

        self.assertFalse(success)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"], "false")

    @patch("app.main.subprocess.Popen")
    @patch("app.main.shutil.which")
    def test_azure_login_disables_windows_broker(self, which, popen) -> None:
        which.return_value = r"C:\Tools\az.cmd"
        popen.return_value.stdout = []
        popen.return_value.wait.return_value = 0

        success, _ = run_process(Job(), ["az", "login"])

        self.assertTrue(success)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"], "false")

    @patch("app.main.run_capture")
    @patch("app.main.azure_cli_management_authenticated")
    def test_reports_cached_authentication_status(
        self,
        azure_cli_management_authenticated,
        run_capture,
    ) -> None:
        azure_cli_management_authenticated.return_value = True
        run_capture.return_value = True, '{"status":"unauthenticated"}'

        statuses = authentication_statuses()

        self.assertEqual(statuses, {"azure-cli": True, "azd": False})

    @patch("app.main.run_capture")
    @patch("app.main.azure_cli_management_authenticated")
    def test_reports_valid_azd_authentication_status(
        self,
        azure_cli_management_authenticated,
        run_capture,
    ) -> None:
        azure_cli_management_authenticated.return_value = False
        run_capture.return_value = (
            True,
            '{"status":"success","expiresOn":"2026-08-31T03:24:40Z"}',
        )

        statuses = authentication_statuses()

        self.assertEqual(statuses, {"azure-cli": False, "azd": True})

    @patch("app.main.subprocess.Popen")
    @patch("app.main.find_edge")
    @patch("app.main.is_windows_sandbox")
    def test_opens_edge_directly_in_windows_sandbox(
        self,
        is_windows_sandbox,
        find_edge,
        popen,
    ) -> None:
        is_windows_sandbox.return_value = True
        find_edge.return_value = MagicMock(__str__=lambda _: r"C:\Edge\msedge.exe")

        self.assertTrue(open_browser_url("http://127.0.0.1:8765"))
        popen.assert_called_once()


class RequestAuthenticationTests(unittest.TestCase):
    @patch("app.main.urlopen")
    def test_http_json_uses_bearer_token(self, urlopen) -> None:
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"{}"
        urlopen.return_value.__enter__.return_value = response

        status, _ = http_json("GET", "https://example.test/api", "token-value")

        self.assertEqual(status, 200)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-value")


class ResponsePlanTests(unittest.TestCase):
    def test_payload_uses_current_incident_filter_schema(self) -> None:
        payload = response_plan_payload(
            "sre-lab-auto-2",
            SUBSCRIPTION_A,
            "rg-sre-lab-auto-2",
        )

        self.assertEqual(payload["handlingAgent"], "incident-handler")
        self.assertEqual(payload["agentMode"], "autonomous")
        self.assertEqual(
            payload["alertId"],
            (
                f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-sre-lab-auto-2"
                "/providers/Microsoft.Insights/metricAlerts/"
                "alert-http-5xx-sre-lab-auto-2"
            ),
        )
        self.assertEqual(payload["titleContains"], "")
        self.assertEqual(payload["titleNotContains"], [])
        self.assertNotIn("maxAttempts", payload)

    @patch("app.main.http_json")
    def test_updates_an_existing_response_plan(self, http_json) -> None:
        http_json.side_effect = [(409, "already exists"), (200, "updated")]
        payload = response_plan_payload(
            "sre-lab-auto-2",
            SUBSCRIPTION_A,
            "rg-sre-lab-auto-2",
        )

        status, response = upsert_response_plan(
            "https://example.test/filters/grubify-http-errors",
            "token-value",
            payload,
        )

        self.assertEqual((status, response), (200, "updated"))
        self.assertEqual(
            [call.args[0] for call in http_json.call_args_list],
            ["PUT", "POST"],
        )

    def test_retries_only_transient_response_plan_failures(self) -> None:
        for status in (0, 404, 405, 408, 425, 429, 500, 502, 503, 504):
            self.assertTrue(response_plan_status_is_retryable(status))
        for status in (400, 401, 403, 422):
            self.assertFalse(response_plan_status_is_retryable(status))


class DeploymentRecoveryTests(unittest.TestCase):
    @patch("app.main.save_state")
    @patch("app.main.post_provision", return_value=True)
    @patch("app.main.restore_container_baseline", return_value=True)
    @patch("app.main.run_process", return_value=(True, ""))
    @patch("app.main.load_state")
    def test_restore_reapplies_declared_baseline_and_post_provisioning(
        self,
        load_state,
        run_process,
        restore_container_baseline,
        post_provision,
        save_state,
    ) -> None:
        state = {"environment": "sre-lab", "deployment_active": True}
        load_state.return_value = state
        job = Job()

        restore_baseline_worker(job)

        commands = [call.args[1] for call in run_process.call_args_list]
        self.assertEqual(
            commands,
            [
                [
                    "az", "provider", "register",
                    "--namespace", "Microsoft.App",
                    "--wait", "--output", "none",
                ],
                [
                    "azd", "provision", "--preview",
                    "-e", "sre-lab", "--no-prompt",
                ],
                ["azd", "up", "-e", "sre-lab", "--no-prompt"],
            ],
        )
        restore_container_baseline.assert_called_once_with(job, "sre-lab")
        post_provision.assert_called_once_with(job, "sre-lab")
        save_state.assert_called_once_with({
            "environment": "sre-lab",
            "deployment_active": True,
        })
        done = list(job.events.queue)[-1]
        self.assertTrue(done["success"])

    @patch("app.main.run_process", return_value=(True, ""))
    @patch("app.main.azd_values")
    def test_restore_container_baseline_enforces_bicep_resources(
        self,
        azd_values,
        run_process,
    ) -> None:
        azd_values.return_value = {
            "AZURE_RESOURCE_GROUP": "rg-sre-lab",
            "CONTAINER_APP_NAME": "api-app",
            "FRONTEND_APP_NAME": "frontend-app",
        }
        job = Job()

        self.assertTrue(restore_container_baseline(job, "sre-lab"))

        self.assertEqual(
            [call.args[1] for call in run_process.call_args_list],
            [
                [
                    "az", "containerapp", "update",
                    "--name", "api-app",
                    "--resource-group", "rg-sre-lab",
                    "--cpu", "0.5",
                    "--memory", "1Gi",
                    "--output", "none",
                ],
                [
                    "az", "containerapp", "update",
                    "--name", "frontend-app",
                    "--resource-group", "rg-sre-lab",
                    "--cpu", "0.25",
                    "--memory", "0.5Gi",
                    "--output", "none",
                ],
            ],
        )

    @patch("app.main.save_state")
    @patch("app.main.post_provision", return_value=True)
    @patch("app.main.run_process", return_value=(True, ""))
    @patch("app.main.load_state")
    def test_deploy_marks_demo_active(
        self,
        load_state,
        run_process,
        post_provision,
        save_state,
    ) -> None:
        load_state.return_value = {
            "environment": "sre-lab",
            "deployment_active": False,
        }

        deploy_worker(Job())

        self.assertTrue(save_state.call_args.args[0]["deployment_active"])

    @patch("app.main.save_state")
    @patch("app.main.run_process", return_value=(True, ""))
    @patch("app.main.load_state")
    def test_teardown_marks_demo_inactive(
        self,
        load_state,
        run_process,
        save_state,
    ) -> None:
        load_state.return_value = {
            "environment": "sre-lab",
            "deployment_active": True,
        }
        job = Job()

        teardown_worker(job)

        self.assertFalse(save_state.call_args.args[0]["deployment_active"])
        done = list(job.events.queue)[-1]
        self.assertTrue(done["success"])

    @patch("app.main.save_state")
    @patch("app.main.run_process", return_value=(False, "failed"))
    @patch("app.main.load_state")
    def test_failed_teardown_keeps_demo_active(
        self,
        load_state,
        run_process,
        save_state,
    ) -> None:
        load_state.return_value = {
            "environment": "sre-lab",
            "deployment_active": True,
        }
        job = Job()

        teardown_worker(job)

        save_state.assert_not_called()
        done = list(job.events.queue)[-1]
        self.assertFalse(done["success"])


class BreakCartTests(unittest.TestCase):
    def test_memory_pressure_accepts_sustained_service_failure(self) -> None:
        self.assertTrue(memory_pressure_observed(73, 127))

    def test_memory_pressure_rejects_insufficient_or_transient_failures(self) -> None:
        self.assertFalse(memory_pressure_observed(49, 127))
        self.assertFalse(memory_pressure_observed(73, 19))

    def test_memory_pressure_accepts_allocation_threshold(self) -> None:
        self.assertTrue(memory_pressure_observed(75, 0))

    def test_detects_request_metric_data(self) -> None:
        self.assertTrue(request_metrics_have_data(
            '{"value":[{"timeseries":[{"data":[{"total":0},{"total":1}]}]}]}'
        ))
        self.assertFalse(request_metrics_have_data(
            '{"value":[{"timeseries":[{"data":[{"total":0}]}]}]}'
        ))
        self.assertFalse(request_metrics_have_data("not-json"))

    @patch("app.main.time.sleep")
    @patch("app.main.urlopen")
    @patch("app.main.run_capture")
    def test_waits_for_a_probe_to_appear_in_request_metrics(
        self,
        run_capture,
        urlopen,
        _sleep,
    ) -> None:
        empty = '{"value":[{"timeseries":[{"data":[{"total":0}]}]}]}'
        ready = '{"value":[{"timeseries":[{"data":[{"total":1}]}]}]}'
        run_capture.side_effect = [(True, empty), (True, ready)]
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response

        self.assertTrue(wait_for_request_metrics(
            Job(),
            "https://example.test",
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/api",
            attempts=1,
            delay_seconds=0,
        ))

    @patch("app.main.time.sleep")
    @patch("app.main.urlopen")
    @patch("app.main.wait_for_request_metrics", return_value=True)
    @patch("app.main.azd_values")
    @patch("app.main.load_state")
    def test_break_cart_succeeds_when_service_stops_after_allocations(
        self,
        load_state,
        azd_values,
        _wait_for_request_metrics,
        urlopen,
        _sleep,
    ) -> None:
        load_state.return_value = {
            "environment": "sre-lab",
            "subscription_id": "sub",
        }
        azd_values.return_value = {
            "CONTAINER_APP_URL": "https://example.test",
            "AZURE_RESOURCE_GROUP": "rg-sre-lab",
            "CONTAINER_APP_NAME": "api-app",
        }
        successful_response = MagicMock()
        successful_response.__enter__.return_value.status = 201
        urlopen.side_effect = (
            [successful_response] * 73
            + [TimeoutError()] * 127
        )
        job = Job()

        break_cart_worker(job)

        events = list(job.events.queue)
        self.assertFalse(any(event["type"] == "error" for event in events))
        self.assertIn(
            "Memory pressure observed",
            "\n".join(
                event["line"]
                for event in events
                if event["type"] == "output"
            ),
        )
        done = events[-1]
        self.assertTrue(done["success"])
        self.assertEqual(done["successes"], 73)
        self.assertEqual(done["errors"], 127)
        self.assertEqual(done["max_consecutive_service_failures"], 127)


class RegionConfigurationTests(unittest.TestCase):
    EXPECTED_REGIONS = frozenset({
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

    def test_regions_match_microsoft_learn_supported_list(self) -> None:
        self.assertEqual(SRE_AGENT_REGIONS, self.EXPECTED_REGIONS)

    def test_region_dropdown_matches_backend_validation(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        select = re.search(
            r'<select id="azure-location">(.*?)</select>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(select)
        options = frozenset(re.findall(r'<option value="([^"]+)"', select.group(1)))
        self.assertEqual(options, SRE_AGENT_REGIONS)

    def test_bicep_regions_match_backend_validation(self) -> None:
        bicep = (VENDOR_DIR / "infra" / "main.bicep").read_text(encoding="utf-8")
        allowed = re.search(
            r"@allowed\(\[(.*?)\]\)\s*param location",
            bicep,
            re.DOTALL,
        )
        self.assertIsNotNone(allowed)
        locations = frozenset(re.findall(r"'([^']+)'", allowed.group(1)))
        self.assertEqual(locations, SRE_AGENT_REGIONS)


if __name__ == "__main__":
    unittest.main()
