from __future__ import annotations

import datetime as dt


def test_logical_review_window_is_narrowed_by_owner_deadline():
    from ouroboros.deadline_utils import logical_operation_timeout_sec

    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat()
    value = logical_operation_timeout_sec(300, deadline_at=deadline, fallback=2700)
    assert 0 < value <= 5


def test_logical_review_window_uses_transport_only_as_settlement_fallback():
    from ouroboros.deadline_utils import logical_operation_timeout_sec

    assert logical_operation_timeout_sec(None, fallback=17) == 17


def test_transport_timeout_is_narrowed_by_numeric_owner_deadline(monkeypatch):
    import ouroboros.deadline_utils as deadlines

    monkeypatch.setattr(deadlines.time, "time", lambda: 1000.0)
    assert deadlines.transport_timeout_with_deadline(90) == 90
    assert deadlines.transport_timeout_with_deadline(
        90, deadline_ts=1010.0, reserve_sec=3,
    ) == 7.0
    # A spent deadline stays a tiny bounded transport operation, never an
    # unbounded/default 90-second child process.
    assert deadlines.transport_timeout_with_deadline(90, deadline_ts=999.0) == 0.001


def test_bounded_engine_seconds_never_widens_explicit_zero():
    from ouroboros.deadline_utils import bounded_seconds

    assert bounded_seconds(0, default=300, maximum=3600) == 1
    assert bounded_seconds(0.001, default=300, maximum=3600) == 1
    assert bounded_seconds(1.2, default=300, maximum=3600) == 2
    assert bounded_seconds(None, default=300, maximum=3600) == 300


def test_main_llm_transport_preserves_anthropic_default_but_narrows_deadline(monkeypatch):
    import ouroboros.deadline_utils as deadlines
    from ouroboros.loop_llm_call import _main_transport_timeout

    monkeypatch.setattr(deadlines.time, "time", lambda: 1000.0)
    monkeypatch.setattr("ouroboros.loop_llm_call.get_finalization_grace_sec", lambda: 3)
    assert _main_transport_timeout("anthropic::claude-fable-5", None) == 120
    assert _main_transport_timeout("anthropic::claude-fable-5", 1010.0) == 7.0


def test_nested_logical_window_reserves_finalization_grace():
    from ouroboros.deadline_utils import logical_operation_timeout_sec

    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).isoformat()
    value = logical_operation_timeout_sec(None, deadline_at=deadline, fallback=2700, reserve_sec=3)
    assert 0 < value <= 7


def test_bounded_delegate_poll_passes_remaining_transport_window():
    from ouroboros.delegate_progress import bounded_poll

    class Gateway:
        def __init__(self):
            self.calls = []

        def get_run(self, run_id, *, timeout_sec=None):
            self.calls.append((run_id, timeout_sec))
            return {"summary": {"state": "succeeded"}}

    gateway = Gateway()
    detail = bounded_poll(gateway, "run-1", 10)
    assert detail["summary"]["state"] == "succeeded"
    assert gateway.calls[0][0] == "run-1"
    assert 0 < gateway.calls[0][1] <= 10


def test_strict_review_poll_does_not_raise_the_remaining_window_to_five_seconds():
    from ouroboros.delegate_progress import bounded_poll

    class Gateway:
        def __init__(self):
            self.timeout = None

        def get_run(self, _run_id, *, timeout_sec=None):
            self.timeout = timeout_sec
            return {"summary": {"state": "succeeded"}}

    gateway = Gateway()
    bounded_poll(gateway, "run-1", 0.001, strict=True)
    assert 0 < gateway.timeout <= 0.001
