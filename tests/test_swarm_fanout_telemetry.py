"""WS8: swarm_fanout telemetry shape + reject-meta marker."""
from __future__ import annotations

import json
import types

from ouroboros.tools.control import _emit_swarm_fanout, maybe_emit_delegated_run_fanout
from supervisor.events import _subagent_rejection_meta, _subagent_scheduled_meta


def test_swarm_fanout_event_shape(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = types.SimpleNamespace(drive_logs=lambda: logs, _last_wave_ts=0.0)
    _emit_swarm_fanout(
        ctx,
        parent_task_id="p1",
        root_task_id="r1",
        depth=2,
        task_group_id="subagents-x",
        task_ids=["a", "b"],
        role="researcher",
        requested_model_lane="auto",
        objective="o" * 300,
        emitted_live=True,
    )
    lines = (logs / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["type"] == "swarm_fanout"
    # Must not be foldable into a grouped-task lane or rendered as a subagent card.
    assert not evt["type"].startswith(("task_", "llm_", "tool_"))
    assert "delegation_role" not in evt and "subagent_task_id" not in evt
    assert evt["requested_count"] == 2 and evt["task_ids"] == ["a", "b"]
    assert evt["slot_count"] == 2
    # A wave event is written BEFORE any child starts, so it names the lane that was
    # asked for and never one that was resolved (v6.87.28).
    assert evt["requested_model_lane"] == "auto"
    assert "effective_model_lanes" not in evt
    assert len(evt["objective_preview"]) == 200
    assert evt["inter_wave_latency_sec"] is None  # first wave (prev ts was 0)


def test_swarm_fanout_inter_wave_latency_on_second_wave(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = types.SimpleNamespace(drive_logs=lambda: logs, _last_wave_ts=0.0)
    for _ in range(2):
        _emit_swarm_fanout(
            ctx, parent_task_id="p", root_task_id="r", depth=1,
            task_group_id="", task_ids=["x"], role="r",
            requested_model_lane="auto", objective="o", emitted_live=False,
        )
    evts = [json.loads(line) for line in (logs / "events.jsonl").read_text().splitlines()]
    assert len(evts) == 2
    assert evts[0]["inter_wave_latency_sec"] is None
    assert isinstance(evts[1]["inter_wave_latency_sec"], float)


# --- delegated harness runs fold into swarm telemetry ONLY under Swarm intent ---


def _delegating_host_ctx(tmp_path, metadata):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return types.SimpleNamespace(
        drive_logs=lambda: logs, _last_wave_ts=0.0,
        task_id="t-host", task_depth=2, task_metadata=metadata,
    )


def _fanout_events(tmp_path):
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row.get("type") == "swarm_fanout"]


def test_delegated_run_fanout_emits_exact_wave_shape_under_swarm_intent(tmp_path):
    ctx = _delegating_host_ctx(
        tmp_path, {"force_plan_source": "swarm", "root_task_id": "r-root"},
    )
    maybe_emit_delegated_run_fanout(
        ctx, run_id="run-1", route_id="codex-route", objective="o" * 300, durable=True,
    )
    evts = _fanout_events(tmp_path)
    assert len(evts) == 1
    evt = evts[0]
    assert evt["type"] == "swarm_fanout"
    assert evt["parent_task_id"] == "t-host" and evt["task_id"] == "t-host"
    assert evt["root_task_id"] == "r-root"
    assert evt["depth"] == 3
    assert evt["requested_count"] == 1 and evt["slot_count"] == 1
    assert evt["task_ids"] == ["run-1"]
    assert evt["role"] == "delegated_run"
    # The REQUESTED lane is the selected session route; nothing has resolved yet.
    assert evt["requested_model_lane"] == "codex-route"
    assert len(evt["objective_preview"]) == 200
    assert evt["emitted_live"] is True
    # Same non-foldable envelope as every wave event (no phantom child card).
    assert "delegation_role" not in evt and "subagent_task_id" not in evt


def test_delegated_run_fanout_defaults_root_to_host_task(tmp_path):
    ctx = _delegating_host_ctx(tmp_path, {"force_plan_source": "swarm"})
    maybe_emit_delegated_run_fanout(
        ctx, run_id="run-2", route_id="r", objective="o", durable=True,
    )
    assert _fanout_events(tmp_path)[0]["root_task_id"] == "t-host"


def test_delegated_run_fanout_silent_without_swarm_intent(tmp_path):
    # An ordinary delegate_start on a task that never asked for a swarm emits
    # nothing — including under the non-swarm force_plan variants.
    for metadata in ({}, {"force_plan_source": "operator"}, {"force_plan": True}):
        ctx = _delegating_host_ctx(tmp_path, metadata)
        maybe_emit_delegated_run_fanout(
            ctx, run_id="run-x", route_id="r", objective="o", durable=True,
        )
    assert _fanout_events(tmp_path) == []


def test_delegated_run_fanout_silent_when_start_uncustodied(tmp_path):
    # started_uncustodied: the custody write failed, so the started fact is not
    # attested — no telemetry.
    ctx = _delegating_host_ctx(tmp_path, {"force_plan_source": "swarm"})
    maybe_emit_delegated_run_fanout(
        ctx, run_id="run-y", route_id="r", objective="o", durable=False,
    )
    assert _fanout_events(tmp_path) == []


def _refused_delegate_start(tmp_path, metadata):
    """Run the REAL delegate_start against a minimal host context.

    The delegated-run transport retired with its own modules (Seed 0), so every
    metadata shape meets the same typed refusal: nothing was started, and there
    is no run for swarm telemetry to fold in.
    """
    import ouroboros.tools.delegate as delegate
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="local_readonly_subagent"),
    )
    ctx.task_id = "t-nanny"
    ctx.task_depth = 1
    ctx.task_metadata = metadata
    return json.loads(delegate._delegate_start(ctx, "edit the README"))


def test_refused_delegate_start_emits_no_swarm_fanout_telemetry(tmp_path):
    # A refusal is not a delegation: no run exists, so a wave event here would
    # mint a phantom child card — under Swarm intent most of all.
    for metadata in (
        {},
        {"force_plan_source": "operator"},
        {"force_plan": True},
        {"force_plan_source": "swarm", "root_task_id": "t-root"},
    ):
        payload = _refused_delegate_start(tmp_path, metadata)
        assert payload["status"] == "refused"
        assert payload["reason"] == "claudexor_retired"
    assert _fanout_events(tmp_path) == []


def test_reject_meta_marks_not_accepted():
    meta = _subagent_rejection_meta(
        "t1", root_task_id="r1", parent_id="p1", role="x", status="failed", error="e",
    )
    assert meta.get("accepted") is False


def test_scheduled_meta_marks_accepted():
    meta = _subagent_scheduled_meta(
        tid="t1",
        role="researcher",
        task_constraint={"surface": "external_workspace"},
        task_group_id="g1",
        requested_model_lane="auto",
        active_subagent_count=2,
        max_active_subagents=6,
    )
    assert meta["accepted"] is True
    # An ACCEPTANCE card cannot carry an effective lane or a model: the child has not
    # been dispatched, so nothing has resolved them (v6.87.28).
    assert "effective_model_lane" not in meta and "model" not in meta
    assert meta["active_subagent_count"] == 2
    assert meta["max_active_subagents"] == 6
    assert meta["subagent_event"] == "scheduled"
    assert meta["write_surface"] == "external_workspace"
