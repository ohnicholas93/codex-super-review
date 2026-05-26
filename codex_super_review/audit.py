from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_sink import NullEventSink
from .models import CodexResult, ModelSpec

class AuditLogger:
    def __init__(
        self,
        enabled: bool,
        cwd: Path,
        implementer_thread_id: str | None,
        implementer_model: ModelSpec,
        reviewer_model: ModelSpec,
        event_sink: Any | None = None,
    ) -> None:
        self.enabled = enabled
        self.cwd = str(cwd)
        self.implementer_thread_id = implementer_thread_id
        self.implementer_model = implementer_model
        self.reviewer_model = reviewer_model
        self.event_sink = event_sink if event_sink is not None else NullEventSink()
        self.path: Path | None = None
        self._file = None
        self._failure_warned = False

        if not enabled:
            return

        try:
            audit_dir = self._select_audit_dir()
            audit_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = (
                audit_dir / f"codex-super-review-{timestamp}-{os.getpid()}.jsonl"
            )
            self._file = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            self.enabled = False
            self.path = None
            self._file = None
            self.status(f"warning: audit logging disabled: {exc}")

    def set_implementer_thread_id(self, thread_id: str) -> None:
        self.implementer_thread_id = thread_id

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def status(self, message: str) -> None:
        self.event_sink.status(message)

    def start(
        self,
        event: str,
        *,
        review_round: int | None = None,
        fix_round: int | None = None,
        message: str | None = None,
    ) -> None:
        self.event_sink.start(
            event,
            review_round=review_round,
            fix_round=fix_round,
            message=message,
        )

    def log(
        self,
        event: str,
        *,
        review_round: int | None = None,
        fix_round: int | None = None,
        reviewer_thread_id: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
        result: CodexResult | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "cwd": self.cwd,
            "implementer_thread_id": self.implementer_thread_id,
            "implementer_model": self.implementer_model.model,
            "implementer_reasoning_effort": self.implementer_model.reasoning_effort,
            "reviewer_model": self.reviewer_model.model,
            "reviewer_reasoning_effort": self.reviewer_model.reasoning_effort,
            "review_round": review_round,
            "fix_round": fix_round,
            "reviewer_thread_id": reviewer_thread_id,
            "prompt": prompt,
            "response": response,
            "codex_exit_code": result.returncode if result is not None else None,
            "usage": result.usage if result is not None else None,
            "event_types": result.event_types if result is not None else None,
            "codex_errors": result.errors if result is not None else None,
            "diagnostics": result.diagnostics[-20:] if result is not None else None,
            "message": message,
        }
        if extra is not None:
            record["extra"] = extra
        if self.enabled and self._file is not None:
            try:
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._file.flush()
            except OSError as exc:
                self._disable_after_failure(exc)
        self.event_sink.audit(record)

    def _disable_after_failure(self, exc: OSError) -> None:
        self.enabled = False
        if not self._failure_warned:
            self.status(f"warning: audit logging disabled after write failure: {exc}")
            self._failure_warned = True
        self.close()

    @staticmethod
    def _select_audit_dir() -> Path:
        system_dir = Path("/var/log/codex-super-review")
        if system_dir.is_dir() and os.access(system_dir, os.W_OK):
            return system_dir

        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home:
            return Path(xdg_state_home) / "codex-super-review" / "audit"

        return Path.home() / ".local" / "state" / "codex-super-review" / "audit"
