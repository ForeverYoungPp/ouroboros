"""Contracts for the remote transport-outage wait episode (net-resilience sprint).

Covers: typed classification of released pre-dispatch transport failures
(remote vs local provider), the one-physical-attempt-per-call contract, the
round-level wait episode (free redials, recovery, deterministic no-resend
terminal, direct/ephemeral fast failure, local-only fallback pass), the
owner-signal-interruptible sleep, and durable ``network_wait`` evidence.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import ouroboros.loop as loop_mod
import ouroboros.loop_transport as loop_transport
from ouroboros import usage_accounting as ua
from ouroboros.loop import run_llm_loop
from ouroboros.loop_llm_call import call_llm_with_retry, classify_llm_exception
from ouroboros.tools.registry import ToolRegistry


def _released_capture(provider: str = "openrouter", state: str = "released") -> ua.PhysicalAttemptCapture:
    return ua.PhysicalAttemptCapture(
        attempt_id="pa-test", model="test-model", provider=provider, state=state,
        candidate_measurement_kind="opaque",
    )


def _typed_transport_exc(provider: str = "openrouter", exc_cls=httpx.ConnectError, state: str = "released"):
    """A provider-wrapper exception with typed transport provenance + custody capture."""
    cause = exc_cls("connection failed")
    try:
        raise RuntimeError("Connection error.") from cause
    except RuntimeError as exc:
        exc.physical_attempt_capture = _released_capture(provider=provider, state=state)
        return exc


def _read_network_wait_events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == "network_wait"]


# --------------------------------------------------------------- classification

def test_released_remote_transport_failure_classifies_transport_unavailable():
    result = classify_llm_exception(_typed_transport_exc())
    assert result.kind == "transport_unavailable"
    assert result.retry_same_request is True


def test_released_connect_timeout_now_classifies_transport_unavailable():
    """Contract flip pinned explicitly: a RELEASED typed ConnectTimeout used to
    ride the generic "timeout" marker into provider_transient; the typed custody
    fact now wins."""
    exc = httpx.ConnectTimeout("connect timed out")
    exc.physical_attempt_capture = _released_capture()
    assert classify_llm_exception(exc).kind == "transport_unavailable"


def test_released_local_provider_failure_stays_generic():
    """A stopped LOCAL model server is not a network outage worth waiting out."""
    result = classify_llm_exception(_typed_transport_exc(provider="local"))
    assert result.kind != "transport_unavailable"


def test_dispatched_capture_stays_provider_outcome_unknown():
    result = classify_llm_exception(_typed_transport_exc(state="dispatched"))
    assert result.kind == "provider_outcome_unknown"
    assert result.retry_same_request is False


def test_released_capture_without_typed_transport_cause_stays_generic():
    """Preparation failures release custody too but carry no typed transport fact."""
    try:
        raise RuntimeError("payload preparation failed")
    except RuntimeError as exc:
        exc.physical_attempt_capture = _released_capture()
        assert classify_llm_exception(exc).kind != "transport_unavailable"


def test_connect_timeout_without_capture_keeps_legacy_transient_path():
    assert classify_llm_exception(httpx.ConnectTimeout("connect timed out")).kind == "provider_transient"


def test_transport_unavailable_is_not_a_transient_or_cooldown_kind():
    """§4e-1 negative membership pin: the kind lives in NO retry frozenset —
    no in-helper burst (_TRANSIENT_RETRY_KINDS) and no model cooldown
    (_COOLDOWN_ERROR_KINDS: the egress is shared, cooling a model is
    meaningless and would poison the fallback chain after recovery)."""
    from ouroboros.loop_llm_call import _COOLDOWN_ERROR_KINDS, _TRANSIENT_RETRY_KINDS

    assert "transport_unavailable" not in _TRANSIENT_RETRY_KINDS
    assert "transport_unavailable" not in _COOLDOWN_ERROR_KINDS


def test_review_actor_keeps_its_bounded_retry_contract():
    """Review custody consults the same classifier: released transport stays
    retryable (its existing 1-2 physical-send cap), and never inherits the
    round-level wait."""
    from ouroboros.review_custody import retryable_review_exception

    assert retryable_review_exception(_typed_transport_exc(), None) is True


# ------------------------------------------------ one physical attempt per call

class _RaisingLLM:
    def __init__(self, exc_factory):
        self.exc_factory = exc_factory
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        raise self.exc_factory()


def test_call_llm_with_retry_makes_exactly_one_attempt_on_transport_unavailable(tmp_path):
    llm = _RaisingLLM(_typed_transport_exc)
    usage = {}
    msg, _cost = call_llm_with_retry(
        llm, [{"role": "user", "content": "hi"}], "test-model", None, "low", 3,
        tmp_path, "t-one", 1, None, usage,
    )
    assert msg is None
    assert llm.calls == 1
    assert usage.get("_last_llm_error_kind") == "transport_unavailable"


def test_call_llm_with_retry_keeps_bounded_burst_for_local_released_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    llm = _RaisingLLM(lambda: _typed_transport_exc(provider="local"))
    usage = {}
    msg, _cost = call_llm_with_retry(
        llm, [{"role": "user", "content": "hi"}], "test-model", None, "low", 3,
        tmp_path, "t-local", 1, None, usage,
    )
    assert msg is None
    assert llm.calls > 1  # the legacy bounded burst, not the one-attempt return


# ----------------------------------------------------------- round-episode loop

class _FakeLoopLLM:
    def default_model(self):
        return "test-model"


def _loop_kwargs(tmp_path, registry, notes, llm=None):
    return dict(
        messages=[{"role": "user", "content": "go"}],
        tools=registry,
        llm=llm or _FakeLoopLLM(),
        drive_logs=tmp_path,
        emit_progress=notes.append,
        incoming_messages=queue.Queue(),
        task_id="t-wait",
        drive_root=tmp_path,
    )


def _transport_failing_call(fail_times: int, final_content: str = "done"):
    calls = {"n": 0, "routes": []}

    def fake_call(_llm, _messages, model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **kwargs):
        calls["n"] += 1
        calls["routes"].append((model, bool(kwargs.get("use_local"))))
        if calls["n"] <= fail_times:
            accumulated_usage["_last_llm_error_kind"] = "transport_unavailable"
            accumulated_usage["_last_llm_error"] = "Connection error."
            return None, 0.0
        accumulated_usage.pop("_last_llm_error_kind", None)
        return {"role": "assistant", "content": final_content}, 0.0

    return fake_call, calls


def test_transport_outage_waits_redials_free_rounds_and_recovers(tmp_path, monkeypatch):
    fake_call, calls = _transport_failing_call(fail_times=3)
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "1")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    notes = []
    result, usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    # Recovery, not a round-limit forced finalization: the 3 redials were FREE
    # rounds (MAX_ROUNDS=1), and the recovered dispatch finished the task.
    assert result == "done"
    assert usage.get("reason_code") is None
    assert calls["n"] == 4
    assert len(sleeps) == 3
    assert all(0.0 < sec <= 60.0 for sec in sleeps)
    assert sleeps == sorted(sleeps)  # backoff grows monotonically toward the cap

    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases[0] == "entered"
    assert phases.count("waiting") == 3
    assert phases[-1] == "recovered"
    assert any("provider connection" in note for note in notes)  # first note, immediate
    assert any("restored" in note for note in notes)  # mandatory recovery note


@pytest.mark.parametrize("flag", ["is_direct_chat", "is_ephemeral_turn"])
def test_responsive_turns_fail_fast_without_waiting(tmp_path, monkeypatch, flag):
    fake_call, calls = _transport_failing_call(fail_times=99)
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    setattr(registry._ctx, flag, True)
    notes = []
    result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert calls["n"] == 1  # one honest attempt, zero redials, zero waiting
    assert sleeps == []
    assert usage.get("execution_status") == "infra_failed"
    assert usage.get("reason_code") == "provider_unavailable"
    assert trace.get("forced_finalization", {}).get("source") == "transport_unavailable_no_resend"
    # Honest zero-wait terminal text: this turn never waited, and must not claim it did.
    assert "fails fast" in result
    assert "waited and redialed" not in result
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases == ["entered", "ended"]


def test_deadline_bounds_wait_with_one_last_free_redial_then_no_resend(tmp_path, monkeypatch):
    fake_call, calls = _transport_failing_call(fail_times=99)
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)

    def _chain_must_not_run(**_kwargs):
        raise AssertionError("remote fallback chain must not dial during a transport outage")

    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", _chain_must_not_run)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "other/model")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    from ouroboros.config import get_finalization_grace_sec

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=get_finalization_grace_sec() + 8)
    registry._ctx.task_metadata = {"deadline_at": deadline.isoformat()}
    notes = []
    result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    # Bounded by the deadline: a couple of redials, the last one granted just
    # before the admission window closes, then the deterministic no-resend
    # terminal — never a forced-final provider call, never deadline_local.
    assert calls["n"] >= 2
    assert calls["n"] == len(sleeps) + 1  # every dispatch after the first followed a wait
    assert usage.get("execution_status") == "infra_failed"
    assert usage.get("reason_code") == "provider_unavailable"
    assert trace.get("forced_finalization", {}).get("source") == "transport_unavailable_no_resend"
    assert "waited and redialed" in result  # honest waited-out terminal text
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases[-1] == "ended"


def test_deadline_refusal_during_episode_takes_transport_no_resend_terminal(tmp_path, monkeypatch):
    """The admission gate overwriting the mutable kind with deadline_exhausted
    must not fork the terminal story: the episode's latched cause wins (no
    [DEADLINE] forced provider call)."""
    calls = {"n": 0}

    def fake_call(_llm, _messages, _model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            accumulated_usage["_last_llm_error_kind"] = "transport_unavailable"
        else:
            accumulated_usage["_last_llm_error_kind"] = "deadline_exhausted"
        return None, 0.0

    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep", lambda _sec, _wake: False)
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    notes = []
    _result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    assert calls["n"] == 2  # the refused redial is terminal; no third (forced) call
    assert usage.get("execution_status") == "infra_failed"
    assert usage.get("reason_code") == "provider_unavailable"
    assert trace.get("forced_finalization", {}).get("source") == "transport_unavailable_no_resend"


def test_scheduled_swarm_handoff_stays_truthful_on_transport_terminal(tmp_path, monkeypatch):
    """An ephemeral router turn fails fast on an outage, but the requested
    managed work was already durably admitted: the no-resend terminal stamp
    must not clobber the router's deliberate execution_status/reason_code
    clear — the successful handoff stays truthful."""
    calls = {"n": 0}

    def fake_call(_llm, _messages, _model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **_k):
        calls["n"] += 1
        accumulated_usage["_last_llm_error_kind"] = "transport_unavailable"
        accumulated_usage["_last_llm_error"] = "Connection error."
        # Mirror _record_llm_call_error's stamps so the test proves the router
        # pop clears them rather than them never having been set.
        accumulated_usage.update(execution_status="infra_failed", reason_code="llm_api_error")
        return None, 0.0

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.is_ephemeral_turn = True
    registry._ctx.task_metadata = {"force_plan": True, "force_plan_source": "swarm"}
    registry._ctx._swarm_handoff_attempt = {
        "task_id": "swarm-task-1",
        "routing_token": "route-token",
        "status": "scheduled",
        "reason": "",
        "response": "OK: task swarm-task-1 accepted and durably scheduled",
    }
    notes = []
    result, usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert "Swarm admitted managed task swarm-task-1" in result
    assert calls["n"] == 1  # ephemeral turn: fast fail, no redials
    assert usage.get("execution_status") is None
    assert usage.get("reason_code") is None


def test_outage_first_observed_mid_chain_latches_episode_and_recovers(tmp_path, monkeypatch):
    """Primary fails generically (429-class), the chain walks, and a REMOTE
    candidate dies pre-dispatch: the post-chain reconcile must latch an episode
    from the FRESH kind — wait, redial, recover — instead of the generic
    terminal dialing a forced-final call over the proven-dead egress."""
    calls = {"n": 0}

    def fake_call(_llm, _messages, _model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            accumulated_usage["_last_llm_error_kind"] = "provider_transient"
            return None, 0.0
        accumulated_usage.pop("_last_llm_error_kind", None)
        return {"role": "assistant", "content": "done"}, 0.0

    chain_calls = {"n": 0}

    def chain_breaks_on_transport(**kwargs):
        chain_calls["n"] += 1
        kwargs["accumulated_usage"]["_last_llm_error_kind"] = "transport_unavailable"
        return (
            None, kwargs["active_model"], kwargs["active_use_local"],
            kwargs["context_fit_plan"], kwargs["active_context_mode"],
        )

    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", chain_breaks_on_transport)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    notes = []
    result, usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    assert result == "done"
    assert chain_calls["n"] == 1
    assert calls["n"] == 2  # the redial went back to the primary and recovered
    assert len(sleeps) == 1
    assert usage.get("reason_code") is None
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases[0] == "entered"
    assert phases[-1] == "recovered"


def test_mid_chain_outage_on_direct_chat_fails_fast_no_resend(tmp_path, monkeypatch):
    """Same mid-chain-first outage on a direct-chat turn: the fresh episode is
    wait-ineligible — fast honest no-resend terminal, no forced-final call."""
    calls = {"n": 0}

    def fake_call(_llm, _messages, _model, _tools, _effort, _max_retries, _drive_logs,
                  _task_id, _round_idx, _event_queue, accumulated_usage, *_a, **_k):
        calls["n"] += 1
        accumulated_usage["_last_llm_error_kind"] = "provider_transient"
        return None, 0.0

    chain_calls = {"n": 0}

    def chain_breaks_on_transport(**kwargs):
        chain_calls["n"] += 1
        kwargs["accumulated_usage"]["_last_llm_error_kind"] = "transport_unavailable"
        return (
            None, kwargs["active_model"], kwargs["active_use_local"],
            kwargs["context_fit_plan"], kwargs["active_context_mode"],
        )

    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", chain_breaks_on_transport)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.is_direct_chat = True
    notes = []
    result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert calls["n"] == 1  # one primary attempt, no forced-final provider call
    assert chain_calls["n"] == 1
    assert sleeps == []
    assert usage.get("execution_status") == "infra_failed"
    assert usage.get("reason_code") == "provider_unavailable"
    assert trace.get("forced_finalization", {}).get("source") == "transport_unavailable_no_resend"
    assert "fails fast" in result
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases == ["entered", "ended"]


def test_exact_model_route_waits_and_redials_its_own_pin(tmp_path, monkeypatch):
    """Q13: an exact route waits and redials its OWN pinned model — the chain
    never runs (even with a configured local fallback) and recovery adopts the
    pinned route's response."""
    fake_call, calls = _transport_failing_call(fail_times=2)
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)

    def _chain_must_not_run(**_kwargs):
        raise AssertionError("exact_model_route must never walk the fallback chain")

    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", _chain_must_not_run)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "other/model")
    monkeypatch.setenv("USE_LOCAL_FALLBACK", "true")
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.exact_model_route = True
    notes = []
    result, _usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert result == "done"
    assert calls["n"] == 3
    assert len(sleeps) == 2
    assert all(0.0 < sec <= 60.0 for sec in sleeps)
    assert {route[0] for route in calls["routes"]} == {"test-model"}  # only the pin dialed


def test_local_fallback_pass_adopts_local_route_when_configured(tmp_path, monkeypatch):
    fake_call, calls = _transport_failing_call(fail_times=99)
    chain_calls = {"n": 0}

    def fake_chain(**kwargs):
        chain_calls["n"] += 1
        return (
            {"role": "assistant", "content": "local-ok"}, "local/candidate", True,
            kwargs["context_fit_plan"], kwargs["active_context_mode"],
        )

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", fake_chain)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "local/candidate")
    monkeypatch.setenv("USE_LOCAL_FALLBACK", "true")
    notes = []
    result, _usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    assert result == "local-ok"
    assert chain_calls["n"] == 1  # Q4: the LOCAL chain dialed, exactly once
    events = _read_network_wait_events(tmp_path)
    assert [row["phase"] for row in events] == ["entered", "ended"]
    assert events[-1].get("detail") == "local_fallback_adopted"


def test_failed_local_pass_keeps_remote_cause_and_runs_at_most_once(tmp_path, monkeypatch):
    """A failed local walk overwrites the mutable error kind, but the latched
    remote cause keeps the episode waiting — and the local pass never re-dials."""
    fake_call, calls = _transport_failing_call(fail_times=3)
    chain_calls = {"n": 0}

    def failing_chain(**kwargs):
        chain_calls["n"] += 1
        kwargs["accumulated_usage"]["_last_llm_error_kind"] = "provider_error"
        return (
            None, kwargs["active_model"], kwargs["active_use_local"],
            kwargs["context_fit_plan"], kwargs["active_context_mode"],
        )

    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", failing_chain)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "local/candidate")
    monkeypatch.setenv("USE_LOCAL_FALLBACK", "true")
    notes = []
    result, _usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    assert result == "done"  # the episode kept waiting and recovered
    assert chain_calls["n"] == 1  # one local pass per episode
    assert calls["n"] == 4
    assert len(sleeps) == 3
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases[-1] == "recovered"


def _unknown_outcome_chain():
    chain_calls = {"n": 0}

    def failing_chain(**kwargs):
        chain_calls["n"] += 1
        kwargs["accumulated_usage"]["_last_llm_error_kind"] = "provider_outcome_unknown"
        return (
            None, kwargs["active_model"], kwargs["active_use_local"],
            kwargs["context_fit_plan"], kwargs["active_context_mode"],
        )

    return failing_chain, chain_calls


def test_failed_local_pass_with_unknown_outcome_keeps_episode_waiting(tmp_path, monkeypatch):
    """Hardening for the refuted local-pass finding: a local pass that dies
    with provider_outcome_unknown overwrites the mutable kind, but the latched
    remote cause keeps the episode waiting until the egress recovers."""
    fake_call, calls = _transport_failing_call(fail_times=3)
    failing_chain, chain_calls = _unknown_outcome_chain()
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", failing_chain)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "local/candidate")
    monkeypatch.setenv("USE_LOCAL_FALLBACK", "true")
    notes = []
    result, _usage, _trace = run_llm_loop(**_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes))

    assert result == "done"  # the episode kept waiting and recovered
    assert chain_calls["n"] == 1  # one local pass per episode
    assert calls["n"] == 4
    assert len(sleeps) == 3
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases[-1] == "recovered"


def test_unknown_outcome_local_pass_then_deadline_takes_transport_no_resend(tmp_path, monkeypatch):
    """Same shape, but the deadline expires while the egress stays dead: the
    terminal must key on the episode's latched cause — transport no-resend —
    not on the local pass's provider_outcome_unknown overwrite."""
    fake_call, calls = _transport_failing_call(fail_times=99)
    failing_chain, chain_calls = _unknown_outcome_chain()
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)
    monkeypatch.setattr(loop_mod, "_run_cross_model_fallback_chain", failing_chain)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "local/candidate")
    monkeypatch.setenv("USE_LOCAL_FALLBACK", "true")
    from ouroboros.config import get_finalization_grace_sec

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=get_finalization_grace_sec() + 8)
    registry._ctx.task_metadata = {"deadline_at": deadline.isoformat()}
    notes = []
    _result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))

    assert chain_calls["n"] == 1
    assert calls["n"] >= 2
    assert usage.get("execution_status") == "infra_failed"
    assert usage.get("reason_code") == "provider_unavailable"
    assert trace.get("forced_finalization", {}).get("source") == "transport_unavailable_no_resend"


class _FlakyChatLLM:
    """Raises a typed released transport failure, then answers."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def default_model(self):
        return "test-model"

    def chat(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _typed_transport_exc()
        return (
            {"role": "assistant", "content": "done"},
            {"prompt_tokens": 1, "completion_tokens": 1},
        )


def test_end_to_end_episode_through_real_retry_helper(tmp_path, monkeypatch):
    """Classification -> one physical attempt per call -> wait episode -> recovery,
    with the durable exception-class evidence riding the existing llm_api_error rows."""
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    llm = _FlakyChatLLM(fail_times=2)
    notes = []
    result, _usage, _trace = run_llm_loop(
        **_loop_kwargs(tmp_path, ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), notes, llm=llm))

    assert result == "done"
    assert llm.calls == 3  # one physical attempt per invocation, two free redials
    assert len(sleeps) == 2
    phases = [row["phase"] for row in _read_network_wait_events(tmp_path)]
    assert phases == ["entered", "waiting", "waiting", "recovered"]
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines() if line.strip()]
    api_errors = [row for row in rows if row.get("type") == "llm_api_error"]
    assert api_errors and all(row.get("error_kind") == "transport_unavailable" for row in api_errors)


# ------------------------------------------------------- interruptible sleeping

def test_interruptible_sleep_reacts_to_owner_signal_within_slices():
    incoming = queue.Queue()
    wake = lambda: not incoming.empty()  # noqa: E731
    threading.Timer(0.2, lambda: incoming.put("owner message")).start()
    start = time.monotonic()
    interrupted = loop_transport.interruptible_wait_sleep(30.0, wake)
    elapsed = time.monotonic() - start
    assert interrupted is True
    assert elapsed < 5.0  # slice-level reaction, not the full sleep


def test_interruptible_sleep_completes_quiet_short_sleep():
    assert loop_transport.interruptible_wait_sleep(0.05, lambda: False) is False


def test_wait_step_caps_sleep_at_note_interval_for_low_idle_timeouts(tmp_path, monkeypatch):
    """Owner-lowered idle timeouts shrink the sleep so waiting notes keep the
    idle rail alive (effective interval = min(constant, idle/2))."""
    sleeps = []
    monkeypatch.setattr(loop_transport, "interruptible_wait_sleep",
                        lambda sec, _wake: (sleeps.append(sec), False)[1])
    monkeypatch.setattr(loop_transport, "get_task_idle_timeout_sec", lambda: 60)
    episode = loop_transport.TransportWaitEpisode(
        started_monotonic=time.monotonic(), wait_iterations=10,
    )
    tools = SimpleNamespace(_ctx=SimpleNamespace(task_metadata={}, task_attempt=None))
    notes = []
    redial = loop_transport.transport_wait_step(
        episode, tools=tools, error_kind="transport_unavailable",
        drive_root=None, drive_logs=tmp_path, task_id="t-idle", model="m",
        emit_progress=notes.append, incoming_messages=None, owner_msg_seen=set(),
    )
    assert redial is True
    assert sleeps == [30.0]  # min(60s backoff cap, 60/2 note interval)
    assert len(notes) == 1  # the periodic note fired for the lowered interval
