from __future__ import annotations

import curses
import json
import os
import signal
import textwrap
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .event_sink import TuiEventSink, TuiRow, TuiSnapshot, _record_monotonic_time

SPINNER = "|/-\\"


class CursesUnavailable(RuntimeError):
    pass


@dataclass
class AuditHeaderState:
    implementer_thread_id: str = ""


def run_tui(
    args: Any,
    run_func: Callable[[Any, Any], int],
) -> int:
    sink = TuiEventSink()
    args.event_sink = sink
    result: dict[str, int | None] = {"returncode": None}
    stop_tail = threading.Event()
    tail_thread = threading.Thread(
        target=_tail_active_audit_log,
        args=(sink, stop_tail),
        name="codex-super-review-audit-tail",
        daemon=True,
    )
    tail_thread.start()

    def worker() -> None:
        try:
            code = run_func(args, sink)
        except KeyboardInterrupt:
            code = 130
            sink.audit(
                {
                    "event": "interrupted",
                    "message": "Interrupted",
                    "review_round": None,
                    "fix_round": None,
                }
            )
        except BaseException:
            code = 1
            sink.start("internal_error", message="internal error")
            sink.audit(
                {
                    "event": "internal_error",
                    "message": traceback.format_exc(),
                    "review_round": None,
                    "fix_round": None,
                }
            )
        result["returncode"] = code
        sink.finish(code, sink.final_message_hint(code))

    worker_started = {"value": False}
    try:
        curses.wrapper(_curses_main, sink, worker, worker_started)
    except curses.error as exc:
        if not worker_started["value"]:
            raise CursesUnavailable(str(exc)) from exc
        raise
    finally:
        stop_tail.set()
        tail_thread.join(timeout=1.0)
    if sink.snapshot().abort_requested:
        return 130
    return result["returncode"] if result["returncode"] is not None else 130


def run_attach_tui(audit_path: Path) -> int:
    audit_path = audit_path.expanduser()
    if not audit_path.is_file():
        raise OSError(f"{audit_path}: no such file")
    _validate_attach_audit_log(audit_path)
    sink = TuiEventSink()
    sink.header("Audit log", str(audit_path))
    sink.status(f"Attached to audit log: {audit_path}")
    stop_tail = threading.Event()
    tail_thread = threading.Thread(
        target=_tail_audit_log,
        args=(sink, audit_path, stop_tail),
        name="codex-super-review-audit-tail",
        daemon=True,
    )
    tail_thread.start()

    worker_started = {"value": False}

    def worker() -> None:
        return

    try:
        curses.wrapper(
            _curses_main,
            sink,
            worker,
            worker_started,
            True,
            False,
        )
    except curses.error as exc:
        raise CursesUnavailable(str(exc)) from exc
    finally:
        stop_tail.set()
        tail_thread.join(timeout=1.0)
    returncode = sink.snapshot().returncode
    return returncode if returncode is not None else 0


def _tail_active_audit_log(sink: TuiEventSink, stop_event: threading.Event) -> None:
    try:
        while not stop_event.is_set():
            value = sink.header_value("Audit log")
            if value:
                _tail_audit_log(sink, Path(value).expanduser(), stop_event)
                return
            time.sleep(0.05)
    except (OSError, ValueError) as exc:
        sink.status(f"warning: audit log tail stopped: {exc}")


def _tail_audit_log(
    sink: TuiEventSink,
    audit_path: Path,
    stop_event: threading.Event,
) -> None:
    try:
        header_state = AuditHeaderState()
        with audit_path.open("rb") as handle:
            line_number = 0
            while not stop_event.is_set():
                position = handle.tell()
                line = handle.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                next_line_number = line_number + 1
                try:
                    record = _parse_audit_line(audit_path, next_line_number, line)
                except ValueError as exc:
                    if not line.endswith(b"\n"):
                        handle.seek(position)
                        time.sleep(0.1)
                        continue
                    sink.status(f"warning: skipped audit log line: {exc}")
                    line_number = next_line_number
                    continue
                line_number = next_line_number
                if record is None:
                    continue
                _update_audit_headers(sink, audit_path, header_state, record)
                sink.apply_audit_record(record)
                terminal = _terminal_record(record)
                if terminal is not None:
                    returncode, message = terminal
                    sink.finish(
                        returncode,
                        message,
                        finished_at=_record_monotonic_time(record),
                    )
    except (OSError, ValueError) as exc:
        sink.status(f"warning: audit log tail stopped: {exc}")


def _validate_attach_audit_log(audit_path: Path) -> None:
    deadline = time.monotonic() + 1.0
    last_error: ValueError | None = None
    while True:
        try:
            _validate_complete_audit_lines(audit_path)
            return
        except ValueError as exc:
            if not _is_trailing_partial_json_error(audit_path, exc):
                raise
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(0.05)


def _validate_complete_audit_lines(audit_path: Path) -> None:
    with audit_path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                _parse_audit_line(audit_path, line_number, line)
            except ValueError as exc:
                if not line.endswith(b"\n"):
                    raise ValueError(f"trailing partial JSON: {exc}") from exc


def _is_trailing_partial_json_error(audit_path: Path, exc: ValueError) -> bool:
    if not str(exc).startswith("trailing partial JSON: "):
        return False
    try:
        with audit_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:
        return False


def _parse_audit_line(
    audit_path: Path,
    line_number: int,
    line: bytes | str,
) -> dict[str, Any] | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{audit_path}:{line_number}: {exc}") from exc
    stripped = line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{audit_path}:{line_number}: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{audit_path}:{line_number}: expected JSON object")
    return record


def _update_audit_headers(
    sink: TuiEventSink,
    audit_path: Path,
    state: AuditHeaderState,
    record: dict[str, Any],
) -> None:
    sink.header("Audit log", str(audit_path))
    cwd = record.get("cwd")
    if isinstance(cwd, str) and cwd:
        sink.header("Review root", _home_relative(cwd))
    if not state.implementer_thread_id:
        implementer_thread_id = _record_string(record, "implementer_thread_id")
        if implementer_thread_id:
            state.implementer_thread_id = implementer_thread_id
            sink.header("Implementer", implementer_thread_id)
    model_summary = _audit_model_summary(record)
    if model_summary:
        sink.header("Models I/R/O", model_summary)


def _audit_model_summary(record: dict[str, Any]) -> str:
    implementer = _audit_model_display(record, "implementer")
    reviewer = _audit_model_display(record, "reviewer")
    oracle = _audit_model_display(record, "oracle")
    if not any((implementer, reviewer, oracle)):
        return ""
    return f"{implementer or '-'} / {reviewer or '-'} / {oracle or '-'}"


def _audit_model_display(record: dict[str, Any], role: str) -> str:
    model = record.get(f"{role}_model")
    effort = record.get(f"{role}_reasoning_effort")
    if not isinstance(model, str) or not model:
        return ""
    if isinstance(effort, str) and effort:
        return f"{model} {effort}"
    return model


def _record_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if isinstance(value, str) and value:
        return value
    return ""


def _terminal_record(record: dict[str, Any]) -> tuple[int, str | None] | None:
    extra = record.get("extra")
    if not isinstance(extra, dict) or extra.get("tui_terminal") is not True:
        return None
    returncode = extra.get("returncode")
    if not isinstance(returncode, int):
        return None
    final_message = extra.get("final_message")
    if isinstance(final_message, str) and final_message:
        return returncode, final_message
    message = record.get("message")
    if isinstance(message, str) and message:
        return returncode, message
    return returncode, None


def _curses_main(
    stdscr: Any,
    sink: TuiEventSink,
    worker: Callable[[], None],
    worker_started: dict[str, bool],
    ctrl_c_quits: bool = False,
    terminate_on_exit: bool = True,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_CYAN, -1)
        except curses.error:
            pass
    stdscr.keypad(True)
    stdscr.nodelay(True)

    previous_handlers = _install_signal_handlers(sink, ctrl_c_quits)
    thread = threading.Thread(
        target=worker,
        name="codex-super-review-worker",
        daemon=True,
    )
    try:
        thread.start()
        worker_started["value"] = True

        selected = 0
        row_scroll = 0
        detail_scroll = 0
        detail_row_id: int | None = None
        view = "list"
        tick = 0

        while True:
            try:
                snapshot = sink.snapshot()
                rows = snapshot.rows
                if rows:
                    selected = max(0, min(selected, len(rows) - 1))
                else:
                    selected = 0
                if view == "detail" and detail_row_id is not None:
                    if not any(row.id == detail_row_id for row in rows):
                        view = "list"
                        detail_row_id = None

                _draw(
                    stdscr,
                    snapshot,
                    selected,
                    row_scroll,
                    view,
                    detail_row_id,
                    detail_scroll,
                    tick,
                    ctrl_c_quits,
                )
                tick += 1

                keys = _pending_keys(stdscr)
                should_break = False
                for key in keys:
                    if key == 3:
                        if ctrl_c_quits or snapshot.finished:
                            should_break = True
                            break
                        sink.request_interrupt()
                    elif view == "detail":
                        height, width = stdscr.getmaxyx()
                        content_start_y = _content_start_y(snapshot, view, height)
                        previous_view = view
                        detail_scroll = _handle_detail_key(
                            key,
                            detail_scroll,
                            snapshot,
                            detail_row_id,
                            width,
                            height,
                            content_start_y,
                        )
                        if key in (27, curses.KEY_BACKSPACE, 8, 127):
                            view = "list"
                            detail_row_id = None
                        if view != previous_view:
                            break
                    else:
                        previous_view = view
                        previous_detail_row_id = detail_row_id
                        selected, row_scroll, view, detail_row_id = _handle_list_key(
                            key, selected, row_scroll, rows
                        )
                        if (
                            view != previous_view
                            or detail_row_id != previous_detail_row_id
                        ):
                            detail_scroll = 0
                            break
                if should_break:
                    break

                if not thread.is_alive() and snapshot.finished:
                    time.sleep(0.08)
                elif keys:
                    time.sleep(0.01)
                else:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                snapshot = sink.snapshot()
                if ctrl_c_quits or snapshot.finished:
                    break
                sink.request_interrupt()
    finally:
        if terminate_on_exit and not sink.snapshot().finished:
            sink.terminate_active_process()
        _restore_signal_handlers(previous_handlers)
        thread.join(timeout=3.0)


def _pending_keys(stdscr: Any) -> list[int]:
    keys: list[int] = []
    for _ in range(128):
        key = stdscr.getch()
        if key == -1:
            break
        keys.append(key)
        if key == 3:
            break
    return keys


def _install_signal_handlers(
    sink: TuiEventSink,
    ctrl_c_quits: bool = False,
) -> dict[int, signal.Handlers]:
    previous_handlers: dict[int, signal.Handlers] = {}

    def handle_signal(signum: int, frame: object) -> None:
        if signum == signal.SIGINT:
            if ctrl_c_quits or sink.snapshot().finished:
                raise KeyboardInterrupt
            sink.request_interrupt()
            return
        sink.terminate_active_process()
        raise SystemExit(128 + signum)

    for signum in _handled_signals():
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_signal)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[int, signal.Handlers]) -> None:
    for signum, previous_handler in previous_handlers.items():
        signal.signal(signum, previous_handler)


def _handled_signals() -> tuple[int, ...]:
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    return tuple(signals)


def _handle_list_key(
    key: int,
    selected: int,
    row_scroll: int,
    rows: tuple[TuiRow, ...],
) -> tuple[int, int, str, int | None]:
    if key in (curses.KEY_UP, ord("k")):
        selected -= 1
    elif key in (curses.KEY_DOWN, ord("j")):
        selected += 1
    elif key == curses.KEY_HOME:
        selected = 0
    elif key == curses.KEY_END:
        selected = len(rows) - 1
    elif key == curses.KEY_PPAGE:
        selected -= 10
    elif key == curses.KEY_NPAGE:
        selected += 10
    elif key in (10, 13) and rows:
        return selected, row_scroll, "detail", rows[selected].id

    if rows:
        selected = max(0, min(selected, len(rows) - 1))
    else:
        selected = 0
    return selected, row_scroll, "list", None


def _handle_detail_key(
    key: int,
    detail_scroll: int,
    snapshot: TuiSnapshot,
    detail_row_id: int | None,
    width: int,
    height: int,
    start_y: int,
) -> int:
    if key in (curses.KEY_UP, ord("k")):
        detail_scroll -= 1
    elif key in (curses.KEY_DOWN, ord("j")):
        detail_scroll += 1
    elif key == curses.KEY_HOME:
        detail_scroll = 0
    elif key == curses.KEY_PPAGE:
        detail_scroll -= 10
    elif key == curses.KEY_NPAGE:
        detail_scroll += 10
    elif key == curses.KEY_END:
        detail_scroll = len(_detail_lines(snapshot, detail_row_id, width))
    return _clamp_detail_scroll(
        detail_scroll,
        snapshot,
        detail_row_id,
        width,
        height,
        start_y,
    )


def _draw(
    stdscr: Any,
    snapshot: TuiSnapshot,
    selected: int,
    row_scroll: int,
    view: str,
    detail_row_id: int | None,
    detail_scroll: int,
    tick: int,
    ctrl_c_quits: bool = False,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    min_height = 11 if view == "detail" else 9
    if height < min_height or width < 40:
        _add(stdscr, 0, 0, "codex-super-review: terminal is too small")
        stdscr.refresh()
        return

    spinner = " " if snapshot.finished else SPINNER[tick % len(SPINNER)]
    code = "" if snapshot.returncode is None else f" exit={snapshot.returncode}"
    title = f" codex-super-review {spinner}{code} "
    timer = _timer_text(snapshot, view, detail_row_id)
    title_line = _right_aligned_line(title, timer, width)
    _add(stdscr, 0, 0, title_line, _color(1))

    y = 2
    _add(stdscr, y, 0, _clip(snapshot.status_message, width), _color(5))
    y += 2

    for line in _header_lines(snapshot.headers, height, view):
        _add(stdscr, y, 0, _clip(line, width))
        y += 1

    if ctrl_c_quits and not snapshot.finished:
        _add(
            stdscr,
            y,
            0,
            "Attached - press Ctrl+C to quit",
            _color(5) | curses.A_BOLD,
        )
    elif snapshot.finished:
        _add(
            stdscr,
            y,
            0,
            _clip((snapshot.final_message or "Done") + " - press Ctrl+C to quit", width),
            _color(2 if snapshot.returncode == 0 else 4) | curses.A_BOLD,
        )
    else:
        _add(
            stdscr,
            y,
            0,
            "Ctrl+C: graceful stop; Ctrl+C again: abort",
            _color(5) | curses.A_BOLD,
        )
    y += 1
    if view == "detail":
        _add(
            stdscr,
            y,
            0,
            "Detail - Esc/Backspace: back",
            _color(5) | curses.A_BOLD,
        )

    content_start_y = _content_start_y(snapshot, view, height)

    if view == "detail":
        _draw_detail(
            stdscr,
            snapshot,
            detail_row_id,
            detail_scroll,
            width,
            height,
            content_start_y,
        )
    else:
        _draw_list(
            stdscr,
            snapshot,
            selected,
            row_scroll,
            width,
            height,
            content_start_y,
        )
    stdscr.refresh()


def _draw_list(
    stdscr: Any,
    snapshot: TuiSnapshot,
    selected: int,
    row_scroll: int,
    width: int,
    height: int,
    header_y: int,
) -> None:
    _add(
        stdscr,
        header_y,
        0,
        _clip("Status   Event                          Rev Fix  Elapsed  Summary", width),
        curses.A_BOLD,
    )
    rows = snapshot.rows
    visible = max(1, height - header_y - 2)
    if selected < row_scroll:
        row_scroll = selected
    if selected >= row_scroll + visible:
        row_scroll = selected - visible + 1
    for screen_index, row in enumerate(rows[row_scroll : row_scroll + visible]):
        y = header_y + 1 + screen_index
        absolute_index = row_scroll + screen_index
        attr = _row_attr(row)
        if absolute_index == selected:
            attr |= curses.A_REVERSE
        elapsed = _elapsed(row)
        text = (
            f"{row.status[:8]:8} "
            f"{row.event[:30]:30} "
            f"{_num(row.review_round):>3} "
            f"{_num(row.fix_round):>3} "
            f"{elapsed:>7}  "
            f"{row.message}"
        )
        _add(stdscr, y, 0, _clip(text, width), attr)
    footer = f"{len(rows)} rows - Enter: details - Up/Down or j/k: move"
    _add(stdscr, height - 1, 0, _clip(footer, width), curses.A_DIM)


def _draw_detail(
    stdscr: Any,
    snapshot: TuiSnapshot,
    detail_row_id: int | None,
    detail_scroll: int,
    width: int,
    height: int,
    start_y: int,
) -> None:
    lines = _detail_lines(snapshot, detail_row_id, width)
    visible = max(1, height - start_y - 1)
    max_scroll = max(0, len(lines) - visible)
    detail_scroll = min(detail_scroll, max_scroll)
    for index, line in enumerate(lines[detail_scroll : detail_scroll + visible]):
        _add(stdscr, start_y + index, 0, _clip(line, width))
    footer = f"line {min(detail_scroll + 1, len(lines) or 1)}/{max(len(lines), 1)}"
    _add(stdscr, height - 1, 0, _clip(footer, width), curses.A_DIM)


def _detail_lines(
    snapshot: TuiSnapshot,
    detail_row_id: int | None,
    width: int,
) -> list[str]:
    row_index = next(
        (index for index, item in enumerate(snapshot.rows) if item.id == detail_row_id),
        None,
    )
    if row_index is None:
        return ["No row selected"]
    row = snapshot.rows[row_index]
    record = row.record or {}
    lines = [
        "",
        "Summary",
        "-" * min(24, max(8, width - 1)),
        f"event: {row.event}",
        f"status: {row.status}",
        f"review_round: {_num(row.review_round)}",
        f"fix_round: {_num(row.fix_round)}",
        f"summary: {row.message}",
    ]
    metadata: list[str] = []
    for key in (
        "timestamp",
        "implementer_thread_id",
        "reviewer_thread_id",
        "codex_exit_code",
        "usage",
        "codex_errors",
        "diagnostics",
        "extra",
        "message",
    ):
        if key in record and record[key] not in (None, [], {}):
            metadata.extend(_wrapped_field(key, record[key], width))
    if metadata:
        lines.append("")
        lines.extend(_section_lines("Metadata", width))
        lines.extend(metadata)
    for key in ("response", "prompt"):
        value = record.get(key)
        if isinstance(value, str) and value:
            lines.append("")
            lines.extend(_section_lines(key.title(), width))
            lines.extend(_wrap_text(value, width))
    return lines


def _section_lines(title: str, width: int) -> list[str]:
    return [title, "-" * min(24, max(8, width - 1))]


def _wrapped_field(key: str, value: Any, width: int) -> list[str]:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    return _wrap_text(f"{key}: {text}", width)


def _wrap_text(text: str, width: int) -> list[str]:
    lines: list[str] = []
    wrap_width = max(20, width - 1)
    for raw_line in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(raw_line, width=wrap_width) or [""])
    return lines


def _content_start_y(snapshot: TuiSnapshot, view: str, height: int) -> int:
    y = 4
    y += len(_header_lines(snapshot.headers, height, view))
    y += 1
    if view == "detail":
        y += 1
        return y
    return y + 1


def _clamp_detail_scroll(
    detail_scroll: int,
    snapshot: TuiSnapshot,
    detail_row_id: int | None,
    width: int,
    height: int,
    start_y: int,
) -> int:
    lines = _detail_lines(snapshot, detail_row_id, width)
    visible = max(1, height - start_y - 1)
    max_scroll = max(0, len(lines) - visible)
    return max(0, min(detail_scroll, max_scroll))


def _header_lines(
    headers: dict[str, str],
    height: int | None = None,
    view: str = "list",
) -> list[str]:
    if not headers:
        return []
    preferred = [
        "Audit log",
        "Implementer",
        "Models I/R/O",
        "Review scope",
        "Oracle workspace",
    ]
    lines = [
        f"{key}: {_header_value(key, headers[key])}"
        for key in preferred
        if key in headers
    ]
    lines.extend(
        f"{key}: {_header_value(key, value)}"
        for key, value in headers.items()
        if key not in preferred
    )
    if height is None:
        return lines
    max_lines = _max_header_lines(height, view)
    if len(lines) <= max_lines:
        return lines
    if max_lines <= 0:
        return []
    if max_lines == 1:
        return [f"... {len(lines)} header lines"]
    hidden = len(lines) - max_lines + 1
    return [*lines[: max_lines - 1], f"... {hidden} more header lines"]


def _max_header_lines(height: int, view: str) -> int:
    if view == "detail":
        return max(0, height - 11)
    return max(0, height - 9)


def _header_value(key: str, value: str) -> str:
    if key == "Audit log":
        marker = "/codex-super-review/audit/"
        if marker in value:
            return value.rsplit(marker, 1)[1]
    if key == "Oracle workspace":
        return _home_relative(value)
    return value


def _home_relative(value: str) -> str:
    home = os.path.expanduser("~")
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return value


def _timer_text(
    snapshot: TuiSnapshot,
    view: str,
    detail_row_id: int | None,
) -> str:
    total_end = snapshot.finished_at if snapshot.finished_at is not None else time.monotonic()
    total = _format_elapsed(total_end - snapshot.started_at)
    if view != "detail":
        return total
    row = next((item for item in snapshot.rows if item.id == detail_row_id), None)
    if row is None:
        return total
    return f"{_elapsed(row)} / {total}"


def _right_aligned_line(left: str, right: str, width: int) -> str:
    if not right:
        return left.ljust(width)
    available = max(0, width - len(right) - 1)
    left = left[:available]
    return f"{left}{' ' * max(1, width - len(left) - len(right))}{right}"


def _row_attr(row: TuiRow) -> int:
    if row.status == "running":
        return _color(5) | curses.A_BOLD
    if row.status == "failed":
        return _color(4) | curses.A_BOLD
    if row.status == "warning":
        return _color(3)
    if row.status == "complete":
        return _color(2) | curses.A_BOLD
    return 0


def _elapsed(row: TuiRow) -> str:
    end = row.completed_at if row.completed_at is not None else time.monotonic()
    return _format_elapsed(end - row.started_at)


def _format_elapsed(elapsed: float) -> str:
    seconds = max(0, int(elapsed))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _num(value: int | None) -> str:
    return "-" if value is None else str(value)


def _clip(text: str, width: int) -> str:
    text = text.replace("\t", " ")
    return text[: max(0, width - 1)]


def _add(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _color(pair: int) -> int:
    if not curses.has_colors():
        return 0
    return curses.color_pair(pair)
