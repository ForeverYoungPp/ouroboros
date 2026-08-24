"""Focused frozen-roster reconciliation regressions."""

import json
from types import SimpleNamespace

from ouroboros.review_custody import (
    merge_frozen_review_reconciliation,
    prepare_frozen_review_reconciliation,
)


def test_duplicate_current_slot_is_rejected_order_independently():
    original = {
        "slot_id": "slot-1", "model_id": "m1", "status": "error",
        "operation_id": "op-1", "operation_state": "in_flight",
        "late_result_pending": True,
    }
    duplicate_rows = [
        {
            "slot_id": "slot-1", "model_id": "m1", "status": "responded",
            "raw_text": "[]", "operation_id": "op-1", "operation_state": "settled",
        },
        {
            "slot_id": "slot-1", "model_id": "m1", "status": "responded",
            "raw_text": "different", "operation_id": "op-other",
            "operation_state": "settled",
        },
    ]
    outcomes = []
    for current in (duplicate_rows, list(reversed(duplicate_rows))):
        ctx = SimpleNamespace(
            _last_triad_raw_results=current, _last_scope_raw_result={},
        )
        prepare_frozen_review_reconciliation(
            ctx, SimpleNamespace(triad_raw_results=[original], scope_raw_result={}),
        )
        merge_frozen_review_reconciliation(ctx)
        outcomes.append(ctx._last_triad_raw_results)
        assert ctx._review_custody_lost is True

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0]["operation_state"] == "custody_lost"
    assert outcomes[0][0]["late_result_pending"] is True


def test_duplicate_against_terminal_original_stays_lost_on_next_retry():
    original = {
        "slot_id": "slot-1", "model_id": "m1", "status": "responded",
        "raw_text": "[]", "operation_id": "op-1", "operation_state": "settled",
        "late_result_pending": False,
    }
    duplicates = [
        dict(original),
        {**original, "raw_text": "other", "operation_id": "op-other"},
    ]
    first = SimpleNamespace(
        _last_triad_raw_results=duplicates, _last_scope_raw_result={},
    )
    prepare_frozen_review_reconciliation(
        first, SimpleNamespace(triad_raw_results=[original], scope_raw_result={}),
    )
    merge_frozen_review_reconciliation(first)

    assert first._review_custody_lost is True
    assert first._last_triad_raw_results[0]["operation_state"] == "custody_lost"
    assert first._last_triad_raw_results[0]["late_result_pending"] is True

    same_operation_settled = [{
        **original, "operation_state": "settled", "late_result_pending": False,
    }]
    second = SimpleNamespace(
        _last_triad_raw_results=same_operation_settled, _last_scope_raw_result={},
    )
    prepare_frozen_review_reconciliation(
        second,
        SimpleNamespace(
            triad_raw_results=first._last_triad_raw_results,
            scope_raw_result=first._last_scope_raw_result,
        ),
    )
    merge_frozen_review_reconciliation(second)

    assert second._review_custody_lost is True
    assert second._last_triad_raw_results[0]["operation_state"] == "custody_lost"
    assert second._last_triad_raw_results[0]["late_result_pending"] is True

    third = SimpleNamespace(
        _last_triad_raw_results=same_operation_settled, _last_scope_raw_result={},
    )
    prepare_frozen_review_reconciliation(
        third,
        SimpleNamespace(
            triad_raw_results=second._last_triad_raw_results,
            scope_raw_result=second._last_scope_raw_result,
        ),
    )
    merge_frozen_review_reconciliation(third)

    assert third._review_custody_lost is True
    assert third._last_triad_raw_results[0]["operation_state"] == "custody_lost"
    assert third._last_triad_raw_results[0]["late_result_pending"] is True


def test_mixed_non_object_triad_row_becomes_durable_custody_loss():
    terminal = {
        "slot_id": "slot-1", "status": "responded", "raw_text": "[]",
        "operation_id": "op-1", "operation_state": "settled",
    }
    ctx = SimpleNamespace(_last_triad_raw_results=[], _last_scope_raw_result={})
    prepare_frozen_review_reconciliation(
        ctx,
        SimpleNamespace(triad_raw_results=[terminal, "malformed"], scope_raw_result={}),
    )
    merge_frozen_review_reconciliation(ctx)

    assert ctx._review_custody_lost is True
    assert ctx._last_triad_raw_results[0] == terminal
    assert ctx._last_triad_raw_results[1]["operation_state"] == "custody_lost"
    assert ctx._last_triad_raw_results[1]["late_result_pending"] is True


def test_malformed_scope_row_and_container_become_durable_custody_loss():
    for malformed_scope in ({"raw_results": ["malformed"]}, "malformed"):
        ctx = SimpleNamespace(_last_triad_raw_results=[], _last_scope_raw_result={})
        prepare_frozen_review_reconciliation(
            ctx,
            SimpleNamespace(
                triad_raw_results=[{
                    "slot_id": "slot-1", "status": "responded", "raw_text": "[]",
                    "operation_id": "op-1", "operation_state": "settled",
                }],
                scope_raw_result=malformed_scope,
            ),
        )
        merge_frozen_review_reconciliation(ctx)

        scope_rows = ctx._last_scope_raw_result["raw_results"]
        assert ctx._review_custody_lost is True
        assert scope_rows[0]["operation_state"] == "custody_lost"
        assert scope_rows[0]["late_result_pending"] is True


def test_malformed_durable_roster_containers_survive_real_state_loading(tmp_path):
    from ouroboros.review_state import load_state, make_repo_key
    from ouroboros.tools.commit_gate import _check_overlapping_review_attempt

    state_path = tmp_path / "state" / "advisory_review.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "attempts": [
            {
                "ts": "2026-08-24T00:00:00Z",
                "commit_message": "test",
                "status": "reviewing",
                "task_id": "task-1",
                "repo_key": make_repo_key(tmp_path),
                "tool_name": "commit_reviewed",
                "attempt": 1,
                "paid": True,
                "review_retry_key": "commit_review:exact",
                "triad_raw_results": "malformed",
                "scope_raw_result": ["malformed"],
            },
            {
                "ts": "2026-08-24T00:01:00Z",
                "commit_message": "historical",
                "status": "succeeded",
                "task_id": "task-old",
                "repo_key": make_repo_key(tmp_path),
                "tool_name": "commit_reviewed",
                "attempt": 2,
                "paid": True,
                "triad_raw_results": "malformed",
                "scope_raw_result": ["malformed"],
            },
        ],
    }), encoding="utf-8")

    state = load_state(tmp_path)

    assert len(state.attempts) == 2
    attempt = next(row for row in state.attempts if row.status == "reviewing")
    historical = next(row for row in state.attempts if row.status == "succeeded")
    assert attempt.paid is True
    assert attempt.late_result_pending is False
    assert attempt.triad_raw_results[0]["operation_state"] == "custody_lost"
    assert attempt.scope_raw_result["raw_results"][0]["operation_state"] == "custody_lost"
    assert historical.late_result_pending is False
    assert historical.triad_raw_results[0]["operation_state"] == "custody_lost"
    assert historical not in state.get_active_attempts(repo_key=make_repo_key(tmp_path))

    ctx = SimpleNamespace(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id="task-1",
        _current_review_tool_name="commit_reviewed",
    )
    assert _check_overlapping_review_attempt(ctx) is None
    assert ctx._review_resume_pending is True
    assert ctx._pending_review_attempt.triad_raw_results[0]["operation_state"] == "custody_lost"
