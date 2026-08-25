"""Causal owner-death reconciliation for process-local review workers."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Dict, Set


log = logging.getLogger(__name__)


def stamp_paid_review_owner(attempt: Any, *, paid: bool) -> None:
    """Bind a newly-paid attempt to the current process custody owner."""
    if not paid or int(getattr(attempt, "review_owner_pid", 0) or 0) > 0:
        return
    from ouroboros.process_custody import current_custody_session_id

    attempt.review_owner_session_id = str(current_custody_session_id() or "")
    attempt.review_owner_pid = os.getpid()


def _recoverable_review_invocations(drive_root: pathlib.Path) -> Dict[str, str]:
    """Unique durable delegated tokens keyed by their reserved operation."""
    from ouroboros import delegate_custody

    candidates: Dict[str, Set[str]] = {}
    records = [
        (record, str(record.get("invocation_id") or ""))
        for record in delegate_custody.pending_invocations(drive_root)
    ]
    for run in delegate_custody.open_runs(drive_root):
        token = str(getattr(run, "invocation_id", "") or "")
        record = delegate_custody.invocation_record(drive_root, token)
        if record is not None:
            records.append((record, token))
    for record, token in records:
        operation_id = str(record.get("operation_id") or "")
        if (
            operation_id
            and token
            and str(record.get("surface") or "")
            in {"multi_model_review", "scope_review"}
        ):
            candidates.setdefault(operation_id, set()).add(token)
    return {
        operation_id: next(iter(tokens))
        for operation_id, tokens in candidates.items()
        if len(tokens) == 1
    }


def reconcile_review_custody_on_process_start(
    drive_root: pathlib.Path,
) -> Dict[str, Any]:
    """Settle only review owners proven dead from an older server generation."""
    from ouroboros.platform_layer import pid_is_alive
    from ouroboros.process_custody import current_custody_session_id
    from ouroboros.review_state import _utc_now, update_state

    root = pathlib.Path(drive_root)
    current_session = str(current_custody_session_id() or "")
    recoverable = _recoverable_review_invocations(root)
    stamp = _utc_now()

    def _mutate(state: Any) -> Dict[str, Any]:
        dead_pids = {
            int(getattr(item, "review_owner_pid", 0) or 0)
            for item in state.attempts
            if str(getattr(item, "review_owner_session_id", "") or "")
            and str(getattr(item, "review_owner_session_id", "") or "")
            != current_session
            and int(getattr(item, "review_owner_pid", 0) or 0) > 0
            and not pid_is_alive(int(getattr(item, "review_owner_pid", 0) or 0))
        }
        reconciled = state.reconcile_process_local_review_custody_after_owner_loss(
            now_ts=stamp,
            recoverable_invocations=recoverable,
            confirmed_dead_owner_pids=dead_pids,
        )
        return {
            "reconciled": reconciled,
            "expired": state.expire_stale_attempts(now_ts=stamp),
        }

    return update_state(root, _mutate)


def reconcile_review_custody_after_confirmed_process_death(
    drive_root: pathlib.Path,
    owner_pid: int,
) -> Dict[str, Any]:
    """Settle tokenless review rows owned by one process confirmed dead."""
    from ouroboros.review_state import _utc_now, update_state

    pid = int(owner_pid or 0)
    if pid <= 0:
        return {"reconciled": [], "expired": []}
    root = pathlib.Path(drive_root)
    recoverable = _recoverable_review_invocations(root)
    stamp = _utc_now()

    def _mutate(state: Any) -> Dict[str, Any]:
        return {
            "reconciled": state.reconcile_process_local_review_custody_after_owner_loss(
                now_ts=stamp,
                recoverable_invocations=recoverable,
                confirmed_dead_owner_pids={pid},
            ),
            "expired": [],
        }

    return update_state(root, _mutate)


def reconcile_confirmed_dead_review_owner(
    drive_root: pathlib.Path,
    owner_pid: int,
) -> None:
    """Fail softly after a caller has proved this exact process dead."""
    pid = int(owner_pid or 0)
    if pid <= 0:
        return
    try:
        reconcile_review_custody_after_confirmed_process_death(drive_root, pid)
    except Exception:
        # State admission remains fail-closed if this write cannot complete; a
        # later retry sees the active paid attempt instead of buying a duplicate.
        log.warning(
            "Failed to reconcile review custody for dead worker pid %s",
            pid,
            exc_info=True,
        )
