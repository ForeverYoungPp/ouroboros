"""The `## Dialogue History` seam now injects Engram.

The consolidated dialogue used to be rendered here from `dialogue_blocks.json` —
unconditionally, unbounded (it reached 30,344 chars), and billed to every prompt.
The content lives in Engram now and is fetched at the SAME seam, so this file
pins the properties that make the replacement faithful rather than merely
different:

* **relevance first** — the owner's own message is the retrieval query;
* **continuity as the fallback** — the section it replaces was an unconditional
  narrative, so a query that matches nothing must still show the newest blocks
  rather than going blank;
* **titles only** — the server returns full bodies from `/search`, so shipping
  them here is exactly how the removed 30 KB would come straight back;
* **typed degradation** — an unreachable or refusing store must say UNKNOWN, not
  "there is no history".
"""

from __future__ import annotations

import json

from ouroboros.context import (
    DIALOGUE_RECALL_BUDGET_CHARS,
    _engram_recall_section,
    build_memory_sections,
)
from ouroboros.engram_read import client_for, search_titles
from ouroboros.engram_sink import (
    _dialogue_block_identity,
    push_local_dialogue_blocks,
    reset_sinks,
)
from ouroboros.memory import Memory


def _block(range_str: str, content: str, *, kind: str = "summary", count: int = 100) -> dict:
    return {"ts": "2026-09-11T00:00:00+00:00", "type": kind, "range": range_str,
            "message_count": count, "content": content}


def _seed(state, blocks, *, start_id: int = 500) -> None:
    """Put dialogue blocks where both the search and the recency read can see them."""
    for offset, block in enumerate(blocks):
        record = {
            "id": start_id + offset,
            "type": "dialogue_summary",
            "title": f"Dialogue {block['type']}: {block['range']}",
            "topic_key": f"dialogue:{block['type']}:{block['range']}",
            "scope": "global",
            "content": block["content"],
            "created_at": "2026-09-11",
            "updated_at": f"2026-09-{10 + offset:02d}",
        }
        state.knowledge[record["id"]] = record
        state.observations.append(record)


# --------------------------------------------------------------------------- #
# Layer 1: titles, bounded
# --------------------------------------------------------------------------- #


def test_the_recall_is_titles_only(engram_stub):
    """The body must not ride into the prompt; the tool is what reads bodies."""
    state, env = engram_stub
    _seed(state, [_block("2026-09-05 10:00 - 12:00", "BODY-MARKER the full narrative text")])

    read = search_titles(client_for(env), "narrative", type_name="dialogue_summary")

    assert read.ok
    assert "BODY-MARKER" not in read.text
    assert "2026-09-05 10:00 - 12:00" in read.text
    reset_sinks()


def test_the_recall_is_bounded(engram_stub):
    from ouroboros.engram_read import MAX_DIGEST_CHARS

    state, env = engram_stub
    _seed(state, [_block(f"range-{i}", "x" * 500) for i in range(40)])

    read = search_titles(client_for(env), "range", type_name="dialogue_summary", limit=5)

    assert read.ok and len(read.text.splitlines()) <= 5
    assert len(read.text) <= MAX_DIGEST_CHARS
    reset_sinks()


# --------------------------------------------------------------------------- #
# The section, at the seam
# --------------------------------------------------------------------------- #


def test_a_relevant_query_drives_the_recall(engram_stub):
    state, env = engram_stub
    _seed(state, [
        _block("2026-09-05 10:00 - 12:00", "we discussed the consolidator pipeline"),
        _block("2026-09-06 10:00 - 12:00", "we discussed engram project pinning"),
    ])
    memory = Memory(env.drive_root, env.repo_dir)

    section = _engram_recall_section(memory, "engram project pinning")

    assert section.startswith("## Remembered History (Engram)")
    assert "2026-09-06" in section
    assert "BODY" not in section
    assert "titles only" in section
    reset_sinks()


def test_an_unmatched_query_falls_back_to_the_newest(engram_stub):
    """Continuity: the old section was unconditional, so this cannot go blank."""
    state, env = engram_stub
    _seed(state, [
        _block("2026-09-05", "older span"),
        _block("2026-09-09", "newest span"),
    ])
    state.search_returns_content = False
    memory = Memory(env.drive_root, env.repo_dir)

    section = _engram_recall_section(memory, "zzzz-nothing-matches-this")

    assert "## Remembered History (Engram)" in section
    assert section.strip(), "the section went blank when the query matched nothing"
    reset_sinks()


def test_an_empty_store_adds_no_section(engram_stub):
    state, env = engram_stub
    memory = Memory(env.drive_root, env.repo_dir)
    assert _engram_recall_section(memory, "anything") == ""
    reset_sinks()


def test_an_unreachable_store_says_unknown_not_absent(engram_stub):
    state, env = engram_stub
    state.fail = True
    memory = Memory(env.drive_root, env.repo_dir)

    section = _engram_recall_section(memory, "anything")

    assert "UNKNOWN" in section
    assert "not absent" in section
    assert "chat_history" in section  # the raw log is still reachable
    reset_sinks()


def test_a_refusing_store_is_not_reported_as_no_history(engram_stub):
    from ouroboros.engram_read import MachineRead

    state, env = engram_stub
    memory = Memory(env.drive_root, env.repo_dir)
    reset_sinks()
    import ouroboros.engram_read as read_mod

    original = read_mod.search_titles
    read_mod.search_titles = lambda *a, **k: MachineRead(False, status="rejected", detail="unknown_project")
    try:
        section = _engram_recall_section(memory, "anything")
    finally:
        read_mod.search_titles = original

    assert "refused" in section
    assert "no history" in section  # ...explicitly told NOT to read it that way
    reset_sinks()


def test_the_section_is_hard_bounded(engram_stub):
    state, env = engram_stub
    _seed(state, [_block(f"r{i}", "y" * 4_000) for i in range(40)])
    memory = Memory(env.drive_root, env.repo_dir)

    section = _engram_recall_section(memory, "r")

    assert len(section) <= DIALOGUE_RECALL_BUDGET_CHARS + 400  # header allowance
    reset_sinks()


# --------------------------------------------------------------------------- #
# The seam itself
# --------------------------------------------------------------------------- #


def test_the_seam_renders_recall_and_no_longer_dialogue_history(engram_stub):
    state, env = engram_stub
    _seed(state, [_block("2026-09-09", "the recent story")])
    memory = Memory(env.drive_root, env.repo_dir)
    # A local block file still exists: its gap projection must keep working.
    memory.drive_root.joinpath("memory").mkdir(parents=True, exist_ok=True)
    memory.scratchpad_blocks_path().write_text("[]", encoding="utf-8")

    sections = build_memory_sections(memory, partition="volatile", recall_query="the recent story")
    joined = "\n\n".join(sections)

    assert "## Remembered History (Engram)" in joined
    assert "## Dialogue History" not in joined
    reset_sinks()


def test_the_owner_message_reaches_the_seam_through_the_real_assembly(engram_stub):
    """The wiring is exercised, not assumed: task['text'] is the query."""
    from ouroboros.context import _capture_context_core

    state, env = engram_stub
    _seed(state, [
        _block("2026-09-05", "capability evidence scope floor"),
        _block("2026-09-09", "engram project pinning for ouroboros"),
    ])
    repo = env.repo_dir
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "ARCHITECTURE.md").write_text("# ARCH", encoding="utf-8")
    (repo / "prompts").mkdir(parents=True, exist_ok=True)
    (repo / "prompts" / "SYSTEM.md").write_text("base prompt", encoding="utf-8")
    (repo / "BIBLE.md").write_text("# BIBLE", encoding="utf-8")
    memdir = env.drive_root / "memory"
    memdir.mkdir(parents=True, exist_ok=True)
    (memdir / "identity.md").write_text("I am a test identity.", encoding="utf-8")
    (memdir / "WORLD.md").write_text("host: test", encoding="utf-8")
    (memdir / "scratchpad.md").write_text("scratchpad body", encoding="utf-8")
    (env.drive_root / "logs").mkdir(parents=True, exist_ok=True)
    (env.drive_root / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    memory = Memory(env.drive_root, env.repo_dir)
    task = {"id": "t", "type": "direct_chat", "chat_id": 1, "text": "engram project pinning"}

    core = _capture_context_core(env, memory, task, None, None)
    dynamic = core.dynamic_text or ""

    assert "## Remembered History (Engram)" in dynamic
    assert "2026-09-09" in dynamic
    queries = [r for r in state.requests if r["path"] == "/search"]
    assert queries and queries[-1]["params"]["q"] == "engram project pinning"
    reset_sinks()


# --------------------------------------------------------------------------- #
# Mirroring, so the seam has something to inject
# --------------------------------------------------------------------------- #


def test_a_distilled_block_is_mirrored_as_one_record(engram_stub):
    state, env = engram_stub
    from ouroboros.engram_sink import sink_for

    sink = sink_for(env)

    receipt = sink.emit_dialogue_summary(_block("2026-09-09 10:00 - 12:00", "# Summary\n\nwhat happened"))

    assert receipt.accepted
    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 1
    assert stored[0]["scope"] == "global"
    # A distilled block is prose, so its newlines must survive the round trip.
    assert "# Summary\n\nwhat happened" in stored[0]["content"]
    assert stored[0]["topic_key"].startswith("dialogue:")
    reset_sinks()


def test_pushing_the_same_blocks_twice_upserts_rather_than_piling_up(engram_stub):
    """The startup reconciliation must be repeatable — it runs on every boot."""
    state, env = engram_stub
    blocks = [_block("2026-09-05", "one"), _block("2026-09-06", "two")]

    first = push_local_dialogue_blocks(env, blocks)
    reset_sinks()
    push_local_dialogue_blocks(env, blocks)

    assert first == 2
    keys = [r["topic_key"] for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(keys) == 2
    assert len(set(keys)) == 2
    reset_sinks()


def test_resummarising_the_same_span_updates_its_record(engram_stub):
    """A retry emitting different prose is the SAME interval, not a second one.

    Content addressing made these two records, because the prose differs — which
    is exactly how one interval ends up duplicated. The interval is what the
    block IS; the prose is only how it was phrased this time.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import sink_for

    sink_for(env).emit_dialogue_summary(_block("2026-09-09 10:00 - 12:00", "first pass"))
    reset_sinks()
    sink_for(env).emit_dialogue_summary(_block("2026-09-09 10:00 - 12:00", "second pass"))

    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 1
    assert stored[0]["topic_key"] == "dialogue:summary:2026-09-09 10:00 - 12:00"
    assert stored[0]["content"] == "second pass"
    reset_sinks()


def test_two_memory_gap_markers_do_not_collapse_into_one(engram_stub):
    """Two lost generations are two P1 facts, not one overwritten record.

    A gap marker carries `range="unknown"`, so it has no interval at all and the
    distinguishing datum lives in `gap_id`. Keying on the span alone would fold
    every discontinuity in the biography onto one identity — and because the gap
    body is a fixed constant, content addressing folded them together too.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import sink_for

    gap = {
        "ts": "2026-09-11T00:00:00+00:00",
        "type": "summary",
        "range": "unknown",
        "message_count": 0,
        "content": "[MEMORY GAP] an un-consolidated span precedes this point.",
    }
    sink_for(env).emit_dialogue_summary({**gap, "gap_id": "gap:aaaa:100"})
    reset_sinks()
    sink_for(env).emit_dialogue_summary({**gap, "gap_id": "gap:bbbb:200"})

    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 2
    assert {r["topic_key"] for r in stored} == {
        "dialogue:summary:gap:aaaa:100",
        "dialogue:summary:gap:bbbb:200",
    }
    # Distinct titles as well: the live store's fallback dedupe keys on the title
    # too, so two records that must stay apart must not share one.
    assert len({r["title"] for r in stored}) == 2
    reset_sinks()


def test_a_block_with_no_range_does_not_collapse_into_another(engram_stub):
    """`push_local_dialogue_blocks` takes arbitrary mappings.

    A missing range is not a shared interval, so the mirror must not treat it as
    one — that would silently destroy every range-less block but the last.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import sink_for

    sink_for(env).emit_dialogue_summary({"type": "summary", "content": "no range at all"})
    reset_sinks()
    sink_for(env).emit_dialogue_summary({"type": "summary", "content": "another range-less block"})

    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 2
    assert len({r["topic_key"] for r in stored}) == 2
    reset_sinks()


def test_the_offset_interval_keeps_two_same_span_chunks_apart(engram_stub):
    """Minute-resolution timestamps CAN collide; message offsets cannot.

    A burst of a hundred messages inside one minute hands two different chunks the
    same `range` string. Addressing the record by that string would let the second
    overwrite the first, so the offsets win whenever the block carries them — and
    the title stays the readable span, because that is all the recall seam shows.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import sink_for

    span = "2026-09-07 03:28 - 03:28"
    first, second = _block(span, "chunk one"), _block(span, "chunk two")
    first["offset_range"], second["offset_range"] = "0-100", "100-200"

    sink_for(env).emit_dialogue_summary(first)
    reset_sinks()
    sink_for(env).emit_dialogue_summary(second)

    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 2
    assert {r["topic_key"] for r in stored} == {
        "dialogue:summary:0-100",
        "dialogue:summary:100-200",
    }
    # Titles may coincide: two intervals can share a span. The identity is what
    # keeps them apart, and the title must stay human-readable for the seam.
    assert {r["title"] for r in stored} == {f"Dialogue summary: {span}"}
    reset_sinks()


def test_boot_reconciliation_pushes_only_the_blocks_engram_lacks(engram_stub):
    """The carried-over history must arrive once, not on every start.

    The seam reads dialogue history from Engram, so the blocks distilled before
    the seam changed have to be mirrored — but a settled mirror must cost no
    writes at all, or every boot would bump every record's revision forever.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import reconcile_local_dialogue_blocks, sink_for

    blocks = [_block("2026-09-05", "one"), _block("2026-09-06", "two")]
    (env.drive_root / "memory" / "dialogue_blocks.json").write_text(
        json.dumps(blocks), encoding="utf-8"
    )
    sink_for(env).emit_dialogue_summary(blocks[0])  # already mirrored
    reset_sinks()

    assert reconcile_local_dialogue_blocks(env) == 1

    stored = [r for r in state.knowledge.values() if r.get("type") == "dialogue_summary"]
    assert len(stored) == 2
    reset_sinks()
    assert reconcile_local_dialogue_blocks(env) == 0
    reset_sinks()


def test_reconciliation_pushes_nothing_when_engram_is_unreachable(engram_stub):
    """A down daemon must not be handed the whole backlog on every start.

    Pushing blind would spool every block, so a daemon that stays down would
    accumulate the same records start after start. Doing nothing costs only a
    later start, when the mirror is up.
    """
    state, env = engram_stub
    from ouroboros.engram_sink import reconcile_local_dialogue_blocks, sink_for

    (env.drive_root / "memory" / "dialogue_blocks.json").write_text(
        json.dumps([_block("2026-09-05", "one")]), encoding="utf-8"
    )
    state.fail = True

    assert reconcile_local_dialogue_blocks(env) == 0
    assert sink_for(env).pending_count() == 0
    reset_sinks()


def test_the_consolidator_mirrors_what_it_just_wrote(engram_stub, tmp_path, monkeypatch):
    """The mirror is wired to the write, so new history is reachable immediately."""
    from ouroboros import consolidator

    state, env = engram_stub
    memory = Memory(env.drive_root, env.repo_dir)
    memory.scratchpad_blocks_path().parent.mkdir(parents=True, exist_ok=True)
    blocks_path = env.drive_root / "memory" / "dialogue_blocks.json"
    blocks_path.parent.mkdir(parents=True, exist_ok=True)
    blocks_path.write_text("[]", encoding="utf-8")
    meta_path = env.drive_root / "memory" / "dialogue_meta.json"
    meta_path.write_text("{}", encoding="utf-8")
    chat = env.drive_root / "logs" / "chat.jsonl"
    chat.parent.mkdir(parents=True, exist_ok=True)
    chat.write_text(
        "\n".join(
            f'{{"ts": "2026-09-11T00:0{i%10}:00+00:00", "direction": "owner", "text": "m{i}"}}'
            for i in range(consolidator.BLOCK_SIZE)
        ) + "\n",
        encoding="utf-8",
    )

    seen: list = []
    monkeypatch.setattr(
        "ouroboros.engram_sink.push_local_dialogue_blocks",
        lambda target, blocks: seen.append(list(blocks)) or len(list(blocks)),
    )
    monkeypatch.setattr(consolidator, "_consolidation_route", lambda: ("test-model", False))
    monkeypatch.setattr(
        consolidator, "_create_block_summary",
        lambda **kw: ("# distilled\n\nsummary text", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}),
    )

    consolidator.consolidate(chat, blocks_path, meta_path, llm_client=object(), identity_text="i")

    assert seen, "the consolidator did not push the blocks it distilled"
    pushed = seen[0][0]
    assert "# distilled" in str(pushed.get("content"))
    # End to end: the consolidator records the offsets it covered and the mirror
    # addresses the record by them, so two chunks sharing a minute cannot collide.
    assert pushed["offset_range"] == f"0-{consolidator.BLOCK_SIZE}"
    assert _dialogue_block_identity(pushed) == f"dialogue:summary:0-{consolidator.BLOCK_SIZE}"
    reset_sinks()
