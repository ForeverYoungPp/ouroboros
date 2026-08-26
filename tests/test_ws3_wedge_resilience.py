"""WS3 — chat-lane wedge resilience (v6.34.0).

A dedicated watchdog thread (outside the supervisor loop) surfaces TWO silent-wedge
classes as observable owner alerts instead of silent hours: a supervisor loop stall
(new-message intake starvation) and a heartbeat-silent in-process direct-chat turn.
New-message intake is reordered EARLY in the loop so a blocking step can't starve it.
The watchdog cannot kill a hung thread or free the chat-agent lock (a wedged turn
holds it for its whole duration; out-of-process kill was deferred per owner), so it
detects + reports + recommends /restart rather than force-recovering in-process; WS10
ephemeral decision turns keep the chat responsive meanwhile.
"""

from __future__ import annotations

import threading
import time


def test_supervisor_loop_stalled_detection():
    import server

    now = 1000.0
    assert server._supervisor_loop_stalled(now - 100, now, 90) is True   # past deadline
    assert server._supervisor_loop_stalled(now - 30, now, 90) is False   # healthy tick
    assert server._supervisor_loop_stalled(now - 100, now, 0) is False   # 0 = disabled


def test_supervisor_liveness_deadline_getter(monkeypatch):
    from ouroboros.config import (
        SUPERVISOR_LIVENESS_DEADLINE_DEFAULT_SEC,
        get_supervisor_liveness_deadline_sec,
    )

    monkeypatch.delenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", raising=False)
    assert get_supervisor_liveness_deadline_sec() == SUPERVISOR_LIVENESS_DEADLINE_DEFAULT_SEC
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "30")
    assert get_supervisor_liveness_deadline_sec() == 30
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "0")
    assert get_supervisor_liveness_deadline_sec() == 0  # disabled


def test_watchdog_noop_when_disabled(monkeypatch):
    import server

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "0")
    before = threading.active_count()
    server._start_supervisor_liveness_watchdog([time.monotonic()])
    assert threading.active_count() == before  # no watchdog thread spawned


def test_watchdog_alerts_owner_once_on_stall(monkeypatch):
    import server

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    alerts = []

    class _Bridge:
        def send_message(self, chat_id, text, *a, **k):
            alerts.append((chat_id, text, k))
            return (True, "")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: _Bridge())
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 5})
    monkeypatch.setattr("supervisor.state.append_jsonl", lambda *a, **k: None)
    stop = threading.Event()  # local per-test token; do NOT touch the global restart flag
    try:
        # The liveness tick is a MONOTONIC stamp (OB-03) — seed it on the same clock.
        server._start_supervisor_liveness_watchdog([time.monotonic() - 100], stop)  # already stale
        end = time.time() + 6
        while not alerts and time.time() < end:
            time.sleep(0.1)
    finally:
        _stop_watchdog(stop)  # join it too: a leaked in-flight iteration outlives the monkeypatch
    assert len(alerts) == 1
    assert alerts[0][0] == 5 and "stalled" in alerts[0][1]
    assert alerts[0][2]["is_progress"] is True
    assert alerts[0][2]["progress_meta"]["task_incident"] == "supervisor_loop_stall"
    assert alerts[0][2]["progress_meta"]["toast_once"].startswith("supervisor-loop-stall:")


def test_chat_turn_wedged_detection():
    import server

    now = 1000.0
    assert server._chat_turn_wedged(True, now - 100, now, 90) is True    # busy + silent past deadline
    assert server._chat_turn_wedged(True, now - 30, now, 90) is False    # busy + recent heartbeat
    assert server._chat_turn_wedged(False, now - 100, now, 90) is False  # not busy
    assert server._chat_turn_wedged(True, None, now, 90) is False        # liveness loop not started yet
    assert server._chat_turn_wedged(True, now - 100, now, 0) is False    # 0 = disabled


def test_chat_turn_liveness_reads_agent_without_taking_the_lock(monkeypatch):
    import types

    import supervisor.workers as w

    monkeypatch.setattr(w, "_chat_agent", None)
    assert w.chat_turn_liveness() == (False, None, None)

    monkeypatch.setattr(w, "_chat_agent", types.SimpleNamespace(
        _busy=True, _current_task_id="t1", _last_activity_ts=1234.0))
    # Hold _chat_agent_lock to prove the liveness read never blocks on it (a wedged
    # turn holds the lock for its whole duration — the watchdog must not deadlock).
    assert w._chat_agent_lock.acquire(blocking=False)
    try:
        assert w.chat_turn_liveness() == (True, "t1", 1234.0)
    finally:
        w._chat_agent_lock.release()


def test_watchdog_alerts_on_chat_turn_wedge(monkeypatch):
    import types

    import server
    import supervisor.workers as w

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    alerts = []

    class _Bridge:
        def send_message(self, chat_id, text, *a, **k):
            alerts.append((chat_id, text, k))
            return (True, "")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: _Bridge())
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 7})
    monkeypatch.setattr("supervisor.state.append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(w, "_chat_agent", types.SimpleNamespace(
        _busy=True, _current_task_id="wedged1", _last_activity_ts=time.time() - 100))
    stop = threading.Event()  # local per-test token
    try:
        server._start_supervisor_liveness_watchdog([time.monotonic()], stop)
        end = time.time() + 6
        while not any("wedged" in a[1] for a in alerts) and time.time() < end:
            time.sleep(0.1)
    finally:
        _stop_watchdog(stop)  # join it too: a leaked in-flight iteration outlives the monkeypatch
    assert any("wedged" in a[1] for a in alerts)  # the chat-turn wedge was surfaced
    assert any(a[0] == 7 for a in alerts)
    wedge = next(a for a in alerts if "wedged" in a[1])
    assert wedge[2]["is_progress"] is True
    assert wedge[2]["task_id"] == "wedged1"
    assert wedge[2]["progress_meta"] == {
        "task_incident": "chat_turn_wedge",
        "toast_once": "wedged1:chat_turn_wedge",
    }


# --- OB-03: the watchdog's two halves run on two different clocks ------------


class _FakeServerClock:
    """A controllable stand-in for the ``time`` module ``server`` reads.

    Only the three calls the watchdog makes are driven (``sleep``/``time``/
    ``monotonic``); everything else falls through to the real module. The TEST
    HARNESS keeps the real clock — this module's own ``time`` is never patched —
    so a simulated wall-clock jump cannot make the harness's own timeouts lie.
    """

    def __init__(self, *, wall: float, mono: float) -> None:
        self.wall = wall
        self.mono = mono
        self.ticks = 0

    def __getattr__(self, name):  # anything the watchdog does not drive stays real
        return getattr(time, name)

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, _seconds: float) -> None:
        self.ticks += 1
        time.sleep(0.01)  # a REAL yield; the fake clock advances only when a test says so


def _wait_until(predicate, budget: float = 6.0) -> None:
    end = time.time() + budget  # real clock: the harness never rides the fake one
    while not predicate() and time.time() < end:
        time.sleep(0.01)


def _stop_watchdog(stop: threading.Event) -> None:
    """Set the token AND wait for the thread to leave the loop.

    The watchdog re-reads the token only at the TOP of the loop, so an iteration
    already in flight still completes its clock reads and checks. Returning before
    that finishes would let it run against a torn-down monkeypatch — reading the
    real ``time`` module against a fake liveness stamp — and emit a phantom alert
    into whatever the next test has patched.
    """
    stop.set()
    for thread in threading.enumerate():
        if thread.name == "supervisor-liveness-watchdog":
            thread.join(timeout=5)


def _collect_alerts(monkeypatch, chat_id: int) -> list:
    alerts: list = []

    class _Bridge:
        def send_message(self, cid, text, *a, **k):
            alerts.append((cid, text, k))
            return (True, "")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: _Bridge())
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": chat_id})
    monkeypatch.setattr("supervisor.state.append_jsonl", lambda *a, **k: None)
    return alerts


def test_wall_clock_jump_neither_fabricates_nor_masks_a_supervisor_stall(monkeypatch):
    """OB-03: the stall half is measured on ``time.monotonic()``.

    The liveness tick and its comparison used to both be ``time.time()``, so an
    ordinary wall-clock step — NTP correction, DST/timezone change, manual set, a
    resumed VM — was indistinguishable from an unresponsive supervisor loop. Both
    directions are pinned: a forward jump must not INVENT a stall, and a backward
    jump must not MASK a real one.
    """
    import server
    import supervisor.workers as w

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    monkeypatch.setattr(w, "_chat_agent", None)  # isolate the stall half
    alerts = _collect_alerts(monkeypatch, 11)

    boot_mono = 500.0
    wall = 1_700_000_000.0
    clock = _FakeServerClock(wall=wall, mono=boot_mono)
    monkeypatch.setattr(server, "time", clock)
    stop = threading.Event()  # local per-test token
    try:
        # The loop ticked "just now" on the monotonic clock — it is healthy.
        server._start_supervisor_liveness_watchdog([boot_mono], stop)
        # Now the wall clock steps an hour forward while the loop keeps ticking.
        clock.wall = wall + 3600.0
        _wait_until(lambda: clock.ticks >= 3)
        assert alerts == [], "a wall-clock jump must not fabricate a supervisor stall"

        # A genuine stall (100s of MONOTONIC silence) is still caught, and a
        # backward wall step cannot hide it.
        clock.wall = wall - 86_400.0
        clock.mono = boot_mono + 100.0
        _wait_until(lambda: alerts)
    finally:
        _stop_watchdog(stop)
    assert len(alerts) == 1
    assert alerts[0][0] == 11 and "stalled" in alerts[0][1]
    assert alerts[0][2]["progress_meta"]["task_incident"] == "supervisor_loop_stall"


def test_wedge_half_stays_on_the_wall_clock_under_a_monotonic_tick(monkeypatch):
    """OB-03 regression: the wedge half must keep comparing WALL stamps.

    ``agent._last_activity_ts`` is a ``time.time()`` stamp. Swapping the whole
    watchdog to one monotonic ``now`` makes ``now - turn_ts`` a large NEGATIVE
    number on any host whose uptime is shorter than the Unix epoch — i.e. every
    host — so ``_chat_turn_wedged`` would answer False forever and this alert
    would silently never fire again. That is the split this test defends.
    """
    import types

    import server
    import supervisor.workers as w

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    alerts = _collect_alerts(monkeypatch, 13)

    wall = 1_700_000_000.0
    clock = _FakeServerClock(wall=wall, mono=42.0)  # a freshly booted host
    monkeypatch.setattr(server, "time", clock)
    monkeypatch.setattr(w, "_chat_agent", types.SimpleNamespace(
        _busy=True, _current_task_id="wedged-split", _last_activity_ts=wall - 100.0))
    stop = threading.Event()  # local per-test token
    try:
        # Liveness tick == the current monotonic reading: the loop is HEALTHY.
        server._start_supervisor_liveness_watchdog([clock.mono], stop)
        _wait_until(lambda: alerts)
    finally:
        _stop_watchdog(stop)
    assert alerts, "a wall-stale chat turn must still be detected under a monotonic tick"
    assert "wedged" in alerts[0][1]
    assert alerts[0][2]["task_id"] == "wedged-split"
    assert not any("stalled" in a[1] for a in alerts)  # the healthy tick raised nothing
