"""S1 — identity reaches Engram, and an evolving identity upserts in place.

Identity was the one memory with NO remote copy at all: the local
``memory/identity.md`` was its only home, so a lost drive lost the self. These
tests drive the real tool against the shared fake service (no live store).
"""

from __future__ import annotations

from ouroboros.engram_sink import reset_sinks
from ouroboros.tools.control import _update_identity
from ouroboros.tools.registry import ToolContext
from tests.test_engram_instruments import stub  # noqa: F401  (shared HTTP stub)


def _writes(state):
    return [r for r in state.requests if r["path"] in ("/observations", "/observations/passive")]


def _ctx(env):
    return ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, task_id="t-identity")


def test_a_multiline_identity_keeps_its_structure(stub):  # noqa: F811
    """Identity is normally headings + paragraphs. ``document=True`` is what keeps
    newlines (the one-line sanitizer would flatten the self's only remote copy),
    so a single-line body could not tell the two paths apart."""
    state, env = stub
    reset_sinks()
    body = "I am Ouroboros.\n\n## What I value\n- continuity\n- honesty\n"

    _update_identity(_ctx(env), body)

    content = _writes(state)[-1]["body"]["content"]
    assert "\n" in content and "## What I value" in content and "- honesty" in content


def test_the_journal_records_whether_the_mirror_landed(stub):  # noqa: F811
    """A refusal (C18) or the per-run cap returns before the spool append, so the
    outcome must be visible locally — otherwise a dropped mirror looks delivered."""
    import json

    state, env = stub
    reset_sinks()
    ctx = _ctx(env)
    _update_identity(ctx, "I am Ouroboros. " + ("continuity matters. " * 5))

    journal = (env.drive_root / "memory" / "identity_journal.jsonl").read_text(encoding="utf-8")
    row = json.loads(journal.strip().splitlines()[-1])
    assert str(row.get("engram_mirror") or "").startswith("sent:")


def test_an_identity_update_is_mirrored(stub):  # noqa: F811
    state, env = stub
    reset_sinks()
    body = "I am Ouroboros. " + ("continuity matters. " * 5)

    out = _update_identity(_ctx(env), body)

    assert out.startswith("OK: identity updated")
    writes = _writes(state)
    assert writes, "an identity update never reached Engram"
    assert writes[-1]["body"]["type"] == "identity"
    assert writes[-1]["body"]["topic_key"] == "identity:manifest"
    assert writes[-1]["body"]["scope"] == "global"
    # One update is ONE record. A second mirror (a leftover block, a second key)
    # would show up here as extra POSTs and as a differently-keyed row.
    assert len(writes) == 1, f"identity was mirrored {len(writes)} times"
    # The document sanitizer trims the trailing space, so compare on substance.
    assert "continuity matters." in writes[-1]["body"]["content"]


def test_an_evolving_identity_upserts_rather_than_piling_up(stub):  # noqa: F811
    state, env = stub
    reset_sinks()
    ctx = _ctx(env)
    first = "I am Ouroboros. " + ("first self. " * 6)
    second = "I am Ouroboros. " + ("evolved self. " * 6)

    _update_identity(ctx, first)
    _update_identity(ctx, second)

    stored = [r for r in state.knowledge.values() if r.get("topic_key") == "identity:manifest"]
    assert len(stored) == 1, "a second identity write became a second memory"
    assert "evolved self." in stored[0]["content"]


def test_a_project_scoped_task_still_cannot_touch_identity(stub):  # noqa: F811
    """The P1 rule outranks the mirror: no per-project identity, and nothing is
    written to the store either."""
    state, env = stub
    reset_sinks()
    ctx = _ctx(env)
    ctx.project_id = "proj-1"

    out = _update_identity(ctx, "I am Ouroboros. " + ("project self. " * 6))

    assert out.startswith("OK: identity is global")
    assert _writes(state) == []
