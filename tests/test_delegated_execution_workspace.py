"""Version-negotiated Claudexor execution-workspace wire contract."""

from types import SimpleNamespace

from ouroboros.subagents import (
    DelegationRoute,
    delegated_execution_workspace_root,
    delegated_run_shape,
)
from ouroboros.tools.delegate import _start_request


def _request(version: str, *, acting: bool) -> dict:
    shape = delegated_run_shape(acting)
    root = "/tmp/private-execution-snapshot"
    gateway = SimpleNamespace(engine_version=version)
    execution_root = delegated_execution_workspace_root(gateway, shape, root)
    return _start_request(
        SimpleNamespace(), DelegationRoute("codex"), shape, root,
        "do the work", 300, "host instructions", execution_root,
    )


def test_legacy_strict_schema_keeps_the_byte_compatible_execution_shape():
    request = _request("3.8.0", acting=True)
    assert request["execution"] == {"isolation": "live", "delegated": True}


def test_new_schema_receives_the_private_snapshot_as_execution_workspace():
    request = _request("3.8.1", acting=True)
    root = request["scope"]["root"]
    assert request["execution"] == {
        "isolation": "live", "delegated": True, "workspaceRoot": root,
    }


def test_readonly_shape_never_sends_a_live_execution_workspace():
    request = _request("99.0.0", acting=False)
    assert request["mode"] == "ask" and "execution" not in request
