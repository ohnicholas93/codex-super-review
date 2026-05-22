from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .app_server import AppServerJsonRpcClient
from .audit import AuditLogger
from .constants import ORACLE_STATUSES
from .errors import CodexAppServerFailure
from .models import OracleClassification
from .prompts import build_oracle_prompt

class CodexOracle:
    def __init__(
        self,
        client: AppServerJsonRpcClient,
        cwd: Path,
        audit: AuditLogger,
        max_retries: int,
    ) -> None:
        self.client = client
        self.cwd = cwd
        self.audit = audit
        self.max_retries = max_retries
        self.thread_id: str | None = None
        self.reset_client_after_failure = False

    def classify(
        self,
        *,
        developer_responses: list[str],
        current_findings: str,
        review_round: int,
    ) -> OracleClassification | None:
        prompt = build_oracle_prompt(developer_responses, current_findings)
        return self._run_structured_turn(
            prompt=prompt,
            output_schema=oracle_output_schema(),
            parser=parse_oracle_classification,
            review_round=review_round,
            audit_event="oracle_classification",
            archive_reason="classification_complete",
        )

    def _run_structured_turn(
        self,
        *,
        prompt: str,
        output_schema: dict[str, Any],
        parser: Callable[[str], Any],
        review_round: int,
        audit_event: str,
        archive_reason: str,
    ) -> Any | None:
        archived_thread_id: str | None = None
        self.reset_client_after_failure = False
        try:
            self.thread_id = self.client.start_thread(cwd=self.cwd)
        except CodexAppServerFailure as exc:
            self.reset_client_after_failure = True
            self.audit.log(
                "oracle_failed_open",
                review_round=review_round,
                message=str(exc),
                extra={"phase": "thread/start"},
            )
            return None

        attempts = self.max_retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                result = self.client.run_turn(
                    self.thread_id,
                    prompt,
                    cwd=self.cwd,
                    output_schema=output_schema,
                )
            except CodexAppServerFailure as exc:
                self.reset_client_after_failure = True
                last_error = str(exc)
                self.audit.log(
                    "oracle_failed_open",
                    review_round=review_round,
                    prompt=prompt,
                    message=last_error,
                    extra={
                        "attempt": attempt,
                        "oracle_thread_id": self.thread_id,
                        "discarded_oracle_thread": True,
                    },
                )
                archived_thread_id = self._archive_current_thread(
                    review_round=review_round,
                    reason="run_turn_failure",
                )
                self.thread_id = None
                return None
            self.audit.log(
                audit_event,
                review_round=review_round,
                prompt=prompt,
                response=result.response,
                extra={
                    "attempt": attempt,
                    "oracle_thread_id": self.thread_id,
                    "oracle_turn_id": result.turn_id,
                    "turn_status": result.turn_status,
                },
            )
            if result.turn_status != "completed":
                last_error = f"oracle turn ended with status {result.turn_status!r}"
                self.audit.log(
                    "oracle_failed_open",
                    review_round=review_round,
                    prompt=prompt,
                    response=result.response,
                    message=last_error,
                    extra={
                        "attempt": attempt,
                        "oracle_thread_id": self.thread_id,
                        "oracle_turn_id": result.turn_id,
                        "turn_status": result.turn_status,
                    },
                )
                try:
                    self.client.rollback_thread(self.thread_id, 1)
                    self.audit.log(
                        "oracle_rollback",
                        review_round=review_round,
                        message="rolled back unsuccessful oracle turn",
                        extra={
                            "attempt": attempt,
                            "oracle_thread_id": self.thread_id,
                            "num_turns": 1,
                            "turn_status": result.turn_status,
                        },
                    )
                except CodexAppServerFailure as rollback_error:
                    self.audit.log(
                        "oracle_rollback_failed",
                        review_round=review_round,
                        message=str(rollback_error),
                        extra={
                            "attempt": attempt,
                            "oracle_thread_id": self.thread_id,
                            "turn_status": result.turn_status,
                        },
                    )
                archived_thread_id = self._archive_current_thread(
                    review_round=review_round,
                    reason="unsuccessful_turn",
                )
                self.thread_id = None
                return None
            try:
                parsed = parser(result.response)
                archived_thread_id = self._archive_current_thread(
                    review_round=review_round,
                    reason=archive_reason,
                )
                self.thread_id = None
                return parsed
            except ValueError as exc:
                last_error = str(exc)
                self.audit.log(
                    "oracle_parse_failed",
                    review_round=review_round,
                    response=result.response,
                    message=last_error,
                    extra={
                        "attempt": attempt,
                        "oracle_thread_id": self.thread_id,
                        "oracle_turn_id": result.turn_id,
                    },
                )
                try:
                    self.client.rollback_thread(self.thread_id, 1)
                    self.audit.log(
                        "oracle_rollback",
                        review_round=review_round,
                        message="rolled back malformed oracle turn",
                        extra={
                            "attempt": attempt,
                            "oracle_thread_id": self.thread_id,
                            "num_turns": 1,
                        },
                    )
                except CodexAppServerFailure as rollback_error:
                    self.reset_client_after_failure = True
                    last_error = f"{last_error}; rollback failed: {rollback_error}"
                    break

        self.audit.log(
            "oracle_failed_open",
            review_round=review_round,
            message=last_error or "oracle failed to produce parseable output",
            extra={
                "oracle_thread_id": self.thread_id,
                "archived_oracle_thread_id": archived_thread_id,
                "attempts": attempts,
            },
        )
        if self.thread_id is not None:
            self._archive_current_thread(
                review_round=review_round,
                reason="classification_failed",
            )
        self.thread_id = None
        return None

    def _archive_current_thread(
        self,
        *,
        review_round: int,
        reason: str,
    ) -> str | None:
        thread_id = self.thread_id
        if thread_id is None:
            return None
        try:
            self.client.archive_thread(thread_id)
            self.audit.log(
                "oracle_thread_archived",
                review_round=review_round,
                message=f"archived oracle thread after {reason}",
                extra={"oracle_thread_id": thread_id, "reason": reason},
            )
            return thread_id
        except CodexAppServerFailure as exc:
            self.audit.log(
                "oracle_thread_archive_failed",
                review_round=review_round,
                message=str(exc),
                extra={"oracle_thread_id": thread_id, "reason": reason},
            )
            return None



def oracle_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": sorted(ORACLE_STATUSES),
            },
            "rejected_findings_explanation": {"type": "string"},
        },
        "required": ["status", "rejected_findings_explanation"],
    }


def parse_oracle_classification(response: str) -> OracleClassification:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"oracle response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("oracle response must be a JSON object")
    status = payload.get("status")
    explanation = payload.get("rejected_findings_explanation")
    if status not in ORACLE_STATUSES:
        raise ValueError(f"oracle status was invalid: {status!r}")
    if not isinstance(explanation, str):
        raise ValueError("oracle rejected_findings_explanation must be a string")
    if status in {"ONLY_REJECTED_FINDINGS", "HAS_REJECTED_AND_NEW_FINDINGS"}:
        if not explanation.strip():
            raise ValueError(
                "oracle rejected_findings_explanation must be non-empty for rejected statuses"
            )
    elif explanation.strip():
        raise ValueError(
            "oracle rejected_findings_explanation must be empty for NO_REJECTED_FINDINGS"
        )
    return OracleClassification(
        status=status,
        rejected_findings_explanation=explanation.strip(),
    )
