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
        latest_developer_response: str,
        current_findings: str,
        review_round: int,
    ) -> OracleClassification | None:
        prompt = build_oracle_prompt(latest_developer_response, current_findings)
        return self._run_structured_turn(
            prompt=prompt,
            output_schema=oracle_output_schema(),
            parser=parse_oracle_classification,
            review_round=review_round,
            audit_event="oracle_classification",
        )

    def _run_structured_turn(
        self,
        *,
        prompt: str,
        output_schema: dict[str, Any],
        parser: Callable[[str], Any],
        review_round: int,
        audit_event: str,
    ) -> Any | None:
        self.reset_client_after_failure = False
        if not self._ensure_thread(review_round=review_round):
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
                    self.reset_client_after_failure = True
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
                    self.thread_id = None
                return None
            try:
                parsed = parser(result.response)
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
                    self.thread_id = None
                    break

        self.audit.log(
            "oracle_failed_open",
            review_round=review_round,
            message=last_error or "oracle failed to produce parseable output",
            extra={
                "oracle_thread_id": self.thread_id,
                "attempts": attempts,
            },
        )
        return None

    def _ensure_thread(self, *, review_round: int) -> bool:
        if self.thread_id is not None:
            return True
        try:
            self.thread_id = self.client.start_thread(cwd=self.cwd)
            self.audit.log(
                "oracle_thread_started",
                review_round=review_round,
                extra={"oracle_thread_id": self.thread_id},
            )
        except CodexAppServerFailure as exc:
            self.reset_client_after_failure = True
            self.audit.log(
                "oracle_failed_open",
                review_round=review_round,
                message=str(exc),
                extra={"phase": "thread/start"},
            )
            return False
        return True



def oracle_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "explanation": {"type": "string"},
            "status": {
                "type": "string",
                "enum": sorted(ORACLE_STATUSES),
            },
        },
        "required": ["explanation", "status"],
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
    explanation = payload.get("explanation")
    if not isinstance(explanation, str):
        raise ValueError("oracle explanation must be a string")
    status = payload.get("status")
    if status not in ORACLE_STATUSES:
        raise ValueError(f"oracle status was invalid: {status!r}")
    if not explanation.strip():
        raise ValueError("oracle explanation must be non-empty")
    keys = list(payload.keys())
    if (
        "explanation" in keys
        and "status" in keys
        and keys.index("explanation") > keys.index("status")
    ):
        raise ValueError("oracle explanation must come before status")
    return OracleClassification(
        explanation=explanation.strip(),
        status=status,
    )
