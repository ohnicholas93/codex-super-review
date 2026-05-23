from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .cli_parsers import parse_bool, parse_model_spec, parse_percent, parse_positive_int
from .constants import DEFAULT_IMPLEMENTER_MODEL, DEFAULT_ORACLE_MODEL, DEFAULT_REVIEWER_MODEL
from .diagnostics import _print_failure_details
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run iterative Codex reviewer streams against a persistent implementer Codex session.",
        epilog="""Examples:
  codex-super-review
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --max-new-reviewer-streams 3
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --max-fix-rounds-per-reviewer 2
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --implementer-compact-threshold-percent 60
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --write-audit-log true
  codex-super-review --branch-base release/2026.05
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --implementer "gpt 5.5 medium" --reviewer "gpt 5.4 xhigh"
  codex-super-review IMPLEMENTER_CODEX_SESSION_ID --oracle "gpt 5.4 mini low"

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if (
        shutil.which(args.codex_bin) is None
        and Path(args.codex_bin).name == args.codex_bin
    ):
        print(
            f"error: could not find Codex executable on PATH: {args.codex_bin}",
            file=sys.stderr,
        )
        return 127

    try:
        return orchestrate(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except CodexExecutableNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 127
    except CodexRunFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_failure_details(exc.result)
        return exc.result.returncode or 1
    except CodexResultDiagnostics as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_failure_details(exc.result)
        return 1
    except CodexAppServerFailure as exc:
        print(f"error: implementer pre-fix compaction failed: {exc}", file=sys.stderr)
        return 1
    except LimitReached as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
