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
