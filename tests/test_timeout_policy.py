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
