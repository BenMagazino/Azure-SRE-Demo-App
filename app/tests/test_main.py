import unittest
from unittest.mock import MagicMock, patch

from app.main import command_version, http_json, parse_device_code, prerequisite_statuses


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
