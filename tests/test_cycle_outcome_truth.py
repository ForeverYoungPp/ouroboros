"""The cycle-outcome vocabulary must say only what the cycle's record supports.

Measured on the live ledger (2026-09-13): the twelve most recent cycles were ALL reported to
the next cycle as ``no_op`` — read as "the cycle chose to do nothing" — while those same
durable rows carried ``execution=infra_failed`` (four of them, three inside 24 seconds),
``execution=cancelled`` (the owner's own stop command) and ``objective=fail`` with a
``blocked_with_evidence`` tier (measured work the commit gate refused). The value was minted
from ONE fact, ``commit_sha == ""`` — evidence about AUTHORSHIP — which cannot support a
claim about INTENT, least of all one fed back into the next cycle's own objective as its
record of itself.
"""

from __future__ import annotations

import ast
import json
import pathlib

from ouroboros.evolution_checkpoints import (
    CHECKPOINTS_REL,
    CYCLE_OUTCOME_VOCABULARY,
    INFRA_FAILED_CYCLE_OUTCOME,
    INTERRUPTED_CYCLE_OUTCOME,
    LEGACY_NO_OP_CYCLE_OUTCOME,
    PENDING_CYCLE_OUTCOME,
    SPENT_CYCLE_OUTCOMES,
    UNCOMMITTED_CYCLE_OUTCOME,
    build_solve_capability_digest,
    uncommitted_cycle_outcome,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------- derivation

def test_a_cancelled_cycle_is_not_reported_as_a_choice():
    axes = {"execution": {"status": "cancelled"}}
    assert uncommitted_cycle_outcome(axes) == INTERRUPTED_CYCLE_OUTCOME


def test_an_interrupted_cycle_is_not_reported_as_a_choice():
    axes = {"execution": {"status": "interrupted"}}
    assert uncommitted_cycle_outcome(axes) == INTERRUPTED_CYCLE_OUTCOME


def test_an_infrastructure_failure_is_not_reported_as_a_choice():
    axes = {"execution": {"status": "infra_failed"}}
    assert uncommitted_cycle_outcome(axes) == INFRA_FAILED_CYCLE_OUTCOME


def test_an_ok_cycle_with_no_commit_claims_only_what_is_proven():
    """execution=ok proves the cycle RAN; it cannot prove why no commit exists."""
    for axes in (
        {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}},
        {"execution": {"status": "degraded"}},
        {"execution": {"status": "best_effort"}},
        {},
        None,
    ):
        assert uncommitted_cycle_outcome(axes) == UNCOMMITTED_CYCLE_OUTCOME


def test_the_derivation_never_returns_the_legacy_word():
    for status in ("", "ok", "degraded", "best_effort", "failed", "cancelled",
                   "infra_failed", "interrupted", "something-new"):
        assert uncommitted_cycle_outcome({"execution": {"status": status}}) != LEGACY_NO_OP_CYCLE_OUTCOME


# -------------------------------------------- the vocabulary is closed by construction

def _minted_cycle_outcomes() -> set:
    """Every string literal assigned into a ``[...]["cycle_outcome"]`` slot."""
    found = set()
    paths = sorted(list(REPO_ROOT.glob("supervisor/*.py")) + list(REPO_ROOT.glob("ouroboros/*.py")))
    assert paths, "no source files scanned"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if not (isinstance(key, ast.Constant) and key.value == "cycle_outcome"):
                continue
            candidates = value.values if isinstance(value, ast.BoolOp) else [value]
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    found.add(candidate.value)
    return found


def test_every_minted_outcome_is_in_the_spent_vocabulary():
    """A new outcome cannot be added without answering the one property consumers need.

    ``post_task_evolution`` used a three-name literal and silently SKIPPED anything else, so
    an outcome not listed there dropped its objective out of the anti-repeat guard entirely.
    Two ways to mint are covered. A string literal is visible to the scan below; the DERIVED
    value is a CALL and therefore invisible to it, so its OUTPUT SPACE over a wide input
    matrix is pinned instead — otherwise the scan would go blind precisely where the new code
    lives, and a value missing from the vocabulary would pass unnoticed.
    """
    minted = _minted_cycle_outcomes()
    assert minted, "the AST scan found no cycle_outcome assignment — it has gone blind"
    assert minted <= CYCLE_OUTCOME_VOCABULARY, sorted(minted - CYCLE_OUTCOME_VOCABULARY)
    # The two sets answer two different questions; the spent subset is DRAWN from the
    # vocabulary rather than restated, so neither can drift from the other.
    assert SPENT_CYCLE_OUTCOMES <= CYCLE_OUTCOME_VOCABULARY
    assert PENDING_CYCLE_OUTCOME not in SPENT_CYCLE_OUTCOMES
    for status in ("", "ok", "degraded", "best_effort", "failed", "cancelled",
                   "infra_failed", "interrupted", "unknown", "something-new"):
        derived = uncommitted_cycle_outcome({"execution": {"status": status}})
        assert derived in SPENT_CYCLE_OUTCOMES, (status, derived)


# --------------------------------------------------- the projection tells the truth

def _write_rows(drive_root, rows):
    path = pathlib.Path(drive_root) / CHECKPOINTS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _done_row(task_id, *, execution, stored="no_op", commit_sha="", rounds=3):
    return {
        "task_id": task_id,
        "campaign_objective": "Autonomously improve Ouroboros",
        "rounds": rounds,
        "outcome_axes": {
            "execution": {"status": execution},
            "objective": {"status": "not_evaluated"},
        },
        "transaction": {"cycle_outcome": stored, "commit_sha": commit_sha},
    }


def test_the_digest_re_derives_a_legacy_label_instead_of_repeating_it(tmp_path):
    """A row stored as `no_op` whose own axes say infra_failed must not be echoed."""
    _write_rows(tmp_path, [
        _done_row("t-infra", execution="infra_failed"),
        _done_row("t-cancel", execution="cancelled"),
    ])
    digest = build_solve_capability_digest(tmp_path)
    assert f"{INFRA_FAILED_CYCLE_OUTCOME}=1" in digest
    assert f"{INTERRUPTED_CYCLE_OUTCOME}=1" in digest
    assert f"{LEGACY_NO_OP_CYCLE_OUTCOME}=" not in digest


def test_cycles_that_did_not_land_are_all_listed(tmp_path):
    """The old section listed only {abandoned,no_op}: infra_failed and interrupted vanished."""
    _write_rows(tmp_path, [
        _done_row("t-infra", execution="infra_failed"),
        _done_row("t-cancel", execution="cancelled"),
        _done_row("t-ok", execution="ok"),
    ])
    digest = build_solve_capability_digest(tmp_path)
    assert "Recent cycles that did NOT land:" in digest
    for name in (INFRA_FAILED_CYCLE_OUTCOME, INTERRUPTED_CYCLE_OUTCOME, UNCOMMITTED_CYCLE_OUTCOME):
        assert f"- {name.upper()}:" in digest


def test_absorbed_cycles_are_still_reported_as_absorbed(tmp_path):
    _write_rows(tmp_path, [
        _done_row("t-win", execution="ok", stored="absorbed", commit_sha="abc123"),
    ])
    digest = build_solve_capability_digest(tmp_path)
    assert "absorbed=1" in digest
    assert "ABSORBED" in digest
    assert "did NOT land" not in digest
