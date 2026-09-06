---
metadata:
  api_version: azuresre.ai/v2
  kind: Skill
name: connectivity-triage
description: Use for any Zava Learning incident where students cannot reach the platform or actions fail at the network/edge layer — quiz launches failing, portal 5xx, requests timing out, or backends appearing unhealthy. Traces the full request path (Application Gateway -> NSG -> Container Apps internal load balancer -> APIs) from telemetry and Azure config, finds the broken hop, and remediates within the permitted-action boundary.
tools:
  - RunAzCliReadCommands
  - RunAzCliWriteCommands
  - GetAzCliHelp
  - SearchMemory
  - microsoft-learn_microsoft_docs_search
  - microsoft-learn_microsoft_docs_fetch
---

## Zava Learning — Connectivity / Edge Incident Runbook

Resource Group: `@@RG@@`. Public entry: Application Gateway -> learner-portal (Container App,
internal ingress) -> course-api / assessment-api (environment-internal). App Insights
`cloud_RoleName` values: `learner-portal`, `course-api`, `assessment-api`.

Diagnose root cause from telemetry and configuration, then remediate within the boundary below.
Do NOT guess the cause from the alert name — the alert is symptom-only by design.

## Trace the path, hop by hop
1. **Application Gateway** — backend health (`az network application-gateway show-backend-health`),
   probe path/host, HTTP settings. A probe pointed at a path the portal doesn't serve marks the
   backend unhealthy and yields 502s.
2. **NSG on the Container Apps subnet** — list effective rules. A higher-priority DENY can beat a
   lower-priority ALLOW (priority inversion) and silently block App Gateway -> apps.
3. **Container Apps internal load balancer / ingress** — revision health, replica counts.
4. **APIs** — are `course-api` / `assessment-api` answering and healthy?

Use the built-in network troubleshooting skills (network_connectivity_troubleshoot,
application_gateway_troubleshoot, load_balancer_troubleshoot, network_topology_mapper) to go deep
on any hop. Filter App Insights/LAW queries by the relevant `cloud_RoleName`.

## Permitted autonomous actions
- **The `legacy-cross-subnet-deny` fault has one deterministic live mitigation.** After confirming
  that exact inbound rule is `Deny` on the affected `nsg-nsglane-*` NSG, run this non-destructive
  access update:
  `az network nsg rule update --subscription <subscription-id> --resource-group @@RG@@ --nsg-name <nsg-nsglane-name> --name legacy-cross-subnet-deny --access Allow`.
  Preserve every other field. **NEVER use a priority-only update for this fault** (including changing
  priority 100 to 4096), and never claim that reprioritizing the deny mitigated it. Do not delete the
  rule; the durable IaC change removes the injected rule after live recovery.
- Correct an Application Gateway probe path / HTTP settings back to a healthy configuration.
- Restart a Container Apps revision.

## Azure CLI usage (avoid avoidable command failures)
- **Do not pass `-o`/`--output` or `--query` to `RunAzCliReadCommands`.** The read tool already
  returns JSON — adding `-o json`, `-o table`, or a `--query` projection makes the command fail with
  a generic "Unknown error occurred." Run the plain command (e.g. `az network nsg rule list
  --nsg-name ... --include-default`) and pick out the fields you need from the JSON in your reasoning.
- If any read still returns "Unknown error occurred," just **retry the plain command once** — the
  first tool call in a session can fail transiently. Do not conclude the resource is broken.
- Always pass `--subscription` and prefer resource IDs to avoid ambiguity. Consult `GetAzCliHelp`
  before an unfamiliar write flag rather than guessing syntax.

## Incident communication (PagerDuty)
Record the request-path diagram and your diagnostic notes for the incident record. PagerDuty
acknowledgement, status/summary notes, and resolution are owned by the `pagerduty-incident-update`
skill.

## Code & change management
- For an Infrastructure-as-Code root cause, the infra lives under `infra/` in `@@REPO@@`
  (the NSG is defined in `infra/modules/network.bicep`). After the live mitigation, the durable
  fix is delivered as a GitHub pull request by the `pr-delivery` skill and recorded as a Change
  Request by `servicenow-change-management`.

## Out of scope (require human approval)
- VNet address-space changes, subnet deletion, IAM modifications, App Gateway SKU/tier changes.

## Verification
For `legacy-cross-subnet-deny`, use this bounded sequence:
1. Immediately read the same rule back and require `access: Allow`. If readback does not show
   `Allow`, retry the identical `--access Allow` update once. If the second readback still fails,
   stop all writes and escalate with the rule state and tool error.
2. Run at most **three recovery rounds**, one Application Gateway probe interval apart (about
   30 seconds). Each round must check both the `quiz-nsg` backend in
   `az network application-gateway show-backend-health` and the public quiz endpoint on port 8081
   (`/quiz/BIO-101`). Recovery requires backend health `Healthy` **and** HTTP 200 from that endpoint.
3. If both signals are not healthy after round three, do not run `show-backend-health` again, do not
   alter priority, and do not make speculative writes. Capture the final backend-health detail, the
   rule readback, and the `quiz-nsg` revision/replica state once, then escalate to the operator with
   those facts. Keep the incident acknowledged; do not mark it mitigated or proceed to closure.

The incident note, RCA, evidence, and report must record the effective live change exactly as
observed: `legacy-cross-subnet-deny access: Deny -> Allow`, followed by the backend-health and
endpoint evidence captured after that update. Never state or imply that a priority-only change
restored service. If recovery cannot be verified, report the access update as attempted/applied and
the incident as escalated and still impacted; do not manufacture a recovery claim.

For other connectivity faults, re-check the hop changed, require the affected public endpoint to
return 200, and confirm the alert auto-mitigated. Repeated health polling without a fixed attempt
budget is forbidden.
