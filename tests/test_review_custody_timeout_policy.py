"""Focused timeout and retry regressions for review custody."""

from __future__ import annotations

import pytest


def test_spent_deadline_restart_reconciliation_gets_only_a_settlement_window(
    tmp_path, monkeypatch,
):
    import time
    from types import SimpleNamespace

    import ouroboros.config as config
    from ouroboros.review_custody import (
        prepare_frozen_review_reconciliation, run_custodied_review_slots,
    )
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    monkeypatch.setattr(config, "NESTED_SETTLEMENT_MARGIN_SEC", 0.2)
    attempt = SimpleNamespace(triad_raw_results=[{
        "slot_id": "slot-1", "model_id": "cursor/test", "status": "error",
        "operation_id": "op-existing", "operation_state": "in_flight",
        "late_result_pending": True, "pending_invocation_id": "inv-existing",
    }], scope_raw_result={})
    paid = []
    ctx = SimpleNamespace(_review_paid_stamp=lambda: paid.append("paid"))
    prepare_frozen_review_reconciliation(ctx, attempt)
    request = ReviewRequest(
        surface="multi_model_review", goal="review", task_id="task-spent-restart",
        retry_key="commit_review:spent-restart", reconcile_only=True,
        deadline_at="2000-01-01T00:00:00Z",
    )
    slot = ReviewSlot(
        slot_id="slot-1", model="cursor/test", route=ReviewRouteKind.AGENT_SESSION,
    )
    calls = []

    def recover(_slot, operation_id, retry_state, deadline, _checkpoint):
        calls.append((operation_id, dict(retry_state), deadline - time.monotonic()))
        return ReviewActorRecord(slot_id="slot-1", model="cursor/test", status="ok")

    [actor] = run_custodied_review_slots(
        request=request,
        slots=[slot],
        usage_ctx=ctx,
        task_id="task-spent-restart",
        usage_meta={"deadline_at": "2000-01-01T00:00:00Z"},
        review_usage_scope=UsageScope(
            drive_root=tmp_path, task_id="task-spent-restart",
        ),
        run_slot=recover,
        error_actor=lambda *_args, **_kwargs: None,
    )

    assert len(calls) == 1
    operation_id, retry_state, remaining = calls[0]
    assert operation_id == "op-existing"
    assert retry_state == {"pending_invocation_id": "inv-existing"}
    assert 0 < remaining <= 0.2
    assert paid == []
    assert actor.operation_id == "op-existing"
    assert actor.operation_state == "settled"


def test_reserved_operation_keeps_not_dispatched_identity_inside_reserve(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from ouroboros.review_custody import (
        reconcile_reserved_review_roster, run_custodied_review_slots,
    )
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    monkeypatch.setenv("OUROBOROS_FINALIZATION_GRACE_SEC", "120")
    reserved = {
        "multi_model_review": [{
            "slot_id": "slot-1", "model_id": "cursor/test", "route": "api_chat",
            "status": "in_flight", "operation_id": "op-reserved",
            "operation_state": "in_flight", "late_result_pending": True,
        }],
        "scope_review": [],
    }
    ctx = SimpleNamespace(
        _review_reserved_operations={"multi_model_review": {"slot-1": "op-reserved"}},
        _review_reserved_roster=reserved,
    )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="not_dispatched",
            error=error, operation_id=operation_id, operation_state=operation_state,
        )

    [actor] = run_custodied_review_slots(
        request=ReviewRequest(
            surface="multi_model_review", goal="review", task_id="reserve-only",
            retry_key="commit_review:reserve-only", deadline_at="2000-01-01T00:00:00Z",
        ),
        slots=[ReviewSlot(
            slot_id="slot-1", model="cursor/test", route=ReviewRouteKind.API_CHAT,
        )],
        usage_ctx=ctx, task_id="reserve-only",
        usage_meta={"deadline_at": "2000-01-01T00:00:00Z"},
        review_usage_scope=UsageScope(drive_root=tmp_path, task_id="reserve-only"),
        run_slot=lambda *_args: pytest.fail("reserve-only row dispatched"),
        error_actor=error_actor,
    )

    assert actor.operation_id == "op-reserved"
    assert actor.operation_state == "not_dispatched"
    ctx._last_triad_raw_results = [{
        "slot_id": actor.slot_id, "status": actor.status,
        "operation_id": actor.operation_id, "operation_state": actor.operation_state,
        "late_result_pending": False,
    }]
    ctx._last_scope_raw_results = []
    reconcile_reserved_review_roster(ctx, reserved)
    assert ctx._last_triad_raw_results[0]["operation_state"] == "not_dispatched"
    assert getattr(ctx, "_review_custody_lost", False) is False


def test_custody_lost_error_actor_remains_pending_for_plan_review(tmp_path):
    from ouroboros.review_substrate import (
        ReviewCoordinator, ReviewRequest, ReviewSlot,
    )

    coordinator = ReviewCoordinator(drive_root=tmp_path, usage_ctx=None)
    actor = coordinator._error_actor(
        ReviewRequest(surface="plan_review", goal="review", task_id="pending"),
        ReviewSlot(slot_id="slot-1", model="test/model"),
        "provider outcome is unknown",
        operation_id="op-1",
        operation_state="custody_lost",
    )

    assert actor.operation_state == "custody_lost"
    assert actor.late_result_pending is True


def test_failed_commit_roster_stamp_drops_unsent_process_local_reservation(
    monkeypatch,
):
    from types import SimpleNamespace

    from ouroboros.tools.parallel_review import _reserve_parallel_review_roster

    ctx = SimpleNamespace(_triad_withheld_seat_records=[])

    def fail_stamp(_ctx):
        raise RuntimeError("write-ahead unavailable")

    monkeypatch.setattr(
        "ouroboros.review_dispatch.stamp_review_paid_on_dispatch", fail_stamp,
    )
    with pytest.raises(RuntimeError, match="write-ahead unavailable"):
        _reserve_parallel_review_roster(
            ctx,
            {"row_plan": {
                "models": ["test/model"], "routes": ["api_chat"],
                "efforts": ["high"], "slot_ids": ["slot-1"],
            }},
            [],
        )

    assert getattr(ctx, "_review_reserved_roster", None) is None
    assert getattr(ctx, "_review_reserved_operations", {}) == {}


def test_spent_owner_window_does_not_stamp_zero_dispatch_commit_roster(
    monkeypatch,
):
    from types import SimpleNamespace

    from ouroboros.tools.parallel_review import _reserve_parallel_review_roster

    stamp_calls = []
    ctx = SimpleNamespace(
        _triad_withheld_seat_records=[],
        task_metadata={"deadline_at": "2000-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        "ouroboros.review_dispatch.stamp_review_paid_on_dispatch",
        lambda _ctx: stamp_calls.append("paid"),
    )

    _reserve_parallel_review_roster(
        ctx,
        {"row_plan": {
            "models": ["test/model"], "routes": ["api_chat"],
            "efforts": ["high"], "slot_ids": ["slot-1"],
        }},
        [],
    )

    assert stamp_calls == []
    assert ctx._review_reserved_roster["multi_model_review"][0]["operation_id"]
    assert ctx._review_reserved_operations["multi_model_review"]["slot-1"]


def test_binding_refusal_clears_commit_reconcile_mode_before_return(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from ouroboros.tools import git as git_tools

    ctx = SimpleNamespace(
        repo_dir=tmp_path,
        task_id="binding-refusal",
        task_metadata={},
        _review_resume_pending=True,
        _pending_review_attempt=SimpleNamespace(review_retry_key="exact"),
    )
    monkeypatch.setattr(
        git_tools, "_stage_candidate_for_review",
        lambda *_args, **_kwargs: ([], [], None),
    )
    monkeypatch.setattr(
        git_tools, "_fingerprint_staged_diff",
        lambda *_args, **_kwargs: {"ok": True, "fingerprint": "fp"},
    )
    monkeypatch.setattr(
        git_tools, "_free_cycle_gate",
        lambda run_ctx, *_args, **_kwargs: (
            setattr(run_ctx, "_review_reconcile_only", True) or None
        ),
    )
    monkeypatch.setattr(
        git_tools, "_review_binding_precondition_error",
        lambda *_args, **_kwargs: "binding is invalid",
    )
    monkeypatch.setattr(git_tools, "_record_commit_attempt", lambda *_a, **_k: None)

    outcome = git_tools._run_reviewed_stage_cycle(ctx, "message", 0.0)

    assert outcome["block_reason"] == "review_binding_invalid"
    assert ctx._review_reconcile_only is False


@pytest.mark.parametrize(
    "pending_invocation_id,operation_id",
    [("inv-existing", ""), ("", "op-existing")],
)
def test_restart_reconciliation_requires_complete_exact_identity(
    tmp_path, pending_invocation_id, operation_id,
):
    from types import SimpleNamespace

    from ouroboros.review_custody import (
        prepare_frozen_review_reconciliation, run_custodied_review_slots,
    )
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    attempt = SimpleNamespace(triad_raw_results=[{
        "slot_id": "slot-1", "model_id": "cursor/test", "status": "error",
        "operation_id": operation_id, "operation_state": "in_flight",
        "late_result_pending": True,
        "pending_invocation_id": pending_invocation_id,
    }], scope_raw_result={})
    ctx = SimpleNamespace()
    prepare_frozen_review_reconciliation(ctx, attempt)
    calls = []

    def error_actor(slot, error, actor_operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=actor_operation_id, operation_state=operation_state,
            late_result_pending=operation_state in {"in_flight", "custody_lost"},
        )

    [actor] = run_custodied_review_slots(
        request=ReviewRequest(
            surface="multi_model_review", goal="review", task_id="task-partial",
            retry_key="commit_review:partial", reconcile_only=True,
        ),
        slots=[ReviewSlot(
            slot_id="slot-1", model="cursor/test", route=ReviewRouteKind.AGENT_SESSION,
        )],
        usage_ctx=ctx,
        task_id="task-partial",
        usage_meta={},
        review_usage_scope=UsageScope(drive_root=tmp_path, task_id="task-partial"),
        run_slot=lambda *_args: calls.append("dispatched"),
        error_actor=error_actor,
    )

    assert calls == []
    assert actor.operation_state == "custody_lost"
    assert ctx._review_custody_lost is True


def test_logical_timeout_actor_carries_live_delegated_restart_token(tmp_path):
    import threading
    import time
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    release = threading.Event()
    request = ReviewRequest(
        surface="multi_model_review", goal="review", task_id="task-timeout-token",
        retry_key="commit_review:token",
    )
    slot = ReviewSlot(
        slot_id="slot-1", model="cursor/test", route=ReviewRouteKind.AGENT_SESSION,
        timeout_sec=0.1,
    )
    calls = []

    def still_running(_slot, _operation_id, retry_state, _deadline, _checkpoint):
        calls.append(_operation_id)
        retry_state["pending_invocation_id"] = "inv-live"
        retry_state["delegated_run_id"] = "run-live"
        release.wait(0.4)
        return ReviewActorRecord(slot_id="slot-1", model="cursor/test", status="ok")

    def error_actor(_slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id="slot-1", model="cursor/test", status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
            late_result_pending=operation_state == "in_flight",
        )

    started = time.monotonic()
    ctx = SimpleNamespace()
    [actor] = run_custodied_review_slots(
        request=request,
        slots=[slot],
        usage_ctx=ctx,
        task_id="task-timeout-token",
        usage_meta={},
        review_usage_scope=UsageScope(drive_root=tmp_path, task_id="task-timeout-token"),
        run_slot=still_running,
        error_actor=error_actor,
    )
    [joined] = run_custodied_review_slots(
        request=ReviewRequest(
            surface="multi_model_review", goal="review",
            task_id="task-timeout-token", retry_key="commit_review:token",
            reconcile_only=True, deadline_at="2000-01-01T00:00:00Z",
        ),
        slots=[slot],
        usage_ctx=ctx,
        task_id="task-timeout-token",
        usage_meta={"deadline_at": "2000-01-01T00:00:00Z"},
        review_usage_scope=UsageScope(
            drive_root=tmp_path, task_id="task-timeout-token",
        ),
        run_slot=still_running,
        error_actor=error_actor,
    )
    release.set()

    assert time.monotonic() - started < 0.3
    assert len(calls) == 1
    assert actor.operation_state == "in_flight"
    assert actor.usage["pending_invocation_id"] == "inv-live"
    assert actor.usage["delegated_run_id"] == "run-live"
    assert joined.operation_id == actor.operation_id
    assert joined.operation_state == "in_flight"
    assert joined.usage["pending_invocation_id"] == "inv-live"
    assert joined.usage["delegated_run_id"] == "run-live"


def test_coordinator_keeps_fresh_empty_session_custody_cell_shared(
    tmp_path, monkeypatch,
):
    """The public coordinator adapter must not replace an empty custody dict."""
    import threading
    from types import SimpleNamespace

    from ouroboros.review_execution import ReviewAttemptResult, ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    release = threading.Event()
    settled = threading.Event()
    checkpoints = []

    class BlockingSessionExecutor:
        def __init__(self):
            self.state = None
            self.checkpoint = None
            self.execute_calls = 0

        def restore_custody(self, state):
            self.state = state

        def set_pending_invocation_checkpoint(self, checkpoint):
            self.checkpoint = checkpoint

        def prompt_payload(self):
            return {"session_prompt": "review"}

        def prompt_chars(self):
            return 6

        def execute(self):
            self.execute_calls += 1
            self.state["pending_invocation_id"] = "inv-shared"
            self.state["delegated_run_id"] = "run-shared"
            if self.checkpoint is not None:
                self.checkpoint("inv-shared")
            release.wait(1.0)
            settled.set()
            return ReviewAttemptResult(
                message={"content": "[]"}, usage={}, raw_text="[]",
            )

        def failure_custody(self):
            return dict(self.state or {})

    executor = BlockingSessionExecutor()
    monkeypatch.setattr(
        "ouroboros.review_substrate._review_route_executor",
        lambda *_args, **_kwargs: executor,
    )
    ctx = SimpleNamespace(
        _review_pending_invocation_checkpoint=lambda **facts: checkpoints.append(facts),
    )
    request = ReviewRequest(
        surface="multi_model_review",
        goal="review",
        task_id="shared-empty-cell",
        retry_key="commit_review:shared-empty-cell",
        session_root=str(tmp_path),
        session_task="review this tree",
    )
    slot = ReviewSlot(
        slot_id="session-slot",
        model="cursor/test",
        route=ReviewRouteKind.AGENT_SESSION,
        timeout_sec=0.2,
    )

    try:
        actor = run_review_request(
            request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx,
        ).actors[0]
        assert actor["operation_state"] == "in_flight"
        assert actor["usage"]["pending_invocation_id"] == "inv-shared"
        assert actor["usage"]["delegated_run_id"] == "run-shared"
        assert checkpoints == [{
            "surface": "multi_model_review",
            "slot_id": "session-slot",
            "operation_id": actor["operation_id"],
            "invocation_id": "inv-shared",
        }]
        joined = run_review_request(
            request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx,
        ).actors[0]
        assert executor.execute_calls == 1
        assert joined["operation_id"] == actor["operation_id"]
        assert joined["usage"]["pending_invocation_id"] == "inv-shared"
        assert joined["usage"]["delegated_run_id"] == "run-shared"
    finally:
        release.set()
        assert settled.wait(1.0)


def test_review_owner_deadline_rechecked_after_prompt_persistence(
    tmp_path, monkeypatch,
):
    """A slow prompt/custody phase must not create a late paid API send."""
    import datetime as dt
    from types import SimpleNamespace

    import ouroboros.deadline_utils as deadlines
    import ouroboros.review_substrate as substrate
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    clock = [dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(deadlines, "utc_now", lambda: clock[0])
    original_persist = substrate.persist_call

    def slow_prompt_persist(drive_root, *, call_type="", **kwargs):
        if str(call_type).endswith("_prompt"):
            clock[0] += dt.timedelta(seconds=400)
        return original_persist(drive_root, call_type=call_type, **kwargs)

    monkeypatch.setattr(substrate, "persist_call", slow_prompt_persist)

    class NeverCalled:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            raise AssertionError("expired review owner deadline must not dispatch")

    llm = NeverCalled()
    request = ReviewRequest(
        surface="multi_model_review", goal="review", task_id="late-review",
        call_type="multi_model_review",
        deadline_at=(clock[0] + dt.timedelta(seconds=300)).isoformat(),
    )
    slot = ReviewSlot(
        slot_id="slot-1", model="openai/test", route=ReviewRouteKind.API_CHAT,
        timeout_sec=30,
    )
    result = run_review_request(
        request, slots=[slot], drive_root=tmp_path, llm=llm,
        usage_ctx=SimpleNamespace(),
    )

    actor = result.actors[0]
    assert llm.calls == 0
    assert actor["operation_state"] == "not_dispatched"
    assert actor["status"] == "not_dispatched"


def test_coordinator_rejoins_exact_recovery_after_spent_owner_deadline(
    tmp_path, monkeypatch,
):
    """The public coordinator must not launder a paid recovery as $0 dispatch."""
    from types import SimpleNamespace

    from ouroboros.review_custody import (
        merge_frozen_review_reconciliation, prepare_frozen_review_reconciliation,
    )
    from ouroboros.review_execution import ReviewAttemptResult, ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    class RecoveringExecutor:
        def __init__(self):
            self.state = None
            self.execute_calls = 0

        def restore_custody(self, state):
            self.state = state

        def set_pending_invocation_checkpoint(self, _checkpoint):
            return None

        def prompt_payload(self):
            return {"session_prompt": "review"}

        def prompt_chars(self):
            return 6

        def execute(self):
            self.execute_calls += 1
            return ReviewAttemptResult(
                message={"content": "[]"}, usage={}, raw_text="[]",
            )

        def failure_custody(self):
            return dict(self.state or {})

    executor = RecoveringExecutor()
    monkeypatch.setattr(
        "ouroboros.review_substrate._review_route_executor",
        lambda *_args, **_kwargs: executor,
    )
    ctx = SimpleNamespace(
        task_id="spent-recovery",
        drive_root=tmp_path,
        task_metadata={"deadline_at": "2000-01-01T00:00:00Z"},
    )
    prepare_frozen_review_reconciliation(ctx, SimpleNamespace(
        triad_raw_results=[{
            "slot_id": "slot-1", "model_id": "test/model", "status": "error",
            "operation_id": "op-existing", "operation_state": "in_flight",
            "late_result_pending": True, "pending_invocation_id": "inv-existing",
        }],
        scope_raw_result={},
    ))
    request = ReviewRequest(
        surface="multi_model_review", goal="review", task_id="spent-recovery",
        retry_key="commit_review:spent-recovery", reconcile_only=True,
        deadline_at="2000-01-01T00:00:00Z",
    )
    result = run_review_request(
        request,
        slots=[ReviewSlot(
            slot_id="slot-1", model="test/model",
            route=ReviewRouteKind.AGENT_SESSION,
        )],
        drive_root=tmp_path,
        usage_ctx=ctx,
    )

    actor = result.actors[0]
    assert executor.execute_calls == 1
    assert actor["operation_id"] == "op-existing"
    assert actor["operation_state"] == "settled"
    assert actor["status"] == "ok"
    ctx._last_triad_raw_results = result.actors
    merge_frozen_review_reconciliation(ctx)
    row = ctx._last_triad_raw_results[0]
    assert row["operation_state"] == "settled"
    assert row.get("pending_invocation_id", "") == ""


def test_durable_triad_and_scope_rows_carry_delegated_restart_identity():
    from ouroboros.tools.review import _parse_model_response
    from ouroboros.tools.review_helpers import build_scope_actor_record
    from ouroboros.tools.scope_review import ScopeReviewResult
    from ouroboros.triad_review import parse_model_review_results

    envelope = _parse_model_response("cursor/test", {
        "choices": [{"message": {"content": "[]"}}], "slot_id": "slot_1",
        "operation_id": "op-1", "operation_state": "in_flight",
        "late_result_pending": True,
        "usage": {
            "pending_invocation_id": "inv-1", "delegated_run_id": "run-1",
        },
    }, None)
    triad = parse_model_review_results({"results": [envelope]})
    triad_row = triad.actor_records[0].to_dict()
    assert triad_row["pending_invocation_id"] == "inv-1"
    assert triad_row["delegated_run_id"] == "run-1"

    scope_row = build_scope_actor_record(ScopeReviewResult(
        model_id="cursor/test", operation_id="op-2", operation_state="in_flight",
        late_result_pending=True, pending_invocation_id="inv-2",
        delegated_run_id="run-2",
    ), slot_id="scope_slot_1")
    assert scope_row["pending_invocation_id"] == "inv-2"
    assert scope_row["delegated_run_id"] == "run-2"


def test_review_does_not_retry_an_unknown_dispatched_api_attempt(tmp_path):
    from types import SimpleNamespace

    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK, _NO_RESEND, _attempt_key
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    class _AmbiguousReviewLLM:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            exc = TimeoutError("review provider outcome unknown")
            exc.physical_attempt_capture = SimpleNamespace(
                state="unresolved", provider_status_code=None,
                provider_code="", provider_error_type="TimeoutError",
            )
            raise exc

    llm = _AmbiguousReviewLLM()
    paid = []
    ctx = SimpleNamespace(
        task_id="task-review", event_queue=None, pending_events=[],
        _review_paid_stamp=lambda: paid.append("paid"),
    )
    request = ReviewRequest(
        surface="multi_model_review", goal="review", task_id="task-review",
        call_type="multi_model_review", retry_key="commit_review:unknown",
    )
    slot = ReviewSlot(slot_id="slot-1", model="openai/test")
    key = _attempt_key(request, slot)
    try:
        first = run_review_request(
            request, slots=[slot], drive_root=tmp_path, llm=llm, usage_ctx=ctx,
        )
        second = run_review_request(
            request, slots=[slot], drive_root=tmp_path, llm=llm, usage_ctx=ctx,
        )

        assert llm.calls == 1
        assert paid == ["paid"]
        assert first.actors[0]["status"] == "error"
        assert first.actors[0]["operation_state"] == "custody_lost"
        assert first.actors[0]["late_result_pending"] is True
        assert second.actors[0]["operation_id"] == first.actors[0]["operation_id"]
        assert second.actors[0]["operation_state"] == "custody_lost"
        assert second.actors[0]["failure_code"] == "provider_outcome_unknown"
        assert second.actors[0]["late_result_pending"] is True
        assert ctx._review_custody_lost is True
        with _ACTIVE_LOCK:
            assert key not in _ACTIVE
            assert _NO_RESEND[key] == first.actors[0]["operation_id"]
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(key, None)
            _NO_RESEND.pop(key, None)


@pytest.mark.parametrize(
    ("surface", "raw_text"),
    [("task_acceptance", "malformed reviewer output"), ("multi_model_review", "")],
)
def test_review_retry_rail_honors_durable_cancel_for_format_and_empty_paths(
    tmp_path, monkeypatch, surface, raw_text,
):
    """A cancel that lands after attempt one forbids every retry shape."""
    from types import SimpleNamespace

    from ouroboros.cancel_intents import request_cancel
    from ouroboros.review_execution import ReviewAttemptResult, ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    calls = []
    task_id = f"cancel-retry-{surface.replace('_', '-')}"

    def attempt(_assignment, **_kwargs):
        calls.append(1)
        request_cancel(tmp_path, task_id, reason="owner stopped retry rail", source="test")
        return ReviewAttemptResult(
            message={"content": raw_text}, usage={}, raw_text=raw_text,
        )

    monkeypatch.setattr("ouroboros.review_substrate._execute_slot_attempt", attempt)
    ctx = SimpleNamespace(task_id=task_id, drive_root=tmp_path)
    result = run_review_request(
        ReviewRequest(
            surface=surface, goal="review", task_id=task_id,
            evidence={}, call_type=f"{surface}_review",
        ),
        slots=[ReviewSlot(
            slot_id="slot-1", model="test/model", route=ReviewRouteKind.API_CHAT,
        )],
        drive_root=tmp_path, usage_ctx=ctx,
    )

    assert calls == [1]
    assert result.actors[0]["raw_text"] == raw_text


def test_keyed_terminal_api_error_is_replayed_while_sibling_is_late(tmp_path):
    """A settled API slot must not be bought twice while its cycle sibling settles."""
    import threading
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    late_started = threading.Event()
    late_finished = threading.Event()
    release_late = threading.Event()
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="mixed-cycle",
        retry_key="plan_review:mixed-cycle:1",
    )
    slots = [
        ReviewSlot(slot_id="fast", model="test/model", route=ReviewRouteKind.API_CHAT,
                   timeout_sec=0.2),
        ReviewSlot(slot_id="late", model="test/model", route=ReviewRouteKind.API_CHAT,
                   timeout_sec=0.01),
    ]

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        if slot.slot_id == "late":
            late_started.set()
            release_late.wait(1.0)
            late_finished.set()
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error=f"{slot.slot_id} terminal API error", operation_id=operation_id,
            http_status=500 if slot.slot_id == "fast" else 503,
            # A terminal actor is sufficient for same-cycle custody.  The
            # optional PhysicalAttemptCapture is intentionally absent here,
            # matching adapters that return only the typed actor record.
            usage={},
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=slots, usage_ctx=ctx, task_id=request.task_id,
        usage_meta={}, review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    assert {actor.slot_id: actor.operation_state for actor in first} == {
        "fast": "settled", "late": "in_flight",
    }
    assert late_started.wait(1.0)
    assert getattr(ctx, "_review_settled_attempts", {})
    second = run_custodied_review_slots(**args)
    assert calls == ["fast", "late"]
    assert {actor.slot_id: actor.operation_state for actor in second} == {
        "fast": "settled", "late": "in_flight",
    }

    release_late.set()
    assert late_finished.wait(1.0)


def test_unresolved_terminal_http_capture_is_replayed_without_second_dispatch(tmp_path):
    """A typed provider response makes an unresolved ledger row known-terminal."""
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="terminal-capture",
        retry_key="plan_review:terminal-capture:1",
    )
    slot = ReviewSlot(
        slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT,
        timeout_sec=0.2,
    )

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error="provider returned 503", operation_id=operation_id,
            http_status=503,
            usage={"physical_attempt_state": "unresolved", "provider_status_code": 503},
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=[slot], usage_ctx=ctx, task_id=request.task_id,
        usage_meta={}, review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    second = run_custodied_review_slots(**args)

    assert calls == ["only"]
    assert first[0].operation_state == "settled"
    assert second[0].operation_id == first[0].operation_id
    assert second[0].usage["physical_attempt_state"] == "unresolved"
    assert second[0].http_status == 503


def test_unresolved_capture_without_terminal_status_is_no_resend_without_typed_code(tmp_path):
    """A legacy actor cannot turn an unknown physical outcome into a resend."""
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="unknown-capture",
        retry_key="plan_review:unknown-capture:1",
    )
    slot = ReviewSlot(
        slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT,
        timeout_sec=0.2,
    )

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error="socket outcome unknown", operation_id=operation_id,
            usage={"physical_attempt_state": "unresolved"},
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=[slot], usage_ctx=ctx, task_id=request.task_id,
        usage_meta={}, review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    second = run_custodied_review_slots(**args)

    assert calls == ["only"]
    assert first[0].operation_state == "custody_lost"
    assert second[0].operation_state == "custody_lost"
    assert second[0].failure_code == "provider_outcome_unknown"
    assert second[0].late_result_pending is True


def test_coordinator_propagates_terminal_capture_status_for_same_cycle_replay(
    tmp_path, monkeypatch,
):
    """The coordinator must carry capture status into custody, not just its state."""
    from types import SimpleNamespace

    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    class ProviderFailure(RuntimeError):
        pass

    class TerminalExecutor:
        def __init__(self):
            self.execute_calls = 0

        def restore_custody(self, _state):
            return None

        def set_pending_invocation_checkpoint(self, _checkpoint):
            return None

        def prompt_payload(self):
            return {"messages": []}

        def prompt_chars(self):
            return 0

        def execute(self):
            self.execute_calls += 1
            error = ProviderFailure("provider HTTP 503")
            error.physical_attempt_capture = SimpleNamespace(
                state="unresolved", provider_status_code=503,
            )
            raise error

        def failure_custody(self):
            return {}

    executor = TerminalExecutor()
    monkeypatch.setattr(
        "ouroboros.review_substrate._review_route_executor",
        lambda *_args, **_kwargs: executor,
    )
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="capture-propagation",
        retry_key="plan_review:capture-propagation:1",
    )
    slot = ReviewSlot(slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT)
    ctx = SimpleNamespace()
    first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx)
    second = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx)

    assert executor.execute_calls == 1
    assert first.actors[0]["http_status"] == 503
    assert first.actors[0]["usage"]["provider_status_code"] == 503
    assert second.actors[0]["operation_id"] == first.actors[0]["operation_id"]


def test_budget_exceeded_review_failure_remains_retryable(tmp_path, monkeypatch):
    """A budget refusal before reserve is a $0 row, never sticky custody."""
    from types import SimpleNamespace

    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
    from ouroboros.usage_accounting import BudgetExceeded

    class BudgetExecutor:
        execute_calls = 0

        def restore_custody(self, _state):
            return None

        def set_pending_invocation_checkpoint(self, _checkpoint):
            return None

        def prompt_payload(self):
            return {"messages": []}

        def prompt_chars(self):
            return 0

        def execute(self):
            self.execute_calls += 1
            raise BudgetExceeded("review budget exhausted")

        def failure_custody(self):
            return {}

    executor = BudgetExecutor()
    monkeypatch.setattr(
        "ouroboros.review_substrate._review_route_executor",
        lambda *_args, **_kwargs: executor,
    )
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="budget-refusal",
        retry_key="plan_review:budget-refusal:1",
    )
    slot = ReviewSlot(slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT)
    ctx = SimpleNamespace()
    first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx)
    second = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx)

    assert executor.execute_calls == 2
    assert first.actors[0]["operation_state"] == "not_dispatched"
    assert second.actors[0]["operation_state"] == "not_dispatched"
    assert not getattr(ctx, "_review_settled_attempts", {})


def test_not_dispatched_error_remains_retryable_for_same_cycle(tmp_path):
    """A $0 admission refusal must not become a sticky replay actor."""
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="retryable-refusal",
        retry_key="plan_review:retryable-refusal:1",
    )
    slot = ReviewSlot(
        slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT,
        timeout_sec=0.2,
    )

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error="deadline exhausted before dispatch", operation_id=operation_id,
            operation_state="not_dispatched",
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="not_dispatched",
            error=error, operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=[slot], usage_ctx=ctx, task_id=request.task_id,
        usage_meta={}, review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    second = run_custodied_review_slots(**args)

    assert [actor.operation_state for actor in first] == ["not_dispatched"]
    assert [actor.operation_state for actor in second] == ["not_dispatched"]
    assert calls == ["only", "only"]
    assert not getattr(ctx, "_review_settled_attempts", {})


@pytest.mark.parametrize("physical_state", ["reserved", "released"])
def test_pre_dispatch_capture_state_remains_retryable_for_same_cycle(
    tmp_path, physical_state,
):
    """Explicit pre-dispatch capture facts must not become sticky replay rows."""
    from types import SimpleNamespace

    from ouroboros.review_custody import run_custodied_review_slots
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id=f"retryable-{physical_state}",
        retry_key=f"plan_review:retryable-{physical_state}:1",
    )
    slot = ReviewSlot(
        slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT,
        timeout_sec=0.2,
    )

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error="pre-dispatch failure", operation_id=operation_id,
            usage={"physical_attempt_state": physical_state},
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=[slot], usage_ctx=ctx, task_id=request.task_id,
        usage_meta={},
        review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    second = run_custodied_review_slots(**args)

    assert [actor.operation_state for actor in first] == ["settled"]
    assert [actor.operation_state for actor in second] == ["settled"]
    assert calls == ["only", "only"]
    assert not getattr(ctx, "_review_settled_attempts", {})


def test_late_pre_dispatch_capture_state_remains_retryable_for_same_cycle(tmp_path):
    """A late worker release must not become a sticky replay row either."""
    import threading
    import time
    from types import SimpleNamespace

    from ouroboros.review_custody import (
        _ACTIVE,
        _ACTIVE_LOCK,
        _attempt_key,
        run_custodied_review_slots,
    )
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest, ReviewSlot
    from ouroboros.usage_accounting import UsageScope

    calls = []
    late_started = threading.Event()
    release_late = threading.Event()
    late_finished = threading.Event()
    ctx = SimpleNamespace()
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="late-retryable-release",
        retry_key="plan_review:late-retryable-release:1",
    )
    slot = ReviewSlot(
        slot_id="only", model="test/model", route=ReviewRouteKind.API_CHAT,
        timeout_sec=0.01,
    )
    key = _attempt_key(request, slot)

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        if len(calls) == 1:
            late_started.set()
            assert release_late.wait(1.0)
            late_finished.set()
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error",
            error="pre-dispatch failure", operation_id=operation_id,
            usage={"physical_attempt_state": "released"},
        )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
        )

    args = dict(
        request=request, slots=[slot], usage_ctx=ctx, task_id=request.task_id,
        usage_meta={},
        review_usage_scope=UsageScope(drive_root=tmp_path, task_id=request.task_id),
        run_slot=run_slot, error_actor=error_actor,
    )
    first = run_custodied_review_slots(**args)
    assert [actor.operation_state for actor in first] == ["in_flight"]
    assert late_started.wait(1.0)
    release_late.set()
    assert late_finished.wait(1.0)
    # The worker signals just before returning, while settlement and removal
    # from process-local custody happen in the coordinator thread afterwards.
    # Wait for that handoff so the second call cannot race the first settlement.
    deadline = time.monotonic() + 1.0
    while True:
        with _ACTIVE_LOCK:
            settled = key not in _ACTIVE
        if settled:
            break
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert not getattr(ctx, "_review_settled_attempts", {})

    second = run_custodied_review_slots(**args)
    assert [actor.operation_state for actor in second] == ["settled"]
    assert calls == ["only", "only"]
    assert not getattr(ctx, "_review_settled_attempts", {})
