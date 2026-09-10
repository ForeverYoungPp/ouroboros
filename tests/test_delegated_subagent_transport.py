"""Phase 3: Claudexor transport, the nanny verbs, and their accounting/failure classes."""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from ouroboros import delegate_custody as dcust, subagents, usage_accounting as ua
from ouroboros.config import (
    CLAUDEXOR_MIN_VERSION,
    CLAUDEXOR_PROTOCOL_MAJOR,
)
from ouroboros.gateways import claudexor as cx
from ouroboros.loop_llm_call import SUBSCRIPTION_WINDOW_EXHAUSTED, classify_llm_exception
from ouroboros.provider_models import MODEL_SETTING_KEYS
from ouroboros.tool_capabilities import (
    ACTING_SUBAGENT_TOOL_NAMES,
    LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
)

NANNY_TOOLS = {"delegate_start", "delegate_wait", "delegate_cancel", "delegate_answer"}


# -- 3.1 the narrow setting key ------------------------------------------------


def test_subagent_harness_key_stays_out_of_the_model_key_sweep():
    # A session-only route is not an API model identity: leaking it into
    # MODEL_SETTING_KEYS would poison credential planning, pricing and provenance.
    assert "OUROBOROS_SUBAGENT_HARNESS" not in MODEL_SETTING_KEYS


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("codex", subagents.DelegationRoute("codex", "", "")),
    ("codex=gpt-5.4-mini", subagents.DelegationRoute("codex", "gpt-5.4-mini", "")),
    ("codex=gpt-5.4-mini:low", subagents.DelegationRoute("codex", "gpt-5.4-mini", "low")),
    # The documented grammar is harness[=model][:effort] — the effort bracket is
    # not tied to the model one. Splitting on `=` first made the whole string the
    # route id, which then failed at dispatch as an unknown route.
    ("claude:high", subagents.DelegationRoute("claude", "", "high")),
    # A typo with an empty head is "no route", not a route named "=opus".
    ("=opus", None),
    ("=model:high", None),
])
def test_route_parsing_is_opaque(raw, expected):
    assert subagents.parse_subagent_harness(raw) == expected


def test_an_unparseable_configured_route_is_disclosed_not_silent(monkeypatch, caplog):
    """A non-empty OUROBOROS_SUBAGENT_HARNESS that parses to nothing ("=opus") used
    to be silently identical to "never configured" — ALL delegation moved onto
    metered API children with no trace anywhere the operator looks."""
    import logging

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "=opus")
    with caplog.at_level(logging.WARNING, logger="ouroboros.subagents"):
        assert subagents.get_subagent_harness() is None
    assert any("unparseable" in r.message for r in caplog.records)

    # The two legitimate "no route" spellings stay silent.
    for quiet in ("", "off"):
        caplog.clear()
        monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", quiet)
        with caplog.at_level(logging.WARNING, logger="ouroboros.subagents"):
            assert subagents.get_subagent_harness() is None
        assert not caplog.records


def test_an_explicit_off_is_a_decision_an_empty_value_is_not(monkeypatch):
    """Both spellings mean "no delegated route"; they differ in owner intent.

    Settings' Subagents section turns delegation on by itself once a subscription
    is connected, and it may only do that over a value nobody decided. Without a
    distinguishable "off" the owner's own Off saved as empty and came back On on
    the next load — an un-saveable choice. Runtime behaviour is identical.
    """
    assert subagents.parse_subagent_harness("off") is None
    assert subagents.parse_subagent_harness("OFF") is None
    assert subagents.parse_subagent_harness("  off  ") is None
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "off")
    assert subagents.get_subagent_harness() is None
    assert subagents.resolve_subagent_executor("auto", route=None).executor == "native"


def test_get_subagent_harness_reads_the_env_key(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=some-model:high")
    route = subagents.get_subagent_harness()
    assert route is not None and route.route_id == "some-route"
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "")
    assert subagents.get_subagent_harness() is None


# -- 3.5 the execution rule table ----------------------------------------------


ROUTE = subagents.DelegationRoute("some-route", "m", "low")


def test_rule_auto_without_harness_runs_native():
    res = subagents.resolve_subagent_executor("auto", route=None)
    assert (res.executor, res.reason) == ("native", "harness_not_configured")


def test_rule_auto_with_healthy_harness_delegates():
    res = subagents.resolve_subagent_executor("auto", route=ROUTE)
    assert (res.executor, res.reason) == ("harness", "harness_ready")


def test_rule_auto_with_every_profile_spent_falls_back_to_the_api_loudly():
    """Owner decision D28. It used to dispatch the child as a NANNY anyway, whose very
    first `delegate_start` was then refused with this SAME fact (executed and pinned
    below) — a spent dispatch, and the child left to improvise a fallback in prose.
    `auto` now falls back to the metered API at the one point that still costs nothing,
    typed, with the reset instant riding along so waiting stays a visible option."""
    res = subagents.resolve_subagent_executor("auto", route=ROUTE, reset_at="2030-01-01T00:00:00Z")
    assert res.executor == "native", "auto must not be dispatched onto a spent substrate"
    assert res.reason == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert res.reset_at == "2030-01-01T00:00:00Z"
    assert not res.blocked, "never a permanent block while metered keys exist"


def test_rule_auto_with_unavailable_harness_falls_native_with_a_visible_marker():
    res = subagents.resolve_subagent_executor("auto", route=ROUTE, unavailable_reason="daemon_unreachable")
    assert (res.executor, res.reason) == ("native", "daemon_unreachable")


@pytest.mark.parametrize("kwargs,reason", [
    ({"route": None}, "harness_not_configured"),
    ({"route": ROUTE, "unavailable_reason": "daemon_unreachable"}, "daemon_unreachable"),
    ({"route": ROUTE, "reset_at": "2030-01-01T00:00:00Z"}, SUBSCRIPTION_WINDOW_EXHAUSTED),
])
def test_rule_explicit_harness_blocks_instead_of_spending_api_money(kwargs, reason):
    res = subagents.resolve_subagent_executor("harness", **kwargs)
    assert res.blocked and res.reason == reason


def test_rule_native_is_native_whatever_the_state():
    res = subagents.resolve_subagent_executor("native", route=ROUTE, unavailable_reason="x")
    assert (res.executor, res.reason) == ("native", "requested_native")


def test_unknown_executor_is_rejected():
    with pytest.raises(ValueError):
        subagents.resolve_subagent_executor("magic")


# -- 3.2 transport -------------------------------------------------------------


def _gateway(handler) -> cx.ClaudexorGateway:
    gateway = cx.ClaudexorGateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
    gateway._client = httpx.Client(
        base_url="http://127.0.0.1:1",
        transport=httpx.MockTransport(handler),
        headers=dict(gateway._client.headers),
    )
    return gateway


def test_discovery_missing_descriptor_is_a_typed_refusal(tmp_path):
    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_not_discovered"


def test_discovery_reads_host_port_and_token(tmp_path):
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "token").write_text("tok\n", encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 4242, "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")
    endpoint = cx.discover_daemon(tmp_path)
    assert (endpoint.host, endpoint.port, endpoint.token) == ("127.0.0.1", 4242, "tok")


@pytest.mark.parametrize("host,loopback", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("127.1.2.3", True), ("fe80::1%lo0", False),
    # The exfiltration shapes: a plain external name, a name that merely LOOKS like a
    # loopback literal, an address that resolves off-host, and the wildcard bind.
    ("evil.example.com", False), ("127.0.0.1.evil.com", False),
    ("10.0.0.5", False), ("0.0.0.0", False), ("169.254.169.254", False),
])
def test_the_daemon_token_is_only_ever_sent_to_loopback(tmp_path, host, loopback):
    """P34P1.3: `discover_daemon` accepted ANY host from control-api.json and the
    gateway shipped the whole-/v2 bearer there. The loopback boundary was documented
    and unenforced, so anything able to write one file under ~/.claudexor could
    redirect the daemon token to a host it controls — token exfiltration plus
    authenticated SSRF, from a file write. The refusal is typed and happens BEFORE any
    client exists; a name is never resolved (a name that resolves to loopback now can
    resolve elsewhere on the next lookup), so only literal loopback addresses and the
    exact name `localhost` pass."""
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "token").write_text("super-secret-daemon-token\n", encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": host, "port": 4242, "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")

    if loopback:
        endpoint = cx.discover_daemon(tmp_path)
        assert endpoint.host == host and endpoint.token == "super-secret-daemon-token"
        return
    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_endpoint_not_loopback"
    assert host in str(excinfo.value)
    # The token must not have travelled: the refusal precedes client construction.
    assert "super-secret-daemon-token" not in str(excinfo.value)


@pytest.mark.parametrize("token_name, token_bytes", [
    # A path out of a JSON descriptor can carry an embedded null: `read_text` raises a
    # bare `ValueError`, which is NOT an `OSError`.
    ("to\x00ken", None),
    # The token FILE can hold bytes that are not UTF-8: `UnicodeDecodeError` is likewise
    # a `ValueError` and not an `OSError`.
    ("token", b"\xff\xfetok"),
])
def test_an_unreadable_token_is_a_typed_refusal_not_a_bare_ValueError(
        tmp_path, token_name, token_bytes):
    """The half of the v6.87.44 widening nothing could falsify.

    The suite referenced `daemon_token_unreadable` nowhere and its only `tokenPath`
    fixture was a valid path, so reverting the catch to `except OSError` left every test
    green while a `ValueError` escaped `discover_daemon` untyped — past the
    `except ClaudexorUnavailable` in `subagents.py` and `delegate.py`, as a traceback.
    The `isinstance` assertion below is the actual claim: the caller's handler catches it.
    """
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    token_path = str(daemon_dir / token_name)
    if token_bytes is not None:
        pathlib.Path(token_path).write_bytes(token_bytes)
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 4242, "tokenPath": token_path,
    }), encoding="utf-8")

    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_token_unreadable"
    assert isinstance(excinfo.value, cx.ClaudexorUnavailable)
    # The descriptor read four lines up refuses the identical shape. Asserting the pair
    # is what keeps them from drifting apart again.
    (daemon_dir / "control-api.json").unlink()
    (daemon_dir / "control-api.json").write_bytes(b"\xff\xfe{}")
    with pytest.raises(cx.ClaudexorUnavailable) as sibling:
        cx.discover_daemon(tmp_path)
    assert sibling.value.code == "daemon_descriptor_unreadable"


def test_handshake_sends_the_protocol_header_and_pins_the_minimum_version():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get(cx.PROTOCOL_HEADER)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR,
            "compatible": True,
            "engine": {"version": CLAUDEXOR_MIN_VERSION},
        })

    with _gateway(handler) as gateway:
        gateway.handshake()
    assert seen["header"] == str(CLAUDEXOR_PROTOCOL_MAJOR)
    assert seen["auth"] == "Bearer secret-token"


def test_handshake_refuses_an_engine_older_than_the_minimum():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR, "compatible": True,
            "engine": {"version": "0.9.0"},
        })

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "engine_too_old"


def test_handshake_refuses_an_incompatible_protocol():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"protocolMajor": 2, "compatible": False})

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "protocol_incompatible"


def test_project_registration_is_a_required_first_step():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v2/projects":
            assert request.headers.get("Idempotency-Key")
            return httpx.Response(200, json={"id": "prj-1", "root": "/tmp/x"})
        return httpx.Response(404, json={
            "code": "project_not_registered", "message": "register the root first", "retryable": False,
        })

    with _gateway(handler) as gateway:
        assert gateway.register_project("/tmp/x") == "prj-1"
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.start_run({"prompt": "hi"})
    assert excinfo.value.code == "project_not_registered"
    assert ("POST", "/v2/projects") in calls


def test_the_window_class_is_chosen_by_the_code_not_by_sniffing_the_context():
    """`quota` was never a Claudexor code. The classifier keys on the real one —
    `subscription_window_exhausted`, the RunFailureCode the engine actually emits — so
    an unrelated refusal carrying a stray reset-shaped key is not announced as a spent
    subscription window and put on a retry timer."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={
            "code": "subscription_window_exhausted", "message": "window spent", "retryable": True,
            "context": {"resetsAt": "2030-01-01T00:00:00Z"},
        })

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorSubscriptionWindowExhausted) as excinfo:
            gateway.get_run("run-1")
    assert excinfo.value.reset_at == "2030-01-01T00:00:00Z"

    def conflict(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "code": "idempotency_conflict", "message": "same key, different body",
            "retryable": False, "context": {"cooldownUntil": "2030-01-01T00:00:00Z"},
        })

    with _gateway(conflict) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.get_run("run-1")
    assert excinfo.value.code == "idempotency_conflict"
    assert not isinstance(excinfo.value, cx.ClaudexorSubscriptionWindowExhausted)


def test_an_unreachable_daemon_is_a_typed_refusal_not_a_crash():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "daemon_unreachable"


def test_the_daemon_token_is_never_returned_to_callers():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"snapshots": []})

    with _gateway(handler) as gateway:
        assert "secret-token" not in json.dumps(gateway.quota_snapshots())


def test_managed_secret_write_uses_the_non_journaled_control_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"name": "anthropic", "stored": True})

    with _gateway(handler) as gateway:
        receipt = gateway.set_secret("anthropic", "test-value")

    assert seen == {
        "method": "POST",
        "path": "/v2/secrets",
        "body": {"name": "anthropic", "value": "test-value"},
    }
    assert receipt == {"name": "anthropic", "stored": True}


def test_a_per_request_bound_reaches_httpx_and_absence_is_not_an_unbounded_call():
    """The per-request bound had no pin at all: `_request` could be mutated to ignore
    `timeout_sec` outright and 253 tests stayed green, because everything that exercises
    it goes through `MockTransport`, which sees the request and never the timeout. So the
    one caller that needs it — a `delegate_wait` poll bounded by what its window has left
    — was relying on transport behaviour nothing checked.

    Both directions matter, and the second is the subtle one. Present, the value must
    ARRIVE at the client call (a bound that is computed and dropped is a 60s read wearing
    a five-second name). Absent, the kwarg must not be passed AT ALL: httpx reads an
    explicit `timeout=None` as "no timeout whatsoever", the exact opposite of the client
    default it would otherwise inherit, so the harmless-looking `timeout=timeout_sec`
    turns every ordinary call unbounded."""
    calls = []

    class _Recorder:
        def request(self, method, path, **kwargs):
            calls.append(kwargs)
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "summary": {}})

    gateway = cx.ClaudexorGateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
    gateway.close()
    gateway._client = _Recorder()

    gateway.get_run("run-1", timeout_sec=5.0)
    assert "timeout" in calls[-1], "a computed bound that never reaches httpx is not a bound"
    assert calls[-1]["timeout"].read == 5.0, calls[-1]
    assert calls[-1]["timeout"].connect == cx._CONNECT_TIMEOUT_SEC, calls[-1]

    gateway.get_run("run-2")
    assert "timeout" not in calls[-1], \
        "an absent bound must inherit the client default, and httpx reads timeout=None as NO timeout"


# -- 3.4 the nanny verbs -------------------------------------------------------


def test_both_child_allowlists_can_see_the_nanny_verbs():
    assert NANNY_TOOLS <= LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert NANNY_TOOLS <= ACTING_SUBAGENT_TOOL_NAMES


def test_there_is_no_hurry_verb():
    from ouroboros.tools import delegate

    names = {entry.name for entry in delegate.get_tools()}
    assert names == NANNY_TOOLS


# -- 3.6 accounting ------------------------------------------------------------


def test_a_subscription_session_settles_at_zero_and_keeps_the_projection_final(tmp_path):
    """A DISCLOSED zero is the free-session case: the money was spent when the plan was
    bought, so the row is final at 0.0 and the projection stays final.

    An UNDISCLOSED spend is not the same fact and must not be written as one. The engine's
    default auth preference is subscription-first with fallback to a paid key, and a route
    can bill by construction — settling those at a confident 0.0/final would hide real
    money from every budget fence while asserting the projection was complete.
    """
    from ouroboros.usage_accounting import record_subscription_session, usage_projection

    disclosed = tmp_path / "disclosed"
    record_subscription_session("s-free", drive_root=disclosed, route="r", task_id="t1",
                                root_task_id="t1", spend_usd=0.0)
    rows = [json.loads(l) for l in (disclosed / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_usd"] == 0.0 and row["cost_final"] is True
    assert usage_projection(disclosed)["cost_final"] is True

    charged = tmp_path / "charged"
    record_subscription_session("s-billed", drive_root=charged, route="r", task_id="t1",
                                root_task_id="t1", spend_usd=4.10)
    rows = [json.loads(l) for l in (charged / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_usd"] == 4.10, "a real charge must ride the ledger as money"
    assert row["cost_final"] is True

    unknown = tmp_path / "unknown"
    record_subscription_session("s-quiet", drive_root=unknown, route="r", task_id="t1",
                                root_task_id="t1")
    rows = [json.loads(l) for l in (unknown / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_final"] is False, "an undisclosed spend is not a proven zero"
    assert row["pricing_known"] is False
    # UNKNOWN must be None, not 0.0. A `cost_final=False` row costing 0.0 adds zero to
    # the projection's `estimated` total, and `not 0.0` is True — so the honest per-row
    # disclosure was invisible one layer up, which reported `cost_final: True` anyway.
    assert row["cost_usd"] is None
    projection = usage_projection(unknown)
    assert projection["cost_final"] is False, "an unknown session must drop finality"
    assert projection["unknown_unmetered"] == 1


def test_the_unmetered_external_row_would_have_dropped_cost_final(tmp_path):
    # The exact reason record_unmetered_external_dispatch must NOT be reused: one such
    # row makes the WHOLE projection non-final.
    ua.record_unmetered_external_dispatch("d1", drive_root=tmp_path, task_id="t1", root_task_id="t1")
    assert ua.usage_projection(tmp_path, root_task_id="t1")["cost_final"] is False


def test_a_session_is_not_counted_as_a_physical_provider_call(tmp_path):
    ua.record_subscription_session("run-2", drive_root=tmp_path, route="some-route", task_id="t2", root_task_id="t2")
    breakdown = ua.usage_breakdown(tmp_path, root_task_id="t2")
    assert breakdown["physical_calls"] == 0
    assert breakdown["subscription_sessions"] == 1


# -- 3.7 the failure class -----------------------------------------------------


def test_the_transport_error_code_is_the_failure_class_name():
    assert cx.ClaudexorSubscriptionWindowExhausted("x").code == SUBSCRIPTION_WINDOW_EXHAUSTED


def test_the_window_class_is_transient_and_scheduled_by_its_reset():
    exc = cx.ClaudexorSubscriptionWindowExhausted("spent", reset_at="2030-01-01T00:00:00Z")
    classification = classify_llm_exception(exc)
    assert classification.kind == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert classification.kind != "quota_exhausted"
    assert classification.retry_same_request is True
    # Scheduled by the reset instant, never by the 60s-capped exponential backoff.
    assert classification.retry_after_sec is not None
    assert classification.retry_after_sec > 60.0
    assert classification.reset_at == "2030-01-01T00:00:00Z"


def test_a_billing_refusal_stays_permanently_classified():
    classification = classify_llm_exception(RuntimeError("402 payment required"))
    assert classification.kind == "quota_exhausted"
    assert classification.retry_same_request is False
    assert classification.retry_after_sec is None


# -- 4. the executor axis actually reaches dispatch -----------------------------


class _HealthStub:
    """A gateway-shaped object for the dispatch rows.

    It used to answer the daemon manifest questions the rule table asked (access
    profiles, quota windows, the engine version). Those readings retired with the owned
    Claudexor gateway, so nothing dials this any more: the rows are resolved from the
    configured route alone, and what they pin is the retirement. The object stays so a
    `harness`/`auto` row is still driven with a real gateway where a live one would go.
    """

    def __init__(self, *, status="ok"):
        self.status = status

    def close(self): pass


def _dispatch(requested, *, route="some-route=weak:low", stub=None, monkeypatch=None,
              raises=None):
    from ouroboros.gateways import claudexor as gw
    from ouroboros.subagents import dispatch_executor_resolution

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", route)

    def _make(*a, **k):
        if raises is not None:
            raise raises
        return stub if stub is not None else _HealthStub()

    monkeypatch.setattr(gw, "ClaudexorGateway", _make)
    return dispatch_executor_resolution(
        {"delegation_role": "subagent", "requested_executor": requested})


def test_one_exhausted_credential_profile_does_not_take_the_harness_offline():
    """Defect D (D28): the readiness predicate reported a blocker as soon as ANY window
    of the harness was spent, so one exhausted account took the WHOLE harness offline
    while its siblings were live — an outage invented out of a healthy substrate, and
    the `harness` executor is a PIN, so the caller was refused rather than re-routed.

    Readiness is per SNAPSHOT now — the engine emits one per credential subject, so in
    practice one per account: the harness is usable while ANY of its snapshots is, and
    when they are all spent the instant reported is the EARLIEST, because the first to
    heal makes the harness usable again. The reader groups by `subject.harness` and
    deliberately never interprets `subject.subject_id`: WHICH profile a run lands on
    stays Claudexor's business, so no rotation moves into Ouroboros."""
    from ouroboros.subagents import _exhausted_window

    def _snap(profile, *, spent, reset="2026-08-03T12:00:00Z", harness="some-route",
              freshness="fresh", applies=None):
        # `subject_id` is the REAL QuotaSubject key for a credential profile
        # (packages/schema/src/quota.ts; the object is `.strict()`, so the `profile`
        # this fixture used to invent would be rejected by the engine's own parser).
        constraint = ({"used_ratio": 1.0, "resets_at": reset} if spent
                      else {"used_ratio": 0.4, "resets_at": reset})
        if applies is not None:
            constraint["applies_to_models"] = applies
        return {"subject": {"harness": harness, "subject_id": profile},
                "freshness": freshness, "constraints": [constraint]}

    class _Quota:
        def __init__(self, snaps, absences=None):
            self._snaps, self._absences = snaps, absences
        def quota_snapshots(self): return self._snaps
        def quota_absences(self): return self._absences or []

    # ONE of two profiles spent: the harness is still usable, so no blocker at all.
    mixed = _Quota([_snap("acct-a", spent=True, reset="2026-08-03T10:00:00Z"),
                    _snap("acct-b", spent=False)])
    assert _exhausted_window(mixed, "some-route") == (False, "")

    # ALL profiles spent: a blocker, at the EARLIEST reset (the first one to heal).
    both = _Quota([_snap("acct-a", spent=True, reset="2026-08-03T12:00:00Z"),
                   _snap("acct-b", spent=True, reset="2026-08-03T10:00:00Z")])
    assert _exhausted_window(both, "some-route") == (True, "2026-08-03T10:00:00Z")

    # A single-profile harness (no profile field at all) behaves exactly as before.
    single = _Quota([{"subject": {"harness": "some-route"}, "freshness": "fresh",
                      "constraints": [{"used_ratio": 1.0, "resets_at": "2026-08-03T09:00:00Z"}]}])
    assert _exhausted_window(single, "some-route") == (True, "2026-08-03T09:00:00Z")

    # Another harness's exhaustion is not ours, and a STALE snapshot never blocks.
    other = _Quota([_snap("acct-a", spent=True, harness="other-route")])
    assert _exhausted_window(other, "some-route") == (False, "")
    stale = _Quota([_snap("acct-a", spent=True, freshness="stale")])
    assert _exhausted_window(stale, "some-route") == (False, "")

    # And the live sibling wins even when the spent one is listed second.
    reordered = _Quota([_snap("acct-b", spent=False), _snap("acct-a", spent=True)])
    assert _exhausted_window(reordered, "some-route") == (False, "")


def test_a_model_scoped_window_does_not_block_a_route_pinned_to_another_model():
    """The live incident (2026-08-06): the claude route was pinned to opus, its ONE
    readable profile carried `weekly_scoped:Fable used_ratio=1.0` next to a healthy
    five-hour window, and the whole route read as spent until the Fable weekly reset —
    $82 of metered spend for a subscription that was free for opus the entire time.
    A window scoped to models this route never uses is someone else's exhaustion."""
    from ouroboros.subagents import _exhausted_window

    fable_scoped = {"subject": {"harness": "some-route", "subject_id": "acct"},
                    "freshness": "fresh",
                    "constraints": [
                        {"used_ratio": 0.0, "resets_at": "2026-08-07T00:00:00Z"},
                        {"used_ratio": 1.0, "resets_at": "2026-08-11T00:00:00Z",
                         "applies_to_models": ["fable", "claude-fable-5", "best"]},
                    ]}

    class _Quota:
        def __init__(self, snaps, absences=None):
            self._snaps, self._absences = snaps, absences
        def quota_snapshots(self): return self._snaps
        def quota_absences(self): return self._absences or []

    quota = _Quota([fable_scoped])
    # Pinned to opus: the Fable weekly window does not apply, the route is usable.
    assert _exhausted_window(quota, "some-route", "opus") == (False, "")
    # Pinned to fable (either alias direction): the scoped window DOES apply, and the
    # profile's healthy sibling constraint does not rescue it (a spent window blocks
    # its own profile whatever the other windows say).
    assert _exhausted_window(quota, "some-route", "fable") == (True, "2026-08-11T00:00:00Z")
    assert _exhausted_window(quota, "some-route", "claude-fable-5") == (True, "2026-08-11T00:00:00Z")
    # No model pin: any scoped window may apply to whatever model the run lands on.
    assert _exhausted_window(quota, "some-route", "") == (True, "2026-08-11T00:00:00Z")


def test_a_spent_window_with_no_reset_instant_is_still_spent():
    """The inverse defect (three reviewers independently): a fully-used window whose
    constraint named neither `resets_at` nor `cooldown_until` produced no collectable
    reset, and the old single-string contract could only express exhaustion AS a
    reset — so a positively spent route read back as healthy and D28's loud fallback
    never fired. Exhaustion and its healing instant are separate facts now."""
    from ouroboros.subagents import _exhausted_window, route_health, delegated_run_shape

    undated = {"subject": {"harness": "some-route", "subject_id": "acct"},
               "freshness": "fresh", "constraints": [{"used_ratio": 1.0}]}

    class _Quota:
        def __init__(self, snaps): self._snaps = snaps
        def quota_snapshots(self): return self._snaps
        def quota_absences(self): return []

    assert _exhausted_window(_Quota([undated]), "some-route") == (True, "")

    # And through the ONE health reader: an undated exhaustion still reaches the rule
    # table as `subscription_window_exhausted`, as the REASON with an empty reset.
    class _Gateway(_Quota):
        engine_version = "9.9.9"
        def agent_capabilities(self):
            return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                                   "accessProfilesSupported": ["readonly"]}]}

    unavailable, reset_at = route_health(
        _Gateway([undated]), "some-route", delegated_run_shape(False))
    assert (unavailable, reset_at) == ("subscription_window_exhausted", "")


def test_an_unreadable_profile_keeps_the_route_usable():
    """Exhaustion needs POSITIVE evidence for the WHOLE route. A profile whose quota
    endpoint answered 429 (or whose refresh failed) is an ABSENCE — unknown, not
    spent — so the readable-but-spent minority must not speak for the route: the
    daemon owns rotation and refuses typed at start time if the route is truly empty.
    (The live incident's second layer: the backup account's usage endpoint kept
    429-ing, so the one readable profile's Fable window silenced the whole harness.)"""
    from ouroboros.subagents import _exhausted_window

    spent = {"subject": {"harness": "some-route", "subject_id": "acct-a"},
             "freshness": "fresh",
             "constraints": [{"used_ratio": 1.0, "resets_at": "2026-08-11T00:00:00Z"}]}
    absence = {"subject": {"harness": "some-route", "subject_id": "acct-b"},
               "reason": "refresh_failed", "detail": "oauth/usage responded 429"}
    foreign_absence = {"subject": {"harness": "other-route", "subject_id": "acct-x"},
                       "reason": "refresh_failed", "detail": "oauth/usage responded 429"}

    class _Quota:
        def __init__(self, snaps, absences=None):
            self._snaps, self._absences = snaps, absences
        def quota_snapshots(self): return self._snaps
        def quota_absences(self): return self._absences or []

    # An absence on THIS route fail-opens it; a foreign route's absence changes nothing.
    assert _exhausted_window(_Quota([spent], [absence]), "some-route") == (False, "")
    assert _exhausted_window(
        _Quota([spent], [foreign_absence]), "some-route"
    ) == (True, "2026-08-11T00:00:00Z")

    # A gateway with no absence reader at all (test stubs, older fakes) keeps the
    # plain positive-evidence answer.
    class _NoAbsences:
        def __init__(self, snaps): self._snaps = snaps
        def quota_snapshots(self): return self._snaps

    assert _exhausted_window(
        _NoAbsences([spent]), "some-route") == (True, "2026-08-11T00:00:00Z")


# One row of the rule table per case, resolved through the REAL dispatch entry point
# rather than through the pure function it wraps.
def test_dispatch_row_auto_without_a_route_runs_native(monkeypatch):
    res = _dispatch("auto", route="", monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("native", "harness_not_configured")


def test_dispatch_row_auto_with_a_configured_route_no_longer_reaches_the_harness(monkeypatch):
    """Seed 0: the owned Claudexor gateway retired, so a configured route can no longer
    reach the harness lane — the row that used to read `harness`/`harness_ready` now
    degrades. `auto` still permits the metered fallback, but the child must not discover
    it by spending: the reason names the retirement and the note says METERED. The PIN
    keeps the opposite answer for the same reason it always did — it exists to refuse
    metered spend — and now names the retirement instead of a fault."""
    from ouroboros.agent import dispatch_executor_note

    res = _dispatch("auto", monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("native", "claudexor_retired")
    assert not res.blocked, "the retirement is permanent and disclosed, never a permanent block"
    note = dispatch_executor_note(res)
    assert "METERED" in note and "claudexor_retired" in note

    pinned = _dispatch("harness", monkeypatch=monkeypatch)
    assert pinned.blocked and pinned.reason == "claudexor_retired"


def test_dispatch_row_explicit_harness_blocks_and_never_reaches_the_native_path(monkeypatch):
    for stub, raises in (
        (_HealthStub(status="unavailable"), None),
        (None, cx.ClaudexorUnavailable("daemon_unreachable", "no daemon")),
    ):
        res = _dispatch("harness", stub=stub, raises=raises, monkeypatch=monkeypatch)
        # The regression this exists for: a pin that silently becomes a metered native
        # run bills the owner for precisely what the pin was asked to prevent.
        assert res.executor != "native", res
        assert res.blocked, res
    res = _dispatch("harness", route="", monkeypatch=monkeypatch)
    assert res.blocked and res.reason == "harness_not_configured"


def test_dispatch_row_native_is_native_and_asks_the_daemon_nothing(monkeypatch):
    from ouroboros.gateways import claudexor as gw
    from ouroboros.subagents import dispatch_executor_resolution

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route")

    def _boom(*a, **k):
        raise AssertionError("a native request must not touch the daemon")

    monkeypatch.setattr(gw, "ClaudexorGateway", _boom)
    res = dispatch_executor_resolution({"delegation_role": "subagent", "requested_executor": "native"})
    assert (res.executor, res.reason) == ("native", "requested_native")


def test_a_blocked_pin_ends_the_task_unrun_instead_of_spending(monkeypatch):
    from ouroboros.agent import executor_blocked_outcome

    res = _dispatch("harness", raises=cx.ClaudexorUnavailable("daemon_unreachable", "x"),
                    monkeypatch=monkeypatch)
    text, usage = executor_blocked_outcome(res)
    assert usage == {"execution_status": "infra_failed",
                     "reason_code": "subagent_executor_unavailable"}
    assert "NOT run on metered API tokens" in text
    # No visible marker for a blocked run: there is no child to inform.
    from ouroboros.agent import dispatch_executor_note
    assert dispatch_executor_note(res) == ""


def test_a_plain_task_is_not_subject_to_the_executor_axis(monkeypatch):
    """The guard lives at the PRODUCTION entry point, `agent.resolve_dispatch_axes`:
    a task with no `delegation_role: subagent` resolves no axes at all and never
    reaches the daemon. (There used to be a second, test-only wrapper in `agent.py`
    carrying its own copy of this guard while production went through
    `resolve_subagent_dispatch`; the guard is pinned where it actually runs.)"""
    from ouroboros.agent import resolve_dispatch_axes
    from ouroboros.gateways import claudexor as gw

    def _boom(*a, **k):
        raise AssertionError("a plain task must not touch the daemon")

    monkeypatch.setattr(gw, "ClaudexorGateway", _boom)
    task = {"type": "improvement"}
    assert resolve_dispatch_axes(task) is None
    assert "effective_executor" not in task


# -- 4. mutating AND read-only children, one nanny, one transport ---------------


def test_the_model_has_no_argument_that_could_widen_the_profile():
    from ouroboros.tools import delegate

    entry = next(e for e in delegate.get_tools() if e.name == "delegate_start")
    properties = set(entry.schema["parameters"]["properties"])
    # `retry_of` names an INVOCATION, not authority (ownership-checked replay);
    # root/bucket/skill_name are a SELECTOR resolved through the same
    # ResolvedResourceBinding authorizer as ordinary writes (R1 item 9).
    assert properties == {
        "prompt", "subagent_id", "max_seconds", "retry_of", "root", "bucket", "skill_name",
    }
    assert entry.schema["parameters"]["properties"]["root"]["enum"] == ["skill_payload"]
    assert not properties & {"access", "mode", "isolation", "scope", "write_surface", "cwd"}


def test_a_read_only_task_cannot_obtain_workspace_write(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.delegate import _derive_authority
    from ouroboros.tools.registry import ToolContext

    for constraint in (
        None,                                                       # no constraint at all
        TaskConstraint(mode="local_readonly_subagent"),             # explicitly read-only
        TaskConstraint(mode="acting_subagent", surface=""),         # acting but unresolved surface
        TaskConstraint(mode="acting_subagent", surface="bogus"),    # acting with an invalid surface
    ):
        ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_constraint=constraint)
        ctx.task_metadata = {"parent_task_id": "p"}
        authority = _derive_authority(ctx)
        assert authority.access == "readonly", constraint
        assert authority.mode == "ask" and authority.isolation == ""


@pytest.mark.parametrize("effective,entitled,widened", [
    ("readonly", "readonly", ""),
    ("readonly", "workspace_write", ""),          # narrower than asked is fine
    ("workspace_write", "workspace_write", ""),
    ("workspace_write", "readonly", "workspace_write"),
    ("full", "workspace_write", "full"),
    ("inherit_native", "workspace_write", "inherit_native"),
    ("a-profile-from-a-future-engine", "workspace_write", "a-profile-from-a-future-engine"),
])
def test_effective_access_is_verified_not_assumed(effective, entitled, widened):
    from ouroboros.tools.delegate import _widened_access

    detail = {"lastSeq": 12, "summary": {"effectiveAccess": effective, "state": "running"}}
    assert _widened_access(detail, entitled) == widened


def test_an_undisclosed_effective_profile_is_unverified_not_compliant():
    """Absence of evidence is not evidence of narrowness.

    An earlier version returned "" (compliant) whenever the field was missing, and a test
    codified that as `# not disclosed yet: nothing to judge` — so any daemon build, harness
    or malformed response that omitted the field turned the only containment gate into a
    silent no-op while the run kept writing. It also fell back to `summary["access"]`,
    which the daemon computes as `effectiveAccess ?? the client's own request`: that
    compares our request against itself and can only ever pass.
    """
    from ouroboros.tools.delegate import _ACCESS_UNVERIFIED, _widened_access

    # Before admission there really is nothing to judge.
    assert _widened_access({"summary": {"state": "queued"}}, "readonly") == ""
    assert _widened_access({"summary": {}}, "readonly") == ""

    # Absence only means "no evidence" while the run can still ACT, and only after it
    # has produced anything. The daemon marks a run `running` at DEQUEUE — before the
    # orchestrator writes the contract the profile is derived from — so judging that
    # moment cancelled healthy runs, and judging a terminal state reported a run that
    # merely failed to start as a containment breach.
    assert _widened_access({"lastSeq": 0, "summary": {"state": "running"}}, "readonly") == ""
    for state in ("succeeded", "failed", "cancelled", "interrupted"):
        detail = {"lastSeq": 40, "summary": {"state": state}}
        assert _widened_access(detail, "readonly") == "", state

    # A live run that HAS produced events and still discloses nothing has no evidence.
    live = {"lastSeq": 12, "summary": {"state": "running"}}
    assert _widened_access(live, "readonly") == _ACCESS_UNVERIFIED

    # The echo must not be accepted as an independent witness.
    detail = {"lastSeq": 12, "summary": {"state": "running", "access": "workspace_write"}}
    assert _widened_access(detail, "workspace_write") == _ACCESS_UNVERIFIED

    # A really widened profile is still caught in every state.
    for state in ("running", "succeeded"):
        detail = {"lastSeq": 12, "summary": {"state": state, "effectiveAccess": "full"}}
        assert _widened_access(detail, "readonly") == "full", state


def test_a_succeeded_run_that_never_proved_its_profile_says_so_in_its_result():
    """P34P1.4: a SUCCEEDED run whose summary carries no `effectiveAccess` was accepted
    as compliant — a result with no evidence that the profile the host asked for is the
    profile the engine enforced, which is the name-without-proof class this module
    exists to refuse.

    Enforcement is NOT the answer for a finished run: it is over, there is nothing left
    to contain, and routing absence through the breach path would CANCEL a succeeded run
    and destroy the very result the lane exists to fetch (the v6.87.37 lesson — the
    containment gate stopped cancelling healthy runs for exactly this reason). So it is
    DISCLOSED, on the same terminal payload the parent reads, like the HOME half's
    missing fact. Both lanes get it: `readonly` staying `readonly` is the profile that
    matters most, and the `containment` block is asked only of marker-carrying runs."""
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _terminal_payload

    # A succeeded run with NO disclosed profile: unverified, and it says why.
    silent = {"lastSeq": 40, "summary": {"state": "succeeded"}}
    evidence = _terminal_payload("run-1", silent, delegated_run_shape(False))["access_evidence"]
    assert evidence["verified"] is False and evidence["effective"] == ""
    assert evidence["requested"] == "readonly" and evidence["state"] == "succeeded"
    assert "SUCCEEDED without ever disclosing" in evidence["note"]

    # A succeeded run that DID disclose one is verified, with no note.
    proven = {"lastSeq": 40, "summary": {"state": "succeeded", "effectiveAccess": "readonly"}}
    evidence = _terminal_payload("run-1", proven, delegated_run_shape(False))["access_evidence"]
    assert evidence == {"requested": "readonly", "effective": "readonly",
                        "verified": True, "state": "succeeded"}

    # A run that did NOT succeed keeps the softer wording: it may never have had a
    # profile at all, so this is absence of evidence rather than a missing proof.
    for state in ("failed", "cancelled", "interrupted"):
        detail = {"lastSeq": 40, "summary": {"state": state}}
        evidence = _terminal_payload("run-1", detail, delegated_run_shape(False))["access_evidence"]
        assert evidence["verified"] is False, state
        assert "absence of evidence, not a breach" in evidence["note"], state

    # The ECHO is never a witness: the daemon computes `access` as
    # `effectiveAccess ?? our own request`, so a payload carrying only the echo must
    # still read unverified.
    echo = {"lastSeq": 40, "summary": {"state": "succeeded", "access": "readonly"}}
    assert _terminal_payload("run-1", echo, delegated_run_shape(False))[
        "access_evidence"]["verified"] is False

    # The mutating lane carries BOTH halves, and neither displaces the other.
    mutating = _terminal_payload("run-1", silent, delegated_run_shape(True))
    assert mutating["access_evidence"]["verified"] is False
    assert mutating["containment"]["verified"] is False


def test_a_mutating_run_requires_an_ACTIVE_workspace_not_merely_agreement(tmp_path):
    """Agreement alone reopened the critical it was written to close.

    `active_repo_dir_for` falls back to `repo_dir` when workspace mode is off, so a
    constraint whose `write_root` happens to name that same directory made the equality
    check pass — and handed an external shell the live repository, which is exactly the
    original defect.
    """
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutation_authority
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(repo)),
    )
    ctx.workspace_root = None
    ctx.workspace_mode = ""
    record, refusal = _mutation_authority(
        ctx, delegated_run_shape(True))
    assert refusal and "workspace_not_active" in refusal, refusal
    assert record == {}


def test_an_unresolvable_write_root_is_a_typed_refusal_not_a_traceback(tmp_path):
    """"Can this path be resolved at all" is ONE question, not an exception set.

    Embedded nulls and symlink loops have changed their exact `Path.resolve()` failure
    behaviour across supported Python versions. Either escaping `_mutating_run_root`
    aborts `delegate_start` with a traceback instead of the typed refusal the function
    exists to produce — and a guard that raises delivers no decision at all.
    """
    import os

    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.delegate_containment import _resolved as containment_resolved
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutation_authority, _resolved
    from ouroboros.tools.registry import ToolContext

    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    assert _resolved(tmp_path / "a" / "x") is None, "a symlink loop must resolve to None"
    assert containment_resolved(tmp_path / "a" / "x") is None
    assert _resolved("/etc/passwd\x00") is None, "an embedded null must resolve to None"
    assert _resolved(tmp_path) == tmp_path.resolve(), "an ordinary path still resolves"
    missing = tmp_path / "missing" / "leaf"
    assert _resolved(missing) == missing.resolve(strict=False)
    assert containment_resolved(missing) == missing.resolve(strict=False)

    workspace = tmp_path.parent / f"ws-{tmp_path.name}"
    workspace.mkdir()
    ctx = ToolContext(
        repo_dir=tmp_path / "repo", drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(tmp_path / "a" / "x")),
    )
    ctx.workspace_root = str(workspace)
    ctx.workspace_mode = "self_worktree"
    record, refusal = _mutation_authority(
        ctx, delegated_run_shape(True))
    assert refusal and "write_root_mismatch" in refusal, refusal
    assert record == {}


def test_an_inactive_workspace_is_refused_even_when_the_root_is_set(tmp_path):
    """The DISTINGUISHING case for the round-3 predicate fix, which had no test.

    The old check was `workspace_mode_block_reason(ctx) == "" and workspace_root set`,
    and `workspace_mode_block_reason` returns "" precisely WHEN `workspace_mode` is
    empty — so with a root set and the mode empty, the old condition passed and handed a
    shell the fallback root. Every existing test cleared BOTH fields, which the old
    predicate also refused via its `workspace_root` leg, so reverting the fix left the
    suite green. This is the one shape that tells the two predicates apart.
    """
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tool_access import workspace_mode_block_reason
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutation_authority
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(repo)),
    )
    ctx.workspace_root = str(repo)   # SET...
    ctx.workspace_mode = ""          # ...but the mode is not, so the workspace is not active

    assert workspace_mode_block_reason(ctx) == "", "the old predicate's leg is satisfied here"
    assert ctx.is_workspace_mode() is False, "yet the workspace is genuinely inactive"

    record, refusal = _mutation_authority(
        ctx, delegated_run_shape(True))
    assert refusal, "an inactive workspace must be refused"
    assert "workspace_not_active" in refusal, refusal
    assert record == {}


# -- 3.8 custody is durable, not process-local ---------------------------------


def _event_types(tmp_path):
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line).get("type") for line in path.read_text().splitlines() if line.strip()]


class _LiveRunStub:
    """A daemon whose run starts and keeps running."""

    def __init__(self, run_id="run-live", state="running"):
        self.run_id, self.state, self.cancels = run_id, state, []

    def handshake(self, **_kw): return {}
    def agent_capabilities(self):
        return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                               "accessProfilesSupported": ["readonly"]}]}
    def quota_snapshots(self): return []
    def find_project_id(self, root): return "prj-existing"
    def start_run(self, request, *, idempotency_key=""): return {"runId": self.run_id}
    # `effectiveAccess` is what the daemon DERIVES, and the containment reader treats an
    # undisclosed profile on a run that has already produced journal events as unverified.
    # A read-only fixture that omits it is not a narrower daemon, it is an unfaithful one.
    def get_run(self, rid, *, timeout_sec=None):
        return {"lastSeq": 1, "summary": {"state": self.state, "effectiveAccess": "readonly"}}
    def cancel_run(self, rid, reason=""):
        self.cancels.append((rid, reason))
        return {"accepted": True, "status": "accepted"}
    def remove_project(self, pid): pass
    def close(self): pass


def test_durable_truncation_is_disclosed_never_a_bare_slice(tmp_path):
    """P34R.5: durable/cognitive surfaces in the delegation core hand-rolled `[:N]`
    slices — the containment-incident row cut its EVIDENCE at 500 chars with no
    marker at all, and the primary-output disclosure reason at 300. Every bound now
    goes through the shared `truncate_review_artifact` contract: the cut is marked,
    the original length is named, and the anti-waste floor never spends a marker
    longer than the text it saves."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate

    entry = dc.RunCustody(run_id="run-x", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry, "cancel_unverified", "E" * 5000)
    fault = dc.open_containment_faults(tmp_path)[0]
    assert fault["detail"].startswith("E" * 2000)
    assert "OMISSION NOTE" in fault["detail"] and "original length 5000" in fault["detail"]

    # The anti-waste floor: a cut that saves fewer chars than its own marker
    # passes the text through whole instead of destroying it.
    entry2 = dc.RunCustody(run_id="run-y", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry2, "cancel_unverified", "F" * 2010)
    fault2 = [f for f in dc.open_containment_faults(tmp_path) if f["run_id"] == "run-y"][0]
    assert fault2["detail"] == "F" * 2010

    class _Boom:
        def get_run_artifact(self, rid, path):
            raise RuntimeError("Z" * 900)

    primary = {"truncated": True, "path": "out.md", "bytes": 10, "text": "abc"}
    _resolved_primary, ok, disclosure = delegate._resolve_full_primary_output(
        _Boom(), "run-x", primary)
    assert ok is False
    assert "OMISSION NOTE" in disclosure["reason"] and "original length" in disclosure["reason"]


def test_an_unresolved_containment_fault_cannot_age_out_of_the_health_view(tmp_path):
    """P34R.3: `open_containment_faults` scanned only the last 4 MB of the canonical
    event log, so an UNRESOLVED containment fault — an overpowered run that may still
    be live — silently vanished from the health invariants once later unrelated
    traffic buried its row, despite the stated contract that it stays CRITICAL until
    a terminal receipt resolves it. Incidents now live in their own compact durable
    projection that is read WHOLE; the event-log tail remains as the fallback surface
    for a fault whose compact write failed."""
    import ouroboros.delegate_custody as dc

    entry = dc.RunCustody(run_id="run-fault", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry, "cancel_unverified", "engine went dark")

    # Bury the fault under MORE than the tail window of later unrelated custody rows.
    noise = json.dumps({"type": "delegate_run_reconciled", "run_id": "run-noise",
                        "task_id": "t-b", "pad": "x" * 1500})
    events = dc.event_log_path(tmp_path)
    with events.open("a", encoding="utf-8") as fh:
        for _ in range(3000):
            fh.write(noise + "\n")
    assert events.stat().st_size > dc._FAULT_SCAN_TAIL_BYTES, "the fault is outside the tail"

    open_faults = dc.open_containment_faults(tmp_path)
    assert [f["run_id"] for f in open_faults] == ["run-fault"], \
        "an unresolved incident must never age out of the health view"
    assert open_faults[0]["reason"] == "cancel_unverified"

    # A resolution clears it durably, and later noise cannot reopen it.
    dc.resolve_containment_fault(tmp_path, entry, "verified_terminal")
    assert dc.open_containment_faults(tmp_path) == []
    with events.open("a", encoding="utf-8") as fh:
        for _ in range(200):
            fh.write(noise + "\n")
    assert dc.open_containment_faults(tmp_path) == []

    # Fallback surface: a fault whose COMPACT write failed is still visible through
    # the event-log tail — either landing alone keeps the incident visible.
    other = tmp_path / "other-drive"
    (other / "logs").mkdir(parents=True)
    dc._faults_path(other).mkdir()          # the compact append will fail loudly
    dc.record_containment_fault(other, entry, "cancel_unreachable", "")
    assert [f["run_id"] for f in dc.open_containment_faults(other)] == ["run-fault"]


# -- 3.9 cancellation reports only what it verified ----------------------------


def test_cancel_and_verify_carries_the_verify_reads_terminal_detail(tmp_path):
    """BR2-1, purely additive: when the verify read discovers a terminal state,
    the already-read run detail rides the result as the OPTIONAL `terminal_detail`
    key, so a caller consuming a discovered natural terminal (completion wins)
    never depends on a second fetch after settlement. The key is ABSENT on every
    other outcome — the historical six-key shape is untouched — and it never
    rides the emitted cancel-outcome event."""
    import ouroboros.delegate_custody as dc

    detail = {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 0.0,
                                        "inputTokens": 1, "outputTokens": 1}}

    class _Finished:
        def cancel_run(self, rid, reason=""):
            return {"accepted": True, "status": "accepted"}
        def get_run(self, rid, **_kw):
            return detail

    entry = dc.RunCustody(run_id="run-td", task_id="t-a", route_id="r", model="m",
                          project_id="p", project_owned=False, root_task_id="t-a",
                          ledger_root=str(tmp_path))
    dc.record_started(tmp_path, entry)
    out = dc.cancel_and_verify(tmp_path, _Finished(), entry, "test")
    assert out["outcome"] == "confirmed" and out["state"] == "succeeded"
    assert out["terminal_detail"] == detail

    class _Live:
        def cancel_run(self, rid, reason=""):
            return {"accepted": True, "status": "accepted"}
        def get_run(self, rid, **_kw):
            return {"lastSeq": 3, "summary": {"state": "running"}}

    entry2 = dc.RunCustody(run_id="run-td2", task_id="t-a", route_id="r", model="m",
                           project_id="p", project_owned=False, root_task_id="t-a",
                           ledger_root=str(tmp_path))
    dc.record_started(tmp_path, entry2)
    out2 = dc.cancel_and_verify(tmp_path, _Live(), entry2, "test")
    assert out2["outcome"] == "requested"
    assert set(out2) == {"outcome", "accepted", "control_status", "state",
                         "fault_reason", "detail"}, out2

    rows = [json.loads(line) for line in
            (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    outcomes = [r for r in rows if r.get("type") == "delegate_run_cancel_outcome"]
    assert outcomes and all("terminal_detail" not in r for r in outcomes)


# -- 3.12 reconciliation on restart / parent terminalization -------------------


def test_an_orphaned_delegated_run_is_reconciled_when_its_owner_is_gone(tmp_path, monkeypatch):
    """The predicate is the one `process_custody.reap_orphaned_processes` already owns:
    the owning task is no longer in the supervisor's live set. A delegated run has no
    pid, so the process reaper cannot see it — but it is still spending quota and still
    writing to a workspace."""
    import ouroboros.delegate_custody as dc

    live = _LiveRunStub(run_id="run-orphan")
    finished = _LiveRunStub(run_id="run-done")
    finished.get_run = lambda rid: {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.0}}

    for stub, task in ((live, "t-gone"), (finished, "t-also-gone")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=stub.run_id, task_id=task, route_id="r", model="m",
            project_id="p", project_owned=False, root_task_id=task, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-alive", task_id="t-running", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-running", ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    class _Router(_LiveRunStub):
        def get_run(self, rid, **_kw):
            return (finished if rid == "run-done" else live).get_run(rid)
        def cancel_run(self, rid, reason=""):
            return live.cancel_run(rid, reason)

    outcomes = dc.reconcile_orphaned_runs(tmp_path, {"t-running"}, gateway_factory=_Router)
    dc._CUSTODY.clear()
    by_run = {row["run_id"]: row for row in outcomes}
    assert set(by_run) == {"run-orphan", "run-done"}, "a live owner's run must be left alone"
    assert by_run["run-orphan"]["action"] == "cancelled"
    assert live.cancels == [("run-orphan", "owner_task_gone")]
    assert by_run["run-done"]["action"] == "settle_attempted" and by_run["run-done"]["settled"] is True

    # Unknown liveness reconciles nothing: never mass-cancel on missing information.
    live.cancels.clear()
    assert dc.reconcile_orphaned_runs(tmp_path, None, gateway_factory=_Router) == []
    assert live.cancels == []
    dc._CUSTODY.clear()


def test_a_terminalizing_parent_releases_the_run_it_still_holds(tmp_path):
    """The in-process twin of reconciliation. A parent that finishes while its delegated
    run is still going used to leave it mutating until the next 10-minute sweep; the
    loop's own resource-release point now settles or cancels it like any held resource.
    A task that delegated nothing must pay nothing for this."""
    import ouroboros.delegate_custody as dc

    live = _LiveRunStub(run_id="run-held")
    dc._CUSTODY.clear()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-held", task_id="t-parent", route_id="r",
        model="m", project_id="p", project_owned=False, ledger_root=str(tmp_path),
    ))
    assert dc.release_task_runs(tmp_path, "t-someone-else", gateway_factory=lambda: live) == []
    assert live.cancels == [], "another task's run is not this task's to release"

    outcomes = dc.release_task_runs(tmp_path, "t-parent", gateway_factory=lambda: live)
    dc._CUSTODY.clear()
    assert [row["action"] for row in outcomes] == ["cancelled"]
    assert live.cancels == [("run-held", "owner_task_gone")]


def test_the_loops_own_release_point_reaches_the_delegated_reconciler(tmp_path, monkeypatch):
    """`release_task_runs` only helps if something CALLS it. The test beside this one drives
    the function directly, so it passed with the loop's wiring deleted — and the loop is
    the ordinary path: without it a terminalized parent leaves its run mutating until the
    next ten-minute sweep. The release must also read the CANONICAL root, not the child
    drive the subagent runs on, or it looks for custody where none was written."""
    from types import SimpleNamespace

    import ouroboros.delegate_custody as dc
    import ouroboros.loop as loop

    released = []
    monkeypatch.setattr(dc, "release_task_runs",
                        lambda root, task_id, **kw: released.append((str(root), task_id)) or [])
    canonical = tmp_path / "canonical"
    inner = SimpleNamespace(drive_root=tmp_path / "child",
                            task_metadata={"budget_drive_root": str(canonical)})
    loop._cleanup_loop_resources(None, loop._LoopExitContext(
        tools=SimpleNamespace(_ctx=inner), drive_root=tmp_path, task_id="t-parent",
        event_queue=None, drive_logs=tmp_path / "logs", accumulated_usage={}, llm_trace={}))
    assert released == [(str(canonical), "t-parent")], released


def test_the_startup_sweep_reconciles_delegated_runs_too(monkeypatch):
    """Nothing is running yet at supervisor startup, so every open delegated run is by
    definition ownerless. The only server-side test covered the PERIODIC tick, so the
    startup half could be deleted without a single failure — and it is the half that
    catches the runs the generation that died was watching."""
    import server
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc

    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes", lambda root, **kw: [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.setdefault("live", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(server, "_installed_skill_names", lambda: None)
    server._startup_custody_sweep()
    assert seen["live"] == set(), "an empty live set is the point: nothing survived the restart"


def test_both_custody_surfaces_see_the_same_live_task_set(monkeypatch):
    """The periodic sweep must hand the delegated reconciler the SAME live task set the
    process reaper gets. Two copies of "is the owner still running" is exactly how one
    custody surface ends up reaping while its twin does not."""
    import time

    import server
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc
    import supervisor.queue as queue

    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes",
                        lambda root, **kw: seen.__setitem__("processes", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.__setitem__("delegated", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(server, "_installed_skill_names", lambda: None)
    monkeypatch.setitem(queue.RUNNING, "t-live", {})
    server._periodic_supervisor_maintenance([0.0], [time.time()])
    assert seen["processes"] == seen["delegated"] == {"t-live"}, seen


def test_the_public_wait_is_event_only_and_its_outer_bound_matches_task_lifetime():
    """The model cannot request a fake return window; the host renews quiet windows."""
    import os

    from ouroboros.config import (
        DELEGATE_WAIT_CEILING_SEC,
        DELEGATE_WAIT_WINDOW_MAX_SEC,
        get_delegate_wait_max_sec,
        get_task_abs_ceiling_sec,
    )
    from ouroboros.delegate_progress import EXTERNAL_WAIT_LEASE_CEILING_SEC
    from ouroboros.loop_tool_execution import _DEADLINE_CLAMPED_TOOLS, _PER_CALL_TIMEOUT_TOOLS
    from ouroboros.tools.delegate import get_tools

    entry = next(e for e in get_tools() if e.schema["name"] == "delegate_wait")
    assert "wait_sec" not in entry.schema["parameters"]["properties"]
    assert entry.timeout_sec == get_task_abs_ceiling_sec() + 120
    assert DELEGATE_WAIT_WINDOW_MAX_SEC < DELEGATE_WAIT_CEILING_SEC < EXTERNAL_WAIT_LEASE_CEILING_SEC
    assert (DELEGATE_WAIT_WINDOW_MAX_SEC, DELEGATE_WAIT_CEILING_SEC,
            EXTERNAL_WAIT_LEASE_CEILING_SEC) == (1800, 2100, 2400)
    # ...and neither escape hatch applies to this tool, which is why the ToolEntry
    # value really is the bound. The task deadline is a separate concern and is
    # honoured INSIDE the tool (see the wait-window test below), which is why the
    # outer clamp still must not apply: it would thread-kill the graceful return.
    assert "delegate_wait" not in _PER_CALL_TIMEOUT_TOOLS
    assert "delegate_wait" not in _DEADLINE_CLAMPED_TOOLS

    previous = os.environ.get("OUROBOROS_DELEGATE_WAIT_MAX_SEC")
    os.environ["OUROBOROS_DELEGATE_WAIT_MAX_SEC"] = "7200"
    try:
        # The configurable max clamps to the hard window max — NOT to the
        # ToolEntry timeout: raising the executor timeout must never silently
        # widen the askable window.
        assert get_delegate_wait_max_sec() == DELEGATE_WAIT_WINDOW_MAX_SEC
    finally:
        if previous is None:
            os.environ.pop("OUROBOROS_DELEGATE_WAIT_MAX_SEC", None)
        else:
            os.environ["OUROBOROS_DELEGATE_WAIT_MAX_SEC"] = previous


# -- the window is a WINDOW: the timer waits, the human's stream does not ------


class _StreamingStub:
    """A daemon whose journal cursor advances on EVERY poll — i.e. a healthy run.

    `_LiveRunStub` and `_AliveStub` both hold `lastSeq` constant, so every existing wait
    test exercises the silent path. This is the busy one, and it is the shape that used
    to cost a full-context nanny round per event batch.
    """

    def __init__(self, *, state="running", batch=1, title="running tests"):
        self.seq, self.state, self.batch, self.title = 0, state, batch, title

    def handshake(self, **_kw): return {"compatible": True, "protocolMajor": 3}

    def get_run(self, rid, *, timeout_sec=None):
        self.seq += 1
        return {
            "lastSeq": self.seq,
            "summary": {"state": self.state, "effectiveAccess": "readonly"},
            "timeline": [{"type": "tool", "title": self.title, "severity": "info"}
                         for _ in range(self.seq * self.batch)],
        }

    def close(self): pass


def test_every_poll_is_bounded_by_what_the_window_has_left(tmp_path, monkeypatch):
    """The bound belongs on EVERY poll, not just the one after the window is spent.

    A poll started a moment BEFORE expiry carries the client's own 60-second read
    default, so it can answer long after the window — and after the task deadline the
    clamp above exists to protect. Measured against an unbounded in-loop poll: 2.11s of
    wall for a window that reported `waited_sec=1`, with the deadline already crossed.
    The window is the ceiling for the transport too, so what each poll may ASK for is
    what the window still has (never below the floor a bound is useful at, and never
    above the read default the client would have used anyway — a bound that WIDENS the
    ask is not a bound)."""
    from ouroboros.delegate_progress import bounded_poll
    from ouroboros.gateways.claudexor import _READ_TIMEOUT_SEC, SHORT_POLL_TIMEOUT_SEC

    asked = []

    class _Recorder:
        def get_run(self, rid, *, timeout_sec=None):
            asked.append(timeout_sec)
            return {"lastSeq": 1, "summary": {"state": "running"}}

    gateway = _Recorder()
    bounded_poll(gateway, "run-1", 40.0)      # plenty of window left -> ask for it
    bounded_poll(gateway, "run-1", 0.5)       # nearly spent -> the floor, not 60s
    bounded_poll(gateway, "run-1", -3.0)      # spent -> the floor, still bounded
    assert asked == [40.0, SHORT_POLL_TIMEOUT_SEC, SHORT_POLL_TIMEOUT_SEC], asked
    assert all(value is not None for value in asked), \
        "an unbounded poll is the client's 60s default, which outlives any window"

    # The direction a floor alone got backwards: max() handed a long window's
    # surplus to the transport ask (1797.0 for a 1800s window), so a hung daemon
    # held the whole window. A bound NARROWS both directions or it is decoration.
    asked.clear()
    bounded_poll(gateway, "run-1", 1797.0)
    bounded_poll(gateway, "run-1", 61.0)
    assert asked == [_READ_TIMEOUT_SEC, _READ_TIMEOUT_SEC], \
        f"a bound above the client's own default grants a hung read MORE rope: {asked}"


def test_a_verbose_timeline_tail_cannot_push_the_whole_payload_over_the_limit():
    """The advance list is sized against what the REST of the payload leaves, not a
    fixed share of the budget. `timeline_tail` carries harness-authored text — three
    fields, each bounded at 300 chars, twelve rows — so it can eat most of the limit on
    its own; a list that fitted its own third then rode on top and the whole result
    overflowed, where the generic truncator cut the JSON mid-structure and the model got
    something it could not parse. That is the same failure the measured bound exists to
    prevent, one level up."""
    from ouroboros.delegate_progress import WindowObservations, window_payload
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    limit = tool_result_limit("delegate_wait")
    # TWO long fields per row is what tips it: one alone stays under the limit.
    verbose = [{"title": "T" * 400, "type": "Y" * 400, "severity": "info"}
               for _ in range(12)]
    seen = WindowObservations()
    timeline = []
    for i in range(601):
        timeline = timeline + [{"title": "x" * 80, "type": "e"}]
        seen.record({"timeline": list(timeline)}, i + 1, i * 3)

    payload = window_payload(
        run_id="run-1", state="running", last_seq=601, window=1800,
        elapsed_seconds=1800, max_seconds=1800, waiting_on_user=False,
        detail={"timeline": verbose}, seen=seen, budget=limit)

    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw) <= limit, len(raw)
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw, \
        "the generic truncator had to cut a payload the sizing claimed would fit"
    assert json.loads(raw) == payload, "the model received unparseable JSON"
    # The tail keeps its full bounded self; it is the ADVANCE list that yields room.
    assert len(payload["timeline_tail"]) == 12
    assert payload["advances"], "the list yielded room without disappearing"


def test_the_advance_list_yields_entirely_rather_than_pushing_the_payload_over():
    """One notch past the sibling above, where the residual cannot hold even the FLOOR.
    The fit loop kept a 400-char floor for the advance list no matter what was left, so
    the floor itself overflowed: twelve tail rows with all three harness fields at 20 000
    chars left ~400 chars once the closing `note` was reserved, the floor shipped 422 on
    top, and the 15 018-char result crossed a 15 000 limit — where the generic truncator
    cut the JSON mid-structure and the model got a payload it could not parse. The
    payload WITHOUT the list measured 14 596, so this is the module's own floor
    overflowing, not harness text that no sizing could have fitted.

    The floor is an ask, not a guarantee: the list drops to its omission marker, and the
    drop is disclosed where the list stood."""
    from ouroboros.delegate_progress import WindowObservations, window_payload
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    limit = tool_result_limit("delegate_wait")
    verbose = [{"title": "T" * 20_000, "type": "Y" * 20_000, "severity": "S" * 20_000}
               for _ in range(12)]
    seen = WindowObservations()
    timeline = []
    for i in range(12):
        timeline = timeline + [{"title": "x" * 80, "type": "e"}]
        seen.record({"timeline": list(timeline)}, i + 1, i * 3)

    payload = window_payload(
        run_id="run-1", state="running", last_seq=12, window=1800,
        elapsed_seconds=1800, max_seconds=1800, waiting_on_user=False,
        detail={"timeline": verbose}, seen=seen, budget=limit)
    raw = json.dumps(payload, ensure_ascii=False, indent=2)

    assert len(raw) <= limit, len(raw)
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw, \
        "the generic truncator had to cut a payload the sizing claimed would fit"
    assert json.loads(raw) == payload, "the model received unparseable JSON"
    # Nothing vanished quietly: the list says it yielded, how much, and through where.
    marker, = payload["advances"]
    assert marker["advances_omitted"] == len(seen.advances) == 12, marker
    assert marker["omitted_through_seq"] == 12, marker
    # ...and the note points at where omitted rows ACTUALLY are (the daemon's
    # run directory). The old "streamed live / in the event log" was untrue on
    # both halves - a pointer to a log that never had the rows is worse than none.
    assert "Claudexor's own timeline" in marker["note"], marker
    assert "event log" not in marker["note"], marker


def test_label_shedding_is_disclosed_on_the_row_that_gave_them_up():
    """The regime `test_the_advance_list_is_a_list_not_a_count` cannot name: a budget
    that forces labels out but keeps every advance. Oldest-first, disclosed per row,
    newest labels kept — a silently emptied `events` list would be a lie about what the
    run did, and this is the only place that lie is cheap to tell."""
    from ouroboros.delegate_progress import WindowObservations

    seen = WindowObservations()
    timeline = []
    for i in range(8):
        timeline = timeline + [{"title": "L" * 200, "type": "harness.event"}]
        seen.record({"timeline": list(timeline)}, i + 1, i)

    rows = seen.rows(1200)          # room for the spine and a couple of label sets

    assert [row["seq"] for row in rows] == list(range(1, 9)), "no advance was dropped"
    assert rows[-1]["events"], "the newest advance keeps its labels"
    shed = [row for row in rows if "events_omitted" in row]
    assert shed, rows
    assert all(row["events"] == [] for row in shed), shed
    assert all(row["events_omitted"] > 0 for row in shed), shed
    assert [row["seq"] for row in shed] == sorted(row["seq"] for row in shed), shed
    assert shed[0]["seq"] == 1, "shedding starts at the OLDEST advance"
    assert len(json.dumps(rows, ensure_ascii=False, indent=2)) <= 1200


def test_a_batch_bigger_than_the_display_tail_says_how_much_it_is_not_showing():
    """The cut that had NO vocabulary at all: not the budget's, the OBSERVATION's.

    `record` sized each batch with the display tail written for the standing timeline —
    the last twelve rows, head dropped in silence. That is right for a timeline whose
    head the model has already seen and wrong for a batch whose head arrived one poll
    ago. Reproduced against a harness emitting sixteen rows per cursor step: 48 rows
    published in-window, 36 delivered, and E17-E20 / E33-E36 / E49-E52 gone from the live
    stream AND from `advances`, with the rows carrying only ['at_sec', 'events', 'seq'].

    A batch may still be bounded — a busy daemon can publish hundreds of rows between two
    three-second polls. What it may not do is cut without saying so, in a module whose
    whole point is that an omission is disclosed."""
    from ouroboros.delegate_progress import WindowObservations, live_line

    seen = WindowObservations()
    timeline, per_step = [], 16
    for step in range(1, 4):
        timeline.extend({"type": "tool", "title": f"E{step * per_step - per_step + i}"}
                        for i in range(per_step))
        seen.record({"timeline": list(timeline)}, step, step)

    rows = seen.rows(100_000)             # a budget that forces no shedding of its own
    assert len(rows) == 3, rows
    for row in rows:
        assert "events_omitted" in row, f"the batch was cut and said nothing: {row}"
        assert len(row["events"]) + row["events_omitted"] == per_step, row
        assert row["events_omitted"] == per_step - 12, row
    # The human's stream is the surface that had no marker whatsoever.
    assert "+4 earlier in this batch" in live_line("run-1", seen.advances[0])
    # And the two cuts ADD rather than one overwriting the other: a row whose batch was
    # already cut, then shed for budget, must report ALL sixteen and not just the twelve
    # this payload dropped.
    tight = seen.rows(400)
    assert [row["seq"] for row in tight if "seq" in row] == [1, 2, 3], tight
    assert [row["events_omitted"] for row in tight if "seq" in row] == [per_step] * 3, tight


def test_a_long_busy_windows_advance_list_is_measured_not_estimated():
    """The high-cardinality sibling of the verbose-harness case: 601 advances of
    ordinary titles - the realistic worst case (1800s ceiling / 3s poll = 600).

    The bound used to be ESTIMATED: a running total decremented by each shed row, and
    then a survivor count of `budget // 40` on the assumption that a bare spine row
    costs about forty characters. Both assumptions ran UNDER the truth (a rendered
    `{"seq": …, "at_sec": …, "events": [], "events_omitted": …}` costs far more than
    forty, and the caller renders with `indent=2`, which the estimate never accounted
    for), so this shape left here already over `tool_result_limit("delegate_wait")`. The
    generic truncator then cut the JSON mid-structure and the model was handed a payload
    it could not parse AT ALL — strictly worse than any amount of shedding, because a
    disclosed omission is still readable and a severed object is not.

    So the size is MEASURED, with the caller's own rendering, and re-measured after every
    shed — including the head shedding, which pays for its own marker row."""
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    from ouroboros import delegate_progress as progress

    limit = tool_result_limit("delegate_wait")
    advances, labels = 601, 12
    # The daemon serves the timeline as a growing LIST, not a delta — so drive `record`
    # with that shape rather than hand-building rows, and each poll's batch is fresh.
    timeline = []
    seen = progress.WindowObservations()
    for seq in range(1, advances + 1):
        timeline.extend({"type": "tool", "title": f"advance {seq:04d} · " + "T" * 65,
                         "severity": "info"} for _ in range(labels))
        seen.record({"timeline": timeline}, seq, seq)
    assert len(seen.advances) == advances

    payload = progress.window_payload(
        run_id="run-1", state="running", last_seq=advances, window=600,
        elapsed_seconds=600, max_seconds=1800, waiting_on_user=False,
        detail={"timeline": timeline}, seen=seen, budget=limit)
    # Exactly how `_delegate_wait` renders it, which is the rendering that has to fit.
    raw = json.dumps(payload, ensure_ascii=False, indent=2)

    # THE defect: what the model is handed arrives WHOLE and parses.
    assert len(raw) <= limit, len(raw)
    delivered = _truncate_tool_result(raw, "delegate_wait", {})
    assert delivered == raw, "the generic truncator had to cut this payload"
    assert json.loads(delivered) == payload, "the model received unparseable JSON"
    rows = payload["advances"]
    # Sized against what is LEFT of the budget, not a fixed share (a share
    # bounds only itself; timeline_tail alone once overflowed the result): the
    # invariant is the WHOLE payload above, and the list keeps a real share.
    assert len(json.dumps(rows, ensure_ascii=False, indent=2)) < len(raw)

    # It shed from the HEAD and it SAYS so, with the accounting adding up: a payload that
    # pretended the window started later than it did is the one shape forbidden here.
    marker, kept = rows[0], rows[1:]
    assert kept, rows
    assert marker["advances_omitted"] == advances - len(kept), (marker, len(kept))
    assert marker["omitted_through_seq"] == kept[0]["seq"] - 1, (marker, kept[0])
    # ...and the note points at where omitted rows ACTUALLY are (the daemon's
    # run directory). The old "streamed live / in the event log" was untrue on
    # both halves - a pointer to a log that never had the rows is worse than none.
    assert "Claudexor's own timeline" in marker["note"], marker
    assert "event log" not in marker["note"], marker
    assert [row["seq"] for row in kept] == list(range(kept[0]["seq"], advances + 1))
    assert kept[-1]["seq"] == advances, "the NEWEST advance is never the one shed"


def _timeline(*titles):
    """A daemon `get_run` detail carrying exactly these timeline rows, in this order."""
    return {"timeline": [{"type": "tool", "title": title, "severity": "info"}
                         for title in titles]}


def test_a_bounded_rolling_timeline_records_the_whole_batch_that_arrived():
    """A real daemon timeline is BOUNDED: past some depth its LENGTH stops growing while
    the cursor keeps moving, so the number of new rows can no longer be read from the
    length. The batch was then taken as a SINGLE tail row, and everything else that
    arrived between two polls vanished — from the live stream the human is watching AND
    from `advances`, with nothing on the payload disclosing the loss. Silent loss of an
    advance is the one failure this whole path exists to prevent.

    Nor does the CURSOR carry the count — that was the first answer here and it was wrong
    in both directions (see the `batch=4` and cursor-overshoot shapes below). The batch is
    read off the DATA: the longest overlap between the END of the previous tail and the
    START of this one is what survived the roll. The review's own shape is first — a full
    twelve-row tail that rolls by two, which is simply two events arriving inside one poll
    interval.
    """
    from ouroboros.delegate_progress import WindowObservations

    a = [f"A{i}" for i in range(1, 13)]
    seen = WindowObservations()

    first = seen.record(_timeline(*a), 12, 0)
    assert [row["title"] for row in first.events] == a

    # THE defect: A1 and A2 fell off the front, B1 and B2 arrived, and the LENGTH is
    # unchanged. Both belong to this advance, in order.
    second = seen.record(_timeline(*a[2:], "B1", "B2"), 14, 3)
    assert [row["title"] for row in second.events] == ["B1", "B2"]

    # A three-event roll from the same rolled state — the batch is a delta, not a
    # constant, and it is read against the PREVIOUS tail rather than from zero.
    third = seen.record(_timeline(*a[5:], "B1", "B2", "B3", "B4", "B5"), 17, 6)
    assert [row["title"] for row in third.events] == ["B3", "B4", "B5"]

    # NOTHING changed on the timeline while the cursor moved (a journal entry that never
    # became a timeline row). There is no news, and inventing some would re-announce rows
    # the human was already shown.
    quiet = seen.record(_timeline(*a[5:], "B1", "B2", "B3", "B4", "B5"), 18, 7)
    assert [row["title"] for row in quiet.events] == []

    # A tail with NOTHING in common with the previous one — a replaced timeline, not a
    # roll. All of it is new; it must not raise and must not over-slice into rows that
    # are not there.
    c = [f"C{i}" for i in range(1, 13)]
    fourth = seen.record(_timeline(*c), 35, 9)
    assert [row["title"] for row in fourth.events] == c

    # Every observation is still exactly one advance, in order, whatever the batch was.
    assert [advance.seq for advance in seen.advances] == [12, 14, 17, 18, 35]
    assert [advance.at_sec for advance in seen.advances] == [0, 3, 6, 7, 9]


def test_a_daemon_that_adds_more_rows_than_cursor_steps_loses_none_of_them():
    """The cursor is not a row count, and this repo's own `_StreamingStub(batch=4)` is the
    proof: it publishes FOUR timeline rows for every single `lastSeq` step, which is what a
    harness whose journal entry carries several session events looks like. Reading the
    batch off the cursor took one row per step and three of every four vanished — end to
    end, a daemon that produced E1..E16 delivered E1..E12 and E16, with E13/E14/E15 gone
    from the live stream and from `advances`, and no `events_omitted` anywhere saying so.
    """
    from ouroboros.delegate_progress import WindowObservations

    rows = [f"E{i}" for i in range(1, 17)]
    seen, recorded = WindowObservations(), []
    for step in range(4):
        end = (step + 1) * 4                      # four rows per single cursor step
        window = rows[max(0, end - 12):end]       # ...through a twelve-row rolling tail
        recorded.extend(row["title"] for row in seen.record(_timeline(*window), step + 1, step).events)

    assert recorded == rows, "a row the daemon published never reached the record"
    assert [advance.seq for advance in seen.advances] == [1, 2, 3, 4]


def test_a_growing_timeline_still_records_exactly_the_rows_that_are_new():
    """The control the rolling shapes above must not cost: while the list is still
    GROWING, the tail comparison has to land on the plain append, even when the cursor
    disagrees. A daemon whose `seq` counts more than the timeline shows — journal
    entries that never become timeline rows — must not inflate the batch beyond the
    rows that actually arrived, and must not re-report rows already recorded."""
    from ouroboros.delegate_progress import WindowObservations

    a = [f"A{i}" for i in range(1, 13)]
    seen = WindowObservations()
    assert [row["title"] for row in seen.record(_timeline(*a), 12, 0).events] == a

    # Three rows appended; the cursor jumped far further than three.
    grown = seen.record(_timeline(*a, "B1", "B2", "B3"), 99, 3)
    assert [row["title"] for row in grown.events] == ["B1", "B2", "B3"]

    # The same overshoot against a ROLLED tail, where the length cannot help either: one
    # row arrived, the cursor moved by three. Reading the batch off the cursor took the
    # last three rows and re-emitted A11 and A12 — rows this window had already reported
    # — as new session events.
    seen = WindowObservations()
    seen.record(_timeline(*a), 12, 0)
    rolled = seen.record(_timeline(*a[1:], "B1"), 15, 3)
    assert [row["title"] for row in rolled.events] == ["B1"]


def test_the_standing_tail_is_adopted_as_history_only_for_a_caught_up_caller():
    """A wait that attaches to a run which has been talking for a while must not announce
    the whole standing tail as this window's news — to the human's live stream or to the
    model. But `since_seq` is the CALLER's cursor, and a caller BEHIND the daemon is
    asking for exactly those standing rows; adopting them there would drop the very batch
    it called for. Rows carry no cursor of their own, so it is all or nothing, and only
    the caught-up caller can be told nothing."""
    from ouroboros.delegate_progress import WindowObservations

    a = [f"A{i}" for i in range(1, 13)]
    detail = dict(_timeline(*a), lastSeq=12)

    caught_up = WindowObservations()
    caught_up.observe_baseline(detail, 12)
    assert [row["title"] for row in caught_up.record(dict(_timeline(*a, "B1"), lastSeq=13),
                                                     13, 1).events] == ["B1"]

    behind = WindowObservations()
    behind.observe_baseline(detail, 4)
    assert [row["title"] for row in behind.record(detail, 12, 1).events] == a


def test_a_sweep_with_no_transport_dials_nothing_and_settles_nothing(tmp_path, monkeypatch):
    """Seed 0: the owned daemon's ensure path retired with Claudexor, so the default
    transport is GONE — there is no gateway factory left to build. What the sweep owes
    then is honest absence, not a guess: it must reconcile nothing, dial nothing (never
    the retired ensure path), and leave the open row open for the reads that still serve
    it (the cursor refresh and the boot backfill), rather than reporting a settlement no
    transport produced."""
    from ouroboros import delegate_custody as dc

    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-orphan", task_id="t-gone", route_id="r", model="m",
        ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    ensured = []
    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway",
                        lambda *a, **k: ensured.append(True))

    assert dc.reconcile_orphaned_runs(tmp_path, set()) == []
    assert not ensured, "the retired ensure path must never be dialed"
    assert [c.run_id for c in dc.open_runs(tmp_path)] == ["run-orphan"], \
        "no transport is no verdict: the row stays open for the reads that remain"
    dc._CUSTODY.clear()

    # And with NOTHING to reconcile the early return comes before any transport question.
    empty = tmp_path / "empty-drive"
    empty.mkdir()
    assert dc.reconcile_orphaned_runs(empty, set()) == []
    assert not ensured


def test_bounded_poll_retries_the_git_atomic_object_race_once():
    """The CI gate learned to tolerate the engine's transient Git atomic-object
    ENOENT (938094a9) while the production poll kept propagating it — so CI could
    pass on an engine whose live delegate_wait still failed. One immediate re-read,
    only for exactly that shape, only while the window has time left."""
    from ouroboros.delegate_progress import bounded_poll, is_transient_git_object_race

    class _RaceOnce:
        def __init__(self):
            self.calls = 0
        def get_run(self, run_id, timeout_sec=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "ENOENT: no such file or directory, open "
                    "'/x/.git/objects/ab/tmp_obj_h4x'")
            return {"state": "running"}

    gw = _RaceOnce()
    assert bounded_poll(gw, "run-1", 60.0) == {"state": "running"}
    assert gw.calls == 2

    # A spent window does not retry (the expiring poll owns that path), and any
    # OTHER failure propagates untouched on the first read.
    gw2 = _RaceOnce()
    try:
        bounded_poll(gw2, "run-1", 0.0)
        raised = False
    except RuntimeError:
        raised = True
    assert raised and gw2.calls == 1

    class _RealFailure:
        def get_run(self, run_id, timeout_sec=None):
            raise RuntimeError("ENOENT: no such file or directory, open '/x/data/config.json'")

    try:
        bounded_poll(_RealFailure(), "run-1", 60.0)
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert not is_transient_git_object_race(RuntimeError("connection refused"))


def test_executor_resolution_row_also_lands_in_canonical_events(tmp_path):
    """W3 adjacent (c): a delegated child's forked drive is pruned with the task,
    so the subagent_executor_resolved row must ALSO land in the canonical
    events.jsonl (the accounting root the task already carries). The root
    agent's own drive IS canonical — no duplicate row there."""
    import json
    from types import SimpleNamespace

    from ouroboros.agent import _record_executor_resolution

    child_logs = tmp_path / "child_drive" / "logs"
    canonical = tmp_path / "data"
    child_logs.mkdir(parents=True)
    (canonical / "logs").mkdir(parents=True)

    dispatch = SimpleNamespace(executor_resolution=SimpleNamespace(
        requested="auto", executor="native",
        reason=SUBSCRIPTION_WINDOW_EXHAUSTED, reset_at="2030-01-01T00:00:00Z", route=None,
    ))
    task = {"id": "child1", "budget_drive_root": str(canonical)}
    _record_executor_resolution(child_logs, task, dispatch)

    def _rows(path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    child_rows = _rows(child_logs / "events.jsonl")
    canon_rows = _rows(canonical / "logs" / "events.jsonl")
    assert len(child_rows) == 1 and len(canon_rows) == 1
    assert canon_rows[0]["type"] == "subagent_executor_resolved"
    assert canon_rows[0]["reason"] == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert canon_rows[0]["reset_at"] == "2030-01-01T00:00:00Z"

    # Same drive (the root agent): exactly one row, no self-duplicate.
    root_task = {"id": "root1", "budget_drive_root": str(canonical)}
    _record_executor_resolution(canonical / "logs", root_task, dispatch)
    canon_rows = _rows(canonical / "logs" / "events.jsonl")
    assert len([r for r in canon_rows if r["task_id"] == "root1"]) == 1


def test_subscription_window_exhausted_beacon_wakes_the_waiting_parent(tmp_path, monkeypatch):
    """W3 adjacent (c): the D28 spent-window resolution appends a typed ADVISORY
    delegation_constraint to the task-tree ledger (reset_at + child id), riding
    the attention channel the wait tools already early-wake on — and the
    enforcement reducer skips it (advisory = disclosure, not a gate)."""
    from types import SimpleNamespace

    from ouroboros import task_tree_ledger as ledger_mod
    from ouroboros.agent import _record_executor_resolution
    from ouroboros.tools.control_delegation import effective_delegation_budget

    monkeypatch.setattr(ledger_mod, "DATA_DIR", tmp_path)
    child_logs = tmp_path / "child_drive" / "logs"
    child_logs.mkdir(parents=True)

    dispatch = SimpleNamespace(executor_resolution=SimpleNamespace(
        requested="auto", executor="native",
        reason=SUBSCRIPTION_WINDOW_EXHAUSTED, reset_at="2030-01-01T00:00:00Z", route=None,
    ))
    task = {"id": "childbeacon1", "parent_task_id": "parentroot1", "root_task_id": "parentroot1"}
    _record_executor_resolution(child_logs, task, dispatch)

    beacons = ledger_mod.tree_ledger_attention_after("parentroot1", "")
    assert len(beacons) == 1
    row = beacons[0]
    assert row["kind"] == "delegation_constraint"
    assert row["needs_parent_attention"] is True
    payload = row["payload"]
    assert payload["advisory"] is True
    assert payload["reset_at"] == "2030-01-01T00:00:00Z"
    assert payload["child_task_id"] == "childbeacon1"
    assert payload["reason"] == SUBSCRIPTION_WINDOW_EXHAUSTED

    # Advisory: the schedule-time enforcement reducer must NOT gate on it.
    decision = effective_delegation_budget(
        {}, missing_capabilities=[],
        unresolved_constraints=ledger_mod.open_delegation_constraints("parentroot1"),
        write_surface="", role="researcher", requested_lane="", intended_lane="light",
        active_child_count=0,
    )
    assert decision.ok

    # A healthy (non-exhausted) resolution appends NO beacon.
    healthy = SimpleNamespace(executor_resolution=SimpleNamespace(
        requested="auto", executor="harness", reason="harness_ready", reset_at="", route=None,
    ))
    _record_executor_resolution(child_logs, {"id": "childbeacon2", "parent_task_id": "parentroot1",
                                             "root_task_id": "parentroot1"}, healthy)
    assert len(ledger_mod.tree_ledger_attention_after("parentroot1", "")) == 1


def test_shared_project_retirement_defers_quietly_for_non_canonical_sharers(tmp_path):
    """Any unsettled sibling defers every sharer QUIETLY; once all settle
    the LOWEST-run_id sharer carries the retry lane."""
    import json as _json

    import ouroboros.delegate_custody as dc
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    class _RefusingGateway:
        def __init__(self):
            self.removals = []
            self.refuse = True

        def remove_project(self, pid):
            self.removals.append(pid)
            if self.refuse:
                raise ClaudexorUnavailable("project_busy", "project has live runs", status_code=409)

    gateway = _RefusingGateway()
    for rid, tid in (("run-aa", "t-1"), ("run-bb", "t-2")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=rid, task_id=tid, route_id="r", model="m",
            project_id="prj-shared", project_owned=True, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    # Non-canonical sharer: quiet deferral.
    custody_b = dc.replay(tmp_path)["run-bb"]
    dc.retire_project(tmp_path, gateway, custody_b)
    assert gateway.removals == []
    assert "delegate_run_project_retire_failed" not in _event_types(tmp_path)
    assert custody_b.project_owned is True

    # Canonical too defers while a sibling is unsettled.
    custody_a = dc.replay(tmp_path)["run-aa"]
    dc.retire_project(tmp_path, gateway, custody_a)
    assert gateway.removals == []
    # Sibling settles: canonical attempts; refusal is typed.
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-bb", "task_id": "t-2", "route": "r"})
    dc._CUSTODY.clear()
    custody_a = dc.replay(tmp_path)["run-aa"]
    dc.retire_project(tmp_path, gateway, custody_a)
    assert gateway.removals == ["prj-shared"]
    rows = [
        _json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [r for r in rows if r.get("type") == "delegate_run_project_retire_failed"]
    assert len(failed) == 1
    assert "live runs" in str(failed[0].get("reason"))

    # Once the daemon accepts, the canonical sharer discharges the registration.
    gateway.refuse = False
    dc.retire_project(tmp_path, gateway, custody_a)
    assert custody_a.project_owned is False
    assert "delegate_run_project_retired" in _event_types(tmp_path)
    dc._CUSTODY.clear()


# ---------------------------------------------------------------------------
# BR1-2: the delegate split has no import cycle — one-way seams only
# ---------------------------------------------------------------------------


def test_delegate_split_modules_import_standalone_without_the_facade():
    """The module split's seam pattern is ONE-WAY: an extracted module never
    imports the facade back. `delegate_interactions` used to import `_fail` /
    `_emit` / `_owned_run` from `ouroboros.tools.delegate` — a cycle with the
    facade's own top-level import of the cluster. Each extracted module must
    import standalone in a FRESH interpreter, and none of them may pull the
    facade into sys.modules as a side effect."""
    import subprocess
    import sys

    for module in ("ouroboros.delegate_shared",
                   "ouroboros.delegate_interactions",
                   "ouroboros.delegate_output",
                   "ouroboros.delegate_progress",
                   "ouroboros.delegate_containment",
                   "ouroboros.delegate_custody"):
        probe = (
            f"import sys; import {module}; "
            "assert 'ouroboros.tools.delegate' not in sys.modules, "
            f"'{module} pulled the facade back in'"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, (
            f"{module} failed to import standalone: {result.stderr}")


def test_facade_reexports_are_the_same_objects_as_their_owners():
    """Monkeypatch targets keep working only when the facade re-export IS the
    owner's object — probe identity, not just importability."""
    from ouroboros import delegate_shared
    from ouroboros.tools import delegate

    assert delegate._fail is delegate_shared._fail
    assert delegate._emit is delegate_shared._emit
    assert delegate._owned_run is delegate_shared._owned_run
