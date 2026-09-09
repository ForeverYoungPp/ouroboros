"""Empty-round guard tests (audited 5-item #3).

The guard fixes a model stuck producing turns with no tool calls, no visible
content, no reviewable effects, and no FINAL ANSWER marker. The A3 no-op nudge
is one-shot, so it cannot re-fire; this guard re-injects a mechanical reminder
on EVERY consecutive empty round, escalating after
EMPTY_ROUND_ESCALATION_THRESHOLD (2). The counter is self-healing: a
discontinuity (activity between empties) resets it automatically.
"""

from types import SimpleNamespace

import ouroboros.loop as loop
from ouroboros import empty_round_guard as erg
from ouroboros.tools.registry import ToolRegistry


def _make_ctx(tmp_path, round_idx=1, accumulated_usage=None):
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = "empty-round-test"
    registry._ctx._owner_directives = []
    usage = accumulated_usage if accumulated_usage is not None else {}
    return registry, loop._RoundLimitContext(
        [{"role": "user", "content": "task"}],
        SimpleNamespace(),
        "test-model",
        "medium",
        0,
        tmp_path,
        "empty-round-test",
        round_idx,
        None,
        usage,
        "",
        False,
        10,
        drive_root=tmp_path,
        status_drive_root=tmp_path,
        root_task_id="empty-round-test",
    )


def _empty_trace():
    return {"tool_calls": [], "reasoning_notes": []}


def _emitter():
    notes = []

    def _emit(text):
        notes.append(text)

    return _emit, notes


def _merge_user(messages, text):
    # The real loop helper: consecutive reminders must merge, not stack turns.
    loop._append_or_merge_user_message(messages, text)


# --- emptiness predicate ---


def test_empty_round_predicate_blank_content():
    assert erg.round_is_answer_empty("")
    assert erg.round_is_answer_empty(None)
    assert erg.round_is_answer_empty("   ")


def test_empty_round_predicate_content_not_empty():
    assert not erg.round_is_answer_empty("answer")
    assert not erg.round_is_answer_empty("  something  ")


def test_empty_round_predicate_final_answer_marker_is_not_empty():
    assert not erg.round_is_answer_empty("FINAL ANSWER: done")


def test_guard_fires_despite_cumulative_tool_history(tmp_path):
    # C1 regression: llm_trace["tool_calls"] (and every effect projection
    # derived from it) is cumulative across the run, so prior tool rounds
    # must NOT silence the guard on a later empty round.
    registry, ctx = _make_ctx(tmp_path, round_idx=12)
    trace = {
        "tool_calls": [
            {"tool": "read_file", "args": {"path": "a"}, "status": "ok"},
            {"tool": "write_file", "args": {"path": "b"}, "status": "ok"},
        ],
        "reasoning_notes": [],
    }
    emit, notes = _emitter()
    messages = [{"role": "user", "content": "task"}]
    assert erg.round_is_answer_empty("")
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True
    assert notes == ["Empty-round reminder injected."]


def test_visible_text_reasoning_block_is_empty():
    # A content list containing only reasoning blocks is not visible text.
    content = [{"type": "thinking", "text": "hidden"}]
    assert erg._visible_round_text(content) == ""
    assert erg.round_is_answer_empty(content)


# --- counter helpers ---


def test_counter_starts_zero():
    assert erg.empty_round_count({}) == 0
    assert erg.empty_round_count({"other": 1}) == 0


def test_note_empty_round_increments():
    usage = {}
    assert erg.note_empty_round(usage, 3) == 1
    assert erg.empty_round_count(usage) == 1
    assert usage["_last_empty_round_idx"] == 3
    assert erg.note_empty_round(usage, 4) == 2
    assert erg.empty_round_count(usage) == 2


def test_note_empty_round_self_heals_on_discontinuity():
    usage = {}
    erg.note_empty_round(usage, 3)  # empty round 3 -> count 1
    erg.note_empty_round(usage, 4)  # empty round 4 -> count 2
    # A tool-call / content round happened at 5 (never calls note_empty_round),
    # so the next empty round at 7 is NOT consecutive -> resets to 1.
    assert erg.note_empty_round(usage, 7) == 1
    assert erg.empty_round_count(usage) == 1


def test_reset_empty_round_count():
    usage = {}
    erg.note_empty_round(usage, 3)
    erg.reset_empty_round_count(usage)
    assert erg.empty_round_count(usage) == 0
    assert "_last_empty_round_idx" not in usage


# --- reminder injection ---


def test_first_empty_round_injects_basic_reminder(tmp_path):
    registry, ctx = _make_ctx(tmp_path, round_idx=5)
    trace = _empty_trace()
    emit, notes = _emitter()
    messages = [{"role": "user", "content": "task"}]

    injected = erg.maybe_inject_empty_round_reminder(
        ctx, trace, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    )

    assert injected is True
    assert erg.empty_round_count(ctx.accumulated_usage) == 1
    assert "[SYSTEM REMINDER]" in messages[-1]["content"]
    assert "previous round was empty" in messages[-1]["content"]
    assert "consecutive rounds" not in messages[-1]["content"]
    assert notes == ["Empty-round reminder injected."]
    assert any("Empty-round reminder injected" in n for n in trace["reasoning_notes"])


def test_second_consecutive_empty_round_escalates(tmp_path):
    registry, ctx = _make_ctx(tmp_path, round_idx=7)
    trace = _empty_trace()
    emit, notes = _emitter()
    messages = [{"role": "user", "content": "task"}]

    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True
    assert erg.empty_round_count(ctx.accumulated_usage) == 1

    # Second consecutive empty round (round 8).
    ctx.round_idx = 8
    trace2 = _empty_trace()
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace2, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True
    assert erg.empty_round_count(ctx.accumulated_usage) == 2
    assert "2 consecutive rounds" in messages[-1]["content"]
    assert notes[-1] == "Escalated empty-round reminder injected."



def test_content_round_resets_counter_without_injection(tmp_path):
    registry, ctx = _make_ctx(tmp_path, round_idx=11)
    trace = _empty_trace()
    emit, _ = _emitter()
    messages = [{"role": "user", "content": "task"}]
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True

    trace2 = _empty_trace()
    messages2 = [{"role": "user", "content": "task"}]
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace2, "real answer", messages2, emit,
        append_or_merge_user_message=_merge_user,
    ) is False
    assert erg.empty_round_count(ctx.accumulated_usage) == 0


def test_final_answer_round_resets_counter(tmp_path):
    registry, ctx = _make_ctx(tmp_path, round_idx=13)
    trace = _empty_trace()
    emit, _ = _emitter()
    messages = [{"role": "user", "content": "task"}]
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace, "", messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True

    trace2 = _empty_trace()
    messages2 = [{"role": "user", "content": "task"}]
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace2, "FINAL ANSWER: done", messages2, emit,
        append_or_merge_user_message=_merge_user,
    ) is False
    assert erg.empty_round_count(ctx.accumulated_usage) == 0


def test_reminder_appends_assistant_content_when_present(tmp_path):
    registry, ctx = _make_ctx(tmp_path, round_idx=15)
    trace = _empty_trace()
    emit, _ = _emitter()
    messages = [{"role": "user", "content": "task"}]
    content = [{"type": "thinking", "text": "hidden"}]
    assert erg.round_is_answer_empty(content)
    assert erg.maybe_inject_empty_round_reminder(
        ctx, trace, content, messages, emit,
        append_or_merge_user_message=_merge_user,
    ) is True
    assert len(messages) == 3
    assert messages[1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("[SYSTEM REMINDER]")


def test_visible_text_mirror_matches_loop_helper():
    # Deliberate byte-ratchet duplication (module docstring); this test locks
    # the guard's mirror to the loop's canonical helper.
    import ast
    import inspect

    def stripped(fn):
        node = ast.parse(inspect.getsource(fn)).body[0]
        node.name = "x"
        node.body = node.body[1:]  # drop docstring
        return ast.dump(node)

    assert stripped(erg._visible_round_text) == stripped(loop._visible_round_text)
