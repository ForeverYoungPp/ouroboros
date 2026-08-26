"""Docker-free contract tests for the CyberGym sidecar helpers."""

from __future__ import annotations

import importlib

import pytest

sidecar = importlib.import_module("devtools.benchmarks.cybergym.cybergym_sidecar")


def _plan():
    return sidecar.build_network_plan("camp-7", "task-42", 8080, 18080)


def _host():
    return sidecar.resolve_rootless_docker_host("unix:///run/user/1006/docker.sock")


def _observation(plan, host, *, wildcard=False, workspace_socket=False, mode=None):
    labels_server = sidecar.required_resource_labels(plan, "server")
    labels_workspace = sidecar.required_resource_labels(plan, "workspace")
    network = {
        "NetworkID": "net-123",
        "Aliases": [plan.server_alias],
    }
    workspace_network = {
        "NetworkID": "net-123",
        "Aliases": [plan.workspace_alias],
    }
    host_ip = "0.0.0.0" if wildcard else plan.verifier_bind_host
    server = {
        "Id": "server-123",
        "Name": "/cyber-server",
        "Config": {"Labels": labels_server, "RepoDigests": ["cyber/server@sha256:" + "a" * 64]},
        "State": {"Pid": 101, "Running": True},
        "HostConfig": {"NetworkMode": mode or plan.network_name},
        "NetworkSettings": {
            "Networks": {plan.network_name: network},
            "Ports": {f"{plan.server_container_port}/tcp": [{"HostIp": host_ip, "HostPort": str(plan.verifier_host_port)}]},
        },
        "Mounts": [{"Source": host.socket_path, "Destination": "/var/run/docker.sock"}],
    }
    workspace_mounts = []
    if workspace_socket:
        workspace_mounts.append({"Source": host.socket_path, "Destination": "/var/run/docker.sock"})
    workspace = {
        "Id": "workspace-123",
        "Name": "/cyber-workspace",
        "Config": {"Labels": labels_workspace},
        "State": {"Pid": 202, "Running": True},
        "HostConfig": {"NetworkMode": plan.network_name},
        "NetworkSettings": {"Networks": {plan.network_name: workspace_network}},
        "Mounts": workspace_mounts,
    }
    return {"docker_host": host.value, "server": server, "workspace": workspace, "executor_network": "host"}


def _connectivity():
    return {
        "agent_to_server": True,
        "verifier_to_private": {"reachable": True},
        "agent_to_public": False,
        "agent_to_verifier": False,
        "agent_socket_visible": False,
    }


def test_rootless_host_is_explicit_and_rootful_or_tcp_is_rejected():
    assert _host().socket_path == "/run/user/1006/docker.sock"
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.resolve_rootless_docker_host(environ={})
    for value in ("unix:///var/run/docker.sock", "tcp://127.0.0.1:2375", "unix:///tmp/default.sock"):
        with pytest.raises(sidecar.SidecarConfigurationError):
            sidecar.resolve_rootless_docker_host(value)
    assert sidecar.resolve_rootless_docker_host("unix:///tmp/owned.sock", allow_custom=True).socket_path == "/tmp/owned.sock"


def test_network_plan_aliases_and_no_proxy_are_deterministic():
    first, second = _plan(), sidecar.build_network_plan("camp-7", "task-42", 8080, 18080)
    assert first.server_alias == second.server_alias
    assert first.workspace_alias == second.workspace_alias
    assert first.network_name == "cybergym-internal"
    assert first.no_proxy == f"{first.server_alias},{first.server_alias}:8080"
    assert sidecar.build_no_proxy(first.server_alias, 8080, existing="localhost") == f"localhost,{first.server_alias},{first.server_alias}:8080"
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.build_no_proxy("*", 8080)


def test_network_argv_uses_internal_named_network_and_explicit_daemon():
    argv = sidecar.build_network_create_argv(_host(), _plan())
    assert argv[:7] == ["docker", "--host", _host().value, "network", "create", "--driver", "bridge"]
    assert "--internal" in argv
    assert argv[-1] == "cybergym-internal"
    assert "--network" not in argv


def test_server_and_workspace_argv_preserve_socket_boundary():
    plan, host = _plan(), _host()
    server = sidecar.SidecarCommandSpec(
        host,
        plan,
        "cyber/server@sha256:" + "a" * 64,
        "cyber-server",
        command=("python", "-m", "cybergym_server"),
    )
    workspace = sidecar.WorkspaceCommandSpec(host, plan, "cyber/worker:pin", "cyber-workspace", "/tmp/cyber-task")
    server_argv, workspace_argv = sidecar.build_sidecar_argv(server), sidecar.build_workspace_argv(workspace)
    assert ["--host", host.value] == server_argv[1:3] == workspace_argv[1:3]
    assert "--network" in server_argv and plan.network_name in server_argv
    assert "--publish" in server_argv and "127.0.0.1:18080:8080/tcp" in server_argv
    assert any(host.socket_path in item for item in server_argv)
    assert "CYBERGYM_API_KEY" in server_argv
    assert "--mount" in workspace_argv and host.socket_path not in workspace_argv
    assert f"CYBERGYM_SERVER_URL={plan.server_url}" in workspace_argv
    assert f"NO_PROXY={plan.no_proxy}" in workspace_argv
    assert all("real-secret" not in item for item in server_argv + workspace_argv)


def test_forbidden_network_modes_and_wildcard_bind_fail_closed():
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.NetworkPlan("camp", "task", 8080, 18080, network_name="bridge")
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.NetworkPlan("camp", "task", 8080, 18080, verifier_bind_host="0.0.0.0")
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.SidecarCommandSpec(_host(), _plan(), "latest", "server")


def test_executor_host_declaration_is_not_docker_host_networking():
    plan, host = _plan(), _host()
    expectation = sidecar.SidecarExpectation(plan, host, "cyber-server", "cyber-workspace", "server-123", "workspace-123", "net-123", host.socket_path, server_pid=101, workspace_pid=202)
    report = sidecar.check_sidecar_attestation(
        _observation(plan, host), expectation, api_key={"present": True, "placeholder": False}, connectivity=_connectivity()
    )
    assert report["ok"] is True
    assert report["executor_network_declaration"] == "host"
    assert report["executor_network_is_docker_host"] is False
    with pytest.raises(sidecar.SidecarAttestationError):
        sidecar.attest_sidecar_runtime({**_observation(plan, host), "executor_network": "none"}, expectation, api_key="valid-key", connectivity=_connectivity())


def test_connectivity_requires_all_positive_and_negative_facts():
    result = sidecar.evaluate_connectivity_checks(_connectivity())
    assert result["ok"] is True
    incomplete = dict(_connectivity())
    del incomplete["agent_to_public"]
    assert sidecar.evaluate_connectivity_checks(incomplete)["ok"] is False
    wrong = dict(_connectivity())
    wrong["agent_socket_visible"] = True
    assert "agent_socket_visible" in sidecar.evaluate_connectivity_checks(wrong)["failed"]


def test_attestation_rejects_socket_leak_wildcard_and_default_bridge():
    plan, host = _plan(), _host()
    expectation = sidecar.SidecarExpectation(plan, host, "cyber-server", "cyber-workspace", "server-123", "workspace-123", "net-123", host.socket_path, server_pid=101, workspace_pid=202)
    for observation in (
        _observation(plan, host, workspace_socket=True),
        _observation(plan, host, wildcard=True),
        _observation(plan, host, mode="bridge"),
    ):
        report = sidecar.check_sidecar_attestation(observation, expectation, api_key="valid-key", connectivity=_connectivity())
        assert report["ok"] is False


def test_cleanup_is_exact_and_never_broad():
    plan, host = _plan(), _host()
    expectation = sidecar.SidecarExpectation(plan, host, "cyber-server", "cyber-workspace", "server-123", "workspace-123", "net-123", host.socket_path)
    cleanup = sidecar.build_cleanup_plan(expectation)
    commands = sidecar.cleanup_argv(cleanup)
    assert commands[0] == ("docker", "--host", host.value, "rm", "--force", "workspace-123", "server-123")
    assert commands[1][-2:] == ("rm", "net-123")
    assert all("prune" not in item and "*" not in item for command in commands for item in command)
    assert sidecar.validate_cleanup_observation({"removed_container_ids": ["workspace-123", "server-123"], "network_removed": True}, cleanup)["ok"] is True


def test_api_key_status_never_returns_secret():
    status = sidecar.api_key_attestation("real-secret-value")
    assert status["present"] is True and status["placeholder"] is False
    assert "real-secret-value" not in repr(status)
    assert sidecar.is_placeholder_api_key("placeholder") is True
    with pytest.raises(sidecar.SidecarConfigurationError):
        sidecar.require_api_key("placeholder")


def test_process_custody_attests_pid_cwd_and_port_without_spawning():
    custody = sidecar.build_process_custody("server", 101, "server-123", command=("python", "-m", "server"), cwd="/tmp/cyber", port=18080)
    report = sidecar.attest_process_custody({"pid": 101, "container_id": "server-123", "cwd": "/tmp/cyber", "port": 18080}, custody)
    assert report["ok"] is True
    assert sidecar.attest_process_custody({"pid": 102, "container_id": "server-123"}, custody)["ok"] is False


def test_lifecycle_builder_is_pure_and_can_skip_existing_campaign_network():
    plan, host = _plan(), _host()
    server = sidecar.SidecarCommandSpec(host, plan, "cyber/server:pin", "cyber-server")
    workspace = sidecar.WorkspaceCommandSpec(host, plan, "cyber/worker:pin", "cyber-workspace", "/tmp/cyber-task")
    commands = sidecar.build_lifecycle_commands(server, workspace, create_network=False)
    assert len(commands) == 2
    assert all(command[0:3] == ("docker", "--host", host.value) for command in commands)
