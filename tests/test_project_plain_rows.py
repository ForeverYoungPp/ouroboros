"""Plain-text contract for system Project lifecycle rows (owner bug report 2026-08-31).

A Project completion card in Main rendered as a huge H1 with a literal
``## Короткий вывод`` glued mid-line: the excerpt producer flattened newlines
while KEEPING markdown markers, the durable ``chat.jsonl`` row stored them raw
(only the live send stripped), history replayed raw text, and the client
rendered every system row as markdown. These tests pin the server half of the
contract: one shared stripper (``ouroboros.utils.strip_markdown``), strip
BEFORE flatten in the producer, no inherited markdown format on host-salvage
``terminal_incident`` rows, read-side normalization of old persisted rows
without rewriting the log, and VERBATIM live delivery (the bridge no longer
strips — live frame == durable row == history replay, owner D14). The client
render arm is pinned in ``web/tests/chat_plain_system_rows.test.js``.
"""

from __future__ import annotations

import asyncio
import json
import types
from types import SimpleNamespace

MARKDOWN_RESULT = (
    "# Report title\n\n## Short conclusion\n\nBody with `inline code` and **bold**."
)
PLAIN_EXCERPT = "Report title Short conclusion Body with inline code and bold."


def test_completion_excerpt_strips_markers_before_flattening_newlines():
    """RO6: flatten-first would glue ``##`` mid-line where the line-anchored
    heading pattern (and every renderer) can no longer treat it as markup —
    exactly the owner's screenshot symptom."""
    from ouroboros.project_dialogue import _completion_excerpt

    excerpt = _completion_excerpt({"summary": MARKDOWN_RESULT})
    assert excerpt == PLAIN_EXCERPT
    for marker in ("#", "**", "`"):
        assert marker not in excerpt
    # The naive flatten-then-strip order would have produced this glued form.
    assert "## Short conclusion" not in excerpt


def test_completion_excerpt_leaves_plain_text_untouched():
    from ouroboros.project_dialogue import _completion_excerpt

    assert _completion_excerpt({"result": "Release shipped."}) == "Release shipped."


def test_completion_excerpt_strip_applies_before_length_cap():
    from ouroboros.project_dialogue import _completion_excerpt

    long_plain = "word " * 100  # 500 chars once flattened
    excerpt = _completion_excerpt({"summary": "## Heading\n" + long_plain})
    assert excerpt.startswith("Heading word")
    assert len(excerpt) <= 240
    assert excerpt.endswith("…")


def test_completion_summary_event_text_is_plain_and_fully_normalized(
    tmp_path, monkeypatch,
):
    from ouroboros.project_dialogue import enqueue_project_completion_summary
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.utils import strip_markdown

    project = create_project(tmp_path, "launch", name="Launch 🚀")
    bind_task_to_project(
        tmp_path, "root-project", project["id"], project["chat_id"],
        origin={"absent": "system"},
    )
    queued = []

    def _enqueue(_root, event, **_kwargs):
        queued.append(dict(event))
        return True

    monkeypatch.setattr(
        "supervisor.terminal_delivery.enqueue_terminal_delivery", _enqueue,
    )
    ctx = types.SimpleNamespace(DRIVE_ROOT=tmp_path)
    root = {
        "id": "root-project", "project_id": "launch",
        "title": "Ship release", "chat_id": project["chat_id"],
    }
    result = {
        "task_id": "root-project", "status": "completed",
        "project_id": "launch", "title": "Ship release",
        "result": MARKDOWN_RESULT,
    }
    done = {"status": "completed", "outcome_axes": {"execution": {"status": "ok"}}}

    assert enqueue_project_completion_summary(
        ctx.DRIVE_ROOT, {"status": "completed"}, "root-project", root, result, done,
    ) is True
    assert queued[0]["text"] == (
        f"Launch 🚀 › Ship release · Completed\n{PLAIN_EXCERPT}"
    )
    for marker in ("#", "**", "`"):
        assert marker not in queued[0]["text"]
    # RO4 convergence: the producer already normalized, so the verbatim live
    # delivery and the durable row carry the same fully-plain text (stripping
    # the producer's output again is an identity).
    assert strip_markdown(queued[0]["text"]) == queued[0]["text"]
    # Structural fields stay intact next to the plain text.
    assert queued[0]["progress_meta"]["target_label"] == "Launch 🚀 › Ship release"
    assert queued[0]["system_type"] == "project_completion_summary"


def test_host_salvage_terminal_incident_drops_inherited_markdown_format(tmp_path):
    """RO9: the fixed host-salvage receipt must not inherit ``format:
    "markdown"`` from the completed-answer base event; the completed paths
    keep it (bug report #7: markdown system rows still render rich)."""
    from supervisor.terminal_delivery import project_terminal_result_event

    raw = "## Salvage heading\nRAW PATCH " * 20
    base = {
        "type": "send_message", "chat_id": 7, "task_id": "terminal-a",
        "text": raw, "format": "markdown",
    }
    host = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin="host_salvage", base_event=dict(base),
    )
    assert host["system_type"] == "terminal_incident"
    assert "format" not in host

    model = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin="model_final", base_event=dict(base),
    )
    assert model["format"] == "markdown"

    legacy = project_terminal_result_event(
        tmp_path, {"chat_id": 7}, "terminal-a",
        result_text=raw, terminal_origin=None, base_event=dict(base),
    )
    assert legacy["format"] == "markdown"


def test_history_normalizes_old_project_rows_on_read_without_rewriting_log(tmp_path):
    """Bug report #10: rows persisted BEFORE the producer stripped markdown are
    normalized on read; ``chat.jsonl`` stays byte-identical; only the two
    Project lifecycle types are touched (assistant markdown and other system
    types replay verbatim)."""
    from ouroboros.gateway.history import make_chat_history_endpoint

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    old_project_text = "Launch › Ship · Completed\n## Short conclusion\n**bold** `code`"
    rows = (
        {
            "ts": "2026-08-21T00:00:01Z", "direction": "system", "chat_id": 1,
            "type": "project_started", "task_id": "root-project",
            "project_id": "launch", "project_name": "Launch",
            "target_label": "Launch › Ship",
            "text": "# Launch › Ship · Started\nWork is running in this Project.",
        },
        {
            "ts": "2026-08-21T00:00:02Z", "direction": "system", "chat_id": 1,
            "type": "project_completion_summary", "task_id": "root-project",
            "project_id": "launch", "project_name": "Launch",
            "target_label": "Launch › Ship", "status": "completed",
            "text": old_project_text,
        },
        {
            "ts": "2026-08-21T00:00:03Z", "direction": "out", "chat_id": 1,
            "format": "markdown", "text": "## Assistant heading stays markdown",
        },
        {
            "ts": "2026-08-21T00:00:04Z", "direction": "system", "chat_id": 1,
            "type": "cancel_receipt", "task_id": "root-project",
            "text": "⚠️ Task cancelled. Below is the preserved text.\n\n## Verbatim heading",
        },
    )
    chat_path = logs / "chat.jsonl"
    chat_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    before = chat_path.read_bytes()

    endpoint = make_chat_history_endpoint(tmp_path)
    payload = json.loads(
        asyncio.run(endpoint(SimpleNamespace(query_params={"chat_id": "1"}))).body
    )
    by_type = {row.get("system_type"): row for row in payload["messages"] if row.get("role") == "system"}

    started = by_type["project_started"]
    assert started["text"] == "Launch › Ship · Started\nWork is running in this Project."
    assert started["markdown"] is False

    completion = by_type["project_completion_summary"]
    assert completion["text"] == "Launch › Ship · Completed\nShort conclusion\nbold code"
    assert completion["markdown"] is False
    for marker in ("#", "**", "`"):
        assert marker not in completion["text"]
    # Structural fields survive normalization untouched.
    assert completion["project_id"] == "launch"
    assert completion["project_name"] == "Launch"
    assert completion["target_label"] == "Launch › Ship"
    assert completion["status"] == "completed"

    # Only the two lifecycle types are normalized: the cancel_receipt salvage
    # stays verbatim (owner D14) and the assistant markdown row is untouched.
    receipt = by_type["cancel_receipt"]
    assert "## Verbatim heading" in receipt["text"]
    assistant = next(row for row in payload["messages"] if row.get("role") == "assistant")
    assert assistant["text"] == "## Assistant heading stays markdown"
    assert assistant["markdown"] is True

    # The durable log was read, never rewritten.
    assert chat_path.read_bytes() == before


def test_strip_markdown_is_the_single_shared_stripper():
    """RO8: the excerpt producer and history's read-side normalization share
    ONE stripper; the live bridge no longer strips AT ALL (verbatim delivery,
    owner D14) so a divergent second copy cannot reappear there."""
    import inspect

    from ouroboros.utils import strip_markdown
    from supervisor import message_bus

    assert "strip_markdown" not in inspect.getsource(message_bus)
    # Idempotent on its own output for the bug-report fixture: normalizing an
    # already-normalized producer row cannot change it.
    once = strip_markdown(MARKDOWN_RESULT)
    assert strip_markdown(once) == once


def test_cancel_receipt_rides_verbatim_live_durable_and_on_replay(
    monkeypatch, tmp_path,
):
    """Owner D14 + sol review: a cancel_receipt with raw markdown markers is
    delivered VERBATIM through the real ``send_with_budget`` — the live WS
    frame, the durable ``chat.jsonl`` row, and the history replay carry
    byte-equal text (the client renders all three as escaped plain text)."""
    import supervisor.message_bus as message_bus
    from ouroboros.gateway.history import make_chat_history_endpoint

    bridge = message_bus.LocalChatBridge({})
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    monkeypatch.setattr(
        message_bus, "load_state", lambda: {"session_id": "s-1", "owner_id": 7},
    )
    monkeypatch.setattr(
        message_bus, "_advance_project_visible_revision", lambda _chat_id: None,
    )
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_k: None)

    verbatim = (
        "⚠️ Task t-1 was cancelled. Below is the last persisted intermediate "
        "model message, preserved WITHOUT review.\n\n"
        "## Heading\n**bold** and `code` and an unclosed `backtick"
    )
    message_bus.send_with_budget(
        1, verbatim, task_id="t-1", role="system", system_type="cancel_receipt",
    )

    live = next(frame for frame in frames if frame.get("type") == "chat")
    assert live["content"] == verbatim
    assert live["markdown"] is False
    assert live["role"] == "system"
    assert live["system_type"] == "cancel_receipt"

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    durable = next(row for row in rows if row.get("type") == "cancel_receipt")
    assert durable["text"] == verbatim

    endpoint = make_chat_history_endpoint(tmp_path)
    payload = json.loads(
        asyncio.run(endpoint(SimpleNamespace(query_params={"chat_id": "1"}))).body
    )
    replayed = next(
        row for row in payload["messages"]
        if row.get("system_type") == "cancel_receipt"
    )
    assert replayed["text"] == verbatim
    assert replayed["markdown"] is False
