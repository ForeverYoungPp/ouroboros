"""Focused typed admission and depth-provenance tests for nested delegation."""

import json
from types import SimpleNamespace

from ouroboros.contracts.task_contract import build_task_contract
from ouroboros.task_results import STATUS_RUNNING, write_task_result
from ouroboros.tools.control_delegation import (
    check_delegation_admission,
    child_budget_for_schedule,
    durable_direct_child_count,
    schedule_delegation_refusal,
    stamp_depth_provenance,
    stamp_task_assignment_depth,
)


def test_explicit_rights_are_typed_and_legacy_omission_stays_permissive():
    assert check_delegation_admission({"may_delegate": False}).reason_code == "delegation_rights_may_delegate"
    assert check_delegation_admission({"may_delegate": "false"}).reason_code == "delegation_rights_may_delegate"
    assert check_delegation_admission({"may_fan_out": False}, direct_child_count=0).ok
    second = check_delegation_admission({"may_fan_out": "false"}, direct_child_count=1)
    assert second.ok is False and second.reason_code == "delegation_rights_may_fan_out"
    exhausted = check_delegation_admission({"depth_remaining": 0})
    assert exhausted.ok is False and exhausted.reason_code == "delegation_rights_depth_exhausted"
    capped = check_delegation_admission({"max_children": 1}, direct_child_count=1)
    assert capped.ok is False and capped.reason_code == "delegation_rights_max_children"
    assert check_delegation_admission({}, direct_child_count=99).ok
    assert check_delegation_admission({}, direct_child_count=None).ok
    unknown_fanout = check_delegation_admission(
        {"may_fan_out": False}, direct_child_count=None,
    )
    assert unknown_fanout.reason_code == "delegation_rights_child_count_unknown"
    unknown_cap = check_delegation_admission(
        {"max_children": 1}, direct_child_count=None,
    )
    assert unknown_cap.reason_code == "delegation_rights_child_count_unknown"

    narrowed = child_budget_for_schedule(
        {"delegation_budget": {"may_delegate": "false", "may_fan_out": "false"}},
        current_depth=0, new_depth=1, max_depth=3, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert narrowed["may_delegate"] is False
    assert narrowed["may_fan_out"] is False


def test_depth_provenance_follows_explicit_request_through_three_levels():
    root = build_task_contract({"delegation_budget": {"depth_remaining": 3}})
    depth_one = child_budget_for_schedule(
        root, current_depth=0, new_depth=1, max_depth=3, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert depth_one["depth_remaining"] == 2
    assert depth_one["depth_provenance"] == {
        "requested_depth": 3, "permitted_depth": 3,
        "attempted_depth": 1, "achieved_depth": None,
    }
    depth_two = child_budget_for_schedule(
        {"delegation_budget": depth_one}, current_depth=1, new_depth=2,
        max_depth=3, may_mutate=False, may_fan_out=True, max_children=0,
        intent_note="",
    )
    assert depth_two["depth_remaining"] == 1
    assert depth_two["depth_provenance"]["requested_depth"] == 3
    assert depth_two["depth_provenance"]["attempted_depth"] == 2
    depth_three = child_budget_for_schedule(
        {"delegation_budget": depth_two}, current_depth=2, new_depth=3,
        max_depth=3, may_mutate=False, may_fan_out=True, max_children=0,
        intent_note="",
    )
    assert depth_three["depth_remaining"] == 0
    assert depth_three["may_delegate"] is False
    assert depth_three["depth_provenance"] == {
        "requested_depth": 3, "permitted_depth": 3,
        "attempted_depth": 3, "achieved_depth": None,
    }


def test_depth_permission_and_remaining_never_widen_after_settings_raise():
    root = build_task_contract({"delegation_budget": {"depth_remaining": 3}})
    depth_one = child_budget_for_schedule(
        root, current_depth=0, new_depth=1, max_depth=2, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert depth_one["depth_remaining"] == 1
    assert depth_one["depth_provenance"]["permitted_depth"] == 2

    depth_two = child_budget_for_schedule(
        {"delegation_budget": depth_one}, current_depth=1, new_depth=2,
        max_depth=7, may_mutate=False, may_fan_out=True, max_children=0,
        intent_note="",
    )
    assert depth_two["depth_remaining"] == 0
    assert depth_two["may_delegate"] is False
    assert depth_two["depth_provenance"] == {
        "requested_depth": 3, "permitted_depth": 2,
        "attempted_depth": 2, "achieved_depth": None,
    }


def test_assignment_preserves_admitted_depth_authority_and_only_adds_achievement():
    contract = build_task_contract({
        "delegation_budget": {
            "depth_remaining": 1,
            "depth_provenance": {
                "requested_depth": 3,
                "permitted_depth": 2,
                "attempted_depth": 1,
                "achieved_depth": None,
            },
        },
    })
    task = {"depth": 1, "task_contract": contract, "metadata": {}}
    fields = stamp_task_assignment_depth(task, max_depth=7)
    assert fields["depth_provenance"] == {
        "requested_depth": 3, "permitted_depth": 2,
        "attempted_depth": 1, "achieved_depth": 1,
    }
    assert task["metadata"]["depth_provenance"] == fields["depth_provenance"]


def test_assignment_does_not_reconstruct_legacy_depth_authority_from_live_settings():
    contract = build_task_contract({
        "delegation_budget": {"depth_remaining": 2},
    })
    task = {"depth": 1, "task_contract": contract, "metadata": {}}

    fields = stamp_task_assignment_depth(task, max_depth=7)

    assert fields["depth_provenance"] == {
        "requested_depth": None,
        "permitted_depth": None,
        "attempted_depth": 1,
        "achieved_depth": 1,
    }


def test_supervisor_ingress_bounds_legacy_permission_by_admitted_remaining_envelope():
    contract = build_task_contract({
        "delegation_budget": {"depth_remaining": 2},
    })

    stamped, provenance = stamp_depth_provenance(
        contract,
        attempted_depth=1,
        max_depth=7,
        achieved_depth=None,
    )

    assert provenance == {
        "requested_depth": None,
        "permitted_depth": 3,
        "attempted_depth": 1,
        "achieved_depth": None,
    }
    assert stamped["delegation_budget"]["depth_provenance"] == provenance


def test_supervisor_ingress_records_explicit_root_depth_request(tmp_path, monkeypatch):
    from supervisor import events

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    contract = build_task_contract({
        "delegation_budget": {"depth_remaining": 3},
    })
    event = _schedule_event("root", "", depth=0, drive_root=tmp_path)
    event.update({
        "type": "schedule_task",
        "chat_id": 1,
        "delegation_role": "root",
        "root_task_id": "root",
        "task_contract": contract,
    })
    enqueued = []

    events._handle_schedule_task(event, _fake_ctx(tmp_path, enqueued))

    assert len(enqueued) == 1
    assert enqueued[0]["task_contract"]["delegation_budget"]["depth_provenance"] == {
        "requested_depth": 3,
        "permitted_depth": 3,
        "attempted_depth": 0,
        "achieved_depth": None,
    }


def test_legacy_budget_reports_unknown_request_but_current_permission():
    budget = child_budget_for_schedule(
        {}, current_depth=0, new_depth=1, max_depth=3, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert budget["depth_provenance"] == {
        "requested_depth": None, "permitted_depth": 3,
        "attempted_depth": 1, "achieved_depth": None,
    }

    # A legacy descendant's `depth_remaining` has already been narrowed by its
    # ancestors. It is not proof of the root's requested envelope.
    descendant = child_budget_for_schedule(
        {"delegation_budget": {"depth_remaining": 2}},
        current_depth=1, new_depth=2, max_depth=4, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert descendant["depth_provenance"] == {
        "requested_depth": None, "permitted_depth": 3,
        "attempted_depth": 2, "achieved_depth": None,
    }
    assert descendant["depth_remaining"] == 1


def test_legacy_contract_does_not_gain_provenance_during_recovery_normalization():
    # Existing delegated rows were fingerprinted without this additive projection.
    # Rebuilding such a row after restart must preserve its canonical budget shape;
    # only an explicitly authored projection is normalized into the frozen contract.
    legacy = build_task_contract({"delegation_budget": {"depth_remaining": 2}})
    assert "depth_provenance" not in legacy["delegation_budget"]
    recovered = build_task_contract({"task_contract": legacy})
    assert recovered["delegation_budget"] == legacy["delegation_budget"]
    explicit = build_task_contract({
        "delegation_budget": {
            "depth_remaining": 2,
            "depth_provenance": {"requested_depth": 3, "attempted_depth": 1},
        },
    })
    assert explicit["delegation_budget"]["depth_provenance"]["requested_depth"] == 3


def test_fresh_depth_default_increases_without_widening_active_cap(monkeypatch):
    from ouroboros.config import get_max_active_subagents_per_root, get_max_subagent_depth, get_max_workers

    monkeypatch.delenv("OUROBOROS_MAX_SUBAGENT_DEPTH", raising=False)
    monkeypatch.delenv("OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT", raising=False)
    assert get_max_subagent_depth() == 3
    assert get_max_active_subagents_per_root() == 6
    assert get_max_workers() == 10


def _schedule_event(task_id, parent_id, *, depth=1, drive_root=None):
    root = str(drive_root or "")
    return {
        "type": "schedule_subagent", "task_id": task_id,
        "objective": f"objective-{task_id}", "expected_output": "a result",
        "depth": depth, "parent_task_id": parent_id, "root_task_id": parent_id,
        "delegation_role": "subagent", "memory_mode": "forked", "drive_root": root,
        "child_drive_root": root, "budget_drive_root": root,
    }


def _fake_ctx(tmp_path, enqueued):
    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 0}

        def enqueue_task(self, task):
            enqueued.append(task)

        def persist_queue_snapshot(self, reason=""):
            return None

        def send_with_budget(self, *args, **kwargs):
            return None

    return FakeCtx()


def test_supervisor_admission_enforces_parent_rights_and_allows_one_non_fanout_child(tmp_path, monkeypatch):
    from supervisor import events

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    parent_contract = build_task_contract({"delegation_budget": {"may_fan_out": False}})
    write_task_result(tmp_path, "parent", STATUS_RUNNING, parent_task_id="", root_task_id="parent",
                      delegation_role="root", task_contract=parent_contract)
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)
    events._handle_schedule_task(_schedule_event("child-1", "parent", drive_root=tmp_path), ctx)
    assert [task["id"] for task in enqueued] == ["child-1"]
    queued = json.loads((tmp_path / "task_results" / "child-1.json").read_text(encoding="utf-8"))
    assert queued["depth_provenance"]["achieved_depth"] is None
    events._handle_schedule_task(_schedule_event("child-2", "parent", drive_root=tmp_path), ctx)
    rejected = json.loads((tmp_path / "task_results" / "child-2.json").read_text(encoding="utf-8"))
    assert len(enqueued) == 1
    assert rejected["reason_code"] == "delegation_rights_may_fan_out"


def test_supervisor_rolls_back_subagent_when_scheduled_result_write_fails(
    tmp_path, monkeypatch,
):
    from supervisor import events, task_admission

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    parent_contract = build_task_contract({"delegation_budget": {"may_fan_out": False}})
    write_task_result(
        tmp_path, "parent", STATUS_RUNNING,
        parent_task_id="", root_task_id="parent",
        delegation_role="root", task_contract=parent_contract,
    )
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)
    write_task_result(
        tmp_path, "child-1", "requested",
        parent_task_id="parent", root_task_id="parent",
        delegation_role="subagent", result="Awaiting supervisor acceptance.",
    )

    def enqueue_task(task):
        admitted = dict(task)
        enqueued.append(admitted)
        ctx.PENDING.append(admitted)
        return admitted

    ctx.enqueue_task = enqueue_task
    original_write = task_admission.write_task_result

    def fail_first_scheduled(root, task_id, status, **fields):
        if task_id == "child-1" and status == events.STATUS_SCHEDULED:
            raise OSError("simulated scheduled receipt failure")
        return original_write(root, task_id, status, **fields)

    monkeypatch.setattr(task_admission, "write_task_result", fail_first_scheduled)

    events._handle_schedule_task(
        _schedule_event("child-1", "parent", drive_root=tmp_path), ctx,
    )
    assert [task["id"] for task in enqueued] == ["child-1"]
    assert ctx.PENDING == []
    rejected_first = json.loads(
        (tmp_path / "task_results" / "child-1.json").read_text(encoding="utf-8")
    )
    assert rejected_first["status"] == "failed"
    assert rejected_first["reason_code"] == "scheduled_result_persist_failed"
    assert rejected_first["delegation_admission"]["status"] == "rejected"

    events._handle_schedule_task(
        _schedule_event("child-2", "parent", drive_root=tmp_path), ctx,
    )
    scheduled = json.loads(
        (tmp_path / "task_results" / "child-2.json").read_text(encoding="utf-8")
    )
    assert [task["id"] for task in enqueued] == ["child-1", "child-2"]
    assert [task["id"] for task in ctx.PENDING] == ["child-2"]
    assert scheduled["status"] == "scheduled"
    assert scheduled["delegation_admission"]["status"] == "accepted"
    assert scheduled["delegation_admission"]["direct_child_count"] == 0
    assert len(scheduled["delegation_admission"]["transition_id"]) == 32


def test_supervisor_receipt_rollback_removes_only_its_enqueue_identity(
    tmp_path, monkeypatch,
):
    from supervisor import events, task_admission

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    parent_contract = build_task_contract({"delegation_budget": {"may_fan_out": True}})
    write_task_result(
        tmp_path, "parent", STATUS_RUNNING,
        root_task_id="parent", delegation_role="root", task_contract=parent_contract,
    )
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)
    preexisting = {
        "id": "same-id",
        "root_task_id": "parent",
        "parent_task_id": "parent",
        "delegation_role": "subagent",
    }
    ctx.PENDING.append(preexisting)
    write_task_result(
        tmp_path,
        "same-id",
        "scheduled",
        parent_task_id="parent",
        root_task_id="parent",
        delegation_role="subagent",
        delegation_admission={
            "status": "accepted",
            "direct_child_count": 0,
            "transition_id": "old-transition",
        },
    )

    def enqueue_task(task):
        admitted = dict(task)
        enqueued.append(admitted)
        ctx.PENDING.append(admitted)
        return admitted

    ctx.enqueue_task = enqueue_task
    original_write = task_admission.write_task_result

    def fail_scheduled(root, task_id, status, **fields):
        if task_id == "same-id" and status == events.STATUS_SCHEDULED:
            raise OSError("simulated pre-commit failure")
        return original_write(root, task_id, status, **fields)

    monkeypatch.setattr(task_admission, "write_task_result", fail_scheduled)

    events._handle_schedule_task(
        _schedule_event("same-id", "parent", drive_root=tmp_path), ctx,
    )

    assert len(ctx.PENDING) == 1
    assert ctx.PENDING[0] is preexisting
    preserved = json.loads(
        (tmp_path / "task_results" / "same-id.json").read_text(encoding="utf-8")
    )
    assert preserved["status"] == "scheduled"
    assert preserved["delegation_admission"] == {
        "status": "accepted",
        "direct_child_count": 0,
        "transition_id": "old-transition",
    }


def test_supervisor_keeps_admission_when_scheduled_write_raises_after_commit(
    tmp_path, monkeypatch,
):
    from supervisor import events, task_admission

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    parent_contract = build_task_contract({"delegation_budget": {"may_fan_out": False}})
    write_task_result(
        tmp_path, "parent", STATUS_RUNNING,
        root_task_id="parent", delegation_role="root", task_contract=parent_contract,
    )
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)

    def enqueue_task(task):
        admitted = dict(task)
        enqueued.append(admitted)
        ctx.PENDING.append(admitted)
        return admitted

    ctx.enqueue_task = enqueue_task
    original_write = task_admission.write_task_result

    def commit_then_raise(root, task_id, status, **fields):
        stored = original_write(root, task_id, status, **fields)
        if task_id == "child" and status == events.STATUS_SCHEDULED:
            raise OSError("simulated post-commit observer failure")
        return stored

    monkeypatch.setattr(task_admission, "write_task_result", commit_then_raise)

    events._handle_schedule_task(
        _schedule_event("child", "parent", drive_root=tmp_path), ctx,
    )

    assert [task["id"] for task in ctx.PENDING] == ["child"]
    scheduled = json.loads(
        (tmp_path / "task_results" / "child.json").read_text(encoding="utf-8")
    )
    assert scheduled["status"] == "scheduled"
    assert scheduled["delegation_admission"]["status"] == "accepted"
    assert len(scheduled["delegation_admission"]["transition_id"]) == 32
    assert scheduled["delegation_admission"]["transition_id"] != "old-transition"


def test_supervisor_rolls_back_when_monotonic_writer_returns_old_terminal(
    tmp_path, monkeypatch,
):
    from supervisor import events

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    parent_contract = build_task_contract({"delegation_budget": {"may_fan_out": True}})
    write_task_result(
        tmp_path, "parent", STATUS_RUNNING,
        root_task_id="parent", delegation_role="root", task_contract=parent_contract,
    )
    write_task_result(
        tmp_path, "child", "completed",
        parent_task_id="parent", root_task_id="parent",
        delegation_role="subagent", result="old terminal child",
    )
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)

    def enqueue_task(task):
        admitted = dict(task)
        enqueued.append(admitted)
        ctx.PENDING.append(admitted)
        return admitted

    ctx.enqueue_task = enqueue_task
    events._handle_schedule_task(
        _schedule_event("child", "parent", drive_root=tmp_path), ctx,
    )

    assert ctx.PENDING == []
    terminal = json.loads(
        (tmp_path / "task_results" / "child.json").read_text(encoding="utf-8")
    )
    assert terminal["status"] == "completed"
    assert terminal["result"] == "old terminal child"


def test_supervisor_admission_enforces_may_delegate_false_even_when_stringified(tmp_path):
    from supervisor import events

    parent_contract = build_task_contract({"delegation_budget": {"may_delegate": "false"}})
    write_task_result(tmp_path, "parent", STATUS_RUNNING, root_task_id="parent",
                      delegation_role="root", task_contract=parent_contract)
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)
    events._handle_schedule_task(_schedule_event("child", "parent", drive_root=tmp_path), ctx)
    rejected = json.loads((tmp_path / "task_results" / "child.json").read_text(encoding="utf-8"))
    assert enqueued == []
    assert rejected["reason_code"] == "delegation_rights_may_delegate"


def test_direct_child_count_read_gap_is_typed_unknown(tmp_path):
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "corrupt-child.json").write_text("{not-json", encoding="utf-8")
    contract = build_task_contract({"delegation_budget": {"max_children": 1}})

    assert durable_direct_child_count(tmp_path, "parent") is None
    refusal = schedule_delegation_refusal(contract, tmp_path, "parent")
    assert "delegation_rights_child_count_unknown" in refusal


def test_supervisor_rejects_count_bounded_child_when_count_scan_fails(
    tmp_path, monkeypatch,
):
    from supervisor import events

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "ouroboros.task_results.list_task_results",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )
    parent_contract = build_task_contract({
        "delegation_budget": {"may_fan_out": False, "max_children": 1},
    })
    write_task_result(
        tmp_path, "parent", STATUS_RUNNING, root_task_id="parent",
        delegation_role="root", task_contract=parent_contract,
    )
    enqueued = []
    ctx = _fake_ctx(tmp_path, enqueued)

    events._handle_schedule_task(
        _schedule_event("child", "parent", drive_root=tmp_path), ctx,
    )

    rejected = json.loads(
        (tmp_path / "task_results" / "child.json").read_text(encoding="utf-8")
    )
    assert enqueued == []
    assert rejected["reason_code"] == "delegation_rights_child_count_unknown"
    assert rejected["delegation_admission"]["direct_child_count"] is None
