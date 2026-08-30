"""Nanny-leaf S1 + custody-absorption D1 contracts: the periodic sweep's late
settlement refreshes a TERMINAL task's stored custody disclosure (audit-only —
never cancels), the boot backfill heals generation-crossing stale rows, the
kill paths clear a stale list, and the retry-lineage projection stops
resurrecting the cleared run."""

from __future__ import annotations

import pathlib
import types

from ouroboros import delegate_custody as custody
from ouroboros import delegate_terminal
from ouroboros.task_results import (
    STATUS_FAILED,
    STATUS_RUNNING,
    load_task_result,
    write_task_result,
)


def _stale_terminal_result(tmp_path: pathlib.Path, task_id: str, **extra) -> None:
    extra.setdefault("delegated_runs_unreconciled", ["run-stale"])
    write_task_result(
        tmp_path, task_id, STATUS_FAILED,
        reason_code="provider_unavailable",
        **extra,
    )


def _emit_started(tmp_path: pathlib.Path, run_id: str, task_id: str, **extra) -> None:
    assert custody.emit(tmp_path, custody.STARTED,
                        {"run_id": run_id, "task_id": task_id, **extra})


def _emit_settled(tmp_path: pathlib.Path, run_id: str, task_id: str) -> None:
    assert custody.emit(tmp_path, custody.SETTLED,
                        {"run_id": run_id, "task_id": task_id})


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


# ---------------------------------------------------------------------------
# D1a — boot backfill (generation-crossing settlements)
# ---------------------------------------------------------------------------


def test_boot_backfill_fixes_row_settled_in_a_previous_generation(tmp_path):
    """A run settled by a PREVIOUS generation's sweep appears in no current
    pass's outcomes; only the boot backfill can heal that stored row. Q2=B: the
    frozen run counters and the primary reason survive the refresh — the
    envelope (trigger + open_run_ids) is the current-liveness surface."""
    _emit_started(tmp_path, "run-1", "t-gen")
    _emit_settled(tmp_path, "run-1", "t-gen")
    write_task_result(
        tmp_path, "t-gen", STATUS_FAILED,
        reason_code="delegated_custody_unreconciled",
        delegated_runs_unreconciled=["run-1"],
        delegated_runs_started=1, delegated_runs_settled=0,
    )

    assert delegate_terminal.backfill_terminal_reconciliations(tmp_path) == ["t-gen"]

    result = load_task_result(tmp_path, "t-gen")
    assert result["delegated_runs_unreconciled"] == []
    envelope = result["delegate_terminal_reconciliation"]
    assert envelope["trigger"] == "boot_backfill"
    assert envelope["open_run_ids"] == []
    # Owner Q2=B: counters are a historical snapshot at the original terminal
    # write, never recomputed; reason_code stays untouched (Q5=A).
    assert result["delegated_runs_started"] == 1
    assert result["delegated_runs_settled"] == 0
    assert result["reason_code"] == "delegated_custody_unreconciled"
    assert result["status"] == STATUS_FAILED


def test_boot_backfill_preserves_undisposed_patch_debt(tmp_path):
    """The incident shape: settled mutating run whose captured patch awaits its
    owner's disposition. The backfill rewrites the honest CURRENT audit —
    ``patch:<run_id>`` — and never blindly clears the disclosure to []."""
    _emit_started(tmp_path, "run-2", "t-incident", snapshot_id="snap-2")
    _emit_settled(tmp_path, "run-2", "t-incident")
    _stale_terminal_result(tmp_path, "t-incident",
                           delegated_runs_unreconciled=["run-2"])

    assert delegate_terminal.backfill_terminal_reconciliations(tmp_path) == ["t-incident"]
    assert load_task_result(tmp_path, "t-incident")["delegated_runs_unreconciled"] == [
        "patch:run-2",
    ]


def test_second_boot_is_a_byte_level_noop_for_a_permanently_stale_row(tmp_path):
    """A row the audit cannot improve (undisposed patch) must not rewrite the
    result or append custody events on every boot."""
    _emit_started(tmp_path, "run-2", "t-incident", snapshot_id="snap-2")
    _emit_settled(tmp_path, "run-2", "t-incident")
    _stale_terminal_result(tmp_path, "t-incident",
                           delegated_runs_unreconciled=["run-2"])
    assert delegate_terminal.backfill_terminal_reconciliations(tmp_path) == ["t-incident"]

    row_path = tmp_path / "task_results" / "t-incident.json"
    events_path = tmp_path / "logs" / "events.jsonl"
    row_before = row_path.read_bytes()
    events_before = events_path.read_bytes()

    assert delegate_terminal.backfill_terminal_reconciliations(tmp_path) == []

    assert row_path.read_bytes() == row_before
    assert events_path.read_bytes() == events_before


def test_boot_backfill_shares_one_custody_replay_snapshot(tmp_path, monkeypatch):
    """Auditing N stored rows must not rescan the unbounded event log 4·N
    times: the whole backfill pays exactly one ``replay()`` pass."""
    for index in range(3):
        run_id, task_id = f"run-{index}", f"t-batch-{index}"
        _emit_started(tmp_path, run_id, task_id)
        _emit_settled(tmp_path, run_id, task_id)
        _stale_terminal_result(tmp_path, task_id,
                               delegated_runs_unreconciled=[run_id])

    replays: list = []
    real_replay = custody.replay
    monkeypatch.setattr(
        custody, "replay",
        lambda *args, **kwargs: replays.append(1) or real_replay(*args, **kwargs),
    )
    refreshed = delegate_terminal.backfill_terminal_reconciliations(tmp_path)
    assert sorted(refreshed) == ["t-batch-0", "t-batch-1", "t-batch-2"]
    assert len(replays) == 1


def test_backfill_lock_timeout_is_fail_soft_and_heals_on_the_next_boot(tmp_path, monkeypatch):
    import ouroboros.task_results as task_results_mod

    _emit_started(tmp_path, "run-4", "t-locked")
    _emit_settled(tmp_path, "run-4", "t-locked")
    _stale_terminal_result(tmp_path, "t-locked",
                           delegated_runs_unreconciled=["run-4"])

    def _timeout(*_args, **_kwargs):
        raise TimeoutError("lock held elsewhere")

    monkeypatch.setattr(task_results_mod, "update_json_locked", _timeout)
    delegate_terminal.backfill_terminal_reconciliations(tmp_path)  # must not raise
    assert load_task_result(tmp_path, "t-locked")["delegated_runs_unreconciled"] == ["run-4"]

    monkeypatch.undo()
    assert delegate_terminal.backfill_terminal_reconciliations(tmp_path) == ["t-locked"]
    assert load_task_result(tmp_path, "t-locked")["delegated_runs_unreconciled"] == []


def test_same_boot_sweep_settlement_is_cleared_in_the_same_generation(tmp_path, monkeypatch):
    """Server ordering (fable #9): the backfill runs AFTER the startup orphan
    reconcile, so a settlement performed by THIS boot's sweep — even one whose
    outcome shape the sweep-side refresh filter never sees — is already visible
    to the backfill audit and the stored row heals in the same generation."""
    import server as server_mod
    from ouroboros import delegate_custody as custody_mod

    _emit_started(tmp_path, "run-5", "t-late")
    _stale_terminal_result(tmp_path, "t-late", delegated_runs_unreconciled=["run-5"])
    monkeypatch.setattr(server_mod, "DATA_DIR", tmp_path)

    def fake_reconcile(drive_root, *, running_task_ids, gateway_factory,
                       recoverable_task_ids):
        _emit_settled(tmp_path, "run-5", "t-late")
        return []

    monkeypatch.setattr(custody_mod, "reconcile_orphaned_runs", fake_reconcile)
    server_mod._startup_custody_sweep()

    result = load_task_result(tmp_path, "t-late")
    assert result["delegated_runs_unreconciled"] == []
    assert result["delegate_terminal_reconciliation"]["trigger"] == "boot_backfill"


# ---------------------------------------------------------------------------
# D1b — kill-path clear
# ---------------------------------------------------------------------------


def test_recorder_and_refresh_never_mint_a_row_for_an_absent_task(tmp_path):
    """The mandatory D1b pin, unit half: a task with NO stored row can never be
    minted through the recorder's STATUS_RUNNING fallback by a clean audit, and
    the guarded kill-path refresh touches only an existing row with a non-empty
    stored list."""
    clean = {
        "task_id": "t-none", "trigger": "cancel_publication",
        "outcomes": [], "unreconciled": [], "audit_status": "ok",
        "deferred_project_retirements": [],
    }
    delegate_terminal.record_terminal_reconciliation(tmp_path, "t-none", clean)
    assert delegate_terminal.refresh_terminal_reconciliation(
        tmp_path, "t-none", trigger="kill_path_clear") is False
    assert not (tmp_path / "task_results" / "t-none.json").exists()


def _kill_env(tmp_path, monkeypatch):
    import supervisor.queue as q
    from supervisor import task_lifecycle, workers

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "RUNNING", {}, raising=False)
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(workers, "respawn_worker", lambda wid: None, raising=False)
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(task_lifecycle, "CANCELLED_ROOT_FENCES", {}, raising=False)
    monkeypatch.setattr(task_lifecycle, "_ACTIVE_CASCADE_FENCES", {}, raising=False)
    return types.SimpleNamespace(q=q, tl=task_lifecycle, drive=tmp_path)


def test_kill_fast_lane_clears_a_stale_disclosure(tmp_path, monkeypatch):
    """The fast already-settled cancel lane performs no terminal write of its
    own; the guarded refresh clears a stale stored list there (D1b)."""
    from ouroboros import cancel_intents as ci

    env = _kill_env(tmp_path, monkeypatch)
    write_task_result(
        env.drive, "t-fast", STATUS_FAILED, result="settled long ago",
        reason_code="delegated_custody_unreconciled",
        delegated_runs_unreconciled=["run-stale"], delegated_runs_settled=0,
    )
    ci.request_cancel(env.drive, "t-fast")

    assert env.tl.cancel_task_custody("t-fast", deliver=False) == env.tl.CANCEL_ALREADY_SETTLED

    stored = load_task_result(env.drive, "t-fast")
    assert stored["status"] == STATUS_FAILED  # settled truth survives the kill
    assert stored["delegated_runs_unreconciled"] == []
    assert stored["delegate_terminal_reconciliation"]["trigger"] == "kill_path_clear"
    # Q2=B / Q5=A: frozen counter and primary reason are untouched.
    assert stored["delegated_runs_settled"] == 0
    assert stored["reason_code"] == "delegated_custody_unreconciled"


def test_kill_fast_lane_adds_no_write_when_nothing_is_stale(tmp_path, monkeypatch):
    """The mandatory D1b pin, lane half: an ordinary fast-lane kill (no stale
    stored list) performs ZERO task-result writes — no RUNNING mint, no second
    write per kill."""
    import ouroboros.task_results as task_results_mod
    from ouroboros import cancel_intents as ci

    env = _kill_env(tmp_path, monkeypatch)
    write_task_result(env.drive, "t-clean-kill", STATUS_FAILED, result="settled")
    ci.request_cancel(env.drive, "t-clean-kill")
    row_path = tmp_path / "task_results" / "t-clean-kill.json"
    row_before = row_path.read_bytes()

    writes: list = []
    real_write = task_results_mod.write_task_result
    monkeypatch.setattr(
        task_results_mod, "write_task_result",
        lambda *args, **kwargs: writes.append(args) or real_write(*args, **kwargs),
    )
    assert env.tl.cancel_task_custody(
        "t-clean-kill", deliver=False) == env.tl.CANCEL_ALREADY_SETTLED
    assert writes == []
    assert row_path.read_bytes() == row_before


def test_retry_lineage_stops_resurrecting_cleared_run(tmp_path):
    from ouroboros.task_status import effective_task_result

    _stale_terminal_result(tmp_path, "t-orig", retry_task_id="t-retry")
    write_task_result(tmp_path, "t-retry", STATUS_FAILED, reason_code="provider_unavailable")

    before = effective_task_result(tmp_path, load_task_result(tmp_path, "t-orig"))
    assert "run-stale" in (before.get("delegated_runs_unreconciled") or [])

    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "t-orig") is True
    after = effective_task_result(tmp_path, load_task_result(tmp_path, "t-orig"))
    assert "run-stale" not in (after.get("delegated_runs_unreconciled") or [])
