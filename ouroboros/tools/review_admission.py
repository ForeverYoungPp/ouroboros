"""Pre-dispatch review admission (Q25=A / Q28=A).

Every commit-gate packet — the triad api pack and each scope row's pack — is
ASSEMBLED AND FIT-CHECKED before any reviewer is dispatched, so a
deterministic assembly failure on one side can never spend money on the other
(previously triad and scope dispatched concurrently). A universal reorder with
zero verdict change: the same assembly code runs, the same results come out —
only the ordering moves the spend after the last deterministic gate.

Q28-A oversized outcomes: packet limits gate only the api rows. A panel whose
agent-session rows alone satisfy the quorum proceeds without the api rows
(recorded, never silent); a panel that cannot reach quorum without them gets a
typed ZERO-SPEND terminal, and for the managed resolver that refusal carries
the settings guidance below (the resolver's terminal contract already explains
rollback + retry).

``prepare_scope_review`` is the assembly half of ``run_scope_review`` — moved
here whole; the dispatch half stays in ``scope_review``. Internals are reached
through the module object (``_scope().name``) so test monkeypatching of
``scope_review`` attributes keeps working.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

# A managed resolution stages the whole two-parent merge tree by contract: the
# ordinary "split the commit" remedy is structurally impossible for it, so every
# managed oversize terminal REPLACES that clause with these two sentences.
MANAGED_SPLIT_IMPOSSIBLE = (
    "A managed-update resolution stages the whole two-parent merge tree and "
    "cannot be split into smaller commits."
)
MANAGED_OVERSIZE_GUIDANCE = (
    "Switch or add agent-route reviewer rows in "
    "Settings → Agents → Review lanes (packet limits do not apply to them), "
    "or configure larger-window models."
)

# Pack-SIZE terminal statuses (and only those): the Q28-A session-quorum
# override applies to packet limits, never to fail-closed integrity statuses
# (omitted/unreadable touched context, infra errors). Measured set: the
# assembly ladder's sub-floor pack arrives as `sub_floor` and its irreducible
# overflow as `fixed_overflow`; no prepare-time ScopeReviewResult carries a
# `budget_exceeded` status (that name exists only on the TouchedContextStatus
# sentinel, which _handle_prompt_signals translates to `sub_floor`).
#
# Q28-A yield scope (deliberate, admission-time ONLY): the session-quorum
# override applies where these statuses are produced — packet ASSEMBLY, before
# any dispatch. A DISPATCH-TIME oversize (the provider's own tokenizer
# rejecting an assembled prompt, scope_review status `fixed_overflow` from the
# transport) still fail-closed blocks: whether that late refusal should also
# yield to a session quorum is an owner-level consistency question, escalated
# separately — no semantics are changed here.
SCOPE_FIT_BLOCK_STATUSES = frozenset({"sub_floor", "fixed_overflow"})


def _scope():
    from ouroboros.tools import scope_review

    return scope_review


def fit_triad_prompt(api_models: list, assemble, current_files_section: str,
                     diff_text: str, changed: str, target_repo, ctx=None,
                     subject=None) -> tuple:
    """The api pack's guaranteed-fit ladder (P3 one-pass): drop only evidence
    duplicated by the complete staged diff — full snapshots first, then unchanged
    diff context. Each api slot's limit uses its REAL window from Capability
    Evidence (a hardcoded 1M treated a 200K reviewer as 1M-capable and lost its
    whole review to a deterministic prompt-too-long 400), with sub-1M windows
    scaling their reserves so a small-window slot gets a fit-sized pack, not a
    zero limit; the shared prompt is sized to the review QUORUM — the same SSOT
    plan review uses — so one small slot degrades its OWN seat rather than
    blocking the gate for the whole panel. Session rows are not constrained by
    this pack at all (5.2/5.7): they retrieve with their own tools. Returns
    ``(prompt, stable_prefix_len, block_message_or_empty)``."""
    # Resolved through the review-module namespace on purpose: these names are
    # documented monkeypatch seams pinned by the fit-ladder tests.
    from ouroboros.tools import review as _rv

    def _slot_input_limit(slot_model: str) -> int:
        window = _rv.reviewer_context_window(slot_model)
        output_reserve, tokenizer_margin = _rv.window_scaled_reserves(
            window,
            output_reserve=_rv._review_output_budget(),
            tokenizer_margin=50_000,
        )
        return max(0, _rv.calibrated_input_token_limit(
            slot_model,
            context_window=window,
            output_reserve=output_reserve,
            tokenizer_margin=tokenizer_margin,
            budget_cap=_rv.REVIEW_PROMPT_TOKEN_BUDGET,
        ))

    estimate_tokens = _rv.estimate_tokens
    input_limit = _rv._quorum_input_token_limit(
        api_models, {m: _slot_input_limit(m) for m in api_models})
    prompt, stable_prefix_len = assemble(current_files_section, diff_text)
    if input_limit and estimate_tokens(prompt) > input_limit:
        touched_paths = [line.strip() for line in changed.splitlines() if line.strip()]
        fit_note = (
            "TRIAD FIT NOTE: Full post-change snapshots were omitted because they "
            "duplicate the complete staged diff and would exceed the strictest "
            "configured reviewer's input limit. Every touched path is listed below; "
            "all added/deleted lines remain in the staged diff.\n\n"
            + ("\n".join(f"- {path}" for path in touched_paths) or "(no paths reported)")
        )
        prompt, stable_prefix_len = assemble(fit_note, diff_text)
        if input_limit and estimate_tokens(prompt) > input_limit:
            from ouroboros.tools.review_binary_context import StagedDiffUnavailable
            from ouroboros.tools.review_subject import capture_review_diff
            try:  # the SAME hardened capture as the primary diff, at zero context
                # A managed subject re-renders ITS OWN pinned trees at -U0: the
                # rung stays bound to the exact subject already under review
                # instead of re-serializing a fresh candidate.
                compact_diff = (
                    subject.render_prompt_diff(unified=0) if subject is not None
                    else capture_review_diff(ctx, target_repo, unified=0)
                )
            except StagedDiffUnavailable:
                compact_diff = ""  # keep the hardened full diff; the gate below blocks if it still overflows
            if compact_diff.strip():
                prompt, stable_prefix_len = assemble(fit_note, compact_diff)
    prompt_tokens = estimate_tokens(prompt)
    if not input_limit or prompt_tokens > input_limit:
        # The split imperative is structurally impossible for a managed
        # resolution — its terminal REPLACES the clause (never appends the
        # managed guidance under a false imperative).
        remedy = (
            f"{MANAGED_SPLIT_IMPOSSIBLE} {MANAGED_OVERSIZE_GUIDANCE} "
            "Reviewer models and evidence authority were not degraded."
            if subject is not None
            else "Split or shrink the staged change; "
            "reviewer models and evidence authority were not degraded."
        )
        return prompt, stable_prefix_len, (
            "⚠️ REVIEW_BLOCKED: The irreducible one-pass triad prompt does not "
            f"fit every configured reviewer ({prompt_tokens:,} estimated input "
            f"tokens; limit {input_limit:,}). {remedy}"
        )
    return prompt, stable_prefix_len, ""


def triad_not_dispatched_records(
    row_plan: dict, reason: str, *, only_api: bool = False
) -> list:
    """Typed $0 ``not_dispatched`` actor records for prepared-but-withheld triad
    seats, in the durable ``ReviewActorRecord.to_dict()`` shape.

    Durable review status must show WHICH configured seats were withheld and
    why — a bare degraded-reason string loses the seat identities. ``only_api``
    restricts the records to the api rows (the Q28-A oversize drop); the
    default covers every row (the Q25-A admission block). ``slot`` keeps each
    seat's ORIGINAL 1-based position in the configured plan."""
    from ouroboros.review_execution import ReviewRouteKind

    models = list(row_plan.get("models") or [])
    routes = list(row_plan.get("routes") or [])
    slot_ids = list(row_plan.get("slot_ids") or [])
    actors = list(row_plan.get("subagent_ids") or [])
    records = []
    for index, model in enumerate(models):
        if only_api and (
            index >= len(routes) or routes[index] is not ReviewRouteKind.API_CHAT
            # A configured-subagent api row retrieves; the packet drop is not
            # its withholding and it keeps its live seat.
            or (index < len(actors) and actors[index])
        ):
            continue
        records.append({
            "model_id": str(model),
            "status": "not_dispatched",
            "raw_text": str(reason),
            "parsed_items": [],
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "slot": index + 1,
            "slot_id": str(slot_ids[index]) if index < len(slot_ids) else "",
            "prompt_ref": {},
            "response_ref": {},
            "operation_id": "",
            "operation_state": "not_dispatched",
            "late_result_pending": False,
        })
    return records


def drop_api_rows(row_plan: dict) -> dict:
    """Filter every aligned triad row vector down to the agent-session rows.

    Q28-A: an irreducible oversize packet drops the api subset when the session
    rows alone satisfy the quorum. The caller records the drop loudly."""
    from ouroboros.review_execution import ReviewRouteKind

    routes = list(row_plan.get("routes") or [])
    actors = list(row_plan.get("subagent_ids") or [])
    # The RETRIEVES class survives the drop: a configured-subagent api row never
    # received the oversized packet, so packet overflow is not its failure.
    keep = [
        i for i, r in enumerate(routes)
        if r is not ReviewRouteKind.API_CHAT or (i < len(actors) and actors[i])
    ]
    filtered = dict(row_plan)
    for key in ("models", "routes", "efforts", "session_targets",
                "session_profiles", "slot_ids", "subagent_ids"):
        rows = list(row_plan.get(key) or [])
        filtered[key] = [rows[i] for i in keep if i < len(rows)]
    return filtered


def prepare_scope_review(
    ctx: Any,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
    scope_review_history: Optional[list] = None,
    scope_model: Optional[str] = None,
    slot_id: str = "",
    route: Any = None,
    slot_effort: str = "",
    session_target: str = "",
    session_profile: str = "",
    subagent_id: str = "",
) -> Tuple[Optional[dict], Optional[Any]]:
    """Assemble ONE scope row's packet without dispatching anything.

    Returns ``(prepared, final)`` — exactly one is non-None. ``final`` is a
    complete ScopeReviewResult (deterministic early exit: low-context skip,
    invalid roots, context-build failure, pack signals); ``prepared`` carries
    everything the dispatch half needs, including the context-manifest and
    stable-prefix values (ContextVars do not cross threads, so they are
    captured here and re-seeded at dispatch).
    """
    sr = _scope()
    if sr._scope_review_skipped_in_low_context():
        return None, sr._low_context_skip_result(scope_model or sr._get_scope_model())
    try:
        governance_repo, repo_dir = sr.review_repo_dirs_for(ctx)
    except (TypeError, ValueError) as exc:
        return None, sr.ScopeReviewResult(
            blocked=True,
            status="error",
            block_message=f"⚠️ SCOPE_REVIEW_BLOCKED: invalid review roots: {exc}.",
        )
    scope_model_id = scope_model or sr._get_scope_model()
    delegated = str(getattr(route, "value", route) or "") == "agent_session"
    # RETRIEVES class: a session row and a configured-subagent api row deliver
    # by retrieval — neither assembles the packet/atlas below.
    retrieves = delegated or bool(subagent_id)

    from ouroboros.tools.review_binary_context import StagedDiffUnavailable
    from ouroboros.tools.review_subject import managed_review_subject

    try:
        subject = managed_review_subject(ctx, repo_dir)
        if retrieves:
            # Session delivery (5.2): same task/checklist/contract, no assembled
            # pack — the session retrieves with its own tools in the repo root.
            # For a managed resolution the authoritative delta is inlined.
            from ouroboros.tools.scope_review_session import ScopeIntentContext as _Intent
            from ouroboros.tools.scope_review_session import build_scope_session_task

            session_task, session_manifest = build_scope_session_task(
                repo_dir, commit_message,
                _Intent(goal=goal, scope=scope, review_rebuttal=review_rebuttal,
                        review_history=review_history,
                        scope_review_history=scope_review_history),
                drive_root=pathlib.Path(ctx.drive_root) if getattr(ctx, "drive_root", None) else None,
                governance_repo_dir=governance_repo,
                managed_subject=subject,
            )
            sr._SCOPE_CONTEXT_MANIFEST.set(session_manifest)
            prompt, context_status = session_task, None
        else:
            session_task = ""
            prompt, context_status = sr._build_scope_prompt(
                repo_dir, commit_message,
                goal=goal, scope=scope,
                review_rebuttal=review_rebuttal,
                review_history=review_history,
                scope_review_history=scope_review_history,
                context=sr._ScopePromptContext(
                    drive_root=(
                        pathlib.Path(ctx.drive_root)
                        if getattr(ctx, "drive_root", None)
                        else None
                    ),
                    scope_model=scope_model_id,
                    governance_repo_dir=governance_repo,
                    represent_binary=subject is not None,
                    managed_subject=subject,
                ),
            )
    except (RuntimeError, StagedDiffUnavailable) as exc:
        return None, sr.ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context — commit blocked.\n"
                f"Error: {exc}\n"
                "Ensure git is available and the repository is in a valid state."
            ),
            model_id=scope_model_id,
            status="error",
            context_manifest=sr._current_scope_context_manifest(),
        )

    # Pack-budget signals belong to an ASSEMBLED pack: a session assembles none, so its
    # context_status is None and this returns None by construction — no route branch.
    signal_result = sr._handle_prompt_signals(
        prompt, context_status, scope_model=scope_model_id,
        input_limit=sr._effective_scope_input_limit(scope_model=scope_model_id),
        managed=subject is not None,
    )
    if signal_result is not None:
        # Keep _handle_prompt_signals as the status SSOT for early exits.
        signal_result.model_id = scope_model_id
        signal_result.context_manifest = sr._current_scope_context_manifest()
        if (
            signal_result.blocked
            and str(getattr(signal_result, "status", "")) in SCOPE_FIT_BLOCK_STATUSES
            and subject is not None
            and MANAGED_OVERSIZE_GUIDANCE not in str(signal_result.block_message or "")
        ):
            # A fit terminal whose message carries no remedy at all (sub_floor)
            # still gets the managed guidance; a message that already carries
            # the managed remedy (ladder_terminal_cause, managed=True) is left
            # alone — never a duplicate, never an append under a split clause.
            signal_result.block_message = (
                f"{signal_result.block_message}\n"
                f"{MANAGED_SPLIT_IMPOSSIBLE} {MANAGED_OVERSIZE_GUIDANCE}"
            )
        return None, signal_result

    return {
        "prompt": prompt,
        "session_task": session_task,
        "repo_dir": repo_dir,
        "scope_model_id": scope_model_id,
        "delegated": delegated,
        "slot_id": slot_id,
        "route": route,
        "slot_effort": slot_effort,
        "session_target": session_target,
        "session_profile": session_profile,
        "subagent_id": subagent_id,
        "context_manifest": sr._current_scope_context_manifest(),
        "stable_prefix_len": int(sr._SCOPE_STABLE_PREFIX_LEN.get() or 0),
    }, None
