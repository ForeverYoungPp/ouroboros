"""Adapter-owned CyberGym sidecar contracts.

The launcher owns execution and waiting.  This module is intentionally pure:
it validates the daemon/network boundary, builds Docker argv, and checks
sanitised observations supplied by an injected runner.  There is no Docker
SDK or network client import, so these rules remain testable on CI hosts that
do not have Docker installed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

SCHEMA_VERSION = "ouroboros.benchmark.cybergym.sidecar_attestation.v1"
NETWORK_NAME = "cybergym-internal"
DOCKER_HOST_ENV = "DOCKER_HOST"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_SOCKET_TARGET = "/var/run/docker.sock"
API_KEY_ENV = "CYBERGYM_API_KEY"
EXECUTOR_NETWORK_DECLARATION = "host"

_FORBIDDEN_NETWORKS = frozenset({"", "none", "host", "bridge", "default", "docker0"})
_WILDCARD_HOSTS = frozenset({"", "*", "0.0.0.0", "::", "::0", "[::]"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_DNS = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class SidecarConfigurationError(ValueError):
    """Raised when an input would weaken the adapter-owned boundary."""


class SidecarAttestationError(SidecarConfigurationError):
    """Raised by the strict attestation entry point."""

    def __init__(self, message: str, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)


def _text(value: Any, name: str, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise SidecarConfigurationError(f"{name} must be a non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or any(char.isspace() for char in value):
        raise SidecarConfigurationError(f"unsafe {name}")
    return value


def _safe_id(value: Any, name: str) -> str:
    value = _text(value, name)
    if value in {".", ".."} or not _SAFE_ID.fullmatch(value):
        raise SidecarConfigurationError(f"unsafe {name}")
    return value


def _safe_name(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _SAFE_NAME.fullmatch(value):
        raise SidecarConfigurationError(f"unsafe {name}")
    return value


def _safe_path(value: Any, name: str, *, absolute: bool = True) -> str:
    value = _text(value, name, max_len=4096)
    if "\x00" in value or "*" in value or "?" in value or (absolute and not value.startswith("/")):
        raise SidecarConfigurationError(f"unsafe {name}")
    if value == "/" or "/../" in f"/{value.strip('/')}/" or value.endswith("/.."):
        raise SidecarConfigurationError(f"unsafe {name}")
    return value


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise SidecarConfigurationError(f"{name} must be a TCP port")
    return value


def _dns_label(value: Any, name: str) -> str:
    value = _text(value, name, max_len=63).lower()
    if not _SAFE_DNS.fullmatch(value):
        raise SidecarConfigurationError(f"unsafe DNS label for {name}")
    return value


def _loopback(value: Any, name: str = "bind_host") -> str:
    value = _text(value, name)
    if value in _WILDCARD_HOSTS:
        raise SidecarConfigurationError(f"{name} must not be wildcard")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SidecarConfigurationError(f"{name} must be numeric loopback") from exc
    if not address.is_loopback:
        raise SidecarConfigurationError(f"{name} must be loopback")
    return value


def _recognised_rootless_socket(path: str) -> bool:
    if path in {"/var/run/docker.sock", "/run/docker.sock", "/docker.sock"}:
        return False
    if re.fullmatch(r"/run/user/[0-9]+/(?:docker|docker-rootless)(?:-[A-Za-z0-9_.-]+)?\.sock", path):
        return True
    return "/rootless/" in path or (path.startswith("/mnt/data/") and "/docker" in path)


@dataclass(frozen=True)
class DockerHostRef:
    """Explicit rootless daemon endpoint, distinct from Docker network mode."""

    value: str
    socket_path: str
    allow_custom: bool = False

    def __post_init__(self) -> None:
        value = _text(self.value, "docker_host", max_len=4096)
        path = _safe_path(self.socket_path, "docker_socket")
        if value != f"unix://{path}" or (not self.allow_custom and not _recognised_rootless_socket(path)):
            raise SidecarConfigurationError("docker_host must be a recognised rootless unix socket")

    @property
    def env(self) -> Mapping[str, str]:
        return {DOCKER_HOST_ENV: self.value}

    def __str__(self) -> str:
        return self.value


def resolve_rootless_docker_host(
    value: str | DockerHostRef | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    allow_custom: bool = False,
) -> DockerHostRef:
    """Resolve explicit ``DOCKER_HOST``; reject TCP, rootful and default endpoints."""

    if isinstance(value, DockerHostRef):
        return value
    source = os.environ if environ is None else environ
    raw = value if value is not None else source.get(DOCKER_HOST_ENV)
    if not isinstance(raw, str) or not raw:
        raise SidecarConfigurationError("DOCKER_HOST must be explicitly set")
    if any(char.isspace() for char in raw) or any(char in raw for char in "*?\x00"):
        raise SidecarConfigurationError("unsafe DOCKER_HOST")
    if raw.startswith("/"):
        path, canonical = raw, f"unix://{raw}"
    else:
        parsed = urlsplit(raw)
        if parsed.scheme != "unix" or parsed.netloc or parsed.query or parsed.fragment:
            raise SidecarConfigurationError("DOCKER_HOST must be a unix socket")
        path, canonical = parsed.path, f"unix://{parsed.path}"
    path = _safe_path(path, "docker_socket")
    if path in {"/var/run/docker.sock", "/run/docker.sock", "/docker.sock"}:
        raise SidecarConfigurationError("the shared/rootful Docker socket is forbidden")
    if not allow_custom and not _recognised_rootless_socket(path):
        raise SidecarConfigurationError("DOCKER_HOST is not recognisably rootless")
    return DockerHostRef(canonical, path, allow_custom=allow_custom)


def require_explicit_rootless_docker_host(environ: Mapping[str, str] | None = None) -> DockerHostRef:
    return resolve_rootless_docker_host(environ=environ)


def docker_host_environment(host: str | DockerHostRef) -> dict[str, str]:
    """Environment shared by launcher/sidecar callbacks for daemon selection."""

    return dict(resolve_rootless_docker_host(host).env)


def make_dns_alias(campaign_id: str, role: str, task_id: str = "") -> str:
    campaign = _safe_id(campaign_id, "campaign_id")
    role = _safe_id(role, "role").lower()
    task = _safe_id(task_id, "task_id") if task_id else "campaign"
    digest = hashlib.sha256(f"{campaign}\0{role}\0{task}".encode()).hexdigest()[:10]
    def slug(item: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-") or "item"
    prefix = f"cybergym-{slug(role)}-{slug(campaign)}-{slug(task)}"
    return _dns_label((prefix[:52].rstrip("-") + "-" + digest)[:63], "dns_alias")


@dataclass(frozen=True)
class NetworkPlan:
    campaign_id: str
    task_id: str
    server_port: int
    verifier_host_port: int
    network_name: str = NETWORK_NAME
    server_alias: str = ""
    workspace_alias: str = ""
    server_container_port: int | None = None
    verifier_bind_host: str = LOOPBACK_HOST

    def __post_init__(self) -> None:
        campaign = _safe_id(self.campaign_id, "campaign_id")
        task = _safe_id(self.task_id, "task_id")
        if self.network_name != NETWORK_NAME:
            raise SidecarConfigurationError(f"network must be {NETWORK_NAME}")
        _port(self.server_port, "server_port")
        _port(self.verifier_host_port, "verifier_host_port")
        container_port = self.server_port if self.server_container_port is None else self.server_container_port
        _port(container_port, "server_container_port")
        object.__setattr__(self, "campaign_id", campaign)
        object.__setattr__(self, "task_id", task)
        object.__setattr__(self, "server_container_port", container_port)
        object.__setattr__(self, "verifier_bind_host", _loopback(self.verifier_bind_host, "verifier_bind_host"))
        server_alias = self.server_alias or make_dns_alias(campaign, "server")
        workspace_alias = self.workspace_alias or make_dns_alias(campaign, "workspace", task)
        object.__setattr__(self, "server_alias", _dns_label(server_alias, "server_alias"))
        object.__setattr__(self, "workspace_alias", _dns_label(workspace_alias, "workspace_alias"))
        if self.server_alias == self.workspace_alias:
            raise SidecarConfigurationError("server and workspace aliases must differ")

    @property
    def server_url(self) -> str:
        return f"http://{self.server_alias}:{self.server_port}"

    @property
    def verifier_url(self) -> str:
        return f"http://{self.verifier_bind_host}:{self.verifier_host_port}"

    @property
    def no_proxy(self) -> str:
        return build_no_proxy(self.server_alias, self.server_port)


def build_network_plan(
    campaign_id: str,
    task_id: str,
    server_port: int,
    verifier_host_port: int,
    *,
    server_alias: str | None = None,
    workspace_alias: str | None = None,
    verifier_bind_host: str = LOOPBACK_HOST,
    server_container_port: int | None = None,
) -> NetworkPlan:
    return NetworkPlan(
        campaign_id,
        task_id,
        server_port,
        verifier_host_port,
        server_alias=server_alias or "",
        workspace_alias=workspace_alias or "",
        verifier_bind_host=verifier_bind_host,
        server_container_port=server_container_port,
    )


def _no_proxy_token(value: str) -> str:
    value = _text(value, "NO_PROXY entry", max_len=256)
    if value in _WILDCARD_HOSTS or value.startswith("*") or "://" in value or "=" in value or "," in value:
        raise SidecarConfigurationError("wildcard/invalid NO_PROXY entry")
    return value


def build_no_proxy(alias: str, port: int | None = None, *, existing: Iterable[str] | str = ()) -> str:
    alias = _dns_label(alias, "server_alias")
    if port is not None:
        _port(port, "server_port")
    old = existing.split(",") if isinstance(existing, str) else list(existing)
    values: list[str] = []
    for item in [*old, alias, f"{alias}:{port}" if port is not None else None]:
        if item is not None and item != "":
            item = _no_proxy_token(item)
            if item not in values:
                values.append(item)
    return ",".join(values)


def build_private_route(plan: NetworkPlan, endpoint: str, *, audience: str) -> str:
    endpoint = _text(endpoint, "endpoint", max_len=512)
    if not endpoint.startswith("/") or "//" in endpoint or ".." in endpoint:
        raise SidecarConfigurationError("endpoint must be a relative absolute path")
    audience = _safe_id(audience, "audience").lower()
    protected = {"/query-poc", "/submit-fix", "/fix", "/protected/query", "/protected/fix"}
    if audience == "agent":
        endpoint_value = endpoint.rstrip("/")
        if any(endpoint_value == item or endpoint_value.startswith(item + "/") for item in protected):
            raise SidecarConfigurationError("agent cannot receive protected verifier route")
        return f"{plan.server_url}{endpoint}"
    if audience in {"verifier", "host-verifier", "sidecar-verifier"}:
        return f"{plan.verifier_url}{endpoint}"
    raise SidecarConfigurationError("unknown route audience")


def build_connectivity_probe_plan(plan: NetworkPlan) -> tuple[dict[str, Any], ...]:
    return (
        {"name": "agent_to_server", "target": f"{plan.server_url}/health", "expected_reachable": True},
        {"name": "verifier_to_private", "target": f"{plan.verifier_url}/query-poc", "expected_reachable": True},
        {"name": "agent_to_public", "target": "https://example.com/", "expected_reachable": False},
        {"name": "agent_to_verifier", "target": f"{plan.verifier_url}/query-poc", "expected_reachable": False},
        {"name": "agent_socket_visible", "target": "unix://docker.sock", "expected_visible": False},
    )


_CONNECTIVITY_EXPECTATIONS = {
    "agent_to_server": True,
    "verifier_to_private": True,
    "agent_to_public": False,
    "agent_to_verifier": False,
    "agent_socket_visible": False,
}


def _probe_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("reachable", "visible", "ok", "success", "passed"):
            if isinstance(value.get(key), bool):
                return value[key]
    return None


def evaluate_connectivity_checks(observed: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate mandatory positive/negative facts; absent facts fail closed."""

    checks: dict[str, Any] = {}
    failed: list[str] = []
    for name, expected in _CONNECTIVITY_EXPECTATIONS.items():
        value = _probe_value(observed.get(name))
        passed = value is expected
        checks[name] = {"observed": value, "expected": expected, "pass": passed}
        if not passed:
            failed.append(name)
    return {"schema": f"{SCHEMA_VERSION}.connectivity", "ok": not failed, "checks": checks, "failed": failed}


def _validate_image(value: str) -> str:
    value = _text(value, "image", max_len=1024)
    if value.lower() in {"latest", "placeholder", "changeme", "example", "example/image"} or "${" in value:
        raise SidecarConfigurationError("placeholder image reference")
    if any(char in value for char in ";,\n\r"):
        raise SidecarConfigurationError("unsafe image reference")
    return value


def _digest(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
        raise SidecarConfigurationError("image digest must be sha256:<64 hex>")
    return value


def _env_name(value: str) -> str:
    value = _text(value, "environment name")
    if not _ENV_NAME.fullmatch(value):
        raise SidecarConfigurationError("unsafe environment name")
    return value


def _mount_path(value: str, name: str) -> str:
    value = _safe_path(value, name)
    if "," in value:
        raise SidecarConfigurationError(f"{name} cannot contain comma")
    return value


def _labels(plan: NetworkPlan, role: str, custom: Mapping[str, str] | None = None) -> dict[str, str]:
    role = _safe_id(role, "role").lower()
    if role not in {"server", "workspace"}:
        raise SidecarConfigurationError("unsupported sidecar role")
    result = {
        "com.ouroboros.benchmark": "cybergym",
        "com.ouroboros.run": plan.campaign_id,
        "com.ouroboros.campaign": plan.campaign_id,
        "com.ouroboros.task": plan.task_id,
        "com.ouroboros.role": role,
        "com.ouroboros.network": plan.network_name,
        "com.ouroboros.owner": "ouroboros",
    }
    for key, value in (custom or {}).items():
        key, value = _text(key, "label key", max_len=256), _text(value, "label value", max_len=512)
        if key in result and result[key] != value:
            raise SidecarConfigurationError(f"custom label attempts to override {key}")
        result[key] = value
    return result


def required_resource_labels(plan: NetworkPlan, role: str) -> dict[str, str]:
    return _labels(plan, role)


def _append_labels(argv: list[str], labels: Mapping[str, str]) -> None:
    for key in sorted(labels):
        argv.extend(("--label", f"{key}={labels[key]}"))


def _host(value: str | DockerHostRef) -> DockerHostRef:
    return resolve_rootless_docker_host(value)


@dataclass(frozen=True)
class SidecarCommandSpec:
    docker_host: str | DockerHostRef
    plan: NetworkPlan
    image: str
    container_name: str
    command: tuple[str, ...] = ()
    api_key_env: str = API_KEY_ENV
    socket_target: str = DEFAULT_SOCKET_TARGET
    data_host_path: str | None = None
    data_container_path: str = "/cybergym-data"
    image_digest: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    container_docker_host: str | None = None
    platform: str = "linux/amd64"

    def __post_init__(self) -> None:
        object.__setattr__(self, "docker_host", _host(self.docker_host))
        _validate_image(self.image)
        _safe_name(self.container_name, "container_name")
        _env_name(self.api_key_env)
        _mount_path(self.socket_target, "socket_target")
        if self.data_host_path is not None:
            _mount_path(self.data_host_path, "data_host_path")
            _safe_path(self.data_container_path, "data_container_path")
        _digest(self.image_digest)
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.platform):
            raise SidecarConfigurationError("unsafe platform")
        for item in self.command:
            _text(item, "command argument", max_len=4096)
        for key, value in self.extra_env.items():
            _env_name(key)
            _text(value, f"environment value for {key}", max_len=4096)
        _labels(self.plan, "server", self.labels)


@dataclass(frozen=True)
class WorkspaceCommandSpec:
    docker_host: str | DockerHostRef
    plan: NetworkPlan
    image: str
    container_name: str
    workspace_host_path: str
    command: tuple[str, ...] = ()
    workspace_container_path: str = "/workspace"
    labels: Mapping[str, str] = field(default_factory=dict)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    container_docker_host: str | None = None
    platform: str = "linux/amd64"

    def __post_init__(self) -> None:
        object.__setattr__(self, "docker_host", _host(self.docker_host))
        _validate_image(self.image)
        _safe_id(self.container_name, "container_name")
        _mount_path(self.workspace_host_path, "workspace_host_path")
        _mount_path(self.workspace_container_path, "workspace_container_path")
        if self.workspace_container_path == "/":
            raise SidecarConfigurationError("workspace mount cannot target root")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.platform):
            raise SidecarConfigurationError("unsafe platform")
        for item in self.command:
            _text(item, "command argument", max_len=4096)
        for key, value in self.extra_env.items():
            _env_name(key)
            _text(value, f"environment value for {key}", max_len=4096)
        _labels(self.plan, "workspace", self.labels)


def _mount_arg(source: str, destination: str) -> str:
    return f"type=bind,src={source},dst={destination}"


def build_network_create_argv(
    docker_host: str | DockerHostRef,
    plan: NetworkPlan,
    *,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    host = _host(docker_host)
    base = {
        "com.ouroboros.benchmark": "cybergym",
        "com.ouroboros.run": plan.campaign_id,
        "com.ouroboros.campaign": plan.campaign_id,
        "com.ouroboros.network": plan.network_name,
    }
    for key, value in (labels or {}).items():
        key, value = _text(key, "label key", max_len=256), _text(value, "label value", max_len=512)
        if key in base and base[key] != value:
            raise SidecarConfigurationError(f"custom label attempts to override {key}")
        base[key] = value
    argv = ["docker", "--host", host.value, "network", "create", "--driver", "bridge", "--internal"]
    _append_labels(argv, base)
    argv.append(plan.network_name)
    return argv


def build_sidecar_argv(spec: SidecarCommandSpec) -> list[str]:
    """Build server argv; API key is inherited by env name and never serialized."""

    plan = spec.plan
    argv = [
        "docker", "--host", spec.docker_host.value, "run", "--detach", "--init", "--name", spec.container_name,
        "--platform", spec.platform, "--network", plan.network_name, "--network-alias", plan.server_alias,
    ]
    _append_labels(argv, _labels(plan, "server", spec.labels))
    argv.extend((
        "--publish", f"{plan.verifier_bind_host}:{plan.verifier_host_port}:{plan.server_container_port}/tcp",
        "--mount", _mount_arg(spec.docker_host.socket_path, spec.socket_target),
        "--env", spec.api_key_env,
    ))
    if spec.container_docker_host:
        argv.extend(("--env", f"{DOCKER_HOST_ENV}={_text(spec.container_docker_host, 'container_docker_host')}"))
    for key in sorted(spec.extra_env):
        argv.extend(("--env", f"{key}={spec.extra_env[key]}"))
    if spec.data_host_path is not None:
        argv.extend(("--mount", _mount_arg(spec.data_host_path, spec.data_container_path)))
    argv.append(spec.image)
    argv.extend(spec.command)
    return argv


def build_workspace_argv(spec: WorkspaceCommandSpec) -> list[str]:
    """Build workspace argv; no Docker socket is mounted into the agent."""

    plan = spec.plan
    no_proxy = plan.no_proxy
    argv = [
        "docker", "--host", spec.docker_host.value, "run", "--detach", "--init", "--name", spec.container_name,
        "--platform", spec.platform, "--network", plan.network_name, "--network-alias", plan.workspace_alias,
    ]
    _append_labels(argv, _labels(plan, "workspace", spec.labels))
    argv.extend((
        "--mount", _mount_arg(spec.workspace_host_path, spec.workspace_container_path),
        "--env", f"CYBERGYM_SERVER_URL={plan.server_url}",
        "--env", f"NO_PROXY={no_proxy}", "--env", f"no_proxy={no_proxy}",
        "--env", f"CYBERGYM_TASK_ID={plan.task_id}",
    ))
    if spec.container_docker_host:
        argv.extend(("--env", f"{DOCKER_HOST_ENV}={_text(spec.container_docker_host, 'container_docker_host')}"))
    for key in sorted(spec.extra_env):
        argv.extend(("--env", f"{key}={spec.extra_env[key]}"))
    argv.append(spec.image)
    argv.extend(spec.command)
    return argv


def api_key_attestation(value: Any) -> dict[str, Any]:
    """Record only presence, placeholder status and a short non-reversible fingerprint."""

    if isinstance(value, Mapping):
        present = value.get("present") is True
        placeholder = value.get("placeholder") is True
        fingerprint = value.get("fingerprint") if isinstance(value.get("fingerprint"), str) else None
        if fingerprint and not re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
            fingerprint = None
        return {"present": present, "placeholder": placeholder, "fingerprint": fingerprint}
    if not isinstance(value, str) or not value:
        return {"present": False, "placeholder": False, "fingerprint": None}
    lowered = value.strip().lower()
    placeholders = {
        "placeholder", "changeme", "change-me", "your_api_key", "your-api-key", "api_key", "test-key", "test_key",
        "none", "null", "<api-key>", "${cybergym_api_key}", "cybergym_api_key", "your-cybergym-api-key",
    }
    placeholder = lowered in placeholders or lowered.startswith(("replace_me", "replace-me"))
    return {"present": True, "placeholder": placeholder, "fingerprint": hashlib.sha256(value.encode()).hexdigest()[:16]}


def is_placeholder_api_key(value: Any) -> bool:
    return api_key_attestation(value)["placeholder"] is True


def require_api_key(value: Any) -> dict[str, Any]:
    status = api_key_attestation(value)
    if not status["present"] or status["placeholder"]:
        raise SidecarConfigurationError("CyberGym API key is missing or a placeholder")
    return status


@dataclass(frozen=True)
class SidecarExpectation:
    plan: NetworkPlan
    docker_host: str | DockerHostRef
    server_container_name: str
    workspace_container_name: str
    server_container_id: str | None = None
    workspace_container_id: str | None = None
    network_id: str | None = None
    socket_path: str | None = None
    image_digest: str | None = None
    server_pid: int | None = None
    workspace_pid: int | None = None
    executor_network_declaration: str = EXECUTOR_NETWORK_DECLARATION

    def __post_init__(self) -> None:
        host = _host(self.docker_host)
        object.__setattr__(self, "docker_host", host)
        _safe_name(self.server_container_name, "server_container_name")
        _safe_name(self.workspace_container_name, "workspace_container_name")
        for value, name in ((self.server_container_id, "server_container_id"), (self.workspace_container_id, "workspace_container_id"), (self.network_id, "network_id")):
            if value is not None:
                _safe_id(value, name)
        if self.socket_path is not None:
            path = _safe_path(self.socket_path, "socket_path")
            if path != host.socket_path:
                raise SidecarConfigurationError("socket_path must equal selected rootless socket")
            object.__setattr__(self, "socket_path", path)
        _digest(self.image_digest)
        for value, name in ((self.server_pid, "server_pid"), (self.workspace_pid, "workspace_pid")):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise SidecarConfigurationError(f"{name} must be positive")
        if self.executor_network_declaration != EXECUTOR_NETWORK_DECLARATION:
            raise SidecarConfigurationError("executor declaration must be non-none host value")


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _name(observation: Mapping[str, Any]) -> str | None:
    value = observation.get("Name") or observation.get("name")
    return value.lstrip("/") if isinstance(value, str) and value.lstrip("/") else None


def _id(observation: Mapping[str, Any]) -> str | None:
    value = observation.get("Id") or observation.get("ID") or observation.get("id")
    return value if isinstance(value, str) and value else None


def _pid(observation: Mapping[str, Any]) -> int | None:
    value = _nested(observation, "State", "Pid")
    if value is None:
        value = observation.get("Pid") or observation.get("pid")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _running(observation: Mapping[str, Any]) -> bool:
    return _nested(observation, "State", "Running") is True or observation.get("running") is True


def _labels_from(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _nested(observation, "Config", "Labels")
    if not isinstance(value, Mapping):
        value = observation.get("Labels")
    return value if isinstance(value, Mapping) else {}


def _network_mode(observation: Mapping[str, Any]) -> str | None:
    value = _nested(observation, "HostConfig", "NetworkMode")
    if value is None:
        value = observation.get("NetworkMode") or observation.get("network_mode")
    return value if isinstance(value, str) else None


def _network(observation: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    values = _nested(observation, "NetworkSettings", "Networks")
    if not isinstance(values, Mapping):
        values = observation.get("Networks")
    value = values.get(name) if isinstance(values, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _mounts(observation: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    values = observation.get("Mounts")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for item in values:
            if isinstance(item, Mapping):
                source = item.get("Source") or item.get("source")
                destination = item.get("Destination") or item.get("destination") or item.get("Target")
                if isinstance(source, str) and isinstance(destination, str):
                    result.append((source, destination))
    values = _nested(observation, "HostConfig", "Binds")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for item in values:
            if isinstance(item, str) and ":" in item:
                source, destination = item.split(":", 1)[:2]
                result.append((source, destination))
    return result


def _bindings(observation: Mapping[str, Any], port: int) -> list[Mapping[str, Any]]:
    values = _nested(observation, "NetworkSettings", "Ports")
    if not isinstance(values, Mapping):
        values = observation.get("Ports")
    value = values.get(f"{port}/tcp") if isinstance(values, Mapping) else None
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _digests(observation: Mapping[str, Any]) -> set[str]:
    values = _nested(observation, "Config", "RepoDigests")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        values = observation.get("RepoDigests")
    result: set[str] = set()
    for item in values or ():
        if isinstance(item, str) and "@" in item:
            result.add(item.split("@", 1)[1])
    image = observation.get("Image")
    if isinstance(image, str) and _DIGEST.fullmatch(image):
        result.add(image)
    config_image = _nested(observation, "Config", "Image")
    if isinstance(config_image, str) and _DIGEST.fullmatch(config_image):
        result.add(config_image)
    return result


def _container_report(
    observation: Mapping[str, Any],
    expected: SidecarExpectation,
    role: str,
    expected_name: str,
    expected_id: str | None,
    expected_pid: int | None,
) -> tuple[dict[str, Any], list[str]]:
    plan = expected.plan
    failures: list[str] = []
    labels = _labels_from(observation)
    label_report: dict[str, Any] = {}
    for key, wanted in _labels(plan, role).items():
        actual = labels.get(key)
        passed = actual == wanted
        label_report[key] = {"observed": actual, "expected": wanted, "pass": passed}
        if not passed:
            failures.append(f"{role}.label.{key}")
    actual_name, actual_id, actual_pid = _name(observation), _id(observation), _pid(observation)
    if actual_name != expected_name:
        failures.append(f"{role}.name")
    if expected_id is not None and actual_id != expected_id:
        failures.append(f"{role}.id")
    if actual_pid is None or not _running(observation):
        failures.append(f"{role}.process")
    if expected_pid is not None and actual_pid != expected_pid:
        failures.append(f"{role}.pid")
    mode = _network_mode(observation)
    if mode != plan.network_name or mode in _FORBIDDEN_NETWORKS:
        failures.append(f"{role}.network_mode")
    network = _network(observation, plan.network_name)
    all_networks = _nested(observation, "NetworkSettings", "Networks")
    aliases = network.get("Aliases", ()) if network else ()
    expected_alias = plan.server_alias if role == "server" else plan.workspace_alias
    if not network or not isinstance(aliases, Sequence) or expected_alias not in aliases:
        failures.append(f"{role}.network_membership")
    if isinstance(all_networks, Mapping) and any(str(name).lower() in _FORBIDDEN_NETWORKS for name in all_networks):
        failures.append(f"{role}.forbidden_network_attachment")
    observed_network_id = network.get("NetworkID") if network else None
    if expected.network_id is not None and observed_network_id != expected.network_id:
        failures.append(f"{role}.network_id")
    digest_ok = expected.image_digest is None or expected.image_digest in _digests(observation)
    if not digest_ok:
        failures.append(f"{role}.image_digest")
    return {
        "name": actual_name, "id": actual_id, "pid": actual_pid, "running": _running(observation),
        "network_mode": mode, "network_name": plan.network_name if network else None,
        "network_id": observed_network_id,
        "aliases": list(aliases) if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)) else [],
        "labels": label_report, "image_digests": sorted(_digests(observation)), "image_digest_ok": digest_ok,
    }, failures


def _socket_report(server: Mapping[str, Any], workspace: Mapping[str, Any], expected: SidecarExpectation) -> tuple[dict[str, Any], list[str]]:
    source = expected.socket_path or expected.docker_host.socket_path
    server_mounts, workspace_mounts = _mounts(server), _mounts(workspace)
    server_ok = any(src == source and dst == DEFAULT_SOCKET_TARGET for src, dst in server_mounts)
    workspace_visible = any(src == source or dst == DEFAULT_SOCKET_TARGET or "docker.sock" in dst for src, dst in workspace_mounts)
    failures = ([] if server_ok else ["server.socket_mount"]) + (["workspace.socket_visibility"] if workspace_visible else [])
    return {
        "socket_path": source, "server_mount": server_ok, "workspace_visible": workspace_visible,
        "server_mounts": [{"source": src, "destination": dst} for src, dst in server_mounts],
        "workspace_mounts": [{"source": src, "destination": dst} for src, dst in workspace_mounts],
    }, failures


def _publish_report(server: Mapping[str, Any], plan: NetworkPlan) -> tuple[dict[str, Any], list[str]]:
    bindings = _bindings(server, int(plan.server_container_port))
    host_ip = bindings[0].get("HostIp") if len(bindings) == 1 else None
    host_port = bindings[0].get("HostPort") if len(bindings) == 1 else None
    try:
        host_port = int(host_port)
    except (TypeError, ValueError):
        host_port = None
    ok = len(bindings) == 1 and host_ip == plan.verifier_bind_host and host_port == plan.verifier_host_port
    return {"host_ip": host_ip, "host_port": host_port, "container_port": plan.server_container_port, "loopback_only": ok}, ([] if ok else ["server.loopback_publish"])


def check_sidecar_attestation(
    observation: Mapping[str, Any],
    expected: SidecarExpectation,
    *,
    api_key: Any = None,
    connectivity: Mapping[str, Any] | None = None,
    require_connectivity: bool = True,
) -> dict[str, Any]:
    """Return a complete secret-free report; unknown custody facts fail closed."""

    server, workspace = observation.get("server"), observation.get("workspace")
    if not isinstance(server, Mapping) or not isinstance(workspace, Mapping):
        return {"schema": SCHEMA_VERSION, "ok": False, "failed_checks": ["server_or_workspace_observation"]}
    failures: list[str] = []
    observed_host = observation.get("docker_host")
    if observed_host is None:
        failures.append("docker_host_unknown")
    else:
        try:
            observed_host_ref = _host(observed_host)
        except SidecarConfigurationError:
            observed_host_ref = None
        if observed_host_ref is None or observed_host_ref.value != expected.docker_host.value:
            failures.append("docker_host_mismatch")
    network_observation = observation.get("network")
    network_report: dict[str, Any] = {"name": expected.plan.network_name, "id": expected.network_id, "internal": None}
    if isinstance(network_observation, Mapping):
        observed_network_name = network_observation.get("Name") or network_observation.get("name")
        observed_network_id = network_observation.get("Id") or network_observation.get("ID") or network_observation.get("id")
        internal = network_observation.get("Internal")
        network_report = {"name": observed_network_name, "id": observed_network_id, "internal": internal}
        if observed_network_name != expected.plan.network_name:
            failures.append("network.name")
        if expected.network_id is not None and observed_network_id != expected.network_id:
            failures.append("network.id")
        if internal is not True:
            failures.append("network.internal")
    server_report, more = _container_report(
        server, expected, "server", expected.server_container_name, expected.server_container_id, expected.server_pid
    )
    failures.extend(more)
    workspace_report, more = _container_report(
        workspace, expected, "workspace", expected.workspace_container_name, expected.workspace_container_id, expected.workspace_pid
    )
    failures.extend(more)
    socket_report, more = _socket_report(server, workspace, expected)
    failures.extend(more)
    publish_report, more = _publish_report(server, expected.plan)
    failures.extend(more)
    key_report = api_key_attestation(api_key)
    if api_key is None:
        key_report = {"present": None, "placeholder": None, "fingerprint": None}
        failures.append("api_key_unknown")
    elif not key_report["present"] or key_report["placeholder"]:
        failures.append("api_key")
    declaration = observation.get("executor_network", expected.executor_network_declaration)
    if declaration != EXECUTOR_NETWORK_DECLARATION:
        failures.append("executor_network_declaration")
    if connectivity is None:
        connectivity_report: Mapping[str, Any] = {"schema": f"{SCHEMA_VERSION}.connectivity", "ok": False, "status": "not_provided"}
        if require_connectivity:
            failures.append("connectivity_unknown")
    else:
        connectivity_report = evaluate_connectivity_checks(connectivity)
        if not connectivity_report.get("ok"):
            failures.append("connectivity")
    network_id = _nested(server, "NetworkSettings", "Networks", expected.plan.network_name, "NetworkID")
    if expected.network_id is None and not network_id:
        failures.append("network.id_unknown")
    if network_report["id"] is None:
        network_report["id"] = network_id or expected.network_id
    return {
        "schema": SCHEMA_VERSION, "ok": not failures, "failed_checks": sorted(set(failures)),
        "docker_host": {"value": expected.docker_host.value, "socket_path": expected.docker_host.socket_path, "rootless": True},
        "network": network_report,
        "server": server_report, "workspace": workspace_report, "socket": socket_report,
        "published_verifier": publish_report, "api_key": key_report,
        "executor_network_declaration": declaration, "executor_network_is_docker_host": False,
        "connectivity": dict(connectivity_report), "cleanup": {"status": "pending", "owned_only": True},
    }


def attest_sidecar_runtime(observation: Mapping[str, Any], expected: SidecarExpectation, **kwargs: Any) -> dict[str, Any]:
    report = check_sidecar_attestation(observation, expected, **kwargs)
    if not report.get("ok"):
        raise SidecarAttestationError("CyberGym sidecar attestation failed", report)
    return report


validate_sidecar_attestation = check_sidecar_attestation


@dataclass(frozen=True)
class ProcessCustody:
    role: str
    pid: int
    container_id: str
    command_digest: str | None = None
    cleanup_status: str = "pending"
    cwd: str | None = None
    port: int | None = None

    def __post_init__(self) -> None:
        _safe_id(self.role, "process_role")
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise SidecarConfigurationError("process pid must be positive")
        _safe_id(self.container_id, "container_id")
        _digest(self.command_digest)
        if self.cwd is not None:
            _safe_path(self.cwd, "process_cwd")
        if self.port is not None:
            _port(self.port, "process_port")
        if self.cleanup_status not in {"pending", "removed", "verified", "failed"}:
            raise SidecarConfigurationError("invalid cleanup status")


def build_process_custody(
    role: str,
    pid: int,
    container_id: str,
    *,
    command: Sequence[str] | None = None,
    cwd: str | None = None,
    port: int | None = None,
) -> ProcessCustody:
    digest = None
    if command is not None:
        values = tuple(_text(item, "command argument", max_len=4096) for item in command)
        digest = "sha256:" + hashlib.sha256("\0".join(values).encode()).hexdigest()
    return ProcessCustody(role, pid, container_id, digest, cwd=cwd, port=port)


def attest_process_custody(observed: Mapping[str, Any], expected: ProcessCustody) -> dict[str, Any]:
    try:
        pid = int(observed.get("pid"))
    except (TypeError, ValueError):
        pid = None
    container_id = observed.get("container_id") or observed.get("Id")
    cwd = observed.get("cwd")
    port = observed.get("port")
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        port = None
    checks = {
        "pid": pid == expected.pid,
        "container_id": container_id == expected.container_id,
        "cwd": expected.cwd is None or cwd == expected.cwd,
        "port": expected.port is None or port == expected.port,
    }
    return {
        "role": expected.role,
        "pid": pid,
        "container_id": container_id,
        "cwd": cwd,
        "port": port,
        "expected_pid": expected.pid,
        "expected_container_id": expected.container_id,
        "expected_cwd": expected.cwd,
        "expected_port": expected.port,
        "checks": checks,
        "ok": all(checks.values()),
        "cleanup_status": expected.cleanup_status,
    }


def _owned_target(value: str, name: str) -> str:
    value = _safe_id(value, name)
    if value.lower() in {"all", "none", "default", "*"}:
        raise SidecarConfigurationError(f"unsafe cleanup target {name}")
    return value


@dataclass(frozen=True)
class CleanupPlan:
    docker_host: str | DockerHostRef
    campaign_id: str
    network_name: str = NETWORK_NAME
    server_container_id: str | None = None
    workspace_container_ids: tuple[str, ...] = ()
    network_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "docker_host", _host(self.docker_host))
        _safe_id(self.campaign_id, "campaign_id")
        if self.network_name != NETWORK_NAME:
            raise SidecarConfigurationError("cleanup network must be cybergym-internal")
        if self.server_container_id is None and not self.workspace_container_ids:
            raise SidecarConfigurationError("cleanup requires owned container ids")
        if self.server_container_id is not None:
            _owned_target(self.server_container_id, "server_container_id")
        for value in self.workspace_container_ids:
            _owned_target(value, "workspace_container_id")
        if self.network_id is not None:
            _owned_target(self.network_id, "network_id")


def build_cleanup_plan(
    expected: SidecarExpectation,
    *,
    workspace_container_ids: Iterable[str] = (),
    network_id: str | None = None,
) -> CleanupPlan:
    workspace_ids = tuple(workspace_container_ids)
    if not workspace_ids and expected.workspace_container_id is not None:
        workspace_ids = (expected.workspace_container_id,)
    return CleanupPlan(
        expected.docker_host,
        expected.plan.campaign_id,
        server_container_id=expected.server_container_id,
        workspace_container_ids=workspace_ids,
        network_id=network_id or expected.network_id,
    )


def cleanup_argv(plan: CleanupPlan) -> tuple[tuple[str, ...], ...]:
    """Emit only exact owned ids; never emits prune, broad names or wildcards."""

    targets = [*plan.workspace_container_ids]
    if plan.server_container_id is not None:
        targets.append(plan.server_container_id)
    commands: list[tuple[str, ...]] = []
    if targets:
        commands.append(("docker", "--host", plan.docker_host.value, "rm", "--force", *targets))
    commands.append(("docker", "--host", plan.docker_host.value, "network", "rm", plan.network_id or plan.network_name))
    return tuple(commands)


build_cleanup_commands = cleanup_argv


def validate_cleanup_observation(observation: Mapping[str, Any], plan: CleanupPlan) -> dict[str, Any]:
    removed = observation.get("removed_container_ids", ())
    removed_ids = {item for item in removed if isinstance(item, str)} if isinstance(removed, Iterable) else set()
    expected = set(plan.workspace_container_ids)
    if plan.server_container_id is not None:
        expected.add(plan.server_container_id)
    network_removed = observation.get("network_removed") is True
    ok = expected.issubset(removed_ids) and network_removed
    return {
        "schema": f"{SCHEMA_VERSION}.cleanup", "ok": ok, "expected_container_ids": sorted(expected),
        "removed_container_ids": sorted(removed_ids), "network": plan.network_id or plan.network_name,
        "network_removed": network_removed, "status": "verified" if ok else "failed",
    }


def build_lifecycle_commands(
    server: SidecarCommandSpec,
    workspace: WorkspaceCommandSpec,
    *,
    create_network: bool = True,
) -> tuple[tuple[str, ...], ...]:
    """Return start commands; execution and process waiting stay launcher-owned."""

    commands: list[tuple[str, ...]] = []
    if create_network:
        commands.append(tuple(build_network_create_argv(server.docker_host, server.plan)))
    commands.extend((tuple(build_sidecar_argv(server)), tuple(build_workspace_argv(workspace))))
    return tuple(commands)


class CommandRunner(Protocol):
    """Injected command seam; implementations own subprocess/process custody."""

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Any: ...


__all__ = [
    "API_KEY_ENV", "CleanupPlan", "CommandRunner", "DEFAULT_SOCKET_TARGET", "DOCKER_HOST_ENV", "DockerHostRef",
    "EXECUTOR_NETWORK_DECLARATION", "LOOPBACK_HOST", "NETWORK_NAME", "NetworkPlan", "ProcessCustody",
    "SCHEMA_VERSION", "SidecarAttestationError", "SidecarCommandSpec", "SidecarConfigurationError",
    "SidecarExpectation", "WorkspaceCommandSpec", "api_key_attestation", "attest_process_custody",
    "attest_sidecar_runtime", "build_cleanup_commands", "build_cleanup_plan", "build_connectivity_probe_plan",
    "build_network_create_argv", "build_network_plan", "build_no_proxy", "build_private_route", "build_process_custody",
    "build_lifecycle_commands",
    "build_server_sidecar_argv", "build_sidecar_argv", "build_task_workspace_argv", "build_workspace_argv", "check_sidecar_attestation",
    "cleanup_argv", "docker_host_environment", "evaluate_connectivity_checks", "is_placeholder_api_key", "make_dns_alias",
    "required_resource_labels", "require_api_key", "require_explicit_rootless_docker_host", "resolve_rootless_docker_host",
    "validate_cleanup_observation", "validate_sidecar_attestation",
]


# Keep one spelling for callers that prefer an assertion-shaped API.
build_task_workspace_argv = build_workspace_argv
build_server_sidecar_argv = build_sidecar_argv
