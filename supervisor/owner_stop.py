"""Owner graceful stop ("Wrap up" / finalize-then-stop, Q1/Q2/Q3=A/Q6=A, 2026-08-15).

The policy half of the S3 cancel-finalization design, kept OUT of the pinned
``supervisor/task_lifecycle.py`` (which keeps a line-neutral dispatch at the
intent sweep) and ``supervisor/queue.py`` (which keeps one typed predicate in
its timeout enforcement):

- the durable cancel intent (``ouroboros/cancel_intents.py``) with
  ``stop_policy=finalize_then_cancel`` is the ONLY owner will — no second
  ledger, timer, or lease;
- the grace episode REUSES the existing coupled ``finalize_now`` control +
  RUNNING-row latch (``task_reaper.request_finalization_grace``), with the
  episode/control identity derived deterministically from the durable stop
  ``request_id`` so watchdog/restart replays never mint a duplicate control;
- ``sweep_cancel_intents`` calls ``sweep_owner_stop_hold`` per open intent:
  before the shared deadline the episode is armed/held (custody NOT fed);
  at the deadline, on a settled result, or after an immediate upgrade the
  generic custody feed proceeds — the existing custody stays the only killer;
- Q6=A cascade: live descendants are hard-settled deepest-first with ZERO paid
  turns through the existing subtree sweep, then only the root's one bounded
  tool-less turn runs over the preserved child results.

Panic and every non-graceful cancel are untouched: absence of the explicit
policy is byte-identical immediate hard cancellation (§13.1).
"""

from __future__ import annotations

import logging
import pathlib
import time
from datetime import datetime, timezone
from typing import Any, Dict

from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

# The sweep outcome recorded while an owner-stop episode holds an open intent.
OWNER_STOP_HOLDING = "owner_stop_finalizing"

_CHILD_PROJECTION_MAX_ROWS = 20
_CHILD_PROJECTION_PREVIEW_CHARS = 240


def _parse_ts(raw: Any) -> float:
    text = str(raw or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _requested_ts(intent: Dict[str, Any]) -> float:
    return _parse_ts(intent.get("requested_at"))


def _drained_ts(intent: Dict[str, Any]) -> float:
    """When the loop actually DELIVERED the finalize control to the model.

    Stamped by the worker's production mailbox drain
    (``cancel_intents.mark_finalize_control_drained``, first drain wins) and
    read back here by the sweep; 0.0 while the control is still undelivered.
    """
    return _parse_ts(intent.get("control_drained_at"))


def owner_stop_deadline_ts(intent: Dict[str, Any], grace_sec: float) -> float:
    """The episode's EFFECTIVE deadline (owner decisions 2026-08-15, 1=A + 2=A).

    Two immutable anchors, never extended by progress:

    - before the finalize control is DELIVERED to the model (no durable drain
      stamp yet): the OUTER safety cap alone applies — stop request time +
      ``OWNER_STOP_OUTER_CAP_SEC`` — so a task inside a long blocking tool
      call keeps its bounded final turn instead of being killed 120s after
      the button press;
    - after delivery: ``min(drain + grace SSOT, request + outer cap)`` — the
      episode budget starts ticking at the drain, and the outer cap still
      bounds the whole episode from the owner's request.

    ``grace_sec<=0`` means the graceful-stop feature is OFF (same semantics
    as ``running_owner_stop_tasks``): NO episode window exists anywhere,
    pre-drain included — never a request+outer-cap window. The sweep then
    feeds custody immediately (the immediate custody path).
    """
    from ouroboros.config import OWNER_STOP_OUTER_CAP_SEC

    if float(grace_sec or 0.0) <= 0:
        return 0.0
    requested = _requested_ts(intent)
    if not requested:
        return 0.0
    outer_deadline = requested + float(OWNER_STOP_OUTER_CAP_SEC)
    drained = _drained_ts(intent)
    if drained:
        return min(drained + float(grace_sec), outer_deadline)
    return outer_deadline


def owner_stop_open(intent: Any) -> bool:
    """Whether an UNCLAIMED finalize-policy intent is still OPEN.

    Open means: the durable intent carries the explicit finalize policy and no
    custody claim holds it (a claim means the kill already started). This is
    the HOLD predicate for the generic timeout rails (§12.2 item 8): a running
    task stays held for the WHOLE open-intent window — the deadline gates only
    the sweep's arm-vs-feed-custody decision (``sweep_owner_stop_hold``), so
    custody remains the sole killer at expiry and the generic rail can never
    withdraw, reap, or retry an intent-covered task in the expiry window.
    """
    if not isinstance(intent, dict):
        return False
    from ouroboros.cancel_intents import INTENT_CLAIMED, STOP_POLICY_FINALIZE, stop_policy

    if stop_policy(intent) != STOP_POLICY_FINALIZE:
        return False
    return intent.get("state") != INTENT_CLAIMED


def owner_stop_active(intent: Any, *, now: float, grace_sec: float) -> bool:
    """Whether an OPEN graceful stop episode is still inside its window.

    Active means OPEN (``owner_stop_open``) plus the EFFECTIVE deadline
    (``owner_stop_deadline_ts``: outer cap before the control is delivered,
    ``min(drain + grace, request + outer cap)`` after) has not passed.
    Own/descendant progress NEVER extends either anchor (§12.2 item 8).
    Consulted by the SWEEP only (arm vs feed custody); the enforcement hold
    deliberately uses the deadline-free ``owner_stop_open`` instead.
    """
    if not owner_stop_open(intent):
        return False
    deadline = owner_stop_deadline_ts(intent, grace_sec)
    return bool(deadline) and now < deadline


def queue_grace_sec(q: Any) -> float:
    """The shared finalization-grace SSOT as the queue currently holds it."""
    try:
        return float(getattr(q, "FINALIZATION_GRACE_SEC", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def running_owner_stop_tasks(drive_root: Any, *, grace_sec: float) -> set:
    """Task ids whose OPEN owner-stop intent must bypass generic timeout rails.

    Read once per enforcement pass (one small locked projection read), then
    checked per RUNNING row — the typed predicate ``supervisor/queue.py``
    consults before every spare-withdraw, spare-reset, second-grace,
    timeout-kill, reaping, and retry branch (§12.2 item 8). The hold covers
    the WHOLE open-intent window, deliberately including the expiry window
    past the grace deadline: the custody sweep (20s cadence) is the sole
    killer there, and the faster enforcement tick must not withdraw the
    episode, falsify the terminal reason, or clone a retry meanwhile.
    ``grace_sec<=0`` means the graceful-stop feature is off (the sweep feeds
    custody immediately), so no hold is needed.
    """
    if float(grace_sec or 0.0) <= 0:
        return set()
    try:
        from ouroboros.cancel_intents import active_intents

        intents = active_intents(drive_root)
    except Exception:
        log.debug("owner-stop enforcement read failed", exc_info=True)
        return set()
    return {tid for tid, intent in intents.items() if owner_stop_open(intent)}


def sweep_owner_stop_hold(q: Any, task_id: str, intent: Dict[str, Any], *, now: float) -> bool:
    """The policy-aware sweep decision for ONE open intent (§12.2 item 9).

    True  = the graceful episode holds this tick: descendants (cascade) are
            hard-settled with zero paid turns, the root's grace episode is
            idempotently armed/held, custody is NOT fed.
    False = the generic custody feed proceeds (immediate policy, hardened
            intent, expired deadline, settled root, or a root that never
            started — pending tasks never buy a model turn).
    """
    grace = queue_grace_sec(q)
    if not owner_stop_active(intent, now=now, grace_sec=grace):
        return False
    try:
        from ouroboros.cancel_intents import settled_status

        if settled_status(q.DRIVE_ROOT, task_id):
            # Natural completion won (or an earlier terminal landed): feed
            # custody so completion-wins settles the intent honestly.
            return False
    except Exception:
        log.debug("owner-stop settled read failed for %s", task_id, exc_info=True)
    try:
        return orchestrate_graceful_stop(q, task_id, intent, now=now)
    except Exception:
        log.warning("owner-stop orchestration failed for %s", task_id, exc_info=True)
        return False


def begin_graceful_stop(task_id: str) -> None:
    """Ingress kick-off: run one orchestration pass off the HTTP thread.

    The HTTP handler answered with the immediate durable pending
    acknowledgement already; this pass arms the episode without waiting for the
    ~20s sweep tick. Crash-safe: the durable intent alone replays the whole
    episode through ``sweep_cancel_intents`` -> ``sweep_owner_stop_hold``.
    """
    from supervisor import queue as q

    try:
        from ouroboros.cancel_intents import active_intent

        intent = active_intent(q.DRIVE_ROOT, task_id)
        if isinstance(intent, dict):
            sweep_owner_stop_hold(q, task_id, intent, now=time.time())
    except Exception:
        log.warning("owner-stop ingress orchestration failed for %s", task_id, exc_info=True)


def orchestrate_graceful_stop(q: Any, task_id: str, intent: Dict[str, Any], *, now: float) -> bool:
    """One idempotent hold tick: settle descendants (Q6=A), arm the root episode.

    Returns True while the episode genuinely holds a LIVE running root; a root
    that is pending/missing returns False so custody settles it immediately
    (zero model turns — §13.1).
    """
    from ouroboros.cancel_intents import SCOPE_CASCADE

    cascade = str(intent.get("scope") or "") == SCOPE_CASCADE
    if cascade:
        _settle_descendants_hard(q, task_id)
    with q._queue_lock:
        running_meta = q.RUNNING.get(task_id) if isinstance(q.RUNNING, dict) else None
        if not isinstance(running_meta, dict):
            # PENDING (never started -> zero turns) or gone (miss lane): feed
            # custody. The sweep's generic path settles both shapes.
            return False
    return _arm_owner_stop_episode(q, task_id, intent, running_meta, now=now, cascade=cascade)


def _settle_descendants_hard(q: Any, task_id: str) -> None:
    """Q6=A: live descendants are hard-stopped deepest-first, zero paid turns.

    Reuses the existing cascade subtree sweep with the ROOT excluded — each
    descendant gets its own durable intent and custody teardown, per-task
    delivery suppressed (the tree's story is the root's finalization or, on
    expiry, the one cascade summary). Idempotent: a settled subtree yields an
    empty sweep. The root id is fenced FIRST so late descendant admission is
    refused while the root finalizes (§12.2 item 3).
    """
    from supervisor.task_lifecycle import (
        CANCELLED_ROOT_FENCES, _cancel_subtree_sweep, _prune_cancellation_fences,
    )

    with q._queue_lock:
        CANCELLED_ROOT_FENCES[task_id] = utc_now_iso()
        _prune_cancellation_fences(protected={task_id})
    try:
        _cancel_subtree_sweep(q, task_id, {task_id})
    except Exception:
        log.warning("owner-stop descendant sweep failed for %s", task_id, exc_info=True)


def owner_stop_control_id(intent: Dict[str, Any]) -> str:
    """Deterministic episode/control identity from the durable stop request."""
    return f"ownerstop:{str(intent.get('request_id') or '')}"


def _arm_owner_stop_episode(
    q: Any, task_id: str, intent: Dict[str, Any], running_meta: Dict[str, Any],
    *, now: float, cascade: bool,
) -> bool:
    """Idempotently arm the coupled finalize_now control + RUNNING latch."""
    from supervisor.task_reaper import request_finalization_grace
    from ouroboros.outcomes import REASON_OWNER_REQUESTED_FINALIZATION

    control_id = owner_stop_control_id(intent)
    with q._queue_lock:
        meta = q.RUNNING.get(task_id)
        if not isinstance(meta, dict):
            return False
        if str(meta.get("finalization_control_msg_id") or "") == control_id:
            return True  # already armed; the mailbox drain dedupes by msg_id
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        chat_id = int(task.get("chat_id") or 0)
        task_drive = q._task_drive_for_task(task, task_id)
    control_text = REASON_OWNER_REQUESTED_FINALIZATION
    if cascade:
        projection = _child_result_projection(q, task_id)
        if projection:
            control_text = f"{control_text}\n{projection}"
    grace_deadline = owner_stop_deadline_ts(intent, queue_grace_sec(q))
    remaining = max(0, int(grace_deadline - now)) if grace_deadline else 0
    written = request_finalization_grace(
        pathlib.Path(task_drive), task_id, REASON_OWNER_REQUESTED_FINALIZATION,
        chat_id=chat_id, stamp=int(_requested_ts(intent) or now),
        control_msg_id=control_id, control_text=control_text,
        toast_text=(
            f"⏳ The owner asked task {task_id} to summarize and stop. One final "
            f"answer is being produced now (≤{remaining}s); Stop now remains "
            "available and escalates the same stop request immediately."
        ),
    )
    if not written:
        # The control write failed; hold anyway (the deadline still bounds the
        # episode) and let the next sweep tick retry the same deterministic id.
        _forensic(q, task_id, "owner_stop_arm_failed", intent)
        return True
    with q._queue_lock:
        meta = q.RUNNING.get(task_id)
        if isinstance(meta, dict):
            meta["finalization_requested_at"] = _requested_ts(intent) or now
            meta["finalization_reason"] = REASON_OWNER_REQUESTED_FINALIZATION
            meta["finalization_control_msg_id"] = written
            q.RUNNING[task_id] = meta
    _forensic(q, task_id, "owner_stop_armed", intent)
    return True


def graceful_summary_suppressed(q: Any, task_id: str) -> bool:
    """Q4=A: suppress the cascade receipt after a SUCCESSFUL graceful stop.

    True only when the root's open cascade intent carries the finalize policy
    AND the root's durable result is COMPLETED — the owner already received the
    model's own final answer through normal delivery, and the card state says
    factual "Done" with the owner-stop marker in its details; a second summary
    message would be the duplicate Q4 forbids. Every other outcome (expiry ->
    cancelled, failed, replayed crash)
    keeps the tree's ONE receipt. The suppression is recorded as a typed
    forensic row so the crash-order evidence shows a conscious decision.
    """
    try:
        from ouroboros.cancel_intents import (
            STOP_POLICY_FINALIZE, active_intent, stop_policy,
        )
        from ouroboros.task_results import STATUS_COMPLETED, load_task_result

        intent = active_intent(q.DRIVE_ROOT, task_id) or {}
        if stop_policy(intent) != STOP_POLICY_FINALIZE:
            return False
        status = str((load_task_result(q.DRIVE_ROOT, task_id) or {}).get("status") or "")
        if status != STATUS_COMPLETED:
            return False
        _forensic(q, task_id, "owner_stop_summary_suppressed", intent)
        return True
    except Exception:
        log.debug("graceful summary suppression check failed for %s", task_id, exc_info=True)
        return False


def _forensic(q: Any, task_id: str, event: str, intent: Dict[str, Any]) -> None:
    try:
        append_jsonl(
            pathlib.Path(q.DRIVE_ROOT) / "logs" / "supervisor.jsonl",
            {"ts": utc_now_iso(), "type": "owner_stop", "event": event,
             "task_id": task_id, "request_id": str(intent.get("request_id") or "")},
        )
    except Exception:
        log.debug("owner-stop forensic append failed for %s", task_id, exc_info=True)


def _child_result_projection(q: Any, task_id: str) -> str:
    """Q6=A: the bounded durable child projection for the root's ONE final turn.

    Built from the existing seams — cascade ancestry enumeration
    (``terminal_delivery._cascade_descendant_rows``) over durable rows plus the
    queue snapshot, and each child's own durable result — DELIBERATELY including
    settled-cancelled children (§12.2 item 4). Bounded: at most
    ``_CHILD_PROJECTION_MAX_ROWS`` rows with the exact omitted count, each
    result previewed to ``_CHILD_PROJECTION_PREVIEW_CHARS``. No ledger is added.
    """
    try:
        from supervisor.terminal_delivery import _cascade_descendant_rows
        from ouroboros.task_results import load_task_result

        rows = _cascade_descendant_rows(pathlib.Path(q.DRIVE_ROOT), task_id)
    except Exception:
        log.debug("owner-stop child projection failed for %s", task_id, exc_info=True)
        return ""
    if not rows:
        return ""
    lines = [
        "[CHILD_RESULTS] Your subtasks were stopped for this owner-requested "
        "finalization; their preserved durable results:",
    ]
    for index, (tid, status) in enumerate(sorted(rows.items())):
        if index >= _CHILD_PROJECTION_MAX_ROWS:
            lines.append(f"- … {len(rows) - _CHILD_PROJECTION_MAX_ROWS} more descendant(s) omitted")
            break
        preview = ""
        try:
            result = load_task_result(pathlib.Path(q.DRIVE_ROOT), tid) or {}
            status = str(result.get("status") or status or "")
            preview = " ".join(str(result.get("result") or "").split())
        except Exception:
            preview = ""
        if len(preview) > _CHILD_PROJECTION_PREVIEW_CHARS:
            preview = preview[:_CHILD_PROJECTION_PREVIEW_CHARS] + "…"
        lines.append(f"- {tid} ({status or 'unknown'}): {preview or '(no result text)'}")
    return "\n".join(lines)
