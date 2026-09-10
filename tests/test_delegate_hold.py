"""Incident-shaped contracts for the unknown-provider hold (nanny-leaf D1-min).

A configured-session nanny whose metered round dies ``provider_outcome_unknown``
while EXACTLY one delegated leaf is alive used to hold on the LEAF (zero provider
calls) and resume with a wake-bearing NEW round, never resending the unknown
request. The leaf's LIVENESS PROBE was a read-only engine poll, and that poll
retired with the Claudexor gateway: this build can no longer prove a live leaf,
so a NEW hold never latches (fail-closed) and every unknown takes today's
no-resend terminal.

What still holds is the DURABLE side: a latch written by an older build (worker
crash adoption) resumes its successor from the hold — on a wake, on owner input,
or through the no-call control terminal — and an unreadable latch fails closed.
The terminal cleanup (leaf cancellation) fires only on terminals.
"""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

import ouroboros.delegate_hold as delegate_hold
import ouroboros.loop as loop_mod
from ouroboros import delegate_custody as custody
from ouroboros.delegate_supervision import read_unknown_hold, write_unknown_hold
from ouroboros.loop import run_llm_loop
from ouroboros.tools.registry import ToolRegistry


def _read_hold_events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == "delegate_hold"]


def _configured_registry(tmp_path, task_id="t-hold"):
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = task_id
    registry._ctx.exact_model_route = True
    registry._ctx.task_metadata = {"configured_subagent": {"config_fingerprint": "fp"}}
    return registry


def _start_leaf(tmp_path, task_id="t-hold", run_id="run-leaf"):
    custody._CUSTODY.pop(run_id, None)
    row = custody.RunCustody(run_id=run_id, task_id=task_id, route_id="claude", model="m")
    assert custody.record_started(tmp_path, row)
    return run_id


@pytest.fixture(autouse=True)
def _quiet_probe(monkeypatch):
    """No test in this file may touch a real daemon or cancel a real leaf: the
    owned gateway is faked, any engine poll is inert, and releases are recorded
    instead of executed."""
    import ouroboros.claudexor_daemon as daemon_mod
    import ouroboros.delegate_progress as progress_mod

    monkeypatch.setattr(daemon_mod, "ensure_owned_gateway",
                        lambda **_k: SimpleNamespace(close=lambda: None), raising=False)
    monkeypatch.setattr(
        progress_mod, "bounded_poll",
        lambda _gw, _run, _sec, **_k: {"summary": {"state": "running"}, "lastSeq": 1},
    )
    released = []
    monkeypatch.setattr(custody, "release_task_runs", lambda root, tid: released.append(tid))
    yield released


def _loop_kwargs(tmp_path, registry, notes):
    return dict(
        messages=[{"role": "user", "content": "supervise"}],
        tools=registry,
        llm=SimpleNamespace(default_model=lambda: "test-model"),
        drive_logs=tmp_path,
        emit_progress=notes.append,
        incoming_messages=queue.Queue(),
        task_id=str(registry._ctx.task_id),
        drive_root=tmp_path,
    )


def _unknown_then_check_call(check):
    calls = {"n": 0}

    def fake_call(_llm, messages, _model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            accumulated_usage["_last_llm_error_kind"] = "provider_outcome_unknown"
            accumulated_usage.update(execution_status="infra_failed", reason_code="llm_api_error")
            return None, 0.0
        return check(messages, accumulated_usage)

    return fake_call, calls


def test_terminal_leaf_never_enters_hold(tmp_path, monkeypatch, _quiet_probe):
    import ouroboros.delegate_progress as progress_mod

    monkeypatch.setattr(
        progress_mod, "bounded_poll",
        lambda _gw, _run, _sec, **_k: {"summary": {"state": "succeeded"}},
    )
    monkeypatch.setattr(delegate_hold, "supervised_wait",
                        lambda *_a, **_k: pytest.fail("terminal leaf must not hold"))
    fake_call, calls = _unknown_then_check_call(lambda *_: pytest.fail("no second dial"))
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = _configured_registry(tmp_path)
    _start_leaf(tmp_path)
    notes = []
    _r, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert calls["n"] == 1
    assert usage.get("execution_status") == "infra_failed"
    assert trace.get("forced_finalization", {}).get("source") == "provider_outcome_unknown_no_resend"
    assert _read_hold_events(tmp_path) == []


def test_generic_task_and_multi_run_never_hold(tmp_path, monkeypatch, _quiet_probe):
    monkeypatch.setattr(delegate_hold, "supervised_wait",
                        lambda *_a, **_k: pytest.fail("ineligible shapes must not hold"))
    fake_call, calls = _unknown_then_check_call(lambda *_: pytest.fail("no second dial"))
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)

    # Generic task: no configured_subagent snapshot.
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = "t-generic"
    _start_leaf(tmp_path, task_id="t-generic", run_id="run-g")
    _r, usage, _t = run_llm_loop(**_loop_kwargs(tmp_path, registry, []))
    assert calls["n"] == 1 and usage.get("execution_status") == "infra_failed"

    # Configured but TWO live leaves.
    calls["n"] = 0
    registry2 = _configured_registry(tmp_path, task_id="t-multi")
    _start_leaf(tmp_path, task_id="t-multi", run_id="run-m1")
    _start_leaf(tmp_path, task_id="t-multi", run_id="run-m2")
    _r, usage2, _t = run_llm_loop(**_loop_kwargs(tmp_path, registry2, []))
    assert calls["n"] == 1 and usage2.get("execution_status") == "infra_failed"
    assert _read_hold_events(tmp_path) == []


def test_recovered_latch_reenters_hold_before_any_dispatch(tmp_path, monkeypatch, _quiet_probe):
    """The durable latch (worker-crash adoption) parks the successor's FIRST
    round in the hold before any LLM call — sol #5 contract."""
    wake_payload = {"status": "attention", "run_id": "run-leaf", "supervision_wake_id": "w3"}
    order = []
    monkeypatch.setattr(
        delegate_hold, "supervised_wait",
        lambda _ctx, _run: order.append("wait") or json.dumps(wake_payload),
    )
    monkeypatch.setattr(delegate_hold, "acknowledge_pending_wake", lambda *_a, **_k: True)

    def fake_call(_llm, messages, *_a, **_k):
        order.append("dispatch")
        assert "[DELEGATED LEAF WAKE / UNKNOWN-HOLD RESUME]" in messages[-1]["content"]
        return {"role": "assistant", "content": "resumed"}, 0.0

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = _configured_registry(tmp_path)
    _start_leaf(tmp_path)
    write_unknown_hold(registry._ctx, "run-leaf", {
        "run_id": "run-leaf", "entered_at": "2026-08-30T00:00:00Z", "hold_cycles": 1,
    })
    result, _u, _t = run_llm_loop(**_loop_kwargs(tmp_path, registry, []))
    assert result == "resumed"
    assert order == ["wait", "dispatch"]


def test_refused_probe_and_state_less_payload_never_hold(tmp_path, monkeypatch, _quiet_probe):
    """A daemon refusal or a state-less payload is not evidence of a live leaf
    (grok #1/#2, fable F3): the probe fails closed to today's terminal."""
    import ouroboros.delegate_progress as progress_mod

    monkeypatch.setattr(delegate_hold, "supervised_wait",
                        lambda *_a, **_k: pytest.fail("refused probe must not hold"))
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)

    def raising_poll(_gw, _run, _sec, **_k):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(progress_mod, "bounded_poll", raising_poll)
    fake_call, calls = _unknown_then_check_call(lambda *_: pytest.fail("no second dial"))
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    registry = _configured_registry(tmp_path, task_id="t-refused")
    _start_leaf(tmp_path, task_id="t-refused", run_id="run-r1")
    _r, usage, _t = run_llm_loop(**_loop_kwargs(tmp_path, registry, []))
    assert calls["n"] == 1 and usage.get("execution_status") == "infra_failed"

    monkeypatch.setattr(progress_mod, "bounded_poll", lambda _gw, _run, _sec, **_k: {})
    calls["n"] = 0
    registry2 = _configured_registry(tmp_path, task_id="t-stateless")
    _start_leaf(tmp_path, task_id="t-stateless", run_id="run-r2")
    _r, usage2, _t = run_llm_loop(**_loop_kwargs(tmp_path, registry2, []))
    assert calls["n"] == 1 and usage2.get("execution_status") == "infra_failed"
    assert _read_hold_events(tmp_path) == []


def test_owner_input_resumes_without_wait(tmp_path, monkeypatch, _quiet_probe):
    """An owner message drained at the round top IS material new input (sol
    HIGH #4): the hold resumes on it without entering supervised_wait."""
    monkeypatch.setattr(delegate_hold, "supervised_wait",
                        lambda *_a, **_k: pytest.fail("owner input must resume without waiting"))

    def fake_call(_llm, messages, *_a, **_k):
        assert any("please integrate" in str(m.get("content")) for m in messages)
        return {"role": "assistant", "content": "resumed-on-owner-input"}, 0.0

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = _configured_registry(tmp_path)
    _start_leaf(tmp_path)
    write_unknown_hold(registry._ctx, "run-leaf", {
        "run_id": "run-leaf", "entered_at": "2026-08-30T00:00:00Z", "hold_cycles": 1,
    })
    kwargs = _loop_kwargs(tmp_path, registry, [])
    # The transcript tail is LONGER than the owner message: an appended short
    # message must still read as new input (final-pair fable F1 — a naive
    # length-sum signature missed exactly this common shape).
    kwargs["messages"] = [
        {"role": "user", "content": "supervise"},
        {"role": "assistant", "content": "long tool result " * 50},
    ]
    kwargs["incoming_messages"].put("please integrate")
    result, _u, _t = run_llm_loop(**kwargs)
    assert result == "resumed-on-owner-input"
    details = [row.get("detail") for row in _read_hold_events(tmp_path) if row["phase"] == "resumed"]
    assert "owner_input" in details


def test_recovered_latch_control_wake_stays_no_call(tmp_path, monkeypatch, _quiet_probe):
    """Fable F1: after crash recovery the usage record is fresh — a control
    wake must still exit through the no-call unknown terminal, never a paid
    [PROVIDER_UNAVAILABLE] forced final."""
    monkeypatch.setattr(
        delegate_hold, "supervised_wait",
        lambda _ctx, _run: json.dumps({
            "status": "progress", "wake_events": [{"type": "cancellation_intent"}],
        }),
    )
    monkeypatch.setattr(loop_mod, "call_llm_with_retry",
                        lambda *_a, **_k: pytest.fail("Stop after recovery must not dial"))
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = _configured_registry(tmp_path)
    _start_leaf(tmp_path)
    write_unknown_hold(registry._ctx, "run-leaf", {
        "run_id": "run-leaf", "entered_at": "2026-08-30T00:00:00Z", "hold_cycles": 1,
    })
    _r, _u, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, []))
    assert trace.get("forced_finalization", {}).get("source") == "provider_outcome_unknown_no_resend"


def test_latch_survives_real_supervised_wait_state_reset(tmp_path, monkeypatch, _quiet_probe):
    """Final-pair CRITICAL (sol #1 / fable F2): the REAL supervised_wait's
    _load_state rebuild for a new run id must carry the durable latch — a
    worker crash mid-wait must find it, or the successor resends."""
    import ouroboros.delegate_supervision as sup

    registry = _configured_registry(tmp_path, task_id="t-reset")
    _start_leaf(tmp_path, task_id="t-reset", run_id="run-reset")
    write_unknown_hold(registry._ctx, "run-reset", {
        "run_id": "run-reset", "entered_at": "2026-08-30T00:00:00Z", "hold_cycles": 1,
    })

    def wait_once(_ctx, _run, _sec, _seq):
        # Mid-wait crash shape: the durable file must STILL carry the latch
        # after supervised_wait's entry persisted its (rebuilt) state.
        data = json.loads(sup._state_path(registry._ctx).read_text())
        assert data.get("unknown_provider_hold", {}).get("run_id") == "run-reset"
        return json.dumps({"status": "succeeded", "run_id": "run-reset", "last_seq": 2})

    raw = sup.supervised_wait(registry._ctx, "run-reset", wait_once=wait_once)
    assert json.loads(raw).get("status") == "succeeded"
    assert read_unknown_hold(registry._ctx).get("run_id") == "run-reset"


def test_unreadable_latch_fails_closed_to_terminal(tmp_path, monkeypatch, _quiet_probe):
    """Final-pair sol #2: an existing-but-corrupt latch file must not read as
    'no hold' — dispatching there could resend the unknown request."""
    import ouroboros.delegate_supervision as sup

    registry = _configured_registry(tmp_path, task_id="t-corrupt")
    _start_leaf(tmp_path, task_id="t-corrupt", run_id="run-c")
    path = sup._state_path(registry._ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt json", encoding="utf-8")
    monkeypatch.setattr(delegate_hold, "supervised_wait",
                        lambda *_a, **_k: pytest.fail("corrupt latch must not wait"))
    monkeypatch.setattr(loop_mod, "call_llm_with_retry",
                        lambda *_a, **_k: pytest.fail("corrupt latch must not dispatch"))
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    _r, _u, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, []))
    assert trace.get("forced_finalization", {}).get("source") == "provider_outcome_unknown_no_resend"
    details = [row.get("detail") for row in _read_hold_events(tmp_path) if row["phase"] == "ended"]
    assert "latch_unreadable" in details


