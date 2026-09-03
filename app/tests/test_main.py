import hashlib
import io
import json
import re
import ssl
import tempfile
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import app.main as main_module
from app.main import (
    AppHandler,
    INSTALL_COMMANDS,
    INSTALL_ORDER,
    LABS,
    MINIMUM_VERSIONS,
    REPAIR_COMMANDS,
    RuntimeOptions,
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
    azure_resource_group_portal_url,
    azure_login_worker,
    break_cart_worker,
    build_existing_environment_catalog,
    build_azure_context_catalog,
    client_lease_expired,
    claims_challenge_login_command,
    command_version,
    deploy_worker,
    demo_external_urls,
    download_with_windows_certificate_store,
    discover_existing_environments,
    discover_edge_profiles,
    http_json,
    install_all_worker,
    install_managed_azd,
    install_managed_azure_cli,
    install_managed_powershell,
    is_device_login_url,
    is_allowed_demo_external_url,
    launch_client_if_unclaimed,
    memory_pressure_observed,
    monitor_client_lease,
    open_browser_url,
    open_edge_profile_url,
    parse_claims_challenge_login,
    parse_device_code,
    parse_runtime_options,
    lab_catalog_payload,
    load_test_mode_config,
    prerequisite_statuses,
    redact_command,
    redact_text,
    request_metrics_have_data,
    response_plan_payload,
    response_plan_is_scoped,
    response_plan_status_is_retryable,
    restore_baseline_worker,
    restore_container_baseline,
    resolved_sre_agent_portal_url,
    resolved_process_command,
    run_capture,
    run_process,
    run_tool_install,
    safe_extract_zip,
    safe_log_payload,
    scoped_azure_login_command,
    set_azd_values,
    set_runtime_options,
    should_open_browser,
    should_fallback_open_client,
    sre_agent_portal_url,
    shutdown_application,
    teardown_worker,
    upsert_response_plan,
    validate_existing_lab,
    validate_container_app_availability,
    validate_metric_alert_availability,
    wait_for_request_metrics,
    wait_for_nonessential_delay,
    version_meets_minimum,
)


TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "00000000-0000-0000-0000-000000000002"
SUBSCRIPTION_A = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_B = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_C = "33333333-3333-3333-3333-333333333333"


class RuntimeOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_options = main_module.RUNTIME_OPTIONS

    def tearDown(self) -> None:
        set_runtime_options(self.original_options)

    def test_test_mode_defaults_off_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"

            options = parse_runtime_options([], config_file)

        self.assertFalse(options.test_mode)
        self.assertEqual(options.config_file, config_file)

    def test_reads_test_mode_from_local_ini(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text(
                "[application]\ntest_mode = true\n",
                encoding="utf-8",
            )

            self.assertTrue(load_test_mode_config(config_file))
            self.assertTrue(parse_runtime_options([], config_file).test_mode)

    def test_command_line_enable_overrides_disabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text(
                "[application]\ntest_mode = false\n",
                encoding="utf-8",
            )

            options = parse_runtime_options(
                ["--config", str(config_file), "--test-mode"]
            )

        self.assertTrue(options.test_mode)

    def test_command_line_disable_overrides_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.ini"
            config_file.write_text(
                "[application]\ntest_mode = true\n",
                encoding="utf-8",
            )

            options = parse_runtime_options(
                ["--config", str(config_file), "--no-test-mode"]
            )

        self.assertFalse(options.test_mode)

    @patch("app.main.time.sleep")
    def test_default_mode_preserves_nonessential_wait(self, sleep) -> None:
        set_runtime_options(RuntimeOptions(Path("config.ini"), test_mode=False))

        wait_for_nonessential_delay("azure_monitor_initialization")

        sleep.assert_called_once_with(30)

    @patch("app.main.time.sleep")
    def test_test_mode_bypasses_only_registered_nonessential_wait(
        self,
        sleep,
    ) -> None:
        set_runtime_options(RuntimeOptions(Path("config.ini"), test_mode=True))

        wait_for_nonessential_delay("azure_monitor_initialization")

        sleep.assert_not_called()


class LabWorkflowTests(unittest.TestCase):
    def test_catalog_exposes_grubify_memory_leak_workflow(self) -> None:
        payload = lab_catalog_payload({"lab_id": "grubify-starter-lab"})

        self.assertEqual(len(LABS), 2)
        self.assertEqual(payload["selected_lab_id"], "grubify-starter-lab")
        self.assertEqual(len(payload["labs"]), 2)
        self.assertEqual(payload["labs"][0]["resource_count"], 17)
        self.assertEqual(payload["labs"][0]["estimated_turnaround"], "10-23 min")
        self.assertEqual(payload["labs"][0]["dependency_ids"], ("az", "azd"))
        self.assertEqual(
            payload["labs"][0]["scenarios"][0]["investigation_delay_seconds"],
            240,
        )
        self.assertEqual(payload["labs"][0]["scenarios"][0]["id"], "memory-leak")

    def test_catalog_exposes_all_zava_scenarios_and_regions(self) -> None:
        payload = lab_catalog_payload({"lab_id": "zava-learning"})
        zava = next(lab for lab in payload["labs"] if lab["id"] == "zava-learning")

        self.assertEqual(zava["dependency_ids"], ("az", "azd", "pwsh"))
        self.assertEqual(
            [region["id"] for region in zava["regions"]],
            ["location", "db_location", "agent_location"],
        )
        self.assertEqual(
            [scenario["id"] for scenario in zava["scenarios"]],
            ["nsg", "appgw", "app", "perf", "query", "pool", "secret", "disk"],
        )

    def test_builds_tenant_scoped_resource_group_portal_link(self) -> None:
        self.assertEqual(
            azure_resource_group_portal_url(
                TENANT_A,
                SUBSCRIPTION_A,
                "rg-sre lab",
            ),
            (
                f"https://portal.azure.com/#@{TENANT_A}/resource/subscriptions/"
                f"{SUBSCRIPTION_A}/resourceGroups/rg-sre%20lab/overview"
            ),
        )

    def test_builds_deployed_sre_agent_portal_link(self) -> None:
        self.assertEqual(
            sre_agent_portal_url(
                SUBSCRIPTION_A,
                "rg-sre lab",
                "sre-agent-a",
            ),
            (
                "https://sre.azure.com/agents/subscriptions/"
                f"{SUBSCRIPTION_A}/resourceGroups/rg-sre%20lab/"
                "providers/Microsoft.App/agents/sre-agent-a"
            ),
        )

    def test_discovers_named_edge_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_data = Path(directory)
            (user_data / "Default").mkdir()
            (user_data / "Profile 1").mkdir()
            (user_data / "System Profile").mkdir()
            (user_data / "Local State").write_text(
                json.dumps({
                    "profile": {
                        "info_cache": {
                            "Default": {
                                "name": "Personal",
                                "user_name": "personal@example.test",
                            },
                            "Profile 1": {
                                "name": "Work",
                                "user_name": "work@example.test",
                            },
                            "System Profile": {"name": "System"},
                        },
                    },
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                discover_edge_profiles(user_data),
                [
                    {
                        "id": "Default",
                        "name": "Personal",
                        "email": "personal@example.test",
                    },
                    {
                        "id": "Profile 1",
                        "name": "Work",
                        "email": "work@example.test",
                    },
                ],
            )

    def test_demo_external_links_are_limited_to_active_https_urls(self) -> None:
        state = {
            "environment": "sre-lab",
            "tenant_id": TENANT_A,
            "subscription_id": SUBSCRIPTION_A,
        }
        values = {
            "AZURE_RESOURCE_GROUP": "rg-sre-lab",
            "AGENT_PORTAL_URL": "https://sre.azure.com",
            "SRE_AGENT_NAME": "sre-agent-a",
            "CONTAINER_APP_URL": "https://api.example.test",
            "FRONTEND_APP_URL": "https://app.example.test",
        }

        self.assertEqual(len(demo_external_urls(state, values)), 4)
        self.assertTrue(
            is_allowed_demo_external_url(
                (
                    "https://sre.azure.com/agents/subscriptions/"
                    f"{SUBSCRIPTION_A}/resourceGroups/rg-sre-lab/"
                    "providers/Microsoft.App/agents/sre-agent-a"
                ),
                state,
                values,
            )
        )
        self.assertEqual(
            resolved_sre_agent_portal_url(state, values),
            (
                "https://sre.azure.com/agents/subscriptions/"
                f"{SUBSCRIPTION_A}/resourceGroups/rg-sre-lab/"
                "providers/Microsoft.App/agents/sre-agent-a"
            ),
        )
        self.assertFalse(
            is_allowed_demo_external_url(
                "https://malicious.example.test",
                state,
                values,
            )
        )
        self.assertFalse(
            is_allowed_demo_external_url(
                "http://app.example.test",
                state,
                values,
            )
        )

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
                    "sre-agent-demo-lab-id": "grubify-starter-lab",
                    "sre-agent-demo-environment": "tagged-lab",
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
            {
                "name": "sre-agent-a",
                "resourceGroup": "rg-legacy-lab",
                "endpoint": "https://agent.example.test",
            },
            {"name": "sre-agent-b", "resourceGroup": "rg-unrelated"},
            {"name": "sre-agent-c", "resourceGroup": "rg-incomplete"},
        ]
        container_apps = [
            {
                "name": "ca-grubify-abc",
                "resourceGroup": "rg-legacy-lab",
                "fqdn": "api.example.test",
            },
            {
                "name": "ca-grubify-fe-abc",
                "resourceGroup": "rg-legacy-lab",
                "fqdn": "ui.example.test",
            },
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
        legacy = next(
            item for item in environments
            if item["environment"] == "legacy-lab"
        )
        self.assertEqual(
            legacy["runtime_values"]["CONTAINER_APP_URL"],
            "https://api.example.test",
        )
        self.assertEqual(
            legacy["runtime_values"]["SRE_AGENT_ENDPOINT"],
            "https://agent.example.test",
        )

    def test_zava_discovery_excludes_tagged_managed_infrastructure_groups(
        self,
    ) -> None:
        tags = {
            "sre-agent-demo-lab-id": "zava-learning",
            "sre-agent-demo-environment": "zava-learning-auto-1",
        }
        environments = build_existing_environment_catalog(
            [
                {
                    "name": "rg-zava-learning-zava-learning-auto-1",
                    "location": "southcentralus",
                    "tags": tags,
                },
                {
                    "name": "ME_cae-zava-nsglane_token_southcentralus",
                    "location": "southcentralus",
                    "tags": tags,
                },
                {
                    "name": "ME_cae-zava_token_southcentralus",
                    "location": "southcentralus",
                    "tags": tags,
                },
            ],
            [{
                "name": "sre-zava-zava-learning-auto-1",
                "resourceGroup": "rg-zava-learning-zava-learning-auto-1",
            }],
            [],
            {"zava-learning-auto-1"},
            "zava-learning",
        )

        self.assertEqual(len(environments), 1)
        self.assertEqual(
            environments[0]["resource_group"],
            "rg-zava-learning-zava-learning-auto-1",
        )

    @patch("app.main.save_environment_cache")
    @patch("app.main.local_azd_environment_names", return_value=set())
    @patch("app.main.run_capture")
    def test_discovery_uses_container_app_provider_for_runtime_urls(
        self,
        run_capture,
        _local_names,
        _save_cache,
    ) -> None:
        run_capture.side_effect = [
            (True, json.dumps([{
                "name": "rg-existing-lab",
                "location": "eastus2",
                "tags": {
                    "sre-agent-demo-lab-id": "grubify-starter-lab",
                    "sre-agent-demo-environment": "existing-lab",
                },
            }])),
            (True, json.dumps([{
                "name": "sre-agent-a",
                "resourceGroup": "rg-existing-lab",
            }])),
            (True, json.dumps([
                {
                    "name": "ca-grubify-api",
                    "resourceGroup": "rg-existing-lab",
                    "fqdn": "api.example.test",
                },
                {
                    "name": "ca-grubify-fe",
                    "resourceGroup": "rg-existing-lab",
                    "fqdn": "ui.example.test",
                },
            ])),
        ]

        result = discover_existing_environments(
            SUBSCRIPTION_A,
            "grubify-starter-lab",
        )

        self.assertEqual(
            run_capture.call_args_list[2].args[0][:3],
            ["az", "containerapp", "list"],
        )
        self.assertEqual(
            result["environments"][0]["runtime_values"]["CONTAINER_APP_URL"],
            "https://api.example.test",
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
            "'sre-agent-demo-lab-id': 'grubify-starter-lab'",
            bicep,
        )
        self.assertIn(
            "'sre-agent-demo-environment': environmentName",
            bicep,
        )
        tag_names = re.findall(r"^\s*'([^']+)':", bicep, re.MULTILINE)
        self.assertFalse(
            any(
                tag.casefold().startswith(("azure", "microsoft", "windows"))
                for tag in tag_names
            )
        )

    def test_sre_agent_bicep_outputs_resource_deep_link(self) -> None:
        bicep = (
            VENDOR_DIR / "infra" / "modules" / "sre-agent.bicep"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "output agentPortalUrl string = "
            "'https://sre.azure.com/agents${sreAgent.id}'",
            bicep,
        )

    @patch("app.main.http_json")
    @patch("app.main.run_capture")
    def test_validates_and_hydrates_one_selected_environment(
        self,
        run_capture,
        http_json,
    ) -> None:
        resource_group = "rg-existing-lab"
        resources = [
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.App/agents/sre-agent-a",
                "name": "sre-agent-a",
                "type": "Microsoft.App/agents",
                "provisioningState": "Succeeded",
            },
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.App/managedEnvironments/cae-a",
                "name": "cae-a",
                "type": "Microsoft.App/managedEnvironments",
                "provisioningState": "Succeeded",
            },
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.ContainerRegistry/registries/acra",
                "name": "acra",
                "type": "Microsoft.ContainerRegistry/registries",
                "provisioningState": "Succeeded",
            },
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.OperationalInsights/workspaces/law-a",
                "name": "law-a",
                "type": "Microsoft.OperationalInsights/workspaces",
                "provisioningState": "Succeeded",
            },
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.Insights/components/appi-a",
                "name": "appi-a",
                "type": "Microsoft.Insights/components",
                "provisioningState": "Succeeded",
            },
            {
                "id": f"/subscriptions/sub/resourceGroups/{resource_group}/providers/Microsoft.Insights/metricAlerts/alert-a",
                "name": "alert-a",
                "type": "Microsoft.Insights/metricAlerts",
                "provisioningState": "Succeeded",
                "enabled": True,
            },
        ]
        apps = [
            {
                "name": "ca-grubify-a",
                "image": "acra.azurecr.io/grubify-api:latest",
                "fqdn": "api.example.test",
                "provisioningState": "Succeeded",
                "runningStatus": "Running",
                "latestRevisionName": "api--0002",
                "latestReadyRevisionName": "api--0002",
            },
            {
                "name": "ca-grubify-fe-a",
                "image": "acra.azurecr.io/grubify-frontend:latest",
                "fqdn": "ui.example.test",
                "provisioningState": "Succeeded",
                "runningStatus": "Ready",
                "latestRevisionName": "frontend--0002",
                "latestReadyRevisionName": "frontend--0002",
            },
        ]
        agent = {
            "name": "sre-agent-a",
            "endpoint": "https://agent.example.test",
            "incidentType": "AzMonitor",
            "provisioningState": "Succeeded",
        }
        run_capture.side_effect = [
            (True, json.dumps(resources)),
            (True, json.dumps(apps)),
            (True, json.dumps(agent)),
            (True, "agent-token"),
        ]
        http_json.side_effect = [
            (HTTPStatus.OK, "{}"),
            (
                HTTPStatus.OK,
                json.dumps(response_plan_payload(
                    "existing-lab",
                    SUBSCRIPTION_A,
                    resource_group,
                )),
            ),
        ]

        with patch(
            "app.main.probe_http_endpoint",
            return_value=(True, ""),
        ) as probe:
            result = validate_existing_lab(
                SUBSCRIPTION_A,
                {
                    "environment": "existing-lab",
                    "resource_group": resource_group,
                    "location": "eastus2",
                },
            )

        self.assertTrue(result["ready"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(
            result["values"]["CONTAINER_APP_URL"],
            "https://api.example.test",
        )
        self.assertEqual(
            result["values"]["FRONTEND_APP_URL"],
            "https://ui.example.test",
        )
        self.assertEqual(len(run_capture.call_args_list), 4)
        self.assertEqual(len(http_json.call_args_list), 2)
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(all(
            check["available"] for check in result["availability_checks"]
        ))

    @patch("app.main.run_capture")
    def test_validation_routes_incomplete_environment_to_update(
        self,
        run_capture,
    ) -> None:
        run_capture.side_effect = [
            (True, "[]"),
            (True, "[]"),
        ]

        result = validate_existing_lab(
            SUBSCRIPTION_A,
            {
                "environment": "incomplete-lab",
                "resource_group": "rg-incomplete-lab",
                "location": "eastus2",
            },
        )

        self.assertFalse(result["ready"])
        self.assertIn("Missing Azure SRE Agent.", result["issues"])
        self.assertIn("Missing Grubify API Container App.", result["issues"])

    @patch("app.main.run_capture")
    def test_saves_validated_outputs_to_selected_azd_environment(
        self,
        run_capture,
    ) -> None:
        run_capture.return_value = (True, "")

        success, error = set_azd_values("existing-lab", {
            "AZURE_RESOURCE_GROUP": "rg-existing-lab",
            "CONTAINER_APP_URL": "https://api.example.test",
        })

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(len(run_capture.call_args_list), 2)
        self.assertEqual(
            run_capture.call_args_list[0].args[0],
            [
                "azd", "env", "set", "-e", "existing-lab",
                "AZURE_RESOURCE_GROUP", "rg-existing-lab",
            ],
        )

    @patch("app.main.set_azd_values", return_value=(True, ""))
    @patch("app.main.validate_existing_lab")
    @patch("app.main.load_environment_cache")
    @patch("app.main.cached_azure_context")
    @patch("app.main.save_state")
    @patch("app.main.load_state")
    def test_ready_validation_activates_existing_lab(
        self,
        load_state,
        save_state,
        cached_azure_context,
        load_environment_cache,
        validate_existing,
        set_values,
    ) -> None:
        state = {
            "lab_id": "grubify-starter-lab",
            "environment": "existing-lab",
            "subscription_id": SUBSCRIPTION_A,
            "existing_environment": True,
            "deployment_active": False,
        }
        candidate = {
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "location": "eastus2",
        }
        load_state.return_value = state
        cached_azure_context.return_value = {
            "tenant": TENANT_A,
            "subscription": SUBSCRIPTION_A,
        }
        load_environment_cache.return_value = [candidate]
        validate_existing.return_value = {
            "ready": True,
            "issues": [],
            "values": {"AZURE_RESOURCE_GROUP": "rg-existing-lab"},
        }
        handler = object.__new__(AppHandler)
        handler.read_json = MagicMock(return_value={
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
        })
        handler.send_json = MagicMock()

        handler.validate_environment()

        self.assertTrue(save_state.call_args.args[0]["deployment_active"])
        self.assertIn("validated_at", save_state.call_args.args[0])
        set_values.assert_called_once_with(
            "existing-lab",
            {"AZURE_RESOURCE_GROUP": "rg-existing-lab"},
        )
        self.assertTrue(handler.send_json.call_args.args[0]["ready"])

    def test_stopped_container_app_is_not_ready_or_probed(self) -> None:
        apps = {
            "api": {
                "name": "ca-grubify-api",
                "provisioningState": "Succeeded",
                "runningStatus": "Stopped",
                "latestRevisionName": "api--0002",
                "latestReadyRevisionName": "api--0002",
                "fqdn": "api.example.test",
            },
            "frontend": None,
        }

        with patch("app.main.probe_http_endpoint") as probe:
            issues, checks = validate_container_app_availability(apps)

        probe.assert_not_called()
        self.assertIn("is not started (running status: Stopped)", issues[0])
        self.assertFalse(checks[0]["started"])
        self.assertFalse(checks[0]["available"])

    def test_started_container_app_requires_reachable_endpoint(self) -> None:
        apps = {
            "api": {
                "name": "ca-grubify-api",
                "provisioningState": "Succeeded",
                "runningStatus": "Running",
                "latestRevisionName": "api--0002",
                "latestReadyRevisionName": "api--0002",
                "fqdn": "api.example.test",
            },
            "frontend": None,
        }

        with patch(
            "app.main.probe_http_endpoint",
            return_value=(False, "HTTP 503"),
        ):
            issues, checks = validate_container_app_availability(apps)

        self.assertIn("is started but its endpoint /health is unavailable", issues[0])
        self.assertTrue(checks[0]["started"])
        self.assertFalse(checks[0]["available"])

    def test_latest_container_app_revision_must_be_ready(self) -> None:
        apps = {
            "api": {
                "name": "ca-grubify-api",
                "provisioningState": "Succeeded",
                "runningStatus": "Running",
                "latestRevisionName": "api--0003",
                "latestReadyRevisionName": "api--0002",
                "fqdn": "api.example.test",
            },
            "frontend": None,
        }

        with patch("app.main.probe_http_endpoint") as probe:
            issues, checks = validate_container_app_availability(apps)

        probe.assert_not_called()
        self.assertIn("latest revision is not ready", issues[0])
        self.assertFalse(checks[0]["available"])

    def test_disabled_metric_alert_is_unavailable(self) -> None:
        issues, checks = validate_metric_alert_availability([{
            "name": "alert-http-5xx-lab",
            "provisioningState": "Succeeded",
            "enabled": False,
        }])

        self.assertIn("is disabled", issues[0])
        self.assertFalse(checks[0]["available"])


class TestModeValidationTests(unittest.TestCase):
    def handler(self, payload: dict[str, Any]) -> AppHandler:
        handler = object.__new__(AppHandler)
        handler.client_address = ("127.0.0.1", 50000)
        handler.read_json = MagicMock(return_value=payload)
        handler.send_json = MagicMock()
        return handler

    @patch("app.main.is_test_mode", return_value=False)
    def test_skip_is_rejected_when_test_mode_is_off(self, _is_test_mode) -> None:
        handler = self.handler({
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "acknowledge_risk": True,
        })
        handler.existing_environment_candidate = MagicMock()

        handler.skip_environment_validation()

        handler.existing_environment_candidate.assert_not_called()
        self.assertEqual(
            handler.send_json.call_args.args[1],
            HTTPStatus.FORBIDDEN,
        )

    @patch("app.main.is_test_mode", return_value=True)
    def test_skip_requires_explicit_risk_acknowledgement(
        self,
        _is_test_mode,
    ) -> None:
        handler = self.handler({
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
        })
        handler.existing_environment_candidate = MagicMock()

        handler.skip_environment_validation()

        handler.existing_environment_candidate.assert_not_called()
        self.assertEqual(
            handler.send_json.call_args.args[1],
            HTTPStatus.BAD_REQUEST,
        )

    @patch("app.main.save_state")
    @patch("app.main.set_azd_values", return_value=(True, ""))
    @patch("app.main.is_test_mode", return_value=True)
    def test_skip_records_explicit_state_in_test_mode(
        self,
        _is_test_mode,
        set_values,
        save_state,
    ) -> None:
        payload = {
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "acknowledge_risk": True,
        }
        handler = self.handler(payload)
        state = {
            "lab_id": "grubify-starter-lab",
            "environment": "existing-lab",
            "existing_environment": True,
            "subscription_id": SUBSCRIPTION_A,
        }
        candidate = {
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "location": "eastus2",
            "detection": "managed",
            "runtime_values": {
                "AZURE_RESOURCE_GROUP": "rg-existing-lab",
                "CONTAINER_APP_NAME": "ca-grubify-api",
                "CONTAINER_APP_URL": "https://api.example.test",
                "FRONTEND_APP_NAME": "ca-grubify-frontend",
                "FRONTEND_APP_URL": "https://ui.example.test",
                "SRE_AGENT_NAME": "sre-agent-a",
                "SRE_AGENT_ENDPOINT": "https://agent.example.test",
            },
        }
        handler.existing_environment_candidate = MagicMock(return_value=(
            state,
            LABS[0],
            {"tenant": TENANT_A, "subscription": SUBSCRIPTION_A},
            candidate,
        ))

        handler.skip_environment_validation()

        saved = save_state.call_args.args[0]
        self.assertTrue(saved["deployment_active"])
        self.assertEqual(saved["validation_status"], "skipped")
        self.assertIn("validation_skipped_at", saved)
        self.assertNotIn("validated_at", saved)
        set_values.assert_called_once_with(
            "existing-lab",
            candidate["runtime_values"],
        )
        response = handler.send_json.call_args.args[0]
        self.assertTrue(response["skipped"])
        self.assertFalse(response["ready"])
        self.assertTrue(response["proceed"])

    @patch("app.main.is_test_mode", return_value=True)
    def test_skip_rejects_unknown_environment_selection(
        self,
        _is_test_mode,
    ) -> None:
        handler = self.handler({
            "environment": "unknown-lab",
            "resource_group": "rg-unknown-lab",
            "acknowledge_risk": True,
        })
        handler.existing_environment_candidate = MagicMock(return_value=None)

        with patch("app.main.save_state") as save_state:
            handler.skip_environment_validation()

        save_state.assert_not_called()

    @patch("app.main.is_test_mode", return_value=True)
    def test_skip_rejects_incomplete_runtime_metadata(
        self,
        _is_test_mode,
    ) -> None:
        handler = self.handler({
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "acknowledge_risk": True,
        })
        handler.existing_environment_candidate = MagicMock(return_value=(
            {"environment": "existing-lab"},
            LABS[0],
            {"tenant": TENANT_A, "subscription": SUBSCRIPTION_A},
            {
                "environment": "existing-lab",
                "resource_group": "rg-existing-lab",
                "runtime_values": {},
            },
        ))

        with (
            patch("app.main.set_azd_values") as set_values,
            patch("app.main.save_state") as save_state,
        ):
            handler.skip_environment_validation()

        set_values.assert_not_called()
        save_state.assert_not_called()
        self.assertEqual(
            handler.send_json.call_args.args[1],
            HTTPStatus.CONFLICT,
        )

    @patch("app.main.save_state")
    @patch("app.main.set_azd_values", return_value=(True, ""))
    @patch("app.main.validate_existing_lab")
    @patch("app.main.is_test_mode", return_value=True)
    def test_normal_validation_still_runs_in_test_mode(
        self,
        _is_test_mode,
        validate_existing,
        set_values,
        save_state,
    ) -> None:
        handler = self.handler({
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
        })
        state = {"deployment_active": False}
        candidate = {
            "environment": "existing-lab",
            "resource_group": "rg-existing-lab",
            "location": "eastus2",
        }
        handler.existing_environment_candidate = MagicMock(return_value=(
            state,
            LABS[0],
            {"tenant": TENANT_A, "subscription": SUBSCRIPTION_A},
            candidate,
        ))
        validate_existing.return_value = {
            "ready": True,
            "issues": [],
            "values": {"AZURE_RESOURCE_GROUP": "rg-existing-lab"},
        }

        handler.validate_environment()

        validate_existing.assert_called_once_with(SUBSCRIPTION_A, candidate)
        set_values.assert_called_once()
        self.assertEqual(save_state.call_args.args[0]["validation_status"], "validated")

    def test_skip_route_requires_local_session_token(self) -> None:
        handler = object.__new__(AppHandler)
        handler.headers = {"X-SRE-Session": "wrong-token"}
        handler.client_address = ("127.0.0.1", 50000)
        handler.path = "/api/environments/skip-validation"
        handler.send_json = MagicMock()
        handler.skip_environment_validation = MagicMock()

        handler.do_POST()

        handler.skip_environment_validation.assert_not_called()
        self.assertEqual(
            handler.send_json.call_args.args[1],
            HTTPStatus.FORBIDDEN,
        )

    def test_skip_route_dispatches_with_valid_local_session(self) -> None:
        handler = object.__new__(AppHandler)
        handler.headers = {"X-SRE-Session": SESSION_TOKEN}
        handler.client_address = ("127.0.0.1", 50000)
        handler.path = "/api/environments/skip-validation"
        handler.send_json = MagicMock()
        handler.skip_environment_validation = MagicMock()

        handler.do_POST()

        handler.skip_environment_validation.assert_called_once_with()


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
            "az": "2.90.0",
            "azd": "1.32.0",
            "pwsh": "7.6.5",
        }
        command_version.side_effect = lambda executable, _args: versions[executable]
        statuses = prerequisite_statuses()
        refresh_process_path.assert_called_once_with()
        self.assertEqual([item.id for item in statuses], ["az", "azd", "pwsh"])
        self.assertTrue(all(item.installed for item in statuses))
        self.assertTrue(all(item.ready for item in statuses))
        self.assertTrue(all(item.state == "ready" for item in statuses))
        self.assertTrue(all(item.required for item in statuses))
        self.assertEqual(
            MINIMUM_VERSIONS,
            {"az": "2.88.0", "azd": "1.28.0", "pwsh": "7.6.3"},
        )
        which.assert_called()

    def test_install_commands_are_app_managed(self) -> None:
        expected = {"az", "azd", "pwsh"}
        self.assertEqual(set(INSTALL_COMMANDS), expected)
        self.assertEqual(set(UPDATE_COMMANDS), expected)
        self.assertEqual(set(REPAIR_COMMANDS), expected)
        self.assertEqual(INSTALL_ORDER, ("az", "azd", "pwsh"))
        for commands in (INSTALL_COMMANDS, UPDATE_COMMANDS, REPAIR_COMMANDS):
            for tool_id in ("az", "azd", "pwsh"):
                command = commands[tool_id]
                self.assertEqual(command[0], "app-managed")
                self.assertNotIn("winget", command)

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
            "az": "2.87.0",
            "azd": "1.27.1",
            "pwsh": "7.5.0",
        }[executable]

        statuses = prerequisite_statuses()

        self.assertTrue(all(item.installed for item in statuses))
        self.assertTrue(all(not item.ready for item in statuses))
        self.assertTrue(all(item.state == "outdated" for item in statuses))
        self.assertTrue(all("app-managed" in item.install_command for item in statuses))

    def test_compares_normalized_semantic_versions(self) -> None:
        self.assertTrue(version_meets_minimum("2.88.0", "2.88.0"))
        self.assertTrue(version_meets_minimum("2.90.1", "2.88.0"))
        self.assertFalse(version_meets_minimum("2.87.9", "2.88.0"))
        self.assertFalse(version_meets_minimum("installed", "2.88.0"))
        self.assertTrue(version_meets_minimum("7.6.3", "7.6.3"))
        self.assertFalse(version_meets_minimum("7.6.2", "7.6.3"))

    @patch("app.main.install_managed_azure_cli")
    @patch("app.main.install_managed_azd")
    @patch("app.main.install_managed_powershell")
    @patch("app.main.run_process")
    @patch("app.main.prerequisite_statuses")
    def test_install_all_runs_missing_tools_sequentially(
        self,
        prerequisite_statuses,
        run_process,
        install_managed_powershell,
        install_managed_azd,
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
                    required=True,
                )
                for tool_id in INSTALL_ORDER
            ]

        def install_azure_cli(_job):
            installed.add("az")
            return True

        def install_azd(_job):
            installed.add("azd")
            return True

        def install_powershell(_job):
            installed.add("pwsh")
            return True

        prerequisite_statuses.side_effect = statuses
        install_managed_azure_cli.side_effect = install_azure_cli
        install_managed_azd.side_effect = install_azd
        install_managed_powershell.side_effect = install_powershell
        job = Job()

        install_all_worker(job)

        run_process.assert_not_called()
        install_managed_azure_cli.assert_called_once_with(job)
        install_managed_azd.assert_called_once_with(job)
        install_managed_powershell.assert_called_once_with(job)
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

    @patch("app.main.refresh_process_path")
    @patch("app.main.urlopen")
    def test_installs_checksum_verified_azd_in_user_profile(
        self,
        urlopen,
        refresh_process_path,
    ) -> None:
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("azd-windows-amd64.exe", "executable")
            archive.writestr("NOTICE.txt", "notices")
        payload = archive_bytes.getvalue()
        urlopen.return_value = io.BytesIO(payload)

        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            azd_dir = tools_dir / "azd"
            with (
                patch("app.main.MANAGED_TOOLS_DIR", tools_dir),
                patch("app.main.AZD_DIR", azd_dir),
                patch(
                    "app.main.AZD_SHA256",
                    hashlib.sha256(payload).hexdigest().upper(),
                ),
            ):
                job = Job()
                self.assertTrue(install_managed_azd(job))

            self.assertTrue((azd_dir / "azd.exe").is_file())
            self.assertFalse((azd_dir / "azd-windows-amd64.exe").exists())
            self.assertFalse((tools_dir / "azd-staging").exists())
            self.assertFalse(any(tools_dir.glob("*.zip")))
        refresh_process_path.assert_called_once_with()

    @patch("app.main.refresh_process_path")
    @patch("app.main.urlopen")
    def test_installs_checksum_verified_powershell_in_user_profile(
        self,
        urlopen,
        refresh_process_path,
    ) -> None:
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("pwsh.exe", "executable")
            archive.writestr("LICENSE.txt", "license")
        payload = archive_bytes.getvalue()
        urlopen.return_value = io.BytesIO(payload)

        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            powershell_dir = tools_dir / "powershell"
            with (
                patch("app.main.MANAGED_TOOLS_DIR", tools_dir),
                patch("app.main.POWERSHELL_DIR", powershell_dir),
                patch(
                    "app.main.POWERSHELL_SHA256",
                    hashlib.sha256(payload).hexdigest().upper(),
                ),
            ):
                job = Job()
                self.assertTrue(install_managed_powershell(job))

            self.assertTrue((powershell_dir / "pwsh.exe").is_file())
            self.assertFalse((tools_dir / "powershell-staging").exists())
            self.assertFalse(any(tools_dir.glob("*.zip")))
        refresh_process_path.assert_called_once_with()

    @patch("app.main.refresh_process_path")
    @patch("app.main.download_with_windows_certificate_store")
    @patch("app.main.urlopen")
    def test_azd_download_retries_with_windows_certificate_store(
        self,
        urlopen,
        windows_download,
        refresh_process_path,
    ) -> None:
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("azd-windows-amd64.exe", "executable")
        payload = archive_bytes.getvalue()
        urlopen.side_effect = URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        windows_download.side_effect = (
            lambda _url, destination: destination.write_bytes(payload)
        )

        with tempfile.TemporaryDirectory() as directory:
            tools_dir = Path(directory) / "tools"
            azd_dir = tools_dir / "azd"
            with (
                patch("app.main.MANAGED_TOOLS_DIR", tools_dir),
                patch("app.main.AZD_DIR", azd_dir),
                patch(
                    "app.main.AZD_SHA256",
                    hashlib.sha256(payload).hexdigest().upper(),
                ),
            ):
                job = Job()
                self.assertTrue(install_managed_azd(job))

        windows_download.assert_called_once()
        refresh_process_path.assert_called_once_with()
        output = [
            event["line"]
            for event in job.events.queue
            if event["type"] == "output"
        ]
        self.assertTrue(any("Windows trusted certificate store" in line for line in output))

    @patch("app.main.subprocess.run")
    @patch("app.main.shutil.which", return_value=r"C:\Windows\powershell.exe")
    @patch("app.main.os.name", "nt")
    def test_windows_download_preserves_certificate_validation(
        self,
        _which,
        run,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "azd.zip"

            def complete_download(*_args, **_kwargs):
                destination.write_bytes(b"archive")
                return MagicMock(returncode=0, stdout="", stderr="")

            run.side_effect = complete_download
            download_with_windows_certificate_store(
                "https://example.test/azd.zip",
                destination,
            )

        command = run.call_args.args[0]
        script = command[-1]
        self.assertIn("Invoke-WebRequest", script)
        self.assertNotIn("SkipCertificateCheck", script)
        self.assertNotIn("ServerCertificateValidationCallback", script)

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
    @patch("app.main.subprocess.Popen")
    @patch("app.main.discover_edge_profiles")
    @patch("app.main.find_edge")
    def test_opens_url_with_selected_edge_profile(
        self,
        find_edge,
        discover_edge_profiles,
        popen,
    ) -> None:
        find_edge.return_value = Path(r"C:\Edge\msedge.exe")
        discover_edge_profiles.return_value = [
            {"id": "Profile 1", "name": "Work", "email": "work@example.test"}
        ]
        popen.return_value.pid = 1234

        self.assertTrue(
            open_edge_profile_url(
                "https://portal.azure.com/example",
                "Profile 1",
            )
        )

        command = popen.call_args.args[0]
        self.assertEqual(command[0], r"C:\Edge\msedge.exe")
        self.assertIn("--profile-directory=Profile 1", command)
        self.assertIn("--new-window", command)
        self.assertEqual(command[-1], "https://portal.azure.com/example")

    @patch("app.main.subprocess.Popen")
    @patch("app.main.discover_edge_profiles", return_value=[])
    @patch("app.main.find_edge", return_value=Path(r"C:\Edge\msedge.exe"))
    def test_rejects_unknown_edge_profile(
        self,
        _find_edge,
        _discover_edge_profiles,
        popen,
    ) -> None:
        self.assertFalse(
            open_edge_profile_url(
                "https://portal.azure.com/example",
                r"..\Unsafe",
            )
        )
        popen.assert_not_called()

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

    def test_default_client_lease_tolerates_browser_timer_throttling(self) -> None:
        self.assertEqual(main_module.CLIENT_LEASE_TIMEOUT_SECONDS, 300.0)
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.addEventListener("visibilitychange"', script)

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
        self.assertIn(r'"%~dp0main.py" %*', launcher)
        self.assertNotIn(r'"%~dp0app\main.py"', launcher)
        self.assertIn("AZURE_SRE_DEMO_NO_BROWSER=1", launcher)
        self.assertNotIn("AZURE_SRE_DEMO_CLIENT_FALLBACK", launcher)
        self.assertIn("Show-Splash.ps1", launcher)
        self.assertNotIn("Repair-Shortcut.ps1", launcher)
        self.assertIn("Azure SRE Agent Demo.link-template", launcher)
        self.assertIn("attrib.exe +R", launcher)

    def test_source_launcher_passes_command_line_options_to_python(self) -> None:
        repository = STATIC_DIR.parents[1]
        command_launcher = (
            repository / "scripts" / "start.cmd"
        ).read_text(encoding="utf-8")
        powershell_launcher = (
            repository / "scripts" / "start.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('-File "%~dp0start.ps1" %*', command_launcher)
        self.assertIn("ValueFromRemainingArguments", powershell_launcher)
        self.assertIn("$quotedAppArguments", powershell_launcher)
        self.assertIn("+ $quotedAppArguments", powershell_launcher)

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
            '"THIRD-PARTY-NOTICES.txt"',
            build_script,
        )
        self.assertIn('$licenseSource = Join-Path $repoRoot "LICENSE"', build_script)
        self.assertIn(
            '$thirdPartyNoticesSource = Join-Path $repoRoot "THIRD-PARTY-NOTICES.txt"',
            build_script,
        )
        self.assertIn(
            "Copy-Item -LiteralPath $licenseSource -Destination $stagingPackage",
            build_script,
        )
        self.assertIn(
            "Copy-Item -LiteralPath $thirdPartyNoticesSource -Destination $stagingPackage",
            build_script,
        )
        self.assertIn(
            'Join-Path $packagedApplication "python\\LICENSE.txt"',
            build_script,
        )
        self.assertIn('"vendor\\starter-lab"', build_script)
        self.assertIn('"vendor\\zava-learning"', build_script)
        self.assertTrue((repository / "LICENSE").is_file())
        self.assertTrue((repository / "THIRD-PARTY-NOTICES.txt").is_file())
        self.assertTrue(
            (repository / "vendor" / "zava-learning" / "README.vendor.md").is_file()
        )

    def test_application_icon_has_web_and_windows_metadata(self) -> None:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
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
        self.assertIn('class="brand-logo"', page)
        self.assertIn('src="/favicon.svg', page)
        self.assertIn(".workflow-compact .brand-logo", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertIn('class="step-leading"', page)
        self.assertLess(page.index('id="back"'), page.index("1. Lab Picker"))
        self.assertNotIn('class="workflow-navigation"', page)
        self.assertIn(".workflow-compact .step-leading #back", styles)
        self.assertIn(".prereq-actions button", styles)
        self.assertIn('id="validate-environment"', page)
        self.assertNotIn('id="skip-environment-validation"', page)

        self.assertIn('id="test-mode-banner"', page)
        self.assertIn('id="azure-context-loading"', page)
        self.assertIn('class="context-spinner"', page)
        self.assertIn(".context-loading-grid", styles)
        self.assertIn("setAzureContextLoading(true)", script)
        self.assertIn('class="context-card environment-browser"', page)
        self.assertIn('class="environment-discovery"', page)
        self.assertIn('class="context-card new-environment-card"', page)
        self.assertIn('id="investigation-countdown"', page)
        self.assertIn('id="edge-profile"', page)
        self.assertIn('apiPost("/api/open-edge-link"', script)
        self.assertIn(".edge-profile-picker", styles)
        self.assertIn("resource_group_portal_url", script)
        self.assertIn(
            '["SRE Agent portal", summary.agent_name, summary.agent_portal_url]',
            script,
        )
        self.assertIn(
            "environmentValidationStatus.textContent =\n"
            "    `${environment.environment} selected. "
            "Validation checks only this lab.`;",
            script,
        )
        self.assertIn("updateDemoActionAvailability();", script)
        self.assertIn(
            "teardownButton.disabled = Boolean(",
            script,
        )
        self.assertNotIn(
            'buttons.forEach((button) => {\n      button.disabled = false;',
            script,
        )
        self.assertIn(
            'title="Reapply the declared infrastructure, application images, '
            'and SRE Agent configuration to repair demo drift."',
            page,
        )
        self.assertIn('externalIcon.textContent = "↗"', script)
        self.assertIn(".external-link-icon", styles)
        self.assertIn("Use these settings only when deploying a new lab.", page)
        self.assertIn(".summary-overview", styles)
        self.assertIn("startInvestigationCountdown(event)", script)
        self.assertIn(".environment-choice-divider", styles)
        self.assertIn(".new-environment-card .form-grid", styles)
        self.assertNotIn("azd stores its sign-in separately", script)
        self.assertIn(".environment-browser .context-heading", styles)
        self.assertIn(".environment-discovery", styles)
        self.assertNotIn("No GitHub connection is required", script)
        self.assertIn('apiPost("/api/environments/validate"', script)
        self.assertIn(
            'skipEnvironmentButton = document.createElement("button")',
            script,
        )
        self.assertIn('if (!testMode || !environment) return;', script)
        self.assertIn(
            'apiPost("/api/environments/skip-validation"',
            script,
        )
        self.assertIn("acknowledge_risk: true", script)
        self.assertIn("window.confirm(", script)
        self.assertIn(
            'activePanelId === "summary" && skipDeploymentForValidatedEnvironment',
            script,
        )
        transition_start = script.index(
            'showPanel("prerequisites");',
            script.index('document.querySelector("#continue-lab")'),
        )
        persistence_start = script.index(
            "await persistSelectedLab();",
            transition_start,
        )
        self.assertLess(transition_start, persistence_start)
        self.assertIn(
            'classList.toggle("workflow-compact", id !== "labs")',
            script,
        )
        self.assertIn("heading.focus({ preventScroll: true })", script)
        self.assertIn(
            'window.scrollTo({ top: Math.max(0, panelTop), behavior: "auto" })',
            script,
        )

    def test_optional_integrations_link_to_official_setup_guides(self) -> None:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        for url in (
            "https://signup.pagerduty.com/",
            "https://www.pagerduty.com/docs/guides/azure-integration-guide/",
            "https://support.pagerduty.com/docs/api-access-keys",
            "https://learn.microsoft.com/azure/sre-agent/set-up-pagerduty-indexing",
            "https://developer.servicenow.com/",
            "https://learn.microsoft.com/azure/sre-agent/setup-github-connector",
            "https://learn.microsoft.com/azure/sre-agent/connect-source-code",
        ):
            self.assertIn(url, page)
        self.assertGreaterEqual(page.count('class="help-tip"'), 7)
        self.assertIn("FindConnectedGitHubRepo", page)
        self.assertIn("<summary>Optional integrations <span>(advanced)</span></summary>", page)
        self.assertEqual(page.count("Optional integrations"), 2)
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            "card.dataset.resourceGroup === environment.resource_group",
            script,
        )
        self.assertIn(
            "resource_group: selectedExistingEnvironment?.resource_group",
            script,
        )
        self.assertIn("Review the log below and retry.", script)

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
        self.assertEqual(
            payload["titleContains"],
            "alert-http-5xx-sre-lab-auto-2",
        )
        self.assertEqual(payload["titleContainsAll"], [])
        self.assertEqual(payload["titleContainsAny"], [])
        self.assertEqual(payload["titleNotContains"], [])
        self.assertTrue(payload["isEnabled"])
        self.assertNotIn("maxAttempts", payload)

    def test_response_plan_scope_requires_this_environment_alert(self) -> None:
        payload = response_plan_payload(
            "sre-lab-auto-2",
            SUBSCRIPTION_A,
            "rg-sre-lab-auto-2",
        )

        self.assertTrue(
            response_plan_is_scoped(
                json.dumps(payload),
                "sre-lab-auto-2",
            )
        )
        self.assertFalse(
            response_plan_is_scoped(
                json.dumps(payload),
                "sre-lab-auto-4",
            )
        )
        payload["titleContains"] = ""
        self.assertFalse(
            response_plan_is_scoped(
                json.dumps(payload),
                "sre-lab-auto-2",
            )
        )

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

    @patch("app.main.run_process")
    @patch("app.main.load_state")
    def test_legacy_environment_teardown_is_blocked(
        self,
        load_state,
        run_process,
    ) -> None:
        load_state.return_value = {
            "environment": "legacy-lab",
            "existing_environment": True,
            "existing_environment_detection": "legacy",
            "deployment_active": True,
        }
        job = Job()

        teardown_worker(job)

        run_process.assert_not_called()
        events = list(job.events.queue)
        self.assertIn("original deployment workflow", events[-2]["message"])
        self.assertFalse(events[-1]["success"])


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
        countdown_events = [
            event
            for event in events
            if event["type"] == "investigation_countdown"
        ]
        self.assertEqual(len(countdown_events), 1)
        self.assertEqual(countdown_events[0]["scenario_id"], "memory-leak")
        self.assertEqual(countdown_events[0]["seconds"], 240)
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
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        grubify = next(
            lab for lab in LABS if lab.id == "grubify-starter-lab"
        )

        self.assertIn('id="region-fields"', html)
        self.assertIn('select.dataset.regionId = region.id', script)
        self.assertEqual(len(grubify.regions), 1)
        self.assertEqual(
            frozenset(grubify.regions[0].allowed_values),
            SRE_AGENT_REGIONS,
        )

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


class ZavaBackendFollowUpTests(unittest.TestCase):
    def tearDown(self) -> None:
        main_module.clear_in_memory_secrets("zava-learning", "atomic-test")

    def test_private_bridge_allowlist_is_operational_only(self) -> None:
        self.assertEqual(
            main_module.ZAVA_SECRET_NAMES,
            {"db-password", "db-pool-password", "vm-admin-password"},
        )

    def test_transient_settings_replace_atomically(self) -> None:
        main_module.replace_in_memory_secrets(
            "zava-learning",
            "atomic-test",
            {"pagerduty_api_token": "old", "servicenow_password": "old"},
        )
        main_module.replace_in_memory_secrets(
            "zava-learning",
            "atomic-test",
            {"pagerduty_api_token": "new"},
        )
        self.assertEqual(
            main_module.get_in_memory_secrets("zava-learning", "atomic-test"),
            {"pagerduty_api_token": "new"},
        )

    def test_failed_integration_setup_retains_one_time_memory(self) -> None:
        main_module.replace_in_memory_secrets(
            "zava-learning",
            "atomic-test",
            {"pagerduty_api_token": "never-log-this"},
        )
        job = Job(["deploy"])
        with patch.object(
            main_module,
            "run_secret_capture",
            return_value=(False, ""),
        ):
            configured, _status = main_module.configure_zava_optional_integrations(
                job,
                "atomic-test",
                {},
                main_module.get_in_memory_secrets("zava-learning", "atomic-test"),
            )
        self.assertFalse(configured)
        self.assertEqual(
            main_module.get_in_memory_secrets(
                "zava-learning",
                "atomic-test",
            )["pagerduty_api_token"],
            "never-log-this",
        )
        self.assertNotIn(
            "never-log-this",
            json.dumps(list(job.events.queue)),
        )

    def test_successful_deployment_clears_one_time_memory(self) -> None:
        state = {
            "lab_id": "zava-learning",
            "environment": "atomic-test",
            "location": "eastus2",
            "db_location": "westus3",
            "agent_location": "westus2",
            "subscription_id": "sub",
            "integration_status": {"pagerduty": "requested"},
        }
        values = {
            "AZURE_LOCATION": "eastus2",
            "AZURE_DB_LOCATION": "westus3",
            "AZURE_AGENT_LOCATION": "westus2",
            "AZURE_RESOURCE_GROUP": "rg-zava",
        }
        job = Job()
        with (
            patch.object(main_module, "load_state", return_value=state),
            patch.object(main_module, "azd_values", return_value=values),
            patch.object(main_module, "set_azd_values", return_value=(True, "")),
            patch.object(
                main_module,
                "zava_process_environment",
                return_value=({"VM_ADMIN_PASSWORD": "hidden"}, None),
            ),
            patch.object(
                main_module,
                "run_process",
                return_value=(True, ""),
            ) as run_process_mock,
            patch.object(
                main_module,
                "hydrate_zava_runtime_outputs",
                return_value=values,
            ),
            patch.object(
                main_module,
                "discover_zava_secure_resource_names",
                return_value=values,
            ),
            patch.object(main_module, "configure_zava_agent_core", return_value=True),
            patch.object(
                main_module,
                "configure_zava_optional_integrations",
                return_value=(True, {"pagerduty": "healthy"}),
            ),
            patch.object(main_module, "save_state"),
            patch.object(main_module, "clear_in_memory_secrets") as clear,
        ):
            main_module.reconcile_zava(job)
        clear.assert_called_once_with("zava-learning", "atomic-test")
        self.assertTrue(list(job.events.queue)[-1]["success"])
        self.assertTrue(run_process_mock.call_args_list)
        self.assertTrue(
            all(
                not call.kwargs.get("no_log_output", False)
                for call in run_process_mock.call_args_list
            )
        )

    def test_incomplete_or_unsupported_integrations_fail_step_four(self) -> None:
        _values, error = main_module.parse_zava_integrations(
            {"pagerduty_api_token": "secret"}
        )
        self.assertIn("requires", error)
        _values, error = main_module.parse_zava_integrations(
            {"github_repository": "owner/repo"}
        )
        self.assertIn("SRE Agent portal", error)

    def test_servicenow_credentials_are_safe_python_literals(self) -> None:
        manifest = (
            main_module.vendor_dir_for_lab(main_module.LABS_BY_ID["zava-learning"])
            / "sre-config"
            / "tools"
            / "CreateServiceNowChangeRequest"
            / "CreateServiceNowChangeRequest.yaml"
        )
        tool = main_module.parse_zava_python_tool_manifest(
            manifest,
            "CreateServiceNowChangeRequest",
            {
                "SERVICENOW_URL": "https://example.test/a\\b",
                "SERVICENOW_USER": 'operator"name',
                "SERVICENOW_PASS": "line1\nline2",
            },
        )

        compile(tool["functionCode"], str(manifest), "exec")
        self.assertNotIn("@@SERVICENOW_", tool["functionCode"])

    def test_summary_links_include_ui_display_value(self) -> None:
        links = main_module.runtime_summary_links(
            {
                "lab_id": "zava-learning",
                "tenant_id": "tenant",
                "subscription_id": "sub",
            },
            {
                "AZURE_RESOURCE_GROUP": "rg-zava",
                "APPGW_PUBLIC_FQDN": "zava.example.test",
            },
        )
        self.assertTrue(links)
        self.assertTrue(all(link["value"] == link["url"] for link in links))

    def test_discovery_preserves_all_three_zava_regions(self) -> None:
        environments = build_existing_environment_catalog(
            [{
                "name": "rg-zava",
                "location": "eastus2",
                "tags": {
                    main_module.LAB_ID_TAG: "zava-learning",
                    main_module.LAB_ENVIRONMENT_TAG: "zava",
                },
            }],
            [{
                "name": "sre-zava",
                "resourceGroup": "rg-zava",
                "location": "westus2",
                "endpoint": "https://agent.example",
            }],
            [],
            set(),
            "zava-learning",
            [],
            [{
                "name": "pg-zava",
                "resourceGroup": "rg-zava",
                "location": "westus3",
            }],
        )
        self.assertEqual(environments[0]["location"], "eastus2")
        self.assertEqual(environments[0]["db_location"], "westus3")
        self.assertEqual(environments[0]["agent_location"], "westus2")

    def test_scenario_signal_queries_match_alert_sources(self) -> None:
        injected = main_module.datetime(2026, 1, 1, tzinfo=main_module.timezone.utc)
        self.assertIn(
            'listenerName_s == "quiz-nsg-listener"',
            main_module.zava_scenario_signal_query("nsg", injected),
        )
        self.assertIn(
            'ContainerAppName_s == "quiz-perf"',
            main_module.zava_scenario_signal_query("perf", injected),
        )
        self.assertIn(
            'ProcessName == "zava-export"',
            main_module.zava_scenario_signal_query("disk", injected),
        )

    def test_zava_azure_yaml_uses_remote_container_builds(self) -> None:
        azure_yaml = (
            main_module.vendor_dir_for_lab(main_module.LABS_BY_ID["zava-learning"])
            / "azure.yaml"
        ).read_text(encoding="utf-8")

        self.assertEqual(azure_yaml.count("remoteBuild: true"), 3)

    def test_zava_names_do_not_repeat_the_lab_prefix(self) -> None:
        self.assertEqual(
            main_module.zava_resource_group_name("zava-learning-auto-1"),
            "rg-zava-learning-auto-1",
        )
        self.assertEqual(
            main_module.zava_agent_name("zava-learning-auto-1"),
            "sre-zava-learning-auto-1",
        )
        self.assertEqual(
            main_module.zava_resource_group_name("demo"),
            "rg-zava-learning-demo",
        )
        self.assertEqual(
            main_module.normalize_azure_location("West US 3"),
            "westus3",
        )
        self.assertEqual(
            main_module.normalize_azure_location("South Central US"),
            "southcentralus",
        )

    @patch("app.main.run_capture")
    def test_discovers_single_existing_zava_agent_name(self, run_capture) -> None:
        run_capture.return_value = (
            True,
            json.dumps(["sre-zava-zava-learning-auto-1"]),
        )

        self.assertEqual(
            main_module.discover_zava_agent_names("rg-zava"),
            ["sre-zava-zava-learning-auto-1"],
        )

    @patch("app.main.run_capture", return_value=(True, "true"))
    def test_partial_zava_resource_group_is_treated_as_existing(
        self,
        _run_capture,
    ) -> None:
        self.assertTrue(main_module.azure_resource_group_exists("rg-zava"))

    @patch("app.main.time.sleep")
    @patch("app.main.set_azd_values", return_value=(True, ""))
    @patch("app.main.run_capture")
    @patch("app.main.azd_values", return_value={})
    def test_zava_runtime_waits_for_agent_endpoint(
        self,
        _azd_values,
        run_capture,
        _set_values,
        sleep,
    ) -> None:
        run_capture.side_effect = [
            (
                True,
                json.dumps({
                    "name": "sre-zava-learning-demo",
                    "endpoint": None,
                    "location": "eastus2",
                }),
            ),
            (
                True,
                json.dumps({
                    "name": "sre-zava-learning-demo",
                    "endpoint": "https://agent.example.test",
                    "location": "eastus2",
                }),
            ),
        ]
        job = Job()

        values = main_module.hydrate_zava_runtime_outputs(
            job,
            "demo",
            {
                "resource_group": "rg-zava-learning-demo",
                "subscription_id": "sub",
            },
            expected_agent_name="sre-zava-learning-demo",
            attempts=2,
            delay_seconds=0.01,
        )

        self.assertEqual(values["SRE_AGENT_ENDPOINT"], "https://agent.example.test")
        sleep.assert_called_once_with(0.01)
        first_command = run_capture.call_args_list[0].args[0]
        self.assertEqual(first_command[:3], ["az", "resource", "show"])
        self.assertIn("2025-05-01-preview", first_command)
        self.assertIn(
            "Waiting for the Zava SRE Agent endpoint",
            [event.get("name") for event in job.events.queue],
        )

    @patch("app.main.resolved_process_command", return_value=["tool"])
    @patch("app.main.subprocess.Popen")
    def test_process_output_redacts_environment_values(
        self,
        popen,
        _resolved,
    ) -> None:
        process = MagicMock()
        process.pid = 123
        process.stdout = iter(["failure included PreviewOnly-Secret\n"])
        process.wait.return_value = 1
        popen.return_value = process
        job = Job()

        success, output = run_process(
            job,
            ["tool"],
            environment_overrides={"PASSWORD": "PreviewOnly-Secret"},
        )

        self.assertFalse(success)
        self.assertNotIn("PreviewOnly-Secret", output)
        self.assertIn("<redacted-environment-value>", output)
        self.assertNotIn(
            "PreviewOnly-Secret",
            json.dumps(list(job.events.queue)),
        )

    def test_sanitizes_terminal_formatting_and_spinner_frames(self) -> None:
        self.assertEqual(
            main_module.sanitize_terminal_output(
                "\x1b[K\x1b[93mSeeding database...\x1b[39m"
            ),
            "Seeding database...",
        )
        self.assertTrue(main_module.is_transient_cli_spinner("/ Running .."))
        self.assertTrue(main_module.is_transient_cli_spinner(r"\ Running .."))
        self.assertFalse(
            main_module.is_transient_cli_spinner("Building learner-portal...")
        )


if __name__ == "__main__":
    unittest.main()
