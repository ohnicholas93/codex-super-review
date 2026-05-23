from __future__ import annotations

import sys
import argparse
import subprocess
from pathlib import Path

from .app_server import AppServerJsonRpcClient
from .audit import AuditLogger
from .codex_exec import CodexExecRunner, CodexImplementer
from .compaction import default_oracle_cwd, maybe_compact_implementer_before_first_fix
from .constants import (
    IMPLEMENTER_APPROVALS_REVIEWER,
    IMPLEMENTER_APPROVAL_POLICY,
)
from .diagnostics import _collect_problem_lines, _print_round_diagnostics, ensure_success
from .errors import (
    CodexAppServerFailure,
    CodexExecutableNotFound,
    CodexResultDiagnostics,
    CodexRunFailure,
    LimitReached,
)
from .interrupts import GracefulInterruptController
from .models import BranchReviewScope, RoundDiagnostics
from .oracle import CodexOracle
from .prompts import (
    PROMPT_REVIEW_BRANCH,
    PROMPT_REVIEW_CHANGES,
    PROMPT_VALIDATE_BRANCH_FIX_COMMENTS,
    PROMPT_VALIDATE_BRANCH_FOLLOWUP_COMMENTS,
    PROMPT_VALIDATE_FIX_COMMENTS,
    PROMPT_VALIDATE_FOLLOWUP_COMMENTS,
    build_branch_reverify_prompt,
    build_reverify_prompt,
    format_prompt,
)
from .reviewer_retries import (
    _run_reviewer_review_with_retries,
    _run_reviewer_reverify_with_retries,
    _run_reviewer_rewrite_without_rejected_with_retries,
)
from .cli_parsers import parse_model_spec
from .workflow_helpers import _has_round_remaining, no_findings

def orchestrate(args: argparse.Namespace) -> int:
    interrupt_controller = GracefulInterruptController()
    cwd = Path.cwd()
    runner = CodexExecRunner(args.codex_bin, cwd, interrupt_controller)
    implementer_model = parse_model_spec(args.implementer_model)
    reviewer_model = parse_model_spec(args.reviewer_model)
    oracle_model = parse_model_spec(args.oracle_model)
    oracle_cwd = default_oracle_cwd()
    oracle_client: AppServerJsonRpcClient | None = None
    oracle: CodexOracle | None = None
    implementer = CodexImplementer(
        runner, args.implementer_codex_session_id, implementer_model
    )
    audit = AuditLogger(
        args.write_audit_log,
        cwd,
        args.implementer_codex_session_id,
        implementer_model,
        reviewer_model,
    )
    round_diagnostics: list[RoundDiagnostics] = []
    implementer_responses: list[str] = []
    branch_scope: BranchReviewScope | None = None
    reviewer_prompt = PROMPT_REVIEW_CHANGES
    initial_fix_prompt = PROMPT_VALIDATE_FIX_COMMENTS
    followup_fix_prompt = PROMPT_VALIDATE_FOLLOWUP_COMMENTS
    last_logged_implementer_thread_id = args.implementer_codex_session_id

    interrupt_controller.__enter__()
    try:
        try:
            branch_scope = _prepare_branch_review_scope(cwd, args.branch_base, audit)
            reviewer_prompt = _reviewer_prompt(branch_scope)
            initial_fix_prompt = _initial_fix_prompt(branch_scope)
            followup_fix_prompt = _followup_fix_prompt(branch_scope)
        except RuntimeError as exc:
            audit.log(
                "branch_preflight_failed",
                message=str(exc),
                extra={
                    "branch_base": args.branch_base,
                },
            )
            raise

        if audit.path is not None:
            print(f"Audit log: {audit.path}", file=sys.stderr)
        if implementer.thread_id is None:
            print(
                "Implementer session: a fresh session will be created on the first fix round",
                file=sys.stderr,
            )
        else:
            print(f"Implementer session: {implementer.thread_id}", file=sys.stderr)
        print(
            f"Requested implementer model: {implementer_model.display}", file=sys.stderr
        )
        print(
            f"Implementer approval policy: {IMPLEMENTER_APPROVAL_POLICY}",
            file=sys.stderr,
        )
        print(
            f"Implementer approvals reviewer: {IMPLEMENTER_APPROVALS_REVIEWER}",
            file=sys.stderr,
        )
        print(f"Requested reviewer model: {reviewer_model.display}", file=sys.stderr)
        print(f"Requested oracle model: {oracle_model.display}", file=sys.stderr)
        print(
            f"Review scope: {'branch' if branch_scope is not None else 'changes'}",
            file=sys.stderr,
        )
        if branch_scope is not None:
            print(f"Branch base: {branch_scope.base_ref}", file=sys.stderr)
            print(f"Branch base commit: {branch_scope.base_commit}", file=sys.stderr)
            print(f"Branch HEAD commit: {branch_scope.head_commit}", file=sys.stderr)
            print(
                f"Branch HEAD ref: {branch_scope.head_ref or '(detached)'}",
                file=sys.stderr,
            )
            print(f"Merge base: {branch_scope.merge_base}", file=sys.stderr)
        print(f"Oracle workspace: {oracle_cwd}", file=sys.stderr)

        outer_round = 0
        while _has_round_remaining(outer_round, args.max_new_reviewer_streams):
            if interrupt_controller.should_stop_before_next_reviewer():
                audit.log(
                    "interrupted_before_next_reviewer",
                    review_round=outer_round,
                    message="interrupt requested; stopped before starting the next reviewer stream",
                )
                print(
                    "Stopped before starting the next reviewer stream",
                    file=sys.stderr,
                )
                return 130
            outer_round += 1
            if interrupt_controller.should_stop_before_next_reviewer():
                audit.log(
                    "interrupted_before_next_reviewer",
                    review_round=outer_round,
                    message="interrupt requested; stopped before starting the next reviewer stream",
                )
                print(
                    "Stopped before starting the next reviewer stream",
                    file=sys.stderr,
                )
                return 130
            print(f"Starting reviewer stream {outer_round}", file=sys.stderr)

            reviewer, review_result = _run_reviewer_review_with_retries(
                runner,
                reviewer_model,
                reviewer_prompt,
                review_round=outer_round,
                audit=audit,
                branch_scope_guard=lambda reviewer_thread_id: _ensure_branch_scope_refs_unchanged(
                    cwd,
                    branch_scope,
                    audit=audit,
                    review_round=outer_round,
                    fix_round=None,
                    reviewer_thread_id=reviewer_thread_id,
                ),
            )

            if no_findings(review_result.response):
                audit.log(
                    "complete",
                    review_round=outer_round,
                    reviewer_thread_id=reviewer.thread_id,
                    message="fresh reviewer found no findings",
                )
                print("Complete: fresh reviewer returned NO_FINDINGS", file=sys.stderr)
                return 0

            fix_round = 0
            current_prompt = initial_fix_prompt
            current_comments = review_result.response

            if outer_round >= 2 and implementer_responses:
                if oracle_client is None:
                    candidate_oracle_client = None
                    try:
                        oracle_cwd.mkdir(parents=True, exist_ok=True)
                        candidate_oracle_client = AppServerJsonRpcClient(
                            args.codex_bin,
                            oracle_cwd,
                            oracle_model,
                            interrupt_controller,
                        )
                        candidate_oracle_client.__enter__()
                    except (
                        OSError,
                        CodexAppServerFailure,
                        CodexExecutableNotFound,
                    ) as exc:
                        if candidate_oracle_client is not None:
                            candidate_oracle_client.close()
                        audit.log(
                            "oracle_failed_open",
                            review_round=outer_round,
                            message=str(exc),
                            extra={
                                "phase": "startup",
                                "oracle_workspace": str(oracle_cwd),
                            },
                        )
                        print(
                            f"Warning: oracle unavailable; continuing without rejected-finding dedupe ({exc})",
                            file=sys.stderr,
                        )
                        candidate_oracle_client = None
                    if candidate_oracle_client is not None:
                        oracle_client = candidate_oracle_client
                        oracle = CodexOracle(
                            oracle_client,
                            oracle_cwd,
                            audit,
                            args.max_oracle_retries,
                        )
                classification = None
                if oracle is not None:
                    print(
                        f"Reviewer stream {outer_round}: checking for previously rejected findings",
                        file=sys.stderr,
                    )
                    classification = oracle.classify(
                        latest_developer_response=implementer_responses[-1],
                        current_findings=current_comments,
                        review_round=outer_round,
                    )
                    if classification is None:
                        print(
                            "Warning: oracle classification unavailable; continuing without rejected-finding dedupe",
                            file=sys.stderr,
                        )
                        if oracle.reset_client_after_failure:
                            audit.log(
                                "oracle_client_reset",
                                review_round=outer_round,
                                message="resetting oracle app-server client after transport failure",
                            )
                            oracle_client.close()
                            oracle_client = None
                            oracle = None
                if classification is not None:
                    audit.log(
                        "oracle_classification_result",
                        review_round=outer_round,
                        reviewer_thread_id=reviewer.thread_id,
                        message=classification.status,
                        extra={
                            "explanation": classification.explanation,
                            "status": classification.status,
                        },
                    )
                    if classification.status == "ONLY_REJECTED_FINDINGS":
                        audit.log(
                            "complete_only_rejected_findings",
                            review_round=outer_round,
                            reviewer_thread_id=reviewer.thread_id,
                            message="fresh reviewer only returned findings previously rejected by implementer",
                            extra={
                                "explanation": classification.explanation,
                            },
                        )
                        print(
                            "Complete: fresh reviewer only returned previously rejected findings",
                            file=sys.stderr,
                        )
                        return 0
                    if classification.status == "HAS_REJECTED_AND_NEW_FINDINGS":
                        try:
                            reviewer, rewrite_result = (
                                _run_reviewer_rewrite_without_rejected_with_retries(
                                    runner,
                                    reviewer,
                                    reviewer_model,
                                    current_comments,
                                    classification.explanation,
                                    review_round=outer_round,
                                    audit=audit,
                                    round_diagnostics=round_diagnostics,
                                    branch_scope_guard=lambda reviewer_thread_id: _ensure_branch_scope_refs_unchanged(
                                        cwd,
                                        branch_scope,
                                        audit=audit,
                                        review_round=outer_round,
                                        fix_round=None,
                                        reviewer_thread_id=reviewer_thread_id,
                                    ),
                                )
                            )
                        except (CodexRunFailure, CodexResultDiagnostics) as exc:
                            failed_result = exc.result
                            audit.log(
                                "reviewer_rewrite_without_rejected_failed_open",
                                review_round=outer_round,
                                reviewer_thread_id=reviewer.thread_id,
                                response=failed_result.response,
                                result=failed_result,
                                message=(
                                    "rewrite without rejected findings failed; "
                                    "continuing with original reviewer comments"
                                ),
                                extra={
                                    "explanation": classification.explanation,
                                    "phase": exc.phase,
                                },
                            )
                            print(
                                "Warning: reviewer rewrite without rejected findings failed; continuing with original reviewer comments",
                                file=sys.stderr,
                            )
                        else:
                            current_comments = rewrite_result.response
                            if no_findings(current_comments):
                                audit.log(
                                    "complete_rewrite_without_rejected_findings",
                                    review_round=outer_round,
                                    reviewer_thread_id=reviewer.thread_id,
                                    response=current_comments,
                                    result=rewrite_result,
                                    message="sanitized reviewer comments returned NO_FINDINGS",
                                    extra={
                                        "explanation": classification.explanation,
                                    },
                                )
                                print(
                                    "Complete: removing previously rejected findings left no remaining findings",
                                    file=sys.stderr,
                                )
                                return 0

            while _has_round_remaining(fix_round, args.max_fix_rounds_per_reviewer):
                fix_round += 1
                print(
                    f"Reviewer {outer_round}, fix round {fix_round}: sending findings to implementer",
                    file=sys.stderr,
                )

                if fix_round == 1:
                    print(
                        "Checking implementer context before first fix round",
                        file=sys.stderr,
                    )
                    precompact_result = maybe_compact_implementer_before_first_fix(
                        codex_bin=args.codex_bin,
                        cwd=cwd,
                        implementer_thread_id=implementer.thread_id,
                        implementer_model=implementer_model,
                        threshold_percent=args.implementer_compact_threshold_percent,
                        interrupt_controller=interrupt_controller,
                    )
                    print(precompact_result.message, file=sys.stderr)
                    audit.log(
                        "implementer_precompact",
                        review_round=outer_round,
                        fix_round=fix_round,
                        reviewer_thread_id=reviewer.thread_id,
                        message=precompact_result.message,
                        extra={
                            "status": precompact_result.status,
                            "total_tokens": precompact_result.total_tokens,
                            "active_context_tokens": precompact_result.active_context_tokens,
                            "context_window": precompact_result.context_window,
                            "usage_percent": precompact_result.usage_percent,
                            "threshold_percent": args.implementer_compact_threshold_percent,
                            "compacted": precompact_result.compacted,
                        },
                    )

                implementer_prompt = f"{current_prompt}\n\n{current_comments}"
                fix_result = implementer.fix(implementer_prompt)
                if (
                    implementer.thread_id is not None
                    and implementer.thread_id != last_logged_implementer_thread_id
                ):
                    last_logged_implementer_thread_id = implementer.thread_id
                    audit.set_implementer_thread_id(implementer.thread_id)
                    print(
                        f"Implementer session: {implementer.thread_id}",
                        file=sys.stderr,
                    )
                fix_errors, fix_diagnostics = _collect_problem_lines(fix_result)
                if fix_errors or fix_diagnostics:
                    round_diagnostics.append(
                        RoundDiagnostics(
                            phase="implementer",
                            review_round=outer_round,
                            fix_round=fix_round,
                            errors=fix_errors,
                            diagnostics=fix_diagnostics,
                        )
                    )
                audit.log(
                    "implementer_fix",
                    review_round=outer_round,
                    fix_round=fix_round,
                    reviewer_thread_id=reviewer.thread_id,
                    prompt=implementer_prompt,
                    response=fix_result.response,
                    result=fix_result,
                )
                try:
                    ensure_success("implementer fix", fix_result)
                except (CodexRunFailure, CodexResultDiagnostics) as exc:
                    try:
                        _ensure_branch_scope_refs_unchanged(
                            cwd,
                            branch_scope,
                            audit=audit,
                            review_round=outer_round,
                            fix_round=fix_round,
                            reviewer_thread_id=reviewer.thread_id,
                        )
                    except RuntimeError as guard_exc:
                        raise RuntimeError(
                            f"{guard_exc}; original implementer fix failure was preserved as the cause"
                        ) from exc
                    raise
                else:
                    _ensure_branch_scope_refs_unchanged(
                        cwd,
                        branch_scope,
                        audit=audit,
                        review_round=outer_round,
                        fix_round=fix_round,
                        reviewer_thread_id=reviewer.thread_id,
                    )
                implementer_responses.append(fix_result.response)

                reverify_prompt = _reverify_prompt(
                    fix_result.response,
                    branch_scope,
                )
                reviewer, reverify_result = _run_reviewer_reverify_with_retries(
                    runner,
                    reviewer,
                    reviewer_model,
                    reverify_prompt,
                    current_comments,
                    fix_result.response,
                    review_round=outer_round,
                    fix_round=fix_round,
                    branch_scope=branch_scope,
                    audit=audit,
                    round_diagnostics=round_diagnostics,
                    branch_scope_guard=lambda reviewer_thread_id: _ensure_branch_scope_refs_unchanged(
                        cwd,
                        branch_scope,
                        audit=audit,
                        review_round=outer_round,
                        fix_round=fix_round,
                        reviewer_thread_id=reviewer_thread_id,
                    ),
                )

                if no_findings(reverify_result.response):
                    audit.log(
                        "reviewer_satisfied",
                        review_round=outer_round,
                        fix_round=fix_round,
                        reviewer_thread_id=reviewer.thread_id,
                        message="reviewer stream satisfied",
                    )
                    print(f"Reviewer stream {outer_round} satisfied", file=sys.stderr)
                    _print_round_diagnostics(
                        [
                            entry
                            for entry in round_diagnostics
                            if entry.review_round == outer_round
                        ]
                    )
                    break

                current_prompt = followup_fix_prompt
                current_comments = reverify_result.response
            else:
                raise LimitReached(
                    f"max fix rounds per reviewer reached for reviewer stream {outer_round}"
                )

        raise LimitReached("max new reviewer streams reached before convergence")
    finally:
        try:
            if oracle_client is not None:
                oracle_client.close()
            audit.close()
        finally:
            interrupt_controller.__exit__(None, None, None)


def _reviewer_prompt(
    branch_scope: BranchReviewScope | None,
) -> str:
    if branch_scope is not None:
        return format_prompt(
            PROMPT_REVIEW_BRANCH,
            branch_base=branch_scope.base_ref,
            branch_base_commit=branch_scope.base_commit,
            merge_base=branch_scope.merge_base,
        )
    return PROMPT_REVIEW_CHANGES


def _initial_fix_prompt(
    branch_scope: BranchReviewScope | None,
) -> str:
    if branch_scope is not None:
        return format_prompt(
            PROMPT_VALIDATE_BRANCH_FIX_COMMENTS,
            branch_base=branch_scope.base_ref,
            branch_base_commit=branch_scope.base_commit,
            merge_base=branch_scope.merge_base,
        )
    return PROMPT_VALIDATE_FIX_COMMENTS


def _followup_fix_prompt(
    branch_scope: BranchReviewScope | None,
) -> str:
    if branch_scope is not None:
        return format_prompt(
            PROMPT_VALIDATE_BRANCH_FOLLOWUP_COMMENTS,
            branch_base=branch_scope.base_ref,
            branch_base_commit=branch_scope.base_commit,
            merge_base=branch_scope.merge_base,
        )
    return PROMPT_VALIDATE_FOLLOWUP_COMMENTS


def _reverify_prompt(
    developer_response: str,
    branch_scope: BranchReviewScope | None,
) -> str:
    if branch_scope is not None:
        return build_branch_reverify_prompt(
            developer_response,
            base_branch=branch_scope.base_ref,
            base_commit=branch_scope.base_commit,
            merge_base=branch_scope.merge_base,
        )
    return build_reverify_prompt(developer_response)


def _prepare_branch_review_scope(
    cwd: Path,
    branch_base: str | None,
    audit: AuditLogger,
) -> BranchReviewScope | None:
    if branch_base is None:
        return None
    if not _git_output(cwd, "rev-parse", "--verify", "--quiet", branch_base):
        raise RuntimeError(f"branch base does not exist: {branch_base}")
    _ensure_clean_worktree_for_branch_review(cwd)
    base_commit = _resolve_ref_commit(cwd, branch_base)
    head_commit = _resolve_head_commit(cwd)
    head_ref = _resolve_head_ref(cwd)
    head_reflog_state = _resolve_head_reflog_state(cwd)
    if head_reflog_state is None:
        audit.log(
            "branch_reflog_guard_unavailable",
            message=(
                "HEAD reflog state is unavailable; branch scope will enforce "
                "final HEAD commit/ref only"
            ),
        )
    merge_base = _resolve_merge_base(cwd, base_commit)
    return BranchReviewScope(
        base_ref=branch_base,
        base_commit=base_commit,
        head_commit=head_commit,
        head_ref=head_ref,
        head_reflog_state=head_reflog_state,
        merge_base=merge_base,
    )


def _ensure_clean_worktree_for_branch_review(cwd: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"could not determine clean worktree status: {details}")
    status = result.stdout.strip()
    if not status:
        return
    raise RuntimeError(
        "branch-scoped review requires a clean worktree at startup so later "
        "staged, unstaged, and untracked edits can be treated as repair edits"
    )


def _resolve_ref_commit(cwd: Path, ref: str) -> str:
    commit = _git_output(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if commit is None:
        raise RuntimeError(f"could not determine commit for branch base: {ref}")
    return commit


def _resolve_head_commit(cwd: Path) -> str:
    head = _git_output(cwd, "rev-parse", "--verify", "HEAD")
    if head is None:
        raise RuntimeError("could not determine current HEAD")
    return head


def _resolve_head_ref(cwd: Path) -> str | None:
    return _git_output(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")


def _resolve_head_reflog_state(cwd: Path) -> str | None:
    reflog_path_output = _git_output(cwd, "rev-parse", "--git-path", "logs/HEAD")
    if reflog_path_output is None:
        return None
    reflog_path = Path(reflog_path_output)
    if not reflog_path.is_absolute():
        reflog_path = cwd / reflog_path
    try:
        lines = reflog_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    tail = lines[-1] if lines else ""
    return f"{len(lines)}\x1f{tail}"


def _ensure_branch_scope_refs_unchanged(
    cwd: Path,
    branch_scope: BranchReviewScope | None,
    *,
    audit: AuditLogger,
    review_round: int,
    fix_round: int | None,
    reviewer_thread_id: str | None,
) -> None:
    if branch_scope is None:
        return
    current_head_commit = _resolve_head_commit(cwd)
    current_head_ref = _resolve_head_ref(cwd)
    current_head_reflog_state = (
        _resolve_head_reflog_state(cwd)
        if branch_scope.head_reflog_state is not None
        else None
    )
    if (
        current_head_commit == branch_scope.head_commit
        and current_head_ref == branch_scope.head_ref
        and current_head_reflog_state == branch_scope.head_reflog_state
    ):
        return
    if (
        current_head_commit != branch_scope.head_commit
        or current_head_ref != branch_scope.head_ref
    ):
        message = (
            "branch-scoped review cannot continue because HEAD changed from "
            f"{branch_scope.head_ref or '(detached)'}@{branch_scope.head_commit} "
            f"to {current_head_ref or '(detached)'}@{current_head_commit}"
        )
        audit.log(
            "branch_scope_ref_changed",
            review_round=review_round,
            fix_round=fix_round,
            reviewer_thread_id=reviewer_thread_id,
            message=message,
            extra={
                "expected_head_commit": branch_scope.head_commit,
                "current_head_commit": current_head_commit,
                "expected_head_ref": branch_scope.head_ref,
                "current_head_ref": current_head_ref,
                "expected_head_reflog_state": branch_scope.head_reflog_state,
                "current_head_reflog_state": current_head_reflog_state,
            },
        )
        raise RuntimeError(message)
    if current_head_reflog_state != branch_scope.head_reflog_state:
        message = (
            "branch-scoped review cannot continue because HEAD reflog changed "
            "during the branch-scoped run"
        )
        audit.log(
            "branch_scope_ref_changed",
            review_round=review_round,
            fix_round=fix_round,
            reviewer_thread_id=reviewer_thread_id,
            message=message,
            extra={
                "expected_head_commit": branch_scope.head_commit,
                "current_head_commit": current_head_commit,
                "expected_head_ref": branch_scope.head_ref,
                "current_head_ref": current_head_ref,
                "expected_head_reflog_state": branch_scope.head_reflog_state,
                "current_head_reflog_state": current_head_reflog_state,
            },
        )
        raise RuntimeError(message)


def _resolve_merge_base(cwd: Path, branch_base: str) -> str:
    merge_base = _git_output(cwd, "merge-base", branch_base, "HEAD")
    if merge_base is None:
        raise RuntimeError(f"could not determine merge base for {branch_base} and HEAD")
    return merge_base


def _git_output(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None
