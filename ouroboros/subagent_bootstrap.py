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


def _with_coordination_context(ctx: Any, raw: str) -> str:
    """Attach one fresh planning snapshot to a startup/recovery receipt."""

    if not raw:
        return raw
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return raw
        from ouroboros.delegate_supervision import coordination_live_context

        payload["coordination_context"] = coordination_live_context(ctx)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return raw


# Definite no-start refusal reasons: the leaf provably did not start and no
# custody/run handle can exist, so the child may end unrun and typed at $0
# (charter D2, owner 2026-08-28). Anything OUTSIDE this closed set — custody
# uncertainty, fence states, unknown future codes — stays a model episode:
# a false "spent nothing" terminal over a possibly-live run is the one
# direction this classification must never fail toward (f9356572 B4 in
# spirit; the set errs toward the episode, never toward the terminal).
_DEFINITE_UNRUN_REASONS = frozenset({
    "route_not_in_capability_catalog",
    "route_disabled",
    "engine_rejects_delegated_marker",
    "subscription_window_exhausted",
    "credential_pool_exhausted",
    "daemon_unreachable",
    "protocol_incompatible",
    "engine_too_old",
    "malformed_response",
    "work_order_source_channel_unavailable",
    "work_order_source_channel_unverified",
    "configured_work_order_unavailable",
})

_CUSTODY_HANDLE_KEYS = (
    "run_id", "pending_invocation_id", "pending_invocation_ids",
    "invocation_id", "job_id", "run_dir",
)


def _startup_refusal_definite(payload: Mapping[str, Any]) -> bool:
    """True only for a refusal that provably left no run behind."""

    if str(payload.get("status") or "") != "refused":
        return False
    if any(str(payload.get(key) or "").strip() for key in _CUSTODY_HANDLE_KEYS):
        return False
    reason = str(payload.get("reason") or "")
    return (
        reason in _DEFINITE_UNRUN_REASONS
        or reason.startswith("access_profile_unsupported")
    )


def _record_startup_refusal(
    ctx: Any, task: Mapping[str, Any], *, reason: str, reset_at: str = "",
) -> None:
    """Stash the typed unrun refusal for the caller's zero-spend terminal."""

    from ouroboros.subagent_runtime import current_subagent_alternatives

    snapshot = task.get("configured_subagent") if isinstance(task.get("configured_subagent"), dict) else {}
    alternatives = current_subagent_alternatives(
        str(snapshot.get("selected_subagent_id") or "")
    )
    ctx._configured_startup_refusal = {
        "reason": str(reason or "configured_session_unavailable"),
        "reset_at": str(reset_at or ""),
        "requested": "harness",
    }
    availability = dict(task.get("subagent_availability") or {}) if isinstance(
        task.get("subagent_availability"), dict) else {}
    availability.update({
        "status": "unavailable",
        "reason": str(reason or "configured_session_unavailable"),
        "reset_at": str(reset_at or ""),
        "alternatives": alternatives,
        "host_fallback": False,
        "route_kind": "agent_session",
    })
    if isinstance(task, dict):
        task["subagent_availability"] = availability


def bootstrap_before_context(ctx: Any, task: Mapping[str, Any], dispatch: Any) -> str:
    """Start the selected session leaf before the first model round (charter).

    An ``agent_session`` child means "this work executes on the harness": the
    substrate choice was the PARENT's, made by selecting the row, so the host
    executes it — freeze the route, start the exact leaf, enter supervision —
    before any metered round (owner decisions 2026-08-28, N1=A). The nanny's
    model rounds exist for judgment on meaningful wakes. Blocked routes and
    definite start refusals end the child unrun and typed at $0; custody
    uncertainty always wakes the model instead. Proven recovery handoffs are
    still adopted first, and durable zero-run/unknown-evidence fences outrank
    every other branch — a fence may hide a live prior run, so no unrun
    terminal may be claimed over one.
    """

    configured = isinstance(task.get("configured_subagent"), dict)
    ctx.exact_model_route = configured
    if not configured or dispatch is None:
        return ""
    snapshot = task.get("configured_subagent") if isinstance(task.get("configured_subagent"), dict) else {}
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    if str(route.get("kind") or "") != "agent_session":
        return ""
    # Hydrate immutable route/work-order authority before every recovery
    # branch. A recovered or already-settled physical run must never fall
    # back to the generic selector/prompt path on the resumed nanny.
    actor_ready = _prepare_actor_first_bootstrap(ctx, task, dispatch)
    recovery = _adopt_recovery_handoff(ctx, task)
    if recovery:
        return _with_coordination_context(ctx, recovery)
    actor_bootstrap = getattr(ctx, "_configured_actor_bootstrap", {})
    actor_bootstrap = actor_bootstrap if isinstance(actor_bootstrap, dict) else {}
    fenced = (
        bool(actor_bootstrap.get("zero_run_receipt_recorded"))
        or str(actor_bootstrap.get("zero_run_evidence_status") or "") == "unknown"
    )
    if bool(getattr(dispatch, "blocked", False)):
        resolution = getattr(dispatch, "executor_resolution", None)
        availability = task.get("subagent_availability") if isinstance(
            task.get("subagent_availability"), dict) else {}
        reason = str(
            getattr(resolution, "reason", "") or availability.get("reason") or ""
        ) or "configured_session_unavailable"
        reset_at = str(
            getattr(resolution, "reset_at", "") or availability.get("reset_at") or ""
        )
        from ouroboros import delegate_custody as custody

        custody.emit(custody.custody_root(ctx), "configured_subagent_startup_fault", {
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "status": "temporarily_unavailable",
            "reason": reason,
            "reset_at": reset_at,
            "selected_subagent_id": str(snapshot.get("selected_subagent_id") or ""),
            "route": str(actor_bootstrap.get("route_id") or route.get("target_id") or ""),
            "work_order_fingerprint": str(actor_bootstrap.get("work_order_fingerprint") or ""),
            "host_fallback": False,
        })
        if fenced:
            # A fence (recorded receipt / unreadable receipt store) may hide a
            # prior physical run: the model must reconcile it, so the blocked
            # fact rides the episode instead of fabricating an unrun terminal —
            # and it rides VISIBLY: the receipt carries the typed route_blocked
            # facts, not just a durable custody row the model never sees.
            try:
                payload = json.loads(actor_ready)
                payload["route_blocked"] = {
                    "reason": reason,
                    **({"reset_at": reset_at} if reset_at else {}),
                }
                actor_ready = json.dumps(payload, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                pass
            return _with_coordination_context(ctx, actor_ready)
        _record_startup_refusal(ctx, task, reason=reason, reset_at=reset_at)
        return ""
    if fenced:
        return _with_coordination_context(ctx, actor_ready)
    return _with_coordination_context(ctx, _pre_start_leaf(ctx, task, actor_bootstrap))


def _pre_start_leaf(
    ctx: Any, task: Mapping[str, Any], actor_bootstrap: dict[str, Any],
) -> str:
    """Start the exact snapshotted leaf through the SAME wrapper the model's
    ``delegate_start(prompt="")`` uses. One start path, one set of refusal
    shapes (P7 SSOT). The host never WAITS here (owner 2026-08-29, 1=A):
    the nanny's first round arrives immediately with the live run's receipt —
    waiting is the model's own ``delegate_wait`` decision, so owner messages,
    hurry controls and parallel children stay live for the whole run."""

    from ouroboros.subagent_runtime import delegate_start_entry

    started_raw = delegate_start_entry(ctx, "")
    try:
        payload = json.loads(started_raw) if isinstance(started_raw, str) else {}
    except (TypeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    status = str(payload.get("status") or "")
    if status in {"started", "started_uncustodied"}:
        # Idempotent beside the start wrapper's own marker: the host episode
        # must never treat a started (possibly uncustodied) run as a zero-run.
        _mark_physical_activity(ctx)
    if status == "started":
        run_id = str(payload.get("run_id") or "")
        if run_id:
            return json.dumps({
                "status": "configured_session_started", "startup": payload,
            }, ensure_ascii=False, indent=2)
        # A started run the host cannot address is a custody fault, not quiet
        # supervision material: wake the model with the raw facts.
        return json.dumps({
            "status": "configured_session_startup_fault", "startup": payload,
        }, ensure_ascii=False, indent=2)
    if _startup_refusal_definite(payload):
        _record_startup_refusal(
            ctx, task,
            reason=str(payload.get("reason") or ""),
            reset_at=str(payload.get("reset_at") or ""),
        )
        return ""
    # Everything else — started_uncustodied, fence refusals raced in by the
    # start path itself, unknown codes, unparseable output — is judgment
    # material for the model episode.
    return json.dumps({
        "status": "configured_session_startup_fault", "startup": payload,
    }, ensure_ascii=False, indent=2)


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
    # An adopted run with no pending wake is simply LIVE: the host does not
    # wait inside bootstrap (owner 2026-08-29, 1=A) — the model supervises it
    # with delegate_wait on its own first round.
    return json.dumps({
        "status": "configured_session_started",
        "recovery": adoption, "run_id": run_id,
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
    """Return a typed terminal fact when a session actor has no completion path.

    Under the charter (owner 2026-08-28) an ``agent_session`` child is clean
    only through its own physical leaf: a started/adopted run, or a durable
    typed zero-run receipt (whose incomplete/unknown decisions the terminal
    projection separately degrades). Host children are auxiliary evidence and
    can no longer substitute for the leaf — a substrate swap onto host API
    children is disclosed as an incomplete execution, never a clean one
    (children remain visible in the fact's evidence fields). Unreadable
    receipt evidence stays ``unknown`` rather than permission to finalize
    cleanly.
    """
    bootstrap = getattr(ctx, "_configured_actor_bootstrap", None)
    if not isinstance(bootstrap, dict):
        return None
    if bool(bootstrap.get("physical_started")) or bool(bootstrap.get("zero_run_receipt_recorded")):
        return None
    if str(bootstrap.get("zero_run_evidence_status") or "") == "unknown":
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
    root = Path(
        str(
            drive_root
            or getattr(ctx, "budget_drive_root", "")
            or getattr(ctx, "drive_root", "")
            or "."
        )
    )
    child_id = str(task_id or getattr(ctx, "task_id", "") or "")
    direct_child_statuses: list[str] = []
    child_evidence = ""
    try:
        from ouroboros.task_status import find_child_tasks

        children = find_child_tasks(
            root,
            parent_task_id=child_id,
            exclude_task_id=child_id,
            scope="direct",
            materialize_artifacts=False,
        )
        direct_child_statuses = sorted({
            str(child.get("status") or "unknown") for child in children
        })
    except Exception as exc:  # noqa: BLE001 - evidence detail only, never a verdict
        child_evidence = type(exc).__name__
    route_available = bootstrap.get("route_available")
    return {
        "status": "incomplete" if route_available is not False else "unknown",
        "reason": "physical_leaf_not_started",
        "route_available": route_available,
        "selected_subagent_id": str(bootstrap.get("selected_subagent_id") or ""),
        "work_order_fingerprint": str(bootstrap.get("work_order_fingerprint") or ""),
        **({"direct_child_statuses": direct_child_statuses}
           if direct_child_statuses else {}),
        **({"child_evidence_error": child_evidence} if child_evidence else {}),
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
    from ouroboros.outcome_receipt_store import terminal_zero_run_receipt

    for receipt in reversed(receipts or []):
        if terminal_zero_run_receipt(receipt, gap_reasons=gap_reasons):
            return dict(receipt)
    return None


def actor_first_terminal_projection(
    ctx: Any, task: Mapping[str, Any], usage: Mapping[str, Any],
    llm_trace: Mapping[str, Any], drive_root: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Attach the unresolved actor fact to the existing terminal projections."""
    # ``emit_task_results`` and the terminal-delivery projection share these
    # mappings with the caller.  Keep that identity for ordinary dict inputs so
    # fields added during finalization (for example the preserved host-salvage
    # path) remain visible to the durable-result path as well as the live event.
    # Non-dict mappings still receive the defensive copy promised by this
    # boundary.
    usage_out = usage if isinstance(usage, dict) else dict(usage or {})
    trace_out = llm_trace if isinstance(llm_trace, dict) else dict(llm_trace or {})
    if ctx is None:
        return None, usage_out, trace_out
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
            # A historical "complete" receipt (pre-charter write enum) still
            # fences a second physical start on read, but a self-reported
            # completion with zero runs is unverifiable authority: project it
            # as UNKNOWN with disclosure (owner 2026-08-28), never as clean.
            fact = {
                "status": "unknown",
                "reason": "historical_zero_run_complete",
                "zero_run": True,
                "zero_run_decision": decision,
                "zero_run_basis": str(bootstrap.get("zero_run_basis") or ""),
                "route_available": bootstrap.get("route_available"),
            }
    else:
        fact = None
    try:
        if fact is None:
            fact = actor_first_unresolved_fact(
                ctx, task_id=str(task.get("id") or ""), drive_root=drive_root,
            )
    except Exception:
        if not isinstance(bootstrap, dict):
            return None, usage_out, trace_out
        fact = {
            "status": "unknown",
            "reason": "actor_terminal_projection_unavailable",
            "route_available": bootstrap.get("route_available"),
        }
    if not fact:
        return None, usage_out, trace_out
    usage_out["actor_first_terminal"] = dict(fact)
    trace_out["actor_first_terminal"] = dict(fact)
    return fact, usage_out, trace_out


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
    if receipt_status == "configured_session_started":
        guidance = (
            "The exact configured leaf run is LIVE — the receipt carries its id. "
            "Waiting is your decision: call delegate_wait when you want its facts, "
            "or use this round for parallel judgment first (schedule auxiliary "
            "children, prepare acceptance). Never rebuild its work on metered "
            "tokens and never start a duplicate leaf; a replacement start is legal "
            "only after cancellation and terminal settlement are verified."
        )
    elif receipt_status == "configured_session_recovered_wake":
        guidance = (
            "The receipt proves an existing physical run was started or recovered, "
            "with its wake facts attached. Do not start a duplicate leaf: verify and "
            "integrate what the run produced, supervise with the existing "
            "wait/answer/cancel controls, and start a replacement only after its "
            "terminal settlement is verified."
        )
    else:
        # Everything else is unresolved physical custody: a durable zero-run
        # fence, unreadable receipt evidence, unfinished recovery, or a start
        # fault. All of them may hide a live or already-decided run.
        guidance = (
            "Physical custody is unresolved: this receipt reports a durable "
            "zero-run fence, unreadable receipt evidence, unfinished recovery, or "
            "a start fault. Do not call delegate_start over it — a prior run or a "
            "terminal delegation_zero_run decision may already exist. Reconcile "
            "the typed facts first; then supervise the run you find, retry the "
            "exact route once absence is proven, or record a typed "
            "delegation_zero_run decision (incomplete|unknown). This receipt "
            "never authorizes native/API fallback."
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


__all__ = [
    "actor_first_terminal_projection",
    "actor_first_unresolved_fact",
    "append_startup_receipt",
    "bootstrap_before_context",
]
