from __future__ import annotations

import sys

from .errors import CodexResultDiagnostics, CodexRunFailure
from .models import CodexResult, RoundDiagnostics

def codex_errors_for_diagnostics(result: CodexResult) -> list[str]:
    recovered_turn = result.returncode == 0 and "turn.completed" in result.event_types
    return [
        line
        for line in result.errors
        if not (recovered_turn and line.startswith("Reconnecting..."))
    ]


def _collect_problem_lines(result: CodexResult) -> tuple[list[str], list[str]]:
    errors = codex_errors_for_diagnostics(result)
    diagnostic_lines = [
        line
        for line in result.diagnostics
        if "ERROR " in line or "error=" in line.lower()
    ]
    return errors, diagnostic_lines


def _round_diagnostics_summary(entry: RoundDiagnostics) -> str:
    parts = [f"{entry.phase}"]
    parts.append(f"reviewer stream {entry.review_round}")
    if entry.fix_round is not None:
        parts.append(f"fix round {entry.fix_round}")
    parts.append(f"errors={len(entry.errors)}")
    parts.append(f"diagnostics={len(entry.diagnostics)}")
    return ", ".join(parts)


def round_diagnostics_lines(entries: list[RoundDiagnostics]) -> list[str]:
    if not entries:
        return []
    lines = ["Round diagnostics:"]
    for entry in entries:
        lines.append(f"  {_round_diagnostics_summary(entry)}")
        for error in entry.errors[-3:]:
            lines.append(f"    error: {error}")
        for line in entry.diagnostics[-3:]:
            lines.append(f"    diagnostic: {line}")
    return lines


def _print_round_diagnostics(entries: list[RoundDiagnostics]) -> None:
    for line in round_diagnostics_lines(entries):
        print(line, file=sys.stderr)



def _raise_for_result_problems(
    phase: str,
    result: CodexResult,
    *,
    allow_diagnostics: bool,
) -> None:
    errors, diagnostics = _collect_problem_lines(result)
    if errors or (diagnostics and not allow_diagnostics):
        raise CodexResultDiagnostics(phase, result)



def ensure_success(phase: str, result: CodexResult) -> None:
    if result.returncode != 0:
        raise CodexRunFailure(phase, result)


def _print_failure_details(result: CodexResult) -> None:
    for line in failure_details_lines(result):
        print(line, file=sys.stderr)


def failure_details_lines(result: CodexResult) -> list[str]:
    lines: list[str] = []
    errors = codex_errors_for_diagnostics(result)
    if errors:
        lines.append("Codex errors:")
        for error in errors:
            lines.append(f"  {error}")
    if result.diagnostics:
        lines.append("Diagnostics:")
        for line in result.diagnostics[-20:]:
            lines.append(f"  {line}")
    return lines
