from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import FrameType
from typing import Any, Iterator


class GracefulInterruptController:
    def __init__(self) -> None:
        self.stop_requested = False
        self.abort_requested = False
        self._previous_handlers: dict[int, signal.Handlers] = {}
        self._active_process: subprocess.Popen[str] | None = None

    def __enter__(self) -> GracefulInterruptController:
        if threading.current_thread() is threading.main_thread():
            for signum in self._handled_signals():
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
        atexit.register(self.terminate_active_process)
        return self

    def __exit__(self, *args: object) -> None:
        atexit.unregister(self.terminate_active_process)
        for signum, previous_handler in self._previous_handlers.items():
            signal.signal(signum, previous_handler)

    @contextmanager
    def track_process(self, process: subprocess.Popen[str]) -> Iterator[None]:
        previous_process = self._active_process
        self._active_process = process
        try:
            self.raise_if_abort_requested()
            yield
        finally:
            if self.abort_requested:
                self.terminate_process(process)
            self._active_process = previous_process

    def should_stop_before_next_reviewer(self) -> bool:
        return self.stop_requested

    def request_stop(self) -> bool:
        if self.stop_requested:
            return False
        self.stop_requested = True
        return True

    def request_abort(self) -> None:
        self.stop_requested = True
        self.abort_requested = True
        self._signal_active_process(signal.SIGTERM)

    def raise_if_abort_requested(self) -> None:
        if self.abort_requested:
            raise KeyboardInterrupt

    def subprocess_kwargs(self) -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            self._kill_process_group(process)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    def terminate_active_process(self) -> None:
        process = self._active_process
        if process is not None:
            self.terminate_process(process)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        if signum != signal.SIGINT:
            self.terminate_active_process()
            raise SystemExit(128 + signum)

        if self.request_stop():
            print(
                "\nInterrupt requested; stopping before the next reviewer stream. Press Ctrl+C again to abort immediately.",
                file=sys.stderr,
            )
            return

        self.request_abort()
        raise KeyboardInterrupt

    def _signal_active_process(self, signum: int) -> None:
        process = self._active_process
        if process is None or process.poll() is not None:
            return
        self._signal_process_group(process, signum)

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signum)
        except (OSError, ProcessLookupError):
            return

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    @staticmethod
    def _handled_signals() -> tuple[int, ...]:
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        return tuple(signals)
