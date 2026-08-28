"""Exact-cycle reconciliation regressions for plan review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.test_plan_review_engine import (
    CLEAN,
    _call,
    _control,
    _finding,
    harness as _engine_harness,
    _patch_health,
    _state,
)

# Explicitly re-export the fixture. pytest 8.x does not reliably discover a
# fixture from a test module via ``pytest_plugins`` when the provider is also
# collected, while pytest 9.x happens to do so.
harness = _engine_harness  # noqa: F811 - pytest fixture re-export


def _install_two_turn_substrate(monkeypatch, calls, *, pending_ids=None, texts=None):
    import ouroboros.review_custody as review_custody
    import ouroboros.review_substrate as review_substrate

    pending_ids = set(pending_ids or [])
    texts = dict(texts or {})

    def substrate(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append((request.retry_key, [slot.slot_id for slot in slots]))
        first = len(calls) == 1
        actors = []
        for slot in slots:
            pending = first and (not pending_ids or slot.slot_id in pending_ids)
            actors.append({
                "slot_id": slot.slot_id, "model": slot.model,
                "status": "error" if pending else "ok",
                "raw_text": "" if pending else texts.get(slot.slot_id, CLEAN),
                "error": "logical wait expired" if pending else "",
                "usage": {"resolved_model": slot.model},
                "prompt_ref": {}, "response_ref": {},
                "operation_id": f"op-{slot.slot_id}",
                "operation_state": "in_flight" if pending else "late_settled",
                "late_result_pending": pending,
            })
        return SimpleNamespace(actors=actors)

    monkeypatch.setattr(review_substrate, "run_review_request", substrate)
    monkeypatch.setattr(review_custody, "review_retry_custody_available", lambda **_kwargs: True)


def test_partial_quorum_stays_open_while_one_paid_slot_is_in_flight(harness, monkeypatch):
    """A 2/3 parseable quorum cannot close over a live paid reviewer worker."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    calls = []
    _install_two_turn_substrate(monkeypatch, calls, pending_ids={"s3"})
    ctx = harness.make_ctx()

    first = _call(ctx)
    state = _state(harness)
    wave = state["waves"][-1]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert wave["aggregate"] == "DEGRADED"
    assert wave["closed"] is False and wave["custody_pending"] is True
    assert "review_late_result_pending" in wave["reasons"]
    assert "Closed: proceed" not in first

    second = _call(ctx)
    assert _control(second) == {"outcome": "GREEN", "closed": True}


def test_expired_deadline_still_reconciles_existing_paid_wave(harness, monkeypatch):
    """An owner deadline must not strand a reviewer cycle already in flight."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    calls = []
    _install_two_turn_substrate(monkeypatch, calls, pending_ids={"s3"})
    ctx = harness.make_ctx()
    ctx.task_metadata["deadline_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=2000)
    ).isoformat()

    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert _state(harness)["waves"][-1]["custody_pending"] is True

    # The second envelope arrives after the task's logical deadline. It must
    # rejoin the exact paid cycle, not return a fresh-deadline skip forever.
    ctx.task_metadata["deadline_at"] = "2000-01-01T00:00:00+00:00"
    second = _call(ctx)

    assert _control(second) == {"outcome": "GREEN", "closed": True}
    assert calls == [calls[0], calls[0]]
    wave = _state(harness)["waves"][-1]
    assert wave["custody_pending"] is False and wave["closed"] is True


def test_resume_keeps_original_dispatched_set_when_skipped_lane_heals(harness, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    health_calls = []

    def health(_slots):
        health_calls.append(1)
        if len(health_calls) > 1:
            raise AssertionError("in-flight reconciliation re-probed live health")
        return {"s1": {"failure_code": "credential_pool_exhausted", "reset_at": ""}}

    _patch_health(monkeypatch, health)
    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    out = _call(ctx)

    assert _control(out) == {"outcome": "GREEN", "closed": True}
    assert calls == [(calls[0][0], ["s2", "s3"]), (calls[0][0], ["s2", "s3"])]
    assert health_calls == [1]
    state = _state(harness)
    assert state["cycles_paid"] == 1 and state["waves"][-1]["cycle_index"] == 1
    frozen = {row["slot_id"]: row for row in state["waves"][-1]["actors"]}["s1"]
    assert frozen["failure_code"] == "credential_pool_exhausted" and frozen["cost"] == 0.0


def test_resume_keeps_dispatched_lane_when_live_health_worsens(harness, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    health_state, health_calls = {"evidence": {}}, []

    def health(_slots):
        health_calls.append(1)
        if len(health_calls) > 1:
            raise AssertionError("in-flight reconciliation re-probed worsened health")
        return dict(health_state["evidence"])

    _patch_health(monkeypatch, health)
    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    health_state["evidence"] = {
        "s1": {"failure_code": "subscription_window_exhausted",
               "reset_at": "2030-01-01T00:00:00+00:00"},
    }
    out = _call(ctx)

    assert _control(out) == {"outcome": "GREEN", "closed": True}
    assert calls == [
        (calls[0][0], ["s1", "s2", "s3"]),
        (calls[0][0], ["s1", "s2", "s3"]),
    ]
    assert health_calls == [1]
    assert _state(harness)["cycles_paid"] == 1


def test_in_flight_wave_defers_need_evidence_until_terminal_reconciliation(
    harness, monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    requested = json.dumps([_finding(
        "e1", "need_evidence", locator="notes.md", summary="read the notes",
    )])
    calls = []
    _install_two_turn_substrate(
        monkeypatch, calls, pending_ids={"s2", "s3"}, texts={"s1": requested},
    )
    ctx = harness.make_ctx()
    _call(ctx)
    first_state = _state(harness)
    first_fp = first_state["waves"][-1]["request_fingerprint"]
    assert first_state["need_evidence_seen"] == []

    out = _call(ctx)
    state = _state(harness)
    assert _control(out) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert [key for key, _slots in calls] == [calls[0][0], calls[0][0]]
    assert state["waves"][-1]["request_fingerprint"] == first_fp
    assert state["waves"][-1]["cycle_index"] == 1 and state["cycles_paid"] == 1
    assert state["need_evidence_seen"] == ["notes.md"]
