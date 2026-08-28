import { useEffect, useState, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./PrereqScreen.css";

export interface PrereqStatus {
  id: string;
  name: string;
  installed: boolean;
  version: string | null;
  winget_command: string;
  install_url: string;
}

interface PrereqScreenProps {
  onAllSatisfied: () => void;
}

function PrereqScreen({ onAllSatisfied }: PrereqScreenProps) {
  const [statuses, setStatuses] = useState<PrereqStatus[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const runCheck = useCallback(async () => {
    setChecking(true);
    try {
      const result = await invoke<PrereqStatus[]>("check_prerequisites");
      setStatuses(result);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    runCheck();
  }, [runCheck]);

  const allInstalled = statuses !== null && statuses.every((s) => s.installed);

  async function copyCommand(id: string, command: string) {
    try {
      await navigator.clipboard.writeText(command);
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 1500);
    } catch {
      // Clipboard access can fail in some contexts; ignore silently.
    }
  }

  return (
    <div className="prereq-screen">
      <h1>Checking prerequisites</h1>
      <p className="prereq-subtitle">
        This wizard needs the following tools installed on your machine. We
        only detect what's present &mdash; we never install or modify
        anything for you.
      </p>

      {statuses === null && <p>Checking your system…</p>}

      {statuses !== null && (
        <ul className="prereq-list">
          {statuses.map((s) => (
            <li
              key={s.id}
              className={`prereq-item ${s.installed ? "ok" : "missing"}`}
            >
              <div className="prereq-row">
                <span className="prereq-icon" aria-hidden>
                  {s.installed ? "✅" : "❌"}
                </span>
                <span className="prereq-name">{s.name}</span>
                {s.installed && s.version && (
                  <span className="prereq-version">v{s.version}</span>
                )}
              </div>

              {!s.installed && (
                <div className="prereq-fix">
                  <code className="prereq-command">{s.winget_command}</code>
                  <button
                    type="button"
                    onClick={() => copyCommand(s.id, s.winget_command)}
                  >
                    {copiedId === s.id ? "Copied!" : "Copy"}
                  </button>
                  <a href={s.install_url} target="_blank" rel="noreferrer">
                    Installer page ↗
                  </a>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="prereq-actions">
        <button type="button" onClick={runCheck} disabled={checking}>
          {checking ? "Checking…" : "Re-check"}
        </button>
        <button type="button" disabled={!allInstalled} onClick={onAllSatisfied}>
          Continue
        </button>
      </div>

      {!allInstalled && statuses !== null && (
        <p className="prereq-hint">
          Install the missing tools above, then click "Re-check". You may
          need to open a new terminal/restart this app for PATH changes to
          take effect.
        </p>
      )}
    </div>
  );
}

export default PrereqScreen;
