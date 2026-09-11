"""Conservative conflict handling over Engram's own endpoints (C4 / AC18).

BIBLE.md P1 forbids an unattended process from silently rewriting existing memory.
Engram exposes a real conflict loop — ``GET /conflicts``, ``POST /conflicts/compare``,
``POST /conflicts/judge``, ``POST /conflicts/scan`` — so there is no reason to invent
a comparison here, and equally no reason to let one run destructively.

The policy is deliberately narrow, and narrow in the direction that cannot lose
anything:

* A verdict is only ever **recorded**, never applied. This module has no delete
  path and never emits ``supersedes``, so both sides of a conflict always survive
  (AC18b holds by construction rather than by discipline).
* Only ``not_conflict`` lands without a human. It is the one verdict that cannot
  rewrite either record — it asserts the two are about different things — and
  persisting it is what suppresses future candidate scans (AC18c).
* Every other verdict is a **finding to be surfaced**, not a licence to act. The
  caller gets ``requires_owner=True`` and is expected to escalate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

#: The only verdict unattended code may persist, and why it is safe to do so.
LANDABLE_RELATIONS = frozenset({"not_conflict"})
#: Verdicts that assert the records disagree or replace one another. Recorded by
#: Engram if asked, but never asked for here: an unattended actor must not decide
#: that one memory replaces another (P1).
DESTRUCTIVE_RELATIONS = frozenset({"supersedes", "conflicts_with"})
#: Verdicts that merely relate two records without judging either.
RELATIONAL_VERDICTS = frozenset({"related", "compatible", "scoped"})


@dataclass(frozen=True)
class VerdictDecision:
    """What the policy allows for one externally-judged verdict."""

    relation: str
    persist: bool
    requires_owner: bool
    reason: str


@dataclass(frozen=True)
class ConflictReceipt:
    """Outcome of one attempt to act on a verdict. Never an exception."""

    relation: str
    persisted: bool
    requires_owner: bool
    ok: bool
    reason: str = ""
    detail: str = ""
    sync_id: str = ""


def decide_verdict(relation: str) -> VerdictDecision:
    """The policy, as a pure function so it can be asserted without a store."""
    verb = str(relation or "").strip()
    if verb in LANDABLE_RELATIONS:
        return VerdictDecision(
            verb,
            persist=True,
            requires_owner=False,
            reason="not_conflict cannot rewrite either record, and recording it "
            "suppresses future candidate scans",
        )
    if verb in DESTRUCTIVE_RELATIONS:
        return VerdictDecision(
            verb,
            persist=False,
            requires_owner=True,
            reason=f"{verb!r} asserts one memory replaces or contradicts another; "
            "an unattended process must not decide that (BIBLE P1)",
        )
    if verb in RELATIONAL_VERDICTS:
        return VerdictDecision(
            verb,
            persist=False,
            requires_owner=True,
            reason=f"{verb!r} is a judgement about how two memories relate; only "
            "not_conflict may land unattended",
        )
    return VerdictDecision(
        verb,
        persist=False,
        requires_owner=True,
        reason=f"unknown relation {verb!r}; nothing was written",
    )


def persist_verdict(
    client: Any,
    *,
    memory_id_a: int,
    memory_id_b: int,
    relation: str,
    reasoning: str = "",
    confidence: float = 1.0,
    model: str = "",
) -> ConflictReceipt:
    """Record a verdict through ``POST /conflicts/compare``, policy permitting.

    ``reasoning`` is mandatory server-side, so a caller that supplies none gets a
    policy-generated one rather than a 400: the reason a verdict was recorded is
    itself part of the memory.
    """
    decision = decide_verdict(relation)
    if not decision.persist:
        return ConflictReceipt(
            relation=decision.relation,
            persisted=False,
            requires_owner=decision.requires_owner,
            ok=True,
            reason=decision.reason,
        )
    try:
        result = client.conflicts_compare(
            memory_id_a=int(memory_id_a),
            memory_id_b=int(memory_id_b),
            relation=decision.relation,
            confidence=float(confidence),
            reasoning=str(reasoning or "").strip() or "no_conflict recorded by Ouroboros",
            model=model,
        )
    except Exception as exc:
        return ConflictReceipt(
            decision.relation,
            persisted=False,
            requires_owner=False,
            ok=False,
            reason="config",
            detail=f"{type(exc).__name__}: {exc}",
        )
    if not result.ok:
        return ConflictReceipt(
            decision.relation,
            persisted=False,
            requires_owner=False,
            ok=False,
            reason=str(getattr(result, "error_kind", "") or "failed"),
            detail=str(getattr(result, "detail", ""))[:200],
        )
    payload = result.one() or {}
    receipt = ConflictReceipt(
        decision.relation,
        persisted=True,
        requires_owner=False,
        ok=True,
        sync_id=str(payload.get("sync_id") or ""),
    )
    # C4: only not_conflict may suppress future candidate scans. If the store ever
    # answered a not_conflict write without a relation id, the suppression did not
    # happen and the caller must not believe it did.
    if decision.relation == "not_conflict" and not receipt.sync_id:
        return ConflictReceipt(
            decision.relation,
            persisted=False,
            requires_owner=False,
            ok=False,
            reason="no_relation_id",
            detail="the store accepted the verdict but returned no sync_id",
        )
    return receipt


def adjudicate_pair(
    client: Any,
    pair: Dict[str, Any],
    *,
    relation: str,
    reasoning: str = "",
    confidence: float = 1.0,
    model: str = "",
) -> ConflictReceipt:
    """Convenience over one candidate pair as ``GET /conflicts`` / scan returns it."""
    row = pair if isinstance(pair, dict) else {}
    ids = _pair_ids(row)
    if ids is None:
        return ConflictReceipt(
            str(relation or ""),
            persisted=False,
            requires_owner=False,
            ok=False,
            reason="unusable_pair",
            detail=f"expected two integer ids, got {sorted(row)}",
        )
    return persist_verdict(
        client,
        memory_id_a=ids[0],
        memory_id_b=ids[1],
        relation=relation,
        reasoning=reasoning,
        confidence=confidence,
        model=model,
    )


def _pair_ids(row: Dict[str, Any]) -> Optional[tuple]:
    """Accept the shapes Engram uses for a relation/candidate row."""
    for left, right in (
        ("memory_id_a", "memory_id_b"),
        ("source_id", "target_id"),
        ("id_a", "id_b"),
        ("a", "b"),
    ):
        first, second = row.get(left), row.get(right)
        if _is_int(first) and _is_int(second):
            return int(first), int(second)
    return None


def _is_int(value: Any) -> bool:
    return str(value if value is not None else "").lstrip("-").isdigit()


def conflict_summary(client: Any, *, limit: int = 20, status: str = "") -> Dict[str, Any]:
    """Bounded read of recorded relations, typed when the store is unreachable.

    This is the read half that makes recording verdicts worth anything: a verdict
    nobody can read back is not a memory, it is a log line.
    """
    try:
        result = client.conflicts(limit=limit, status=status)
    except Exception as exc:
        return {"status": "unavailable", "count": 0, "rows": [], "detail": type(exc).__name__}
    if not result.ok:
        return {
            "status": "unavailable",
            "count": 0,
            "rows": [],
            "detail": str(getattr(result, "error_kind", "") or "failed"),
        }
    rows: List[Dict[str, Any]] = result.items()
    return {
        "status": "ok" if rows else "empty",
        "count": len(rows),
        "rows": [
            {
                "relation": str(row.get("relation") or ""),
                "judgment_status": str(row.get("judgment_status") or ""),
                "reason": str(row.get("reason") or "")[:200],
            }
            for row in rows
        ],
        "detail": "",
    }


__all__ = [
    "DESTRUCTIVE_RELATIONS",
    "LANDABLE_RELATIONS",
    "RELATIONAL_VERDICTS",
    "ConflictReceipt",
    "VerdictDecision",
    "adjudicate_pair",
    "conflict_summary",
    "decide_verdict",
    "persist_verdict",
]
