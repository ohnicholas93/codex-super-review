from __future__ import annotations

import sys

from .audit import AuditLogger
from .codex_exec import CodexExecRunner, CodexReviewer
from .constants import MAX_REVIEWER_TOOL_RETRIES
from .diagnostics import _collect_problem_lines, _raise_for_result_problems, ensure_success
from .errors import CodexResultDiagnostics, CodexRunFailure
from .models import CodexResult, ModelSpec, RoundDiagnostics
from .prompts import build_reverify_retry_prompt, build_rewrite_without_rejected_prompt
from .workflow_helpers import no_findings

def _run_reviewer_review_with_retries(
    runner: CodexExecRunner,
    reviewer_model: ModelSpec,
    prompt: str,
    *,
    review_round: int,
    audit: AuditLogger,
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    while True:
        reviewer = CodexReviewer(runner, reviewer_model)
        result = reviewer.review(prompt)
        audit.log(
            "review",
            review_round=review_round,
            reviewer_thread_id=reviewer.thread_id,
            prompt=prompt,
            response=result.response,
            result=result,
        )
        try:
            ensure_success("reviewer initial review", result)
            _raise_for_result_problems(
                "reviewer initial review", result, allow_diagnostics=False
            )
            if reviewer.thread_id is None:
                raise RuntimeError("reviewer did not emit a thread id")
            return reviewer, result
        except (CodexRunFailure, CodexResultDiagnostics) as exc:
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            print(
                f"Reviewer stream {review_round} hit a transient failure during initial review; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
                file=sys.stderr,
            )
            audit.log(
                "reviewer_retryable_failure",
                review_round=review_round,
                reviewer_thread_id=reviewer.thread_id,
                prompt=prompt,
                response=result.response,
                result=result,
                message=f"initial review retry {attempt} of {MAX_REVIEWER_TOOL_RETRIES}",
            )


def _run_reviewer_reverify_with_retries(
    runner: CodexExecRunner,
    reviewer: CodexReviewer,
    reviewer_model: ModelSpec,
    prompt: str,
    reviewer_comments: str,
    developer_response: str,
    *,
    review_round: int,
    fix_round: int,
    audit: AuditLogger,
    round_diagnostics: list[RoundDiagnostics],
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    current_reviewer = reviewer
    while True:
        if attempt > 0:
            current_reviewer = CodexReviewer(runner, reviewer_model)
            retry_prompt = build_reverify_retry_prompt(
                reviewer_comments, developer_response
            )
            result = current_reviewer.review(retry_prompt)
        else:
            retry_prompt = prompt
            result = current_reviewer.reverify(prompt)
        reverify_errors, reverify_diagnostics = _collect_problem_lines(result)
        if reverify_errors or reverify_diagnostics:
            round_diagnostics.append(
                RoundDiagnostics(
                    phase="reviewer-reverify",
                    review_round=review_round,
                    fix_round=fix_round,
                    errors=reverify_errors,
                    diagnostics=reverify_diagnostics,
                )
            )
        audit.log(
            "reviewer_reverify",
            review_round=review_round,
            fix_round=fix_round,
            reviewer_thread_id=current_reviewer.thread_id,
            prompt=retry_prompt,
            response=result.response,
            result=result,
        )
        try:
            ensure_success("reviewer reverification", result)
            _raise_for_result_problems(
                "reviewer reverification", result, allow_diagnostics=False
            )
            if current_reviewer.thread_id is None:
                raise RuntimeError("reviewer did not emit a thread id")
            return current_reviewer, result
        except (CodexRunFailure, CodexResultDiagnostics) as exc:
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            print(
                f"Reviewer stream {review_round} hit a transient failure during reverification; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
                file=sys.stderr,
            )
            audit.log(
                "reviewer_retryable_failure",
                review_round=review_round,
                fix_round=fix_round,
                reviewer_thread_id=current_reviewer.thread_id,
                prompt=retry_prompt,
                response=result.response,
                result=result,
                message=f"reverification retry {attempt} of {MAX_REVIEWER_TOOL_RETRIES}",
            )
            current_reviewer = CodexReviewer(runner, reviewer_model)


def _run_reviewer_rewrite_without_rejected_with_retries(
    runner: CodexExecRunner,
    reviewer: CodexReviewer,
    reviewer_model: ModelSpec,
    reviewer_comments: str,
    rejected_findings_explanation: str,
    *,
    review_round: int,
    audit: AuditLogger,
    round_diagnostics: list[RoundDiagnostics],
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    while True:
        rewrite_prompt = build_rewrite_without_rejected_prompt(
            reviewer_comments,
            rejected_findings_explanation,
        )
        if attempt > 0:
            current_reviewer = CodexReviewer(runner, reviewer_model)
            result = current_reviewer.review(rewrite_prompt)
        else:
            current_reviewer = reviewer
            result = current_reviewer.reverify(rewrite_prompt)
        rewrite_errors, rewrite_diagnostics = _collect_problem_lines(result)
        if rewrite_errors or rewrite_diagnostics:
            round_diagnostics.append(
                RoundDiagnostics(
                    phase="reviewer-rewrite-without-rejected",
                    review_round=review_round,
                    fix_round=None,
                    errors=rewrite_errors,
                    diagnostics=rewrite_diagnostics,
                )
            )
        audit.log(
            "reviewer_rewrite_without_rejected",
            review_round=review_round,
            reviewer_thread_id=current_reviewer.thread_id,
            prompt=rewrite_prompt,
            response=result.response,
            result=result,
            extra={
                "rejected_findings_explanation": rejected_findings_explanation,
                "attempt": attempt + 1,
            },
        )
        try:
            ensure_success("reviewer rewrite without rejected findings", result)
            _raise_for_result_problems(
                "reviewer rewrite without rejected findings",
                result,
                allow_diagnostics=False,
            )
            if current_reviewer.thread_id is None:
                raise RuntimeError("reviewer did not emit a thread id")
            if no_findings(result.response):
                audit.log(
                    "complete_rewrite_without_rejected_findings",
                    review_round=review_round,
                    reviewer_thread_id=current_reviewer.thread_id,
                    prompt=rewrite_prompt,
                    response=result.response,
                    result=result,
                    message=(
                        "rewrite returned NO_FINDINGS after removing previously rejected findings"
                    ),
                    extra={
                        "attempt": attempt + 1,
                        "rejected_findings_explanation": rejected_findings_explanation,
                    },
                )
            return current_reviewer, result
        except (CodexRunFailure, CodexResultDiagnostics) as exc:
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            print(
                f"Reviewer stream {review_round} hit a transient failure while rewriting rejected findings; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
                file=sys.stderr,
            )
            audit.log(
                "reviewer_retryable_failure",
                review_round=review_round,
                reviewer_thread_id=current_reviewer.thread_id,
                prompt=rewrite_prompt,
                response=result.response,
                result=result,
                message=f"rewrite without rejected findings retry {attempt} of {MAX_REVIEWER_TOOL_RETRIES}",
                extra={
                    "rejected_findings_explanation": rejected_findings_explanation,
                },
            )

