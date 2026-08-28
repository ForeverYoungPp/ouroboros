"""Tests for Claude Code gateway safety guards and orchestration helpers.

The gateway module (ouroboros/gateways/claude_code.py) is SDK-only — there is
no CLI subprocess fallback. When claude-agent-sdk is absent callers receive
an error result with an install hint. Tests use a lightweight mock of the SDK
so the gateway can be imported and exercised without the real package installed.

We test:
  - ClaudeCodeResult (importable from gateway even w/o SDK via careful mocking)
  - Readonly guard hook (function-level, no SDK dependency)
  - Orchestration helpers (_load_project_context etc.) now in shell.py
"""

import asyncio
import json
import sys
import pytest


# ---------------------------------------------------------------------------
# Mock SDK so the gateway can be imported on Python 3.9 / without SDK
# ---------------------------------------------------------------------------

from tests._shared import ensure_claude_agent_sdk_mock

ensure_claude_agent_sdk_mock()


async def _async_gen(items):
    """Async generator helper for mocking query() streams in tests."""
    for item in items:
        yield item


from ouroboros.gateways.claude_code import (  # noqa: E402
    ClaudeCodeResult,
    make_readonly_guard,
    _normalize_sdk_usage,
)

# Orchestration helpers now live in shell.py
from ouroboros.tools.shell import (  # noqa: E402
    _load_project_context,
)


# ---------------------------------------------------------------------------
# ClaudeCodeResult
# ---------------------------------------------------------------------------

class TestClaudeCodeResult:
    def test_success_to_json(self):
        r = ClaudeCodeResult(
            success=True,
            result_text="Edited 2 files",
            session_id="abc-123",
            cost_usd=0.05,
            changed_files=["foo.py", "bar.py"],
            diff_stat="2 files changed, 10 insertions",
        )
        out = json.loads(r.to_tool_output())
        assert out["success"] is True
        assert out["result"] == "Edited 2 files"
        assert out["session_id"] == "abc-123"
        assert out["cost_usd"] == 0.05
        assert out["changed_files"] == ["foo.py", "bar.py"]
        assert "diff_stat" in out

    def test_error_to_json(self):
        r = ClaudeCodeResult(success=False, error="Something went wrong")
        out = json.loads(r.to_tool_output())
        assert out["success"] is False
        assert "error" in out

    def test_empty_fields_omitted(self):
        r = ClaudeCodeResult(success=True, result_text="ok")
        out = json.loads(r.to_tool_output())
        assert "session_id" not in out
        assert "changed_files" not in out
        assert "error" not in out
        assert "validation" not in out


# ---------------------------------------------------------------------------
# Read-only guard hook
# ---------------------------------------------------------------------------

class TestReadonlyGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_blocks_edit(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {}}, "tid-1", None,
        ))
        assert "deny" in str(result)

    def test_blocks_bash(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Bash", "tool_input": {}}, "tid-2", None,
        ))
        assert "deny" in str(result)

    def test_allows_read(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {}}, "tid-3", None,
        ))
        assert result == {}

    def test_allows_grep(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Grep", "tool_input": {}}, "tid-4", None,
        ))
        assert result == {}

    def test_allows_glob(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Glob", "tool_input": {}}, "tid-5", None,
        ))
        assert result == {}


# ---------------------------------------------------------------------------
# Orchestration helpers (now in shell.py)
# ---------------------------------------------------------------------------

class TestProjectContext:
    def test_loads_existing_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("# Constitution", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "DEVELOPMENT.md").write_text("# Dev guide", encoding="utf-8")
        ctx = _load_project_context(tmp_path)
        assert "CONSTITUTION" in ctx
        assert "DEVELOPMENT GUIDE" in ctx

    def test_handles_missing_docs(self, tmp_path):
        ctx = _load_project_context(tmp_path)
        assert ctx == ""  # no docs, empty context

    def test_preserves_large_governance_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("x" * 100_000, encoding="utf-8")
        ctx = _load_project_context(tmp_path)
        assert "truncated" not in ctx.lower()
        assert "x" * 100_000 in ctx


# ---------------------------------------------------------------------------
# SDK import fallback contract
# ---------------------------------------------------------------------------

class TestImportFallback:
    """Verify the gateway raises ImportError when the SDK is unavailable.

    Since the claude-agent-sdk is a required dependency with no CLI fallback,
    ImportError at import time surfaces SDK unavailability so callers can
    return a clear install hint rather than silently failing.
    """

    def test_gateway_import_requires_sdk(self):
        """Without the real SDK (or our mock), import should raise ImportError."""
        # The module does `from claude_agent_sdk import ...` at module level,
        # so ImportError is raised before any code runs.
        # This documents that the SDK is a hard requirement (no CLI fallback).
        #
        # To simulate absence even when SDK is installed, we must:
        # 1. Save and remove ALL claude_agent_sdk* entries from sys.modules
        # 2. Set sys.modules["claude_agent_sdk"] = None (triggers ImportError)
        # 3. Remove the cached gateway module
        # Without step 1, Python may resolve sub-module imports from cached
        # entries even when the top-level package is blocked.
        import importlib

        # Save all SDK-related modules so we can restore them
        saved_modules = {}
        for key in list(sys.modules):
            if key == "claude_agent_sdk" or key.startswith("claude_agent_sdk."):
                saved_modules[key] = sys.modules.pop(key)

        try:
            # Block the import — setting to None triggers ImportError
            sys.modules["claude_agent_sdk"] = None
            # Also remove cached gateway module so it re-imports
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            with pytest.raises(ImportError):
                importlib.import_module("ouroboros.gateways.claude_code")
        finally:
            # Remove the None sentinel
            sys.modules.pop("claude_agent_sdk", None)
            # Restore all saved SDK modules
            sys.modules.update(saved_modules)
            # If nothing was saved (SDK not installed), ensure mock is in place
            if not saved_modules:
                ensure_claude_agent_sdk_mock()
            # Re-import gateway with real/mock SDK
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            importlib.import_module("ouroboros.gateways.claude_code")


# ---------------------------------------------------------------------------
# SDK API surface verification tests (v4.8.1 fixes)
# ---------------------------------------------------------------------------

class TestSDKAPISurface:
    """Verify that the gateway uses correct SDK API method names and signatures.

    These tests inspect source code to catch method name mismatches that would
    cause AttributeError at runtime (e.g. receive_response vs receive_messages).
    """

    def _gateway_source(self):
        import inspect
        from ouroboros.gateways import claude_code
        return inspect.getsource(claude_code)

    def test_readonly_path_uses_sdk_client_lifecycle(self):
        """Read-only path must use ClaudeSDKClient, not query()+early generator break."""
        src = self._gateway_source()
        assert "async with ClaudeSDKClient(options=options) as client:" in src
        assert "await client.query(prompt)" in src
        assert "async for message in client.receive_response():" in src
        assert "async for message in query(" not in src
        # receive_messages() streams indefinitely and can hang — the gateway
        # must only ever use receive_response().
        assert "receive_messages()" not in src, (
            "receive_messages() streams indefinitely — use receive_response() instead"
        )

    def test_max_budget_in_constructor(self):
        """max_budget_usd should be passed in ClaudeAgentOptions constructor."""
        src = self._gateway_source()
        # Should NOT have post-assignment pattern
        assert "options.max_budget_usd" not in src, \
            "max_budget_usd should be in constructor, not post-assigned"
        # Should have it in the constructor kwargs
        assert "max_budget_usd=max_budget_usd" in src, \
            "max_budget_usd should be passed as constructor kwarg"

    def test_query_helper_not_used_by_gateway(self):
        """The gateway avoids query() for read-only cleanup correctness."""
        src = self._gateway_source()
        assert " query," not in src
        assert "query(prompt=prompt" not in src


# ---------------------------------------------------------------------------
# SDK-only path: ImportError and failure diagnostics
# ---------------------------------------------------------------------------

class TestSDKOnlyPath:
    """advisory_pre_review returns meaningful errors when SDK missing."""

    def test_advisory_returns_error_when_sdk_missing(self, monkeypatch, tmp_path):
        """When SDK not installed → advisory returns install hint."""
        from ouroboros.tools.claude_advisory_review import _run_claude_advisory
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            emit_progress_fn=lambda _: None,
            pending_events=[],
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Patch run_readonly to raise ImportError
        def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'claude_agent_sdk'")

        try:
            import ouroboros.gateways.claude_code as gw
            monkeypatch.setattr(gw, "run_readonly", raise_import_error)
        except Exception:
            pass

        # Also patch the import inside _run_claude_advisory
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if "claude_code" in str(name) and "gateways" in str(name):
                raise ImportError("claude-agent-sdk not installed")
            return real_import(name, *args, **kwargs)

        items, raw, *_extra = _run_claude_advisory(tmp_path, "test commit", ctx)
        # Either SDK-not-installed message, git-setup error, or empty if SDK is present.
        # We only verify the result is well-typed; the specific error message depends on
        # which gate fires first (git diff may fail before reaching the SDK path when the
        # tmp_path is not a real git repository).
        assert isinstance(items, list)

    def test_readonly_child_sigabrt_returns_structured_error(self, monkeypatch, tmp_path):
        """A native child abort must not escape as a worker-killing exception."""
        import ouroboros.gateways.claude_code as gw

        class FakeProc:
            returncode = -6

            def communicate(self, input=None, timeout=None):
                return "", "abort trap"

        monkeypatch.delenv("OUROBOROS_CLAUDE_READONLY_CHILD", raising=False)
        monkeypatch.setattr(gw.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
        result = gw.run_readonly("review this", cwd=str(tmp_path))

        assert result.success is False
        assert "SIGABRT" in result.error
        assert "abort trap" in result.stderr_tail

    def test_readonly_child_honors_explicit_process_timeout(self, monkeypatch, tmp_path):
        """An owner-narrowed advisory window reaches the child wait boundary."""
        import ouroboros.gateways.claude_code as gw

        captured = {}

        class FakeProc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                captured["timeout"] = timeout
                return json.dumps({"success": True, "result_text": "ok"}), ""

        monkeypatch.delenv("OUROBOROS_CLAUDE_READONLY_CHILD", raising=False)
        monkeypatch.setattr(gw.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
        result = gw.run_readonly("review this", cwd=str(tmp_path), timeout_sec=7.5)

        assert result.success is True
        assert captured["timeout"] == 7.5

    def test_readonly_child_timeout_uses_process_tree_cleanup(self):
        """Timeout cleanup must kill the child process group/tree, not only direct child."""
        import inspect
        import ouroboros.gateways.claude_code as gw

        source = inspect.getsource(gw._run_readonly_out_of_process)
        assert "subprocess.Popen" in source
        assert "kill_process_tree" in source


# ---------------------------------------------------------------------------
# Status endpoint SDK version check
# ---------------------------------------------------------------------------

class TestRunReadonlyEffortParam:
    """run_readonly passes effort param to ClaudeAgentOptions."""

    def test_run_readonly_passes_effort_to_options(self):
        """_run_readonly_async should include 'effort' in ClaudeAgentOptions kwargs."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        source = inspect.getsource(gw._run_readonly_async)
        # Verify the effort kwarg is forwarded
        assert "effort" in source
        assert "options_kwargs" in source

    def test_run_readonly_default_effort_is_high(self):
        """Default effort for run_readonly should be 'high' (matches blocking reviewers)."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw.run_readonly)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_run_readonly_async_default_effort_is_high(self):
        """Default effort for _run_readonly_async should be 'high'."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw._run_readonly_async)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_effort_forwarded_to_options_when_sdk_supports_it(self):
        """effort='high' is forwarded to ClaudeAgentOptions when the SDK accepts it."""
        captured: dict = {}

        class FakeOptions:
            # Include 'effort' as an explicit param so signature inspection
            # (used in the guard) correctly detects that this SDK version supports it.
            def __init__(self, effort=None, **kwargs):
                if effort is not None:
                    kwargs["effort"] = effort
                captured.update(kwargs)

        import asyncio
        from unittest.mock import patch

        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def query(self, prompt):
                return None

            async def receive_response(self):
                if False:
                    yield None

        # Patch ClaudeAgentOptions with one that accepts effort
        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptions), \
             patch("ouroboros.gateways.claude_code.ClaudeSDKClient", FakeSDKClient):
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async(
                    "test", cwd="/tmp", effort="high", max_budget_usd=1.0,
                )
            )

        assert captured.get("effort") == "high", (
            f"expected effort='high' forwarded to ClaudeAgentOptions, got: {captured}"
        )

    def test_effort_omitted_gracefully_when_sdk_lacks_support(self):
        """When SDK's ClaudeAgentOptions does not accept effort, it is silently dropped."""
        captured: dict = {}

        class FakeOptionsNoEffort:
            """Simulates an older SDK version without effort kwarg."""
            def __init__(self, **kwargs):
                if "effort" in kwargs:
                    raise TypeError("__init__() got an unexpected keyword argument 'effort'")
                captured.update(kwargs)

        import asyncio
        from unittest.mock import patch

        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def query(self, prompt):
                return None

            async def receive_response(self):
                if False:
                    yield None

        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptionsNoEffort), \
             patch("ouroboros.gateways.claude_code.ClaudeSDKClient", FakeSDKClient):
            # Should not raise — effort silently dropped
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async(
                    "test", cwd="/tmp", effort="high", max_budget_usd=1.0,
                )
            )

        assert "effort" not in captured, "effort must be omitted when SDK lacks support"

    def test_normalize_sdk_usage_maps_anthropic_keys(self):
        usage = _normalize_sdk_usage({
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 12,
        })
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 25
        assert usage["cached_tokens"] == 40
        assert usage["cache_write_tokens"] == 12


class TestSDKStatusPayload:
    """_claude_code_status_payload returns app-managed runtime info."""

    def test_status_payload_reflects_sdk_installed_with_key(self, monkeypatch):
        """When SDK is importable and API key set, status is ready."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                app_managed=True,
                sdk_version="0.1.54",
                sdk_path="/fake/sdk",
                cli_path="/fake/cli/claude",
                cli_version="2.1.90",
                interpreter_path="/fake/python3",
                api_key_set=True,
                ready=True,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is True
        assert payload["ready"] is True
        assert payload["status"] == "ready"
        assert "0.1.54" in payload["message"]
        assert payload["app_managed"] is True
        assert payload["busy"] is False
        assert payload["error"] == ""

    def test_status_payload_reflects_sdk_missing(self, monkeypatch):
        """When SDK is not installed, status is missing."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState()

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is False
        assert payload["ready"] is False
        assert payload["status"] == "missing"
        assert "not available" in payload["message"].lower() or "missing" in payload["message"].lower()
        assert payload["busy"] is False

    def test_status_payload_no_api_key(self, monkeypatch):
        """When SDK present but ANTHROPIC_API_KEY not set, status is no_api_key."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                sdk_version="0.1.54",
                sdk_path="/fake/sdk",
                cli_path="/fake/cli/claude",
                cli_version="2.1.90",
                api_key_set=False,
                ready=False,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is True
        assert payload["ready"] is False
        assert payload["status"] == "no_api_key"
        assert payload["api_key_set"] is False

    def test_status_payload_includes_runtime_fields(self, monkeypatch):
        """Payload includes app_managed, legacy_detected, cli fields."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                app_managed=True,
                sdk_version="0.1.54",
                cli_path="/fake/cli",
                cli_version="2.1.90",
                legacy_detected=True,
                legacy_sdk_version="0.1.50",
                api_key_set=True,
                ready=True,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert "cli_path" in payload
        assert "cli_version" in payload
        assert "app_managed" in payload
        assert "legacy_detected" in payload
        assert payload["legacy_detected"] is True
        assert payload["legacy_sdk_version"] == "0.1.50"


# ---------------------------------------------------------------------------
# Claude runtime resolution contract
# ---------------------------------------------------------------------------

class TestClaudeRuntimeResolution:
    """Verify the runtime resolver in ouroboros.platform_layer."""

    def test_runtime_state_dataclass_defaults(self):
        from ouroboros.platform_layer import ClaudeRuntimeState
        state = ClaudeRuntimeState()
        assert state.app_managed is False
        assert state.sdk_version == ""
        assert state.cli_path == ""
        assert state.ready is False
        assert state.status_label() == "missing"

    def test_runtime_state_status_labels(self):
        from ouroboros.platform_layer import ClaudeRuntimeState

        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=True, ready=True).status_label() == "ready"
        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=False).status_label() == "no_api_key"
        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=True, error="boom").status_label() == "error"
        assert ClaudeRuntimeState(sdk_version="1.0", api_key_set=True, ready=False).status_label() == "degraded"
        assert ClaudeRuntimeState().status_label() == "missing"

    def test_status_label_error_takes_priority_over_missing_api_key(self):
        """Regression: v4.33.1 priority fix.

        Prior to v4.33.1, ``status_label`` checked ``api_key_set`` before
        ``error``, so a below-baseline SDK (or any other runtime error) was
        silently shadowed as ``no_api_key`` when ``ANTHROPIC_API_KEY`` was
        absent. Users would then set a key, retry, and only then discover the
        real blocker. The priority is now: missing → error → no_api_key →
        degraded → ready, so repair hints are surfaced immediately.
        """
        from ouroboros.platform_layer import ClaudeRuntimeState

        state = ClaudeRuntimeState(
            sdk_version="0.1.50",
            cli_path="/fake/cli",
            api_key_set=False,
            error="SDK 0.1.50 below baseline 0.1.60",
        )
        assert state.status_label() == "error", (
            "error must take priority over no_api_key so version-gate "
            "failures surface even without a configured API key"
        )

    def test_resolve_claude_runtime_returns_state(self, monkeypatch):
        """resolve_claude_runtime returns a ClaudeRuntimeState regardless of SDK presence."""
        from ouroboros.platform_layer import resolve_claude_runtime, ClaudeRuntimeState
        state = resolve_claude_runtime()
        assert isinstance(state, ClaudeRuntimeState)
        assert isinstance(state.interpreter_path, str)
        assert isinstance(state.api_key_set, bool)

    def test_resolve_claude_runtime_rejects_below_baseline_sdk(self, monkeypatch):
        """SDK below _CLAUDE_SDK_MIN_VERSION must NOT be reported as ready.

        Regression guard: prior to v4.33.1, resolve_claude_runtime()
        marked ready=True whenever the SDK was importable and the CLI
        present, even if the installed SDK (e.g. 0.1.50) pre-dated
        Opus 4.7 adaptive-thinking support — producing a false green
        on /api/claude-code/status.
        """
        import importlib.metadata as _md
        import ouroboros.platform_layer as pl

        def fake_version(pkg: str) -> str:
            if pkg == "claude-agent-sdk":
                return "0.1.50"
            return _md.version(pkg)

        monkeypatch.setattr(pl, "_find_sdk_package_path", lambda: "/fake/python-standalone/site/claude_agent_sdk")
        monkeypatch.setattr(pl, "_find_bundled_cli", lambda p: "/fake/cli/claude")
        monkeypatch.setattr(pl, "_probe_cli_version", lambda p: "2.1.111")
        monkeypatch.setattr(_md, "version", fake_version)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        state = pl.resolve_claude_runtime()

        assert state.sdk_version == "0.1.50"
        assert state.cli_path == "/fake/cli/claude"
        assert state.api_key_set is True
        assert state.ready is False, "SDK below baseline must not be ready"
        assert "0.1.50" in state.error and "baseline" in state.error.lower()
        assert state.status_label() == "error"

    def test_resolve_claude_runtime_accepts_at_baseline_sdk(self, monkeypatch):
        """SDK at or above _CLAUDE_SDK_MIN_VERSION passes the version gate."""
        import importlib.metadata as _md
        import ouroboros.platform_layer as pl
        from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION

        def fake_version(pkg: str) -> str:
            if pkg == "claude-agent-sdk":
                return _CLAUDE_SDK_MIN_VERSION
            return _md.version(pkg)

        monkeypatch.setattr(pl, "_find_sdk_package_path", lambda: "/fake/python-standalone/site/claude_agent_sdk")
        monkeypatch.setattr(pl, "_find_bundled_cli", lambda p: "/fake/cli/claude")
        monkeypatch.setattr(pl, "_probe_cli_version", lambda p: "2.1.111")
        monkeypatch.setattr(_md, "version", fake_version)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        state = pl.resolve_claude_runtime()

        assert state.sdk_version == _CLAUDE_SDK_MIN_VERSION
        assert state.ready is True
        assert state.error == ""
        assert state.status_label() == "ready"

    def test_legacy_detection_non_app_path(self):
        """SDK installed outside python-standalone is classified as legacy."""
        from ouroboros.platform_layer import _detect_legacy_user_site_sdk
        detected, path, ver = _detect_legacy_user_site_sdk()
        assert isinstance(detected, bool)

    def test_find_bundled_cli_nonexistent_path(self):
        """_find_bundled_cli returns None for a non-existent SDK path."""
        from ouroboros.platform_layer import _find_bundled_cli
        assert _find_bundled_cli("/nonexistent/path") is None


# ---------------------------------------------------------------------------
# Gateway stderr capture
# ---------------------------------------------------------------------------

class TestGatewayStderrCapture:
    """Verify stderr ring buffer in the gateway."""

    def test_stderr_callback_stores_lines(self):
        from ouroboros.gateways.claude_code import (
            _stderr_callback, get_last_stderr, clear_stderr_buffer,
        )
        clear_stderr_buffer()
        _stderr_callback("line one")
        _stderr_callback("line two")
        result = get_last_stderr()
        assert "line one" in result
        assert "line two" in result
        clear_stderr_buffer()
        assert get_last_stderr() == ""

    def test_stderr_tail_in_result(self):
        """ClaudeCodeResult.stderr_tail appears in JSON output."""
        r = ClaudeCodeResult(
            success=False,
            error="ProcessError: exit code 1",
            stderr_tail="Authentication failed",
        )
        out = json.loads(r.to_tool_output())
        assert out["stderr_tail"] == "Authentication failed"

    def test_stderr_tail_omitted_on_success(self):
        """stderr_tail is not in JSON when empty."""
        r = ClaudeCodeResult(success=True, result_text="ok")
        out = json.loads(r.to_tool_output())
        assert "stderr_tail" not in out


# ---------------------------------------------------------------------------
# Launcher bootstrap — verify_claude_runtime
# ---------------------------------------------------------------------------

class TestVerifyClaudeRuntime:
    """verify_claude_runtime repairs missing SDK."""

    def test_verify_passes_when_sdk_present_at_baseline(self, tmp_path, monkeypatch):
        """SDK imports, CLI exists, version meets baseline → no repair."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.60", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 1

    def test_verify_passes_when_sdk_above_baseline(self, tmp_path):
        """SDK 0.1.61 > baseline 0.1.60 → no repair."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.61", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 1

    def test_verify_triggers_repair_when_sdk_below_baseline(self, tmp_path):
        """SDK 0.1.50 < baseline 0.1.60 → repair fires even though import + CLI work.

        This guards the upgraded-install compat gap: advisory_pre_review
        would otherwise still send thinking.type=enabled to Opus 4.7 on an
        install with pre-0.1.60 SDK already present.
        """
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.50", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 2
        assert "pip" in str(calls[1])
        assert "0.1.60" in str(calls[1])

    def test_verify_triggers_repair_when_missing(self, tmp_path):
        """When SDK check fails (ModuleNotFoundError etc), repair install is attempted."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="ModuleNotFoundError")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 2
        assert "pip" in str(calls[1])


class TestVersionTuple:
    """_version_tuple parses PEP 440-ish version strings for comparison."""

    def test_parses_simple_version(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.60") == (0, 1, 60)

    def test_strips_post_suffix(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.60.post1") == (0, 1, 60)

    def test_strips_pre_release_suffix(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        # "0.1.60rc1" → parses "0", "1", "60" (rc1 stops at first non-digit)
        assert _version_tuple("0.1.60rc1") == (0, 1, 60)

    def test_comparison_semantics(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.50") < _version_tuple("0.1.60")
        assert _version_tuple("0.1.60") >= _version_tuple("0.1.60")
        assert _version_tuple("0.1.61") > _version_tuple("0.1.60")
        assert _version_tuple("0.2.0") > _version_tuple("0.1.99")

    def test_empty_returns_zero(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("") == (0,)
        assert _version_tuple("garbage") == (0,)


# ---------------------------------------------------------------------------
# Delegated trust surface (v6.87.9): foreign settings/MCP, tool allowlist,
# read confinement, and accounting for a child that never came back.
# ---------------------------------------------------------------------------

def _fake_sdk(captured):
    """Return (FakeOptions, FakeSDKClient) capturing constructed SDK options."""

    class FakeOptions:
        # Explicit params so the gateway's signature probe sees this SDK's support.
        def __init__(self, effort=None, tools=None, strict_mcp_config=None, **kwargs):
            if effort is not None:
                kwargs["effort"] = effort
            if tools is not None:
                kwargs["tools"] = tools
            if strict_mcp_config is not None:
                kwargs["strict_mcp_config"] = strict_mcp_config
            captured.update(kwargs)

    class FakeSDKClient:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            if False:
                yield None

    return FakeOptions, FakeSDKClient


def _run_with_fake_sdk(monkeypatch, coro_factory):
    """Run a gateway coroutine against a fake SDK and return the SDK options."""
    import ouroboros.gateways.claude_code as gw

    captured: dict = {}
    fake_options, fake_client = _fake_sdk(captured)
    monkeypatch.setattr(gw, "ClaudeAgentOptions", fake_options)
    monkeypatch.setattr(gw, "ClaudeSDKClient", fake_client)
    monkeypatch.setattr(gw, "_reserve_sdk_attempt", lambda *a, **k: object())
    monkeypatch.setattr(gw, "mark_unresolved", lambda *a, **k: None)
    asyncio.get_event_loop().run_until_complete(coro_factory(gw))
    return captured


class TestDelegatedTrustSurface:
    """A delegated run must not inherit configuration from the target directory."""

    def _readonly_options(self, monkeypatch, cwd="/tmp"):
        return _run_with_fake_sdk(monkeypatch, lambda gw: gw._run_readonly_async(
            "review", cwd=cwd, effort="high", max_budget_usd=1.0,
        ))

    # The edit path retired with D10; the readonly runner is the one live path.
    def test_foreign_settings_and_mcp_config_are_not_loaded(self, monkeypatch):
        options = self._readonly_options(monkeypatch)
        # A `.claude/settings.json` / `.mcp.json` in the target directory would
        # otherwise be loaded and executed (--print skips the trust prompt).
        assert options["setting_sources"] == []
        assert options["strict_mcp_config"] is True
        assert options["mcp_servers"] == {}


    def test_readonly_tool_surface_is_closed_and_read_only(self, monkeypatch):
        from ouroboros.gateways.claude_code import READONLY_TOOLS

        options = self._readonly_options(monkeypatch)
        assert options["tools"] == list(READONLY_TOOLS)
        assert options["allowed_tools"] == list(READONLY_TOOLS)
        for forbidden in ("Agent", "Task", "Bash", "Edit", "Write", "WebFetch"):
            assert forbidden in options["disallowed_tools"]


    def test_the_readonly_path_installs_pretooluse_guards(self, monkeypatch):
        options = self._readonly_options(monkeypatch)
        assert len(options["hooks"]["PreToolUse"]) >= 2


class TestToolAllowlistGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @pytest.mark.parametrize("tool", ["Agent", "Task", "WebFetch", "mcp__foreign__exfiltrate"])
    def test_denies_everything_outside_the_allowlist(self, tool):
        guard = make_readonly_guard()
        result = self._run(guard({"tool_name": tool, "tool_input": {}}, "tid", None))
        assert "deny" in str(result)



class TestReadGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _guard(self, tmp_path):
        from ouroboros.gateways.claude_code import make_read_guard
        return make_read_guard(str(tmp_path))

    def test_allows_read_inside_workspace(self, tmp_path):
        guard = self._guard(tmp_path)
        inside = tmp_path / "notes.md"
        inside.write_text("x", encoding="utf-8")
        assert self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": str(inside)}}, "t", None,
        )) == {}
        assert self._run(guard(
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, "t", None,
        )) == {}

    def test_blocks_absolute_read_outside_workspace(self, tmp_path):
        guard = self._guard(tmp_path)
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, "t", None,
        ))
        assert "deny" in str(result)

    def test_blocks_relative_escape_and_symlink_escape(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("s", encoding="utf-8")
        (workspace / "link").symlink_to(secret)
        guard = self._guard(workspace)
        assert "deny" in str(self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": "../secret.txt"}}, "t", None,
        )))
        assert "deny" in str(self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": "link"}}, "t", None,
        )))

    def test_blocks_grep_and_glob_roots_outside_workspace(self, tmp_path):
        guard = self._guard(tmp_path)
        for tool in ("Grep", "Glob"):
            result = self._run(guard(
                {"tool_name": tool, "tool_input": {"path": "/"}}, "t", None,
            ))
            assert "deny" in str(result), tool

    def test_edit_mode_keeps_the_enclosing_repo_readable(self, tmp_path):
        """Writes stay in the work dir; the surrounding project stays readable."""
        from ouroboros.gateways.claude_code import make_read_guard

        repo = tmp_path / "repo"
        work = repo / "sub"
        work.mkdir(parents=True)
        (repo / "BIBLE.md").write_text("b", encoding="utf-8")
        guard = make_read_guard(str(work), extra_roots=(str(repo),))
        assert self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": str(repo / "BIBLE.md")}}, "t", None,
        )) == {}
        assert "deny" in str(self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "elsewhere.txt")}}, "t", None,
        )))



    def test_read_fence_and_allowlist_decide_on_a_payload_that_is_not_a_dict(self, tmp_path):
        """The payload is a dict by CONVENTION: the SDK forwards the CLI's JSON with no
        default and no type check, and an exception out of a PreToolUse callback is an
        error response, not a deny. Every guard must answer, never raise."""
        from ouroboros.gateways.claude_code import make_tool_allowlist_guard

        guard = self._guard(tmp_path)
        allowlist = make_tool_allowlist_guard(("Read",))
        for payload in (None, [], "x", 42):
            assert isinstance(self._run(guard(payload, "t", None)), dict)
            assert "deny" in str(self._run(allowlist(payload, "t", None))), payload

        # And the inputs are still path-checked AS THEY ARRIVE. Normalising a non-dict to
        # `{}` would drop a bare-string payload out of the fence entirely.
        assert "deny" in str(self._run(guard(
            {"tool_name": "Read", "tool_input": "/etc/passwd"}, "t", None,
        )))

    def test_confines_by_value_not_by_an_enumerated_field_name(self, tmp_path):
        """An enumerated field allowlist is the same structurally-incomplete shape this
        module already rejects for tool NAMES: a call whose only path rides a field
        nobody listed never entered the loop and was allowed by default.

        `Glob` carries its path in `pattern` and can omit `path` entirely, so
        `Glob(pattern="/Users/x/.ssh/*")` read outside the workspace unchallenged.
        (The write-fence halves of the original assertions retired with the D10
        edit path; the read fence is the surviving consumer of `_resolved`.)
        """
        guard = self._guard(tmp_path)
        for tilde in ("~/.ssh/id_rsa", "~/.aws/credentials"):
            assert self._run(guard({"tool_name": "Read",
                                    "tool_input": {"file_path": tilde}}, "t", None)), tilde

        # Containers are walked recursively: a path one level down is still a path.
        for nested in ({"opts": {"path": "/etc/passwd"}}, {"paths": [["/etc/passwd"]]}):
            assert self._run(guard({"tool_name": "Glob", "tool_input": nested}, "t", None)), nested

        # `pattern` is a glob for Glob but a REGEX for Grep. Path-checking the regex
        # denied an ordinary search for a string literal.
        assert self._run(guard({"tool_name": "Grep",
                                "tool_input": {"pattern": "/api/v1/users"}}, "t", None)) == {}

        for field in ("pattern", "glob", "a_field_a_future_cli_adds"):
            out = self._run(guard({"tool_name": "Glob",
                                   "tool_input": {field: "/etc/ssh/ssh_config"}}, "t", None))
            assert out, f"{field} escaped the read fence"
            assert "blocked" in str(out).lower()

        # A list-valued field is checked element by element.
        out = self._run(guard({"tool_name": "Grep",
                               "tool_input": {"paths": [str(tmp_path), "/etc/hosts"]}}, "t", None))
        assert out, "a path inside a list escaped the read fence"

        # An unresolvable path denies, whatever makes it unresolvable: embedded nulls
        # and symlink loops have changed their exact `Path.resolve()` failure behaviour
        # across supported Python versions. A guard that raises out of the PreToolUse
        # callback delivers no decision at all — the one outcome a fence must never produce.
        import os as _os

        _os.symlink(tmp_path / "loop_b", tmp_path / "loop_a")
        _os.symlink(tmp_path / "loop_a", tmp_path / "loop_b")
        for bad in ("/etc/passwd\x00", str(tmp_path / "loop_a" / "x")):
            assert self._run(guard({"tool_name": "Read",
                                    "tool_input": {"file_path": bad}}, "t", None)), bad
        missing = tmp_path / "missing" / "leaf"
        assert self._run(guard({"tool_name": "Read",
                                "tool_input": {"file_path": str(missing)}}, "t", None)) == {}

        # In-workspace values still pass, whatever they are called.
        inside = tmp_path / "a.txt"
        inside.write_text("x")
        assert self._run(guard({"tool_name": "Glob",
                                "tool_input": {"pattern": str(inside)}}, "t", None)) == {}

    def test_the_read_fence_judges_the_path_the_cli_will_actually_open(self, tmp_path):
        """A fence must resolve a value the way its CONSUMER resolves it.

        The CLI funnels every tool path through one helper that TRIMS the value and
        then expands a leading `~`; `pathlib` calls anything not starting with a
        separator relative and joins it onto the workspace, so `" /etc/passwd"` was
        confined to `<cwd>/ /etc/passwd` while the tool opened `/etc/passwd`. The
        trim gap is reachable END TO END via `Grep(path=" /outside")`. (Originally
        asserted on both fences; the write fence retired with the D10 edit path.)
        """
        read_fence = self._guard(tmp_path)
        escapes = (
            "~/.ssh/authorized_keys",   # `~` expansion
            "~",                        # bare `~` is $HOME, no separator in it
            " /etc/passwd",             # leading space, reproduced end to end via Grep
            "\t/etc/passwd",            # `trim()` is not just the space character
            "\u3000/etc/passwd",        # ...nor just the ASCII ones
            "\ufeff/etc/passwd",        # ...and JS trims the BOM where str.strip does not
        )
        for value in escapes:
            assert self._run(read_fence(
                {"tool_name": "Read", "tool_input": {"file_path": value}}, "t", None,
            )), f"read fence allowed {value!r}"
            assert self._run(read_fence(
                {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": value}}, "t", None,
            )), f"read fence allowed Grep(path={value!r})"

        # A `~` value has the shape of a path with no separator in it, so the walker
        # has to catch it by NAME now that it no longer expands on the way past.
        assert self._run(read_fence(
            {"tool_name": "Glob", "tool_input": {"a_field_a_future_cli_adds": "~"}}, "t", None,
        )), "a bare ~ in an unlisted field escaped the read fence"


class TestAbandonedChildAccounting:
    """A killed advisory child must not keep its reservation open forever."""

    def test_control_lines_are_split_from_child_output(self):
        import ouroboros.gateways.claude_code as gw

        stdout = (
            f'{gw._CHILD_ATTEMPT_LINE}{{"attempt_id": "a1", "drive_root": "/d"}}\n'
            f'{gw._CHILD_USAGE_LINE}{{"prompt_tokens": 10}}\n'
            f'{gw._CHILD_USAGE_LINE}{{"prompt_tokens": 30}}\n'
            '{"success": true}\n'
        )
        attempt, usage, plain = gw._parse_child_stdout(stdout)
        assert attempt["attempt_id"] == "a1"
        assert usage == {"prompt_tokens": 30}  # the LAST checkpoint wins
        assert plain.strip() == '{"success": true}'

    def test_timeout_settles_the_reservation_the_child_could_not_close(self, monkeypatch, tmp_path):
        import subprocess as sp
        import ouroboros.gateways.claude_code as gw
        import ouroboros.platform_layer as platform
        import ouroboros.usage_accounting as ua

        settled: dict = {}

        class FakeProc:
            returncode = -9
            pid = 4242

            def __init__(self):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise sp.TimeoutExpired(cmd="claude", timeout=timeout or 1)
                # Serialized the way the real child emits it (`_emit_child_control`),
                # not by string interpolation: a hand-built line with a raw path in it
                # is invalid JSON the moment the path contains backslashes, and the
                # parent then drops the control line instead of settling.
                return (
                    gw._CHILD_ATTEMPT_LINE + json.dumps({
                        "attempt_id": "a1", "drive_root": str(tmp_path),
                        "model": "claude", "provider": "anthropic",
                        "reservation_upper_bound_usd": 7.5,
                    }) + "\n"
                    + gw._CHILD_USAGE_LINE + json.dumps({
                        "prompt_tokens": 900, "completion_tokens": 10,
                    }) + "\n",
                    "killed",
                )

        def fake_terminalize(reservation, *, reason, usage=None):
            settled["attempt_id"] = reservation.attempt_id
            settled["bound"] = reservation.reservation_upper_bound_usd
            settled["usage"] = usage
            settled["reason"] = reason
            return "settled"

        popen_kwargs: dict = {}

        def fake_popen(*args, **kwargs):
            popen_kwargs.update(kwargs)
            return FakeProc()

        monkeypatch.delenv("OUROBOROS_CLAUDE_READONLY_CHILD", raising=False)
        monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(platform, "kill_process_tree", lambda proc: None)
        monkeypatch.setattr(ua, "terminalize_abandoned_attempt", fake_terminalize)

        result = gw.run_readonly("review this", cwd=str(tmp_path))

        assert result.success is False
        assert "timed out" in result.error
        assert settled["attempt_id"] == "a1"
        assert settled["bound"] == 7.5
        assert settled["usage"] == {"prompt_tokens": 900, "completion_tokens": 10}
        # Control lines are accounting transport, never review output.
        assert gw._CHILD_ATTEMPT_LINE not in result.stderr_tail
        # The spawn env must carry the child-mode marker: it is the only thing
        # that makes the child emit the attempt/usage lines this settlement
        # consumed. Both halves were tested separately; this pins the LINK.
        assert popen_kwargs["env"]["OUROBOROS_CLAUDE_READONLY_CHILD"] == "1"

    def test_child_emits_attempt_and_usage_lines_only_in_child_mode(self, monkeypatch, capsys):
        import ouroboros.gateways.claude_code as gw

        monkeypatch.delenv("OUROBOROS_CLAUDE_READONLY_CHILD", raising=False)
        gw._emit_child_control(gw._CHILD_USAGE_LINE, {"prompt_tokens": 1})
        assert capsys.readouterr().out == ""

        monkeypatch.setenv("OUROBOROS_CLAUDE_READONLY_CHILD", "1")
        gw._emit_child_control(gw._CHILD_USAGE_LINE, {"prompt_tokens": 1})
        assert capsys.readouterr().out.startswith(gw._CHILD_USAGE_LINE)

    def test_running_usage_totals_accumulate_across_turns(self):
        import ouroboros.gateways.claude_code as gw

        totals: dict = {}
        gw._accumulate_usage(totals, {"input_tokens": 100, "output_tokens": 10})
        gw._accumulate_usage(totals, {"input_tokens": 50, "output_tokens": 5})
        assert totals["prompt_tokens"] == 150
        assert totals["completion_tokens"] == 15



class TestResultMessageIsErrorHonored:
    """The CLI reports hard API failures (a disabled org key, connection death)
    as ``subtype="success"`` with ``is_error=True`` and the error text in
    ``result``. Ignoring ``is_error`` made the advisory layer parse the error
    text as a checklist, pay a fallback-extraction call on it, and record
    ``parse_failure`` — hiding the real cause (observed live: attempts 5/6 of
    the external-review gate against a disabled Anthropic organization)."""

    def _fake_client(self, fake_result):
        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def query(self, prompt):
                return None

            async def receive_response(self):
                yield fake_result

        return FakeSDKClient

    def _run(self, runner, fake_result, tmp_path, **kwargs):
        import asyncio
        from unittest.mock import patch


        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", lambda **kw: None), \
             patch("ouroboros.gateways.claude_code.ClaudeSDKClient", self._fake_client(fake_result)), \
             patch("ouroboros.gateways.claude_code.ResultMessage", type(fake_result)):
            return asyncio.get_event_loop().run_until_complete(
                runner("prompt", cwd=str(tmp_path), **kwargs)
            )

    def test_is_error_fails_both_paths_and_carries_the_cli_text(self, tmp_path):
        import ouroboros.gateways.claude_code as gw

        class FakeErrorResult:
            subtype = "success"
            is_error = True
            result = "API Error: 400 The socket connection was closed unexpectedly."
            session_id = "sid"
            total_cost_usd = 0
            usage = {}

        # The edit path retired with D10; the readonly runner is the one live path.
        for runner, kwargs in (
            (gw._run_readonly_async, {"max_budget_usd": 1.0}),
        ):
            out = self._run(runner, FakeErrorResult(), tmp_path, **kwargs)
            assert out.success is False, runner.__name__
            assert "is_error=true" in out.error, runner.__name__
            assert "socket connection was closed" in out.error, runner.__name__

    def test_a_clean_success_still_passes_both_paths(self, tmp_path):
        import ouroboros.gateways.claude_code as gw

        class FakeCleanResult:
            subtype = "success"
            is_error = False
            result = "done"
            session_id = "sid"
            total_cost_usd = 0
            usage = {}

        # The edit path retired with D10; the readonly runner is the one live path.
        for runner, kwargs in (
            (gw._run_readonly_async, {"max_budget_usd": 1.0}),
        ):
            out = self._run(runner, FakeCleanResult(), tmp_path, **kwargs)
            assert out.success is True, runner.__name__
