"""C-scheme sender identity (v6.114.3): main chat distinguishes agent / BG /
other identities end to end.

Data layer: log_chat persists ``sender_identity`` on the durable row.
Projection layer: the history endpoint passes ``sender_identity`` through to
the SPA projection.
UI layer: web/tests/sender_identity.test.js covers the senderLabel branch.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Data layer: log_chat persists the field; absent stays absent (backward compat)
# ---------------------------------------------------------------------------


def test_log_chat_persists_sender_identity(tmp_path, monkeypatch):
    import supervisor.message_bus as mb

    monkeypatch.setattr(mb, "DATA_DIR", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    mb.log_chat("out", 1, 1, "bg reply", sender_identity="background")
    mb.log_chat("out", 1, 1, "agent reply", sender_identity="agent")
    mb.log_chat("out", 1, 1, "legacy reply")
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines()]
    assert rows[0]["sender_identity"] == "background"
    assert rows[1]["sender_identity"] == "agent"
    assert "sender_identity" not in rows[2]


def test_send_with_budget_persists_sender_identity(tmp_path, monkeypatch):
    """The producer-facing send seam forwards the identity to the durable row."""
    import supervisor.message_bus as mb

    monkeypatch.setattr(mb, "DATA_DIR", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    # make_chat_history_endpoint also needs a progress file when read back.
    (tmp_path / "logs" / "progress.jsonl").write_text("", encoding="utf-8")

    class _FakeBridge:
        def __init__(self):
            self.sent = []

        def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            return True, "ok"

    fake = _FakeBridge()
    monkeypatch.setattr(mb, "_BRIDGE", fake)
    mb.send_with_budget(1, "bg reply", sender_identity="background")
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines()]
    assert rows[0]["sender_identity"] == "background"


def test_send_with_budget_live_frame_carries_sender_identity(tmp_path, monkeypatch):
    """BOTH send branches hand the identity to the live frame, not just the row.

    Regression (v6.114.19): ``_send_markdown`` declared ``sender_identity`` from
    v6.114.3 on but never forwarded it, so every markdown frame — which is what
    ``send_user_message`` sends, for the BG lane and the foreground alike —
    reached the SPA without the field while its durable row kept it. The SPA
    keys a row on its identity, so the live bubble ("Ouroboros") never deduped
    against the same durable row replayed from /api/chat/history
    ("🧠 Background"): one message, two bubbles, until a refresh rebuilt the
    list from history alone. The durable-row assertion above cannot see this —
    only the frame kwargs can.
    """
    import supervisor.message_bus as mb

    monkeypatch.setattr(mb, "DATA_DIR", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    class _FakeBridge:
        def __init__(self):
            self.sent = []

        def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            return True, "ok"

    fake = _FakeBridge()
    monkeypatch.setattr(mb, "_BRIDGE", fake)
    # Markdown branch (_send_markdown) and the plain branch must agree.
    mb.send_with_budget(1, "bg reply", fmt="markdown", sender_identity="background")
    mb.send_with_budget(1, "agent reply", sender_identity="agent")
    assert [frame[2].get("sender_identity") for frame in fake.sent] == ["background", "agent"]


# ---------------------------------------------------------------------------
# Projection layer: history passes the field through (never infers)
# ---------------------------------------------------------------------------


def test_history_endpoint_passes_sender_identity_through(tmp_path):
    from ouroboros.gateway.history import make_chat_history_endpoint

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    (logs / "chat.jsonl").write_text(
        json.dumps({
            "ts": "2026-09-07T19:00:00Z",
            "direction": "out",
            "chat_id": 1,
            "user_id": 1,
            "text": "BG 收到 ✅",
            "type": "proactive_message",
            "sender_identity": "background",
        }) + "\n",
        encoding="utf-8",
    )
    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]
    rec = next(item for item in payload if item.get("text") == "BG 收到 ✅")
    assert rec["role"] == "assistant"  # projection keeps out -> assistant
    assert rec["sender_identity"] == "background"
