"""Nanny tools: run a subagent's cognition on an already-paid subscription session.

A delegated subagent is an ORDINARY Ouroboros subagent acting as a NANNY: it lives in
the task tree with its own deadline and authority, but instead of thinking on metered
API tokens it starts a Claudexor run, watches it, and brings the result home. Because
the nanny IS the host, verification receipts stay host-authored and the harness's
output is a claim, not proof.

The transport those verbs drove — the owned Claudexor daemon and its gateway — retired
with its modules (Seed 0): every verb below now answers with ONE typed refusal naming the
retirement, and the harness lane itself is left for the replacement that will own it.

Four verbs: ``delegate_start``, a time-bounded ``delegate_wait``,
``delegate_cancel``, and ``delegate_answer`` (a run's pending interactive question is
answered by its own nanny — owner decision 7=A, poltergeist phase B). There is still
no ``hurry`` — Claudexor's only control verb is ``cancel``, and cancelling a reviewer
destroys the verdict you wanted.

Read-only and mutating children share ONE nanny and ONE transport. The only difference
is the access profile the HOST derives from the calling task's authority (``readonly``
vs ``workspace_write``) and the run shape that follows from it; there is no second
pipeline and no second slot. The child gets a broker tool, never a shell, so it can ask
the host to run something but never choose with what powers.

Custody: the daemon token never left the gateway that held it; nothing here put it in
a ToolContext, a child environment, or a harness sandbox. WHICH run belonged to WHICH
task was decided by ``ouroboros.delegate_custody`` against the durable event log, not by
a dict this process happens to still hold.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ouroboros import delegate_custody as custody
from ouroboros import delegate_progress as progress
from ouroboros.delegate_custody import RunCustody as _RunCustody
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.subagent_work_order import (  # noqa: F401 - compatibility re-export
    _FIELD_CHARS as _ASSIGNMENT_FIELD_CHARS,
    assignment_instructions as _assignment_instructions,
)
from ouroboros.delegate_source_coverage import (
    add_terminal_source_verification,
    record_started_custody,  # noqa: F401 - compatibility re-export: tests name it here
)
from ouroboros.delegate_supervision import delegate_wait_entry as _delegate_wait_entry
from ouroboros.delegate_start_instructions import (
    HOST_INSTRUCTIONS as _HOST_INSTRUCTIONS,
    UNPROVEN_BOUNDARY_INSTRUCTION as _UNPROVEN_BOUNDARY_INSTRUCTION,
    append_coordination_context,
)
from ouroboros.subagent_runtime import (  # noqa: F401 - shared primitive re-export
    delegate_start_entry as _delegate_start_entry,
    exact_start,
)
from ouroboros.subagent_runtime import (
    prepare_delegate_start_actor,  # noqa: F401 - compatibility re-export: tests patch it here
)
# The staged-output + read-receipt cluster lives in its own module (size gate);
# re-exported here because sibling code, the tests and the convergence census all
# name it on THIS surface, and `_READ_COVERAGE` must stay the same object.
from ouroboros.delegate_output import (  # noqa: F401
    _ARTIFACT_SUBDIR,
    _BULK_FIELDS,
    _PAYLOAD_ENVELOPE_HEADROOM,
    _PREVIEW_PREFIX_SLACK,
    _PREVIEW_STEPS,
    _READ_COVERAGE,
    _READ_COVERAGE_MAX_KEYS,
    _STRUCTURED_FIELDS,
    _covered_whole,
    _preview_payload,
    _resolve_full_primary_output,
    _safe_run_filename,
    _stage_full_output,
    acknowledge_staged_output_read,
)
# The interactive-question cluster (waiting_on_user + delegate_answer) lives in
# its own module too (size gate); re-exported here because the wait loop, the
# tests and sibling code name it on THIS surface, and `_REPORTED_INTERACTIONS`
# must stay the same object.
from ouroboros.delegate_interactions import (  # noqa: F401
    _ANSWER_NOTES,
    _REPORTED_INTERACTIONS,
    _answer_delivery_unknown,
    _bounded_interactions,
    _delegate_answer,
    _interactions_are_news,
    _normalized_answers,
    _waiting_on_user_payload,
)
# The refusal/emit/ownership helpers live in the neutral leaf
# `ouroboros/delegate_shared.py` (moved to break the facade back-edge:
# delegate_interactions needs them, and an extracted module never imports the
# facade back); re-exported here because sibling code, the tests and
# monkeypatch targets name them on THIS surface.
from ouroboros.deadline_utils import (  # noqa: F401 - compatibility re-export: tests name it here
    deadline_expired,
)
from ouroboros.delegate_shared import (  # noqa: F401
    _emit,
    _fail,
    _owned_run,
)
# The C1 integration seam (mutation authority, execution snapshots, retry binding,
# terminal patch capture) lives in its own module (size gate); re-exported here
# (same objects) because sibling code and the tests address it on THIS surface.
# `_fail` is NOT re-imported from it — the one shared refusal author is
# `delegate_shared._fail`, which delegate_integration itself imports.
from ouroboros.tools.delegate_integration import (  # noqa: F401
    _CAPTURE_DELEGATED_SNAPSHOT,
    _capture_block,
    _capture_terminal_patch,
    _mutation_authority,
    _payload_mutation_authority,
    _payload_selector_refusal,
    _provision_payload_snapshot,
    _provision_snapshot,
    _resolve_retry_invocation,
    _resolved,
    _retry_binding_refusal,
    _validated_invocation,
    claimed_start_request,
    payload_host_instructions,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.subagents import DelegatedRunShape

log = logging.getLogger(__name__)

_TERMINAL_STATES = custody.TERMINAL_STATES

# The containment verifiers moved to `ouroboros/delegate_containment.py` whole (the
# module-size gate); re-exported here because the nanny's seams and the existing
# tests address them through this module.
from ouroboros.delegate_containment import (  # noqa: E402
    _ACCESS_UNVERIFIED,  # noqa: F401  (re-export: tests address it through this module)
    _Breach,
    _home_isolation_breach,
    _widened_access,
    home_nested_under_operator_home,
)
_POLL_INTERVAL_SEC = 3.0
# Claudexor's own schema bound on maxSeconds (packages/schema/src/control.ts).
_CLAUDEXOR_MAX_SECONDS = 604_800

# The process-local memo of the durable custody rows (the authority lives in the module
# above); re-bound here because sibling code and tests name it on this surface.
_CUSTODY = custody._CUSTODY


def _host_instructions(authority: "DelegatedRunShape", assignment: str = "",
                       payload_skill: str = "") -> str:
    """The system-prompt text this run's shape earns. One builder, no dialect.

    ``assignment`` is the host-authored contract block (``_assignment_instructions``);
    appended last so the prohibitions stay the opening statement. A payload run
    (``payload_skill`` non-empty) gets the truthful variant: editing the selected
    skill's user-authored files IS the assignment (gate fix 3).
    """
    text = _HOST_INSTRUCTIONS
    if payload_skill:
        text = payload_host_instructions(text, payload_skill)
    if authority.delegated:
        text += _UNPROVEN_BOUNDARY_INSTRUCTION
    if assignment:
        text += "\n\n" + assignment
    return text


def _build_start_instructions(
    authority: "DelegatedRunShape", assignment: str = "", payload_skill: str = "",
    coordination_context: str = "",
) -> tuple[str, str]:
    """Build the bounded instruction field for a fresh physical start."""
    return append_coordination_context(
        _host_instructions(authority, assignment, payload_skill), coordination_context,
        instruction_budget_chars=_ASSIGNMENT_FIELD_CHARS,
    )


def _derive_authority(ctx: ToolContext) -> "DelegatedRunShape":
    """Derive the run shape from the task's own authority — one question, asked here.

    Host-derived, never model-supplied: the child asks the host to run something, and
    the host decides with what powers. Ouroboros asks for an access PROFILE and lets
    Claudexor pick the mechanism (fs sandbox, tool allowlist, ...) — no harness branch.

    The SHAPE itself belongs to ``subagents.delegated_run_shape``, which the dispatcher
    also reads: this function only answers "does this task hold a mutating surface",
    which is the one part that needs the live ``ToolContext``. Two authorities qualify
    (B5, owner 2=A): an ACTING CHILD with a valid write surface, and the ROOT of an
    EXTERNAL-WORKSPACE task — the root already holds write+shell inside the project,
    so its delegated runs carry the same mutating shape, bounded by the same
    workspace; ``_mutation_authority`` (``tools.delegate_integration``) validates the
    concrete target either way.
    """
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tool_access import active_tool_profile

    profile = active_tool_profile(ctx)
    mutating = profile in ("acting_subagent", "external_workspace_task")
    if mutating:
        from ouroboros.presence_authority import presence_ceiling_allows_delegated_surface

        constraint = getattr(ctx, "task_constraint", None)
        surface = str(getattr(constraint, "surface", "") or "external_workspace")
        mutating = presence_ceiling_allows_delegated_surface(ctx, surface)
    return delegated_run_shape(mutating)


def _presence_delegate_read_refusal(ctx: ToolContext) -> str:
    from ouroboros.presence_authority import presence_ceiling_allows_delegated_read

    if presence_ceiling_allows_delegated_read(ctx):
        return ""
    return _fail(
        "delegate_start",
        "presence_delegate_read_root_unselected",
        "This Presence profile did not select whole-root read access for the active repository, "
        "so a delegated harness cannot honestly be started inside that broader read surface.",
    )


def _containment_breach(detail: Dict[str, Any], authority: "DelegatedRunShape") -> Optional[_Breach]:
    """Everything the ENGINE enforced, checked against what the host asked for.

    ONE reader for both halves of containment — the access profile and the harness
    HOME — because they fail identically: the request is only a request, the engine
    derives the truth, and a verification written for one half leaves the other
    trusting an echo. The HOME half is asked only of a run that carried the marker;
    a read-only child is scoped by Claudexor's ordinary envelope and asks for nothing.
    """
    widened = _widened_access(detail, authority.access)
    if widened:
        return _Breach(
            "access_profile_widened",
            f"The delegated run was enforced at access profile {widened!r} while this "
            f"task is only entitled to {authority.access!r}.",
            {"entitled_access": authority.access, "effective_access": widened},
        )
    if authority.delegated:
        return _home_isolation_breach(detail)
    return None


_NESTED_HOME_NOTE = (
    "The scoped harness HOME for this run sits INSIDE the operator's own home, which is "
    "where the engine roots its scoped homes. That is allowed and the run's work is usable, "
    "but it is not isolation from the operator's home: everything there — credential stores "
    "and the Claudexor daemon token included — stays readable at its absolute path. Do NOT "
    "describe this run as running in an isolated home"
)

_NO_BOUNDARY_NOTE = (
    "NO OS-ENFORCED BOUNDARY was applied to this run. The engine reported no confinement "
    "mechanism for it, so the only containment it had is a scoped HOME — a redirect of "
    "`~`-relative lookups, which leaves the operator's home, credential stores and the "
    "Claudexor daemon token readable at their absolute paths. The run was allowed and its "
    "work is usable; do NOT describe it as sandboxed, confined or isolated, and weigh its "
    "output as coming from an unconfined shell in this worktree"
)


def _containment_evidence(detail: Dict[str, Any]) -> Dict[str, Any]:
    """What the ARTIFACTS prove about this run's containment — never what was asked.

    DESTINATION 3 of the disclosure: this is what the nanny hands its parent.

    BOTH halves, in one reader, because a report that states only the scoped HOME is the
    defect this function was rewritten to remove: a run with a kernel-enforced boundary
    and a run with none produced BYTE-IDENTICAL evidence here, both reading
    ``verified: true`` with a note about the HOME. Claudexor's own confinement document
    says the scoped home "is not a boundary and must never be reported as one".

    The predicate is what the engine says it APPLIED (``confinement_mechanism`` plus the
    denied path it proved), never which OS this host is. Ouroboros does not know what the
    engine did — only the artifact does — and a platform test would additionally freeze
    today's answer: the day a boundary ships for another OS, this reader is already right.

    Judged by the SAME predicate that halts a breached run, not by having been reached
    after it: a report whose honesty depends on its call site is one refactor away from
    claiming a containment nobody checked.

    This is also where a MISSING fact lands, because it is a reporting question and not an
    enforcement one: an attempt that disclosed nothing proves nothing, so ``verified``
    stays false and ``disclosed`` says how much of the run is actually covered. Silence
    read as success and silence enforced as a fault are the two ways to be wrong here,
    and stating the count avoids both.
    """
    # The attempt-artifact reader retired with the Claudexor gateway (Seed 0): no run
    # this build starts can leave one behind, so the list is empty and the report below
    # keeps saying UNPROVEN rather than inventing a boundary nobody proved.
    attempts: List[Any] = []
    disclosed = sum(1 for attempt in attempts if attempt.home_isolated is not None)
    # An engine that reported nothing is indistinguishable from one that applied nothing,
    # and the mechanisms the ATTEMPTS name are the vocabulary — Ouroboros keeps no list of
    # its own to fall out of date. "Every attempt" and not "any": one unconfined attempt
    # is an unconfined run.
    mechanisms = sorted({attempt.boundary_mechanism for attempt in attempts})
    boundary = mechanisms[0] if attempts and len(mechanisms) == 1 and mechanisms[0] else ""
    # A3: the engine's own typed reason for a missing boundary — an AMPLIFIER of
    # the unconfined disclosure (why there is no mechanism on this host), parsed
    # from the same attempt artifact. Telemetry only, never an admission token.
    unavailable_reasons = sorted({
        attempt.confinement_unavailable_reason
        for attempt in attempts if attempt.confinement_unavailable_reason
    })
    # A3: a scoped home NESTED under the operator's own is allowed (the engine's
    # own layout — disclosed, never refused), but it is NOT "outside the
    # operator's own": the daemon token stays reachable at its absolute path.
    # Recorded on the report and honoured by every branch below, so a run that
    # ALSO carries an OS boundary can no longer be promoted to verified with a
    # note that contradicts its own artifact — and so `_record_containment` keeps
    # emitting the durable unconfined row for it.
    nested = home_nested_under_operator_home(detail)
    report = {"verified": False, "attempts": len(attempts), "disclosed": disclosed,
              "os_boundary": boundary, "nested_under_operator_home": nested}
    if unavailable_reasons:
        report["confinement_unavailable_reason"] = "; ".join(unavailable_reasons)
    breach = _home_isolation_breach(detail)
    if breach is not None:
        return {**report, "note": breach.detail}
    if not disclosed:
        return {**report, "note":
                "this run recorded no harness-HOME fact, so its confinement is UNPROVEN "
                "— do not report it as isolated"}
    if disclosed < len(attempts):
        return {**report, "note":
                "not every attempt of this run recorded a harness-HOME fact, so its "
                "confinement is UNPROVEN — do not report it as isolated"}
    if nested:
        note = _NESTED_HOME_NOTE
        if boundary:
            note += (
                f" (an {boundary} boundary WAS applied — weigh it as the real containment, "
                "but the scoped HOME is not one)"
            )
        if unavailable_reasons:
            note += " (engine-declared reason: " + "; ".join(unavailable_reasons) + ")"
        return {**report, "note": note}
    if not boundary:
        note = _NO_BOUNDARY_NOTE
        if unavailable_reasons:
            note += (
                " (engine-declared reason: " + "; ".join(unavailable_reasons) + ")"
            )
        return {**report, "note": note}
    return {**report, "verified": True, "note":
            f"every attempt recorded a scoped harness HOME outside the operator's own AND "
            f"an applied {boundary} boundary, proven against a path it denies"}


def _terminal_payload(run_id: str, detail: Dict[str, Any],
                      authority: "DelegatedRunShape") -> Dict[str, Any]:
    summary = custody.summary_of(detail)
    payload = {
        "status": "terminal",
        "run_id": run_id,
        "state": str(summary.get("state") or ""),
        # The APPLIED model, from the engine's own summary — '' when the run
        # never disclosed one (live unpinned runs really do), shown as absence
        # rather than the requested model dressed up as the applied one.
        "model": str(summary.get("model") or ""),
        "outcome_banner": detail.get("outcomeBanner"),
        "outcome_facts": summary.get("outcomeFacts"),
        "output_conformance": summary.get("outputConformance"),
        "final_summary": detail.get("finalSummary"),
        "primary_output": detail.get("primaryOutput"),
        "failure": summary.get("failure"),
        "last_seq": int(detail.get("lastSeq") or 0),
        "cost": _reported_cost(summary),
        # The ACCESS half of the same honesty, on EVERY terminal payload — see
        # `_access_evidence`. Both lanes: `readonly` staying `readonly` is the profile
        # that matters most, while `containment` is asked only of marker-carrying runs.
        "access_evidence": _access_evidence(detail, authority.access),
    }
    if authority.delegated:
        payload["containment"] = _containment_evidence(detail)
    facts = payload.get("outcome_facts")
    if isinstance(facts, dict) and str(facts.get("reason") or "") == "input_required":
        # The codex-shaped question (B4): that lane has no mid-run channel, so a
        # question arrives as this TERMINAL. There is deliberately NO rerun verb
        # here — the engine's rerun_with_feedback would start a run outside this
        # task's custody trail — so the honest answer path is a plain new start.
        payload["input_required_note"] = (
            "This run ended NEEDING INPUT (outcome_facts.reason=input_required — "
            "see outcome_facts.work_state.required_inputs). Its harness has no "
            "mid-run question channel, so the question arrives as this terminal. Answer it by "
            "starting a plain NEW delegate_start(subagent_id=..., prompt=...) whose "
            "prompt carries the original "
            "assignment plus the answers; custody of the new run stays with you. "
            "Do not look for a rerun/decision verb — none exists on this surface."
        )
    return payload


def _access_evidence(detail: Dict[str, Any], expected: str) -> Dict[str, Any]:
    """What the engine's own DERIVED profile proves about this finished run.

    ``effectiveAccess`` is the only witness: ``summary["access"]`` is computed as
    ``effectiveAccess ?? the client's own request``, so reading it compares the request
    against itself and always passes. A WIDER profile is already a breach before this
    runs; an ABSENT one cannot be enforced on a run that is over — cancelling a
    succeeded run to punish missing evidence would destroy the result the lane exists
    to fetch (the v6.87.37 lesson) — so it is named here instead.
    """
    summary = custody.summary_of(detail)
    effective = str(summary.get("effectiveAccess") or "")
    state = str(summary.get("state") or "")
    report = {"requested": expected, "effective": effective,
              "verified": bool(effective), "state": state}
    if effective:
        return report
    if state in custody.SUCCEEDED_STATES:
        return {**report, "note":
                "this run SUCCEEDED without ever disclosing an effective access "
                f"profile, so there is no evidence the engine enforced {expected!r} — "
                "do not report its containment as verified"}
    return {**report, "note":
            "no effective access profile was disclosed; a run that did not succeed may "
            "never have had one, so this is absence of evidence, not a breach"}


def _record_containment(ctx: ToolContext, entry: Optional[_RunCustody],
                        payload: Dict[str, Any]) -> None:
    """DESTINATION 1 of the disclosure: the durable record, written once per run.

    A missing boundary is not a fault and produces no refusal, which is exactly why it
    needs a durable line of its own — the run succeeds, its patch is integrated, and
    nothing else in the record would ever say the work came out of an unconfined shell.
    Emitted from what the PARENT was told, so the two cannot disagree.

    "Once per run" is now a DURABLE fact rather than a process-local one: the custody
    entry is replayed from the event log, so a restarted worker polling an already
    terminal run does not append a second identical finding.

    A NESTED scoped home is disclosed even when an OS boundary WAS recorded (A3):
    the boundary is real containment, the scoped home is not, and suppressing the
    row for that shape left the one durable line that says "this ran with the
    operator's home reachable" unwritten.
    """
    containment = payload.get("containment")
    if not isinstance(containment, dict):
        return
    if containment.get("os_boundary") and not containment.get("nested_under_operator_home"):
        return
    if entry is not None and entry.containment_disclosed:
        return
    _emit(ctx, custody.UNCONFINED, {
        "run_id": entry.run_id if entry is not None else "",
        "route": entry.route_id if entry is not None else "",
        "state": str(payload.get("state") or ""),
        "os_boundary": str(containment.get("os_boundary") or ""),
        "attempts": containment.get("attempts"),
        "home_disclosed": containment.get("disclosed"),
        "nested_under_operator_home": bool(containment.get("nested_under_operator_home")),
        "note": containment.get("note"),
        **({"confinement_unavailable_reason": containment["confinement_unavailable_reason"]}
           if containment.get("confinement_unavailable_reason") else {}),
    })
    if entry is not None:
        entry.containment_disclosed = True


def _reported_cost(summary: Dict[str, Any]) -> Dict[str, Any]:
    """What this run cost, as the AGENT will read it.

    This is the payload the nanny relays to its parent, so it must tell the same story
    the ledger does. It used to hardcode `$0.00 / final` — the exact shape the settlement
    fix was written to eliminate — so a run that really charged money settled honestly in
    the ledger and then told the reasoning path the work was free.
    """
    spend, estimated = custody.disclosed_spend(summary)
    if spend is None:
        return {
            "cost_usd": None,
            "cost_final": False,
            "note": "the harness disclosed no spend for this run; treat the cost as UNKNOWN, not zero",
        }
    if estimated:
        # The amount is the best fact anyone has, so it rides; the FINALITY does not. An
        # estimated zero is not a proven free session and an estimated charge is not a
        # closed book — both are `cost_final: False`, matching the ledger row exactly.
        return {
            "cost_usd": spend,
            "cost_final": False,
            "note": "the harness ESTIMATED this run's spend rather than settling it; treat "
                    "the amount as APPROXIMATE and the cost as NOT final",
        }
    if spend > 0:
        return {
            "cost_usd": spend,
            "cost_final": True,
            "note": "this run was BILLED — it did not ride the subscription",
        }
    return {
        "cost_usd": 0.0,
        "cost_final": True,
        "note": "subscription session — already paid; the nanny's own model calls are metered separately",
    }


# -- output delivery -----------------------------------------------------------


def _delivered_terminal_payload(ctx: ToolContext, run_id: str, detail: Dict[str, Any],
                                authority: "DelegatedRunShape",
                                entry: Optional[_RunCustody] = None,
                                gateway: Any = None) -> Dict[str, Any]:
    """The terminal payload, delivered whole or declared partial — never head-cut.

    ``final_summary``/``primary_output`` carry the run's real work product, and Claudexor
    returns a preview of up to 256 KiB. Outer truncation would head-cut that at the tool
    result limit and sever the JSON mid-string, which destroys the document rather than
    shortening it. So the payload bounds ITSELF against the same limit the truncator
    applies, and the remainder becomes a readable artifact — after the engine's bounded
    preview has been resolved to the verified full artifact, because a payload built on
    a truncated preview delivers 256 KiB wearing the whole result's name.
    """
    full = _terminal_payload(run_id, detail, authority)
    if entry is not None:
        add_terminal_source_verification(full, entry)
    # Requested-vs-applied model, the review lane's own lexicon and rule
    # (AgentSessionReviewExecutor): compared only when BOTH are non-empty —
    # the engine writes aliases ('sonnet' beside 'claude-opus-5'), so a
    # mismatch is an advisory disclosure, never a failure of the run.
    requested_model = str(getattr(entry, "model", "") or "") if entry is not None else ""
    applied_model = str(full.get("model") or "")
    if requested_model and applied_model and requested_model != applied_model:
        full["capability_delta"] = [{
            "kind": "capability_delta",
            "requested": f"model {requested_model}",
            "effective": f"model {applied_model}",
            "reason": "session_route_resolves_its_own_model",
        }]
    primary, full_ok, full_note = _resolve_full_primary_output(
        gateway, run_id, full.get("primary_output"))
    full["primary_output"] = primary
    budget = tool_result_limit("delegate_wait")
    text = json.dumps(full, ensure_ascii=False, indent=2)
    if len(text) <= budget - _PAYLOAD_ENVELOPE_HEADROOM:
        full["output_delivery"] = {
            # An unresolved engine-side truncation makes even an inline-fitting payload
            # NOT the whole result: complete/consumed follow the verified fact.
            "complete": full_ok, "consumed": full_ok, "inline_is_preview": False,
            "total_chars": len(text), "artifact": None, "read_next": None,
            "note": ("The whole terminal payload is inline." if full_ok else
                     "INLINE BUT INCOMPLETE AT THE SOURCE: the engine reported its "
                     "primary output as a bounded preview and the full artifact could "
                     "not be matched to the size or the preview the run itself reported "
                     "(see primary_output_full). Treat this "
                     "as incomplete evidence, not as the verdict."),
        }
        if full_note is not None:
            full["output_delivery"]["primary_output_full"] = full_note
        return full
    artifact = _stage_full_output(ctx, run_id, text)
    _emit(ctx, custody.OUTPUT_SPILLED, {"run_id": run_id, "total_chars": len(text),
                                        "artifact": (artifact or {}).get("path", ""),
                                        "bytes": (artifact or {}).get("bytes"),
                                        "sha256": (artifact or {}).get("sha256", ""),
                                        "staged": artifact is not None,
                                        "full_content": bool(full_ok and artifact is not None)})
    if entry is not None and artifact is not None:
        if entry.output_consumed and entry.output_sha and artifact["sha256"] != entry.output_sha:
            # The ack named OTHER bytes: a re-stage of different content at the same
            # path owes a fresh acknowledgement — consumed never transfers by path.
            entry.output_consumed = False
        entry.output_sha = artifact["sha256"]
        entry.output_artifact = artifact["path"]
        entry.output_complete = bool(full_ok)
    return _preview_payload(full, text, artifact, budget,
                            consumed=bool(entry is not None and entry.output_consumed),
                            full_ok=full_ok, full_note=full_note)


# -- tools --------------------------------------------------------------------


def _start_request(ctx: ToolContext, route: Any, authority: "DelegatedRunShape",
                   root: str, text: str, seconds: int, instructions: str, execution_root: str = "") -> Dict[str, Any]:
    """The POST body for one delegated run, built from the derived SHAPE.

    Extracted so the caller stays inside the method-size gate, and so the body has ONE
    author: the shape decides the mode and whether the delegated marker rides along,
    and nothing here re-derives either.

    ``seconds`` and ``instructions`` arrive PRE-BUILT rather than being derived here:
    a transport retry of a pending invocation must present a byte-identical body for
    the engine's replay match, and both the deadline-derived bound and the
    contract-derived instructions can change between calls, so the caller decides
    whether to recompute them or replay the recorded ones (the retry path never calls
    this function at all — it replays the stored canonical body verbatim).
    """
    request: Dict[str, Any] = {
        "prompt": text,
        # Built from the SHAPE plus the task contract, so a mutating delegated
        # child is told that its boundary is a request and not a fact — the same
        # disclosure the durable record and the parent's result carry, in the one
        # place the child can read — and the nanny's own objective rides along
        # structurally (`_assignment_instructions`).
        "instructions": instructions,
        # The engine's default authPreference is `auto` = subscription-first WITH
        # policy fallback to a paid API key. That fallback is invisible to us and
        # would be settled at a confident $0.00 — the one shape the ledger must
        # never produce. Ask for the substrate we are actually claiming.
        "authPreference": "subscription",
        # The run SHAPE comes from the derived authority, not from re-deriving it
        # here: one predicate decides what this child may do, and the mode follows it.
        "mode": authority.mode,
        "scope": {"kind": "project", "root": root},
        # PIN, not preference: `primaryHarness` only fronts the engine's
        # auto-pool, which still holds every other doctor-OK harness — the run
        # could fail over onto a route the owner never configured. The
        # explicit one-element `harnesses` pool is the engine's pinning
        # contract (its own MCP surface spells a forced route exactly this
        # way): the child rides THIS route or the start refuses typed.
        "harnesses": [route.route_id],
        "primaryHarness": route.route_id,
        "access": authority.access,
    }
    if authority.isolation:
        # `delegated` rides WITH the isolation, from the same record, because they
        # are the same decision: `live` is in-place, and in place is exactly where
        # Claudexor would otherwise hand the harness the operator's real `$HOME`
        # — daemon control token included. Sending one without the other is the
        # containment hole, so neither is assembled separately.
        request["execution"] = {"isolation": authority.isolation, "delegated": authority.delegated,
                                **({"workspaceRoot": execution_root} if execution_root else {})}
    if route.model:
        request["model"] = route.model
    if route.effort:
        request["effort"] = route.effort
    if route.profile_id:
        # Account pin (D-U5), reviewer-slot wire contract; strict (D-U6). In the stored canonical body, so a retry_of replay stays byte-identical, pin included.
        request["credentialProfileId"] = route.profile_id
    if seconds:
        request["maxSeconds"] = seconds
    return request


def _delegate_start(ctx: ToolContext, prompt: str, max_seconds: Optional[int] = None,
                    retry_of: Optional[str] = None, root: Optional[str] = None,
                    bucket: Optional[str] = None, skill_name: Optional[str] = None,
                    _resolved_binding: Any = None,
                    _canonical_work_order_fingerprint: str = "",
                    _work_order_source_request: Any = None,
                    _coordination_context: str = "") -> str:
    # The delegated-run transport retired with its own modules (Seed 0): the owned
    # Claudexor daemon and its gateway are gone from this build, so there is no harness
    # lane left to start a run on. This is the SAME typed refusal the verb already
    # returned when that daemon was unreachable — now naming the retirement instead of a
    # fault — and never a silently empty success or a fallback onto metered API spend.
    # `definitely_unrun` carries the producer's own no-run verdict: nothing was started,
    # so nothing has to be waited on, cancelled or recovered.
    from ouroboros.subagents import CLAUDEXOR_RETIRED

    return _fail(
        "delegate_start", CLAUDEXOR_RETIRED,
        "Delegated runs no longer exist in this build: the owned Claudexor daemon and its "
        "gateway were retired and removed, so there is no harness lane to start a run on. "
        "No run was started by this call and none is live. Do the work here, or schedule a "
        "native child (schedule_subagent) when it needs another agent.",
        executor="blocked", definitely_unrun=True,
    )


def _started_payload(handle: Dict[str, Any], run_id: str, route: Any, access: str,
                     authority: "DelegatedRunShape", root: str, *, durable: bool,
                     recovering: bool, invocation_id: str, snapshot_id: str, target_root: str,
                     baseline_sha: str) -> str:
    """The one author of delegate_start's started result (note + payload).

    The AUTHORITY guidance and the CUSTODY warning are independent facts about the same
    start, so both are said. An undurable custody row is the louder one and goes first:
    a nanny that walks away from an uncustodied MUTATING run leaves a live shell in its
    own worktree that nothing outside this process can name.
    """
    note = "" if durable else (
        "CUSTODY IS NOT DURABLE: the run started, but its custody row could not be written, "
        "so nothing outside this worker can wait on, cancel or settle it. Do not walk away "
        "from it — finish it or delegate_cancel it in this session. ")
    note += (
        "You are the nanny and the host. Poll with delegate_wait; the run's own "
        "claims are evidence to check, not a verified result."
        + (
            " This run edits a PRIVATE SNAPSHOT of your write root, not the shared "
            "tree: at terminal its diff is captured for you, and NOTHING lands in "
            "the shared tree until you explicitly integrate_delegated_patch(run_id="
            "...) to apply or reject it — read the captured diff before you claim "
            "it, and never let the run commit. It was ASKED to run under a scoped "
            "HOME and an OS-enforced boundary; whether the engine applied either is "
            "a per-run fact that delegate_wait reads back from the run's own "
            "artifacts. A host with no boundary mechanism runs it anyway and says "
            "so there."
            if authority.isolation == "live" else
            " This run cannot write anything: it reads and answers."
        )
    )
    payload = {
        "status": "started" if durable else "started_uncustodied",
        "run_id": run_id,
        "run_dir": handle.get("runDir"),
        "route": route.route_id,
        "model": route.model,
        "effort": route.effort,
        "access": access,
        "mode": authority.mode,
        "isolation": authority.isolation or "envelope",
        "idempotent_recovery": recovering,
        # ASKED, not applied. The proof arrives with the run's own artifacts and is
        # relayed by delegate_wait; saying "isolated" here would be the exact claim
        # this whole verification exists to stop anyone from making.
        "scoped_home_requested": authority.delegated,
        "root": root,
        "custody_durable": durable,
        "invocation_id": str(invocation_id or ""),
        "note": note,
    }
    if not durable:
        payload["pending_invocation_id"] = str(invocation_id or "")
    if snapshot_id:
        # The C1 binding, stated where the nanny can read it: the run edits the
        # EXECUTION snapshot; the authority target receives nothing until apply.
        payload["execution_root"] = root
        payload["authority_target_root"] = target_root
        payload["baseline_id"] = baseline_sha
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _retire_orphaned_registration(ctx: ToolContext, gateway: Any, project_id: str, *,
                                  definite_refusal: bool, reason: str,
                                  project_persistent: bool = False,
                                  invocation_id: str = "",
                                  snapshot_id: str = "") -> Dict[str, Any]:
    """Retire a registration this start created but never bound to a run.

    Only when the daemon gave a DEFINITE negative answer (a 4xx refusal): a transport
    error, a 5xx, or a 2xx handle with no run id all mean the POST's fate is unknown, and
    a run may well be live against this very registration. An unverified outcome is never
    grounds for destroying state — the durable row names the id either way, which is what
    the old code lacked. The caller supplies the verdict, so every failing start reaches
    this one path instead of one branch retiring and its twin abandoning.

    The row also settles the INVOCATION's fate: ``definite: true`` retires the logical
    invocation id (a definitely refused invocation must not be reused — the daemon may
    hold its key against a body a reconfigured route can no longer reproduce, which
    would 409 forever), while an unknown outcome leaves it pending so a transport retry
    presents the same key and lands on whatever the daemon really has. Written even
    with no registration to retire, because the invocation's fate is its own fact.
    """
    if snapshot_id and definite_refusal:
        # The C1 execution snapshot THIS attempt provisioned. Only a definite refusal
        # proves no run can be live against it; an unknown outcome keeps it — the
        # pending invocation names it durably, and the startup GC reconciles it.
        try:
            from ouroboros.subagent_worktrees import remove_execution_snapshot

            remove_execution_snapshot(snapshot_id)
        except Exception:
            log.warning("Failed to retire delegated execution snapshot %s", snapshot_id,
                        exc_info=True)
    retired = False
    if project_id and definite_refusal and not project_persistent:
        try:
            gateway.remove_project(project_id)
            retired = True
        except Exception as exc:
            # A registration the daemon does not have is already retired: the same
            # absence-is-discharge fact `retire_project` settles on.
            retired = custody.daemon_says_absent(exc)
            if not retired:
                log.warning("Failed to retire orphaned delegated project %s", project_id, exc_info=True)
    if project_id or invocation_id:
        _emit(ctx, custody.START_FAILED, {"run_id": "", "project_id": project_id,
                                          "project_retired": retired, "reason": reason,
                                          "invocation_id": invocation_id,
                                          "definite": bool(definite_refusal)})
    if not project_id:
        return {"project_retired": False}
    if project_persistent and definite_refusal:
        # #362: a definite refusal still never deletes the user's stable
        # project (the f9356572 skip) — but the invocation's fate row above
        # has landed, so the lane cannot livelock on a forever-pending id.
        return {"project_retired": False, "project_id": project_id,
                "project_retention_reason": "persistent_registration"}
    if retired or definite_refusal:
        return {"project_retired": retired, "project_id": project_id}
    return {"project_retired": False, "project_id": project_id,
            "project_retention_reason": "start_outcome_unknown_run_may_exist"}


def _bounded_max_seconds(ctx: ToolContext, requested: Optional[int]) -> int:
    """Narrow-only: the delegated run may never outlive the nanny's own deadline.

    A caller must ask ``deadline_expired`` FIRST: an expired deadline cannot produce an
    honest bound at all, and this function's fallback is for a nanny that has NO
    deadline, never for one whose deadline is behind it.
    """
    from ouroboros.deadline_utils import deadline_remaining_sec

    remaining = int(max(0.0, deadline_remaining_sec(ctx)))
    try:
        asked = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        asked = 0
    candidates = [value for value in (asked, remaining) if value > 0]
    if candidates:
        # Clamp HERE too, not only on the fallback below: `max_seconds` is a model-supplied
        # tool argument with no maximum in its schema, so an explicit ask sailed past the
        # bound the fallback branch was careful about — the same defect, one branch over.
        return min(_CLAUDEXOR_MAX_SECONDS, min(candidates))
    # No positive bound is knowable: either the nanny has no deadline, or its deadline
    # has already passed. Omitting `maxSeconds` — the old behavior — handed the run
    # Claudexor's 7-day schema bound; the cap is damage limitation, and custody (the
    # durable start row plus reconciliation) is what actually stops an orphan.
    from ouroboros.config import get_task_abs_ceiling_sec

    # Claudexor bounds maxSeconds at 7 days (control.ts `.max(604_800)`), and the task
    # ceiling clamps only from BELOW — an owner who raises it past a week would make
    # every deadline-less start send an out-of-schema value.
    return min(_CLAUDEXOR_MAX_SECONDS, int(get_task_abs_ceiling_sec()))


def _halt_breached_run(ctx: ToolContext, gateway: Any, entry: _RunCustody,
                       breach: _Breach) -> str:
    """Stop a run the engine did not contain as asked, and say exactly what failed.

    The BREACH incident goes through ``custody.record_containment_fault``, the same
    writer an unverified cancel uses, so a breached run also surfaces as the CRITICAL
    health invariant that stays open until a terminal receipt resolves it. Emitting a
    look-alike event here instead left the breach out of the open-fault sweep.

    The stop itself goes through ``custody.cancel_and_verify`` — the ONE cancel path,
    with its four typed outcomes — and the sentence handed back to the agent is built
    from the outcome it returns. The ad-hoc cancel this replaced swallowed every
    exception into a log line and then said "The run was cancelled" unconditionally,
    which is precisely what ``record_containment_fault``'s own contract forbids: an
    incident must never surface "as a reassuring string in a tool result". An
    overpowered run that refused to stop was reported to the agent as stopped.
    """
    run_id = entry.run_id
    drive = custody.custody_root(ctx)
    try:
        cancelled = custody.cancel_and_verify(drive, gateway, entry, breach.code)
    except Exception:
        log.warning("Failed to cancel an uncontained delegated run %s", run_id, exc_info=True)
        cancelled = {"outcome": custody.CANCEL_CONTAINMENT_FAULT}
    custody.record_containment_fault(drive, entry, breach.code, breach.detail,
                                     fault=breach.code, **breach.facts)
    outcome = str(cancelled.get("outcome") or custody.CANCEL_CONTAINMENT_FAULT)
    return _fail(
        "delegate_wait", breach.code,
        f"{breach.detail} {_CANCEL_NOTES.get(outcome, '')} Do not retry it: this is a "
        "containment fault in the transport or the engine, not a task failure — report "
        "it and continue within your own authority.",
        run_id=run_id, cancel_outcome=outcome, **breach.facts,
    )


# The typed external-wait lease lives in `delegate_progress` (the wait-liveness
# module); re-bound here because the wait's own seams and the tests name it on
# this surface, exactly like the staged-output cluster above.
_external_wait_lease_until = progress.external_wait_lease_until
_emit_external_wait_lease = progress.emit_external_wait_lease


def _delegate_wait(ctx: ToolContext, run_id: str, wait_sec: Optional[int] = None,
                   since_seq: Optional[int] = None) -> str:
    """Typed refusal: the delegated-run transport retired with its own modules (Seed 0).

    There is no daemon left to hold a window against and no run left to watch: the owned
    Claudexor gateway is gone from this build, so the wait the nanny verbs were built
    around (journal-cursor progress, terminal settlement, containment faults) has no
    producer. The answer is the refusal this verb already returned on an unreachable
    daemon, naming the retirement instead of a fault — never a wait reported as quiet,
    which would read as a run that finished and said nothing.
    """
    from ouroboros.subagents import CLAUDEXOR_RETIRED

    return _fail(
        "delegate_wait", CLAUDEXOR_RETIRED,
        "There is no delegated run to wait on: the owned Claudexor daemon and its gateway "
        "were retired and removed from this build, so no run is live and none was started. "
        "Read the result you already hold, or re-plan the work as a native child "
        "(schedule_subagent) if it needs another agent.",
        run_id=str(run_id or "").strip(),
    )


# The cancel-outcome vocabulary, retained for the fault reporter above: it is keyed by
# the durable custody outcomes, not by any transport, so it outlives the engine that
# used to produce them.
_CANCEL_NOTES = {
    custody.CANCEL_CONFIRMED: "VERIFIED terminal: the run has stopped. Partial artifacts are "
                              "preserved; a cancelled run has no verdict.",
    custody.CANCEL_REQUESTED: "The daemon ACCEPTED the cancel but the run is not terminal yet. "
                              "It is still running. Call delegate_wait to confirm it stops.",
    custody.CANCEL_FAILED: "The daemon REFUSED the cancel and the run is still live and still "
                           "mutating. Escalate — this is not a stopped run.",
    custody.CANCEL_CONTAINMENT_FAULT: "CONTAINMENT FAULT: the cancel could not be verified, so an "
                                      "overpowered mutating run MAY STILL BE LIVE. A durable "
                                      "incident was recorded and is surfaced as a critical health "
                                      "invariant until a terminal receipt clears it.",
}


def _delegate_cancel(ctx: ToolContext, run_id: str, reason: str = "") -> str:
    """Typed refusal: the delegated-run transport retired with its own modules (Seed 0).

    There is no control channel left to stop a run on — and nothing this build started can
    be mutating, because every start refuses. Reporting a cancel outcome over a transport
    that no longer exists would retire the operator's attention from exactly the case that
    needs it, so the verb answers with the refusal instead.
    """
    from ouroboros.subagents import CLAUDEXOR_RETIRED

    return _fail(
        "delegate_cancel", CLAUDEXOR_RETIRED,
        "There is nothing to stop: the owned Claudexor daemon and its gateway were retired "
        "and removed, so this build starts no delegated runs and holds no control channel. "
        "A run an OLDER build left running must be stopped out of band.",
        run_id=str(run_id or "").strip(),
    )


def get_tools() -> List[ToolEntry]:
    from ouroboros.config import get_task_abs_ceiling_sec

    return [
        ToolEntry("delegate_start", {
            "name": "delegate_start",
            "description": (
                "Start a delegated run on the owner's configured subscription harness and "
                "become its NANNY. Subscription execution is REQUESTED, so the usual case "
                "is no metered API money — but the actual spend is a fact of the finished "
                "run, not a promise of this call: it may come back zero, billed, "
                "estimated, or undisclosed (an expired session, a route that bills by "
                "construction, or an auth fallback all charge real money). Read the "
                "terminal `cost` block from delegate_wait before you treat this as free; "
                "it also costs time, quota and a worker slot. Your working root, "
                "access profile and route come from YOUR task authority; you cannot widen "
                "them, and there is no argument here that would let you try. If you hold "
                "a MUTATING shape the run executes in a PRIVATE SNAPSHOT of your write "
                "root — it never edits the shared tree in place. Its diff is captured at "
                "terminal (delegate_wait's workspace_capture block) and reaches your tree "
                "ONLY when you explicitly call integrate_delegated_patch(run_id=..., "
                "decision='apply'|'reject'); read the captured diff before applying, and "
                "never let the run commit inside its snapshot. If you are read-only it "
                "can only read and answer. "
                "A TOP-LEVEL task may instead select ONE exact installed user-managed "
                "skill payload with root='skill_payload' + bucket + skill_name: the "
                "selector chooses authority you already hold (it grants nothing), the "
                "run edits a private standalone snapshot of that payload, the LIVE "
                "payload stays byte-identical until you explicitly "
                "integrate_delegated_patch, and after an apply the skill's prior "
                "review is stale — run skill_preflight and skill_review as usual. "
                "The payload must already exist (create a NEW skill's manifest first). "
                "Seeded native stays system-repo territory; markerless native is logical external. "
                "Returns a run_id: watch it with delegate_wait, stop it with "
                "delegate_cancel. The run's output is a CLAIM you must check — you are the "
                "host, so verification receipts are still yours to write. If no route is "
                "configured or it is unavailable you get a typed refusal: choose an "
                "explicit configured alternative, wait, narrow, or report blocked. A direct "
                "fresh start requires subagent_id. In a configured session the host already STARTED the exact "
                "leaf before your first round (the startup receipt carries its run id): never start a duplicate — "
                "supervise it; a replacement delegate_start(prompt='') is legal only after verified cancellation/"
                "terminal settlement or a typed refusal proving no run exists. Recovery retries use retry_of without a new selector."
            ),
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                "prompt": {"type": "string", "description":
                    "Complete task for a direct start; for the configured snapshotted session (retry/"
                    "replacement), only optional advisory coordination context — the host supplies the canonical work order."},
                "subagent_id": {"type": "string", "description":
                    "Required for a fresh start made directly: exact agent_session actor id from Available "
                    "subagents. Omit for the current configured snapshotted route and for retry_of. API actor ids are refused here "
                    "and must be scheduled as recursive children."},
                "root": {"type": "string", "enum": ["skill_payload"], "description":
                    "Optional exact-resource selector: 'skill_payload' delegates ONE "
                    "installed user-managed skill payload you can already write. Omit "
                    "for ordinary workspace delegation."},
                "bucket": {"type": "string", "description":
                    "With root='skill_payload': the payload location "
                    "(external|clawhub|ouroboroshub|user_repo)."},
                "skill_name": {"type": "string", "description":
                    "With root='skill_payload': the exact skill name."},
                "max_seconds": {"type": "integer", "description":
                    "Wall-clock cap for the run; narrowed to your own remaining deadline. "
                    "Harness runs routinely need 3-5+ minutes end to end, so do not set a "
                    "tight cap for what feels like a quick edit. While delegate_wait shows "
                    "an advancing cursor the run is WORKING, and it enforces this cap "
                    "itself — cancelling a progressing run discards the whole run's spend."},
                "retry_of": {"type": "string", "description":
                    "EXPLICIT retry token: the pending_invocation_id from a start whose "
                    "outcome was unknown (transport failure, lost response). Replays THAT "
                    "invocation byte-identically under its original key, so the engine "
                    "returns the run it already accepted instead of starting a second one. "
                    "Omit subagent_id on this recovery path; supplying both selectors is a "
                    "typed conflict. "
                    "Never set it for an intended new run — a plain call always starts a "
                    "NEW invocation, even with an identical prompt."},
                },
            },
        }, _delegate_start_entry,
           timeout_sec=120),
        ToolEntry("delegate_wait", {
            "name": "delegate_wait",
            "description": (
                "Sleep on a delegated run until a meaningful event. Quiet transport windows "
                "are renewed by the host with zero model calls; journal progress still streams "
                "to the human but does not wake you. Terminal settlement, a new interaction, "
                "fault, addressed owner/task message, a direct-child attention/terminal event, "
                "cancel/deadline control, recovery judgment, or an explicit one-shot checkpoint "
                "wakes exactly once. A run that asks its "
                "user a question returns IMMEDIATELY as status='waiting_on_user' with "
                "the full question set (interaction/question ids ride WHOLE, never "
                "truncated): answer it with delegate_answer, or raise it with the "
                "escalate verb (parent-first) and keep waiting (a question with a "
                "timeout_at benign-declines "
                "at the engine timeout; timeout_at=null waits until answered). A "
                "large terminal result is delivered as a bounded preview plus an "
                "artifact: read output_delivery and finish reading the artifact before "
                "you rely on it."
            ),
            "parameters": {"type": "object", "required": ["run_id"], "properties": {
                "run_id": {"type": "string", "description": "Run id from delegate_start."},
                "since_seq": {"type": "integer", "description": "Event cursor: advances past it are recorded as progress."},
                "checkpoint_after_sec": {"type": "integer", "description":
                    "Optional one-shot future inspection time. Requires checkpoint_reason; "
                    "a real earlier wake consumes it."},
                "checkpoint_reason": {"type": "string", "description":
                    "Why one proactive inspection is worth a model call. No repeating cadence."},
            }},
        }, _delegate_wait_entry, timeout_sec=get_task_abs_ceiling_sec() + 120),
        ToolEntry("delegate_cancel", {
            "name": "delegate_cancel",
            "description": (
                "Cancel a delegated run. Claudexor keeps partial artifacts, but a cancelled "
                "session has no verdict and no finished work product — cancel a stuck or "
                "misdirected run, never one you merely want to hurry. The result is typed: "
                "only `confirmed` means a verified terminal receipt; `requested`, `failed` "
                "and `containment_fault_run_may_still_be_live` all mean it may still be running."
            ),
            "parameters": {"type": "object", "required": ["run_id"], "properties": {
                "run_id": {"type": "string", "description": "Run id from delegate_start."},
                "reason": {"type": "string", "description": "Why you are stopping it."},
            }},
        }, lambda ctx, run_id, reason="": _delegate_cancel(ctx, run_id, reason), timeout_sec=120),
        ToolEntry("delegate_answer", {
            "name": "delegate_answer",
            "description": (
                "Answer a delegated run's pending interactive question — the "
                "status='waiting_on_user' payload from delegate_wait names the "
                "interaction_id and its questions. Only the task that started the run "
                "may answer. Policy: answer from the task context you already hold; a "
                "question ABOVE your authority (spending money, changing scope, "
                "external actions) is not yours to guess — escalate it with the "
                "escalate verb (parent-first; the reply reaches your mailbox on a "
                "later round and you relay it back here) and keep waiting; an "
                "unanswered question with a "
                "timeout_at benign-declines at the engine timeout (the run continues "
                "on stated assumptions), while timeout_at=null waits until answered. "
                "Typed outcomes: delivered; already_resolved (the run moved on — do "
                "not re-post); not_found; rejected (a definite engine refusal of "
                "these rows — HTTP 400/409/413/422 only — fix them); "
                "subscription_window_exhausted (a distinct outcome carrying reset_at "
                "— the answer did NOT land; retry the SAME answers after reset_at); "
                "delivery_unknown "
                "(transport died mid-answer — re-check with delegate_wait and NEVER "
                "post a different answer for the same interaction). Codex-lane runs "
                "have no mid-run questions: a run that ENDS needing input "
                "(outcome_facts.reason=input_required) is answered with a plain NEW "
                "delegate_start(subagent_id=..., prompt=...) whose prompt carries the "
                "assignment plus the answers "
                "— there is no rerun/decision verb, and custody stays with you."
                " For an over-budget work order, pass the host-verified "
                "source_response envelope alongside the ordinary answer; the host "
                "checks its exact canonical range before recording coverage."
            ),
            "parameters": {"type": "object",
                           "required": ["run_id", "interaction_id", "answers"],
                           "properties": {
                "run_id": {"type": "string", "description": "Run id from delegate_start."},
                "interaction_id": {"type": "string", "description":
                    "The interaction being answered, from the waiting_on_user payload."},
                "answers": {"type": "array", "items": {"type": "object", "properties": {
                    "question_id": {"type": "string", "description":
                        "The question's id from the waiting_on_user payload."},
                    "selected_labels": {"type": "array", "items": {"type": "string"},
                                        "description": "Labels of the chosen option(s)."},
                    "free_text": {"type": "string", "description":
                        "Free-text answer; omit when options were selected."},
                }, "required": ["question_id"]}, "description":
                    "One row per question you are answering."},
                "source_response": {"type": "object", "description":
                    "Only for a partial work-order run: exact canonical source range "
                    "receipt. The host verifies schema=1, kind=source_response, the "
                    "full brief SHA, canonical selector, and text at start_char:end_char "
                    "before delivering it."},
            }},
        }, lambda ctx, run_id, interaction_id, answers, source_response=None: _delegate_answer(
            ctx, run_id, interaction_id, answers, source_response), timeout_sec=120),
    ]


__all__ = ["get_tools"]
