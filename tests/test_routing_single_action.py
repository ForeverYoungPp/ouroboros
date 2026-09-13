"""One owner message yields exactly ONE routing action (steer vs promote vs route).

Regression for the 2026-09-12 duplicate-task incident: one owner message produced
BOTH a ``steer_task`` mailbox delivery and a ``promote_chat_to_task`` second root.
Both committed. Because the annotation sidecar keeps only the LATEST row per
``client_message_id``, the promote silently superseded the steer's receipt, and
nothing in the host refused the second decision.

The floor is turn-local and armed only by a COMMITTED receipt, so the recovery
paths stay open: a steer whose target is gone (``needs_manual_target``) or whose
delivery is unconfirmed does NOT block the documented "promote instead" fallback.
"""

from __future__ import annotations

import types

import pytest


def _ctx(drive_root, **metadata):
    return types.SimpleNamespace(
        pending_events=[],
        event_queue=None,
        current_chat_id=1,
        drive_root=drive_root,
        task_metadata=dict(metadata),
    )


def _steer_returns(monkeypatch, status: str, reason: str = ""):
    monkeypatch.setattr(
        "ouroboros.tools.control._wait_for_routing_annotation",
        lambda *_args, **_kwargs: {"status": status, "reason": reason},
    )


def _promote_returns(monkeypatch, status: str, reason: str = ""):
    monkeypatch.setattr(
        "ouroboros.tools.control._wait_for_promotion_admission",
        lambda *_args, **_kwargs: {"status": status, "reason": reason},
    )


def test_routing_action_label_matches_the_turn_marker(monkeypatch):
    """The committed fact and the turn marker share ONE verb derivation."""
    from ouroboros.tools import control

    assert control._routing_action_label({"type": "steer_task"}) == "steer_task"
    assert control._routing_action_label({"type": "promote_chat_to_task"}) == "promote_chat_to_task"
    assert (
        control._routing_action_label({"type": "promote_chat_to_task", "routed_from_main": True})
        == "route_to_project"
    )
    assert control._routing_action_label({"type": "runtime_mode_change"}) == ""


def test_committed_steer_refuses_a_following_promote_in_the_same_turn(tmp_path, monkeypatch):
    """The reported incident: steer + promote for one message -> promote refused."""
    from ouroboros.tools.control import _promote_chat_to_task, _steer_task

    _steer_returns(monkeypatch, "delivered")
    ctx = _ctx(tmp_path, client_message_id="msg-1")

    steered = _steer_task(ctx, "task-abc", "also do the backlog")
    assert "Steering task task-abc" in steered
    assert ctx._committed_routing_action == "steer_task"

    out = _promote_chat_to_task(ctx, "Clean the backlog", predecessor_task_id="")
    assert out.startswith("⚠️ ROUTING_ALREADY_COMMITTED")
    assert "steer_task" in out
    # No second routing event reached the task lane.
    assert [evt["type"] for evt in ctx.pending_events] == ["steer_task"]


def test_committed_promote_refuses_a_following_steer(tmp_path, monkeypatch):
    from ouroboros.tools.control import _promote_chat_to_task, _steer_task

    _promote_returns(monkeypatch, "scheduled")
    ctx = _ctx(tmp_path, client_message_id="msg-2")

    promoted = _promote_chat_to_task(ctx, "Clean the backlog", predecessor_task_id="")
    assert promoted.startswith("OK: task")

    out = _steer_task(ctx, "task-abc", "and also this")
    assert out.startswith("⚠️ ROUTING_ALREADY_COMMITTED")
    assert [evt["type"] for evt in ctx.pending_events] == ["promote_chat_to_task"]


def test_committed_action_refuses_a_following_project_route(tmp_path, monkeypatch):
    from ouroboros.tools.control import _route_to_project, _steer_task

    _steer_returns(monkeypatch, "delivered")
    ctx = _ctx(tmp_path, client_message_id="msg-3")
    assert "Steering task task-abc" in _steer_task(ctx, "task-abc", "keep going")

    out = _route_to_project(ctx, project_id="cat-tower", message="keep going", predecessor_task_id="")
    assert out.startswith("⚠️ ROUTING_ALREADY_COMMITTED")
    assert [evt["type"] for evt in ctx.pending_events] == ["steer_task"]


@pytest.mark.parametrize(
    "status, reason",
    [
        ("needs_manual_target", "target_not_steerable"),
        ("needs_manual_target", "target_finished"),
        ("unconfirmed", "confirmation_timeout"),
        ("rejected", "duplicate"),
    ],
)
def test_only_a_committed_receipt_arms_the_floor(tmp_path, monkeypatch, status, reason):
    """A failed steer must NOT trap the message — "promote instead" still works."""
    from ouroboros.tools.control import _promote_chat_to_task, _steer_task

    _steer_returns(monkeypatch, status, reason)
    ctx = _ctx(tmp_path, client_message_id="msg-4")

    refused = _steer_task(ctx, "task-abc", "keep going")
    assert "Steering task task-abc" not in refused
    assert not getattr(ctx, "_committed_routing_action", "")

    _promote_returns(monkeypatch, "scheduled")
    out = _promote_chat_to_task(ctx, "Start it as its own task", predecessor_task_id="")
    assert out.startswith("OK: task")


def test_the_floor_does_not_leak_across_turns(tmp_path, monkeypatch):
    """Turn-local: a later turn re-decides from current state, not from history."""
    from ouroboros.tools.control import _promote_chat_to_task, _steer_task

    _steer_returns(monkeypatch, "delivered")
    first = _ctx(tmp_path, client_message_id="msg-5")
    assert "Steering task task-abc" in _steer_task(first, "task-abc", "keep going")

    _promote_returns(monkeypatch, "scheduled")
    later_turn = _ctx(tmp_path, client_message_id="msg-6")
    assert _promote_chat_to_task(later_turn, "Fresh work", predecessor_task_id="").startswith("OK: task")


def test_the_floor_is_turn_local_not_message_id_scoped(tmp_path, monkeypatch):
    """The TURN is the decision unit, so no message id is needed to arm the floor.

    The contract gives one routing decision per owner message, and a turn that
    emitted a committed route has spent it. ``client_message_id`` is absent on the
    API/CLI/scheduled lanes, so keying the floor on it would leave exactly those
    lanes unfenced.
    """
    from ouroboros.tools.control import _promote_chat_to_task, _steer_task

    _steer_returns(monkeypatch, "delivered")
    _promote_returns(monkeypatch, "scheduled")
    ctx = _ctx(tmp_path)

    assert "Steering task task-abc" in _steer_task(ctx, "task-abc", "keep going")
    out = _promote_chat_to_task(ctx, "Fresh work", predecessor_task_id="")
    assert out.startswith("⚠️ ROUTING_ALREADY_COMMITTED")
    assert [evt["type"] for evt in ctx.pending_events] == ["steer_task"]


def test_the_turn_marker_is_still_set_on_emit(tmp_path, monkeypatch):
    """The pre-existing turn-local marker keeps its emit-time contract."""
    from ouroboros.tools.control import _steer_task

    _steer_returns(monkeypatch, "needs_manual_target", "target_not_steerable")
    ctx = _ctx(tmp_path, client_message_id="msg-7")

    _steer_task(ctx, "task-abc", "keep going")

    assert ctx._typed_routing_action_emitted == "steer_task"
