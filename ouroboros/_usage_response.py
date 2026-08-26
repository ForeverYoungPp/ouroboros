"""Pure provider-response usage normalization for physical accounting."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return value


def _number(value: Any) -> Optional[float]:
    """Parse a provider-reported cost; ``None`` means the value cannot be trusted.

    This is the AUTHORITATIVE boundary — what survives here is what the durable
    attempt ledger settles as final money — so it applies the same predicate as
    the loop-side projection in ``loop_llm_call._provider_cost_value``: ``bool``
    is rejected FIRST (``float(True)`` is a plausible-looking 1.0 that would
    settle as a FINAL $1.00 and eat real budget admission), then anything
    unparseable, non-finite, or negative. A reported ``0.0`` stays a legitimate
    zero. Rejecting here settles the attempt as unknown rather than as a
    fabricated amount (BIBLE P1).
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _reported_token_count(usage: Dict[str, Any], *keys: str) -> Optional[int]:
    """Return the first reported count; absence stays distinct from explicit zero."""
    for key in keys:
        if key in usage and usage.get(key) is not None:
            return max(0, int(usage[key]))
    return None


def usage_from_response(response: Any) -> Tuple[Dict[str, Any], Optional[float], bool]:
    """Extract common usage/cost facts without retaining response text."""
    payload: Any = _plain(response)
    if not isinstance(payload, dict) and callable(getattr(response, "json", None)):
        try:
            payload = response.json()
        except Exception:
            payload = None
    usage: Any = payload.get("usage") if isinstance(payload, dict) else getattr(response, "usage", None)
    usage = _plain(usage)
    if not isinstance(usage, dict):
        usage = {}
    native_cache_read = _reported_token_count(usage, "cache_read_input_tokens")
    native_cache_write = _reported_token_count(usage, "cache_creation_input_tokens")
    cache_read = _reported_token_count(
        usage, "cache_read_input_tokens", "cached_tokens", "precached_prompt_tokens",
    )
    cache_write = _reported_token_count(
        usage, "cache_creation_input_tokens", "cache_write_tokens",
    )
    input_tokens = _reported_token_count(usage, "input_tokens")
    prompt = _reported_token_count(usage, "prompt_tokens")
    if prompt is None and any(
        value is not None for value in (input_tokens, native_cache_read, native_cache_write)
    ):
        # Anthropic native input_tokens excludes cache reads and writes.
        prompt = int(input_tokens or 0) + int(native_cache_read or 0) + int(native_cache_write or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict):
        detail_read = _reported_token_count(details, "cached_tokens")
        cache_read = detail_read if detail_read is not None else cache_read
        detail_write = _reported_token_count(
            details, "cache_write_tokens", "cache_creation_tokens", "cache_creation_input_tokens",
        )
        cache_write = detail_write if detail_write is not None else cache_write
    normalized = {
        **usage,
        "prompt_tokens": prompt,
        "completion_tokens": _reported_token_count(usage, "completion_tokens", "output_tokens"),
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        split = {
            tier: int(creation.get(key) or 0)
            for tier, key in (("5m", "ephemeral_5m_input_tokens"),
                              ("1h", "ephemeral_1h_input_tokens"))
            if int(creation.get(key) or 0) > 0
        }
        if split:
            normalized["cache_write_tokens_by_ttl"] = split
    completion = normalized["completion_tokens"]
    cache_usage_reported = bool(
        (cache_read or 0)
        or (cache_write or 0)
        or any((normalized.get("cache_write_tokens_by_ttl") or {}).values())
    )
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("error"), dict)
        and not (prompt or 0)
        and not (completion or 0)
        and not cache_usage_reported
    ):
        normalized.update(prompt_tokens=0, completion_tokens=0, cached_tokens=0, cache_write_tokens=0)
        return normalized, 0.0, True
    candidates = (
        usage.get("cost"), usage.get("total_cost"),
        payload.get("total_cost_usd") if isinstance(payload, dict) else None,
        getattr(response, "total_cost_usd", None),
    )
    cost = next((number for value in candidates if (number := _number(value)) is not None), None)
    return normalized, cost, cost is not None
