"""Exact-cycle reconciliation regressions for plan review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize(
    ("actors", "custody_pending"),
    [
        (["CORRUPT-ROW"], True),
        ([{
            "slot_id": "s1", "operation_id": "op-s1",
            "operation_state": "settled", "late_result_pending": False,
            "status": "error", "error": "unknown custody",
            "usage": {"physical_attempt_state": "future_state"},
        }], False),
    ],
)
def test_malformed_paid_wave_cannot_bypass_resume_validation(
    harness, monkeypatch, actors, custody_pending,
):
    """Malformed exact custody must not fall through to a fresh paid cycle."""
    from ouroboros.tools import plan_review as plan_review_tool

    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert len(calls) == 1

    materialize = plan_review_tool._authority_wave

    def malformed_authority(*args, **kwargs):
        wave = dict(materialize(*args, **kwargs))
        wave.update({
            "actors": actors, "paid": True,
            "custody_pending": custody_pending,
            "aggregate": "DEGRADED", "closed": False, "health_epoch": [],
        })
        return wave

    monkeypatch.setattr(plan_review_tool, "_authority_wave", malformed_authority)
    second = _call(ctx)

    assert len(calls) == 1
    assert second.startswith("ERROR: PLAN_REVIEW_CUSTODY_INVALID:")
    assert "Refusing" in second


def test_contradictory_positive_capture_reenters_custody_instead_of_fresh_cycle(
    harness, monkeypatch,
):
    """A synthetic $0 label cannot erase a positive physical-attempt fact."""
    import ouroboros.review_custody as review_custody
    from ouroboros.tools import plan_review as plan_review_tool

    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert len(calls) == 1

    materialize = plan_review_tool._authority_wave

    def contradictory_authority(*args, **kwargs):
        wave = dict(materialize(*args, **kwargs))
        wave.update({
            "actors": [{
                "slot_id": "s1", "operation_id": "op-s1",
                "operation_state": "not_dispatched", "late_result_pending": False,
                "status": "not_dispatched", "error": "synthetic refusal",
                "usage": {
                    "physical_attempt_state": "unresolved",
                    "provider_status_code": 503,
                },
            }, {
                "slot_id": "s2", "operation_id": "op-s2-free",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "frozen $0 refusal",
            }, {
                "slot_id": "s3", "operation_id": "op-s3-free",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "frozen $0 refusal",
            }],
            "paid": True, "custody_pending": False,
            "aggregate": "DEGRADED", "closed": False, "health_epoch": [],
        })
        return wave

    monkeypatch.setattr(plan_review_tool, "_authority_wave", contradictory_authority)
    monkeypatch.setattr(
        review_custody, "review_retry_custody_available", lambda **_kwargs: False,
    )
    second = _call(ctx)

    assert len(calls) == 1
    assert _control(second) == {"outcome": "DEGRADED", "closed": False}
    assert "process-local custody is unavailable" in second


def test_resume_excludes_synthetic_not_dispatched_operation_ids(tmp_path):
    """A pre-dispatch $0 row has an operation id, but is not a callable lane."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    slots = [
        SimpleNamespace(slot_id="s1", model="model/one"),
        SimpleNamespace(slot_id="s2", model="model/two"),
    ]
    result = in_flight_resume_inputs(
        {
            "actors": [
                {
                    "slot_id": "s1", "operation_id": "op-paid",
                    "operation_state": "in_flight", "late_result_pending": True,
                    "status": "error", "error": "still running",
                },
                {
                    "slot_id": "s2", "operation_id": "op-free",
                    "operation_state": "not_dispatched", "status": "not_dispatched",
                    "error": "budget admission refused",
                },
            ],
        },
        {}, tmp_path, "mixed-resume", slots,
    )

    assert result["dispatched_slot_ids"] == ["s1"]
    assert [row["slot_id"] for row in result["frozen_rows"]] == ["s2"]


def test_resume_counts_positive_capture_despite_synthetic_not_dispatched(tmp_path):
    """Positive physical custody outranks contradictory synthetic $0 labels."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "synthetic refusal",
                "usage": {
                    "physical_attempt_state": "unresolved",
                    "provider_status_code": 503,
                },
            }],
        },
        {}, tmp_path, "contradictory-resume", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert result["dispatched_slot_ids"] == ["s1"]
    assert result["frozen_rows"] == []


def test_resume_rejects_non_object_rows_in_exact_paid_roster(tmp_path):
    """A corrupt durable roster must not lose rows during reconciliation."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "in_flight", "late_result_pending": True,
                "status": "error", "error": "still running",
            }, "CORRUPT-ROW"],
        },
        {}, tmp_path, "malformed-roster", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert "error" in result
    assert "drop rows" in result["error"]


def test_resume_rejects_unknown_physical_attempt_state(tmp_path):
    """Unknown custody facts cannot be inferred as a safe retry or refusal."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "settled", "status": "error",
                "error": "provider state unavailable",
                "usage": {"physical_attempt_state": "future_state"},
            }],
        },
        {}, tmp_path, "unknown-roster-state", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert "error" in result
    assert "unknown physical-attempt state" in result["error"]


def test_zero_send_route_refusal_does_not_spend_a_plan_cycle(harness, monkeypatch):
    """Callable configuration is not monetary proof when every slot refuses pre-send."""
    import ouroboros.review_substrate as review_substrate

    calls = []

    def zero_send(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append([slot.slot_id for slot in slots])
        return SimpleNamespace(actors=[{
            "slot_id": slot.slot_id,
            "model": slot.model,
            "status": "not_dispatched",
            "raw_text": "",
            "error": "agent session slot has no session task",
            "failure_code": "session_task_missing",
            "usage": {},
            "prompt_ref": {},
            "response_ref": {},
            "operation_id": f"op-{slot.slot_id}",
            "operation_state": "not_dispatched",
            "late_result_pending": False,
        } for slot in slots])

    monkeypatch.setattr(review_substrate, "run_review_request", zero_send)
    output = _call(harness.make_ctx())
    state = _state(harness)
    wave = state["waves"][-1]

    assert calls == [["s1", "s2", "s3"]]
    assert _control(output) == {"outcome": "DEGRADED", "closed": False}
    assert wave["paid"] is False
    assert state["cycles_paid"] == 0
    assert {row["failure_code"] for row in wave["actors"]} == {
        "session_task_missing",
    }


@pytest.mark.parametrize("row", [
    {"status": "error", "error": "substrate omitted the actor"},
    {"status": "error", "usage": {"physical_attempt_state": "settled"}},
    {"status": "error", "usage": {"physical_attempt_state": "future_state"}},
])
def test_missing_operation_identity_cannot_prove_a_free_wave(row):
    from ouroboros.tools.plan_review_artifacts import _row_has_physical_dispatch

    assert _row_has_physical_dispatch(row) is True


def test_explicit_zero_send_fact_is_the_only_missing_identity_free_proof():
    from ouroboros.tools.plan_review_artifacts import _row_has_physical_dispatch

    assert _row_has_physical_dispatch({
        "status": "not_dispatched", "operation_state": "not_dispatched",
    }) is False


def test_missing_substrate_actor_stays_paid_and_custody_lost(harness, monkeypatch):
    """A dropped actor is unknown physical custody, never a terminal free retry."""
    import ouroboros.review_substrate as review_substrate

    calls = []

    def no_actors(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append([slot.slot_id for slot in slots])
        return SimpleNamespace(actors=[])

    monkeypatch.setattr(review_substrate, "run_review_request", no_actors)
    first = _call(harness.make_ctx())
    state = _state(harness)
    wave = state["waves"][-1]

    assert calls == [["s1", "s2", "s3"]]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert wave["paid"] is True and wave["custody_pending"] is True
    assert state["cycles_paid"] == 1
    assert {row["failure_code"] for row in wave["actors"]} == {
        "review_custody_lost",
    }
    assert {row["operation_state"] for row in wave["actors"]} == {
        "custody_lost",
    }

    second = _call(harness.make_ctx())
    assert _control(second) == {"outcome": "DEGRADED", "closed": False}
    assert "Refusing a duplicate paid send" in second
    assert calls == [["s1", "s2", "s3"]]
    assert _state(harness)["cycles_paid"] == 1
