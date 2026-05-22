from __future__ import annotations

import sys

from .errors import CodexResultDiagnostics, CodexRunFailure
from .models import CodexResult, RoundDiagnostics

def _collect_problem_lines(result: CodexResult) -> tuple[list[str], list[str]]:
    diagnostic_lines = [
        line
        for line in result.diagnostics
        if "ERROR " in line or "error=" in line.lower()
    ]
    return result.errors.copy(), diagnostic_lines


def _round_diagnostics_summary(entry: RoundDiagnostics) -> str:
    parts = [f"{entry.phase}"]
    parts.append(f"reviewer stream {entry.review_round}")
    if entry.fix_round is not None:
        parts.append(f"fix round {entry.fix_round}")
    parts.append(f"errors={len(entry.errors)}")
    parts.append(f"diagnostics={len(entry.diagnostics)}")
    return ", ".join(parts)


def _print_round_diagnostics(entries: list[RoundDiagnostics]) -> None:
    if not entries:
        return
    print("Round diagnostics:", file=sys.stderr)
    for entry in entries:
        print(f"  {_round_diagnostics_summary(entry)}", file=sys.stderr)
        for error in entry.errors[-3:]:
            print(f"    error: {error}", file=sys.stderr)
        for line in entry.diagnostics[-3:]:
            print(f"    diagnostic: {line}", file=sys.stderr)



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
    if result.errors:
        print("Codex errors:", file=sys.stderr)
        for error in result.errors:
            print(f"  {error}", file=sys.stderr)
    if result.diagnostics:
        print("Diagnostics:", file=sys.stderr)
        for line in result.diagnostics[-20:]:
            print(f"  {line}", file=sys.stderr)
