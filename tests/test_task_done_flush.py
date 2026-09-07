"""Fix for incident ec2db052: post-task dispatch failure must not lose task_done.

emit_task_results buffers the task_done event into pending_events and only then
calls _dispatch_root_post_task. The caller catches ONLY BudgetExceeded, so any
other exception escaping the dispatch used to propagate to worker_main, which
logs a worker crash and returns — pending_events never flushed, task_done lost,
queue idling instead of terminalizing. The dispatch call is now wrapped:
BudgetExceeded re-raises (the caller owns the budget-pause transition), every
other exception is logged and swallowed so the buffered terminal events fall
through to the caller's flush.
"""

import pathlib
import time
from types import SimpleNamespace

import pytest

import ouroboros.agent_task_pipeline as atp
from ouroboros.usage_accounting import BudgetExceeded


def _make_env(drive_root: pathlib.Path):
    class FakeMemory:
        def load_identity(self):
            return "test identity"

    class FakeCtx:
        pending_restart_reason = None
        pending_restart_is_evolution = False

    class FakeEnv:
        def __init__(self, root):
            self.drive_root = root
            self.repo_dir = root

        def drive_path(self, sub):
            p = self.drive_root / sub
            p.mkdir(parents=True, exist_ok=True)
            return p

    return FakeEnv(drive_root), FakeMemory(), FakeCtx()


def _make_drive(tmp_path):
    drive_root = tmp_path / "data"
    logs = drive_root / "logs"
    logs.mkdir(parents=True)
    (drive_root / "memory").mkdir()
    (drive_root / "task_results").mkdir()
    return drive_root, logs


def _run_emit(env, memory, ctx, pending_events, task, monkeypatch, dispatch_exc=None):
    """Drive the REAL emit_task_results with stubbed consolidation and dispatch.

    Consolidation helpers only run inside _dispatch_root_post_task, so stubbing
    them is defensive only; the dispatch stub lets the test control the raised
    exception. All file I/O lands under env.drive_root (tmp_path).
    """
    calls = []
    if dispatch_exc is not None:
        def _failing_dispatch(*args, **kwargs):
            calls.append(1)
            raise dispatch_exc
    else:
        def _failing_dispatch(*args, **kwargs):
            calls.append(1)

    monkeypatch.setattr(atp, "_run_chat_consolidation", lambda *a, **kw: None)
    monkeypatch.setattr(atp, "_run_scratchpad_consolidation", lambda *a, **kw: None)
    monkeypatch.setattr(atp, "_run_post_task_processing_async", lambda *a, **kw: None)
    monkeypatch.setattr(atp, "_dispatch_root_post_task", _failing_dispatch)

    atp.emit_task_results(
        env=env, memory=memory, llm=object(),
        pending_events=pending_events,
        task=task, text="Reply text",
        usage={"cost": 0.01, "rounds": 1, "prompt_tokens": 10, "completion_tokens": 5},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=time.time() - 1.0,
        drive_logs=env.drive_root / "logs",
        ctx=ctx,
        event_queue=None,  # live delivery skipped; events stay buffered
    )
    return calls


def test_dispatch_failure_still_returns_pending_events(tmp_path, monkeypatch):
    drive_root, logs = _make_drive(tmp_path)
    env, memory, ctx = _make_env(drive_root)
    pending_events = []
    task = {"id": "flush-regress", "type": "task", "chat_id": 1, "text": "hello"}

    calls = _run_emit(env, memory, ctx, pending_events, task, monkeypatch,
                      dispatch_exc=RuntimeError("synthesis blew up"))

    # The stubbed dispatch actually ran and raised — the failure was real.
    assert calls == [1]
    # emit_task_results must NOT raise: the RuntimeError is swallowed so the
    # worker survives to flush pending_events.
    types = [e.get("type") for e in pending_events]
    assert "send_message" in types
    done = [e for e in pending_events if e.get("type") == "task_done"]
    assert len(done) == 1, f"expected exactly one buffered task_done, got {types}"
    assert done[0]["status"] == "completed"


def test_dispatch_budgetexceeded_propagates(tmp_path, monkeypatch):
    drive_root, logs = _make_drive(tmp_path)
    env, memory, ctx = _make_env(drive_root)
    pending_events = []
    task = {"id": "budget-rail", "type": "task", "chat_id": 1, "text": "hello"}

    with pytest.raises(BudgetExceeded):
        _run_emit(env, memory, ctx, pending_events, task, monkeypatch,
                  dispatch_exc=BudgetExceeded("limit", limit_scope="global"))
