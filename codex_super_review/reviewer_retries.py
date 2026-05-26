from __future__ import annotations

from collections.abc import Callable

from .audit import AuditLogger
from .codex_exec import CodexExecRunner, CodexReviewer
from .constants import MAX_REVIEWER_TOOL_RETRIES
from .diagnostics import _collect_problem_lines, _raise_for_result_problems, ensure_success
from .errors import CodexResultDiagnostics, CodexRunFailure
from .models import BranchReviewScope, CodexResult, ModelSpec, RoundDiagnostics
from .prompts import build_reverify_retry_prompt, build_rewrite_without_rejected_prompt
from .workflow_helpers import no_findings

def _run_reviewer_review_with_retries(
    runner: CodexExecRunner,
    reviewer_model: ModelSpec,
    prompt: str,
    *,
    review_round: int,
    audit: AuditLogger,
    branch_scope_guard: Callable[[str | None], None] | None = None,
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    while True:
        reviewer = CodexReviewer(runner, reviewer_model)
        audit.start(
            "review",
            review_round=review_round,
            message=f"Reviewer stream {review_round}: initial review",
        )
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
            if branch_scope_guard is not None:
                branch_scope_guard(reviewer.thread_id)
            return reviewer, result
        except (CodexRunFailure, CodexResultDiagnostics) as exc:
            if branch_scope_guard is not None:
                _raise_branch_scope_violation_after_failure(
                    branch_scope_guard,
                    reviewer.thread_id,
                    "reviewer initial review",
                    exc,
                )
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            audit.status(
                f"Reviewer stream {review_round} hit a transient failure during initial review; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
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
    branch_scope: BranchReviewScope | None,
    audit: AuditLogger,
    round_diagnostics: list[RoundDiagnostics],
    branch_scope_guard: Callable[[str | None], None] | None = None,
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    current_reviewer = reviewer
    while True:
        if attempt > 0:
            current_reviewer = CodexReviewer(runner, reviewer_model)
            retry_prompt = build_reverify_retry_prompt(
                reviewer_comments,
                developer_response,
                branch_base=branch_scope.base_ref if branch_scope else None,
                branch_base_commit=branch_scope.base_commit
                if branch_scope
                else None,
                merge_base=branch_scope.merge_base if branch_scope else None,
            )
            audit.start(
                "reviewer_reverify",
                review_round=review_round,
                fix_round=fix_round,
                message=(
                    f"Reviewer stream {review_round}, fix round {fix_round}: "
                    "fresh reverification retry"
                ),
            )
            result = current_reviewer.review(retry_prompt)
        else:
            retry_prompt = prompt
            audit.start(
                "reviewer_reverify",
                review_round=review_round,
                fix_round=fix_round,
                message=(
                    f"Reviewer stream {review_round}, fix round {fix_round}: "
                    "reverification"
                ),
            )
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
            if branch_scope_guard is not None:
                branch_scope_guard(current_reviewer.thread_id)
            return current_reviewer, result
        except (CodexRunFailure, CodexResultDiagnostics) as exc:
            if branch_scope_guard is not None:
                _raise_branch_scope_violation_after_failure(
                    branch_scope_guard,
                    current_reviewer.thread_id,
                    "reviewer reverification",
                    exc,
                )
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            audit.status(
                f"Reviewer stream {review_round} hit a transient failure during reverification; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
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
    branch_scope_guard: Callable[[str | None], None] | None = None,
) -> tuple[CodexReviewer, CodexResult]:
    attempt = 0
    while True:
        rewrite_prompt = build_rewrite_without_rejected_prompt(
            reviewer_comments,
            rejected_findings_explanation,
        )
        if attempt > 0:
            current_reviewer = CodexReviewer(runner, reviewer_model)
            audit.start(
                "reviewer_rewrite_without_rejected",
                review_round=review_round,
                message=(
                    f"Reviewer stream {review_round}: fresh rewrite retry "
                    "without rejected findings"
                ),
            )
            result = current_reviewer.review(rewrite_prompt)
        else:
            current_reviewer = reviewer
            audit.start(
                "reviewer_rewrite_without_rejected",
                review_round=review_round,
                message=(
                    f"Reviewer stream {review_round}: rewrite without rejected findings"
                ),
            )
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
            if branch_scope_guard is not None:
                branch_scope_guard(current_reviewer.thread_id)
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
            if branch_scope_guard is not None:
                _raise_branch_scope_violation_after_failure(
                    branch_scope_guard,
                    current_reviewer.thread_id,
                    "reviewer rewrite without rejected findings",
                    exc,
                )
            if attempt >= MAX_REVIEWER_TOOL_RETRIES:
                raise exc
            attempt += 1
            audit.status(
                f"Reviewer stream {review_round} hit a transient failure while rewriting rejected findings; retrying with a fresh reviewer ({attempt}/{MAX_REVIEWER_TOOL_RETRIES})",
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


def _raise_branch_scope_violation_after_failure(
    branch_scope_guard: Callable[[str | None], None],
    reviewer_thread_id: str | None,
    phase: str,
    original_exc: CodexRunFailure | CodexResultDiagnostics,
) -> None:
    try:
        branch_scope_guard(reviewer_thread_id)
    except RuntimeError as guard_exc:
        raise RuntimeError(
            f"{guard_exc}; original {phase} failure was preserved as the cause"
        ) from original_exc
