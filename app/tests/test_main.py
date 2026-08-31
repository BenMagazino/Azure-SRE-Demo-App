import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.main import (
    INSTALL_COMMANDS,
    INSTALL_ORDER,
    Job,
    ToolStatus,
    authentication_statuses,
    azure_cli_management_authenticated,
    azure_login_worker,
    claims_challenge_login_command,
    command_version,
    http_json,
    install_all_worker,
    is_device_login_url,
    open_browser_url,
    parse_claims_challenge_login,
    parse_device_code,
    prerequisite_statuses,
    redact_command,
    redact_text,
    resolved_process_command,
    run_capture,
    run_process,
    safe_log_payload,
    scoped_azure_login_command,
)


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
    @patch("app.main.run_capture")
    @patch("app.main.run_process")
    @patch("app.main.cached_azure_context")
    def test_explains_conditional_access_retry(
        self,
        cached_azure_context,
        run_process,
        run_capture,
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
        self.assertEqual(request.get_header("Authorization"), "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
