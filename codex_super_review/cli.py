from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .cli_parsers import parse_bool, parse_model_spec, parse_percent, parse_positive_int
from .constants import DEFAULT_IMPLEMENTER_MODEL, DEFAULT_ORACLE_MODEL, DEFAULT_REVIEWER_MODEL
from .diagnostics import failure_details_lines
from .event_sink import NullEventSink
from .errors import (
    CodexAppServerFailure,
    CodexExecutableNotFound,
    CodexResultDiagnostics,
    CodexRunFailure,
    LimitReached,
)
from .workflow import orchestrate

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-super-review",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run iterative Codex reviewer streams against a persistent implementer Codex session.",
        epilog="""Examples:
  codex-super-review
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --max-new-reviewer-streams 3
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --max-fix-rounds-per-reviewer 2
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --implementer-compact-threshold-percent 60
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --write-audit-log true
  codex-super-review --attach ~/.local/state/codex-super-review/audit/RUN.jsonl
  codex-super-review --branch-base release/2026.05
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --implementer "gpt 5.5 medium" --reviewer "gpt 5.4 xhigh"
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --oracle "gpt 5.4 mini medium"

The command must be run from the project root to review. If an implementer
session ID is provided, it must be resumable by `codex exec resume`. If omitted,
the first implementer fix round creates a fresh persistent session. This harness
should be the only active controller of the implementer session while it runs.

Model arguments are formatted as "<model> <reasoning_effort>" or
"<model>:<reasoning_effort>". Spaces inside the model name are normalized to
hyphens, so "gpt 5.4 xhigh" becomes --model gpt-5.4 with
model_reasoning_effort="xhigh".""",
    )
    parser.add_argument(
        "implementer_codex_session_id",
        nargs="?",
        default=None,
        help=(
            "Existing Codex session/thread id for the persistent implementer stream. "
            "If omitted, a fresh implementer session is created on the first fix round."
        ),
    )
    parser.add_argument(
        "--max-new-reviewer-streams",
        type=parse_positive_int,
        default=15,
        metavar="N",
        help="Maximum fresh reviewer streams to create. Default: 15.",
    )
    parser.add_argument(
        "--max-fix-rounds-per-reviewer",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="Maximum fix/reverify rounds inside each reviewer stream. Default: infinite.",
    )
    parser.add_argument(
        "--write-audit-log",
        type=parse_bool,
        default=True,
        metavar="true|false",
        help="Write JSONL audit logs containing prompts, responses, diagnostics, and usage. Default: true.",
    )
    parser.add_argument(
        "--branch-base",
        dest="branch_base",
        default=None,
        metavar="REF",
        help=(
            "Review the currently checked out branch against this explicit base ref. "
            "If omitted, reviewers inspect staged, unstaged, and untracked changes."
        ),
    )
    parser.add_argument(
        "--implementer-compact-threshold-percent",
        type=parse_percent,
        default=40.0,
        metavar="PERCENT",
        help=(
            "Before the first fix round for each fresh reviewer stream, check the implementer "
            "thread through Codex app-server and compact if restored context usage is at or "
            "above this percentage. Skipped until an implementer session exists. Use 0 to "
            "disable. Default: 40."
        ),
    )
    parser.add_argument(
        "--implementer",
        dest="implementer_model",
        default=DEFAULT_IMPLEMENTER_MODEL,
        metavar="MODEL EFFORT",
        help=f'Implementer model and reasoning effort. Default: "{DEFAULT_IMPLEMENTER_MODEL}".',
    )
    parser.add_argument(
        "--implementer-model",
        dest="implementer_model",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reviewer",
        dest="reviewer_model",
        default=DEFAULT_REVIEWER_MODEL,
        metavar="MODEL EFFORT",
        help=f'Reviewer model and reasoning effort. Default: "{DEFAULT_REVIEWER_MODEL}".',
    )
    parser.add_argument(
        "--reviewer-model",
        dest="reviewer_model",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--oracle",
        dest="oracle_model",
        default=DEFAULT_ORACLE_MODEL,
        metavar="MODEL EFFORT",
        help=f'Oracle classifier model and reasoning effort. Default: "{DEFAULT_ORACLE_MODEL}".',
    )
    parser.add_argument(
        "--oracle-model",
        dest="oracle_model",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-oracle-retries",
        type=parse_positive_int,
        default=2,
        metavar="N",
        help="Maximum oracle parse-failure retries after rolling back malformed oracle turns. Default: 2.",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_SUPER_REVIEW_CODEX_BIN", "codex"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Use legacy line-oriented terminal output instead of the interactive TUI.",
    )
    parser.add_argument(
        "--attach",
        metavar="PATH",
        help="Attach a TUI to an existing JSONL audit log without starting a review.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_args)

    if args.attach is not None:
        _validate_attach_args(parser, args, raw_args)
        if not _should_use_tui(args):
            print(
                "error: --attach requires an interactive terminal with TUI support",
                file=sys.stderr,
            )
            return 2
        try:
            from .tui import CursesUnavailable, run_attach_tui

            return run_attach_tui(Path(args.attach))
        except ImportError as exc:
            print(f"error: could not import TUI support: {exc}", file=sys.stderr)
            return 2
        except CursesUnavailable as exc:
            print(f"error: could not initialize TUI: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: could not read audit log: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: invalid audit log: {exc}", file=sys.stderr)
            return 2

    if (
        shutil.which(args.codex_bin) is None
        and Path(args.codex_bin).name == args.codex_bin
    ):
        print(
            f"error: could not find Codex executable on PATH: {args.codex_bin}",
            file=sys.stderr,
        )
        return 127

    if _should_use_tui(args):
        try:
            from .tui import CursesUnavailable, run_tui

            return run_tui(args, _run_with_error_handling)
        except ImportError as exc:
            print(
                f"warning: could not import TUI support; falling back to legacy output: {exc}",
                file=sys.stderr,
            )
        except CursesUnavailable as exc:
            print(
                f"warning: could not initialize TUI; falling back to legacy output: {exc}",
                file=sys.stderr,
            )

    args.event_sink = NullEventSink()
    return _run_with_error_handling(args, args.event_sink)


def _validate_attach_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    raw_args: list[str],
) -> None:
    conflicts: list[str] = []
    if args.implementer_codex_session_id is not None:
        conflicts.append("implementer_codex_session_id")
    review_options = (
        "--max-new-reviewer-streams",
        "--max-fix-rounds-per-reviewer",
        "--write-audit-log",
        "--branch-base",
        "--implementer-compact-threshold-percent",
        "--implementer",
        "--implementer-model",
        "--reviewer",
        "--reviewer-model",
        "--oracle",
        "--oracle-model",
        "--max-oracle-retries",
        "--codex-bin",
        "--no-tui",
    )
    for option in review_options:
        if _option_present(raw_args, option):
            conflicts.append(option)
    if conflicts:
        parser.error(
            "--attach cannot be combined with review arguments: "
            + ", ".join(conflicts)
        )


def _option_present(raw_args: list[str], option: str) -> bool:
    return any(arg == option or arg.startswith(option + "=") for arg in raw_args)


def _should_use_tui(args: argparse.Namespace) -> bool:
    term = os.environ.get("TERM", "")
    return (
        not args.no_tui
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and term not in {"", "dumb"}
    )


def _run_with_error_handling(
    args: argparse.Namespace,
    emit: object,
) -> int:
    args._audit_logger = None
    code: int | None = None
    try:
        code = orchestrate(args)
        return code
    except ValueError as exc:
        _emit_top_level_error(args, emit, "error", f"error: {exc}")
        code = 2
        return code
    except KeyboardInterrupt:
        _emit_top_level_error(args, emit, "interrupted", "Interrupted")
        code = 130
        return code
    except CodexExecutableNotFound as exc:
        _emit_top_level_error(args, emit, "error", f"error: {exc}")
        code = 127
        return code
    except CodexRunFailure as exc:
        _emit_top_level_error(
            args,
            emit,
            "codex_run_failure",
            f"error: {exc}",
            details=failure_details_lines(exc.result),
        )
        code = exc.result.returncode or 1
        return code
    except CodexResultDiagnostics as exc:
        _emit_top_level_error(
            args,
            emit,
            "codex_result_diagnostics",
            f"error: {exc}",
            details=failure_details_lines(exc.result),
        )
        code = 1
        return code
    except CodexAppServerFailure as exc:
        _emit_top_level_error(
            args,
            emit,
            "app_server_failure",
            f"error: implementer pre-fix compaction failed: {exc}",
        )
        code = 1
        return code
    except LimitReached as exc:
        _emit_top_level_error(args, emit, "limit_reached", f"error: {exc}")
        code = 2
        return code
    except RuntimeError as exc:
        _emit_top_level_error(args, emit, "runtime_error", f"error: {exc}")
        code = 1
        return code
    finally:
        audit = getattr(args, "_audit_logger", None)
        if audit is not None:
            if code is not None:
                message = (
                    emit.final_message_hint(code)
                    if hasattr(emit, "final_message_hint")
                    else None
                ) or getattr(args, "_top_level_error_message", None)
                audit.finish(code, message)
            audit.close()


def _emit_top_level_error(
    args: argparse.Namespace,
    emit: object,
    event: str,
    message: str,
    *,
    details: list[str] | None = None,
) -> None:
    args._top_level_error_message = message
    status = (
        emit.status
        if hasattr(emit, "status")
        else emit
        if callable(emit)
        else print
    )
    status(message)
    if details:
        for line in details:
            status(line)
    audit = getattr(args, "_audit_logger", None)
    if audit is not None:
        audit.log(
            event,
            message=message,
            extra={"diagnostics": details} if details else None,
        )
    elif hasattr(emit, "audit"):
        record: dict[str, object] = {
            "event": event,
            "message": message,
            "review_round": None,
            "fix_round": None,
        }
        if details:
            record["diagnostics"] = details
        emit.audit(record)
