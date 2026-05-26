from __future__ import annotations

import curses
import json
import signal
import textwrap
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from .event_sink import TuiEventSink, TuiRow, TuiSnapshot

SPINNER = "|/-\\"


class CursesUnavailable(RuntimeError):
    pass


def run_tui(
    args: Any,
    run_func: Callable[[Any, Any], int],
) -> int:
    sink = TuiEventSink()
    args.event_sink = sink
    result: dict[str, int | None] = {"returncode": None}

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
    if sink.snapshot().abort_requested:
        return 130
    return result["returncode"] if result["returncode"] is not None else 130


def _curses_main(
    stdscr: Any,
    sink: TuiEventSink,
    worker: Callable[[], None],
    worker_started: dict[str, bool],
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

    previous_handlers = _install_signal_handlers(sink)
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
                )
                tick += 1

                key = stdscr.getch()

                if key == -1:
                    pass
                elif key == 3:
                    if snapshot.finished:
                        break
                    sink.request_interrupt()
                elif view == "detail":
                    detail_scroll = _handle_detail_key(
                        key, detail_scroll, snapshot, detail_row_id
                    )
                    if key in (27, curses.KEY_BACKSPACE, 8, 127):
                        view = "list"
                        detail_row_id = None
                else:
                    selected, row_scroll, view, detail_row_id = _handle_list_key(
                        key, selected, row_scroll, rows
                    )

                if not thread.is_alive() and snapshot.finished:
                    time.sleep(0.08)
                else:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                snapshot = sink.snapshot()
                if snapshot.finished:
                    break
                sink.request_interrupt()
    finally:
        if not sink.snapshot().finished:
            sink.terminate_active_process()
        _restore_signal_handlers(previous_handlers)
        thread.join(timeout=3.0)


def _install_signal_handlers(sink: TuiEventSink) -> dict[int, signal.Handlers]:
    previous_handlers: dict[int, signal.Handlers] = {}

    def handle_signal(signum: int, frame: object) -> None:
        if signum == signal.SIGINT:
            if sink.snapshot().finished:
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
        detail_scroll = len(_detail_lines(snapshot, detail_row_id, 80))
    return max(0, detail_scroll)


def _draw(
    stdscr: Any,
    snapshot: TuiSnapshot,
    selected: int,
    row_scroll: int,
    view: str,
    detail_row_id: int | None,
    detail_scroll: int,
    tick: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 8 or width < 40:
        _add(stdscr, 0, 0, "codex-super-review: terminal is too small")
        stdscr.refresh()
        return

    spinner = " " if snapshot.finished else SPINNER[tick % len(SPINNER)]
    code = "" if snapshot.returncode is None else f" exit={snapshot.returncode}"
    title = f" codex-super-review {spinner}{code} "
    _add(stdscr, 0, 0, title.ljust(width), _color(1) | curses.A_BOLD)
    _add(stdscr, 1, 0, _clip(snapshot.status_message, width), _color(5))

    header = _header_line(snapshot.headers)
    _add(stdscr, 2, 0, _clip(header, width))
    if snapshot.finished:
        _add(
            stdscr,
            3,
            0,
            _clip((snapshot.final_message or "Done") + " - press Ctrl+C to quit", width),
            _color(2 if snapshot.returncode == 0 else 4) | curses.A_BOLD,
        )
    else:
        _add(stdscr, 3, 0, "Ctrl+C: graceful stop; Ctrl+C again: abort")

    if view == "detail":
        _draw_detail(stdscr, snapshot, detail_row_id, detail_scroll, width, height)
    else:
        _draw_list(stdscr, snapshot, selected, row_scroll, width, height)
    stdscr.refresh()


def _draw_list(
    stdscr: Any,
    snapshot: TuiSnapshot,
    selected: int,
    row_scroll: int,
    width: int,
    height: int,
) -> None:
    header_y = 5
    _add(stdscr, header_y, 0, _clip("Status   Event                          Rev Fix  Elapsed  Summary", width), curses.A_BOLD)
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
) -> None:
    lines = _detail_lines(snapshot, detail_row_id, width)
    start_y = 5
    visible = max(1, height - start_y - 2)
    max_scroll = max(0, len(lines) - visible)
    detail_scroll = min(detail_scroll, max_scroll)
    _add(stdscr, start_y, 0, _clip("Detail - Esc/Backspace: back", width), curses.A_BOLD)
    for index, line in enumerate(lines[detail_scroll : detail_scroll + visible]):
        _add(stdscr, start_y + 1 + index, 0, _clip(line, width))
    footer = f"line {min(detail_scroll + 1, len(lines) or 1)}/{max(len(lines), 1)}"
    _add(stdscr, height - 1, 0, _clip(footer, width), curses.A_DIM)


def _detail_lines(
    snapshot: TuiSnapshot,
    detail_row_id: int | None,
    width: int,
) -> list[str]:
    row = next((item for item in snapshot.rows if item.id == detail_row_id), None)
    if row is None:
        return ["No row selected"]
    record = row.record or {}
    lines = [
        f"event: {row.event}",
        f"status: {row.status}",
        f"review_round: {_num(row.review_round)}",
        f"fix_round: {_num(row.fix_round)}",
        f"elapsed: {_elapsed(row)}",
        f"summary: {row.message}",
    ]
    for key in (
        "timestamp",
        "implementer_thread_id",
        "reviewer_thread_id",
        "codex_exit_code",
        "usage",
        "event_types",
        "codex_errors",
        "diagnostics",
        "extra",
        "message",
    ):
        if key in record and record[key] not in (None, [], {}):
            lines.extend(_wrapped_field(key, record[key], width))
    for key in ("response", "prompt"):
        value = record.get(key)
        if isinstance(value, str) and value:
            lines.append("")
            lines.append(f"{key}:")
            lines.extend(_wrap_text(value, width))
    return lines


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


def _header_line(headers: dict[str, str]) -> str:
    if not headers:
        return ""
    preferred = ["Audit log", "Implementer", "Models", "Review scope", "Oracle workspace"]
    parts = [f"{key}: {headers[key]}" for key in preferred if key in headers]
    parts.extend(f"{key}: {value}" for key, value in headers.items() if key not in preferred)
    return " | ".join(parts)


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
    seconds = max(0, int(end - row.started_at))
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
