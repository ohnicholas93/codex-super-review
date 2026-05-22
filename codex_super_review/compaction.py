from __future__ import annotations

import os
from pathlib import Path

from .app_server import AppServerJsonRpcClient
from .constants import CODEX_TUI_CONTEXT_BASELINE_TOKENS
from .models import ModelSpec, PreCompactResult
from .utils import _coerce_int, _get_nested

def maybe_compact_implementer_before_first_fix(
    *,
    codex_bin: str,
    cwd: Path,
    implementer_thread_id: str,
    implementer_model: ModelSpec,
    threshold_percent: float,
) -> PreCompactResult:
    if threshold_percent <= 0:
        return PreCompactResult(
            status="disabled",
            message="implementer pre-fix compaction disabled",
        )

    with AppServerJsonRpcClient(codex_bin, cwd, implementer_model) as client:
        token_usage = client.resume_thread_for_usage(implementer_thread_id)
        if token_usage is None:
            return PreCompactResult(
                status="skipped_no_usage",
                message="skipped implementer pre-fix compaction: no restored token usage available",
            )

        total = _coerce_int(_get_nested(token_usage, "total", "totalTokens"))
        active_context = _coerce_int(_get_nested(token_usage, "last", "totalTokens"))
        context_window = _coerce_int(token_usage.get("modelContextWindow"))
        if active_context is None or context_window is None or context_window <= 0:
            return PreCompactResult(
                status="skipped_incomplete_usage",
                message="skipped implementer pre-fix compaction: token usage did not include last.totalTokens and modelContextWindow",
                total_tokens=total,
                active_context_tokens=active_context,
                context_window=context_window,
            )

        effective_window = context_window - CODEX_TUI_CONTEXT_BASELINE_TOKENS
        effective_used = max(active_context - CODEX_TUI_CONTEXT_BASELINE_TOKENS, 0)
        if effective_window <= 0:
            usage_percent = 100.0
        else:
            usage_percent = min(
                max((effective_used / effective_window) * 100, 0.0), 100.0
            )
        if usage_percent < threshold_percent:
            return PreCompactResult(
                status="skipped_below_threshold",
                message=(
                    "skipped implementer pre-fix compaction: "
                    f"context usage {usage_percent:.1f}% is below {threshold_percent:.1f}%"
                ),
                total_tokens=total,
                active_context_tokens=active_context,
                context_window=context_window,
                usage_percent=usage_percent,
            )

        client.compact_thread(implementer_thread_id)
        return PreCompactResult(
            status="compacted",
            message=(
                "compacted implementer before first fix: "
                f"context usage {usage_percent:.1f}% met {threshold_percent:.1f}% threshold"
            ),
            total_tokens=total,
            active_context_tokens=active_context,
            context_window=context_window,
            usage_percent=usage_percent,
            compacted=True,
        )



def default_oracle_cwd() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "codex-super-review" / "oracle-workspace"
    return Path.home() / ".local" / "state" / "codex-super-review" / "oracle-workspace"
