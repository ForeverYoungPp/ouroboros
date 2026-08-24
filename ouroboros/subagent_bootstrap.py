"""Pre-first-model bootstrap for configured session nannies."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from ouroboros.subagent_work_order import (
    WorkOrderBudgetExceeded,
    build_work_order_source_request,
    compile_external_work_order,
    route_source_request_channel,
)


def bootstrap_before_context(ctx: Any, task: Mapping[str, Any], dispatch: Any) -> str:
    """Freeze the selected route before the first ordinary actor episode.

    A configured session no longer starts a new physical leaf from this seam.
    The host records the exact work-order authority and lets the model choose
    children, a leaf start, or an honest zero-run. Proven recovery handoffs are
    still adopted for continuity.
    """

    configured = isinstance(task.get("configured_subagent"), dict)
    ctx.exact_model_route = configured
    if not configured or dispatch is None:
        return ""
    snapshot = task.get("configured_subagent") if isinstance(task.get("configured_subagent"), dict) else {}
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    if str(route.get("kind") or "") == "agent_session":
        # Hydrate immutable route/work-order authority before every recovery
        # branch. A recovered or already-settled physical run must never fall
        # back to the generic selector/prompt path on the resumed nanny.
        actor_ready = _prepare_actor_first_bootstrap(ctx, task, dispatch)
        recovery = _adopt_recovery_handoff(ctx, task)
        if recovery:
            return recovery
        if bool(getattr(dispatch, "blocked", False)):
            # A route fault is evidence for the ordinary host turn, never a
            # reason to silently spend on native/API fallback.
            from ouroboros import delegate_custody as custody
            from ouroboros.subagent_runtime import current_subagent_alternatives

            availability = task.get("subagent_availability") if isinstance(task.get("subagent_availability"), dict) else {}
            resolution = getattr(dispatch, "executor_resolution", None)
            actor_bootstrap = getattr(ctx, "_configured_actor_bootstrap", {})
            actor_bootstrap = actor_bootstrap if isinstance(actor_bootstrap, dict) else {}
            resolved_route = getattr(resolution, "route", None)
            startup = {
                "status": "temporarily_unavailable",
                "reason": str(
                    getattr(resolution, "reason", "") or availability.get("reason")
                    or "configured_session_unavailable"
                ),
                "reset_at": str(getattr(resolution, "reset_at", "") or availability.get("reset_at") or ""),
                "selected_subagent_id": str(snapshot.get("selected_subagent_id") or ""),
                "route": str(
                    getattr(resolved_route, "route_id", "")
                    or actor_bootstrap.get("route_id")
                    or route.get("target_id") or ""
                ),
                "work_order_fingerprint": str(actor_bootstrap.get("work_order_fingerprint") or ""),
                "work_order_chars": int(actor_bootstrap.get("work_order_chars") or 0),
                "work_order_complete": bool(actor_bootstrap.get("canonical_work_order")),
                "alternatives": current_subagent_alternatives(
                    str(snapshot.get("selected_subagent_id") or "")
                ),
                "host_fallback": False,
                "actor_first": True,
                "exact_start_pending": bool(actor_bootstrap.get("exact_start_pending", True)),
                **({
                    "zero_run_receipt_recorded": True,
                    "zero_run_decision": str(actor_bootstrap.get("zero_run_decision") or ""),
                } if actor_bootstrap.get("zero_run_receipt_recorded") else {}),
                **({
                    "zero_run_evidence_status": "unknown",
                    "zero_run_evidence_gaps": list(
                        actor_bootstrap.get("zero_run_evidence_gaps") or []
                    ),
                } if actor_bootstrap.get("zero_run_evidence_status") == "unknown" else {}),
            }
            custody.emit(custody.custody_root(ctx), "configured_subagent_startup_fault", {
                "task_id": str(getattr(ctx, "task_id", "") or ""), **startup,
            })
            return json.dumps({
                "status": "configured_session_actor_ready", "startup": startup,
            }, ensure_ascii=False, indent=2)
        return actor_ready
    return ""


def _adopt_recovery_handoff(ctx: Any, task: Mapping[str, Any]) -> str:
    """Adopt a proven successor before considering a fresh actor episode."""
    from ouroboros.delegate_recovery import adopt_handoff

    adoption = adopt_handoff(ctx, task)
    status = str(adoption.get("status") or "")
    if status == "none":
        return ""
    if status == "recovery_required":
        return json.dumps({
            "status": "configured_session_recovery_wake", "recovery": adoption,
        }, ensure_ascii=False, indent=2)
    if status == "settled_recovered":
        _mark_physical_activity(ctx)
        return json.dumps({
            "status": "configured_session_recovered_wake",
            "recovery": adoption,
            "wake": adoption.get("wake") if isinstance(adoption.get("wake"), dict) else {},
        }, ensure_ascii=False, indent=2)
    if status != "adopted":
        return json.dumps({
            "status": "configured_session_recovery_wake", "recovery": adoption,
        }, ensure_ascii=False, indent=2)
    pending_wake = adoption.get("wake") if isinstance(adoption.get("wake"), dict) else {}
    _mark_physical_activity(ctx)
    if pending_wake:
        return json.dumps({
            "status": "configured_session_recovered_wake",
            "recovery": adoption, "wake": pending_wake,
        }, ensure_ascii=False, indent=2)
    run_id = str(adoption.get("run_id") or "")
    if not run_id:
        return json.dumps({
            "status": "configured_session_recovery_wake",
            "recovery": {
                **adoption, "status": "recovery_required",
                "reason": "adopted_without_run_id",
            },
        }, ensure_ascii=False, indent=2)
    from ouroboros.delegate_supervision import supervised_wait

    wake_raw = supervised_wait(ctx, run_id)
    try:
        wake = json.loads(wake_raw)
    except (TypeError, ValueError):
        wake = {"status": "wake_fault", "detail": wake_raw}
    return json.dumps({
        "status": "configured_session_recovered_wake",
        "recovery": adoption, "wake": wake,
    }, ensure_ascii=False, indent=2)


def _mark_physical_activity(ctx: Any) -> None:
    """Tell nanny economics that an existing physical run was adopted."""
    ctx._nanny_physical_activity_seed = True
    bootstrap = getattr(ctx, "_configured_actor_bootstrap", None)
    if isinstance(bootstrap, dict):
        bootstrap["physical_started"] = True
        bootstrap["exact_start_pending"] = False


def actor_first_unresolved_fact(
    ctx: Any, *, task_id: str = "", drive_root: Any = None,
) -> dict[str, Any] | None:
    """Return a typed terminal fact when an actor-first turn has no completion path.

    A plain final answer cannot prove that a configured actor either started its
    assigned leaf or deliberately chose a zero-run. A completed, non-discarded
    direct-child result is a legitimate host-side path; a merely failed/cancelled
    child is only an attempted path and cannot make the actor clean. Existing
    custody/absorption gates remain authoritative. Failure to read the child store
    is itself ``unknown`` rather than permission to finalize cleanly.
    """
    bootstrap = getattr(ctx, "_configured_actor_bootstrap", None)
    if not isinstance(bootstrap, dict):
        return None
    if bool(bootstrap.get("physical_started")) or bool(bootstrap.get("zero_run_receipt_recorded")):
        return None
    zero_run_evidence_unknown = (
        str(bootstrap.get("zero_run_evidence_status") or "") == "unknown"
    )
    if not bool(bootstrap.get("exact_start_pending", True)) and not zero_run_evidence_unknown:
        return None
    root = Path(
        str(
            drive_root
            or getattr(ctx, "budget_drive_root", "")
            or getattr(ctx, "drive_root", "")
            or "."
        )
    )
    child_id = str(task_id or getattr(ctx, "task_id", "") or "")
    try:
        from ouroboros.task_status import find_child_tasks

        children = find_child_tasks(
            root,
            parent_task_id=child_id,
            exclude_task_id=child_id,
            scope="direct",
            materialize_artifacts=True,
        )
    except Exception as exc:  # noqa: BLE001 - unknown evidence must stay visible
        return {
            "status": "unknown",
            "reason": "child_evidence_unavailable",
            "detail": type(exc).__name__,
            "route_available": bool(bootstrap.get("route_available")),
        }
    completed_children = [
        child for child in children
        if str(child.get("status") or "").strip().lower() == "completed"
        and str(child.get("child_result_disposition") or "").strip().lower()
        not in {"irrelevant", "deferred"}
    ]
    if completed_children:
        return None
    if zero_run_evidence_unknown:
        return {
            "status": "unknown",
            "reason": "zero_run_evidence_unavailable",
            "zero_run": True,
            "zero_run_evidence_status": "unknown",
            "zero_run_evidence_gaps": list(
                bootstrap.get("zero_run_evidence_gaps") or []
            ),
            "route_available": bootstrap.get("route_available"),
        }
    route_available = bootstrap.get("route_available")
    return {
        "status": "incomplete" if route_available is not False else "unknown",
        "reason": (
            "direct_children_without_completed_result"
            if children else "physical_leaf_not_started_and_no_direct_child"
        ),
        "route_available": route_available,
        "selected_subagent_id": str(bootstrap.get("selected_subagent_id") or ""),
        "work_order_fingerprint": str(bootstrap.get("work_order_fingerprint") or ""),
        **({
            "direct_child_statuses": sorted({
                str(child.get("status") or "unknown") for child in children
            }),
        } if children else {}),
    }


def _durable_zero_run_receipt(
    ctx: Any, *, gap_reasons: set[str] | None = None,
) -> dict[str, Any] | None:
    """Recover a previously written terminal zero-run fact for this task.

    The in-memory bootstrap marker is intentionally process-local, while the
    receipt is the continuity authority.  A resumed actor therefore has to
    hydrate the marker before it can expose ``delegate_start`` again. A
    gap-aware read distinguishes clean absence from malformed or unreadable
    authority. A valid terminal row still wins, while a store with gaps and no
    valid terminal row leaves the actor in typed UNKNOWN and fences a new start.
    """
    task_id = str(getattr(ctx, "task_id", "") or "")
    if not task_id:
        return None
    try:
        from ouroboros.tool_access import canonical_data_root

        canonical_root = canonical_data_root(ctx)
    except Exception:
        canonical_root = Path(
            str(
                getattr(ctx, "budget_drive_root", None)
                or getattr(ctx, "drive_root", None)
                or "."
            )
        )
    roots = []
    local_root = getattr(ctx, "drive_root", None)
    if local_root:
        roots.append(Path(str(local_root)).resolve(strict=False))
    roots.append(canonical_root)
    try:
        from ouroboros.outcomes import read_verification_receipts_from_roots

        receipts = read_verification_receipts_from_roots(
            roots, task_id, gap_reasons=gap_reasons,
        )
    except Exception:
        if gap_reasons is not None:
            gap_reasons.add("verification_receipts_unavailable")
        return None
    for receipt in reversed(receipts or []):
        if not isinstance(receipt, dict):
            continue
        if (
            str(receipt.get("contract_kind") or "") == "delegation_zero_run"
            and bool(receipt.get("zero_run"))
        ):
            return dict(receipt)
    return None


def actor_first_terminal_projection(
    ctx: Any, task: Mapping[str, Any], usage: Mapping[str, Any],
    llm_trace: Mapping[str, Any], drive_root: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Attach the unresolved actor fact to the existing terminal projections."""
    if ctx is None:
        return None, dict(usage or {}), dict(llm_trace or {})
    bootstrap = getattr(ctx, "_configured_actor_bootstrap", None)
    if isinstance(bootstrap, dict) and bool(bootstrap.get("zero_run_receipt_recorded")):
        decision = str(bootstrap.get("zero_run_decision") or "unknown").strip().lower()
        if decision in {"incomplete", "unknown"}:
            fact = {
                "status": decision,
                "reason": f"configured_actor_zero_run_{decision}",
                "zero_run": True,
                "zero_run_decision": decision,
                "zero_run_basis": str(bootstrap.get("zero_run_basis") or ""),
                "route_available": bootstrap.get("route_available"),
            }
        else:
            return None, dict(usage or {}), dict(llm_trace or {})
    else:
        fact = None
    try:
        if fact is None:
            fact = actor_first_unresolved_fact(
                ctx, task_id=str(task.get("id") or ""), drive_root=drive_root,
            )
    except Exception:
        if not isinstance(bootstrap, dict):
            return None, dict(usage or {}), dict(llm_trace or {})
        fact = {
            "status": "unknown",
            "reason": "actor_terminal_projection_unavailable",
            "route_available": bootstrap.get("route_available"),
        }
    if not fact:
        return None, dict(usage or {}), dict(llm_trace or {})
    updated_usage = dict(usage or {})
    updated_usage["actor_first_terminal"] = dict(fact)
    updated_trace = dict(llm_trace or {})
    updated_trace["actor_first_terminal"] = dict(fact)
    return fact, updated_usage, updated_trace


def _prepare_actor_first_bootstrap(
    ctx: Any, task: Mapping[str, Any], dispatch: Any,
) -> str:
    """Freeze exact actor authority while keeping a new physical start pending."""
    snapshot = task.get("configured_subagent") if isinstance(task.get("configured_subagent"), dict) else {}
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    try:
        work_order = compile_external_work_order(task)
        work_order_fingerprint = sha256(work_order.encode("utf-8")).hexdigest()
        work_order_chars = len(work_order)
        source_prompt = ""
        source_request: dict[str, Any] = {}
        source_channel: dict[str, Any] = {}
    except WorkOrderBudgetExceeded as exc:
        source_prompt, source_request = build_work_order_source_request(task, exc)
        source_channel = {"status": "unverified", "reason": "not_checked"}
        route_id = str(route.get("target_id") or "")
        resolved_route = getattr(getattr(dispatch, "executor_resolution", None), "route", None)
        channel_route_id = str(getattr(resolved_route, "route_id", "") or route_id)
        gateway = None
        try:
            from ouroboros.claudexor_daemon import ensure_owned_gateway

            gateway = ensure_owned_gateway()
            source_channel = route_source_request_channel(gateway, channel_route_id)
        except Exception as channel_error:  # noqa: BLE001 - unknown is typed
            source_channel = {
                "status": "unverified",
                "reason": "capability_probe_failed",
                "detail": type(channel_error).__name__,
                "route": channel_route_id,
            }
        finally:
            if gateway is not None:
                try:
                    gateway.close()
                except Exception:
                    pass
        work_order = ""
        work_order_fingerprint = exc.sha256
        work_order_chars = exc.chars

    route_id = str(route.get("target_id") or "")
    ctx._configured_actor_bootstrap = {
        "snapshot": dict(snapshot),
        "route": dict(route),
        "route_id": route_id,
        "selected_subagent_id": str(snapshot.get("selected_subagent_id") or ""),
        "config_fingerprint": str(snapshot.get("config_fingerprint") or ""),
        "canonical_work_order": work_order,
        "source_prompt": source_prompt,
        "source_request": source_request,
        "source_channel": source_channel,
        "work_order_fingerprint": work_order_fingerprint,
        "work_order_chars": work_order_chars,
        "route_available": not bool(getattr(dispatch, "blocked", False)),
        "exact_start_pending": True,
        "physical_started": False,
    }
    zero_run_evidence_gaps: set[str] = set()
    durable_zero_run = _durable_zero_run_receipt(
        ctx, gap_reasons=zero_run_evidence_gaps,
    )
    if durable_zero_run:
        ctx._configured_actor_bootstrap.update({
            "zero_run_receipt_recorded": True,
            "zero_run_decision": str(durable_zero_run.get("zero_run_decision") or ""),
            "zero_run_basis": str(durable_zero_run.get("zero_run_basis") or ""),
            "exact_start_pending": False,
        })
    elif zero_run_evidence_gaps:
        # Clean absence leaves the exact start available. Malformed/unreadable
        # lifecycle evidence is different: it may be a torn terminal zero-run
        # row, so UNKNOWN must not mint a second physical invocation. The actor
        # may still repair this state by recording a new durable zero-run fact.
        ctx._configured_actor_bootstrap.update({
            "zero_run_evidence_status": "unknown",
            "zero_run_evidence_gaps": sorted(zero_run_evidence_gaps),
            "exact_start_pending": False,
        })
    return json.dumps({
        "status": "configured_session_actor_ready",
        "startup": {
            "status": (
                "zero_run_evidence_unknown"
                if zero_run_evidence_gaps and not durable_zero_run else "pending"
            ),
            "selected_subagent_id": str(snapshot.get("selected_subagent_id") or ""),
            "route": route_id,
            "work_order_fingerprint": work_order_fingerprint,
            "work_order_chars": work_order_chars,
            "work_order_complete": bool(work_order),
            **({"source_channel": source_channel} if source_channel else {}),
            "actor_first": True,
            "exact_start_pending": not bool(
                durable_zero_run or zero_run_evidence_gaps
            ),
            "host_fallback": False,
            **({
                "zero_run_receipt_recorded": True,
                "zero_run_decision": str(durable_zero_run.get("zero_run_decision") or ""),
                "zero_run_basis": str(durable_zero_run.get("zero_run_basis") or ""),
            } if durable_zero_run else {}),
            **({
                "zero_run_evidence_status": "unknown",
                "zero_run_evidence_gaps": sorted(zero_run_evidence_gaps),
            } if zero_run_evidence_gaps and not durable_zero_run else {}),
        },
    }, ensure_ascii=False, indent=2)


def append_startup_receipt(
    ctx: Any, messages: list[dict[str, Any]], startup_wake: str,
) -> None:
    if not startup_wake:
        return
    try:
        receipt = json.loads(startup_wake) if isinstance(startup_wake, str) else {}
    except (TypeError, ValueError):
        receipt = {}
    receipt_status = str(receipt.get("status") or "") if isinstance(receipt, dict) else ""
    startup = receipt.get("startup") if isinstance(receipt, dict) else {}
    if (
        isinstance(startup, dict)
        and startup.get("zero_run_evidence_status") == "unknown"
    ):
        guidance = (
            "The host could not prove whether the verification-receipt store already "
            "contains a terminal delegation_zero_run decision. Do not start a physical "
            "leaf from this task. Reconcile the durable evidence or record a new typed "
            "delegation_zero_run fact after checking physical custody."
        )
    elif isinstance(startup, dict) and startup.get("zero_run_receipt_recorded"):
        guidance = (
            "A durable delegation_zero_run receipt already closes this actor's physical "
            "run decision. Do not call delegate_start for the same task; continue only "
            "with host evidence/children or an explicitly bound new task/retry."
        )
    elif receipt_status in {"configured_session_wake", "configured_session_recovered_wake"}:
        guidance = (
            "The receipt proves an existing physical run was started or recovered. "
            "Do not call delegate_start again for this work; supervise the run, inspect "
            "its evidence, and use the existing wait/answer/cancel controls."
        )
    elif receipt_status == "configured_session_recovery_wake":
        guidance = (
            "Recovery is unresolved. Do not start a replacement or a native/API fallback; "
            "inspect the typed recovery facts and reconcile the existing invocation first."
        )
    else:
        guidance = (
            "The host froze the selected route and canonical work-order authority before "
            "this ordinary actor-first round. The physical leaf may still be pending: "
            "call delegate_start for the exact selected session when a physical run is "
            "useful, or do visible host-side coordination/children. The coordination "
            "prompt is not a replacement for the canonical work order. A typed route "
            "unavailable receipt never authorizes native/API fallback; choose an explicit "
            "next action, retry the exact route, or report incomplete/unknown."
        )
    messages.append({
        "role": "user",
        "content": (
            "[CONFIGURED SESSION STARTUP / WAKE RECEIPT]\n" + startup_wake
            + "\n" + guidance
        ),
    })
    from ouroboros.delegate_supervision import acknowledge_pending_wake

    acknowledge_pending_wake(ctx, startup_wake)


__all__ = ["append_startup_receipt", "bootstrap_before_context"]
