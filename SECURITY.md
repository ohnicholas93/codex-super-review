# Security Policy

## Supported Versions

Security fixes are applied to the current `main` branch.

## Reporting a Vulnerability

Report suspected vulnerabilities privately to the repository maintainer. Do not
open a public issue that includes exploit details, secrets, logs, Codex thread
IDs, or private repository paths.

Useful reports include:

- Affected commit or release.
- The command and options used.
- Expected and observed behavior.
- Whether audit logging was disabled.
- Minimal reproduction details that do not include secrets.

## Sensitive Data Handling

`codex-super-review` can process proprietary source code and model output.
Audit logs are written by default and can include prompts, reviewer findings,
implementer responses, diagnostics, usage metadata, working directory paths,
and Codex thread IDs. Disable them explicitly when needed.

Before sharing logs or terminal output, review them for:

- Source code excerpts.
- Environment variables or credential-like strings.
- Private file paths.
- Codex thread IDs.
- Internal review findings.

## Maintainer Checklist

Before publishing a release or sharing the repository:

- Run the checks listed in `README.md`.
- Search tracked files and history for credentials.
- Confirm `.env`, key material, logs, `.codex/`, local `codex` checkouts, and
  `private/` notes are not tracked.
- Review any audit logs before distributing artifacts.
