"""Shared httpx transport construction for remote LLM clients.

Extracted from ``llm.py`` (size-ratchet byte budget, same precedent as
``loop_transport.py``): one factory owns the TCP-keepalive socket options
for every remote httpx client class, so a NAT/VPN mapping silently dropped
during a long silent reasoning stretch is detected by kernel probes within
minutes instead of hanging until the transport read timeout.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def remote_httpx_transport(
    async_client: bool = False,
    *,
    trust_env: bool = True,
    limits: Optional[Any] = None,
):
    """Build the shared keepalive (Async)HTTPTransport.

    Socket options live on the transport (httpx ignores ``socket_options``
    on the Client itself). ``trust_env`` must be forwarded here too: httpx
    uses an explicit transport as-is, so ``Client(trust_env=False)`` alone
    never reaches ``create_ssl_context`` — the no-proxy clients pass
    ``trust_env=False`` to keep SSL_CERT_FILE/SSL_CERT_DIR env isolation.
    """
    import httpx

    from ouroboros.platform_layer import tcp_keepalive_socket_options

    kwargs: Dict[str, Any] = {
        "socket_options": tcp_keepalive_socket_options(),
        "trust_env": trust_env,
    }
    if limits is not None:
        kwargs["limits"] = limits
    if async_client:
        return httpx.AsyncHTTPTransport(**kwargs)
    return httpx.HTTPTransport(**kwargs)


def _sdk_pool_limits():
    """openai-SDK-equivalent pool limits for long-lived clients.

    An explicit transport ignores the Client-level limits, silently
    downgrading the SDK's 1000/100 pool to the httpx 100/20 defaults — and a
    self-inflicted PoolTimeout would then read as a transport outage.
    """
    import httpx

    return httpx.Limits(
        max_connections=1000, max_keepalive_connections=100, keepalive_expiry=5.0
    )


def env_proxies_configured() -> bool:
    """True when HTTP(S)_PROXY/ALL_PROXY-style env proxies are configured.

    httpx honors env proxies only when no explicit transport is passed
    (``allow_env_proxies = trust_env and transport is None``), so attaching
    the keepalive transport on a proxy-routed install would silently break
    proxy routing. ``no_proxy`` alone does not count.
    """
    import urllib.request

    proxies = urllib.request.getproxies_environment()
    return any(scheme != "no" for scheme in proxies)


def keepalive_http_client(async_client: bool = False):
    """openai Default(Async)HttpxClient on the keepalive transport, or None.

    None on proxy-routed installs: they keep the SDK default construction so
    httpx env-proxy mounts survive (disclosed residual: no TCP-keepalive
    tuning there).
    """
    if env_proxies_configured():
        return None
    if async_client:
        from openai import DefaultAsyncHttpxClient

        return DefaultAsyncHttpxClient(
            transport=remote_httpx_transport(True, limits=_sdk_pool_limits())
        )
    from openai import DefaultHttpxClient

    return DefaultHttpxClient(
        transport=remote_httpx_transport(limits=_sdk_pool_limits())
    )


def make_no_proxy_client(target: Dict[str, Any], timeout: Any) -> Tuple[Any, Any]:
    """Per-call OpenAI client fully isolated from proxy/SSL environment."""
    import httpx
    from openai import OpenAI

    http_client = httpx.Client(
        trust_env=False,
        mounts={},
        timeout=timeout,
        transport=remote_httpx_transport(trust_env=False),
    )
    oa_client = OpenAI(
        api_key=str(target.get("api_key") or ""),
        base_url=str(target.get("base_url") or ""),
        default_headers=dict(target.get("default_headers") or {}),
        http_client=http_client,
        max_retries=0,
    )
    return oa_client, http_client


def make_no_proxy_async_client(target: Dict[str, Any], timeout: Any) -> Tuple[Any, Any]:
    """Async variant of :func:`make_no_proxy_client`."""
    import httpx
    from openai import AsyncOpenAI

    http_client = httpx.AsyncClient(
        trust_env=False,
        mounts={},
        timeout=timeout,
        transport=remote_httpx_transport(async_client=True, trust_env=False),
    )
    oa_client = AsyncOpenAI(
        api_key=str(target.get("api_key") or ""),
        base_url=str(target.get("base_url") or ""),
        default_headers=dict(target.get("default_headers") or {}),
        http_client=http_client,
        max_retries=0,
    )
    return oa_client, http_client


def web_search_openai_client(
    *, api_key: str, base_url: Optional[str], timeout: Optional[float] = None
):
    """Web-search OpenAI client (Q16 coverage) on the keepalive transport."""
    from openai import OpenAI

    kwargs: Dict[str, Any] = {"api_key": api_key, "max_retries": 0}
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    http_client = keepalive_http_client()
    if http_client is not None:
        kwargs["http_client"] = http_client
    return OpenAI(**kwargs)
