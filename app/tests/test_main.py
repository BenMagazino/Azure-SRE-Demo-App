import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    INSTALL_COMMANDS,
    INSTALL_ORDER,
    Job,
    ToolStatus,
    authentication_statuses,
    command_version,
    http_json,
    install_all_worker,
    is_device_login_url,
    open_browser_url,
    parse_claims_challenge_login,
    parse_device_code,
    prerequisite_statuses,
    run_process,
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


class PrerequisiteTests(unittest.TestCase):
    @patch("app.main.command_version")
    def test_reports_each_configured_tool(self, command_version) -> None:
        command_version.return_value = "1.2.3"
        statuses = prerequisite_statuses()
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

    @patch("app.main.refresh_process_path")
    @patch("app.main.run_process")
    @patch("app.main.prerequisite_statuses")
    def test_install_all_runs_missing_tools_sequentially(
        self,
        prerequisite_statuses,
        run_process,
        refresh_process_path,
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
        self.assertEqual(refresh_process_path.call_count, len(INSTALL_ORDER))
        events = list(job.events.queue)
        self.assertTrue(events[-1]["success"])

    @patch("app.main.subprocess.run")
    @patch("app.main.shutil.which")
    def test_runs_resolved_windows_command(self, which, run) -> None:
        which.return_value = r"C:\Tools\az.cmd"
        run.return_value = MagicMock(stdout='{"azure-cli":"2.89.1"}', stderr="")
        self.assertEqual(command_version("az", ("version",)), "2.89.1")
        self.assertEqual(run.call_args.args[0][0], r"C:\Tools\az.cmd")


class ProcessTests(unittest.TestCase):
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
    def test_reports_cached_authentication_status(self, run_capture) -> None:
        run_capture.side_effect = [(True, ""), (False, "not logged in")]

        statuses = authentication_statuses()

        self.assertEqual(statuses, {"azure-cli": True, "azd": False})

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
