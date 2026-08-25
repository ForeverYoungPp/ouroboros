"""Focused contract tests for durable Plan Review refresh references."""

from __future__ import annotations

import json
import queue
from hashlib import sha256
from types import SimpleNamespace

import pytest

from ouroboros import task_results
from ouroboros.tools import plan_review_references


def _revision(state: dict) -> str:
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _reference_rows(events: queue.Queue) -> list[dict]:
    rows = []
    while not events.empty():
        event = events.get_nowait()
        if event.get("type") == "log_event" and event.get("data", {}).get("type") == "review_reference":
            rows.append(event["data"])
    return rows


def test_plan_reference_uses_shared_best_effort_log_event_seam(monkeypatch):
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=23)
    state = {"current_attempt": {"fingerprint": "review-fingerprint"}, "waves": []}
    calls = []

    def capture(event_queue, payload, **kwargs):
        calls.append((event_queue, payload, kwargs))

    monkeypatch.setattr(plan_review_references, "emit_log_event", capture)
    plan_review_references._emit_plan_review_reference(ctx, "task-1", state)

    assert len(calls) == 1
    event_queue, payload, kwargs = calls[0]
    assert event_queue is events
    assert kwargs == {"log_label": "plan-review state reference"}
    assert payload == {
        "type": "review_reference",
        "surface": "plan_review",
        "task_id": "task-1",
        "chat_id": 23,
        "review_fingerprint": "review-fingerprint",
        "state_revision": _revision(state),
        "ts": payload["ts"],
    }


def test_attempt_helper_publishes_immediately_after_the_canonical_write(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []
    state = {"current_attempt": {"fingerprint": "a" * 64}}

    monkeypatch.setattr(
        plan_review_references, "record_plan_review_attempt",
        lambda *args, **kwargs: calls.append("attempt") or state,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    result = plan_review_references._record_plan_review_attempt_with_reference(
        ctx, None, "task-1", fingerprint="a" * 64,
    )

    assert result is state
    assert calls == ["attempt", "reference"]


def test_raw_request_helper_publishes_immediately_after_the_canonical_write(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []

    monkeypatch.setattr(
        plan_review_references, "record_raw_plan_request_attempt",
        lambda *args, **kwargs: calls.append("raw") or "a" * 64,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    fingerprint = plan_review_references._record_raw_plan_request_with_reference(
        ctx, None, "task-1", {"plan": "invalid"}, reason="plan_input_invalid",
    )

    assert fingerprint == "a" * 64
    assert calls == ["raw", "reference"]


def test_cycles_exhausted_helper_publishes_each_write_before_the_typed_event(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []
    marked = {"request_fingerprint": "a" * 64, "cycles_exhausted": True}
    attempt = {"current_attempt": {"fingerprint": "b" * 64}}

    monkeypatch.setattr(
        plan_review_references, "mark_plan_review_cycles_exhausted",
        lambda *args, **kwargs: calls.append("mark") or marked,
    )
    monkeypatch.setattr(
        plan_review_references, "record_plan_review_attempt",
        lambda *args, **kwargs: calls.append("attempt") or attempt,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    result = plan_review_references._record_cycles_exhausted_with_references(
        ctx, None, "task-1", wave_fingerprint="a" * 64,
        attempt_fingerprint="b" * 64, cycles_paid=2, cap=2,
    )

    assert result is marked
    assert calls == ["mark", "reference", "attempt", "reference"]


def test_first_cycles_exhausted_revision_is_published_if_second_write_fails(
    tmp_path, monkeypatch,
):
    fingerprint = "a" * 64
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    task_results.record_plan_review_attempt(
        tmp_path, "task-1", fingerprint=fingerprint,
    )
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=23)

    def fail_second_write(*args, **kwargs):
        raise OSError("second canonical write failed")

    monkeypatch.setattr(plan_review_references, "record_plan_review_attempt", fail_second_write)

    with pytest.raises(OSError, match="second canonical write failed"):
        plan_review_references._record_cycles_exhausted_with_references(
            ctx, tmp_path, "task-1", wave_fingerprint=fingerprint,
            attempt_fingerprint=fingerprint, cycles_paid=2, cap=2,
        )

    durable = task_results.load_plan_review_state(tmp_path, "task-1")
    refs = _reference_rows(events)
    assert len(refs) == 1
    assert refs[0]["review_fingerprint"] == fingerprint
    assert refs[0]["state_revision"] == _revision(durable)
