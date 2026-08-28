# Vendored starter-lab assets (Scenario 1 only)

This directory is a trimmed, vendored copy of
[`microsoft/sre-agent/labs/starter-lab`](https://github.com/microsoft/sre-agent/tree/main/labs/starter-lab),
scoped to **Scenario 1** only ("Break app → Agent investigates logs + remediates" — no GitHub
integration).

It is consumed directly by the Tauri app (`src-tauri/`), which drives `azd up` against
`infra/main.bicep` and re-implements the post-provision configuration steps natively in Rust
instead of shelling out to the upstream `post-provision.sh`.

## What's included
- `infra/` — Bicep templates (Log Analytics, App Insights, managed identity, Grubify Container
  Apps + ACR, SRE Agent resource, HTTP 5xx alert rule, subscription RBAC)
- `knowledge-base/` — `http-500-errors.md` runbook and `grubify-architecture.md` reference doc,
  uploaded to the agent's memory during post-provision
- `sre-config/agents/incident-handler-core.yaml` — the Scenario-1-only subagent spec (no GitHub
  tools)
- `scripts/break-app.sh` — reference shell implementation of the Scenario 1 fault injection,
  kept for parity/documentation; the app implements this natively (see `src-tauri`)
- `azure.yaml` — azd project descriptor

## What's intentionally excluded
GitHub OAuth connector config, `code-analyzer`/`issue-triager` subagent specs,
`incident-handler-full.yaml` (GitHub-tool variant), `create-sample-issues.sh`, and the
`github-issue-triage.md` knowledge base doc — all of these back Scenarios 2/3, which are out of
scope for this app.
