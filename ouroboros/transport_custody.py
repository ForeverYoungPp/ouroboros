"""Typed transport facts for the physical-attempt custody seam."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def is_pre_dispatch_transport_failure(exc: BaseException) -> bool:
    """Return true only for exceptions raised before request bytes can be sent."""
    try:
        import httpx

        safe_types = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    except Exception:  # pragma: no cover - httpx ships with the runtime
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, safe_types):
            return True
        current = current.__cause__ or current.__context__
    return False


def release_pre_dispatch_attempt(reservation: Any, exc: BaseException) -> bool:
    """Release a marked attempt only after a typed pre-dispatch transport fact."""
    if not is_pre_dispatch_transport_failure(exc):
        return False
    from ouroboros.usage_accounting import _transition

    try:
        _transition(
            reservation,
            "released",
            _allow_dispatched_release=True,
            reason=f"before_dispatch_failed:{type(exc).__name__}",
        )
    except Exception:
        log.exception("Failed to release pre-dispatch physical attempt")
        return False
    return True
