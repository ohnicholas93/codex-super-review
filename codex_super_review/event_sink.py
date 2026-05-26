from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


@dataclass
class TuiRow:
    id: int
    status: str
    event: str
    review_round: int | None
    fix_round: int | None
    message: str
    started_at: float
    completed_at: float | None = None
    record: dict[str, Any] | None = None


@dataclass(frozen=True)
class TuiSnapshot:
    rows: tuple[TuiRow, ...]
    headers: dict[str, str]
    status_message: str
    started_at: float
    finished_at: float | None
    finished: bool
    returncode: int | None
    final_message: str | None
    abort_requested: bool


class NullEventSink:
    def __init__(self, stream: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    def set_interrupt_controller(self, controller: Any) -> None:
        return

    def header(self, key: str, value: str | None) -> None:
        return

    def status(self, message: str) -> None:
        print(message, file=self.stream)

    def start(
        self,
        event: str,
        *,
        review_round: int | None = None,
        fix_round: int | None = None,
        message: str | None = None,
    ) -> None:
        return

    def audit(self, record: dict[str, Any]) -> None:
        return

    def finish(self, returncode: int, message: str | None = None) -> None:
        return


class TuiEventSink:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: list[TuiRow] = []
        self._headers: dict[str, str] = {}
        self._status_message = "Starting"
        self._started_at = time.monotonic()
        self._finished_at: float | None = None
        self._finished = False
        self._returncode: int | None = None
        self._final_message: str | None = None
        self._next_id = 1
        self._interrupt_controller: Any = None
        self._interrupt_count = 0
        self._abort_requested = False

    def set_interrupt_controller(self, controller: Any) -> None:
        with self._lock:
            self._interrupt_controller = controller
            interrupt_count = self._interrupt_count
        if interrupt_count >= 1:
            controller.request_stop()
        if interrupt_count >= 2:
            controller.request_abort()
            controller.terminate_active_process()

    def header(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._headers.pop(key, None)
            else:
                self._headers[key] = value

    def header_value(self, key: str) -> str | None:
        with self._lock:
            return self._headers.get(key)

    def status(self, message: str) -> None:
        with self._lock:
            self._status_message = message

    def start(
        self,
        event: str,
        *,
        review_round: int | None = None,
        fix_round: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            self._status_message = message or _event_title(event)

    def audit(self, record: dict[str, Any]) -> None:
        return

    def apply_audit_record(self, record: dict[str, Any]) -> None:
        event = str(record.get("event") or "event")
        review_round = _optional_int(record.get("review_round"))
        fix_round = _optional_int(record.get("fix_round"))
        status = _record_status(record)
        message = _record_message(record)
        record_time = _record_monotonic_time(record)
        with self._lock:
            if record_time is not None and (
                not self._rows or record_time < self._started_at
            ):
                self._started_at = record_time
            if _is_intermediate_event(record):
                row = self._find_pending_row(event, review_round, fix_round)
                if row is not None:
                    row.message = message
                    row.record = record.copy()
                    self._status_message = message
                    return
            row = self._find_pending_row(event, review_round, fix_round)
            if row is None:
                pending_event = _pending_event_for(event)
                if pending_event is not None:
                    row = self._find_pending_row(
                        pending_event, review_round, fix_round
                    )
            if row is None and status in {"failed", "warning"}:
                row = self._find_related_pending_row(review_round, fix_round)
            if (
                row is None
                and status in {"failed", "warning"}
                and review_round is None
                and fix_round is None
            ):
                row = self._find_latest_pending_row()
            if row is None:
                row = TuiRow(
                    id=self._next_id,
                    status=status,
                    event=event,
                    review_round=review_round,
                    fix_round=fix_round,
                    message=message,
                    started_at=record_time if record_time is not None else time.monotonic(),
                )
                self._rows.append(row)
                self._next_id += 1
            else:
                row.event = event
            row.status = status
            row.message = message
            row.completed_at = (
                None
                if status == "running"
                else record_time if record_time is not None else time.monotonic()
            )
            row.record = record.copy()
            self._status_message = message

    def finish(
        self,
        returncode: int,
        message: str | None = None,
        *,
        finished_at: float | None = None,
    ) -> None:
        with self._lock:
            self._finished = True
            self._finished_at = finished_at if finished_at is not None else time.monotonic()
            self._returncode = returncode
            self._final_message = message or (
                "Review complete" if returncode == 0 else f"Review exited with {returncode}"
            )
            self._status_message = self._final_message

    def final_message_hint(self, returncode: int) -> str | None:
        if returncode == 0:
            return None
        with self._lock:
            for row in reversed(self._rows):
                if row.status in {"failed", "warning"} and row.message:
                    return row.message
            return None

    def request_interrupt(self) -> None:
        controller = None
        with self._lock:
            if self._finished:
                return
            self._interrupt_count += 1
            controller = self._interrupt_controller
            if self._interrupt_count == 1:
                self._status_message = (
                    "Interrupt requested; stopping before the next reviewer stream"
                )
            else:
                self._abort_requested = True
                self._status_message = "Abort requested; terminating active Codex work"
        if controller is None:
            return
        if self._interrupt_count == 1:
            controller.request_stop()
        else:
            controller.request_abort()
            controller.terminate_active_process()

    def terminate_active_process(self) -> None:
        with self._lock:
            controller = self._interrupt_controller
            self._abort_requested = True
            self._status_message = "Terminating active Codex work"
        if controller is not None:
            controller.terminate_active_process()

    def snapshot(self) -> TuiSnapshot:
        with self._lock:
            return TuiSnapshot(
                rows=tuple(replace(row, record=row.record.copy() if row.record else None) for row in self._rows),
                headers=self._headers.copy(),
                status_message=self._status_message,
                started_at=self._started_at,
                finished_at=self._finished_at,
                finished=self._finished,
                returncode=self._returncode,
                final_message=self._final_message,
                abort_requested=self._abort_requested,
            )

    def _find_pending_row(
        self, event: str, review_round: int | None, fix_round: int | None
    ) -> TuiRow | None:
        for row in reversed(self._rows):
            if (
                row.status == "running"
                and row.event == event
                and row.review_round == review_round
                and row.fix_round == fix_round
            ):
                return row
        return None

    def _find_related_pending_row(
        self, review_round: int | None, fix_round: int | None
    ) -> TuiRow | None:
        for row in reversed(self._rows):
            if (
                row.status == "running"
                and row.review_round == review_round
                and row.fix_round == fix_round
            ):
                return row
        return None

    def _find_latest_pending_row(self) -> TuiRow | None:
        for row in reversed(self._rows):
            if row.status == "running":
                return row
        return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _event_title(event: str) -> str:
    return event.replace("_", " ")


def _record_status(record: dict[str, Any]) -> str:
    event = str(record.get("event") or "")
    extra = record.get("extra")
    if isinstance(extra, dict) and extra.get("tui_status") == "running":
        return "running"
    if event == "review_finished" and isinstance(extra, dict):
        returncode = extra.get("returncode")
        if returncode == 0:
            return "complete"
        if returncode == 130:
            return "warning"
        if isinstance(returncode, int):
            return "failed"
    if record.get("codex_exit_code") not in (None, 0):
        return "failed"
    if record.get("codex_errors"):
        return "failed"
    if event == "branch_scope_ref_changed":
        return "failed"
    if _is_warning_event(event):
        return "warning"
    if "failed" in event or "failure" in event or "error" in event or "limit" in event:
        return "failed"
    if "interrupted" in event:
        return "warning"
    if event.startswith("complete") or event == "reviewer_satisfied":
        return "complete"
    if event.startswith("oracle_failed") or "warning" in event:
        return "warning"
    return "done"


def _is_warning_event(event: str) -> bool:
    return event in {
        "branch_reflog_guard_unavailable",
        "oracle_failed_open",
        "oracle_parse_failed",
        "oracle_rollback_failed",
        "reviewer_rewrite_without_rejected_failed_open",
    }


def _is_intermediate_event(record: dict[str, Any]) -> bool:
    event = str(record.get("event") or "")
    if event == "oracle_classification":
        return True
    return False


def _pending_event_for(event: str) -> str | None:
    if event == "oracle_classification_result":
        return "oracle_classification"
    return None


def _record_message(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    response = record.get("response")
    if isinstance(response, str) and response.strip():
        return response.strip().splitlines()[0][:160]
    event = record.get("event")
    return _event_title(str(event or "event"))


def _record_monotonic_time(record: dict[str, Any]) -> float | None:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        wall_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return time.monotonic() + (wall_time.timestamp() - time.time())
