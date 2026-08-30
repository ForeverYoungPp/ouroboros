"""One durable non-panic terminal boundary for delegated custody."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

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


def custody_audit_snapshot(drive_root: Any) -> Dict[str, Any]:
    """One shared read of the custody rows for a BATCH of audits.

    One ``replay()`` pass plus one pending-invocations pass, reused by every
    audit in the batch — the boot backfill must not rescan the unbounded event
    log four times per stored row.
    """
    return {
        "state": custody.replay(drive_root),
        "pending": custody.pending_invocations(drive_root),
    }


def _audit_task_custody(drive_root: Any, mine: str, result: Dict[str, Any], *,
                        snapshot: Optional[Mapping[str, Any]] = None,
                        emit_evidence: bool = True) -> None:
    """Read-only custody audit: fills unreconciled/audit fields and emits evidence.

    ``snapshot`` is a ``custody_audit_snapshot`` shared across a batch of
    audits; ``emit_evidence=False`` defers the evidence rows so a caller can
    compare the audit against the stored disclosure first (a no-op refresh must
    not append custody events every boot).
    """
    state = snapshot.get("state") if snapshot is not None else None
    pending = snapshot.get("pending") if snapshot is not None else None
    # The keyword rides only when a snapshot is really shared, so the
    # no-snapshot call shape stays byte-identical for every existing caller
    # and test seam over these projections.
    state_kw: Dict[str, Any] = {} if state is None else {"state": state}
    audit_failure = ""
    if custody.custody_log_unreadable(drive_root):
        audit_failure = "custody_log_unreadable"
    try:
        open_ids = (
            [row.run_id for row in custody.open_runs(drive_root, **state_kw)
             if row.task_id == mine]
            if not audit_failure else []
        )
    except Exception:
        open_ids, audit_failure = [], "audit_failed"
    try:
        invocation_ids = (
            [
                str(row.get("invocation_id") or "")
                for row in (pending if pending is not None
                            else custody.pending_invocations(drive_root))
                if str(row.get("task_id") or "") == mine
                and str(row.get("invocation_id") or "")
            ]
            if not audit_failure else []
        )
    except Exception:
        invocation_ids, audit_failure = [], "pending_invocation_audit_failed"
    try:
        patch_ids = (
            [row.run_id for row in custody.undisposed_patches(drive_root, **state_kw)
             if row.task_id == mine]
            if not audit_failure else []
        )
    except Exception:
        patch_ids, audit_failure = [], "undisposed_patch_audit_failed"
    deferred_retirements: list = []
    try:
        if not audit_failure:
            deferred_retirements = [
                row.run_id
                for row in custody.owned_project_registrations(drive_root, **state_kw)
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
    if emit_evidence:
        _emit_audit_evidence(drive_root, result)


def _emit_audit_evidence(drive_root: Any, result: Mapping[str, Any]) -> None:
    """The audit's durable evidence rows — separated so a comparison can run first."""
    mine = str(result.get("task_id") or "")
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


def refresh_terminal_reconciliation(
    drive_root: Any, task_id: str, *,
    trigger: str = "sweep_refresh",
    snapshot: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Audit-only refresh of a TERMINAL task's stored custody disclosure.

    The periodic sweep can settle a run AFTER its owning task already wrote its
    terminal result with a non-empty ``delegated_runs_unreconciled`` — the
    custody ledger then knows the truth while the stored projection keeps
    lying (nanny-leaf S1). This re-runs ONLY the read-side audit (never
    ``reconcile_task_runs`` — a refresh must not cancel anything) and rewrites
    the disclosure through the same recorder, whose guard permits clearing a
    stale non-empty list. The primary ``reason_code`` is deliberately left
    untouched (owner Q5=A), and already-rendered chat frames are out of scope —
    the fixed surfaces are the stored result, task details, and the API view
    (including retry-lineage projections, which read this row live).

    ``trigger`` names the refreshing surface on the envelope and its evidence
    rows (``sweep_refresh`` | ``boot_backfill`` | ``kill_path_clear``);
    ``snapshot`` shares one ``custody_audit_snapshot`` across a batch. An audit
    that MATCHES the stored disclosure performs no write and no emit — a
    permanently-unreconcilable row (e.g. an undisposed patch) must not grow
    events.jsonl on every boot. Returns True only when the row was refreshed.
    """
    mine = str(task_id or "")
    if not mine:
        return False
    try:
        from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

        existing = load_task_result(drive_root, mine) or {}
        if not existing.get("delegated_runs_unreconciled"):
            return False
        if str(existing.get("status") or "") not in _TRULY_TERMINAL_STATUSES:
            return False
    except Exception:
        log.debug("Sweep refresh skipped: task result unreadable for %s", mine, exc_info=True)
        return False
    result: Dict[str, Any] = {
        "task_id": mine, "trigger": str(trigger or "sweep_refresh"),
        "outcomes": [], "unreconciled": [], "audit_status": "ok",
    }
    _audit_task_custody(drive_root, mine, result, snapshot=snapshot, emit_evidence=False)
    if _stored_disclosure_matches(existing, result):
        return False
    _emit_audit_evidence(drive_root, result)
    record_terminal_reconciliation(drive_root, mine, result)
    return True


def backfill_terminal_reconciliations(drive_root: Any) -> List[str]:
    """Boot backfill: refresh every stored TERMINAL row that still discloses
    unreconciled delegated runs.

    The sweep-side refresh covers only task ids named in the CURRENT pass's
    reconcile outcomes, so a settlement from a previous server generation
    leaves the stored projection stale forever (generation-crossing residual).
    Driven by the reverse join — the stored results with a non-empty
    ``delegated_runs_unreconciled`` are a self-clearing set — never by a replay
    scan of the unbounded event log; one shared snapshot serves every audit,
    and each row is fail-soft. Returns the task ids actually refreshed.
    """
    try:
        from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, list_task_results

        stale = [
            str(row.get("task_id") or "")
            for row in list_task_results(drive_root)
            if row.get("delegated_runs_unreconciled")
            and str(row.get("status") or "") in _TRULY_TERMINAL_STATUSES
        ]
    except Exception:
        log.debug("Boot custody-disclosure backfill scan failed", exc_info=True)
        return []
    if not stale:
        return []
    snapshot = custody_audit_snapshot(drive_root)
    refreshed: List[str] = []
    for task_id in stale:
        try:
            if refresh_terminal_reconciliation(
                    drive_root, task_id, trigger="boot_backfill", snapshot=snapshot):
                refreshed.append(task_id)
        except Exception:
            log.debug("Boot custody-disclosure backfill failed for %s", task_id, exc_info=True)
    return refreshed


def _stored_disclosure_matches(
    existing: Mapping[str, Any], result: Mapping[str, Any],
) -> bool:
    """Whether the stored row already carries exactly this audit's disclosure.

    Compared on the behavior-bearing surfaces the recorder writes — the
    unreconciled list and the deferred-retirement disclosure — never on the
    envelope's provenance (trigger/outcomes), which differs between otherwise
    identical audits and would defeat the no-churn gate.
    """
    stored_envelope = existing.get("delegate_terminal_reconciliation")
    stored_envelope = stored_envelope if isinstance(stored_envelope, dict) else {}
    return (
        list(existing.get("delegated_runs_unreconciled") or [])
        == list(result.get("unreconciled") or [])
        and list(stored_envelope.get("deferred_project_retirements") or [])
        == list(result.get("deferred_project_retirements") or [])
    )


def record_terminal_reconciliation(
    drive_root: Any, task_id: str, result: Mapping[str, Any],
) -> None:
    """Attach the audit to the task record without choosing lifecycle policy."""

    try:
        from ouroboros.task_results import STATUS_RUNNING, load_task_result, write_task_result

        existing = load_task_result(drive_root, str(task_id or "")) or {}
        # Write only when the disclosure MOVES. Subsumes the old empty-over-empty
        # guard: a task that never delegated still gets no row minted here (the
        # STATUS_RUNNING fallback below exists for a legitimately mid-flight
        # loop-exit row, never for inventing one), and an already-current
        # disclosure is not rewritten on every kill or boot.
        if _stored_disclosure_matches(existing, result):
            return
        write_task_result(
            drive_root,
            str(task_id or ""),
            str(existing.get("status") or STATUS_RUNNING),
            delegate_terminal_reconciliation=dict(result),
            delegated_runs_unreconciled=list(result.get("unreconciled") or []),
        )
    except Exception:
        log.warning("Failed to persist terminal custody audit for %s", task_id, exc_info=True)


__all__ = [
    "backfill_terminal_reconciliations",
    "custody_audit_snapshot",
    "record_terminal_reconciliation",
    "refresh_terminal_reconciliation",
    "terminal_reconcile_task",
]
