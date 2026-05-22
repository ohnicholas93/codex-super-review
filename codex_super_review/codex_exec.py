from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from .constants import IMPLEMENTER_APPROVALS_REVIEWER, IMPLEMENTER_APPROVAL_POLICY
from .errors import CodexExecutableNotFound
from .models import CodexResult, ModelSpec
from .utils import _stringify_error

class CodexExecRunner:
    def __init__(self, codex_bin: str, cwd: Path) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd

    def run(
        self,
        prompt: str,
        *,
        sandbox: str,
        phase: str,
        model_spec: ModelSpec,
        resume_thread_id: str | None = None,
        approval_never: bool = False,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
    ) -> CodexResult:
        with tempfile.NamedTemporaryFile(
            prefix="codex-super-review-", suffix=".txt", delete=False
        ) as tmp:
            last_message_path = Path(tmp.name)

        command = [
            self.codex_bin,
            "exec",
            "--json",
            "--model",
            model_spec.model,
            "--config",
            f'model_reasoning_effort="{model_spec.reasoning_effort}"',
            "--sandbox",
            sandbox,
            "--cd",
            str(self.cwd),
            "--output-last-message",
            str(last_message_path),
        ]
        if approval_never:
            command.extend(["--config", 'approval_policy="never"'])
        if approval_policy is not None:
            command.extend(["--config", f'approval_policy="{approval_policy}"'])
        if approvals_reviewer is not None:
            command.extend(["--config", f'approvals_reviewer="{approvals_reviewer}"'])
        if resume_thread_id is not None:
            command.extend(["resume", resume_thread_id])

        thread_id = resume_thread_id
        usage: dict[str, Any] | None = None
        event_types: list[str] = []
        errors: list[str] = []
        diagnostics: list[str] = []

        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            self._remove_temp_file(last_message_path)
            raise CodexExecutableNotFound(
                f"could not find Codex executable: {self.codex_bin}"
            ) from exc

        stdin_errors: list[str] = []

        def write_prompt() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(prompt)
            except BrokenPipeError:
                stdin_errors.append(
                    "codex process closed stdin before reading the full prompt"
                )
            except OSError as exc:
                stdin_errors.append(f"failed to write prompt to codex stdin: {exc}")
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=write_prompt, name="codex-stdin-writer")
        writer.start()

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostics.append(line)
                print(f"[{phase}] {line}", file=sys.stderr)
                continue
            if not isinstance(event, dict):
                diagnostic = f"unexpected non-object JSON event: {line}"
                diagnostics.append(diagnostic)
                print(f"[{phase}] {diagnostic}", file=sys.stderr)
                continue

            event_type = event.get("type")
            if isinstance(event_type, str):
                event_types.append(event_type)

            if event_type == "thread.started" and isinstance(
                event.get("thread_id"), str
            ):
                thread_id = event["thread_id"]
            elif event_type == "turn.completed" and isinstance(
                event.get("usage"), dict
            ):
                usage = event["usage"]
            elif event_type == "turn.failed":
                errors.append(_stringify_error(event.get("error")))
            elif event_type == "error":
                errors.append(str(event.get("message", event)))

        writer.join()
        returncode = process.wait()
        diagnostics.extend(stdin_errors)
        response = ""
        try:
            response = last_message_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            diagnostics.append(
                f"last message file was not written: {last_message_path}"
            )
        finally:
            self._remove_temp_file(last_message_path)

        return CodexResult(
            command=command,
            returncode=returncode,
            response=response,
            thread_id=thread_id,
            usage=usage,
            event_types=event_types,
            errors=errors,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _remove_temp_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass



class CodexReviewer:
    def __init__(self, runner: CodexExecRunner, model_spec: ModelSpec) -> None:
        self.runner = runner
        self.model_spec = model_spec
        self.thread_id: str | None = None

    def review(self, prompt: str) -> CodexResult:
        result = self.runner.run(
            prompt,
            sandbox="read-only",
            phase="reviewer",
            model_spec=self.model_spec,
            approval_never=True,
        )
        if result.thread_id is not None:
            self.thread_id = result.thread_id
        return result

    def reverify(self, prompt: str) -> CodexResult:
        if self.thread_id is None:
            raise RuntimeError("cannot reverify before starting reviewer thread")
        result = self.runner.run(
            prompt,
            sandbox="read-only",
            phase="reviewer-reverify",
            model_spec=self.model_spec,
            resume_thread_id=self.thread_id,
            approval_never=True,
        )
        return result


class CodexImplementer:
    def __init__(
        self, runner: CodexExecRunner, thread_id: str, model_spec: ModelSpec
    ) -> None:
        self.runner = runner
        self.thread_id = thread_id
        self.model_spec = model_spec

    def fix(self, prompt: str) -> CodexResult:
        result = self.runner.run(
            prompt,
            sandbox="workspace-write",
            phase="implementer",
            model_spec=self.model_spec,
            resume_thread_id=self.thread_id,
            approval_policy=IMPLEMENTER_APPROVAL_POLICY,
            approvals_reviewer=IMPLEMENTER_APPROVALS_REVIEWER,
        )
        return result


