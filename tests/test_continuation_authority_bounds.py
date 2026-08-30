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
    assert result["source_ref"] == {**source, "field": "authority.result"}
    assert result["preview"].startswith(full_result[:200])
    assert "OMISSION NOTE" in result["preview"]
    assert tail not in json.dumps(automatic)
    assert len(json.dumps(automatic)) < 40_000
    # The bounded continuation envelope (delegation-usefulness, owner
    # 2026-08-30): identity + digest + pull source instead of the recursive
    # full-body copy that compiled 300K+ work orders.
    assert automatic["kind"] == "bounded_continuation_envelope"
    assert automatic["authority_chars"] > len(full_result)
    assert len(automatic["authority_sha256"]) == 64
    assert automatic["status"] == "failed"
    assert automatic["reason_code"] == "provider_unavailable"
    assert automatic["outcome_axes"] == row["outcome_axes"]
    assert "task_contract" not in automatic, "no nested contract - no recursion"

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


def test_automatic_authority_rides_whole_or_pointer(tmp_path):
    """Decoupled contract (delegation-usefulness, owner 2026-08-30): the
    automatic predecessor injection is a bounded envelope for EVERY origin.
    A result that fits one ordinary tool-result budget rides whole (no pull
    round for small hops); a bigger one rides as a bounded preview beside the
    named pull source - the full body stays in task_results, explicitly
    readable (the sibling salvage test pins that half)."""
    from ouroboros.agent_startup_checks import (
        _AUTOMATIC_HOST_SALVAGE_RESULT_CHARS,
        validate_task_authority_sources,
    )

    env = SimpleNamespace(drive_root=tmp_path, budget_drive_root=tmp_path)
    small_result = "concise model answer"
    big_result = "complete model answer\n" + (
        "m" * (_AUTOMATIC_HOST_SALVAGE_RESULT_CHARS + 5_000)
    )
    for task_id, full_result in (("model-small", small_result), ("model-big", big_result)):
        _write_result(tmp_path, {
            "task_id": task_id, "status": "completed", "result": full_result,
            "terminal_origin": "model_final",
        })
        task = {
            "id": f"continue-{task_id}",
            "budget_drive_root": str(tmp_path),
            "predecessor_authority_source": _authority_source(task_id),
        }
        assert validate_task_authority_sources(env, task) == {}
        automatic = task["predecessor_authority"]
        assert automatic["kind"] == "bounded_continuation_envelope"
        if full_result is small_result:
            assert automatic["result"] == full_result, "small hops ride whole"
        else:
            assert automatic["result"]["kind"] == "bounded_result_preview"
            assert automatic["result"]["full_chars"] == len(full_result)
            assert "OMISSION NOTE" in automatic["result"]["preview"]
