# Zava Learning - Redaction Standard (`zava-redaction`)

Sensitive data must never appear in an operator-visible surface: the incident thread, PagerDuty or
ServiceNow notes, commit messages, pull-request bodies, or report artifacts. Retrieve this standard
with `SearchMemory("zava-redaction")` and apply it before posting any generated content.

## Always redact

- Passwords, connection strings, Key Vault secret values, client secrets, API keys, access keys,
  shared-access keys, and signed-access tokens.
- Source-control credentials, OAuth access tokens, identity tokens, and authorization-header values.
- Private keys, SSH keys, and certificate private-key material.
- Credentials embedded in URIs.
- Email addresses and learner personal data, including names, addresses, and student identifiers.

Resource names, resource groups, regions, container-app names, alert names, pull-request or change
request numbers, and non-secret resource IDs are not sensitive and should remain in the narrative.

## Required behavior

- Never read or print credential files. This includes environment files, private-key files,
  certificates, kubeconfig files, Azure CLI profile data, Terraform state, and variable files.
- Do not paste output from a command that can reveal a secret. Confirm the result without exposing
  the value.
- If a string may be sensitive, redact it.
- Use a typed marker such as `[REDACTED:SECRET]`, `[REDACTED:TOKEN]`, or `[REDACTED:EMAIL]`.
- For a URI containing credentials, retain only the non-sensitive scheme and host details.
- Apply the deterministic scrubber defined by the `redaction-guard` skill to every emitted string.
  The scrubber is idempotent and must run immediately before output leaves the agent.

## Application points

- Scrub a chat or incident-thread summary before posting it.
- Scrub a PagerDuty note before adding it to an incident.
- Scrub a ServiceNow change description and attachment before upload.
- Scrub pull-request bodies and commit messages, and never stage credentials.
- Scrub all text assembled for HTML, Markdown, presentation, email, or Teams deliverables.

## Verification

Before any artifact or message leaves the agent, confirm that the redaction guard has run, no
credential file was printed, and every sensitive value has been replaced by a typed redaction marker.
