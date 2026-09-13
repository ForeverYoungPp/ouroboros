"""AC14 / C16 — consciousness consumes Engram's native review cycle.

Consciousness used to be the one consumer with no repoint path at all: it read
its inputs straight off the drive and kept no cross-cycle state. C16 replaces
that with Engram's own ``review_after`` decay — ``GET /review`` decides what is
due, ``POST /review/mark_reviewed`` advances it — so the cycle needs no local
watermark and cannot double-consume a record across two wake-ups.

The tests below are deliberately about the *ordering* invariant as much as the
requests: reading what is due must not, by itself, mark anything. A cycle that
is paused, stopped or produces no thought never considered those memories, so
advancing the clock for them would be silent memory loss dressed as progress.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.engram_read import (
    MAX_DIGEST_CHARS,
    MAX_REVIEW_ITEMS,
    client_for,
    due_for_review,
)
from ouroboros.engram_sink import reset_sinks
from tests.test_engram_instruments import stub  # noqa: F401  (shared HTTP stub)


def _review_items(n: int) -> list[dict]:
    return [
        {
            "id": i,
            "type": "learning",
            "title": f"memory {i}",
            "created_at": "2026-01-01",
            "updated_at": f"2026-01-{i:02d}",
        }
        for i in range(1, n + 1)
    ]


def _scaffold_drive(drive) -> None:
    """The minimum a real consciousness context build reads from the drive."""
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    for name in ("chat.jsonl", "progress.jsonl", "tools.jsonl", "events.jsonl",
                 "supervisor.jsonl", "task_reflections.jsonl"):
        (drive / "logs" / name).write_text("", encoding="utf-8")
    (drive / "state").mkdir(parents=True, exist_ok=True)
    (drive / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (drive / "memory").mkdir(parents=True, exist_ok=True)
    (drive / "memory" / "identity.md").write_text("I am Ouroboros", encoding="utf-8")
    (drive / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")


@pytest.fixture()
def consciousness(stub):  # noqa: F811  (fixture name shadows the import on purpose)
    state, env = stub
    _scaffold_drive(env.drive_root)
    (env.repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (env.repo_dir / "docs" / "DEVELOPMENT.md").write_text("# Dev", encoding="utf-8")

    from ouroboros.consciousness import BackgroundConsciousness

    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        bc = BackgroundConsciousness(
            drive_root=env.drive_root,
            repo_dir=env.repo_dir,
            event_queue=None,
            owner_chat_id_fn=lambda: None,
        )
    return state, env, bc



def _cycle_read_a_body(bc):
    """The S3 precondition: the cycle's own accounting must show that a memory
    body was actually read before its due set may be advanced."""
    from ouroboros.tools.engram import _spend, turn_usage

    bc._registry._ctx.task_id = "bg-consciousness"
    _spend(bc._registry._ctx, chars=1, reads=1)
    assert turn_usage(bc._registry._ctx)["reads"] == 1


def _requests(state, path):
    return [r for r in state.requests if r["path"] == path]


def _state_files(env):
    state_dir = env.drive_root / "state"
    return sorted(p.name for p in state_dir.iterdir() if p.is_file())


# --------------------------------------------------------------------------- #
# AC14(a) — the read path is Engram's review cursor, scoped and bounded
# --------------------------------------------------------------------------- #


def test_consciousness_asks_engram_what_is_due(consciousness):
    state, env, bc = consciousness
    state.review = _review_items(3)

    text = bc._build_context(observations=[])

    asks = _requests(state, "/review")
    assert asks, "consciousness never consulted the Engram review cycle"
    assert asks[-1]["params"]["project"] == "repo"
    assert int(asks[-1]["params"]["limit"]) == MAX_REVIEW_ITEMS
    assert "## Engram review cycle (3 due)" in text
    assert "memory 1" in text


def test_the_review_read_is_bounded_like_its_predecessor(stub):  # noqa: F811
    """AC16 / C14: an explicit limit, capped, and a bounded rendering."""
    state, env = stub
    state.review = _review_items(50)

    batch = due_for_review(client_for(env), limit=10_000)

    assert int(state.requests[-1]["params"]["limit"]) == MAX_REVIEW_ITEMS
    assert len(batch.ids) <= MAX_REVIEW_ITEMS
    assert len(batch.read.text) <= MAX_DIGEST_CHARS


def test_review_read_is_project_scoped(stub):  # noqa: F811
    state, env = stub
    state.review = _review_items(1)
    due_for_review(client_for(env))
    assert state.requests[-1]["params"]["project"] == "repo"


# --------------------------------------------------------------------------- #
# AC14(b) — advancing the cycle uses Engram's endpoint
# --------------------------------------------------------------------------- #


def test_advancing_the_cycle_marks_every_reviewed_record(consciousness):
    state, env, bc = consciousness
    state.review = _review_items(2)

    bc._build_context(observations=[])
    _cycle_read_a_body(bc)
    marked = bc._advance_review_cycle()

    assert marked == 2
    posts = _requests(state, "/review/mark_reviewed")
    assert [p["body"]["observation_id"] for p in posts] == [1, 2]
    assert state.marked == [1, 2]


def test_reading_due_records_does_not_by_itself_mark_them(consciousness):
    """The ordering invariant: only a completed cycle may advance the clock."""
    state, env, bc = consciousness
    state.review = _review_items(3)

    bc._build_context(observations=[])

    assert state.marked == [], "a cycle that never finished consumed its due set"
    assert bc._review_batch_ids == (1, 2, 3), "the due set must survive for the next cycle"


def test_a_cycle_with_nothing_due_marks_nothing(consciousness):
    state, env, bc = consciousness
    bc._build_context(observations=[])
    assert bc._advance_review_cycle() == 0
    assert state.marked == []


# --------------------------------------------------------------------------- #
# AC14(c) — no double consumption across wake-ups
# --------------------------------------------------------------------------- #


def test_a_second_wakeup_does_not_reconsume_marked_records(consciousness):
    state, env, bc = consciousness
    state.review = _review_items(2)

    bc._build_context(observations=[])
    assert bc._review_batch_ids == (1, 2)
    _cycle_read_a_body(bc)
    bc._advance_review_cycle()

    # Second wake-up: Engram's decay has already moved those records out of the
    # due set, so the same memories are not worked twice.
    bc._build_context(observations=[])
    assert bc._review_batch_ids == ()
    assert "## Engram review cycle" not in bc._build_context(observations=[])


# --------------------------------------------------------------------------- #
# AC14(d) — no locally maintained watermark
# --------------------------------------------------------------------------- #


def test_no_watermark_file_is_created(consciousness):
    state, env, bc = consciousness
    state.review = _review_items(2)
    before = _state_files(env)

    bc._build_context(observations=[])
    _cycle_read_a_body(bc)
    bc._advance_review_cycle()

    after = _state_files(env)
    assert not [name for name in after if "watermark" in name.lower()]
    assert after == before, f"the review cycle wrote local state: {set(after) - set(before)}"


# --------------------------------------------------------------------------- #
# Failure shape — unreachable is not empty
# --------------------------------------------------------------------------- #


def test_unreachable_store_reads_as_unknown_not_empty(consciousness):
    """C15: 'no service' must never be rendered as 'nothing to review'."""
    state, env, bc = consciousness
    state.fail = True

    text = bc._build_context(observations=[])

    assert "UNKNOWN this cycle" in text
    assert bc._review_batch_ids == ()
    assert bc._advance_review_cycle() == 0


def test_an_empty_due_set_is_not_rendered_as_a_section(consciousness):
    state, env, bc = consciousness
    text = bc._build_context(observations=[])
    assert "## Engram review cycle" not in text


# --------------------------------------------------------------------------- #
# F7 — a bare drive root must not become its own Engram project
# --------------------------------------------------------------------------- #


def test_a_bare_drive_root_resolves_the_repo_project(stub, monkeypatch):  # noqa: F811
    """The drive is ``.../data``; the memory scope is the repository.

    ``deep_self_review`` and ``evolution_checkpoints`` legitimately hold only the
    drive root. Resolving their project from the drive's directory name would
    file one system's memories under a second Engram project and split its memory
    in half — the F7 leak arriving through the back door.
    """
    state, env = stub
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(env.repo_dir))
    reset_sinks()
    state.review = _review_items(1)

    client_for(env.drive_root)   # bare path, exactly as those callers pass it
    due_for_review(client_for(env.drive_root))

    assert state.requests[-1]["params"]["project"] == "repo"


def test_a_drive_local_engram_config_still_wins(stub, monkeypatch):  # noqa: F811
    """An operator pinning the project on the drive means it for every consumer."""
    state, env = stub
    (env.drive_root / ".engram").mkdir(parents=True, exist_ok=True)
    (env.drive_root / ".engram" / "config.json").write_text(
        '{"project_name": "pinned-on-drive"}', encoding="utf-8"
    )
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(env.repo_dir))
    reset_sinks()
    state.review = _review_items(1)

    due_for_review(client_for(env.drive_root))

    assert state.requests[-1]["params"]["project"] == "pinned-on-drive"


def test_an_unresolvable_scope_reads_as_unknown_not_empty(consciousness, monkeypatch):
    """Fail-closed reads: an unresolvable project is a config fact mapped to
    the UNKNOWN family, never a silent skip that reads as a clean backlog."""
    monkeypatch.setenv("ENGRAM_PROJECT", "local")  # forbidden name
    from ouroboros.engram_sink import reset_sinks

    reset_sinks()
    state, env, bc = consciousness
    text = bc._build_context(observations=[])
    assert "UNKNOWN this cycle" in text
    reset_sinks()


# --------------------------------------------------------------------------- #
# The body read the cycle is told to perform must actually be reachable
# --------------------------------------------------------------------------- #


def test_the_background_cycle_can_read_a_due_record(stub):  # noqa: F811
    """The cycle context tells the model to read a body with the `engram` tool
    (and the review section tells it to verify/correct/merge what is due).
    A whitelist that omits that tool left the cycle inspecting titles only —
    the refusal must not come back, and the read must reach the store."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.search_returns_content = True
    state.observations = _review_items(2)
    args = json.dumps({"op": "search", "query": "the one"})
    out = bc._execute_tool({"function": {"name": "engram", "arguments": args}}, [])

    assert "not available in background mode" not in out
    assert [r for r in state.requests if r["path"] == "/search"], out
    reset_sinks()


def test_each_cycle_starts_with_a_fresh_retrieval_budget(stub):  # noqa: F811
    """The tool's budget is keyed on (task_id, chat_id), and a background cycle
    pins BOTH for the life of the process. Without a per-cycle reset, one cycle
    that read its 3 allowed bodies would starve every later wake-up forever."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = _review_items(4)

    def read(obs_id):
        args = json.dumps({"op": "read", "observation_id": obs_id})
        return bc._execute_tool({"function": {"name": "engram", "arguments": args}}, [])

    for obs_id in (1, 2, 3):
        assert "BUDGET SPENT" not in read(obs_id)
    assert "BUDGET SPENT" in read(4)  # the cap is real within one turn

    bc._reset_retrieval_budget()  # what the next wake-up does
    assert "BUDGET SPENT" not in read(4)
    reset_sinks()


def test_the_wake_up_itself_clears_the_previous_turns_budget(stub, monkeypatch):  # noqa: F811
    """Wiring, not just the helper: the reset must run as part of the cycle.

    The cycle is stopped right at the context step (the real overflow guard) so no
    model call happens — the assertion is that a fresh turn's accounting survives
    that entry point.
    """
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = _review_items(4)
    args = json.dumps({"op": "read", "observation_id": 1})
    for _ in range(3):
        bc._execute_tool({"function": {"name": "engram", "arguments": args}}, [])

    monkeypatch.setattr(bc, "_snapshot_pending_observations", lambda: [])
    monkeypatch.setattr(
        bc, "_build_cycle_context",
        lambda observations, context_mode="": (_ for _ in ()).throw(OverflowError("stop here")),
    )
    assert bc._think_scoped() is False  # overflow guard ends the cycle

    out = bc._execute_tool({"function": {"name": "engram", "arguments": args}}, [])
    assert "BUDGET SPENT" not in out
    reset_sinks()


# --------------------------------------------------------------------------- #
# S3 — a due set may not be consumed by a cycle that never inspected it
# --------------------------------------------------------------------------- #


def test_a_cycle_that_read_no_body_does_not_consume_the_due_set(stub):  # noqa: F811
    """Listing what is due is not reviewing it: "reviewed" must not describe work
    that never happened."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = _review_items(4)
    bc._reset_retrieval_budget()
    bc._review_batch_ids = (1, 2, 3)

    assert bc._advance_review_cycle() == 0
    assert state.marked == []
    events = (env.drive_root / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "consciousness_review_unread" in events
    reset_sinks()


def test_the_unread_pushback_is_bounded_so_the_cursor_cannot_livelock(stub):  # noqa: F811
    """The witness UNDER-counts — `knowledge_read` and `chat_history` verify a
    record without spending the tool's read budget — so a permanent gate would
    re-inject the same due set into every wake-up forever. After the limit the
    cursor advances and the disclosure gets louder.

    Driven through the REAL per-cycle order: `_build_context` is what re-arms the
    batch from Engram's due set each wake-up, so the streak must survive it."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import REVIEW_UNREAD_STREAK_LIMIT, BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.review = _review_items(3)
    assert REVIEW_UNREAD_STREAK_LIMIT >= 2, "a limit below 2 is no pushback at all"

    for _ in range(REVIEW_UNREAD_STREAK_LIMIT - 1):
        bc._reset_retrieval_budget()
        bc._build_context(observations=[])
        assert bc._review_batch_ids == (1, 2, 3)
        assert bc._advance_review_cycle() == 0
    assert state.marked == [], "an unverified set was consumed before the limit"

    bc._reset_retrieval_budget()
    bc._build_context(observations=[])
    assert bc._advance_review_cycle() == 3
    assert sorted(state.marked) == [1, 2, 3]
    events = (env.drive_root / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "consciousness_review_unread_persistent" in events
    assert bc._review_unread_streak == 0, "the settled set must not tax the next one"
    reset_sinks()


def test_reading_a_body_advances_and_clears_the_unread_streak(stub):  # noqa: F811
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = _review_items(4)
    bc._reset_retrieval_budget()
    bc._review_batch_ids = (1, 2, 3)
    bc._advance_review_cycle()
    assert bc._review_unread_streak == 1

    bc._reset_retrieval_budget()
    args = json.dumps({"op": "read", "observation_id": 1})
    bc._execute_tool({"function": {"name": "engram", "arguments": args}}, [])
    bc._review_batch_ids = (1, 2, 3)

    assert bc._advance_review_cycle() == 3
    assert bc._review_unread_streak == 0
    reset_sinks()


def test_each_wake_up_retrieves_against_its_newest_observation(stub):  # noqa: F811
    """A wake-up has no owner message, so its newest observation IS the question.
    With an empty query the recall seam always took its recency branch and the
    cycle was handed "what happened lately" instead of what it is thinking about."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = [{
        "id": 1, "type": "dialogue_summary",
        "title": "Dialogue summary: zstd frames",
        "topic_key": "dialogue:summary:0-10", "content": "we agreed on zstd frames",
    }]

    text = bc._build_context(
        observations=[{"id": "o1", "payload": "the zstd frames decision needs review"}]
    )

    asks = [r for r in state.requests if r["path"] == "/search"]
    assert asks, "the recall seam never searched (it fell back to recency)"
    assert "zstd" in str(asks[-1]["params"].get("q") or "")
    assert "zstd" in text


def test_a_wake_up_query_keeps_the_newest_summaries_beside_the_hits(stub):  # noqa: F811
    """OR matching made a NON-EMPTY hit set the normal outcome of a wake-up query,
    which would let keyword overlap displace the cycle's continuity anchor — the
    newest summaries. The section carries both, under headings the reader can tell
    apart, and the two assertions here are each reachable through only one half:
    the search cannot return the newest block (its text does not contain the
    query), and the recency read cannot know what the query matched."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    payload = "the zstd frames decision needs review"
    state.observations = [
        {
            "id": 1, "type": "dialogue_summary",
            "title": "Dialogue summary: zstd frames decision",
            "topic_key": "dialogue:summary:0-10",
            "content": f"we agreed on zstd frames; {payload}",
        },
        {
            "id": 2, "type": "dialogue_summary",
            "title": "Dialogue summary: the newest span",
            "topic_key": "dialogue:summary:10-20",
            "content": "later work, nothing about compression",
        },
    ]

    text = bc._build_context(observations=[{"id": "o1", "payload": payload}])

    assert "Dialogue summary: zstd frames decision" in text  # relevance: the hit
    assert "Dialogue summary: the newest span" in text  # continuity: recency only
    continuity = text.index("### Newest remembered (continuity)")
    assert continuity < text.index("Dialogue summary: the newest span")
    reset_sinks()


def test_a_wake_up_with_no_observation_still_falls_back_to_recency(stub):  # noqa: F811
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "Dialogue summary: span",
        "topic_key": "dialogue:summary:0-10", "content": "x",
    }]

    text = bc._build_context(observations=[])

    assert [r for r in state.requests if r["path"] == "/search"] == []
    assert "Dialogue summary: span" in text
    reset_sinks()


def test_a_null_observation_payload_is_not_the_query(stub):  # noqa: F811
    """`json.dumps(None)` is the literal "null": a one-token AND search that CAN
    match, which would surface unrelated hits instead of the newest real text."""
    state, env = stub
    reset_sinks()
    from ouroboros.consciousness import BackgroundConsciousness

    bc = BackgroundConsciousness(
        drive_root=env.drive_root,
        repo_dir=env.repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )
    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "zstd frames",
        "topic_key": "dialogue:summary:0-10", "content": "we chose zstd frames",
    }]

    bc._build_context(observations=[
        {"id": "o1", "payload": "the zstd frames decision"},
        {"id": "o2", "payload": None},
    ])

    asks = [r for r in state.requests if r["path"] == "/search"]
    assert asks, "the recall seam never searched"
    assert str(asks[-1]["params"].get("q") or "") != "null"
    assert "zstd" in str(asks[-1]["params"].get("q") or "")
    reset_sinks()
