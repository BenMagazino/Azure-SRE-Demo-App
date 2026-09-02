const prereqList = document.querySelector("#prereq-list");
const continueButton = document.querySelector("#continue-to-auth");
const installAllButton = document.querySelector("#install-all");
const installAllProgress = document.querySelector("#install-all-progress");
const azureContextCard = document.querySelector("#azure-context-card");
const azureContextLoadingCard = document.querySelector("#azure-context-loading");
const azureTenant = document.querySelector("#azure-tenant");
const azureSubscription = document.querySelector("#azure-subscription");
const azureContextStatus = document.querySelector("#azure-context-status");
const applyAzureContextButton = document.querySelector("#apply-azure-context");
const validateEnvironmentButton = document.querySelector("#validate-environment");
const environmentValidationStatus = document.querySelector(
  "#environment-validation-status"
);
const teardownButton = document.querySelector("#teardown");
const investigationCountdown = document.querySelector("#investigation-countdown");
const investigationCountdownValue = document.querySelector("#investigation-countdown-value");
const investigationCountdownProgress = document.querySelector("#investigation-countdown-progress");
const investigationCountdownStatus = document.querySelector("#investigation-countdown-status");
const edgeProfileSelect = document.querySelector("#edge-profile");
const edgeProfileStatus = document.querySelector("#edge-profile-status");
const backButton = document.querySelector("#back");
const shutdownButton = document.querySelector("#shutdown");
const shell = document.querySelector(".shell");
const workflowPanels = [
  "labs",
  "prerequisites",
  "authentication",
  "configuration",
  "deployment",
  "summary",
];
const heartbeatIntervalMilliseconds = 10000;
const authStatus = { "azure-cli": false, azd: false };
let sessionToken = "";
let installInProgress = false;
let azureContextCatalog = null;
let labCatalog = [];
let persistedLabId = "";
let selectedLabId = "";
let selectedScenarioId = "";
let azureContextApplied = false;
let selectedExistingEnvironment = null;
let skipDeploymentForValidatedEnvironment = false;
let currentSummary = null;
let activePanelId = "labs";
let heartbeatTimer = null;
let investigationCountdownTimer = null;
let investigationCountdownState = "idle";
let investigationCountdownEnd = 0;
let investigationCountdownDuration = 0;
let edgeProfilesLoaded = false;

async function loadSessionToken() {
  const retryDelays = [0, 250, 500, 1000];
  let lastError = new Error("Local application API did not respond.");
  for (const delay of retryDelays) {
    if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      const contentType = response.headers.get("Content-Type") || "";
      if (!response.ok || !contentType.includes("application/json")) {
        throw new Error(
          `/api/session returned HTTP ${response.status} with ${contentType || "unknown content"}`
        );
      }
      const session = await response.json();
      if (typeof session.token !== "string" || !session.token) {
        throw new Error("/api/session returned an invalid session token.");
      }
      return session.token;
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`Local application API was not ready: ${lastError.message}`);
}

async function initialize() {
  sessionToken = await loadSessionToken();
  await sendClientHeartbeat();
  startClientHeartbeat();
  await loadLabs();
  if (persistedLabId) await loadPrerequisites();
  await loadAuthStatus();
}

async function recheckApplication() {
  if (!sessionToken) {
    prereqList.textContent = "Reconnecting to the local application...";
    await initialize();
    return;
  }
  if (!persistedLabId) {
    showPanel("labs");
    return;
  }
  await loadPrerequisites();
}

function apiPost(path, body) {
  const options = {
    method: "POST",
    headers: { "X-SRE-Session": sessionToken },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  return fetch(path, options);
}

async function sendClientHeartbeat() {
  const response = await apiPost("/api/heartbeat");
  if (!response.ok) throw new Error("The local application heartbeat failed.");
}

function startClientHeartbeat() {
  if (heartbeatTimer !== null) return;
  heartbeatTimer = window.setInterval(() => {
    void sendClientHeartbeat().catch((error) => {
      console.warn("Unable to refresh the local application heartbeat.", error);
    });
  }, heartbeatIntervalMilliseconds);
}

function reportClientError(message) {
  if (!sessionToken) return;
  void apiPost("/api/client-log", {
    message: String(message).slice(0, 2000),
  }).catch(() => {});
}

window.addEventListener("error", (event) => {
  reportClientError(`${event.message} at ${event.filename}:${event.lineno}:${event.colno}`);
});

window.addEventListener("unhandledrejection", (event) => {
  reportClientError(`Unhandled promise rejection: ${event.reason}`);
});

function showPanel(id) {
  if (!workflowPanels.includes(id)) return;
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.add("hidden"));
  document.querySelector(`#${id}`).classList.remove("hidden");
  activePanelId = id;
  shell.classList.toggle("workflow-compact", id !== "labs");
  backButton.disabled = workflowPanels.indexOf(id) === 0;
  document.querySelectorAll(".steps [data-panel]").forEach((step) => {
    const active = step.dataset.panel === id;
    step.classList.toggle("active", active);
    if (active) {
      step.setAttribute("aria-current", "step");
    } else {
      step.removeAttribute("aria-current");
    }
  });
}

function navigateBack() {
  if (activePanelId === "summary" && skipDeploymentForValidatedEnvironment) {
    showPanel("configuration");
    return;
  }
  const currentIndex = workflowPanels.indexOf(activePanelId);
  if (currentIndex > 0) showPanel(workflowPanels[currentIndex - 1]);
}

function showShutdownComplete() {
  const shell = document.createElement("main");
  shell.className = "shell";
  const panel = document.createElement("section");
  panel.className = "panel";
  const heading = document.createElement("h1");
  heading.textContent = "Application stopped";
  const message = document.createElement("p");
  message.textContent = "The local backend has stopped. You can close this tab if your browser did not close it automatically.";
  panel.append(heading, message);
  shell.append(panel);
  document.body.replaceChildren(shell);
}

async function shutdownApplication() {
  if (!window.confirm("Stop the local Azure SRE Agent Demo application?")) return;
  shutdownButton.disabled = true;
  shutdownButton.setAttribute("aria-busy", "true");
  shutdownButton.textContent = "Stopping...";
  try {
    const response = await apiPost("/api/shutdown");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to stop the application.");
    if (heartbeatTimer !== null) {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    showShutdownComplete();
    window.setTimeout(() => window.close(), 150);
  } catch (error) {
    shutdownButton.disabled = false;
    shutdownButton.removeAttribute("aria-busy");
    shutdownButton.textContent = "Shutdown";
    window.alert(`Shutdown failed: ${error}`);
  }
}

function currentLab() {
  return labCatalog.find((lab) => lab.id === selectedLabId) || null;
}

function renderLabPicker() {
  const picker = document.querySelector("#lab-picker");
  picker.replaceChildren();
  labCatalog.forEach((lab) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `picker-card${lab.id === selectedLabId ? " selected" : ""}`;
    button.setAttribute("aria-pressed", lab.id === selectedLabId ? "true" : "false");
    const title = document.createElement("span");
    title.className = "picker-title";
    title.textContent = lab.name;
    const description = document.createElement("span");
    description.className = "picker-description";
    description.textContent = lab.description;
    const meta = document.createElement("span");
    meta.className = "picker-meta";
    meta.textContent = `${lab.dependency_ids.length} dependencies · ${lab.scenarios.length} scenario${lab.scenarios.length === 1 ? "" : "s"} · ${lab.resource_count} resource${lab.resource_count === 1 ? "" : "s"} · ${lab.estimated_turnaround} turnaround`;
    button.append(title, description, meta);
    button.addEventListener("click", () => {
      selectedLabId = lab.id;
      renderLabPicker();
      document.querySelector("#continue-lab").disabled = false;
    });
    picker.append(button);
  });
  document.querySelector("#continue-lab").disabled = !selectedLabId;
}

function updateLabCopy() {
  const lab = currentLab();
  if (!lab) return;
  document.querySelector("#prerequisite-copy").textContent = `${lab.name} requires the tools below. Azure CLI and Azure Developer CLI are installed privately without administrator approval.`;
  document.querySelector("#configure-copy").textContent = `Choose the Azure environment for ${lab.name}.`;
  document.querySelector("#deploy-title").textContent = `Deploy ${lab.name}`;
  document.querySelector("#deploy-copy").textContent = `${lab.description} Azure resources and lab automation are created automatically.`;
}

async function loadLabs() {
  const response = await fetch("/api/labs", { cache: "no-store" });
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error(
      "The running backend does not support the Lab Picker. Restart the local application."
    );
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Unable to load labs.");
  labCatalog = payload.labs || [];
  persistedLabId = payload.selected_lab_id || "";
  selectedLabId = persistedLabId || labCatalog[0]?.id || "";
  selectedScenarioId = payload.selected_scenario_id || "";
  renderLabPicker();
  updateLabCopy();
}

async function persistSelectedLab() {
  const response = await apiPost("/api/lab", { lab_id: selectedLabId });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Unable to select the lab.");
  persistedLabId = result.lab.id;
  selectedLabId = result.lab.id;
  updateLabCopy();
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    return copied;
  }
}

async function showDeviceCode(device, event) {
  const copied = await copyToClipboard(event.code);
  const code = document.createElement("strong");
  code.textContent = event.code;

  const notice = document.createElement("p");
  notice.className = `device-notice${copied ? "" : " warning"}`;
  notice.setAttribute("role", "status");
  notice.textContent = copied
    ? "Device code copied to your clipboard."
    : "Automatic copy was blocked. Use Copy code below.";

  const actions = document.createElement("div");
  actions.className = "device-actions";
  const copy = document.createElement("button");
  copy.className = "secondary";
  copy.textContent = copied ? "Copy again" : "Copy code";
  copy.addEventListener("click", async () => {
    const retryCopied = await copyToClipboard(event.code);
    notice.classList.toggle("warning", !retryCopied);
    notice.textContent = retryCopied
      ? "Device code copied to your clipboard."
      : "Clipboard access is blocked. Select and copy the code above.";
  });
  const link = document.createElement("a");
  link.href = event.verification_url;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = event.browser_opened
    ? "Microsoft sign-in opened in your browser"
    : "Open Microsoft sign-in";
  actions.append(copy, link);
  device.replaceChildren(code, notice, actions);
  device.classList.remove("hidden");
}

function showMfaNotice(device) {
  if (device.querySelector(".mfa-notice")) return;
  const notice = document.createElement("p");
  notice.className = "mfa-notice";
  notice.setAttribute("role", "status");
  notice.textContent =
    "Your organization requires additional MFA. Complete the Microsoft browser prompt; " +
    "you do not need to run the commands shown in the log.";
  device.append(notice);
  device.classList.remove("hidden");
}

function showAuthComplete(device, button, kind, existingSession = false) {
  const title = document.createElement("div");
  title.className = "auth-result-title";
  title.textContent = "Signed in";
  const detail = document.createElement("p");
  detail.className = "auth-result-detail";
  if (existingSession) {
    detail.textContent = kind === "azure-cli"
      ? "An existing Azure CLI session is ready."
      : "An existing Azure Developer CLI session is ready.";
  } else {
    detail.textContent = kind === "azure-cli"
      ? "Azure CLI authentication completed successfully."
      : "Azure Developer CLI authentication completed successfully.";
  }
  device.replaceChildren(title, detail);
  device.classList.add("complete");
  device.classList.remove("hidden");
  button.textContent = kind === "azure-cli"
    ? "Signed in with Azure CLI"
    : "Signed in with azd";
  button.disabled = true;
}

function updateAuthenticationGate() {
  document.querySelector("#continue-to-configure").disabled =
    !Object.values(authStatus).every(Boolean) || !azureContextApplied;
}

function selectedAzureContext() {
  if (!azureTenant.value || !azureSubscription.value) return null;
  return {
    tenant_id: azureTenant.value,
    subscription_id: azureSubscription.value,
  };
}

function azureOptionLabel(name, id) {
  return name && name !== id ? `${name} (${id})` : id;
}

function populateSubscriptions(preferredSubscription = "") {
  const tenant = azureContextCatalog?.tenants.find(
    (item) => item.id === azureTenant.value
  );
  const subscriptions = tenant?.subscriptions || [];
  azureSubscription.replaceChildren(...subscriptions.map((subscription) => {
    const option = document.createElement("option");
    option.value = subscription.id;
    option.textContent = azureOptionLabel(subscription.name, subscription.id);
    return option;
  }));
  if (subscriptions.some((item) => item.id === preferredSubscription)) {
    azureSubscription.value = preferredSubscription;
  }
}

function markAzureContextChanged() {
  azureContextApplied = false;
  azureContextStatus.className = "";
  azureContextStatus.textContent = "Apply this selection before continuing.";
  applyAzureContextButton.disabled = false;
  applyAzureContextButton.textContent = "Use selected subscription";
  updateAuthenticationGate();
}

function setAzureContextLoading(loading) {
  azureContextLoadingCard.classList.toggle("hidden", !loading);
  azureContextLoadingCard.setAttribute("aria-busy", loading ? "true" : "false");
}

async function loadAzureContexts() {
  const showLoadingCard = azureContextCard.classList.contains("hidden");
  if (showLoadingCard) setAzureContextLoading(true);
  try {
    const response = await fetch("/api/azure-context", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) {
      azureContextCatalog = null;
      azureContextApplied = false;
      azureContextCard.classList.toggle("hidden", !authStatus["azure-cli"]);
      azureContextStatus.className = "warning";
      azureContextStatus.textContent = result.error || "Unable to load Azure accounts.";
      updateAuthenticationGate();
      return;
    }

    azureContextCatalog = result;
    azureContextCard.classList.remove("hidden");
    azureTenant.replaceChildren(...result.tenants.map((tenant) => {
      const option = document.createElement("option");
      option.value = tenant.id;
      option.textContent = azureOptionLabel(tenant.name, tenant.id);
      return option;
    }));

    const active = result.active || {};
    if (result.tenants.some((tenant) => tenant.id === active.tenant)) {
      azureTenant.value = active.tenant;
    }
    populateSubscriptions(active.subscription || "");
    const selected = selectedAzureContext();
    azureContextApplied = Boolean(
      selected
      && selected.tenant_id === active.tenant
      && selected.subscription_id === active.subscription
      && authStatus["azure-cli"]
    );
    azureContextStatus.className = azureContextApplied ? "success" : "";
    azureContextStatus.textContent = azureContextApplied
      ? "Active and authenticated. This subscription will be used for deployment."
      : "Choose and apply a subscription before continuing.";
    applyAzureContextButton.disabled = azureContextApplied;
    applyAzureContextButton.textContent = azureContextApplied
      ? "Active subscription"
      : "Use selected subscription";
    updateAuthenticationGate();
  } catch (error) {
    azureContextCatalog = null;
    azureContextApplied = false;
    azureContextCard.classList.toggle("hidden", !authStatus["azure-cli"]);
    azureContextStatus.className = "warning";
    azureContextStatus.textContent = `Unable to load Azure accounts: ${error}`;
    updateAuthenticationGate();
    throw error;
  } finally {
    if (showLoadingCard) setAzureContextLoading(false);
  }
}

async function applyAzureContext() {
  const selected = selectedAzureContext();
  if (!selected) return;
  applyAzureContextButton.disabled = true;
  azureContextStatus.className = "";
  azureContextStatus.textContent = "Validating the selected subscription...";
  try {
    const response = await apiPost("/api/azure-context", selected);
    const result = await response.json();
    if (!response.ok) {
      azureContextApplied = false;
      azureContextStatus.className = "warning";
      azureContextStatus.textContent = result.error || "Unable to apply the Azure context.";
      if (result.requires_auth) {
        authStatus["azure-cli"] = false;
        const authButton = document.querySelector('[data-auth="azure-cli"]');
        authButton.disabled = false;
        authButton.textContent = "Authenticate selected tenant";
      }
      applyAzureContextButton.disabled = false;
      updateAuthenticationGate();
      return;
    }
    authStatus["azure-cli"] = true;
    azureContextApplied = true;
    azureContextStatus.className = "success";
    azureContextStatus.textContent =
      "Active and authenticated. This subscription will be used for deployment.";
    applyAzureContextButton.textContent = "Active subscription";
  } catch (error) {
    azureContextApplied = false;
    azureContextStatus.className = "warning";
    azureContextStatus.textContent = `Unable to apply the Azure context: ${error}`;
    applyAzureContextButton.disabled = false;
  }
  updateAuthenticationGate();
}

async function loadAuthStatus() {
  const response = await fetch("/api/auth/status");
  if (!response.ok) return;
  const statuses = await response.json();
  Object.entries(statuses).forEach(([kind, signedIn]) => {
    authStatus[kind] = signedIn;
    if (!signedIn) return;
    const button = document.querySelector(`[data-auth="${kind}"]`);
    const device = document.querySelector(`#${kind}-device`);
    showAuthComplete(device, button, kind, true);
  });
  if (authStatus["azure-cli"]) {
    await loadAzureContexts();
  } else {
    azureContextCard.classList.add("hidden");
    azureContextApplied = false;
  }
  updateAuthenticationGate();
}

async function loadPrerequisites() {
  prereqList.textContent = "Checking installed tools...";
  continueButton.disabled = true;
  try {
    const response = await fetch("/api/prerequisites");
    const tools = await response.json();
    if (!response.ok) {
      throw new Error(tools.error || "Unable to check prerequisites.");
    }
    prereqList.replaceChildren(...tools.map(renderTool));
    const unresolvedRequired = tools
      .filter((tool) => tool.required)
      .filter((tool) => !tool.ready);
    continueButton.disabled = unresolvedRequired.length > 0;
    installAllButton.classList.toggle("hidden", unresolvedRequired.length === 0);
    setInstallerButtonsDisabled(installInProgress);
  } catch (error) {
    prereqList.textContent = `Unable to check prerequisites: ${error}`;
  }
}

function setInstallerButtonsDisabled(disabled) {
  document.querySelectorAll(".install-tool").forEach((item) => {
    item.disabled = disabled;
  });
  installAllButton.disabled = disabled;
}

function renderTool(tool) {
  const row = document.createElement("div");
  row.className = `tool ${tool.ready ? "ok" : tool.state}`;
  row.dataset.toolId = tool.id;
  const status = document.createElement("span");
  status.className = "status";
  status.textContent = tool.ready ? "OK" : tool.state === "outdated" ? "UP" : "!";
  const name = document.createElement("strong");
  name.textContent = tool.name;
  const version = document.createElement("span");
  version.className = "version";
  if (tool.ready) {
    version.textContent = `${tool.version} (minimum ${tool.minimum_version})`;
  } else if (tool.state === "outdated") {
    version.textContent = `${tool.version} installed; requires ${tool.minimum_version}+`;
  } else if (tool.state === "invalid") {
    version.textContent = `Version check failed; requires ${tool.minimum_version}+`;
  } else {
    version.textContent = `Missing; requires ${tool.minimum_version}+`;
  }
  row.append(status, name, version);

  if (!tool.ready) {
    const install = document.createElement("div");
    install.className = "install";
    const code = document.createElement("code");
    const appManaged = ["az", "azd"].includes(tool.id);
    code.textContent = appManaged
      ? "App-managed user-profile installation (no UAC)"
      : tool.install_command;
    const copy = document.createElement("button");
    copy.className = "secondary";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(tool.install_command);
      copy.textContent = "Copied";
    });
    const installButton = document.createElement("button");
    installButton.className = "install-tool";
    installButton.textContent = {
      missing: "Install",
      outdated: "Update",
      invalid: "Repair",
    }[tool.state];
    installButton.addEventListener("click", () => installTool(tool, installButton, progress));
    const link = document.createElement("a");
    link.href = tool.install_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Installer documentation";
    const progress = document.createElement("pre");
    progress.className = "install-progress hidden";
    progress.setAttribute("aria-live", "polite");
    install.append(installButton, code);
    if (!appManaged) install.append(copy);
    install.append(link, progress);
    row.append(install);
  }
  return row;
}

function updateToolStatus(event) {
  const row = prereqList.querySelector(`[data-tool-id="${event.tool_id}"]`);
  if (!row) return;

  const status = row.querySelector(".status");
  const version = row.querySelector(".version");
  row.classList.remove("ok", "missing", "outdated", "invalid", "installing", "failed");
  if (["installing", "updating", "repairing"].includes(event.status)) {
    row.classList.add("installing");
    status.textContent = "...";
    version.textContent = `${event.status[0].toUpperCase()}${event.status.slice(1)}...`;
    return;
  }
  if (event.status === "ready") {
    row.classList.add("ok");
    status.textContent = "OK";
    version.textContent = event.version || "Installed";
    row.querySelector(".install")?.classList.add("hidden");
    return;
  }
  row.classList.add("failed");
  status.textContent = "!";
  version.textContent = "Install failed";
}

async function refreshInstalledToolTiles() {
  try {
    const response = await fetch("/api/prerequisites", { cache: "no-store" });
    if (!response.ok) return;
    const tools = await response.json();
    tools
      .filter((tool) => tool.ready)
      .forEach((tool) => updateToolStatus({
        tool_id: tool.id,
        status: "ready",
        version: tool.version,
      }));
  } catch (error) {
    console.warn("Unable to refresh dependency tile status.", error);
  }
}

async function installTool(tool, button, progress) {
  const action = {
    missing: "installation",
    outdated: "update",
    invalid: "repair",
  }[tool.state];
  installInProgress = true;
  setInstallerButtonsDisabled(true);
  button.textContent = `${action[0].toUpperCase()}${action.slice(1)} in progress...`;
  progress.textContent = `Starting ${tool.name} ${action}...\n`;
  progress.classList.remove("hidden");
  try {
    const response = await apiPost(`/api/install/${tool.id}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start installation");
    const completed = await streamJob(result.job_id, progress, {
      onEvent: updateToolStatus,
    });
    if (completed.success) {
      progress.textContent += `\n${action[0].toUpperCase()}${action.slice(1)} completed. Re-checking prerequisites...\n`;
      installInProgress = false;
      await loadPrerequisites();
      return;
    }
    const fallback = tool.id === "az"
      ? "review the installer documentation"
      : "use the fallback command";
    progress.textContent += `\n${action[0].toUpperCase()}${action.slice(1)} failed. Review the output above or ${fallback}.\n`;
  } catch (error) {
    progress.textContent += `\n${action[0].toUpperCase()}${action.slice(1)} failed: ${error}\n`;
  }
  installInProgress = false;
  button.textContent = `Retry ${action}`;
  setInstallerButtonsDisabled(false);
}

async function installAll() {
  installInProgress = true;
  setInstallerButtonsDisabled(true);
  installAllButton.textContent = "Updating dependencies...";
  installAllProgress.textContent =
    "Installing, repairing, or updating dependencies sequentially. This may take several minutes...\n";
  installAllProgress.classList.remove("hidden");
  try {
    const response = await apiPost("/api/install/all");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start installation");
    const statusPoll = window.setInterval(() => {
      void refreshInstalledToolTiles();
    }, 1500);
    let completed;
    try {
      completed = await streamJob(result.job_id, installAllProgress, {
        onEvent: updateToolStatus,
      });
    } finally {
      window.clearInterval(statusPoll);
    }
    await refreshInstalledToolTiles();
    installAllProgress.textContent += completed.success
      ? "\nDependency updates completed. Re-checking prerequisites...\n"
      : "\nSome dependencies could not be installed or updated. Review the output above and retry them individually.\n";
  } catch (error) {
    installAllProgress.textContent += `\nDependency update failed: ${error}\n`;
  }
  installInProgress = false;
  installAllButton.textContent = "Resolve all dependencies";
  await loadPrerequisites();
}

async function startAuth(kind) {
  const button = document.querySelector(`[data-auth="${kind}"]`);
  const log = document.querySelector(`#${kind}-log`);
  const device = document.querySelector(`#${kind}-device`);
  button.disabled = true;
  if (kind === "azure-cli") {
    authStatus[kind] = false;
    azureContextApplied = false;
    updateAuthenticationGate();
  }
  device.classList.add("hidden");
  if (kind === "azure-cli") {
    log.textContent = "Starting secure Azure CLI device-code sign-in...\n";
    const notice = document.createElement("p");
    notice.className = "device-notice";
    notice.setAttribute("role", "status");
    notice.textContent =
      "Waiting for an Azure CLI device code. It will be copied and opened automatically. Conditional Access may require one additional code.";
    device.replaceChildren(notice);
    device.classList.remove("hidden");
  } else {
    log.textContent = "Starting device-code sign-in...\n";
  }

  try {
    const context = kind === "azure-cli" && !azureContextCard.classList.contains("hidden")
      ? selectedAzureContext()
      : undefined;
    const response = await apiPost(`/api/auth/${kind}`, context);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start sign-in");
    const events = new EventSource(`/api/jobs/${result.job_id}/events`);
    let completed = false;
    events.onmessage = ({ data }) => {
      try {
        const event = JSON.parse(data);
        if (event.type === "output") {
          log.textContent += `${event.line}\n`;
          log.scrollTop = log.scrollHeight;
          if (/AADSTS50076|continue the login in the pop-up window/i.test(event.line)) {
            showMfaNotice(device);
          }
        }
        if (event.type === "device_code") {
          void showDeviceCode(device, event).catch((error) => {
            log.textContent += `Unable to display the device code: ${error}\n`;
            reportClientError(`Unable to display the device code: ${error}`);
          });
        }
        if (event.type === "auth_phase") {
          const notice = device.querySelector(".device-notice");
          if (notice) notice.textContent = event.message;
          device.classList.remove("hidden");
        }
        if (event.type === "error") {
          log.textContent += `ERROR: ${event.message}\n`;
        }
        if (event.type === "done") {
          completed = true;
          log.textContent += event.success
            ? "\nSign-in completed.\n"
            : `\nSign-in failed (exit ${event.exit_code}).\n`;
          authStatus[kind] = event.success;
          if (event.success) {
            showAuthComplete(device, button, kind);
            if (kind === "azure-cli") {
              void loadAzureContexts().catch((error) => {
                azureContextStatus.className = "warning";
                azureContextStatus.textContent = `Unable to load Azure accounts: ${error}`;
                reportClientError(`Unable to load Azure accounts: ${error}`);
              });
            }
          } else {
            button.disabled = false;
          }
          updateAuthenticationGate();
          events.close();
        }
      } catch (error) {
        completed = true;
        log.textContent += `Sign-in event processing failed: ${error}\n`;
        reportClientError(`Sign-in event processing failed: ${error}`);
        events.close();
        if (!authStatus[kind]) {
          button.disabled = false;
        }
      }
    };
    events.onerror = () => {
      if (completed) return;
      completed = true;
      log.textContent +=
        "Sign-in event stream was interrupted. Review the diagnostic log and retry.\n";
      reportClientError(`Sign-in event stream interrupted for ${kind}`);
      events.close();
      if (!authStatus[kind]) button.disabled = false;
    };
  } catch (error) {
    log.textContent += `Failed to start sign-in: ${error}\n`;
    button.disabled = false;
  }
}

function streamJob(jobId, log, options = {}) {
  return new Promise((resolve) => {
    const events = new EventSource(`/api/jobs/${jobId}/events`);
    events.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      options.onEvent?.(event);
      if (event.type === "output") {
        log.textContent += `${event.line}\n`;
        log.scrollTop = log.scrollHeight;
      }
      if (event.type === "command") log.textContent += `> ${event.command.join(" ")}\n`;
      if (event.type === "step") {
        if (options.stepElement) {
          options.stepElement.textContent = event.name;
        } else {
          log.textContent += `\n${event.name}...\n`;
          log.scrollTop = log.scrollHeight;
        }
      }
      if (event.type === "error") log.textContent += `ERROR: ${event.message}\n`;
      if (event.type === "done") {
        events.close();
        resolve(event);
      }
    };
  });
}

function azureLocationLabel(location) {
  const option = [...document.querySelector("#azure-location").options]
    .find((item) => item.value === location);
  return option?.textContent || location;
}

function selectExistingEnvironment(environment) {
  selectedExistingEnvironment = environment;
  skipDeploymentForValidatedEnvironment = false;
  document.querySelector("#environment-name").value = environment.environment;
  document.querySelector("#azure-location").value = environment.location;
  validateEnvironmentButton.disabled = false;
  environmentValidationStatus.className = "";
  environmentValidationStatus.textContent =
    `Validate ${environment.environment} to confirm it is ready for the demo.`;
  document.querySelector("#configure-status").textContent =
    `${environment.environment} selected. Validation checks only this lab.`;
  document.querySelectorAll(".environment-card").forEach((card) => {
    const selected = card.dataset.environment === environment.environment;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function renderExistingEnvironments(payload) {
  const list = document.querySelector("#existing-environment-list");
  const status = document.querySelector("#environment-discovery-status");
  const environments = payload.environments || [];
  validateEnvironmentButton.disabled = true;
  environmentValidationStatus.className = "";
  environmentValidationStatus.textContent =
    "Select an existing lab to validate only that environment.";
  list.replaceChildren(...environments.map((environment) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "picker-card environment-card";
    button.dataset.environment = environment.environment;
    button.setAttribute("aria-pressed", "false");

    const title = document.createElement("span");
    title.className = "picker-title";
    title.textContent = environment.environment;
    const description = document.createElement("span");
    description.className = "picker-description";
    description.textContent =
      `${environment.resource_group} · ${azureLocationLabel(environment.location)}`;
    const meta = document.createElement("span");
    meta.className = "picker-meta";
    const detection = environment.detection === "managed"
      ? "Tagged lab"
      : "Compatible legacy lab";
    meta.textContent = environment.local
      ? `${detection} · Known to local azd`
      : `${detection} · Will be added to local azd`;
    button.append(title, description, meta);
    button.addEventListener("click", () => selectExistingEnvironment(environment));
    return button;
  }));

  if (payload.warning) {
    status.className = "warning";
    status.textContent = payload.warning;
  } else if (environments.length) {
    status.className = "success";
    status.textContent =
      `${environments.length} compatible lab environment${environments.length === 1 ? "" : "s"} found in Azure.`;
  } else {
    status.className = "";
    status.textContent =
      "No compatible lab environments were found. Configure a new environment below.";
  }
}

async function loadExistingEnvironments() {
  const status = document.querySelector("#environment-discovery-status");
  const refresh = document.querySelector("#refresh-environments");
  refresh.disabled = true;
  refresh.setAttribute("aria-busy", "true");
  refresh.textContent = "Scanning";
  selectedExistingEnvironment = null;
  skipDeploymentForValidatedEnvironment = false;
  validateEnvironmentButton.disabled = true;
  status.className = "";
  status.textContent =
    "Scanning the selected subscription and checking local azd environments...";
  try {
    const response = await fetch("/api/environments", { cache: "no-store" });
    const contentType = response.headers.get("Content-Type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error("Restart the local application to enable environment discovery.");
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Environment discovery failed.");
    }
    renderExistingEnvironments(payload);
  } catch (error) {
    status.className = "warning";
    status.textContent = `Unable to scan for existing labs: ${error}`;
    document.querySelector("#existing-environment-list").replaceChildren();
  } finally {
    refresh.disabled = false;
    refresh.removeAttribute("aria-busy");
    refresh.textContent = "Scan subscription";
  }
}

async function saveEnvironmentConfiguration() {
  const response = await apiPost("/api/configure", {
    environment: document.querySelector("#environment-name").value,
    location: document.querySelector("#azure-location").value,
    existing_environment: Boolean(selectedExistingEnvironment),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || "Configuration failed.");
  }
  return result;
}

function prepareExistingLabUpdate(issues = []) {
  const deployButton = document.querySelector("#start-deploy");
  deployButton.textContent = "Update existing lab";
  document.querySelector("#deploy-step").textContent = issues.length
    ? `Validation found: ${issues.join(" ")}`
    : "Update the existing Azure resources to match the current lab definition.";
}

async function configureEnvironment(event) {
  event.preventDefault();
  const status = document.querySelector("#configure-status");
  status.className = "";
  status.textContent = "Saving environment...";
  try {
    const result = await saveEnvironmentConfiguration();
    skipDeploymentForValidatedEnvironment = false;
    if (result.existing_environment) {
      status.textContent =
        `Existing lab selected: ${result.environment} in ${result.location}.`;
      prepareExistingLabUpdate();
    } else {
      status.textContent = `Ready: ${result.environment} in ${result.location}`;
      document.querySelector("#start-deploy").textContent = "Deploy lab";
      document.querySelector("#deploy-step").textContent = "";
    }
    showPanel("deployment");
  } catch (error) {
    status.className = "warning";
    status.textContent = `Unable to save the environment: ${error.message}`;
  }
}

async function validateExistingEnvironment() {
  const environment = selectedExistingEnvironment;
  if (!environment) return;

  validateEnvironmentButton.disabled = true;
  validateEnvironmentButton.setAttribute("aria-busy", "true");
  validateEnvironmentButton.textContent = "Validating";
  environmentValidationStatus.className = "";
  environmentValidationStatus.textContent =
    `Checking ${environment.environment} without scanning other labs...`;
  try {
    await saveEnvironmentConfiguration();
    const response = await apiPost("/api/environments/validate", {
      environment: environment.environment,
      resource_group: environment.resource_group,
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "Lab validation failed.");
    }
    if (result.ready) {
      environmentValidationStatus.className = "success";
      environmentValidationStatus.textContent =
        `${environment.environment} is ready. Opening the demo.`;
      skipDeploymentForValidatedEnvironment = true;
      await loadSummary();
      showPanel("summary");
      return;
    }

    skipDeploymentForValidatedEnvironment = false;
    environmentValidationStatus.className = "warning";
    environmentValidationStatus.textContent =
      `${environment.environment} needs an update before the demo.`;
    prepareExistingLabUpdate(result.issues || []);
    showPanel("deployment");
  } catch (error) {
    skipDeploymentForValidatedEnvironment = false;
    environmentValidationStatus.className = "warning";
    environmentValidationStatus.textContent =
      `Unable to validate ${environment.environment}: ${error.message}`;
  } finally {
    validateEnvironmentButton.removeAttribute("aria-busy");
    validateEnvironmentButton.textContent = "Validate selected lab";
    validateEnvironmentButton.disabled =
      selectedExistingEnvironment !== environment;
  }
}

function clearExistingEnvironmentSelection() {
  selectedExistingEnvironment = null;
  skipDeploymentForValidatedEnvironment = false;
  validateEnvironmentButton.disabled = true;
  environmentValidationStatus.className = "";
  environmentValidationStatus.textContent =
    "Select an existing lab to validate only that environment.";
  document.querySelectorAll(".environment-card").forEach((card) => {
    card.classList.remove("selected");
    card.setAttribute("aria-pressed", "false");
  });
}

async function startDeploy() {
  skipDeploymentForValidatedEnvironment = false;
  const button = document.querySelector("#start-deploy");
  const log = document.querySelector("#deploy-log");
  const step = document.querySelector("#deploy-step");
  button.disabled = true;
  log.textContent = "Starting deployment...\n";
  const response = await apiPost("/api/deploy");
  const result = await response.json();
  if (!response.ok) {
    log.textContent += `${result.error || "Deployment did not start."}\n`;
    button.disabled = false;
    return;
  }
  const completed = await streamJob(result.job_id, log, { stepElement: step });
  button.disabled = false;
  if (completed.success) {
    await loadSummary();
    showPanel("summary");
  } else {
    step.textContent = "Deployment failed. Review the log above and retry.";
  }
}

async function loadSummary() {
  const response = await fetch("/api/summary");
  const summary = await response.json();
  if (!response.ok) throw new Error(summary.error || "Unable to load deployment summary.");
  currentSummary = summary;
  const legacyEnvironment =
    summary.existing_environment && summary.environment_detection === "legacy";
  teardownButton.disabled = legacyEnvironment;
  teardownButton.textContent = legacyEnvironment
    ? "Teardown unavailable"
    : "Tear down Azure resources";
  teardownButton.title = legacyEnvironment
    ? "Compatible legacy labs must be removed through their original deployment workflow."
    : "";
  const fields = [
    ["Lab", summary.lab_name, ""],
    ["Environment", summary.environment, ""],
    ["Azure resource group", summary.resource_group, summary.resource_group_portal_url],
    ["SRE Agent portal", summary.agent_portal_url, summary.agent_portal_url],
    ["Grubify UI", summary.frontend_url, summary.frontend_url],
    ["Grubify API", summary.api_url, summary.api_url],
  ];
  const container = document.querySelector("#summary-links");
  container.replaceChildren(...fields.map(([label, value, url]) => {
    const item = document.createElement("div");
    item.className = "summary-item";
    const heading = document.createElement("strong");
    heading.textContent = label;
    const content = url ? document.createElement("a") : document.createElement("span");
    content.textContent = value || "Unavailable";
    if (content instanceof HTMLAnchorElement) {
      content.href = url;
      content.target = "_blank";
      content.rel = "noreferrer";
      content.title = "Open in a new tab or window";
      const externalIcon = document.createElement("span");
      externalIcon.className = "external-link-icon";
      externalIcon.setAttribute("aria-hidden", "true");
      externalIcon.textContent = "↗";
      content.append(externalIcon);
      content.addEventListener("click", (event) => {
        void openSummaryLink(event, url);
      });
    }
    item.append(heading, content);
    return item;
  }));
  resetInvestigationCountdown();
  renderScenarioPicker();
  void loadEdgeProfiles();
}

async function loadEdgeProfiles() {
  if (edgeProfilesLoaded) return;
  edgeProfileStatus.textContent = "Finding Microsoft Edge profiles...";
  try {
    const response = await fetch("/api/edge-profiles", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "Unable to load Microsoft Edge profiles.");
    }
    const profiles = result.profiles || [];
    const options = [new Option("Current browser profile", "")];
    profiles.forEach((profile) => {
      const identity = profile.email ? ` - ${profile.email}` : "";
      options.push(new Option(`${profile.name}${identity}`, profile.id));
    });
    edgeProfileSelect.replaceChildren(...options);
    edgeProfileSelect.disabled = !result.edge_available || profiles.length === 0;
    edgeProfileStatus.textContent = edgeProfileSelect.disabled
      ? "No selectable Microsoft Edge profiles were found."
      : "Select a work profile to open Azure and SRE links with that identity.";
    edgeProfilesLoaded = true;
  } catch (error) {
    edgeProfileSelect.disabled = true;
    edgeProfileStatus.textContent = `Unable to load Microsoft Edge profiles: ${error}`;
  }
}

async function openSummaryLink(event, url) {
  const profile = edgeProfileSelect.value;
  if (!profile) return;
  event.preventDefault();
  edgeProfileStatus.textContent = "Opening link in the selected Microsoft Edge profile...";
  try {
    const response = await apiPost("/api/open-edge-link", { url, profile });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || "Unable to open the link.");
    }
    const selectedLabel =
      edgeProfileSelect.options[edgeProfileSelect.selectedIndex]?.textContent;
    edgeProfileStatus.textContent = `Opened in ${selectedLabel}.`;
  } catch (error) {
    edgeProfileStatus.textContent = `Unable to open the selected profile: ${error}`;
  }
}

function formatCountdown(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function selectedScenario() {
  return currentLab()?.scenarios.find((item) => item.id === selectedScenarioId) || null;
}

function renderInvestigationCountdownEstimate(scenario) {
  if (investigationCountdownState !== "idle") return;
  const seconds = Number(scenario?.investigation_delay_seconds || 0);
  investigationCountdownValue.textContent = formatCountdown(seconds);
  investigationCountdownProgress.style.transform = "scaleX(0)";
  investigationCountdownStatus.textContent = scenario
    ? "Starts when this scenario confirms its alert-triggering failure."
    : "Choose a scenario to see its expected response window.";
}

function resetInvestigationCountdown(scenario = selectedScenario()) {
  if (investigationCountdownTimer !== null) {
    window.clearInterval(investigationCountdownTimer);
    investigationCountdownTimer = null;
  }
  investigationCountdownState = "idle";
  investigationCountdownEnd = 0;
  investigationCountdownDuration = 0;
  investigationCountdown.classList.remove("active", "complete");
  renderInvestigationCountdownEstimate(scenario);
}

function startInvestigationCountdown(event) {
  const seconds = Number(event.seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return;
  if (investigationCountdownTimer !== null) {
    window.clearInterval(investigationCountdownTimer);
  }
  investigationCountdownState = "active";
  investigationCountdownDuration = seconds;
  const startedAt = Number(event.started_at) * 1000;
  investigationCountdownEnd =
    (Number.isFinite(startedAt) && startedAt > 0 ? startedAt : Date.now())
    + seconds * 1000;
  investigationCountdown.classList.add("active");
  investigationCountdown.classList.remove("complete");
  investigationCountdownStatus.textContent =
    "Alert evaluation is in progress. When the timer ends, check the Azure resource group and SRE Agent portal.";

  const update = () => {
    const remaining = Math.max(
      0,
      Math.ceil((investigationCountdownEnd - Date.now()) / 1000),
    );
    investigationCountdownValue.textContent = formatCountdown(remaining);
    const elapsed = 1 - remaining / investigationCountdownDuration;
    investigationCountdownProgress.style.transform =
      `scaleX(${Math.min(1, Math.max(0, elapsed))})`;
    if (remaining > 0) return;

    window.clearInterval(investigationCountdownTimer);
    investigationCountdownTimer = null;
    investigationCountdownState = "complete";
    investigationCountdown.classList.remove("active");
    investigationCountdown.classList.add("complete");
    investigationCountdownStatus.textContent =
      "Expected alert window elapsed. Check Azure portal for the fired alert and SRE Agent portal for the response plan and initial investigation.";
  };

  update();
  if (investigationCountdownState === "active") {
    investigationCountdownTimer = window.setInterval(update, 250);
  }
}

function renderScenarioPicker() {
  const picker = document.querySelector("#scenario-picker");
  const runButton = document.querySelector("#run-scenario");
  const scenarios = currentLab()?.scenarios || [];
  if (!scenarios.some((scenario) => scenario.id === selectedScenarioId)) {
    selectedScenarioId = scenarios[0]?.id || "";
  }
  picker.replaceChildren(...scenarios.map((scenario) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `picker-card${scenario.id === selectedScenarioId ? " selected" : ""}`;
    button.setAttribute(
      "aria-pressed",
      scenario.id === selectedScenarioId ? "true" : "false",
    );
    const title = document.createElement("span");
    title.className = "picker-title";
    title.textContent = scenario.name;
    const description = document.createElement("span");
    description.className = "picker-description";
    description.textContent = scenario.description;
    button.append(title, description);
    button.addEventListener("click", () => {
      selectedScenarioId = scenario.id;
      renderScenarioPicker();
    });
    return button;
  }));
  const scenario = scenarios.find((item) => item.id === selectedScenarioId);
  runButton.disabled = !scenario;
  runButton.textContent = scenario?.action_label || "Run selected scenario";
  renderInvestigationCountdownEstimate(scenario);
}

async function runSelectedScenario() {
  const scenario = currentLab()?.scenarios.find(
    (item) => item.id === selectedScenarioId
  );
  if (!scenario) return;
  resetInvestigationCountdown(scenario);
  await runDemoAction(
    "scenarios/run",
    scenario.confirmation,
    { scenario_id: scenario.id },
    "run-scenario",
    scenario.name,
  );
}

async function runDemoAction(
  path,
  confirmation,
  payload = undefined,
  buttonId = path,
  actionName = path,
) {
  if (confirmation && !window.confirm(confirmation)) return;
  const log = document.querySelector("#demo-log");
  const buttons = document.querySelectorAll(".demo-actions button");
  const activeButton = document.getElementById(buttonId);
  const activeButtonLabel = activeButton?.textContent;
  buttons.forEach((button) => {
    button.disabled = true;
  });
  if (activeButton) {
    activeButton.setAttribute("aria-busy", "true");
    activeButton.textContent = `${activeButtonLabel} running`;
  }
  log.setAttribute("aria-busy", "true");
  log.textContent = `Starting ${actionName}...\n`;
  try {
    const response = await apiPost(`/api/${path}`, payload);
    const result = await response.json();
    if (!response.ok) {
      log.textContent += `${result.error || "Action did not start."}\n`;
      return;
    }
    const completed = await streamJob(result.job_id, log, {
      onEvent: (event) => {
        if (event.type === "investigation_countdown") {
          startInvestigationCountdown(event);
        }
      },
    });
    log.textContent += completed.success ? "\nCompleted.\n" : "\nAction failed.\n";
    if (!completed.success) return;

    if (path === "restore-baseline") {
      await loadSummary();
      log.textContent +=
        "\nBaseline restored from the declared infrastructure and application configuration.\n";
    }
    if (path === "teardown") {
      resetInvestigationCountdown();
      document.querySelector("#summary-links").replaceChildren();
      log.textContent = "";
      document.querySelector("#deploy-log").textContent = "";
      document.querySelector("#deploy-step").textContent =
        "Azure resources removed. Select Deploy lab to create the demo again.";
      showPanel("deployment");
    }
  } catch (error) {
    log.textContent += `Action failed: ${error}\n`;
  } finally {
    log.removeAttribute("aria-busy");
    if (activeButton) {
      activeButton.removeAttribute("aria-busy");
      activeButton.textContent = activeButtonLabel;
    }
    buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}

document.querySelector("#refresh-prereqs").addEventListener("click", () => {
  void recheckApplication().catch((error) => {
    prereqList.textContent = `Unable to reconnect to the app: ${error}`;
  });
});
backButton.addEventListener("click", navigateBack);
shutdownButton.addEventListener("click", shutdownApplication);
document.querySelector("#continue-lab").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  prereqList.textContent = "Checking installed tools...";
  continueButton.disabled = true;
  installAllButton.classList.add("hidden");
  showPanel("prerequisites");
  try {
    await persistSelectedLab();
    await loadPrerequisites();
  } catch (error) {
    showPanel("labs");
    const message = document.createElement("p");
    message.className = "warning";
    message.setAttribute("role", "alert");
    message.textContent = `Unable to select the lab: ${error}`;
    document.querySelector("#lab-picker").append(message);
    button.disabled = false;
  }
});
installAllButton.addEventListener("click", installAll);
continueButton.addEventListener("click", () => showPanel("authentication"));
document.querySelectorAll("[data-auth]").forEach((button) => {
  button.addEventListener("click", () => startAuth(button.dataset.auth));
});
azureTenant.addEventListener("change", () => {
  populateSubscriptions();
  markAzureContextChanged();
});
azureSubscription.addEventListener("change", markAzureContextChanged);
applyAzureContextButton.addEventListener("click", applyAzureContext);
document.querySelector("#refresh-azure-context").addEventListener("click", () => {
  void loadAzureContexts().catch((error) => {
    azureContextStatus.className = "warning";
    azureContextStatus.textContent = `Unable to refresh Azure accounts: ${error}`;
  });
});
document.querySelector("#continue-to-configure").addEventListener("click", () => {
  showPanel("configuration");
  void loadExistingEnvironments();
});
document.querySelector("#refresh-environments").addEventListener(
  "click",
  loadExistingEnvironments,
);
validateEnvironmentButton.addEventListener("click", validateExistingEnvironment);
document.querySelector("#environment-name").addEventListener("input", (event) => {
  if (
    selectedExistingEnvironment
    && event.target.value !== selectedExistingEnvironment.environment
  ) {
    clearExistingEnvironmentSelection();
  }
});
document.querySelector("#azure-location").addEventListener("change", (event) => {
  if (
    selectedExistingEnvironment
    && event.target.value !== selectedExistingEnvironment.location
  ) {
    clearExistingEnvironmentSelection();
  }
});
document.querySelector("#configure-form").addEventListener("submit", configureEnvironment);
document.querySelector("#start-deploy").addEventListener("click", startDeploy);
document.querySelector("#run-scenario").addEventListener("click", runSelectedScenario);
document.querySelector("#restore-baseline").addEventListener("click", () => runDemoAction(
  "restore-baseline",
  "Reapply the declared Azure infrastructure, application images, and SRE Agent configuration? This can overwrite configuration drift."
));
teardownButton.addEventListener("click", () => runDemoAction(
  "teardown",
  currentSummary?.existing_environment
    ? "Permanently delete this existing demo and all of its Azure resources?"
    : "Permanently delete this demo's Azure resources?"
));

initialize().catch((error) => {
  document.querySelector("#lab-picker").textContent = `Unable to initialize the app: ${error}`;
});
