"""Queue-custody regressions for malformed nested-task depth rows."""

from types import SimpleNamespace

import pytest


def test_assignment_skips_unresolved_invalid_depth_without_starving_healthy_rows(
    tmp_path, monkeypatch,
):
    from supervisor import queue, state, workers
    from ouroboros.task_results import load_task_result

    delivered = []

    class FakeWorkerQueue:
        def put(self, task):
            delivered.append(dict(task))

    worker = SimpleNamespace(wid=1, busy_task_id=None, reaping=False, in_q=FakeWorkerQueue())
    pending = [
        {
            "id": "unresolved-invalid-depth",
            "type": "task",
            "chat_id": 1,
            "description": "retry me",
            "depth": -1,
            "budget_drive_root": str(tmp_path),
        },
        {
            "id": "healthy-after-invalid-depth",
            "type": "task",
            "chat_id": 1,
            "description": "dispatch me",
            "depth": 0,
            "budget_drive_root": str(tmp_path),
        },
    ]
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {1: worker})
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(state, "budget_remaining", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(workers, "_emit_task_done_terminal", lambda *args, **kwargs: True)
    original_terminalize = workers._terminalize_invalid_pending_depth
    terminalization_attempts = []

    def fail_one_terminalization(task, detail):
        if task.get("id") == "unresolved-invalid-depth":
            terminalization_attempts.append(task["id"])
            if len(terminalization_attempts) == 1:
                return False
        return original_terminalize(task, detail)

    monkeypatch.setattr(workers, "_terminalize_invalid_pending_depth", fail_one_terminalization)
    queue.BUDGET_ROOT_FENCES.clear()

    workers.assign_tasks()

    assert [task["id"] for task in delivered] == ["healthy-after-invalid-depth"]
    assert [task["id"] for task in pending] == ["unresolved-invalid-depth"]
    assert worker.busy_task_id == "healthy-after-invalid-depth"

    worker.busy_task_id = None
    workers.RUNNING.clear()
    workers.assign_tasks()

    assert pending == []
    assert terminalization_attempts == ["unresolved-invalid-depth", "unresolved-invalid-depth"]
    assert load_task_result(tmp_path, "unresolved-invalid-depth")["reason_code"] == "invalid_task_depth"


@pytest.mark.parametrize("raw_value", [float("inf"), float("-inf"), float("nan"), "bad"])
def test_enqueue_normalizes_malformed_queue_order_metadata(raw_value, tmp_path, monkeypatch):
    from supervisor import queue

    pending, running = [], {}
    queue.init_queue_refs(pending, running, {"value": 0})
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", running)
    monkeypatch.setattr(queue, "ADMISSION_RESERVATIONS", {})

    admitted = queue.enqueue_task({
        "id": "malformed-order",
        "type": "task",
        "chat_id": 1,
        "depth": 0,
        "priority": raw_value,
        "_queue_seq": raw_value,
    })

    assert admitted["priority"] == 0
    assert admitted["_queue_seq"] == 1
    assert queue.enqueue_task({"id": "healthy-order", "type": "task", "depth": 0})["id"] == "healthy-order"


@pytest.mark.parametrize(
    ("evolution_block", "remaining"),
    [("Evolution is disabled in light runtime mode.", 100.0), ("", 0.1)],
)
def test_evolution_cleanup_preserves_unresolved_invalid_depth_rows(
    tmp_path, monkeypatch, evolution_block, remaining,
):
    from supervisor import evolution_lifecycle, queue, state, workers
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result

    task = {
        "id": "unresolved-evolution-depth",
        "type": "evolution",
        "chat_id": 1,
        "description": "retry malformed evolution",
        "depth": -1,
        "budget_drive_root": str(tmp_path),
    }
    sibling = {
        "id": "healthy-evolution-sibling",
        "type": "evolution",
        "chat_id": 1,
        "description": "policy-handled sibling",
        "depth": 0,
        "budget_drive_root": str(tmp_path),
    }
    pending = [task, sibling]
    worker = SimpleNamespace(wid=1, busy_task_id=None, reaping=False, in_q=SimpleNamespace(put=lambda _task: None))
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {1: worker})
    monkeypatch.setattr(workers, "load_state", lambda: {"owner_chat_id": 0})
    monkeypatch.setattr(state, "budget_remaining", lambda *_args, **_kwargs: remaining)
    monkeypatch.setattr(evolution_lifecycle, "evolution_block_reason", lambda: evolution_block)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(workers, "_terminalize_invalid_pending_depth", lambda *_args, **_kwargs: False)
    queue.BUDGET_ROOT_FENCES.clear()

    workers.assign_tasks()

    assert pending == [task]
    if evolution_block:
        assert load_task_result(tmp_path, sibling["id"])["status"] == STATUS_CANCELLED
    else:
        assert not (tmp_path / "task_results" / f"{sibling['id']}.json").exists()
    assert not (tmp_path / "task_results" / f"{task['id']}.json").exists()
