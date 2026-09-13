"""The bounded native tool-round review executor (configured-subagent api rows).

One episode = ONE logical review attempt of at most the configured round cap of
``chat(tools=…)`` calls against a fresh instance-local inspection registry.
Caps fail closed (typed refusal, never compaction), EXCEPT that the last
permitted round withdraws the tools so the episode delivers a verdict from the
evidence it collected instead of returning nothing; that deliverable turn is
floored at the review surface's own output budget; the second actor attempt
repairs format locally over the collected answer; every read is host-observed
disclosure, never a full-coverage claim.
"""

import json

import pytest

from ouroboros.review_execution import (
    ReviewAssignment,
    ReviewRouteKind,
    ReviewRouteUnavailable,
    _review_route_executor,
)
from ouroboros.review_native_episode import NativeToolRoundReviewExecutor
from ouroboros.review_substrate import ReviewRequest, ReviewSlot

_VERDICT = '[{"severity": "advisory", "item": "x", "evidence": "e", "recommendation": "r"}]'


class _ScriptedLLM:
    """chat() replays a script; captures every messages payload it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("script exhausted — executor made an extra call")
        entry = self.script.pop(0)
        return entry, {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0}


def _tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.fixture()
def subject_repo(tmp_path):
    repo = tmp_path / "subject"
    repo.mkdir()
    (repo / "greeting.txt").write_text("hello native reviewer\n", encoding="utf-8")
    return repo


def _assignment(repo, llm, session_task="Review the staged change; cite files."):
    request = ReviewRequest(
        surface="multi_model_review",
        goal="review",
        task_id="t-native",
        session_root=str(repo),
        session_task=session_task,
        policy={"output_contract": "JSON array of findings"},
        no_proxy=True,
    )
    slot = ReviewSlot(
        slot_id="t1",
        model="openai/fake-reviewer",
        effort="low",
        route=ReviewRouteKind.API_CHAT,
        subagent_id="api-critic",
    )
    return ReviewAssignment(request=request, slot=slot, call_id="op-1")


def test_route_seam_selects_native_executor(subject_repo):
    executor = _review_route_executor(_assignment(subject_repo, None))
    assert isinstance(executor, NativeToolRoundReviewExecutor)


def test_episode_reads_then_answers(subject_repo):
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"})]},
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    result = executor.execute()
    assert result.raw_text == _VERDICT
    assert result.message["native_transcript"] == _VERDICT
    usage = result.usage
    assert usage["native_rounds"] == 2
    assert usage["host_file_read_attestation"] == "host_observed"
    assert usage["native_tool_receipts"][0]["tool"] == "read_file"
    assert usage["native_tool_receipts"][0]["path"] == "greeting.txt"
    # The REAL inspection tool ran against the pinned root: its output (with
    # the file body) went back to the model as a role=tool message.
    round2_messages = llm.calls[1]["messages"]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert tool_msgs and "hello native reviewer" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    # tools were offered on every round, from the curated inspection set only
    offered = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "read_file" in offered and "search_code" in offered
    assert "schedule_subagent" not in offered and "write_file" not in offered


def test_second_execute_repairs_locally_without_new_episode(subject_repo):
    llm = _ScriptedLLM([
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    first = executor.execute()
    calls_after_first = len(llm.calls)
    second = executor.execute()
    # No new provider round: format repair reuses the collected answer.
    assert len(llm.calls) == calls_after_first
    assert second.raw_text == first.raw_text


def test_round_cap_fails_closed(subject_repo, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_ROUNDS", "2")
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"}, "c1")]},
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"}, "c2")]},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    with pytest.raises(ReviewRouteUnavailable) as exc:
        executor.execute()
    assert exc.value.code == "native_rounds_exhausted"
    # The settled failure replays; no second paid episode.
    with pytest.raises(ReviewRouteUnavailable):
        executor.execute()
    assert not llm.script and len(llm.calls) == 2


def test_transcript_cap_fails_closed(subject_repo, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_TRANSCRIPT_CHARS", "50000")
    (subject_repo / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "big.txt"})]},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    with pytest.raises(ReviewRouteUnavailable) as exc:
        executor.execute()
    assert exc.value.code == "native_transcript_cap_exceeded"


def test_transcript_counter_includes_system_schemas_and_args(subject_repo, monkeypatch):
    """The bound is a SEND bound, so it must measure what every send carries:
    the system instructions and tool schemas ride each provider call, and
    tool-call argument objects accumulate in the message list like results.
    A cap sized to admit the bare prompt but not prompt+system+schemas must
    therefore refuse BEFORE the first send (previously it passed — each send
    was understated by the fixed ~9K system/schema cost plus the argument
    tail). Units are chars on both sides.
    """
    llm = _ScriptedLLM([
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    # Comfortably above the episode prompt alone, strictly below what the
    # first send actually carries (prompt + instructions + tool schemas).
    # The env knob clamps at a 50K floor, so the getter is patched directly —
    # the subject is the COUNTER's coverage, not the knob's clamp.
    import ouroboros.review_native_episode as native_episode

    cap = len(executor.episode_prompt) + 100
    monkeypatch.setattr(
        native_episode, "review_native_max_transcript_chars", lambda: cap
    )
    with pytest.raises(ReviewRouteUnavailable) as exc:
        executor.execute()
    assert exc.value.code == "native_transcript_cap_exceeded"
    assert not llm.calls, "the send bound must refuse before paying for a send"


def test_uninspectable_tool_is_refused_in_episode(subject_repo):
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("write_file", {"path": "greeting.txt", "content": "hacked"})]},
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    executor.execute()
    round2_messages = llm.calls[1]["messages"]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert tool_msgs and "not available" in tool_msgs[0]["content"]
    # The subject was NOT mutated.
    assert (subject_repo / "greeting.txt").read_text(encoding="utf-8") == "hello native reviewer\n"


def test_missing_session_task_refuses_typed(subject_repo):
    llm = _ScriptedLLM([])
    with pytest.raises(ReviewRouteUnavailable) as exc:
        NativeToolRoundReviewExecutor(
            _assignment(subject_repo, llm, session_task=""), llm=llm,
        ).execute()
    assert exc.value.code == "session_task_missing"


# ---------------------------------------------------------------------------
# The DELIVERABLE rail. Measured 2026-09-13 on a real episode: the inspection
# loop never terminated on its own — 40/40 rounds across 68 tool calls, and on
# another run it stopped at round 11 with an EMPTY body — so triad, scope and
# advisory all produced no verdict for an inspection already paid for. Two host
# guarantees close it: the last permitted round withdraws the tools, and that
# deliverable turn gets a real output budget (hidden reasoning shares it with
# the answer, so a slot-sized budget can be spent entirely on reasoning).
# ---------------------------------------------------------------------------


def test_last_round_withdraws_tools_and_delivers(subject_repo, monkeypatch):
    from ouroboros.review_native_episode import _FORCED_FINAL_NOTICE

    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_ROUNDS", "2")
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"}, "c1")]},
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    result = executor.execute()
    assert result.raw_text == _VERDICT
    assert executor._rounds_used == 2
    # The inspection round offers tools; the deliverable round withdraws them.
    assert llm.calls[0].get("tools")
    assert "tools" not in llm.calls[1] and "tool_choice" not in llm.calls[1]
    # ...and says why, so the model answers from what it already read.
    last_user = [m for m in llm.calls[1]["messages"] if m.get("role") == "user"][-1]
    assert _FORCED_FINAL_NOTICE in last_user["content"]


def test_deliverable_turn_gets_the_review_output_budget(subject_repo, monkeypatch):
    from ouroboros.tools.review import _review_output_budget

    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_ROUNDS", "2")
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"}, "c1")]},
        {"content": _VERDICT},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    executor.execute()
    # The inspection round keeps the slot's configured budget...
    assert llm.calls[0]["max_tokens"] == 16384
    # ...and the deliverable turn is floored at the review surface's own budget,
    # so an operator who lowered that lever keeps the behaviour they asked for.
    assert llm.calls[1]["max_tokens"] == max(16384, _review_output_budget())


def test_empty_deliverable_still_fails_closed(subject_repo, monkeypatch):
    """The rail guarantees the ATTEMPT, not a verdict: a deliverable turn that
    returns nothing keeps the typed refusal it always was."""
    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_ROUNDS", "2")
    llm = _ScriptedLLM([
        {"tool_calls": [_tool_call("read_file", {"path": "greeting.txt"}, "c1")]},
        {"content": ""},
    ])
    executor = NativeToolRoundReviewExecutor(_assignment(subject_repo, llm), llm=llm)
    with pytest.raises(ReviewRouteUnavailable) as exc:
        executor.execute()
    assert exc.value.code == "native_rounds_exhausted"
    # The deliverable round really was attempted (tools withdrawn).
    assert "tools" not in llm.calls[1]
