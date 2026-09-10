"""Regression guards for the DeepSeek thinking-mode ``reasoning_content`` contract.

The forensic failure (OpenAI Compatible provider, thinking-mode model, tool round):

    BadRequestError("Error code: 400 - ... 'The `reasoning_content` in the
    thinking mode must be passed back to the API.'")

MEASURED boundary (2026-09-10, api.deepseek.com, deepseek-flash + deepseek-v4-pro,
``max_tokens=1`` so only the validation path runs). It is NARROWER than "every
assistant turn":

    tools + TRAILING assistant turn, key ABSENT     -> 400
    tools + trailing assistant turn, key "" or " "  -> 200
    tools + assistant turn FOLLOWED BY a user turn  -> 200 (interior: unvalidated)
    tools + tool_calls turn, then tool + user       -> 200
    no tools + trailing assistant turn              -> 200
    thinking explicitly disabled                    -> 200

PROBE BLIND SPOT — read this before weakening any guard here. The first probe of
this route concluded "an absent reasoning_content is accepted in every shape" and
deleted the send-time backfill below; every one of its 14 cases had placed a
``user`` message AFTER the assistant turn, so it only ever exercised the interior
shape the API does not validate. A live task then died on the trailing shape
(751b9e70, 2026-09-10T11:42:39Z: its reclaim pass rewrote a tool-call assistant
turn into a capsule carrying neither ``tool_calls`` nor the CoT, and the request
ended on that turn with tools present). Any probe of this route MUST end on the
turn under test — ``test_build_backfills_a_trailing_capsule_turn`` pins the shape.

ouroboros treated ``reasoning_content`` as provider-private noise on every direct
lane — dropped it before the transcript AND scrubbed it on the way out — so the
second request of any tool round died. These guards lock in the per-route exception
while keeping the strict vLLM/SGLang + GLM/Z.AI scrub for every other
OpenAI-compatible route."""


def _deepseek_target(**overrides):
    target = {
        "provider": "openai-compatible",
        "resolved_model": "deepseek-chat",
        "usage_model": "openai-compatible/deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "supports_openrouter_extensions": False,
        "supports_generation_cost": False,
    }
    target.update(overrides)
    return target


def _glm_target():
    return {
        "provider": "openai-compatible",
        "resolved_model": "glm-4.7",
        "usage_model": "openai-compatible/glm-4.7",
        "base_url": "https://strict-vllm.example.com/v1",
        "supports_openrouter_extensions": False,
        "supports_generation_cost": False,
    }


def _history():
    return [
        {"role": "user", "content": "what's the weather?"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "reasoning_content": "I should call get_weather.",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "get_weather", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]


# --- route predicate ----------------------------------------------------------

def test_predicate_matches_official_host_and_deepseek_models():
    from ouroboros.provider_models import requires_reasoning_content_echo

    # official host, any model name (incl. an aliased deployment)
    assert requires_reasoning_content_echo(
        "openai-compatible", "my-reasoner", "https://api.deepseek.com/v1") is True
    # proxy/aggregator host, DeepSeek-named model (namespaced id included)
    assert requires_reasoning_content_echo(
        "openai-compatible", "deepseek-chat", "http://localhost:8000/v1") is True
    assert requires_reasoning_content_echo(
        "openai-compatible", "deepseek-ai/DeepSeek-R1", "https://proxy.example.com/v1") is True


def test_predicate_leaves_strict_and_other_lanes_alone():
    from ouroboros.provider_models import requires_reasoning_content_echo

    # a strict GLM/vLLM route must keep the outbound scrub
    assert requires_reasoning_content_echo(
        "openai-compatible", "glm-4.7", "https://strict-vllm.example.com/v1") is False
    # only the openai-compatible lane owns this contract
    assert requires_reasoning_content_echo(
        "cloudru", "deepseek-v3.1", "https://foundation-models.api.cloud.ru/v1") is False
    assert requires_reasoning_content_echo(
        "openrouter", "deepseek/deepseek-v4-pro", "") is False


# --- response normalization ---------------------------------------------------

def _response(reasoning_content="secret deepseek CoT"):
    return {
        "id": "chatcmpl-1",
        "choices": [{"message": {
            "role": "assistant",
            "content": "hello",
            "reasoning_content": reasoning_content,
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_normalize_keeps_reasoning_content_for_deepseek_route():
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="x")
    msg, _usage = client._normalize_remote_response(
        _response(), _deepseek_target(), skip_cost_fetch=True)
    # the next request must be able to replay it
    assert msg["reasoning_content"] == "secret deepseek CoT"


def test_normalize_still_drops_reasoning_content_for_strict_route():
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="x")
    msg, _usage = client._normalize_remote_response(
        _response("secret glm reasoning"), _glm_target(), skip_cost_fetch=True)
    assert "reasoning_content" not in msg


def test_normalize_never_echoes_a_malformed_value():
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="x")
    for bad in (["weird", "list"], {"x": 1}, "   ", None):
        msg, _usage = client._normalize_remote_response(
            _response(bad), _deepseek_target(), skip_cost_fetch=True)
        assert "reasoning_content" not in msg


# --- outbound send copy -------------------------------------------------------

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _build(target, messages, tools):
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="x")
    return client._build_remote_kwargs(
        target, messages, reasoning_effort="low", max_tokens=64,
        tool_choice="auto", temperature=None, tools=tools,
    )


def test_build_replays_reasoning_content_on_deepseek_route():
    kwargs = _build(_deepseek_target(), _history(), tools=[_TOOL])
    asst = kwargs["messages"][1]
    assert asst["reasoning_content"] == "I should call get_weather."
    # the canonical input is untouched
    assert _history()[1]["reasoning_content"] == "I should call get_weather."


def test_build_backfills_missing_reasoning_content_when_tools_present():
    """A turn with no preserved CoT (reasoning-disabled answer, prior model, or a
    session persisted before the fix) must still satisfy the presence check."""
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "again"},
    ]
    kwargs = _build(
        _deepseek_target(base_url="", resolved_model="deepseek-reasoner"),
        history, tools=[_TOOL],
    )
    asst = kwargs["messages"][1]
    assert asst["reasoning_content"] == " "
    assert "reasoning_content" not in history[1]


def test_build_does_not_backfill_without_tools():
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    kwargs = _build(_deepseek_target(), history, tools=None)
    assert "reasoning_content" not in kwargs["messages"][1]


def test_build_backfills_a_trailing_capsule_turn():
    """The live 751b9e70 shape: a reclaim pass rewrote a tool-call assistant turn
    into a capsule with no ``tool_calls`` and no CoT, and the request ENDED on it
    while carrying tools. Absent ``reasoning_content`` there is a hard 400; the
    send-time placeholder is what keeps the round alive."""
    history = [
        {"role": "user", "content": "测试"},
        {"role": "assistant",
         "content": "[Context capsule generation 1; exact source retained by checkpoint]\n"
                    "I inspected two sources and decided to answer."},
    ]
    kwargs = _build(_deepseek_target(), history, tools=[_TOOL])
    assert kwargs["messages"][-1]["role"] == "assistant"
    assert kwargs["messages"][-1]["reasoning_content"] == " "
    # the canonical transcript stays clean: the placeholder is send-time only
    assert "reasoning_content" not in history[-1]


def test_build_does_not_backfill_a_trailing_assistant_turn_without_tools():
    """Same trailing shape, no tools: the API does not validate, so nothing is
    invented (measured; keeps the placeholder scoped to the failing case)."""
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "answer with no CoT"},
    ]
    kwargs = _build(_deepseek_target(), history, tools=None)
    assert "reasoning_content" not in kwargs["messages"][-1]


def test_build_keeps_strict_route_scrubbed():
    kwargs = _build(_glm_target(), _history(), tools=[_TOOL])
    asst = kwargs["messages"][1]
    assert "reasoning_content" not in asst
    # no placeholder is injected for a route whose server rejects the field
    assert all("reasoning_content" not in m for m in kwargs["messages"])


def test_strip_helper_keeps_only_the_flat_deepseek_field():
    from ouroboros.llm import LLMClient

    history = [{
        "role": "assistant",
        "reasoning_content": "keep me",
        "reasoning": "openrouter shape",
        "reasoning_details": [{"type": "reasoning.text", "text": "drop"}],
        "response_id": "resp_1",
        "content": [
            {"type": "thinking", "thinking": "drop", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ],
    }]
    kept = LLMClient._strip_openrouter_roundtrip_metadata(
        history, keep_reasoning_content=True)
    assert kept[0]["reasoning_content"] == "keep me"
    assert "reasoning" not in kept[0] and "reasoning_details" not in kept[0]
    assert "response_id" not in kept[0]
    assert kept[0]["content"] == [{"type": "text", "text": "answer"}]
    # default stays the strict-server behavior
    scrubbed = LLMClient._strip_openrouter_roundtrip_metadata(history)
    assert "reasoning_content" not in scrubbed[0]


def test_send_time_wire_payload_keeps_the_replayed_cot():
    """The send-time finalizer must not undo the route-aware keep: the failure was
    observed on the wire, so the guard belongs on the physical payload too."""
    from ouroboros.request_wire_recovery import prepare_wire_payload_for_send

    kwargs = _build(_deepseek_target(), _history(), tools=[_TOOL])
    wire = prepare_wire_payload_for_send(
        _deepseek_target(), kwargs, api_surface="chat.completions")
    assert wire["messages"][1]["reasoning_content"] == "I should call get_weather."

