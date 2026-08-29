"""Durable root post-task phase and final-cost checkpoint helpers."""

from __future__ import annotations

import logging
import pathlib
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from ouroboros.cost_projection import honest_accounted_amount, with_cost_aliases
from ouroboros.task_results import (
    TASK_COST_META_FIELDS,
    STATUS_COMPLETED,
    load_task_result,
    resolve_task_lineage,
    write_task_result,
)
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

POST_TASK_SYNTHESIS_LOCK = threading.Lock()
POST_TASK_SYNTHESIS_INFLIGHT: set[tuple[str, str]] = set()
POST_TASK_SYNTHESIS_OPEN_STATUSES = frozenset({"pending_once", "running"})
POST_TASK_SYNTHESIS_TERMINAL_STATUSES = frozenset({"completed", "degraded"})
_TERMINAL_ACCOUNTING_FIELDS = (
    *TASK_COST_META_FIELDS,
    "total_rounds",
    "prompt_tokens",
    "completion_tokens",
)


def post_task_synthesis_is_open(value: Any) -> bool:
    """Return whether a root still owes post-task synthesis."""
    return str(value or "") in POST_TASK_SYNTHESIS_OPEN_STATUSES


def post_task_synthesis_is_terminal(value: Any) -> bool:
    """Return whether canonical post-task synthesis has settled."""
    return str(value or "") in POST_TASK_SYNTHESIS_TERMINAL_STATUSES


def _parse_updated_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def project_replica_task_result_fields(
    canonical_fields: Dict[str, Any],
    replica_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the replica overlay permitted over a canonical task result.

    A terminal canonical post-task checkpoint owns only its synthesis fields
    and the accounting snapshot written with that checkpoint. The replica
    continues to own acceptance, result, review, and trace fields. ``updated_at``
    is monotonic projection metadata only; it never selects field authority.
    """
    overlay = dict(replica_fields)
    canonical_checkpoint = canonical_fields.get("root_phase_checkpoint")
    canonical_post_task = (
        str(canonical_checkpoint.get("post_task_synthesis") or "")
        if isinstance(canonical_checkpoint, dict)
        else ""
    )
    if post_task_synthesis_is_terminal(canonical_post_task):
        replica_checkpoint = overlay.get("root_phase_checkpoint")
        merged_checkpoint = dict(canonical_checkpoint)
        if isinstance(replica_checkpoint, dict):
            merged_checkpoint.update(replica_checkpoint)
        merged_checkpoint["post_task_synthesis"] = canonical_post_task
        if "post_task_stop_reason" in canonical_checkpoint:
            merged_checkpoint["post_task_stop_reason"] = canonical_checkpoint[
                "post_task_stop_reason"
            ]
        overlay["root_phase_checkpoint"] = merged_checkpoint
        for field in _TERMINAL_ACCOUNTING_FIELDS:
            overlay.pop(field, None)

    # Non-Project split synthesis writes this field in the canonical parent
    # root.  A later child replica must not replace it with stale child text.
    if isinstance(canonical_fields.get("continuation_narrative"), dict):
        if str(canonical_fields["continuation_narrative"].get("text") or "").strip():
            overlay.pop("continuation_narrative", None)

    canonical_updated_at = _parse_updated_at(canonical_fields.get("updated_at"))
    replica_updated_at = _parse_updated_at(overlay.get("updated_at"))
    if canonical_updated_at is not None and (
        replica_updated_at is None or canonical_updated_at > replica_updated_at
    ):
        overlay["updated_at"] = canonical_fields["updated_at"]
    return overlay


def project_root_post_task_checkpoint_fields(
    canonical_fields: Dict[str, Any],
    patch_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge a root post-task patch against the CURRENT canonical checkpoint.

    The root writer owns only post-task synthesis and its accounting snapshot;
    acceptance remains whatever the current record says. Once post-task state
    is terminal, an open or different-terminal stale patch cannot replace that
    state or its accounting. A same-terminal patch remains valid so the
    proactive namer's explicit ``refresh`` can update the final cost snapshot.
    """
    overlay = dict(patch_fields)
    if canonical_fields.get("status"):
        # This writer enriches the current lifecycle record; it never owns a
        # possibly stale pre-lock lifecycle transition.
        overlay["status"] = canonical_fields["status"]
    canonical_checkpoint = canonical_fields.get("root_phase_checkpoint")
    patch_checkpoint = overlay.get("root_phase_checkpoint")
    current = (
        dict(canonical_checkpoint)
        if isinstance(canonical_checkpoint, dict)
        else {"phase": "task_acceptance", "status": "not_required", "pass_index": 0}
    )
    patch = dict(patch_checkpoint) if isinstance(patch_checkpoint, dict) else {}
    canonical_post_task = str(current.get("post_task_synthesis") or "")
    patch_post_task = str(patch.get("post_task_synthesis") or "")

    if post_task_synthesis_is_terminal(canonical_post_task):
        current["post_task_synthesis"] = canonical_post_task
        if isinstance(canonical_checkpoint, dict) and "post_task_stop_reason" in canonical_checkpoint:
            current["post_task_stop_reason"] = canonical_checkpoint[
                "post_task_stop_reason"
            ]
        if patch_post_task != canonical_post_task:
            for field in _TERMINAL_ACCOUNTING_FIELDS:
                overlay.pop(field, None)
    else:
        if "post_task_stop_reason" in patch:
            current["post_task_stop_reason"] = patch["post_task_stop_reason"]
        if patch_post_task:
            current["post_task_synthesis"] = patch_post_task
    overlay["root_phase_checkpoint"] = current
    return overlay


def is_root_post_task(task: Dict[str, Any]) -> bool:
    """Structural root test for the single global post-task synthesis authority."""
    if bool(task.get("_skip_post_task_synthesis")):
        return False
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    task_id = str(task.get("id") or task.get("task_id") or "")
    return bool(resolve_task_lineage(
        task_id,
        metadata=meta,
        root_task_id=task.get("root_task_id"),
        parent_task_id=task.get("parent_task_id"),
        delegation_role=task.get("delegation_role"),
        original_task_id=task.get("original_task_id"),
        timeout_retry_from=task.get("timeout_retry_from"),
    )["is_root_task"])


def root_checkpoint_roots(env: Any, task: Dict[str, Any]) -> list[pathlib.Path]:
    """Return the one durable phase authority (compatibility list shape)."""
    raw = task.get("budget_drive_root") or getattr(env, "drive_root", None)
    if not raw:
        return []
    try:
        return [pathlib.Path(raw).resolve(strict=False)]
    except (TypeError, OSError, ValueError):
        return []


def set_root_post_task_checkpoint(
    env: Any,
    task: Dict[str, Any],
    status: str,
    *,
    stop_reason: str = "",
) -> Dict[str, Any] | None:
    """Merge the phase marker and return the record actually stored, if any."""
    if not is_root_post_task(task):
        return
    task_id = str(task.get("id") or task.get("task_id") or "")
    if not task_id:
        return
    requested_status = str(status)
    roots = root_checkpoint_roots(env, task)
    if not roots:
        return
    authority_root = roots[0]
    finalized_event: Dict[str, Any] | None = None
    # The proactive namer can settle concurrently with post-task synthesis. A
    # shared critical section makes its refresh and the final snapshot linear.
    with POST_TASK_SYNTHESIS_LOCK:
        existing = load_task_result(authority_root, task_id) or {}
        checkpoint = existing.get("root_phase_checkpoint")
        saved = str(checkpoint.get("post_task_synthesis") or "") if isinstance(checkpoint, dict) else ""
        effective_status = saved if requested_status == "refresh" and saved else requested_status
        cost_fields: Dict[str, Any] = {"cost_final": False, "cost_with_children_partial": True}
        if post_task_synthesis_is_terminal(effective_status):
            try:
                from ouroboros.usage_accounting import usage_breakdown
                from supervisor.state import reconstruct_task_cost

                cost_fields.update(reconstruct_task_cost(task_id, fields=True, drive_root=authority_root))
                metadata = (
                    task.get("metadata")
                    if isinstance(task.get("metadata"), dict)
                    else {}
                )
                logical_root_id = str(
                    task.get("root_task_id")
                    or metadata.get("root_task_id")
                    or task_id
                )
                subtree = usage_breakdown(
                    authority_root, root_task_id=logical_root_id
                )
                subtree_final = bool(subtree.get("cost_final"))
                subtree_amount = honest_accounted_amount(subtree)
                cost_fields.update({
                    "cost_usd_with_children": (
                        round(subtree_amount, 6) if subtree_amount is not None else None
                    ),
                    "cost_with_children_partial": not subtree_final,
                    "cost_final": bool(cost_fields.get("cost_final") and subtree_final),
                })
            except Exception:
                log.error("Failed to refresh final root cost projection for %s", task_id, exc_info=True)
                cost_fields.update({
                    "cost_accounting_status": "unavailable",
                    "cost_accounting_error": "ledger_unavailable",
                    "cost_usd": None,
                    "cost_usd_with_children": None,
                })
        # SSOT cost naming (C2/F12): re-converge the alias pairs as the LAST step,
        # after every branch above has finished mutating the deprecated spellings
        # (`reconstruct_task_cost` aliases at ITS seam, then the subtree refresh
        # and the unavailable fallback edit `cost_usd[_with_children]` only) —
        # otherwise this producer persists a DIVERGED pair: an honest name still
        # carrying the pre-refresh amount beside a corrected alias.
        cost_fields = with_cost_aliases(cost_fields)
        checkpoint_patch = {"post_task_synthesis": effective_status}
        if stop_reason:
            checkpoint_patch["post_task_stop_reason"] = str(stop_reason)
        stored: Dict[str, Any] | None = None
        try:
            stored = write_task_result(
                authority_root,
                task_id,
                str(existing.get("status") or task.get("status") or STATUS_COMPLETED),
                _field_projector=project_root_post_task_checkpoint_fields,
                root_task_id=str(task.get("root_task_id") or task_id),
                parent_task_id=task.get("parent_task_id"),
                budget_drive_root=str(authority_root),
                child_drive_root=task.get("child_drive_root") or task.get("drive_root"),
                project_id=str(task.get("project_id") or ""),
                root_phase_checkpoint=checkpoint_patch,
                **cost_fields,
            )
        except Exception:
            log.debug("Failed to update root post-task checkpoint", exc_info=True)
        stored_checkpoint = (
            stored.get("root_phase_checkpoint") if isinstance(stored, dict) else None
        )
        stored_post_task = (
            str(stored_checkpoint.get("post_task_synthesis") or "")
            if isinstance(stored_checkpoint, dict)
            else ""
        )
        if post_task_synthesis_is_terminal(stored_post_task):
            finalized_event = {
                "type": "task_cost_finalized",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "root_task_id": str(stored.get("root_task_id") or task.get("root_task_id") or task_id),
                "post_task_status": stored_post_task,
                **{
                    field: stored[field]
                    for field in _TERMINAL_ACCOUNTING_FIELDS
                    if field in stored
                },
            }
    if finalized_event is not None:
        try:
            append_jsonl(authority_root / "logs" / "events.jsonl", finalized_event)
        except Exception:
            log.warning("Failed to persist finalized task cost for %s", task_id, exc_info=True)
        else:
            # v6.74.0 (D3): the durable append above is the record of truth; the
            # live UI push is best-effort. `get_bridge()` ASSERTS `init()` was
            # called and raised in post-task contexts without a live bus
            # (benchmark workers, headless finalization) — pure log noise.
            # `try_get_bridge` pushes only when a bridge actually exists.
            try:
                from supervisor.message_bus import try_get_bridge

                bridge = try_get_bridge()
                if bridge is not None:
                    # A bridge exists only in the server process, where the
                    # live RUNNING table is available for addressing.
                    from supervisor.log_addressing import address_handler_push

                    bridge.push_log(address_handler_push(authority_root, dict(finalized_event)))
            except Exception:
                log.debug("Live push of finalized task cost skipped for %s", task_id, exc_info=True)
    pending_projection = (
        stored.get("canonical_terminal_projection_ready")
        if isinstance(stored, dict) else None
    )
    if (
        isinstance(pending_projection, dict)
        and post_task_synthesis_is_terminal(stored_post_task)
        and not isinstance((stored or {}).get("canonical_terminal_projection"), dict)
    ):
        try:
            from ouroboros.project_dialogue import append_terminal_task_projection

            projection_task = {**task, **(stored or {}), "id": task_id}
            append_terminal_task_projection(
                authority_root,
                task_id,
                projection_task,
                stored or {},
                {
                    "ts": str(pending_projection.get("task_done_ts") or utc_now_iso()),
                    "chat_id": int(pending_projection.get("chat_id") or 0),
                    "status": str((stored or {}).get("status") or STATUS_COMPLETED),
                },
            )
        except Exception:
            log.warning(
                "Failed to settle canonical terminal projection for %s",
                task_id,
                exc_info=True,
            )
    return stored


def root_post_task_already_completed(env: Any, task: Dict[str, Any]) -> bool:
    if not is_root_post_task(task):
        return False
    task_id = str(task.get("id") or task.get("task_id") or "")
    roots = root_checkpoint_roots(env, task)
    existing = load_task_result(roots[0], task_id) if roots and task_id else None
    checkpoint = existing.get("root_phase_checkpoint") if isinstance(existing, dict) else None
    return bool(
        isinstance(checkpoint, dict)
        and post_task_synthesis_is_terminal(checkpoint.get("post_task_synthesis"))
    )
