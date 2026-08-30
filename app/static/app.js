const prereqList = document.querySelector("#prereq-list");
const continueButton = document.querySelector("#continue-to-auth");
const authStatus = { "azure-cli": false, azd: false };
let sessionToken = "";

async function initialize() {
  const response = await fetch("/api/session");
  const session = await response.json();
  sessionToken = session.token;
  await loadPrerequisites();
  await loadAuthStatus();
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

function showPanel(id) {
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.add("hidden"));
  document.querySelector(`#${id}`).classList.remove("hidden");
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
  link.textContent = "Microsoft sign-in opens in 3 seconds";
  actions.append(copy, link);
  device.replaceChildren(code, notice, actions);
  device.classList.remove("hidden");

  await new Promise((resolve) => setTimeout(resolve, 3000));
  try {
    const response = await apiPost("/api/open-device-login", {
      url: event.verification_url,
    });
    const result = await response.json();
    link.textContent = response.ok && result.opened
      ? "Microsoft sign-in opened in your browser"
      : "Open Microsoft sign-in";
  } catch {
    link.textContent = "Open Microsoft sign-in";
  }
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

function showAuthComplete(device, button, kind) {
  const title = document.createElement("div");
  title.className = "auth-result-title";
  title.textContent = "Signed in";
  const detail = document.createElement("p");
  detail.className = "auth-result-detail";
  detail.textContent = kind === "azure-cli"
    ? "Azure CLI authentication completed successfully."
    : "Azure Developer CLI authentication completed successfully.";
  device.replaceChildren(title, detail);
  device.classList.add("complete");
  device.classList.remove("hidden");
  button.textContent = kind === "azure-cli"
    ? "Signed in with Azure CLI"
    : "Signed in with azd";
  button.disabled = true;
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
    showAuthComplete(device, button, kind);
  });
  document.querySelector("#continue-to-configure").disabled =
    !Object.values(authStatus).every(Boolean);
}

async function loadPrerequisites() {
  prereqList.textContent = "Checking installed tools...";
  continueButton.disabled = true;
  try {
    const response = await fetch("/api/prerequisites");
    const tools = await response.json();
    prereqList.replaceChildren(...tools.map(renderTool));
    continueButton.disabled = !tools
      .filter((tool) => tool.required)
      .every((tool) => tool.installed);
  } catch (error) {
    prereqList.textContent = `Unable to check prerequisites: ${error}`;
  }
}

function renderTool(tool) {
  const row = document.createElement("div");
  row.className = `tool ${tool.installed ? "ok" : tool.required ? "" : "optional"}`;
  const status = document.createElement("span");
  status.className = "status";
  status.textContent = tool.installed ? "OK" : tool.required ? "!" : "i";
  const name = document.createElement("strong");
  name.textContent = tool.name;
  const version = document.createElement("span");
  version.className = "version";
  version.textContent = tool.version || (tool.required ? "Missing" : "Missing (recommended)");
  row.append(status, name, version);

  if (!tool.installed) {
    const install = document.createElement("div");
    install.className = "install";
    const code = document.createElement("code");
    code.textContent = tool.install_command;
    const copy = document.createElement("button");
    copy.className = "secondary";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(tool.install_command);
      copy.textContent = "Copied";
    });
    const installButton = document.createElement("button");
    installButton.className = "install-tool";
    installButton.textContent = "Install";
    installButton.addEventListener("click", () => installTool(tool, installButton, progress));
    const link = document.createElement("a");
    link.href = tool.install_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Installer documentation";
    const progress = document.createElement("pre");
    progress.className = "install-progress hidden";
    progress.setAttribute("aria-live", "polite");
    install.append(installButton, code, copy, link, progress);
    row.append(install);
  }
  return row;
}

async function installTool(tool, button, progress) {
  document.querySelectorAll(".install-tool").forEach((item) => {
    item.disabled = true;
  });
  button.textContent = "Installing...";
  progress.textContent = `Starting ${tool.name} installation...\n`;
  progress.classList.remove("hidden");
  try {
    const response = await apiPost(`/api/install/${tool.id}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start installation");
    const completed = await streamJob(result.job_id, progress);
    if (completed.success) {
      progress.textContent += "\nInstallation completed. Re-checking prerequisites...\n";
      await loadPrerequisites();
      return;
    }
    progress.textContent += "\nInstallation failed. Review the output above or use the fallback command.\n";
  } catch (error) {
    progress.textContent += `\nInstallation failed: ${error}\n`;
  }
  button.textContent = "Retry install";
  document.querySelectorAll(".install-tool").forEach((item) => {
    item.disabled = false;
  });
}

async function startAuth(kind) {
  const button = document.querySelector(`[data-auth="${kind}"]`);
  const log = document.querySelector(`#${kind}-log`);
  const device = document.querySelector(`#${kind}-device`);
  button.disabled = true;
  device.classList.add("hidden");
  if (kind === "azure-cli") {
    log.textContent = "Starting secure Azure CLI sign-in...\n";
    const notice = document.createElement("p");
    notice.className = "device-notice";
    notice.setAttribute("role", "status");
    notice.textContent =
      "Complete the Microsoft sign-in page in your browser. Your organization may require MFA.";
    device.replaceChildren(notice);
    device.classList.remove("hidden");
  } else {
    log.textContent = "Starting device-code sign-in...\n";
  }

  try {
    const response = await apiPost(`/api/auth/${kind}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start sign-in");
    const events = new EventSource(`/api/jobs/${result.job_id}/events`);
    events.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      if (event.type === "output") {
        log.textContent += `${event.line}\n`;
        log.scrollTop = log.scrollHeight;
        if (/AADSTS50076|continue the login in the pop-up window/i.test(event.line)) {
          showMfaNotice(device);
        }
      }
      if (event.type === "device_code") {
        void showDeviceCode(device, event);
      }
      if (event.type === "done") {
        log.textContent += event.success
          ? "\nSign-in completed.\n"
          : `\nSign-in failed (exit ${event.exit_code}).\n`;
        authStatus[kind] = event.success;
        document.querySelector("#continue-to-configure").disabled =
          !Object.values(authStatus).every(Boolean);
        if (event.success) {
          showAuthComplete(device, button, kind);
        } else {
          button.disabled = false;
        }
        events.close();
      }
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
      if (event.type === "output") {
        log.textContent += `${event.line}\n`;
        log.scrollTop = log.scrollHeight;
      }
      if (event.type === "command") log.textContent += `> ${event.command.join(" ")}\n`;
      if (event.type === "step" && options.stepElement) options.stepElement.textContent = event.name;
      if (event.type === "error") log.textContent += `ERROR: ${event.message}\n`;
      if (event.type === "done") {
        events.close();
        resolve(event);
      }
    };
  });
}

async function configureEnvironment(event) {
  event.preventDefault();
  const status = document.querySelector("#configure-status");
  status.textContent = "Saving environment...";
  const response = await apiPost("/api/configure", {
    environment: document.querySelector("#environment-name").value,
    location: document.querySelector("#azure-location").value,
  });
  const result = await response.json();
  if (!response.ok) {
    status.textContent = result.error || "Configuration failed.";
    return;
  }
  status.textContent = `Ready: ${result.environment} in ${result.location}`;
  showPanel("deployment");
}

async function startDeploy() {
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
  const fields = [
    ["Environment", summary.environment],
    ["Resource group", summary.resource_group],
    ["Agent portal", summary.agent_portal_url],
    ["Grubify UI", summary.frontend_url],
    ["Grubify API", summary.api_url],
  ];
  const container = document.querySelector("#summary-links");
  container.replaceChildren(...fields.map(([label, value]) => {
    const item = document.createElement("div");
    item.className = "summary-item";
    const heading = document.createElement("strong");
    heading.textContent = label;
    const content = value && value.startsWith("http")
      ? document.createElement("a")
      : document.createElement("span");
    content.textContent = value || "Unavailable";
    if (content instanceof HTMLAnchorElement) {
      content.href = value;
      content.target = "_blank";
      content.rel = "noreferrer";
    }
    item.append(heading, content);
    return item;
  }));
}

async function runDemoAction(path, confirmation) {
  if (confirmation && !window.confirm(confirmation)) return;
  const log = document.querySelector("#demo-log");
  log.textContent = `Starting ${path}...\n`;
  const response = await apiPost(`/api/${path}`);
  const result = await response.json();
  if (!response.ok) {
    log.textContent += `${result.error || "Action did not start."}\n`;
    return;
  }
  const completed = await streamJob(result.job_id, log);
  log.textContent += completed.success ? "\nCompleted.\n" : "\nAction failed.\n";
}

document.querySelector("#refresh-prereqs").addEventListener("click", loadPrerequisites);
continueButton.addEventListener("click", () => showPanel("authentication"));
document.querySelectorAll("[data-auth]").forEach((button) => {
  button.addEventListener("click", () => startAuth(button.dataset.auth));
});
document.querySelector("#continue-to-configure").addEventListener("click", () => showPanel("configuration"));
document.querySelector("#configure-form").addEventListener("submit", configureEnvironment);
document.querySelector("#start-deploy").addEventListener("click", startDeploy);
document.querySelector("#break-cart").addEventListener("click", () => runDemoAction(
  "break-cart",
  "Send 200 cart requests to trigger the Scenario 1 memory leak?"
));
document.querySelector("#teardown").addEventListener("click", () => runDemoAction(
  "teardown",
  "Permanently delete this demo's Azure resources?"
));

initialize().catch((error) => {
  prereqList.textContent = `Unable to initialize the app: ${error}`;
});
