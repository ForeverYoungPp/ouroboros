"""AC12: the two SSOT documents carry the memory rule, and BIBLE P1 is not weakened.

The seed requires the rule to live in `prompts/SYSTEM.md` (behaviour) *and*
`BIBLE.md` Principle 1 (constitution), because P1 named the mechanisms this change
moves: it lists `identity.md, scratchpad, chat history` as memory and biography,
and it forbids any compression or caching strategy from destroying
recoverability.

The negative assertions here matter as much as the positive ones. Updating a
constitution to match a change is legitimate; deleting the clause it rested on is
how a governance guarantee disappears without anyone noticing.
"""

from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_MD = REPO / "prompts" / "SYSTEM.md"
BIBLE_MD = REPO / "BIBLE.md"


@pytest.fixture(scope="module")
def system_text() -> str:
    return SYSTEM_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bible_text() -> str:
    return BIBLE_MD.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse Markdown line wrapping.

    These are prose guarantees written into Markdown, so a phrase routinely
    straddles a line break. Asserting on the raw text makes the test fail on
    reflow rather than on meaning.
    """
    return " ".join(text.split())


def _p1(bible_text: str) -> str:
    """Principle 1's body, whitespace-normalised."""
    return _flat(bible_text.split("## Principle 1: Continuity", 1)[1].split("## Principle 2", 1)[0])


def _memory_section(system_text: str) -> str:
    """The `## Memory and Context` body, up to the next top-level heading."""
    after = system_text.split("## Memory and Context", 1)[1]
    return _flat(after.split("\n## ", 1)[0])


# --------------------------------------------------------------------------- #
# prompts/SYSTEM.md — the behaviour rule
# --------------------------------------------------------------------------- #


def test_memory_and_context_section_still_exists(system_text):
    assert "## Memory and Context" in system_text


def test_durable_knowledge_now_points_at_engram(system_text):
    assert "### Durable Knowledge (Engram)" in system_text


def test_old_local_only_knowledge_wording_is_gone(system_text):
    """The sentence that told the agent knowledge was a local prompt section."""
    assert "Use knowledge files for stable operational facts" not in system_text


def test_retrieval_is_self_directed(system_text):
    """The model decides what to pull in; it is not auto-injected."""
    section = _memory_section(system_text)
    assert "I retrieve it myself" in section or "retrieve it myself" in section
    assert "on demand" in section


def test_progressive_disclosure_is_spelled_out(system_text):
    section = _memory_section(system_text)
    assert "progressive disclosure" in section.lower()
    for step in ("Discover", "Neighbour", "Full"):
        assert step in section, f"missing disclosure layer: {step}"
    # And the budget rule that keeps it from becoming one big pull.
    assert "never cost more than" in section


def test_engram_stores_memories_not_work_items(system_text):
    """C18 in the prompt: a to-do is not a memory."""
    section = _memory_section(system_text)
    assert "not work items" in section
    assert "backlog" in section


def test_memory_gaps_are_disclosed_in_the_prompt(system_text):
    """P1: the gap must stay visible, not merely computable."""
    assert "### Memory Gaps" in system_text


def test_reserved_internal_name_still_warned_about(system_text):
    assert "reserved internal name" in system_text


def test_the_retired_local_write_claim_is_gone(system_text):
    """S2 executed: the prompt must not still say the local store is the record.

    A behaviour rule that lags the code is worse than no rule — the agent would
    keep trusting a store that stopped being written, and read stale text as
    current. This is the sentence that became false when the stop landed.
    """
    flat = _flat(system_text)
    assert "still records locally" not in flat
    assert "write-of-record" not in flat


def test_the_local_file_is_described_as_an_archive(system_text):
    """The pre-switch files stay readable, so the prompt has to date them."""
    section = _memory_section(system_text)
    assert "no longer written" in section
    assert "archive" in section
    assert "may be older" in section


def test_unreachable_is_distinguished_from_absent(system_text):
    """C15 in the prompt: 'no service' must never read as 'I never learned this'."""
    section = _memory_section(system_text)
    assert "unreachable" in section
    assert "never read an unreachable store as" in section


def test_a_deferred_append_is_described(system_text):
    """C17: the no-loss path is part of the behaviour rule, not only the code."""
    section = _memory_section(system_text)
    assert "deferred" in section
    assert "spooled" in section


# --------------------------------------------------------------------------- #
# BIBLE.md — the constitutional clarification
# --------------------------------------------------------------------------- #


def test_principle_1_has_the_carrier_clarification(bible_text):
    p1 = _p1(bible_text)
    assert "Continuity is carried by durable stores, not by the prompt" in p1


def test_clarification_forbids_destroying_recoverability(bible_text):
    p1 = _p1(bible_text)
    assert "the prompt may shrink, the record may not" in p1
    assert "unrecoverable" in p1


@pytest.mark.parametrize(
    "clause",
    [
        "identity.md, scratchpad, chat history, git log",
        "Memory loss is partial death",
        "remain visible for reflection",
        "No optimization,",
        "compression, or caching strategy may destroy the ability to recover",
        "Cognitive horizon is part of continuity",
        "Trans-interface continuity",
        "Observability is part of continuity",
        "Process memory",
    ],
)
def test_principle_1_original_clauses_are_not_weakened(bible_text, clause):
    """The clarification must clarify, not replace."""
    p1 = _p1(bible_text)
    assert clause in p1, f"P1 clause disappeared: {clause!r}"


def test_principle_count_is_unchanged(bible_text):
    """No new principle was invented: this is a clarification, not an amendment."""
    assert bible_text.count("\n## Principle ") == 14


def test_the_conflict_rule_keeps_both_sides(system_text):
    """C4 in the prompt: a verdict is recorded, never applied (BIBLE P1)."""
    section = _memory_section(system_text)
    assert "both survive" in section
    assert "I record a verdict, I never apply one" in section
    assert "no memory is deleted" in section
    assert "not_conflict" in section
    assert "finding for the owner" in section


def test_the_working_memory_split_is_described(system_text):
    """S3's resolution in the prompt: mirrored to Engram, read from the local window."""
    section = _memory_section(system_text)
    assert "mirrored into Engram" in section
    assert "working state, not reference material" in section
