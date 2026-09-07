"""Terminal-row crosscheck (Fix 2).

The enforce loop probes ONCE per attempt whether a truly-terminal result row for
THIS attempt is already on disk; a positive probe skips the finalization grace
window and falls through to the Variant-A reaper teardown (which honors the row
post-kill instead of killing a finished task's worker). The reaper's post-kill
check only honors rows fresh enough to belong to the current attempt, so a
predecessor attempt's terminal row never wins over a live same-id retry.
"""
import json
import queue as stdqueue
import time
import types
from datetime import datetime, timezone

import pytest

from ouroboros.task_results import write_task_result
from supervisor.task_reaper import _row_is_current_attempt


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _enforce_env(monkeypatch, tmp_path, running):
    from supervisor import queue as queue_mod

    monkeypatch.setattr(queue_mod, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_mod, "RUNNING", running)
    monkeypatch.setattr(queue_mod, "PENDING", [])
    monkeypatch.setattr(queue_mod, "get_task_idle_timeout_sec", lambda: 2)
    monkeypatch.setattr(queue_mod, "get_per_call_timeout_ceiling_sec", lambda: 2)
    monkeypatch.setattr(queue_mod, "get_task_abs_ceiling_sec", lambda: 10_000_000.0)
    monkeypatch.setattr(queue_mod, "FINALIZATION_GRACE_SEC", 120.0, raising=False)
    requested = []
    monkeypatch.setattr(
        queue_mod, "_request_finalization_grace",
        lambda _drive, tid, reason, **_k: requested.append((tid, reason)) or f"ctl:{tid}",
    )
    monkeypatch.setattr(queue_mod, "_ensure_reaper_started", lambda: None)
    reap_jobs = stdqueue.Queue()
    monkeypatch.setattr(queue_mod, "_reap_queue", reap_jobs)
    monkeypatch.setattr(queue_mod, "persist_queue_snapshot", lambda reason="": None)

    def _run(now):
        queue_mod._enforce_task_timeouts_locked(
            types.SimpleNamespace(WORKERS={}), now, 7, {})

    return requested, reap_jobs, _run


def _meta(task_id, started_at, **extra):
    meta = {
        "task": {"id": task_id, "type": "task", "chat_id": 0},
        "started_at": started_at,
        "last_heartbeat_at": started_at,
        "last_progress_at": started_at,
        "attempt": 1,
    }
    meta.update(extra)
    return meta


def _write_row(drive_root, task_id, status, *, updated_at=None):
    write_task_result(drive_root, task_id, status, result="done")
    if updated_at is not None:
        path = drive_root / "task_results" / f"{task_id}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["ts"] = updated_at
        row["updated_at"] = updated_at
        path.write_text(json.dumps(row), encoding="utf-8")


def test_probe_positive_falls_through_to_reap(tmp_path, monkeypatch):
    task_id = "crosscheck1"
    now = time.time()
    started_at = now - 300.0
    meta = _meta(task_id, started_at)
    # Row written moments ago: its updated_at (real now) >= started_at, so it IS
    # this attempt's terminal result — the exact lost-task_done incident shape.
    _write_row(tmp_path, task_id, "completed")
    running = {task_id: meta}
    requested, reap_jobs, run = _enforce_env(monkeypatch, tmp_path, running)

    run(now)

    assert requested == []                              # grace never opened
    assert "finalization_requested_at" not in meta
    assert meta["_row_terminal_probed"] is True
    assert task_id not in running                       # popped for teardown
    job = reap_jobs.get_nowait()
    assert job["task_id"] == task_id
    assert job["terminal_reason"] == "idle_timeout"
    assert job["started_at"] == started_at
    assert reap_jobs.qsize() == 0


def test_probe_stale_row_keeps_grace(tmp_path, monkeypatch):
    task_id = "crosscheck2"
    now = time.time()
    started_at = now - 300.0
    meta = _meta(task_id, started_at)
    # Same-id retry shape: a predecessor's completed row predates THIS attempt's
    # start and must not authorize a teardown over the live retry.
    _write_row(tmp_path, task_id, "completed", updated_at=_iso(started_at - 60.0))
    running = {task_id: meta}
    requested, reap_jobs, run = _enforce_env(monkeypatch, tmp_path, running)

    run(now)

    assert requested == [(task_id, "idle_timeout")]
    assert meta["finalization_requested_at"] == now
    assert meta["finalization_reason"] == "idle_timeout"
    assert meta["_row_terminal_probed"] is True
    assert reap_jobs.qsize() == 0
    assert running[task_id] is meta


def test_probe_negative_keeps_grace(tmp_path, monkeypatch):
    task_id = "crosscheck3"
    now = time.time()
    started_at = now - 300.0
    meta = _meta(task_id, started_at)
    # Durable row still running (non-terminal rank): ordinary grace path.
    _write_row(tmp_path, task_id, "running")
    running = {task_id: meta}
    requested, reap_jobs, run = _enforce_env(monkeypatch, tmp_path, running)

    run(now)

    assert requested == [(task_id, "idle_timeout")]
    assert meta["finalization_requested_at"] == now
    assert reap_jobs.qsize() == 0
    assert running[task_id] is meta


def test_probe_active_llm_never_runs(tmp_path, monkeypatch):
    task_id = "crosscheck4"
    now = time.time()
    started_at = now - 300.0
    # In-flight LLM call matching the attempt: the progressing gate continues
    # BEFORE the terminal-row probe, even with a terminal row on disk.
    meta = _meta(
        task_id, started_at,
        last_progress_at=now - 10.0,
        active_llm_call={"task_attempt": 1},
    )
    _write_row(tmp_path, task_id, "completed")
    running = {task_id: meta}
    requested, reap_jobs, run = _enforce_env(monkeypatch, tmp_path, running)

    run(now)

    assert "_row_terminal_probed" not in meta
    assert requested == []
    assert reap_jobs.qsize() == 0
    assert running[task_id] is meta


def test_row_is_current_attempt_units():
    now = time.time()
    started_at = now - 300.0
    # Fresh row (updated_at, then ts fallback) belongs to this attempt.
    assert _row_is_current_attempt({"updated_at": _iso(now)}, started_at) is True
    assert _row_is_current_attempt({"ts": _iso(now)}, started_at) is True
    # Within the 1s clock-slack of the attempt start.
    assert _row_is_current_attempt({"updated_at": _iso(started_at - 0.5)}, started_at) is True
    # Older than this attempt's start: a predecessor's result.
    assert _row_is_current_attempt({"updated_at": _iso(started_at - 60.0)}, started_at) is False
    # Unparseable or missing timestamps never authorize honoring a row.
    assert _row_is_current_attempt({"updated_at": "not-a-date"}, started_at) is False
    assert _row_is_current_attempt({}, started_at) is False
