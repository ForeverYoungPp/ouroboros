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
