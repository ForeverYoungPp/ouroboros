"""One durable non-panic terminal boundary for delegated custody."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

from ouroboros import delegate_custody as custody

log = logging.getLogger(__name__)


def terminal_reconcile_task(
    drive_root: Any,
    task_id: str,
    *,
    gateway_factory: Optional[Callable[[], Any]] = None,
    trigger: str = "terminal_boundary",
) -> Dict[str, Any]:
    """Reconcile durable starts, then re-audit runs, invocations, and patches."""

    mine = str(task_id or "")
    result: Dict[str, Any] = {
        "task_id": mine, "trigger": str(trigger or "terminal_boundary"),
        "outcomes": [], "unreconciled": [], "audit_status": "ok",
    }
    if not mine:
        result.update({
            "audit_status": "failed",
            "unreconciled": ["delegated_run_state_unknown:missing_task_id"],
        })
        return result
    try:
        result["outcomes"] = custody.reconcile_task_runs(
            drive_root, mine, gateway_factory=gateway_factory,
        )
    except Exception:
        log.warning("Terminal delegated custody reconciliation failed for %s", mine, exc_info=True)
    _audit_task_custody(drive_root, mine, result)
    return result


def _audit_task_custody(drive_root: Any, mine: str, result: Dict[str, Any]) -> None:
    """Read-only custody audit: fills unreconciled/audit fields and emits evidence."""
    audit_failure = ""
    if custody.custody_log_unreadable(drive_root):
        audit_failure = "custody_log_unreadable"
    try:
        open_ids = (
            [row.run_id for row in custody.open_runs(drive_root) if row.task_id == mine]
            if not audit_failure else []
        )
    except Exception:
        open_ids, audit_failure = [], "audit_failed"
    try:
        invocation_ids = (
            [
                str(row.get("invocation_id") or "")
                for row in custody.pending_invocations(drive_root)
                if str(row.get("task_id") or "") == mine
                and str(row.get("invocation_id") or "")
            ]
            if not audit_failure else []
        )
    except Exception:
        invocation_ids, audit_failure = [], "pending_invocation_audit_failed"
    try:
        patch_ids = (
            [row.run_id for row in custody.undisposed_patches(drive_root) if row.task_id == mine]
            if not audit_failure else []
        )
    except Exception:
        patch_ids, audit_failure = [], "undisposed_patch_audit_failed"
    deferred_retirements: list = []
    try:
        if not audit_failure:
            deferred_retirements = [
                row.run_id
                for row in custody.owned_project_registrations(drive_root)
                if row.task_id == mine and row.settled
            ]
    except Exception:
        # Fail-closed like the audits above: an unreadable registration state
        # must not read as "no deferred retirements".
        audit_failure = audit_failure or "registration_audit_failed"
    if not audit_failure:
        result.update({
            "open_run_ids": open_ids,
            "pending_invocation_ids": invocation_ids,
            "undisposed_patch_run_ids": patch_ids,
            # DISCLOSED, never unreconciled: a settled run's project registration
            # awaiting retirement is cleanup debt with its own retry lane - it
            # must not convert the task's outcome (the old coupling did).
            "deferred_project_retirements": deferred_retirements,
            "unreconciled": [
                *open_ids,
                *(f"invocation:{item}" for item in invocation_ids),
                *(f"patch:{item}" for item in patch_ids),
            ],
        })
    else:
        result.update({
            "audit_status": "failed",
            "unreconciled": [f"delegated_run_state_unknown:{audit_failure}"],
        })
    if result["unreconciled"]:
        custody.emit(drive_root, "delegated_runs_unreconciled", {
            "task_id": mine, "trigger": result["trigger"],
            "run_ids": list(result["unreconciled"]),
            "audit_status": result["audit_status"],
            **({"flavor": "audit_failed"} if result["audit_status"] != "ok" else {}),
            "open_run_ids": list(result.get("open_run_ids") or []),
            "pending_invocation_ids": list(result.get("pending_invocation_ids") or []),
            "undisposed_patch_run_ids": list(result.get("undisposed_patch_run_ids") or []),
        })
    else:
        custody.emit(drive_root, "delegate_terminal_custody_reconciled", {
            "task_id": mine, "trigger": result["trigger"],
            "outcome_count": len(result["outcomes"]),
        })


_EVIDENCE_COUNTER_KEYS = (
    "delegated_runs_started", "delegated_runs_settled",
    "delegated_runs_succeeded", "delegated_runs_failed",
    "delegated_runs_source_unresolved",
)


def _stored_evidence_stale(existing: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    """True when the stored substrate/evidence projection disagrees with custody.

    Only tasks that ever wrote the harness-dispatch mirror participate: a task
    with neither top-level counters nor an envelope evidence block was not
    delegated, and minting one here would fabricate a dispatch record.
    """
    envelope = existing.get("subagent_envelope")
    stored_ev = (envelope or {}).get("execution_evidence") if isinstance(envelope, Mapping) else None
    has_top = any(key in existing for key in _EVIDENCE_COUNTER_KEYS)
    if not has_top and not isinstance(stored_ev, Mapping):
        return False
    if live.get("evidence_read_failed"):
        # Unreadable custody proves nothing; never rewrite over it.
        return False
    for key in _EVIDENCE_COUNTER_KEYS:
        if has_top and int(existing.get(key) or 0) != int(live.get(key) or 0):
            return True
        if isinstance(stored_ev, Mapping) and int(stored_ev.get(key) or 0) != int(live.get(key) or 0):
            return True
    if isinstance(stored_ev, Mapping):
        if stored_ev.get("subscription_cost_usd") != live.get("subscription_cost_usd"):
            return True
    return False


def _rewrite_execution_evidence(drive_root: Any, task_id: str, existing: Mapping[str, Any], live: Mapping[str, Any]) -> None:
    """Rewrite the stored envelope evidence + top-level substrate mirror from
    live custody, through the same producers the terminal write used."""
    from ouroboros.subagents import actual_substrate, substrate_result_fields
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    envelope = dict(existing.get("subagent_envelope") or {})
    envelope["execution_evidence"] = dict(live)
    substrate = actual_substrate(live)
    if substrate:
        envelope["actual_substrate"] = substrate
    write_task_result(
        drive_root, str(task_id or ""),
        str(existing.get("status") or STATUS_RUNNING),
        subagent_envelope=envelope,
        **substrate_result_fields(envelope),
    )


def refresh_terminal_reconciliation(drive_root: Any, task_id: str) -> bool:
    """Audit-only refresh of a TERMINAL task's stored custody disclosure.

    The periodic sweep can settle a run AFTER its owning task already wrote its
    terminal result — the custody ledger then knows the truth while the stored
    projection keeps lying (nanny-leaf S1). Two independent stale classes are
    healed: a non-empty ``delegated_runs_unreconciled`` disclosure, and stored
    substrate counters/cost that disagree with live custody (the PR #402 test
    pinned only the first, so ``actual_substrate='harness_attempted'`` and
    ``subscription_cost_usd=None`` survived a successful refresh). This re-runs
    ONLY the read-side audit (never ``reconcile_task_runs`` — a refresh must
    not cancel anything) and rewrites through the same recorders. The primary
    ``reason_code`` is deliberately left untouched (owner Q5=A), and
    already-rendered chat frames are out of scope — the fixed surfaces are the
    stored result, task details, and the API view (including retry-lineage
    projections, which read this row live).
    """
    mine = str(task_id or "")
    if not mine:
        return False
    try:
        from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

        existing = load_task_result(drive_root, mine) or {}
        if str(existing.get("status") or "") not in _TRULY_TERMINAL_STATUSES:
            return False
        live = custody.task_execution_evidence(drive_root, mine)
        evidence_stale = _stored_evidence_stale(existing, live)
        if not existing.get("delegated_runs_unreconciled") and not evidence_stale:
            return False
    except Exception:
        log.debug("Sweep refresh skipped: task result unreadable for %s", mine, exc_info=True)
        return False
    result: Dict[str, Any] = {
        "task_id": mine, "trigger": "sweep_refresh",
        "outcomes": [], "unreconciled": [], "audit_status": "ok",
    }
    _audit_task_custody(drive_root, mine, result)
    record_terminal_reconciliation(drive_root, mine, result)
    if evidence_stale:
        try:
            _rewrite_execution_evidence(drive_root, mine, existing, live)
        except Exception:
            log.warning("Sweep evidence rewrite failed for %s", mine, exc_info=True)
    return True


_REFRESH_CURSOR_REL = "state/delegate_terminal_refresh_cursor.json"
_REFRESH_SCAN_CAP_BYTES = 5 * 1024 * 1024  # bounded work per sweep tick


def refresh_recently_settled_terminals(drive_root: Any) -> int:
    """Refresh terminal results of tasks whose runs settled since the cursor.

    The orphan sweep only revisits tasks named in THIS generation's reconcile
    outcomes; a run settled at the terminal boundary (or by an earlier
    generation) never reappears there, so its task's stored evidence stays
    stale forever. A durable byte-offset cursor over the append-only custody
    event log keeps each tick bounded to newly appended SETTLED rows (house
    projection-beside-the-log pattern) — never a full replay per sweep. A
    shrunken/rotated log resets the cursor; the one-time historical pass is
    paced by the per-tick byte cap. Returns the number of refreshed tasks.
    """
    import json as _json
    import pathlib as _pathlib

    from ouroboros.utils import atomic_write_json, read_json_dict

    log_path = custody.event_log_path(drive_root)
    cursor_path = _pathlib.Path(drive_root) / _REFRESH_CURSOR_REL
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        return 0
    offset = int((read_json_dict(cursor_path) or {}).get("offset") or 0)
    if offset > size:
        offset = 0  # rotated/truncated log: re-ground once
    if offset >= size:
        return 0
    task_ids: set = set()
    end_offset = offset
    try:
        with log_path.open("rb") as fh:
            fh.seek(offset)
            read_bytes = 0
            for raw in fh:
                if not raw.endswith(b"\n") or read_bytes > _REFRESH_SCAN_CAP_BYTES:
                    break  # incomplete tail line stays for the next tick
                read_bytes += len(raw)
                end_offset += len(raw)
                try:
                    row = _json.loads(raw)
                except Exception:
                    continue
                if str(row.get("type") or "") in (custody.SETTLED, custody.CLOSED_ABSENT):
                    tid = str(row.get("task_id") or "")
                    if tid:
                        task_ids.add(tid)
    except OSError:
        return 0
    refreshed = 0
    for tid in sorted(task_ids):
        try:
            if refresh_terminal_reconciliation(drive_root, tid):
                refreshed += 1
        except Exception:
            log.debug("Cursor refresh failed for %s", tid, exc_info=True)
    try:
        atomic_write_json(cursor_path, {"offset": end_offset})
    except Exception:
        log.debug("Refresh cursor write failed", exc_info=True)
    return refreshed


def record_terminal_reconciliation(
    drive_root: Any, task_id: str, result: Mapping[str, Any],
) -> None:
    """Attach the audit to the task record without choosing lifecycle policy."""

    try:
        from ouroboros.task_results import STATUS_RUNNING, load_task_result, write_task_result

        existing = load_task_result(drive_root, str(task_id or "")) or {}
        unreconciled = list(result.get("unreconciled") or [])
        deferred = list(result.get("deferred_project_retirements") or [])
        if (not unreconciled and not deferred
                and not existing.get("delegated_runs_unreconciled")):
            return
        write_task_result(
            drive_root,
            str(task_id or ""),
            str(existing.get("status") or STATUS_RUNNING),
            delegate_terminal_reconciliation=dict(result),
            delegated_runs_unreconciled=unreconciled,
        )
    except Exception:
        log.warning("Failed to persist terminal custody audit for %s", task_id, exc_info=True)


__all__ = [
    "record_terminal_reconciliation",
    "refresh_recently_settled_terminals",
    "refresh_terminal_reconciliation",
    "terminal_reconcile_task",
]
