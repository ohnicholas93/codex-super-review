from __future__ import annotations

import sys
import argparse
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
from .models import RoundDiagnostics
from .oracle import CodexOracle
from .prompts import (
    PROMPT_REVIEW_CHANGES,
    PROMPT_VALIDATE_FIX_COMMENTS,
    PROMPT_VALIDATE_FOLLOWUP_COMMENTS,
    build_reverify_prompt,
)
from .reviewer_retries import (
    _run_reviewer_review_with_retries,
    _run_reviewer_reverify_with_retries,
    _run_reviewer_rewrite_without_rejected_with_retries,
)
from .cli_parsers import parse_model_spec
from .workflow_helpers import _has_round_remaining, no_findings

def orchestrate(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    runner = CodexExecRunner(args.codex_bin, cwd)
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

    try:
        if audit.path is not None:
            print(f"Audit log: {audit.path}", file=sys.stderr)
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
        print(f"Oracle workspace: {oracle_cwd}", file=sys.stderr)

        outer_round = 0
        while _has_round_remaining(outer_round, args.max_new_reviewer_streams):
            outer_round += 1
            print(f"Starting reviewer stream {outer_round}", file=sys.stderr)

            reviewer, review_result = _run_reviewer_review_with_retries(
                runner,
                reviewer_model,
                PROMPT_REVIEW_CHANGES,
                review_round=outer_round,
                audit=audit,
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
            current_prompt = PROMPT_VALIDATE_FIX_COMMENTS
            current_comments = review_result.response

            if outer_round >= 2 and implementer_responses:
                if oracle_client is None:
                    candidate_oracle_client = None
                    try:
                        oracle_cwd.mkdir(parents=True, exist_ok=True)
                        candidate_oracle_client = AppServerJsonRpcClient(
                            args.codex_bin, oracle_cwd, oracle_model
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
                            "status": classification.status,
                            "rejected_findings_explanation": classification.rejected_findings_explanation,
                        },
                    )
                    if classification.status == "ONLY_REJECTED_FINDINGS":
                        audit.log(
                            "complete_only_rejected_findings",
                            review_round=outer_round,
                            reviewer_thread_id=reviewer.thread_id,
                            message="fresh reviewer only returned findings previously rejected by implementer",
                            extra={
                                "rejected_findings_explanation": classification.rejected_findings_explanation,
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
                                    classification.rejected_findings_explanation,
                                    review_round=outer_round,
                                    audit=audit,
                                    round_diagnostics=round_diagnostics,
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
                                    "rejected_findings_explanation": classification.rejected_findings_explanation,
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
                                        "rejected_findings_explanation": classification.rejected_findings_explanation,
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
                        implementer_thread_id=args.implementer_codex_session_id,
                        implementer_model=implementer_model,
                        threshold_percent=args.implementer_compact_threshold_percent,
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
                ensure_success("implementer fix", fix_result)
                implementer_responses.append(fix_result.response)

                reverify_prompt = build_reverify_prompt(fix_result.response)
                reviewer, reverify_result = _run_reviewer_reverify_with_retries(
                    runner,
                    reviewer,
                    reviewer_model,
                    reverify_prompt,
                    current_comments,
                    fix_result.response,
                    review_round=outer_round,
                    fix_round=fix_round,
                    audit=audit,
                    round_diagnostics=round_diagnostics,
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

                current_prompt = PROMPT_VALIDATE_FOLLOWUP_COMMENTS
                current_comments = reverify_result.response
            else:
                raise LimitReached(
                    f"max fix rounds per reviewer reached for reviewer stream {outer_round}"
                )

        raise LimitReached("max new reviewer streams reached before convergence")
    finally:
        if oracle_client is not None:
            oracle_client.close()
        audit.close()
