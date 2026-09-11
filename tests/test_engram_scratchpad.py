"""S3 — the agent's working memory is mirrored into Engram, with its readers repointed.

Scope note (the AC11 / S3 conflict, resolved deliberately): the ``## Scratchpad``
prompt section STAYS. AC11 is an acceptance criterion that names it explicitly, and
S3's *stop* target is the local WRITE. Stopping that write would require rendering
the section from Engram, which Engram's HTTP surface cannot support: ``GET
/observations/recent`` and its ``GET /observations`` alias accept only
project/scope/limit/sort, and ``GET /search`` requires a text query — there is no
type-filtered recency read, so "the newest N scratchpad blocks" cannot be fetched
without over-fetching the whole store or abusing ``scope``.

So this file guards the half that IS both correct and safe:

* the block is mirrored into Engram (the user's actual ask: the memory is reachable
  from Engram, not only from the local drive);
* both named consumers are repointed — A10 already probes Engram, and A4's review
  pack now states how much working memory it saw;
* the local write keeps its bounded-window + recoverable-eviction semantics, which
  is what makes working memory safe to keep injecting at all.
"""

from __future__ import annotations

import json

from ouroboros.engram_read import client_for, type_digest
from ouroboros.engram_sink import reset_sinks, sink_for
from ouroboros.memory import Memory


def _blocks(state) -> list[dict]:
    return [rec for rec in state.knowledge.values() if rec.get("type") == "scratchpad_block"]


def _journal(drive) -> list[dict]:
    path = drive / "memory" / "scratchpad_journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(env, content: str, *, source: str = "task") -> Memory:
    memory = Memory(env.drive_root, env.repo_dir)
    memory.append_scratchpad_block(content, source=source)
    return memory


# --------------------------------------------------------------------------- #
# The mirror
# --------------------------------------------------------------------------- #


def test_a_scratchpad_block_is_mirrored_into_engram(engram_stub):
    state, env = engram_stub
    _append(env, "Working on the recall path; the digest is the lever.")

    blocks = _blocks(state)
    assert len(blocks) == 1
    assert blocks[0]["topic_key"].startswith("scratchpad:")
    assert "the digest is the lever" in blocks[0]["content"]
    reset_sinks()


def test_the_mirror_is_recorded_per_block(engram_stub):
    """"The working memory is also in Engram" must be auditable, per block."""
    state, env = engram_stub
    memory = _append(env, "a durable working note")

    entries = [row for row in _journal(env.drive_root) if row.get("type") == "block_appended"]
    assert entries[-1]["engram_mirror"] == "sent"
    # ...and the local write still happened, unchanged.
    assert memory.scratchpad_path().exists()
    reset_sinks()


def test_the_mirror_identity_is_content_addressed(engram_stub):
    """C17: a forward retry must land on the SAME identity, so it upserts.

    The identity is derived from the block, not from a counter or a timestamp, so
    a retry in a LATER PROCESS (the spool path) addresses the same record. The
    in-run dedupe suppresses an identical repeat inside one run, so the identity is
    asserted directly and then through a spool forward.
    """
    from ouroboros.memory import _scratchpad_block_fingerprint

    state, env = engram_stub
    memory = Memory(env.drive_root, env.repo_dir)
    block = {"ts": "2026-01-01T00:00:00+00:00", "source": "task", "content": "same content"}

    # Deterministic, and sensitive to the content it addresses.
    assert _scratchpad_block_fingerprint(block) == _scratchpad_block_fingerprint(dict(block))
    other = dict(block, content="different content")
    assert _scratchpad_block_fingerprint(block) != _scratchpad_block_fingerprint(other)

    # A failed mirror leaves the fragment spooled with that identity...
    state.fail = True
    memory._mirror_block_to_engram(block, source="task")
    spooled = sink_for(env).pending()
    assert [rec["identity"] for rec in spooled] == [
        f"scratchpad:{_scratchpad_block_fingerprint(block)}"
    ]

    # ...and the forward retry sends exactly that topic_key, so the record upserts.
    state.fail = False
    assert sink_for(env).flush() == 1
    keys = [rec["topic_key"] for rec in _blocks(state)]
    assert keys == [f"scratchpad:{_scratchpad_block_fingerprint(block)}"]
    reset_sinks()


def test_a_todo_shaped_block_is_skipped_and_the_skip_is_recorded(engram_stub):
    """C18 vs working memory: a to-do is still not a memory — but say so.

    A scratchpad block may legitimately lead with what to do next. That is not a
    memory, so it is correctly refused; the point of this test is that the refusal
    is *recorded* rather than looking like a successful mirror.
    """
    state, env = engram_stub
    _append(env, "下一步：把 recall 的 digest 换掉", source="task")

    assert _blocks(state) == []
    entries = [row for row in _journal(env.drive_root) if row.get("type") == "block_appended"]
    assert entries[-1]["engram_mirror"] == "skipped:todo_not_memory"
    # The local working memory still holds it — that is where a to-do belongs.
    assert "下一步" in Memory(env.drive_root).load_scratchpad()
    reset_sinks()


def test_an_unreachable_store_never_breaks_the_scratchpad(engram_stub):
    """The mirror is soft: the local write is the source of truth and must land."""
    state, env = engram_stub
    state.fail = True
    memory = _append(env, "written while the store was down")

    assert "written while the store was down" in memory.load_scratchpad()
    assert "written while the store was down" in memory.scratchpad_path().read_text(encoding="utf-8")
    entries = [row for row in _journal(env.drive_root) if row.get("type") == "block_appended"]
    assert entries[-1]["engram_mirror"] == "spooled"
    # C17: not lost — it is spooled for the next run.
    pending = sink_for(env).pending()
    assert any("written while the store was down" in str(rec.get("content")) for rec in pending)
    reset_sinks()


def test_the_mirror_is_bounded(engram_stub):
    from ouroboros.engram_sink import KNOWLEDGE_DOC_CHARS

    state, env = engram_stub
    _append(env, "x" * 60_000)
    assert len(_blocks(state)[0]["content"]) <= KNOWLEDGE_DOC_CHARS
    reset_sinks()


# --------------------------------------------------------------------------- #
# A4's repoint — the review pack must state what working memory it saw
# --------------------------------------------------------------------------- #


def test_the_review_pack_states_how_much_working_memory_it_saw(engram_stub):
    from ouroboros.deep_self_review import _engram_review_section

    state, env = engram_stub
    _append(env, "the working note the review should see")

    section = _engram_review_section(env.drive_root)

    assert "### Working memory in Engram (1 block(s))" in section
    assert "Scratchpad block" in section
    reset_sinks()


def test_the_review_pack_says_when_there_is_no_working_memory(engram_stub):
    from ouroboros.deep_self_review import _engram_review_section

    state, env = engram_stub
    state.observations = [
        {"id": 1, "type": "decision", "title": "a memory", "created_at": "2026-01-01"}
    ]

    section = _engram_review_section(env.drive_root)

    assert "no scratchpad block recorded" in section
    reset_sinks()


def test_the_review_pack_does_not_call_an_unreachable_store_empty(engram_stub):
    from ouroboros.deep_self_review import _engram_review_section

    state, env = engram_stub
    state.fail = True

    section = _engram_review_section(env.drive_root)

    assert "UNKNOWN" in section
    assert "not absent" in section


# --------------------------------------------------------------------------- #
# type_digest — the bounded primitive the repoint is built on
# --------------------------------------------------------------------------- #


def test_type_digest_filters_by_type_over_a_bounded_window(engram_stub):
    state, env = engram_stub
    state.observations = [
        {"id": 1, "type": "decision", "title": "not a block", "created_at": "2026-01-01"},
        {"id": 2, "type": "scratchpad_block", "title": "block one", "created_at": "2026-01-02"},
        {"id": 3, "type": "scratchpad_block", "title": "block two", "created_at": "2026-01-03"},
    ]

    read = type_digest(client_for(env), "scratchpad_block", limit=8, window=50)

    assert read.ok and read.count == 2
    assert "block one" in read.text and "block two" in read.text
    assert "not a block" not in read.text
    # One bounded request, not a sweep.
    assert [r["path"] for r in state.requests if r["path"] not in ("/project/current", "/sessions")] == ["/observations/recent"]
    assert int(state.requests[-1]["params"]["limit"]) == 50


def test_type_digest_discloses_a_saturated_window(engram_stub):
    """Under-reporting a bounded read is acceptable; claiming completeness is not."""
    state, env = engram_stub
    state.observations = [
        {"id": index, "type": "decision", "title": f"m{index}", "created_at": "2026-01-01"}
        for index in range(50)
    ] + [{"id": 99, "type": "scratchpad_block", "title": "only block", "created_at": "2026-01-02"}]

    read = type_digest(client_for(env), "scratchpad_block", limit=8, window=50)

    assert read.ok and read.count == 1
    assert "window saturated at 50 records" in read.text


def test_type_digest_keeps_the_three_states_apart(engram_stub):
    state, env = engram_stub
    empty = type_digest(client_for(env), "scratchpad_block", window=50)
    assert empty.readable and empty.status == "empty"

    state.fail = True
    down = type_digest(client_for(env), "scratchpad_block", window=50)
    assert not down.readable and down.status == "unavailable"
