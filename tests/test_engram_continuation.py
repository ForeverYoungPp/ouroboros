"""S4 — the predecessor's authored account survives outside the task tree.

``continuation_narrative`` is the one local record in this phase that is *not*
just a blob: it is node-associated (``summary_id``, ``result_ref``,
``source_coverage``) and the authority projection consumes that structure, so the
local write is retained. What Engram adds is durability: the same authored account
exists as one episodic-memory record per task, so a predecessor's own words
survive losing the local drive.

The read is therefore a *fallback*, and the interesting assertions are about what
happens when it also misses. "No narrative was ever authored" and "the store that
would hold it could not be read" must not come back as the same answer — that
collapse is how a memory-loss signal turns into noise (C15).
"""

from __future__ import annotations

import copy

from ouroboros.engram_read import client_for, continuation_narrative, knowledge_topic
from ouroboros.engram_sink import reset_sinks
from ouroboros.main_context_authority import project_main_task_authority


def _seed_narrative(state, task_id: str, text: str, *, obs_id: int = 700) -> None:
    state.knowledge[obs_id] = {
        "id": obs_id,
        "type": "episodic_memory",
        "title": f"Task narrative {task_id}",
        "topic_key": f"continuation:{task_id}",
        "scope": "project",
        "content": text,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
    }


def _authority(task_id: str) -> dict:
    ref = {"kind": "task_result", "task_id": task_id, "reader": "get_task_result"}
    return {
        "task_id": task_id,
        "source": {**ref, "arguments": {"task_id": task_id, "include_authority": True}},
        "result": "R" * 200_001,
        "task_contract": {"objective": "old"},
    }


def _project(task_id: str, env, **extra) -> dict:
    node = {"id": "next", "predecessor_authority": {**_authority(task_id), **extra}}
    return project_main_task_authority(node, drive_root=env.drive_root)["predecessor_authority"]


# --------------------------------------------------------------------------- #
# The read primitive
# --------------------------------------------------------------------------- #


def test_the_narrative_is_found_by_the_identity_the_sink_writes(engram_stub):
    state, env = engram_stub
    _seed_narrative(state, "task-abc", "The authored account of what happened.")

    read = continuation_narrative(client_for(env), "task-abc")

    assert read.ok and read.text == "The authored account of what happened."
    search = next(r for r in state.requests if r["path"] == "/search")
    assert search["params"]["type"] == "episodic_memory"
    assert search["params"]["project"] == "repo"
    assert search["params"]["q"] == "task-abc"


def test_a_task_id_merely_mentioned_elsewhere_is_not_the_narrative(engram_stub):
    """Matching on ranking alone would fabricate a predecessor's own account."""
    state, env = engram_stub
    state.knowledge[1] = {
        "id": 1,
        "type": "memory_action",
        "title": "a note that mentions task-abc",
        "topic_key": "knowledge:unrelated",
        "content": "while working on task-abc I learned something",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
    }

    read = continuation_narrative(client_for(env), "task-abc")

    assert read.readable and read.status == "empty"
    assert "no 'continuation:task-abc' record" in read.detail


def test_one_round_trip_when_the_hit_carries_the_body(engram_stub):
    """The server's search shape includes content, so no second fetch is needed."""
    state, env = engram_stub
    state.search_returns_content = True
    _seed_narrative(state, "task-one", "body included in the hit")

    read = continuation_narrative(client_for(env), "task-one")

    assert read.ok and read.text == "body included in the hit"
    assert [r["path"] for r in state.requests if r["path"] not in ("/project/current", "/sessions")] == ["/search"]


def test_a_body_less_hit_still_resolves(engram_stub):
    """A preview-style response must degrade to a second fetch, not to 'absent'."""
    state, env = engram_stub
    state.search_returns_content = False
    _seed_narrative(state, "task-two", "body fetched separately")

    read = continuation_narrative(client_for(env), "task-two")

    assert read.ok and read.text == "body fetched separately"
    assert [r["path"] for r in state.requests if r["path"] not in ("/project/current", "/sessions")] == ["/search", "/observations/700"]


def test_the_narrative_read_is_bounded_and_typed(engram_stub):
    from ouroboros.engram_read import MAX_TOPIC_CHARS

    state, env = engram_stub
    state.search_returns_content = True
    _seed_narrative(state, "task-big", "x" * 30_000)
    assert len(continuation_narrative(client_for(env), "task-big").text) <= MAX_TOPIC_CHARS

    state.fail = True
    down = continuation_narrative(client_for(env), "task-two")
    assert not down.readable and down.status == "unavailable"


def test_knowledge_topic_also_uses_a_single_round_trip_when_it_can(engram_stub):
    """Same server fact, same saving on the knowledge read path."""
    state, env = engram_stub
    state.search_returns_content = True
    state.knowledge[9] = {
        "id": 9, "type": "knowledge", "title": "Knowledge: auth",
        "topic_key": "knowledge:auth", "content": "tokens rotate atomically",
        "created_at": "2026-01-01", "updated_at": "2026-01-02",
    }

    read = knowledge_topic(client_for(env), "auth")

    assert read.ok and read.text == "tokens rotate atomically"
    # The bootstrap pair precedes it; the DATA read is still a single round trip.
    assert [
        r["path"] for r in state.requests
        if r["path"] not in ("/project/current", "/sessions")
    ] == ["/search"]


# --------------------------------------------------------------------------- #
# A11 — Engram is the third source, and the gap says which kind of miss it was
# --------------------------------------------------------------------------- #


def test_engram_supplies_the_narrative_when_the_local_sources_miss(engram_stub):
    state, env = engram_stub
    _seed_narrative(state, "eng-task", "Recovered from Engram after the drive was lost.")

    result = _project("eng-task", env)["result"]

    assert result["narrative_status"] == "available"
    assert result["narrative"]["text"] == "Recovered from Engram after the drive was lost."
    assert result["narrative"]["origin"] == "engram"
    # The reference a reader follows stays the one the local record would have made.
    assert result["narrative"]["summary_id"] == "task-narrative:eng-task"
    # ...and it passes the SAME validator a local narrative must pass, so a consumer
    # never has to branch on where the account came from.
    from ouroboros.project_dialogue import continuation_narrative_is_valid

    assert continuation_narrative_is_valid(result["narrative"], "eng-task")
    assert "R" * 200_001 not in str(result)
    reset_sinks()


def test_the_local_narrative_still_wins(engram_stub):
    """Engram is a fallback: the node's own copy is authoritative and free."""
    state, env = engram_stub
    _seed_narrative(state, "local-task", "the Engram copy")

    ref = {"kind": "task_result", "task_id": "local-task", "reader": "get_task_result"}
    result = _project(
        "local-task", env, continuation_narrative={
            "summary_id": "task-narrative:local-task",
            "summary_kind": "authored_root_summary",
            "task_id": "local-task",
            "result_ref": ref,
            "source_coverage": {"task_result": ref},
            "text": "the local copy",
        }
    )["result"]

    assert result["narrative"]["text"] == "the local copy"
    assert result["narrative"].get("origin") != "engram"
    assert state.requests == [], "a local hit must not spend a remote read"
    reset_sinks()


def test_everywhere_absent_is_reported_as_absent(engram_stub):
    state, env = engram_stub
    result = _project("nowhere", env)["result"]

    assert result["narrative_status"] == "unavailable"
    assert result["narrative_gap"]["reason"] == "no_exact_authored_summary"
    assert result["narrative_gap"]["memory_presence"] == "absent"
    reset_sinks()


def test_an_unreadable_store_is_not_reported_as_absent(engram_stub):
    """The whole point: 'no service' must not be filed as 'never authored'."""
    state, env = engram_stub
    state.fail = True

    result = _project("unreachable-task", env)["result"]

    assert result["narrative_status"] == "unavailable"
    assert result["narrative_gap"]["reason"] == "engram_unreadable"
    assert result["narrative_gap"]["memory_presence"] == "unknown"
    reset_sinks()


def test_the_projection_still_does_not_mutate_its_input(engram_stub):
    state, env = engram_stub
    _seed_narrative(state, "deep-task", "an Engram-sourced account")
    node = {"id": "next", "predecessor_authority": _authority("deep-task")}
    before = copy.deepcopy(node)

    project_main_task_authority(node, drive_root=env.drive_root)

    assert node == before
    reset_sinks()
