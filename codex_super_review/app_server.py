from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .constants import (
    APP_SERVER_COMPACTION_TIMEOUT_SECONDS,
    APP_SERVER_ORACLE_TURN_TIMEOUT_SECONDS,
    APP_SERVER_ROLLBACK_TIMEOUT_SECONDS,
    APP_SERVER_TOKEN_USAGE_TIMEOUT_SECONDS,
)
from .errors import CodexAppServerFailure, CodexExecutableNotFound
from .models import AppServerTurnResult, ModelSpec
from .utils import _get_nested

class AppServerJsonRpcClient:
    def __init__(self, codex_bin: str, cwd: Path, model_spec: ModelSpec) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.model_spec = model_spec
        self._next_request_id = 1
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending_messages: deque[dict[str, Any]] = deque()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    def __enter__(self) -> "AppServerJsonRpcClient":
        command = [
            self.codex_bin,
            "app-server",
            "--listen",
            "stdio://",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise CodexExecutableNotFound(
                f"could not find Codex executable: {self.codex_bin}"
            ) from exc

        assert self._process.stdout is not None
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="codex-app-server-stdout-reader",
            daemon=True,
        )
        self._reader.start()

        assert self._process.stderr is not None
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="codex-app-server-stderr-reader",
            daemon=True,
        )
        self._stderr_reader.start()

        self._initialize()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            self._process = None

    def resume_thread_for_usage(self, thread_id: str) -> dict[str, Any] | None:
        self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "model": self.model_spec.model,
                "cwd": str(self.cwd),
                "config": {"model_reasoning_effort": self.model_spec.reasoning_effort},
            },
            timeout=60,
        )
        deadline = time.monotonic() + APP_SERVER_TOKEN_USAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            message = self.next_message(timeout=max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            if (
                message.get("method") == "thread/tokenUsage/updated"
                and _get_nested(message, "params", "threadId") == thread_id
            ):
                token_usage = _get_nested(message, "params", "tokenUsage")
                if isinstance(token_usage, dict):
                    return token_usage
        return None

    def start_thread(self, *, cwd: Path | None = None) -> str:
        self._clear_stale_messages()
        effective_cwd = cwd or self.cwd
        result = self.request(
            "thread/start",
            {
                "model": self.model_spec.model,
                "cwd": str(effective_cwd),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "config": {"model_reasoning_effort": self.model_spec.reasoning_effort},
            },
            timeout=60,
        )
        thread_id = _get_nested(result, "thread", "id")
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerFailure("thread/start did not return a thread id")
        return thread_id

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: Path | None = None,
        output_schema: dict[str, Any] | None = None,
        timeout: int = APP_SERVER_ORACLE_TURN_TIMEOUT_SECONDS,
    ) -> AppServerTurnResult:
        self._clear_stale_messages()
        effective_cwd = cwd or self.cwd
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(effective_cwd),
            "model": self.model_spec.model,
            "effort": self.model_spec.reasoning_effort,
            "sandboxPolicy": {"type": "readOnly"},
            "approvalPolicy": "never",
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema

        result = self.request("turn/start", params, timeout=60)
        turn_id = _get_nested(result, "turn", "id")
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerFailure("turn/start did not return a turn id")

        items: list[Any] = []
        turn_status: str | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next_message(timeout=max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            if _get_nested(message, "params", "threadId") != thread_id:
                continue
            message_turn_id = _turn_id_from_message(message)

            method = message.get("method")
            if method == "item/completed":
                if message_turn_id != turn_id:
                    continue
                item = _get_nested(message, "params", "item")
                if isinstance(item, dict):
                    items.append(item)
            elif method == "turn/completed":
                turn = _get_nested(message, "params", "turn")
                if isinstance(turn, dict):
                    completed_turn_id = turn.get("id")
                    if completed_turn_id != turn_id:
                        continue
                    status = turn.get("status")
                    if isinstance(status, str):
                        turn_status = status
                    completed_items = turn.get("items")
                    if isinstance(completed_items, list):
                        items = _merge_app_server_items(items, completed_items)
                return AppServerTurnResult(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    response=_final_assistant_response_from_items(items),
                    turn_status=turn_status,
                )

        raise CodexAppServerFailure(
            f"timed out waiting for app-server turn {turn_id} to complete"
        )

    def rollback_thread(self, thread_id: str, num_turns: int = 1) -> None:
        self._clear_stale_messages()
        self.request(
            "thread/rollback",
            {"threadId": thread_id, "numTurns": num_turns},
            timeout=APP_SERVER_ROLLBACK_TIMEOUT_SECONDS,
        )

    def archive_thread(self, thread_id: str) -> None:
        self._clear_stale_messages()
        self.request("thread/archive", {"threadId": thread_id}, timeout=60)

    def compact_thread(self, thread_id: str) -> None:
        self._clear_stale_messages()
        self.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
        compaction_turn_id: str | None = None
        saw_compaction_item = False
        deadline = time.monotonic() + APP_SERVER_COMPACTION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            message = self.next_message(timeout=max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            if _get_nested(message, "params", "threadId") != thread_id:
                continue
            method = message.get("method")
            item_type = _get_nested(message, "params", "item", "type")
            if (
                method in {"item/started", "item/completed"}
                and item_type == "contextCompaction"
            ):
                saw_compaction_item = True
                turn_id = _get_nested(message, "params", "turnId")
                if isinstance(turn_id, str):
                    compaction_turn_id = turn_id
            if method == "turn/completed":
                turn = _get_nested(message, "params", "turn")
                if not isinstance(turn, dict):
                    continue
                turn_id = turn.get("id")
                if compaction_turn_id is not None and turn_id != compaction_turn_id:
                    continue
                if saw_compaction_item:
                    status = turn.get("status")
                    if status != "completed":
                        raise CodexAppServerFailure(
                            f"compaction turn ended with status {status!r}"
                        )
                    return
        raise CodexAppServerFailure(
            "timed out waiting for app-server compaction to complete"
        )

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        deadline = time.monotonic() + timeout
        ignored_messages: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            message = self._read_queue_message(
                timeout=max(0.1, deadline - time.monotonic())
            )
            if message is None:
                break
            if message.get("id") != request_id:
                ignored_messages.append(message)
                continue
            if "error" in message:
                self._pending_messages.extendleft(reversed(ignored_messages))
                error = message["error"]
                if isinstance(error, dict):
                    raise CodexAppServerFailure(
                        f"{method} failed: {error.get('message', error)}"
                    )
                raise CodexAppServerFailure(f"{method} failed: {error}")
            self._pending_messages.extendleft(reversed(ignored_messages))
            return message.get("result")
        self._pending_messages.extendleft(reversed(ignored_messages))
        raise CodexAppServerFailure(
            f"timed out waiting for app-server response to {method}"
        )

    def next_message(self, *, timeout: float) -> dict[str, Any] | None:
        if self._pending_messages:
            return self._pending_messages.popleft()
        return self._read_queue_message(timeout=timeout)

    def _clear_stale_messages(self) -> None:
        self._pending_messages.clear()
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _read_queue_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            if self._process is not None and self._process.poll() is not None:
                raise CodexAppServerFailure(
                    f"app-server exited unexpectedly with code {self._process.returncode}"
                )
            return None

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_super_review",
                    "title": "codex-super-review",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self._send({"method": "initialized"})

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerFailure("app-server process is not running")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise CodexAppServerFailure(
                f"failed to write to app-server stdin: {exc}"
            ) from exc

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._events.put(message)

    def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for _line in self._process.stderr:
            pass



def _thread_item_type(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if isinstance(item_type, str):
        return item_type
    # Older enum serializations can use variant names as object keys.
    if "agentMessage" in item:
        return "agentMessage"
    if "AgentMessage" in item:
        return "agentMessage"
    return ""


def _agent_message_payload(item: dict[str, Any]) -> dict[str, Any]:
    if _thread_item_type(item) != "agentMessage":
        return {}
    for key in ("agentMessage", "AgentMessage"):
        payload = item.get(key)
        if isinstance(payload, dict):
            return payload
    return item


def _agent_message_text(agent_message: dict[str, Any]) -> str | None:
    text = agent_message.get("text")
    if isinstance(text, str):
        return text
    for key in (
        "structuredContent",
        "structured_content",
        "structuredOutput",
        "structured_output",
    ):
        value = agent_message.get(key)
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
    return None


def _turn_id_from_message(message: dict[str, Any]) -> str | None:
    turn_id = _get_nested(message, "params", "turnId")
    if isinstance(turn_id, str):
        return turn_id
    turn_id = _get_nested(message, "params", "turn", "id")
    if isinstance(turn_id, str):
        return turn_id
    return None


def _merge_app_server_items(
    streamed_items: list[Any], completed_items: list[Any]
) -> list[Any]:
    if not streamed_items:
        return completed_items
    if not completed_items:
        return streamed_items

    merged = list(streamed_items)
    id_to_index = {
        item.get("id"): index
        for index, item in enumerate(streamed_items)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for item in completed_items:
        if isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str):
                existing_index = id_to_index.get(item_id)
                if existing_index is not None:
                    merged[existing_index] = item
                    continue
                id_to_index[item_id] = len(merged)
                merged.append(item)
                continue
        if item not in merged:
            merged.append(item)
    return merged


def _is_final_answer_phase(phase: Any) -> bool:
    if not isinstance(phase, str):
        return False
    return phase in {"final_answer", "finalAnswer", "FinalAnswer"}


def _final_assistant_response_from_items(items: list[Any]) -> str:
    last_unknown_phase_response: str | None = None

    for item in reversed(items):
        if not isinstance(item, dict) or _thread_item_type(item) != "agentMessage":
            continue
        agent_message = _agent_message_payload(item)
        text = _agent_message_text(agent_message)
        if text is None:
            continue
        phase = agent_message.get("phase")
        if _is_final_answer_phase(phase):
            return text.strip()
        if phase is None and last_unknown_phase_response is None:
            last_unknown_phase_response = text

    return (last_unknown_phase_response or "").strip()
