"""Reading-side contract tests: what the main chat prompt does and does not carry.

Covers the seed's Phase-3 acceptance criteria
(``.ouroboros/seed-engram-memory.yaml``):

- AC9  ``## Dialogue History`` leaves the prompt, but the consolidation data and
       its cursor survive on disk (the layer is demoted, not deleted)
- AC10 the four derived-experience sections stop being injected, and the
       assembly shrinks by the amount those sections occupied
- AC11 the governance / runtime sections are still resident — the net subtraction
       must not take the wrong things with it
- the durable-gap disclosure keeps being computed even though its section is gone

Two deliberate test-design points, both learned the hard way against the live drive:

* Assert on **content and on builder output**, not on bare titles. ``## Recent
  chat`` renders raw conversation, and owner messages contain ``##`` headings of
  their own — a substring scan for ``## Improvement Backlog`` reports PRESENT on a
  task type that never injects it.
* ``## Improvement Backlog`` is **gated**, not removed: evolution and
  deep_self_review genuinely need it as action input. Getting this wrong would
  look like a clean AC10 pass while starving self-evolution.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from ouroboros import context as C
from ouroboros.memory import Memory

# Distinctive sentinels: if any of these reach the prompt, the section was injected.
INDEX_SENTINEL = "SENTINEL-KNOWLEDGE-INDEX-ENTRY"
PATTERN_SENTINEL = "SENTINEL-KNOWLEDGE-PATTERN-ENTRY"
BLOCK_SENTINEL = "SENTINEL-DIALOGUE-BLOCK-TEXT"
DEEP_REVIEW_SENTINEL = "SENTINEL-DEEP-REVIEW-TEXT"
BACKLOG_SENTINEL = "SENTINEL-BACKLOG-ITEM"

REMOVED_TITLES = (
    "## Knowledge base",
    "## Project knowledge",
    "## Known error patterns",
    "## Last Deep Self-Review",
    "## Dialogue History",
    "## Legacy Dialogue Summary",
)

# AC11: these must survive the subtraction.
KEPT_TITLES = (
    "BIBLE.md",
    "ARCHITECTURE.md",
    "## Identity",
    "## Environment Profile",
    "## Scratchpad",
    "## Recent chat",
    "## Recent chat coverage",
)


@pytest.fixture()
def drive(tmp_path: pathlib.Path):
    """A self-contained drive + repo pair with every reading-side source present."""
    root = tmp_path / "drive"
    memory = root / "memory"
    (memory / "knowledge").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)

    (repo / "prompts" / "SYSTEM.md").write_text("# base prompt\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("# BIBLE.md — Constitution\n", encoding="utf-8")
    (repo / "docs" / "ARCHITECTURE.md").write_text(
        "## ARCHITECTURE.md\n\nbody\n", encoding="utf-8"
    )

    (memory / "identity.md").write_text("I am a test identity.", encoding="utf-8")
    (memory / "WORLD.md").write_text("host: test", encoding="utf-8")
    (memory / "scratchpad.md").write_text("scratchpad body", encoding="utf-8")
    (memory / "knowledge" / "index-full.md").write_text(INDEX_SENTINEL, encoding="utf-8")
    (memory / "knowledge" / "patterns.md").write_text(PATTERN_SENTINEL, encoding="utf-8")
    (memory / "knowledge" / "improvement-backlog.md").write_text(
        "# Improvement Backlog\n\n### item\n- status: open\n- summary: " + BACKLOG_SENTINEL + "\n",
        encoding="utf-8",
    )
    (memory / "deep_review.md").write_text(DEEP_REVIEW_SENTINEL, encoding="utf-8")
    (memory / "dialogue_blocks.json").write_text(
        json.dumps([{"type": "block", "content": BLOCK_SENTINEL, "ts": "2026-01-01"}]),
        encoding="utf-8",
    )
    (memory / "dialogue_meta.json").write_text(
        json.dumps({"last_consolidated_offset": 0, "chat_log_signature": {}}), encoding="utf-8"
    )
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "chat.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"ts": "2026-01-01T00:00:00+00:00", "session_id": "s1", "direction": "owner",
                 "chat_id": 1, "user_id": 1, "text": "owner message one"},
                {"ts": "2026-01-01T00:00:01+00:00", "session_id": "s1", "direction": "assistant",
                 "chat_id": 1, "user_id": 1, "text": "assistant reply one"},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    env = SimpleNamespace(
        repo_dir=repo,
        drive_root=root,
        budget_drive_root=root,
        branch_dev="ouroboros",
        repo_path=lambda rel: repo / rel,
        drive_path=lambda rel: root / rel,
    )
    return {"env": env, "repo": repo, "root": root, "memory": Memory(drive_root=root, repo_dir=repo)}


def _assemble(drive, task_type: str = "direct_chat"):
    task = {"id": "t-reading-side", "type": task_type, "chat_id": 1}
    core = C._capture_context_core(drive["env"], drive["memory"], task, None, None)
    text = "\n\n".join(
        part for part in (core.semi_stable_text or "", core.dynamic_text or "") if part
    )
    total = sum(
        len(part or "")
        for part in (
            core.base_prompt,
            core.bible_md,
            core.architecture_md,
            core.development_md,
            core.semi_stable_text,
            core.dynamic_text,
        )
    )
    return core, text, total


# --------------------------------------------------------------------------- #
# AC10 — the derived-experience sections leave the prompt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sentinel", [INDEX_SENTINEL, PATTERN_SENTINEL, BLOCK_SENTINEL, DEEP_REVIEW_SENTINEL])
def test_derived_content_never_reaches_the_main_chat_prompt(drive, sentinel):
    """Content-level, not title-level: a title check alone can be fooled."""
    _, text, _ = _assemble(drive)
    assert sentinel not in text


@pytest.mark.parametrize("title", REMOVED_TITLES)
def test_removed_titles_are_absent_for_main_chat(drive, title):
    _, text, _ = _assemble(drive)
    assert title not in text


def test_knowledge_builder_returns_nothing_for_the_main_chat_path(drive):
    """Structural: the builder itself yields no derived sections when opted out."""
    assert C.build_knowledge_sections(
        drive["env"], project_id="", include_derived_knowledge=False
    ) == []


def test_dialogue_history_section_is_gone_from_the_volatile_partition(drive):
    sections = C.build_memory_sections(drive["memory"], "volatile")
    headers = [s.splitlines()[0] for s in sections]
    assert any(h.startswith("## Scratchpad") for h in headers)
    assert not any(h.startswith("## Dialogue History") for h in headers)
    assert not any(h.startswith("## Legacy Dialogue Summary") for h in headers)


def test_enlarging_derived_sources_does_not_grow_the_reading_side(drive):
    """AC10 in its strongest form: the removed sources are DISCONNECTED.

    Asserting that this fixture's copies are absent only shows the fixture is
    small. Bloating every removed source and requiring the reading side not to
    move by a single byte is what actually proves the wiring is gone — a
    regression that re-adds any of these calls fails immediately.
    """
    core_before, _, _ = _assemble(drive)
    baseline = len(core_before.semi_stable_text or "") + len(core_before.dynamic_text or "")

    (drive["root"] / "memory" / "knowledge" / "index-full.md").write_text("I" * 20_000, encoding="utf-8")
    (drive["root"] / "memory" / "knowledge" / "patterns.md").write_text("P" * 20_000, encoding="utf-8")
    (drive["root"] / "memory" / "dialogue_blocks.json").write_text(
        json.dumps([{"content": "D" * 20_000}]), encoding="utf-8"
    )
    (drive["root"] / "memory" / "deep_review.md").write_text("R" * 20_000, encoding="utf-8")

    core_after, _, _ = _assemble(drive)
    after = len(core_after.semi_stable_text or "") + len(core_after.dynamic_text or "")

    assert after == baseline, (
        f"reading side grew by {after - baseline} chars when the derived sources "
        "were enlarged — one of them is wired back into the prompt"
    )


def test_reading_side_delta_is_measured_without_block0(drive):
    """AC10's scope, made explicit: governance edits must not move this number.

    BLOCK[0] (SYSTEM.md / BIBLE.md / ARCHITECTURE.md / DEVELOPMENT.md) is edited
    by AC12 on purpose. A criterion that included it would report AC12's work as
    AC10's failure — which is exactly how the live measurement was misread twice.
    """
    core, _, _ = _assemble(drive)
    block0 = sum(
        len(part or "")
        for part in (core.base_prompt, core.bible_md, core.architecture_md, core.development_md)
    )
    reading_side = len(core.semi_stable_text or "") + len(core.dynamic_text or "")
    assert block0 > 0 and reading_side > 0
    # Governance text lives in block0, so it is not part of the reading-side budget.
    assert "BIBLE.md" not in (core.semi_stable_text or "") + (core.dynamic_text or "")


# --------------------------------------------------------------------------- #
# AC10c — the backlog gate is NOT the same thing as the removed sections
# --------------------------------------------------------------------------- #


def test_backlog_is_skipped_for_direct_chat_but_injected_for_evolution(drive):
    """KEPT-1: the backlog is the self-evolution queue, not prompt decoration."""
    _, chat_text, _ = _assemble(drive, "direct_chat")
    _, evo_text, _ = _assemble(drive, "evolution")
    assert BACKLOG_SENTINEL not in chat_text
    assert BACKLOG_SENTINEL in evo_text


# --------------------------------------------------------------------------- #
# AC11 — the net subtraction must not take the wrong things
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("title", KEPT_TITLES)
def test_governance_and_runtime_sections_are_still_resident(drive, title):
    """Each kept item is asserted where it actually lives, not where it is easy.

    Governance lives in the shared prefix (block[0]); the rest live in the
    semi-stable / dynamic bodies. Conflating the two is how a test passes while
    the prompt silently lost its constitution.
    """
    core, body, _ = _assemble(drive)
    block0 = "\n\n".join(
        part
        for part in (core.base_prompt, core.bible_md, core.architecture_md, core.development_md)
        if part
    )
    haystack = block0 + "\n\n" + body
    assert title in haystack, f"{title!r} disappeared from the assembled prompt"


def test_block0_shared_prefix_keeps_governance(drive):
    core, _, _ = _assemble(drive)
    assert "BIBLE.md" in core.bible_md
    assert "ARCHITECTURE.md" in core.architecture_md


def test_scratchpad_still_carries_its_body(drive):
    """S3 (stopping scratchpad) is a later phase; Phase 3 keeps it whole."""
    _, text, _ = _assemble(drive)
    assert "scratchpad body" in text


# --------------------------------------------------------------------------- #
# AC9 — demoted, not deleted
# --------------------------------------------------------------------------- #


def test_consolidation_artifacts_survive_on_disk(drive):
    """AC9: leaving the prompt is not the same as deleting the layer."""
    _assemble(drive)
    blocks = drive["root"] / "memory" / "dialogue_blocks.json"
    assert blocks.exists()
    assert BLOCK_SENTINEL in blocks.read_text(encoding="utf-8")
    meta = json.loads((drive["root"] / "memory" / "dialogue_meta.json").read_text(encoding="utf-8"))
    # The cursor field must survive: the recording flow is unchanged, only the
    # rendering left the prompt (C8 / AC9).
    assert "last_consolidated_offset" in meta


def test_durable_gap_projection_still_runs_without_its_section(drive):
    """The 'history has a hole' disclosure outlives the section that used to show it."""
    gaps: list = []
    sections = C.build_memory_sections(
        drive["memory"], "volatile", durable_dialogue_gaps_out=gaps
    )
    assert not any(s.startswith("## Dialogue History") for s in sections)
    # A plain block carries no gap; a [MEMORY GAP] block must still be reported.
    (drive["root"] / "memory" / "dialogue_blocks.json").write_text(
        json.dumps([{"type": "gap", "gap_id": "g1", "content": "[MEMORY GAP] lost span"}]),
        encoding="utf-8",
    )
    gaps_after: list = []
    C.build_memory_sections(drive["memory"], "volatile", durable_dialogue_gaps_out=gaps_after)
    assert any(g.get("gap_id") == "g1" for g in gaps_after)
    assert any(g.get("kind") == "durable_consolidation_gap" for g in gaps_after)


# --------------------------------------------------------------------------- #
# the gap disclosure that replaced the narrative section
# --------------------------------------------------------------------------- #


def test_no_gap_section_when_history_is_intact(drive):
    """The disclosure is emitted only when there is something to disclose."""
    sections = C.build_memory_sections(drive["memory"], "volatile")
    assert not any(s.startswith("## Memory Gaps") for s in sections)


def test_gap_section_is_rendered_when_a_gap_exists(drive):
    """BIBLE P1: a gap is a fact in memory, not a silent absence."""
    (drive["root"] / "memory" / "dialogue_blocks.json").write_text(
        json.dumps([{"gap_id": "g-abc", "content": "[MEMORY GAP] a span is unknowable"}]),
        encoding="utf-8",
    )
    sections = C.build_memory_sections(drive["memory"], "volatile")
    gap_sections = [s for s in sections if s.startswith("## Memory Gaps")]
    assert len(gap_sections) == 1
    assert "[MEMORY GAP]" in gap_sections[0]
    assert "g-abc" in gap_sections[0]
    # ...and the narrative it used to ride in on is still gone.
    assert not any(s.startswith("## Dialogue History") for s in sections)


def test_gap_section_is_bounded(drive):
    """A drive with many gaps must not turn disclosure into a second corpus."""
    (drive["root"] / "memory" / "dialogue_blocks.json").write_text(
        json.dumps(
            [{"gap_id": f"g{i}", "content": "[MEMORY GAP] " + "x" * 500} for i in range(50)]
        ),
        encoding="utf-8",
    )
    sections = C.build_memory_sections(drive["memory"], "volatile")
    gap_section = next(s for s in sections if s.startswith("## Memory Gaps"))
    assert len(gap_section) < 4_000
    assert gap_section.count("[MEMORY GAP]") <= 10
