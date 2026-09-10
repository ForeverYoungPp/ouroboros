"""Review-lane contracts that outlived Claudexor's retirement (Seed 0).

The agent_session transport is gone: ``run_delegated_review_session`` refuses
typed, and ``ReviewSessionWaitingOnUser`` survives only as an exported name of
the deleted poller. What is pinned here is what still runs — verdict
canonicalization and its light-model extraction rail, the api_chat route's
delivery/stamp boundaries, route configuration, and the scope/triad wiring
around the (refusing) session route.

Deterministic OFFLINE fixtures (owner test rule: cheap, weak, no live harness):
a FakeGateway stands in for the Claudexor /v2 control plane with the same
semantics the real engine documents — capability catalog, idempotent starts,
terminal details, artifact serving — and a FakeLLM answers the one sanctioned
light-model extraction call. No network, no daemon, no subscription.
"""

import json
from types import SimpleNamespace

import pytest

from ouroboros.review_execution import (
    REVIEW_SESSION_ROUTE_ENV,
    SCOPE_REVIEW_ROUTES_ENV,
    TRIAD_REVIEW_ROUTES_ENV,
    ReviewRouteKind,
    canonicalize_session_verdict,
    configured_review_routes,
)
from ouroboros.review_substrate import (
    ReviewRequest,
    ReviewSlot,
    reviewer_slots,
    run_review_request,
    scope_reviewer_slots,
)
from ouroboros.triad_review import empty_array_is_verified_clean


@pytest.fixture(autouse=True)
def _owned_gateway_uses_each_test_transport(monkeypatch):
    from ouroboros import claudexor_daemon
    from ouroboros.gateways import claudexor as gateway_module

    monkeypatch.setattr(
        claudexor_daemon,
        "ensure_owned_gateway",
        lambda: gateway_module.ClaudexorGateway(),
    )


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------


def _terminal_detail(text, *, state="succeeded", conformance="", truncated=False,
                     path="", reported_bytes=None, model="fake-small"):
    summary = {
        "state": state,
        "model": model,
        "spendUsd": 0.0,
        "spendEstimated": False,
    }
    if conformance:
        summary["outputConformance"] = conformance
    primary = {"text": text, "truncated": truncated}
    if path:
        primary["path"] = path
    if reported_bytes is not None:
        primary["bytes"] = reported_bytes
    return {"summary": summary, "primaryOutput": primary, "lastSeq": 3}


class FakeGateway:
    """The /v2 surface the executor drives, with recorded evidence."""

    instances = []
    catalog_entry = {}
    manifest_capabilities = {}
    detail = {}
    # Optional scripted behaviors.
    start_error = None            # exception raised on the FIRST start only
    poll_error = None             # exception raised on the FIRST terminal read only
    artifact_bytes = None
    artifact_error = None
    nonterminal = False
    project_unregistered = False

    def __init__(self, *args, **kwargs):
        FakeGateway.instances.append(self)
        self.start_requests = []
        self.start_keys = []
        self.cancels = []
        self.artifact_gets = []
        self.health_asked = []
        self.run_gets = []
        self.project_lookups = []
        self.registrations = []
        self.removals = []
        self.engine_version = "3.3.7"

    @classmethod
    def reset(cls):
        # Faithful to the real /v2 split: the agent-capability catalog row
        # (CatalogHarness) carries NO transport flags — json_schema_output and
        # interactive live only on the /v2/harnesses row's manifest. A fixture
        # that invents a catalog flag would keep alive exactly the dead read
        # this suite exists to catch.
        cls.instances = []
        cls.catalog_entry = {
            "id": "fake-review", "enabled": True, "status": "ok",
            "accessProfilesSupported": ["readonly", "workspace_write"],
        }
        cls.manifest_capabilities = {"json_schema_output": True}
        cls.detail = _terminal_detail('{"findings": []}', conformance="passed")
        cls.start_error = None
        cls.poll_error = None
        cls.artifact_bytes = None
        cls.artifact_error = None
        cls.nonterminal = False
        cls.project_unregistered = False

    def handshake(self, **_kw):
        return {"compatible": True, "protocolMajor": 3, "engine": {"version": self.engine_version}}

    def agent_capabilities(self):
        return {"harnesses": [dict(FakeGateway.catalog_entry)]}

    def harnesses(self):
        return [{
            "id": FakeGateway.catalog_entry["id"],
            "status": FakeGateway.catalog_entry.get("status", "ok"),
            "manifest": {"capabilities": dict(FakeGateway.manifest_capabilities)},
        }]

    def quota_snapshots(self):
        return []

    def find_project_id(self, root):
        self.project_lookups.append(root)
        return "" if FakeGateway.project_unregistered else "proj-1"

    def register_project(self, root):
        self.registrations.append(root)
        return "proj-new"

    def remove_project(self, project_id):
        self.removals.append(project_id)
        return {"removed": True}

    def start_run(self, request, *, idempotency_key=""):
        self.start_requests.append(dict(request))
        self.start_keys.append(str(idempotency_key))
        if FakeGateway.start_error is not None:
            exc = FakeGateway.start_error
            FakeGateway.start_error = None
            raise exc
        return {"runId": "run-1", "runDir": "/tmp/fake-run"}

    def get_run(self, run_id, **_kw):
        self.run_gets.append(run_id)
        if FakeGateway.poll_error is not None:
            exc = FakeGateway.poll_error
            FakeGateway.poll_error = None
            raise exc
        if FakeGateway.nonterminal:
            return {"summary": {"state": "running"}, "lastSeq": 1}
        return json.loads(json.dumps(FakeGateway.detail))

    def get_run_artifact(self, run_id, path):
        self.artifact_gets.append((run_id, path))
        if FakeGateway.artifact_error is not None:
            raise FakeGateway.artifact_error
        return FakeGateway.artifact_bytes or b""

    def cancel_run(self, run_id, *, reason=""):
        self.cancels.append((run_id, reason))
        return {"accepted": True}

    def close(self):
        pass


class FakeLLM:
    """Answers only the light-model extraction call."""

    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": self.reply}, {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.0001}


@pytest.fixture()
def fake_route(monkeypatch):
    FakeGateway.reset()
    monkeypatch.setattr("ouroboros.gateways.claudexor.ClaudexorGateway", FakeGateway)
    monkeypatch.setenv(REVIEW_SESSION_ROUTE_ENV, "fake-review=fake-small:low")
    monkeypatch.delenv(TRIAD_REVIEW_ROUTES_ENV, raising=False)
    monkeypatch.delenv(SCOPE_REVIEW_ROUTES_ENV, raising=False)
    return FakeGateway


def _agent_request(**overrides):
    base = dict(
        surface="scope_review",
        goal="Review the staged change.",
        task_id="t-agent",
        call_type="scope_review",
        session_root="/tmp/fake-repo",
        session_task="Review the staged diff of this repository: run `git diff --cached`.",
    )
    base.update(overrides)
    return ReviewRequest(**base)


def _agent_slot(**overrides):
    base = dict(slot_id="scope_slot_1", model="api/model-a", timeout_sec=30,
                route=ReviewRouteKind.AGENT_SESSION)
    base.update(overrides)
    return ReviewSlot(**base)


# ---------------------------------------------------------------------------
# The typed verdict: the strict parse, its selection, and canonicalization
# ---------------------------------------------------------------------------


def test_strict_requires_the_whole_answer_never_an_embedded_array():
    """A transcript that merely CONTAINS a JSON array is not a verdict.

    ``_strictly_parseable`` used to SCAN with ``extract_json_array``, so a
    refusal that quoted the contract's own example was passed through
    byte-identical as a TRUSTED ``strict`` verdict and the extraction rail never
    ran. Downstream that is not cosmetic: the quoted example carries ``item`` and
    ``verdict`` keys, so the actor projected ``parse_status='valid'`` /
    ``semantic_verdict='PASS'`` — a clean quorum vote from a session that
    reviewed nothing.
    """
    from ouroboros.review_execution import _strictly_parseable

    refusal = (
        'I reviewed NOTHING: the sandbox denied every read. The contract asked '
        'for entries like [{"item": "P1 honesty", "verdict": "PASS", '
        '"severity": "advisory", "reason": "example only"}]. Please re-run.'
    )
    assert not _strictly_parseable(refusal)

    # The bare payload — the shape the contract actually asks for — stays strict.
    assert _strictly_parseable('[{"item": "x", "verdict": "FAIL"}]')
    assert _strictly_parseable("[]")


def test_extraction_never_fabricates_a_clean_verdict():
    """A refusal canonicalizes to UNEXTRACTABLE, never to []: the light model's
    contract forbids blessing a non-review as clean, and an UNEXTRACTABLE reply
    leaves the raw narrative in place (upstream marks it a parse failure)."""
    llm = FakeLLM(reply="UNEXTRACTABLE")
    text, method, _usage = canonicalize_session_verdict(
        "I cannot review this diff.", conformance_passed=False, llm=llm)
    assert text == "I cannot review this diff."
    assert method == "unparsed"


def test_light_extraction_transport_is_narrowed_by_the_request_deadline(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("OUROBOROS_FINALIZATION_GRACE_SEC", "1")
    llm = FakeLLM(reply="[]")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    text, method, _usage = canonicalize_session_verdict(
        "narrative: clean",
        conformance_passed=False,
        llm=llm,
        deadline_at=deadline,
    )
    assert method == "light_model_extraction" and text == "[]"
    assert 0 < llm.calls[0]["timeout"] <= 4.1


def test_a_fenced_verdict_stays_unparsed_at_this_layer():
    """Disclosed residual (audit 2026-08-05): a verdict wrapped in a ```json
    fence that only the coordinator's downstream scanner parses is telemetered
    `unparsed` HERE — labeling it would need a duplicate parser (drift) or a
    backward import of the coordinator (the one-way seam ARCHITECTURE pins).
    The refusal-quoting-an-array case also stays `unparsed`."""
    llm = FakeLLM(reply="UNEXTRACTABLE")
    fenced = (
        "Review complete. My findings:\n```json\n"
        '[{"item": "x", "verdict": "FAIL", "severity": "critical", "reason": "r"}]\n'
        "```\nEnd of review."
    )
    text, method, _usage = canonicalize_session_verdict(
        fenced, conformance_passed=False, llm=llm)
    assert method == "unparsed"
    assert text == fenced
    assert len(llm.calls) == 1  # extraction was still consulted first (D19 order)

    quoted = ('I reviewed NOTHING. The contract asked for entries like '
              '[{"item": "a", "verdict": "PASS", "severity": "advisory", "reason": "e"}].')
    _text2, method2, _u2 = canonicalize_session_verdict(
        quoted, conformance_passed=False, llm=FakeLLM(reply="UNEXTRACTABLE"))
    assert method2 == "unparsed"


def test_extraction_runs_under_its_own_physical_rail():
    """The extraction is NOT a review call: it claims no send from the reviewing
    actor's two-physical-send rail (D19)."""
    from ouroboros.usage_accounting import physical_attempt_limit

    class RailProbeLLM(FakeLLM):
        def chat(self, **kwargs):
            from ouroboros.usage_accounting import _claim_physical_dispatch

            _claim_physical_dispatch()  # what a real provider send does
            return super().chat(**kwargs)

    llm = RailProbeLLM(reply="[]")
    with physical_attempt_limit(0):  # the actor rail is EXHAUSTED
        text, method, _usage = canonicalize_session_verdict(
            "narrative: clean.", conformance_passed=False, llm=llm)
    assert method == "light_model_extraction" and text == "[]"


def test_extraction_reads_the_whole_transcript_no_window():
    """CLAIM-2 regression: the extraction rail must see the artifact WHOLE. A
    head+tail window silently dropped everything mid-transcript, so a finding
    reported there never reached the light model — and a faithful "[]" over
    the visible cut fabricated a verified-clean verdict."""
    finding = ("CRITICAL FINDING: the retry path drops the durable ledger row "
               "(reported verbatim, mid-transcript).")
    raw = ("transcript begins\n" + "context line\n" * 500      # > old 4k head
           + finding + "\n"
           + "trailing tool output\n" * 6_000)                 # > old 60k tail
    canonical = ('[{"item": "retry path", "verdict": "FAIL", '
                 '"severity": "critical", "reason": "drops the ledger row"}]')
    llm = FakeLLM(reply=canonical)
    text, method, _usage = canonicalize_session_verdict(
        raw, conformance_passed=False, llm=llm)
    assert method == "light_model_extraction"
    assert text == canonical
    prompt = llm.calls[0]["messages"][0]["content"]
    assert finding in prompt  # the mid-transcript finding reached extraction


def test_oversized_transcript_is_typed_extraction_incomplete_never_clean():
    """The single-send rail has one hard bound; past it extraction REFUSES with
    the typed disposition — no send at all, so no windowed read a light model
    could faithfully canonicalize into a clean verdict."""
    from ouroboros.review_execution import _EXTRACT_MAX_CHARS

    big = "narrative that never parses\n" * (_EXTRACT_MAX_CHARS // 20)
    assert len(big) > _EXTRACT_MAX_CHARS
    llm = FakeLLM(reply="[]")  # would fabricate clean IF it were consulted
    text, method, _usage = canonicalize_session_verdict(
        big, conformance_passed=False, llm=llm)
    assert method == "extraction_incomplete"
    assert text == big                       # forensics keep the raw transcript
    assert not empty_array_is_verified_clean(text)
    assert llm.calls == []                   # the light model was never shown a cut


def test_spent_owner_deadline_skips_light_model_extraction():
    from ouroboros.review_execution import _extract_verdict_via_light_model

    class NeverCalled:
        def chat(self, **_kwargs):
            raise AssertionError("spent deadline must not dispatch extraction")

    canonical, usage = _extract_verdict_via_light_model(
        "narrative", llm=NeverCalled(), deadline_at="2000-01-01T00:00:00Z",
    )

    assert canonical is None
    assert usage["reason_code"] == "deadline_exhausted"
    assert usage["dispatch"] == "not_dispatched"


def test_reserve_only_owner_window_skips_light_model_extraction(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ouroboros.review_execution import _extract_verdict_via_light_model

    monkeypatch.setenv("OUROBOROS_FINALIZATION_GRACE_SEC", "120")

    class NeverCalled:
        def chat(self, **_kwargs):
            raise AssertionError("reserve-only owner window must not dispatch extraction")

    deadline = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    canonical, usage = _extract_verdict_via_light_model(
        "narrative", llm=NeverCalled(), deadline_at=deadline,
    )

    assert canonical is None
    assert usage["reason_code"] == "deadline_exhausted"
    assert usage["dispatch"] == "not_dispatched"


def test_multi_model_wrapper_preserves_typed_refusal_into_skill_actor_record():
    from ouroboros.tools.review import _parse_model_response
    from ouroboros.triad_review import parse_model_review_results

    envelope = _parse_model_response("claude-fable-5", {
        "error": "Error: route unavailable",
        "slot_id": "skill-slot-1",
        "failure_code": "agent_session_route_unavailable",
        "reset_at": "2030-01-01T00:00:00Z",
        "http_status": 429,
        "transport_status": "unavailable",
    }, None)
    parsed = parse_model_review_results(
        {"results": [envelope]}, required_items=("manifest_schema",),
    )

    assert envelope["failure_code"] == "agent_session_route_unavailable"
    actor = parsed.actor_records[0].to_dict()
    assert actor["slot_id"] == "skill-slot-1"
    assert actor["failure_code"] == "agent_session_route_unavailable"
    assert actor["reset_at"] == "2030-01-01T00:00:00Z"
    assert actor["http_status"] == 429
    assert actor["transport_status"] == "unavailable"


def test_an_expired_cooldown_is_history_not_exhaustion():
    """The `_exhausted_window` reader (the admission seam above) treated ANY non-empty
    `cooldown_until` as spent. A cooldown whose instant already PASSED is a stale fact
    the harness has not refreshed, not positive evidence of a spent window; a FUTURE
    one still blocks, and an illegible instant keeps the conservative old reading."""
    from ouroboros.subagents import _exhausted_window

    def _quota(cooldown):
        class _Q:
            def quota_snapshots(self):
                return [{"subject": {"harness": "some-route", "subject_id": "a"},
                         "freshness": "fresh",
                         "constraints": [{"used_ratio": 0.4,
                                          "cooldown_until": cooldown}]}]

            def quota_absences(self):
                return []
        return _Q()

    assert _exhausted_window(_quota("2020-01-01T00:00:00Z"), "some-route") == (False, "")
    assert _exhausted_window(_quota("2099-01-01T00:00:00Z"), "some-route") == (
        True, "2099-01-01T00:00:00Z")
    assert _exhausted_window(_quota("soon-ish"), "some-route") == (True, "soon-ish")


# ---------------------------------------------------------------------------
# 5.1/5.3 — routes on slots and typed refusals
# ---------------------------------------------------------------------------


def test_delegated_session_route_is_explicitly_unavailable(tmp_path):
    """Seed 0: the owned gateway carried this transport (registration, POST,
    poll, verified cancel, settlement) and retired with it. The runner refuses
    typed on the caller's own slot — never an empty session result a caller
    could read as an honest zero verdict, and never an api fallback."""
    from ouroboros.errors import OuroborosUnavailableError
    from ouroboros.review_execution import (
        SessionInvocation, run_delegated_review_session,
    )

    with pytest.raises(OuroborosUnavailableError):
        run_delegated_review_session(
            prompt="review this", root="/tmp/fake-repo", custody_drive=tmp_path,
            invocation=SessionInvocation(task_id="t-b", surface="scope_review",
                                         slot_id="scope_slot_1", timeout_sec=30),
        )


def test_unconfigured_session_route_is_a_typed_refusal(tmp_path, fake_route, monkeypatch):
    monkeypatch.delenv(REVIEW_SESSION_ROUTE_ENV, raising=False)
    monkeypatch.delenv("OUROBOROS_SUBAGENT_HARNESS", raising=False)
    llm = FakeLLM()
    stamps = []
    usage_ctx = SimpleNamespace(_review_paid_stamp=lambda: stamps.append("paid"))
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm, usage_ctx=usage_ctx)
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "no configured session route" in actor["error"]
    assert llm.calls == []  # never a silent fallback onto the api route
    assert stamps == []  # a typed pre-start refusal is a $0 unpaid wave


def test_missing_api_transport_refuses_before_paid_stamp(tmp_path, fake_route):
    stamps = []
    ctx = SimpleNamespace(_review_paid_stamp=lambda: stamps.append("paid"))
    result = run_review_request(
        _agent_request(), slots=[_agent_slot(route=ReviewRouteKind.API_CHAT)],
        drive_root=tmp_path, llm=SimpleNamespace(), usage_ctx=ctx,
    )
    assert result.actors[0]["status"] == "error"
    assert "api_chat client exposes no callable transport" in result.actors[0]["error"]
    assert stamps == []


def test_callable_api_refusal_before_physical_attempt_stays_unpaid(tmp_path, fake_route):
    stamps = []

    class RefusingLLM:
        def chat(self, **_kwargs):
            raise RuntimeError("route resolution failed before provider send")

    ctx = SimpleNamespace(_review_paid_stamp=lambda: stamps.append("paid"))
    result = run_review_request(
        _agent_request(), slots=[_agent_slot(route=ReviewRouteKind.API_CHAT)],
        drive_root=tmp_path, llm=RefusingLLM(), usage_ctx=ctx,
    )
    assert result.actors[0]["status"] == "error"
    assert "route resolution failed" in result.actors[0]["error"]
    assert stamps == []


def test_api_review_rechecks_owner_deadline_at_physical_boundary(tmp_path, fake_route, monkeypatch):
    from ouroboros.review_execution import (
        ApiChatReviewExecutor, ReviewAssignment, ReviewRouteUnavailable,
    )

    llm = FakeLLM()
    executor = ApiChatReviewExecutor(
        ReviewAssignment(
            request=_agent_request(deadline_at="2000-01-01T00:00:00Z"),
            slot=_agent_slot(route=ReviewRouteKind.API_CHAT),
            custody_root=tmp_path,
        ),
        llm=llm,
    )
    # Stand in for a long prompt/render/persistence phase. The final check is
    # deliberately after `_kwargs()` and immediately before `chat`.
    monkeypatch.setattr(executor, "_kwargs", lambda: {"messages": [], "model": "fake"})

    with pytest.raises(ReviewRouteUnavailable) as caught:
        executor.execute()
    assert caught.value.code == "deadline_exhausted"
    assert llm.calls == []


def test_api_review_does_not_stamp_pre_dispatch_released_capture(
    tmp_path, fake_route,
):
    from ouroboros.usage_accounting import PhysicalAttemptCapture
    from ouroboros.review_execution import (
        ApiChatReviewExecutor, ReviewAssignment,
    )

    stamps = []

    class ReleasedBeforeSend:
        def chat(self, **_kwargs):
            error = RuntimeError("request preparation failed")
            error.physical_attempt_capture = PhysicalAttemptCapture(
                attempt_id="attempt-released", model="fake", provider="test",
                state="released", candidate_measurement_kind="opaque",
            )
            raise error

    executor = ApiChatReviewExecutor(
        ReviewAssignment(
            request=_agent_request(),
            slot=_agent_slot(route=ReviewRouteKind.API_CHAT),
            custody_root=tmp_path,
            dispatch_stamp=lambda: stamps.append("paid"),
        ),
        llm=ReleasedBeforeSend(),
    )

    with pytest.raises(RuntimeError, match="request preparation failed"):
        executor.execute()
    assert stamps == []


def test_api_review_does_not_dispatch_inside_finalization_reserve(
    tmp_path, fake_route, monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from ouroboros.review_execution import (
        ApiChatReviewExecutor, ReviewAssignment, ReviewRouteUnavailable,
    )

    monkeypatch.setenv("OUROBOROS_FINALIZATION_GRACE_SEC", "120")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    llm = FakeLLM()
    executor = ApiChatReviewExecutor(
        ReviewAssignment(
            request=_agent_request(deadline_at=deadline),
            slot=_agent_slot(route=ReviewRouteKind.API_CHAT),
            custody_root=tmp_path,
        ),
        llm=llm,
    )
    monkeypatch.setattr(executor, "_kwargs", lambda: {"messages": [], "model": "fake"})

    with pytest.raises(ReviewRouteUnavailable) as caught:
        executor.execute()
    assert caught.value.code == "deadline_exhausted"
    assert llm.calls == []


def test_async_only_api_transport_fires_at_the_same_physical_boundary(tmp_path, fake_route):
    from ouroboros import usage_accounting as ua

    stamps = []

    class AsyncLLM:
        def __init__(self):
            self.calls = []

        async def chat_async(self, **kwargs):
            self.calls.append(kwargs)
            request = ua.AttemptRequest(
                model="local-review-test", provider="local", reservation_usd=0.0,
                drive_root=tmp_path, task_id="review", root_task_id="review",
            )

            async def send():
                return {"content": "[]"}, {"prompt_tokens": 0, "completion_tokens": 0}

            return await ua.execute_physical_attempt_async(request, send)

    llm = AsyncLLM()
    ctx = SimpleNamespace(_review_paid_stamp=lambda: stamps.append("paid"))
    result = run_review_request(
        _agent_request(), slots=[_agent_slot(route=ReviewRouteKind.API_CHAT)],
        drive_root=tmp_path, llm=llm, usage_ctx=ctx,
    )
    assert result.actors[0]["status"] == "ok"
    assert stamps == ["paid"] and len(llm.calls) == 1


def test_mixed_panel_failed_agent_slot_does_not_shrink_n(tmp_path, fake_route, monkeypatch):
    """5.3: one panel, two deliveries. The agent slot failing typed leaves the
    panel at its configured size — the failed slot is an error actor on its own
    row, never a smaller N."""
    monkeypatch.delenv(REVIEW_SESSION_ROUTE_ENV, raising=False)
    monkeypatch.delenv("OUROBOROS_SUBAGENT_HARNESS", raising=False)

    class ApiFindingsLLM(FakeLLM):
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return ({"content": '{"verdict": "PASS", "findings": [], "summary": "ok"}'},
                    {"prompt_tokens": 3, "completion_tokens": 2})

    llm = ApiFindingsLLM()
    slots = [
        ReviewSlot(slot_id="scope_slot_1", model="api/model-a", timeout_sec=10),
        _agent_slot(slot_id="scope_slot_2"),
    ]
    result = run_review_request(_agent_request(), slots=slots,
                                drive_root=tmp_path, llm=llm)
    assert len(result.actors) == 2
    by_id = {a["slot_id"]: a for a in result.actors}
    assert by_id["scope_slot_1"]["status"] == "ok"
    assert by_id["scope_slot_2"]["status"] == "error"
    assert len(llm.calls) == 1  # the api row; the agent row never touched chat


def test_configured_review_routes_parsing(monkeypatch):
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "api_chat, agent_session")
    routes = configured_review_routes(TRIAD_REVIEW_ROUTES_ENV, 3)
    assert routes == [ReviewRouteKind.API_CHAT, ReviewRouteKind.AGENT_SESSION,
                      ReviewRouteKind.API_CHAT]
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "codex")
    with pytest.raises(ValueError):
        configured_review_routes(TRIAD_REVIEW_ROUTES_ENV, 1)


def test_scope_rows_carry_their_configured_routes(monkeypatch):
    monkeypatch.setenv(SCOPE_REVIEW_ROUTES_ENV, "agent_session")
    rows = scope_reviewer_slots(["m1", "m2"])
    assert rows[0].route is ReviewRouteKind.AGENT_SESSION
    assert rows[1].route is ReviewRouteKind.API_CHAT
    assert rows[0].slot_id == "scope_slot_1" and rows[1].slot_id == "scope_slot_2"


def test_scope_rows_default_to_the_configured_scope_review_effort(monkeypatch):
    """Regression (v6.89.0): with no structured reviewer slots, the legacy path took
    this function's old literal default ("medium") instead of the owner's configured
    OUROBOROS_EFFORT_SCOPE_REVIEW — the BLOCKING constitutional scope reviewer
    silently ran below its configured reasoning strength on every stock install."""
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)
    monkeypatch.delenv(SCOPE_REVIEW_ROUTES_ENV, raising=False)
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_MODELS", "some/model")
    monkeypatch.delenv("OUROBOROS_EFFORT_SCOPE_REVIEW", raising=False)
    assert [row.effort for row in scope_reviewer_slots()] == ["high"]  # config default
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "xhigh")
    assert [row.effort for row in scope_reviewer_slots()] == ["xhigh"]
    # An explicit effort still wins for callers that rebuild one positional row.
    assert [row.effort for row in scope_reviewer_slots(["m"], effort="low")] == ["low"]


def test_mixed_scope_fanout_sends_each_row_over_its_own_route(tmp_path, monkeypatch):
    """A MIXED scope configuration must deliver each row over the route it was
    configured with.

    `_call_scope_llm` rebuilt its slot from `scope_reviewer_slots([model])`, and a
    one-element list always re-reads ROUTES **row 1** — so with
    `agent_session,api_chat` the configured api row inherited agent_session while
    its request carried the api pack and no session task: a deterministic
    ReviewRouteUnavailable error actor that failed the blocking scope gate.
    """
    import ouroboros.tools.scope_review as scope_mod

    monkeypatch.setenv(SCOPE_REVIEW_ROUTES_ENV, "agent_session,api_chat")
    dispatched: list = []

    def _capture(request, *, slots, drive_root, llm, usage_ctx=None):
        slot = slots[0]
        dispatched.append((slot.slot_id, slot.model, slot.route.value,
                           bool(request.session_task), bool(request.messages)))
        return SimpleNamespace(actors=[{
            "slot_id": slot.slot_id, "model": slot.model, "status": "ok",
            "raw_text": json.dumps(_scope_matrix_rows()),
            "usage": {}, "prompt_ref": {}, "response_ref": {},
        }])

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", _capture)
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=1_000_000, status="confirmed"))

    for slot in scope_reviewer_slots(["m/session", "m/api"]):
        scope_mod.run_scope_review(
            _scope_ctx(tmp_path), "mixed route fan-out",
            scope_model=slot.model, slot_id=slot.slot_id, route=slot.route,
        )

    # Row 1 is the session (task, no api pack); row 2 is api (pack, no task).
    assert dispatched == [
        ("scope_slot_1", "m/session", "agent_session", True, False),
        ("scope_slot_2", "m/api", "api_chat", False, True),
    ], dispatched


def test_acceptance_rows_stay_api_even_when_triad_routes_delegate(monkeypatch):
    """D15: task acceptance is pinned to the API (plan review follows each configured
    row's delivery since the spec-gate redesign). The triad's route list must not
    leak into surfaces that pass no route_env_key."""
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "agent_session,agent_session,agent_session")
    rows = reviewer_slots(["m1", "m2"], effort="high", role_hint="task acceptance")
    assert all(row.route is ReviewRouteKind.API_CHAT for row in rows)


def test_agent_slot_without_session_task_refuses_the_api_pack(tmp_path, fake_route):
    """5.2: the giant assembled pack is not sendable to a session. A surface
    that supplied no route-owned session task gets a typed refusal, not a
    silently forwarded api pack."""
    request = _agent_request(session_task="",
                             messages=[{"role": "system", "content": "GIANT PACK"}])
    llm = FakeLLM()
    result = run_review_request(request, slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["status"] == "not_dispatched"
    assert "no session task" in actor["error"]
    assert llm.calls == []
    assert not any(inst.start_requests for inst in fake_route.instances)


# ---------------------------------------------------------------------------
# Surface wiring: scope and triad rows, their routes and their composition
# ---------------------------------------------------------------------------


def _scope_matrix_rows():
    from ouroboros.tools.scope_review_contract import SCOPE_REQUIRED_ITEMS

    return [
        {"item": item, "verdict": "PASS", "severity": "advisory",
         "reason": "checked the relevant code path and its consumers thoroughly"}
        for item in sorted(SCOPE_REQUIRED_ITEMS)
    ]


def _scope_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    gov = tmp_path / "gov"
    drive = tmp_path / "data"
    gov.mkdir(exist_ok=True)
    drive.mkdir(exist_ok=True)
    return ToolContext(repo_dir=gov, drive_root=drive)


def _scope_matrix_with_critical():
    rows = _scope_matrix_rows()
    rows[0] = {**rows[0], "verdict": "FAIL", "severity": "critical",
               "reason": "the change contradicts a documented invariant on a live path"}
    return rows


def test_api_scope_row_keeps_the_1m_floor_and_still_blocks_sub_floor(
    tmp_path, fake_route, monkeypatch
):
    """The api (push) delivery is untouched: its authority rests on the assembled
    pack fitting, so a sub-1M reviewer is still the loud `sub_floor` block."""
    import ouroboros.tools.scope_review as scope_mod

    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=200_000, status="confirmed"))
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_with_critical()), {}, ""),
    )
    result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "api row, sub-floor window",
        scope_model="api/small-window", slot_id="scope_slot_1",
    )
    assert result.status == "sub_floor", result.status
    assert result.blocked is True
    assert "does not establish the required >=1M floor" in result.block_message


def test_scope_quorum_refuses_a_session_advisory_row_as_authoritative(tmp_path, monkeypatch):
    """The scope quorum must not count a non-host-attested session row as the
    authoritative verdict, and must disclose the shortfall it leaves."""
    from ouroboros.tools import parallel_review, review
    from ouroboros.tools.scope_review import ScopeReviewResult

    rows = {
        "api/big": ScopeReviewResult(blocked=False, status="responded", model_id="api/big"),
        "session/row": ScopeReviewResult(
            blocked=False, status="session_advisory", model_id="session/row",
            advisory_findings=[{
                "verdict": "FAIL", "severity": "advisory",
                "item": "scope_review_session_window_unproven",
                "reason": "SCOPE_SESSION_ADVISORY_ONLY: window not sourced-proven",
            }],
        ),
    }
    monkeypatch.setattr(parallel_review, "run_scope_review",
                        lambda _ctx, _msg, **kwargs: rows[kwargs["scope_model"]])
    monkeypatch.setattr(parallel_review, "scope_reviewer_slots", lambda *_a, **_k: [
        SimpleNamespace(model="api/big", slot_id="scope_slot_1", route=None,
                        effort="", session_target="", session_profile=""),
        SimpleNamespace(model="session/row", slot_id="scope_slot_2", route=None,
                        effort="", session_target="", session_profile=""),
    ])
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(review, "_prepare_unified_review", lambda *_a, **_k: (None, None, True))
    from ouroboros.tools import review_admission
    monkeypatch.setattr(review_admission, "prepare_scope_review",
                        lambda *_a, **_k: ({"packet": 1}, None))

    ctx = SimpleNamespace(
        repo_dir=tmp_path, drive_root=tmp_path, task_id="scope-quorum",
        pending_events=[], _review_history=[], _review_advisory=[], _scope_review_history={},
    )
    parallel_review.run_parallel_review(ctx, "quorum commit")

    manifest = (ctx._last_scope_raw_result or {}).get("context_manifest") or {}
    # Two configured rows, adaptive quorum 2 — but only ONE authoritative verdict.
    assert manifest["scope_responded_count"] == 1, manifest
    assert manifest["scope_session_advisory_only_count"] == 1, manifest
    assert any("scope_session_advisory_only" in str(r)
               for r in manifest["scope_degraded_reasons"]), manifest


def test_triad_session_task_carries_criteria_and_nav_maps_not_evidence():
    import ouroboros.tools.review as review_mod

    task = review_mod._triad_session_task(
        None,
        goal_section="## Goal\nDo the thing.",
        scope_section="## Scope\nOnly here.",
        checklist_section="## Review Checklist\n- correctness",
        rebuttal_section="",
        review_history_section="",
        dev_guide_text="# Dev\n\n## Rules\n\ntext\n",
        architecture_text="## Parent\nbody\n### Child\nbody\n#### Detail\nbody\n",
    )
    assert "## Review Checklist" in task
    assert "## Goal" in task and "## Scope" in task
    assert "git diff --cached" in task           # subject pointer, not the diff
    assert "DEVELOPMENT.md (navigation map)" in task
    assert "ARCHITECTURE.md (navigation map)" in task
    assert "- Parent — lines 1-6" in task
    assert "  - Child — lines 3-6" in task
    assert "    - Detail — lines 5-6" in task
    assert "Read BIBLE.md and docs/DESIGN.md in full" in task


# ---------------------------------------------------------------------------
# The blocking scope gate must not fail OPEN on an all-retrieving panel.
# ---------------------------------------------------------------------------


def _all_session_scope_panel(tmp_path, monkeypatch, *, window, provenance):
    """The REAL fan-out + aggregate over a panel of two retrieving rows.

    Only the two genuinely external things are faked: the reviewer's window
    evidence and the model call. Everything the gate actually decides with —
    `run_scope_review`, `_apply_scope_authority`, `session_scope_authority`,
    `run_parallel_review`'s quorum, `aggregate_review_verdict` — runs for real.
    """
    from ouroboros import config as cfg
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools import parallel_review, review
    import ouroboros.tools.scope_review as scope_mod

    if provenance:
        resolved = scope_mod.ReviewerWindow(window_tokens=int(window), status=provenance)
    else:
        resolved = scope_mod.ReviewerWindow(window_tokens=0, status="")
    monkeypatch.setattr(cfg, "get_review_enforcement", lambda: "blocking")
    monkeypatch.setattr(scope_mod, "_scope_window", lambda *_a, **_k: resolved)
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_rows()), {}, ""),
    )
    monkeypatch.setattr(parallel_review, "scope_reviewer_slots", lambda *_a, **_k: [
        ReviewSlot(slot_id="scope_slot_1", model="codex=gpt-5.6-sol",
                   route=ReviewRouteKind.AGENT_SESSION, session_target="codex=gpt-5.6-sol"),
        ReviewSlot(slot_id="scope_slot_2", model="claude=fable-5",
                   route=ReviewRouteKind.AGENT_SESSION, session_target="claude=fable-5"),
    ])
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(review, "_prepare_unified_review", lambda *_a, **_k: (None, None, True))

    ctx = _scope_ctx(tmp_path)
    ctx._review_history = []
    ctx._review_advisory = []
    ctx._scope_review_history = {}
    ctx.task_id = "scope-fail-open"
    ctx.pending_events = []
    args = parallel_review.run_parallel_review(ctx, "all-retrieving scope panel")
    blocked, message, reason, _findings, _advisory = parallel_review.aggregate_review_verdict(
        *args, ctx, "all-retrieving scope panel", 0.0, tmp_path,
    )
    manifest = (ctx._last_scope_raw_result or {}).get("context_manifest") or {}
    return blocked, message or "", reason, manifest


def test_all_retrieving_scope_panel_blocks_instead_of_failing_open(tmp_path, monkeypatch):
    """A scope panel of retrieving rows with no sourced window evidence yields ZERO
    authoritative verdicts — and must BLOCK, exactly as the api panel does.

    This is the fail-open the adversarial panel measured on a6a3c1f: the same panel
    shape gave `api_chat status=sub_floor -> BLOCKED=True` and
    `agent_session status=session_advisory -> BLOCKED=False`. Nothing downstream
    could recover it — `partial_quorum_shortfall` only fires above zero responders,
    so a zero-authoritative run walked straight through the blocking scope gate of
    BIBLE P3 while looking armed.
    """
    blocked, message, reason, manifest = _all_session_scope_panel(
        tmp_path, monkeypatch, window=0, provenance="",
    )

    assert blocked is True, "the blocking scope gate must not pass a zero-authoritative panel"
    assert reason == "scope_blocked", reason
    assert "SCOPE_REVIEW_BLOCKED" in message
    assert "authoritative scope verdict required to commit" in message
    # The shortfall is still disclosed, not merely converted into a block.
    assert manifest["scope_responded_count"] == 0, manifest
    assert manifest["scope_session_advisory_only_count"] == 2, manifest
    assert any("scope_session_advisory_only" in str(r)
               for r in manifest["scope_degraded_reasons"]), manifest


def test_retrieving_and_api_panels_agree_on_an_unestablished_window(tmp_path, monkeypatch):
    """The asymmetry itself is the defect: an unestablished window blocks on BOTH
    deliveries, and SOURCED evidence at the row's own floor authorises on both."""
    import ouroboros.tools.scope_review as scope_mod

    # Retrieving row, SOURCED at the session floor -> authoritative, no block.
    blocked, _msg, _reason, manifest = _all_session_scope_panel(
        tmp_path, monkeypatch, window=200_000, provenance="confirmed",
    )
    assert blocked is False, "sourced >=200K evidence must restore an authoritative verdict"
    assert manifest["scope_responded_count"] == 2, manifest

    # api row, window below its own floor -> blocks (the twin, unchanged).
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=200_000, status="confirmed"))
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_rows()), {}, ""),
    )
    api_result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "api row, sub-floor window", scope_model="api/small",
        slot_id="scope_slot_1",
    )
    assert api_result.blocked is True and api_result.status == "sub_floor"


def test_a_retrieving_row_can_actually_reach_sourced_evidence(tmp_path, monkeypatch):
    """The >=200K floor must be REACHABLE, not decorative.

    Retrieving rows were excluded from Capability-Evidence probing and their opaque
    `harness[=model]` target does not resolve through `provider_for_model`, so no
    product path could ever take such a row to `confirmed`/`asserted`: advisory-only
    was the mode's ONLY possible outcome. The settings save now offers the row its
    ack against its own floor, and acking that exact route restores authority.
    """
    from ouroboros import capability_evidence as ce
    from ouroboros.gateway import settings as smod
    from ouroboros.reviewer_window import SESSION_ROUTE_PROVIDER
    from ouroboros.tools.scope_review_session import (
        SESSION_WINDOW_FLOOR,
        session_window_is_authoritative,
    )
    from ouroboros.tools.scope_window import scope_window

    monkeypatch.setattr(ce, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(smod, "_candidate_scope_models", lambda _s: [])
    slots = json.dumps({
        "triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "api/m"}}],
        "scope": [{"slot_id": "s1", "route": {"kind": "agent_session",
                                              "target_id": "codex=gpt-5.6-sol"}}],
    })

    notices = smod._review_capability_notices({"OUROBOROS_REVIEWER_SLOTS": slots})
    assert len(notices) == 1, notices
    notice = notices[0]
    assert notice["surface"] == "scope_review_session"
    assert notice["floor_tokens"] == SESSION_WINDOW_FLOOR
    ack_route = notice["needs_ack"]
    assert ack_route["provider"] == SESSION_ROUTE_PROVIDER, ack_route
    assert ack_route["model"] == "codex=gpt-5.6-sol"

    # Before the ack the row cannot authorise...
    before = scope_window("codex=gpt-5.6-sol", session=True)
    assert session_window_is_authoritative(before.window_tokens, before.status) is False

    # ...and the ack the UI records against that exact route is what restores it.
    ce.record_owner_ack(tmp_path, provider=ack_route["provider"], model=ack_route["model"],
                        base_url=ack_route["base_url"], window_tokens=SESSION_WINDOW_FLOOR)
    after = scope_window("codex=gpt-5.6-sol", session=True)
    assert session_window_is_authoritative(after.window_tokens, after.status) is True


def test_session_schema_floor_matches_each_surfaces_clean_contract():
    """`{"findings": []}` is the honest clean verdict for a TRIAD session, but on
    scope and Skill Review (mandatory matrix rows) it is schema-conformant but
    downstream-invalid. The floor lets a conforming engine regenerate instead."""
    from ouroboros.review_execution import (
        REVIEW_SESSION_OUTPUT_SCHEMA,
        review_session_output_schema,
    )

    assert review_session_output_schema("commit_review") is REVIEW_SESSION_OUTPUT_SCHEMA
    # Advisory keeps the clean-capable shared schema: its ORDINARY mode's required
    # clean verdict is exactly the empty array, so a floor would starve it of the
    # one answer its contract demands (checklist coverage is checked downstream).
    assert review_session_output_schema("advisory_review") is REVIEW_SESSION_OUTPUT_SCHEMA
    assert "minItems" not in REVIEW_SESSION_OUTPUT_SCHEMA["properties"]["findings"]
    shaped = review_session_output_schema("scope_review")
    assert shaped["properties"]["findings"]["minItems"] == 1
    assert review_session_output_schema("skill_review")["properties"]["findings"]["minItems"] == 1
    # A shaped copy, never a mutation of the shared schema.
    assert "minItems" not in REVIEW_SESSION_OUTPUT_SCHEMA["properties"]["findings"]


# ---------------------------------------------------------------------------
# The cancel-honesty wording — pure vocabulary, no poller behind it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state,must,must_not", [
    ("failed", "its own terminal state 'failed'", "host-cancelled"),
    ("interrupted", "its own terminal state 'interrupted'", "host-cancelled"),
    ("cancelled", "host-cancelled with a verified terminal receipt", "its own terminal"),
    ("settled", "host-cancelled with a verified terminal receipt", "its own terminal"),
    ("absent", "host-cancelled with a verified terminal receipt", "its own terminal"),
    ("", "host-cancelled with a verified terminal receipt", "its own terminal"),
])
def test_confirmed_attribution_follows_the_verified_state(state, must, must_not):
    """BR2-2, every wording branch: a `confirmed` whose verified state is the
    run's OWN non-success terminal (failed/interrupted) is attributed to the
    run; 'cancelled' — and the receipt-is-the-cancel states ''/settled/absent —
    keep the host-cancelled verified-receipt wording; nothing here ever says
    "may still be live"."""
    from ouroboros.review_execution import _cancel_honesty_clause

    text = _cancel_honesty_clause("confirmed", state)
    assert must in text, text
    assert must_not not in text, text
    assert "may still be live" not in text
    # The unverified wording is unchanged by the state parameter.
    assert "may still be live" in _cancel_honesty_clause("requested", state)


