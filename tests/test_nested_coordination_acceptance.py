"""Production-shaped deterministic acceptance for host-visible depth-three trees."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from ouroboros import task_tree_ledger
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.contracts.task_contract import build_task_contract
from ouroboros.headless import copy_child_task_result
from ouroboros.depth_evidence import build_depth_summary
from ouroboros.outcome_receipt_store import merge_verification_receipts
from ouroboros.outcomes import latest_unreconciled_failed_receipt
from ouroboros.loop import (
    _task_acceptance_eligible,
    _task_acceptance_subtree_snapshot,
)
from ouroboros.review_evidence import build_task_acceptance_evidence
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    load_task_result,
    write_task_result,
)
from ouroboros.tools import control
from ouroboros.tools.registry import ToolContext
from supervisor import (
    events,
    queue as queue_module,
    state as state_module,
    workers,
)


def test_depth3_control_plane_reaches_root_acceptance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    settings = {
        "OUROBOROS_SUBAGENTS": json.dumps(
            {
                "enabled": True,
                "items": [
                    {
                        "subagent_id": "api-depth",
                        "name": "Depth actor",
                        "recommended_use": (
                            "Deterministic nested control-plane fixture."
                        ),
                        "route": {
                            "kind": "api_model",
                            "target_id": "openai/gpt-5.6-sol",
                        },
                        "effort": "high",
                    }
                ],
            }
        )
    }

    pending = []
    running = {}
    delivered = []
    emitted = []

    class WorkerQueue:
        def put(self, task):
            # Freeze what the worker really received. A shallow alias could make
            # later mutation hide an assignment/copy-back regression.
            delivered.append(copy.deepcopy(task))

    worker_map = {
        worker_id: SimpleNamespace(
            wid=worker_id,
            busy_task_id=None,
            reaping=False,
            in_q=WorkerQueue(),
        )
        for worker_id in (1, 2, 3)
    }

    class SupervisorContext:
        DRIVE_ROOT = tmp_path
        PENDING = pending
        RUNNING = running
        WORKERS = worker_map

        def load_state(self):
            return {}

        def enqueue_task(self, task):
            pending.append(task)
            return task

        def persist_queue_snapshot(self, reason=""):
            return None

        def send_with_budget(self, *_args, **_kwargs):
            return None

    supervisor_context = SupervisorContext()

    class ImmediateEventQueue:
        def put_nowait(self, event):
            # Preserve the real schedule_subagent event construction. Only the
            # asynchronous transport is collapsed into this deterministic call.
            emitted.append(copy.deepcopy(event))
            events._handle_schedule_task(event, supervisor_context)

    event_queue = ImmediateEventQueue()

    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "3")
    monkeypatch.setenv("OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT", "6")
    monkeypatch.setattr(control, "load_settings", lambda: settings)
    monkeypatch.setattr(
        events,
        "_find_duplicate_task",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(task_tree_ledger, "DATA_DIR", tmp_path)

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    monkeypatch.setattr(workers, "WORKERS", worker_map)
    monkeypatch.setattr(workers, "load_state", lambda: {})

    monkeypatch.setattr(
        state_module,
        "budget_remaining",
        lambda *_args, **_kwargs: 100.0,
    )
    monkeypatch.setattr(
        queue_module,
        "persist_queue_snapshot",
        lambda reason="": None,
    )
    monkeypatch.setattr(queue_module, "BUDGET_ROOT_FENCES", {})

    root_contract = build_task_contract(
        {
            "delegation_budget": {
                "depth_remaining": 3,
            }
        }
    )
    write_task_result(
        tmp_path,
        "root",
        STATUS_RUNNING,
        root_task_id="root",
        delegation_role="root",
        task_contract=root_contract,
    )

    def actor_context(
        task_id,
        depth,
        task_contract,
        drive_root,
        task=None,
    ):
        ctx = ToolContext(
            repo_dir=repo,
            drive_root=Path(drive_root),
            budget_drive_root=str(tmp_path),
        )
        ctx.task_id = task_id
        ctx.task_depth = depth
        ctx.event_queue = event_queue
        ctx.task_contract = task_contract

        if task is not None:
            ctx.task_constraint = normalize_task_constraint(
                task.get("task_constraint")
            )

        ctx.task_metadata = {
            "root_task_id": "root",
            "parent_task_id": str(
                (task or {}).get("parent_task_id") or ""
            ),
            "delegation_role": (
                "root" if depth == 0 else "subagent"
            ),
            "budget_drive_root": str(tmp_path),
            "task_contract": task_contract,
            "configured_subagent": copy.deepcopy(
                (task or {}).get("configured_subagent") or {}
            ),
        }
        return ctx

    def schedule_and_assign(
        parent_id,
        parent_depth,
        parent_contract,
        parent_drive,
        parent_task=None,
    ):
        previous_event_count = len(emitted)
        previous_delivery_count = len(delivered)

        result = control._schedule_task(
            actor_context(
                parent_id,
                parent_depth,
                parent_contract,
                parent_drive,
                parent_task,
            ),
            subagent_id="api-depth",
            objective=f"depth-{parent_depth + 1}",
            expected_output="typed handoff",
            memory_mode="empty",
        )

        assert "Subagent request queued" in result
        assert len(emitted) == previous_event_count + 1

        task_id = emitted[-1]["task_id"]
        admitted = load_task_result(tmp_path, task_id)

        assert admitted["status"] == STATUS_SCHEDULED
        assert (
            admitted["depth_provenance"]["achieved_depth"]
            is None
        )

        workers.assign_tasks()

        assert len(delivered) == previous_delivery_count + 1
        assigned = delivered[-1]
        assert assigned["id"] == task_id
        return assigned

    l1 = schedule_and_assign(
        "root",
        0,
        root_contract,
        tmp_path,
    )
    l2 = schedule_and_assign(
        l1["id"],
        1,
        l1["task_contract"],
        l1["drive_root"],
        l1,
    )
    l3 = schedule_and_assign(
        l2["id"],
        2,
        l2["task_contract"],
        l2["drive_root"],
        l2,
    )

    assert [task["depth"] for task in (l1, l2, l3)] == [1, 2, 3]
    assert [
        task["parent_task_id"] for task in (l1, l2, l3)
    ] == [
        "root",
        l1["id"],
        l2["id"],
    ]
    assert {
        task["root_task_id"] for task in (l1, l2, l3)
    } == {"root"}

    for depth, task in enumerate((l1, l2, l3), start=1):
        expected_provenance = {
            "requested_depth": 3,
            "permitted_depth": 3,
            "attempted_depth": depth,
            "achieved_depth": depth,
        }

        assert task["depth_provenance"] == expected_provenance
        assert (
            task["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
            == expected_provenance
        )
        assert (
            task["task_contract"]["delegation_budget"][
                "depth_remaining"
            ]
            == 3 - depth
        )

        canonical = load_task_result(tmp_path, task["id"])
        assert canonical["status"] == STATUS_RUNNING
        assert canonical["depth_provenance"] == expected_provenance
        assert (
            canonical["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
            == expected_provenance
        )

    assert (
        l3["task_contract"]["delegation_budget"]["may_delegate"]
        is False
    )

    def terminal_copy(task):
        write_task_result(
            Path(task["drive_root"]),
            task["id"],
            STATUS_COMPLETED,
            parent_task_id=task["parent_task_id"],
            root_task_id="root",
            delegation_role="subagent",
            task_contract=task["task_contract"],
            depth_provenance=task["depth_provenance"],
            result=f"done-{task['id']}",
        )

        assert copy_child_task_result(tmp_path, task) is not None

        canonical = load_task_result(tmp_path, task["id"])
        assert canonical["status"] == STATUS_COMPLETED
        assert canonical["depth_provenance"] == (
            canonical["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
        )

    terminal_copy(l3)

    root_context = actor_context(
        "root",
        0,
        root_contract,
        tmp_path,
    )
    quiescent, early_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )

    assert quiescent is False
    assert {
        row["task_id"]: row["status"]
        for row in early_rows
    } == {
        l1["id"]: STATUS_RUNNING,
        l2["id"]: STATUS_RUNNING,
        l3["id"]: STATUS_COMPLETED,
    }

    terminal_copy(l2)
    terminal_copy(l1)

    # The queue-owned acceptance fence is a separate liveness authority. Even
    # terminal task-result replicas cannot prove quiescence while the supervisor
    # still reports physical descendants as running.
    root_context._task_acceptance_queue_descendants = [
        {"task_id": task["id"], "status": STATUS_RUNNING}
        for task in (l1, l2, l3)
    ]
    quiescent, queue_live_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )
    assert quiescent is False
    assert sum(row.get("source") == "supervisor_queue" for row in queue_live_rows) == 3

    running.clear()
    for worker in worker_map.values():
        worker.busy_task_id = None
    root_context._task_acceptance_queue_descendants = []

    quiescent, terminal_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )

    assert quiescent is True
    assert {
        row["task_id"] for row in terminal_rows
    } == {
        l1["id"],
        l2["id"],
        l3["id"],
    }
    assert all(
        row["status"] == STATUS_COMPLETED
        for row in terminal_rows
    )
    assert all(
        len(row["child_result_sha256"]) == 64
        for row in terminal_rows
    )
    assert sorted(
        row["depth_provenance"]["achieved_depth"] for row in terminal_rows
    ) == [1, 2, 3]

    # Keep this test independent from the checkout's own dirty/staged diff.
    # The acceptance packet itself remains the real production builder.
    import ouroboros.review_evidence as review_evidence

    monkeypatch.setattr(
        review_evidence,
        "collect_turn_diff",
        lambda *_args, **_kwargs: "",
    )

    packet = build_task_acceptance_evidence(
        root_context,
        drive_root=tmp_path,
        task_id="root",
        canonical_subject="root synthesis",
        subtree_statuses=terminal_rows,
    )

    assert packet["terminal_subtree_statuses"] == terminal_rows
    assert (
        packet["__provenance__"]["terminal_subtree_statuses"]
        == "host_attested"
    )
    assert packet["depth_summary"] == {
        "requested_depth": 3,
        "permitted_depth": 3,
        "attempted_depth": 3,
        "achieved_depth": 3,
        "status": "achieved",
        "host_visible_only": True,
    }
    assert packet["__provenance__"]["depth_summary"] == "host_attested"

    assert _task_acceptance_eligible(
        "required",
        {},
        False,
        is_root_task=False,
    ) == (
        False,
        "skipped_child_advisory",
    )
    assert _task_acceptance_eligible(
        "required",
        {},
        False,
        is_root_task=True,
    )[0] is True


def test_real_over_cap_refusal_reaches_root_acceptance_depth_summary(tmp_path, monkeypatch):
    import ouroboros.review_evidence as review_evidence

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "2")
    monkeypatch.setattr(
        control,
        "load_settings",
        lambda: {
            "OUROBOROS_SUBAGENTS": json.dumps({
                "enabled": True,
                "items": [{
                    "subagent_id": "api-depth",
                    "name": "Depth actor",
                    "recommended_use": "Typed depth refusal fixture.",
                    "route": {
                        "kind": "api_model",
                        "target_id": "openai/gpt-5.6-sol",
                    },
                    "effort": "high",
                }],
            }),
        },
    )
    root_contract = build_task_contract({
        "delegation_budget": {"depth_remaining": 3},
    })
    parent_provenance = {
        "requested_depth": 3,
        "permitted_depth": 2,
        "attempted_depth": 2,
        "achieved_depth": 2,
    }
    parent_contract = build_task_contract({
        "parent_task_id": "depth-one",
        "root_task_id": "root",
        "delegation_role": "subagent",
        "delegation_budget": {
            "may_delegate": False,
            "depth_remaining": 0,
            "depth_provenance": parent_provenance,
        },
    })
    write_task_result(
        tmp_path,
        "depth-one",
        STATUS_COMPLETED,
        parent_task_id="root",
        root_task_id="root",
        delegation_role="subagent",
        depth_provenance={
            **parent_provenance,
            "attempted_depth": 1,
            "achieved_depth": 1,
        },
        result="depth one complete",
    )
    write_task_result(
        tmp_path,
        "depth-two",
        STATUS_RUNNING,
        parent_task_id="depth-one",
        root_task_id="root",
        delegation_role="subagent",
        task_contract=parent_contract,
        depth_provenance=parent_provenance,
    )
    parent_ctx = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
    )
    parent_ctx.task_id = "depth-two"
    parent_ctx.task_depth = 2
    parent_ctx.task_contract = parent_contract
    parent_ctx.task_metadata = {
        "root_task_id": "root",
        "parent_task_id": "depth-one",
        "delegation_role": "subagent",
        "budget_drive_root": str(tmp_path),
        "task_contract": parent_contract,
    }

    refused = control._schedule_task(
        parent_ctx,
        subagent_id="api-depth",
        objective="attempt depth three",
        expected_output="typed refusal",
    )
    assert "subtask_depth_limit" in refused
    refused_id = refused.split("task_id=", 1)[1].split(";", 1)[0]
    assert load_task_result(tmp_path, refused_id)["status"] == STATUS_FAILED
    write_task_result(
        tmp_path,
        "depth-two",
        STATUS_COMPLETED,
        parent_task_id="depth-one",
        root_task_id="root",
        delegation_role="subagent",
        task_contract=parent_contract,
        depth_provenance=parent_provenance,
        result="depth two complete",
    )

    root_ctx = SimpleNamespace(
        task_id="root",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_contract=root_contract,
        task_metadata={
            "root_task_id": "root",
            "budget_drive_root": str(tmp_path),
            "task_contract": root_contract,
        },
        _task_acceptance_queue_descendants=[],
    )
    quiescent, statuses = _task_acceptance_subtree_snapshot(
        root_ctx, tmp_path, "root",
    )
    assert quiescent is True
    assert {row["task_id"] for row in statuses} == {
        "depth-one", "depth-two", refused_id,
    }
    monkeypatch.setattr(review_evidence, "collect_turn_diff", lambda *_a, **_k: "")
    packet = build_task_acceptance_evidence(
        root_ctx,
        drive_root=tmp_path,
        task_id="root",
        canonical_subject="root depth reduction",
        subtree_statuses=statuses,
    )
    assert packet["depth_summary"] == {
        "requested_depth": 3,
        "permitted_depth": 2,
        "attempted_depth": 3,
        "achieved_depth": 2,
        "status": "capability_reduced",
        "host_visible_only": True,
    }


def test_depth_summary_reports_lower_cap_as_typed_reduction(monkeypatch):
    # Live Settings may change after admission; persisted child provenance wins.
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "7")
    root_contract = build_task_contract({"delegation_budget": {"depth_remaining": 3}})
    statuses = [
        {
            "task_id": f"child-{depth}",
            "depth_provenance": {
                "requested_depth": 3,
                "permitted_depth": 2,
                "attempted_depth": depth,
                "achieved_depth": depth,
            },
        }
        for depth in (1, 2)
    ]

    assert build_depth_summary(root_contract, statuses) == {
        "requested_depth": 3,
        "permitted_depth": 2,
        "attempted_depth": 2,
        "achieved_depth": 2,
        "status": "capability_reduced",
        "host_visible_only": True,
    }


def test_depth_summary_is_order_independent_and_allows_chosen_shallower():
    root_contract = build_task_contract({
        "delegation_budget": {
            "depth_remaining": 3,
            "depth_provenance": {
                "requested_depth": 3,
                "permitted_depth": 3,
                "attempted_depth": 0,
                "achieved_depth": None,
            },
        },
    })
    mixed = [
        {
            "depth_provenance": {
                "requested_depth": 3, "permitted_depth": 3,
                "attempted_depth": 1, "achieved_depth": 1,
            },
        },
        {
            "depth_provenance": {
                "requested_depth": 3, "permitted_depth": 2,
                "attempted_depth": 2, "achieved_depth": 2,
            },
        },
    ]
    expected = {
        "requested_depth": 3, "permitted_depth": 2,
        "attempted_depth": 2, "achieved_depth": 2,
        "status": "capability_reduced", "host_visible_only": True,
    }
    assert build_depth_summary(root_contract, mixed) == expected
    assert build_depth_summary(root_contract, reversed(mixed)) == expected

    assert build_depth_summary(root_contract, [mixed[0]]) == {
        "requested_depth": 3, "permitted_depth": 3,
        "attempted_depth": 1, "achieved_depth": 1,
        "status": "chosen_shallower", "host_visible_only": True,
    }


def test_depth_summary_never_recomputes_missing_history_from_live_settings(monkeypatch):
    root_contract = build_task_contract({"delegation_budget": {"depth_remaining": 3}})
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "7")
    assert build_depth_summary(root_contract, []) == {
        "requested_depth": 3, "permitted_depth": None,
        "attempted_depth": 0, "achieved_depth": 0,
        "status": "evidence_unknown", "host_visible_only": True,
    }


def test_split_root_receipts_reconcile_in_host_timestamp_order():
    old_pass = {
        "criterion_id": "claim_1", "status": "pass",
        "contract_kind": "explicit_command", "ts": "2026-01-01T00:00:01+00:00",
    }
    new_fail = {
        "criterion_id": "claim_1", "status": "fail",
        "contract_kind": "explicit_command", "ts": "2026-01-01T00:00:02+00:00",
    }
    merged = merge_verification_receipts([new_fail], [old_pass])
    assert merged == [old_pass, new_fail]
    assert latest_unreconciled_failed_receipt(merged) == new_fail

    old_fail = {**new_fail, "ts": "2026-01-01T00:00:01+00:00"}
    new_pass = {**old_pass, "ts": "2026-01-01T00:00:02+00:00"}
    merged = merge_verification_receipts([new_pass], [old_fail])
    assert merged == [old_fail, new_pass]
    assert latest_unreconciled_failed_receipt(merged) is None
