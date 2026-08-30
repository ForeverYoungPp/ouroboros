from __future__ import annotations

import pytest
import httpx

from ouroboros import usage_accounting as ua


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    (root / "state").mkdir(parents=True)
    return root


def _request(root):
    return ua.AttemptRequest(
        model="openai/gpt-5.2", provider="openai", reservation_usd=1.0,
        drive_root=root, task_id="transport", root_task_id="transport",
        source="test.transport_custody",
    )


def test_implicit_fallback_context_cannot_release_a_read_timeout(data_root):
    """A later fallback exception inherits the prior leg as implicit context."""
    from ouroboros.transport_custody import (
        is_pre_dispatch_transport_failure, release_pre_dispatch_attempt,
    )

    reservation = ua.reserve_attempt(_request(data_root))
    ua.mark_dispatched(reservation)
    try:
        raise httpx.ConnectError("first leg refused")
    except httpx.ConnectError:
        try:
            raise httpx.ReadTimeout("second leg timed out after dispatch")
        except httpx.ReadTimeout as exc:
            assert exc.__context__ is not None
            assert not is_pre_dispatch_transport_failure(exc)
            assert not release_pre_dispatch_attempt(reservation, exc)
            ua.mark_unresolved(reservation, "read timeout after dispatch")

    assert ua.usage_projection(data_root)["unresolved_upper_bound_usd"] == 1.0


def test_explicit_transport_cause_still_proves_pre_dispatch(data_root):
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    try:
        raise httpx.ConnectError("socket refused")
    except httpx.ConnectError as cause:
        try:
            raise RuntimeError("provider connection wrapper") from cause
        except RuntimeError as wrapper:
            assert is_pre_dispatch_transport_failure(wrapper)


def test_requests_new_connection_error_proves_pre_dispatch(data_root):
    import requests
    import urllib3
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    reason = urllib3.exceptions.NewConnectionError(None, "connection refused")
    wrapped = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(None, "/messages", reason=reason)
    )
    assert is_pre_dispatch_transport_failure(wrapped)


def test_requests_read_timeout_connection_error_does_not_prove_pre_dispatch(data_root):
    import requests
    import urllib3
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    reason = urllib3.exceptions.ReadTimeoutError(None, "/messages", "read timed out")
    wrapped = requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(None, "/messages", reason=reason)
    )
    assert not is_pre_dispatch_transport_failure(wrapped)


def test_requests_proxy_error_with_nested_connect_evidence_proves_pre_dispatch(data_root):
    """The standard unreachable-proxy chain (native Anthropic behind a dead
    proxy): requests.exceptions.ProxyError -> MaxRetryError -> urllib3
    ProxyError -> NewConnectionError is typed pre-dispatch evidence."""
    import requests
    import urllib3
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    nested = urllib3.exceptions.NewConnectionError(
        None, "Failed to establish a new connection: [Errno 111] Connection refused"
    )
    proxy = urllib3.exceptions.ProxyError("Cannot connect to proxy.", nested)
    wrapped = requests.exceptions.ProxyError(
        urllib3.exceptions.MaxRetryError(None, "/messages", reason=proxy)
    )
    assert is_pre_dispatch_transport_failure(wrapped)


def test_requests_proxy_error_with_connect_timeout_evidence_proves_pre_dispatch(data_root):
    import requests
    import urllib3
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    nested = urllib3.exceptions.ConnectTimeoutError("timed out connecting to proxy")
    proxy = urllib3.exceptions.ProxyError("Cannot connect to proxy.", nested)
    wrapped = requests.exceptions.ProxyError(
        urllib3.exceptions.MaxRetryError(None, "/messages", reason=proxy)
    )
    assert is_pre_dispatch_transport_failure(wrapped)


def test_requests_proxy_error_without_connect_evidence_stays_untyped(data_root):
    """A proxy failure that is NOT connect-time (a proxy HTTP response, a
    post-dispatch read failure) must never release custody."""
    import requests
    import urllib3
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure

    proxy = urllib3.exceptions.ProxyError(
        "Your proxy appears to only use HTTP and not HTTPS",
        urllib3.exceptions.HTTPError("bad proxy response"),
    )
    wrapped = requests.exceptions.ProxyError(
        urllib3.exceptions.MaxRetryError(None, "/messages", reason=proxy)
    )
    assert not is_pre_dispatch_transport_failure(wrapped)


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:1234/v1", True),
    ("http://[::1]:8000/v1", True),
    ("https://openrouter.ai/api/v1", False),
    ("https://api.anthropic.com/v1", False),
    ("", False),
    ("not a url", False),
])
def test_is_loopback_base_url(url, expected):
    from ouroboros.transport_custody import is_loopback_base_url

    assert is_loopback_base_url(url) is expected
