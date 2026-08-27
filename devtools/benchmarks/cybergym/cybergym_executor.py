"""Concrete, adapter-owned CyberGym execution lifecycle.

The benchmark launcher keeps its pre-admission path dependency free.  This
module is loaded only after admission and provides the small amount of runtime
wiring that the pure protocol helpers cannot provide: a pinned CyberGym
generator, a rootless Docker sidecar/workspace pair, an Ouroboros task gateway,
and the official final PoC verification path.  All effects are behind injected
command and HTTP callables, which keeps the contract unit-testable without
Docker, the upstream package, or provider credentials on CI workers.

The executor deliberately does not implement scoring or a second scheduler.
The upstream server remains the source of truth for vulnerable/fixed exits;
``run_campaign`` owns the campaign budget and result rows.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from devtools.benchmarks.cybergym.cybergym_adapter import (
    DEFAULT_FINAL_POC_PATH,
    DEFAULT_LEVEL,
    DEFAULT_DISABLED_TOOLS,
    CyberGymIntegrationUnavailable,
    FinalPoc,
    TaskSpec,
    build_generate_task_argv,
    build_submit_argv,
    final_poc_record,
    safe_task_path,
    task_contract_metadata,
)
from devtools.benchmarks.cybergym.cybergym_sidecar import (
    API_KEY_ENV,
    EXECUTOR_NETWORK_DECLARATION,
    DockerHostRef,
    NetworkPlan,
    SidecarCommandSpec,
    WorkspaceCommandSpec,
    build_network_create_argv,
    build_sidecar_argv,
    build_workspace_argv,
    make_opaque_agent_id,
    resolve_rootless_docker_host,
)


_SETTLED = frozenset({"completed", "failed", "cancelled", "rejected_duplicate"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# The pinned upstream image ships a public fallback key.  Keep only a digest
# prefix here so the known value is never copied into source, logs, or a test
# fixture.  This prefix is a detector, not an authentication credential.
_DEFAULT_CYBERGYM_KEY_SHA256_PREFIX = "9605ed570966a4e0"


class ExecutorFailure(CyberGymIntegrationUnavailable):
    """A typed post-admission infrastructure failure."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """Small subprocess result accepted by the injected command runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: pathlib.Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


class HttpRunner(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 60,
    ) -> Mapping[str, Any]: ...


def run_command(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Run an argv list without a shell and return bounded text output."""

    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(124, stdout, stderr or "timeout")
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _json_command(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> Any:
    result = runner(argv, cwd=cwd, env=env, timeout=timeout)
    if result.returncode != 0:
        raise ExecutorFailure(f"command failed ({result.returncode}): {argv[0]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExecutorFailure(f"command returned invalid JSON: {argv[0]}") from exc


def urllib_json(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> Mapping[str, Any]:
    """Minimal JSON HTTP transport; response bodies are never logged."""

    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Do not include the response body: upstream errors can echo request
        # metadata and the caller may have supplied a private route.
        raise ExecutorFailure(f"HTTP {method.upper()} {urllib.parse.urlsplit(url).path} returned {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ExecutorFailure(f"HTTP {method.upper()} transport failed") from exc
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExecutorFailure(f"HTTP {method.upper()} returned non-JSON data") from exc
    if not isinstance(value, Mapping):
        raise ExecutorFailure("HTTP response must be a JSON object")
    return value


def _safe_abs(value: pathlib.Path | str, name: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser().resolve(strict=False)
    if not path.is_absolute() or path == pathlib.Path("/"):
        raise ExecutorFailure(f"{name} must be a non-root absolute path")
    return path


def _inside(path: pathlib.Path, root: pathlib.Path, name: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ExecutorFailure(f"{name} is outside its approved root") from exc


def _image_digest(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ExecutorFailure(f"{name} must be a resolved sha256 digest")
    return text


@dataclasses.dataclass(frozen=True)
class ExecutorConfig:
    """Immutable inputs for one campaign executor.

    ``server_root`` is mounted at the identical absolute path in the server
    container.  The upstream verifier launches nested containers through the
    mounted Docker socket and bind-mounts these paths, so translating it to a
    cosmetic ``/cybergym-data`` path would make every verification fail.
    """

    campaign_id: str
    source_root: pathlib.Path
    data_root: pathlib.Path
    mask_map: pathlib.Path
    run_root: pathlib.Path
    server_root: pathlib.Path
    server_image: str
    server_image_digest: str
    workspace_image: str
    workspace_image_digest: str
    ouroboros_url: str
    docker_host: str | DockerHostRef
    server_port: int = 8666
    verifier_host_port: int = 0
    task_timeout_sec: int = 14_400
    difficulty: str = DEFAULT_LEVEL
    api_key_env: str = API_KEY_ENV
    provider_key_env: str = "OPENROUTER_API_KEY"
    provider_url: str = "https://openrouter.ai/api/v1/chat/completions"
    provider_probe: bool = True
    binary_dir: pathlib.Path | None = None
    log_dir: pathlib.Path | None = None
    db_path: pathlib.Path | None = None
    python_executable: str = "python"
    command: tuple[str, ...] = ("tail", "-f", "/dev/null")
    disabled_tools: tuple[str, ...] = DEFAULT_DISABLED_TOOLS
    poll_interval_sec: float = 3.0
    command_runner: CommandRunner = run_command
    http_runner: HttpRunner = urllib_json
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.campaign_id):
            raise ExecutorFailure("campaign_id is unsafe")
        source = _safe_abs(self.source_root, "source_root")
        data = _safe_abs(self.data_root, "data_root")
        mask = _safe_abs(self.mask_map, "mask_map")
        run = _safe_abs(self.run_root, "run_root")
        server_root = _safe_abs(self.server_root, "server_root")
        # The mask map may be kept beside the dataset or in a separately
        # mounted immutable cache.  It must still be an explicit absolute file;
        # the server sidecar must see the same path through its identical-root
        # bind, so keep it below that approved root.
        if mask == pathlib.Path("/"):
            raise ExecutorFailure("mask_map cannot be the filesystem root")
        _inside(mask, server_root, "mask_map")
        if self.difficulty != DEFAULT_LEVEL:
            raise ExecutorFailure("only Level-1 CyberGym is supported")
        if self.task_timeout_sec <= 0 or self.task_timeout_sec != int(self.task_timeout_sec):
            raise ExecutorFailure("task_timeout_sec must be a positive integer")
        if not str(self.ouroboros_url).startswith(("http://", "https://")):
            raise ExecutorFailure("ouroboros_url must be an HTTP URL")
        if self.api_key_env != API_KEY_ENV:
            raise ExecutorFailure(f"api_key_env must be {API_KEY_ENV}")
        if not str(self.provider_key_env).strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.provider_key_env):
            raise ExecutorFailure("provider_key_env is unsafe")
        parsed_provider = urllib.parse.urlsplit(str(self.provider_url))
        if parsed_provider.scheme != "https" or not parsed_provider.netloc:
            raise ExecutorFailure("provider_url must be an HTTPS endpoint")
        if not self.server_port or not 1 <= int(self.server_port) <= 65535:
            raise ExecutorFailure("server_port must be a TCP port")
        try:
            host = resolve_rootless_docker_host(self.docker_host)
        except Exception as exc:
            # Keep the public executor boundary typed even though the pure
            # sidecar validator uses ValueError for CI-friendly callers.
            raise ExecutorFailure("invalid rootless Docker host") from exc
        object.__setattr__(self, "source_root", source)
        object.__setattr__(self, "data_root", data)
        object.__setattr__(self, "mask_map", mask)
        object.__setattr__(self, "run_root", run)
        object.__setattr__(self, "server_root", server_root)
        for field_name in ("binary_dir", "log_dir", "db_path"):
            value = getattr(self, field_name)
            if value is not None:
                resolved = _safe_abs(value, field_name)
                _inside(resolved, server_root, field_name)
                object.__setattr__(self, field_name, resolved)
        object.__setattr__(self, "docker_host", host)
        object.__setattr__(self, "server_image_digest", _image_digest(self.server_image_digest, "server_image_digest"))
        object.__setattr__(self, "workspace_image_digest", _image_digest(self.workspace_image_digest, "workspace_image_digest"))


def _response_status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status") or "").strip().lower()


def _gateway_path(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """Extract only members that remain below the task workspace."""

    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=False)
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve(strict=False)
            _inside(target, root, "archive member")
            if member.issym() or member.islnk() or member.isdev():
                raise ExecutorFailure("task archive contains a link or device member")
        for member in tar.getmembers():
            tar.extract(member, destination)


def _read_text(path: pathlib.Path, name: str, limit: int = 256_000) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExecutorFailure(f"missing or unreadable {name}") from exc
    return value[:limit]


def _parse_json_stdout(text: str) -> dict[str, Any]:
    # submit.sh may print a short informational line before its JSON response;
    # parse the last complete object without treating arbitrary text as evidence.
    candidates = [line.strip() for line in text.splitlines() if line.strip().startswith("{")]
    for line in reversed(candidates):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _record_matches(record: Mapping[str, Any], task_id: str, digest: str) -> bool:
    record_task = str(record.get("task_id") or "")
    record_hash = str(record.get("poc_hash") or record.get("hash") or "").lower()
    return record_task == task_id and record_hash == digest


class CyberGymExecutor:
    """Run one task at a time against a campaign-owned sidecar."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self.host = resolve_rootless_docker_host(config.docker_host)
        self.server_name = f"cybergym-server-{config.campaign_id}"
        self.network_id = ""
        self._network_created = False
        self.server_id = ""
        self.started = False
        self._task_containers: set[str] = set()
        self._plans: dict[str, NetworkPlan] = {}
        self.provider_observation: dict[str, Any] = {"required": bool(config.provider_probe), "status": "not_run"}

    def _docker(self, *args: str, timeout: float = 60) -> CommandResult:
        return self.config.command_runner(
            ["docker", "--host", self.host.value, *args],
            cwd=self.config.run_root,
            env={**os.environ, "DOCKER_HOST": self.host.value},
            timeout=timeout,
        )

    def _inspect(self, kind: str, name: str) -> Mapping[str, Any]:
        result = self._docker(kind, "inspect", name)
        if result.returncode != 0:
            raise ExecutorFailure(f"docker inspect failed for {kind} {name}")
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutorFailure("docker inspect returned invalid JSON") from exc
        if not isinstance(values, list) or not values or not isinstance(values[0], Mapping):
            raise ExecutorFailure("docker inspect returned no object")
        return values[0]

    def _ensure_key(self) -> str:
        value = os.environ.get(self.config.api_key_env, "")
        key_digest = hashlib.sha256(value.encode()).hexdigest() if value else ""
        if (
            not value
            or key_digest.startswith(_DEFAULT_CYBERGYM_KEY_SHA256_PREFIX)
            or value.lower() in {"placeholder", "changeme"}
        ):
            raise ExecutorFailure("CYBERGYM_API_KEY is missing or the upstream public default")
        return value

    def _probe_provider(self) -> None:
        """Probe the exact OpenRouter model before the first paid task.

        Only redacted identity/usage fields are persisted.  The provider key
        is held in the request header for the duration of this call and never
        enters a command line, checkpoint, or manifest.
        """
        if not self.config.provider_probe:
            self.provider_observation = {"required": False, "status": "disabled_by_injected_test"}
            return
        key = os.environ.get(self.config.provider_key_env, "")
        if not key or key.lower() in {"placeholder", "changeme"}:
            raise ExecutorFailure("OpenRouter provider key is missing or a placeholder")
        body = {
            "model": "deepseek/deepseek-v4-flash-0731",
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
            "temperature": 0,
            "provider": {"allow_fallbacks": True, "require_parameters": True},
        }
        response = self.config.http_runner(
            "POST", self.config.provider_url, body=body,
            headers={"Authorization": f"Bearer {key}"}, timeout=60,
        )
        observed = str(response.get("model") or "")
        if observed and observed != body["model"] and "deepseek-v4-flash" not in observed:
            raise ExecutorFailure("provider probe served a different model family")
        usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
        self.provider_observation = {
            "required": True,
            "status": "passed",
            "requested_model": body["model"],
            "observed_model": observed,
            "provider": str(response.get("provider") or ""),
            "response_id": str(response.get("id") or ""),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": usage.get("prompt_cache_hit_tokens"),
            "key_fingerprint": hashlib.sha256(key.encode()).hexdigest()[:16],
        }
        _write_json(self.config.run_root / "provider_probe.json", self.provider_observation)

    def _network(self) -> None:
        argv = build_network_create_argv(self.host, self._network_plan("campaign"))
        result = self.config.command_runner(argv, cwd=self.config.run_root, env={**os.environ, "DOCKER_HOST": self.host.value}, timeout=60)
        if result.returncode == 0:
            self.network_id = result.stdout.strip()
            self._network_created = True
        else:
            # The logical name is intentionally fixed by the upstream contract.
            # Reuse is allowed only when its labels prove this campaign owns it.
            info = self._inspect("network", "cybergym-internal")
            labels = ((info.get("Labels") or {}) if isinstance(info, Mapping) else {})
            if labels.get("com.ouroboros.campaign") != self.config.campaign_id:
                raise ExecutorFailure("cybergym-internal exists but is not campaign-owned")
            self.network_id = str(info.get("Id") or "")
            self._network_created = False
        if not self.network_id:
            raise ExecutorFailure("network create did not return an id")
        info = self._inspect("network", "cybergym-internal")
        if info.get("Name") != "cybergym-internal" or info.get("Internal") is not True or info.get("Driver") != "bridge":
            raise ExecutorFailure("CyberGym network attestation failed")

    def _network_plan(self, task_id: str) -> NetworkPlan:
        # ``NetworkPlan`` rejects aliases containing the task token.  A
        # campaign-level server has no real task, so use an opaque bootstrap
        # token rather than the human word ``campaign`` (which commonly occurs
        # in the campaign id itself).
        plan_task_id = "bootstrap" if task_id == "campaign" else task_id
        plan = NetworkPlan(
            self.config.campaign_id,
            plan_task_id,
            int(self.config.server_port),
            int(self.config.verifier_host_port or self.config.server_port + 1),
            server_container_port=int(self.config.server_port),
        )
        if task_id != "campaign":
            # The server has one campaign alias; per-task plans only change the
            # opaque workspace alias/agent identity.
            campaign_plan = self._network_plan("campaign")
            plan = dataclasses.replace(plan, server_alias=campaign_plan.server_alias)
        return plan

    def start(self) -> None:
        if self.started:
            return
        self._ensure_key()
        self._probe_provider()
        if not self.config.mask_map.is_file() or not self.config.data_root.is_dir():
            raise ExecutorFailure("CyberGym data or mask map is unavailable")
        self.config.server_root.mkdir(parents=True, exist_ok=True)
        binary_dir = self.config.binary_dir or (self.config.server_root / "binary")
        log_dir = self.config.log_dir or (self.config.server_root / "logs")
        db_path = self.config.db_path or (self.config.server_root / "poc.db")
        binary_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        self._network()
        plan = self._network_plan("campaign")
        server_command = (
            "python", "-m", "cybergym.server", "--host", "0.0.0.0",
            "--port", str(plan.server_container_port),
            "--mask_map_path", str(self.config.mask_map),
            "--log_dir", str(log_dir),
            "--db_path", str(db_path),
            "--binary_dir", str(binary_dir),
        )
        spec = SidecarCommandSpec(
            self.host,
            plan,
            self.config.server_image,
            self.server_name,
            command=server_command,
            image_digest=self.config.server_image_digest,
            data_host_path=str(self.config.server_root),
            data_container_path=str(self.config.server_root),
        )
        result = self.config.command_runner(
            build_sidecar_argv(spec), cwd=self.config.run_root,
            env={**os.environ, "DOCKER_HOST": self.host.value}, timeout=120,
        )
        if result.returncode != 0:
            raise ExecutorFailure("CyberGym server sidecar failed to start")
        self.server_id = result.stdout.strip()
        if not self.server_id:
            raise ExecutorFailure("CyberGym server sidecar returned no container id")
        observed = self._inspect("container", self.server_name)
        networks = ((observed.get("NetworkSettings") or {}).get("Networks") or {})
        if "cybergym-internal" not in networks:
            raise ExecutorFailure("server sidecar is not on cybergym-internal")
        self.started = True
        self._write_campaign_state({"server_container": self.server_name, "server_id": self.server_id, "network_id": self.network_id, "docker_host": self.host.value})
        self._wait_server(plan)

    def _wait_server(self, plan: NetworkPlan) -> None:
        # FastAPI's /docs is HTML; the JSON transport uses the equivalent
        # OpenAPI route so readiness does not mistake a healthy server for a
        # malformed JSON response.
        url = f"http://127.0.0.1:{plan.verifier_host_port}/openapi.json"
        deadline = time.time() + 120
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self.config.http_runner("GET", url, timeout=10)
                return
            except Exception as exc:  # transport/readiness only; do not leak body
                last_error = exc
            self.config.sleep(1)
        raise ExecutorFailure("CyberGym server did not expose its documented route") from last_error

    def _write_campaign_state(self, state: Mapping[str, Any]) -> None:
        _write_json(self.config.run_root / "sidecar_state.json", state)

    def _generate(self, task: TaskSpec, task_dir: pathlib.Path, agent_id: str) -> None:
        argv = build_generate_task_argv(
            task.task_id,
            out_dir=task_dir,
            data_dir=self.config.data_root,
            server=self._network_plan(task.task_id).server_url,
            mask_map=self.config.mask_map,
            difficulty=self.config.difficulty,
            python=self.config.python_executable,
            agent_id=agent_id,
        )
        result = self.config.command_runner(argv, cwd=self.config.source_root, env={**os.environ, "DOCKER_HOST": self.host.value}, timeout=600)
        if result.returncode != 0:
            raise ExecutorFailure("official CyberGym task generation failed")
        expected = ("repo-vul.tar.gz", "description.txt", "README.md", "submit.sh")
        if any(not (task_dir / name).is_file() for name in expected):
            raise ExecutorFailure("generator did not produce the complete Level-1 task")
        _safe_extract(task_dir / "repo-vul.tar.gz", task_dir)
        (task_dir / "submissions").mkdir(exist_ok=True)

    def _workspace(self, task: TaskSpec, task_dir: pathlib.Path, plan: NetworkPlan) -> str:
        container_name = f"cybergym-workspace-{plan.opaque_agent_id}"
        spec = WorkspaceCommandSpec(
            self.host,
            plan,
            self.config.workspace_image,
            container_name,
            str(task_dir),
            command=self.config.command,
            labels={"com.ouroboros.image_digest": self.config.workspace_image_digest},
        )
        result = self.config.command_runner(
            build_workspace_argv(spec), cwd=self.config.run_root,
            env={**os.environ, "DOCKER_HOST": self.host.value}, timeout=120,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise ExecutorFailure("CyberGym workspace failed to start")
        self._task_containers.add(container_name)
        observed = self._inspect("container", container_name)
        networks = ((observed.get("NetworkSettings") or {}).get("Networks") or {})
        if "cybergym-internal" not in networks:
            raise ExecutorFailure("workspace is not on cybergym-internal")
        return container_name

    def _task_body(self, task: TaskSpec, task_dir: pathlib.Path, container_name: str, attempt_id: str) -> dict[str, Any]:
        opaque = "cybergym-" + hashlib.sha256(f"{self.config.campaign_id}\0{task.task_id}\0{attempt_id}".encode()).hexdigest()[:32]
        description = _read_text(task_dir / "description.txt", "description")
        return {
            "task_id": opaque,
            "description": description + "\n\nWrite exactly one final PoC to /workspace/final.poc and do not expose hidden artifacts.",
            "workspace_root": str(task_dir),
            "workspace_mode": "external",
            "memory_mode": "empty",
            "allowed_resources": {"network": True, "web": False, "internet": False},
            "disabled_tools": sorted(set(self.config.disabled_tools)),
            "executor_ref": {
                "type": "docker_exec",
                "id": container_name,
                "container_name": container_name,
                "network": EXECUTOR_NETWORK_DECLARATION,
                "workspace_host_path": str(task_dir),
                "workspace_backend_path": "/workspace",
            },
            "timeout_sec": int(self.config.task_timeout_sec),
            "actor_id": "cybergym",
            "source": "cybergym",
            "metadata": {
                "benchmark": "cybergym",
                "attempt_id": attempt_id,
                "level": DEFAULT_LEVEL,
                "final_poc_path": DEFAULT_FINAL_POC_PATH,
                # The gateway-visible contract must not carry the upstream
                # real id; it is retained only in the host-side result row.
                "task_contract": task_contract_metadata(task_id="", disabled_tools=self.config.disabled_tools),
            },
        }

    def _gateway_wait(self, body: Mapping[str, Any], checkpoint: pathlib.Path) -> Mapping[str, Any]:
        created = self.config.http_runner("POST", _gateway_path(self.config.ouroboros_url, "/api/tasks"), body=body, timeout=60)
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise ExecutorFailure("Ouroboros gateway returned no task id")
        _write_json(checkpoint, {"gateway_task_id": task_id, "status": "submitted", "body": {k: v for k, v in body.items() if k != "description"}})
        deadline = time.time() + self.config.task_timeout_sec
        latest: Mapping[str, Any] = created
        while time.time() < deadline:
            latest = self.config.http_runner("GET", _gateway_path(self.config.ouroboros_url, "/api/tasks/" + urllib.parse.quote(task_id, safe="")), timeout=60)
            _write_json(checkpoint, {"gateway_task_id": task_id, "status": _response_status(latest), "result": dict(latest)})
            if _response_status(latest) in _SETTLED:
                return latest
            self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
        raise ExecutorFailure("Ouroboros task exceeded its logical deadline; checkpoint retained")

    def _submit_final(self, task: TaskSpec, task_dir: pathlib.Path, container_name: str) -> tuple[dict[str, Any], str]:
        marker = final_poc_record(task_dir)
        result = self.config.command_runner(
            ["docker", "--host", self.host.value, "exec", "--workdir", "/workspace", container_name, *build_submit_argv(pathlib.Path("/workspace/submit.sh"), pathlib.Path("/workspace/final.poc"))[0:]],
            cwd=self.config.run_root, env={**os.environ, "DOCKER_HOST": self.host.value}, timeout=300,
        )
        response = _parse_json_stdout(result.stdout)
        if result.returncode != 0 or not response:
            raise ExecutorFailure("official submit.sh did not return a JSON response")
        response["final_poc_sha256"] = marker.sha256
        response["submit_returncode"] = result.returncode
        return response, marker.sha256

    def _private_query(self, agent_id: str, real_task_id: str) -> list[dict[str, Any]]:
        key = self._ensure_key()
        headers = {"X-API-Key": key}
        # The private server is loopback-published; do not put the key in any
        # checkpoint or returned artifact.
        plan = self._network_plan("campaign")
        payload = self.config.http_runner(
            "POST", f"http://127.0.0.1:{plan.verifier_host_port}/query-poc",
            body={"agent_id": agent_id, "task_id": real_task_id}, headers=headers, timeout=60,
        )
        records = payload.get("records", payload.get("pocs", payload)) if isinstance(payload, Mapping) else []
        if isinstance(records, Mapping):
            records = records.get("items", [])
        return [dict(item) for item in records if isinstance(item, Mapping)] if isinstance(records, Sequence) and not isinstance(records, (str, bytes)) else []

    def run_task(self, task: TaskSpec, task_dir: pathlib.Path) -> Mapping[str, Any]:
        """Execute one admitted task; callback-compatible with ``run_campaign``."""

        self.start()
        attempt_id = str(task.metadata.get("attempt_id") or uuid.uuid4().hex)
        agent_id = make_opaque_agent_id(f"{self.config.campaign_id}-{attempt_id[:16]}", task.task_id)
        plan = self._network_plan(task.task_id)
        self._plans[attempt_id] = plan
        task_dir = _safe_abs(task_dir, "task_dir")
        _inside(task_dir, _safe_abs(self.config.run_root, "run_root"), "task_dir")
        self._generate(task, task_dir, agent_id)
        container_name = self._workspace(task, task_dir, plan)
        body = self._task_body(task, task_dir, container_name, attempt_id)
        # Checkpoints and verifier responses are host-private.  Keeping them
        # beside the mounted task files would let a still-running agent read
        # server ids, raw exits, or another task's diagnostics.
        checkpoint = safe_task_path(self.config.run_root / "checkpoints", task.task_id) / "gateway_checkpoint.json"
        gateway_result = self._gateway_wait(body, checkpoint)
        if _response_status(gateway_result) != "completed":
            return {"status": "infra_failed", "lifecycle": "gateway_terminal", "infra_reason": _response_status(gateway_result) or "gateway_failed", "runtime_result": dict(gateway_result), "artifact_refs": {"task_dir": str(task_dir), "checkpoint": str(checkpoint)}}
        submit_response, digest = self._submit_final(task, task_dir, container_name)
        # verify-agent-pocs is the upstream operation that reruns both images.
        key = self._ensure_key()
        plan_host = f"http://127.0.0.1:{plan.verifier_host_port}"
        verify_response = self.config.http_runner("POST", plan_host + "/verify-agent-pocs", body={"agent_id": agent_id}, headers={"X-API-Key": key}, timeout=300)
        records = self._private_query(agent_id, task.task_id)
        matching = [item for item in records if _record_matches(item, task.task_id, digest)]
        if not matching:
            raise ExecutorFailure("private query returned no record for the designated final PoC")
        record = matching[-1]
        trial = {
            "trial_id": str(record.get("poc_id") or digest[:16]),
            "poc_id": record.get("poc_id"),
            "poc_hash": digest,
            "vul_exit_code": record.get("vul_exit_code"),
            "fix_exit_code": record.get("fix_exit_code"),
            "is_final": True,
        }
        private_artifact = safe_task_path(self.config.run_root / "private", task.task_id) / "submit_response.json"
        _write_json(private_artifact, {"submit": submit_response, "verify": verify_response, "record": record})
        return {
            "status": "completed",
            "lifecycle": "official_verified",
            "final_poc": FinalPoc(str(task_dir / "final.poc"), digest, int((task_dir / "final.poc").stat().st_size)),
            "final_poc_sha256": digest,
            "trials": [trial],
            "final_trial": trial,
            "artifact_refs": {"task_dir": str(task_dir), "checkpoint": str(checkpoint), "submit": str(private_artifact)},
            "runtime_result": dict(gateway_result),
            "observed_model": str(gateway_result.get("observed_model") or ""),
            "observed_provider": str(gateway_result.get("observed_provider") or ""),
            "observed_effort": str(gateway_result.get("observed_effort") or ""),
            "prompt_tokens": gateway_result.get("prompt_tokens"),
            "completion_tokens": gateway_result.get("completion_tokens"),
            "cached_tokens": gateway_result.get("cached_tokens"),
            "cost_usd": gateway_result.get("cost_usd"),
            "leakage": {"agent_id": agent_id, "masked_id_source": "official_generator"},
        }

    def close(self) -> None:
        """Remove only containers created by this executor; retain unknown custody."""

        for name in sorted(self._task_containers):
            result = self._docker("rm", "--force", name, timeout=60)
            if result.returncode not in {0, 1}:
                raise ExecutorFailure(f"workspace cleanup failed for owned container {name}")
        if self.server_name and self.server_id:
            result = self._docker("rm", "--force", self.server_name, timeout=60)
            if result.returncode not in {0, 1}:
                raise ExecutorFailure("server sidecar cleanup failed")
        if self.network_id and self._network_created:
            result = self._docker("network", "rm", self.network_id, timeout=60)
            if result.returncode not in {0, 1}:
                raise ExecutorFailure("campaign network cleanup failed")
        self._task_containers.clear()
        self.network_id = ""
        self.server_id = ""
        self._network_created = False
        self.started = False

    def __enter__(self) -> "CyberGymExecutor":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_executor(config: ExecutorConfig) -> CyberGymExecutor:
    """Factory used by the launcher after admission."""

    return CyberGymExecutor(config)


__all__ = [
    "CommandResult",
    "ExecutorConfig",
    "ExecutorFailure",
    "CyberGymExecutor",
    "build_executor",
    "run_command",
    "urllib_json",
]
