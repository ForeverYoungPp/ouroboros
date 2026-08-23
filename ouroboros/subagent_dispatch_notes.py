"""Dispatch-time executor notes for delegated children.

The child-facing halves of the executor axis, moved WHOLE from ``agent.py`` at
its module-size ceiling (B1/F7): the substrate note a dispatched child reads
(``dispatch_executor_note``) and the typed terminal of a pin no route can honor
(``executor_blocked_outcome``) — one pair, one vocabulary, both speaking about
the same ``SubagentExecutorResolution``. ``agent`` re-exports both under their
historical names, so every existing import and monkeypatch target keeps working
(the byte-pinned transport suite imports them from ``ouroboros.agent``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from ouroboros.subagents import SubagentExecutorResolution, SubagentLaneResolution


def dispatch_executor_note(decision: Optional[SubagentExecutorResolution],
                           lane: Optional["SubagentLaneResolution"] = None) -> str:
    """The child's VISIBLE marker for a substrate decision it did not make ('' = silent).

    The rule table's `auto` rows are only honest if the child can see which way they
    went: a nanny must know to delegate, and a child that fell back to metered tokens
    must know its route was unavailable rather than discovering it by spending.

    ``lane`` is the same dispatch's lane resolution: a nanny that landed on the
    LIGHT lane by policy is told so, with the sanctioned escalation
    (``switch_model`` for real acceptance judgment) named beside it — a policy the
    child cannot see is a policy it will fight by accident.

    The harness branch SUPERSEDES any native-self-execution framing in the frozen
    task text (owner decision 2A): the composed text is written at schedule time,
    when the executor is unknown, so its execution framing describes the metered
    fallback — and this note rides ONLY the FINAL post-preflight harness dispatch
    (the call site runs after the delegate-visibility preflight), so a native or
    preflight-demoted child never receives the override.
    """
    if decision is None or decision.blocked:
        return ""
    if decision.executor == "harness":
        route = decision.route.route_id if decision.route else ""
        configured_atomic = lane is not None and lane.provenance == "configured_subagent"
        if configured_atomic:
            return (
                f"EXECUTOR: your parent selected the configured agent-session route ({route}). "
                "You are its Ouroboros NANNY. This first model round is the host's ordinary "
                "coordination episode: the host has not started a new physical leaf merely "
                "because the row is configured. Choose whether to schedule host children, "
                "publish evidence, start the snapshotted leaf with delegate_start, or record a "
                "typed zero-run decision. A typed startup/wake receipt alone is not a zero-run decision; "
                "record the typed zero-run "
                "decision through verify_and_record(contract_kind=delegation_zero_run, "
                "zero_run_decision, zero_run_basis). That receipt is terminal for this actor: "
                "after it is durably recorded, do not start a physical leaf in the same task. "
                "The typed startup/wake receipt "
                "says whether a leaf is pending, live, recovered, or refused; do not repeat a receipt-proven "
                "start or adoption in parallel. You retain your full ordinary tool surface and judgment: "
                "supervise and inspect evidence, coordinate task-tree messages, answer "
                "authorized leaf questions, wait again, and accept or reject the result. "
                "When correction is necessary and no truthful in-place control exists, "
                "verify cancellation and terminal settlement before starting a replacement. "
                "Any API-backed or otherwise separate work must be an explicit separate "
                "child, so its authorship and spend remain visible."
            )
        note = (
            f"EXECUTOR: your parent scheduled you on the delegated substrate ({route}). "
            "You are a NANNY. "
            + "Decide your delegation plan FIRST — right after reading your objective "
            "and constraints, before any substantive work. "
            + "Cost classes: "
            "a subscription-lane run has known-zero marginal cost when the route reports "
            "its settled spend as $0 (an estimated or undisclosed spend is estimated/unknown, "
            "not zero); every token YOU think on is metered API money. "
            "While the lane is healthy, delegate everything you can — even small tasks — "
            "with delegate_start / delegate_wait, and verify what comes back rather than "
            "believing it. After a delegated run SUCCEEDS, your job is to VERIFY and "
            "INTEGRATE its output — never to rebuild the same work yourself on metered "
            "tokens. Follow-up work (fixes, the next increment, a retry with a corrected "
            "prompt) is delegated too, with a new "
            "delegate_start(subagent_id=..., prompt=...); your own metered rounds "
            "are for judgment — acceptance, integration, honest settlement — not for "
            "co-building around a $0 run. If your run asks a question (delegate_wait "
            "returns waiting_on_user), answer it from the task context with "
            "delegate_answer; a question above your authority — money, scope, external "
            "actions — goes to your human via progress while you keep waiting (a timeout_at "
            "question benign-declines at the engine timeout; timeout_at=null waits until answered). "
            "If your task text instructs you to execute the work natively yourself, that "
            "instruction described the metered fallback and is superseded by this dispatch. "
            "Route thinking-work (code, research, generation) through "
            "delegate_start/delegate_wait; your own run_command/read_file rounds are for "
            "verification, integration, and acceptance. The parent's step-by-step context "
            "is the WORK ORDER for your delegated run's prompt, not a script for you to "
            "execute natively. If a child asks for an omitted source range, answer from "
            "the canonical task context with the exact range, source selector, and digest; "
            "a disclosed partial preview is never complete authority."
        )
        if lane is not None and lane.provenance == "policy" and lane.effective_lane == "light":
            note += (
                " You run on the LIGHT model lane by dispatch policy: custody chores "
                "(starting runs, waiting, reading results, relaying) belong on this "
                "cheap lane. For a genuine acceptance or integration judgment you may "
                "raise your own power with switch_model and drop back after — that is "
                "the sanctioned escalation, not a workaround."
            )
        if decision.reset_at:
            note += (
                f" The route's plan window is currently spent and resets at "
                f"{decision.reset_at}. Decide explicitly: wait for the reset, deliver "
                "partial work, or say you fell back — do not drift into spending."
            )
        return note
    if decision.reason in {"requested_native", "harness_not_configured"}:
        return ""  # the ordinary case has nothing to announce
    if decision.reset_at:
        # D28's fallback, stated as the CAPABILITY DELTA it is: the parent asked for the
        # already-paid substrate to be used when available, every profile of it is spent,
        # and the work is proceeding on metered money instead. Destination 2 of 3 (the
        # child's own prompt); the durable event and the parent's envelope carry the same
        # two facts. The reset instant is named so the child can weigh waiting against
        # spending instead of guessing.
        return (
            "EXECUTOR CAPABILITY DELTA: every plan window of the configured delegated "
            f"substrate is spent (resets at {decision.reset_at}), so you FELL BACK to "
            "METERED API tokens. Your parent asked for 'auto', which permits this "
            "fallback rather than a wait — but it is real money that the subscription "
            "would have covered: keep the work proportionate, and say in your result "
            "that you ran below the substrate you were scheduled for and why."
        )
    return (
        f"EXECUTOR: the configured delegated substrate is unavailable "
        f"({decision.reason}), so you are running on METERED API tokens. Your parent "
        "asked for 'auto', which permits this — but say so in your result."
    )


def executor_blocked_outcome(
    decision: SubagentExecutorResolution,
    *,
    availability: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """The terminal ``(text, usage)`` of a child that was pinned and could not run.

    Deliberately NOT a fallback: the task ends unrun and typed, having spent nothing.
    """
    availability = availability if isinstance(availability, dict) else {}
    if (
        availability.get("route_kind") == "api_model"
        or (decision.requested == "native" and decision.reason == "credentials_unavailable")
    ):
        alternatives = availability.get("alternatives")
        alternatives = alternatives if isinstance(alternatives, list) else []
        text = (
            "⚠️ SUBAGENT_UNAVAILABLE: the selected API-model actor has no usable "
            "credentials for its exact configured route. The task was NOT run and the "
            "host did not substitute another model or substrate. Current configured "
            "alternatives (not ranked): "
            + json.dumps(alternatives, ensure_ascii=False, sort_keys=True)
        )
        return text, {
            "execution_status": "infra_failed",
            "reason_code": "subagent_executor_unavailable",
            "unavailable_reason": "credentials_unavailable",
            "alternatives": alternatives,
            "host_fallback": False,
        }
    if decision.reason in ("delegate_tools_invisible", "delegate_visibility_unverified"):
        # Q1A preflight (2026-08-10 amendments): the route is healthy but the
        # child's MATERIALIZED toolset does not carry the delegate verbs — or
        # the toolset introspection itself failed, so visibility is UNKNOWN,
        # not disproven (distinct reason: the terminal states exactly what is
        # known). Either way the pin cannot be honored, and the fix is tool
        # policy/contract, not waiting for the route to recover.
        detail = (
            "the delegate tools (delegate_start/delegate_wait/delegate_cancel) "
            "are not visible in its materialized toolset"
            if decision.reason == "delegate_tools_invisible"
            else "the toolset introspection failed, so the delegate tools' "
            "(delegate_start/delegate_wait/delegate_cancel) visibility could "
            "not be verified"
        )
        text = (
            "⚠️ EXECUTOR_UNAVAILABLE: this subagent was pinned to the delegated "
            f"substrate (executor='harness'), but {detail}, so the pin cannot be "
            "honored. The task was NOT run on metered API tokens. Fix the tool "
            "policy / task contract that hides the delegate verbs, or explicitly "
            "select another Available subagent."
        )
        # Literal codes (not `decision.reason`) so the provenance drift guard
        # keeps seeing every code the runtime can emit.
        if decision.reason == "delegate_visibility_unverified":
            return text, {"execution_status": "infra_failed", "reason_code": "delegate_visibility_unverified"}
        return text, {"execution_status": "infra_failed", "reason_code": "delegate_tools_invisible"}
    # ":delegation_" is route_health's structural refinement (Phase D3): the
    # catalog row's manifest cannot run delegated work AT ALL, so "reschedule
    # once the route recovers" would honestly mean "wait forever" (e.g. agy).
    text = (
        "⚠️ EXECUTOR_UNAVAILABLE: this subagent was pinned to the delegated substrate "
        f"(executor='harness') and the route cannot run: {decision.reason}."
        + (f" It resets at {decision.reset_at}." if decision.reset_at else "")
        + " The task was NOT run on metered API tokens, because that spend is exactly "
        "what the pin exists to prevent. "
        + ("This harness structurally cannot run delegated work (its manifest does not "
           "support it), so waiting will not heal it: change the delegated route, or "
           "select another Available subagent explicitly."
           if ":delegation_" in decision.reason else
           "Reschedule once the route recovers, or explicitly select another "
           "Available subagent.")
    )
    return text, {
        "execution_status": "infra_failed",
        "reason_code": "subagent_executor_unavailable",
    }
