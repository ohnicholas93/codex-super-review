from __future__ import annotations

from .constants import NO_FINDINGS

def no_findings(response: str) -> bool:
    return response.strip() == NO_FINDINGS


def _has_round_remaining(completed_rounds: int, max_rounds: int | None) -> bool:
    return max_rounds is None or completed_rounds < max_rounds
