"""Phase-4 instruments: the probes must not lie once memory moves.

Covers seed C15 / AC17 — the precondition for stopping any local write. The old
instruments answered a *local, byte-shaped* question ("is this file big / what is
its sha"), and that question becomes meaningless the moment the file stops being
written:

* a thin local `scratchpad.md` is not decay if the memory is remote;
* an unreachable store is not an empty one;
* a local file's sha is not a memory-version once knowledge lives elsewhere.

Every assertion here is about that distinction, because collapsing any of the
three pairs is exactly how a memory-loss signal turns into noise.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.engram_read import (
    MAX_COUNT_LIMIT,
    MAX_DIGEST_CHARS,
    client_for,
    digest,
    entry_count,
    memory_version,
    recent,
)


@pytest.fixture()
def stub(engram_stub):
    """Local alias for the shared fake Engram service (``tests/conftest.py``)."""
    return engram_stub


def _observations(n: int) -> list[dict]:
    return [
        {"id": i, "type": "decision", "title": f"memory {i}",
         "created_at": "2026-01-01", "updated_at": f"2026-01-{i:02d}"}
        for i in range(1, n + 1)
    ]


# --------------------------------------------------------------------------- #
# AC16 — machine reads stay bounded and scoped
# --------------------------------------------------------------------------- #


def test_read_is_bounded_and_project_scoped(stub):
    """The recency window is bounded well below Engram's ceiling, on purpose.

    ``/observations/recent`` has no type filter and no compact mode, so every row
    carries its body: asking for the 500 ceiling would be "fetch the whole store".
    """
    from ouroboros.engram_read import MAX_WINDOW

    state, env = stub
    state.observations = _observations(3)
    recent(client_for(env))
    req = state.requests[-1]
    assert req["params"]["project"] == "repo"
    assert int(req["params"]["limit"]) == MAX_WINDOW
    assert MAX_WINDOW < MAX_COUNT_LIMIT


def test_digest_honours_the_local_predecessor_bounds(stub):
    """C14: a repointed consumer costs what it used to cost, not what the store holds."""
    state, env = stub
    state.observations = _observations(50)
    read = digest(client_for(env), limit=8, max_chars=3000)
    assert read.ok
    assert len(read.text.splitlines()) <= 8
    assert len(read.text) <= MAX_DIGEST_CHARS


def test_empty_and_unavailable_are_distinct(stub):
    """C15: 'no memory' and 'no service' must never be the same answer."""
    state, env = stub
    state.observations = []
    empty = entry_count(client_for(env))
    assert empty.readable and empty.status == "empty" and empty.count == 0

    state.fail = True
    down = entry_count(client_for(env))
    assert not down.readable and down.status == "unavailable"


def test_version_token_changes_with_the_store(stub):
    state, env = stub
    state.observations = _observations(2)
    first = memory_version(client_for(env))
    state.observations = _observations(3)
    second = memory_version(client_for(env))
    assert first.version and second.version and first.version != second.version


# --------------------------------------------------------------------------- #
# AC17(a) — the evolution checkpoint stops hashing a local index file
# --------------------------------------------------------------------------- #


def _checkpoint_row(tmp_path, drive_attr):
    from ouroboros.evolution_checkpoints import CHECKPOINTS_REL

    path = drive_attr / CHECKPOINTS_REL
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-1]


def test_checkpoint_records_engram_version_instead_of_a_local_file_sha(stub):
    from ouroboros.evolution_checkpoints import append_evolution_checkpoint

    state, env = stub
    state.observations = _observations(4)
    # A local index file exists and is non-empty: the old field would hash THIS,
    # and would keep hashing it (stably) long after it stopped being written.
    index = env.drive_root / "memory" / "knowledge" / "index-full.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("stale local index", encoding="utf-8")

    append_evolution_checkpoint(env.drive_root, env.repo_dir, task_id="E1")
    row = _checkpoint_row(env.drive_root, env.drive_root)

    assert "knowledge_index_sha256" not in row, "a local file sha is no longer a memory version"
    assert row["engram_memory_version_status"] == "ok"
    assert row["engram_memory_version"]
    assert row["engram_memory_count"] == 4


def test_checkpoint_marks_unavailable_rather_than_empty(stub):
    from ouroboros.evolution_checkpoints import append_evolution_checkpoint

    state, env = stub
    state.fail = True
    append_evolution_checkpoint(env.drive_root, env.repo_dir, task_id="E2")
    row = _checkpoint_row(env.drive_root, env.drive_root)

    assert row["engram_memory_version_status"] == "unavailable"
    assert row["engram_memory_version"] == ""
    assert row["engram_memory_count"] == 0


def test_checkpoint_reading_memory_does_not_break_the_write(stub):
    """The checkpoint is durable state; a dead memory service must not lose it."""
    from ouroboros.evolution_checkpoints import append_evolution_checkpoint

    state, env = stub
    state.fail = True
    append_evolution_checkpoint(env.drive_root, env.repo_dir, task_id="E3", outcome_axes={"a": 1})
    row = _checkpoint_row(env.drive_root, env.drive_root)
    assert row["task_id"] == "E3"
    assert row["outcome_axes"]


# --------------------------------------------------------------------------- #
# AC17(b) — the health probe stops crying wolf about a thin local file
# --------------------------------------------------------------------------- #


def _health(env) -> str:
    from ouroboros.context_health import build_health_invariants

    (env.drive_root / "logs").mkdir(parents=True, exist_ok=True)
    return build_health_invariants(env, task_id="t")


def test_thin_scratchpad_with_remote_memory_is_not_a_loss_warning(stub):
    """The whole point of AC17b: local thinness alone is not decay."""
    state, env = stub
    state.observations = _observations(3)
    (env.drive_root / "memory" / "scratchpad.md").write_text("x", encoding="utf-8")

    out = _health(env)
    assert "MEMORY LOSS" not in out
    assert "3 memories in Engram" in out


def test_thin_scratchpad_with_empty_engram_is_a_loss_warning(stub):
    """Both stores empty IS memory loss — the warning must still be reachable."""
    state, env = stub
    state.observations = []
    (env.drive_root / "memory" / "scratchpad.md").write_text("x", encoding="utf-8")

    out = _health(env)
    assert "WARNING: MEMORY LOSS" in out


def test_thin_scratchpad_with_unreachable_engram_is_a_note_not_a_warning(stub):
    """Unusable is not absent: an unreadable store must not read as 'no memory'."""
    state, env = stub
    state.fail = True
    (env.drive_root / "memory" / "scratchpad.md").write_text("x", encoding="utf-8")

    out = _health(env)
    assert "MEMORY LOSS" not in out
    assert "UNKNOWN" in out


def test_healthy_scratchpad_keeps_its_original_reporting(stub):
    """No behaviour change when the local store is fine."""
    state, env = stub
    state.observations = _observations(1)
    (env.drive_root / "memory" / "scratchpad.md").write_text("y" * 500, encoding="utf-8")

    out = _health(env)
    assert "OK: scratchpad size (500 chars)" in out


# --------------------------------------------------------------------------- #
# AC17(c) — the deep-review pack keeps its knowledge inputs
# --------------------------------------------------------------------------- #


def _pack_memory(env):
    from ouroboros.deep_self_review import _append_memory_whitelist

    parts: list[str] = []
    skipped: list[str] = []
    file_count = _append_memory_whitelist(parts, skipped, drive_root=env.drive_root)
    return "\n".join(parts), skipped, file_count


def test_review_pack_pulls_remote_memory_when_local_files_are_absent(stub):
    """The pack would otherwise collapse to identity + WORLD — silently."""
    state, env = stub
    state.observations = _observations(5)
    text, _, file_count = _pack_memory(env)
    assert "## ENGRAM MEMORY (5 records)" in text
    assert file_count == 0, "fixture has no local memory files; the remote half carries it"


def test_review_pack_says_unknown_when_memory_is_unreachable(stub):
    state, env = stub
    state.fail = True
    text, _, _ = _pack_memory(env)
    assert "UNKNOWN" in text
    assert "no records" not in text


def test_review_pack_says_empty_only_when_the_store_answered(stub):
    state, env = stub
    state.observations = []
    text, _, _ = _pack_memory(env)
    assert "no records for this project" in text


def test_review_pack_remote_section_is_bounded(stub):
    """One long LLM call with a fixed budget: the remote half must not dump."""
    from ouroboros.deep_self_review import _ENGRAM_REVIEW_CHARS

    state, env = stub
    state.observations = _observations(50)
    text, _, _ = _pack_memory(env)
    remote = text.split("## ENGRAM MEMORY", 1)[1]
    assert len(remote) <= _ENGRAM_REVIEW_CHARS + 200


# --------------------------------------------------------------------------- #
# S2 precondition — the agent's own knowledge_write mirrors to Engram
# --------------------------------------------------------------------------- #


def _tool_ctx(env):
    from ouroboros.tools.registry import ToolContext

    return ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, task_id="T1")


def _saved_observations(state) -> list[dict]:
    return [r for r in state.requests if r["path"] == "/observations"]


def test_knowledge_write_retires_the_canonical_local_file(stub):
    """S2 stop: the canonical ``<topic>.md`` is no longer written; Engram is the record.

    The file is not deleted either — an operator's pre-switch archive has to stay
    readable (C2 / BIBLE P1) — so the assertion is about the WRITE, not the path.
    """
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    out = _knowledge_write(_tool_ctx(env), "auth-model", "tokens rotate atomically")

    assert not (env.drive_root / "memory" / "knowledge" / "auth-model.md").exists()
    assert not (env.drive_root / "memory" / "knowledge" / "index-full.md").exists()
    assert "Engram" in out

    mirrored = _saved_observations(state)
    assert len(mirrored) == 1
    assert mirrored[0]["body"]["topic_key"] == "knowledge:auth-model"
    assert mirrored[0]["body"]["scope"] == "global"
    assert "tokens rotate atomically" in mirrored[0]["body"]["content"]


def test_knowledge_write_keeps_the_provenance_audit_trail(stub):
    """P1: the old/new record survives even though the topic file does not."""
    import json

    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "auth-model", "first take")
    _knowledge_write(ctx, "auth-model", "second take")

    rows = [
        json.loads(line)
        for line in (env.drive_root / "memory" / "knowledge_history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    # The second write read its base from the LIVE store (Engram), so the
    # provenance chain is unbroken across the retirement.
    assert rows[-1]["base_source"] == "engram"
    assert rows[-1]["old_content"] == "first take"
    assert rows[-1]["new_content"] == "second take"


def test_evolving_topic_upserts_rather_than_piling_up(stub):
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "auth-model", "first take")
    _knowledge_write(ctx, "auth-model", "second take")
    keys = [r["body"]["topic_key"] for r in _saved_observations(state)]
    assert keys == ["knowledge:auth-model", "knowledge:auth-model"]
    assert len(set(keys)) == 1


def test_knowledge_write_survives_an_unreachable_engram(stub):
    """C17: with the local landing point retired, the SPOOL is the no-loss guarantee."""
    from ouroboros.engram_sink import reset_sinks, sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    state.fail = True
    out = _knowledge_write(_tool_ctx(env), "resilience", "must not be lost when the store is down")

    assert out.startswith("✅")
    assert not (env.drive_root / "memory" / "knowledge" / "resilience.md").exists()
    pending = sink_for(env).pending()
    assert any(
        rec.get("identity") == "knowledge:resilience"
        and "must not be lost" in str(rec.get("content") or "")
        for rec in pending
    ), pending
    reset_sinks()


def test_append_defers_when_the_live_record_cannot_be_read(stub):
    """C17: an unreadable base defers the merge — it must not be guessed or dropped.

    Merging onto a guessed base would upsert over content nobody read (P1);
    refusing outright would lose the fragment for the whole outage. So the
    fragment is spooled and merged when the record can actually be read.
    """
    from ouroboros.engram_sink import sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    state.fail = True

    out = _knowledge_write(ctx, "auth-model", "extra detail", mode="append")

    assert "DEFERRED" in out
    assert not _saved_observations(state), "an unreadable base must not be written blind"
    pending = sink_for(env).pending()
    assert [rec["identity"] for rec in pending] == ["knowledge:auth-model"]
    assert pending[0]["content"] == "extra detail"
    assert pending[0]["deferred_merge"] is True


def test_deferred_append_merges_at_forward_time(stub):
    """The spooled fragment is merged onto the LIVE base once it can be read."""
    from ouroboros.engram_sink import reset_sinks, sink_for
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "auth-model", "existing fact")

    state.fail = True
    _knowledge_write(ctx, "auth-model", "late addition", mode="append")
    assert sink_for(env).pending_count() == 1

    # The store comes back and the next run forwards what it had to spool.
    state.fail = False
    forwarded = sink_for(env).flush()
    assert forwarded == 1
    assert sink_for(env).pending_count() == 0

    # Merged, not replaced: the pre-existing half survived.
    stored = _saved_observations(state)[-1]["body"]["content"]
    assert stored == "existing fact\nlate addition"
    assert "existing fact" in _knowledge_read(ctx, "auth-model")
    reset_sinks()


def test_deferred_append_is_idempotent_across_a_lost_ack(stub):
    """C17: a forward retry must not append the same fragment twice."""
    from ouroboros.engram_sink import reset_sinks, sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    state.fail = True
    _knowledge_write(ctx, "auth-model", "fragment", mode="append")
    sink = sink_for(env)
    record = sink.pending()[0]

    # First forward applies it but its ack is lost, so it stays pending...
    state.fail = False
    assert sink._send(record).ok
    # ...and re-forwarding recognises the fragment is already there.
    result = sink._send(record)
    assert result.ok and result.one() == {"already_applied": True}
    saved = _saved_observations(state)[-1]["body"]["content"]
    assert saved == "fragment"
    assert saved.count("fragment") == 1
    reset_sinks()


def test_deferred_append_keeps_the_fragment_when_the_store_stays_down(stub):
    """No loss and no overwrite: the record simply stays spooled."""
    from ouroboros.engram_sink import reset_sinks, sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    state.fail = True
    _knowledge_write(ctx, "auth-model", "fragment", mode="append")

    sink = sink_for(env)
    assert sink.flush() == 0
    assert sink.pending_count() == 1
    assert not _saved_observations(state)
    reset_sinks()


def test_knowledge_write_mirror_is_bounded(stub):
    """A huge topic must not become an unbounded payload — but must stay a DOCUMENT."""
    from ouroboros.engram_sink import KNOWLEDGE_DOC_CHARS
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    _knowledge_write(_tool_ctx(env), "big", "x" * 40_000)
    body = _saved_observations(state)[-1]["body"]
    assert len(body["content"]) <= KNOWLEDGE_DOC_CHARS
    # Truncation is disclosed, never hidden: a silently amputated topic would
    # read as a complete one.
    assert "truncated before storing" in body["content"]


def test_knowledge_mirror_keeps_the_document_structure(stub):
    """Engram is the ONLY record now, so the one-liner sanitizer must not flatten it.

    Collapsing whitespace would fold a markdown table, a list and a code block
    into a single unreadable line — silent degradation of the durable store (P1).
    """
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    document = "# Recipe\n\n- step one\n- step two\n\n```sh\necho hi\n```\n"
    _knowledge_write(_tool_ctx(env), "recipe", document)

    stored = _saved_observations(state)[-1]["body"]["content"]
    assert stored == document.strip()
    assert "- step one\n- step two" in stored
    assert "```sh\necho hi\n```" in stored


def test_append_merges_onto_the_live_record(stub):
    """An append keeps document structure too, in both the record and the read."""
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "auth-model", "base fact")
    _knowledge_write(ctx, "auth-model", "added fact", mode="append")

    body = _saved_observations(state)[-1]["body"]
    assert body["content"] == "base fact\nadded fact"
    assert "added fact" in _knowledge_read(ctx, "auth-model")


def test_project_scoped_write_still_lands_locally(stub, monkeypatch, tmp_path):
    """S2 names only the canonical store; per-project isolation has no Engram twin."""
    from ouroboros import config as cfg
    from ouroboros.tools.knowledge import _knowledge_write
    from ouroboros.tools.registry import ToolContext

    state, env = stub
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    ctx = ToolContext(
        repo_dir=env.repo_dir, drive_root=env.drive_root, task_id="T1", project_id="proj_abc"
    )
    _knowledge_write(ctx, "facts", "project fact")

    from ouroboros.project_facts import project_knowledge_dir

    local = project_knowledge_dir("proj_abc") / "facts.md"
    assert local.exists() and "project fact" in local.read_text(encoding="utf-8")
    # ...and it still reaches Engram, scoped to the project.
    assert _saved_observations(state)[-1]["body"]["scope"] == "project"


# --------------------------------------------------------------------------- #
# S2 read-before-stop — the knowledge read path exists before the local write stops
# --------------------------------------------------------------------------- #


def _seed_knowledge(state, topic: str, content: str, *, obs_id: int = 101) -> None:
    state.knowledge[obs_id] = {
        "id": obs_id,
        "type": "knowledge",
        "title": f"Knowledge: {topic}",
        "topic_key": f"knowledge:{topic}",
        "content": content,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
    }


def test_knowledge_topic_reads_one_record_by_topic_key(stub):
    from ouroboros.engram_read import knowledge_topic

    state, env = stub
    _seed_knowledge(state, "auth-model", "tokens rotate atomically")

    read = knowledge_topic(client_for(env), "auth-model")

    assert read.ok and read.text == "tokens rotate atomically"
    # Bounded + targeted: one search, one body fetch — never a store sweep.
    assert [r["path"] for r in state.requests] == ["/search", "/observations/101"]
    assert int(_params(state, "/search")["limit"]) == 8
    assert _params(state, "/search")["project"] == "repo"


def _params(state, path):
    return next(r["params"] for r in state.requests if r["path"] == path)


def test_knowledge_topic_body_is_bounded(stub):
    from ouroboros.engram_read import MAX_TOPIC_CHARS, knowledge_topic

    state, env = stub
    _seed_knowledge(state, "big", "x" * 20_000)
    read = knowledge_topic(client_for(env), "big")
    assert read.ok and len(read.text) <= MAX_TOPIC_CHARS


def test_knowledge_topic_absent_and_unreachable_are_distinct(stub):
    """C15: 'you never learned this' must not be said when the store is down."""
    from ouroboros.engram_read import knowledge_topic

    state, env = stub
    absent = knowledge_topic(client_for(env), "never-written")
    assert absent.readable and absent.status == "empty"

    state.fail = True
    down = knowledge_topic(client_for(env), "never-written")
    assert not down.readable and down.status == "unavailable"


def test_knowledge_read_falls_back_to_engram(stub):
    """The write/read pair must stay symmetric once the local file is gone."""
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    _seed_knowledge(state, "gpu-facts", "the box has one 4090")

    out = _knowledge_read(_tool_ctx(env), "gpu-facts")

    assert "the box has one 4090" in out
    assert "from Engram" in out
    assert "not found" not in out


def test_knowledge_read_round_trips_a_fresh_write(stub):
    """Write then read must return what was written — the pair's core promise."""
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "fresh", "learned just now")

    out = _knowledge_read(ctx, "fresh")

    assert "learned just now" in out


def test_knowledge_read_prefers_the_live_store_over_the_archive(stub):
    """Once the local write is retired, a local-first read would serve STALE text.

    The canonical file is the pre-switch archive: it can be arbitrarily older than
    the Engram record, so it must not win.
    """
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "gpu-facts.md").write_text("stale archive copy", encoding="utf-8")
    _seed_knowledge(state, "gpu-facts", "current remote copy")

    out = _knowledge_read(_tool_ctx(env), "gpu-facts")

    assert "current remote copy" in out
    assert "stale archive copy" not in out


def test_knowledge_read_falls_back_to_the_archive(stub):
    """Pre-switch topics have no Engram record; the archive still answers."""
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "legacy.md").write_text("learned before the switch", encoding="utf-8")

    out = _knowledge_read(_tool_ctx(env), "legacy")

    assert out == "learned before the switch"


def test_knowledge_read_says_unknown_when_engram_is_down(stub):
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    state.fail = True

    out = _knowledge_read(_tool_ctx(env), "somewhere-else")

    assert "UNKNOWN" in out
    assert "not found" in out  # it IS missing locally...
    assert "never learned" in out  # ...but that is not a verdict about memory


def test_knowledge_list_discloses_the_remote_half(stub):
    from ouroboros.tools.knowledge import _knowledge_list

    state, env = stub
    state.observations = _observations(3)

    out = _knowledge_list(_tool_ctx(env))

    assert "Engram additionally holds 3 durable record(s)" in out
    assert "op=search" in out


def test_knowledge_list_marks_an_unreachable_store_as_unknown(stub):
    from ouroboros.tools.knowledge import _knowledge_list

    state, env = stub
    state.fail = True

    out = _knowledge_list(_tool_ctx(env))

    assert "UNKNOWN" in out


# --------------------------------------------------------------------------- #
# S2 stop — the switch itself, and what must survive it
# --------------------------------------------------------------------------- #


def test_the_canonical_local_write_is_actually_retired():
    """The stop is a live claim: flipping the switch back must fail this suite.

    Asserted as a one-liner rather than inferred from behaviour, because every
    other test in this file would still pass if the switch were silently re-enabled
    and only the assertions above had been relaxed.
    """
    from ouroboros.tools.knowledge import CANONICAL_LOCAL_WRITE

    assert CANONICAL_LOCAL_WRITE is False


def test_project_facts_keep_a_live_local_write():
    """The stop must not have been applied to the path the seed never named."""
    from ouroboros.tools.knowledge import _local_write_live
    from ouroboros.tools.registry import ToolContext

    canonical = ToolContext(repo_dir="/tmp/r", drive_root="/tmp/d")
    project = ToolContext(repo_dir="/tmp/r", drive_root="/tmp/d", project_id="p1")

    assert _local_write_live(canonical) is False
    assert _local_write_live(project) is True


def test_stopping_the_write_did_not_delete_the_archive(stub):
    """C2 / P1: the local files stay on disk, readable, forever.

    A stop that deletes its predecessor is not a stop, it is amnesia.
    """
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    original = "# legacy\n\nlearned before the switch\n"
    (archive / "legacy.md").write_text(original, encoding="utf-8")

    # No Engram record for this topic, so the archive is the answer...
    assert "learned before the switch" in _knowledge_read(_tool_ctx(env), "legacy")

    # ...and a write to a DIFFERENT topic does not disturb it.
    _knowledge_write(_tool_ctx(env), "other", "something new")
    assert (archive / "legacy.md").read_text(encoding="utf-8") == original


def test_startup_flush_forwards_a_deferred_append(stub):
    """The production drain path (A5 startup) must handle a deferred merge.

    ``flush_engram_spool`` is what a later run actually calls; the sink-level test
    above proves the merge logic, and this proves the wiring that reaches it.
    """
    from ouroboros.engram_sink import flush_engram_spool, reset_sinks
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "protocol", "step one")

    state.fail = True
    _knowledge_write(ctx, "protocol", "step two", mode="append")
    state.fail = False

    forwarded = flush_engram_spool(env)

    assert forwarded == 1
    stored = _knowledge_read(ctx, "protocol")
    assert "step one" in stored and "step two" in stored
    reset_sinks()


# --------------------------------------------------------------------------- #
# Loop-closing: one process must speak to ONE project, whatever its callers do
# --------------------------------------------------------------------------- #


def test_every_production_call_shape_resolves_the_same_project(stub, monkeypatch, tmp_path):
    """The project must not depend on WHICH caller touches the sink first.

    Production holds two shapes: ``Env`` (lowercase) for the agent/context paths,
    and the supervisor's ctx (UPPERCASE ``DRIVE_ROOT``/``REPO_DIR``). A shape
    mismatch does not fail — it silently resolves a DIFFERENT project, which splits
    one system's memory in two. And because the sink is process-cached, the first
    caller used to decide for everyone: a bare-drive caller at boot pinned every
    later caller to the drive's own directory name.
    """
    from types import SimpleNamespace

    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    repo, drive = env.repo_dir, env.drive_root
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(repo))
    reset_sinks()

    # A bare drive root FIRST (the shape an evolution checkpoint uses at boot)...
    assert sink_for(drive).client.config.project == "repo"
    # ...must not decide for the Env-shaped callers that follow.
    assert sink_for(env).client.config.project == "repo"
    assert sink_for(SimpleNamespace(DRIVE_ROOT=drive, REPO_DIR=repo)).client.config.project == "repo"
    # And the reverse order agrees.
    reset_sinks()
    assert sink_for(env).client.config.project == "repo"
    assert sink_for(SimpleNamespace(DRIVE_ROOT=drive, REPO_DIR=repo)).client.config.project == "repo"
    assert sink_for(drive).client.config.project == "repo"
    reset_sinks()


def test_the_supervisor_ctx_spelling_is_accepted(stub):
    """Uppercase roots must resolve, not silently degrade to the drive's name."""
    from types import SimpleNamespace

    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    ctx = SimpleNamespace(DRIVE_ROOT=env.drive_root, REPO_DIR=env.repo_dir)
    assert sink_for(ctx).client.config.project == "repo"
    reset_sinks()


def test_a_bare_drive_root_never_silently_becomes_its_own_project(stub, monkeypatch):
    """With no repo root knowable, the caller must pass one — not guess.

    ``_repo_root_hint`` falls back to the drive root only as a last resort; this
    pins the fact that the fallback IS the drive name, so a future caller that
    drops its repo root is caught by ``test_every_production_call_shape...`` rather
    than discovered as two half-empty memory stores.
    """
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    monkeypatch.delenv("OUROBOROS_REPO_DIR", raising=False)
    reset_sinks()
    assert sink_for(env.drive_root).client.config.project == env.drive_root.name
    reset_sinks()


def test_engram_port_is_honoured(monkeypatch):
    """`ENGRAM_PORT` is the SERVICE's own knob; ignoring it means a silent miss."""
    from ouroboros.engram_client import DEFAULT_BASE_URL, env_base_url

    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)
    monkeypatch.delenv("ENGRAM_PORT", raising=False)
    assert env_base_url() == ""  # the client's own default applies
    assert DEFAULT_BASE_URL.endswith(":7437")

    monkeypatch.setenv("ENGRAM_PORT", "8123")
    assert env_base_url() == "http://127.0.0.1:8123"

    monkeypatch.setenv("ENGRAM_PORT", "not-a-port")
    assert env_base_url() == ""  # garbage never becomes an endpoint

    monkeypatch.setenv("ENGRAM_BASE_URL", "http://127.0.0.1:9999")
    assert env_base_url() == "http://127.0.0.1:9999"  # explicit override wins


def test_the_engram_knobs_are_projectable_from_settings():
    """The operator's normal surface is settings.json, not a hand-exported env."""
    from ouroboros.config import settings_env_keys

    keys = set(settings_env_keys())
    for name in ("ENGRAM_BASE_URL", "ENGRAM_PROJECT", "ENGRAM_HTTP_TOKEN", "OUROBOROS_REPO_DIR"):
        assert name in keys, f"{name} cannot be set through settings.json"


# --------------------------------------------------------------------------- #
# The bug class itself: a caller holding ONLY the drive root
# --------------------------------------------------------------------------- #
# A drive root cannot identify the repository, so it resolves the project from the
# drive's own directory name (or its `.engram/config.json` pin). That is not a
# crash — it is a second, half-empty memory store, which is exactly how "the
# feature is wired" and "the feature works" drift apart. This scan fails on a NEW
# bare-drive call site rather than waiting for someone to notice two stores.

_SCOPE_FUNCS = (
    "client_for(",
    "sink_for(",
    "flush_engram_spool(",
    "emit_reflection_memory_actions(",
    "emit_task_narrative(",
    "emit_evolution_outcome(",
    "emit_review_verdicts(",
)

#: Call sites that legitimately hold only a drive root. Each relies on the DRIVE
#: carrying the project pin (``<drive>/.engram/config.json``), which is why the
#: live drive is pinned as well as the repository. Adding an entry here is a
#: deliberate decision, not a way to silence the check:
#: ``save_state``/``append_authored_task_summary`` are reached from dozens of call
#: sites that hold no repository root, so threading one through them all would be a
#: far larger change than the pin it replaces.
_DRIVE_ONLY_CALLERS = {
    "ouroboros/project_dialogue.py",
    "ouroboros/review_state.py",
}


def _bare_drive_call_sites() -> list:
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "ouroboros"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for index, line in enumerate(source.splitlines(), start=1):
            if not any(name in line for name in _SCOPE_FUNCS):
                continue
            if "def " in line and any(name.rstrip("(") in line for name in _SCOPE_FUNCS):
                continue
            if not re.search(r"\b(drive_root|DRIVE_ROOT|canonical_root)\b", line):
                continue
            if re.search(r"\b(repo_dir|REPO_DIR|_scope\(|context_env)\b", line):
                continue
            offenders.append(f"{path.relative_to(root.parent)}:{index}: {line.strip()}")
    return offenders


def test_no_production_caller_hands_engram_a_bare_drive_root():
    """Every scope resolution must see the repository, or be pinned on the drive."""
    offenders = [row for row in _bare_drive_call_sites() if row.split(":")[0] not in _DRIVE_ONLY_CALLERS]
    assert not offenders, (
        "these pass only a drive root, so they would resolve the project from the "
        "drive's directory name and write to a SECOND memory store:\n  "
        + "\n  ".join(offenders)
    )
