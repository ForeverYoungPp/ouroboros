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
