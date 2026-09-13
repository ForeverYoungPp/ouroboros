"""Append-only checkpoints for evolution progress and later eval curves."""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
from typing import Any, Dict, Optional

from ouroboros.outcomes import (
    EXECUTION_CANCELLED,
    EXECUTION_INFRA_FAILED,
    EXECUTION_INTERRUPTED,
    normalize_outcome_axes,
)
from ouroboros.utils import append_jsonl, utc_now_iso


CHECKPOINTS_REL = pathlib.Path("state") / "evolution_checkpoints.jsonl"

#: The genuinely-pending outcome, recorded while a commit awaits its restart.
PENDING_CYCLE_OUTCOME = "waiting_for_restart"

#: The fallback outcome for a cycle that carries no reviewed commit. It names the fact the
#: record actually proves — "no reviewed commit landed" — and deliberately not a conclusion
#: about intent, which is why it replaced the bare ``no_op`` the mint used to write.
UNCOMMITTED_CYCLE_OUTCOME = "uncommitted"
#: No commit AND the axes prove the cycle never reached its own terms.
INTERRUPTED_CYCLE_OUTCOME = "interrupted"
INFRA_FAILED_CYCLE_OUTCOME = "infra_failed"
#: The historical fallback, kept readable: rows already on disk still carry it, and a
#: projection re-derives what they support instead of echoing the word.
LEGACY_NO_OP_CYCLE_OUTCOME = "no_op"
#: The owner stopped the campaign while this cycle was in flight.
OWNER_STOPPED_CYCLE_OUTCOME = "owner_stopped"

#: Every durable cycle-outcome name — the CLOSURE of the mint. A new value is added here
#: first, and ``tests/test_cycle_outcome_truth.py`` pins the mint against this set by AST
#: scan, so a name cannot be written without answering where it belongs.
CYCLE_OUTCOME_VOCABULARY = frozenset({
    "absorbed",
    "abandoned",
    OWNER_STOPPED_CYCLE_OUTCOME,
    PENDING_CYCLE_OUTCOME,
    LEGACY_NO_OP_CYCLE_OUTCOME,
    UNCOMMITTED_CYCLE_OUTCOME,
    INTERRUPTED_CYCLE_OUTCOME,
    INFRA_FAILED_CYCLE_OUTCOME,
})

#: The subset meaning "this objective's attempt is SPENT — stop feeding it back as fresh
#: work". A consumer that lists attempted objectives reads THIS, never a literal of its own:
#: a literal is correct only until one side gains a value, at which point it silently skips
#: the newcomer and drops its objective out of the anti-repeat guard entirely (which is
#: exactly what ``post_task_evolution`` did with its own three-name set).
#: Two vocabulary members are deliberately absent, each for its own reason:
#: ``waiting_for_restart`` because an unverified commit leaves the transaction open, so that
#: objective is neither spent nor re-proposable yet; and ``owner_stopped`` because the
#: owner's stop is a verdict about the CAMPAIGN, not about the objective — widening the
#: anti-repeat guard is not a side effect an honesty fix gets to have.
SPENT_CYCLE_OUTCOMES = CYCLE_OUTCOME_VOCABULARY - {
    PENDING_CYCLE_OUTCOME,
    OWNER_STOPPED_CYCLE_OUTCOME,
}


def uncommitted_cycle_outcome(axes: Dict[str, Any] | None) -> str:
    """Name WHY a cycle carries no reviewed commit, from the axes it already holds.

    The mint used to derive this from one fact — the agent's ``commit_sha`` is empty — and
    write ``no_op``: a claim about INTENT minted from evidence about AUTHORSHIP. On the live
    ledger the twelve most recent cycles all read ``no_op`` while those same durable rows
    carried ``execution=infra_failed`` (four of them, three inside 24 seconds),
    ``execution=cancelled`` (the owner's own stop command) and ``objective=fail`` with a
    ``blocked_with_evidence`` tier (measured work the commit gate refused). Not one of the
    twelve had chosen to do nothing — and the word was fed back into the next cycle's own
    objective as its record of itself.

    So the value is derived from the strongest evidence present, and where the record cannot
    tell "chose nothing" from "did everything it could and nothing landed", it says the thing
    it CAN prove.
    """
    execution = str(((axes or {}).get("execution") or {}).get("status") or "").strip()
    if execution in {EXECUTION_CANCELLED, EXECUTION_INTERRUPTED}:
        return INTERRUPTED_CYCLE_OUTCOME
    if execution == EXECUTION_INFRA_FAILED:
        return INFRA_FAILED_CYCLE_OUTCOME
    return UNCOMMITTED_CYCLE_OUTCOME


def resolve_reported_cycle_outcome(transaction: Dict[str, Any] | None) -> str:
    """What a task-done checkpoint may TRUTHFULLY say about the cycle's outcome.

    The two-phase write exists because an absorbing cycle's verdict is only
    confirmed after a restart verifies the commit. It is not a licence to report
    "waiting_for_restart" for a cycle whose verdict is already known: the live
    ledger shows nine consecutive cycles that are all ``no_op`` with
    ``restart_required=False``, and none of them will ever reach a second write.
    Filing those as pending would leave Engram holding a permanent lie, and the
    memory of "which objectives actually improved the system" — the whole point of
    this write — would be worthless.

    Precedence is therefore:

    1. a genuinely pending restart wins — an unverified commit is NOT yet an
       absorbed outcome, and saying "absorbed" here would claim a verification
       that has not happened;
    2. otherwise the transaction's own verdict, passed through unchanged (a
       verdict this function does not recognise is still its own word);
    3. an absent verdict is ``unknown`` — never laundered into "pending".
    """
    tx = transaction if isinstance(transaction, dict) else {}
    restart_pending = bool(tx.get("restart_required")) and not bool(tx.get("restart_verified"))
    if restart_pending:
        return PENDING_CYCLE_OUTCOME
    return str(tx.get("cycle_outcome") or "").strip() or "unknown"



def _sha_file(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _git_value(repo_dir: pathlib.Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _engram_memory_version(drive_root: Any, repo_dir: Any = None) -> Dict[str, Any]:
    """Engram-side memory version for a checkpoint. Never raises.

    Bounded (Engram's own 500 ceiling) and soft: an unreachable store records
    ``status="unavailable"`` rather than an empty string that a later reader
    would mistake for "nothing changed".
    """
    unavailable = {
        "engram_memory_version": "",
        "engram_memory_version_status": "unavailable",
        "engram_memory_count": 0,
    }
    try:
        from types import SimpleNamespace

        from ouroboros.engram_read import client_for, memory_version

        # Both roots: a bare drive root resolves the project from ``.../data``.
        scope = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir or drive_root)
        read = memory_version(client_for(scope))
        return {
            "engram_memory_version": read.version,
            "engram_memory_version_status": read.status,
            "engram_memory_count": read.count,
        }
    except Exception:
        return unavailable


def append_cycle_outcome_checkpoint(
    drive_root: pathlib.Path,
    *,
    campaign: Dict[str, Any] | None = None,
    transaction: Dict[str, Any] | None = None,
    source: str = "",
    backlog_id: str = "",
) -> None:
    """Tag the ledger when a cycle outcome is decided AFTER task-done.

    A commit-bearing cycle is recorded ``waiting_for_restart`` at task-done;
    the absorbed/abandoned resolution lands later (restart verification or
    boot reconcile) and previously never reached this ledger — the solve-
    capability history could not tell which objectives actually got absorbed.
    Append-only and schema-additive (``kind="cycle_outcome"``); join key with
    the task-done row is ``task_id``.
    """
    tx = transaction if isinstance(transaction, dict) else {}
    entry = {
        "schema_version": 1,
        "kind": "cycle_outcome",
        "ts": utc_now_iso(),
        "source": str(source or ""),
        "task_id": str(tx.get("task_id") or ""),
        "campaign_id": str((campaign or {}).get("id") or tx.get("campaign_id") or ""),
        "campaign_objective": str((campaign or {}).get("objective") or ""),
        "backlog_id": str(backlog_id or ""),
        "cycle_outcome": str(tx.get("cycle_outcome") or ""),
        "abandoned_reason": str(tx.get("abandoned_reason") or ""),
        "commit_sha": str(tx.get("commit_sha") or ""),
        "outcome_axes": normalize_outcome_axes({"outcome_axes": tx.get("outcome_axes") or {}}),
    }
    append_jsonl(pathlib.Path(drive_root) / CHECKPOINTS_REL, entry)


def _projected_cycle_outcome(info: Dict[str, Any]) -> str:
    """The outcome a cycle row SUPPORTS, for projection into the next cycle.

    Legacy rows were minted with a bare ``no_op`` from ONE fact — this agent's
    ``commit_sha`` is empty — and that word asserts INTENT. A stored label is history and is
    never rewritten; a PROJECTION must not repeat a claim the row cannot support, so a
    no-commit row still carrying that fallback is re-derived here from the same outcome axes
    the live mint now reads. (Precedent: the disclosure reads the path that decided —
    DEVELOPMENT.md, receipt identity.)
    """
    stored = str(info.get("cycle_outcome") or "unknown")
    if stored != LEGACY_NO_OP_CYCLE_OUTCOME or info.get("commit_sha"):
        return stored
    return uncommitted_cycle_outcome(info.get("axes") or {})


def build_solve_capability_digest(drive_root: pathlib.Path, *, max_entries: int = 200) -> str:
    """Compact digest of which objectives actually improved the system.

    Joins task-done checkpoints with later ``cycle_outcome`` tags (last wins
    per task) and renders absorbed vs every non-absorbed outcome for the
    promotion chooser. Returns "" when there is no usable history.
    """
    import json as _json

    path = pathlib.Path(drive_root) / CHECKPOINTS_REL
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    omitted_lines = max(0, len(all_lines) - max_entries)
    lines = all_lines[-max_entries:]
    rows = []
    for line in lines:
        try:
            row = _json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        return ""

    by_task: Dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        merged = by_task.setdefault(task_id, {})
        if task_id not in order:
            order.append(task_id)
        _row_axes = row.get("outcome_axes") if isinstance(row.get("outcome_axes"), dict) else {}
        if _row_axes and not merged.get("axes"):
            merged["axes"] = _row_axes
        if str(row.get("kind") or "") == "cycle_outcome":
            merged["cycle_outcome"] = str(row.get("cycle_outcome") or merged.get("cycle_outcome") or "")
            merged["abandoned_reason"] = str(row.get("abandoned_reason") or merged.get("abandoned_reason") or "")
            merged.setdefault("objective", str(row.get("campaign_objective") or ""))
            merged["commit_sha"] = str(row.get("commit_sha") or merged.get("commit_sha") or "")
        else:
            tx = row.get("transaction") if isinstance(row.get("transaction"), dict) else {}
            # Task-done rows: a later cycle_outcome tag wins over the
            # waiting_for_restart recorded at task end.
            merged.setdefault("cycle_outcome", str(tx.get("cycle_outcome") or ""))
            merged["objective"] = str(row.get("campaign_objective") or merged.get("objective") or "")
            merged.setdefault("commit_sha", str(tx.get("commit_sha") or ""))
            merged["rounds"] = int(row.get("rounds") or 0)
            merged["cost_accounting_status"] = str(
                row.get("cost_accounting_status") or "available"
            )
            merged["cost_usd"] = (
                float(row.get("cost_usd")) if row.get("cost_usd") is not None else None
            )
            execution = (row.get("outcome_axes") or {}).get("execution") or {}
            merged["execution"] = str(execution.get("status") or "")

    counts: Dict[str, int] = {}
    absorbed: list[str] = []
    failed: list[str] = []
    for task_id in reversed(order):  # newest first
        info = by_task.get(task_id) or {}
        outcome = _projected_cycle_outcome(info)
        counts[outcome] = counts.get(outcome, 0) + 1
        objective = str(info.get("objective") or "").strip().replace("\n", " ")
        if len(objective) > 110:
            # Explicit omission marker (no silent [:N] of cognitive artifacts);
            # the full objective stays in the ledger row.
            objective = objective[:110] + " …[truncated; full objective in the ledger]"
        if outcome == "absorbed" and len(absorbed) < 8:
            extras = []
            if info.get("commit_sha"):
                extras.append(str(info["commit_sha"])[:10])
            if info.get("rounds"):
                extras.append(f"rounds={info['rounds']}")
            if info.get("cost_accounting_status") == "unavailable":
                extras.append("cost=unavailable")
            elif info.get("cost_usd"):
                extras.append(f"cost=${info['cost_usd']:.2f}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            absorbed.append(f"- ABSORBED: {objective or '(objective unknown)'}{suffix}")
        elif outcome in SPENT_CYCLE_OUTCOMES and outcome != "absorbed" and len(failed) < 4:
            reason = str(info.get("abandoned_reason") or "").strip()
            suffix = f" — {reason}" if reason else ""
            failed.append(f"- {outcome.upper()}: {objective or '(objective unknown)'}{suffix}")
    if not counts:
        return ""
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    parts = [f"Cycle outcomes ({len(by_task)} recent cycles): {summary}."]
    if omitted_lines:
        # P1: no silent truncation of cognitive history — disclose the window.
        parts.append(
            f"[OMISSION NOTE: digest covers the newest {max_entries} ledger rows; "
            f"{omitted_lines} older rows omitted — full history in state/evolution_checkpoints.jsonl]"
        )
    if absorbed:
        parts.append("Objectives that ACTUALLY got absorbed (reviewed commit survived restart):")
        parts.extend(absorbed)
    if failed:
        parts.append("Recent cycles that did NOT land:")
        parts.extend(failed)
    return "\n".join(parts)


def append_evolution_checkpoint(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    task_id: str,
    campaign: Dict[str, Any] | None = None,
    outcome_axes: Dict[str, Any] | None = None,
    cost_usd: Optional[float] = 0.0,
    cost_accounting_status: str = "available",
    rounds: int = 0,
    transaction: Dict[str, Any] | None = None,
) -> None:
    """Persist a lightweight checkpoint after an evolution cycle."""
    memory = pathlib.Path(drive_root) / "memory"
    entry = {
        "schema_version": 1,
        "ts": utc_now_iso(),
        "task_id": str(task_id or ""),
        "campaign_id": str((campaign or {}).get("id") or ""),
        "campaign_objective": str((campaign or {}).get("objective") or ""),
        "git_sha": _git_value(pathlib.Path(repo_dir), ["rev-parse", "HEAD"]),
        "git_branch": _git_value(pathlib.Path(repo_dir), ["rev-parse", "--abbrev-ref", "HEAD"]),
        "identity_sha256": _sha_file(memory / "identity.md"),
        "scratchpad_sha256": _sha_file(memory / "scratchpad.md"),
        # C15/AC17(a): durable knowledge lives in Engram now, so hashing a local
        # index file would silently hash an EMPTY file and read as "unchanged".
        # Record an Engram-side version together with its status, so a reader can
        # tell "memory unchanged" from "memory could not be read" — the old sha
        # could not, because a missing file and a stable file both looked stable.
        **_engram_memory_version(drive_root, repo_dir),
        "outcome_axes": normalize_outcome_axes({"outcome_axes": outcome_axes or {}}),
        "cost_usd": (
            float(cost_usd) if cost_accounting_status == "available" and cost_usd is not None
            else None
        ),
        "cost_accounting_status": (
            "available" if cost_accounting_status == "available" and cost_usd is not None
            else "unavailable"
        ),
        "rounds": int(rounds or 0),
    }
    if isinstance(transaction, dict) and transaction:
        entry["transaction"] = dict(transaction)
    append_jsonl(pathlib.Path(drive_root) / CHECKPOINTS_REL, entry)
