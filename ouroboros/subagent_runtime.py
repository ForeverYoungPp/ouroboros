"""Immutable configured-subagent selection for scheduling and exact starts.

This module consumes :mod:`ouroboros.configured_subagents`; it deliberately does
not parse either the new setting or the bounded legacy settings itself.  A task
copies the returned snapshot once and every later dispatch/recovery decision reads
that copy rather than mutable owner settings.
"""

from __future__ import annotations

import contextvars
import json
import os
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, Mapping, Optional

from ouroboros.configured_subagents import (
    ConfiguredSubagent,
    ConfiguredSubagentsResolution,
    SOURCE_INVALID,
    SOURCE_LEGACY_MIGRATED,
    SOURCE_UNDECIDED,
    configured_subagents_fingerprint,
    resolve_configured_subagents,
)
from ouroboros.route_spec import route_spec_dict
from ouroboros.utils import utc_now_iso


# These are the only inputs read by the bounded legacy compiler.  At task start
# provider normalization has already projected the EFFECTIVE values into the
# process environment.  Overlaying exactly these keys prevents a later raw disk
# read from resurrecting a shipped Heavy default which normalization cleared.
_RUNTIME_LEGACY_KEYS = (
    "OUROBOROS_SUBAGENT_HARNESS",
    "OUROBOROS_SUBAGENT_PROFILE",
    "OUROBOROS_MODEL_HEAVY",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL",
    "USE_LOCAL_HEAVY",
    "USE_LOCAL_LIGHT",
    "USE_LOCAL_MAIN",
    "LOCAL_MODEL_SOURCE",
)

# Call-stack carrier only.  The selected row itself is durable in the task and
# start records; ContextVar keeps concurrent tool calls from mutating ToolContext.
_EXACT_START_SELECTION: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "delegate_exact_start_selection", default={}
)


@dataclass(frozen=True)
class SubagentSelectionError(ValueError):
    """Typed scheduling/start refusal, safe to project directly to a tool result."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def effective_runtime_subagent_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay normalized task-start legacy values without rereading raw defaults."""

    effective = dict(settings)
    for key in _RUNTIME_LEGACY_KEYS:
        # Absence is meaningful: apply_settings_to_env removes a normalized-empty
        # setting, so retaining the raw disk value here would undo normalization.
        effective[key] = os.environ.get(key, "")
    return effective


def model_visible_subagent_catalog(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Project saved, dispatchable rows without probing or ranking them."""

    resolution = resolve_configured_subagents(settings)
    config = resolution.config
    if (
        resolution.source in {SOURCE_INVALID, SOURCE_UNDECIDED}
        or config is None
        or not config.enabled
        or not config.items
    ):
        return {}

    rows: list[dict[str, Any]] = []
    for row in config.items:
        session = row.route.is_session
        projected: dict[str, Any] = {
            "subagent_id": row.subagent_id,
            "name": row.name,
            "recommended_use": row.recommended_use,
            "route_class": "Agent session" if session else "API model",
            "requested_effort": row.effort or "(not explicitly set)",
        }
        if session:
            projected["requested_target"] = row.route.target_id
            if row.route.credential_profile_id:
                projected["account_policy"] = "explicit profile pin"
                projected["credential_profile_id"] = row.route.credential_profile_id
            else:
                projected["account_policy"] = "automatic compatible account selection"
        else:
            projected["requested_model"] = row.route.target_id
            projected["account_policy"] = (
                "API provider credentials (session account selection does not apply)"
            )
        rows.append(projected)

    return {
        "source": resolution.source,
        "config_fingerprint": configured_subagents_fingerprint(config),
        "rows": rows,
        "selection_guidance": (
            "Choose subagent_id from the owner descriptions and saved route intent. "
            "Prefer suitable Agent session choices often when they fit, to reduce "
            "incremental API spend; use API model choices when their described strengths "
            "fit. The host does not rank or substitute rows."
        ),
        "dispatch_contract": (
            "schedule_subagent attempts the exact selected row. If live dispatch finds it "
            "unavailable, it returns a typed refusal; choose the next action or another "
            "subagent_id."
        ),
    }


def current_model_visible_subagent_catalog() -> dict[str, Any]:
    """Read the current normalized settings and return the stable catalog."""

    from ouroboros.config import load_settings

    return model_visible_subagent_catalog(
        effective_runtime_subagent_settings(load_settings())
    )


def apply_task_start_settings() -> None:
    """Project the provider-normalized in-memory snapshot for one task start."""

    from ouroboros.config import apply_settings_to_env, load_settings
    from ouroboros.server_runtime import apply_runtime_provider_defaults

    effective, _changed, _keys = apply_runtime_provider_defaults(load_settings())
    apply_settings_to_env(effective)


def _resolution(
    settings: Mapping[str, Any], *, allow_undecided_legacy: bool,
) -> ConfiguredSubagentsResolution:
    resolution = resolve_configured_subagents(settings)
    if resolution.source == SOURCE_INVALID:
        raise SubagentSelectionError(
            "subagent_configuration_invalid",
            resolution.diagnostic or "Available subagents are not configured.",
        )
    if resolution.source == SOURCE_UNDECIDED and not allow_undecided_legacy:
        raise SubagentSelectionError(
            "subagent_configuration_unsaved",
            "Available subagents have not been saved; choose them in onboarding or Settings first.",
        )
    if resolution.config is None:
        raise SubagentSelectionError(
            "subagent_selection_required",
            "The legacy selector does not identify a migrated configured subagent.",
        )
    if not resolution.config.enabled:
        raise SubagentSelectionError(
            "subagents_disabled", "Available subagents are disabled by owner configuration."
        )
    if not resolution.config.items:
        raise SubagentSelectionError(
            "no_available_subagents", "The enabled Available-subagents list is empty."
        )
    return resolution


def _legacy_matches(
    resolution: ConfiguredSubagentsResolution,
    *,
    model_lane: str,
    executor: str,
) -> list[ConfiguredSubagent]:
    """Return only rows the bounded legacy migration can identify exactly.

    ``auto`` is intentionally no filter.  It never selects by list order or live
    health.  New configured rows are not reverse-engineered into the retired axes;
    the compatibility seam is for the canonical legacy-migration projection only.
    """

    if resolution.source not in {SOURCE_LEGACY_MIGRATED, SOURCE_UNDECIDED}:
        return []
    rows = list(resolution.config.items if resolution.config else ())
    if executor == "harness":
        rows = [row for row in rows if row.route.is_session]
    elif executor == "native":
        rows = [row for row in rows if not row.route.is_session]
    if model_lane == "heavy":
        rows = [row for row in rows if row.subagent_id.startswith("legacy-heavy")]
    elif model_lane == "light":
        rows = [row for row in rows if row.subagent_id.startswith("fast-scout")]
    elif model_lane == "main":
        # The bounded compiler has no Main identity row.  Guessing by model value
        # would recreate mutable slot semantics after the list became authoritative.
        rows = []
    return rows


def select_subagent_snapshot(
    settings: Mapping[str, Any],
    *,
    subagent_id: str = "",
    legacy_model_lane: Any = None,
    legacy_executor: Any = None,
    legacy_model_lane_supplied: bool = False,
    legacy_executor_supplied: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Resolve one row and return ``(immutable_snapshot, used_legacy_seam)``.

    The snapshot is JSON-only so the queue/result stores can copy it losslessly.
    Availability is deliberately not embedded here: saved intent is immutable;
    dispatch/start records its dated live observation in a separate task field.
    """

    selected_id = str(subagent_id or "").strip()
    has_legacy = bool(legacy_model_lane_supplied or legacy_executor_supplied)
    if selected_id and has_legacy:
        raise SubagentSelectionError(
            "subagent_selector_conflict",
            "subagent_id cannot be combined with legacy model_lane/executor selectors.",
        )
    resolution = _resolution(settings, allow_undecided_legacy=has_legacy)
    config = resolution.config
    assert config is not None
    used_legacy = False
    if selected_id:
        matches = [row for row in config.items if row.subagent_id == selected_id]
        if not matches:
            raise SubagentSelectionError(
                "unknown_subagent_id", f"No configured subagent has id {selected_id!r}."
            )
        row = matches[0]
    else:
        lane = str(legacy_model_lane or "auto").strip().lower() or "auto"
        executor = str(legacy_executor or "auto").strip().lower() or "auto"
        if not has_legacy or (lane == "auto" and executor == "auto"):
            raise SubagentSelectionError(
                "subagent_selection_required",
                "Choose an explicit subagent_id; omitted/auto legacy selectors are ambiguous.",
            )
        matches = _legacy_matches(resolution, model_lane=lane, executor=executor)
        if len(matches) != 1:
            raise SubagentSelectionError(
                "subagent_selection_required",
                f"Legacy selectors mapped to {len(matches)} configured rows; choose subagent_id explicitly.",
            )
        row = matches[0]
        used_legacy = True

    return {
        "schema": 1,
        "selected_subagent_id": row.subagent_id,
        "config_fingerprint": configured_subagents_fingerprint(config),
        "name": row.name,
        "recommended_use": row.recommended_use,
        "source": resolution.source,
        "route": route_spec_dict(
            row.route, api_kind="api_model", pin_key="credential_profile_id"
        ),
        "effort": row.effort,
        "selected_at": utc_now_iso(),
    }, used_legacy


def validate_subagent_snapshot(raw: Any) -> dict[str, Any]:
    """Validate the small durable execution subset without consulting settings."""

    snapshot = dict(raw) if isinstance(raw, dict) else {}
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    kind = str(route.get("kind") or "")
    target = str(route.get("target_id") or "").strip()
    if (
        int(snapshot.get("schema") or 0) != 1
        or not str(snapshot.get("selected_subagent_id") or "").strip()
        or not str(snapshot.get("config_fingerprint") or "").strip()
        or kind not in {"api_model", "agent_session"}
        or not target
    ):
        raise SubagentSelectionError(
            "subagent_snapshot_invalid", "The task has no complete immutable subagent snapshot."
        )
    return snapshot


def resolve_configured_actor_dispatch(
    task: Mapping[str, Any], *, task_type: str,
) -> Any:
    """Resolve a frozen row into the existing ``SubagentDispatch`` contract."""

    # Lazy import avoids a module cycle: subagents calls this only after defining
    # the shared dispatch dataclasses and route helpers used below.
    from ouroboros.config import resolve_effort
    from ouroboros.provider_models import model_has_credentials
    from ouroboros.subagents import (
        CapabilityDelta,
        DelegationRoute,
        SubagentDispatch,
        SubagentExecutorResolution,
        SubagentLaneResolution,
        delegated_run_shape,
        derive_capability_reason,
        parse_subagent_harness,
        route_health,
    )
    from ouroboros.tools.control_delegation import profile_from_task_constraint

    snapshot = validate_subagent_snapshot(task.get("configured_subagent"))
    route_spec = snapshot["route"]
    route_kind = str(route_spec.get("kind") or "")
    route_target = str(route_spec.get("target_id") or "").strip()
    selected_effort = str(snapshot.get("effort") or "").strip().lower()
    derived_effort = selected_effort or resolve_effort(task_type or str(task.get("type") or "task"))
    constraint = task.get("task_constraint") if isinstance(task.get("task_constraint"), dict) else {}
    profile = profile_from_task_constraint(constraint)
    observed_at = utc_now_iso()
    lane = SubagentLaneResolution(
        requested_lane="auto", effective_lane="main", model="",
        resolved_from="main", provenance="configured_subagent",
    )

    if route_kind == "api_model":
        use_local = route_target.endswith(" (local)")
        model = route_target[:-8].strip() if use_local else route_target
        available = bool(model_has_credentials(route_target))
        unavailable = "" if available else "credentials_unavailable"
        executor = "native" if available else "blocked"
        lane = dataclass_replace(lane, model=model, use_local_model=use_local)
        availability = {
            "observed_at": observed_at,
            "status": "ready" if available else "credentials_unavailable",
            "reason": unavailable,
            "route_kind": route_kind,
            "selected_subagent_id": str(snapshot.get("selected_subagent_id") or ""),
            "alternatives": (
                [] if available else current_subagent_alternatives(
                    str(snapshot.get("selected_subagent_id") or "")
                )
            ),
            "host_fallback": False,
        }
        reasons = []
        if unavailable:
            reasons.append(unavailable)
        delta = CapabilityDelta(
            derived_effort=derived_effort,
            effective_effort=derived_effort,
            requested_executor="native", effective_executor=executor,
            reason=derive_capability_reason(reasons), reduced=bool(reasons),
            reduction_reasons=tuple(reasons),
        )
        return SubagentDispatch(
            lane=lane, effort=derived_effort, executor=executor,
            route=route_target if available else "",
            profile=profile, delta=delta,
            executor_resolution=SubagentExecutorResolution(
                "native", executor, reason=unavailable or "requested_native",
            ),
            availability=availability,
        )

    parsed = parse_subagent_harness(route_target)
    if parsed is None:
        unavailable, reset_at = "configured_session_route_invalid", ""
        exact_route = DelegationRoute(route_id="")
    else:
        exact_route = dataclass_replace(
            parsed,
            effort=selected_effort or parsed.effort,
            profile_id=str(route_spec.get("credential_profile_id") or ""),
        )
        from ouroboros.contracts.task_constraint import normalize_task_constraint
        from ouroboros.tool_access import predicted_subagent_profile

        normalized = normalize_task_constraint(task.get("task_constraint"))
        surface = str(getattr(normalized, "surface", "") or "")
        shape = delegated_run_shape(
            predicted_subagent_profile(write_surface=surface) == "acting_subagent"
        )
        gateway = None
        try:
            from ouroboros.claudexor_daemon import ensure_owned_gateway

            gateway = ensure_owned_gateway()
            unavailable, reset_at = route_health(
                gateway, exact_route.route_id, shape, route_model=exact_route.model,
                pinned_profile=exact_route.profile_id,
            )
        except Exception as exc:
            unavailable, reset_at = (
                str(getattr(exc, "code", "") or type(exc).__name__),
                str(getattr(exc, "reset_at", "") or ""),
            )
        finally:
            if gateway is not None:
                gateway.close()

    cognitive = task.get("parent_cognitive_route") if isinstance(task.get("parent_cognitive_route"), dict) else {}
    nanny_model = str(cognitive.get("model") or "").strip()
    nanny_effort = str(cognitive.get("effort") or "").strip().lower()
    if not nanny_model and not unavailable:
        unavailable = "parent_cognitive_route_missing"
    if not nanny_effort:
        nanny_effort = resolve_effort(task_type or str(task.get("type") or "task"))
    lane = dataclass_replace(
        lane, model=nanny_model,
        use_local_model=bool(cognitive.get("use_local_model")),
    )
    executor = "blocked" if unavailable else "harness"
    availability = {
        "observed_at": observed_at,
        "status": "unavailable" if unavailable else "ready",
        "reason": unavailable, "reset_at": reset_at, "route_kind": route_kind,
    }
    reasons = []
    if unavailable:
        reasons.append(unavailable)
    delta = CapabilityDelta(
        derived_effort=nanny_effort,
        effective_effort=nanny_effort,
        requested_executor="harness", effective_executor=executor,
        reason=derive_capability_reason(reasons), reduced=bool(reasons),
        reduction_reasons=tuple(reasons),
    )
    resolution = SubagentExecutorResolution(
        "harness", executor, exact_route if exact_route.route_id else None,
        unavailable or "harness_ready", reset_at,
    )
    return SubagentDispatch(
        lane=lane, effort=nanny_effort, executor=executor,
        route=route_target if executor == "harness" else "", profile=profile,
        delta=delta, executor_resolution=resolution, availability=availability,
    )


def current_exact_start_selection() -> dict[str, Any]:
    return dict(_EXACT_START_SELECTION.get() or {})


def current_subagent_alternatives(exclude_id: str = "") -> list[dict[str, Any]]:
    """Project the current saved choices without ranking or probing them."""

    try:
        from ouroboros.config import load_settings

        resolution = resolve_configured_subagents(
            effective_runtime_subagent_settings(load_settings())
        )
    except Exception:
        return []
    config = resolution.config
    if config is None or not config.enabled:
        return []
    excluded = str(exclude_id or "")
    return [
        {
            "subagent_id": row.subagent_id,
            "name": row.name,
            "recommended_use": row.recommended_use,
            "route_kind": row.route.kind,
            "target_id": row.route.target_id,
            "effort": row.effort,
            "availability": "check_at_dispatch",
        }
        for row in config.items
        if row.subagent_id != excluded
    ]


def exact_session_binding(raw_snapshot: Any) -> tuple[dict[str, Any], Any]:
    """Validate one snapshotted session row and construct its exact route."""

    snapshot = validate_subagent_snapshot(raw_snapshot)
    route_spec = snapshot["route"]
    if str(route_spec.get("kind") or "") != "agent_session":
        raise SubagentSelectionError(
            "api_actor_requires_schedule_subagent",
            "The selected row is an API actor. Create it as an ordinary recursive child with "
            "schedule_subagent(subagent_id=...), not as a Claudexor leaf.",
        )
    from ouroboros.subagents import parse_subagent_harness

    route = parse_subagent_harness(route_spec.get("target_id"))
    if route is None:
        raise SubagentSelectionError(
            "configured_session_route_invalid", "The selected session route is invalid."
        )
    return snapshot, dataclass_replace(
        route,
        effort=str(snapshot.get("effort") or route.effort),
        profile_id=str(route_spec.get("credential_profile_id") or ""),
    )


def prepare_delegate_start_actor(
    ctx: Any,
    drive_root: Any,
    *,
    recovering: bool,
    invocation_id: str,
    work_order_fingerprint: str,
    authority_fingerprint: str,
) -> tuple[dict[str, Any], str]:
    """Resolve the exact actor/start fence without growing the transport facade."""

    from ouroboros import delegate_custody as custody
    from ouroboros.delegate_recovery import unsettled_start_ids
    from ouroboros.delegate_shared import _fail
    selection = current_exact_start_selection()
    selected_snapshot = selection.get("snapshot")
    if recovering:
        if selected_snapshot:
            return {}, _fail(
                "delegate_start", "retry_selector_conflict",
                "retry_of replays its already-bound immutable route and cannot accept a new "
                "subagent selector.",
            )
        invocation = custody.invocation_record(drive_root, invocation_id) or {}
        return {
            "selected_subagent_id": str(invocation.get("selected_subagent_id") or ""),
            "config_fingerprint": str(invocation.get("config_fingerprint") or ""),
            "work_order_fingerprint": str(
                invocation.get("work_order_fingerprint") or work_order_fingerprint
            ),
            "authority_fingerprint": str(
                invocation.get("authority_fingerprint") or authority_fingerprint
            ),
        }, ""

    if selected_snapshot is None:
        return {}, _fail(
            "delegate_start", "subagent_selection_required",
            "A fresh delegated start requires an explicit agent_session subagent_id. "
            "Only retry_of may replay a selectorless immutable invocation.",
        )
    snapshot, route = exact_session_binding(selected_snapshot)
    selected_id = str(snapshot.get("selected_subagent_id") or "")
    config_fingerprint = str(snapshot.get("config_fingerprint") or "")
    blockers = unsettled_start_ids(
        drive_root, str(getattr(ctx, "task_id", "") or "")
    )
    if any(blockers.values()):
        return {}, _fail(
            "delegate_start", "replacement_requires_settlement",
            "This task already owns an unsettled run or start invocation. Wait/cancel and "
            "verify terminal settlement before starting a replacement.",
            **blockers,
        )
    return {
        "route": route,
        "selected_subagent_id": selected_id,
        "config_fingerprint": config_fingerprint,
        "work_order_fingerprint": work_order_fingerprint,
        "authority_fingerprint": authority_fingerprint,
        "compiled_work_order": bool(selection.get("compiled_work_order")),
    }, ""


def exact_start(ctx: Any, prompt: str, spec: Optional[dict[str, Any]] = None) -> str:
    """Shared exact-start primitive for atomic bootstrap and root-direct calls."""

    options = dict(spec or {})
    selected_id = str(options.pop("subagent_id", "") or "").strip()
    selected_snapshot = options.pop("snapshot", None)
    compiled_work_order = bool(options.pop("compiled_work_order", False))
    canonical_work_order_fingerprint = str(
        options.pop("work_order_fingerprint", "") or ""
    ).strip()
    coordination_context = str(options.pop("_coordination_context", "") or "").strip()
    work_order_source_request = options.pop("work_order_source_request", None)
    try:
        if str(options.get("retry_of") or "").strip() and (
            selected_id or selected_snapshot is not None
        ):
            raise SubagentSelectionError(
                "retry_selector_conflict",
                "retry_of replays its already-bound immutable route and cannot accept a new "
                "subagent selector.",
            )
        if selected_id and selected_snapshot is not None:
            raise SubagentSelectionError(
                "subagent_selector_conflict", "subagent_id cannot be combined with a snapshot."
            )
        if not str(options.get("retry_of") or "").strip() and not selected_id and selected_snapshot is None:
            raise SubagentSelectionError(
                "subagent_selection_required",
                "A fresh delegated start requires subagent_id; only retry_of replays without it.",
            )
        if selected_id:
            from ouroboros.config import load_settings

            selected_snapshot, _legacy = select_subagent_snapshot(
                effective_runtime_subagent_settings(load_settings()),
                subagent_id=selected_id,
            )
        if selected_snapshot is not None:
            selected_snapshot = validate_subagent_snapshot(selected_snapshot)
    except SubagentSelectionError as exc:
        from ouroboros.delegate_shared import _fail

        return _fail("delegate_start", exc.code, exc.detail)

    token = _EXACT_START_SELECTION.set({
        "snapshot": selected_snapshot,
        "compiled_work_order": compiled_work_order,
    })
    try:
        from ouroboros.tools.delegate import _delegate_start

        result = _delegate_start(
            ctx, prompt, options.pop("max_seconds", None), options.pop("retry_of", None),
            root=options.pop("root", None), bucket=options.pop("bucket", None),
            skill_name=options.pop("skill_name", None),
            _resolved_binding=options.pop("_resolved_binding", None),
            _canonical_work_order_fingerprint=canonical_work_order_fingerprint,
            _work_order_source_request=work_order_source_request,
            _coordination_context=coordination_context,
        )
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return result
        if isinstance(selected_snapshot, dict):
            payload["selected_subagent_id"] = str(
                selected_snapshot.get("selected_subagent_id") or ""
            )
            payload["config_fingerprint"] = str(
                selected_snapshot.get("config_fingerprint") or ""
            )
        if isinstance(work_order_source_request, dict):
            payload["work_order_source_request"] = dict(work_order_source_request)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except SubagentSelectionError as exc:
        from ouroboros.delegate_shared import _fail

        return _fail("delegate_start", exc.code, exc.detail)
    finally:
        _EXACT_START_SELECTION.reset(token)


def delegate_start_entry(ctx: Any, prompt: str, _resolved_binding: Any = None, **params: Any) -> str:
    # Actor-first configured sessions bind every fresh start to the immutable
    # snapshot captured before the episode. The model supplies only an advisory
    # coordination appendix; the canonical work order remains host-owned.
    bootstrap = getattr(ctx, "_configured_actor_bootstrap", None)
    retry_of = str(params.get("retry_of") or "").strip()
    from ouroboros.delegate_shared import _fail
    if isinstance(bootstrap, dict) and not retry_of:
        expected_id = str(bootstrap.get("selected_subagent_id") or "")
        requested_id = str(params.get("subagent_id") or "").strip()
        if requested_id and requested_id != expected_id:
            return _fail(
                "delegate_start", "configured_actor_route_mismatch",
                "This actor-first turn is bound to its scheduled configured session; "
                "select another actor with schedule_subagent instead.",
                selected_subagent_id=expected_id,
                requested_subagent_id=requested_id,
                host_fallback=False,
            )
        if any(str(params.get(key) or "").strip() for key in ("root", "bucket", "skill_name")):
            return _fail(
                "delegate_start", "configured_actor_resource_mismatch",
                "An actor-first configured session may start only its assigned route, "
                "not a skill-payload resource.",
                selected_subagent_id=expected_id,
                host_fallback=False,
            )
        canonical_work_order = str(bootstrap.get("canonical_work_order") or "")
        source_request = bootstrap.get("source_request")
        if not canonical_work_order and isinstance(source_request, dict) and source_request:
            if str((bootstrap.get("source_channel") or {}).get("status") or "") != "available":
                return _fail(
                    "delegate_start", "work_order_source_channel_unavailable",
                    "The complete canonical work order exceeds the host wire budget and "
                    "the selected route has no verified interactive source channel; the "
                    "physical leaf was not started from a prefix.",
                    work_order_fingerprint=str(bootstrap.get("work_order_fingerprint") or ""),
                    source_channel=bootstrap.get("source_channel") or {},
                    host_fallback=False,
                )
            canonical_work_order = str(bootstrap.get("source_prompt") or "")
        if not canonical_work_order:
            return _fail(
                "delegate_start", "configured_work_order_unavailable",
                "The canonical work order is unavailable; do not start a physical leaf "
                "from a prefix. Resolve the existing source-range interaction first.",
                work_order_fingerprint=str(bootstrap.get("work_order_fingerprint") or ""),
                work_order_chars=int(bootstrap.get("work_order_chars") or 0),
                host_fallback=False,
            )
        bound = dict(params)
        bound.pop("subagent_id", None)
        bound.update({
            "snapshot": dict(bootstrap.get("snapshot") or {}),
            "compiled_work_order": True,
            "work_order_fingerprint": str(bootstrap.get("work_order_fingerprint") or ""),
            "_coordination_context": str(prompt or "").strip(),
        })
        if _resolved_binding is not None:
            bound["_resolved_binding"] = _resolved_binding
        if isinstance(source_request, dict) and source_request:
            bound["work_order_source_request"] = dict(source_request)
        return exact_start(ctx, canonical_work_order, bound)
    if retry_of and isinstance(bootstrap, dict):
        # Retry replays the stored canonical request byte-for-byte; do not let a
        # new coordination sentence become a prefix when the original order was
        # over the host wire budget.
        canonical_work_order = str(bootstrap.get("canonical_work_order") or "")
        if not canonical_work_order:
            source_request = bootstrap.get("source_request")
            source_channel = bootstrap.get("source_channel")
            if (
                isinstance(source_request, dict)
                and source_request
                and isinstance(source_channel, dict)
                and str(source_channel.get("status") or "") == "available"
            ):
                canonical_work_order = str(bootstrap.get("source_prompt") or "")
            if not canonical_work_order:
                return _fail(
                    "delegate_start", "configured_work_order_unavailable",
                    "The retry has no complete canonical work order or verified source "
                    "lens; the coordination prompt cannot replace the original assignment.",
                    work_order_fingerprint=str(bootstrap.get("work_order_fingerprint") or ""),
                    work_order_chars=int(bootstrap.get("work_order_chars") or 0),
                    host_fallback=False,
                )
        retry_spec = {
            "retry_of": retry_of,
            "_resolved_binding": _resolved_binding,
        }
        if isinstance(bootstrap.get("source_request"), dict) and bootstrap.get("source_request"):
            retry_spec["work_order_source_request"] = dict(bootstrap["source_request"])
        return exact_start(ctx, canonical_work_order, retry_spec)
    return exact_start(ctx, prompt, {**params, "_resolved_binding": _resolved_binding})


__all__ = [
    "SubagentSelectionError",
    "apply_task_start_settings",
    "current_exact_start_selection",
    "current_model_visible_subagent_catalog",
    "current_subagent_alternatives",
    "delegate_start_entry",
    "effective_runtime_subagent_settings",
    "exact_session_binding",
    "exact_start",
    "model_visible_subagent_catalog",
    "prepare_delegate_start_actor",
    "resolve_configured_actor_dispatch",
    "select_subagent_snapshot",
    "validate_subagent_snapshot",
]
