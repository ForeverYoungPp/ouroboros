"""Supervisor handlers for structured chat delivery events."""

from __future__ import annotations

import base64
from typing import Any, Dict

from ouroboros.utils import utc_now_iso
from supervisor.log_addressing import bound_project_chat_id as _bound_project_chat_id


def _delivery_chat_id(evt: Dict[str, Any], ctx: Any) -> int | None:
    bound_chat = _bound_project_chat_id(
        ctx, evt.get("task_id"), evt.get("parent_task_id"), evt.get("root_task_id")
    )
    raw_chat_id = evt.get("chat_id")
    if not bound_chat and (raw_chat_id is None or raw_chat_id == ""):
        return None
    return bound_chat or int(raw_chat_id)


def _log_error(ctx: Any, event_type: str, **fields: Any) -> None:
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {"ts": utc_now_iso(), "type": event_type, **fields},
    )


def _handle_send_photo(evt: Dict[str, Any], ctx: Any) -> None:
    """Send a photo to the owner's chat."""
    try:
        # Binding precedence matches text delivery: post-hoc bound media stays
        # in the project panel even when the task retained its original chat id.
        chat_id = _bound_project_chat_id(
            ctx, evt.get("task_id"), evt.get("parent_task_id"), evt.get("root_task_id")
        ) or int(evt.get("chat_id") or 0)
        image_b64 = str(evt.get("image_base64") or "")
        caption = str(evt.get("caption") or "")
        mime = str(evt.get("mime") or "image/png")
        if not chat_id or not image_b64:
            return
        photo_bytes = base64.b64decode(image_b64)
        ok, err = ctx.bridge.send_photo(
            chat_id, photo_bytes, caption=caption, mime=mime,
            task_id=str(evt.get("task_id") or ""),
        )
        if not ok:
            _log_error(ctx, "send_photo_error", chat_id=chat_id, error=err)
    except Exception as exc:
        _log_error(ctx, "send_photo_event_error", error=repr(exc))


def _handle_send_video(evt: Dict[str, Any], ctx: Any) -> None:
    """Send a video to the owner's chat."""
    try:
        chat_id = _delivery_chat_id(evt, ctx)
        video_b64 = str(evt.get("video_base64") or "")
        caption = str(evt.get("caption") or "")
        mime = str(evt.get("mime") or "video/mp4")
        if chat_id is None or not video_b64:
            return
        video_bytes = base64.b64decode(video_b64)
        ok, err = ctx.bridge.send_video(
            chat_id, video_bytes, caption=caption, mime=mime,
            task_id=str(evt.get("task_id") or ""),
        )
        if not ok:
            _log_error(ctx, "send_video_error", chat_id=chat_id, error=err)
    except Exception as exc:
        _log_error(ctx, "send_video_event_error", error=repr(exc))


def _handle_send_document(evt: Dict[str, Any], ctx: Any) -> None:
    """Send an arbitrary document/file to the owner's chat."""
    try:
        chat_id = _delivery_chat_id(evt, ctx)
        file_b64 = str(evt.get("file_base64") or "")
        if chat_id is None or not file_b64:
            return
        ok, err = ctx.bridge.send_document(
            chat_id,
            base64.b64decode(file_b64),
            filename=str(evt.get("filename") or "file"),
            caption=str(evt.get("caption") or ""),
            mime=str(evt.get("mime") or "application/octet-stream"),
            download_url=str(evt.get("download_url") or ""),
            task_id=str(evt.get("task_id") or ""),
        )
        if not ok:
            _log_error(ctx, "send_document_error", chat_id=chat_id, error=err)
    except Exception as exc:
        _log_error(ctx, "send_document_event_error", error=repr(exc))


def _handle_send_links(evt: Dict[str, Any], ctx: Any) -> None:
    """Send structured HTTP(S) actions to the owner's chat."""
    try:
        chat_id = _delivery_chat_id(evt, ctx)
        actions = evt.get("actions")
        if chat_id is None or not isinstance(actions, list) or not actions:
            return
        ok, err = ctx.bridge.send_links(
            chat_id,
            actions,
            title=str(evt.get("title") or ""),
            task_id=str(evt.get("task_id") or ""),
        )
        if not ok:
            _log_error(ctx, "send_links_error", chat_id=chat_id, error=err)
    except Exception as exc:
        _log_error(ctx, "send_links_event_error", error=repr(exc))


def _handle_send_quiz(evt: Dict[str, Any], ctx: Any) -> None:
    """Send an owner quiz card to the owner's chat."""
    try:
        chat_id = _delivery_chat_id(evt, ctx)
        options = evt.get("options")
        if chat_id is None or not isinstance(options, list) or not options:
            return
        if chat_id == 0:
            # Deliberate exception to the "0 is a real hidden session" policy:
            # an interactive card in the hidden panel can never be seen or
            # answered, so a headless author's quiz goes to Main.
            chat_id = 1
        ok, err = ctx.bridge.send_quiz(
            chat_id,
            quiz_id=str(evt.get("quiz_id") or ""),
            question=str(evt.get("question") or ""),
            options=options,
            stake=str(evt.get("stake") or ""),
            assumption=str(evt.get("assumption") or ""),
            state=str(evt.get("state") or "open"),
            task_id=str(evt.get("task_id") or ""),
        )
        if not ok:
            _log_error(ctx, "send_quiz_error", chat_id=chat_id, error=err)
    except Exception as exc:
        _log_error(ctx, "send_quiz_event_error", error=repr(exc))


EVENT_HANDLERS = {
    "send_photo": _handle_send_photo,
    "send_video": _handle_send_video,
    "send_document": _handle_send_document,
    "send_links": _handle_send_links,
    "send_quiz": _handle_send_quiz,
}


__all__ = ["EVENT_HANDLERS"]
