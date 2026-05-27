from __future__ import annotations

from codex_super_review.event_sink import TuiEventSink


def test_oracle_classification_result_moves_after_thread_start() -> None:
    sink = TuiEventSink()

    sink.apply_audit_record(
        {
            "timestamp": "2026-05-27T00:00:00+00:00",
            "event": "oracle_classification",
            "review_round": 2,
            "fix_round": None,
            "message": "classifying findings",
            "extra": {"tui_status": "running"},
        }
    )
    sink.apply_audit_record(
        {
            "timestamp": "2026-05-27T00:00:01+00:00",
            "event": "oracle_thread_started",
            "review_round": 2,
            "fix_round": None,
            "message": "oracle thread started",
        }
    )
    sink.apply_audit_record(
        {
            "timestamp": "2026-05-27T00:00:02+00:00",
            "event": "oracle_classification_result",
            "review_round": 2,
            "fix_round": None,
            "message": "NO_REJECTED_FINDINGS",
        }
    )

    rows = sink.snapshot().rows

    assert [row.event for row in rows] == [
        "oracle_thread_started",
        "oracle_classification_result",
    ]
    assert rows[1].started_at < rows[1].completed_at
