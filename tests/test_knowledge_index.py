"""The store-side knowledge index: enumeration, write-through, projection, listing.

`index-full.md` is the PRE-SWITCH archive: its writer is retired, so it can never name
a topic written since, and Engram has no type-enumerating route (`/observations/recent`
takes no type filter, `/stats` has no breakdown, `/search` needs a non-empty query).
The result was measured on a live drive: topics existing in the store with NO surface
that could list them. This file pins the four things that close that gap:

* the index is SEEDED from the provenance log (so a drive that predates the feature
  still names everything), idempotently and without duplicating rows;
* it is written THROUGH by the knowledge write path, carrying the transport outcome,
  so "queued in Engram" is distinguishable from "sent";
* the RESIDENT projection is bounded, discloses what it omits, and is present in the
  MAIN CHAT while staying out of the background lane (which already carries the full
  archive index through the un-gated builder);
* `knowledge_list` names STORE topics first — the acceptance test is a post-switch
  topic that lives only in Engram, which no listing surface could show before.
"""

from __future__ import annotations

import json
import pathlib

from ouroboros.tools.knowledge import (
    INDEX_LEDGER,
    _knowledge_list,
    _knowledge_write,
    knowledge_index_rows,
)


class _Ctx:
    """Minimal ToolContext stand-in (the shape the knowledge tools read)."""

    def __init__(self, drive_root, project_id=""):
        self.drive_root = pathlib.Path(drive_root)
        self.repo_dir = pathlib.Path(__file__).resolve().parents[1]
        self.project_id = project_id
        self.task_id = "t1"

    def drive_path(self, rel):
        return self.drive_root / rel


def _env(ctx):
    from ouroboros.agent import Env

    return Env(repo_dir=pathlib.Path("."), drive_root=ctx.drive_root)


def _kdir(ctx) -> pathlib.Path:
    return ctx.drive_root / "memory" / "knowledge"


def _history_row(topic: str, ts: str, **extra) -> str:
    row = {"ts": ts, "task_id": "t", "topic": topic, "mode": "overwrite", **extra}
    return json.dumps(row) + "\n"


def _seed_history(ctx, rows: str) -> None:
    _kdir(ctx).mkdir(parents=True, exist_ok=True)
    (_kdir(ctx).parent / "knowledge_history.jsonl").write_text(rows, encoding="utf-8")


def _archive(ctx, entries: dict) -> None:
    """Write the pre-switch archive index (its entries carry per-topic summaries)."""
    _kdir(ctx).mkdir(parents=True, exist_ok=True)
    (_kdir(ctx) / "index-full.md").write_text(
        "# Knowledge Base Index\n\n"
        + "".join(f"- **{topic}**: {summary}\n" for topic, summary in entries.items()),
        encoding="utf-8",
    )


def _seed_ledger(ctx, count: int) -> None:
    """Rows in the STORE ledger itself (the file the union's pointer must name)."""
    from ouroboros.tools.knowledge import _index_ledger_path, _index_write_rows

    assert _index_write_rows(_index_ledger_path(ctx), [
        {
            "ts": f"2026-09-12T00:00:{index % 60:02d}+00:00",
            "topic": f"store-topic-{index:03d}",
            "identity": f"knowledge:store-topic-{index:03d}",
            "scope": "", "mode": "overwrite", "status": "sent",
        }
        for index in range(count)
    ])


# --------------------------------------------------------------------------- #
# The seed
# --------------------------------------------------------------------------- #


def test_the_seed_names_topics_the_archive_could_never_show(tmp_path):
    """The acceptance case for the whole item: a topic with NO local file.

    Before this, such a topic was unnameable by any surface — the archive predates it
    and Engram cannot be enumerated — so the agent could not even ask for it.
    """
    ctx = _Ctx(tmp_path / "drive")
    _seed_history(ctx, _history_row("post-switch-topic", "2026-09-11T00:00:00+00:00"))

    rows = knowledge_index_rows(ctx)

    assert [r["topic"] for r in rows] == ["post-switch-topic"]
    assert rows[0]["identity"] == "knowledge:post-switch-topic"
    assert rows[0]["status"] == "seeded"
    assert not (_kdir(ctx) / "post-switch-topic.md").exists(), "there is no local file"


def test_the_seed_keeps_the_newest_row_per_topic_and_drops_the_local_only_ones(tmp_path):
    ctx = _Ctx(tmp_path / "drive")
    _seed_history(
        ctx,
        _history_row("alpha", "2026-09-09T00:00:00+00:00")
        + _history_row("alpha", "2026-09-12T00:00:00+00:00")
        + _history_row("beta", "2026-09-10T00:00:00+00:00", new_sha256="x")
        # the backlog's audit row is topic-only — still evidence the topic exists
        + _history_row("improvement-backlog", "2026-09-12T01:00:00+00:00", mode="add->merge")
        + _history_row("patterns", "2026-09-12T02:00:00+00:00"),
    )

    rows = {r["topic"]: r for r in knowledge_index_rows(ctx)}

    assert sorted(rows) == ["alpha", "beta"], "the two local-only topics stay in the archive"
    assert rows["alpha"]["ts"] == "2026-09-12T00:00:00+00:00"  # newest wins
    assert rows["beta"]["ts"] == "2026-09-10T00:00:00+00:00"


def test_the_seed_is_idempotent_and_reaches_every_earlier_topic(tmp_path):
    """A first write seeds the WHOLE log, then never re-seeds (the local-index trap)."""
    ctx = _Ctx(tmp_path / "drive")
    _seed_history(
        ctx,
        _history_row("older-one", "2026-09-01T00:00:00+00:00")
        + _history_row("older-two", "2026-09-02T00:00:00+00:00"),
    )
    ledger = _kdir(ctx).parent / INDEX_LEDGER

    from ouroboros.tools.knowledge import _index_seed

    _index_seed(ctx)
    once = ledger.read_text(encoding="utf-8")
    assert len(once.splitlines()) == 2, "both pre-existing topics must be seeded"

    _index_seed(ctx)
    assert ledger.read_text(encoding="utf-8") == once, "a second seed must not duplicate"


def test_a_drive_with_no_history_or_ledger_yields_no_rows_and_writes_nothing(tmp_path):
    ctx = _Ctx(tmp_path / "drive")

    assert knowledge_index_rows(ctx) == []
    assert not (_kdir(ctx).parent / INDEX_LEDGER).exists()
    assert not _kdir(ctx).exists()


# --------------------------------------------------------------------------- #
# The write-through
# --------------------------------------------------------------------------- #


def test_a_write_appends_one_newest_row_carrying_the_transport_status(tmp_path, monkeypatch):
    """Write-through records what the SINK said, not what we hoped."""
    from ouroboros.engram_sink import EngramSink

    ctx = _Ctx(tmp_path / "drive")
    kdir = _kdir(ctx)
    monkeypatch.setattr(
        EngramSink, "_send",
        lambda self, record: type("R", (), {"ok": True, "error_kind": "", "detail": "", "status": 200})(),
    )
    monkeypatch.setattr(EngramSink, "ensure_ready", lambda self: True)
    monkeypatch.setattr(EngramSink, "compact_if_idle", lambda self: False)
    monkeypatch.setattr("ouroboros.tools.knowledge._engram_topic_read", lambda *a, **k: None)

    _knowledge_write(ctx, topic="gamma", content="# gamma\n\nbody\n")

    ledger = kdir.parent / INDEX_LEDGER
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    gamma = [r for r in rows if r["topic"] == "gamma"]
    assert len(gamma) == 1, "one row per write"
    assert gamma[0]["status"] in ("sent", "spooled"), gamma[0]
    assert gamma[0]["identity"] == "knowledge:gamma"

    # A second write to the SAME topic appends a second row, and the reader collapses
    # them to the newest — the append-only trail never becomes a duplicate listing.
    _knowledge_write(ctx, topic="gamma", content="# gamma\n\nbody v2\n")
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len([r for r in rows if r["topic"] == "gamma"]) == 2
    assert [r["topic"] for r in knowledge_index_rows(ctx)].count("gamma") == 1


def test_the_index_stays_bounded_and_records_evictions(tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path / "drive")
    ledger = _kdir(ctx).parent / INDEX_LEDGER
    _kdir(ctx).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "ouroboros.tools.knowledge.KNOWLEDGE_INDEX_TOPIC_CAP", 3
    )
    from ouroboros.tools.knowledge import _index_record

    for index in range(5):
        _index_record(ctx, topic=f"t{index}", scope="global", mode="overwrite", status="sent")

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = {r["topic"] for r in rows if not r.get("event")}
    evicted = {r["topic"] for r in rows if r.get("event") == "evict"}
    assert len(kept) == 3, kept
    assert evicted, "an evicted topic must be audited, never silently dropped"
    assert not (kept & evicted)


# --------------------------------------------------------------------------- #
# The resident projection — main chat only
# --------------------------------------------------------------------------- #


def _section_rows(ctx, count: int, *, day: int = 10) -> None:
    _seed_history(
        ctx,
        "".join(
            _history_row(f"topic-{i:02d}", f"2026-09-{day:02d}T00:00:{i:02d}+00:00")
            for i in range(count)
        ),
    )


def test_the_projection_is_bounded_and_names_what_it_omits(tmp_path):
    from ouroboros.context import KNOWLEDGE_INDEX_BUDGET_CHARS, _knowledge_index_section

    ctx = _Ctx(tmp_path / "drive")
    _section_rows(ctx, 200)

    section = _knowledge_index_section(_env(ctx))

    assert len(section) <= KNOWLEDGE_INDEX_BUDGET_CHARS, len(section)
    assert section.startswith("## Knowledge index (titles only)")
    assert "knowledge:topic-" in section, "rows carry the identity to read"
    assert "not listed here" in section, "the omission must be NAMED (P1)"
    assert "knowledge_index.jsonl" in section, "and the full index pointed at"
    assert "not proof a record is still present" in section, "name-index, not a census"


def test_the_projection_says_nothing_when_the_index_is_empty(tmp_path):
    from ouroboros.context import _knowledge_index_section

    ctx = _Ctx(tmp_path / "drive")

    assert _knowledge_index_section(_env(ctx)) == ""


def test_the_projection_is_in_the_main_chat_and_not_in_the_background_lane():
    """MAIN CHAT ONLY, pinned so a future "add it to BG too" fails a test.

    BG already renders the full archive index through the un-gated builder, so a
    titles-only list there would duplicate most of its rows for less information each.
    """
    import inspect

    from ouroboros import consciousness
    from ouroboros import context as context_mod

    main_src = inspect.getsource(context_mod)
    assert "_knowledge_index_section(context_env)" in main_src, "wired into the main chat"
    assert "knowledge_index = _knowledge_index_section" in main_src
    assert "_knowledge_index_section" not in inspect.getsource(consciousness), (
        "the BG lane must keep rendering its own (archive) index, not this projection"
    )
    # ...and the builder the two lanes DO share is still called un-gated by BG.
    bg_call = inspect.getsource(consciousness.BackgroundConsciousness._build_context)
    assert "build_knowledge_sections(" in bg_call
    assert "include_derived_knowledge" not in bg_call, (
        "BG takes the default True, which is what keeps its archive index resident"
    )


# --------------------------------------------------------------------------- #
# The background lane's union — archive entries + store titles
# --------------------------------------------------------------------------- #


def test_the_bg_lane_names_a_topic_the_archive_froze_before(tmp_path):
    """The acceptance case: the lane that renders the archive can NAME a topic
    written after the local write was retired — without losing the summaries."""
    from ouroboros.context import build_knowledge_sections

    ctx = _Ctx(tmp_path / "drive")
    _archive(ctx, {"pre-switch-topic": "the archived summary"})
    _seed_history(ctx, _history_row("post-switch-topic", "2026-09-12T00:00:00+00:00"))

    blob = "\n".join(build_knowledge_sections(_env(ctx)))

    assert "the archived summary" in blob, "an archive entry keeps its summary"
    assert "knowledge:post-switch-topic" in blob, "a post-switch topic is NAMED"
    assert "- pre-switch-topic —" not in blob, "an archive topic is not repeated as a title"


def test_a_topic_in_both_is_listed_once_with_its_summary(tmp_path):
    """Deduped by topic: the archive's line wins because it carries the summary."""
    from ouroboros.context import build_knowledge_sections

    ctx = _Ctx(tmp_path / "drive")
    _archive(ctx, {"shared-topic": "the archived summary"})
    _seed_history(ctx, _history_row("shared-topic", "2026-09-12T00:00:00+00:00"))

    blob = "\n".join(build_knowledge_sections(_env(ctx)))

    assert blob.count("shared-topic") == 1, "one line per topic"
    assert "the archived summary" in blob, "and it is the one that keeps the summary"
    assert "knowledge:shared-topic" not in blob, "the titles-only row is suppressed"


def test_the_bg_union_names_store_topics_with_no_archive_at_all(tmp_path):
    """A project (or a drive that never had an archive) must not go blank: the old
    builder skipped the section whenever the archive read was empty."""
    from ouroboros.context import build_knowledge_sections

    ctx = _Ctx(tmp_path / "drive")
    _seed_history(ctx, _history_row("store-only-topic", "2026-09-12T00:00:00+00:00"))

    blob = "\n".join(build_knowledge_sections(_env(ctx)))

    assert "knowledge:store-only-topic" in blob


def test_the_bg_union_bounds_the_store_half_and_names_the_omission(tmp_path):
    from ouroboros.context import KNOWLEDGE_INDEX_BUDGET_CHARS, build_knowledge_sections

    ctx = _Ctx(tmp_path / "drive")
    _archive(ctx, {"pre-switch-topic": "the archived summary"})
    _seed_ledger(ctx, 200)

    blob = "\n".join(build_knowledge_sections(_env(ctx)))

    assert "the archived summary" in blob, "the frozen archive half is never clipped"
    assert "not listed here" in blob, "the omission must be NAMED (P1)"
    assert "knowledge_index.jsonl" in blob, "and the full store index pointed at"
    store_half = blob[blob.index("### Store topics"):]
    assert len(store_half) <= KNOWLEDGE_INDEX_BUDGET_CHARS, len(store_half)


def test_the_main_chat_projection_still_carries_store_titles_only(tmp_path):
    """(d): the union is the BACKGROUND lane's shape. The resident main-chat projection
    is untouched — it names store topics and never re-hosts the archive."""
    from ouroboros.context import _knowledge_index_section

    ctx = _Ctx(tmp_path / "drive")
    _archive(ctx, {"archived-topic": "an archive-only summary"})
    _seed_history(ctx, _history_row("store-topic", "2026-09-12T00:00:00+00:00"))

    section = _knowledge_index_section(_env(ctx))

    assert "store-topic" in section
    assert "archived-topic" not in section
    assert "an archive-only summary" not in section


# --------------------------------------------------------------------------- #
# The listing — the day-one consumer
# --------------------------------------------------------------------------- #


def test_the_listing_names_a_topic_that_lives_only_in_engram(tmp_path):
    """The acceptance test for the item: impossible before this change.

    `knowledge_list` read the local archive (which predates the switch) or rendered the
    local files, so a topic mirrored to Engram with no local file was invisible — the
    agent could not learn that it existed in order to read it.
    """
    ctx = _Ctx(tmp_path / "drive")
    _seed_history(ctx, _history_row("engram-only-topic", "2026-09-12T05:00:00+00:00"))
    assert not (_kdir(ctx) / "engram-only-topic.md").exists()

    listing = _knowledge_list(ctx)

    assert "STORE INDEX" in listing
    assert "engram-only-topic" in listing
    assert "knowledge:engram-only-topic" in listing


def test_the_listing_keeps_the_archive_half_for_the_local_only_topics(tmp_path):
    ctx = _Ctx(tmp_path / "drive")
    kdir = _kdir(ctx)
    kdir.mkdir(parents=True)
    (kdir / "patterns.md").write_text("| Error class | Count |\n", encoding="utf-8")
    (kdir / "index-full.md").write_text(
        "# Knowledge Base Index\n\n"
        "- **patterns**: local by ruling\n"
        "- **covered-topic**: also in the store now\n",
        encoding="utf-8",
    )
    _seed_history(ctx, _history_row("covered-topic", "2026-09-12T00:00:00+00:00"))

    listing = _knowledge_list(ctx)

    assert "STORE INDEX" in listing and "covered-topic" in listing
    assert "ARCHIVE INDEX" in listing, "the archive half keeps its scope note"
    assert "patterns" in listing, "a local-only topic must survive the redirect"
    # ...and a topic the store half already names is not repeated in the archive half.
    assert "**covered-topic**" not in listing, listing
    assert "**patterns**" in listing, listing


def test_the_listing_claims_only_what_the_write_through_index_knows(tmp_path):
    """The listing must not over-claim about what is ABSENT from it.

    The index is write-through: the governance-doc mirror writes to Engram through the
    sink directly, so its records (312 on the live drive) exist in the store and are not
    listed here. "a name absent here was never written to the store" was therefore
    literally false — an absence claim stronger than the mechanism can support.
    """
    ctx = _Ctx(tmp_path / "drive")
    _seed_history(ctx, _history_row("known-topic", "2026-09-12T00:00:00+00:00"))

    listing = _knowledge_list(ctx)

    assert "through the knowledge tools" in listing, "the scope of the index is named"
    assert "WRITE-THROUGH index" in listing
    assert "governance-doc mirror" in listing, "the bypasser is named, not implied"
    assert "never written to the store" not in listing, "the over-claim must not return"


def test_the_listing_still_renders_local_files_the_store_does_not_know(tmp_path):
    """A drive whose topic files predate the provenance log must not go blank."""
    ctx = _Ctx(tmp_path / "drive")
    kdir = _kdir(ctx)
    kdir.mkdir(parents=True)
    (kdir / "git-recipes.md").write_text("# Git recipes\n\nUse rebase sparingly.\n", encoding="utf-8")

    listing = _knowledge_list(ctx)

    assert "git-recipes" in listing
    assert "rebase" in listing, "summaries are still rendered for archive entries"


# --------------------------------------------------------------------------- #
# The residue class: a row whose body is nowhere is DISCLOSED, once verified
# --------------------------------------------------------------------------- #


def test_a_verified_absent_row_says_so_and_nothing_else_does(tmp_path, monkeypatch):
    """The caveat is earned, not inferred.

    "No local file" is the NORMAL shape for a post-switch topic (23 of the live drive's 90
    rows are store-only, every one readable), so a render that inferred absence from it
    would cry wolf on every prompt. Only the audit's recorded verdict may speak.
    """
    from ouroboros import engram_read
    from ouroboros.engram_read import MachineRead
    from ouroboros.tools.knowledge import audit_index_topics, knowledge_index_line

    ctx = _Ctx(tmp_path / "drive")
    _seed_history(
        ctx,
        _history_row("still-in-store", "2026-09-11T00:00:00+00:00")
        + _history_row("gone-for-good", "2026-09-11T01:00:00+00:00"),
    )

    unverified = {r["topic"]: knowledge_index_line(r) for r in knowledge_index_rows(ctx)}
    assert "exists nowhere" not in unverified["gone-for-good"], "unverified rows stay silent"

    monkeypatch.setattr(engram_read, "client_for", lambda _ctx: object())
    monkeypatch.setattr(
        engram_read,
        "knowledge_topic",
        lambda _client, topic, scope="", **_kw: (
            MachineRead(True, status="ok", count=1, text="a body")
            if topic == "still-in-store"
            else MachineRead(True, status="empty", count=0)
        ),
    )

    assert audit_index_topics(ctx) == {"present": 1, "not_found": 1, "unreadable": 0}
    lines = {r["topic"]: knowledge_index_line(r) for r in knowledge_index_rows(ctx)}
    assert "exists nowhere" in lines["gone-for-good"], "the verified absence is disclosed"
    assert "knowledge_read" in lines["gone-for-good"], "and the check is named"
    assert "exists nowhere" not in lines["still-in-store"], "a readable topic gets no caveat"


def test_an_unreadable_store_records_no_verdict(tmp_path, monkeypatch):
    """'Could not read' must never be written down as 'absent' (C15)."""
    from ouroboros import engram_read
    from ouroboros.engram_read import MachineRead
    from ouroboros.tools.knowledge import (
        INDEX_STATUS_NOT_FOUND,
        audit_index_topics,
        knowledge_index_line,
    )

    ctx = _Ctx(tmp_path / "drive")
    _seed_history(ctx, _history_row("unchecked", "2026-09-11T00:00:00+00:00"))
    monkeypatch.setattr(engram_read, "client_for", lambda _ctx: object())
    monkeypatch.setattr(
        engram_read,
        "knowledge_topic",
        lambda *_a, **_kw: MachineRead(False, status="unavailable", detail="store down"),
    )

    assert audit_index_topics(ctx)["unreadable"] == 1
    rows = {r["topic"]: r for r in knowledge_index_rows(ctx)}
    assert rows["unchecked"]["status"] != INDEX_STATUS_NOT_FOUND, "no verdict was recorded"
    assert "exists nowhere" not in knowledge_index_line(rows["unchecked"])
