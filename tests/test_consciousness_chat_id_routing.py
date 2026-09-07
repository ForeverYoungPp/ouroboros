"""chat_id routing regression: injects carry chat_id into rows and the cache,
and _execute_tool prefers the cached value over the owner fallback."""
from __future__ import annotations

import json
import queue
from unittest.mock import MagicMock, patch

from ouroboros.consciousness import BackgroundConsciousness


def _make(tmp_path):
    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    repo.mkdir(exist_ok=True)
    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        return BackgroundConsciousness(
            drive_root=drive,
            repo_dir=repo,
            event_queue=queue.Queue(),
            owner_chat_id_fn=lambda: None,
        ), drive


def test_inject_observation_carries_chat_id_into_row_and_cache(tmp_path):
    bc, drive = _make(tmp_path)
    assert bc.inject_observation("hello", chat_id=42, observation_id="c1", source="chat")
    assert bc._last_observation_chat_id == 42
    rows = [json.loads(line) for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()]
    assert rows[-1]["chat_id"] == 42


def test_inject_observation_without_chat_id_does_not_clobber_cache(tmp_path):
    bc, drive = _make(tmp_path)
    assert bc.inject_observation("with-chat", chat_id=7, observation_id="c1", source="chat")
    assert bc.inject_observation("digest-no-chat", observation_id="c2", source="digest")
    assert bc._last_observation_chat_id == 7
    rows = [json.loads(line) for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()]
    assert rows[-1]["chat_id"] is None


def test_execute_tool_prefers_cached_chat_id_over_owner_fallback(tmp_path):
    bc, _ = _make(tmp_path)
    bc.inject_observation("warm", chat_id=99, observation_id="c1", source="chat")
    tc = {"function": {"name": "update_scratchpad", "arguments": "{\"scratchpad\": \"x\"}"}}
    bc._execute_tool(tc, [])
    assert bc._registry._ctx.current_chat_id == 99


def test_execute_tool_falls_back_to_owner_when_cache_cold(tmp_path):
    bc, _ = _make(tmp_path)
    tc = {"function": {"name": "update_scratchpad", "arguments": "{\"scratchpad\": \"x\"}"}}
    bc._execute_tool(tc, [])
    assert bc._registry._ctx.current_chat_id is None  # owner_chat_id_fn() returns None
