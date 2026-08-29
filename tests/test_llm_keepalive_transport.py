"""Lane C2 contracts: every remote httpx LLM client is built on the shared
keepalive transport factory; unsupported platforms fall back to the safe
minimum. No assertions on private httpx internals."""

from __future__ import annotations

import socket
import sys
import types
from unittest.mock import patch

import httpx

from ouroboros import platform_layer
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


def test_keepalive_socket_options_are_accepted_by_httpx_transports():
    options = platform_layer.tcp_keepalive_socket_options()
    httpx.HTTPTransport(socket_options=options)
    httpx.AsyncHTTPTransport(socket_options=options)


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


def test_cached_sync_client_carries_keepalive_transport(monkeypatch):
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
