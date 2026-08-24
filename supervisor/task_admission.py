"""Atomic admission transitions for the managed task queue."""

from __future__ import annotations

import logging
import pathlib
import uuid
from typing import Any, Dict

from ouroboros.task_results import (
    STATUS_REQUESTED,
    STATUS_SCHEDULED,
    load_task_result,
    write_task_result,
)

log = logging.getLogger(__name__)


def enqueue_subagent_with_scheduled_result(
    ctx: Any,
    task: Dict[str, Any],
    *,
    result_fields: Dict[str, Any],
    admitted_task_contract: Dict[str, Any],
    admitted_depth_provenance: Dict[str, Any],
    direct_child_count: Any,
    pending_ref: list[Any],
) -> tuple[Any, str, str, bool]:
    """Enqueue a child only together with its first durable authority row.

    Assignment takes the same queue RLock.  A pre-commit result failure can
    therefore remove this exact still-pending object before a worker observes
    it.  A late observer exception after atomic file replacement keeps the
    already-authoritative admission instead of compensating a committed row.
    """
    from supervisor import queue

    tid = str(task.get("id") or "")
    transition_id = uuid.uuid4().hex

    def _committed(record: Any) -> bool:
        admission = record.get("delegation_admission") if isinstance(record, dict) else None
        return bool(
            isinstance(record, dict)
            and str(record.get("status") or "") == STATUS_SCHEDULED
            and isinstance(admission, dict)
            and str(admission.get("status") or "") == "accepted"
            and str(admission.get("transition_id") or "") == transition_id
        )

    with queue._queue_lock:
        admitted = ctx.enqueue_task(task)
        if isinstance(admitted, dict) and admitted.get("_admission_blocked"):
            return admitted, "", "", False
        previous = load_task_result(ctx.DRIVE_ROOT, tid) or {}
        result_fields["task_contract"] = admitted_task_contract
        result_fields["depth_provenance"] = admitted_depth_provenance
        result_fields["delegation_admission"] = {
            "status": "accepted",
            "direct_child_count": direct_child_count,
            "transition_id": transition_id,
        }
        try:
            stored = write_task_result(
                ctx.DRIVE_ROOT,
                tid,
                STATUS_SCHEDULED,
                **result_fields,
                result="Subagent accepted and scheduled.",
            )
        except Exception:
            committed = load_task_result(ctx.DRIVE_ROOT, tid) or {}
            if _committed(committed):
                log.warning(
                    "Scheduled subagent write for %s raised after its accepted "
                    "receipt committed; keeping admission",
                    tid,
                    exc_info=True,
                )
                return admitted, "", "", False
            log.warning(
                "Failed to persist scheduled subagent status for %s; rolled back "
                "its exact queue admission",
                tid,
                exc_info=True,
            )
        else:
            if _committed(stored):
                return admitted, "", "", False
            log.warning(
                "Scheduled subagent status for %s did not commit this admission; "
                "rolling back its exact queue row",
                tid,
            )
        for index, row in enumerate(pending_ref):
            if row is admitted:
                pending_ref.pop(index)
                break
        current = load_task_result(ctx.DRIVE_ROOT, tid) or {}
        prior_status = str(previous.get("status") or "")
        current_status = str(current.get("status") or "")
        if any(
            status not in {"", STATUS_REQUESTED}
            for status in (prior_status, current_status)
        ):
            return (
                admitted,
                "scheduled_result_conflict",
                "Subagent not scheduled: another durable result already owns this "
                "task id, so the new queue admission was rolled back without "
                "overwriting that result.",
                False,
            )
        return (
            admitted,
            "scheduled_result_persist_failed",
            "Subagent not scheduled: its durable scheduled-result receipt could not "
            "be persisted, so queue admission was rolled back.",
            True,
        )


def reserve_task_admission(
    task_id: str,
    admission_token: str,
    *,
    require_worker_pool: bool = False,
    drive_root: Any = None,
    worker_pool: Any = None,
) -> Dict[str, Any]:
    """Atomically reserve one fresh user-ingress id before side effects."""
    from supervisor import queue

    tid = str(task_id or "").strip()
    token = str(admission_token or "").strip()
    if not tid or not token:
        return {"status": "blocked", "reason": "invalid_admission_reservation"}
    with queue._queue_lock:
        reserved = queue.ADMISSION_RESERVATIONS.get(tid)
        if reserved:
            if reserved == token:
                return {"status": "already_reserved", "reason": ""}
            return {"status": "blocked", "reason": "duplicate_task_id"}
        if tid in queue.RUNNING or any(
            isinstance(row, dict) and str(row.get("id") or "") == tid
            for row in queue.PENDING
        ):
            return {"status": "blocked", "reason": "duplicate_task_id"}
        try:
            from ouroboros.task_results import load_task_result

            existing = load_task_result(
                pathlib.Path(drive_root or queue.DRIVE_ROOT), tid
            ) or {}
        except Exception:
            return {"status": "blocked", "reason": "task_id_lookup_failed"}
        if existing:
            admission = existing.get("promotion_admission")
            if (
                isinstance(admission, dict)
                and str(admission.get("routing_token") or "") == token
            ):
                return {
                    "status": "existing_same_token",
                    "reason": "",
                    "task_status": str(existing.get("status") or ""),
                    "promotion_admission": dict(admission),
                }
            return {"status": "blocked", "reason": "duplicate_task_id"}
        if require_worker_pool:
            try:
                from supervisor import workers

                disabled_reason = str(workers._WORKER_POOL_DISABLED_REASON or "")
                pool = workers.WORKERS if worker_pool is None else worker_pool
                worker_count = len(pool)
            except Exception:
                return {"status": "blocked", "reason": "worker_pool_state_unavailable"}
            if disabled_reason or worker_count <= 0:
                return {
                    "status": "blocked",
                    "reason": "worker_pool_unavailable",
                    "worker_pool_disabled_reason": disabled_reason or "no_workers",
                }
        queue.ADMISSION_RESERVATIONS[tid] = token
        return {"status": "reserved", "reason": ""}


def release_task_admission(task_id: str, admission_token: str) -> bool:
    """Release only the reservation owned by the supplied token."""
    from supervisor import queue

    tid = str(task_id or "").strip()
    token = str(admission_token or "").strip()
    with queue._queue_lock:
        if queue.ADMISSION_RESERVATIONS.get(tid) != token:
            return False
        queue.ADMISSION_RESERVATIONS.pop(tid, None)
        return True


__all__ = [
    "enqueue_subagent_with_scheduled_result",
    "release_task_admission",
    "reserve_task_admission",
]
