"""AC23 — retrieval is progressive, bounded, and cannot become one big pull.

The bounds matter because the prompt sections this replaces were *measured*: the
knowledge index was 6,570 chars, the Pattern Register 15,298, the deep self-review
307, the dialogue history 30,345. A retrieval path that can quietly fetch "the whole
store" would re-inflate exactly the context this phase removed — and it would do it
on the agent's initiative, which no reviewer would see coming.

Two of the assertions here are about the SERVER's shape rather than about taste:
``GET /observations/recent`` has no type filter and no compact mode, so every row it
returns carries a body; and there is no preview-only route. Those facts are why the
recency window is bounded well below Engram's 500 ceiling instead of at it.
"""

from __future__ import annotations

from ouroboros.engram_client import ENGRAM_MAX_BYTES_CEILING
from ouroboros.engram_read import (
    MAX_COUNT_LIMIT,
    MAX_DIGEST_CHARS,
    MAX_WINDOW,
    client_for,
    recent,
)
from ouroboros.engram_sink import reset_sinks
from ouroboros.tools import engram as tool
from ouroboros.tools.registry import ToolContext


def _ctx(env, task_id: str = "turn-1"):
    return ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, task_id=task_id)


def _observations(n: int) -> list[dict]:
    return [
        {"id": i, "type": "decision", "title": f"memory {i}", "content": f"body {i}" * 40,
         "created_at": "2026-01-01", "updated_at": f"2026-01-{i:02d}"}
        for i in range(1, n + 1)
    ]


# --------------------------------------------------------------------------- #
# AC23(a) — layer 1 is titles, with an explicit small limit
# --------------------------------------------------------------------------- #


def test_layer_one_renders_titles_and_never_bodies(engram_stub):
    state, env = engram_stub
    state.observations = _observations(30)

    out = tool._engram(_ctx(env), op="search", query="memory")

    assert "memory 1" in out
    assert "body 1" not in out, "layer 1 leaked a body into the context"
    assert "titles only" in out
    assert int(state.requests[-1]["params"]["limit"]) <= tool.MAX_SEARCH_LIMIT
    assert tool.MAX_SEARCH_LIMIT <= 20
    reset_sinks()


def test_layer_one_limit_is_explicit_and_capped(engram_stub):
    state, env = engram_stub
    state.observations = _observations(30)

    tool._engram(_ctx(env), op="search", query="memory", limit=10_000)

    assert int(state.requests[-1]["params"]["limit"]) == tool.MAX_SEARCH_LIMIT
    reset_sinks()


def test_the_context_class_is_compact_and_byte_bounded():
    """`GET /context` is the list/context class: compact=true by default here."""
    import inspect

    from ouroboros.engram_client import EngramClient

    signature = inspect.signature(EngramClient.context)
    assert signature.parameters["compact"].default is True
    assert signature.parameters["max_bytes"].default <= ENGRAM_MAX_BYTES_CEILING


# --------------------------------------------------------------------------- #
# AC23(b) — the neighbourhood layer is small and explicit
# --------------------------------------------------------------------------- #


def test_timeline_radius_is_small(engram_stub):
    state, env = engram_stub
    state.observations = _observations(10)

    tool._engram(_ctx(env), op="timeline", observation_id=5, before=99, after=99)

    params = state.requests[-1]["params"]
    assert int(params["before"]) <= 10
    assert int(params["after"]) <= 10
    reset_sinks()


# --------------------------------------------------------------------------- #
# AC23(c) — one record per read, and a bounded number of reads per turn
# --------------------------------------------------------------------------- #


def test_read_returns_exactly_one_record(engram_stub):
    state, env = engram_stub
    state.search_returns_content = True
    state.observations = _observations(10)

    out = tool._engram(_ctx(env), op="read", observation_id=3)

    assert "memory 3" in out
    assert "memory 4" not in out
    assert [r["path"] for r in state.requests] == ["/observations/3"]
    reset_sinks()


def test_the_turn_may_pull_only_a_bounded_number_of_full_records(engram_stub):
    state, env = engram_stub
    state.observations = _observations(10)
    ctx = _ctx(env, "turn-reads")

    for index in range(1, tool.MAX_READS_PER_TURN + 1):
        assert "body" in tool._engram(ctx, op="read", observation_id=index).lower()

    refused = tool._engram(ctx, op="read", observation_id=99)

    assert "BUDGET SPENT" in refused
    assert not any(r["path"] == "/observations/99" for r in state.requests)
    # The refusal names the caps rather than silently truncating a record.
    assert str(tool.MAX_READS_PER_TURN) in refused
    reset_sinks()


def test_a_budget_hit_does_not_block_the_cheap_layers(engram_stub):
    """The point is to steer toward the cheaper layer, not to lock memory away."""
    state, env = engram_stub
    state.observations = _observations(10)
    ctx = _ctx(env, "turn-steer")
    for index in range(1, tool.MAX_READS_PER_TURN + 1):
        tool._engram(ctx, op="read", observation_id=index)

    out = tool._engram(ctx, op="search", query="memory")

    assert "BUDGET SPENT" not in out
    assert "titles only" in out
    reset_sinks()


def test_turn_budgets_are_isolated_per_turn(engram_stub):
    state, env = engram_stub
    state.observations = _observations(10)
    for index in range(1, tool.MAX_READS_PER_TURN + 1):
        tool._engram(_ctx(env, "turn-a"), op="read", observation_id=index)

    assert "BUDGET SPENT" not in tool._engram(_ctx(env, "turn-b"), op="read", observation_id=1)
    reset_sinks()


def test_an_unidentifiable_caller_is_not_budgeted(engram_stub):
    """Guessing a shared bucket would let unrelated callers exhaust each other."""
    state, env = engram_stub
    state.observations = _observations(10)
    ctx = ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root)  # no task_id

    for index in range(1, tool.MAX_READS_PER_TURN + 3):
        assert "BUDGET SPENT" not in tool._engram(ctx, op="read", observation_id=index)
    reset_sinks()


# --------------------------------------------------------------------------- #
# AC23(d) — a whole turn cannot exceed the section it stands in for
# --------------------------------------------------------------------------- #


def test_the_turn_budget_matches_the_largest_section_it_may_replace():
    """30,345 chars is the measured dialogue-history section — the ceiling."""
    assert tool.MAX_TURN_OUTPUT_CHARS == 30_345
    # Per-op caps alone do not bound a turn, which is why the turn cap exists.
    assert tool.MAX_SEARCH_OUTPUT_CHARS + tool.MAX_TIMELINE_OUTPUT_CHARS < tool.MAX_TURN_OUTPUT_CHARS


def test_a_turn_that_spends_its_char_budget_is_refused(engram_stub):
    from ouroboros.tools.engram import reset_turn_budgets, turn_usage

    state, env = engram_stub
    state.fail = True
    ctx = _ctx(env, "turn-chars")
    tool._spend(ctx, chars=tool.MAX_TURN_OUTPUT_CHARS)

    refused = tool._engram(ctx, op="search", query="anything")

    assert "BUDGET SPENT" in refused
    assert turn_usage(ctx)["chars"] >= tool.MAX_TURN_OUTPUT_CHARS
    reset_turn_budgets()


def test_the_turn_ledger_is_bounded(engram_stub):
    """A long process must not accumulate one entry per task forever."""
    state, env = engram_stub
    for index in range(tool.MAX_TRACKED_TURNS + 20):
        tool._spend(_ctx(env, f"turn-{index}"), chars=1)
    assert len(tool._TURN_USAGE) <= tool.MAX_TRACKED_TURNS


# --------------------------------------------------------------------------- #
# AC23(e) — no path fetches the whole store's bodies in one request
# --------------------------------------------------------------------------- #


def test_the_recency_window_is_bounded_far_below_the_ceiling(engram_stub):
    state, env = engram_stub
    state.observations = _observations(80)

    read = recent(client_for(env))

    assert int(state.requests[-1]["params"]["limit"]) == MAX_WINDOW
    assert MAX_WINDOW < MAX_COUNT_LIMIT
    assert len(read.text) <= MAX_DIGEST_CHARS


def test_no_reader_asks_for_the_ceiling(engram_stub):
    """A ceiling-sized recency read IS 'all the bodies in one request'."""
    import inspect

    from ouroboros import engram_read

    source = inspect.getsource(engram_read)
    assert "recent(client, limit=MAX_COUNT_LIMIT)" not in source
    # The ceiling survives only as an input clamp, never as a requested window.
    assert "limit or MAX_WINDOW" in source


def test_engram_is_a_registered_read_only_tool():
    """The retrieval surface is one tool, and it is not a writer."""
    from ouroboros.safety import POLICY_SKIP, TOOL_POLICY

    assert TOOL_POLICY.get("engram") == POLICY_SKIP
