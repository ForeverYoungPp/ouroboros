"""S1 reversed — identity is LOCAL-ONLY: the local write + journal, no Engram copy.

Owner ruling: the fixed manifesto has no search scenario and no consumer in
Engram, so nothing may depend on an identity record existing there. These tests
drive the real tool against the shared fake service (no live store).
"""

from __future__ import annotations

import json

from ouroboros.engram_sink import reset_sinks
from ouroboros.tools.control import _update_identity
from ouroboros.tools.registry import ToolContext
from tests.test_engram_instruments import stub  # noqa: F401  (shared HTTP stub)


def _writes(state):
    return [r for r in state.requests if r["path"] in ("/observations", "/observations/passive")]


def _ctx(env):
    return ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, task_id="t-identity")


def test_an_identity_update_is_local_only(stub):  # noqa: F811
    """The local file and its journal row are the whole contract now."""
    state, env = stub
    reset_sinks()
    body = "I am Ouroboros. " + ("continuity matters. " * 5)

    out = _update_identity(_ctx(env), body)

    assert out.startswith("OK: identity updated")
    assert (env.drive_root / "memory" / "identity.md").read_text(encoding="utf-8") == body
    journal = (env.drive_root / "memory" / "identity_journal.jsonl").read_text(encoding="utf-8")
    row = json.loads(journal.strip().splitlines()[-1])
    assert row["new_content"] == body
    assert row["new_sha256"] and row["new_len"] == len(body)
    assert "engram_mirror" not in row
    assert _writes(state) == [], "an identity update reached Engram after S1 was reversed"


def test_a_project_scoped_task_still_cannot_touch_identity(stub):  # noqa: F811
    """P1 outranks everything: no per-project identity, and nothing is stored."""
    state, env = stub
    reset_sinks()
    ctx = _ctx(env)
    ctx.project_id = "proj-1"

    out = _update_identity(ctx, "I am Ouroboros. " + ("project self. " * 6))

    assert out.startswith("OK: identity is global")
    assert _writes(state) == []
