# codex-super-review

`codex-super-review` runs iterative Codex review loops against an existing
implementer Codex session. It starts fresh read-only reviewer streams, sends
actionable findings back to the implementer session, and repeats until a fresh
reviewer returns `NO_FINDINGS` or a configured round limit is reached.

## ⚠️ Warning ⚠️

> This tool can and likely will consume an enormous number of tokens,
especially across multiple reviewer streams and fix rounds. Use at your own risk.

## Requirements

- Python 3.10 or newer.
- The `codex` CLI available on `PATH`.
- A resumable Codex implementer session ID created in the repository you want
  to review.

This project has no third-party Python dependencies.

## Install

From this repository:

```bash
./install.sh
```

The installer writes a small wrapper to `~/.local/bin/codex-super-review`. Make
sure `~/.local/bin` is on your `PATH`.

To uninstall:

```bash
./install.sh --uninstall
```

## Usage

Run the command from the project root you want Codex to review:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID
```

Useful limits for bounded runs:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID \
  --max-new-reviewer-streams 3 \
  --max-fix-rounds-per-reviewer 2
```

Before the first fix round for each fresh reviewer stream, the tool checks the
implementer thread's restored context usage through Codex app-server. If usage is
at or above 40%, it triggers Codex's built-in compaction before sending the
reviewer findings to the implementer:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID \
  --implementer-compact-threshold-percent 40
```

Use `0` to disable this pre-fix compaction check.

Model arguments use `"<model> <reasoning_effort>"` or
`"<model>:<reasoning_effort>"`:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID \
  --implementer-model "gpt-5.5 medium" \
  --reviewer-model "gpt-5.4 xhigh"
```

The implementer session should not be controlled by another process while this
tool is running.

## Audit Logs

Audit logs are written by default:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID
```

Disable them explicitly if needed:

```bash
codex-super-review IMPLEMENTER_CODEX_SESSION_ID --write-audit-log false
```

Logs are written to the first available location:

1. `/var/log/codex-super-review`, if it exists and is writable.
2. `$XDG_STATE_HOME/codex-super-review/audit`, if `XDG_STATE_HOME` is set.
3. `~/.local/state/codex-super-review/audit`.

## Security Notes

- Reviewer streams run with Codex read-only sandboxing and
  `approval_policy="never"`.
- Implementer fix rounds run with workspace-write sandboxing, Codex
  `approval_policy="on-request"`, and `approvals_reviewer="auto_review"`.
  This lets the implementer request an auto-reviewed escalation for Git-index
  operations, such as staging files, when `.git` is not writable inside the
  sandbox.
- The tool invokes Codex through `subprocess.Popen` with an argument list, not
  through a shell command string.
- Do not commit `private/`, `.codex/`, local `codex` checkouts, `.env` files,
  private keys, or generated logs. The repository `.gitignore` excludes these
  by default.
- Treat audit logs as sensitive review artifacts. They may include source
  excerpts, file paths, and model output.

## Development Checks

The lightweight checks for this repository are:

```bash
python3 -m py_compile bin/codex-super-review
python3 bin/codex-super-review --help
bash -n install.sh
```

There is no dependency lockfile or package manifest, so dependency vulnerability
audits such as `npm audit` or `pip-audit` do not apply.
