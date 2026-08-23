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
from typing import Any, Callable, Dict, List

from ouroboros.deadline_utils import llm_transport_timeout_sec, logical_operation_timeout_sec
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


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Dict[str, ActiveReviewAttempt] = {}


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
    payload = {
        "retry_key": retry_key,
        "surface": getattr(request, "surface", ""),
        "task_id": getattr(request, "task_id", ""),
        "call_type": getattr(request, "call_type", ""),
        "slot": slot_data,
        "messages": slot_messages if slot_messages is not None else getattr(request, "messages", []),
        "goal": getattr(request, "goal", ""),
        "scope": getattr(request, "scope", ""),
        "subject": getattr(request, "subject", ""),
        "evidence_refs": getattr(request, "evidence_refs", []),
        "evidence": getattr(request, "evidence", {}),
        "policy": getattr(request, "policy", {}),
        "session_root": getattr(request, "session_root", ""),
        "session_task": getattr(request, "session_task", ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _logical_timeout(slot: Any, request: Any, usage_meta: Dict[str, Any]) -> float:
    deadline = str(getattr(request, "deadline_at", "") or "").strip()
    if not deadline and isinstance(usage_meta, dict):
        deadline = str(usage_meta.get("deadline_at") or "").strip()
    transport = llm_transport_timeout_sec(getattr(slot, "transport_timeout_sec", None))
    from ouroboros.config import get_finalization_grace_sec

    return logical_operation_timeout_sec(
        getattr(slot, "timeout_sec", None),
        deadline_at=deadline,
        fallback=transport,
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


def run_custodied_review_slots(
    *,
    request: Any,
    slots: List[Any],
    usage_ctx: Any,
    task_id: str,
    usage_meta: Dict[str, Any],
    review_usage_scope: Any,
    run_slot: Callable[[Any, str], Any],
    error_actor: Callable[..., Any],
) -> List[Any]:
    """Run slots with independent logical windows and late-result custody."""
    from ouroboros.usage_accounting import usage_scope

    result_queue: "queue.Queue[Any]" = queue.Queue()
    slot_entries: Dict[str, ActiveReviewAttempt] = {}
    slot_deadlines: Dict[str, float] = {}
    slot_windows: Dict[str, float] = {}
    def settle(entry: ActiveReviewAttempt, slot: Any, actor: Any) -> None:
        with _ACTIVE_LOCK:
            late = bool(entry.timed_out)
            actor.operation_id = entry.operation_id
            actor.operation_state = "late_settled" if late else "settled"
            actor.late_result_pending = False
            entry.actor = actor
            entry.event.set()
            if _ACTIVE.get(entry.key) is entry:
                _ACTIVE.pop(entry.key, None)
            if late and usage_ctx is not None:
                settled = getattr(usage_ctx, "_review_settled_attempts", None)
                if not isinstance(settled, dict):
                    settled = {}
                    setattr(usage_ctx, "_review_settled_attempts", settled)
                try:
                    settled[entry.key] = copy.deepcopy(actor)
                except Exception:
                    log.debug("late review actor copy failed", exc_info=True)
        _emit_operation(
            usage_ctx, task_id=task_id, request=request, entry=entry, slot=slot,
            phase="failed" if actor.status == "error" else "finished",
        )
        if late and usage_ctx is not None:
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
        key = _attempt_key(request, slot)
        with _ACTIVE_LOCK:
            settled = getattr(usage_ctx, "_review_settled_attempts", None)
            cached_actor = settled.get(key) if isinstance(settled, dict) else None
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
            elif entry is None:
                entry = ActiveReviewAttempt(
                    key=key,
                    operation_id=new_call_id(
                        f"review_{getattr(request, 'surface', 'review')}_{getattr(slot, 'slot_id', 'slot')}"
                    ),
                )
                _ACTIVE[key] = entry
        slot_id = str(getattr(slot, "slot_id", "") or "")
        slot_entries[slot_id] = entry
        slot_windows[slot_id] = _logical_timeout(slot, request, usage_meta)
        slot_deadlines[slot_id] = time.monotonic() + slot_windows[slot_id]
        if owner:
            def worker() -> None:
                _emit_operation(
                    usage_ctx, task_id=task_id, request=request, entry=entry, slot=slot,
                    phase="started",
                )
                try:
                    with usage_scope(review_usage_scope):
                        actor = run_slot(slot, entry.operation_id)
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

    actors: List[Any] = []
    pending = {str(getattr(slot, "slot_id", "") or "") for slot in slots}
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
        if entry is not None:
            with _ACTIVE_LOCK:
                if entry.event.is_set() and entry.actor is not None:
                    try:
                        actor = copy.deepcopy(entry.actor)
                    except Exception:
                        actor = entry.actor
                    actors.append(actor)
                    returned_ids.add(slot_id)
                    continue
                entry.timed_out = True
        timeout = slot_windows.get(slot_id)
        if timeout is None:
            timeout = _logical_timeout(slot, request, usage_meta)
        actors.append(error_actor(
            slot,
            f"Timeout after {timeout:g}s; physical review operation remains in flight",
            entry.operation_id if entry is not None else "",
            "in_flight" if entry is not None else "settled",
        ))
    return actors
