"""Charter pre-start contracts: definite refusals terminal at $0, ambiguity wakes.

Ported f9356572 test contracts, rewritten for the pre-start seam (sprint plan
SS8): a typed refusal that provably left no run behind ends the child unrun and
typed before any model round; anything ambiguous wakes the model instead.
"""

import json
from types import SimpleNamespace

import pytest

from tests.test_available_subagents_runtime import _session_row, _settings, _snapshot


@pytest.mark.parametrize(("reason", "extra"), [
    ("credential_pool_exhausted", {}),
    ("subscription_window_exhausted", {}),
    ("daemon_unreachable", {}),
    ("access_profile_unsupported:workspace_write", {}),
    ("route_disabled", {}),
    # Producer-marker cases (P2 class fix): the pre-POST refusal sites stamp
    # their own definitely_unrun verdict, so real refusals outside the
    # engine-reason frozenset still terminal at $0.
    ("task_deadline_expired", {"definitely_unrun": True}),
    ("start_request_row_unwritable", {"definitely_unrun": True}),
])
def test_definite_configured_session_start_refusal_terminalizes_before_llm(
    monkeypatch, tmp_path, reason, extra,
):
    # Ported f9356572 contract (rewritten for the pre-start seam): a typed
    # refusal that provably left no run behind ends the child unrun and typed
    # at $0 — bootstrap returns an empty wake and the agent's terminal gate
    # takes over, so no model round ever exists.
    import ouroboros.subagent_runtime as runtime
    from ouroboros.subagent_bootstrap import bootstrap_before_context

    monkeypatch.setattr(runtime, "exact_start", lambda _ctx, _prompt, _spec: json.dumps({
        "status": "refused", "reason": reason, "reset_at": "2030-01-01T00:00:00Z",
        **extra,
    }))
    monkeypatch.setattr(
        runtime, "current_subagent_alternatives", lambda _exclude: [],
    )
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    ctx = SimpleNamespace(
        task_id="child-refused", drive_root=tmp_path,
        budget_drive_root=str(tmp_path), task_metadata={},
    )
    dispatch = SimpleNamespace(
        executor="harness", blocked=False,
        executor_resolution=SimpleNamespace(route=SimpleNamespace(route_id="codex")),
    )
    task = {"id": "child-refused", "configured_subagent": snapshot,
            "task_contract": {"objective": "Build"}}
    assert bootstrap_before_context(ctx, task, dispatch) == ""
    assert ctx._configured_startup_refusal["reason"] == reason
    assert ctx._configured_startup_refusal["reset_at"] == "2030-01-01T00:00:00Z"
    assert task["subagent_availability"]["route_kind"] == "agent_session"
    assert task["subagent_availability"]["host_fallback"] is False


@pytest.mark.parametrize("payload", [
    # A custody handle means a run may exist: never a $0 terminal.
    {"status": "refused", "reason": "credential_pool_exhausted", "run_id": "run-x"},
    {"status": "refused", "reason": "daemon_unreachable",
     "pending_invocation_id": "inv-1"},
    # An unknown refusal code errs toward the episode, not the terminal.
    {"status": "refused", "reason": "some_future_code"},
    # An uncustodied start IS a live run somewhere.
    {"status": "started_uncustodied", "run_id": ""},
    # Unparseable output proves nothing about the run's absence.
    "not-json-at-all",
])
def test_startup_refusal_classifier_preserves_ambiguous_wakes(
    monkeypatch, tmp_path, payload,
):
    # Ported f9356572 B4 contract (in spirit): a false "spent nothing" terminal
    # over a possibly-live run is the one direction the classification must
    # never fail toward — everything ambiguous wakes the model instead.
    import ouroboros.subagent_runtime as runtime
    from ouroboros.subagent_bootstrap import bootstrap_before_context

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(runtime, "exact_start", lambda _ctx, _prompt, _spec: raw)
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    ctx = SimpleNamespace(
        task_id="child-ambiguous", drive_root=tmp_path,
        budget_drive_root=str(tmp_path), task_metadata={},
    )
    dispatch = SimpleNamespace(
        executor="harness", blocked=False,
        executor_resolution=SimpleNamespace(route=SimpleNamespace(route_id="codex")),
    )
    task = {"id": "child-ambiguous", "configured_subagent": snapshot,
            "task_contract": {"objective": "Build"}}
    out = bootstrap_before_context(ctx, task, dispatch)
    assert out != ""
    parsed = json.loads(out)
    assert parsed["status"] == "configured_session_startup_fault"
    assert getattr(ctx, "_configured_startup_refusal", None) is None
    if isinstance(payload, dict) and payload.get("status") == "started_uncustodied":
        # A possibly-live run must also fence a false zero-run claim.
        assert ctx._configured_actor_bootstrap["physical_started"] is True

def test_blocked_session_bootstrap_terminals_unrun_with_alternatives(monkeypatch, tmp_path):
    # Charter D2 (owner 2026-08-28, N2=A): a route that is blocked AT DISPATCH
    # ends the child unrun and typed at $0 — no model episode, no metered
    # fallback. The bootstrap returns an empty wake and stashes the typed
    # refusal; agent._prepare_task_context turns it into the existing
    # executor-blocked terminal. The dc4c0204 non-empty wake retired with this.
    from ouroboros import delegate_custody as custody
    import ouroboros.subagent_runtime as runtime
    from ouroboros.subagent_bootstrap import bootstrap_before_context
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    alternatives = [{
        "subagent_id": "api-scout", "route_kind": "api_model",
        "target_id": "google/gemini-3.7-flash", "availability": "check_at_dispatch",
    }]
    monkeypatch.setattr(
        runtime, "current_subagent_alternatives", lambda _exclude: list(alternatives),
    )
    ctx = SimpleNamespace(
        task_id="child1",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_metadata={
            "root_task_id": "root",
            "parent_task_id": "root",
            "delegation_role": "subagent",
            "budget_drive_root": str(tmp_path),
        },
    )
    dispatch = SimpleNamespace(
        blocked=True,
        executor_resolution=SimpleNamespace(
            reason="subscription_window_exhausted", reset_at="2030-01-01T00:00:00Z",
        ),
    )
    task = {"id": "child1", "configured_subagent": snapshot}
    assert bootstrap_before_context(ctx, task, dispatch) == ""
    assert ctx._configured_startup_refusal == {
        "reason": "subscription_window_exhausted",
        "reset_at": "2030-01-01T00:00:00Z",
        "requested": "harness",
    }
    availability = task["subagent_availability"]
    assert {key: availability[key] for key in (
        "status", "reason", "reset_at", "alternatives", "host_fallback", "route_kind",
    )} == {
        "status": "unavailable",
        "reason": "subscription_window_exhausted",
        "reset_at": "2030-01-01T00:00:00Z",
        "alternatives": alternatives,
        "host_fallback": False,
        "route_kind": "agent_session",
    }
    # The frozen route/work-order authority still exists (recovery/economics
    # readers consume it), and the blocked fact is durable custody evidence.
    bootstrap = ctx._configured_actor_bootstrap
    assert bootstrap["selected_subagent_id"] == "session-builder"
    assert len(bootstrap["work_order_fingerprint"]) == 64
    rows = [json.loads(line) for line in custody.event_log_path(tmp_path).read_text().splitlines()]
    assert rows[-1]["type"] == "configured_subagent_startup_fault"
    assert rows[-1]["reason"] == "subscription_window_exhausted"
    assert rows[-1]["host_fallback"] is False
