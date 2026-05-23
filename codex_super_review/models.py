from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class CodexResult:
    command: list[str]
    returncode: int
    response: str
    thread_id: str | None
    usage: dict[str, Any] | None
    event_types: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelSpec:
    raw: str
    model: str
    reasoning_effort: str

    @property
    def display(self) -> str:
        return f"{self.model} {self.reasoning_effort}"


@dataclass(frozen=True)
class BranchReviewScope:
    base_ref: str
    base_commit: str
    head_commit: str
    head_ref: str | None
    head_reflog_state: str | None
    merge_base: str


@dataclass
class RoundDiagnostics:
    phase: str
    review_round: int
    fix_round: int | None
    errors: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.errors and not self.diagnostics


@dataclass(frozen=True)
class PreCompactResult:
    status: str
    message: str
    total_tokens: int | None = None
    active_context_tokens: int | None = None
    context_window: int | None = None
    usage_percent: float | None = None
    compacted: bool = False


@dataclass(frozen=True)
class AppServerTurnResult:
    thread_id: str
    turn_id: str
    response: str
    turn_status: str | None = None


@dataclass(frozen=True)
class OracleClassification:
    explanation: str
    status: str
