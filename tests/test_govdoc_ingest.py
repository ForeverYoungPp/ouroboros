"""The governance-doc Engram mirror: chunking, identity, linkage, isolation.

The four governance docs are mirrored into Engram as retrieval-sized records, and
the navigation maps injected into the prompt name the record per entry. These tests
pin the properties that make that mirror faithful rather than merely present:

* **one read per record** — every assembled record (breadcrumb included) fits
  ``GOVDOC_READ_CHARS``, the bound ``engram op="read"`` enforces. A record over it
  comes back truncated and reads like a complete one, so the chunker is sized to
  that bound, not to the sink's 16,000-char document cap;
* **nothing lost** — the chunk bodies are an in-order, byte-conserving partition of
  the doc, so the mirror cannot quietly drop or reorder text;
* **addressable** — one stable identity per chunk, unique even when a source line had
  to be split, and the map's per-entry pointer resolves to the chunk holding it;
* **additive** — a map built without an ``engram_slug`` is byte-identical to the
  file-only map the four existing callers already depend on;
* **isolated** — the mirror's record type is invisible to a ``type="knowledge"``
  digest, so the existing knowledge surface is never rewritten by name.

Everything here is hermetic: no test talks to an Engram service.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

from ouroboros import context_layout as cl
from ouroboros.engram_read import MAX_DIGEST_ITEMS, MAX_WINDOW, type_digest

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNNER = REPO / ".ouroboros" / "engram-packet" / "govdoc_ingest.py"
IDENTITY_RE = re.compile(
    r"^knowledge:govdoc:(system|bible|architecture|development):[0-9]+$"
)


def _runner():
    """Import the one-shot writer by path — it is not a package module."""
    spec = importlib.util.spec_from_file_location("govdoc_ingest", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(slug: str) -> tuple[str, list[str]]:
    rel_path, _name = cl.GOVDOC_DOCS[slug]
    text = (REPO / rel_path).read_text(encoding="utf-8")
    return text, text.splitlines()


def _records(slug: str):
    text, lines = _doc(slug)
    chunks = cl.chunk_doc(text)
    return [
        cl.govdoc_record(slug, lines=lines, chunks=chunks, ordinal=i)
        for i in range(1, len(chunks) + 1)
    ]


# --------------------------------------------------------------------------- #
# Chunking — the partition properties
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slug", sorted(cl.GOVDOC_DOCS))
def test_chunk_bodies_partition_the_doc_in_order_without_losing_a_byte(slug):
    """Bodies are in-order slices of the source and conserve its length.

    This is the anti-truncation guard behind BIBLE P1: a mirror that drops or
    reorders text would still look complete from the outside.
    """
    text, lines = _doc(slug)
    chunks = cl.chunk_doc(text)
    bodies = [cl.chunk_body(lines, chunk) for chunk in chunks]
    joined = "\n".join(bodies)

    assert chunks[0].start == 1 and chunks[-1].end == len(lines)
    assert len(joined) == len("\n".join(lines))  # every cut consumes exactly one space
    position = 0
    for body in bodies:
        found = "\n".join(lines).find(body, position)
        assert found >= 0, "a chunk body does not occur in the source"
        position = found + len(body)
    # non-piece spans tile the line space; a split line contributes ONE line
    covered = 0
    for chunk in chunks:
        if chunk.piece is None:
            covered += chunk.end - chunk.start + 1
    split_lines = {c.start for c in chunks if c.piece is not None}
    assert covered + len(split_lines) == len(lines)


@pytest.mark.parametrize("slug", sorted(cl.GOVDOC_DOCS))
def test_every_heading_maps_into_one_chunk_that_contains_its_line(slug):
    """The map's per-entry pointer must resolve — every time, not usually."""
    _text, lines = _doc(slug)
    chunks = cl.chunk_doc("\n".join(lines))
    for _level, _title, line in cl._markdown_headings(lines):
        containing = chunks[cl.chunk_ordinal_for(chunks, line) - 1]
        assert containing.start <= line <= containing.end


def test_a_section_over_the_floor_starts_its_own_chunk():
    filler = "word " * 400  # comfortably over GOVDOC_MIN_FILL_CHARS
    text = f"## One\n\n{filler}\n\n## Two\n\n{filler}\n"
    chunks = cl.chunk_doc(text)
    assert [c.start for c in chunks] == [1, 5]


def test_a_heading_inside_a_fence_is_content_not_a_boundary():
    """The heading definition is shared with the map, fence rule included."""
    text = "## Real\n\nbody\n```markdown\n## fake\n```\n\nmore\n"
    assert [t for _lv, t, _ln in cl._markdown_headings(text.splitlines())] == ["Real"]
    assert [c.start for c in cl.chunk_doc(text)] == [1]


def test_sections_below_the_floor_coalesce_into_one_record():
    small = "x" * 300
    text = f"## A\n\n{small}\n## B\n\n{small}\n## C\n\n{small}\n"
    chunks = cl.chunk_doc(text)
    assert len(chunks) == 1
    assert chunks[0] == cl.DocChunk(1, len(text.splitlines()))


def test_an_overflowing_span_closes_on_a_paragraph_break():
    """A chunk must not end mid-sentence when a blank line was available."""
    paragraphs = [f"## Section {i}\n\n" + ("word " * 120) for i in range(12)]
    text = "\n\n".join(paragraphs) + "\n"
    for chunk in cl.chunk_doc(text):
        if chunk.piece is None and chunk.end < len(text.splitlines()):
            assert text.splitlines()[chunk.end - 1].strip() == ""


def test_a_line_beyond_the_cap_splits_into_balanced_space_aligned_parts():
    line = " ".join(f"tok{i}" for i in range(2_500))
    assert len(line) > cl.GOVDOC_CHUNK_CHARS
    text = f"## Big\n\n{line}\n"
    chunks = cl.chunk_doc(text)
    pieces = [c for c in chunks if c.piece is not None]
    assert len(pieces) > 1
    assert all(len(c.piece) <= cl.GOVDOC_CHUNK_CHARS for c in pieces)
    # balanced, not one long part plus a stub
    assert min(len(c.piece) for c in pieces) > cl.GOVDOC_CHUNK_CHARS // 2
    # the parts reproduce the source line byte-for-byte at the record boundaries
    rejoined = "\n".join(c.piece for c in pieces)
    assert len(rejoined) == len(line)
    assert rejoined.replace("\n", " ") == line


# --------------------------------------------------------------------------- #
# The record contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slug", sorted(cl.GOVDOC_DOCS))
def test_every_assembled_record_fits_one_untruncated_read(slug):
    """The governing bound is the READ bound, not the sink's document cap."""
    for title, content in _records(slug):
        assert len(content) <= cl.GOVDOC_READ_CHARS
        assert len(content) <= cl.GOVDOC_READ_CHARS  # breadcrumb included
        assert title and len(title) <= 300  # the sink clips a title past 300 chars
        assert content.startswith(title)


@pytest.mark.parametrize("slug", sorted(cl.GOVDOC_DOCS))
def test_identities_are_unique_stable_and_store_shaped(slug):
    text, lines = _doc(slug)
    chunks = cl.chunk_doc(text)
    identities = [cl.govdoc_identity(slug, i) for i in range(1, len(chunks) + 1)]
    assert len(set(identities)) == len(identities)
    assert all(IDENTITY_RE.match(i) for i in identities)
    # stable: the same doc and ordinal always yields the same key (upsert, never duplicate)
    assert identities == [cl.govdoc_identity(slug, i) for i in range(1, len(chunks) + 1)]
    assert identities[0] == f"knowledge:govdoc:{slug}:1"


def test_a_record_names_its_successor_so_a_long_section_stays_walkable():
    records = _records("architecture")
    for ordinal, (_title, content) in enumerate(records, start=1):
        if ordinal < len(records):
            assert f"Next: knowledge:govdoc:architecture:{ordinal + 1}" in content
        else:
            assert "Next:" not in content


# --------------------------------------------------------------------------- #
# Crowding isolation — the claim the mirror rests on
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, items):
        self.ok = True
        self.status = 200
        self._items = items

    def items(self):
        return self._items


class _Client:
    """A recency feed only: exactly what ``type_digest`` consumes."""

    def __init__(self, records):
        self.config = type("C", (), {"project": "ouroboros"})()
        self._records = records

    def recent(self, *, limit=50, scope="", all_projects=False):
        return _Result(self._records[:limit])


def _record(identity: str, rtype: str, updated: str) -> dict:
    return {
        "id": identity,
        "topic_key": identity,
        "type": rtype,
        "title": f"Knowledge: {identity}",
        "scope": "global",
        "created_at": updated,
        "updated_at": updated,
    }


def test_the_mirror_type_can_never_appear_in_a_knowledge_digest():
    """Type separation is what keeps the existing knowledge surface untouched.

    Window rows include both types here, so this pins the MATCH rule the isolation
    actually rests on: the client-side type comparison drops every mirror row.
    """
    knowledge = [_record(f"knowledge:topic{i}", "knowledge", f"2026-09-0{i}") for i in range(1, 6)]
    mirror = [_record(f"knowledge:govdoc:architecture:{i}", "govdoc", "2026-09-10") for i in range(1, 6)]
    client = _Client(knowledge + mirror)

    known = type_digest(client, "knowledge", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    assert known.status == "ok" and known.count == len(knowledge)
    assert "govdoc" not in known.text
    assert all(f"knowledge:topic{i}" in known.text for i in range(1, 6))

    gov = type_digest(client, "govdoc", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    assert gov.status == "ok" and gov.count == len(mirror)
    assert "govdoc:architecture" in gov.text


def test_the_shared_window_is_why_the_knowledge_digest_count_falls():
    """The mirror's cost, pinned as behaviour rather than described in prose.

    ``type_digest`` counts matches inside the newest ``MAX_WINDOW`` records and the
    recency feed takes no type filter, so mirror rows displace older rows out of the
    window. The records themselves are untouched — this test exists so that the
    displacement is a known, deliberate property and not a surprise later.
    """
    knowledge = [_record(f"knowledge:topic{i}", "knowledge", f"2026-09-{i:02d}") for i in range(1, 6)]
    before = type_digest(_Client(knowledge), "knowledge", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    assert before.status == "ok" and before.count == 5

    displaced = [_record(f"knowledge:govdoc:system:{i}", "govdoc", "2026-09-12") for i in range(1, 60)]
    after = type_digest(_Client(displaced + knowledge), "knowledge", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    assert after.status == "empty" and after.count == 0
    # ...and the same window read still FINDS the mirror by its own type
    mirror = type_digest(_Client(displaced + knowledge), "govdoc", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    assert mirror.status == "ok"


# --------------------------------------------------------------------------- #
# Nav-map linkage and its additivity
# --------------------------------------------------------------------------- #


def test_a_linked_map_names_the_chunk_holding_each_entry():
    text, lines = _doc("architecture")
    chunks = cl.chunk_doc(text)
    rendered = cl.generate_doc_nav_map(
        text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md", engram_slug="architecture"
    )
    assert "knowledge:govdoc:architecture:" in rendered
    assert "Ingested:" in rendered
    for line in rendered.splitlines():
        match = re.search(r"— lines (\d+)-\d+ · record knowledge:govdoc:architecture:(\d+)", line)
        if not match:
            continue
        heading_line, ordinal = int(match.group(1)), int(match.group(2))
        chunk = chunks[ordinal - 1]
        assert chunk.start <= heading_line <= chunk.end


def test_a_map_without_a_slug_is_unchanged_apart_from_the_linkage():
    """The four existing callers pass no slug and must see byte-identical output."""
    text = "## A\n\nbody A\n\n### A child\n\nchild body\n"
    plain = cl.generate_doc_nav_map(text, title="X", rel_path="x.md")
    linked = cl.generate_doc_nav_map(
        text, title="X", rel_path="x.md", engram_slug="architecture"
    )

    assert "knowledge:govdoc" not in plain and "Ingested:" not in plain
    plain_lines = plain.splitlines()
    linked_lines = linked.splitlines()
    # the header paragraph is untouched; the hint is an extra paragraph after it
    assert linked_lines[:4] == plain_lines[:4]
    assert linked_lines[3] == "" and linked_lines[4].startswith("Ingested:")
    entries = [ln for ln in plain_lines if ln.strip().startswith("- ")]
    assert len(entries) == 2
    linked_entries = [ln for ln in linked_lines if ln.strip().startswith("- ")]
    assert [ln.split(" · record ")[0] for ln in linked_entries] == entries


def test_low_mode_development_is_a_linked_map_not_a_bare_name():
    """D-DEV keeps the handbook out of the prompt for external work — as a MAP.

    The name is still visible (P1) but it now arrives with the doc's line ranges and
    its Engram records, instead of a path the agent has to remember to open.
    """
    dev = "## Dev A\n\nbody\n\n## Dev B\n\nmore body\n"
    parts = cl.reference_doc_sections(
        None,
        context_mode="low",
        include_development=False,
        architecture_text="## Arch A\n\narch body\n",
        development_text=dev,
    )
    rendered = "\n\n".join(parts)

    assert "body" not in rendered  # neither doc's body is inlined
    assert "## DEVELOPMENT.md (navigation map)" in rendered
    assert 'path="docs/DEVELOPMENT.md"' in rendered
    assert "knowledge:govdoc:development:1" in rendered
    assert "`docs/DEVELOPMENT.md`" not in rendered.split("## Reference docs available on demand")[-1]


# --------------------------------------------------------------------------- #
# The one-shot writer
# --------------------------------------------------------------------------- #


def test_the_writer_plan_covers_the_four_docs_with_bounded_batches():
    module = _runner()
    docs = module._plan(REPO, [])
    assert [d["slug"] for d in docs] == ["system", "bible", "architecture", "development"]
    rows = module._records(docs)
    module._guards(rows)  # fails closed if any record breaks the contract

    assert len({r["identity"] for r in rows}) == len(rows)
    assert all(IDENTITY_RE.match(r["identity"]) for r in rows)
    assert all(len(r["content"]) <= cl.GOVDOC_READ_CHARS for r in rows)
    batches = module._batch_plan(rows, 20)
    assert len(batches) == -(-len(rows) // 20)
    assert all(0 < len(b) <= 20 for b in batches)
    assert [r for b in batches for r in b] == rows  # order preserved, nothing dropped


def test_the_writer_refuses_a_record_that_one_read_could_not_return():
    module = _runner()
    over = {
        "slug": "system",
        "identity": "knowledge:govdoc:system:1",
        "title": "Govdoc: SYSTEM.md § X — lines 1-1 [chunk 1/1]",
        "content": "x" * (cl.GOVDOC_READ_CHARS + 1),
        "start": 1,
        "end": 1,
        "bytes": 0,
    }
    with pytest.raises(AssertionError, match="truncated"):
        module._guards([over])
    over["content"] = "x" * (cl.GOVDOC_READ_CHARS - 1)
    module._guards([over])


def test_only_filters_the_plan_to_one_doc():
    module = _runner()
    docs = module._plan(REPO, ["bible"])
    assert [d["slug"] for d in docs] == ["bible"]


def test_the_manifest_check_is_offline_and_reports_drift(tmp_path):
    module = _runner()
    docs = module._plan(REPO, ["bible"])
    rows = module._records(docs)
    manifest_path = tmp_path / "govdoc_manifest.json"

    assert module.main(["--check", "--manifest", str(manifest_path), "--repo", str(REPO), "--only", "bible"]) == 0
    assert not manifest_path.exists()  # a check never writes

    manifest_path.write_text(
        module.json.dumps(module._manifest(docs, rows, REPO)), encoding="utf-8"
    )
    assert module._check(manifest_path, docs, rows) == 0

    manifest = module.json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["docs"]["bible"]["records"][0]["sha256"] = "0" * 64
    manifest_path.write_text(module.json.dumps(manifest), encoding="utf-8")
    assert module._check(manifest_path, docs, rows) == 1


def test_the_written_record_carries_the_mirror_type_and_a_stable_identity(tmp_path, monkeypatch):
    """The production emit path, with the network stubbed — never a live service."""
    from ouroboros.engram_client import EngramClient
    from ouroboros.engram_sink import EngramSink

    module = _runner()
    docs = module._plan(REPO, ["development"])
    rows = module._records(docs)

    repo = tmp_path / "repo"
    repo.mkdir()
    client = EngramClient.from_repo(repo, base_url="http://127.0.0.1:1", timeout=0.05)
    sink = EngramSink(client=client, drive_root=tmp_path, session_id="s1")
    captured: list[dict] = []
    monkeypatch.setattr(EngramSink, "_send", lambda self, record: captured.append(dict(record)) or _ok())
    monkeypatch.setattr(sink, "compact_if_idle", lambda: False)

    row = rows[0]
    sink.emit(
        "knowledge",
        title=row["title"],
        content=row["content"],
        identity=row["identity"],
        type="govdoc",
        scope="global",
        fields={"task_id": "govdoc-ingest", "topic": row["identity"], "mode": "overwrite"},
        document=True,
    )

    assert len(captured) == 1
    record = captured[0]
    assert record["type"] == "govdoc"
    assert record["identity"] == row["identity"]
    assert record["title"] == row["title"]
    assert record["content"] == row["content"]
    assert "_sanitize_document" not in record  # documents keep their newlines
    assert record["content"].count("\n") > 10


def _ok():
    from ouroboros.engram_client import EngramResult

    return EngramResult(True, status=200, data={"id": 1})
