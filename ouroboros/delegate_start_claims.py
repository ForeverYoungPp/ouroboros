"""Atomic pre-transport claims for delegated actor and payload starts."""

from __future__ import annotations

import pathlib
import threading
from typing import Any, Callable, Dict, Tuple

from ouroboros import delegate_custody as custody


_PAYLOAD_CLAIM_LOCK = threading.Lock()


def claimed_start_request(
    drive: pathlib.Path, *, claim_target: str,
    payload_busy: Callable[[pathlib.Path, pathlib.Path], str],
    actor_ctx: Any = None, enforce_actor_idle: bool = False,
    **request_row: Any,
) -> Tuple[bool, Dict[str, Any]]:
    """Atomically claim a fresh actor start and optional payload target."""

    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
    )

    def _write_request() -> Tuple[bool, Dict[str, Any]]:
        if not claim_target:
            return custody.record_start_requested(drive, **request_row), {}
        lock_path = pathlib.Path(drive) / "state" / ".payload_delegation_claim.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _PAYLOAD_CLAIM_LOCK:
            fd = acquire_exclusive_file_lock(
                lock_path, timeout_sec=20.0, stale_sec=120.0,
            )
            if fd is None:
                return False, {
                    "reason": "payload_delegation_busy",
                    "holder": "payload claim lock unavailable",
                    "detail": "The payload start claim is currently held by another caller.",
                }
            try:
                holder = payload_busy(drive, pathlib.Path(claim_target))
                if holder:
                    return False, {
                        "reason": "payload_delegation_busy", "holder": holder,
                        "detail": (
                            "Another delegated run claimed this exact payload first. "
                            "Finish it before starting another assignment against the skill."
                        ),
                    }
                return custody.record_start_requested(drive, **request_row), {}
            finally:
                release_exclusive_file_lock(lock_path, fd)

    if not enforce_actor_idle:
        return _write_request()
    task_id = str(request_row.get("task_id") or "").strip()
    try:
        with custody.actor_decision_lock(drive, task_id):
            if custody.custody_log_unreadable(drive):
                return False, {
                    "reason": "replacement_custody_unknown",
                    "custody_log_unreadable": True,
                    "detail": "The host cannot read the actor's custody authority.",
                }
            from ouroboros.delegate_recovery import unsettled_start_ids

            blockers = unsettled_start_ids(drive, task_id)
            if any(blockers.values()):
                if claim_target:
                    holder = payload_busy(drive, pathlib.Path(claim_target))
                    if holder:
                        return False, {
                            "reason": "payload_delegation_busy",
                            "holder": holder,
                            "detail": (
                                "Another delegated run claimed this exact payload first. "
                                "Finish it before starting another assignment against the skill."
                            ),
                        }
                return False, {
                    "reason": "replacement_requires_settlement", **blockers,
                    "detail": (
                        "The actor gained an unsettled start/run or undisposed patch "
                        "before this fresh request could be claimed."
                    ),
                }
            if actor_ctx is not None:
                from ouroboros.subagent_bootstrap import _durable_zero_run_receipt

                gaps: set[str] = set()
                zero_run = _durable_zero_run_receipt(actor_ctx, gap_reasons=gaps)
                if zero_run:
                    return False, {
                        "reason": "zero_run_already_recorded",
                        "detail": (
                            "The actor recorded a terminal delegation_zero_run decision "
                            "before this physical start could be claimed."
                        ),
                        "zero_run_decision": str(
                            zero_run.get("zero_run_decision") or "unknown"
                        ),
                    }
                if gaps:
                    return False, {
                        "reason": "zero_run_evidence_unavailable",
                        "detail": (
                            "The host cannot prove whether the actor already recorded "
                            "a terminal delegation_zero_run decision."
                        ),
                        "zero_run_evidence_status": "unknown",
                        "zero_run_evidence_gaps": sorted(gaps),
                    }
            return _write_request()
    except (TimeoutError, ValueError) as exc:
        return False, {
            "reason": "replacement_custody_unknown",
            "detail": f"actor decision claim unavailable: {type(exc).__name__}",
        }
