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


def test_logical_review_window_does_not_widen_explicit_zero():
    from ouroboros.deadline_utils import logical_operation_timeout_sec

    assert logical_operation_timeout_sec(0, fallback=2700) == 1
    assert logical_operation_timeout_sec(-1, fallback=2700) == 1


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


def test_expiring_strict_review_poll_keeps_the_subsecond_bound():
    from ouroboros.delegate_progress import expiring_poll

    class Gateway:
        def __init__(self):
            self.timeout = None

        def get_run(self, _run_id, *, timeout_sec=None):
            self.timeout = timeout_sec
            return {"summary": {"state": "succeeded"}}

    gateway = Gateway()
    expiring_poll(gateway, "run-1", strict=True)
    assert 0 < gateway.timeout <= 0.001


def test_strict_poll_splits_http_phase_budget_and_recomputes_retry():
    import time
    from ouroboros.delegate_progress import bounded_poll

    class AtomicRace(Exception):
        code = "ENOENT"

    class Gateway:
        def __init__(self):
            self.timeouts = []

        def get_run(self, _run_id, *, timeout_sec=None):
            self.timeouts.append(timeout_sec)
            if len(self.timeouts) == 1:
                time.sleep(0.01)
                raise AtomicRace("/.git/objects/ab/tmp_obj_123")
            return {"summary": {"state": "succeeded"}}

    gateway = Gateway()
    bounded_poll(gateway, "run-1", 0.08, strict=True)
    assert 0 < gateway.timeouts[0] <= 0.08
    assert 0 < gateway.timeouts[1] < gateway.timeouts[0]


def test_strict_poll_phase_budget_is_bounded_in_wall_time():
    import pytest
    import time
    from ouroboros.delegate_progress import bounded_poll
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    class OneSlowPhase:
        def get_run(self, _run_id, *, timeout_sec=None):
            time.sleep(0.06)
            return {"summary": {"state": "succeeded"}}

    started = time.monotonic()
    bounded_poll(OneSlowPhase(), "run-1", 0.08, strict=True)
    assert time.monotonic() - started < 0.1

    class MultiPhaseStall:
        def get_run(self, _run_id, *, timeout_sec=None):
            time.sleep(timeout_sec * 4)
            return {"summary": {"state": "succeeded"}}

    started = time.monotonic()
    with pytest.raises(ClaudexorUnavailable, match="wall-clock bound"):
        bounded_poll(MultiPhaseStall(), "run-2", 0.08, strict=True)
    assert time.monotonic() - started < 0.1


def test_claudexor_bound_applies_to_connect_phase_too():
    import httpx
    from ouroboros.gateways import claudexor as cx

    calls = []

    class Recorder:
        def request(self, method, path, **kwargs):
            calls.append(kwargs)
            return httpx.Response(200, json={"id": "run-1", "summary": {}})

    gateway = cx.ClaudexorGateway(cx.DaemonEndpoint("127.0.0.1", 1, "token"))
    gateway.close()
    gateway._client = Recorder()
    gateway.get_run("run-1", timeout_sec=0.001)
    assert calls[-1]["timeout"].read == 0.001
    assert calls[-1]["timeout"].connect == 0.001


def test_main_round_call_propagates_task_attempt(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod

    captured = {}

    def fake_call(*args, **kwargs):
        captured.update(kwargs)
        captured["task_attempt"] = args[10].get("_task_attempt")
        return {"content": "ok"}, 0.0

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    tools = SimpleNamespace(
        _ctx=SimpleNamespace(task_attempt=7, task_metadata={}),
    )
    ctx = loop_mod._RoundModelCallContext(
        llm=object(), messages=[], tools=tools, context_fit_plan=None,
        active_model="model", tool_schemas=[], active_effort="high",
        max_retries=1, drive_logs=tmp_path / "logs", task_id="task",
        round_idx=1, event_queue=None, accumulated_usage={"_task_attempt": 7}, task_type="task",
        active_use_local=False, active_context_mode="max", drive_root=tmp_path,
    )
    loop_mod._dispatch_round_model(ctx, None, attempt_cap=1)
    assert captured["task_attempt"] == 7


def test_forced_finalization_transport_uses_full_grace_deadline(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod

    captured = {}

    def fake_call(*args, **kwargs):
        captured.update(kwargs)
        captured["task_attempt"] = args[10].get("_task_attempt")
        return {"content": "final"}, 0.0

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    ctx = loop_mod._RoundLimitContext(
        messages=[], llm=object(), active_model="model", active_effort="high",
        max_retries=1, drive_logs=tmp_path / "logs", task_id="task", round_idx=1,
        event_queue=None, accumulated_usage={"_task_attempt": 4}, task_type="task",
        active_use_local=False, max_rounds=1,
        tools=SimpleNamespace(_ctx=SimpleNamespace(task_attempt=4)),
    )
    ctx.deadline_ts = __import__("time").time() + 3
    assert loop_mod._call_forced_model_once(ctx) == "final"
    assert captured["task_attempt"] == 4
    assert captured["transport_reserve_sec"] == 0.0
    assert 0 < captured["deadline_ts"] - __import__("time").time() <= 3.1


def test_finalize_control_carries_original_grace_deadline(monkeypatch, tmp_path):
    import queue
    import time
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod
    from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, write_owner_message

    monkeypatch.setattr(loop_mod.task_pacing, "effective_finalization_reserve_sec", lambda _ctx: 3)
    before = time.time()
    assert write_owner_message(tmp_path, "deadline", "task", kind=KIND_FINALIZE_NOW)
    controls = loop_mod._drain_incoming_messages(
        [], queue.Queue(), tmp_path, "task", None, set(),
        owner_ctx=SimpleNamespace(task_attempt=1),
    )
    assert controls["finalize_now"] == "deadline"
    assert before + 3 <= controls["finalize_deadline_ts"] <= time.time() + 3


def test_forced_finalization_does_not_rebase_existing_grace(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod

    deadline = __import__("time").time() + 1
    ctx = loop_mod._RoundLimitContext(
        messages=[], llm=object(), active_model="model", active_effort="high",
        max_retries=1, drive_logs=tmp_path / "logs", task_id="task", round_idx=1,
        event_queue=None, accumulated_usage={}, task_type="task",
        active_use_local=False, max_rounds=1, deadline_ts=deadline,
        tools=SimpleNamespace(_ctx=SimpleNamespace()),
    )
    monkeypatch.setattr(loop_mod, "_finalize_forced_services", lambda *_args: None)
    monkeypatch.setattr(
        loop_mod, "_forced_swarm_router_result",
        lambda *_args: ("routed", {}, {}),
    )
    loop_mod._forced_final_answer(
        ctx, prompt="finish", fallback_text="fallback",
        reason_code="finalization_grace",
    )
    assert ctx.deadline_ts == deadline
