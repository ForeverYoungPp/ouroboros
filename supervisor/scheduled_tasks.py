"""Scheduled-task table CRUD, extracted from supervisor/queue.py (P7 module-size relief).

Owns the persisted scheduled-task table: path resolution, list/write, upsert,
remove, and the schedule-record -> task builder. ``sync_skill_schedules``,
``resync_skill_schedules`` and the due-check tick ``check_scheduled_tasks`` stay
in the queue module, which re-exports every historical name from here
(schedule_time.py precedent).

Cross-dependencies resolve LAZILY through ``supervisor.queue`` at call time,
never at module level: a module-level import would cycle (the queue module
imports this module for its re-exports), and a call-time
``from supervisor.queue import X`` reads the queue module's CURRENT attribute -
the test suite monkeypatches these names on the queue module
(``queue._write_scheduled_tasks`` et al.), so that surface and the process-wide
``_queue_lock`` survive the extraction unchanged. Stable SSOT utils helpers come
from ``ouroboros.utils`` directly.
"""

from __future__ import annotations

import pathlib
import uuid
from typing import Any, Dict

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso


SCHEDULED_TASKS_FILE = pathlib.Path("state") / "scheduled_tasks.json"


def _scheduled_tasks_path(drive_root: pathlib.Path | None = None) -> pathlib.Path:
    from supervisor.queue import DRIVE_ROOT  # lazy: the queue module re-exports this module

    return pathlib.Path(drive_root or DRIVE_ROOT) / SCHEDULED_TASKS_FILE


def list_scheduled_tasks(drive_root: pathlib.Path | None = None) -> Dict[str, Any]:
    """Return the persisted scheduled task table."""
    from supervisor.queue import _scheduled_tasks_path

    data = read_json_dict(_scheduled_tasks_path(drive_root)) or {}
    if not isinstance(data, dict):
        data = {}
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        data["tasks"] = []
    data.setdefault("schema_version", 1)
    return data


def _write_scheduled_tasks(data: Dict[str, Any], drive_root: pathlib.Path | None = None) -> None:
    from supervisor.queue import _scheduled_tasks_path

    path = _scheduled_tasks_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, trailing_newline=True)


def upsert_scheduled_task(record: Dict[str, Any], *, drive_root: pathlib.Path | None = None) -> Dict[str, Any]:
    """Create or replace a scheduled task record."""
    from supervisor.queue import (
        _queue_lock,
        _schedule_next_run,
        _write_scheduled_tasks,
        list_scheduled_tasks,
    )

    with _queue_lock:
        data = list_scheduled_tasks(drive_root)
        tasks = [item for item in data.get("tasks") or [] if isinstance(item, dict)]
        incoming = dict(record)
        schedule_id = str(incoming.get("id") or "").strip() or uuid.uuid4().hex[:8]
        incoming["id"] = schedule_id
        incoming.setdefault("enabled", True)
        incoming.setdefault("created_at", utc_now_iso())
        incoming["updated_at"] = utc_now_iso()
        if not incoming.get("next_run_at"):
            incoming["next_run_at"] = _schedule_next_run(incoming)
        tasks = [item for item in tasks if str(item.get("id") or "") != schedule_id]
        tasks.append(incoming)
        data["tasks"] = tasks
        _write_scheduled_tasks(data, drive_root)
        return incoming


def remove_scheduled_task(schedule_id: str, *, drive_root: pathlib.Path | None = None) -> bool:
    """Remove a scheduled task record by id."""
    from supervisor.queue import _queue_lock, _write_scheduled_tasks, list_scheduled_tasks

    wanted = str(schedule_id or "").strip()
    if not wanted:
        return False
    with _queue_lock:
        data = list_scheduled_tasks(drive_root)
        tasks = [item for item in data.get("tasks") or [] if isinstance(item, dict)]
        kept = [item for item in tasks if str(item.get("id") or "") != wanted]
        if len(kept) == len(tasks):
            return False
        data["tasks"] = kept
        _write_scheduled_tasks(data, drive_root)
        return True


def _task_from_schedule(record: Dict[str, Any]) -> Dict[str, Any]:
    from supervisor.queue import (
        RESERVED_TEMPLATE_FIELDS,
        build_task_contract,
        load_state,
        normalize_allowed_resources,
    )

    template = dict(record.get("task") or {})
    owner_chat_id = load_state().get("owner_chat_id") or 0
    task_id = uuid.uuid4().hex[:8]
    session_id = str(template.get("session_id") or f"schedule-{record.get('id') or task_id}")
    raw_metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
    metadata = {
        key: value for key, value in dict(raw_metadata).items()
        if key not in RESERVED_TEMPLATE_FIELDS
    }
    task = {
        "id": task_id,
        "type": "task",
        "text": str(template.get("text") or template.get("description") or record.get("description") or record.get("name") or "Scheduled task"),
        "description": str(template.get("description") or template.get("text") or record.get("description") or record.get("name") or "Scheduled task"),
        "chat_id": template.get("chat_id") if template.get("chat_id") not in (None, "") else owner_chat_id,
        "priority": int(template["priority"]) if str(template.get("priority") or "").strip().lstrip("-").isdigit() else None,
        "root_task_id": task_id,
        "session_id": session_id,
        "actor_id": "scheduler",
        "delegation_role": "root",
        "metadata": metadata,
    }
    for key in ("attachments", "context", "expected_output", "constraints", "deadline_at"):
        if key in template:
            task[key] = template[key]
    allowed_resources = normalize_allowed_resources(template.get("allowed_resources") or metadata.get("allowed_resources") or {})
    if allowed_resources:
        task["allowed_resources"] = allowed_resources
    existing_contract = template.get("task_contract") if isinstance(template.get("task_contract"), dict) else {}
    if existing_contract:
        task["task_contract"] = existing_contract
    task["task_contract"] = build_task_contract(task)
    task["metadata"]["schedule_id"] = str(record.get("id") or "")
    task["metadata"]["schedule_name"] = str(record.get("name") or "")
    task["metadata"]["schedule_trigger"] = dict(record.get("trigger") or {})
    task["metadata"]["task_contract"] = task["task_contract"]
    if allowed_resources:
        task["metadata"]["allowed_resources"] = allowed_resources
    if task.get("deadline_at"):
        task["metadata"]["deadline_at"] = task.get("deadline_at")
    task["metadata"].setdefault("source", "scheduled_task")
    return task
