"""Configured-subagent references on reviewer rows (generic-actor bridge).

A reviewer row may reference an ``OUROBOROS_SUBAGENTS`` roster row instead of
carrying an inline route. Resolution happens once at load/admission from the
APPLIED env; the resolved slot carries the actor id as identity/provenance and
the roster row's execution facts. An api_model actor is the RETRIEVES class
(bounded native tool rounds) and must never enter the assembled-packet plane:
not the pack-assembly predicate, not the D15 acceptance projection.
"""

import json

import pytest

from ouroboros.reviewer_slot_config import (
    REVIEWER_SLOTS_ENV,
    commit_triad_delivery,
    load_reviewer_slot_config,
    parse_reviewer_slots,
    project_reviewer_slots_into_env,
    structured_scope_review_slots,
)

_ROSTER = {
    "enabled": True,
    "items": [
        {
            "subagent_id": "api-critic",
            "name": "API critic",
            "recommended_use": "Exact recursive API reviewer.",
            "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"},
            "effort": "medium",
        },
        {
            "subagent_id": "session-critic",
            "name": "Session critic",
            "recommended_use": "Subscription reviewer.",
            "route": {
                "kind": "agent_session",
                "target_id": "codex=gpt-5.6-sol",
                "credential_profile_id": "profile-1",
            },
            "effort": "high",
        },
    ],
}


def _payload(triad_rows, scope_rows=None):
    return json.dumps({
        "triad": triad_rows,
        "scope": scope_rows or [
            {"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"}},
        ],
    })


@pytest.fixture()
def roster_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps(_ROSTER))
    for key in ("OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS",
                "OUROBOROS_SCOPE_REVIEW_MODEL"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_session_actor_row_resolves_from_roster(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "session-critic"}]))
    row = load_reviewer_slot_config().triad[0]
    assert row.subagent_id == "session-critic"
    assert row.is_session and row.retrieves and not row.native_retrieval
    assert row.target_id == "codex=gpt-5.6-sol"
    assert row.session_target == "codex=gpt-5.6-sol"
    assert row.profile_id == "profile-1"
    # Roster effort applies when the row has no explicit one.
    assert row.effort == "high"


def test_api_actor_row_is_native_retrieval(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))
    row = load_reviewer_slot_config().triad[0]
    assert row.subagent_id == "api-critic"
    assert row.kind == "api_chat"  # wire vocabulary stays closed
    assert row.native_retrieval and row.retrieves and not row.is_session
    assert row.target_id == "openai/gpt-5.6-terra"
    assert row.effort == "medium"


def test_explicit_row_effort_outranks_roster_effort(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic", "effort": "xhigh"}]))
    assert load_reviewer_slot_config().triad[0].effort == "xhigh"


def test_unknown_subagent_id_refuses_typed(roster_env):
    with pytest.raises(ValueError, match="unknown_subagent_id"):
        parse_reviewer_slots(_payload([{"slot_id": "t1", "subagent_id": "ghost"}]))


def test_route_and_subagent_id_are_mutually_exclusive(roster_env):
    with pytest.raises(ValueError, match="either route or subagent_id"):
        parse_reviewer_slots(_payload([{
            "slot_id": "t1", "subagent_id": "api-critic",
            "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"},
        }]))


def test_empty_subagent_id_refuses(roster_env):
    with pytest.raises(ValueError, match="subagent_id"):
        parse_reviewer_slots(_payload([{"slot_id": "t1", "subagent_id": "  "}]))


def test_actor_rows_never_project_into_api_only_acceptance(roster_env):
    """D15: only DIRECT api_chat rows feed the legacy comma-key projection."""
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload([
        {"slot_id": "t1", "subagent_id": "api-critic"},
        {"slot_id": "t2", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.5"}},
    ]))
    project_reviewer_slots_into_env()
    assert roster_env is not None
    import os

    assert os.environ["OUROBOROS_REVIEW_MODELS"] == "openai/gpt-5.5"


def test_actor_only_triad_discloses_acceptance_fallback(roster_env):
    """A triad of actors and sessions leaves API-only acceptance on defaults."""
    from ouroboros.reviewer_slot_config import api_fallback_disclosure

    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload([
        {"slot_id": "t1", "subagent_id": "api-critic"},
        {"slot_id": "t2", "subagent_id": "session-critic"},
    ]))
    disclosure = api_fallback_disclosure(load_reviewer_slot_config())
    assert "triad" in disclosure


def test_commit_triad_delivery_carries_actor_vector(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload([
        {"slot_id": "t1", "subagent_id": "api-critic"},
        {"slot_id": "t2", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.5"}},
    ]))
    plan = commit_triad_delivery()
    assert plan["subagent_ids"] == ["api-critic", ""]


def test_scope_actor_slot_reaches_review_slot(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.5"}}],
        scope_rows=[{"slot_id": "s1", "subagent_id": "api-critic"}],
    ))
    slots = structured_scope_review_slots()
    assert slots is not None and len(slots) == 1
    assert slots[0].subagent_id == "api-critic"
    assert slots[0].native_retrieval and slots[0].retrieves


def test_actor_binding_is_attempt_identity(roster_env):
    """A changed actor reference mints a new custody attempt key (#285 class)."""
    from types import SimpleNamespace

    from ouroboros.review_custody import _attempt_key
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot

    request = SimpleNamespace(retry_key="", slot_messages={}, surface="multi_model_review",
                              task_id="t", call_type="multi_model_review")
    base = dict(slot_id="t1", model="openai/gpt-5.6-terra", effort="medium",
                route=ReviewRouteKind.API_CHAT)
    a = ReviewSlot(subagent_id="api-critic", **base)
    b = ReviewSlot(subagent_id="", **base)
    assert _attempt_key(request, a) != _attempt_key(request, b)


def test_roster_edit_changes_next_load_only(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))
    before = load_reviewer_slot_config().triad[0]
    mutated = json.loads(json.dumps(_ROSTER))
    mutated["items"][0]["route"]["target_id"] = "openai/gpt-5.5"
    roster_env.setenv("OUROBOROS_SUBAGENTS", json.dumps(mutated))
    after = load_reviewer_slot_config().triad[0]
    assert before.target_id == "openai/gpt-5.6-terra"  # frozen materialization
    assert after.target_id == "openai/gpt-5.5"  # next load sees the edit


def test_endpoint_round_trips_the_actor_reference(roster_env):
    """GET /api/reviewer-slots returns an actor row as its subagent_id
    REFERENCE (resolved route only as read-only disclosure) — else the next
    UI save rewrites the reference into an inline route and the roster row
    stops being the SSOT for that reviewer."""
    import asyncio

    from starlette.requests import Request

    from ouroboros.gateway.settings import api_reviewer_slots

    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))
    request = Request({"type": "http", "method": "GET", "path": "/api/reviewer-slots",
                       "headers": [], "query_string": b""})
    body = json.loads(asyncio.run(api_reviewer_slots(request)).body)
    row = body["triad"][0]
    assert row["subagent_id"] == "api-critic"
    assert "route" not in row  # the reference IS the stored form
    assert row["resolved_route"]["kind"] == "api_chat"
    assert row["resolved_route"]["target_id"] == "openai/gpt-5.6-terra"
