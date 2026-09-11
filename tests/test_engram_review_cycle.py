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
