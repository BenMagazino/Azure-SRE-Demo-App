const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "static", "app.js"),
  "utf8"
);
const start = source.indexOf("function resetEnvironmentDiscovery()");
const end = source.indexOf("async function saveEnvironmentConfiguration()", start);
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    elements.set(selector, {
      textContent: "",
      disabled: false,
      children: [],
      setAttribute() {},
      removeAttribute() {},
      replaceChildren(...children) { this.children = children; },
    });
  }
  return elements.get(selector);
}
const pending = [];
const rendered = [];
const context = vm.createContext({
  environmentDiscoveryRequestId: 0,
  selectedLabId: "grubify",
  selectedExistingEnvironment: { environment: "sre-lab" },
  skipDeploymentForValidatedEnvironment: true,
  document: { querySelector: element },
  clearExistingEnvironmentSelection() {
    context.selectedExistingEnvironment = null;
    context.skipDeploymentForValidatedEnvironment = false;
    element("#validate-environment").disabled = true;
    element("#skip-environment-validation").disabled = true;
    element("#environment-validation-status").textContent = "Select an existing lab.";
  },
  fetch() {
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  },
  renderExistingEnvironments(payload) { rendered.push(payload); },
});
vm.runInContext(source.slice(start, end), context);
const response = (environment) => ({
  ok: true,
  headers: { get: () => "application/json" },
  json: async () => ({ environments: [environment] }),
});

async function run() {
  element("#existing-environment-list").children = ["Grubify card"];
  element("#environment-validation-status").textContent = "Old skip status";
  const oldScan = context.loadExistingEnvironments();
  assert.deepEqual(element("#existing-environment-list").children, []);
  assert.equal(context.selectedExistingEnvironment, null);
  assert.equal(context.skipDeploymentForValidatedEnvironment, false);
  assert.equal(element("#skip-environment-validation").disabled, true);
  assert.equal(element("#validate-environment").disabled, true);
  assert.equal(element("#environment-validation-status").textContent, "Select an existing lab.");

  context.selectedLabId = "zava-learning";
  context.resetEnvironmentDiscovery();
  const newScan = context.loadExistingEnvironments();
  pending[0].resolve(response("Grubify"));
  await oldScan;
  assert.deepEqual(rendered, []);
  assert.equal(element("#refresh-environments").disabled, true);
  pending[1].resolve(response("Zava"));
  await newScan;
  assert.equal(rendered[0].environments[0], "Zava");
  assert.equal(element("#refresh-environments").disabled, false);

  const staleFailure = context.loadExistingEnvironments();
  const latestScan = context.loadExistingEnvironments();
  pending[2].reject(new Error("Old request failed"));
  await staleFailure;
  assert.match(element("#environment-discovery-status").textContent, /^Scanning/);
  assert.equal(element("#refresh-environments").disabled, true);
  pending[3].resolve(response("Latest Zava"));
  await latestScan;
  assert.equal(rendered.at(-1).environments[0], "Latest Zava");

  const failedScan = context.loadExistingEnvironments();
  pending[4].reject(new Error("Discovery unavailable"));
  await failedScan;
  assert.match(element("#environment-discovery-status").textContent, /Discovery unavailable/);
  assert.equal(element("#refresh-environments").disabled, false);
  console.log("Environment discovery reset, stale response, overlap, and failure tests passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
