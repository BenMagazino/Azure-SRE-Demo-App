// Prerequisite detection for the SRE Agent onboarding wizard.
//
// Detects whether Azure CLI, Azure Developer CLI (azd), Git, and Python are
// installed and resolvable on PATH, without attempting any installation or
// elevation. Each tool that is missing gets a copyable `winget install`
// command and a link to its official installer page so the user can install
// it themselves, then re-run detection.

use serde::Serialize;
use std::process::Command;

#[derive(Serialize, Clone)]
pub struct PrereqStatus {
    /// Machine-readable identifier, e.g. "az", "azd", "git", "python".
    pub id: String,
    /// Human-readable name, e.g. "Azure CLI".
    pub name: String,
    /// Whether the tool was found and ran successfully.
    pub installed: bool,
    /// Parsed version string, if detected.
    pub version: Option<String>,
    /// winget package id/command to copy-paste for installation.
    pub winget_command: String,
    /// Link to the official installer/docs page.
    pub install_url: String,
}

struct ToolSpec {
    id: &'static str,
    name: &'static str,
    exe: &'static str,
    args: &'static [&'static str],
    winget_command: &'static str,
    install_url: &'static str,
}

const TOOLS: &[ToolSpec] = &[
    ToolSpec {
        id: "az",
        name: "Azure CLI",
        exe: "az",
        args: &["version"],
        winget_command: "winget install --id Microsoft.AzureCLI",
        install_url: "https://learn.microsoft.com/cli/azure/install-azure-cli-windows",
    },
    ToolSpec {
        id: "azd",
        name: "Azure Developer CLI",
        exe: "azd",
        args: &["version"],
        winget_command: "winget install --id Microsoft.Azd",
        install_url: "https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd",
    },
    ToolSpec {
        id: "git",
        name: "Git",
        exe: "git",
        args: &["--version"],
        winget_command: "winget install --id Git.Git",
        install_url: "https://git-scm.com/download/win",
    },
    ToolSpec {
        id: "python",
        name: "Python",
        exe: "python",
        args: &["--version"],
        winget_command: "winget install --id Python.Python.3.12",
        install_url: "https://www.python.org/downloads/windows/",
    },
];

/// Runs a command hidden (no console window flash on Windows) and returns
/// combined stdout+stderr as a UTF-8 lossy string, if the process launched.
fn run_captured(exe: &str, args: &[&str]) -> Option<String> {
    let mut cmd = Command::new(exe);
    cmd.args(args);

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: avoid flashing a console window when spawned
        // from the GUI app.
        cmd.creation_flags(0x08000000);
    }

    let output = cmd.output().ok()?;
    let mut text = String::new();
    text.push_str(&String::from_utf8_lossy(&output.stdout));
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    if text.trim().is_empty() {
        None
    } else {
        Some(text)
    }
}

/// Extracts a plausible version number (first `\d+\.\d+(\.\d+)?` match) from
/// free-form CLI output, e.g. `az version` (JSON), `git --version`, etc.
fn extract_version(raw: &str) -> Option<String> {
    // az CLI `az version` prints JSON like: "azure-cli": "2.62.0"
    if let Some(idx) = raw.find("azure-cli") {
        if let Some(rest) = raw.get(idx..) {
            if let Some(v) = extract_first_semver(rest) {
                return Some(v);
            }
        }
    }
    extract_first_semver(raw)
}

fn extract_first_semver(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i].is_ascii_digit() {
            let start = i;
            let mut j = i;
            let mut dots = 0;
            while j < bytes.len() && (bytes[j].is_ascii_digit() || bytes[j] == b'.') {
                if bytes[j] == b'.' {
                    dots += 1;
                }
                j += 1;
            }
            // Trim a trailing dot if the scan ended on one.
            let mut end = j;
            if end > start && bytes[end - 1] == b'.' {
                end -= 1;
            }
            if dots >= 1 && end > start {
                return Some(s[start..end].to_string());
            }
            i = j.max(i + 1);
        } else {
            i += 1;
        }
    }
    None
}

fn check_tool(spec: &ToolSpec) -> PrereqStatus {
    let raw = run_captured(spec.exe, spec.args);
    let (installed, version) = match &raw {
        Some(text) => (true, extract_version(text)),
        None => (false, None),
    };

    PrereqStatus {
        id: spec.id.to_string(),
        name: spec.name.to_string(),
        installed,
        version,
        winget_command: spec.winget_command.to_string(),
        install_url: spec.install_url.to_string(),
    }
}

#[tauri::command]
pub fn check_prerequisites() -> Vec<PrereqStatus> {
    TOOLS.iter().map(check_tool).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_version_from_git_output() {
        assert_eq!(
            extract_version("git version 2.46.0.windows.1"),
            Some("2.46.0".to_string())
        );
    }

    #[test]
    fn extracts_version_from_simple_semver() {
        assert_eq!(extract_version("Python 3.12.4"), Some("3.12.4".to_string()));
    }

    #[test]
    fn extracts_azure_cli_version_from_json_like_output() {
        let raw = "{\n  \"azure-cli\": \"2.62.0\",\n  \"azure-cli-core\": \"2.62.0\"\n}\n";
        assert_eq!(extract_version(raw), Some("2.62.0".to_string()));
    }

    #[test]
    fn returns_none_for_no_digits() {
        assert_eq!(extract_version("no version info here"), None);
    }
}
