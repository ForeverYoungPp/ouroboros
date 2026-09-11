"""AC18 — the conflict loop uses Engram's endpoints and stays conservative.

C4's rule is asymmetric on purpose. Recording a verdict is cheap and reversible;
*acting* on one is neither. So the policy lands exactly one verdict unattended
(``not_conflict``, which asserts nothing about either record and suppresses future
candidate scans) and surfaces every other finding for the owner.

The assertions below check the absence of destructive behaviour as carefully as the
presence of the loop: an implementation that resolved conflicts correctly but
deleted the loser would satisfy "the conflict was handled" while violating P1.
"""

from __future__ import annotations

from ouroboros.engram_conflicts import (
    DESTRUCTIVE_RELATIONS,
    LANDABLE_RELATIONS,
    adjudicate_pair,
    conflict_summary,
    decide_verdict,
    persist_verdict,
)
from ouroboros.engram_read import client_for
from ouroboros.engram_sink import reset_sinks


def _writes(state, path: str) -> list[dict]:
    return [r for r in state.requests if r["path"] == path]


def _seeded_pair(state, *, a: int = 1, b: int = 2) -> None:
    from tests.test_engram_instruments import _seed_knowledge

    _seed_knowledge(state, f"memory-{a}", "first memory", obs_id=a)
    _seed_knowledge(state, f"memory-{b}", "second memory", obs_id=b)


# --------------------------------------------------------------------------- #
# AC18(a) — the verdict is persisted through Engram's own endpoint
# --------------------------------------------------------------------------- #


def test_not_conflict_is_recorded_through_the_native_endpoint(engram_stub):
    state, env = engram_stub
    receipt = persist_verdict(
        client_for(env),
        memory_id_a=11,
        memory_id_b=22,
        relation="not_conflict",
        reasoning="one is about auth, the other about caching",
        confidence=0.9,
    )

    assert receipt.ok and receipt.persisted and not receipt.requires_owner
    calls = _writes(state, "/conflicts/compare")
    assert len(calls) == 1
    body = calls[0]["body"]
    assert body["memory_id_a"] == 11 and body["memory_id_b"] == 22
    assert body["relation"] == "not_conflict"
    assert body["confidence"] == 0.9
    # The server requires a non-empty reasoning; the policy must never send an empty one.
    assert str(body["reasoning"]).strip()
    reset_sinks()


def test_the_policy_never_invents_a_comparison(engram_stub):
    """C4: Engram HAS a conflict loop, so there is nothing to re-implement here."""
    state, env = engram_stub
    persist_verdict(client_for(env), memory_id_a=1, memory_id_b=2, relation="not_conflict")

    # No client-side similarity/FTS probing, no reads at all: one write, nothing else.
    assert [r["path"] for r in state.requests] == ["/conflicts/compare"]
    reset_sinks()


# --------------------------------------------------------------------------- #
# AC18(b) — both sides of a conflict survive
# --------------------------------------------------------------------------- #


def test_a_destructive_verdict_is_never_persisted_unattended(engram_stub):
    state, env = engram_stub
    for relation in sorted(DESTRUCTIVE_RELATIONS):
        receipt = persist_verdict(
            client_for(env), memory_id_a=1, memory_id_b=2, relation=relation
        )
        assert receipt.persisted is False
        assert receipt.requires_owner is True
        assert "P1" in receipt.reason or "unattended" in receipt.reason

    assert _writes(state, "/conflicts/compare") == []
    reset_sinks()


def test_the_policy_has_no_delete_path_at_all(engram_stub):
    """The strongest form of 'keep both': there is no code that could remove one."""
    import inspect

    from ouroboros import engram_conflicts

    source = inspect.getsource(engram_conflicts)
    assert ".delete(" not in source
    assert "supersedes" in source  # named, only to refuse it
    assert "DESTRUCTIVE_RELATIONS" in source

    # ...and no verdict it can produce ever reaches the wire as `supersedes`.
    state, env = engram_stub
    for relation in ("not_conflict", "related", "compatible", "scoped", "conflicts_with",
                     "supersedes", "nonsense", ""):
        persist_verdict(client_for(env), memory_id_a=1, memory_id_b=2, relation=relation)
    sent = [r["body"]["relation"] for r in _writes(state, "/conflicts/compare")]
    assert sent == ["not_conflict"], sent
    assert not _writes(state, "/observations")
    reset_sinks()


def test_no_relation_is_marked_superseded(engram_stub):
    """Assert on the wire: nothing we send could mark either memory superseded."""
    state, env = engram_stub
    for relation in ("not_conflict", "conflicts_with", "supersedes", "related"):
        persist_verdict(client_for(env), memory_id_a=1, memory_id_b=2, relation=relation)

    blob = str(state.requests).lower()
    assert "superseded" not in blob
    assert "delete" not in blob
    reset_sinks()


# --------------------------------------------------------------------------- #
# AC18(c) — only not_conflict lands, and it suppresses later scans
# --------------------------------------------------------------------------- #


def test_only_not_conflict_lands_directly():
    landable = {verb for verb in
                ("related", "compatible", "scoped", "conflicts_with", "supersedes",
                 "not_conflict", "nonsense", "")
                if decide_verdict(verb).persist}
    assert landable == set(LANDABLE_RELATIONS) == {"not_conflict"}


def test_every_refused_verdict_asks_for_a_human():
    for verb in ("related", "compatible", "scoped", "conflicts_with", "supersedes", ""):
        decision = decide_verdict(verb)
        assert decision.persist is False
        assert decision.requires_owner is True
        assert decision.reason


def test_a_recorded_not_conflict_carries_the_relation_id(engram_stub):
    """The relation id is what suppresses future candidate scans — so its absence fails."""
    state, env = engram_stub
    good = persist_verdict(
        client_for(env), memory_id_a=1, memory_id_b=2, relation="not_conflict"
    )
    assert good.ok and good.persisted and good.sync_id

    # A store that accepts the write but returns no relation id has NOT suppressed
    # the next scan, and the caller must not be told that it did.
    state.compare_returns_sync = False
    hollow = persist_verdict(
        client_for(env), memory_id_a=3, memory_id_b=4, relation="not_conflict"
    )
    assert hollow.ok is False and hollow.persisted is False
    assert hollow.reason == "no_relation_id"
    reset_sinks()


# --------------------------------------------------------------------------- #
# The read half, and the shapes Engram actually returns
# --------------------------------------------------------------------------- #


def test_a_pair_is_accepted_in_either_id_shape(engram_stub):
    state, env = engram_stub
    client = client_for(env)

    first = adjudicate_pair(client, {"memory_id_a": 1, "memory_id_b": 2},
                            relation="not_conflict")
    second = adjudicate_pair(client, {"source_id": "3", "target_id": "4"},
                             relation="not_conflict")
    bad = adjudicate_pair(client, {"who": "knows"}, relation="not_conflict")

    assert first.persisted and second.persisted
    assert bad.ok is False and bad.reason == "unusable_pair"
    sent = [r["body"] for r in _writes(state, "/conflicts/compare")]
    assert (sent[0]["memory_id_a"], sent[0]["memory_id_b"]) == (1, 2)
    assert (sent[1]["memory_id_a"], sent[1]["memory_id_b"]) == (3, 4)
    reset_sinks()


def test_an_unreachable_store_is_typed_not_empty(engram_stub):
    state, env = engram_stub
    state.fail = True

    summary = conflict_summary(client_for(env))

    assert summary["status"] == "unavailable"
    assert summary["count"] == 0
    assert summary["detail"]


def test_the_summary_reads_recorded_relations(engram_stub):
    """A verdict nobody can read back is a log line, not a memory."""
    state, env = engram_stub
    state.relations = [
        {"id": 1, "relation": "not_conflict", "judgment_status": "judged", "reason": "different topics"},
        {"id": 2, "relation": "conflicts_with", "judgment_status": "judged", "reason": "contradiction"},
    ]

    summary = conflict_summary(client_for(env), limit=20)

    assert summary["status"] == "ok" and summary["count"] == 2
    assert [row["relation"] for row in summary["rows"]] == ["not_conflict", "conflicts_with"]
    assert int(state.requests[-1]["params"]["limit"]) == 20
    assert state.requests[-1]["params"]["project"] == "repo"
