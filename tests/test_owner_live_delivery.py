"""Live-first owner delivery: gates, lineage stamping, and final-answer pick."""
import json
import queue
import types

from ouroboros.tools.owner_delivery import deliver_owner_event


class _Queue:
    def __init__(self, full=False):
        self.full = full
        self.items = []

    def put_nowait(self, item):
        if self.full:
            raise queue.Full()
        self.items.append(item)

    put = put_nowait


def _ctx(chat_id=123, *, event_queue=None, meta=None):
    return types.SimpleNamespace(
        current_chat_id=chat_id,
        pending_events=[],
        drive_root=None,
        task_id="t-live",
        task_metadata={"parent_task_id": "t-parent", "root_task_id": "t-root", **(meta or {})},
        event_queue=event_queue,
    )


class TestDeliverOwnerEvent:
    def test_live_path_stamps_lineage_and_skips_buffer(self):
        q = _Queue()
        ctx = _ctx(event_queue=q)
        mode = deliver_owner_event(ctx, {"type": "send_message", "chat_id": 123, "text": "hi"})
        assert mode == "live"
        assert ctx.pending_events == []
        assert len(q.items) == 1
        frame = q.items[0]
        assert frame["task_id"] == "t-live"
        assert frame["parent_task_id"] == "t-parent"
        assert frame["root_task_id"] == "t-root"

    def test_no_queue_defers_with_lineage(self):
        ctx = _ctx(event_queue=None)
        mode = deliver_owner_event(ctx, {"type": "send_photo", "chat_id": 123})
        assert mode == "deferred"
        assert ctx.pending_events[0]["root_task_id"] == "t-root"

    def test_background_consciousness_always_deferred_and_unstamped(self):
        from ouroboros.tool_capabilities import BACKGROUND_DELEGATION_ROLE

        q = _Queue()
        ctx = _ctx(event_queue=q, meta={"delegation_role": BACKGROUND_DELEGATION_ROLE})
        mode = deliver_owner_event(ctx, {"type": "send_message", "chat_id": 1, "text": "x"})
        assert mode == "deferred"
        assert q.items == []
        # BG frames stay exactly as before the seam: buffered, no pseudo-lineage.
        assert "task_id" not in ctx.pending_events[0]

    def test_background_role_overrides_preset_agent_identity(self):
        # v6.114.8: control.py's _send_user_message presets
        # sender_identity="agent"; owner_delivery must FORCE "background" for a
        # genuine BG reply instead of leaving the wrong preset on the frame.
        # Regression for the web renderer showing "Ouroboros" instead of
        # "🧠 Background" on BG proactive replies.
        from ouroboros.tool_capabilities import BACKGROUND_DELEGATION_ROLE

        ctx = _ctx(meta={"delegation_role": BACKGROUND_DELEGATION_ROLE})
        deliver_owner_event(ctx, {
            "type": "send_message",
            "chat_id": 1,
            "text": "bg reply",
            "sender_identity": "agent",
        })
        assert ctx.pending_events[0]["sender_identity"] == "background"

    def test_non_background_preset_identity_passes_through(self):
        # The foreground proactive path (control.py presets "agent") must keep
        # its identity unchanged; only the BG role forces the override.
        q = _Queue()
        ctx = _ctx(event_queue=q)
        deliver_owner_event(ctx, {
            "type": "send_message",
            "chat_id": 1,
            "text": "fg reply",
            "sender_identity": "agent",
        })
        assert q.items[0]["sender_identity"] == "agent"
        assert ctx.pending_events == []

    def test_consciousness_stamps_the_shared_background_role(self):
        # Literal-drift pin: the producer (consciousness) and the gate
        # (owner_delivery) must share ONE constant, not two literals.
        import inspect

        from ouroboros import consciousness

        src = inspect.getsource(consciousness)
        assert "BACKGROUND_DELEGATION_ROLE" in src
        assert '"delegation_role": "background"' not in src

    def test_retry_duplicates_are_accepted_policy(self):
        # A live-delivered frame from attempt 1 is not recalled; a retried
        # task re-narrates with a fresh ctx and delivers again. The seam
        # performs NO cross-attempt dedup — this pins the accepted policy.
        q = _Queue()
        for _attempt in (1, 2):
            ctx = _ctx(event_queue=q)  # fresh per-attempt ctx, sticky reset
            assert deliver_owner_event(
                ctx, {"type": "send_message", "chat_id": 1, "text": "same"}
            ) == "live"
        assert len(q.items) == 2

    def test_a2a_chat_always_deferred(self):
        q = _Queue()
        ctx = _ctx(chat_id=-5, event_queue=q)
        mode = deliver_owner_event(ctx, {"type": "send_message", "chat_id": -5, "text": "x"})
        assert mode == "deferred"
        assert q.items == []

    def test_full_queue_goes_sticky_deferred(self):
        broken = _Queue(full=True)
        ctx = _ctx(event_queue=broken)
        first = deliver_owner_event(ctx, {"type": "send_message", "chat_id": 1, "text": "a"})
        assert first == "deferred"
        # The queue recovers, but ordering wins: the task stays deferred so
        # frame B cannot overtake the already-buffered frame A.
        ctx.event_queue = _Queue()
        second = deliver_owner_event(ctx, {"type": "send_message", "chat_id": 1, "text": "b"})
        assert second == "deferred"
        assert [e["text"] for e in ctx.pending_events] == ["a", "b"]
        assert ctx.event_queue.items == []


class TestSendToolsLiveIntegration:
    def test_send_user_message_live_and_chat_zero(self, tmp_path):
        from ouroboros.tools.control import _send_user_message

        q = _Queue()
        ctx = _ctx(chat_id=0, event_queue=q)
        ctx.drive_logs = lambda: tmp_path
        result = _send_user_message(ctx, "hello owner")
        assert "OK" in result
        assert len(q.items) == 1
        frame = q.items[0]
        assert frame["chat_id"] == 0  # chat 0 is a real hidden session
        assert frame["task_id"] == "t-live"
        assert frame["is_progress"] is False
        assert (tmp_path / "events.jsonl").exists()

    def test_send_user_message_carries_proactive_discriminator(self, tmp_path):
        from ouroboros.tools.control import _send_user_message

        q = _Queue()
        ctx = _ctx(event_queue=q)
        ctx.drive_logs = lambda: tmp_path
        assert "sent to owner chat" in _send_user_message(ctx, "ping")
        frame = q.items[0]
        # Replay safety: an untyped assistant row with a task_id reads as the
        # task's final on history reload; the discriminator (persisted via
        # log_chat record_type) keeps the live card open.
        assert frame["system_type"] == "proactive_message"

    def test_send_photo_accepts_chat_zero(self, tmp_path):
        from ouroboros.tools.core import _send_photo

        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        q = _Queue()
        ctx = _ctx(chat_id=0, event_queue=q)
        result = _send_photo(ctx, file_path=str(img))
        assert "OK" in result
        assert len(q.items) == 1
        assert q.items[0]["chat_id"] == 0

    def test_media_and_links_go_live_with_honest_receipts(self, tmp_path):
        from ouroboros.tools.core import _send_file, _send_links, _send_video

        vid = tmp_path / "c.mp4"
        vid.write_bytes(b"\x00" * 32)
        doc = tmp_path / "r.csv"
        doc.write_text("a,b\n", encoding="utf-8")

        q = _Queue()
        ctx = _ctx(event_queue=q)
        assert "sent to owner chat" in _send_video(ctx, file_path=str(vid))
        assert "sent to owner chat" in _send_file(ctx, file_path=str(doc))
        assert "sent to owner chat" in _send_links(
            ctx, links=[{"label": "Docs", "url": "https://example.com/d"}])
        assert [e["type"] for e in q.items] == ["send_video", "send_document", "send_links"]
        assert ctx.pending_events == []  # live XOR deferred: never both

        deferred_ctx = _ctx(event_queue=None)
        assert "queued for delivery" in _send_video(deferred_ctx, file_path=str(vid))
        assert "queued for delivery" in _send_file(deferred_ctx, file_path=str(doc))
        assert "queued for delivery" in _send_links(
            deferred_ctx, links=[{"label": "Docs", "url": "https://example.com/d"}])
        assert len(deferred_ctx.pending_events) == 3


class TestFinalAnswerSelection:
    def test_final_beats_deferred_proactive_with_same_task_id(self):
        from ouroboros.task_finalization import deliver_final_message_live

        q = _Queue()
        buffer = [
            {"type": "send_message", "task_id": "t1", "chat_id": 1,
             "text": "mid-task proactive", "is_progress": False},
            {"type": "send_message", "task_id": "t1", "chat_id": 1,
             "text": "the final answer", "is_progress": False},
        ]
        assert deliver_final_message_live(q, buffer, "t1") is True
        assert len(q.items) == 1
        assert q.items[0]["text"] == "the final answer"


class TestSupervisorPhotoChatZero:
    def test_handle_send_photo_delivers_to_chat_zero(self):
        from supervisor.chat_delivery_events import _handle_send_photo

        sent = []

        class _Bridge:
            def send_photo(self, chat_id, data, caption="", mime="", task_id=""):
                sent.append(chat_id)
                return True, ""

        ctx = types.SimpleNamespace(bridge=_Bridge(), append_jsonl=lambda *a, **k: None,
                                    DRIVE_ROOT=None)
        import base64 as b64
        evt = {"type": "send_photo", "chat_id": 0, "task_id": "", "parent_task_id": "",
               "root_task_id": "", "image_base64": b64.b64encode(b"x" * 120).decode(),
               "caption": "", "mime": "image/png"}
        _handle_send_photo(evt, ctx)
        assert sent == [0]


# --------------------------------------------------------------------------- #
# Option (a): BG answer arbitration against an already-handled owner message.
# --------------------------------------------------------------------------- #

_BG_CHAT_BASE = "2026-09-11T19:18:26+00:00"


def _bg_ctx(drive_root, *, chat_id=1):
    from ouroboros.tool_capabilities import BACKGROUND_DELEGATION_ROLE

    ctx = _ctx(chat_id=chat_id, meta={"delegation_role": BACKGROUND_DELEGATION_ROLE})
    ctx.drive_root = drive_root
    return ctx


def _iso(seconds_offset: float = 0.0) -> str:
    from datetime import datetime, timedelta

    base = datetime.fromisoformat(_BG_CHAT_BASE)
    return (base + timedelta(seconds=seconds_offset)).isoformat()


def _now_iso(seconds_offset: float = 0.0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_offset)).isoformat()


def _write_chat(drive_root, rows):
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "chat.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _inbox_rows(drive_root):
    path = drive_root / "state" / "consciousness_observations.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestBackgroundAnswerArbitration:
    """A BG reply that would duplicate an answered owner message becomes an
    observation-inbox note instead of a second chat frame."""

    def test_notes_instead_of_duplicating_an_answered_message(self, tmp_path):
        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        text = "bg would-be duplicate"
        mode = deliver_owner_event(
            ctx, {"type": "send_message", "chat_id": 1, "text": text, "ts": _iso(5)}
        )
        assert mode == "noted"
        assert ctx.pending_events == []  # no owner-facing frame
        rows = _inbox_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["op"] == "enqueue"
        assert rows[0]["source"] == "owner_delivery"
        assert rows[0]["kind"] == "note"
        assert rows[0]["payload"] == text
        assert rows[0]["chat_id"] == 1
        assert rows[0]["ref"]["arbitration"] == "foreground_answer_recent"

    def test_sends_normally_when_no_owner_row_is_pending(self, tmp_path):
        # No owner row for THIS chat (the only row belongs to another thread), so
        # nothing is in flight and the BG frame is sent as before.
        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 2, "text": "another thread"},
            {"ts": _iso(1), "direction": "out", "chat_id": 2, "sender_identity": "",
             "text": "answered there"},
        ])
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(
            ctx, {"type": "send_message", "chat_id": 1, "text": "bg answer", "ts": _iso(5)}
        )
        assert mode == "deferred"
        assert ctx.pending_events[0]["sender_identity"] == "background"
        assert _inbox_rows(tmp_path) == []

    def test_a_stale_foreground_answer_does_not_arbitrate(self, tmp_path):
        from ouroboros.tools.owner_delivery import (
            BG_ANSWER_ARBITRATION_WINDOW_SECONDS as window,
        )

        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(ctx, {
            "type": "send_message", "chat_id": 1, "text": "late bg answer",
            "ts": _iso(2 + window + 1),
        })
        assert mode == "deferred"
        assert ctx.pending_events[0]["sender_identity"] == "background"

    def test_an_unanswered_owner_row_arbitrates(self, tmp_path):
        # The observed case, twice over (19:19:11 and 22:32:43): the BG answered
        # while the turn the owner's message had started was still running. The
        # turn is witnessed by the TAIL — the owner row no out row answers — and
        # deliberately NOT by the queue snapshot, which witnessed neither case
        # (that is the live miss this clause was re-sourced for). This test
        # therefore writes no snapshot at all.
        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
        ])
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(
            ctx, {"type": "send_message", "chat_id": 1, "text": "bg answer", "ts": _iso(5)}
        )
        assert mode == "noted"
        assert ctx.pending_events == []
        assert _inbox_rows(tmp_path)[0]["ref"]["arbitration"] == "owner_turn_pending"

    def test_an_unanswered_owner_row_stops_arbitrating_past_the_bound(self, tmp_path):
        # The bound exists because an unanswered row is NOT proof of a live turn:
        # a turn can error, be cancelled, or the tab can close. Without it the
        # clause stays true forever and the lane is silently muted in that chat.
        from ouroboros.tools.owner_delivery import BG_OWNER_TURN_PENDING_MAX_SECONDS as bound

        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
        ])

        young = _bg_ctx(tmp_path)
        assert deliver_owner_event(
            young, {"type": "send_message", "chat_id": 1, "text": "young bg", "ts": _iso(bound - 1)}
        ) == "noted"

        # The SAME unanswered row, past the bound: the normal frame is sent.
        late = _bg_ctx(tmp_path)
        assert deliver_owner_event(
            late, {"type": "send_message", "chat_id": 1, "text": "late bg", "ts": _iso(bound + 1)}
        ) == "deferred"
        assert late.pending_events[0]["sender_identity"] == "background"

    def test_an_answered_row_is_not_pending_however_old(self, tmp_path):
        # The newest-row test decides first: an answered chat is never "pending",
        # whatever the age — so a long-settled conversation keeps its normal BG
        # frame even far beyond the pending bound and the answer window.
        from ouroboros.tools import owner_delivery as arbitration

        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(ctx, {
            "type": "send_message", "chat_id": 1, "text": "bg after it settled",
            "ts": _iso(2 + max(arbitration.BG_ANSWER_ARBITRATION_WINDOW_SECONDS,
                             arbitration.BG_OWNER_TURN_PENDING_MAX_SECONDS) + 60),
        })
        assert mode == "deferred"
        assert _inbox_rows(tmp_path) == []

    def test_a_failed_note_write_never_loses_the_message(self, tmp_path, monkeypatch):
        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        import ouroboros.consciousness as consciousness

        monkeypatch.setattr(consciousness, "append_observation_row", lambda *a, **k: False)
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(
            ctx, {"type": "send_message", "chat_id": 1, "text": "bg answer", "ts": _iso(5)}
        )
        assert mode == "deferred"
        assert ctx.pending_events[0]["text"] == "bg answer"

    def test_non_message_frames_are_not_arbitrated(self, tmp_path):
        # Only the send-message family answers an owner message; a media frame
        # keeps its existing transport decision under the same evidence.
        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        mode = deliver_owner_event(ctx, {"type": "send_photo", "chat_id": 1, "ts": _iso(5)})
        assert mode == "deferred"
        assert len(ctx.pending_events) == 1

    def test_the_note_is_readable_by_the_wake_loop(self, tmp_path):
        # Not just written: the running loop's own reader must index the row as
        # a pending observation (stable-ID index, no gap), or a suppressed frame
        # would be lost to a store the cycle never consults.
        import ouroboros.consciousness as consciousness_module

        _write_chat(tmp_path, [
            {"ts": _iso(0), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _iso(2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        assert deliver_owner_event(
            ctx, {"type": "send_message", "chat_id": 1, "text": "bg note body", "ts": _iso(5)}
        ) == "noted"
        instance = object.__new__(consciousness_module.BackgroundConsciousness)
        instance._drive_root = tmp_path
        state = instance._read_observation_state(force=True)
        assert state.get("gap_reasons") == []
        payloads = [row.get("payload") for row in state["rows"].values()]
        assert "bg note body" in payloads

    def test_the_tool_receipt_reports_the_note_honestly(self, tmp_path):
        from ouroboros.tools.control import _send_user_message

        _write_chat(tmp_path, [
            {"ts": _now_iso(-5), "direction": "in", "chat_id": 1, "text": "owner question"},
            {"ts": _now_iso(-2), "direction": "out", "chat_id": 1,
             "sender_identity": "", "text": "foreground answer"},
        ])
        ctx = _bg_ctx(tmp_path)
        ctx.drive_logs = lambda: tmp_path
        result = _send_user_message(ctx, "bg reply")
        assert "background note" in result
        assert "queued for delivery" not in result
        assert ctx.pending_events == []
