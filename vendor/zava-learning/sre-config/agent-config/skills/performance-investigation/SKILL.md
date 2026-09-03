---
metadata:
  api_version: azuresre.ai/v2
  kind: Skill
name: performance-investigation
description: Use for Zava Learning incidents where the platform is reachable but a compute tier is degraded — elevated 5xx from the APIs, slow quiz responses, exceptions, an API with no healthy instances, or a back-office batch job failing on the reporting-worker VM (e.g. nightly grade exports). Diagnoses from Application Insights / Log Analytics / Syslog and remediates the application or worker tier.
tools:
  - RunAzCliReadCommands
  - RunAzCliWriteCommands
  - GetAzCliHelp
  - SearchMemory
  - ExecutePythonCode
  - microsoft-learn_microsoft_docs_search
  - microsoft-learn_microsoft_docs_fetch
---

## Zava Learning — Application Incident Runbook

Resource Group: `@@RG@@`. Services (Container Apps): `learner-portal`, `course-api`, `assessment-api`.
Quiz launch path: portal -> assessment-api -> course-api.

Confirm the network/edge is healthy first (App Gateway backend healthy, NSG clean) so you don't
misattribute an app fault. Then:

1. Query App Insights failures/exceptions filtered by the failing `cloud_RoleName`.
2. Check Container App revision health and replica counts — an API with zero replicas serves no
   requests and surfaces as quiz launch / 502 failures with a clean network path. A revision can hit
   zero replicas two ways: scaled to zero, OR the active revision was **deactivated** (an inactive
   revision runs zero replicas). `az containerapp revision list` shows only ACTIVE revisions, so a
   deactivated lane looks like it has "no revisions" — list with `--all` and inspect
   `properties.active` / `properties.latestRevisionName`. The latent cause is often a scale floor of
   zero in IaC (`appLaneMinReplicas: 0`); the durable fix restores a non-zero minimum.
3. Inspect recent revisions/config changes.
4. For LATENCY regressions (slow quiz responses / the latency alert) on a clean network path: pull assessment-api request durations from Log Analytics console logs (each request logs `ms=<duration>`), find the step-change in latency and align it to the most recent assessment-api revision/image deployment, then inspect `src/assessment-api/server.js` for expensive synchronous work added on the request path (e.g. a synchronous KDF/crypto call). This is a code regression — mitigate live by rolling back to the prior revision, then fix durably with a code PR.

## Reporting-worker grade-export failures
If the symptom is the nightly grade-export job failing (no quiz/portal impact — this is a back-office
batch job on a dedicated VM `vm-zava-reporting-*`):
1. Query Log Analytics `Syslog` for `ProcessName == "zava-export"` — the worker logs a heartbeat on
   success and a `FAILED` line plus the raw OS error (`export write error: ...`) on failure. Read the
   raw error to identify the failure mode.
2. Correlate with disk telemetry: the `Perf` table carries Logical Disk `% Used Space` and
   `Free Megabytes` for the worker. A data volume (`/data`) at ~100% used / near-zero free is the
   signal that the export writes are failing for lack of space.
3. Confirm on the VM with a read command (`az vm run-command invoke ... --scripts "df -h /data"`).
4. Mitigate live: free space on the worker's data disk (remove the accumulated backlog file under
   `/data/exports`) via `az vm run-command`. Re-check that the next export run logs a success heartbeat.
5. Durable fix: the worker's export retention/rotation lives in `src/reporting-worker/cloud-init.yaml`
   — a PR that enforces retention (or grows the data disk in `infra/modules/vm.bicep`) is the lasting fix.

## Database-backed quiz lanes (slow loading / errors under load / auth failures)
The quiz lanes read PostgreSQL on the request path, so a DB fault produces app-shaped symptoms on a
clean edge + healthy replicas. Do NOT stop at "the DB looks slow" and report root cause unknown —
identify WHICH database surface — log-first, confirm from the lane's error `detail`, then verify the
relevant database state. The pool-lane database checks below are mandatory, not optional:
1. Read the quiz service's console logs (`ContainerAppConsoleLogs_CL` for the failing `quiz-*` app /
   `assessment-api`) for the request shape: a slow `ms=<duration>` on `/quiz/*` → index/latency;
   a burst of `status=500` on `/quiz/*` (and any `pool error:` lines) → a backend DB failure.
2. Get the EXACT backend error straight from the lane — the service returns the underlying Postgres
   error in the response `detail` field (it is NOT written to the logs): `curl` the lane's `/health`
   (returns `503 {"status":"degraded","detail":"<pg error>"}` when the DB path is broken) or
   `/quiz/<courseId>` (`500 {"error":"...","detail":"<pg error>"}`). The `detail` names the cause
   directly — e.g. `password authentication failed` (28P01) → bad DB secret; `too many connections`
   / `remaining connection slots` / connection-limit → pool exhaustion; a slow-but-200 `/quiz` with
   climbing `ms=` → missing index. This needs no DB credentials and is the primary confirmation.
3. Confirm against the database itself with the connection recipe in the knowledge base. Run the
   query inside the reporting VM via `az vm run-command invoke`; the VM's managed identity privately
   retrieves `db-password` and invokes `psql` without returning the secret:
   - **Slow quiz loading (query lane, db `zava_query`):** check the `question_bank` indexes —
     `SELECT indexname FROM pg_indexes WHERE tablename='question_bank';` and
     `SELECT relname,seq_scan,idx_scan FROM pg_stat_user_tables WHERE relname='question_bank';`.
     A missing index on `question_bank` with seq-scans climbing is the root cause; the durable fix is
     to (re)build it (REINDEX/CREATE INDEX) — delivered as a PR.
   - **Errors under load (pool lane, db `zava`):** follow the mandatory deterministic workflow below.
   - **Authentication errors (secret lane):** follow the mandatory invalid-secret workflow below. Do
     not assume the Container App secret reference is still isolated merely because the dedicated Key
     Vault value was restored.
4. Mitigate live with the smallest corrective action (rebuild the index / restore the connection limit
   / restore the secret), verify with the lane-specific standard below, then deliver the durable fix
   as a PR.
Base the RCA on the confirmed surface above — DB-internal faults must resolve to a named cause (index,
connection limit, or secret), never "root cause unknown."

### Mandatory invalid-secret workflow
When the trigger is `Zava-quiz-launch-errors-elevated` or the affected workload is `quiz-secret`,
this workflow overrides generic secret and restart guidance:

1. Confirm the auth-failure signature without printing a credential. Read only the `pg-password`
   secret-reference metadata with `az containerapp secret list --resource-group @@RG@@ --name
   quiz-secret --query "[?name=='pg-password'] | [0].{keyVaultUrl:keyVaultUrl,identity:identity}"`.
   Never request, print, log, or attach a secret value.
2. Resolve the expected app UAMI (`id-zava-<resource-token>`) and the Key Vault URI. The only valid
   reference is the **versionless** URL ending `/secrets/db-password-secretlane` with that exact UAMI
   resource ID. A reference ending `/secrets/db-password` is cross-lane drift, even if HTTP requests
   currently succeed.
3. Through the reporting VM managed-identity/private-Key-Vault bridge, copy the baseline
   `db-password` **value into `db-password-secretlane`** without emitting either value. This is the
   only permitted secret-value mutation. Never alter the baseline `db-password`.
4. If the reference or identity drifted, restore `pg-password` with `az containerapp secret set`
   using `keyvaultref:<vault-uri>/secrets/db-password-secretlane` and
   `identityref:<expected-app-uami>`. Repointing `pg-password` to the shared
   `/secrets/db-password` reference is prohibited and must never be described as recovery.
5. Read back the same two metadata fields and require the versionless dedicated suffix plus the
   expected UAMI. Only after the value copy and reference readback succeed, create a **new revision**
   with `az containerapp update --set-env-vars FORCE_ROTATE=<timestamp>`. A revision restart does not
   re-resolve the Key Vault reference and is not recovery.
6. Check at most 17 times, 15 seconds apart (a four-minute convergence budget). Completion requires
   a new latest-ready revision in `Running` state, a second metadata readback still ending
   `/secrets/db-password-secretlane` with the expected UAMI, and HTTP 200 from port 8087
   `/quiz/BIO-101`. If any condition remains false, stop writes and escalate acknowledged/unresolved
   with sanitized reference, identity, revision, and HTTP-status evidence.
7. Every note, RCA, evidence table, and report must distinguish the value copy from any reference
   repair and record only secret-safe metadata. Never include either secret value.

### Mandatory connection-exhaustion workflow
When the trigger is `Zava-quiz-errors-elevated` or the affected workload is `quiz-pool`, this workflow
overrides generic restart guidance:

1. Use the reporting VM managed-identity bridge (`az vm run-command invoke` with the private `psql`
   recipe from the knowledge base) to run both read-only queries against database `zava`:
   - `SELECT rolname,rolconnlimit FROM pg_roles WHERE rolname='app_pool';`
   - `SELECT usename,state,count(*) AS connections FROM pg_stat_activity WHERE usename='app_pool' GROUP BY usename,state ORDER BY state;`
   Record the sanitized results. A role readback is required even when the lane response mentions
   connection termination or connection slots.
2. The controlled fault is confirmed only when `app_pool.rolconnlimit` reads back as `1` (or another
   explicit value below the healthy baseline). `terminating connection due to administrator command`,
   sparse recent 500 logs, or a successful single request do not prove the cause. The administrator-
   command message is an expected side effect of terminating sessions during fault injection, not
   evidence that stale pooled connections are the root cause.
3. Through the same reporting VM bridge, run exactly
   `ALTER ROLE app_pool CONNECTION LIMIT -1;`. Do not change another role or database setting.
4. Immediately re-run both SQL queries and require `app_pool.rolconnlimit = -1`. Only after that
   database readback succeeds, refresh `quiz-pool` once with
   `az containerapp update --resource-group @@RG@@ --name quiz-pool --set-env-vars FORCE_RECONNECT=<timestamp>`.
   The revision refresh drains stale pooled connections; it is not the causal fix. **Restart-only
   recovery is forbidden**, and a restart or refresh must never run before the database correction.
5. Run at most **three verification rounds**, 20 seconds apart. Each round must send **30 concurrent
   requests** to the public pool lane on port 8086 (`/quiz/BIO-101`) and calculate the HTTP error rate.
   Recovery requires the role still read back as `-1`, the refreshed revision to be ready, and
   **0/30 failed requests** in a concurrent round. A single `/health` or quiz request, or zero recent
   500 log rows, is never sufficient recovery evidence.
6. If the criteria are not met after three rounds, stop writes and restarts. Capture the final
   `pg_roles`, `pg_stat_activity`, revision, and concurrent error-rate evidence; keep the incident
   acknowledged and unresolved; and escalate once instead of looping.
7. Every mitigation note, RCA, evidence table, and report must record the effective database change
   read back as `rolconnlimit: 1 -> -1` (or the actual explicit before value to `-1`) and the concurrent
   verification result. Never claim that a restart, sparse logs, or a single health check restored
   service without the role readback and load result.

## Permitted autonomous actions
- Restore replica counts / restart a Container Apps revision.
- Reactivate a deactivated revision (`az containerapp revision activate`) to bring back zero-replica lanes.
- Roll back to the last-known-good revision.
- Through the reporting VM managed-identity bridge, read `pg_roles` / `pg_stat_activity` and restore
  only the controlled pool baseline with `ALTER ROLE app_pool CONNECTION LIMIT -1`.
- Free space on the reporting-worker VM's data disk (remove an accumulated backlog file under `/data`).

## Code fix
If the root cause is in application source, the app code lives under `src/` in `@@REPO@@`. After
the live mitigation (revision rollback/restart), the durable code fix is delivered as a GitHub PR
by the `pr-delivery` skill and recorded as a Change Request by `servicenow-change-management`.

## Incident communication
PagerDuty acknowledgement, status/summary notes, and resolution are owned by the
`pagerduty-incident-update` skill.

## Verification
For the pool lane, use the mandatory concurrent-load and role-readback criteria above. For other
application incidents, confirm `/api/quiz/*` returns 200 and the API success rate is back to baseline.
