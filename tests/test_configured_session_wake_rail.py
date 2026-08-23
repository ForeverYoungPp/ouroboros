"""Focused contract tests for the sleeping configured-session wake rail."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros import task_tree_ledger
from ouroboros.delegate_supervision import (
    acknowledge_pending_wake,
    delegate_wait_entry,
    supervised_wait,
)
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


def test_child_terminal_before_first_sleep_is_not_lost_as_cursor_baseline(tmp_path):
    from ouroboros.artifacts import copy_file_to_task_artifacts
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256

    local = tmp_path / "child-drive"
    canonical = tmp_path / "canonical"
    local.mkdir()
    canonical.mkdir()
    source = tmp_path / "child-report.txt"
    source.write_text("exact child artifact", encoding="utf-8")
    copy_file_to_task_artifacts(
        SimpleNamespace(drive_root=canonical, task_id="child"),
        source,
        kind="user_file",
    )
    _child(canonical, status=STATUS_COMPLETED)
    ctx = _ctx(local)
    ctx.budget_drive_root = str(canonical)
    assert task_tree_ledger.tree_ledger_append(
        "root",
        "review_requested",
        "Please independently challenge this evidence before integration.",
        task_id="child",
        payload={"evidence_ref": "artifact:claim-set", "evidence_sha256": "a" * 64},
        data_root=canonical,
    ).startswith("OK:")

    wake = json.loads(supervised_wait(
        ctx,
        "run-1",
        wait_once=lambda *_args: json.dumps({
            "status": "no_progress", "run_id": "run-1", "last_seq": 1,
        }),
    ))

    terminal = next(event for event in wake["wake_events"] if event["type"] == "child_terminal")
    assert terminal["child_task_id"] == "child"
    assert terminal["status"] == STATUS_COMPLETED
    assert terminal["result_sha256"] == _child_result_sha256(
        load_effective_task_result(canonical, "child")
    )
    event = next(item for item in wake["wake_events"] if item["type"] == "child_attention_beacon")
    assert event["beacon"]["kind"] == "review_requested"
    assert event["beacon"]["payload"]["evidence_sha256"] == "a" * 64
    assert not any(path.name.startswith("task_acceptance") for path in canonical.rglob("*"))


def test_oversized_coordination_wake_is_valid_bounded_json_with_exact_source(tmp_path):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    ctx = _ctx(tmp_path)
    for index in range(5):
        child_id = f"child-{index}"
        _child(tmp_path, task_id=child_id)
        assert task_tree_ledger.tree_ledger_append(
            "root", "blocker", f"blocker-{index}:" + "x" * 3800,
            task_id=child_id, data_root=tmp_path,
        ).startswith("OK:")

    raw = supervised_wait(
        ctx,
        "run-large",
        wait_once=lambda *_args: json.dumps({
            "status": "no_progress", "run_id": "run-large", "last_seq": 1,
        }),
    )
    assert len(raw) <= tool_result_limit("delegate_wait")
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    delivered = json.loads(raw)
    assert delivered["supervision_wake_id"]
    assert delivered["wake_delivery"]["complete"] is False
    assert delivered["wake_delivery"]["wake_events_total"] == 5
    source = delivered["wake_delivery"]["source"]
    full = json.loads(read_actor_source_bytes(tmp_path, "parent", source))
    assert len(full["wake_events"]) == 5
    assert all(len(item["beacon"]["text"]) > 3800 for item in full["wake_events"])
    assert acknowledge_pending_wake(ctx, raw)

    after = json.loads(supervised_wait(
        ctx,
        "run-large",
        wait_once=lambda *_args: json.dumps({
            "status": "completed", "run_id": "run-large", "last_seq": 2,
        }),
    ))
    assert not any(
        item.get("type") == "child_attention_beacon"
        for item in after.get("wake_events", [])
    )


def test_delegate_wait_entry_never_acks_an_undelivered_pending_wake(tmp_path, monkeypatch):
    from ouroboros.tools import delegate as delegate_module

    _child(tmp_path, status=STATUS_COMPLETED)
    ctx = _ctx(tmp_path)
    first = supervised_wait(
        ctx,
        "run-1",
        wait_once=lambda *_args: json.dumps({
            "status": "no_progress", "run_id": "run-1", "last_seq": 1,
        }),
    )
    assert acknowledge_pending_wake(ctx, "truncated-not-json") is False
    monkeypatch.setattr(
        delegate_module,
        "_delegate_wait",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("pending wake must replay before another physical poll")
        ),
    )

    assert delegate_wait_entry(ctx, "run-1") == first
