"""Small shared helpers for deadline-aware task behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_deadline_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_until(value: Any) -> Optional[float]:
    """Non-negative wall-clock seconds until an ISO instant; None when unparsable."""
    parsed = parse_deadline_ts(value)
    if parsed is None:
        return None
    return max(0.0, (parsed - utc_now()).total_seconds())


def deadline_remaining_sec(ctx: Any) -> float:
    meta = getattr(ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return 0.0
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    return (deadline - utc_now()).total_seconds() if deadline is not None else 0.0


def has_deadline(ctx: Any) -> bool:
    """Whether a deadline EXISTS, which the remaining seconds alone cannot answer.

    ``deadline_remaining_sec`` returns 0.0 both for "no deadline set" and for one that
    expires exactly now, and goes negative once it is spent — so a caller deciding
    whether to bound itself has to ask this separately or it will read a spent deadline
    (negative) and a sub-second one (0.x truncated to 0) as "unbounded".
    """
    meta = getattr(ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return False
    return parse_deadline_ts(meta.get("deadline_at")) is not None


def window_within_deadline(ctx: Any, requested: int) -> int:
    """``requested`` seconds, narrowed so a held window cannot outlive the deadline.

    NARROW-ONLY, and keyed on EXISTENCE rather than sign: `int(remaining) > 0` let a
    wait hold its whole window past a deadline in two shapes — a sub-second remainder
    truncated to 0, and a spent deadline, whose remainder is negative. Only "no deadline
    set" takes the full ask; both spent shapes land on the floor. The finalization GRACE
    is subtracted for the reason the network tools subtract it: targeting the whole
    remaining deadline returns at the instant there is no time left to answer at all.
    """
    if not has_deadline(ctx):
        return max(1, int(requested))
    from ouroboros.task_pacing import effective_finalization_reserve_sec

    remaining = float(deadline_remaining_sec(ctx) or 0.0)
    return max(1, int(min(requested, remaining - float(effective_finalization_reserve_sec(ctx)))))


def llm_transport_timeout_sec(explicit: Any = None) -> float:
    """Return the transport/dead-socket timeout for one LLM request.

    A caller may narrow this value explicitly.  The shared default is deliberately
    read from ``config.py`` and is never used as a logical cognition deadline.
    """
    try:
        requested = float(explicit)
    except (TypeError, ValueError):
        requested = 0.0
    if requested > 0:
        return requested
    from ouroboros.config import get_llm_transport_read_timeout_sec

    return float(get_llm_transport_read_timeout_sec())


def logical_operation_timeout_sec(
    explicit: Any = None,
    *,
    deadline_at: Any = None,
    fallback: Any = None,
    reserve_sec: Any = 0.0,
) -> float:
    """Choose a logical operation wait without borrowing a socket timeout.

    An explicit slot/task value is the operation's requested window, but an
    existing owner deadline always narrows it.  Absent both, ``fallback`` is
    used (normally the transport bound only as a settlement wait, not as a
    cognition target).
    """
    try:
        requested = float(explicit)
    except (TypeError, ValueError):
        requested = 0.0
    remaining = seconds_until(deadline_at)
    if remaining is not None:
        try:
            reserve = max(0.0, float(reserve_sec or 0.0))
        except (TypeError, ValueError):
            reserve = 0.0
        remaining = max(0.0, remaining - reserve)
        if requested <= 0:
            requested = 0.0
        else:
            requested = min(requested, remaining)
        return max(0.001, requested if requested > 0 else remaining)
    if requested > 0:
        return requested
    try:
        value = float(fallback)
    except (TypeError, ValueError):
        value = llm_transport_timeout_sec()
    return max(0.001, value)
