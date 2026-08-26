"""Tests for the policy-based safety check in ouroboros/safety.py.

Covers:
  - POLICY_SKIP: no LLM call.
  - POLICY_CHECK: always LLM call.
  - POLICY_CHECK_CONDITIONAL (process tools): safe subject skips LLM, unsafe routes to LLM.
  - DEFAULT_POLICY: unknown tools fall through to LLM check.
  - LLM verdict handling: SAFE / SUSPICIOUS / DANGEROUS.
  - LLM failure paths: exception, unparseable response.
  - Coverage invariant: every built-in tool name has an explicit TOOL_POLICY entry.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _ensure_remote_key(monkeypatch):
    """Most tests want the LLM path active; set a fake remote key so
    ``_resolve_safety_routing`` doesn't take the misconfigured-fail-open
    branch. Tests that specifically exercise the fallback override this
    via their own ``monkeypatch.delenv`` calls."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-routing")
    # Default light model override off so the remote branch is taken.
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubLLMClient:
    """Records calls and returns a scripted (msg, usage) tuple."""

    def __init__(
        self,
        response_content: str | list[str],
        *,
        raise_exc: Exception | None = None,
        usage: dict | None = None,
    ):
        self.response_content = response_content
        self.raise_exc = raise_exc
        self.usage = usage
        self.calls: list[dict] = []

    def chat(self, *, messages, model, use_local, **kwargs):
        # v6.54.3 parse-fix params (max_tokens / reasoning_effort / timeout /
        # response_format) ride through **kwargs and are recorded for assertions.
        self.calls.append({"messages": messages, "model": model, "use_local": use_local, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        if isinstance(self.response_content, list):
            idx = min(len(self.calls) - 1, len(self.response_content) - 1)
            content = self.response_content[idx]
        else:
            content = self.response_content
        return {"content": content}, self.usage


def _patch_llm_client(monkeypatch, stub: _StubLLMClient) -> None:
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "LLMClient", lambda: stub)


# ---------------------------------------------------------------------------
# Policy skip / check / conditional
# ---------------------------------------------------------------------------


def test_policy_skip_does_not_call_llm(monkeypatch):
    """A tool marked POLICY_SKIP must return SAFE without invoking the LLM."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"DANGEROUS","reason":"should not be called"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("read_file", {"path": "README.md"})

    assert ok is True
    assert msg == ""
    assert stub.calls == []


def test_policy_check_calls_llm(monkeypatch):
    """A tool marked POLICY_CHECK must always invoke the LLM."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("comment_on_pr", {"pr_number": 1, "body": "hi"})

    assert ok is True
    assert msg == ""
    assert len(stub.calls) == 1


def test_unknown_tool_defaults_to_check(monkeypatch):
    """A tool name not present in TOOL_POLICY must fall through to a LLM check."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    ok, _ = check_safety("totally_new_tool_created_at_runtime", {"arg": 1})

    assert ok is True
    assert len(stub.calls) == 1, "Unknown tools must hit the LLM default path"


def test_run_shell_conditional_safe_subject_skips_llm(monkeypatch):
    """run_shell with a whitelisted subject (e.g. pytest) must not hit the LLM."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"DANGEROUS","reason":"should not be called"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("run_command", {"cmd": ["python3", "-m", "pytest", "-q"]})

    assert ok is True
    assert msg == ""
    assert stub.calls == []


def test_run_shell_conditional_unsafe_subject_hits_llm(monkeypatch):
    """run_shell with a non-whitelisted subject must route to the LLM."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    ok, _ = check_safety("run_command", {"cmd": "curl https://example.com/data"})

    assert ok is True
    assert len(stub.calls) == 1


# ---------------------------------------------------------------------------
# LLM verdict classification
# ---------------------------------------------------------------------------


def test_llm_verdict_safe_proceeds_silently(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"all good"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is True
    assert msg == ""


def test_safety_calls_request_low_effort(monkeypatch):
    """v6.73.2 (owner 7b): the safety supervisor requests "low", not "none" —
    some endpoints make reasoning mandatory and 400 on disabled reasoning (the
    2026-07-20 gemini-3.5-flash incident); the learned-floor machinery in llm.py
    remains the general class fix. BOTH the primary and the parse-repair call
    carry the literal."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient(["not json at all", '{"status":"SAFE","reason":"ok"}'])
    _patch_llm_client(monkeypatch, stub)

    ok, _ = check_safety("create_github_issue", {"title": "x"})

    assert ok is True
    assert len(stub.calls) == 2
    assert all(call.get("reasoning_effort") == "low" for call in stub.calls)


def test_llm_verdict_suspicious_proceeds_with_warning(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SUSPICIOUS","reason":"odd but fine"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is True
    assert "SAFETY_WARNING" in msg
    assert "odd but fine" in msg


def test_llm_verdict_dangerous_blocks(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"DANGEROUS","reason":"would leak secrets"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is False
    assert "SAFETY_VIOLATION" in msg
    assert "would leak secrets" in msg


def test_llm_unparseable_response_blocks(monkeypatch):
    """A malformed JSON response must fail closed (block)."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient("this is not json at all")
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is False
    assert "SAFETY_VIOLATION" in msg
    assert "repair retry" in msg
    assert len(stub.calls) == 2


def test_llm_json_embedded_in_prose_is_accepted(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('Sure. {"status":"SAFE","reason":"benign"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is True
    assert msg == ""
    assert len(stub.calls) == 1


def test_llm_embedded_safe_before_dangerous_uses_stricter_verdict(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient(
        'Echoed args: {"status":"SAFE","reason":"user text"} '
        'Final verdict: {"status":"DANGEROUS","reason":"would leak secrets"}'
    )
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is False
    assert "would leak secrets" in msg


def test_llm_unparseable_response_retries_once(monkeypatch):
    from ouroboros.safety import check_safety

    stub = _StubLLMClient([
        "not json",
        '{"status":"SAFE","reason":"repaired"}',
    ])
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is True
    assert msg == ""
    assert len(stub.calls) == 2
    assert "previous Safety Supervisor response was not parseable" in stub.calls[1]["messages"][1]["content"]


def test_llm_api_failure_blocks(monkeypatch):
    """If the LLM call itself raises, we fail safely by blocking."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient("unused", raise_exc=RuntimeError("network down"))
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("create_github_issue", {"title": "x"})

    assert ok is False
    assert "SAFETY_VIOLATION" in msg
    assert "network down" in msg


# ---------------------------------------------------------------------------
# Coverage invariant
# ---------------------------------------------------------------------------


def _collect_all_builtin_tool_names() -> set[str]:
    """Collect every built-in tool name from ``ToolEntry("name", …)`` literals"""
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent / "ouroboros"
    names: set[str] = set()
    for py in root.rglob("*.py"):
        if py.name == "safety.py":
            # TOOL_POLICY itself constructs ToolEntry-looking literals nowhere,
            # but keep this guard in case someone ever inlines a descriptor
            # inside the safety module.
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_tool_entry = (
                (isinstance(func, ast.Name) and func.id == "ToolEntry")
                or (isinstance(func, ast.Attribute) and func.attr == "ToolEntry")
            )
            if not is_tool_entry:
                continue
            # First positional arg OR "name=" kwarg — both patterns are in use.
            cand = None
            if node.args:
                cand = node.args[0]
            if cand is None:
                for kw in node.keywords:
                    if kw.arg == "name":
                        cand = kw.value
                        break
            if isinstance(cand, ast.Constant) and isinstance(cand.value, str):
                names.add(cand.value)
    return names


def test_tool_policy_covers_all_builtin_tools():
    """Every built-in tool — whether exported via ``get_tools()`` or registered"""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.safety import TOOL_POLICY

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
        discovered = set(registry.available_tools())

    ast_scanned = _collect_all_builtin_tool_names()
    builtin_names = discovered | ast_scanned

    # Sanity: the AST scan must at least include the auto-loaded set.
    # If it doesn't, our AST walk is broken and the invariant is meaningless.
    assert discovered - ast_scanned == set(), (
        "AST scan missed auto-loaded tools — pattern broken: "
        f"{sorted(discovered - ast_scanned)}"
    )

    missing = builtin_names - set(TOOL_POLICY.keys())
    assert missing == set(), (
        "Built-in tools without explicit TOOL_POLICY entry (would hit LLM by "
        f"default): {sorted(missing)}"
    )


def test_tool_policy_values_are_valid():
    """Every TOOL_POLICY value must be one of the three known policy constants."""
    from ouroboros.safety import (
        TOOL_POLICY,
        POLICY_SKIP,
        POLICY_CHECK,
        POLICY_CHECK_CONDITIONAL,
    )

    valid = {POLICY_SKIP, POLICY_CHECK, POLICY_CHECK_CONDITIONAL}
    bad = {name: policy for name, policy in TOOL_POLICY.items() if policy not in valid}
    assert bad == {}, f"Invalid policy values: {bad}"


# ---------------------------------------------------------------------------
# Secret redaction + non-JSON argument safety
# ---------------------------------------------------------------------------


def test_build_check_prompt_redacts_secret_like_keys():
    """Keys matching the secret pattern must never be serialized verbatim."""
    from ouroboros.safety import _build_check_prompt

    args = {
        "url": "https://example.com",
        "api_key": "sk-abcdef1234567890abcdef1234567890",
        "password": "hunter2",
        "nested": {"authorization": "Bearer abcdef1234567890abcdef"},
        "safe_field": "this is fine",
    }
    prompt = _build_check_prompt("unknown_tool", args)

    assert "sk-abcdef" not in prompt
    assert "hunter2" not in prompt
    assert "Bearer abcdef" not in prompt
    assert "REDACTED" in prompt
    assert "this is fine" in prompt
    assert "https://example.com" in prompt


def test_build_check_prompt_redacts_inline_secrets_in_messages():
    """Secret-shaped substrings inside conversation context must be scrubbed."""
    from ouroboros.safety import _build_check_prompt

    args = {"cmd": "echo hi"}
    messages = [
        {"role": "user", "content": "use this: sk-abcdef1234567890abcdefABCDEF"},
        {"role": "assistant", "content": "ok"},
    ]
    prompt = _build_check_prompt("run_command", args, messages)

    assert "sk-abcdef1234567890abcdef" not in prompt
    assert "REDACTED" in prompt


def test_build_check_prompt_tolerates_non_json_argument_values():
    """Arbitrary objects as tool args must not crash the safety prompt."""
    from ouroboros.safety import _build_check_prompt

    class Weird:
        def __repr__(self) -> str:  # pragma: no cover — trivial
            return "<Weird:ok>"

    args = {"obj": Weird(), "count": 3}
    prompt = _build_check_prompt("unknown_tool", args)

    assert "Weird:ok" in prompt or "Weird" in prompt


def test_build_check_prompt_includes_runtime_mode(monkeypatch):
    from ouroboros.safety import _build_check_prompt

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    prompt = _build_check_prompt("run_command", {"command": "ls"})

    assert "Runtime mode: pro" in prompt


def test_unknown_tool_with_secret_arg_does_not_leak_to_llm(monkeypatch):
    """End-to-end: secrets in an unknown-tool arg never reach the LLM message body."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    # Ensure a remote key is visible so routing goes to LLM (not the skip path).
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-routing")
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    ok, _ = check_safety(
        "totally_new_tool_with_secret",
        {"api_key": "sk-leakysecret1234567890abcdef"},
    )
    assert ok is True

    assert len(stub.calls) == 1
    payload = json.dumps(stub.calls[0]["messages"])
    assert "sk-leakysecret" not in payload
    assert "REDACTED" in payload


# ---------------------------------------------------------------------------
# Local-only / misconfigured routing fallback
# ---------------------------------------------------------------------------


def test_unknown_tool_under_local_only_config_uses_local_light(monkeypatch):
    """When no remote key is set but USE_LOCAL_LIGHT is enabled, route to local."""
    from ouroboros.safety import check_safety

    for k in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USE_LOCAL_LIGHT", "true")

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    ok, _ = check_safety("totally_new_tool_local_only", {"arg": 1})
    assert ok is True
    assert len(stub.calls) == 1
    assert stub.calls[0]["use_local"] is True


def test_minimax_key_enables_remote_safety_routing(monkeypatch):
    from ouroboros.safety import (
        _REMOTE_PROVIDER_KEYS,
        _any_remote_provider_configured,
        _light_model_has_reachable_provider,
    )

    for key in _REMOTE_PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)

    assert not _any_remote_provider_configured()
    assert not _light_model_has_reachable_provider("minimax::MiniMax-M2.7")

    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-key")

    assert _any_remote_provider_configured()
    assert _light_model_has_reachable_provider("minimax::MiniMax-M2.7")


def test_unknown_tool_with_no_safety_backend_fails_open_with_warning(monkeypatch):
    """When the runtime has neither remote keys nor local routing configured,"""
    from ouroboros.safety import check_safety

    for k in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
        "USE_LOCAL_MAIN",
        "USE_LOCAL_HEAVY",
        "USE_LOCAL_LIGHT",
        "USE_LOCAL_FALLBACK",
    ):
        monkeypatch.delenv(k, raising=False)

    stub = _StubLLMClient('{"status":"DANGEROUS","reason":"should not be called"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("totally_new_tool_misconfigured", {"arg": 1})
    assert ok is True
    assert "SAFETY_WARNING" in msg
    assert "not configured" in msg
    assert stub.calls == [], "misconfigured routing must not reach the LLM"


def test_openrouter_only_with_direct_provider_light_model_fails_open(monkeypatch):
    """Provider-mismatch: OPENROUTER_API_KEY set but OUROBOROS_MODEL_LIGHT
    points at a direct provider (anthropic::/openai::/...) whose key is
    absent. The direct call would raise and turn every POLICY_CHECK into
    SAFETY_VIOLATION — fail open with a visible warning instead.
    """
    from ouroboros.safety import check_safety

    for k in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
        "USE_LOCAL_MAIN",
        "USE_LOCAL_HEAVY",
        "USE_LOCAL_LIGHT",
        "USE_LOCAL_FALLBACK",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-fake")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic::claude-sonnet-4-6")

    stub = _StubLLMClient('{"status":"DANGEROUS","reason":"should not reach here"}')
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("totally_new_tool_mismatch", {"arg": 1})
    assert ok is True
    assert "SAFETY_WARNING" in msg
    assert "provider key missing" in msg or "not configured" in msg
    assert stub.calls == []


def test_mixed_remote_local_provider_mismatch_local_failure_fails_open(monkeypatch):
    """Edge case flagged in review pass 10: remote key set, light-model"""
    from ouroboros.safety import check_safety

    for k in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
        "USE_LOCAL_LIGHT",
        "USE_LOCAL_HEAVY",
        "USE_LOCAL_FALLBACK",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-fake")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic::claude-sonnet-4.6")
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")

    stub = _StubLLMClient("unused", raise_exc=RuntimeError("local server down"))
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("totally_new_tool_mixed_config", {"arg": 1})
    assert ok is True
    assert "SAFETY_WARNING" in msg
    assert "Local safety runtime unreachable" in msg


def test_explicit_local_light_failure_still_blocks(monkeypatch):
    """When USE_LOCAL_LIGHT is explicitly opted-in, local is PRIMARY, not
    fallback, so a local transport failure must NOT silently fail open —
    that would hide a real misconfiguration the operator asked for."""
    from ouroboros.safety import check_safety

    for k in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USE_LOCAL_LIGHT", "true")

    stub = _StubLLMClient("unused", raise_exc=RuntimeError("local down"))
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("some_tool", {"arg": 1})
    assert ok is False
    assert "SAFETY_VIOLATION" in msg


def test_local_fallback_runtime_error_fails_open_with_warning(monkeypatch):
    """When local routing is configured but the local runtime is unreachable,
    the safety check must fail open with a warning instead of blocking every
    tool. This protects local-only installs from being locked out of unknown
    tools when the local server is momentarily down."""
    from ouroboros.safety import check_safety

    for k in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    stub = _StubLLMClient("unused", raise_exc=RuntimeError("local server down"))
    _patch_llm_client(monkeypatch, stub)

    ok, msg = check_safety("totally_new_tool_local_down", {"arg": 1})
    assert ok is True
    assert "SAFETY_WARNING" in msg
    assert "Local safety runtime unreachable" in msg


def test_inline_secret_inside_cmd_array_is_redacted(monkeypatch):
    """Inline secret shapes inside positional / list arguments (e.g. cmd=[...])
    must also be scrubbed — the reviewer flagged that key-level redaction
    alone is not enough for shell-style tools."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    secret = "sk-leakysecret1234567890abcdef"
    ok, _ = check_safety(
        "run_command",
        {"cmd": ["curl", "-H", f"Authorization: Bearer {secret}", "https://example.com"]},
    )
    assert ok is True
    assert len(stub.calls) == 1
    payload = json.dumps(stub.calls[0]["messages"])
    assert secret not in payload
    assert "Bearer " not in payload or "REDACTED" in payload


# ---------------------------------------------------------------------------
# run_shell whitelist tightening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["pip", "install", "evil-package"],
        "pip install evil-package",
        ["pip", "uninstall", "-y", "setuptools"],
    ],
)
def test_direct_pip_mutations_do_not_bypass_llm(cmd):
    """pip install / uninstall must route to the LLM check, not the whitelist."""
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(cmd) == "", (
        "pip is mutative and must not appear in SAFE_SHELL_COMMANDS"
    )


def test_python_m_pytest_still_whitelisted_after_pip_removal():
    """Removing pip from the shell whitelist must not regress pytest routing."""
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(["python3", "-m", "pytest", "-q"]) == "pytest"


def test_check_conditional_is_only_process_tools():
    """POLICY_CHECK_CONDITIONAL applies only to command-running tools; another tool
    using it must extend the safe-subject extraction in _run_llm_check or it would
    silently bypass the LLM via the shell whitelist. verify_and_record runs its
    declared `check` like run_command (its `check` arg IS wired into the extractor)."""
    from ouroboros.safety import TOOL_POLICY, POLICY_CHECK_CONDITIONAL

    conditional = {n for n, p in TOOL_POLICY.items() if p == POLICY_CHECK_CONDITIONAL}
    assert conditional == {"run_command", "run_script", "start_service", "verify_and_record"}, (
        "Extend _run_llm_check if you add another check_conditional tool; "
        f"found: {conditional}"
    )


# ---------------------------------------------------------------------------
# Segment-aware secret key matching (no over-redaction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apikey",
        "OPENAI_API_KEY",
        "secret",
        "access_token",
        "auth_token",
        "Authorization",
        "password",
        "session_token",
    ],
)
def test_secret_key_segments_are_redacted(key):
    from ouroboros.safety import _is_secret_key

    assert _is_secret_key(key), f"{key!r} should be classified as secret"


@pytest.mark.parametrize(
    "key",
    [
        "override_author",  # PR intake arg — must be preserved
        "author",
        "authored_date",
        "coauthor",
        "primary_key",     # DB-style key — not a credential
        "key_path",        # filesystem path field
        "path",
        "title",
        "body",
    ],
)
def test_non_secret_keys_are_not_redacted(key):
    from ouroboros.safety import _is_secret_key

    assert not _is_secret_key(key), f"{key!r} should NOT be classified as secret"


def test_secret_crossing_truncation_boundary_is_still_redacted():
    """Redaction must run BEFORE the 500-char message truncation so a
    Bearer-style token that straddles the cutoff can't evade the regex."""
    from ouroboros.safety import _format_messages_for_safety

    secret = "sk-crossingboundary1234567890ABCDEF"
    # Place the secret so it starts well before the 500-char cutoff but
    # extends past it; the pre-truncation redaction must catch the whole shape.
    prefix = "A" * 480
    long_text = prefix + secret + "B" * 200
    output = _format_messages_for_safety([
        {"role": "user", "content": long_text},
    ])

    assert "sk-crossingboundary" not in output
    assert "REDACTED" in output


def test_check_safety_tolerates_none_arguments(monkeypatch):
    """An LLM that serialises a tool call without arguments passes None here;
    the check must not AttributeError before routing to the policy."""
    from ouroboros.safety import check_safety

    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}')
    _patch_llm_client(monkeypatch, stub)

    ok, _ = check_safety("totally_unknown_tool", None)
    assert ok is True


def test_override_author_argument_survives_redaction():
    """Regression for over-redaction: the documented ``override_author`` field
    on ``cherry_pick_pr_commits`` must reach the safety LLM intact so the
    model can evaluate the author-rewrite request on its merits."""
    from ouroboros.safety import _build_check_prompt

    args = {"override_author": {"name": "Alice", "email": "alice@example.com"}}
    prompt = _build_check_prompt("cherry_pick_pr_commits", args)

    assert "Alice" in prompt
    assert "alice@example.com" in prompt
    assert "REDACTED" not in prompt


# ---------------------------------------------------------------------------
# python-interpreter argv parsing hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # Script path appears before -m — the -m belongs to the script, not python.
        ["python3", "malicious.py", "-m", "pytest"],
        "python3 malicious.py -m pytest -q",
        # Multiple positional args.
        ["python", "./tool.py", "arg1", "-m", "pytest"],
        # Explicit "--" terminator.
        ["python3", "--", "-m", "pytest"],
    ],
)
def test_script_with_m_pytest_does_not_bypass_llm(cmd):
    """python <script> -m pytest must NOT be whitelisted — the -m flag
    belongs to the script, not to the interpreter."""
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(cmd) == ""


# ---------------------------------------------------------------------------
# Usage-accounting branch coverage (resolved_model / provider / source)
# ---------------------------------------------------------------------------


def _capture_usage_event(monkeypatch) -> dict:
    """Patch emit_llm_usage_event and return a dict that records the last call."""
    captured: dict = {}

    def _fake_emit(event_queue, task_id, model_name, usage, cost, *, category, provider, source, **kwargs):
        captured.update({
            "event_queue": event_queue,
            "task_id": task_id,
            "model_name": model_name,
            "usage": usage,
            "cost": cost,
            "category": category,
            "provider": provider,
            "source": source,
            **kwargs,
        })

    import ouroboros.safety as safety
    monkeypatch.setattr(safety, "emit_llm_usage_event", _fake_emit)
    return captured


def test_usage_event_uses_resolved_model_and_inferred_provider_on_openrouter(monkeypatch):
    """OpenRouter-routed safety call: provider must come from usage (or be
    inferred from the raw model), source must be ``safety_check``, and the
    emitted model identity must prefer ``usage['resolved_model']``."""
    from ouroboros.safety import check_safety

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic/claude-sonnet-4.6")

    usage_payload = {
        "resolved_model": "anthropic/claude-sonnet-4.6",
        "provider": "openrouter",
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "cost": 0.0007,
    }
    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}', usage=usage_payload)
    _patch_llm_client(monkeypatch, stub)

    captured = _capture_usage_event(monkeypatch)

    class _Ctx:
        event_queue = object()
        task_id = "t-openrouter"

    ok, _ = check_safety("create_github_issue", {"title": "x"}, ctx=_Ctx())
    assert ok is True
    assert captured["provider"] == "openrouter"
    assert captured["source"] == "safety_check"
    assert captured["category"] == "safety"
    assert captured["model_name"] == "anthropic/claude-sonnet-4.6"
    assert captured["task_id"] == "t-openrouter"


def test_usage_event_uses_direct_provider_when_resolved_by_client(monkeypatch):
    """Direct-provider safety call: provider from usage must win over the
    hardcoded ``openrouter`` default so /api/cost-breakdown attributes the
    spend correctly."""
    from ouroboros.safety import check_safety

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic::claude-sonnet-4.6")
    # Provider-prefixed light model needs its provider key reachable; the
    # autouse fixture only seeds OPENROUTER_API_KEY.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")

    usage_payload = {
        "resolved_model": "anthropic/claude-sonnet-4-6",
        "provider": "anthropic",
        "prompt_tokens": 200,
        "completion_tokens": 50,
        "cost": 0.0,  # force estimate path
    }
    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}', usage=usage_payload)
    _patch_llm_client(monkeypatch, stub)

    captured = _capture_usage_event(monkeypatch)

    class _Ctx:
        event_queue = object()
        task_id = "t-anthropic"

    ok, _ = check_safety("create_github_issue", {"title": "x"}, ctx=_Ctx())
    assert ok is True
    assert captured["provider"] == "anthropic"
    assert captured["model_name"] == "anthropic/claude-sonnet-4-6"
    # cost should be non-zero after estimate path even though usage.cost=0.
    assert isinstance(captured["cost"], float)


def test_no_event_queue_preserves_unknown_cost_in_budget_fallback(monkeypatch):
    """When ctx is None (or ctx.event_queue is missing), the safety path must
    attribute spend via ``supervisor.state.update_budget_from_usage`` instead
    of emitting an ``llm_usage`` event — otherwise direct-provider safety
    calls made outside the supervisor context would never be counted."""
    from ouroboros.safety import check_safety
    import ouroboros.safety as safety_mod

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic/claude-sonnet-4.6")

    usage_payload = {
        "resolved_model": "anthropic/claude-sonnet-4.6",
        "provider": "anthropic",
        "prompt_tokens": 200,
        "completion_tokens": 50,
        "cost": None,
    }
    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}', usage=usage_payload)
    _patch_llm_client(monkeypatch, stub)

    # Emit should never be called on this branch.
    def _explode(*args, **kwargs):  # pragma: no cover — guardrail
        raise AssertionError("emit_llm_usage_event must not be called when ctx has no event_queue")
    monkeypatch.setattr(safety_mod, "emit_llm_usage_event", _explode)

    captured: list[dict] = []

    def _record(usage):
        captured.append(dict(usage))

    monkeypatch.setattr(safety_mod, "update_budget_from_usage", _record)

    # ctx=None path
    ok, _ = check_safety("create_github_issue", {"title": "x"}, ctx=None)
    assert ok is True
    assert len(captured) == 1
    # Direct Anthropic has no automatic catalog. The fallback must preserve
    # unknown rather than fabricating a price or silently attributing $0.
    assert captured[0]["cost"] is None
    assert captured[0]["prompt_tokens"] == 200

    # ctx present but without event_queue path
    class _CtxNoQueue:
        task_id = "t-no-queue"

    captured.clear()
    stub.calls.clear()
    ok2, _ = check_safety("create_github_issue", {"title": "y"}, ctx=_CtxNoQueue())
    assert ok2 is True
    assert len(captured) == 1


def test_usage_event_uses_local_provider_when_use_local_light(monkeypatch):
    """Local routing: provider must be ``local`` and model_name annotated."""
    from ouroboros.safety import check_safety

    for k in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USE_LOCAL_LIGHT", "true")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "local-light-model")

    usage_payload = {
        "prompt_tokens": 30,
        "completion_tokens": 10,
        "cost": 0.0,
    }
    stub = _StubLLMClient('{"status":"SAFE","reason":"ok"}', usage=usage_payload)
    _patch_llm_client(monkeypatch, stub)

    captured = _capture_usage_event(monkeypatch)

    class _Ctx:
        event_queue = object()
        task_id = "t-local"

    ok, _ = check_safety("create_github_issue", {"title": "x"}, ctx=_Ctx())
    assert ok is True
    assert captured["provider"] == "local"
    assert "(local)" in captured["model_name"]


# ---------------------------------------------------------------------------
# Rate-limit fail-open + bounded transcript (OB-02)
# ---------------------------------------------------------------------------


class _ScriptedLLMClient:
    """``_StubLLMClient`` with a per-call script: each entry is an Exception to raise or a
    ``(content, usage)`` tuple to return. The last entry repeats, so a one-element script
    models a provider that keeps failing the same way."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, *, messages, model, use_local, **kwargs):
        self.calls.append({"messages": messages, "model": model, "use_local": use_local, **kwargs})
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        content, usage = step
        return {"content": content}, usage


class _RateLimitError(Exception):
    """Exception-shaped 429 (the shape `classify_llm_exception` reads a status from)."""

    status_code = 429


class _QuotaError(Exception):
    """Structured insufficient-quota that ALSO carries HTTP 429 — the case whose
    PERMANENT classification must win over the status code and keep blocking."""

    status_code = 429
    code = "insufficient_quota"


# The production shape: HTTP 200 whose BODY carried the rate limit, surfaced by
# ``llm._normalize_remote_response`` as a typed marker on ``usage``. Nothing raises.
_BODY_RATE_LIMIT_USAGE = {
    "provider_error": {
        "code": "429",
        "type": "rate_limit_error",
        "message": "Rate limit exceeded",
        "kind": "rate_limit",
    },
    "prompt_tokens": 12,
    "completion_tokens": 0,
    "cost": 0.0,
}


class _DriveCtx:
    """ToolContext-shaped stub whose durable safety events land in a REAL ``events.jsonl``
    under tmp_path — the audit surface the fix must write, read back through the same
    file an owner would open."""

    def __init__(self, root):
        self.task_id = "t-safety"
        self.drive_root = str(root)
        self._logs = pathlib.Path(root) / "logs"
        self._logs.mkdir(parents=True, exist_ok=True)

    def drive_logs(self):
        return self._logs


def _read_events(ctx, event_type: str | None = None) -> list[dict]:
    path = ctx.drive_logs() / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if event_type is None or r.get("type") == event_type]


@pytest.fixture
def _no_backoff(monkeypatch):
    """Skip the real 2s sleep. The backoff's PLACEMENT is asserted separately by
    ``test_rate_limit_retry_takes_one_slot_per_attempt``."""
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "_safety_rate_limit_backoff", lambda ctx: None)


def test_exception_shaped_rate_limit_fails_open_after_one_retry(monkeypatch, tmp_path, _no_backoff):
    """Two 429s: allow WITH the unchecked warning, and leave a durable audit row."""
    from ouroboros.safety import _UNCHECKED_WARNING_SUFFIX, check_safety

    stub = _ScriptedLLMClient([_RateLimitError("Rate limit exceeded")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is True, "a 429 is an infra fact about the supervisor, not a tool verdict"
    assert "SAFETY_WARNING" in msg
    assert "rate-limited" in msg
    assert _UNCHECKED_WARNING_SUFFIX in msg
    assert "SAFETY_VIOLATION" not in msg
    assert len(stub.calls) == 2, "exactly one retry, then fail open"

    rows = _read_events(ctx, "safety_check_rate_limited")
    assert len(rows) == 1
    assert rows[0]["tool"] == "create_github_issue"
    assert rows[0]["action"] == "fail_open_after_retry"
    assert rows[0]["error"], "the audit row carries the sanitized bounded error"
    assert rows[0]["task_id"] == "t-safety"


def test_http200_body_rate_limit_fails_open_after_one_retry(monkeypatch, tmp_path, _no_backoff):
    """THE production shape: HTTP 200, empty content, ``usage['provider_error']``. Nothing
    raises, so an exception-only check would still walk the unparseable-response repair
    path into SAFETY_VIOLATION — this fails with the bug alive even when (a) passes."""
    import ouroboros.safety as safety
    from ouroboros.safety import _UNCHECKED_WARNING_SUFFIX, check_safety

    monkeypatch.setattr(safety, "update_budget_from_usage", lambda usage: None)
    stub = _ScriptedLLMClient([("", dict(_BODY_RATE_LIMIT_USAGE))])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is True
    assert "SAFETY_WARNING" in msg and "rate-limited" in msg
    assert _UNCHECKED_WARNING_SUFFIX in msg
    assert len(stub.calls) == 2
    assert len(_read_events(ctx, "safety_check_rate_limited")) == 1
    # A rate limit is not an unparseable verdict: the parse-repair lane must not run.
    assert _read_events(ctx, "safety_parse_retry") == []
    assert _read_events(ctx, "safety_parse_failed") == []


def test_single_rate_limit_then_success_returns_the_normal_verdict(monkeypatch, tmp_path, _no_backoff):
    """One 429 then a real verdict: ordinary result, no fail-open, no audit row."""
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([
        _RateLimitError("Rate limit exceeded"),
        ('{"status":"SAFE","reason":"ok"}', None),
    ])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is True
    assert msg == "", "a recovered check returns the ordinary SAFE verdict"
    assert len(stub.calls) == 2
    assert _read_events(ctx) == [], "no audit row when the retry actually succeeded"


def test_structured_insufficient_quota_with_429_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """PERMANENT precedence: an insufficient-quota carried on a 429 keeps TODAY'S
    blocking path — one attempt, the exact existing message, no fail-open."""
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([_QuotaError("Rate limit exceeded (insufficient_quota)")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False
    assert msg == (
        "⚠️ SAFETY_VIOLATION: Safety check failed with error: "
        "_QuotaError: Rate limit exceeded (insufficient_quota)"
    )
    assert len(stub.calls) == 1, "a permanent class must not buy a retry"
    assert _read_events(ctx, "safety_check_rate_limited") == []


def test_non_rate_limit_exception_keeps_todays_violation_path(monkeypatch, tmp_path, _no_backoff):
    """Every other exception class is byte-identical to today: block, one attempt."""
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([RuntimeError("network down")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False
    assert msg == (
        "⚠️ SAFETY_VIOLATION: Safety check failed with error: RuntimeError: network down"
    )
    assert len(stub.calls) == 1
    assert _read_events(ctx) == []


def test_conversation_section_is_bounded_newest_first_with_counted_marker():
    """The CONVERSATION section is bounded, keeps the NEWEST rounds, and discloses
    the exact number it dropped. The tool-arguments section is untouched."""
    import re

    from ouroboros.safety import _SAFETY_CONTEXT_CHAR_BUDGET, _build_check_prompt

    messages = [{"role": "user", "content": f"round{i} " + "x" * 400} for i in range(40)]
    messages[-1] = {"role": "user", "content": "NEWEST_ROUND_MARKER"}

    prompt = _build_check_prompt("run_command", {"cmd": ["echo", "hello"]}, messages)

    head, context = prompt.split("Conversation context:\n", 1)
    context = context.rsplit("\nIs this safe?", 1)[0]

    assert len(context) <= _SAFETY_CONTEXT_CHAR_BUDGET
    assert "NEWEST_ROUND_MARKER" in context, "the newest round must survive"
    assert "round0 " not in context, "the oldest rounds must be dropped"

    lines = context.splitlines()
    matched = re.match(r"^\[… (\d+) older messages omitted\]$", lines[0])
    assert matched, f"missing counted omission marker: {lines[0]!r}"
    assert int(matched.group(1)) == len(messages) - (len(lines) - 1)

    # The proposed call is the SUBJECT of the check and stays outside the budget.
    assert '"cmd"' in head and "hello" in head


def test_short_conversation_keeps_every_message_and_no_marker():
    """Under budget: no marker, nothing dropped (the marker is not decoration)."""
    from ouroboros.safety import _format_messages_for_safety

    rendered = _format_messages_for_safety([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ])

    assert rendered == "[user] first\n[assistant] second"
    assert "omitted" not in rendered


def test_rate_limit_retry_takes_one_slot_per_attempt(monkeypatch, tmp_path):
    """model_concurrency caps CONCURRENT calls, so the retry must take a FRESH slot
    and the backoff must sleep BETWEEN slot contexts — never inside a held one."""
    import contextlib

    import ouroboros.safety as safety
    from ouroboros import model_concurrency
    from ouroboros.safety import check_safety

    state = {"acquired": 0, "depth": 0, "slept_at_depth": []}

    @contextlib.contextmanager
    def _counting_slot(model, use_local=False, deadline_ts=None):
        state["acquired"] += 1
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1

    monkeypatch.setattr(model_concurrency, "model_call_slot", _counting_slot)
    monkeypatch.setattr(
        safety, "_safety_rate_limit_backoff",
        lambda ctx: state["slept_at_depth"].append(state["depth"]),
    )

    stub = _ScriptedLLMClient([_RateLimitError("Rate limit exceeded")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, _ = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is True
    assert state["acquired"] == 2, "one slot per attempt, not one slot around both"
    assert state["slept_at_depth"] == [0], "the backoff must not run inside a held slot"
    assert state["depth"] == 0, "every acquired slot is released"


def test_rate_limit_backoff_is_capped_by_the_task_deadline(monkeypatch):
    """The one sleep is bounded by the REAL task deadline; an expired deadline
    skips it entirely rather than sleeping past the task."""
    import ouroboros.safety as safety

    slept: list[float] = []
    monkeypatch.setattr(safety.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(safety, "_safety_deadline_epoch", lambda ctx: safety.time.time() + 0.25)

    safety._safety_rate_limit_backoff(None)
    assert slept and slept[0] <= 0.25 < safety._SAFETY_RATE_LIMIT_BACKOFF_SEC

    slept.clear()
    monkeypatch.setattr(safety, "_safety_deadline_epoch", lambda ctx: safety.time.time() - 5)
    safety._safety_rate_limit_backoff(None)
    assert slept == [], "an expired deadline must not sleep at all"


class _ServerError(Exception):
    """A 503 outage: `classify_llm_exception` calls it `provider_transient`, but it is
    NOT throttling, so the safety lane must keep blocking on it."""

    status_code = 503


def test_server_outage_is_not_a_rate_limit_and_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """5xx is an outage, not throughput: today's blocking path, one attempt, no audit."""
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([_ServerError("Service Unavailable")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False
    assert msg == (
        "⚠️ SAFETY_VIOLATION: Safety check failed with error: "
        "_ServerError: Service Unavailable"
    )
    assert len(stub.calls) == 1, "an outage must not buy the rate-limit retry"
    assert _read_events(ctx) == []


def test_read_timeout_is_not_a_rate_limit_and_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """A transport timeout also classifies `provider_transient`; it must still block."""
    from ouroboros.safety import check_safety

    class _ReadTimeout(Exception):
        pass

    stub = _ScriptedLLMClient([_ReadTimeout("The read operation timed out")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False
    assert "SAFETY_VIOLATION" in msg
    assert len(stub.calls) == 1
    assert _read_events(ctx) == []


def test_http200_body_transient_that_is_not_429_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """Body lane: only `kind == "rate_limit"` (llm.py assigns it solely to a transient
    body error whose code IS 429) waves through; a body `provider_transient` keeps the
    existing unparseable-response outcome."""
    import ouroboros.safety as safety
    from ouroboros.safety import check_safety

    monkeypatch.setattr(safety, "update_budget_from_usage", lambda usage: None)
    usage = {"provider_error": {"code": "502", "type": "server_error",
                                "message": "Bad gateway", "kind": "provider_transient"},
             "prompt_tokens": 5, "completion_tokens": 0, "cost": 0.0}
    stub = _ScriptedLLMClient([("", usage)])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False
    assert "SAFETY_VIOLATION" in msg
    assert _read_events(ctx, "safety_check_rate_limited") == []


def test_local_fallback_lane_rate_limit_takes_the_audited_fail_open(monkeypatch, tmp_path, _no_backoff):
    """Disclosed nuance: on the local-FALLBACK lane a genuine 429-shaped error takes the
    two-attempt fail-open WITH the audit row, while every other error keeps its unchanged
    one-attempt 'Local safety runtime unreachable' warning. Both allow."""
    from ouroboros.safety import check_safety

    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY",
              "OPENAI_COMPATIBLE_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY",
              "GIGACHAT_CREDENTIALS", "USE_LOCAL_LIGHT", "USE_LOCAL_HEAVY",
              "USE_LOCAL_FALLBACK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-fake")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic::claude-sonnet-4.6")
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")

    stub = _ScriptedLLMClient([_RateLimitError("Rate limit exceeded")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("totally_new_tool_mixed_config", {"arg": 1}, ctx=ctx)
    assert ok is True
    assert "rate-limited" in msg
    assert len(stub.calls) == 2
    assert len(_read_events(ctx, "safety_check_rate_limited")) == 1

    # Everything else on this lane is unchanged: one attempt, original wording.
    other = _ScriptedLLMClient([RuntimeError("local server down")])
    _patch_llm_client(monkeypatch, other)
    ctx2 = _DriveCtx(tmp_path / "second")
    ok2, msg2 = check_safety("totally_new_tool_mixed_config", {"arg": 1}, ctx=ctx2)
    assert ok2 is True
    assert "Local safety runtime unreachable" in msg2
    assert len(other.calls) == 1
    assert _read_events(ctx2) == []


def test_omission_marker_space_is_reserved_inside_the_budget():
    """Boundary case: the messages tile the budget so tightly that the MARKER'S OWN
    length decides the final cut. Without the reservation the last line still fits, the
    marker is appended after the check, and the section overflows the budget."""
    import re

    from ouroboros.safety import _SAFETY_CONTEXT_CHAR_BUDGET, _format_messages_for_safety

    # Each message renders to exactly 40 chars ("[user] " is 7).
    messages = [{"role": "user", "content": "y" * 33} for _ in range(130)]
    context = _format_messages_for_safety(messages)

    assert len(context) <= _SAFETY_CONTEXT_CHAR_BUDGET, (
        "the marker must be paid for INSIDE the budget, not appended after the check"
    )
    # Prove the case is genuinely tight: one more 40-char line would have fit if the
    # marker had cost nothing, so this test fails the moment the reserve is dropped.
    assert len(context) > _SAFETY_CONTEXT_CHAR_BUDGET - 80

    lines = context.splitlines()
    matched = re.match(r"^\[… (\d+) older messages omitted\]$", lines[0])
    assert matched, f"missing counted omission marker: {lines[0]!r}"
    assert int(matched.group(1)) == len(messages) - (len(lines) - 1)


def test_marker_bearing_5xx_message_is_not_a_rate_limit_and_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """The markers are SUBSTRINGS, so `429`/`rpm`/`tpm` occur inside the request ids and
    hostnames real outages carry. A KNOWN non-429 status must never reach the text
    branch: this 503 carries `429` in its request id and must still block."""
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([
        _ServerError("503 Service Unavailable (request id: req_1f429ab0)"),
    ])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is False, "a marker inside a request id must not wave a 5xx outage through"
    assert "SAFETY_VIOLATION" in msg
    assert "req_1f429ab0" in msg
    assert len(stub.calls) == 1
    assert _read_events(ctx) == []


def test_statusless_rate_limit_text_still_fails_open(monkeypatch, tmp_path, _no_backoff):
    """The text branch keeps its own case: a bare throttling message, no status code."""
    from ouroboros.safety import check_safety

    class _BareRateLimit(Exception):
        pass

    stub = _ScriptedLLMClient([_BareRateLimit("rate limit exceeded")])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)

    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)

    assert ok is True
    assert "rate-limited" in msg
    assert len(stub.calls) == 2
    assert len(_read_events(ctx, "safety_check_rate_limited")) == 1


def test_body_shaped_quota_refusal_on_a_429_still_blocks(monkeypatch, tmp_path, _no_backoff):
    """Quota precedence holds in the HTTP-200 body shape too: permanent, one attempt,
    no fail-open, no audit row (mirrors the exception lane's quota-over-429 rule)."""
    from ouroboros.safety import check_safety

    usage = dict(_BODY_RATE_LIMIT_USAGE)
    usage["provider_error"] = dict(usage["provider_error"],
                                   type="insufficient_quota",
                                   message="insufficient_quota: billing hard limit reached")
    stub = _ScriptedLLMClient([("", usage), ("", dict(usage))])
    _patch_llm_client(monkeypatch, stub)
    ctx = _DriveCtx(tmp_path)
    ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=ctx)
    assert ok is False and "SAFETY_VIOLATION" in msg
    # today's blocking path for an empty body IS the parse lane incl. its one repair
    # retry; the point here is the rate-limit fail-open lane must NOT engage.
    assert len(stub.calls) == 2
    assert _read_events(ctx, "safety_check_rate_limited") == []


def test_safe_verdict_with_invalid_reported_cost_stays_safe(monkeypatch, tmp_path, caplog):
    """A garbage usage cost must never flip a SAFE verdict into a violation: the
    cost degrades to unknown and the verdict survives (no-fabricated-costs rule)."""
    import logging
    from ouroboros.safety import check_safety

    stub = _ScriptedLLMClient([('{"status":"SAFE","reason":"ok"}', {"cost": "abc", "prompt_tokens": 5})])
    _patch_llm_client(monkeypatch, stub)
    with caplog.at_level(logging.WARNING):
        ok, msg = check_safety("create_github_issue", {"title": "x"}, ctx=_DriveCtx(tmp_path))
    assert ok is True and msg == ""
    assert "invalid reported cost" in caplog.text
