"""Fix 8: ephemeral decision-turn lock hardening.

Covers the three behaviors added by Fix 8 to supervisor/workers.py:
8a — the turn gets a wall-clock deadline_at so a hung provider request cannot
     hold _ephemeral_chat_lock forever (the per-read-interval transport timeout
     is resettable by a streaming/keepalive response);
8b — past OUROBOROS_EPHEMERAL_QUEUE_MAX queued turns, a new owner message gets
     a visible acknowledgment and is dropped instead of silently piling up an
     unbounded waiter stack;
8c — make_agent runs OUTSIDE _ephemeral_chat_lock (construction is pure setup).
"""

import threading
import time


def _fake_agent_factory(*, built: list, hold_lock: threading.Event = None):
    """Returns a make_agent replacement that records builds and, when
    hold_lock is set, blocks inside _run_chat_task (simulating a hung turn)."""
    built.append("agent")

    class _FakeAgent:
        _event_queue = None

        def handle_task(self, task):
            if hold_lock is not None:
                hold_lock.wait(timeout=10)
            return []

    return _FakeAgent()


def test_ephemeral_turn_injects_deadline_at(monkeypatch):
    """8a: a turn without a caller-supplied deadline gets a future deadline_at."""
    from supervisor import workers as w
    import ouroboros.agent as agent_mod
    import supervisor.state as state_mod

    built = []
    seen = {}

    monkeypatch.setattr(agent_mod, "make_agent", lambda **kw: _fake_agent_factory(built=built))
    monkeypatch.setattr(w, "_repo_writer_turn_allowed", lambda cid: True)
    monkeypatch.setattr(state_mod, "budget_remaining", lambda st, strict=False: float("inf"))
    monkeypatch.setattr(state_mod, "load_state", lambda: {"owner_id": 1})
    original = w._run_chat_task

    def _capture(agent, chat_id, text, image_data=None, task_constraint=None, task_metadata=None, **kw):
        seen["meta"] = dict(task_metadata or {})
        return original(agent, chat_id, text, image_data, task_constraint=task_constraint,
                        task_metadata=task_metadata, **kw)

    monkeypatch.setattr(w, "_run_chat_task", _capture)

    # Run synchronously (no thread) by invoking the inner path directly.
    w.handle_chat_ephemeral(1, "probe")
    assert "deadline_at" in seen.get("meta", {}), "deadline_at must be injected"
    from ouroboros.deadline_utils import parse_deadline_ts, utc_now
    parsed = parse_deadline_ts(seen["meta"]["deadline_at"])
    assert parsed is not None and (parsed - utc_now()).total_seconds() > 0


def test_ephemeral_preserves_caller_deadline(monkeypatch):
    """8a: a caller-supplied deadline_at is honored, never overwritten."""
    from supervisor import workers as w
    import ouroboros.agent as agent_mod
    import supervisor.state as state_mod

    built = []
    seen = {}

    monkeypatch.setattr(agent_mod, "make_agent", lambda **kw: _fake_agent_factory(built=built))
    monkeypatch.setattr(w, "_repo_writer_turn_allowed", lambda cid: True)
    monkeypatch.setattr(state_mod, "budget_remaining", lambda st, strict=False: float("inf"))
    monkeypatch.setattr(state_mod, "load_state", lambda: {"owner_id": 1})
    original = w._run_chat_task

    def _capture(agent, chat_id, text, image_data=None, task_constraint=None, task_metadata=None, **kw):
        seen["meta"] = dict(task_metadata or {})
        return original(agent, chat_id, text, image_data, task_constraint=task_constraint,
                        task_metadata=task_metadata, **kw)

    monkeypatch.setattr(w, "_run_chat_task", _capture)
    caller_deadline = "2099-01-01T00:00:00+00:00"
    w.handle_chat_ephemeral(1, "probe", task_metadata={"deadline_at": caller_deadline})
    assert seen["meta"]["deadline_at"] == caller_deadline


def test_ephemeral_queue_full_acknowledges_and_drops(monkeypatch):
    """8b: past OUROBOROS_EPHEMERAL_QUEUE_MAX, a new message is acked+dropped."""
    from supervisor import workers as w
    import ouroboros.agent as agent_mod
    import supervisor.state as state_mod

    receipts = []
    monkeypatch.setattr(state_mod, "budget_remaining", lambda st, strict=False: float("inf"))
    monkeypatch.setattr(state_mod, "load_state", lambda: {"owner_id": 1})
    monkeypatch.setattr(agent_mod, "make_agent", lambda **kw: _fake_agent_factory(built=[]))
    monkeypatch.setattr(w, "_repo_writer_turn_allowed", lambda cid: True)
    monkeypatch.setattr(w, "send_with_budget", lambda cid, text, **kw: receipts.append(text))
    monkeypatch.setenv("OUROBOROS_EPHEMERAL_QUEUE_MAX", "0")

    engaged = threading.Event()

    def _dummy_run(agent, chat_id, text, image_data=None, task_constraint=None, task_metadata=None, **kw):
        engaged.set()
        time.sleep(0.5)
        return []

    monkeypatch.setattr(w, "_run_chat_task", _dummy_run)

    # First turn runs in a thread and holds the single slot (sleeping);
    # a SECOND message while it is in flight must be dropped with a receipt.
    t1 = threading.Thread(target=w.handle_chat_ephemeral, args=(1, "first"))
    t1.start()
    assert engaged.wait(timeout=5), "first turn must engage the slot"
    w.handle_chat_ephemeral(1, "second")   # concurrent: slot still held
    t1.join(timeout=5)
    assert receipts, "queue-full receipt must be sent"
    assert any("处理中" in r or "排队" in r for r in receipts)


def test_ephemeral_make_agent_outside_lock(monkeypatch):
    """8c: make_agent runs before the lock is acquired — builds proceed while a
    locked turn is hung."""
    from supervisor import workers as w
    import ouroboros.agent as agent_mod
    import supervisor.state as state_mod

    builds = []
    lock_held = threading.Event()
    started = threading.Event()
    build_while_locked = []

    monkeypatch.setattr(state_mod, "budget_remaining", lambda st, strict=False: float("inf"))
    monkeypatch.setattr(state_mod, "load_state", lambda: {"owner_id": 1})

    def _make_agent(**kw):
        builds.append(1)
        if lock_held.is_set():
            build_while_locked.append(1)  # make_agent ran while lock was held
        return _fake_agent_factory(built=[])

    monkeypatch.setattr(agent_mod, "make_agent", _make_agent)

    def _hung_run(agent, chat_id, text, image_data=None, task_constraint=None, task_metadata=None, **kw):
        started.set()
        lock_held.set()
        time.sleep(0.6)
        return []

    monkeypatch.setattr(w, "_run_chat_task", _hung_run)

    t = threading.Thread(target=w.handle_chat_ephemeral, args=(1, "hang"))
    t.start()
    started.wait(timeout=5)
    # While the first turn holds the lock (inside _run_chat_task), a second
    # call builds its agent BEFORE waiting on the lock. Verify make_agent ran
    # concurrently — i.e. not serialized behind the locked turn.
    event = threading.Event()

    def _second():
        w.handle_chat_ephemeral(1, "second")
        event.set()

    t2 = threading.Thread(target=_second)
    t2.start()
    event.wait(timeout=5)
    t.join(timeout=5)
    assert build_while_locked, "make_agent must be able to run while the lock is held by another turn"