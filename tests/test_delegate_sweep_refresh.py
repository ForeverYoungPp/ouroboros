"""Nanny-leaf S1 contracts: the periodic sweep's late settlement refreshes a
TERMINAL task's stored custody disclosure (audit-only — never cancels), and the
retry-lineage projection stops resurrecting the cleared run."""

from __future__ import annotations

import pathlib

from ouroboros import delegate_terminal
from ouroboros.task_results import (
    STATUS_FAILED,
    STATUS_RUNNING,
    load_task_result,
    write_task_result,
)


def _stale_terminal_result(tmp_path: pathlib.Path, task_id: str, **extra) -> None:
    write_task_result(
        tmp_path, task_id, STATUS_FAILED,
        reason_code="provider_unavailable",
        delegated_runs_unreconciled=["run-stale"],
        **extra,
    )


def test_sweep_refresh_clears_stale_unreconciled_after_settlement(tmp_path):
    _stale_terminal_result(tmp_path, "t-stale")
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-stale") is True
    result = load_task_result(tmp_path, "t-stale")
    assert result["delegated_runs_unreconciled"] == []
    assert result["delegate_terminal_reconciliation"]["trigger"] == "sweep_refresh"
    # Q5=A: the primary reason survives — the refresh never rewrites reason_code.
    assert result["reason_code"] == "provider_unavailable"
    assert result["status"] == STATUS_FAILED


def test_sweep_refresh_skips_running_and_clean_tasks(tmp_path):
    write_task_result(
        tmp_path, "t-running", STATUS_RUNNING,
        delegated_runs_unreconciled=["run-live"],
    )
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-running") is False
    assert load_task_result(tmp_path, "t-running")["delegated_runs_unreconciled"] == ["run-live"]

    write_task_result(tmp_path, "t-clean", STATUS_FAILED)
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-clean") is False
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "") is False


def test_sweep_refresh_keeps_disclosure_while_custody_still_open(tmp_path):
    from ouroboros import delegate_custody as custody

    assert custody.record_start_requested(
        tmp_path, run_id="", task_id="t-open", invocation_id="inv-9",
        idempotency_key="inv-9", max_seconds=30, request={"prompt": "brief"},
        project_id="project-9", project_owned=False, route="codex",
    )
    _stale_terminal_result(tmp_path, "t-open")
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-open") is True
    result = load_task_result(tmp_path, "t-open")
    # The refresh writes the honest CURRENT audit, not a blind clear.
    assert result["delegated_runs_unreconciled"] == ["invocation:inv-9"]


def test_retry_lineage_stops_resurrecting_cleared_run(tmp_path):
    from ouroboros.task_status import effective_task_result

    _stale_terminal_result(tmp_path, "t-orig", retry_task_id="t-retry")
    write_task_result(tmp_path, "t-retry", STATUS_FAILED, reason_code="provider_unavailable")

    before = effective_task_result(tmp_path, load_task_result(tmp_path, "t-orig"))
    assert "run-stale" in (before.get("delegated_runs_unreconciled") or [])

    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-orig") is True
    after = effective_task_result(tmp_path, load_task_result(tmp_path, "t-orig"))
    assert "run-stale" not in (after.get("delegated_runs_unreconciled") or [])


def _emit_started(tmp_path, run_id, task_id):
    from ouroboros import delegate_custody as custody

    assert custody.emit(tmp_path, custody.STARTED, {
        "run_id": run_id, "task_id": task_id, "route": "claude", "shape": {},
    })


def _emit_settled(tmp_path, run_id, task_id, *, cost=1.25):
    from ouroboros import delegate_custody as custody

    assert custody.emit(tmp_path, custody.SETTLED, {
        "run_id": run_id, "task_id": task_id, "route": "claude",
        "model": "claude-x", "state": "succeeded", "cost_usd": cost,
        "cost_final": True, "spend_disclosed": True, "spend_estimated": False,
    })


def test_refresh_rewrites_stale_substrate_counters_after_late_settlement(tmp_path):
    """The scout-A contract: a terminal result written BEFORE the run settled
    must, after refresh, tell the truth in the fields readers consume —
    top-level counters, actual_substrate, and the envelope's evidence — not
    only in the unreconciled list (the PR #402 gap)."""
    _emit_started(tmp_path, "run-1", "t-late")
    write_task_result(
        tmp_path, "t-late", STATUS_FAILED,
        reason_code="provider_unavailable",
        delegated_runs_unreconciled=["run-1"],
        actual_substrate="harness_attempted",
        delegated_runs_started=1, delegated_runs_settled=0,
        delegated_runs_succeeded=0, delegated_runs_failed=0,
        delegated_runs_source_unresolved=0,
        subagent_envelope={
            "executor_route": "claude", "effective_executor": "harness",
            "actual_substrate": "harness_attempted",
            "execution_evidence": {
                "delegated_runs_started": 1, "delegated_runs_settled": 0,
                "delegated_runs_succeeded": 0, "delegated_runs_failed": 0,
                "subscription_cost_usd": None,
            },
        },
    )
    _emit_settled(tmp_path, "run-1", "t-late", cost=1.25)

    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-late") is True
    result = load_task_result(tmp_path, "t-late")
    assert result["delegated_runs_unreconciled"] == []
    assert result["actual_substrate"] == "harness_used"
    assert result["delegated_runs_settled"] == 1
    assert result["delegated_runs_succeeded"] == 1
    ev = result["subagent_envelope"]["execution_evidence"]
    assert ev["delegated_runs_succeeded"] == 1
    assert ev["subscription_cost_usd"] == 1.25
    # Dispatch decisions are never overwritten by evidence reconciliation.
    assert result["subagent_envelope"]["executor_route"] == "claude"
    # Q5=A: the primary reason stays untouched.
    assert result["reason_code"] == "provider_unavailable"


def test_refresh_never_mints_evidence_for_undelegated_tasks(tmp_path):
    """A terminal task that never carried the harness-dispatch mirror gets no
    fabricated substrate block from a refresh."""
    write_task_result(tmp_path, "t-native", STATUS_FAILED)
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-native") is False
    result = load_task_result(tmp_path, "t-native")
    assert "actual_substrate" not in result


def test_cursor_pass_heals_tasks_the_outcome_sweep_never_names(tmp_path):
    """A run settled OUTSIDE the sweep's reconcile outcomes (terminal-boundary
    settlement) is found by the cursor pass over newly appended custody rows;
    a second tick with no new rows does no work."""
    _emit_started(tmp_path, "run-9", "t-boundary")
    write_task_result(
        tmp_path, "t-boundary", STATUS_FAILED,
        delegated_runs_unreconciled=[],
        actual_substrate="harness_attempted",
        delegated_runs_started=1, delegated_runs_settled=0,
        delegated_runs_succeeded=0, delegated_runs_failed=0,
        delegated_runs_source_unresolved=0,
    )
    _emit_settled(tmp_path, "run-9", "t-boundary")

    assert delegate_terminal.refresh_recently_settled_terminals(tmp_path) == 1
    result = load_task_result(tmp_path, "t-boundary")
    assert result["actual_substrate"] == "harness_used"
    assert result["delegated_runs_succeeded"] == 1
    # Cursor advanced: the same rows are never reprocessed.
    assert delegate_terminal.refresh_recently_settled_terminals(tmp_path) == 0
