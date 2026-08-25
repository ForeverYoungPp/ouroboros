from __future__ import annotations

import pathlib
from types import SimpleNamespace


def test_normal_model_response_stamps_model_final_origin():
    from ouroboros.loop import _handle_text_response

    usage = {}
    text, returned_usage, _trace = _handle_text_response(
        "A complete answer", {"reasoning_notes": []}, usage,
    )
    assert text == "A complete answer"
    assert returned_usage["terminal_origin"] == "model_final"


def test_terminal_projection_preserves_model_and_legacy_but_receipts_host_salvage(tmp_path):
    from supervisor.terminal_delivery import (
        delivery_id_for,
        project_terminal_result_event,
    )

    raw = "RAW PATCH " * 1000
    base = {
        "type": "send_message",
        "chat_id": 7,
        "task_id": "terminal-a",
        "text": raw,
        "format": "markdown",
    }
    host = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin="host_salvage", base_event=base,
    )
    assert host["role"] == "system"
    assert host["system_type"] == "terminal_incident"
    assert host["task_id"] == "terminal-a"
    assert host["terminal_origin"] == "host_salvage"
    assert raw not in host["text"]
    assert "model-provider outage" in host["text"]
    assert "terminal-a" not in host["text"]
    assert "task details" in host["text"]
    assert host["delivery_id"] == delivery_id_for("terminal-a", raw)

    model = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin="model_final", base_event=base,
    )
    assert model["text"] == raw
    assert model["terminal_origin"] == "model_final"
    assert "role" not in model and "system_type" not in model

    legacy = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin=None, base_event=base,
    )
    assert legacy == {**base, "delivery_id": delivery_id_for("terminal-a", raw)}


def test_host_salvage_live_outbox_and_replay_use_same_projection(tmp_path, monkeypatch):
    import ouroboros.agent_task_pipeline as pipeline
    from supervisor import state as supervisor_state
    from supervisor.terminal_delivery import build_completed_result_event, pending_deliveries

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(
        pipeline, "_derive_host_bound_loop_outcome",
        lambda *_a, **_k: {
            "outcome_axes": {
                "execution": {"status": "infra_failed"},
                "objective": {"status": "not_evaluated"},
                "review": {"status": "not_evaluated"},
                "artifacts": {"status": "not_applicable"},
            },
            "reason_code": "provider_unavailable",
        },
    )
    monkeypatch.setattr(pipeline, "apply_receipt_absent_flag", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "_store_task_result", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "_root_post_task_already_completed", lambda *_a, **_k: False)
    observed = {}
    monkeypatch.setattr(
        pipeline, "_run_post_task_processing_async",
        lambda *_a, **kwargs: observed.update(sealed=kwargs.get("sealed_final")),
    )
    monkeypatch.setattr(supervisor_state, "reconstruct_task_cost", lambda *_a, **_k: {
        "cost_accounting_status": "available", "cost_final": True,
        "cost_usd": 0.0, "total_rounds": 1, "prompt_tokens": 1,
        "completion_tokens": 1, "reserved_usd": 0.0,
        "unresolved_upper_bound_usd": 0.0, "unknown_unmetered": 0,
    })
    raw = "RAW_TOOL_ENVELOPE\n" + ("x" * 20000)
    usage = {
        "rounds": 1,
        "cost": 0.0,
        "reason_code": "provider_unavailable",
        "terminal_origin": "host_salvage",
    }
    pending = []
    pipeline.emit_task_results(
        env=SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path),
        memory=object(), llm=object(), pending_events=pending,
        task={"id": "host-live", "type": "task", "chat_id": 7, "text": "do it"},
        text=raw, usage=usage,
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0, drive_logs=logs,
        ctx=SimpleNamespace(pending_restart_reason=""),
    )

    live = pending[0]
    assert live["role"] == "system"
    assert live["system_type"] == "terminal_incident"
    assert raw not in live["text"]
    preserved = pathlib.Path(usage["terminal_salvage_path"])
    assert preserved.read_text(encoding="utf-8") == raw
    (owed,) = pending_deliveries(tmp_path)
    assert owed["text"] == live["text"]
    assert owed["role"] == live["role"]
    assert owed["system_type"] == live["system_type"]
    assert owed["delivery_id"] == live["delivery_id"]

    replay = build_completed_result_event(
        tmp_path, {"chat_id": 7}, "host-live",
        {"result": raw, "terminal_origin": "host_salvage",
         "terminal_salvage_path": str(preserved)},
    )
    assert replay is not None
    assert replay["text"] == live["text"]
    assert replay["role"] == live["role"]
    assert replay["system_type"] == live["system_type"]
    assert replay["delivery_id"] == live["delivery_id"]
    assert observed["sealed"]["final_result_text"] == live["text"]
    assert raw not in observed["sealed"]["final_result_text"]


def test_task_result_persists_origin_and_full_host_salvage(tmp_path):
    import ouroboros.agent_task_pipeline as pipeline

    env = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path)
    raw = "full raw salvage"
    path = tmp_path / "observability" / "salvaged" / "persisted.txt"
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    pipeline._store_task_result(
        env,
        {"id": "persisted", "root_task_id": "persisted", "type": "task"},
        raw,
        {"rounds": 1, "cost": 0.0, "terminal_origin": "host_salvage",
         "terminal_salvage_path": str(path)},
        {"tool_calls": [], "reasoning_notes": []},
    )
    stored = pipeline.load_task_result(tmp_path, "persisted")
    assert stored["result"] == raw
    assert stored["terminal_origin"] == "host_salvage"
    assert stored["terminal_salvage_path"] == str(path)


def test_project_completion_host_salvage_uses_neutral_details_copy(tmp_path, monkeypatch):
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.project_dialogue import enqueue_project_completion_summary

    project = create_project(tmp_path, "salvage", name="Salvage Project")
    bind_task_to_project(
        tmp_path, "salvage-root", project["id"], project["chat_id"],
        origin={"absent": "system"},
    )
    queued = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.enqueue_terminal_delivery",
        lambda _root, event, **_kwargs: queued.append(dict(event)) or True,
    )
    raw = "RAW PATCH " * 100
    task = {
        "id": "salvage-root", "chat_id": project["chat_id"],
        "project_id": project["id"], "title": "Interrupted task",
    }
    result = {
        **task, "task_id": "salvage-root", "status": "failed",
        "reason_code": "provider_unavailable", "result": raw,
        "terminal_origin": "host_salvage",
    }
    assert enqueue_project_completion_summary(
        tmp_path, {}, "salvage-root", task, result,
        {"status": "failed", "reason_code": "provider_unavailable"},
    )
    assert len(queued) == 1
    assert raw not in queued[0]["text"]
    assert queued[0]["text"].endswith("Open the Project for details.")
