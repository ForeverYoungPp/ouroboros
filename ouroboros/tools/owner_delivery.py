"""Live-first delivery of owner-directed chat events from a running task.

One seam for the send family (``send_user_message`` / ``send_photo`` /
``send_video`` / ``send_file`` / ``send_links``): try the live worker event
queue first so the owner sees the frame while the task is still RUNNING, and
fall back to ``ctx.pending_events`` (the end-of-task drain) when live
transport is unavailable. The transport contract mirrors
``_emit_control_event`` (tools/control.py) — live XOR deferred, never both —
with three structural gates:

- **Background consciousness stays deferred.** The consciousness loop wires a
  live queue into the shared ctx before every tool call and aggregates
  ``pending_events`` at cycle end (with pause deferral), so gating on queue
  presence alone would leak frames from uncommitted or paused cycles.
- **A2A chats stay deferred.** A peer's ``wait_for_response`` subscription
  resolves on the FIRST non-progress chat frame, so a live mid-task frame
  would hijack the answer the peer is waiting for.
- **Sticky-deferred after the first live failure.** Once one frame falls back
  to the buffer, later frames must not overtake it: narrative order beats
  freshness, so the task finishes the attempt on the deferred path.

Frames must stay multiprocessing-safe by construction (JSON primitives and
base64 strings only): the production worker queue is manager-backed, so a
poison value raises at the caller and falls back cleanly — but a plain
``multiprocessing.Queue`` transport would serialize in a feeder thread and
lose it AFTER ``put_nowait`` returned, so the by-construction rule is the
contract, not the transport's forgiveness.

Retry semantics are the progress channel's: a live-delivered frame from a
failed attempt is not recalled, and a retried task may narrate again. That is
an accepted property of live delivery, not a defect to dedupe away.

Known, pre-existing exception to the ordering rule: the blocking-post-task
final-answer shortcut (``deliver_final_message_live``) may ship the FINAL
ahead of frames still buffered here — the answer is deliberately never held
hostage to trailing narration. The sticky rule orders the send family among
itself, not against the terminal shortcut.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict

_STICKY_ATTR = "_owner_delivery_sticky_deferred"

# PRODUCT PARAMETER — tuneable. How long after a foreground answer (or during a
# running foreground turn) the BG still treats an owner message as answered.
# The observed double answer was ~6 s apart (BG 19:19:11, foreground 19:19:17);
# 30 s is that gap with slack for a turn's finalization. Raising it suppresses
# more BG replies (fewer duplicates, more risk of muting a genuine one);
# lowering it trades the other way. One number, no other behaviour depends on it.
BG_ANSWER_ARBITRATION_WINDOW_SECONDS = 30

# Bounded scan of the recent chat rows the predicate inspects. The newest owner
# row is always far inside this window (the reader is tail-bounded anyway).
_ARBITRATION_SCAN_ROWS = 200


def _as_epoch(value: Any) -> float:
    """Epoch seconds for a chat-row or frame timestamp; 0.0 when unusable."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _row_chat_id(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("chat_id"))
    except (TypeError, ValueError):
        return 0


def _recent_chat_rows(drive_root: Any, limit: int = _ARBITRATION_SCAN_ROWS) -> list:
    """The newest chat rows, read straight from the live generation's tail.

    Deliberately WITHOUT a ``Memory`` handle: ``Memory`` wants a repository root,
    and this seam holds only a drive root. Handing it the drive would resolve the
    project from the drive's own directory name — F7, "one system, two projects",
    which stays invisible on a drive that happens to carry the
    ``.engram/config.json`` pin and appears on one that does not. Nothing in this
    predicate needs project resolution, so it asks the file directly and cannot
    get a scope wrong by construction.

    The newest rows are always in the live generation (rotation moves the OLD
    file into archive/), so a bounded tail is the whole window; a rotation that
    lands inside the window simply yields no owner row, and the caller then
    declines to arbitrate (fail open — the frame is sent as before).
    """
    path = pathlib.Path(drive_root) / "logs" / "chat.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _foreground_turn_running(drive_root: Any, chat_id: int) -> bool:
    """Whether the persisted RUNNING set holds a foreground task for this chat.

    Read from the supervisor's own durable snapshot
    (`state/queue_snapshot.json`, `running[].task.chat_id`) rather than the live
    registry: the BG's ctx is a plain ToolContext with no supervisor handle, so
    the snapshot IS the cross-process seam. A stale entry can therefore mute a
    BG frame (the text survives as an inbox note); an unreadable snapshot reads
    as "not running" (fail open), so a broken read can only cost a duplicate.
    """
    try:
        snapshot = json.loads(
            (pathlib.Path(drive_root) / "state" / "queue_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return False
    for row in snapshot.get("running") or []:
        if not isinstance(row, dict):
            continue
        task = row.get("task") if isinstance(row.get("task"), dict) else {}
        if _row_chat_id(task) == chat_id:
            return True
    return False


def _foreground_answer_recent(ctx: Any, evt: Dict[str, Any]) -> str:
    """Why this BG frame would duplicate an already-handled owner message, or "".

    Two durable, cross-process signals — no supervisor handle needed:

    1. ``foreground_answer_recent``: this chat's newest owner row
       (``direction == "in"``) already has a non-background answer row
       (``direction == "out"``, ``sender_identity != "background"``) inside the
       window.
    2. ``foreground_turn_running``: the persisted RUNNING set carries a task
       bound to this chat — the observed case (BG answered at 19:19:11 while the
       turn triggered by the owner's 19:18:26 message answered at 19:19:17).

    Precedence: the rule only ever PREVENTS a frame that has not been sent. A BG
    answer already delivered is never withdrawn, and a later foreground answer is
    untouched — so a BG-first pair (the observed one) keeps its BG row and this
    predicate is what stops the reverse-order duplicate.

    Every unreadable input returns "" (fail open): a broken predicate can cost a
    duplicate, never an answer.
    """
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return ""
    try:
        chat_id = int(evt.get("chat_id"))
    except (TypeError, ValueError):
        return ""
    frame_ts = _as_epoch(evt.get("ts")) or time.time()
    window = BG_ANSWER_ARBITRATION_WINDOW_SECONDS
    if _foreground_turn_running(drive_root, chat_id):
        return "foreground_turn_running"
    entries = _recent_chat_rows(drive_root)
    if not entries:
        return ""
    owner_ts = 0.0
    for row in entries:
        if not isinstance(row, dict) or str(row.get("direction") or "") != "in":
            continue
        if _row_chat_id(row) == chat_id:
            owner_ts = max(owner_ts, _as_epoch(row.get("ts")))
    if owner_ts <= 0.0:
        return ""
    for row in entries:
        if not isinstance(row, dict) or str(row.get("direction") or "") != "out":
            continue
        if str(row.get("sender_identity") or "") == "background":
            continue
        if _row_chat_id(row) != chat_id:
            continue
        answer_ts = _as_epoch(row.get("ts"))
        if owner_ts <= answer_ts and 0.0 <= frame_ts - answer_ts <= window:
            return "foreground_answer_recent"
    return ""


def _record_arbitrated_note(ctx: Any, evt: Dict[str, Any], reason: str) -> bool:
    """Keep an arbitrated BG frame as an observation-inbox note.

    False when the note could not be written: the caller then falls back to the
    ordinary deferred send, so a suppressed frame is never silently lost.
    """
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return False
    try:
        from ouroboros.consciousness import append_observation_row
    except Exception:
        return False
    return append_observation_row(
        drive_root,
        str(evt.get("text") or ""),
        source="owner_delivery",
        kind="note",
        chat_id=evt.get("chat_id"),
        ref={
            "arbitration": reason,
            "window_seconds": BG_ANSWER_ARBITRATION_WINDOW_SECONDS,
            "frame_ts": str(evt.get("ts") or ""),
        },
    )


def deliver_owner_event(ctx: Any, evt: Dict[str, Any]) -> str:
    """Deliver an owner-directed chat event live, or buffer it. Returns
    ``"live"`` or ``"deferred"`` — the caller words its receipt honestly.

    Lineage (``task_id`` / ``parent_task_id`` / ``root_task_id``) is stamped
    for every real-task frame so the supervisor can resolve project binding
    for live deliveries exactly as it does for the end-of-task drain.
    Background-consciousness frames are buffered UNSTAMPED, exactly as they
    were before this seam existed: a pseudo task id would only send the
    supervisor on lineage recovery for a task that never was.
    """
    meta = getattr(ctx, "task_metadata", {})
    meta = meta if isinstance(meta, dict) else {}

    def _deferred() -> str:
        ctx.pending_events.append(evt)
        return "deferred"

    from ouroboros.tool_capabilities import BACKGROUND_DELEGATION_ROLE

    if str(meta.get("delegation_role") or "") == BACKGROUND_DELEGATION_ROLE:
        # C-scheme sender identity (v6.114.3): background-consciousness frames
        # are stamped here so the durable row and live replay can distinguish
        # BG from the foreground agent. Plain task frames stay legacy (no
        # field), keeping old rows and other producers byte-compatible.
        # v6.114.8: FORCE the background identity instead of setdefault —
        # control.py's _send_user_message presets sender_identity="agent", and
        # setdefault would leave that wrong value on a genuine BG reply, so the
        # web renderer showed "Ouroboros" instead of "🧠 Background". The BG
        # role on ctx.task_metadata is the single reliable authority here.
        evt["sender_identity"] = "background"
        # Option (a) arbitration: a BG answer that would duplicate an owner
        # message the foreground has already answered (or is answering right
        # now) is kept as an observation-inbox note instead of becoming a second
        # chat frame. Suppression happens ONLY after the note is durably kept —
        # otherwise the ordinary deferred send stands, so nothing is lost to a
        # failed note write (and the receipt says which of the two happened).
        if str(evt.get("type") or "") == "send_message":
            reason = _foreground_answer_recent(ctx, evt)
            if reason and _record_arbitrated_note(ctx, evt, reason):
                return "noted"
        return _deferred()

    evt.setdefault("task_id", str(getattr(ctx, "task_id", "") or ""))
    evt.setdefault("parent_task_id", str(meta.get("parent_task_id") or ""))
    evt.setdefault("root_task_id", str(meta.get("root_task_id") or ""))
    try:
        from ouroboros.contracts.chat_id_policy import is_a2a_chat_id

        if is_a2a_chat_id(int(evt.get("chat_id"))):
            return _deferred()
    except (TypeError, ValueError):
        pass
    if getattr(ctx, _STICKY_ATTR, False):
        return _deferred()
    event_queue = getattr(ctx, "event_queue", None)
    if event_queue is None:
        return _deferred()
    try:
        event_queue.put_nowait(dict(evt))
        return "live"
    except Exception:
        try:
            setattr(ctx, _STICKY_ATTR, True)
        except Exception:
            pass
        return _deferred()
