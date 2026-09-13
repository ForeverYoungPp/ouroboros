"""The cacheable system prefix must stay free of release-version churn.

Measured 2026-09-13 (dry build, Engram neutralised): in `max` mode the ARCHITECTURE title
carried the release version at offset 119,468 of the system content, and the projection
moved it out — the PROJECTED system message keeps only the health row's `version sync`
token, which rides the volatile tail (post-horizon). In `low` mode the nav map carries no
title copy at all, so the same rule holds with no relocation needed.

These pins keep that true: the projection is the ONE mechanism that takes version text out
of the prefix, and it must keep moving it (BIBLE P1: relocation, not silent truncation).
"""

from __future__ import annotations

from ouroboros.system_projection import (
    is_relocated_block,
    strip_relocated,
)
from ouroboros.system_projection import (
    project as project_system_content,
)


def _system_with_version(version: str = "6.114.37"):
    """The corpus (block 0) carries the ARCH title, exactly as the live assembly does.

    NOTE the structural boundary this fixture encodes: ``project`` inspects ONLY the first
    system block, so a version token living in a later block would never be relocated. The
    live assembly satisfies that (the ARCHITECTURE section is rendered inside block 0).
    """
    return [
        {
            "type": "text",
            "text": (
                "## ARCHITECTURE.md\n\n"
                f"# Ouroboros v{version} — Architecture & Reference\n\nbody\n"
            ),
        },
        {"type": "text", "text": "## Health Invariants\n\n- OK: version sync\n"},
    ]


def test_the_projection_moves_version_text_out_of_the_prefix():
    sent, relocated = project_system_content(_system_with_version())

    prefix = "\n".join(str(b.get("text") or "") for b in sent)
    tail = "\n".join(
        str(b.get("text") or "")
        for message in relocated
        for b in (message.get("content") or [])
    )

    assert "6.114.37" not in prefix, "a release version must not stay in the cacheable prefix"
    assert "6.114.37" in tail, "relocation must MOVE the bytes, never drop them"
    assert any(is_relocated_block(b) for m in relocated for b in (m.get("content") or []))


def test_the_removal_site_keeps_a_visible_pointer():
    sent, _ = project_system_content(_system_with_version())

    prefix = "\n".join(str(b.get("text") or "") for b in sent)

    assert "relocated to the end of this request" in prefix, (
        "P1: the removal site must say where the bytes went"
    )


def test_stripping_relocated_blocks_leaves_owner_reads_clean():
    _, relocated = project_system_content(_system_with_version())
    content = [b for m in relocated for b in (m.get("content") or [])]

    # All-relocated content strips to the empty string (nothing to show the owner).
    assert strip_relocated(content) == ""
    assert strip_relocated([{"type": "text", "text": "kept"}]) == [{"type": "text", "text": "kept"}]
