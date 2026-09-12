"""Single source of truth for the LLM context LAYOUT of reference docs.

This module decides, in ONE place, which governance / reference docs enter the
always-on agent context and in what form, per context mode (low / max) and task
kind. Centralizing it here keeps the low/max split from drifting across the
surfaces that consume it (main task context, background consciousness, deep
self-review).

Doc matrix (agent cognition surfaces; D-ARCH unification, owner 2026-08-08):

  | doc            | max            | low                                   |
  |----------------|----------------|---------------------------------------|
  | SYSTEM / BIBLE | full (tier-0; caller-owned, never varied here)              |
  | ARCHITECTURE   | full — ALWAYS, every task class | navigation map (full sections on demand) |
  | DEVELOPMENT    | full when the caller includes dev context, else navigation map — MODE-INDEPENDENT |
  | README         | on-demand pointer (removed from always-on for all modes)     |
  | CHECKLISTS     | on-demand pointer (reviewers load their own copy)            |

ARCHITECTURE.md follows the OWNER CONTEXT MODE alone — no per-task downgrade.
Owner's motivation (recorded so it is not lost): architecture.md is Ouroboros's
capability/tools/access map; it stays resident in max even for project/evolution
work because without it the agent cannot reason about HOW to work effectively —
context economy comes from dropping DEVELOPMENT.md for project work, never
ARCHITECTURE. DEVELOPMENT.md inclusion is the caller's per-task decision and is
deliberately decoupled from the mode.

The four docs above are also MIRRORED into Engram in retrieval-sized chunks
(``chunk_doc`` / ``govdoc_record`` below; the one-shot writer is
``.ouroboros/engram-packet/govdoc_ingest.py``). A map rendered with an
``engram_slug`` names, per entry, the chunk record holding those lines, so the map
and the store cannot disagree about which record covers which lines. The mirror
never replaces the canonical file: ``read_file`` stays the lossless route and the
copy on disk stays authoritative.

D-DEV (owner decision, 2026-08-08) fixes what that per-task decision keys on:
DEVELOPMENT.md is the self-engineering handbook, so it loads exactly when the
work targets Ouroboros's own body — and the signal is the ACTIVE REPO BINDING, a
path fact, never a guess from message text (P5). ``context.py`` derives it from
``not _task_uses_external_context(task)``: a bound workspace, a subagent, or an
api/cli/scheduled surface means another codebase and gets the pointer, while a
direct-chat turn in a PROJECT ROOM with no workspace bound keeps the handbook.
Project MEMBERSHIP is deliberately not the signal — an id in a room does not mean
the work left Ouroboros's body.

The TIER-0 protected core (SYSTEM, BIBLE, identity, scratchpad, recent dialogue)
is ALWAYS full in every mode (BIBLE P1 cognitive-horizon / P4) and is declared
here as a data invariant. The knowledge index is no longer part of that core:
durable knowledge is retrieved on demand now, so the index sits in
``TIER0_RETRIEVAL_BACKED`` (a declared demotion, not a silent drop).
Memory-section SIZE (not inclusion) is governed separately by consolidation
granularity, not by this layout.

No imports from ``ouroboros.context`` (avoids a circular import); docs are read
directly via ``env.repo_path``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence, Tuple

# Protected core: always rendered in full, in every context mode. Encoded as
# data so a drift-guard test can assert no future change demotes it.
#
# `knowledge_index` left this set when durable knowledge stopped being prompt-
# resident: it is retrieved from Engram on demand (see `prompts/SYSTEM.md`
# § Memory and Context and `ouroboros/tools/engram.py`). That is a DEMOTION, so
# it is declared rather than silently dropped — the guarantee that replaced
# "always resident in full" is a DIFFERENT promise ("always retrievable, bounded,
# and disclosed when unreachable"), and a reader has to be able to see the swap.
TIER0_ALWAYS_FULL = frozenset({
    "system",
    "bible",
    "identity",
    "scratchpad",
    "recent_dialogue",
})

#: Sections that left the always-resident core and are satisfied by bounded
#: on-demand retrieval instead. Kept as data so the replacement is auditable
#: rather than an invisible deletion.
TIER0_RETRIEVAL_BACKED = frozenset({
    "knowledge_index",
})

# --------------------------------------------------------------------------- #
# The governance-doc Engram mirror (see the module docstring).
# --------------------------------------------------------------------------- #

#: The read bound one mirrored chunk must fit inside, so a SINGLE ``engram op="read"``
#: returns it whole: ``MAX_READ_CONTENT_CHARS`` (``ouroboros/tools/engram.py``), matched
#: by ``MAX_TOPIC_CHARS`` in ``ouroboros/engram_read.py``. A record over this is
#: returned TRUNCATED — and a truncated record reads exactly like a complete one,
#: which is the failure the whole bound exists to prevent.
GOVDOC_READ_CHARS = 4_000
#: Budget reserved inside that bound for a chunk's breadcrumb line (the header naming
#: the doc, the line range, the source path and the successor record). Measured worst
#: case over the four docs is under 200 chars; the reserve is deliberately generous
#: because exceeding it would truncate silently rather than fail.
GOVDOC_BREADCRUMB_CHARS = 400
#: Largest chunk BODY the chunker may emit.
GOVDOC_CHUNK_CHARS = GOVDOC_READ_CHARS - GOVDOC_BREADCRUMB_CHARS
#: Smallest body worth a record of its own. Without a floor the four docs shed dozens
#: of degenerate sub-500-char records (SYSTEM.md alone has 18) that each cost a store
#: slot and a place in the shared recency window, while carrying one sentence.
GOVDOC_MIN_FILL_CHARS = 1_500

#: The mirrored docs, by identity slug: ``slug -> (repo-relative path, file name)``.
#: The slug is part of the record identity, so these four strings are a store contract
#: and must not drift.
GOVDOC_DOCS: Mapping[str, Tuple[str, str]] = {
    "system": ("prompts/SYSTEM.md", "SYSTEM.md"),
    "bible": ("BIBLE.md", "BIBLE.md"),
    "architecture": ("docs/ARCHITECTURE.md", "ARCHITECTURE.md"),
    "development": ("docs/DEVELOPMENT.md", "DEVELOPMENT.md"),
}


def _read_doc(env: Any, rel_path: str) -> str:
    try:
        return env.repo_path(rel_path).read_text(encoding="utf-8")
    except Exception:
        return ""


@dataclass(frozen=True)
class DocChunk:
    """One retrieval-sized slice of a governance doc.

    ``start``/``end`` are 1-based inclusive source lines. ``piece`` is set when the
    slice is ONE PART of a single source line too long for the read bound: such a line
    is split at spaces and each part becomes its own record, because the alternative —
    one oversized record — is returned truncated by ``engram op="read"`` and would
    read like a complete one.
    """

    start: int
    end: int
    piece: str | None = None


def _markdown_headings(lines: List[str]) -> List[Tuple[int, str, int]]:
    """``(level, text, 1-based line)`` for every ``##``/``###``/``####`` outside a fence.

    The ONE heading definition, shared by the navigation map and the chunker: a
    second scanner with a different idea of what a heading is would mis-align the
    map's line ranges against the chunk identities it prints next to them.
    """
    out: List[Tuple[int, str, int]] = []
    in_fence = False
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            out.append((2, line[3:].strip(), i))
        elif line.startswith("### "):
            out.append((3, line[4:].strip(), i))
        elif line.startswith("#### "):
            out.append((4, line[5:].strip(), i))
    return out


def _split_long_line(line: str, cap: int) -> List[str]:
    """Split one over-long line into balanced, space-aligned parts.

    The cut lands on a space so no word is broken, and the remaining length is
    re-divided at every step so the LAST part cannot come out a stub. The parts of a
    line, read back in order with their record boundaries taken as newlines, reproduce
    that line byte-for-byte (each cut space is exactly the boundary), so nothing is
    lost across a record's parts — the total byte count of the doc is unchanged.
    """
    parts: List[str] = []
    rest = line
    while len(rest) > cap:
        parts_left = -(-len(rest) // cap)
        want = -(-len(rest) // parts_left)
        cut = rest.rfind(" ", 0, want + 1)
        if cut <= 0:
            cut = want
        parts.append(rest[:cut])
        rest = rest[cut + 1:] if rest[cut: cut + 1] == " " else rest[cut:]
    parts.append(rest)
    return parts


def chunk_doc(text: str) -> List[DocChunk]:
    """Partition a governance doc into chunks that each fit the read bound.

    Boundaries, in order of precedence:

    * line 1;
    * every ``##`` heading that follows at least :data:`GOVDOC_MIN_FILL_CHARS` of the
      running chunk — sub-headings stay with their parent section, so a chunk is a
      whole thought rather than an arbitrary window;
    * a single line longer than :data:`GOVDOC_CHUNK_CHARS`, split at spaces;
    * for a span that would exceed the cap, the LAST blank line in its final quarter —
      so chunks break between paragraphs, never mid-sentence.

    The result is a total, non-overlapping, in-order partition: chunk ``n+1`` starts on
    the line after chunk ``n`` ends, and every heading in the doc falls inside exactly
    one chunk (which is what lets the navigation map name the record per entry).
    """
    lines = text.splitlines()
    total = len(lines)
    if not total:
        return []
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def joined(first: int, last: int) -> int:
        """Length of ``"\\n".join(lines[first-1:last])`` without building it."""
        return starts[last] - starts[first - 1] + (last - first)

    breaks = {i for level, _text, i in _markdown_headings(lines) if level == 2}
    breaks.add(1)

    out: List[DocChunk] = []
    cur, i = 1, 1
    while i <= total:
        if len(lines[i - 1]) > GOVDOC_CHUNK_CHARS:
            if cur != i:
                out.append(DocChunk(cur, i - 1))
            for piece in _split_long_line(lines[i - 1], GOVDOC_CHUNK_CHARS):
                out.append(DocChunk(i, i, piece))
            cur, i = i + 1, i + 1
            continue
        if i in breaks and i != cur and joined(cur, i - 1) >= GOVDOC_MIN_FILL_CHARS:
            out.append(DocChunk(cur, i - 1))
            cur = i
            continue
        if joined(cur, i) > GOVDOC_CHUNK_CHARS:
            floor = cur + max(1, int((i - cur) * 0.75))
            cut = next(
                (j for j in range(i - 1, floor - 1, -1) if not lines[j - 1].strip()),
                None,
            )
            end = (cut - 1) if cut else (i - 1)
            if end < cur:
                end = i - 1
            out.append(DocChunk(cur, end))
            cur = end + 1
            continue
        i += 1
    if cur <= total:
        out.append(DocChunk(cur, total))
    return out


def chunk_body(lines: List[str], chunk: DocChunk) -> str:
    """The chunk's text: a source line range, or the split part of one long line."""
    if chunk.piece is not None:
        return chunk.piece
    return "\n".join(lines[chunk.start - 1: chunk.end])


def chunk_ordinal_for(chunks: Sequence[DocChunk], line: int) -> int:
    """The 1-based ordinal of the chunk CONTAINING ``line`` (an addressable key).

    Total by construction: the chunks partition the doc, so every line — and therefore
    every heading the navigation map lists — belongs to exactly one of them. When
    several chunks share a start line (the parts of a split source line) the last of
    them is named, which is still a chunk that contains the line.
    """
    return max(1, bisect.bisect_right([c.start for c in chunks], line))


def govdoc_identity(slug: str, ordinal: int) -> str:
    """The Engram ``topic_key`` one mirrored chunk is addressed by.

    ``ordinal`` is the chunk's 1-based position in its doc. Deliberately NOT the
    start line: a split source line yields several chunks that share a start line and
    a key built from it would make them upsert over each other.
    """
    return f"knowledge:govdoc:{slug}:{ordinal}"


def _governing_heading(lines: List[str], chunk: DocChunk) -> str:
    """The section a chunk belongs to, for the record title."""
    headings = _markdown_headings(lines)
    before = [text for _level, text, line in headings if line <= chunk.start]
    if before:
        return before[-1]
    inside = [text for _level, text, line in headings if chunk.start <= line <= chunk.end]
    if inside:
        return inside[0]
    return "(preamble — no heading)"


def govdoc_record(
    slug: str,
    *,
    lines: List[str],
    chunks: Sequence[DocChunk],
    ordinal: int,
) -> Tuple[str, str]:
    """``(title, content)`` for one chunk — the exact record the writer emits.

    ``content`` is a breadcrumb line (doc, section, line range, chunk index, source
    path, successor identity) then the chunk body. The breadcrumb is what makes a
    record self-locating and walkable: a read returns one chunk, and the successor
    named here is how a reader gets the next one without guessing.
    """
    rel_path, file_name = GOVDOC_DOCS[slug]
    chunk = chunks[ordinal - 1]
    total = len(chunks)
    heading = _governing_heading(lines, chunk)
    title = (
        f"Govdoc: {file_name} § {heading} — lines {chunk.start}-{chunk.end}"
        f" [chunk {ordinal}/{total}]"
    )
    breadcrumb = f"{title}. Source: {rel_path}."
    if ordinal < total:
        breadcrumb += f" Next: {govdoc_identity(slug, ordinal + 1)}"
    return title, f"{breadcrumb}\n\n{chunk_body(lines, chunk)}"


def generate_doc_nav_map(
    text: str,
    *,
    title: str,
    rel_path: str,
    engram_slug: str = "",
) -> str:
    """Build a compact, fence-aware navigation map of a markdown doc.

    Lists every ``##`` through ``####`` heading with its inclusive line range
    so the agent knows what exists and where, and can pull the full section on demand via
    ``read_file(root="system_repo", path=rel_path, start_line=A, max_lines=N)``.
    A parent's range includes its complete descendant group, so parent and child
    ranges intentionally overlap. This is a lossless index
    (P1: no silent truncation) — the single canonical file on disk is unchanged.

    ``engram_slug`` — one of :data:`GOVDOC_DOCS` — additionally ties the map to that
    doc's Engram mirror: the header gains the retrieval sentence and each entry gains
    the identity of the chunk that CONTAINS it, so the map and the store cannot
    disagree about which record holds which lines. Left empty (the default) the output
    is byte-identical to the file-only map, which is what the existing callers rely on.
    """
    lines = text.splitlines()
    total = len(lines)
    headings = _markdown_headings(lines)

    out = [
        f"## {title} (navigation map)",
        "",
        f"Full text is NOT inlined to keep the working context window fit. Read any "
        f"section on demand with `read_file(root=\"system_repo\", path=\"{rel_path}\", "
        f"start_line=A, max_lines=N)` (untruncated). Ranges are inclusive; a "
        f"parent includes its complete descendant group, and `max_lines=B-A+1` "
        f"for `lines A-B`. Sections:",
    ]
    chunks: List[DocChunk] = []
    if engram_slug:
        chunks = chunk_doc(text)
        out += [
            "",
            f"Ingested: this doc is also mirrored into Engram, one record per chunk of "
            f"≤ {GOVDOC_CHUNK_CHARS} chars — `{title} § <section>` is addressed as "
            f"`knowledge:govdoc:{engram_slug}:<n>`, and each entry below names the one "
            f"holding its lines. Fetch it with the `engram` tool (op=\"search\" on the "
            f"title, then op=\"read\" the observation_id): one read returns a whole "
            f"chunk untruncated and names its successor. `{rel_path}` stays canonical.",
        ]
    out.append("")
    if not headings:
        out.append(f"- (no `##`/`###`/`####` headings; read `{rel_path}` directly)")
    for idx, (level, htitle, lineno) in enumerate(headings):
        end = total
        for later_level, _later_title, later_lineno in headings[idx + 1:]:
            if later_level <= level:
                end = later_lineno - 1
                break
        indent = "  " * (level - 2)
        entry = f"{indent}- {htitle} — lines {lineno}-{end}"
        if engram_slug:
            entry += (
                f" · record "
                f"{govdoc_identity(engram_slug, chunk_ordinal_for(chunks, lineno))}"
            )
        out.append(entry)
    return "\n".join(out)


def architecture_context_section(
    env: Any,
    *,
    context_mode: str,
    text: str | None = None,
) -> str:
    """ARCHITECTURE.md: full in max, navigation map in low. Empty if unreadable."""
    if text is None:
        text = _read_doc(env, "docs/ARCHITECTURE.md")
    if not text.strip():
        return ""
    if context_mode == "low":
        rel_path, doc_title = GOVDOC_DOCS["architecture"]
        return generate_doc_nav_map(
            text,
            title=doc_title,
            rel_path=rel_path,
            engram_slug="architecture",
        )
    return "## ARCHITECTURE.md\n\n" + text


def reference_doc_sections(
    env: Any,
    *,
    context_mode: str,
    include_development: bool,
    architecture_text: str | None = None,
    development_text: str | None = None,
) -> List[str]:
    """Return the reference-doc parts for the always-on static block.

    SYSTEM.md and BIBLE.md are tier-0 and added by the caller; this owns
    ARCHITECTURE / DEVELOPMENT / README / CHECKLISTS per the doc matrix. Anything
    not inlined is named in a single visible on-demand pointer (P1: no silent
    omission).

    ``context_mode`` is the OWNER context mode and decides only the ARCHITECTURE
    form (full in max, nav map in low — D-ARCH, owner 2026-08-08).
    ``include_development`` is the caller's mode-independent decision whether the
    self-engineering handbook is inline (self-body/self-mod/evolution work) or a
    navigation map (project tasks — folder or not — and external surfaces). The map
    is still a visible, line-addressed form of the doc (P1) — it replaces a bare
    name with an index whose entries name the doc's Engram records.
    """
    parts: List[str] = []
    on_demand: List[str] = []

    arch_section = architecture_context_section(
        env,
        context_mode=context_mode,
        text=architecture_text,
    )
    if arch_section:
        parts.append(arch_section)

    dev_text = (
        development_text
        if development_text is not None
        else _read_doc(env, "docs/DEVELOPMENT.md")
    )
    if dev_text.strip():
        if include_development:
            parts.append("## DEVELOPMENT.md\n\n" + dev_text)
        else:
            rel_path, doc_title = GOVDOC_DOCS["development"]
            parts.append(
                generate_doc_nav_map(
                    dev_text,
                    title=doc_title,
                    rel_path=rel_path,
                    engram_slug="development",
                )
            )

    # README (user-facing) and CHECKLISTS (reviewers load their own copy) are not
    # inlined in the agent context in any mode.
    on_demand.extend(["README.md", "docs/CHECKLISTS.md"])

    if on_demand:
        listing = ", ".join(f"`{p}`" for p in on_demand)
        parts.append(
            "## Reference docs available on demand\n\n"
            f"Not inlined in the working context: {listing}. "
            "Read them in full (untruncated) with `read_file(root=\"system_repo\", path=...)` when relevant."
        )
    return parts
