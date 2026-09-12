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


def test_a_refused_knowledge_write_says_so_and_keeps_the_local_row(stub):
    """F1: never claim Engram durability the sink did not deliver.

    A capped emit returns BEFORE the spool, and the canonical local file is
    retired, so the local provenance row is the record. The message has to say
    what actually happened, and that row has to be on disk BEFORE the sink is
    asked — otherwise the "nothing was lost" it claims is only a hope.
    """
    import json

    from ouroboros.engram_sink import sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    ctx = _tool_ctx(env)
    sink_for(ctx).max_emits = 0  # every emit is refused by the cap

    out = _knowledge_write(ctx, "capped-topic", "a lesson that never reached Engram")

    assert "NOT Engram" in out, out
    assert "capped" in out, out
    assert "saved" not in out, out
    rows = [
        json.loads(line)
        for line in (env.drive_root / "memory" / "knowledge_history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["topic"] == "capped-topic"
    assert rows[-1]["new_content"] == "a lesson that never reached Engram"
    assert not [
        r for r in _saved_observations(state)
        if r["body"].get("topic_key") == "knowledge:capped-topic"
    ]


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
    # The bootstrap pair comes first: the server owns project policy, and the
    # session is created before any read can be scoped to an existing project.
    assert [r["path"] for r in state.requests] == [
        "/project/current", "/sessions", "/search", "/observations/101",
    ]
    assert int(_params(state, "/search")["limit"]) == 8
    assert _params(state, "/search")["project"] == "repo"


def _params(state, path):
    return next(r["params"] for r in state.requests if r["path"] == path)


def _seed_knowledge_recorded(state, topic: str, content: str, *, obs_id: int) -> None:
    """Seed BOTH projections: the real write path mirrors one row into the search
    table and the recency feed, and the fallback scan reads the recency feed."""
    _seed_knowledge(state, topic, content, obs_id=obs_id)
    state.observations.append(dict(state.knowledge[obs_id]))


def test_a_topic_below_the_search_window_is_still_resolved(stub):
    """The live risk the 3 -> 45 knowledge growth created.

    ``/search`` answers with a RANKED SLICE (limit=8); a target can sit below it,
    and the pre-fix code then reported "no Engram record" — an under-report shown
    as a whole-set absence. The bounded exact-key scan resolves it — bounded by
    ``MAX_WINDOW``, the module's record-shaped recency bound, never the ceiling.
    """
    from ouroboros.engram_read import MAX_DIGEST_ITEMS, MAX_WINDOW, knowledge_topic

    state, env = stub
    # The window fills with decoys that MATCH the query but are not the target...
    for i in range(MAX_DIGEST_ITEMS + 2):
        _seed_knowledge_recorded(
            state, f"decoy-{i}", "a body mentioning under-the-window in passing",
            obs_id=300 + i,
        )
    # ...and the target is seeded last, so it sorts BELOW the window.
    _seed_knowledge_recorded(state, "under-the-window", "the real body", obs_id=101)

    read = knowledge_topic(client_for(env), "under-the-window")

    assert read.ok and read.status == "ok" and read.text == "the real body"
    paths = [r["path"] for r in state.requests]
    assert "/search" in paths, "the fast path still runs first"
    assert "/observations/recent" in paths, "the fallback scan ran on the window miss"
    assert int(_params(state, "/observations/recent")["limit"]) == MAX_WINDOW
    assert _params(state, "/observations/recent")["project"] == "repo"   # same scope, no leak


def test_a_failed_fallback_scan_is_not_reported_as_an_absence(stub):
    """Isomorphic with the two sibling reads: a FAILED read is not an absence.

    The fallback scan must route its own failure through ``_read_failure`` — falling
    through to the bounded-absence branch would assert "an exact-key scan of the
    newest N records" for a scan that never completed.
    """
    from ouroboros.engram_read import MAX_DIGEST_ITEMS, MAX_WINDOW, knowledge_topic

    state, env = stub
    for i in range(MAX_DIGEST_ITEMS + 2):   # fill the search window; target sorts below it
        _seed_knowledge_recorded(
            state, f"decoy-{i}", "a body mentioning under-the-window in passing", obs_id=300 + i,
        )
    _seed_knowledge_recorded(state, "under-the-window", "the real body", obs_id=101)
    state.fail_paths.add("/observations/recent")   # ONLY the fallback's read fails

    read = knowledge_topic(client_for(env), "under-the-window")

    assert read.ok is False, read
    assert read.status == "unavailable", read      # what _read_failure maps a 500 to
    assert read.status != "empty", read
    assert f"newest {MAX_WINDOW} records" not in read.detail, read.detail


def test_a_match_with_an_unusable_id_is_not_reported_as_an_absence(stub):
    """A MATCH was found; ``empty`` would claim the store holds nothing."""
    from ouroboros.engram_read import knowledge_topic

    state, env = stub
    state.knowledge[999] = {
        "id": "weird", "type": "knowledge", "title": "Knowledge: odd-topic",
        "topic_key": "knowledge:odd-topic", "content": "",
        "created_at": "2026-01-01", "updated_at": "2026-01-02",
    }
    read = knowledge_topic(client_for(env), "odd-topic")

    assert read.ok is False and read.status == "unavailable", read
    assert "matched" in read.detail and "not an absence" in read.detail


def test_a_match_whose_payload_never_arrives_is_not_a_success(stub):
    """``got.ok`` with no record must not become status="ok" with an empty body."""
    from ouroboros.engram_read import knowledge_topic

    state, env = stub
    # the search hit carries id 102, but the store holds no record under it
    state.knowledge[101] = {
        "id": 102, "type": "knowledge", "title": "Knowledge: ghost-topic",
        "topic_key": "knowledge:ghost-topic", "content": "",
        "created_at": "2026-01-01", "updated_at": "2026-01-02",
    }
    read = knowledge_topic(client_for(env), "ghost-topic")

    assert read.ok is False and read.status == "unavailable", read
    assert "no record" in read.detail and "not an absence" in read.detail


def test_continuation_match_failures_are_not_absences_either(stub):
    """The same two branches in ``continuation_narrative``: unusable id, then no payload."""
    from ouroboros.engram_read import continuation_narrative

    state, env = stub
    state.knowledge[900] = {
        "id": "weird", "type": "episodic_memory", "title": "Task narrative t-1",
        "topic_key": "continuation:t-1", "content": "",
        "created_at": "2026-01-01", "updated_at": "2026-01-02",
    }
    bad_id = continuation_narrative(client_for(env), "t-1")
    assert bad_id.ok is False and bad_id.status == "unavailable", bad_id
    assert "not an absence" in bad_id.detail

    state.knowledge[901] = {
        "id": 902, "type": "episodic_memory", "title": "Task narrative t-2",
        "topic_key": "continuation:t-2", "content": "",
        "created_at": "2026-01-01", "updated_at": "2026-01-02",
    }
    no_payload = continuation_narrative(client_for(env), "t-2")
    assert no_payload.ok is False and no_payload.status == "unavailable", no_payload
    assert "no record" in no_payload.detail and "not an absence" in no_payload.detail


def test_a_genuinely_absent_topic_reports_a_bounded_absence(stub):
    """C15-style truthfulness: a bounded read must not claim the SET is empty."""
    from ouroboros.engram_read import MAX_DIGEST_ITEMS, MAX_WINDOW, knowledge_topic

    state, env = stub
    _seed_knowledge_recorded(state, "some-other-topic", "unrelated body", obs_id=101)

    read = knowledge_topic(client_for(env), "never-written")

    assert read.ok and read.status == "empty" and read.count == 0
    assert f"limit={MAX_DIGEST_ITEMS}" in read.detail
    assert str(MAX_WINDOW) in read.detail
    for forbidden in ("never learned", "does not exist", "not stored at all"):
        assert forbidden not in read.detail.lower()


def test_knowledge_topic_body_is_bounded(stub):
    from ouroboros.engram_read import MAX_TOPIC_CHARS, knowledge_topic

    state, env = stub
    _seed_knowledge(state, "big", "x" * 20_000)
    read = knowledge_topic(client_for(env), "big")
    assert read.ok and len(read.text) <= MAX_TOPIC_CHARS
    # The bound is unchanged, and the truncation names where the record continues —
    # through the tool THIS reader holds, not one it may not have.
    assert "truncated at char " in read.text and "of 20000" in read.text
    assert "continue with knowledge_read('big', offset=" in read.text
    assert "the engram tool" not in read.text


def test_knowledge_read_continues_a_truncated_topic(stub):
    """A lane with `knowledge_read` and no `engram` tool can still continue."""
    from ouroboros.engram_read import MAX_TOPIC_CHARS
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    body = "A" * 4_000 + "B" * MAX_TOPIC_CHARS
    _seed_knowledge(state, "long-topic", body)

    first = _knowledge_read(_tool_ctx(env), "long-topic")
    assert "knowledge_read('long-topic', offset=" in first
    assert "the engram tool" not in first

    offset = int(first.rsplit("offset=", 1)[1].split(")")[0])
    continued = _knowledge_read(_tool_ctx(env), "long-topic", offset)
    assert f"[continued from char {offset} of {len(body)}]" in continued
    # The window resumes exactly where the note said: the only A's left are the
    # tail of the first run (the cut lands mid-run by construction).
    assert "B" in continued
    assert continued.count("A") <= 4000 - offset

    past = _knowledge_read(_tool_ctx(env), "long-topic", 10 ** 7)
    assert "is past the end of this topic" in past and "nothing further to read" in past


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


def test_knowledge_read_no_longer_falls_back_to_the_archive(stub):
    """Owner ruling (option i): an ordinary topic resolves in Engram alone.

    The local file is the pre-switch archive, not a fallback — a store miss is
    reported as not found (or as UNKNOWN when the store cannot answer) instead of
    serving text Engram may since have superseded.
    """
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "legacy.md").write_text("learned before the switch", encoding="utf-8")

    out = _knowledge_read(_tool_ctx(env), "legacy")

    assert "learned before the switch" not in out
    assert "not found" in out


def test_knowledge_read_keeps_the_local_exceptions(stub):
    """`patterns` has no Engram record, so it stays local.

    `index-full` is the other local exception, but ``knowledge_read`` refuses it
    outright — it is a reserved internal name (SYSTEM.md: "do NOT call it
    directly"), so only the internal readers touch that file.
    """
    from ouroboros.tools.knowledge import _knowledge_read

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "patterns.md").write_text("local patterns body", encoding="utf-8")
    (archive / "index-full.md").write_text("local index body", encoding="utf-8")

    assert _knowledge_read(_tool_ctx(env), "patterns") == "local patterns body"
    assert "Reserved topic name" in _knowledge_read(_tool_ctx(env), "index-full")


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

    A stop that deletes its predecessor is not a stop, it is amnesia. Under the
    owner's Engram-only ruling these files are no longer the READ answer for an
    ordinary topic — which is exactly why this asserts the FILE, byte-for-byte,
    rather than trusting a read to prove it is still there.
    """
    from ouroboros.tools.knowledge import _knowledge_read, _knowledge_write

    state, env = stub
    archive = env.drive_root / "memory" / "knowledge"
    archive.mkdir(parents=True, exist_ok=True)
    original = "# legacy\n\nlearned before the switch\n"
    (archive / "legacy.md").write_text(original, encoding="utf-8")

    # The archive is still on disk; it is no longer the read path for this topic.
    assert (archive / "legacy.md").read_text(encoding="utf-8") == original
    assert "learned before the switch" not in _knowledge_read(_tool_ctx(env), "legacy")

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


def test_a_bare_drive_root_is_resolved_by_the_server(stub, monkeypatch):
    """When the service answers, it owns the answer — even for a bare drive root.

    Engram's reference client resolves through the server for exactly this reason,
    so the client cannot invent a scope the service disagrees with. The drive's own
    directory name is only what is LEFT when nothing better can be learned.
    """
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    monkeypatch.delenv("OUROBOROS_REPO_DIR", raising=False)
    state.detected_project = "resolved-by-server"
    reset_sinks()
    assert sink_for(env.drive_root).client.config.project == "resolved-by-server"
    reset_sinks()


def test_the_offline_fallback_is_the_drive_name_when_nothing_can_answer(stub, monkeypatch):
    """Offline with no repo root, the fallback IS the drive name.

    That is the last resort, not a design: it is why every production caller passes
    a root (``test_no_production_caller_hands_engram_a_bare_drive_root``) and why
    the drive carries its own ``.engram/config.json`` pin.
    """
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    monkeypatch.delenv("OUROBOROS_REPO_DIR", raising=False)
    state.fail = True  # nothing to ask
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


def test_deleting_both_knobs_still_cannot_reach_the_live_endpoint(monkeypatch, tmp_path):
    """The suite guard has to survive a test that removes both knobs on purpose.

    `test_engram_port_is_honoured` above does exactly that, and any test may call
    `monkeypatch.undo()` — which reverts every patch on the shared fixture instance,
    not just its own. In either window the client's resolution chain lands on
    `DEFAULT_BASE_URL`, the operator's LIVE endpoint. Setting the two knobs is
    therefore not enough: the transport itself refuses that endpoint, and the
    refusal is what this asserts (asserting "unreachable" would also pass on a
    machine that simply has no daemon running, which proves nothing).
    """
    from ouroboros.engram_sink import reset_sinks, sink_for

    monkeypatch.delenv("ENGRAM_BASE_URL", raising=False)
    monkeypatch.delenv("ENGRAM_PORT", raising=False)
    reset_sinks()
    try:
        sink = sink_for(tmp_path / "drive")
        result = sink.client.health()
        assert result.ok is False
        assert "refused a request to the live Engram endpoint" in str(result.detail), result.detail
        assert sink.emit("memory_action", title="t", content="must never land").status != "sent"
    finally:
        reset_sinks()


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


# A drive root handed to a REPOSITORY-scoped argument is the same second-store
# failure one step harder to see: the call names a ``repo_dir``, so the token
# check above passes, while the VALUE still resolves the project from the drive
# directory. The class is only closed when the value is checked, not the shape.
_DRIVE_ROOT_TOKEN = r"(?:drive_root|DRIVE_ROOT|canonical_root)"


def _repo_dir_is_the_drive_root_sites(package_root=None) -> list:
    """Call sites passing a DRIVE root as the repository root (F7 by value).

    Catches the two shapes the value can take: a bare drive-root token
    (``repo_dir=drive_root``) and the same expression used for both roots
    (``Memory(drive_root=root, repo_dir=root)``) — the shape that is literally a
    drive root bound to a variable and then handed over twice.
    """
    import pathlib as _pathlib
    import re

    root = (
        _pathlib.Path(package_root)
        if package_root is not None
        else _pathlib.Path(__file__).resolve().parents[1] / "ouroboros"
    )
    offenders = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for index, line in enumerate(source.splitlines(), start=1):
            if "repo_dir" not in line:
                continue
            value = re.search(r"\brepo_dir\s*=\s*([^,)\n]+)", line)
            if value is None:
                continue
            expression = value.group(1).strip()
            if re.fullmatch(rf"{_DRIVE_ROOT_TOKEN}(?:\.[A-Za-z_]\w*\(\))*", expression):
                offenders.append(f"{path.relative_to(root.parent)}:{index}: {line.strip()}")
                continue
            drive = re.search(rf"\b{_DRIVE_ROOT_TOKEN}\s*=\s*([A-Za-z_][\w.]*)\b", line)
            if drive is not None and expression == drive.group(1):
                offenders.append(f"{path.relative_to(root.parent)}:{index}: {line.strip()}")
    return offenders


#: Class B call sites whose repository root is genuinely unavailable at that
#: layer: ``consolidator._knowledge_ctx_for`` derives its ctx from a knowledge
#: DIRECTORY (``<drive>/memory/knowledge`` or a test fixture's
#: ``<root>/knowledge``), so it has no repository to thread through — the drive's
#: ``.engram/config.json`` pin is what keeps it on the right project. Flagged by
#: the value check when it was added (line 896), kept here as a declared
#: decision; a caller that CAN name a repository root must not be added.
_DRIVE_ROOT_AS_REPO_DIR_CALLERS = {
    "ouroboros/consolidator.py",
}


def test_no_production_caller_passes_the_drive_root_as_the_repo_dir():
    """The value, not only the shape: ``repo_dir=<drive root>`` is F7 by value.

    Invisible on a drive that carries the ``.engram/config.json`` pin, and a
    second, half-empty store on one that does not — which is why the check is on
    the value. The sanctioned fallback idiom that deliberately degrades to the
    drive root (``repo_dir=getattr(ctx, "repo_dir", None) or ctx.drive_root``)
    stays green: it is an expression, not a bare drive-root token.
    """
    offenders = [
        row
        for row in _repo_dir_is_the_drive_root_sites()
        if row.split(":")[0] not in _DRIVE_ROOT_AS_REPO_DIR_CALLERS
    ]
    assert not offenders, (
        "these hand a DRIVE root to a repository-scoped argument, so they resolve "
        "the project from the drive's directory name:\n  " + "\n  ".join(offenders)
    )


def test_the_repo_dir_value_check_catches_both_shapes_and_spares_real_roots(tmp_path):
    """The detector's own contract, on sources whose answer is known."""
    package = tmp_path / "ouroboros"
    package.mkdir()
    (package / "offender.py").write_text(
        "memory = Memory(drive_root=root, repo_dir=root)\n"
        "sink = sink_for(drive_root=drive_root, repo_dir=drive_root)\n",
        encoding="utf-8",
    )
    (package / "legit.py").write_text(
        "ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)\n"
        'mem = Memory(drive_root=ctx.drive_root, repo_dir=getattr(ctx, "repo_dir", None) or ctx.drive_root)\n'
        "sink = sink_for(drive_root, repo_dir=env.repo_dir)\n"
        "other = Memory(env.drive_root, getattr(env, \"repo_dir\", None))\n",
        encoding="utf-8",
    )

    offenders = _repo_dir_is_the_drive_root_sites(package)

    assert len(offenders) == 2, offenders
    assert all("offender.py" in row for row in offenders)
    assert [row for row in offenders if "legit.py" in row] == []


# --------------------------------------------------------------------------- #
# The bootstrap Engram's own client performs, and which we were missing
# --------------------------------------------------------------------------- #


def test_the_project_comes_from_the_server_not_from_our_own_rules(stub):
    """One GET /project/current?cwd= — the server owns project policy.

    Reimplementing the rules client-side is how the two sides end up disagreeing;
    Engram's reference client asks, and so do we.
    """
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    state.detected_project = "server-says-so"
    state.detected_source = "config"
    reset_sinks()

    sink = sink_for(env)

    assert sink.client.config.project == "server-says-so"
    detect = next(r for r in state.requests if r["path"] == "/project/current")
    assert detect["params"]["cwd"] == str(env.repo_dir.resolve())
    # The resolver must not be told the answer it is being asked for.
    assert "project" not in detect["params"]
    reset_sinks()


def test_an_ambiguous_detection_is_not_adopted(stub):
    """`error_hint` means detection did not decide; keep the offline resolution."""
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    state.detected_project = ""
    state.detected_source = "ambiguous"
    state.detect_error_hint = "10 repositories under this directory"
    reset_sinks()

    assert sink_for(env).client.config.project == "repo"  # the .engram/config.json name
    reset_sinks()


def test_a_forbidden_detection_is_refused(stub):
    """`local` is exactly the cross-project leak F7 names; never adopt it."""
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    state.detected_project = "local"
    state.detected_source = "process_override"
    reset_sinks()

    assert sink_for(env).client.config.project == "repo"
    reset_sinks()


def test_an_operator_override_beats_the_server(stub, monkeypatch):
    from ouroboros.engram_sink import reset_sinks, sink_for

    state, env = stub
    state.detected_project = "server-says-so"
    monkeypatch.setenv("ENGRAM_PROJECT", "operator-says-so")
    reset_sinks()

    assert sink_for(env).client.config.project == "operator-says-so"
    reset_sinks()


def test_the_session_is_created_before_anything_is_written(stub):
    """`POST /observations` requires a session; without this every write 404s."""
    from ouroboros.engram_sink import reset_sinks
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    reset_sinks()
    ctx = _tool_ctx(env)
    _knowledge_write(ctx, "bootstrap", "a fact")

    paths = [r["path"] for r in state.requests]
    assert paths.index("/sessions") < paths.index("/observations")
    session = next(r for r in state.requests if r["path"] == "/sessions")
    assert session["body"]["project"] == "repo"
    assert session["body"]["id"] == "repo"
    assert session["body"]["directory"] == str(env.repo_dir.resolve())
    reset_sinks()


def test_the_write_inherits_the_project_from_the_session(stub):
    """Engram: "Normal writes should not pass `project` as an arbitrary override"."""
    from ouroboros.engram_sink import reset_sinks
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    reset_sinks()
    _knowledge_write(_tool_ctx(env), "inherit", "a fact")

    body = _saved_observations(state)[-1]["body"]
    assert body["session_id"] == "repo"
    assert not str(body.get("project") or "").strip(), (
        "the write asserted a project instead of inheriting the session's"
    )
    reset_sinks()


def test_reads_also_bootstrap_once(stub):
    """A project-scoped READ 404s until the project exists, so reads bootstrap too."""
    from ouroboros.engram_read import client_for
    from ouroboros.engram_sink import reset_sinks

    state, env = stub
    reset_sinks()
    client_for(env)

    assert [r["path"] for r in state.requests] == ["/project/current", "/sessions"]
    reset_sinks()


def test_the_bootstrap_happens_once_per_process(stub):
    from ouroboros.engram_read import client_for
    from ouroboros.engram_sink import reset_sinks

    state, env = stub
    reset_sinks()
    for _ in range(4):
        client_for(env)

    assert [r["path"] for r in state.requests].count("/sessions") == 1
    reset_sinks()


def test_a_failed_bootstrap_stays_visible(stub):
    """A refused session must not be assumed away: the write spools and discloses."""
    from ouroboros.engram_sink import reset_sinks, sink_for
    from ouroboros.tools.knowledge import _knowledge_write

    state, env = stub
    state.fail = True
    reset_sinks()
    _knowledge_write(_tool_ctx(env), "no-bootstrap", "a fact")

    sink = sink_for(env)
    assert sink.ensure_ready() is False
    assert sink.pending_count() == 1
    assert (env.drive_root / "logs" / "engram.jsonl").exists()
    reset_sinks()


def test_a_4xx_is_reported_as_rejected_not_as_unreachable():
    """The store ANSWERED and refused.

    Reporting that as "unreachable" is what sent me chasing a dead service while
    /health was 200 — the failure the operator can fix (an unknown project, a
    missing session) was hidden behind the one they cannot.
    """
    from ouroboros.engram_client import EngramResult
    from ouroboros.engram_read import MachineRead, _read_failure

    refused = _read_failure(
        EngramResult(False, status=404, error_kind="http",
                     detail='{"code":"unknown_project","error":"project \"ouroboros\" not found"}')
    )
    assert refused.status == "rejected"
    assert refused.unknown is True and refused.readable is False
    assert "404" in refused.detail and "unknown_project" in refused.detail

    for status in (400, 401, 403, 409, 422):
        assert _read_failure(
            EngramResult(False, status=status, error_kind="http", detail="x")
        ).status == "rejected"

    # A transport failure keeps its own name: the fix is different.
    assert _read_failure(
        EngramResult(False, error_kind="transport", detail="connection refused")
    ).status == "unavailable"
    assert _read_failure(
        EngramResult(False, status=503, error_kind="http", detail="upstream")
    ).status == "unavailable"
    # ...and both mean "absence is unproven", which is the question consumers ask.
    for status in ("rejected", "unavailable"):
        assert MachineRead(False, status=status).unknown is True
    assert MachineRead(True, status="empty").unknown is False


def test_the_socket_transport_mismatch_is_named(monkeypatch):
    """Socket-only mode is a CLIENT limitation, not an outage."""
    from ouroboros.engram_client import env_socket

    monkeypatch.setenv("ENGRAM_SOCKET", "/tmp/engram.sock")
    assert env_socket() == "/tmp/engram.sock"
    monkeypatch.delenv("ENGRAM_SOCKET", raising=False)
    assert env_socket() == ""
