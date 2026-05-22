from __future__ import annotations

from .models import CodexResult

class CodexExecutableNotFound(RuntimeError):
    pass


class CodexRunFailure(RuntimeError):
    def __init__(self, phase: str, result: CodexResult) -> None:
        self.phase = phase
        self.result = result
        super().__init__(
            f"codex exec failed during {phase} with exit code {result.returncode}"
        )


class LimitReached(RuntimeError):
    pass


class CodexResultDiagnostics(RuntimeError):
    def __init__(self, phase: str, result: CodexResult) -> None:
        self.phase = phase
        self.result = result
        super().__init__(f"codex exec reported tool or router errors during {phase}")


class CodexAppServerFailure(RuntimeError):
    pass
