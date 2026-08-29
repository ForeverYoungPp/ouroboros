"""Lane C2 contracts: the covered remote httpx LLM client classes (cached,
no-proxy, web-search OpenAI) are built on the shared keepalive transport
factory; unsupported platforms fall back to the safe minimum; proxy-routed
installs keep env-proxy mounts. No assertions on private httpx internals."""

from __future__ import annotations

import socket
import sys
import types
from unittest.mock import patch

import httpx

from ouroboros import net_transport, platform_layer
from ouroboros.config import (
    TCP_KEEPALIVE_IDLE_SEC,
    TCP_KEEPALIVE_INTERVAL_SEC,
    TCP_KEEPALIVE_PROBE_COUNT,
)
from ouroboros.llm import LLMClient


def _target():
    return {
        "provider": "openrouter",
        "resolved_model": "openai/gpt-5.5",
        "usage_model": "openai/gpt-5.5",
        "api_key": "test-key",
        "base_url": "https://openrouter.ai/api/v1",
        "default_headers": {},
        "supports_openrouter_extensions": True,
        "supports_generation_cost": False,
    }


def _clear_proxy_env(monkeypatch):
    """Keep transport-construction tests hermetic against ambient env proxies."""
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def test_keepalive_socket_options_enable_keepalive_everywhere():
    options = platform_layer.tcp_keepalive_socket_options()
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options
    for option in options:
        assert len(option) == 3
        assert all(isinstance(part, int) for part in option)


def test_keepalive_socket_options_unsupported_platform_falls_back_to_minimum(monkeypatch):
    monkeypatch.setattr(platform_layer, "IS_LINUX", False)
    monkeypatch.setattr(platform_layer, "IS_MACOS", False)
    options = platform_layer.tcp_keepalive_socket_options()
    assert options == [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]


def test_keepalive_socket_options_pin_linux_probe_tuning(monkeypatch):
    """Linux gets the full probe tuning with the config constants, in order
    (deleting the IS_LINUX branch must not stay green)."""
    monkeypatch.setattr(platform_layer, "IS_LINUX", True)
    monkeypatch.setattr(platform_layer, "IS_MACOS", False)
    # Guarantee the constants exist on every CI platform.
    monkeypatch.setattr(socket, "TCP_KEEPIDLE", 4, raising=False)
    monkeypatch.setattr(socket, "TCP_KEEPINTVL", 5, raising=False)
    monkeypatch.setattr(socket, "TCP_KEEPCNT", 6, raising=False)

    options = platform_layer.tcp_keepalive_socket_options()

    assert options == [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE_SEC),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL_SEC),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_PROBE_COUNT),
    ]


def test_keepalive_socket_options_pin_darwin_idle_spelling(monkeypatch):
    """Darwin spells the idle threshold TCP_KEEPALIVE; when the constant
    exists the option carries the config idle value."""
    monkeypatch.setattr(platform_layer, "IS_LINUX", False)
    monkeypatch.setattr(platform_layer, "IS_MACOS", True)
    monkeypatch.setattr(socket, "TCP_KEEPALIVE", 0x10, raising=False)

    options = platform_layer.tcp_keepalive_socket_options()

    assert options == [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, TCP_KEEPALIVE_IDLE_SEC),
    ]


def test_keepalive_socket_options_are_accepted_by_httpx_transports():
    options = platform_layer.tcp_keepalive_socket_options()
    httpx.HTTPTransport(socket_options=options)
    httpx.AsyncHTTPTransport(socket_options=options)


def test_transport_factory_forwards_trust_env_and_limits(monkeypatch):
    """The factory forwards trust_env and limits into the httpx transports
    (an explicit transport is used as-is, so Client-level settings never
    reach the SSL context or the pool)."""
    sync_kwargs = []
    async_kwargs = []

    class FakeTransport:
        def __init__(self, **kwargs):
            sync_kwargs.append(kwargs)

    class FakeAsyncTransport:
        def __init__(self, **kwargs):
            async_kwargs.append(kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeAsyncTransport)
    limits = httpx.Limits(max_connections=7)

    net_transport.remote_httpx_transport(trust_env=False)
    net_transport.remote_httpx_transport(True, limits=limits)

    assert sync_kwargs[0]["trust_env"] is False
    assert "limits" not in sync_kwargs[0]
    assert sync_kwargs[0]["socket_options"]
    assert async_kwargs[0]["trust_env"] is True
    assert async_kwargs[0]["limits"] is limits


def test_no_proxy_sync_client_carries_keepalive_transport():
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("httpx.Client", FakeClient), patch("openai.OpenAI"):
        LLMClient._make_no_proxy_client(_target())

    assert captured.get("trust_env") is False
    assert captured.get("mounts") == {}
    assert isinstance(captured.get("transport"), httpx.HTTPTransport)


def test_no_proxy_async_client_carries_keepalive_transport():
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("httpx.AsyncClient", FakeAsyncClient), patch("openai.AsyncOpenAI"):
        LLMClient._make_no_proxy_async_client(_target())

    assert captured.get("trust_env") is False
    assert captured.get("mounts") == {}
    assert isinstance(captured.get("transport"), httpx.AsyncHTTPTransport)


def test_no_proxy_transports_ignore_ssl_env_and_keep_httpx_pool_defaults(monkeypatch):
    """The no-proxy clients pass trust_env=False into their transports (SSL
    env isolation survives the explicit transport) and do not override the
    per-call httpx pool defaults."""
    sync_kwargs = []
    async_kwargs = []

    class FakeTransport:
        def __init__(self, **kwargs):
            sync_kwargs.append(kwargs)

    class FakeAsyncTransport:
        def __init__(self, **kwargs):
            async_kwargs.append(kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeAsyncTransport)
    with patch("httpx.Client"), patch("openai.OpenAI"):
        LLMClient._make_no_proxy_client(_target())
    with patch("httpx.AsyncClient"), patch("openai.AsyncOpenAI"):
        LLMClient._make_no_proxy_async_client(_target())

    assert len(sync_kwargs) == 1 and len(async_kwargs) == 1
    assert sync_kwargs[0]["trust_env"] is False
    assert async_kwargs[0]["trust_env"] is False
    assert "limits" not in sync_kwargs[0]
    assert "limits" not in async_kwargs[0]


def test_cached_sync_client_carries_keepalive_transport(monkeypatch):
    _clear_proxy_env(monkeypatch)
    captured = {}
    built_clients = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeDefaultHttpxClient:
        def __init__(self, **kwargs):
            built_clients.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI, DefaultHttpxClient=FakeDefaultHttpxClient),
    )
    LLMClient._new_remote_client(_target())

    assert captured.get("max_retries") == 0
    assert isinstance(captured.get("http_client"), FakeDefaultHttpxClient)
    assert len(built_clients) == 1
    assert isinstance(built_clients[0].get("transport"), httpx.HTTPTransport)


def test_cached_async_client_carries_keepalive_transport(monkeypatch):
    _clear_proxy_env(monkeypatch)
    captured = {}
    built_clients = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeDefaultAsyncHttpxClient:
        def __init__(self, **kwargs):
            built_clients.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(
            AsyncOpenAI=FakeAsyncOpenAI,
            DefaultAsyncHttpxClient=FakeDefaultAsyncHttpxClient,
        ),
    )
    client = LLMClient(api_key="test-key")
    client._get_async_remote_client(_target())

    assert captured.get("max_retries") == 0
    assert isinstance(captured.get("http_client"), FakeDefaultAsyncHttpxClient)
    assert len(built_clients) == 1
    assert isinstance(built_clients[0].get("transport"), httpx.AsyncHTTPTransport)


def test_cached_client_transports_carry_sdk_pool_limits(monkeypatch):
    """The cached-client transports forward SDK-equivalent pool limits — an
    explicit transport would otherwise silently downgrade openai's 1000/100
    defaults to the httpx 100/20 defaults."""
    _clear_proxy_env(monkeypatch)
    transports = []

    class FakeTransport:
        def __init__(self, **kwargs):
            transports.append(kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", FakeTransport)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(
            DefaultHttpxClient=lambda **kw: kw,
            DefaultAsyncHttpxClient=lambda **kw: kw,
        ),
    )
    net_transport.keepalive_http_client()
    net_transport.keepalive_http_client(async_client=True)

    assert len(transports) == 2
    for kwargs in transports:
        limits = kwargs["limits"]
        assert limits.max_connections == 1000
        assert limits.max_keepalive_connections == 100
        assert limits.keepalive_expiry == 5.0


def test_cached_clients_keep_env_proxy_mounts_on_proxied_installs(monkeypatch):
    """With HTTP(S)_PROXY configured the cached clients are built WITHOUT an
    explicit transport: httpx enables env-proxy mounts only when no transport
    is passed, so the keepalive transport would break proxy routing."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    sync_captured = {}
    async_captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            sync_captured.update(kwargs)

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            async_captured.update(kwargs)

    def _must_not_build(**_kwargs):
        raise AssertionError("proxied installs must keep SDK default construction")

    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(
            OpenAI=FakeOpenAI,
            AsyncOpenAI=FakeAsyncOpenAI,
            DefaultHttpxClient=_must_not_build,
            DefaultAsyncHttpxClient=_must_not_build,
        ),
    )
    LLMClient._new_remote_client(_target())
    LLMClient(api_key="test-key")._get_async_remote_client(_target())

    assert "http_client" not in sync_captured
    assert "http_client" not in async_captured
    assert sync_captured.get("max_retries") == 0
    assert async_captured.get("max_retries") == 0


def test_env_proxies_detection_ignores_no_proxy_alone(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("no_proxy", "localhost")
    assert net_transport.env_proxies_configured() is False
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    assert net_transport.env_proxies_configured() is True


def test_web_search_openai_client_carries_keepalive_transport(monkeypatch):
    """Q16 coverage: the web-search OpenAI client construction rides the
    shared keepalive transport (the anthropic web-search client is a
    disclosed residual)."""
    _clear_proxy_env(monkeypatch)
    captured = {}
    built_clients = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeDefaultHttpxClient:
        def __init__(self, **kwargs):
            built_clients.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI, DefaultHttpxClient=FakeDefaultHttpxClient),
    )
    net_transport.web_search_openai_client(
        api_key="k", base_url="https://openrouter.ai/api/v1", timeout=30.0,
    )

    assert captured["max_retries"] == 0
    assert captured["timeout"] == 30.0
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert isinstance(captured["http_client"], FakeDefaultHttpxClient)
    assert len(built_clients) == 1
    assert isinstance(built_clients[0].get("transport"), httpx.HTTPTransport)
