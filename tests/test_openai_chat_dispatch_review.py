"""Adversarial regressions for direct-OpenAI Chat Phase 2A seams."""

from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import ouroboros.consciousness as consciousness
import ouroboros.context_compaction as context_compaction
import ouroboros.llm as llm_module
import ouroboros.llm_observability as llm_observability
import ouroboros.openai_chat_dispatch as dispatch
from ouroboros.context_fit import estimate_context_prompt_tokens
from ouroboros.openai_chat_custom import normalize_openai_custom_tool_calls
from ouroboros.request_wire_receipts import (
    WireCandidateSpec,
    bind_wire_candidate,
)
from ouroboros.utils import estimate_tokens


def _target():
    return {
        "provider": "openai",
        "resolved_model": "future-model-without-prefix",
        "usage_model": "openai/future-model-without-prefix",
        "base_url": "https://api.openai.com/v1",
    }


def _read_tool():
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one allowed path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"const": "allowed"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }


def _custom_call(call_id, name, raw_input):
    return {
        "id": call_id,
        "type": "custom",
        "custom": {"name": name, "input": raw_input},
        "function": None,
    }


def _custom_exchange(tool, raw_input, *, effort="medium"):
    name = tool["function"]["name"]
    source = {
        "model": "future-model-without-prefix",
        "messages": [{"role": "user", "content": f"Use {name}."}],
        "reasoning_effort": effort,
        "tool_choice": "required",
        "tools": [copy.deepcopy(tool)],
    }
    candidate = bind_wire_candidate(
        target=_target(),
        api_surface="chat.completions",
        source_payload=source,
        candidate_spec=WireCandidateSpec(
            "openai_chat_custom", effort, "requested_wire_form"
        ),
        requested_effort=effort,
        ladder_ordinal=1,
    )
    calls, receipts = normalize_openai_custom_tool_calls(
        [_custom_call("call-custom", name, raw_input)],
        candidate,
    )
    message = {"role": "assistant", "content": "", "tool_calls": calls}
    usage = {
        "cost": 0.0,
        dispatch.REQUEST_WIRE_USAGE_KEY: {
            "candidate_sha256": receipts[0].candidate_sha256,
        },
        dispatch.CUSTOM_RECEIPTS_USAGE_KEY: receipts,
    }
    return message, usage


class _QueuedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def _resolve_remote_target(self, _model):
        return _target()

    def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        message, usage = self.responses.pop(0)
        return copy.deepcopy(message), usage


def _background_instance(tmp_path, llm, tools, *, rounds=2):
    instance = object.__new__(consciousness.BackgroundConsciousness)
    instance._build_context = lambda: "context"
    instance._tool_schemas = lambda: copy.deepcopy(tools)
    instance._llm = llm
    instance._drive_root = tmp_path
    instance._max_bg_rounds = rounds
    instance._paused = False
    instance._emit_live_log = lambda *_a, **_k: None
    instance._emit_progress = lambda _content: None
    instance._bg_spent_usd = 0.0
    instance._check_budget = lambda: True
    instance._event_queue = None
    instance._last_idle_reason = ""
    instance._next_wakeup_sec = 300
    instance._wakeup_max = 3600
    instance._owner_chat_id_fn = lambda: None
    instance._registry = SimpleNamespace(_ctx=SimpleNamespace())
    (tmp_path / "logs").mkdir(exist_ok=True)
    return instance


def _capture_persistence(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        llm_observability,
        "persist_call",
        lambda *_a, **kwargs: persisted.append(copy.deepcopy(kwargs)),
    )
    return persisted


def _response_payloads(persisted):
    return [
        item["payload"]
        for item in persisted
        if str(item.get("call_type") or "").endswith("_response")
    ]


def _pin_remote_background(monkeypatch):
    monkeypatch.setattr(
        consciousness,
        "get_consciousness_model",
        lambda: "openai::future-model-without-prefix",
    )
    monkeypatch.setattr(consciousness, "resolve_effort", lambda _slot: "medium")


def test_background_chat_observed_persists_public_usage_and_returns_receipt(
    monkeypatch,
    tmp_path,
):
    tool = _read_tool()
    invalid, first_usage = _custom_exchange(tool, '{"path":"wrong"}')
    llm = _QueuedLLM([
        (invalid, first_usage),
        ({"role": "assistant", "content": "corrected"}, {"cost": 0.0}),
    ])
    instance = _background_instance(tmp_path, llm, [tool])
    persisted = _capture_persistence(monkeypatch)
    _pin_remote_background(monkeypatch)

    assert instance._think_scoped() is True
    assert len(llm.requests) == 2
    assert llm.requests[1]["messages"][-1]["role"] == "tool"
    assert "TOOL_ARG_ERROR" in llm.requests[1]["messages"][-1]["content"]
    assert dispatch.CUSTOM_RECEIPTS_USAGE_KEY not in first_usage
    response_payloads = _response_payloads(persisted)
    assert response_payloads[0]["usage"]["request_wire"]
    assert all(
        dispatch.CUSTOM_RECEIPTS_USAGE_KEY not in item["usage"]
        for item in response_payloads
    )


def test_compaction_chat_observed_persists_public_usage_and_returns_receipt(
    monkeypatch,
    tmp_path,
):
    part = context_compaction._part("source", "text")
    invalid, first_usage = _custom_exchange(
        context_compaction._CONTEXT_SUMMARIES_TOOL,
        '{"summaries":"wrong"}',
        effort="low",
    )
    arguments = json.dumps({
        "summaries": [{"source_id": part.source_id, "summary": "ok"}],
    }, separators=(",", ":"))
    valid = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-valid",
            "type": "function",
            "function": {
                "name": "emit_context_summaries",
                "arguments": arguments,
            },
        }],
    }
    llm = _QueuedLLM([(invalid, first_usage), (valid, {"cost": 0.0})])
    persisted = _capture_persistence(monkeypatch)
    monkeypatch.setattr(llm_module, "LLMClient", lambda: llm)

    result = context_compaction._call_summarizer(
        [part],
        drive_root=tmp_path,
        task_id="compaction",
        phase="map",
        spec={
            "model": "openai::future-model-without-prefix",
            "effort": "low",
            "output_budget": 100,
            "use_local": False,
        },
        summary_budgets={"source": 100},
        usage_total={},
    )

    assert result == {part.source_id: "ok"}
    assert len(llm.requests) == 2
    assert llm.requests[1]["messages"][-1]["role"] == "tool"
    assert dispatch.CUSTOM_RECEIPTS_USAGE_KEY not in first_usage
    response_payloads = _response_payloads(persisted)
    assert response_payloads[0]["usage"]["request_wire"]
    assert all(
        dispatch.CUSTOM_RECEIPTS_USAGE_KEY not in item["usage"]
        for item in response_payloads
    )


class _HistoryError(Exception):
    def __init__(self):
        message = (
            "A custom tool call in an assistant message is not compatible "
            "without a matching tool result"
        )
        super().__init__(message)
        self.status_code = 400
        self.code = "invalid_request_error"
        self.param = "messages[3].tool_call_id"
        self.body = {"error": {
            "status_code": 400,
            "code": self.code,
            "param": self.param,
            "message": message,
        }}


def _dispatch_source():
    return {
        "model": "future-model-without-prefix",
        "messages": [{"role": "user", "content": "Use a tool."}],
        "reasoning_effort": "medium",
        "tool_choice": "auto",
        "tools": [_read_tool()],
    }


def _initial_custom_candidate():
    source = _dispatch_source()
    return bind_wire_candidate(
        target=_target(),
        api_surface="chat.completions",
        source_payload=source,
        candidate_spec=WireCandidateSpec(
            "openai_chat_custom", "medium", "requested_wire_form",
        ),
        requested_effort="medium",
        ladder_ordinal=1,
    )


def test_history_exception_cannot_advance_the_dialect_ladder():
    candidate = _initial_custom_candidate()
    error = _HistoryError()
    assert dispatch.exact_tool_dialect_rejection(error, candidate) is False
    assert dispatch.plan_direct_openai_dialect_candidate(
        target=_target(),
        source_payload=_dispatch_source(),
        current=candidate,
        error=error,
        body_error=False,
    ) is None


def test_history_body_error_cannot_advance_the_dialect_ladder():
    candidate = _initial_custom_candidate()
    error = _HistoryError().body["error"]
    assert dispatch.plan_direct_openai_dialect_candidate(
        target=_target(),
        source_payload=_dispatch_source(),
        current=candidate,
        error=error,
        body_error=True,
    ) is None


def test_background_remeasures_growth_before_every_physical_send(monkeypatch, tmp_path):
    tool = _read_tool()
    first = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-large",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"allowed"}'},
        }],
    }
    llm = _QueuedLLM([
        (first, {"cost": 0.0}),
        ({"role": "assistant", "content": "must not send"}, {"cost": 0.0}),
    ])
    instance = _background_instance(tmp_path, llm, [tool])
    instance._execute_tool = (
        lambda *_a, **_k: "x" * (consciousness.BG_CONTEXT_MAX_CHARS + 100)
    )
    _pin_remote_background(monkeypatch)
    measured = []
    real_measure = dispatch.projected_context_size_bytes

    def measure(*args, **kwargs):
        result = real_measure(*args, **kwargs)
        measured.append(result)
        return result

    monkeypatch.setattr(dispatch, "projected_context_size_bytes", measure)

    assert instance._think_scoped() is False
    assert len(llm.requests) == 1
    assert len(measured) == 2
    assert measured[0] < consciousness.BG_CONTEXT_MAX_CHARS
    assert measured[1] > consciousness.BG_CONTEXT_MAX_CHARS
    assert instance._last_idle_reason == "context_overflow"
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["type"] == "consciousness_context_overflow"


def _serialized_projection(messages, tools):
    return json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _projection_measures(projections):
    serialized = [
        _serialized_projection(messages, tools)
        for messages, tools in projections
    ]
    return (
        [len(item.encode("utf-8")) for item in serialized],
        [estimate_tokens(item) for item in serialized],
    )


def test_localized_109_tools_max_each_admission_metric_independently():
    localized = [
        {
            "type": "function",
            "function": {
                "name": f"localized_{index:03d}",
                "description": "Run the localized operation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "я" * 315,
                        },
                    },
                    "required": ["query"],
                },
            },
        }
        for index in range(109)
    ]
    messages = [{"role": "user", "content": "x"}]
    projections = dispatch.direct_openai_context_projections(
        messages,
        localized,
        provider="openai",
        reasoning_effort="medium",
    )
    byte_sizes, token_sizes = _projection_measures(projections)

    assert len(projections) == 2
    assert projections[0][1][0]["type"] == "function"
    assert projections[1][1][0]["type"] == "custom"
    assert byte_sizes[0] > byte_sizes[1]
    assert token_sizes[1] > token_sizes[0]
    assert dispatch.projected_context_size_bytes(
        messages,
        localized,
        provider="openai",
        reasoning_effort="medium",
    ) == max(byte_sizes)
    assert estimate_context_prompt_tokens(
        messages,
        localized,
        provider="openai",
        reasoning_effort="medium",
    ) == max(token_sizes)


def test_current_112_tool_registry_exposes_and_measures_both_projections(tmp_path):
    from ouroboros.tools.registry import ToolRegistry

    repo_dir = pathlib.Path(__file__).resolve().parents[1]
    tools = ToolRegistry(repo_dir=repo_dir, drive_root=tmp_path).schemas()
    assert len(tools) == 112
    messages = [{"role": "user", "content": "x"}]
    projections = dispatch.direct_openai_context_projections(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    )
    byte_sizes, token_sizes = _projection_measures(projections)

    assert len(projections) == 2
    assert dispatch.projected_context_size_bytes(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    ) == max(byte_sizes)
    assert estimate_context_prompt_tokens(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    ) == max(token_sizes)


def test_context_sizing_uses_canonical_projection_for_unknown_historical_tool():
    tools = [_read_tool()]
    messages = [
        {"role": "user", "content": "Use a tool."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-unknown",
                "type": "function",
                "function": {"name": "unknown_tool", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-unknown",
            "content": "Unknown tool: unknown_tool",
        },
    ]

    projections = dispatch.direct_openai_context_projections(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    )

    assert projections == ((messages, tools),)
    assert dispatch.projected_context_size_bytes(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    ) > 0
    assert estimate_context_prompt_tokens(
        messages,
        tools,
        provider="openai",
        reasoning_effort="medium",
    ) > 0
