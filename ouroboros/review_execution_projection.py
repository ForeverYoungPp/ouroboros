"""Tiny public execution receipts projected from review actor usage.

This leaf owns the cross-surface presentation wire. It deliberately imports no
review engine: callers pass returned actor usage, and only actual receipt facts
can become an API or harness execution badge.
"""

from __future__ import annotations

from typing import Any, Dict, List


_API_EXECUTION_RECEIPT_KEYS = frozenset({
    "resolved_model", "provider", "prompt_tokens", "completion_tokens",
    "cached_tokens", "cache_write_tokens", "cost", "total_cost",
    "ledger_attempt_ids",
})


def normalize_review_executions(value: Any) -> List[Dict[str, str]]:
    """Allowlist the tiny public execution wire and deduplicate it stably."""
    out: List[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"api", "harness"}:
            continue
        harness_id = str(item.get("harness_id") or "").strip() if kind == "harness" else ""
        model = str(item.get("model") or "").strip()
        identity = (kind, harness_id, model)
        if identity in seen:
            continue
        seen.add(identity)
        row = {"kind": kind}
        if harness_id:
            row["harness_id"] = harness_id
        if model:
            row["model"] = model
        out.append(row)
    return out


def review_executions_from_actor_usage(actors: Any) -> List[Dict[str, str]]:
    """Project only executions proved by returned per-actor usage receipts."""
    executions: List[Dict[str, str]] = []
    for actor in actors if isinstance(actors, list) else []:
        if not isinstance(actor, dict):
            continue
        usage = actor.get("usage") if isinstance(actor.get("usage"), dict) else {}
        delegated_route = str(usage.get("delegated_route") or "").strip()
        model = str(usage.get("resolved_model") or "").strip()
        if delegated_route:
            executions.append({
                "kind": "harness", "harness_id": delegated_route,
                **({"model": model} if model else {}),
            })
        elif any(key in usage for key in _API_EXECUTION_RECEIPT_KEYS):
            executions.append({"kind": "api", **({"model": model} if model else {})})
    return normalize_review_executions(executions)


__all__ = ["normalize_review_executions", "review_executions_from_actor_usage"]
