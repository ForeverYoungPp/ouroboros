"""
Tests for BackgroundConsciousness helpers.

Verifies progress events have the correct shape, reach the queue,
and respect pause / chat_id=None semantics. Also covers backlog digest
inclusion in background context.

Run: pytest tests/test_consciousness.py -v
"""

import json
import os
import pathlib
import queue
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestEmitProgress(unittest.TestCase):
    """Tests for BackgroundConsciousness._emit_progress."""

    def _make_consciousness(self, chat_id=42, event_queue=None):
        """Create a BackgroundConsciousness with mocked dependencies."""
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = tempfile.mkdtemp()
        drive_root = pathlib.Path(tmpdir)
        (drive_root / "logs").mkdir(parents=True, exist_ok=True)
        repo_dir = pathlib.Path(tmpdir) / "repo"
        repo_dir.mkdir()

        eq = event_queue if event_queue is not None else queue.Queue()

        with patch.object(BackgroundConsciousness, '_build_registry', return_value=MagicMock()):
            bc = BackgroundConsciousness(
                drive_root=drive_root,
                repo_dir=repo_dir,
                event_queue=eq,
                owner_chat_id_fn=lambda: chat_id,
            )
        return bc, eq, drive_root

    def test_event_shape(self):
        """Event has type, chat_id, text, is_progress, ts."""
        bc, eq, _ = self._make_consciousness(chat_id=99)
        bc._emit_progress("thinking about things")
        evt = eq.get_nowait()

        self.assertEqual(evt["type"], "send_message")
        self.assertEqual(evt["chat_id"], 99)
        self.assertEqual(evt["text"], "💬 thinking about things")
        self.assertEqual(evt["format"], "markdown")
        self.assertTrue(evt["is_progress"])
        self.assertIn("ts", evt)

    def test_empty_content_skipped(self):
        """Empty or whitespace-only content produces no event."""
        bc, eq, drive_root = self._make_consciousness()
        progress_path = drive_root / "logs" / "progress.jsonl"

        bc._emit_progress("")
        bc._emit_progress("   ")
        bc._emit_progress(None)

        self.assertTrue(eq.empty())
        # Also should not persist to file
        self.assertFalse(progress_path.exists())

    def test_chat_id_none_skips_queue_but_persists(self):
        """When chat_id is None, event is NOT queued but IS persisted."""
        bc, eq, drive_root = self._make_consciousness(chat_id=None)
        bc._emit_progress("background thought")

        # Queue should be empty
        self.assertTrue(eq.empty())

        # File should have the entry
        progress_path = drive_root / "logs" / "progress.jsonl"
        self.assertTrue(progress_path.exists())
        entry = json.loads(progress_path.read_text().strip())
        self.assertEqual(entry["type"], "send_message")
        self.assertEqual(entry["content"], "background thought")
        self.assertTrue(entry["is_progress"])

    def test_paused_events_go_to_deferred(self):
        """When paused, events go to _deferred_events, not the queue."""
        bc, eq, _ = self._make_consciousness()
        bc._paused = True
        bc._emit_progress("deferred thought")

        self.assertTrue(eq.empty())
        self.assertEqual(len(bc._deferred_events), 1)
        self.assertEqual(bc._deferred_events[0]["type"], "send_message")
        self.assertEqual(bc._deferred_events[0]["text"], "💬 deferred thought")


class TestBackgroundContext(unittest.TestCase):
    def test_build_context_includes_improvement_backlog_digest(self):
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = pathlib.Path(tempfile.mkdtemp())
        drive_root = tmpdir / "drive"
        repo_dir = tmpdir / "repo"
        (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
        (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
        (drive_root / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
        (drive_root / "logs").mkdir(parents=True, exist_ok=True)
        (drive_root / "state").mkdir(parents=True, exist_ok=True)

        (repo_dir / "prompts" / "CONSCIOUSNESS.md").write_text("Consciousness prompt", encoding="utf-8")
        (repo_dir / "BIBLE.md").write_text("Bible", encoding="utf-8")
        (repo_dir / "VERSION").write_text("1.2.3", encoding="utf-8")
        (repo_dir / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
        (repo_dir / "README.md").write_text("README", encoding="utf-8")
        (repo_dir / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
        (repo_dir / "docs" / "DEVELOPMENT.md").write_text('# Dev', encoding="utf-8")
        (drive_root / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
        (drive_root / "memory" / "identity.md").write_text("I am Ouroboros", encoding="utf-8")
        (drive_root / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")
        (drive_root / "memory" / "knowledge" / "improvement-backlog.md").write_text(
            "# Improvement Backlog\n\n### ibl-1\n- status: open\n- created_at: 2026-04-14T09:00:00+00:00\n- source: execution_reflection\n- category: process\n- task_id: task-1\n- requires_plan_review: yes\n- fingerprint: fp-1\n- summary: Reduce recurring task friction around REVIEW_BLOCKED\n",
            encoding="utf-8",
        )
        for name in ("chat.jsonl", "progress.jsonl", "tools.jsonl", "events.jsonl", "supervisor.jsonl", "task_reflections.jsonl"):
            (drive_root / "logs" / name).write_text("", encoding="utf-8")

        with patch.object(BackgroundConsciousness, '_build_registry', return_value=MagicMock()):
            bc = BackgroundConsciousness(
                drive_root=drive_root,
                repo_dir=repo_dir,
                event_queue=None,
                owner_chat_id_fn=lambda: None,
            )
        text = bc._build_context()
        self.assertIn("## Improvement Backlog", text)
        self.assertIn("Reduce recurring task friction around REVIEW_BLOCKED", text)


class TestBackgroundConsciousnessToolScope(unittest.TestCase):
    def test_background_consciousness_cannot_execute_or_delegate(self):
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = pathlib.Path(tempfile.mkdtemp())
        drive_root = tmpdir / "drive"
        repo_dir = tmpdir / "repo"
        (drive_root / "logs").mkdir(parents=True, exist_ok=True)
        repo_dir.mkdir(parents=True, exist_ok=True)
        eq = queue.Queue()

        bc = BackgroundConsciousness(
            drive_root=drive_root,
            repo_dir=repo_dir,
            event_queue=eq,
            owner_chat_id_fn=lambda: 42,
        )

        schema_names = {s.get("function", {}).get("name") for s in bc._tool_schemas()}
        self.assertIn("send_user_message", schema_names)
        self.assertIn("update_identity", schema_names)
        self.assertIn("recent_tasks", schema_names)
        self.assertNotIn("schedule_subagent", schema_names)
        self.assertNotIn("get_task_result", schema_names)
        self.assertNotIn("wait_task", schema_names)
        self.assertNotIn("wait_tasks", schema_names)
        self.assertNotIn("run_command", schema_names)
        self.assertNotIn("commit_reviewed", schema_names)


class TestBackgroundConsciousnessCost(unittest.TestCase):
    def test_unknown_round_cost_stays_nullable_in_durable_thought(self):
        from ouroboros.consciousness import BackgroundConsciousness

        tmpdir = pathlib.Path(tempfile.mkdtemp())
        drive_root = tmpdir / "drive"
        repo_dir = tmpdir / "repo"
        (drive_root / "logs").mkdir(parents=True)
        repo_dir.mkdir()

        with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
            bc = BackgroundConsciousness(
                drive_root=drive_root,
                repo_dir=repo_dir,
                event_queue=None,
                owner_chat_id_fn=lambda: None,
            )

        with (
            patch.object(bc, "_build_context", return_value="context"),
            patch.object(bc, "_tool_schemas", return_value=[]),
            patch.object(bc, "_check_budget", return_value=True),
            patch(
                "ouroboros.llm_observability.chat_observed",
                return_value=({"content": "thought"}, {"cost": None}),
            ),
        ):
            self.assertTrue(bc._think_scoped())

        events = [
            json.loads(line)
            for line in (drive_root / "logs" / "events.jsonl").read_text().splitlines()
        ]
        thought = next(event for event in events if event.get("type") == "consciousness_thought")
        self.assertIsNone(thought["cost_usd"])
        self.assertFalse(thought["cost_final"])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# An over-ceiling prompt must not be a dead end
# --------------------------------------------------------------------------- #


def _bare_consciousness():
    from ouroboros.consciousness import BackgroundConsciousness

    return BackgroundConsciousness.__new__(BackgroundConsciousness)


def test_an_over_ceiling_prompt_retries_on_the_task_local_low(monkeypatch):
    """Grooming happens INSIDE a cycle, so a wake-up that aborts before its first
    round can never shrink the memory that overflowed it. Over the ceiling, the
    cycle rebuilds ONCE on the Low projection instead of stalling forever."""
    bc = _bare_consciousness()
    seen = {}

    def fake_build(observations, context_mode=""):
        seen["mode"] = context_mode
        return "x" * 10 if context_mode == "low" else "y" * 5_000

    monkeypatch.setattr(bc, "_build_cycle_context", fake_build)
    messages = [
        {"role": "system", "content": "y" * 5_000},
        {"role": "user", "content": "Wake up. Think."},
    ]

    assert bc._degrade_context_for_size(
        messages, [], provider="openai", effort="medium", tools=[]
    ) is True
    assert seen["mode"] == "low"
    assert messages[0]["content"] == "x" * 10
    assert messages[1]["content"] == "Wake up. Think."


def test_a_low_projection_that_does_not_shrink_is_declined(monkeypatch):
    """One retry, not a loop: if Low is not smaller, the caller keeps its abort
    path and its overflow disclosure rather than re-rendering forever."""
    bc = _bare_consciousness()
    monkeypatch.setattr(
        bc, "_build_cycle_context", lambda observations, context_mode="": "y" * 5_000
    )
    messages = [{"role": "system", "content": "y" * 5_000}, {"role": "user", "content": "Wake."}]

    assert bc._degrade_context_for_size(
        messages, [], provider="openai", effort="medium", tools=[]
    ) is False
    assert messages[0]["content"] == "y" * 5_000


def test_governance_sections_honour_a_task_local_low(tmp_path):
    """The mechanism the retry relies on: the same env renders the full doc in
    max and the navigation map in low, and the owner's global mode is untouched."""
    from types import SimpleNamespace

    from ouroboros.config import get_context_mode
    from ouroboros.context import build_governance_sections

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "BIBLE.md").write_text("# BIBLE\n\nprinciple", encoding="utf-8")
    body = "\n\n".join(
        f"## Section {i}\n\n" + ("detail line\n" * 200) for i in range(1, 12)
    )
    (repo / "docs" / "ARCHITECTURE.md").write_text(body, encoding="utf-8")
    env = SimpleNamespace(repo_path=lambda rel: repo / rel)

    before = get_context_mode()
    full = "\n\n".join(build_governance_sections(env, context_mode="max"))
    low = "\n\n".join(build_governance_sections(env, context_mode="low"))

    assert len(low) < len(full)
    assert "detail line" in full
    assert get_context_mode() == before  # a task-local Low never leaks into the global


# --------------------------------------------------------------------------- #
# The WIRING: one real cycle, over the ceiling
# --------------------------------------------------------------------------- #
# The helpers above are green either way. Production measured 1,319,625 physical
# bytes against a 1,200,000 ceiling, so every wake-up aborted forever — what was
# broken was `_think_scoped`'s use of them, which nothing pinned. A full LLM round
# is not the point, so the four seams below are patched; everything else (the real
# class, the real context assembly, the real receipts) runs.


def _bg_cycle_env(engram_stub):
    """Repo/drive layout the real `_build_context` needs, on the shared stub env."""
    state, env = engram_stub
    repo, drive = env.repo_dir, env.drive_root
    for path in (repo / "docs", repo / "prompts", drive / "memory", drive / "logs", drive / "state"):
        path.mkdir(parents=True, exist_ok=True)
    # Big enough that the Low projection is genuinely smaller (it replaces this
    # document with a navigation map), which is what the degrade decision rests on.
    (repo / "docs" / "ARCHITECTURE.md").write_text(
        "\n\n".join(f"## Section {i}\n\n" + ("detail line\n" * 120) for i in range(1, 12)),
        encoding="utf-8",
    )
    (repo / "docs" / "DEVELOPMENT.md").write_text("# Dev", encoding="utf-8")
    (repo / "prompts" / "SYSTEM.md").write_text("base prompt", encoding="utf-8")
    (repo / "BIBLE.md").write_text("# BIBLE\n\nprinciple", encoding="utf-8")
    (drive / "memory" / "identity.md").write_text("I am a test identity.", encoding="utf-8")
    (drive / "memory" / "WORLD.md").write_text("host: test", encoding="utf-8")
    (drive / "memory" / "scratchpad.md").write_text("scratchpad body", encoding="utf-8")
    (drive / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    (drive / "state" / "state.json").write_text("{}", encoding="utf-8")
    return state, env


def _wire_cycle(engram_stub, monkeypatch, measure):
    """A real cycle with only the LLM-size/LLM seams replaced.

    ``measure`` gets the physical-size call count. Returns (bc, rebuilds, events,
    thought-calls) where ``rebuilds`` records every context build as
    ``(context_mode, observations, text)`` — including the ones `_think_scoped`
    itself makes, so the mode and the snapshot identity are assertable rather than
    assumed.
    """
    from ouroboros import llm_observability, openai_chat_dispatch
    from ouroboros.consciousness import BackgroundConsciousness
    from ouroboros.engram_sink import reset_sinks

    state, env = _bg_cycle_env(engram_stub)
    reset_sinks()
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")  # the global mode the degrade departs from
    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        bc = BackgroundConsciousness(
            drive_root=env.drive_root,
            repo_dir=env.repo_dir,
            event_queue=None,
            owner_chat_id_fn=lambda: None,
        )
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    monkeypatch.setattr(bc._llm, "_resolve_remote_target", lambda model: {"provider": "openai"})
    monkeypatch.setattr(bc, "_check_budget", lambda: True)

    real_build = bc._build_cycle_context
    rebuilds: list = []

    def recording_build(observations, context_mode=""):
        text = real_build(observations, context_mode=context_mode)
        rebuilds.append((context_mode, observations, text))
        return text

    monkeypatch.setattr(bc, "_build_cycle_context", recording_build)

    round_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}
    monkeypatch.setattr(
        llm_observability,
        "chat_observed",
        lambda *args, **kwargs: ({"role": "assistant", "content": "one thought", "tool_calls": []}, dict(round_usage)),
    )
    monkeypatch.setattr(openai_chat_dispatch, "projected_context_size_bytes", measure)

    events_path = env.drive_root / "logs" / "events.jsonl"
    def _events():
        if not events_path.exists():
            return []
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    return bc, rebuilds, _events


def test_an_over_ceiling_cycle_rebuilds_on_the_low_projection_and_records_it(engram_stub, monkeypatch):
    """One real cycle, over the ceiling on its first measurement: it must rebuild
    the system prompt with ``context_mode="low"`` for the SAME observation
    snapshot, retry on exactly that compact text (not merely "some rebuild"), and
    disclose the deviation — otherwise the loop is the one that aborted forever."""
    from ouroboros.consciousness import BG_CONTEXT_MAX_CHARS

    measured: list = []

    def measure(messages, tools=None, provider="", reasoning_effort=None):
        measured.append(str(messages[0]["content"]))
        # Over the ceiling first, under it once the prompt has been rebuilt.
        return BG_CONTEXT_MAX_CHARS + 500 if len(measured) == 1 else 1_000

    bc, rebuilds, events = _wire_cycle(engram_stub, monkeypatch, measure)

    assert bc._think_scoped() is True, "the cycle aborted on the oversized prompt"

    # 1. The rebuild asked for the LOW projection, for the cycle's OWN snapshot.
    cycle_build, degrade_build = rebuilds[0], rebuilds[1]
    assert cycle_build[0] == "", cycle_build[0]
    assert degrade_build[0] == "low", degrade_build[0]
    assert degrade_build[1] is cycle_build[1], "the rebuild used a different snapshot"

    # 2. The retry used exactly the text that build returned.
    assert len(measured) >= 2 and len(measured[1]) < len(measured[0])
    assert measured[1] == degrade_build[2]

    # 3. And the deviation is disclosed under its own reason.
    degraded = [e for e in events() if e.get("type") == "consciousness_context_degraded"]
    assert degraded and degraded[0]["reason"] == "task_local_low"
    assert not [e for e in events() if e.get("type") == "consciousness_context_overflow"]
    assert [e for e in events() if e.get("type") == "consciousness_thought"], "the cycle did not finish"


def test_a_prompt_still_over_the_ceiling_aborts_with_its_receipt(engram_stub, monkeypatch):
    """The disclosure path has to survive the fix: when even the Low projection is
    over the ceiling, the cycle must still abort AND say so durably."""
    from ouroboros.consciousness import BG_CONTEXT_MAX_CHARS

    measured: list = []

    def measure(messages, tools=None, provider="", reasoning_effort=None):
        measured.append(str(messages[0]["content"]))
        return BG_CONTEXT_MAX_CHARS + 500

    bc, rebuilds, events = _wire_cycle(engram_stub, monkeypatch, measure)

    assert bc._think_scoped() is False
    assert rebuilds[1][0] == "low", "the retry did not even ask for the Low projection"

    overflow = [e for e in events() if e.get("type") == "consciousness_context_overflow"]
    assert overflow, "the abort lost its disclosure"
    assert "too large" in overflow[0]["error"]
    assert not [e for e in events() if e.get("type") == "consciousness_thought"]
