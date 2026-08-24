"""Small process-local custody seam for review workers.

The review substrate owns parsing and quorum semantics.  This module owns only
the physical-worker lifecycle: a logical slot deadline may return a typed
in-flight actor, while a retry with the same caller identity discovers the
existing worker or consumes its late result instead of dispatching twice.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

from ouroboros.deadline_utils import review_operation_timeout_sec
from ouroboros.observability import new_call_id
from ouroboros.utils import emit_cognitive_operation_event

log = logging.getLogger("review_custody")


@dataclass
class ActiveReviewAttempt:
    key: str
    operation_id: str
    event: threading.Event = field(default_factory=threading.Event)
    actor: Any = None
    timed_out: bool = False
    retry_state: Dict[str, Any] = field(default_factory=dict)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Dict[str, ActiveReviewAttempt] = {}
_PENDING_STATES = {"in_flight", "custody_lost"}


def _row_is_pending(row: Dict[str, Any]) -> bool:
    return bool(row.get("late_result_pending")) or str(
        row.get("operation_state") or ""
    ) in _PENDING_STATES


def _custody_lost_row(
    surface: str, index: Any, reason: str, row: Any = None,
) -> Dict[str, Any]:
    lost = copy.deepcopy(row) if isinstance(row, dict) else {}
    if not str(lost.get("slot_id") or lost.get("slot") or ""):
        lost["slot_id"] = f"__custody_lost_{surface}_{index}"
    lost.setdefault("status", "error")
    lost.setdefault("error", reason)
    lost["operation_state"] = "custody_lost"
    lost["late_result_pending"] = True
    return lost


def _freeze_roster_rows(usage_ctx: Any, surface: str, raw: Any) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        rows: List[Any] = []
    elif isinstance(raw, (list, tuple)):
        rows = list(raw)
    else:
        rows = [_custody_lost_row(surface, "container", "review roster is not a list")]
        setattr(usage_ctx, "_review_custody_lost", True)
    by_slot: Dict[str, Dict[str, Any]] = {}
    for index, value in enumerate(rows):
        row = copy.deepcopy(value) if isinstance(value, dict) else _custody_lost_row(
            surface, index, "review roster row is not an object",
        )
        slot_id = str(row.get("slot_id") or row.get("slot") or "")
        if not slot_id:
            row = _custody_lost_row(surface, index, "review roster row has no slot id", row)
            slot_id = str(row["slot_id"])
        if (
            not isinstance(value, dict)
            or slot_id in by_slot
            or str(row.get("operation_state") or "") == "custody_lost"
        ):
            setattr(usage_ctx, "_review_custody_lost", True)
        if slot_id in by_slot:
            by_slot[slot_id] = _custody_lost_row(
                surface, index, "review roster contains duplicate slot ids", by_slot[slot_id],
            )
            continue
        by_slot[slot_id] = row
    return by_slot


def prepare_frozen_review_reconciliation(usage_ctx: Any, attempt: Any) -> None:
    """Expose one durable commit attempt as the exact reconciliation roster."""
    triad = _freeze_roster_rows(
        usage_ctx, "multi_model_review", getattr(attempt, "triad_raw_results", None),
    )
    raw_scope = getattr(attempt, "scope_raw_result", None)
    scope_wrapper = copy.deepcopy(raw_scope) if isinstance(raw_scope, dict) else {}
    if isinstance(raw_scope, dict):
        scope_input = (
            raw_scope.get("raw_results")
            if "raw_results" in raw_scope
            else [raw_scope] if raw_scope else []
        )
    else:
        scope_input = raw_scope
    scope = _freeze_roster_rows(usage_ctx, "scope_review", scope_input)
    frozen = {"multi_model_review": triad, "scope_review": scope}
    if not triad and not scope:
        triad["__custody_lost_multi_model_review_empty"] = _custody_lost_row(
            "multi_model_review", "empty", "review roster is empty",
        )
        setattr(usage_ctx, "_review_custody_lost", True)
    setattr(usage_ctx, "_review_frozen_rows", frozen)
    setattr(usage_ctx, "_review_frozen_scope_raw", scope_wrapper)


def _frozen_actor(row: Dict[str, Any], slot: Any) -> Any:
    from ouroboros.review_substrate import ReviewActorRecord

    status = str(row.get("status") or "")
    raw_text = str(row.get("raw_text") or "")
    actor_status = (
        "not_dispatched" if status == "not_dispatched"
        else "ok" if raw_text or status in {"responded", "ok", "empty", "parse_failure", "partial"}
        else "error"
    )
    usage = {
        "pending_invocation_id": str(row.get("pending_invocation_id") or ""),
        "delegated_run_id": str(row.get("delegated_run_id") or ""),
    }
    return ReviewActorRecord(
        slot_id=str(getattr(slot, "slot_id", "") or ""),
        model=str(row.get("model_id") or row.get("model") or getattr(slot, "model", "")),
        status=actor_status,
        raw_text=raw_text,
        error=str(row.get("error") or ""),
        usage=usage,
        prompt_ref=dict(row.get("prompt_ref") or {}),
        response_ref=dict(row.get("response_ref") or {}),
        operation_id=str(row.get("operation_id") or ""),
        operation_state=str(row.get("operation_state") or "settled"),
        late_result_pending=False,
    )


def merge_frozen_review_reconciliation(usage_ctx: Any) -> None:
    """Keep the original roster; only its exact pending operation may change."""
    frozen = getattr(usage_ctx, "_review_frozen_rows", None)
    if not isinstance(frozen, dict):
        return

    def _merge(surface: str, current: List[Any]) -> List[Dict[str, Any]]:
        originals = frozen.get(surface) if isinstance(frozen.get(surface), dict) else {}
        current_groups: Dict[str, List[Dict[str, Any]]] = {}
        invalid_rows: List[Dict[str, Any]] = []
        for index, row in enumerate(current):
            if not isinstance(row, dict):
                invalid_rows.append(_custody_lost_row(
                    surface, index, "current review roster row is not an object",
                ))
                setattr(usage_ctx, "_review_custody_lost", True)
                continue
            slot_id = str(row.get("slot_id") or row.get("slot") or "")
            if not slot_id:
                invalid_rows.append(_custody_lost_row(
                    surface, index, "current review roster row has no slot id", row,
                ))
                setattr(usage_ctx, "_review_custody_lost", True)
                continue
            current_groups.setdefault(slot_id, []).append(row)
        ambiguous_slots = {
            slot_id for slot_id, rows in current_groups.items() if len(rows) != 1
        }
        if ambiguous_slots:
            setattr(usage_ctx, "_review_custody_lost", True)
        current_by_slot = {
            slot_id: rows[0]
            for slot_id, rows in current_groups.items()
            if slot_id not in ambiguous_slots
        }
        merged: List[Dict[str, Any]] = []
        for slot_id, original in originals.items():
            if str(original.get("operation_state") or "") == "custody_lost":
                merged.append(copy.deepcopy(original))
                setattr(usage_ctx, "_review_custody_lost", True)
                continue
            if slot_id in ambiguous_slots:
                merged.append(_custody_lost_row(
                    surface, slot_id, "current review roster contains duplicate slot ids", original,
                ))
                setattr(usage_ctx, "_review_custody_lost", True)
                continue
            if not _row_is_pending(original):
                merged.append(copy.deepcopy(original))
                continue
            candidate = current_by_slot.get(slot_id)
            operation_id = str(original.get("operation_id") or "")
            if candidate is not None and operation_id and str(
                candidate.get("operation_id") or ""
            ) == operation_id:
                merged.append(copy.deepcopy(candidate))
                continue
            lost = copy.deepcopy(original)
            lost["operation_state"] = "custody_lost"
            lost["late_result_pending"] = True
            merged.append(lost)
            setattr(usage_ctx, "_review_custody_lost", True)
        for slot_id in sorted(current_by_slot):
            row = current_by_slot[slot_id]
            if slot_id not in originals:
                extra = copy.deepcopy(row)
                extra["operation_state"] = "custody_lost"
                extra["late_result_pending"] = True
                merged.append(extra)
                setattr(usage_ctx, "_review_custody_lost", True)
        for slot_id in sorted(ambiguous_slots - set(originals)):
            rows = current_groups[slot_id]
            row = min(
                rows,
                key=lambda item: json.dumps(
                    item, sort_keys=True, ensure_ascii=True, default=str,
                ),
            )
            merged.append(_custody_lost_row(
                surface, slot_id, "current review roster contains duplicate slot ids", row,
            ))
        for row in sorted(
            invalid_rows,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True, default=str),
        ):
            merged.append(copy.deepcopy(row))
        return merged

    triad_now = getattr(usage_ctx, "_last_triad_raw_results", None)
    triad_rows = (
        list(triad_now) if isinstance(triad_now, (list, tuple))
        else [] if triad_now is None else [triad_now]
    )
    triad = _merge(
        "multi_model_review",
        triad_rows,
    )
    scope_now = getattr(usage_ctx, "_last_scope_raw_result", None)
    scope_wrapper = copy.deepcopy(scope_now) if isinstance(scope_now, dict) else {}
    if isinstance(scope_now, dict):
        raw_rows = scope_now.get("raw_results") if "raw_results" in scope_now else None
        scope_rows = (
            list(raw_rows) if isinstance(raw_rows, (list, tuple))
            else [raw_rows] if raw_rows is not None
            else [scope_now] if scope_now else []
        )
    else:
        scope_rows = [] if scope_now is None else [scope_now]
    merged_scope = _merge("scope_review", scope_rows)
    if not scope_wrapper:
        frozen_wrapper = getattr(usage_ctx, "_review_frozen_scope_raw", None)
        scope_wrapper = copy.deepcopy(frozen_wrapper) if isinstance(frozen_wrapper, dict) else {}
    if merged_scope:
        scope_wrapper["raw_results"] = merged_scope
    usage_ctx._last_triad_raw_results = triad
    usage_ctx._last_scope_raw_result = scope_wrapper


def retryable_review_exception(exc: Exception, usage_ctx: Any) -> bool:
    """Whether a second byte-identical review send has a terminal basis."""
    from ouroboros.loop_llm_call import classify_llm_exception

    classification = classify_llm_exception(exc)
    if classification.kind == "provider_outcome_unknown":
        if usage_ctx is not None:
            setattr(usage_ctx, "_review_custody_lost", True)
        if not str(getattr(exc, "code", "") or ""):
            setattr(exc, "code", "provider_outcome_unknown")
    return classification.retry_same_request


def _attempt_key(request: Any, slot: Any) -> str:
    retry_key = str(getattr(request, "retry_key", "") or "").strip()
    slot_messages = (getattr(request, "slot_messages", {}) or {}).get(
        str(getattr(slot, "slot_id", "") or "")
    )
    slot_data = {
        key: getattr(slot, key, None) for key in (
            "slot_id", "model", "effort", "max_tokens", "temperature", "role_hint",
            "use_local", "route", "session_target", "session_profile",
        )
    }
    identity = {
        "retry_key": retry_key,
        "surface": getattr(request, "surface", ""),
        "task_id": getattr(request, "task_id", ""),
        "call_type": getattr(request, "call_type", ""),
        "session_root": getattr(request, "session_root", ""),
        "slot": slot_data,
    }
    # An explicit retry key is the logical-operation identity. Mutable prompt
    # history must not turn a retry of that operation into a second paid send.
    # Callers change the key for genuinely new material/cycles; old callers
    # without one retain the full content-addressed behavior byte-for-byte.
    if retry_key:
        payload = identity
    else:
        payload = {
            **identity,
            "task_attempt": getattr(request, "task_attempt", None),
            "max_tokens": getattr(request, "max_tokens", None),
            "temperature": getattr(request, "temperature", None),
            "no_proxy": getattr(request, "no_proxy", None),
            "messages": slot_messages if slot_messages is not None else getattr(request, "messages", []),
            "goal": getattr(request, "goal", ""),
            "scope": getattr(request, "scope", ""),
            "subject": getattr(request, "subject", ""),
            "evidence_refs": getattr(request, "evidence_refs", []),
            "evidence": getattr(request, "evidence", {}),
            "policy": getattr(request, "policy", {}),
            "session_task": getattr(request, "session_task", ""),
        }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _logical_timeout(slot: Any, request: Any, usage_meta: Dict[str, Any]) -> float:
    deadline = str(getattr(request, "deadline_at", "") or "").strip()
    if not deadline and isinstance(usage_meta, dict):
        deadline = str(usage_meta.get("deadline_at") or "").strip()
    from ouroboros.config import get_finalization_grace_sec

    return review_operation_timeout_sec(
        getattr(slot, "timeout_sec", None),
        route=getattr(slot, "route", None),
        deadline_at=deadline,
        transport_timeout_sec=getattr(slot, "transport_timeout_sec", None),
        reserve_sec=get_finalization_grace_sec(),
    )


def _emit_operation(
    usage_ctx: Any,
    *,
    task_id: str,
    request: Any,
    entry: ActiveReviewAttempt,
    slot: Any,
    phase: str,
    **extra: Any,
) -> None:
    if usage_ctx is None:
        return
    try:
        event_queue = getattr(usage_ctx, "event_queue", None)
        values = {
            "task_id": task_id,
            "operation_id": entry.operation_id,
            "phase": phase,
            "kind": "review",
            "task_attempt": getattr(request, "task_attempt", None),
            "execution_id": str(getattr(usage_ctx, "execution_id", "") or ""),
            # ReviewRequest has no separate round carrier; the unique physical
            # operation is the stable round identity for both terminal events.
            "round_id": str(getattr(request, "round_id", "") or entry.operation_id),
            "slot_id": str(getattr(slot, "slot_id", "") or ""),
            **extra,
        }
        if event_queue is not None:
            emit_cognitive_operation_event(event_queue, **values)
            return
        from ouroboros.tools.review_helpers import emit_review_event

        emit_review_event(usage_ctx, {"type": "cognitive_operation", **values})
    except Exception:
        log.debug("review operation event failed", exc_info=True)


def _late_or_timeout_actor(
    slot: Any, entry: Any, timeout: float, error_actor: Callable[..., Any],
) -> Any:
    if entry is not None:
        with _ACTIVE_LOCK:
            if entry.event.is_set() and entry.actor is not None:
                try:
                    return copy.deepcopy(entry.actor)
                except Exception:
                    return entry.actor
            entry.timed_out = True
    actor = error_actor(
        slot,
        f"Timeout after {timeout:g}s; physical review operation remains in flight",
        entry.operation_id if entry is not None else "",
        "in_flight" if entry is not None else "settled",
    )
    if entry is not None and entry.retry_state:
        usage = dict(getattr(actor, "usage", None) or {})
        usage.update({
            key: str(entry.retry_state.get(key) or "")
            for key in ("pending_invocation_id", "delegated_run_id")
            if entry.retry_state.get(key)
        })
        actor.usage = usage
    return actor


def run_custodied_review_slots(
    *,
    request: Any,
    slots: List[Any],
    usage_ctx: Any,
    task_id: str,
    usage_meta: Dict[str, Any],
    review_usage_scope: Any,
    run_slot: Callable[[Any, str, Dict[str, Any], float], Any],
    error_actor: Callable[..., Any],
) -> List[Any]:
    """Run slots with independent logical windows and late-result custody."""
    from ouroboros.usage_accounting import usage_scope

    result_queue: "queue.Queue[Any]" = queue.Queue()
    slot_entries: Dict[str, ActiveReviewAttempt] = {}
    slot_deadlines: Dict[str, float] = {}
    slot_windows: Dict[str, float] = {}
    immediate_actors: Dict[str, Any] = {}
    paid_stamped = False

    def settle(entry: ActiveReviewAttempt, slot: Any, actor: Any) -> None:
        with _ACTIVE_LOCK:
            late = bool(entry.timed_out)
            failure_custody = getattr(actor, "usage", None) or {}
            pending_invocation = str(failure_custody.get("pending_invocation_id") or "")
            custody_lost = str(getattr(actor, "failure_code", "") or "") == "provider_outcome_unknown"
            actor.operation_id = entry.operation_id
            actor.operation_state = (
                "custody_lost" if custody_lost else
                "in_flight" if pending_invocation else "late_settled" if late else "settled"
            )
            actor.late_result_pending = bool(pending_invocation or custody_lost)
            entry.actor = actor
            entry.event.set()
            if _ACTIVE.get(entry.key) is entry:
                _ACTIVE.pop(entry.key, None)
            # API transport errors may retry after settlement. A delegated
            # session has already spent its one run even when it settled as an
            # error, so replay that terminal actor instead of buying another.
            pending = getattr(usage_ctx, "_review_pending_invocations", None)
            if pending_invocation and usage_ctx is not None:
                if not isinstance(pending, dict):
                    pending = {}
                    setattr(usage_ctx, "_review_pending_invocations", pending)
                pending[entry.key] = {
                    "pending_invocation_id": pending_invocation,
                    "operation_id": entry.operation_id,
                }
            if (
                bool(getattr(request, "reconcile_only", False))
                and str(getattr(actor, "failure_code", "") or "") == "review_custody_lost"
                and usage_ctx is not None
            ):
                setattr(usage_ctx, "_review_custody_lost", True)
            explicit_retry = bool(str(getattr(request, "retry_key", "") or "").strip())
            route = getattr(slot, "route", "")
            route_value = str(getattr(route, "value", route) or "")
            # A plan cycle owns one exact paid API attempt.  If its caller's
            # logical window expired and that API attempt later settled as an
            # error, retain the terminal actor so reconciliation can close the
            # same cycle instead of either losing custody or paying again.
            # Agent-session preflight errors remain retryable unless the
            # delegated run actually started; commit-review bindings likewise
            # keep their existing same-binding infrastructure retry semantics.
            late_plan_api_error = bool(
                late
                and explicit_retry
                and str(getattr(request, "surface", "") or "") == "plan_review"
                and route_value != "agent_session"
                and not pending_invocation
            )
            late_commit_api_error = bool(
                late
                and explicit_retry
                and str(getattr(request, "retry_key", "") or "").startswith("commit_review:")
                and route_value != "agent_session"
                and not pending_invocation
            )
            replayable = actor.status in {"ok", "empty"} or late_plan_api_error or late_commit_api_error or bool(
                failure_custody.get("delegated_run_started") and not pending_invocation
            )
            if replayable and usage_ctx is not None and (late or explicit_retry):
                settled = getattr(usage_ctx, "_review_settled_attempts", None)
                if not isinstance(settled, dict):
                    settled = {}
                    setattr(usage_ctx, "_review_settled_attempts", settled)
                try:
                    settled[entry.key] = copy.deepcopy(actor)
                except Exception:
                    log.debug("late review actor copy failed", exc_info=True)
        if not pending_invocation and not custody_lost:
            _emit_operation(
                usage_ctx, task_id=task_id, request=request, entry=entry, slot=slot,
                phase="failed" if actor.status == "error" else "finished",
            )
        if late and not pending_invocation and not custody_lost and usage_ctx is not None:
            try:
                from ouroboros.tools.review_helpers import emit_review_event

                emit_review_event(usage_ctx, {
                    "type": "review_late_result",
                    "task_id": task_id,
                    "surface": getattr(request, "surface", ""),
                    "slot_id": getattr(slot, "slot_id", ""),
                    "operation_id": entry.operation_id,
                    "status": actor.status,
                    "response_ref": actor.response_ref,
                    "operation_state": actor.operation_state,
                })
            except Exception:
                log.debug("late review result event failed", exc_info=True)
        result_queue.put(actor)

    def start(slot: Any) -> None:
        nonlocal paid_stamped
        key = _attempt_key(request, slot)
        slot_id = str(getattr(slot, "slot_id", "") or "")
        window = _logical_timeout(slot, request, usage_meta)
        slot_windows[slot_id] = window
        slot_deadlines[slot_id] = time.monotonic() + window
        custody_lost = False
        with _ACTIVE_LOCK:
            frozen_surfaces = getattr(usage_ctx, "_review_frozen_rows", None)
            frozen_surface = (
                frozen_surfaces.get(str(getattr(request, "surface", "") or ""), {})
                if isinstance(frozen_surfaces, dict) else {}
            )
            frozen_row = frozen_surface.get(slot_id) if isinstance(frozen_surface, dict) else None
            pending_attempts = getattr(usage_ctx, "_review_pending_invocations", None)
            retry_state = dict(
                pending_attempts.get(key, {}) if isinstance(pending_attempts, dict) else {}
            )
            settled = getattr(usage_ctx, "_review_settled_attempts", None)
            cached_actor = settled.get(key) if isinstance(settled, dict) else None
            if bool(getattr(request, "reconcile_only", False)) and isinstance(frozen_row, dict):
                if _row_is_pending(frozen_row):
                    token = str(frozen_row.get("pending_invocation_id") or "")
                    if not retry_state and token:
                        retry_state = {
                            "pending_invocation_id": token,
                            "operation_id": str(frozen_row.get("operation_id") or ""),
                        }
                else:
                    cached_actor = _frozen_actor(frozen_row, slot)
            retry_payload = dict(retry_state)
            retry_payload.pop("operation_id", None)
            if cached_actor is not None:
                try:
                    cached_actor = copy.deepcopy(cached_actor)
                except Exception:
                    log.debug("cached review actor copy failed", exc_info=True)
            entry = _ACTIVE.get(key)
            owner = entry is None and cached_actor is None
            if cached_actor is not None:
                entry = ActiveReviewAttempt(
                    key=key,
                    operation_id=str(cached_actor.operation_id or new_call_id("review_replay")),
                    actor=cached_actor,
                )
                entry.event.set()
            elif (
                entry is None
                and bool(getattr(request, "reconcile_only", False))
                and not retry_state
            ):
                custody_lost = True
                owner = False
            elif entry is None and window > 0:
                entry = ActiveReviewAttempt(
                    key=key,
                    operation_id=str(retry_state.get("operation_id") or "") or new_call_id(
                        f"review_{getattr(request, 'surface', 'review')}_{getattr(slot, 'slot_id', 'slot')}"),
                    retry_state=retry_payload,
                )
                _ACTIVE[key] = entry
                if isinstance(pending_attempts, dict):
                    pending_attempts.pop(key, None)
        if custody_lost:
            if usage_ctx is not None:
                setattr(usage_ctx, "_review_custody_lost", True)
            immediate_actors[slot_id] = error_actor(
                slot,
                "Exact review custody is unavailable; refusing a second paid dispatch",
                "",
                "custody_lost",
            )
            return
        if entry is None:
            immediate_actors[slot_id] = error_actor(
                slot, "Owner deadline exhausted before physical review dispatch",
                "", "not_dispatched",
            )
            return
        slot_entries[slot_id] = entry
        if window <= 0 and cached_actor is None:
            with _ACTIVE_LOCK:
                entry.timed_out = True
            immediate_actors[slot_id] = error_actor(
                slot, "Owner deadline exhausted while the physical review remains in flight",
                entry.operation_id, "in_flight",
            )
            return
        if owner:
            if not paid_stamped and not retry_state:
                from ouroboros.review_dispatch import stamp_review_paid_on_dispatch

                try:
                    stamp_review_paid_on_dispatch(usage_ctx)
                except Exception:
                    with _ACTIVE_LOCK:
                        if _ACTIVE.get(entry.key) is entry:
                            _ACTIVE.pop(entry.key, None)
                    slot_entries.pop(slot_id, None)
                    raise
                paid_stamped = True
            def worker() -> None:
                _emit_operation(
                    usage_ctx, task_id=task_id, request=request, entry=entry, slot=slot,
                    phase="started",
                )
                try:
                    with usage_scope(review_usage_scope):
                        actor = run_slot(
                            slot, entry.operation_id, entry.retry_state,
                            slot_deadlines[slot_id],
                        )
                except Exception as exc:
                    actor = error_actor(
                        slot, f"{type(exc).__name__}: {exc}", entry.operation_id,
                    )
                settle(entry, slot, actor)

            threading.Thread(
                target=worker,
                name=f"ouroboros-review-{getattr(request, 'surface', 'review')}-{slot_id}",
                daemon=True,
            ).start()
        elif cached_actor is not None:
            result_queue.put(cached_actor)
        else:
            def relay() -> None:
                entry.event.wait()
                if entry.actor is not None:
                    try:
                        result_queue.put(copy.deepcopy(entry.actor))
                    except Exception:
                        result_queue.put(entry.actor)

            threading.Thread(
                target=relay,
                name=f"ouroboros-review-relay-{slot_id}",
                daemon=True,
            ).start()

    for slot in slots:
        start(slot)

    actors: List[Any] = list(immediate_actors.values())
    pending = {
        str(getattr(slot, "slot_id", "") or "") for slot in slots
        if str(getattr(slot, "slot_id", "") or "") not in immediate_actors
    }
    while pending:
        now = time.monotonic()
        expired = {slot_id for slot_id in pending if slot_deadlines[slot_id] <= now}
        for slot_id in expired:
            pending.remove(slot_id)
        if not pending:
            break
        remaining = min(slot_deadlines[slot_id] - now for slot_id in pending)
        try:
            actor = result_queue.get(timeout=remaining)
        except queue.Empty:
            continue
        if actor.slot_id in pending:
            actors.append(actor)
            pending.remove(actor.slot_id)

    returned_ids = {str(getattr(actor, "slot_id", "") or "") for actor in actors}
    for slot in slots:
        slot_id = str(getattr(slot, "slot_id", "") or "")
        if slot_id in returned_ids:
            continue
        entry = slot_entries.get(slot_id)
        timeout = slot_windows.get(slot_id)
        if timeout is None:
            timeout = _logical_timeout(slot, request, usage_meta)
        actors.append(_late_or_timeout_actor(slot, entry, timeout, error_actor))
    return actors


def review_retry_custody_available(
    *,
    retry_key: str,
    surface: str,
    task_id: str,
    call_type: str,
    session_root: str,
    slots: List[Any],
    usage_ctx: Any,
) -> bool:
    """Whether a same-cycle retry can join/replay without a new dispatch.

    This is intentionally process-local, matching the custody store itself. A
    caller that recovered a durable ``in_flight`` wave after process loss must
    report the outcome as unknown instead of turning absence of local custody
    into permission for a second paid send.
    """
    request = SimpleNamespace(
        retry_key=str(retry_key or ""),
        surface=str(surface or ""),
        task_id=str(task_id or ""),
        call_type=str(call_type or ""),
        session_root=str(session_root or ""),
    )
    settled = getattr(usage_ctx, "_review_settled_attempts", None)
    pending = getattr(usage_ctx, "_review_pending_invocations", None)
    with _ACTIVE_LOCK:
        for slot in slots:
            key = _attempt_key(request, slot)
            if key in _ACTIVE:
                continue
            if isinstance(settled, dict) and key in settled:
                continue
            if isinstance(pending, dict) and key in pending:
                continue
            return False
    return True
