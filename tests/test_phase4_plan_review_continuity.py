from __future__ import annotations

import json

import httpx
import pytest

from tests import test_plan_review_engine as plan_review_engine


plan_review_harness_fixture = pytest.fixture(name="_harness")(
    plan_review_engine.harness.__wrapped__
)


def _finding(index: int, klass: str = "note") -> dict:
    return {
        "id": f"f{index}",
        "class": klass,
        "breaks": "goal" if klass == "blocking" else "",
        "locator": "",
        "summary": f"finding {index}",
        "recommendation": "repair it",
    }


def test_33rd_blocking_finding_is_aggregated() -> None:
    from ouroboros.tools.plan_spec import aggregate, validate_findings

    raw = [_finding(i) for i in range(1, 33)] + [_finding(33, "blocking")]
    findings, disclosures, _seen = validate_findings(
        raw, spec_ids={"goal"}, seen_locators=(), slot="slot_a",
    )
    result = aggregate([
        {"slot": "slot_a", "model": "m/a", "ok": True, "findings": findings},
    ], quorum=1)

    assert disclosures == []
    assert len(findings) == 33
    assert result["aggregate"] == "REVISE_PLAN"
    assert result["counts"]["blocking"] == 1
    assert result["findings"][-1]["finding_id"] == "slot_a:f33"


def test_exact_evidence_selectors_return_the_requested_slice(tmp_path) -> None:
    from ouroboros.tools.plan_evidence import resolve_evidence

    source = tmp_path / "evidence.txt"
    # The resolver's exact-byte contract must not depend on the host newline
    # translation performed by Path.write_text on Windows.
    source.write_bytes(b"one\ntwo\nthree\nfour\nfive\n")
    manifest = resolve_evidence(
        ["evidence.txt::lines=3-4", "evidence.txt::tail=5"],
        active_root=tmp_path,
        allowed_roots=[tmp_path],
    )

    assert manifest["omissions"] == []
    assert manifest["attached"][0]["text"] == "three\nfour\n"
    assert manifest["attached"][0]["selector"] == {
        "kind": "line_range", "start": 3, "end": 4,
    }
    assert manifest["attached"][1]["text"] == "five\n"
    assert manifest["attached"][1]["selector"] == {"kind": "tail", "bytes": 5}


def test_symbol_selector_uses_the_qualified_definition(tmp_path) -> None:
    from ouroboros.tools.plan_evidence import resolve_evidence

    source = tmp_path / "subject.py"
    source.write_text(
        "class First:\n    def decide(self):\n        return 'wrong'\n\n"
        "class Second:\n    def decide(self):\n        return 'exact'\n",
        encoding="utf-8",
    )
    manifest = resolve_evidence(
        ["subject.py::symbol=Second.decide"], active_root=tmp_path,
        allowed_roots=[tmp_path],
    )

    assert manifest["omissions"] == []
    assert "return 'exact'" in manifest["attached"][0]["text"]
    assert "return 'wrong'" not in manifest["attached"][0]["text"]


def test_requested_tail_preempts_a_full_120k_declared_pack(_harness) -> None:
    from tests.test_plan_review_engine import CLEAN, DECK_SPEC, _call, _finding, _user_text

    for index, char in enumerate("abc", start=1):
        (_harness.workspace / f"bulk-{index}.txt").write_text(char * 40_000, encoding="utf-8")
    decisive = _harness.workspace / "decisive.txt"
    decisive.write_text("x" * 50_000 + "DECISIVE_TAIL\n", encoding="utf-8")
    spec = {**DECK_SPEC, "evidence": [f"bulk-{index}.txt" for index in range(1, 4)]}
    ask = json.dumps([
        _finding(
            "tail", "need_evidence", breaks="goal", locator="decisive.txt::tail=64",
            summary="need the decisive tail",
        )
    ])
    _harness.install({"s1": ask, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx(), spec=spec)

    substrate = _harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx(), spec=spec)

    current_user = _user_text(substrate.calls[0]["request"].slot_messages["s1"][-1]["content"])
    assert "DECISIVE_TAIL" in current_user


def test_missing_requested_evidence_cannot_close_clean(_harness) -> None:
    from tests.test_plan_review_engine import CLEAN, _call, _control, _finding

    ask = json.dumps([
        _finding(
            "f1", "need_evidence", breaks="goal", locator="missing.md::lines=1-2",
            summary="read the exact lines",
        )
    ])
    _harness.install({"s1": ask, "s2": CLEAN, "s3": CLEAN})
    assert _control(_call(_harness.make_ctx())) == {
        "outcome": "REVIEW_REQUIRED", "closed": False,
    }

    substrate = _harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    out = _call(_harness.make_ctx())

    assert "cannot_verify" in out
    assert _control(out)["closed"] is False
    assert substrate.calls == []


def test_wave_9_and_65_remain_exactly_readable_after_hot_trimming(tmp_path) -> None:
    from ouroboros.task_results import load_plan_review_state, record_plan_review_wave
    from ouroboros.tools.plan_review_runtime import (
        persist_plan_review_wave_artifact,
        read_plan_review_wave_artifact,
    )

    task_id = "task-review"
    refs = {}
    for index in range(1, 66):
        fingerprint = f"{index:064x}"
        exact = {
            "schema_version": 1,
            "cycle_index": index,
            "request_fingerprint": fingerprint,
            "findings": [{"id": f"tail-{index}", "summary": "x" * 5000}],
            "reviewer_outputs": [{"slot_id": "s1", "text": f"exact-wave-{index}"}],
        }
        ref = persist_plan_review_wave_artifact(tmp_path, task_id, exact)
        refs[index] = ref
        record_plan_review_wave(tmp_path, task_id, {
            "schema_version": 2,
            "cycle_index": index,
            "request_fingerprint": fingerprint,
            "spec": {"goal": "g"},
            "findings": exact["findings"],
            "aggregate": "GREEN",
            "closed": True,
            "paid": True,
            "dispositions": [],
            "wave_artifact": ref,
        })

    state = load_plan_review_state(tmp_path, task_id)
    assert len(state["waves"]) == 64
    assert sum(1 for wave in state["waves"] if not wave.get("compact")) == 8
    wave9 = next(w for w in state["waves"] if w["cycle_index"] == 9)
    assert wave9["compact"] is True
    assert wave9["wave_artifact"] == refs[9]
    assert read_plan_review_wave_artifact(tmp_path, task_id, refs[9])["reviewer_outputs"][0]["text"] == "exact-wave-9"
    assert read_plan_review_wave_artifact(tmp_path, task_id, refs[65])["reviewer_outputs"][0]["text"] == "exact-wave-65"


def test_compacted_wave_keeps_the_full_blocking_count() -> None:
    from ouroboros.task_results import _compact_plan_review_wave

    wave = {
        "cycle_index": 1,
        "request_fingerprint": "a" * 64,
        "aggregate": "REVISE_PLAN",
        "findings": [_finding(index) for index in range(1, 33)],
        "findings_total": 33,
        "counts": {"blocking": 1, "note": 32, "need_evidence": 0},
        "closed": False,
        "paid": True,
    }

    assert _compact_plan_review_wave(wave)["counts"]["blocking"] == 1


@pytest.mark.parametrize("current_ids", [("s1", "s2"), ("s1", "s2", "s3", "s4")])
def test_continuation_refuses_a_changed_reviewer_assignment_set(tmp_path, current_ids) -> None:
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools.plan_review_artifacts import continuation_inputs, persist_wave

    prior_slots = [ReviewSlot(slot_id=sid, model=f"model-{sid}") for sid in ("s1", "s2", "s3")]
    exact = {
        "schema_version": 1,
        "cycle_index": 1,
        "request_fingerprint": "b" * 64,
        "slots": [
            {
                "slot_id": slot.slot_id, "model": slot.model, "effort": slot.effort,
                "route": "api_chat", "session_target": "", "session_profile": "",
            }
            for slot in prior_slots
        ],
        "reviewer_outputs": [
            {
                "slot_id": slot.slot_id,
                "request_messages": [{"role": "user", "content": "prior"}],
                "text": "[]",
            }
            for slot in prior_slots
        ],
    }
    ref = persist_wave(tmp_path, "task-1", exact)
    current = [ReviewSlot(slot_id=sid, model=f"model-{sid}") for sid in current_ids]

    _slots, messages, threads, error = continuation_inputs(
        tmp_path, "task-1", {"wave_artifact": ref}, current, user_content="continue",
    )

    assert error == "prior_reviewer_assignment_set_changed"
    assert messages == {} and threads == {}


def test_continuation_repins_the_prior_applied_session_profile(tmp_path) -> None:
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools.plan_review_artifacts import continuation_inputs, persist_wave, slot_row

    prior = ReviewSlot(
        slot_id="s1", model="claude=fable", route="agent_session",
        session_target="claude=fable", session_profile="profile-a",
    )
    exact = {
        "schema_version": 1,
        "cycle_index": 1,
        "request_fingerprint": "c" * 64,
        "slots": [slot_row(prior)],
        "reviewer_outputs": [{
            "slot_id": "s1", "review_thread_id": "thread-1",
            "applied_profile": "profile-b", "text": "need evidence",
        }],
    }
    ref = persist_wave(tmp_path, "task-1", exact)

    rebound, _messages, threads, error = continuation_inputs(
        tmp_path, "task-1", {"wave_artifact": ref}, [prior], user_content="continue",
    )

    assert error is None
    assert threads == {"s1": "thread-1"}
    assert rebound[0].session_profile == "profile-b"


def test_api_chat_continuation_uses_exact_slot_transcript() -> None:
    from ouroboros.review_execution import ApiChatReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    prior = [
        {"role": "system", "content": "system-v1"},
        {"role": "user", "content": "plan-v1"},
        {"role": "assistant", "content": "need exact tail"},
        {"role": "user", "content": "plan-v1 + exact tail"},
    ]
    request = ReviewRequest(
        surface="plan_review",
        goal="review",
        task_id="task-1",
        messages=[{"role": "user", "content": "wrong common transcript"}],
        slot_messages={"slot_a": prior},
    )
    slot = ReviewSlot(slot_id="slot_a", model="m/a")
    executor = ApiChatReviewExecutor(ReviewAssignment(request=request, slot=slot))

    assert executor.messages == prior
    assert executor._kwargs()["model"] == "m/a"


@pytest.mark.parametrize(
    "prior_messages",
    [[], [{}], [{"role": "user"}], [{"role": "", "content": "prior"}]],
)
def test_api_chat_continuation_refuses_partial_or_invalid_transcript(
    tmp_path, prior_messages,
) -> None:
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools.plan_review_artifacts import continuation_inputs, persist_wave, slot_row

    slot = ReviewSlot(slot_id="s1", model="model-s1")
    exact = {
        "schema_version": 1,
        "cycle_index": 1,
        "request_fingerprint": "d" * 64,
        "slots": [slot_row(slot)],
        "reviewer_outputs": [{
            "slot_id": "s1", "request_messages": prior_messages, "text": "need evidence",
        }],
    }
    ref = persist_wave(tmp_path, "task-1", exact)

    _slots, messages, threads, error = continuation_inputs(
        tmp_path, "task-1", {"wave_artifact": ref}, [slot], user_content="continue",
    )

    assert error == "prior_api_transcript_invalid:s1"
    assert messages == {} and threads == {}


def test_exact_wave_preserves_an_explicit_empty_slot_transcript() -> None:
    from ouroboros.tools.plan_review_artifacts import exact_wave

    result = exact_wave(
        {"cycle_index": 1}, plan_prose="plan", manifest={}, slots=[], rows=[{
            "slot_id": "s1", "route": "api_chat", "model": "m",
        }], system_prompt="system", user_content="user", session_task="",
        slot_messages={"s1": []},
    )

    assert result["reviewer_outputs"][0]["request_messages"] == []


def test_review_thread_receipt_requires_run_and_turn_to_match() -> None:
    from ouroboros.review_thread_continuity import review_thread_receipt
    from ouroboros.review_execution import ReviewRouteUnavailable

    class Gateway:
        def get_thread(self, _thread_id):
            return {
                "thread": {"headRunId": "other-run"},
                "turns": [{"id": "turn-1", "runId": "other-run"}],
                "sessions": [],
            }

        def get_run_artifact(self, _run_id, _path):
            return b""

    with pytest.raises(ReviewRouteUnavailable) as excinfo:
        review_thread_receipt(Gateway(), "thread-1", "target-run", "turn-1")

    assert excinfo.value.code == "review_thread_receipt_missing"


def test_claudexor_gateway_thread_turn_contract(monkeypatch) -> None:
    from ouroboros.gateways.claudexor import ClaudexorGateway, DaemonEndpoint

    seen: list[tuple[str, str, dict, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        seen.append((request.method, request.url.path, body, request.headers.get("Idempotency-Key", "")))
        if request.url.path == "/v2/threads":
            return httpx.Response(200, json={"id": "thread-1"})
        if request.url.path == "/v2/threads/thread-1/turns":
            return httpx.Response(200, json={
                "jobId": "job-2", "runId": "run-2", "runDir": "/tmp/run-2",
                "threadId": "thread-1", "turnId": "turn-2",
            })
        if request.url.path == "/v2/threads/thread-1":
            return httpx.Response(200, json={
                "thread": {"id": "thread-1", "headRunId": "run-2"},
                "sessions": [{
                    "id": "session-1", "threadId": "thread-1", "harnessId": "claude",
                    "profileId": "profile-b", "state": "live",
                }],
                "turns": [{
                    "id": "turn-2", "threadId": "thread-1", "runId": "run-2",
                    "continuity": {
                        "kind": "packet", "packetTurns": 1, "summarized": False,
                        "laneSwitchedFrom": {"harness": "claude", "profileId": "profile-a"},
                    },
                }],
            })
        return httpx.Response(404, json={"code": "not_found", "message": "no"})

    gateway = ClaudexorGateway(DaemonEndpoint(host="127.0.0.1", port=1, token="token"))
    gateway._client.close()
    gateway._client = httpx.Client(
        base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer token"},
    )
    try:
        thread = gateway.create_thread({
            "scope": {"kind": "project", "root": "/repo"},
            "mode": "ask", "authPreference": "subscription",
            "primaryHarness": "claude", "eligibleHarnesses": ["claude"],
            "credentialProfileId": "profile-a", "access": "readonly",
        }, idempotency_key="thread-key")
        turn = gateway.start_thread_turn(
            thread["id"], {"prompt": "exact evidence", "mode": "ask"},
            idempotency_key="turn-key",
        )
        detail = gateway.get_thread(thread["id"])
    finally:
        gateway.close()

    assert turn["threadId"] == "thread-1" and turn["runId"] == "run-2"
    assert detail["turns"][0]["continuity"]["kind"] == "packet"
    assert detail["sessions"][0]["profileId"] == "profile-b"
    assert seen[0][3] == "thread-key" and seen[1][3] == "turn-key"


def test_continued_thread_explicitly_repins_the_expected_profile() -> None:
    from ouroboros.review_thread_continuity import start_review_thread_turn

    captured = {}

    class Gateway:
        def start_thread_turn(self, thread_id, request, *, idempotency_key):
            captured.update({
                "thread_id": thread_id, "request": request,
                "idempotency_key": idempotency_key,
            })
            return {"runId": "run-2", "threadId": thread_id, "turnId": "turn-2"}

    start_review_thread_turn(Gateway(), "thread-1", {
        "prompt": "continue", "model": "fable", "harnesses": ["claude"],
        "credentialProfileId": "profile-a", "_thread_id": "thread-1",
        "scope": {"kind": "project", "root": "/repo"},
    }, idempotency_key="turn-key")

    assert captured["request"]["model"] == "fable"
    assert captured["request"]["harnesses"] == ["claude"]
    assert captured["request"]["credentialProfileId"] == "profile-a"


def test_initial_unpinned_thread_omits_profile_but_its_turn_sends_explicit_null() -> None:
    from types import SimpleNamespace

    from ouroboros.gateways.claudexor import ClaudexorGateway, DaemonEndpoint
    from ouroboros.review_thread_continuity import ensure_review_thread, start_review_thread_turn

    captured = {}

    class Custody:
        @staticmethod
        def idempotency_key(*parts):
            return ":".join(str(part) for part in parts)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if request.url.path == "/v2/threads":
            # Installed ControlThreadCreateRequest: nonblank string or omission.
            assert "credentialProfileId" not in body
            captured["create"] = body
            return httpx.Response(200, json={"id": "thread-1"})
        if request.url.path == "/v2/threads/thread-1/turns":
            # Installed ControlThreadTurnRequest: explicit null clears a sticky pin.
            assert "credentialProfileId" in body and body["credentialProfileId"] is None
            captured["turn"] = body
            return httpx.Response(200, json={
                "threadId": "thread-1", "turnId": "turn-1", "runId": "run-1",
            })
        return httpx.Response(404, json={"code": "not_found", "message": "no"})

    gateway = ClaudexorGateway(DaemonEndpoint(host="127.0.0.1", port=1, token="token"))
    gateway._client.close()
    gateway._client = httpx.Client(
        base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer token"},
    )
    try:
        thread_id = ensure_review_thread(
            gateway, Custody(), "", route=SimpleNamespace(route_id="claude", profile_id=""),
            root="/repo", surface="plan_review", slot_id="s1", task_id="task-1",
        )
        start_review_thread_turn(
            gateway, thread_id,
            {"prompt": "review", "credentialProfileId": None, "_thread_id": thread_id},
            idempotency_key="turn-key",
        )
    finally:
        gateway.close()

    assert thread_id == "thread-1"
    assert captured["turn"]["credentialProfileId"] is None


def test_profile_rotation_receipt_is_read_from_the_settled_run_events() -> None:
    from ouroboros.review_thread_continuity import profile_rotation_receipts

    class Gateway:
        def get_run_artifact(self, run_id, path):
            assert (run_id, path) == ("run-2", "events.jsonl")
            return (json.dumps({
                "seq": 7,
                "type": "route.profile.rotated",
                "payload": {
                    "from_profile_id": "profile-a", "to_profile_id": "profile-b",
                    "reason": "profile_headroom_preflight", "resets_at": "later",
                },
            }) + "\n").encode()

    assert profile_rotation_receipts(Gateway(), "run-2") == [{
        "seq": 7,
        "type": "route.profile.rotated",
        "from_profile_id": "profile-a",
        "to_profile_id": "profile-b",
        "reason": "profile_headroom_preflight",
        "attempt_id": "",
        "resets_at": "later",
    }]


def _rotation(seq, source, target, *, reason="vendor_limit_rejected"):
    return {
        "seq": seq, "type": "route.profile.rotated",
        "from_profile_id": source, "to_profile_id": target,
        "reason": reason, "attempt_id": f"attempt-{seq}", "resets_at": "later",
    }


@pytest.mark.parametrize(
    ("applied", "rotations", "expected_status", "expected_reason"),
    [
        (
            "profile-b", [_rotation(10, "profile-a", "profile-b")],
            "typed_rotation", "typed_rotation_chain",
        ),
        (
            "profile-c",
            [_rotation(10, "profile-a", "profile-b"), _rotation(20, "profile-b", "profile-c")],
            "typed_rotation", "typed_rotation_chain",
        ),
        (
            "profile-c",
            [_rotation(10, "profile-a", "profile-b"), _rotation(20, "profile-x", "profile-c")],
            "cannot_verify", "rotation_chain_gap",
        ),
        (
            "profile-c", [_rotation(10, "profile-a", "profile-b")],
            "cannot_verify", "rotation_terminal_mismatch",
        ),
        (
            "profile-c",
            [_rotation(20, "profile-a", "profile-b"), _rotation(10, "profile-b", "profile-c")],
            "cannot_verify", "rotation_event_order_invalid",
        ),
        (
            "profile-b",
            [{**_rotation(10, "profile-a", "profile-b"), "reason": ""}],
            "cannot_verify", "rotation_event_malformed",
        ),
    ],
    ids=("one-hop", "multi-hop", "broken-gap", "terminal-mismatch", "reordered", "malformed"),
)
def test_profile_continuity_folds_only_one_ordered_engine_rotation_chain(
    applied, rotations, expected_status, expected_reason,
) -> None:
    from ouroboros.review_thread_continuity import profile_continuity_receipt

    receipt = profile_continuity_receipt("profile-a", applied, rotations)

    assert receipt["status"] == expected_status
    assert receipt["verification_reason"] == expected_reason
    if expected_status == "typed_rotation":
        assert receipt["rotation_receipts"] == rotations
        assert receipt["rotation_receipt"] == rotations[-1]
    else:
        assert receipt["rotation_receipt"] == {}


def test_agent_session_continuation_passes_the_real_thread_id(monkeypatch, tmp_path) -> None:
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    captured = {}

    def fake_run(*, prompt, root, custody_drive, invocation):
        captured.update({"prompt": prompt, "root": root, "invocation": invocation})
        return {
            "run_id": "run-2", "thread_id": "thread-1", "turn_id": "turn-2",
            "thread_receipt": {"continuity": {"kind": "native_resume"}},
            "text": "[]\nNO_FINDINGS", "conformance": "passed", "schema_asked": True,
            "custody_durable": True, "settlement": "settled", "route_id": "claude",
            "effective_route_ids": ["claude"], "model": "fable", "spend": 0.0,
            "spend_estimated": False, "applied_profile": "profile-a", "applied_access": "readonly",
            "auth_route_receipt": {
                "requested": "subscription", "effective": "subscription",
                "reason": "quota_exhausted", "profileId": "profile-a",
            },
        }

    monkeypatch.setattr("ouroboros.review_execution.run_delegated_review_session", fake_run)
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="task-1",
        session_root=str(tmp_path), session_task="review exact evidence",
        session_threads={"slot_a": "thread-1"},
        policy={"output_contract": "return findings"},
    )
    slot = ReviewSlot(
        slot_id="slot_a", model="fable", route="agent_session",
        session_target="claude=fable", session_profile="profile-a",
    )
    result = AgentSessionReviewExecutor(
        ReviewAssignment(request=request, slot=slot, custody_root=tmp_path)
    ).execute()

    assert captured["invocation"].thread_id == "thread-1"
    assert captured["invocation"].use_thread is True
    assert result.usage["review_thread_id"] == "thread-1"
    assert result.usage["review_thread_receipt"]["continuity"]["kind"] == "native_resume"
    assert result.usage["auth_route_receipt"]["reason"] == "quota_exhausted"


def test_agent_session_rejects_unexplained_applied_profile_drift(monkeypatch, tmp_path) -> None:
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    def fake_run(*, prompt, root, custody_drive, invocation):
        return {
            "run_id": "run-2", "thread_id": "thread-1", "turn_id": "turn-2",
            "thread_receipt": {"continuity": {"kind": "native_resume"}},
            "profile_continuity_receipt": {
                "expected_profile": "profile-a", "applied_profile": "profile-b",
                "status": "cannot_verify", "rotation_receipt": {},
            },
            "text": "[]\nNO_FINDINGS", "conformance": "passed", "schema_asked": True,
            "custody_durable": True, "settlement": "settled", "route_id": "claude",
            "effective_route_ids": ["claude"], "model": "fable", "spend": 0.0,
            "spend_estimated": False, "applied_profile": "profile-b", "applied_access": "readonly",
            "auth_route_receipt": {
                "requested": "subscription", "effective": "subscription",
                "reason": "subscription_preferred", "profileId": "profile-b",
            },
        }

    monkeypatch.setattr("ouroboros.review_execution.run_delegated_review_session", fake_run)
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="task-1",
        session_root=str(tmp_path), session_task="review exact evidence",
        session_threads={"slot_a": "thread-1"},
        policy={"output_contract": "return findings"},
    )
    slot = ReviewSlot(
        slot_id="slot_a", model="fable", route="agent_session",
        session_target="claude=fable", session_profile="profile-a",
    )

    result = AgentSessionReviewExecutor(
        ReviewAssignment(request=request, slot=slot, custody_root=tmp_path)
    ).execute()

    assert result.raw_text == ""
    assert result.usage["profile_continuity_receipt"] == {
        "expected_profile": "profile-a",
        "applied_profile": "profile-b",
        "status": "cannot_verify",
        "rotation_receipt": {},
    }


def test_agent_session_accepts_only_a_typed_profile_rotation_receipt(monkeypatch, tmp_path) -> None:
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    rotation = {
        "type": "route.profile.rotated", "from_profile_id": "profile-a",
        "to_profile_id": "profile-b", "reason": "vendor_limit_rejected",
        "attempt_id": "a01", "resets_at": "2026-08-22T00:00:00Z",
    }

    def fake_run(*, prompt, root, custody_drive, invocation):
        return {
            "run_id": "run-2", "thread_id": "thread-1", "turn_id": "turn-2",
            "thread_receipt": {
                "continuity": {
                    "kind": "packet", "laneSwitchedFrom": {
                        "harness": "claude", "profileId": "profile-a",
                    },
                },
            },
            "profile_continuity_receipt": {
                "expected_profile": "profile-a", "applied_profile": "profile-b",
                "status": "typed_rotation", "rotation_receipt": rotation,
            },
            "text": "[]\nNO_FINDINGS", "conformance": "passed", "schema_asked": True,
            "custody_durable": True, "settlement": "settled", "route_id": "claude",
            "effective_route_ids": ["claude"], "model": "fable", "spend": 0.0,
            "spend_estimated": False, "applied_profile": "profile-b", "applied_access": "readonly",
            "auth_route_receipt": {
                "requested": "subscription", "effective": "subscription",
                "reason": "subscription_preferred", "profileId": "profile-b",
            },
        }

    monkeypatch.setattr("ouroboros.review_execution.run_delegated_review_session", fake_run)
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="task-1",
        session_root=str(tmp_path), session_task="review exact evidence",
        session_threads={"slot_a": "thread-1"},
        policy={"output_contract": "return findings"},
    )
    slot = ReviewSlot(
        slot_id="slot_a", model="fable", route="agent_session",
        session_target="claude=fable", session_profile="profile-a",
    )

    result = AgentSessionReviewExecutor(
        ReviewAssignment(request=request, slot=slot, custody_root=tmp_path)
    ).execute()

    assert result.raw_text == "[]\nNO_FINDINGS"
    assert result.usage["profile_continuity_receipt"] == {
        "expected_profile": "profile-a",
        "applied_profile": "profile-b",
        "status": "typed_rotation",
        "rotation_receipt": rotation,
    }


def test_disposition_supersedes_the_exact_wave_before_hot_state(_harness) -> None:
    from ouroboros.task_results import load_plan_review_state
    from ouroboros.tools import plan_review as pr
    from ouroboros.tools.plan_review_artifacts import read_wave
    from tests.test_plan_review_engine import CLEAN, _call, _finding

    note = json.dumps([_finding("n1", "note")])
    _harness.install({"s1": note, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx())
    prior = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]
    prior_ref = prior["wave_artifact"]

    pr._handle_plan_task(_harness.make_ctx(), review_disposition={
        "review_fingerprint": prior["request_fingerprint"],
        "items": [{"finding_id": "s1:n1", "decision": "accept", "rationale": "will do"}],
    })

    stored = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]
    assert stored["wave_artifact"] != prior_ref
    exact = read_wave(_harness.drive, "task-1", stored["wave_artifact"])
    assert exact["dispositions"][0]["finding_id"] == "s1:n1"
    assert exact["supersedes_wave_artifact"] == prior_ref
    assert exact["artifact_meta"]["retention_owner"] == "task_artifact_store"


def test_33rd_blocker_is_dispositionable(_harness) -> None:
    from ouroboros.task_results import load_plan_review_state
    from ouroboros.tools import plan_review as pr
    from tests.test_plan_review_engine import CLEAN, _call

    findings = json.dumps([_finding(index) for index in range(1, 33)] + [_finding(33, "blocking")])
    _harness.install({"s1": findings, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx())
    wave = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]

    out = pr._handle_plan_task(_harness.make_ctx(), review_disposition={
        "review_fingerprint": wave["request_fingerprint"],
        "items": [{"finding_id": "s1:f33", "decision": "reject", "rationale": "not valid"}],
    })

    assert "unknown finding ids" not in out
    assert "blocking_finding_below_quorum_stays_open" in out
