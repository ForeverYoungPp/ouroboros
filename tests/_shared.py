"""Shared helpers for the Ouroboros test suite.

These functions are reused across multiple ``tests/test_*.py`` modules to
avoid duplicated boilerplate (extension-loader cleanup, mock contexts).
They are intentionally plain module-level callables, not fixtures — many
callers need them at module import time.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock


def clean_extension_runtime_state() -> None:
    """Reset every extension_loader namespace to a pristine state.

    Superset of cleanup logic that previously lived (with minor variations)
    in ``test_skill_exec.py``, ``test_extensions_api.py`` and
    ``test_extension_loader.py``. Extra clears are inert when the namespace
    is already empty, so the superset is safe for every caller.
    """
    from ouroboros import extension_loader

    with extension_loader._lock:
        loaded_names = list(extension_loader._extensions.keys())
    for name in loaded_names:
        extension_loader.unload_extension(name)
    with extension_loader._lock:
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._unloading.clear()
        extension_loader._lifecycle_locks.clear()
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()
        extension_loader._settings_sections.clear()
        extension_loader.set_ws_broadcaster(None)


def make_safe_mock_ctx(tmp_path, *, repo_dir=None):
    """Return a MagicMock ToolContext whose drive paths resolve to real dirs.

    Several observability paths append to ``ctx.drive_logs() / "events.jsonl"``.
    A bare MagicMock would stringify into a filename in the repo root.
    """
    ctx = MagicMock()
    ctx.repo_dir = repo_dir if repo_dir is not None else tmp_path
    ctx.drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    ctx.drive_logs.return_value = logs
    ctx.emit_progress_fn = lambda *a, **kw: None
    ctx.task_id = "test-task"
    return ctx


def configure_test_subagent(
    monkeypatch,
    *,
    subagent_id: str = "api-scout",
    kind: str = "api_model",
    target: str = "openai/gpt-5.6-sol",
    profile_id: str = "",
    effort: str = "high",
) -> str:
    """Install one explicit Available-subagent row for scheduling tests."""
    route = {"kind": kind, "target_id": target}
    if kind == "agent_session" and profile_id:
        route["credential_profile_id"] = profile_id
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps({
        "enabled": True,
        "items": [{
            "subagent_id": subagent_id,
            "name": "Test subagent",
            "recommended_use": "Production-shaped test actor.",
            "route": route,
            "effort": effort,
        }],
    }))
    return subagent_id
