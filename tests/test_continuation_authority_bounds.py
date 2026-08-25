"""Focused continuation-authority regressions for terminal provenance."""

from __future__ import annotations

import json
from types import SimpleNamespace


def _authority_source(task_id: str) -> dict:
    return {
        "kind": "task_result",
        "task_id": task_id,
        "tool": "get_task_result",
        "arguments": {"task_id": task_id, "include_authority": True},
    }


def _write_result(root, row: dict) -> None:
    result_dir = root / "task_results"
    result_dir.mkdir(exist_ok=True)
    (result_dir / f"{row['task_id']}.json").write_text(
        json.dumps(row), encoding="utf-8",
    )


def test_host_salvage_automatic_authority_is_bounded_but_explicit_read_is_full(
    tmp_path,
):
    from ouroboros.agent_startup_checks import (
        _AUTOMATIC_HOST_SALVAGE_RESULT_CHARS,
        task_result_authority_projection,
        validate_task_authority_sources,
    )
    from ouroboros.tools.control import _get_task_result
    from ouroboros.tools.registry import ToolContext

    task_id = "failed-predecessor"
    tail = "FULL_SALVAGE_TAIL_REMAINS_EXPLICITLY_READABLE"
    full_result = "unreviewed provider output\n" + (
        "x" * (_AUTOMATIC_HOST_SALVAGE_RESULT_CHARS + 5_000)
    ) + tail
    row = {
        "task_id": task_id,
        "status": "failed",
        "reason_code": "provider_unavailable",
        "terminal_origin": "host_salvage",
        "result": full_result,
        "outcome_axes": {"objective": "best_effort", "process": "failed"},
    }
    _write_result(tmp_path, row)
    source = _authority_source(task_id)
    task = {
        "id": "continuation",
        "budget_drive_root": str(tmp_path),
        "predecessor_authority_source": source,
    }
    env = SimpleNamespace(drive_root=tmp_path, budget_drive_root=tmp_path)

    assert validate_task_authority_sources(env, task) == {}
    automatic = task["predecessor_authority"]
    result = automatic["result"]
    assert result["kind"] == "unreviewed_host_salvage"
    assert len(result["preview"]) <= _AUTOMATIC_HOST_SALVAGE_RESULT_CHARS
    assert result["full_chars"] == len(full_result)
    assert result["omitted_chars"] == len(full_result) - result["carried_chars"]
    omission_note = (
        "\n⚠️ OMISSION NOTE: truncated at "
        f"{_AUTOMATIC_HOST_SALVAGE_RESULT_CHARS} chars; "
        f"original length {len(full_result)}"
    )
    assert result["preview"] == (
        full_result[:result["carried_chars"]] + omission_note
    )
    assert result["source_ref"] == {**source, "field": "authority.result"}
    assert result["preview"].startswith(full_result[:200])
    assert "OMISSION NOTE" in result["preview"]
    assert tail not in json.dumps(automatic)
    assert len(json.dumps(automatic)) < 20_000
    assert automatic["status"] == "failed"
    assert automatic["reason_code"] == "provider_unavailable"
    assert automatic["outcome_axes"] == row["outcome_axes"]

    # The shared projection and the explicit tool remain full. Only startup's
    # automatic predecessor injection is narrowed.
    assert task_result_authority_projection(row)["result"] == full_result
    explicit = json.loads(_get_task_result(
        ToolContext(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            budget_drive_root=str(tmp_path),
        ),
        task_id,
        include_authority=True,
    ))
    assert explicit["authority"]["result"] == full_result
    assert tail in explicit["authority"]["result"]


def test_model_and_legacy_automatic_authority_remain_full(tmp_path):
    from ouroboros.agent_startup_checks import validate_task_authority_sources

    full_result = "complete model answer\n" + ("m" * 20_000)
    env = SimpleNamespace(drive_root=tmp_path, budget_drive_root=tmp_path)
    for task_id, origin in (("model-final", "model_final"), ("legacy", None)):
        row = {
            "task_id": task_id,
            "status": "completed",
            "result": full_result,
        }
        if origin is not None:
            row["terminal_origin"] = origin
        _write_result(tmp_path, row)
        task = {
            "id": f"continue-{task_id}",
            "budget_drive_root": str(tmp_path),
            "predecessor_authority_source": _authority_source(task_id),
        }

        assert validate_task_authority_sources(env, task) == {}
        assert task["predecessor_authority"]["result"] == full_result
