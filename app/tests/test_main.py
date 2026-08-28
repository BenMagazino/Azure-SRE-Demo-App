import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    Job,
    authentication_statuses,
    command_version,
    http_json,
    is_device_login_url,
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
        self.assertEqual([item.id for item in statuses], ["az", "azd", "git"])
        self.assertTrue(all(item.installed for item in statuses))

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
