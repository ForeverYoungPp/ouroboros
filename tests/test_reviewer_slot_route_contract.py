"""Exact structured reviewer-route and effort-authority regressions."""

import json

import pytest

from ouroboros.reviewer_slot_config import (
    REVIEWER_SLOTS_ENV,
    commit_triad_delivery,
    load_reviewer_slot_config,
    parse_reviewer_slots,
    structured_scope_review_slots,
)


def _payload() -> dict:
    return {
        "triad": [
            {
                "slot_id": "triad-route",
                "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol"},
            },
        ],
        "scope": [
            {
                "slot_id": "scope-route",
                "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-sol"},
            },
        ],
        "advisory": {"enabled": True, "route": {"kind": "api", "target_id": ""}},
    }


@pytest.mark.parametrize("target", ["off", "OFF", "=malformed", ":high"])
@pytest.mark.parametrize("surface", ["triad", "scope", "advisory"])
def test_structured_session_target_must_name_a_concrete_harness(target, surface):
    payload = _payload()
    if surface == "advisory":
        payload["advisory"] = {
            "enabled": True,
            "route": {"kind": "agent_session", "target_id": target},
        }
    else:
        payload[surface][0]["route"] = {
            "kind": "agent_session",
            "target_id": target,
        }

    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        parse_reviewer_slots(json.dumps(payload))


def test_disabled_advisory_allows_empty_session_but_not_persisted_junk():
    payload = _payload()
    payload["advisory"] = {
        "enabled": False,
        "route": {"kind": "agent_session", "target_id": ""},
    }
    advisory = parse_reviewer_slots(json.dumps(payload)).advisory
    assert advisory.enabled is False and advisory.target_id == ""

    payload["advisory"]["route"]["target_id"] = "off"
    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        parse_reviewer_slots(json.dumps(payload))


def test_settings_save_refuses_unparseable_session_target_before_persistence():
    from starlette.requests import Request

    from ouroboros.gateway.settings import _api_settings_post_locked

    payload = _payload()
    payload["triad"][0]["route"]["target_id"] = "=malformed"
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/settings",
        "headers": [],
        "query_string": b"",
    })
    response = _api_settings_post_locked(
        request,
        {REVIEWER_SLOTS_ENV: json.dumps(payload)},
    )
    body = json.loads(response.body)
    assert response.status_code == 400
    assert body["saved"] is False
    assert "does not name a concrete harness route" in body["error"]


def test_malformed_advisory_target_never_consults_the_shared_route(monkeypatch):
    from ouroboros.tools import claude_advisory_review as advisory

    payload = _payload()
    payload["advisory"] = {
        "enabled": True,
        "route": {"kind": "agent_session", "target_id": "off"},
    }
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    monkeypatch.setenv("OUROBOROS_REVIEW_SESSION_ROUTE", "codex=gpt-5.6-sol:high")

    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        advisory.advisory_gate_unavailability_reason()


def test_compound_session_effort_precedes_surface_defaults(monkeypatch):
    from ouroboros.tools import plan_review_runtime

    payload = _payload()
    payload["triad"] = [
        {
            "slot_id": "cursor-row",
            "route": {
                "kind": "agent_session",
                "target_id": "cursor=cursor-grok-4.6-xhigh-fast",
            },
        },
        {
            "slot_id": "plain-row",
            "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol"},
        },
    ]
    payload["scope"] = [
        {
            "slot_id": "agy-row",
            "route": {
                "kind": "agent_session",
                "target_id": "agy=gemini-3.1-pro-max-fast",
            },
        },
    ]
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "low")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "medium")

    config = load_reviewer_slot_config()
    assert [row.effort for row in config.triad] == ["", ""]
    assert commit_triad_delivery()["efforts"] == ["xhigh", "low"]
    assert [slot.effort for slot in structured_scope_review_slots()] == ["max"]
    assert [slot.effort for slot in plan_review_runtime.plan_review_slots()] == [
        "xhigh",
        plan_review_runtime.PLAN_REVIEW_EFFORT,
    ]


def test_compound_effort_stabilizes_replay_identity_against_global_drift(monkeypatch):
    from ouroboros.skill_review_cycles import skill_review_contract_fingerprint

    payload = _payload()
    payload["triad"][0]["route"]["target_id"] = "cursor=cursor-grok-4.6-xhigh"
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "low")
    first = commit_triad_delivery()
    first_fp = skill_review_contract_fingerprint(
        first["models"], required_items=("manifest_schema",), delivery=first,
    )

    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    same = commit_triad_delivery()
    same_fp = skill_review_contract_fingerprint(
        same["models"], required_items=("manifest_schema",), delivery=same,
    )
    assert first["efforts"] == same["efforts"] == ["xhigh"]
    assert first_fp == same_fp

    payload["triad"][0]["route"]["target_id"] = "cursor=cursor-grok-4.6-max"
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    changed = commit_triad_delivery()
    changed_fp = skill_review_contract_fingerprint(
        changed["models"], required_items=("manifest_schema",), delivery=changed,
    )
    assert changed["efforts"] == ["max"]
    assert changed_fp != first_fp


def test_compound_effort_stabilizes_commit_fingerprint_against_global_drift(
    monkeypatch,
):
    from ouroboros.tools.commit_gate import commit_review_contract_fingerprint

    payload = _payload()
    payload["triad"][0]["route"]["target_id"] = "cursor=cursor-grok-4.6-xhigh"
    payload["scope"][0]["route"] = {
        "kind": "agent_session",
        "target_id": "agy=gemini-3.1-pro-max-fast",
    }
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "low")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "medium")
    first = commit_review_contract_fingerprint()

    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "low")
    assert commit_review_contract_fingerprint() == first

    payload["scope"][0]["route"]["target_id"] = "agy=gemini-3.1-pro-xhigh-fast"
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    assert commit_review_contract_fingerprint() != first
