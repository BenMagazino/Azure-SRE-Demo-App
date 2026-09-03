// Configure the Zava Learning SRE Agent via public Azure MCP tools and supported
// SRE Agent data-plane APIs.
//
// Bicep deploys the agent; THIS script applies all agent configuration:
//   MCP connectors -> tools -> skills -> agents -> PagerDuty incident platform -> response plan
//   -> knowledge base.
//
// We invoke the native azmcp binary directly (not `npx`) because Node refuses to
// execFile a .cmd shim with shell:false, and we must pass multi-line skill/runbook
// content verbatim (PowerShell/`npx` mangle embedded newlines).
//
// Usage:  node scripts/configure-agent.mjs
// Env/args: AGENT, RESOURCE_GROUP, AZURE_SUBSCRIPTION_ID (else read from the values below).
// Secrets are read from sre-config/.env (gitignored): PAGERDUTY_API_TOKEN,
//   GITHUB_REPO, SERVICENOW_URL/USER/PASS.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, "..");

// ---- inputs -------------------------------------------------------------
const AGENT = process.env.AGENT || "sre-zavalearning-ops";
const RG = process.env.RESOURCE_GROUP || "rg-zava-learning-demo";
const SUB = process.env.AZURE_SUBSCRIPTION_ID || "";
if (!SUB) {
  console.error("Set AZURE_SUBSCRIPTION_ID to your lab subscription before running configure-agent.mjs.");
  process.exit(1);
}
process.env.AZURE_SUBSCRIPTION_ID = SUB;

// ---- load gitignored secrets from sre-config/.env -----------------------
const envFile = path.join(repoRoot, "sre-config", ".env");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
    if (m && !line.trimStart().startsWith("#")) process.env[m[1]] = m[2];
  }
}
const REPO = process.env.GITHUB_REPO || "";

// ---- locate the azmcp native binary -------------------------------------
function findAzmcp() {
  if (process.env.AZMCP_EXE && fs.existsSync(process.env.AZMCP_EXE)) return process.env.AZMCP_EXE;
  const exe = os.platform() === "win32" ? "azmcp.exe" : "azmcp";
  let root;
  try {
    root = execFileSync(os.platform() === "win32" ? "npm.cmd" : "npm", ["root", "-g"], { encoding: "utf8", shell: true }).trim();
  } catch { root = ""; }
  const base = path.join(root, "@azure", "mcp", "node_modules", "@azure");
  if (fs.existsSync(base)) {
    for (const d of fs.readdirSync(base)) {
      const cand = path.join(base, d, "dist", exe);
      if (fs.existsSync(cand)) return cand;
    }
  }
  throw new Error("azmcp not found. Run: npm i -g @azure/mcp@latest  (or set AZMCP_EXE)");
}
const AZMCP = findAzmcp();

console.log(`azmcp: ${AZMCP}`);
console.log(`target: ${AGENT}  rg=${RG}\n`);

// ---- helper -------------------------------------------------------------
function run(label, args) {
  process.stdout.write(`-> ${label} ... `);
  try {
    const out = execFileSync(AZMCP, args, { encoding: "utf8", maxBuffer: 32 * 1024 * 1024 });
    let status = "?";
    try { status = JSON.parse(out.slice(out.indexOf("{"))).status; } catch {}
    console.log(`status ${status}`);
    return true;
  } catch (e) {
    const msg = (e.stdout || "") + (e.stderr || "") + (e.message || "");
    console.log("FAILED");
    console.log(msg.split(/\r?\n/).slice(0, 8).join("\n"));
    return false;
  }
}
const base = ["--agent", AGENT, "--resource-group", RG];
const SN_URL = process.env.SERVICENOW_URL || "";
const SN_USER = process.env.SERVICENOW_USER || "";
const SN_PASS = process.env.SERVICENOW_PASS || "";

// ---- discover live resource identifiers ---------------------------------
// Pre-resolve the exact IDs the agent would otherwise have to look up at runtime, so the
// knowledge base (and skills) can hand them to the agent directly and save a tool call.
// Critically, `az monitor log-analytics query --workspace` and `az monitor app-insights
// query --app` want the workspace GUID / App Insights appId, NOT the resource name. Passing
// the name returns ResourceNotFound, which the runtime escalates to a human approval
// (PendingAuthorization) and stalls the investigation. Resolved at apply time, so these
// always match the current deployment (the resource-name token changes on every redeploy).
const AZ_BIN = os.platform() === "win32" ? "az.cmd" : "az";
function azJson(args) {
  try {
    return JSON.parse(execFileSync(AZ_BIN, [...args, "--subscription", SUB, "-o", "json"],
      { encoding: "utf8", shell: os.platform() === "win32", maxBuffer: 32 * 1024 * 1024 }));
  } catch { return null; }
}
// Pick the PLATFORM resource (log-zava*/appi-zava*), never the agent's own workspace/app-insights.
const pick = (arr, re) => (Array.isArray(arr) ? arr.find((x) => re.test(x?.name || "")) : null) || {};
const law = pick(azJson(["monitor", "log-analytics", "workspace", "list", "-g", RG,
  "--query", "[].{name:name,customerId:customerId}"]), /^log-zava/);
const appi = pick(azJson(["monitor", "app-insights", "component", "show", "-g", RG,
  "--query", "[].{name:name,appId:appId}"]), /^appi-zava/);
const acr = pick(azJson(["acr", "list", "-g", RG, "--query", "[].{name:name}"]), /^acr/);
const cae = pick(azJson(["containerapp", "env", "list", "-g", RG, "--query", "[].{name:name}"]), /./);
const LAW_NAME = law.name || "", LAW_GUID = law.customerId || "";
const APPI_NAME = appi.name || "", APPI_APPID = appi.appId || "";
const ACR_NAME = acr.name || "", CAE_NAME = cae.name || "";
if (LAW_GUID) console.log(`law: ${LAW_NAME} (${LAW_GUID})`);
if (APPI_APPID) console.log(`appinsights: ${APPI_NAME} (${APPI_APPID})`);
if (!LAW_GUID || !APPI_APPID) console.log("(WARNING: some resource IDs unresolved; knowledge placeholders will be blank)");

// ServiceNow creds are injected into staged tool copies at apply time (never committed): the
// Python function-tool sandbox cannot read env/.env, so the creds must be literal in functionCode.
const sub = (s) => s
  .split("@@RG@@").join(RG)
  .split("@@REPO@@").join(REPO)
  .split("@@LAW_NAME@@").join(LAW_NAME)
  .split("@@LAW_GUID@@").join(LAW_GUID)
  .split("@@APPINSIGHTS_NAME@@").join(APPI_NAME)
  .split("@@APPINSIGHTS_APPID@@").join(APPI_APPID)
  .split("@@ACR_NAME@@").join(ACR_NAME)
  .split("@@CAE_NAME@@").join(CAE_NAME)
  .split("@@SERVICENOW_URL@@").join(SN_URL)
  .split("@@SERVICENOW_USER@@").join(SN_USER)
  .split("@@SERVICENOW_PASS@@").join(SN_PASS);

// ---- tools + skills + agents via the supported data-plane API ------------
// azmcp cannot express structured skill tool lists or Python function tools. Use the
// same authenticated data-plane routes as the SRE Agent portal instead of the unreleased
// srectl CLI. Tools are registered first so skill references resolve; agents are last so
// their allowedSkills references resolve.
function parseScalar(value) {
  const text = value.trim();
  if (text.startsWith('"') && text.endsWith('"')) {
    try { return JSON.parse(text); } catch {}
  }
  if (text.startsWith("'") && text.endsWith("'")) return text.slice(1, -1).replaceAll("''", "'");
  return text;
}

function readBlockScalar(lines, key) {
  const index = lines.findIndex((line) => new RegExp(`^  ${key}:\\s*\\|[-+]?\\s*$`).test(line));
  if (index < 0) throw new Error(`Missing ${key} block in Python tool manifest.`);
  const result = [];
  for (let i = index + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() && !line.startsWith("    ")) break;
    result.push(line.startsWith("    ") ? line.slice(4) : "");
  }
  return result.join("\n");
}

function parsePythonFunctionTool(raw, expectedName) {
  const lines = raw.split(/\r?\n/);
  const manifestName = parseScalar(lines.find((line) => /^  name:\s*/.test(line))?.replace(/^  name:\s*/, "") || "");
  if (!manifestName || manifestName !== expectedName) {
    throw new Error(`Tool manifest name does not match directory: ${expectedName}.`);
  }

  const type = parseScalar(lines.find((line) => /^  type:\s*/.test(line))?.replace(/^  type:\s*/, "") || "");
  if (type !== "PythonFunctionTool") {
    throw new Error(`Tool ${expectedName} must use type PythonFunctionTool.`);
  }

  const timeoutText = lines.find((line) => /^  timeoutSeconds:\s*/.test(line))?.replace(/^  timeoutSeconds:\s*/, "") || "";
  const timeoutSeconds = Number(timeoutText);
  if (!Number.isFinite(timeoutSeconds)) throw new Error(`Tool ${expectedName} has an invalid timeoutSeconds.`);

  const parameters = [];
  let current = null;
  let inParameters = false;
  for (const line of lines) {
    if (/^  parameters:\s*$/.test(line)) { inParameters = true; continue; }
    if (!inParameters) continue;
    const start = line.match(/^  - name:\s*(.*)$/);
    if (start) {
      if (current) parameters.push(current);
      current = { name: parseScalar(start[1]) };
      continue;
    }
    const field = line.match(/^    (type|description|required):\s*(.*)$/);
    if (field && current) {
      current[field[1]] = field[1] === "required"
        ? field[2].trim().toLowerCase() === "true"
        : parseScalar(field[2]);
    }
  }
  if (current) parameters.push(current);

  return {
    type: "PythonFunctionTool",
    description: readBlockScalar(lines, "description"),
    functionCode: readBlockScalar(lines, "functionCode"),
    timeoutSeconds,
    parameters,
    authEnabled: false,
    authScopes: [],
  };
}

function parseSkillMarkdown(raw, expectedName) {
  const match = raw.match(/^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
  if (!match) throw new Error(`Skill ${expectedName} has no YAML frontmatter.`);
  const frontmatter = match[1].split(/\r?\n/);
  const name = parseScalar(frontmatter.find((line) => /^name:\s*/.test(line))?.replace(/^name:\s*/, "") || "");
  if (name !== expectedName) throw new Error(`Skill name does not match directory: ${expectedName}.`);
  const description = parseScalar(frontmatter.find((line) => /^description:\s*/.test(line))?.replace(/^description:\s*/, "") || "");
  const tools = [];
  let inTools = false;
  for (const line of frontmatter) {
    if (/^tools:\s*$/.test(line)) { inTools = true; continue; }
    if (!inTools) continue;
    const item = line.match(/^  -\s+(.+)$/);
    if (item) tools.push(parseScalar(item[1]));
    else if (line.trim() && !line.startsWith(" ")) break;
  }
  assertBareToolNames(tools, `Skill ${expectedName}`);
  return { name, description, tools, skillContent: raw, additionalFiles: [] };
}

function assertBareToolNames(tools, owner) {
  const qualified = tools.filter((tool) => typeof tool === "string" && tool.includes("/"));
  if (qualified.length > 0) {
    throw new Error(`${owner} must use bare registered tool names, not connector-qualified names: ${qualified.join(", ")}.`);
  }
}

function parseYamlValue(value) {
  const text = value.trim();
  if (text === "true" || text === "false") return text === "true";
  if (text === "null" || text === "~") return null;
  if (text === "[]") return [];
  if (/^-?(?:\d+|\d*\.\d+)$/.test(text)) return Number(text);
  return parseScalar(text);
}

function parseExtendedAgent(raw, expectedName) {
  const lines = raw.split(/\r?\n/);
  const manifestName = parseScalar(lines.find((line) => /^  name:\s*/.test(line))?.replace(/^  name:\s*/, "") || "");
  if (!manifestName || manifestName !== expectedName) {
    throw new Error(`Agent manifest name does not match directory: ${expectedName}.`);
  }

  const properties = {};
  for (let i = lines.findIndex((line) => /^spec:\s*$/.test(line)) + 1; i < lines.length; i++) {
    const field = lines[i].match(/^  ([A-Za-z][A-Za-z0-9]*):\s*(.*)$/);
    if (!field) continue;
    const [, key, rawValue] = field;
    if (/^\|[-+]?$/.test(rawValue.trim())) {
      properties[key] = readBlockScalar(lines, key);
      continue;
    }
    if (rawValue.trim()) {
      properties[key] = parseYamlValue(rawValue);
      continue;
    }

    const items = [];
    let next = i + 1;
    while (next < lines.length) {
      const item = lines[next].match(/^  -\s+(.+)$/);
      if (!item) break;
      items.push(parseYamlValue(item[1]));
      next++;
    }
    properties[key] = items;
    i = next - 1;
  }
  assertBareToolNames(Array.isArray(properties.mcpTools) ? properties.mcpTools : [], `Agent ${expectedName}`);
  return properties;
}

async function putExtendedEntity(token, kind, name, type, properties) {
  const endpoint = agentEndpoint.replace(/\/+$/, "");
  const response = await fetch(`${endpoint}/api/v2/extendedAgent/${kind}/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ name, type, tags: [], properties }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} applying ${kind}/${name}.`);
}

async function applyToolsSkillsAndAgents() {
  const toolSrc = path.join(repoRoot, "sre-config", "tools");
  const skillSrc = path.join(repoRoot, "sre-config", "agent-config", "skills");
  const agentSrc = path.join(repoRoot, "sre-config", "agent-config", "agents");

  const tools = fs.existsSync(toolSrc)
    ? fs.readdirSync(toolSrc, { withFileTypes: true })
        .filter((d) => d.isDirectory() && fs.existsSync(path.join(toolSrc, d.name, `${d.name}.yaml`)))
        .map((d) => d.name)
    : [];
  const skills = fs.existsSync(skillSrc)
    ? fs.readdirSync(skillSrc, { withFileTypes: true })
        .filter((d) => d.isDirectory() && fs.existsSync(path.join(skillSrc, d.name, "SKILL.md")))
        .map((d) => d.name)
    : [];
  // Custom agents (ExtendedAgent YAML): scope skills via allowedSkills and prescribe the
  // ordered runbook via instructions; the incident filter routes to one by name (handlingAgent).
  const agents = fs.existsSync(agentSrc)
    ? fs.readdirSync(agentSrc, { withFileTypes: true })
        .filter((d) => d.isDirectory() && fs.existsSync(path.join(agentSrc, d.name, `${d.name}.yaml`)))
        .map((d) => d.name)
    : [];
  if (tools.length === 0 && skills.length === 0 && agents.length === 0) { console.log("-> tools/skills/agents ... none found, skipped"); return; }
  if (!agentEndpoint) { console.log("-> tools/skills/agents ... skipped (agent data-plane endpoint not resolved)"); return; }

  if (!SN_URL || !SN_USER || !SN_PASS) {
    console.log("-> ServiceNow credentials incomplete; tools will be registered but return a configuration error when called.");
  }
  const token = azToken(DATAPLANE_SCOPE);
  for (const name of tools) {
    process.stdout.write(`-> tool ${name} (data plane) ... `);
    const raw = sub(fs.readFileSync(path.join(toolSrc, name, `${name}.yaml`), "utf8"));
    await putExtendedEntity(token, "tools", name, "ExtendedAgentTool", parsePythonFunctionTool(raw, name));
    console.log("applied");
  }
  for (const name of skills) {
    process.stdout.write(`-> skill ${name} (data plane) ... `);
    const raw = sub(fs.readFileSync(path.join(skillSrc, name, "SKILL.md"), "utf8"));
    await putExtendedEntity(token, "skills", name, "Skill", parseSkillMarkdown(raw, name));
    console.log("applied");
  }
  for (const name of agents) {
    process.stdout.write(`-> agent ${name} (data plane) ... `);
    const raw = sub(fs.readFileSync(path.join(agentSrc, name, `${name}.yaml`), "utf8"));
    await putExtendedEntity(token, "agents", name, "ExtendedAgent", parseExtendedAgent(raw, name));
    console.log("applied");
  }
}

// ---- ARM helpers --------------------------------------------------------
// The agent's incident-management type and incident filters (response plans) are
// configured as ARM child resources / properties. The azmcp `incidents plans_create`
// targets a data-plane route that is read-only on current agent builds (HTTP 405),
// so we apply these two pieces declaratively over ARM instead.
const API_VERSION = "2025-05-01-preview";
const ARM = "https://management.azure.com";
const DATAPLANE_SCOPE = "59f0a04a-b322-4310-adc9-39ac41e9631e/.default";
const AZ = os.platform() === "win32" ? "az.cmd" : "az";
const AGENT_URL = `${ARM}/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.App/agents/${AGENT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function azToken(audience) {
  const tokenArgs = audience.endsWith("/.default")
    ? ["--scope", audience]
    : ["--resource", audience];
  return execFileSync(AZ, ["account", "get-access-token", ...tokenArgs, "--query", "accessToken", "-o", "tsv"],
    { encoding: "utf8", shell: os.platform() === "win32" }).trim();
}
async function arm(method, urlNoApi, body) {
  const r = await fetch(`${urlNoApi}?api-version=${API_VERSION}`, {
    method,
    headers: { Authorization: `Bearer ${azToken(ARM)}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  return { ok: r.ok, status: r.status, text };
}

// Resolve the PagerDuty "on behalf of" user email used for incident-platform write
// actions. Prefer an explicit env value; otherwise look up the API token's user.
async function resolvePagerDutyOboUser() {
  const explicit = (process.env.PAGERDUTY_OBO_USER || process.env.PAGERDUTY_FROM_EMAIL || "").trim();
  if (explicit) return explicit;
  const token = process.env.PAGERDUTY_API_TOKEN;
  if (!token) return null;
  try {
    const r = await fetch("https://api.pagerduty.com/users/me", {
      headers: { Authorization: `Token token=${token}`, Accept: "application/vnd.pagerduty+json;version=2" },
    });
    if (!r.ok) return null;
    return (await r.json())?.user?.email ?? null;
  } catch {
    return null;
  }
}

// Resolve the agent's data-plane endpoint once; used by direct entity application and
// the PagerDuty platform-sync wait below.
let agentEndpoint = null;
try {
  const r = await arm("GET", AGENT_URL);
  agentEndpoint = JSON.parse(r.text)?.properties?.agentEndpoint ?? null;
} catch {}

async function applyMcpConnector(name, endpoint, requestHeaders, selectedTools) {
  if (!agentEndpoint) {
    console.log(`-> connector ${name} ... skipped (agent data-plane endpoint not resolved)`);
    return false;
  }

  const extendedProperties = {
    type: "http",
    endpoint,
    authType: "CustomHeaders",
    ...requestHeaders,
    selectedTools,
    toolsVisibleToMetaAgent: selectedTools,
  };
  const properties = {
    dataConnectorType: "Mcp",
    dataSource: new URL(endpoint).origin,
    identity: "",
    endpoint,
    source: "Agent",
    extendedProperties,
  };
  const body = { name, type: "AgentConnector", tags: [], properties };

  process.stdout.write(`-> connector ${name} (ARM) ... `);
  const armResult = await arm("PUT", `${AGENT_URL}/connectors/${encodeURIComponent(name)}`, { properties });
  if (armResult.ok) {
    console.log(`status ${armResult.status}`);
  } else {
    console.log(`status ${armResult.status}; retrying through data plane`);
    const token = azToken(DATAPLANE_SCOPE);
    const response = await fetch(
      `${agentEndpoint.replace(/\/+$/, "")}/api/v2/extendedAgent/connectors/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      console.log(`-> connector ${name} (data plane) ... FAILED ${response.status}`);
      return false;
    }
    console.log(`-> connector ${name} (data plane) ... status ${response.status}`);
  }

  const token = azToken(DATAPLANE_SCOPE);
  const test = await fetch(
    `${agentEndpoint.replace(/\/+$/, "")}/api/v2/extendedAgent/connectors/${encodeURIComponent(name)}/testconnection`,
    {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  const result = await test.json().catch(() => ({}));
  if (!test.ok || !result.success) {
    const secretValues = Object.values(requestHeaders);
    let error = String(result.errorMessage || result.message || `HTTP ${test.status}`);
    for (const secret of secretValues) error = error.replaceAll(String(secret), "[REDACTED]");
    console.log(`-> connector ${name} test ... FAILED: ${error.slice(-600)}`);
    return false;
  }
  console.log(`-> connector ${name} test ... connected (${result.totalCount ?? 0} tools discovered)`);
  return true;
}

async function uploadKnowledge(name, file) {
  if (!agentEndpoint) {
    console.log(`-> knowledge ${name} ... skipped (agent data-plane endpoint not resolved)`);
    return false;
  }

  const content = sub(fs.readFileSync(file, "utf8"));
  const form = new FormData();
  form.append("triggerIndexing", "true");
  form.append("files", new Blob([content], { type: "text/markdown" }), `${name}.md`);
  const token = azToken(DATAPLANE_SCOPE);
  const response = await fetch(`${agentEndpoint.replace(/\/+$/, "")}/api/v1/AgentMemory/upload`, {
    method: "POST",
    headers: { Authorization: "Bearer " + token },
    body: form,
  });
  console.log(`-> knowledge ${name} (data plane) ... ${response.ok ? `status ${response.status}` : `FAILED ${response.status}`}`);
  return response.ok;
}

const MICROSOFT_LEARN_TOOLS = [
  "microsoft_docs_search",
  "microsoft_code_sample_search",
  "microsoft_docs_fetch",
];
const PAGERDUTY_MCP_TOOLS = [
  "pagerduty_get_incident",
  "pagerduty_list_incidents",
  "pagerduty_manage_incidents",
  "pagerduty_add_note_to_incident",
];

// ---- 1. Microsoft Learn MCP connector -----------------------------------
await applyMcpConnector(
  "microsoft-learn",
  "https://learn.microsoft.com/api/mcp",
  {},
  MICROSOFT_LEARN_TOOLS,
);

// ---- 2. Custom tools + skills + agent (with structured tools) -----------
await applyToolsSkillsAndAgents();

// ---- 3. PagerDuty: MCP connector + incident-management platform ----------
// PagerDuty's current remote MCP server uses the global endpoint and the API-key
// header scheme "Token token=...", not the legacy tenant /mcp URL or Bearer auth.
// The agent's incident platform (type/connectionKey) remains a top-level ARM
// property so the runtime scans PagerDuty and accepts PagerDuty incident filters.
if (process.env.PAGERDUTY_API_TOKEN) {
  await applyMcpConnector(
    "pagerduty",
    "https://mcp.pagerduty.com/mcp",
    { Authorization: `Token token=${process.env.PAGERDUTY_API_TOKEN}` },
    PAGERDUTY_MCP_TOOLS,
  );

  process.stdout.write("-> pagerduty incident platform (ARM) ... ");
  // Write actions (acknowledge/resolve/add-note) require PagerDuty's "From" header,
  // which the runtime sends from IncidentManagementSettings.OboUser. Without it PD returns
  // illegitimate_requester_error / code 1027. Resolve a real PD user email: explicit env
  // (PAGERDUTY_FROM_EMAIL/PAGERDUTY_OBO_USER) else the account's first/owner user.
  const oboUser = await resolvePagerDutyOboUser();
  if (oboUser) console.log(`(obo user: ${oboUser})`);
  else console.log("(WARNING: no PagerDuty obo user resolved; write actions will fail)");
  let synced = false;
  try {
    const r = await fetch(`${agentEndpoint}/api/v1/incidentplayground/incidentPlatformType`,
      { headers: { Authorization: "Bearer " + azToken(DATAPLANE_SCOPE) } });
    synced = (await r.json())?.incidentPlatformType === "PagerDuty";
  } catch {}
  const imConfig = {
    type: "PagerDuty",
    connectionName: "pagerduty",
    connectionKey: process.env.PAGERDUTY_API_TOKEN,
  };
  if (oboUser) imConfig.oboUser = oboUser;
  process.stdout.write("-> pagerduty incident platform PATCH ... ");
  const imRes = await arm("PATCH", AGENT_URL, {
    properties: {
      incidentManagementConfiguration: imConfig,
    },
  });
  console.log(imRes.ok ? `status ${imRes.status}` : `FAILED ${imRes.status}\n${imRes.text.slice(0, 400)}`);

  // The runtime syncs the platform from ARM into its reloadable settings store
  // asynchronously; the incident-filter PUT is rejected until it reports PagerDuty.
  process.stdout.write("-> waiting for runtime to sync incident platform ");
  const endpoint = agentEndpoint;
  for (let i = 0; i < 30 && !synced; i++) {
    try {
      const r = await fetch(`${endpoint}/api/v1/incidentplayground/incidentPlatformType`,
        { headers: { Authorization: `Bearer ${azToken(DATAPLANE_AUDIENCE)}` } });
      if ((await r.json())?.incidentPlatformType === "PagerDuty") { synced = true; break; }
    } catch {}
    process.stdout.write(".");
    await sleep(10000);
  }
  console.log(synced ? " ok" : " timed out (filter PUT may fail; re-run script)");
} else {
  console.log("-> pagerduty connector ... skipped (PAGERDUTY_API_TOKEN not set)");
}

// ---- 4. Incident response plan / trigger (symptom-keyed, autonomous) ------
// Created as a Microsoft.App/agents/incidentFilters child resource over ARM.
// Symptom-only: titleContains "Zava" routes every student-facing Zava incident to the
// custom zava-incident-responder agent, which scopes the skill menu (allowedSkills + system
// skills) and prescribes the ordered runbook (triage -> mitigate -> RCA -> evidence ->
// recommendations -> PR -> Change Request -> report) via its instructions + critic-on-handoff
// gate. The cause-level split (NSG vs LB vs AppGW vs app) is decided inside the skills from
// telemetry, never by alert name.
const filterSpec = {
  incidentPlatform: process.env.PAGERDUTY_API_TOKEN ? "PagerDuty" : "AzMonitor",
  titleContains: "Zava",
  agentMode: "autonomous",
  handlingAgent: "zava-incident-responder",
  isEnabled: true,
  maxAutomatedInvestigationAttempts: 3,
};
process.stdout.write("-> incident filter zava-learning-response (ARM) ... ");
const fRes = await arm("PUT", `${AGENT_URL}/incidentFilters/zava-learning-response`, {
  properties: { value: Buffer.from(JSON.stringify(filterSpec)).toString("base64") },
});
console.log(fRes.ok ? `status ${fRes.status}` : `FAILED ${fRes.status}\n${fRes.text.slice(0, 400)}`);

// ---- 5. Knowledge base --------------------------------------------------
const kbFile = path.join(repoRoot, "sre-config", "knowledge-base", "zava-learning-architecture.md");
if (fs.existsSync(kbFile)) {
  await uploadKnowledge("zava-learning-architecture", kbFile);
}

// Reporting standard + report skeleton — retrieved by the reporting skills (rca-analysis,
// evidence-before-after, recommendations-next-steps, zava-reporting) via SearchMemory.
for (const [name, rel] of [
  ["zava-brand", path.join("sre-config", "templates", "zava-brand.md")],
  ["zava-report-template", path.join("sre-config", "templates", "zava-report-template.md")],
  ["zava-audit-report", path.join("sre-config", "templates", "zava-audit-report.md")],
  ["zava-redaction", path.join("sre-config", "templates", "zava-redaction.md")],
]) {
  const f = path.join(repoRoot, rel);
  if (fs.existsSync(f)) {
    await uploadKnowledge(name, f);
  }
}

// ---- 6. Knowledge already applied above. ServiceNow integration is change-management
// only: it is delivered through the CreateServiceNowChangeRequest / UploadServiceNowAttachment
// Python function tools (applied in step 2, owned by the servicenow-change-management skill). The Python
// sandbox cannot read env vars, so SERVICENOW_URL/USER/PASS are injected as literals into the tool
// functionCode at apply time by sub() from the gitignored sre-config/.env (committed source keeps
// @@SERVICENOW_*@@ placeholders). We deliberately do NOT create a ServiceNow *incident* connector
// here — incident management is PagerDuty's job (step 3/4).

// ---- 6. Weekly governance audit scheduled tasks (VERIFY-ONLY) ----------
// Three weekly audits (NSG, RBAC, cost), each run by its own custom ExtendedAgent (applied in
// step 2: zava-nsg-auditor / zava-rbac-auditor / zava-cost-analyst) and producing a branded PPTX
// via the zava-audit-report skill.
//
// These scheduled tasks are USER-MANAGED and intentionally NOT created, deleted, or edited here.
// This script only verifies the expected tasks exist by name (the demo's --audits launches them by
// name). The reference YAML under sre-config/scheduled-tasks/ is kept for documentation only.
// Task name == YAML file stem == spec.name by construction.
async function applyScheduledTasks() {
  const taskSrc = path.join(repoRoot, "sre-config", "scheduled-tasks");
  const expected = fs.existsSync(taskSrc)
    ? fs.readdirSync(taskSrc).filter((f) => /\.ya?ml$/i.test(f)).map((f) => f.replace(/\.ya?ml$/i, ""))
    : [];
  if (expected.length === 0) { console.log("-> scheduled tasks ... none expected, skipped"); return; }
  if (!agentEndpoint) { console.log("-> scheduled tasks ... skipped (agent data-plane endpoint not resolved)"); return; }

  const epBase = agentEndpoint.replace(/\/+$/, "");
  const liveNames = new Set();
  try {
    const token = azToken(DATAPLANE_SCOPE);
    const r = await fetch(`${epBase}/api/v1/scheduledtasks`, { headers: { Authorization: `Bearer ${token}` } });
    if (r.ok) {
      const list = await r.json();
      if (Array.isArray(list)) for (const t of list) { if (t?.name) liveNames.add(t.name); }
    }
  } catch {}

  // Verify only — never create/delete/edit (these tasks are user-managed).
  for (const name of expected) {
    console.log(liveNames.has(name)
      ? `-> scheduledtask ${name} (user-managed) ... present`
      : `-> scheduledtask ${name} (user-managed) ... MISSING — create it manually (reference YAML in sre-config/scheduled-tasks/)`);
  }
}
await applyScheduledTasks();

console.log("\nDone. Verify with: node scripts/configure-agent.mjs (idempotent) or `azmcp sreagent skills list`.");
