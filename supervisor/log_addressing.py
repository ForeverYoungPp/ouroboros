"""Explicit audience for task-scoped live log events (owner decision Q1=A).

Split out of ``supervisor/events.py`` at the module-size boundary;
``events.py`` re-exports the public names so callers keep one surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


def resolve_project_chat(
    drive_root: Any, task_id: Any, parent_task_id: Any = "", root_task_id: Any = ""
) -> int:
    """Project chat for a task tree (own binding -> parent -> root), 0 when
    unbound; never raises. The one binding-resolution seam shared by
    ``address_task_event`` and every per-handler ``_bound_project_chat_id``
    caller."""
    tid = str(task_id or "").strip()
    if not tid:
        return 0
    try:
        from ouroboros.projects_registry import project_chat_for_task_tree

        return int(
            project_chat_for_task_tree(drive_root, tid, parent_task_id, root_task_id) or 0
        )
    except Exception:
        return 0


def bound_project_chat_id(ctx: Any, task_id: Any, parent_task_id: Any = "", root_task_id: Any = "") -> int:
    """Project chat by lineage for ctx-shaped callers (events.py handlers)."""
    return resolve_project_chat(
        getattr(ctx, "DRIVE_ROOT", None), task_id, parent_task_id, root_task_id
    )


def address_task_event(running: Any, drive_root: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Give a task-scoped live event its explicit audience (owner decision Q1=A).

    Worker diagnostics (llm_api_error, tool_timeout, ...) carry only their own
    task_id — never lineage or a chat — so a Project child's error used to fall
    through to the legacy "no address -> Main" default and mint an empty
    Working... card there (issue #296 residual). The supervisor holds the
    host-attested truth: fill missing parent/root from the RUNNING row, let the
    Project binding win (a post-hoc bound task keeps its original chat_id on
    the row, so the binding must take precedence — same order as
    _handle_task_heartbeat), otherwise preserve an explicit chat_id already on
    the event (0 is a real session — the Skill Review panel — never "missing";
    a None value is absence, not an address), otherwise stamp the task row's
    own chat_id, otherwise the DirectActivityRegistry entry (in-process
    direct/ephemeral turns are never in RUNNING). Addressing is HONEST — an
    A2A synthetic id is stamped as the row says it; the broadcast choke
    (push_log) suppresses A2A frames, so machine traffic never reaches the
    browser while the durable row keeps the true audience.
    READ-ONLY over RUNNING: no write-back, so a row popped by a
    cross-thread cancel stays popped (see the in-place-mutation note in
    events._handle_llm_usage). Mutates and returns ``payload``.
    """
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return payload
    task_row: Dict[str, Any] = {}
    if isinstance(running, dict):
        meta = running.get(task_id)
        if isinstance(meta, dict) and isinstance(meta.get("task"), dict):
            task_row = meta["task"]
    for key in ("parent_task_id", "root_task_id"):
        if not payload.get(key) and task_row.get(key):
            payload[key] = str(task_row[key])
    bound_chat = resolve_project_chat(
        drive_root, task_id, payload.get("parent_task_id"), payload.get("root_task_id")
    )
    if bound_chat:
        payload["chat_id"] = bound_chat
        return payload
    if payload.get("chat_id") is not None:
        return payload
    candidate = task_row.get("chat_id")
    if candidate is None:
        try:
            from supervisor.active_activity import get_direct_activity_registry

            entry = get_direct_activity_registry().get(task_id)
        except Exception:
            entry = None
        if entry is not None:
            candidate = entry.chat_id
    if candidate is None:
        return payload
    try:
        payload["chat_id"] = int(candidate)
    except (TypeError, ValueError):
        pass
    return payload


def address_ctx_event(ctx: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """``address_task_event`` for ctx-shaped callers (events.py handlers)."""
    return address_task_event(
        getattr(ctx, "RUNNING", None), getattr(ctx, "DRIVE_ROOT", None), payload
    )


class TurnEventQueue:
    """Address a direct/ephemeral turn's task-scoped events BY VALUE at the
    producer boundary: the turn's DirectActivityRegistry entry (its only chat
    authority) dies with the turn while the supervisor drains its queued
    events LATER, so the address must ride the event itself (the ingress
    capture rule of DEVELOPMENT.md). Wraps the turn's real queue and stamps
    the turn chat onto its own still-unaddressed task-scoped payloads."""

    def __init__(self, inner: Any, task_id: Any, chat_id: Any) -> None:
        self._inner = inner
        self._task_id = str(task_id or "")
        self._chat_id = int(chat_id or 0)

    def stamp(self, item: Any) -> Any:
        if isinstance(item, dict):
            data = item.get("data") if item.get("type") == "log_event" else item
            if (
                isinstance(data, dict)
                and str(data.get("task_id") or "") == self._task_id
                and data.get("chat_id") is None
            ):
                data["chat_id"] = self._chat_id
        return item

    def put(self, item: Any, *args: Any, **kwargs: Any) -> Any:
        return self._inner.put(self.stamp(item), *args, **kwargs)

    def put_nowait(self, item: Any) -> Any:
        return self._inner.put_nowait(self.stamp(item))


def make_server_log_sink(bridge: Any, drive_root: Any, running: Any = None):
    """Build the server-process append_jsonl live sink (installed by server.py).

    The raw ``set_log_sink(bridge.push_log)`` predecessor broadcast every
    server-process append unaddressed (direct-chat turns and Background
    Consciousness run in the server process, so their rows never cross the
    worker sink) and re-broadcast every type a supervisor handler already
    pushes. This wrapper is the exactly-once + explicit-audience choke:
    suppressed types are dropped (their handler push is the one delivery),
    everything else is addressed through ``address_task_event`` on a copy.
    ``running=None`` reads the live ``supervisor.workers.RUNNING`` table at
    call time; tests pass their own mapping.
    """
    from supervisor.workers import SERVER_LOG_SINK_SUPPRESSED_TYPES

    def _server_log_sink(obj: Any) -> None:
        try:
            if isinstance(obj, dict):
                if str(obj.get("type") or "") in SERVER_LOG_SINK_SUPPRESSED_TYPES:
                    return
                if running is None:
                    from supervisor import workers as _workers

                    live_running = getattr(_workers, "RUNNING", None)
                else:
                    live_running = running
                obj = address_task_event(live_running, drive_root, dict(obj))
            bridge.push_log(obj)
        except Exception:
            log.debug("Server log sink delivery failed", exc_info=True)

    return _server_log_sink


def address_handler_push(drive_root: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Address a supervisor-handler's explicit push against the LIVE RUNNING
    table (the handlers that push a suppressed type own the one delivery of
    that event, so they carry the same explicit-audience duty as the sink)."""
    try:
        from supervisor import workers as _workers

        running = getattr(_workers, "RUNNING", None)
    except Exception:
        running = None
    return address_task_event(running, drive_root, payload)
