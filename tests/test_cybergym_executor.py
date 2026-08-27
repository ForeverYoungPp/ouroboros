"""Dependency-injected CyberGym executor tests.

No Docker daemon, upstream package, or provider credential is used here.  The
tests exercise the immutable task body and the exact command/HTTP boundaries;
the live smoke remains an operator action documented by the benchmark.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from devtools.benchmarks.cybergym.cybergym_executor import (
    CommandResult,
    CyberGymExecutor,
    ExecutorConfig,
    ExecutorFailure,
    _require_exact_effort,
    _validate_verify_response,
)


def _config(tmp_path: pathlib.Path, **overrides):
    source = tmp_path / "source"
    data = tmp_path / "data"
    run = tmp_path / "run"
    server = tmp_path / "server"
    for path in (source, data, run, server):
        path.mkdir(exist_ok=True)
    mask = server / "mask_map.json"
    mask.write_text("{}", encoding="utf-8")
    values = dict(
        campaign_id="test-campaign",
        source_root=source,
        data_root=data,
        mask_map=mask,
        run_root=run,
        server_root=server,
        server_image="cybergym/server:pin",
        server_image_digest="sha256:" + "1" * 64,
        workspace_image="ouroboros/workspace:pin",
        workspace_image_digest="sha256:" + "2" * 64,
        ouroboros_url="http://127.0.0.1:8765",
        docker_host="unix:///run/user/1006/docker.sock",
        provider_probe=False,
    )
    values.update(overrides)
    return ExecutorConfig(**values)


def test_executor_rejects_non_rootless_or_missing_digest(tmp_path):
    with pytest.raises(ExecutorFailure):
        _config(tmp_path, docker_host="unix:///var/run/docker.sock")
    with pytest.raises(ExecutorFailure):
        _config(tmp_path, workspace_image_digest="latest")


def test_task_body_is_opaque_and_preserves_network_contract(tmp_path):
    config = _config(tmp_path)
    executor = CyberGymExecutor(config)
    task_dir = config.run_root / "task"
    task_dir.mkdir()
    (task_dir / "description.txt").write_text("Find the crash", encoding="utf-8")
    body = executor._task_body(  # noqa: SLF001 - pure boundary assertion
        type("Task", (), {"task_id": "arvo:1", "metadata": {}})(),
        task_dir,
        "cybergym-workspace-agent-" + "a" * 24,
        "attempt-1",
    )
    assert body["task_id"].startswith("cybergym-")
    assert ":" not in body["task_id"]
    assert body["allowed_resources"] == {"network": True, "web": False, "internet": False}
    assert body["executor_ref"]["network"] == "host"
    assert body["executor_ref"]["workspace_backend_path"] == "/workspace"
    assert "arvo:1" not in body["metadata"]


def test_start_uses_same_absolute_server_root_and_docs_probe(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setenv("CYBERGYM_API_KEY", "test-secret-value")
    calls = []

    def command(argv, *, cwd=None, env=None, timeout=None):
        calls.append(list(argv))
        if "network" in argv and "create" in argv:
            return CommandResult(0, "network-id\n", "")
        if "inspect" in argv and "network" in argv:
            return CommandResult(0, '[{"Name":"cybergym-internal","Id":"network-id","Internal":true,"Driver":"bridge","Labels":{"com.ouroboros.campaign":"test-campaign"}}]', "")
        if "run" in argv:
            return CommandResult(0, "server-container-id\n", "")
        if "inspect" in argv and "container" in argv:
            return CommandResult(0, '[{"Name":"/cybergym-server-test-campaign","Id":"server-container-id","State":{"Running":true},"HostConfig":{"NetworkMode":"cybergym-internal"},"NetworkSettings":{"Networks":{"cybergym-internal":{"Aliases":["cybergym-server-test-campaign"],"NetworkID":"network-id"}}},"Config":{"Labels":{},"Image":"sha256:' + "1" * 64 + '"}}]', "")
        return CommandResult(0, "", "")

    seen_http = []

    def http(method, url, **kwargs):
        seen_http.append((method, url))
        return {
            "openapi": "3.0.0",
            "paths": {
                "/submit-vul": {},
                "/submit-fix": {},
                "/query-poc": {},
                "/verify-agent-pocs": {},
            },
        }

    executor = CyberGymExecutor(dataclasses_replace(config, command_runner=command, http_runner=http, provider_probe=False))
    executor.start()
    assert any("--mount" in call and str(config.server_root) in " ".join(call) for call in calls)
    assert seen_http == [("GET", "http://127.0.0.1:8667/openapi.json")]


def test_readiness_rejects_openapi_without_private_submit_fix(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setenv("CYBERGYM_API_KEY", "test-secret-value")
    import devtools.benchmarks.cybergym.cybergym_executor as executor_module

    ticks = iter((0.0, 121.0))
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: next(ticks))

    def command(argv, *, cwd=None, env=None, timeout=None):
        if "network" in argv and "create" in argv:
            return CommandResult(0, "network-id\n", "")
        if "inspect" in argv and "network" in argv:
            return CommandResult(
                0,
                '[{"Name":"cybergym-internal","Id":"network-id","Internal":true,"Driver":"bridge","Labels":{"com.ouroboros.campaign":"test-campaign"}}]',
                "",
            )
        if "run" in argv:
            return CommandResult(0, "server-container-id\n", "")
        if "inspect" in argv and "container" in argv:
            return CommandResult(
                0,
                '[{"Name":"/cybergym-server-test-campaign","Id":"server-container-id","State":{"Running":true},"HostConfig":{"NetworkMode":"cybergym-internal"},"NetworkSettings":{"Networks":{"cybergym-internal":{"Aliases":["cybergym-server-test-campaign"],"NetworkID":"network-id"}}},"Config":{"Labels":{},"Image":"sha256:'
                + "1" * 64
                + '"}}]',
                "",
            )
        return CommandResult(0, "", "")

    def http(method, url, **kwargs):
        return {"openapi": "3.0.0", "paths": {"/submit-vul": {}, "/query-poc": {}, "/verify-agent-pocs": {}}}

    executor = CyberGymExecutor(
        dataclasses_replace(config, command_runner=command, http_runner=http, provider_probe=False)
    )
    with pytest.raises(ExecutorFailure, match="documented route"):
        executor.start()


def test_verify_response_requires_success_body_and_designated_poc():
    good = {"message": "All 1 PoCs for this agent_id have been verified", "poc_ids": ["poc-1"]}
    assert _validate_verify_response(good, expected_poc_id="poc-1") == good
    with pytest.raises(ExecutorFailure, match="HTTP 500"):
        _validate_verify_response({"status_code": 500, "body": {"detail": "failed"}})
    with pytest.raises(ExecutorFailure, match="poc_ids"):
        _validate_verify_response({"message": "ok", "poc_ids": []})
    with pytest.raises(ExecutorFailure, match="designated poc_id"):
        _validate_verify_response(good, expected_poc_id="other")


def test_observed_effort_must_be_exactly_high():
    assert _require_exact_effort("high") == "high"
    for value in ("", "High", "max", None):
        with pytest.raises(ExecutorFailure, match="exactly high"):
            _require_exact_effort(value)


def test_private_query_rejects_http_and_body_errors(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setenv("CYBERGYM_API_KEY", "test-secret-value")
    executor = CyberGymExecutor(
        dataclasses_replace(
            config,
            http_runner=lambda *args, **kwargs: {"status_code": 404, "body": {"detail": "Record not found"}},
        )
    )
    with pytest.raises(ExecutorFailure, match="HTTP 404"):
        executor._private_query("agent-" + "a" * 24, "arvo:1")

    executor = CyberGymExecutor(
        dataclasses_replace(
            config,
            http_runner=lambda *args, **kwargs: {"status_code": 200, "body": {"error": {"message": "bad"}}},
        )
    )
    with pytest.raises(ExecutorFailure, match="error object"):
        executor._private_query("agent-" + "a" * 24, "arvo:1")


def test_submit_response_binds_poc_id_not_nonexistent_hash_and_keeps_exit_code(tmp_path):
    config = _config(tmp_path)
    task_dir = config.run_root / "task"
    task_dir.mkdir()
    (task_dir / "final.poc").write_bytes(b"poc-bytes")
    (task_dir / "submit.sh").write_text("TASK_ID=opaque1234\n", encoding="utf-8")

    def command(argv, *, cwd=None, env=None, timeout=None):
        return CommandResult(
            0,
            json.dumps(
                {
                    "task_id": "opaque1234",
                    "poc_id": "poc-1",
                    "exit_code": 71,
                    "output": "known",
                    # Upstream does not define a response hash; an incidental
                    # field must not override the local marker binding.
                    "hash": "not-the-poc-hash",
                }
            ),
            "",
        )

    response, digest, masked = CyberGymExecutor(
        dataclasses_replace(config, command_runner=command)
    )._submit_final(  # noqa: SLF001 - boundary contract assertion
        type("Task", (), {"task_id": "arvo:1"})(), task_dir, "workspace"
    )
    assert response["poc_id"] == "poc-1"
    assert response["exit_code"] == 71
    assert digest == hashlib.sha256(b"poc-bytes").hexdigest()
    assert masked == "opaque1234"


def dataclasses_replace(config, **changes):
    import dataclasses

    return dataclasses.replace(config, **changes)
