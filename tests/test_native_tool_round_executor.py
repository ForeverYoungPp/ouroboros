"""The bounded native tool-round review executor (configured-subagent api rows).

One episode = ONE logical review attempt of at most the configured round cap of
``chat(tools=…)`` calls against a fresh instance-local inspection registry.
Caps fail closed (typed refusal, never compaction); the second actor attempt
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
