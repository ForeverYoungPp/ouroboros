"""Focused typed admission and depth-provenance tests for nested delegation."""

import json
from types import SimpleNamespace

from ouroboros.contracts.task_contract import build_task_contract
from ouroboros.task_results import STATUS_RUNNING, write_task_result
from ouroboros.tools.control_delegation import check_delegation_admission, child_budget_for_schedule


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


def test_legacy_budget_reports_unknown_request_but_current_permission():
    budget = child_budget_for_schedule(
        {}, current_depth=0, new_depth=1, max_depth=3, may_mutate=False,
        may_fan_out=True, max_children=0, intent_note="",
    )
    assert budget["depth_provenance"] == {
        "requested_depth": None, "permitted_depth": 3,
        "attempted_depth": 1, "achieved_depth": None,
    }


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
