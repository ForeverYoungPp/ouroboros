"""EXPERIMENT n6 — keep version churn out of the shared system prompt prefix.

MEASURED CAUSE.  Cross-task reuse of a round-1 request dies inside system
block[0] for 97% (dev) / 93% (test) of the billed token weight, and block[0] is
otherwise byte-identical across tasks (its share against the best previous
same-route task is p25 = p50 = p75 = p90 = 1.000).  The churn is three things, of
which the largest is the project version:

    >= 0.31 of the weight  "# Ouroboros v6.114.7 — Architecture & Reference"
                           (also v6.114.6 / .5 / .4 / .1 / .0) — the title line of
                           the INLINED ARCHITECTURE.md, ~11% into the request.
                           Ouroboros bumps VERSION between tasks, so one changed
                           digit costs the following ~89% of the prompt.
    0.136                  "## ARCHITECTURE.md (navigation map)" vs the full text
                           (low renders the nav map, max inlines 596KB).
    0.090                  "## DEVELOPMENT.md" inline vs the on-demand pointer.

This module handles the first one only.  It CARRIES THE VERSION LINE to the tail
of the request instead of dropping it: nothing is deleted (the benchmark's P1
gate requires the original system text's character multiset to survive across
"sent" + "relocated"), the version stays visible to the model, and the bytes
before it become identical for every task on a route.

The other two are deliberate rendering choices (D-ARCH, D-DEV) and need an owner
decision; they are not touched here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Private marker on the block that ``project`` relocated.  It is (a) how
#: ``ContextFitPlan.reproject_transcript`` finds and re-derives the block on a
#: context-mode switch, (b) how owner-evidence readers tell relocated system text
#: apart from the owner's own words, and (c) stripped before dispatch by
#: ``llm._physical_candidate`` so the provider never sees it.
RELOCATED_KEY = "_system_relocated"

#: Section marker whose first body line carries the project version.  Anchoring
#: to the section is what keeps this precise: a level-1 line is moved ONLY when it
#: is the first line of the ARCHITECTURE document, so a fenced example or a prose
#: mention like "# Ouroboros v6.1 (see also)" is left alone.
_ARCH_SECTION = "## ARCHITECTURE.md"

#: A visible pointer left at the removal site (BIBLE P1: relocation, not silent
#: truncation).  Constant, so it costs the shared prefix nothing.
_POINTER = "_(document title relocated to the end of this request)_\n"


def _first_body_line(text: str, section_start: int) -> Tuple[int, int, str]:
    """Span of the first non-empty line after ``section_start``'s own line."""
    cursor = text.find("\n", section_start)
    if cursor < 0:
        return -1, -1, ""
    while cursor < len(text):
        start = cursor + 1
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        line = text[start:end]
        if line.strip():
            return start, end, line
        cursor = end
    return -1, -1, ""


def _is_version_title(line: str) -> bool:
    """``# Ouroboros v<digit>…`` — the document title, not a prose mention."""
    stripped = line.strip()
    if not stripped.startswith("# "):
        return False
    rest = stripped[2:].lstrip()
    if not rest.startswith("Ouroboros"):
        return False
    rest = rest[len("Ouroboros"):].lstrip()
    return rest.startswith("v") and rest[1:2].isdigit()


def _split_version_lines(text: str) -> Tuple[str, str]:
    """Return ``(kept, moved)`` for the ARCHITECTURE title line.

    ``kept`` keeps every original character except the moved line's, which
    ``moved`` carries verbatim; a constant pointer is ADDED at the removal site
    (the benchmark gate only forbids losing characters, never adding them).
    """
    section = text.find(_ARCH_SECTION)
    if section < 0:
        return text, ""
    start, end, line = _first_body_line(text, section)
    if start < 0 or not _is_version_title(line):
        return text, ""
    tail = "\n" if end < len(text) else ""
    moved = line + tail
    kept = text[:start] + _POINTER + text[end + len(tail):]
    return kept, moved


def relocated_block(text: str) -> Dict[str, Any]:
    """The tagged text block that carries relocated system bytes."""
    return {"type": "text", "text": text, RELOCATED_KEY: True}


def is_relocated_block(block: Any) -> bool:
    """True for a block that ``project`` relocated out of the system prefix."""
    return isinstance(block, dict) and bool(block.get(RELOCATED_KEY))


def strip_relocated(content: Any) -> Any:
    """Drop relocated blocks from owner-visible reads of a turn's content."""
    if not isinstance(content, list):
        return content
    kept = [block for block in content if not is_relocated_block(block)]
    if len(kept) == len(content):
        return content
    if not kept:
        return ""
    return kept


def project(system_content: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return ``(system_blocks_to_send, tail_messages)``.

    Only the static block is touched; the version title it carries travels in a
    tagged trailing user message so the prefix in front of it can be reused.
    """
    if not isinstance(system_content, list) or not system_content:
        return list(system_content or []), []
    sent: List[Dict[str, Any]] = []
    moved_parts: List[str] = []
    for index, block in enumerate(system_content):
        if index != 0 or not isinstance(block, dict):
            sent.append(dict(block) if isinstance(block, dict) else block)
            continue
        text = str(block.get("text") or "")
        kept, moved = _split_version_lines(text)
        new_block = dict(block)
        new_block["text"] = kept
        sent.append(new_block)
        if moved:
            moved_parts.append(moved)
    if not moved_parts:
        return sent, []
    tail: List[Dict[str, Any]] = [
        {"role": "user", "content": [relocated_block("".join(moved_parts))]}
    ]
    return sent, tail
