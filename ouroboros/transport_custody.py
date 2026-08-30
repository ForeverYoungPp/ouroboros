"""Typed transport facts for the physical-attempt custody seam."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_base_url(base_url: Any) -> bool:
    """True when the configured route targets this very host.

    A loopback OPENAI_COMPATIBLE_BASE_URL (the documented Ollama / LM Studio /
    vLLM setups) is a LOCAL server even though its provider name is not
    "local": its connect failure means that server is down, not that the
    network egress is — so such routes must never classify as a remote
    transport outage worth waiting out.
    """
    text = str(base_url or "").strip()
    if not text:
        return False
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOOPBACK_HOSTS


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
        # Only an explicit ``raise ... from ...`` chain carries transport
        # provenance.  Implicit ``__context__`` also links a later fallback
        # exception to the previous provider leg, which would misclassify a
        # dispatched read timeout as a pre-dispatch connect failure.
        current = current.__cause__
    try:
        import requests
        import urllib3

        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
        if not isinstance(exc, requests.exceptions.ConnectionError):
            return False
        # requests.exceptions.ProxyError subclasses ConnectionError; both the
        # direct and the proxied connect failure arrive as MaxRetryError args.
        for value in getattr(exc, "args", ()):
            if isinstance(value, urllib3.exceptions.MaxRetryError):
                reason = getattr(value, "reason", None)
                if isinstance(reason, urllib3.exceptions.ConnectTimeoutError):
                    return True
                if isinstance(reason, urllib3.exceptions.ProxyError):
                    # An unreachable proxy is a pre-dispatch fact only with
                    # nested connect-time evidence (NewConnectionError is a
                    # ConnectTimeoutError subclass); a proxy HTTP response or
                    # a post-dispatch read failure never matches.
                    nested = getattr(reason, "original_error", None)
                    if isinstance(nested, (
                        urllib3.exceptions.ConnectTimeoutError,
                        urllib3.exceptions.NewConnectionError,
                    )):
                        return True
    except Exception:  # pragma: no cover - optional transport dependency
        pass
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
