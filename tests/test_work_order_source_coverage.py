"""Production-shaped custody and acceptance checks for over-budget work orders."""

from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace


def _fixture(tmp_path):
    from ouroboros.subagent_work_order import (
        WorkOrderBudgetExceeded,
        build_work_order_source_request,
        canonical_work_order_source,
        compile_external_work_order,
    )
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    contract = {
        "objective": "Build the exact external patch " + ("x" * 250_100),
        "expected_output": "A reviewed patch",
    }
    task = {
        "id": "source-child",
        "objective": contract["objective"],
        "expected_output": contract["expected_output"],
        "task_contract": contract,
        "task_constraint": {},
        "workspace_root": str(repo),
        "workspace_mode": "external_workspace",
    }
    overflow = None
    try:
        compile_external_work_order(task)
    except WorkOrderBudgetExceeded as exc:
        overflow = exc
    else:  # pragma: no cover - the fixture must exercise the over-budget branch
        raise AssertionError("fixture unexpectedly fits the work-order budget")
    assert overflow is not None
    prompt, request = build_work_order_source_request(task, overflow)
    ctx = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path,
        workspace_root=repo,
        workspace_mode="external_workspace",
        task_id=task["id"],
        task_metadata={
            "task_constraint": {},
            "workspace_root": str(repo),
            "workspace_mode": "external_workspace",
        },
        task_contract=contract,
    )
    full_text, reason = canonical_work_order_source(ctx, request)
    assert not reason
    assert len(full_text) == overflow.chars
    return ctx, request, full_text, prompt


def _started_entry(tmp_path, request, sha):
    from ouroboros import delegate_custody as custody

    custody._CUSTODY.pop("source-run", None)
    entry = custody.RunCustody(
        run_id="source-run",
        task_id="source-child",
        route_id="claude",
        model="claude-fable-5",
        work_order_fingerprint=sha,
        work_order_coverage="partial",
        work_order_source_request=request,
        settled=True,
        execution_root=str(tmp_path / "snapshot"),
        target_root=str(tmp_path / "repo"),
    )
    assert custody.record_started(tmp_path, entry)
    custody._CUSTODY[entry.run_id] = entry
    return entry


def test_route_source_request_channel_fails_closed_on_unknown_manifest():
    from ouroboros.subagent_work_order import route_source_request_channel

    class Gateway:
        def harnesses(self):
            return [{"id": "route", "manifest": {"capabilities": {}}}]

    assert route_source_request_channel(Gateway(), "route") == {
        "status": "unverified",
        "reason": "interactive_capability_missing",
        "route": "route",
    }


def test_actor_first_coordination_appendix_refuses_without_truncation(monkeypatch):
    import ouroboros.tools.delegate as delegate

    authority = SimpleNamespace(delegated=False)
    monkeypatch.setattr(delegate, "_host_instructions", lambda *_a, **_k: "base")
    monkeypatch.setattr(delegate, "_ASSIGNMENT_FIELD_CHARS", 32)
    instructions, refusal = delegate._build_start_instructions(
        authority, coordination_context="x" * 100,
    )
    assert instructions == ""
    payload = json.loads(refusal)
    assert payload["reason"] == "coordination_context_over_budget"
    assert payload["coordination_context_chars"] == 100


def test_started_replay_keeps_partial_request_and_verified_ranges(tmp_path):
    from ouroboros import delegate_custody as custody

    ctx, request, full_text, _prompt = _fixture(tmp_path)
    entry = _started_entry(tmp_path, request, request["complete_sha256"])
    assert custody.replay(tmp_path)[entry.run_id].work_order_source_request == request
    assert custody.work_order_source_verification(entry)["status"] == "cannot_verify"
    assert not custody.record_source_range_verified(
        tmp_path,
        entry,
        start_char=0,
        end_char=len(full_text) + 1,
        complete_sha256=request["complete_sha256"],
        source=request["source"],
        text_sha256="f" * 64,
        text_chars=len(full_text) + 1,
    )
    assert custody.work_order_source_verification(entry)["status"] == "cannot_verify"
    assert custody.emit(tmp_path, custody.SOURCE_RANGE_VERIFIED, {
        "run_id": entry.run_id,
        "task_id": entry.task_id,
        "start_char": 0,
        "end_char": len(full_text) + 1,
        "complete_sha256": request["complete_sha256"],
        "source": request["source"],
        "text_sha256": "f" * 64,
        "text_chars": len(full_text) + 1,
    })
    custody._CUSTODY.clear()
    assert custody.work_order_source_verification(
        custody.replay(tmp_path)[entry.run_id]
    )["status"] == "cannot_verify"
    midpoint = len(full_text) // 2
    assert custody.record_source_range_verified(
        tmp_path,
        entry,
        start_char=0,
        end_char=midpoint,
        complete_sha256=request["complete_sha256"],
        source=request["source"],
        text_sha256="a" * 64,
        text_chars=midpoint,
    )
    assert custody.record_source_range_verified(
        tmp_path,
        entry,
        start_char=midpoint,
        end_char=len(full_text),
        complete_sha256=request["complete_sha256"],
        source=request["source"],
        text_sha256="b" * 64,
        text_chars=len(full_text) - midpoint,
    )
    custody._CUSTODY.clear()
    replayed = custody.replay(tmp_path)[entry.run_id]
    assert custody.work_order_source_verification(replayed)["status"] == "complete"


def test_orphaned_pending_recovery_preserves_partial_source_gate(tmp_path, monkeypatch):
    from ouroboros import delegate_custody as custody

    _ctx, request, _full_text, _prompt = _fixture(tmp_path)
    assert custody.record_start_requested(
        tmp_path,
        run_id="",
        task_id="source-child",
        invocation_id="source-invocation",
        idempotency_key="source-invocation",
        max_seconds=60,
        request={"prompt": "WORK ORDER SOURCE REQUEST", "access": "readonly"},
        project_id="",
        project_owned=False,
        route="claude",
        work_order_fingerprint=request["complete_sha256"],
        work_order_coverage="partial",
        work_order_source_request=request,
    )
    record = custody.pending_invocations(tmp_path)[0]
    monkeypatch.setattr(custody, "_reconcile_one", lambda *_args, **_kwargs: {"ok": True})

    class Gateway:
        def start_run(self, _request, *, idempotency_key=""):
            assert idempotency_key == "source-invocation"
            return {"runId": "source-recovered"}

    assert custody._recover_pending_invocation(tmp_path, Gateway(), record) == {"ok": True}
    recovered = custody.replay(tmp_path)["source-recovered"]
    assert recovered.work_order_coverage == "partial"
    assert recovered.work_order_source_request == request
    assert custody.work_order_source_verification(recovered)["status"] == "cannot_verify"


def test_existing_task_result_reader_returns_the_same_canonical_range(tmp_path):
    from ouroboros.task_results import task_result_path, write_task_result
    from ouroboros.subagent_work_order import canonical_work_order_source
    from ouroboros.tools.control import _get_task_result

    ctx, request, full_text, _prompt = _fixture(tmp_path)
    write_task_result(
        tmp_path,
        "source-child",
        "running",
        objective=ctx.task_contract["objective"],
        expected_output=ctx.task_contract["expected_output"],
        task_contract=dict(ctx.task_contract),
        task_constraint={},
    )
    stored = json.loads(task_result_path(tmp_path, "source-child", create=False).read_text())
    assert "workspace_root" not in stored
    assert "workspace_mode" not in stored
    expected_text, reason = canonical_work_order_source(ctx, request)
    assert not reason
    midpoint = len(full_text) // 2
    payload = json.loads(_get_task_result(
        ctx,
        "source-child",
        include_authority=True,
        include_work_order_source=True,
        source_start_char=0,
        source_end_char=midpoint,
    ))
    source = payload["work_order_source"]
    assert payload["source"] == request["source"]
    assert source["complete_sha256"] == request["complete_sha256"]
    assert source["complete_chars"] == request["complete_chars"]
    assert source["complete_sha256"] == hashlib.sha256(
        expected_text.encode("utf-8")
    ).hexdigest()
    assert source["complete_chars"] == len(expected_text)
    assert source["text"] == expected_text[:midpoint]


def test_terminal_partial_is_cannot_verify_and_apply_is_refused_but_reject_remains(tmp_path):
    from ouroboros import delegate_custody as custody
    from ouroboros.tools.delegate import _delivered_terminal_payload
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, request, _full_text, _prompt = _fixture(tmp_path)
    entry = _started_entry(tmp_path, request, request["complete_sha256"])
    terminal = _delivered_terminal_payload(
        ctx,
        entry.run_id,
        {"summary": {"state": "succeeded", "model": "claude-fable-5", "effectiveAccess": "readonly"}, "lastSeq": 1},
        SimpleNamespace(access="readonly", delegated=False),
        entry,
        None,
    )
    assert terminal["work_order_verification"]["status"] == "cannot_verify"
    assert terminal["acceptance_status"] == "cannot_verify"
    apply_out = _integrate_delegated_patch(ctx, entry.run_id, "apply", "")
    assert "SOURCE_UNRESOLVED" in apply_out
    reject_out = _integrate_delegated_patch(ctx, entry.run_id, "reject", "not accepted")
    assert "SOURCE_UNRESOLVED" not in reject_out
    # Once the durable interval union is complete, the source gate opens and the
    # normal capture/apply guards own the next answer (there is no false permanent
    # refusal just because this was once over budget).
    assert custody.record_source_range_verified(
        tmp_path,
        entry,
        start_char=0,
        end_char=request["complete_chars"],
        complete_sha256=request["complete_sha256"],
        source=request["source"],
        text_sha256="c" * 64,
        text_chars=request["complete_chars"],
    )
    apply_after_complete = _integrate_delegated_patch(ctx, entry.run_id, "apply", "")
    assert "SOURCE_UNRESOLVED" not in apply_after_complete
    custody._CUSTODY.clear()
