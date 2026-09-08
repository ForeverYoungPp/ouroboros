"""Consecutive-empty-round guard (audited 5-item #3).

A model stuck producing turns with no tool calls, no visible content, no
reviewable effects, and no FINAL ANSWER marker freezes ~146K tokens/round
(one task burned 40.1% of its budget across 6 such rounds). The A3 no-op
nudge is one-shot, so it cannot re-fire; this guard re-injects a mechanical
reminder on EVERY consecutive empty round, escalating after the threshold.

The guard lives in its own module so the main loop's byte debt (a shrink-only
ratchet) does not grow: ``ouroboros/loop.py`` keeps a single short call site.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ouroboros.outcomes import extract_final_answer

EMPTY_ROUND_ESCALATION_THRESHOLD = 2


def _visible_round_text(content: Any) -> str:
    """The round's visible assistant text as plain string (mirror of the loop's
    helper; reasoning blocks are not visible answer text)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out: List[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if str(b.get("type") or "") in ("thinking", "reasoning", "redacted_thinking") or b.get("thought") is True:
                continue
            txt = b.get("text")
            if isinstance(txt, str):
                out.append(txt)
        return "".join(out).strip()
    return ""


def round_is_answer_empty(content: Any) -> bool:
    """Round-scoped emptiness for the no-tool finalization path (audited
    5-item #3). The caller gates this on ``not tool_calls``, so the CURRENT
    round by construction produced no tool calls and no new reviewable
    effects (effects derive solely from ``llm_trace["tool_calls"]``). Only
    content decides: visible text vs a FINAL ANSWER marker. The run-wide
    trace must NOT be consulted — it is cumulative, so prior tool work would
    permanently silence the guard (review finding C1)."""
    return not (_visible_round_text(content) or extract_final_answer(str(content or "")))


def empty_round_count(usage: Dict[str, Any]) -> int:
    try:
        return int(usage.get("_consecutive_empty_rounds") or 0)
    except (TypeError, ValueError):
        return 0


def note_empty_round(usage: Dict[str, Any], round_idx: int) -> int:
    """Increment the consecutive-empty-round counter, self-healing on
    discontinuity: if a non-empty round happened between the last empty round
    and this one (round_idx is not last+1), reset first — so callers never need
    an explicit reset on activity paths."""
    # free_redial re-enters the same round_idx (loop.py), so a re-entrant
    # empty round resets to 1 — the count stays pinned at 1 across redials.
    # Benign: every empty round still nudges, the count is never overstated.
    if int(usage.get("_last_empty_round_idx") or -1) != round_idx - 1:
        usage["_consecutive_empty_rounds"] = 0
    usage["_consecutive_empty_rounds"] = empty_round_count(usage) + 1
    usage["_last_empty_round_idx"] = round_idx
    return usage["_consecutive_empty_rounds"]


def reset_empty_round_count(usage: Dict[str, Any]) -> None:
    usage["_consecutive_empty_rounds"] = 0
    usage.pop("_last_empty_round_idx", None)


def maybe_inject_empty_round_reminder(
    limit_ctx: Any,
    llm_trace: Dict[str, Any],
    content: Any,
    messages: List[Dict[str, Any]],
    emit_progress: Callable[[str], None],
    *,
    append_or_merge_user_message: Callable[[List[Dict[str, Any]], str], None],
) -> bool:
    """Re-firing empty-round reminder (audited 5-item #3).

    Runs in the no-tool finalization path AFTER every meaningful gate has had
    its chance (delivery control, swarm, handoff, absorption, skill
    finalization, task-service finalization, A3/verify/final-marker nudges).
    It runs after task-service finalization so an empty round can never defer
    or rush past pending service evidence (review finding C1 follow-up).
    Returns True when a reminder was injected (the caller continues the loop —
    never forces a terminal). The counter is task-local on the limit context's
    accumulated_usage and resets on any real activity.
    """
    if not round_is_answer_empty(content):
        reset_empty_round_count(limit_ctx.accumulated_usage)
        return False
    count = note_empty_round(limit_ctx.accumulated_usage, limit_ctx.round_idx)
    if content and str(content).strip():
        messages.append({"role": "assistant", "content": content})
    if count >= EMPTY_ROUND_ESCALATION_THRESHOLD:
        text = (
            "[SYSTEM REMINDER]\nThe last "
            f"{count} consecutive rounds produced NO tool call, NO visible answer, and NO "
            "reviewable effect. This task is burning budget on empty turns. Do one of these "
            "NOW: (1) call a tool that makes real progress, or (2) finalize with your best "
            "current answer (or an explicit blocker statement). Do not produce another empty round."
        )
        emit_progress("Escalated empty-round reminder injected.")
    else:
        text = (
            "[SYSTEM REMINDER]\nThe previous round was empty: no tool call, no visible answer, "
            "no reviewable effect. If you are blocked, state the blocker and the evidence; "
            "otherwise call a tool that makes progress or finalize. Another empty round will "
            "trigger an escalated reminder."
        )
        emit_progress("Empty-round reminder injected.")
    append_or_merge_user_message(messages, text)
    llm_trace["reasoning_notes"].append("Empty-round reminder injected after service finalization.")
    return True
