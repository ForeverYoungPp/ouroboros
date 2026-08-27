"""Dependency-injected CyberGym executor tests.

No Docker daemon, upstream package, or provider credential is used here.  The
tests exercise the immutable task body and the exact command/HTTP boundaries;
the live smoke remains an operator action documented by the benchmark.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib

import pytest

from devtools.benchmarks.cybergym.cybergym_executor import (
    CommandResult,
    CyberGymExecutor,
    ExecutorConfig,
    ExecutorFailure,
    _bind_container_image,
    _parse_json_stdout,
    _require_exact_effort,
    _served_telemetry,
    _validate_verify_response,
)
from devtools.benchmarks.cybergym.cybergym_sidecar import required_resource_labels


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


def test_container_image_binding_rejects_cached_digest_for_wrong_container():
    digest = "sha256:" + "1" * 64
    with pytest.raises(ExecutorFailure, match="identity does not match"):
        _bind_container_image(
            {
                "Image": "sha256:" + "2" * 64,
                "Config": {"Image": "cyber/server@" + digest},
            },
            {"Id": "sha256:" + "3" * 64, "RepoDigests": ["cyber/server@" + digest]},
            digest,
            "server",
        )


def test_task_body_is_opaque_and_preserves_network_contract(tmp_path):
    config = _config(tmp_path)
    executor = CyberGymExecutor(config)
    task_dir = config.run_root / "task"
    task_dir.mkdir()
    (task_dir / "description.txt").write_text("Find the crash", encoding="utf-8")
    container_name = "cybergym-workspace-agent-" + "a" * 24
    executor._task_containers[container_name] = "b" * 64
    body = executor._task_body(  # noqa: SLF001 - pure boundary assertion
        type("Task", (), {"task_id": "arvo:1", "metadata": {}})(),
        task_dir,
        container_name,
        "attempt-1",
    )
    assert body["task_id"].startswith("cybergym-")
    assert ":" not in body["task_id"]
    assert body["allowed_resources"] == {"network": True, "web": False, "internet": False}
    assert body["executor_ref"]["network"] == "host"
    assert body["executor_ref"]["workspace_backend_path"] == "/workspace"
    assert body["executor_ref"]["id"] == "b" * 64
    assert body["executor_ref"]["container_name"] == "b" * 64
    assert "arvo:1" not in body["metadata"]


def test_task_body_requires_immutable_workspace_id(tmp_path):
    config = _config(tmp_path)
    executor = CyberGymExecutor(config)
    task_dir = config.run_root / "task"
    task_dir.mkdir()
    (task_dir / "description.txt").write_text("Find the crash", encoding="utf-8")
    with pytest.raises(ExecutorFailure, match="immutable container id"):
        executor._task_body(  # noqa: SLF001 - boundary contract assertion
            type("Task", (), {"task_id": "arvo:1", "metadata": {}})(),
            task_dir,
            "cybergym-workspace-agent-" + "a" * 24,
            "attempt-1",
        )


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


def test_served_telemetry_prefers_authoritative_trace_refs_over_requested_fields():
    payload = {
        "model": "requested/not-served",
        "reasoning_effort": "high",
        "trace_refs": {
            "llm_call_refs": [
                {"resolved_model": "deepseek/deepseek-v4-flash-0731", "provider": "provider-a"}
            ]
        },
    }
    observed = _served_telemetry(payload)
    assert observed["observed_model"] == "deepseek/deepseek-v4-flash-0731"
    assert observed["observed_provider"] == "provider-a"
    assert observed["trace_call_count"] == 1
    assert observed["effort_source"] == "runtime_requested_field"


def test_served_telemetry_rejects_incomplete_or_mixed_trace_identity():
    with pytest.raises(ExecutorFailure, match="incomplete served-call"):
        _served_telemetry({"trace_refs": {"llm_call_refs": [{"provider": "provider-a"}]}})
    with pytest.raises(ExecutorFailure, match="mixed served models"):
        _served_telemetry(
            {
                "trace_refs": {
                    "llm_call_refs": [
                        {"resolved_model": "model-a", "provider": "provider-a"},
                        {"resolved_model": "model-b", "provider": "provider-a"},
                    ]
                }
            }
        )


def test_served_telemetry_reads_verified_response_wire_effort(tmp_path):
    drive = tmp_path / "drive"
    calls = drive / "observability" / "calls" / "opaque"
    calls.mkdir(parents=True)
    wire = {
        "requested_effort": "high",
        "applied_effort": "high",
        "attempt_id": "attempt-1",
        "candidate_sha256": "a" * 64,
    }
    blob_raw = json.dumps(
        {"usage": {"request_wire": wire}}, sort_keys=True
    ).encode("utf-8")
    blob_path = drive / "observability" / "blobs" / ("b" * 64 + ".json.gz")
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(gzip.compress(blob_raw))
    blob_ref = {
        "path": str(blob_path),
        "sha256": hashlib.sha256(blob_raw).hexdigest(),
        "size": len(blob_raw),
        "kind": "json",
        "encoding": "gzip",
    }
    manifest_raw = json.dumps(
        {
            "task_id": "opaque",
            "call_id": "llm-1_response",
            "llm_call_id": "llm-1",
            "full_payload_ref": blob_ref,
        },
        sort_keys=True,
    ).encode("utf-8")
    manifest_path = calls / "llm-1_response.json"
    manifest_path.write_bytes(manifest_raw)
    manifest_ref = {
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "call_id": "llm-1_response",
    }

    observed = _served_telemetry(
        {
            "reasoning_effort": "low",
            "trace_refs": {
                "llm_call_refs": [
                    {
                        "llm_call_id": "llm-1",
                        "resolved_model": "deepseek/deepseek-v4-flash-0731",
                        "provider": "provider-a",
                        "response_ref": manifest_ref,
                    }
                ]
            },
        },
        allowed_roots=(drive,),
    )
    assert observed["observed_effort"] == "high"
    assert observed["effort_source"] == "served_response_wire"
    assert observed["response_wire_effort_count"] == 1


def test_submit_stdout_parser_accepts_preceding_prose_and_multiline_json():
    parsed = _parse_json_stdout('notice\n{\n  "task_id": "opaque1234",\n  "poc_id": "poc-1"\n}\n')
    assert parsed == {"task_id": "opaque1234", "poc_id": "poc-1"}


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


def test_runtime_attestation_reinspects_immutable_ids_before_gateway_boundary(tmp_path, monkeypatch):
    config = _config(tmp_path)
    executor = CyberGymExecutor(config)
    agent_id = "agent-" + "a" * 24
    workspace_name = "cybergym-workspace-" + agent_id
    plan = executor._task_network_plan("arvo:1", agent_id)
    executor.network_id = "network-123"
    executor.server_id = "server-123"
    executor.server_name = "cybergym-server-test-campaign"
    executor._task_containers = {workspace_name: "workspace-123"}

    server = {
        "Id": "server-123",
        "Name": "/" + executor.server_name,
        "Config": {
            "Labels": required_resource_labels(plan, "server"),
            "RepoDigests": ["cybergym/server@" + config.server_image_digest],
        },
        "State": {"Pid": 101, "Running": True},
        "HostConfig": {"NetworkMode": "cybergym-internal"},
        "NetworkSettings": {
            "Networks": {
                "cybergym-internal": {
                    "NetworkID": "network-123",
                    "Aliases": [plan.server_alias],
                }
            },
            # Rootless Docker does not publish ports from an --internal
            # bridge; private calls use the server's immutable-id exec path.
            "Ports": {"8666/tcp": None},
        },
        "Mounts": [
            {
                "Source": "/run/user/1006/docker.sock",
                "Destination": "/var/run/docker.sock",
            }
        ],
    }
    workspace = {
        "Id": "workspace-123",
        "Name": "/" + workspace_name,
        "Config": {
            "Labels": required_resource_labels(plan, "workspace"),
            "RepoDigests": ["ouroboros/workspace@" + config.workspace_image_digest],
        },
        "State": {"Pid": 202, "Running": True},
        "HostConfig": {"NetworkMode": "cybergym-internal"},
        "NetworkSettings": {
            "Networks": {
                "cybergym-internal": {
                    "NetworkID": "network-123",
                    "Aliases": [plan.workspace_alias],
                }
            }
        },
        "Mounts": [],
    }
    network = {
        "Name": "cybergym-internal",
        "Id": "network-123",
        "Internal": True,
        "Driver": "bridge",
        "Labels": {"com.ouroboros.campaign": config.campaign_id},
    }
    executor._server_observation = server
    executor._workspace_observations[workspace_name] = workspace
    inspected = []

    def inspect(kind, name):
        inspected.append((kind, name))
        if kind == "network":
            return network
        if name == "server-123":
            return server
        if name == "workspace-123":
            return workspace
        raise AssertionError((kind, name))

    monkeypatch.setattr(executor, "_inspect", inspect)
    monkeypatch.setattr(
        executor,
        "_connectivity_observation",
        lambda plan, workspace_id, api_key: {
            "agent_to_server": True,
            "verifier_to_private": {"reachable": True},
            "agent_to_public": False,
            "agent_to_verifier": False,
            "agent_socket_visible": False,
            "agent_hidden_artifacts": {
                "/cybergym-server-data": True,
                "/cybergym-mask-map.json": True,
                "/cybergym-poc.db": True,
                "/cybergym-fixed": True,
            },
            "agent_secret_env_absent": True,
            "agent_probe_tools": True,
        },
    )
    report = executor._attest_runtime(  # noqa: SLF001 - boundary contract assertion
        type("Task", (), {"task_id": "arvo:1"})(),
        "attempt-1",
        plan,
        workspace_name,
        "valid-key",
    )
    assert report["ok"] is True
    assert ("container", "server-123") in inspected
    assert ("container", "workspace-123") in inspected
    assert ("network", "network-123") in inspected
    assert (config.run_root / "attestations" / "arvo__1" / "attempt-1" / "sidecar_attestation.json").is_file()


def test_settled_workspace_cleanup_uses_exact_id_and_postcondition(tmp_path, monkeypatch):
    config = _config(tmp_path)
    executor = CyberGymExecutor(config)
    executor.network_id = "network-123"
    name = "cybergym-workspace-agent-" + "a" * 24
    container_id = "workspace-123"
    executor._task_containers = {name: container_id}
    observed = {
        "Id": container_id,
        "Name": "/" + name,
        "Config": {
            "Labels": {
                "com.ouroboros.campaign": config.campaign_id,
                "com.ouroboros.role": "workspace",
            }
        },
        "NetworkSettings": {
            "Networks": {
                "cybergym-internal": {"NetworkID": executor.network_id}
            }
        },
    }
    inspect_calls = []
    removed = False

    def inspect_optional(kind, target):
        nonlocal removed
        inspect_calls.append((kind, target))
        if kind != "container":
            raise AssertionError((kind, target))
        if target == container_id and not removed:
            return observed
        return None

    docker_calls = []

    def docker(*args, **kwargs):
        nonlocal removed
        docker_calls.append(args)
        removed = True
        return CommandResult(0, "", "")

    monkeypatch.setattr(executor, "_inspect_optional", inspect_optional)
    monkeypatch.setattr(executor, "_docker", docker)
    report_path = config.run_root / "cleanup.json"
    report = executor._cleanup_workspace_container(  # noqa: SLF001 - custody assertion
        name, "arvo:1", "attempt-1", report_path
    )
    assert report["ok"] is True
    assert docker_calls == [("rm", "--force", container_id)]
    assert ("container", container_id) in inspect_calls
    assert all(name not in call for call in docker_calls)
    assert report_path.is_file()
    assert name not in executor._task_containers


def test_private_query_accepts_nested_items_wrapper(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setenv("CYBERGYM_API_KEY", "test-secret-value")
    record = {"task_id": "arvo:1", "poc_id": "poc-1", "poc_hash": "a" * 64}
    executor = CyberGymExecutor(
        dataclasses_replace(
            config,
            http_runner=lambda *args, **kwargs: {"pocs": {"items": [record]}},
        )
    )
    assert executor._private_query("agent-" + "a" * 24, "arvo:1") == [record]


def test_submit_response_binds_poc_id_not_nonexistent_hash_and_keeps_exit_code(tmp_path):
    config = _config(tmp_path)
    task_dir = config.run_root / "task"
    task_dir.mkdir()
    (task_dir / "final.poc").write_bytes(b"poc-bytes")
    (task_dir / "submit.sh").write_text("TASK_ID=opaque1234\n", encoding="utf-8")
    executor_name = "workspace"
    executor_id = "c" * 64

    submit_calls = []

    def command(argv, *, cwd=None, env=None, timeout=None):
        submit_calls.append(list(argv))
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

    executor = CyberGymExecutor(dataclasses_replace(config, command_runner=command))
    executor._task_containers[executor_name] = executor_id
    response, digest, masked = executor._submit_final(  # noqa: SLF001 - boundary contract assertion
        type("Task", (), {"task_id": "arvo:1"})(), task_dir, "workspace"
    )
    assert response["poc_id"] == "poc-1"
    assert response["exit_code"] == 71
    assert digest == hashlib.sha256(b"poc-bytes").hexdigest()
    assert masked == "opaque1234"
    assert executor_id in submit_calls[0]
    assert executor_name not in submit_calls[0]


def test_unknown_gateway_attempt_blocks_campaign_cleanup(tmp_path):
    config = _config(tmp_path)
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        raise AssertionError("cleanup must not run while gateway custody is unknown")

    executor = CyberGymExecutor(dataclasses_replace(config, command_runner=command))
    executor.started = True
    executor.server_id = "server-123"
    executor.network_id = "network-123"
    executor._task_containers = {"workspace-agent-aaaaaaaaaaaaaaaaaaaaaaaa": "workspace-123"}
    executor._gateway_attempts = {
        "cybergym-attempt": {
            "gateway_task_id": "cybergym-attempt",
            "status": "admission_unknown",
            "checkpoint": str(config.run_root / "checkpoint.json"),
        }
    }

    report = executor.close()
    assert report["ok"] is False
    assert report["status"] == "custody_pending"
    assert executor.custody_blocked is True
    assert executor.server_id == "server-123"
    assert executor.network_id == "network-123"
    assert calls == []
    assert (config.run_root / "custody_pending.json").is_file()


def test_gateway_admission_transport_error_registers_durable_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    seen = {}

    def failing_http(*args, **kwargs):
        seen.update(kwargs)
        raise ExecutorFailure("HTTP POST transport failed")

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=failing_http))
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-opaque-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="transport failed"):
        executor._gateway_wait(body, checkpoint)
    assert "cybergym-opaque-attempt" in executor._gateway_attempts
    assert executor._gateway_attempts["cybergym-opaque-attempt"]["status"] == "admission_unknown"
    assert seen["headers"]["Idempotency-Key"].startswith("cybergym-")
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["custody_required"] is True
    assert saved["status"] == "admission_unknown"


def test_gateway_definitive_admission_rejection_releases_phantom_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    executor = CyberGymExecutor(
        dataclasses_replace(
            config,
            http_runner=lambda *args, **kwargs: {
                "status_code": 400,
                "body": {"detail": "invalid task"},
            },
        )
    )
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-rejected-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="HTTP 400"):
        executor._gateway_wait(body, checkpoint)
    assert executor._gateway_attempts == {}
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "admission_rejected"
    assert saved["custody_required"] is False


def test_gateway_malformed_admission_keeps_unknown_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=lambda *args, **kwargs: {})
    )
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-malformed-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="no task id"):
        executor._gateway_wait(body, checkpoint)
    assert "cybergym-malformed-attempt" in executor._gateway_attempts
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "admission_unknown_response"
    assert saved["custody_required"] is True


def test_gateway_waits_for_final_cost_after_completed_status(tmp_path):
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=10)
    task_id = "cybergym-cost-pending"
    calls = []
    status_rows = iter(
        (
            {"task_id": task_id, "status": "completed", "cost_final": False},
            {"task_id": task_id, "status": "completed", "cost_final": True},
        )
    )

    def http(method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        return next(status_rows)

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    result = executor._gateway_wait(
        {"task_id": task_id, "description": "test"},
        config.run_root / "checkpoint.json",
    )

    assert result["cost_final"] is True
    assert calls == ["POST", "GET", "GET"]


def dataclasses_replace(config, **changes):
    import dataclasses

    return dataclasses.replace(config, **changes)
