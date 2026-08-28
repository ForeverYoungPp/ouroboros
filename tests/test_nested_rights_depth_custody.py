"""Queue-custody regressions for malformed nested-task depth rows."""

import json
from types import SimpleNamespace

import pytest


def test_terminalization_retry_rows_prefer_one_marker_per_task_id():
    from supervisor.task_admission import prefer_terminalization_retry_rows

    marker = {"id": "same-task", "_terminalization_retry": {"status": "failed"}}
    ordinary = {"id": "same-task", "depth": 0}
    assert prefer_terminalization_retry_rows([ordinary, marker, dict(marker)]) == [marker]


def test_terminalization_retry_status_normalizes_unknown_value():
    from supervisor import workers

    spec = workers._terminalization_retry_spec({
        "_terminalization_retry": {"status": "completed"},
    })
    assert spec["status"] == "failed"


def test_interrupted_terminalization_retry_is_finalized_after_worker_boot(
    tmp_path, monkeypatch,
):
    """A post-boot retry must not leave a retry-less interrupted result wedged."""
    from supervisor import workers
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.utils import append_jsonl, utc_now_iso

    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "queue_snapshot.json").write_text(
        '{"pending": [], "running": []}', encoding="utf-8",
    )
    append_jsonl(tmp_path / "logs" / "events.jsonl", {
        "ts": utc_now_iso(), "type": "worker_boot",
    })

    pending = [{
        "id": "post-boot-interrupted",
        "chat_id": 0,
        "_terminalization_retry": {
            "status": "interrupted",
            "reason": "terminal event was unavailable during update",
            "trigger": "worker_pool_kill",
        },
    }]
    writes = []
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "_audit_delegate_terminal_custody", lambda *a, **k: None)

    def write_retry(task_id, *, reason, status):
        writes.append({"reason": reason, "status": status})
        write_task_result(tmp_path, task_id, status, result=reason)
        return status

    monkeypatch.setattr(workers, "_write_failure_result", write_retry)
    monkeypatch.setattr(workers, "_emit_task_done_terminal", lambda *a, **k: True)

    assert workers._retry_terminalization_pending() == (
        ["post-boot-interrupted"], [],
    )
    assert pending == []
    assert writes[0]["status"] == "failed"
    assert "Original shutdown status was interrupted" in writes[0]["reason"]
    assert load_task_result(tmp_path, "post-boot-interrupted")["status"] == "failed"
    assert load_effective_task_result(tmp_path, "post-boot-interrupted")["status"] == "failed"


def test_terminalization_retry_restore_survives_durable_result_until_event_published(
    tmp_path, monkeypatch,
):
    """A durable outcome must not discard its still-unpublished terminal event."""
    from ouroboros.task_results import STATUS_FAILED, load_task_result, write_task_result
    from ouroboros.utils import utc_now_iso
    from supervisor import queue, state, workers

    pending, running = [], {}
    counter = {"value": 0}
    queue.init_queue_refs(pending, running, counter)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    snapshot_path = tmp_path / "state" / "queue_snapshot.json"
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snapshot_path)
    queue.ACCEPTANCE_FENCES.clear()
    queue.ADMISSION_RESERVATIONS.clear()

    task_id = "durable-terminal-event-retry"
    task = {
        "id": task_id,
        "type": "task",
        "chat_id": 0,
        "depth": -1,
        "_terminalization_retry": {
            "status": STATUS_FAILED,
            "reason": "terminal event was not published before shutdown",
            "trigger": "worker_pool_kill",
        },
    }
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps({
            "ts": utc_now_iso(),
            "pending": [{"id": task_id, "queue_seq": 1, "task": task}],
            "running": [],
            "acceptance_fences": [],
        }),
        encoding="utf-8",
    )
    # This is the failure window: the result write succeeded, but task_done did not.
    write_task_result(tmp_path, task_id, STATUS_FAILED, result="already durable")

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    assert queue.restore_pending_from_snapshot() == 1
    assert [row["id"] for row in pending] == [task_id]
    assert pending[0]["_terminalization_retry"]["status"] == STATUS_FAILED

    writes, emitted = [], []
    delivered = []
    worker = SimpleNamespace(
        wid=1,
        busy_task_id=None,
        reaping=False,
        in_q=SimpleNamespace(put=lambda row: delivered.append(row)),
    )
    monkeypatch.setattr(workers, "WORKERS", {1: worker})
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(state, "budget_remaining", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    queue.BUDGET_ROOT_FENCES.clear()
    monkeypatch.setattr(workers, "_audit_delegate_terminal_custody", lambda *a, **k: None)
    monkeypatch.setattr(
        workers,
        "_write_failure_result",
        lambda tid, *, reason, status: writes.append((tid, reason, status)) or STATUS_FAILED,
    )
    publish_results = iter([False, True])
    monkeypatch.setattr(
        workers,
        "_emit_task_done_terminal",
        lambda task_row, tid, status: emitted.append((tid, status)) or next(publish_results),
    )

    # A failed publication keeps custody; assign_tasks must not drop the row
    # merely because its outcome is already terminal.
    workers.assign_tasks()
    assert [row["id"] for row in pending] == [task_id]
    assert delivered == []
    workers.assign_tasks()
    assert pending == []
    assert worker.busy_task_id is None
    assert writes[0][0] == writes[1][0] == task_id
    assert emitted == [(task_id, STATUS_FAILED), (task_id, STATUS_FAILED)]
    assert load_task_result(tmp_path, task_id)["status"] == STATUS_FAILED


def test_restore_failed_depth_terminalization_preserves_snapshot_order(
    tmp_path, monkeypatch,
):
    from supervisor import queue, task_admission
    from ouroboros.utils import utc_now_iso

    pending, running = [], {}
    counter = {"value": 0}
    queue.init_queue_refs(pending, running, counter)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    snapshot_path = tmp_path / "state" / "queue_snapshot.json"
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snapshot_path)
    queue.ACCEPTANCE_FENCES.clear()
    queue.ADMISSION_RESERVATIONS.clear()
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps({
            "ts": utc_now_iso(),
            "pending": [
                {
                    "id": "healthy-before-1", "priority": 0, "queue_seq": 10,
                    "task": {"id": "healthy-before-1", "type": "task", "chat_id": 1, "depth": 0},
                },
                {
                    "id": "healthy-before-2", "priority": 0, "queue_seq": 20,
                    "task": {"id": "healthy-before-2", "type": "task", "chat_id": 1, "depth": 0},
                },
                {
                    "id": "invalid-last", "priority": 0, "queue_seq": 30,
                    "task": {"id": "invalid-last", "type": "task", "chat_id": 1, "depth": -1},
                },
            ],
            "running": [],
            "acceptance_fences": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        task_admission,
        "terminalize_invalid_depth_restore",
        lambda *_args, **_kwargs: False,
    )

    assert queue.restore_pending_from_snapshot() == 2
    assert [row["id"] for row in pending] == [
        "healthy-before-1", "healthy-before-2", "invalid-last",
    ]
    assert [row["_queue_seq"] for row in pending] == [1, 2, 3]
    assert counter["value"] == 3


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


@pytest.mark.parametrize("marker_first", [True, False])
def test_terminalization_retry_snapshot_bypasses_depth_gate_and_is_not_dispatchable(
    tmp_path, monkeypatch, marker_first,
):
    from supervisor import queue, state, workers
    from ouroboros.utils import utc_now_iso

    pending, running = [], {}
    counter = {"value": 0}
    queue.init_queue_refs(pending, running, counter)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    snapshot_path = tmp_path / "state" / "queue_snapshot.json"
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snapshot_path)
    queue.ACCEPTANCE_FENCES.clear()
    queue.ADMISSION_RESERVATIONS.clear()
    task = {
        "id": "shutdown-retry",
        "type": "task",
        "chat_id": 0,
        "depth": -1,
        "_terminalization_retry": {
            "reason": "shutdown write was not durable",
            "status": "failed",
            "trigger": "worker_pool_kill",
        },
    }
    snapshot_path.parent.mkdir(parents=True)
    ordinary = {
        "id": task["id"], "type": "task", "chat_id": 0, "depth": 0,
    }
    marker_row = {"id": task["id"], "queue_seq": 7, "task": task}
    ordinary_row = {"id": task["id"], "queue_seq": 8, "task": ordinary}
    snapshot_rows = [marker_row, ordinary_row] if marker_first else [ordinary_row, marker_row]
    snapshot_path.write_text(
        json.dumps({
            "ts": utc_now_iso(),
            "pending": snapshot_rows,
            "running": [],
            "acceptance_fences": [],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    assert queue.restore_pending_from_snapshot() == 1
    assert pending[0]["depth"] == -1
    assert pending[0]["_terminalization_retry"]["status"] == "failed"
    assert pending[0]["_queue_seq"] == 1

    delivered = []
    worker = SimpleNamespace(
        wid=1,
        busy_task_id=None,
        reaping=False,
        in_q=SimpleNamespace(put=lambda row: delivered.append(row)),
    )
    monkeypatch.setattr(workers, "WORKERS", {1: worker})
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(state, "budget_remaining", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(workers, "_audit_delegate_terminal_custody", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workers, "_write_failure_result", lambda *_args, **_kwargs: "failed")
    monkeypatch.setattr(workers, "_emit_task_done_terminal", lambda *_args, **_kwargs: True)
    queue.BUDGET_ROOT_FENCES.clear()

    workers.assign_tasks()

    assert pending == []
    assert delivered == []
    assert worker.busy_task_id is None


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
