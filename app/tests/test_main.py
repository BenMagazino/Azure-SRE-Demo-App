import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.main import (
    AppHandler,
    INSTALL_COMMANDS,
    INSTALL_ORDER,
    LABS,
    MINIMUM_VERSIONS,
    REPAIR_COMMANDS,
    UPDATE_COMMANDS,
    Job,
    SESSION_TOKEN,
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
    build_existing_environment_catalog,
    build_azure_context_catalog,
    client_lease_expired,
    claims_challenge_login_command,
    command_version,
    deploy_worker,
    discover_existing_environments,
    http_json,
    install_all_worker,
    install_managed_azure_cli,
    is_device_login_url,
    launch_client_if_unclaimed,
    memory_pressure_observed,
    monitor_client_lease,
    open_browser_url,
    parse_claims_challenge_login,
    parse_device_code,
    lab_catalog_payload,
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
    run_tool_install,
    safe_extract_zip,
    safe_log_payload,
    scoped_azure_login_command,
    should_open_browser,
    should_fallback_open_client,
    shutdown_application,
    teardown_worker,
    upsert_response_plan,
    wait_for_request_metrics,
    version_meets_minimum,
)


TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "00000000-0000-0000-0000-000000000002"
SUBSCRIPTION_A = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_B = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_C = "33333333-3333-3333-3333-333333333333"


class LabWorkflowTests(unittest.TestCase):
    def test_catalog_exposes_grubify_memory_leak_workflow(self) -> None:
        payload = lab_catalog_payload({"lab_id": "grubify-starter-lab"})

        self.assertEqual(len(LABS), 1)
        self.assertEqual(payload["selected_lab_id"], "grubify-starter-lab")
        self.assertEqual(len(payload["labs"]), 1)
        self.assertEqual(payload["labs"][0]["dependency_ids"], ("winget", "az", "azd"))
        self.assertEqual(payload["labs"][0]["scenarios"][0]["id"], "memory-leak")

    @patch("app.main.save_state")
    @patch("app.main.load_state")
    def test_selecting_lab_migrates_legacy_deployment_state(
        self,
        load_state,
        save_state,
    ) -> None:
        load_state.return_value = {
            "environment": "existing-lab",
            "deployment_active": True,
        }
        handler = object.__new__(AppHandler)
        handler.read_json = MagicMock(
            return_value={"lab_id": "grubify-starter-lab"}
        )
        handler.send_json = MagicMock()

        handler.select_lab()

        state = save_state.call_args.args[0]
        self.assertEqual(state["lab_id"], "grubify-starter-lab")
        self.assertEqual(state["environment"], "existing-lab")
        self.assertTrue(state["deployment_active"])

    @patch("app.main.save_state")
    @patch("app.main.run_capture", return_value=(True, ""))
    @patch("app.main.azure_cli_management_authenticated", return_value=True)
    @patch("app.main.cached_azure_context")
    @patch("app.main.load_state")
    def test_configuration_preserves_selected_lab(
        self,
        load_state,
        cached_azure_context,
        _management_authenticated,
        _run_capture,
        save_state,
    ) -> None:
        load_state.return_value = {
            "lab_id": "grubify-starter-lab",
            "scenario_id": "memory-leak",
        }
        cached_azure_context.return_value = {
            "tenant": TENANT_A,
            "subscription": SUBSCRIPTION_A,
        }
        handler = object.__new__(AppHandler)
        handler.read_json = MagicMock(return_value={
            "environment": "sre-lab",
            "location": "eastus2",
        })
        handler.send_json = MagicMock()

        handler.configure_environment()

        state = save_state.call_args.args[0]
        self.assertEqual(state["lab_id"], "grubify-starter-lab")
        self.assertNotIn("scenario_id", state)
        self.assertEqual(state["environment"], "sre-lab")

    @patch("app.main.create_job")
    @patch("app.main.save_state")
    @patch("app.main.load_state")
    def test_scenario_route_dispatches_selected_lab_worker(
        self,
        load_state,
        save_state,
        create_job,
    ) -> None:
        state = {
            "lab_id": "grubify-starter-lab",
            "deployment_active": True,
        }
        load_state.return_value = state
        create_job.return_value.id = "job-1"
        handler = object.__new__(AppHandler)
        handler.read_json = MagicMock(return_value={"scenario_id": "memory-leak"})
        handler.send_json = MagicMock()

        handler.run_scenario()

        self.assertEqual(
            create_job.call_args.kwargs["worker"],
            break_cart_worker,
        )
        self.assertEqual(save_state.call_args.args[0]["scenario_id"], "memory-leak")
        self.assertEqual(handler.send_json.call_args.args[0]["job_id"], "job-1")

    def test_builds_tagged_and_legacy_environment_catalog(self) -> None:
        groups = [
            {
                "name": "rg-tagged-lab",
                "location": "eastus2",
                "tags": {
                    "azure-sre-agent-lab-id": "grubify-starter-lab",
                    "azure-sre-agent-environment": "tagged-lab",
                },
            },
            {
                "name": "rg-legacy-lab",
                "location": "swedencentral",
                "tags": {},
            },
            {
                "name": "rg-unrelated",
                "location": "eastus2",
                "tags": {},
            },
            {
                "name": "rg-incomplete",
                "location": "eastus2",
                "tags": {},
            },
        ]
        agents = [
            {"name": "sre-agent-a", "resourceGroup": "rg-legacy-lab"},
            {"name": "sre-agent-b", "resourceGroup": "rg-unrelated"},
            {"name": "sre-agent-c", "resourceGroup": "rg-incomplete"},
        ]
        container_apps = [
            {"name": "ca-grubify-abc", "resourceGroup": "rg-legacy-lab"},
            {"name": "ca-grubify-fe-abc", "resourceGroup": "rg-legacy-lab"},
            {"name": "some-app", "resourceGroup": "rg-unrelated"},
            {"name": "ca-grubify-fe-only", "resourceGroup": "rg-incomplete"},
        ]

        environments = build_existing_environment_catalog(
            groups,
            agents,
            container_apps,
            {"tagged-lab"},
            "grubify-starter-lab",
        )

        self.assertEqual(
            [
                (
                    item["environment"],
                    item["detection"],
                    item["local"],
                )
                for item in environments
            ],
            [
                ("tagged-lab", "managed", True),
                ("legacy-lab", "legacy", False),
            ],
        )

    @patch("app.main.load_environment_cache")
    @patch("app.main.run_capture", return_value=(False, "unavailable"))
    @patch("app.main.local_azd_environment_names", return_value={"cached-lab"})
    def test_uses_offline_environment_cache_when_azure_is_unavailable(
        self,
        _local_names,
        _run_capture,
        load_environment_cache,
    ) -> None:
        load_environment_cache.return_value = [{
            "environment": "cached-lab",
            "resource_group": "rg-cached-lab",
            "location": "eastus2",
            "detection": "managed",
            "local": False,
        }]

        result = discover_existing_environments(
            SUBSCRIPTION_A,
            "grubify-starter-lab",
        )

        self.assertEqual(result["source"], "cache")
        self.assertTrue(result["stale"])
        self.assertTrue(result["environments"][0]["local"])

    def test_resource_group_has_stable_discovery_tags(self) -> None:
        bicep = (VENDOR_DIR / "infra" / "main.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "'azure-sre-agent-lab-id': 'grubify-starter-lab'",
            bicep,
        )
        self.assertIn(
            "'azure-sre-agent-environment': environmentName",
            bicep,
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
    @patch("app.main.command_version", return_value="2.90.0")
    @patch("app.main.shutil.which", return_value=r"C:\Tools\az.exe")
    def test_filters_tools_for_selected_lab(
        self,
        _which,
        _command_version,
        _refresh_process_path,
    ) -> None:
        statuses = prerequisite_statuses(("az",))

        self.assertEqual([status.id for status in statuses], ["az"])

    @patch("app.main.refresh_process_path")
    @patch("app.main.command_version")
    @patch("app.main.shutil.which", return_value=r"C:\Tools\tool.exe")
    def test_reports_each_configured_tool(
        self,
        which,
        command_version,
        refresh_process_path,
    ) -> None:
        versions = {
            "winget": "1.29.290",
            "az": "2.90.0",
            "azd": "1.32.0",
        }
        command_version.side_effect = lambda executable, _args: versions[executable]
        statuses = prerequisite_statuses()
        refresh_process_path.assert_called_once_with()
        self.assertEqual([item.id for item in statuses], ["winget", "az", "azd"])
        self.assertTrue(all(item.installed for item in statuses))
        self.assertTrue(all(item.ready for item in statuses))
        self.assertTrue(all(item.state == "ready" for item in statuses))
        self.assertFalse(statuses[0].required)
        self.assertTrue(all(item.required for item in statuses[1:]))
        self.assertEqual(
            MINIMUM_VERSIONS,
            {"winget": "1.29.280", "az": "2.88.0", "azd": "1.28.0"},
        )
        which.assert_called()

    def test_install_commands_are_allowlisted_and_noninteractive(self) -> None:
        expected = {"winget", "az", "azd"}
        self.assertEqual(set(INSTALL_COMMANDS), expected)
        self.assertEqual(set(UPDATE_COMMANDS), expected)
        self.assertEqual(set(REPAIR_COMMANDS), expected)
        self.assertEqual(INSTALL_ORDER, ("winget", "az", "azd"))
        for commands in (INSTALL_COMMANDS, UPDATE_COMMANDS, REPAIR_COMMANDS):
            for tool_id in ("az", "azd"):
                command = commands[tool_id]
                self.assertIn("--source", command)
                self.assertIn("winget", command)
                self.assertIn("--disable-interactivity", command)
        self.assertEqual(UPDATE_COMMANDS["az"][1], "upgrade")
        self.assertEqual(UPDATE_COMMANDS["azd"][1], "upgrade")
        self.assertIn("--force", REPAIR_COMMANDS["az"])
        self.assertIn("--force", REPAIR_COMMANDS["azd"])

    @patch("app.main.refresh_process_path")
    @patch("app.main.command_version")
    @patch("app.main.shutil.which", return_value=r"C:\Tools\tool.exe")
    def test_marks_old_versions_outdated(
        self,
        _which,
        command_version,
        _refresh_process_path,
    ) -> None:
        command_version.side_effect = lambda executable, _args: {
            "winget": "1.29.279",
            "az": "2.87.0",
            "azd": "1.27.1",
        }[executable]

        statuses = prerequisite_statuses()

        self.assertTrue(all(item.installed for item in statuses))
        self.assertTrue(all(not item.ready for item in statuses))
        self.assertTrue(all(item.state == "outdated" for item in statuses))
        self.assertTrue(all("upgrade" in item.install_command for item in statuses[1:]))

    def test_compares_normalized_semantic_versions(self) -> None:
        self.assertTrue(version_meets_minimum("2.88.0", "2.88.0"))
        self.assertTrue(version_meets_minimum("2.90.1", "2.88.0"))
        self.assertFalse(version_meets_minimum("2.87.9", "2.88.0"))
        self.assertFalse(version_meets_minimum("installed", "2.88.0"))

    @patch("app.main.install_managed_azure_cli")
    @patch("app.main.run_process")
    @patch("app.main.prerequisite_statuses")
    def test_install_all_runs_missing_tools_sequentially(
        self,
        prerequisite_statuses,
        run_process,
        install_managed_azure_cli,
    ) -> None:
        installed = set()

        def statuses():
            return [
                ToolStatus(
                    id=tool_id,
                    name=tool_id,
                    installed=tool_id in installed,
                    version="1.0" if tool_id in installed else None,
                    minimum_version=MINIMUM_VERSIONS[tool_id],
                    ready=tool_id in installed,
                    state="ready" if tool_id in installed else "missing",
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

        def install_azure_cli(_job):
            installed.add("az")
            return True

        prerequisite_statuses.side_effect = statuses
        run_process.side_effect = install
        install_managed_azure_cli.side_effect = install_azure_cli
        job = Job()

        install_all_worker(job)

        self.assertEqual(
            [call.args[1] for call in run_process.call_args_list],
            [INSTALL_COMMANDS["winget"], INSTALL_COMMANDS["azd"]],
        )
        install_managed_azure_cli.assert_called_once_with(job)
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

    @patch("app.main.install_managed_azure_cli", return_value=True)
    @patch("app.main.run_process")
    @patch("app.main.prerequisite_statuses")
    def test_updates_an_outdated_tool(
        self,
        prerequisite_statuses,
        run_process,
        install_managed_azure_cli,
    ) -> None:
        outdated = ToolStatus(
            id="az",
            name="Azure CLI",
            installed=True,
            version="2.87.0",
            minimum_version="2.88.0",
            ready=False,
            state="outdated",
            install_command="",
            install_url="",
            required=True,
        )
        ready = ToolStatus(
            id="az",
            name="Azure CLI",
            installed=True,
            version="2.90.0",
            minimum_version="2.88.0",
            ready=True,
            state="ready",
            install_command="",
            install_url="",
            required=True,
        )
        prerequisite_statuses.side_effect = [[outdated], [ready]]
        job = Job()

        self.assertTrue(run_tool_install(job, "az"))

        run_process.assert_not_called()
        install_managed_azure_cli.assert_called_once_with(job)
        events = list(job.events.queue)
        self.assertEqual(events[0]["status"], "updating")
        self.assertEqual(events[-2]["status"], "ready")

    def test_safe_extract_zip_rejects_parent_traversal(self) -> None:
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("../outside.txt", "unsafe")
        archive_bytes.seek(0)

        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(archive_bytes) as archive:
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP entry"):
                    safe_extract_zip(archive, Path(directory))

    @patch("app.main.refresh_process_path")
    @patch("app.main.urlopen")
    def test_installs_checksum_verified_azure_cli_in_user_profile(
        self,
        urlopen,
        refresh_process_path,
    ) -> None:
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("bin/az.cmd", "@echo off")
            archive.writestr("python.exe", "runtime")
        payload = archive_bytes.getvalue()
        urlopen.return_value = io.BytesIO(payload)

        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            cli_dir = tools_dir / "azure-cli"
            with (
                patch("app.main.MANAGED_TOOLS_DIR", tools_dir),
                patch("app.main.AZURE_CLI_DIR", cli_dir),
                patch(
                    "app.main.AZURE_CLI_SHA256",
                    hashlib.sha256(payload).hexdigest().upper(),
                ),
            ):
                job = Job()
                self.assertTrue(install_managed_azure_cli(job))

            self.assertTrue((cli_dir / "bin" / "az.cmd").is_file())
            self.assertFalse((tools_dir / "azure-cli-staging").exists())
            self.assertFalse(any(tools_dir.glob("*.zip")))
        refresh_process_path.assert_called_once_with()

    @patch("app.main.urlopen")
    def test_rejects_azure_cli_download_with_wrong_checksum(self, urlopen) -> None:
        urlopen.return_value = io.BytesIO(b"not the expected archive")

        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            cli_dir = tools_dir / "azure-cli"
            with (
                patch("app.main.MANAGED_TOOLS_DIR", tools_dir),
                patch("app.main.AZURE_CLI_DIR", cli_dir),
                patch("app.main.AZURE_CLI_SHA256", "0" * 64),
            ):
                job = Job()
                self.assertFalse(install_managed_azure_cli(job))

            self.assertFalse(cli_dir.exists())
            self.assertFalse(any(tools_dir.glob("*.zip")))

    @patch("app.main.subprocess.run")
    @patch("app.main.shutil.which")
    def test_runs_resolved_windows_command(self, which, run) -> None:
        which.return_value = r"C:\Tools\az.cmd"
        run.return_value = MagicMock(
            stdout='{"azure-cli":"2.89.1"}',
            stderr="",
            returncode=0,
        )
        self.assertEqual(command_version("az", ("version",)), "2.89.1")
        self.assertEqual(run.call_args.args[0][0], r"C:\Tools\az.cmd")

    @patch("app.main.subprocess.run")
    @patch("app.main.shutil.which", return_value=r"C:\Tools\az.cmd")
    def test_rejects_failed_or_unparseable_version_commands(self, _which, run) -> None:
        run.return_value = MagicMock(stdout="", stderr="broken", returncode=1)
        self.assertIsNone(command_version("az", ("version",)))
        run.return_value = MagicMock(stdout="installed", stderr="", returncode=0)
        self.assertIsNone(command_version("az", ("version",)))


class ProcessTests(unittest.TestCase):
    def test_client_lease_expires_after_two_minutes_without_heartbeat(self) -> None:
        with (
            patch("app.main.LAST_CLIENT_HEARTBEAT", None),
            patch("app.main.CLIENT_LEASE_TIMEOUT_SECONDS", 120.0),
        ):
            self.assertFalse(client_lease_expired(100.0, now=219.9))
            self.assertTrue(client_lease_expired(100.0, now=220.0))

        with (
            patch("app.main.LAST_CLIENT_HEARTBEAT", 200.0),
            patch("app.main.CLIENT_LEASE_TIMEOUT_SECONDS", 120.0),
        ):
            self.assertFalse(client_lease_expired(100.0, now=319.9))
            self.assertTrue(client_lease_expired(100.0, now=320.0))

    def test_manual_stop_launcher_uses_graceful_api_only(self) -> None:
        repository = STATIC_DIR.parents[1]
        stop_launcher = (
            repository
            / "packaging"
            / "windows"
            / "Stop Azure SRE Agent Demo.cmd"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/shutdown", stop_launcher)
        self.assertIn("X-SRE-Session", stop_launcher)
        self.assertNotIn("Stop-Process", stop_launcher)
        self.assertNotIn("taskkill", stop_launcher.lower())

    def test_heartbeat_route_renews_client_lease(self) -> None:
        handler = object.__new__(AppHandler)
        handler.headers = {"X-SRE-Session": SESSION_TOKEN}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/heartbeat"
        handler.send_json = MagicMock()

        with patch("app.main.record_client_heartbeat") as record_client_heartbeat:
            handler.do_POST()

        record_client_heartbeat.assert_called_once_with()
        handler.send_json.assert_called_once_with({"active": True})

    @patch("app.main.shutdown_application")
    @patch("app.main.active_jobs_running", side_effect=[True, False])
    @patch("app.main.client_lease_expired", return_value=True)
    def test_expired_lease_waits_for_active_job_before_shutdown(
        self,
        _client_lease_expired,
        _active_jobs_running,
        shutdown_application,
    ) -> None:
        server = MagicMock()
        server.shutdown_event.wait.side_effect = [False, False]

        monitor_client_lease(server, started_at=100.0)

        self.assertEqual(server.shutdown_event.wait.call_count, 2)
        shutdown_application.assert_called_once_with(server)

    @patch("app.main.threading.Thread")
    def test_shutdown_route_starts_background_server_shutdown(self, thread) -> None:
        shutdown_thread = thread.return_value
        handler = object.__new__(AppHandler)
        handler.headers = {"X-SRE-Session": SESSION_TOKEN}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/shutdown"
        handler.server = MagicMock()
        handler.send_json = MagicMock()

        handler.do_POST()

        handler.send_json.assert_called_once_with({"shutting_down": True})
        thread.assert_called_once_with(
            target=shutdown_application,
            args=(handler.server,),
            daemon=True,
            name="application-shutdown",
        )
        shutdown_thread.start.assert_called_once_with()

    @patch("app.main.JOBS", {"active": MagicMock()})
    def test_shutdown_stops_jobs_before_server(self) -> None:
        server = MagicMock()

        shutdown_application(server)

        from app.main import JOBS

        JOBS["active"].terminate_process.assert_called_once_with()
        server.shutdown.assert_called_once_with()

    def test_portable_launcher_uses_background_python_runtime(self) -> None:
        repository = STATIC_DIR.parents[1]
        launcher = (
            repository
            / "packaging"
            / "windows"
            / "Start Azure SRE Agent Demo.cmd"
        ).read_text(encoding="utf-8")

        self.assertIn('start "" /b', launcher)
        self.assertIn(r"python\pythonw.exe", launcher)
        self.assertNotIn(r"python\python.exe", launcher)
        self.assertIn(r'"%~dp0main.py"', launcher)
        self.assertNotIn(r'"%~dp0app\main.py"', launcher)
        self.assertIn("AZURE_SRE_DEMO_NO_BROWSER=1", launcher)
        self.assertIn("AZURE_SRE_DEMO_CLIENT_FALLBACK=1", launcher)
        self.assertIn("Show-Splash.ps1", launcher)
        self.assertNotIn("Repair-Shortcut.ps1", launcher)
        self.assertIn("Azure SRE Agent Demo.link-template", launcher)
        self.assertIn("attrib.exe +R", launcher)

    def test_splash_waits_for_health_before_opening_browser(self) -> None:
        repository = STATIC_DIR.parents[1]
        splash = (
            repository / "packaging" / "windows" / "Show-Splash.ps1"
        ).read_text(encoding="utf-8")
        build_script = (
            repository / "scripts" / "build-windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/health", splash)
        self.assertIn('Storyboard.TargetName="SpinnerRotation"', splash)
        self.assertIn('"--app=$AppUrl"', splash)
        self.assertIn("Microsoft\\Edge\\Application\\msedge.exe", splash)
        self.assertIn("Start-Process $AppUrl", splash)
        self.assertIn("$window.Icon", splash)
        self.assertIn("Show-Splash.ps1", build_script)
        self.assertIn("Azure SRE Agent Demo.ico", build_script)
        self.assertIn("Azure SRE Agent Demo.lnk", build_script)
        self.assertIn("New-RelativeShortcut", build_script)
        self.assertIn("A0000001", build_script)
        self.assertIn("A0000007", build_script)
        self.assertIn('"%SystemRoot%\\System32\\cmd.exe"', build_script)
        self.assertIn("IsReadOnly = $true", build_script)
        self.assertIn(
            '$relativeLauncher = "app\\Start Azure SRE Agent Demo.cmd"',
            build_script,
        )
        self.assertIn("Azure SRE Agent Demo.link-template", build_script)
        self.assertNotIn("Launch.vbs", build_script)
        self.assertNotIn("SetRelativePath", build_script)

    def test_portable_package_consolidates_application_files(self) -> None:
        repository = STATIC_DIR.parents[1]
        build_script = (
            repository / "scripts" / "build-windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '$stagingApplication = Join-Path $stagingPackage "app"',
            build_script,
        )
        self.assertIn(
            '$stagingRuntime = Join-Path $stagingApplication "python"',
            build_script,
        )
        self.assertIn(
            '$stagingVendor = Join-Path $stagingApplication "vendor"',
            build_script,
        )
        self.assertIn(
            '@("app", $shortcutName, "README.txt")',
            build_script,
        )

    def test_application_icon_has_web_and_windows_metadata(self) -> None:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        manifest = json.loads(
            (STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        icon = (STATIC_DIR / "favicon.ico").read_bytes()

        self.assertIn('rel="icon" href="/favicon.ico', page)
        self.assertIn('rel="manifest" href="/manifest.webmanifest', page)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(
            {entry["sizes"] for entry in manifest["icons"]},
            {"192x192", "512x512"},
        )
        self.assertEqual(icon[:4], b"\x00\x00\x01\x00")
        self.assertEqual(int.from_bytes(icon[4:6], "little"), 7)

    def test_diagnostic_download_is_in_footer_without_visible_path(self) -> None:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("diagnostic-path", page)
        self.assertIn('class="app-footer"', page)
        self.assertIn("Download diagnostic log", page)
        self.assertLess(page.index('id="shutdown"'), page.index('class="steps"'))
        self.assertGreater(
            page.index("Download diagnostic log"),
            page.index('id="summary"'),
        )

    @patch.dict("os.environ", {"AZURE_SRE_DEMO_NO_BROWSER": "true"})
    def test_can_disable_automatic_browser_launch(self) -> None:
        self.assertFalse(should_open_browser())

    @patch.dict("os.environ", {"AZURE_SRE_DEMO_CLIENT_FALLBACK": "true"})
    def test_can_enable_client_launch_fallback(self) -> None:
        self.assertTrue(should_fallback_open_client())

    @patch("app.main.open_application_window")
    @patch("app.main.LAST_CLIENT_HEARTBEAT", None)
    def test_client_launch_fallback_opens_unclaimed_application(
        self,
        open_application_window,
    ) -> None:
        server = MagicMock()
        server.shutdown_event.wait.return_value = False

        launch_client_if_unclaimed(server, "http://127.0.0.1:8765")

        open_application_window.assert_called_once_with(
            "http://127.0.0.1:8765"
        )

    @patch("app.main.open_application_window")
    @patch("app.main.LAST_CLIENT_HEARTBEAT", 100.0)
    def test_client_launch_fallback_skips_claimed_application(
        self,
        open_application_window,
    ) -> None:
        server = MagicMock()
        server.shutdown_event.wait.return_value = False

        launch_client_if_unclaimed(server, "http://127.0.0.1:8765")

        open_application_window.assert_not_called()

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

    @patch("app.main.shutil.which")
    def test_runs_managed_azure_cli_through_its_supported_cmd_entrypoint(
        self,
        which,
    ) -> None:
        cli_dir = Path(r"C:\Users\demo\AppData\Local\AzureSREAgentDemo\tools\azure-cli")
        which.return_value = str(cli_dir / "bin" / "az.cmd")

        with patch("app.main.AZURE_CLI_DIR", cli_dir):
            command = resolved_process_command(["az", "account", "show"])

        self.assertEqual(
            command,
            [str(cli_dir / "bin" / "az.cmd"), "account", "show"],
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
        job = Job()

        self.assertTrue(wait_for_request_metrics(
            job,
            "https://example.test",
            "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/api",
            attempts=1,
            delay_seconds=0,
        ))
        output = [
            event["line"]
            for event in job.events.queue
            if event["type"] == "output"
        ]
        self.assertEqual(
            output,
            [
                "Checking Azure Monitor request metrics before fault injection...",
                (
                    "No recent request metric is visible yet. Sending a harmless "
                    "readiness probe every 0 seconds (up to 1 attempts)."
                ),
                (
                    "Metrics warm-up attempt 1/1: probe accepted; "
                    "checking Azure Monitor in 0 seconds..."
                ),
                "Azure Monitor request metrics are ready.",
            ],
        )

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
