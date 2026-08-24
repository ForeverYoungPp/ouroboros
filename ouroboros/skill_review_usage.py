"""Read-only canonical usage projection for one exact Skill Review wave."""

from __future__ import annotations

import pathlib
from typing import Any, Dict

from ouroboros._usage_rows import _skill_review_usage_bucket
from ouroboros._usage_rows_memo import _render_cached
from ouroboros.usage_ledger import _drive_root


def skill_review_usage_markdown(
    usage: Dict[str, Any], *, coverage_known: bool, expected: int, recorded: int,
) -> str:
    """Render the canonical per-wave attempt projection without creating totals."""
    def value(name: str) -> str:
        item = usage.get(name)
        return "unknown" if item is None else str(item)

    attempt_ids = [str(item) for item in (usage.get("attempt_ids") or []) if str(item)]
    coverage_complete = coverage_known and recorded >= expected
    cash_label = "Cash" if coverage_complete else "Recorded-row cash"
    lines = [
        "### Review accounting",
        "",
        "- Canonical attempt IDs: " + (", ".join(attempt_ids) or "none recorded"),
        (f"- {cash_label}: settled ${{settled_usd:.6f}}; confirmed ${{confirmed_usd:.6f}}; "
         "estimated ${estimated_usd:.6f}; unresolved upper bound "
         "${unresolved_upper_bound_usd:.6f}.").format(**usage),
        (f"- Calls: API physical={int(usage.get('physical_calls') or 0)}; "
         f"subscription sessions={int(usage.get('subscription_sessions') or 0)}."),
        (f"- Tokens: prompt={value('prompt_tokens')}; completion={value('completion_tokens')}; "
         f"cached={value('cached_tokens')}."),
        (f"- Finality: unknown/unmetered={int(usage.get('unknown_unmetered') or 0)}; "
         f"non-final rows={int(usage.get('non_final_rows') or 0)}; "
         f"ledger integrity={'degraded' if usage.get('integrity_degraded') else 'verified'}; "
         f"slot attribution={'complete' if usage.get('attribution_complete') else 'incomplete'}."),
    ]
    if coverage_complete:
        lines.append(f"- Wave attempt coverage: complete ({recorded}/{expected} recorded).")
    else:
        coverage = f"incomplete ({recorded}/{expected} recorded)" if coverage_known else "unknown"
        lines.append(
            f"- Wave attempt coverage: {coverage}; whole-wave cash and finality are unavailable."
        )
    for slot_id, bucket in (usage.get("by_slot") or {}).items():
        lines.append(
            f"- Slot {slot_id}: API physical={int(bucket.get('physical_calls') or 0)}, "
            f"subscription sessions={int(bucket.get('subscription_sessions') or 0)}, "
            f"settled=${float(bucket.get('settled_usd') or 0):.6f}."
        )
    windows = usage.get("subscription_windows") or {}
    if windows:
        lines.append("- Subscription windows: " + ", ".join(
            f"{route} resets {reset_at}" for route, reset_at in sorted(windows.items())
        ) + ".")
    for attempt in usage.get("attempts") or []:
        cost = attempt.get("cost_usd")
        cost_text = "unknown" if cost is None else f"${float(cost):.6f}"
        route = attempt.get("subscription_route") or attempt.get("provider") or "unknown"
        lines.append(
            f"- `{attempt.get('attempt_id')}`: slot={attempt.get('review_slot_id') or 'unattributed'}, "
            f"kind={attempt.get('kind') or 'attempt'}, state={attempt.get('state') or 'unknown'}, "
            f"model={attempt.get('model') or 'unknown'}, route={route}, "
            f"profile={attempt.get('credential_profile_id') or 'automatic/undisclosed'}, "
            f"access={attempt.get('access_profile') or 'undisclosed'}, cash={cost_text}."
        )
    return "\n".join(lines)


def skill_review_attempt_coverage(
    record: Dict[str, Any], usage: Dict[str, Any],
) -> tuple[bool, int, int]:
    """Compare exact terminal actor slots with canonical physical-attempt slots."""
    expected: Dict[str, int] = {}
    actors = [item for item in (record.get("raw_actor_records") or []) if isinstance(item, dict)]
    for actor in actors:
        slot_id = str(actor.get("slot_id") or "")
        if not slot_id:
            return False, 0, 0
        expected[slot_id] = expected.get(slot_id, 0) + 1
    if not expected:
        return False, 0, 0
    observed: Dict[str, int] = {}
    for attempt in usage.get("attempts") or []:
        # The light-model extraction is a real paid attempt and stays in all
        # monetary/token totals, but it canonicalizes a session answer; it is
        # not the reviewer transport whose late settlement this coverage proves.
        if str(attempt.get("source") or "") == "review_substrate.extraction":
            continue
        slot_id = str(attempt.get("review_slot_id") or "")
        if slot_id:
            observed[slot_id] = observed.get(slot_id, 0) + 1
    return True, sum(expected.values()), sum(
        min(count, observed.get(slot_id, 0)) for slot_id, count in expected.items()
    )


def skill_review_usage(
    drive_root: pathlib.Path | str | None = None, *, review_skill: str,
    review_wave_id: str,
) -> Dict[str, Any]:
    """Return exact final physical attempts attributed to one skill/wave."""
    root = _drive_root(drive_root)
    skill, wave = str(review_skill or ""), str(review_wave_id or "")
    cache_key = ("skill_review_usage", skill, wave, None, True)

    def render(final: list, integrity_degraded: bool) -> Dict[str, Any]:
        return _skill_review_usage_bucket(
            final, review_skill=skill, review_wave_id=wave,
            integrity_degraded=integrity_degraded,
        )

    return _render_cached(root, cache_key, render)


__all__ = [
    "skill_review_attempt_coverage",
    "skill_review_usage",
    "skill_review_usage_markdown",
]
