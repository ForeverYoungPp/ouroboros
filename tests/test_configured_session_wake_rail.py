"""Focused contract tests for the sleeping configured-session wake rail."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros import task_tree_ledger
from ouroboros.delegate_supervision import acknowledge_pending_wake, supervised_wait
from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result


def _ctx(tmp_path):
    return SimpleNamespace(
        task_id="parent",
        task_attempt=1,
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_metadata={"root_task_id": "root", "delegation_role": "subagent"},
    )


def _child(tmp_path, task_id="child", parent_task_id="parent", status=STATUS_RUNNING):
    return write_task_result(
        tmp_path,
        task_id,
        status,
        parent_task_id=parent_task_id,
        root_task_id="root",
        delegation_role="subagent",
    )


def test_sleeping_nanny_wakes_for_only_a_direct_child_beacon(tmp_path):
    _child(tmp_path)
    _child(tmp_path, "sibling", parent_task_id="other")
    ctx = _ctx(tmp_path)

    def wait_once(_ctx, _run_id, _timeout, _cursor):
        assert task_tree_ledger.tree_ledger_append(
            "root", "blocker", "child needs parent input", task_id="child",
            data_root=tmp_path,
        ).startswith("OK:")
        assert task_tree_ledger.tree_ledger_append(
            "root", "question", "sibling asks unrelated question", task_id="sibling",
            data_root=tmp_path,
        ).startswith("OK:")
        return json.dumps({"status": "no_progress", "run_id": "run-1", "last_seq": 1})

    wake = json.loads(supervised_wait(ctx, "run-1", wait_once=wait_once))
    assert wake["status"] == "no_progress"
    assert [event["type"] for event in wake["wake_events"]] == ["child_attention_beacon"]
    assert wake["wake_events"][0]["beacon"]["task_id"] == "child"


def test_child_terminal_transition_coalesces_with_leaf_wake_and_replays_until_ack(tmp_path):
    _child(tmp_path)
    ctx = _ctx(tmp_path)
    calls = []

    def wait_once(_ctx, _run_id, _timeout, _cursor):
        calls.append(1)
        write_task_result(tmp_path, "child", STATUS_COMPLETED, result="verified child artifact")
        return json.dumps({"status": "no_progress", "run_id": "run-1", "last_seq": 2})

    first = json.loads(supervised_wait(ctx, "run-1", wait_once=wait_once))
    terminal = next(event for event in first["wake_events"] if event["type"] == "child_terminal")
    assert terminal["child_task_id"] == "child"
    assert terminal["status"] == STATUS_COMPLETED
    replay = json.loads(supervised_wait(
        ctx,
        "run-1",
        wait_once=lambda *_args: (_ for _ in ()).throw(
            AssertionError("the unacknowledged child wake must replay before polling the leaf")
        ),
    ))
    assert replay == first
    assert acknowledge_pending_wake(ctx, replay)
    assert calls == [1]
