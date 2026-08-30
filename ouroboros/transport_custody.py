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

        # ProxyError is tunnel establishment (CONNECT/SOCKS) failing before any
        # provider request exists — pre-dispatch by construction, like connects.
        safe_types = (
            httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
            httpx.ProxyError,
        )
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


def attempt_custody_event_fields(error: BaseException) -> dict:
    """Additive custody binding for durable error events (nanny-leaf S3).

    The physical-attempt capture already rides the exception; without these
    fields the durable ``llm_api_error`` row cannot be joined to the attempt
    ledger, and the transport class of a wrapped cause (ConnectError vs
    RemoteProtocolError) is unrecoverable after the fact. Bounded type names
    only — never raw cause text.
    """
    # Deliberately read off the exception chain, NOT the contextvar helper
    # (physical_attempt_capture_from_exception's fallback): a durable join key
    # must never bind a stale attempt from an unrelated call.
    capture = getattr(error, "physical_attempt_capture", None)
    seen: set = set()
    walker = getattr(error, "__cause__", None)
    while capture is None and isinstance(walker, BaseException) and id(walker) not in seen:
        # Wrappers (LocalContextTooLargeError, recovery RuntimeError) carry the
        # capture only on their explicit cause — walk it the same way.
        seen.add(id(walker))
        capture = getattr(walker, "physical_attempt_capture", None)
        walker = walker.__cause__
    fields: dict = {}
    if capture is not None:
        fields["physical_attempt_id"] = str(getattr(capture, "attempt_id", "") or "")
        fields["attempt_custody_state"] = str(getattr(capture, "state", "") or "")
        provider_error_type = str(getattr(capture, "provider_error_type", "") or "")
        if provider_error_type:
            fields["provider_error_type"] = provider_error_type
    seen = set()
    current = getattr(error, "__cause__", None)
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        module = type(current).__module__ or ""
        if module.split(".")[0] in (
            "httpx", "httpcore", "requests", "urllib3", "ssl", "socket", "anyio",
        ) or (module == "builtins" and isinstance(current, (ConnectionError, TimeoutError))):
            fields["transport_cause_type"] = type(current).__name__
            break
        current = current.__cause__
    return fields
