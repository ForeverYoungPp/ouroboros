"""Adapter-owned isolated Ouroboros server for CyberGym runs.

The CyberGym executor talks to an Ouroboros HTTP gateway.  This wrapper makes
that gateway an owned, throwaway server instead of silently attaching to the
operator's live process.  It deliberately reuses the common
``IsolatedServer`` lifecycle and changes only the adapter boundary: the
selected rootless Docker socket is injected into the isolated server process,
while the repository clone and settings/data roots remain run-local.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from devtools.benchmarks.cybergym.cybergym_sidecar import (
    DockerHostRef,
    resolve_rootless_docker_host,
)


class CyberGymServerError(RuntimeError):
    """Typed refusal for isolated-server preparation or attestation."""


GitRunner = Callable[[Sequence[str], pathlib.Path], int]
ServerFactory = Callable[..., Any]


def _run_git(argv: Sequence[str], cwd: pathlib.Path) -> int:
    """Run a fixed argv git operation without a shell or output logging."""
    try:
        result = subprocess.run(
            ["git", *list(argv)],
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return 127
    return int(result.returncode)


def _run_git_output(argv: Sequence[str], cwd: pathlib.Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *list(argv)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 127, ""
    return int(result.returncode), (result.stdout or "").strip()


class _RootlessIsolatedServer:
    """Small subclass that adds the explicit rootless socket to IsolatedServer._env."""

    def __init__(self, *args: Any, docker_host: DockerHostRef, **kwargs: Any) -> None:
        from devtools.benchmarks.common.server_runner import IsolatedServer

        self._docker_host = docker_host
        self._delegate = IsolatedServer(*args, **kwargs)

    def _env(self) -> dict[str, str]:
        env = self._delegate._env()  # noqa: SLF001 - existing lifecycle seam
        env["DOCKER_HOST"] = self._docker_host.value
        return env

    def start(self, *args: Any, **kwargs: Any) -> Any:
        # Bind the subclass environment method for the existing server
        # implementation without copying its process/attestation lifecycle.
        self._delegate._env = self._env  # type: ignore[method-assign]  # noqa: SLF001
        return self._delegate.start(*args, **kwargs)

    def stop(self) -> None:
        self._delegate.stop()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class CyberGymIsolatedServer:
    """Prepare and own one isolated Ouroboros server for a CyberGym campaign.

    ``run_root`` must already be an adapter-approved fresh output directory.
    The wrapper refuses to reuse an existing clone/data child, preventing a
    resumed run from silently changing the server identity or settings.
    """

    def __init__(
        self,
        seed_repo: pathlib.Path | str,
        run_root: pathlib.Path | str,
        applied_settings: pathlib.Path | str,
        docker_host: str | DockerHostRef,
        *,
        expected_commit: str = "",
        server_factory: ServerFactory | None = None,
        git_runner: GitRunner | None = None,
    ) -> None:
        self.seed_repo = pathlib.Path(seed_repo).expanduser().resolve(strict=False)
        self.run_root = pathlib.Path(run_root).expanduser().resolve(strict=False)
        self.applied_settings = pathlib.Path(applied_settings).expanduser().resolve(strict=False)
        self.docker_host = resolve_rootless_docker_host(docker_host)
        self.expected_commit = str(expected_commit or "").strip().lower()
        self._server_factory = server_factory
        self._git_runner = git_runner or _run_git
        self.clone_root = self.run_root / "ouroboros-clone"
        self.data_root = self.run_root / "ouroboros-data"
        self.settings_path = self.data_root / "settings.json"
        self._server: Any | None = None
        self._prepared = False
        self._started = False
        self.attestation: dict[str, Any] = {}

        if not self.seed_repo.is_dir():
            raise CyberGymServerError("seed_repo must be an existing directory")
        if not self.expected_commit:
            raise CyberGymServerError("expected_commit is required for isolated-server provenance")
        if not self.run_root.is_absolute() or self.run_root == pathlib.Path("/"):
            raise CyberGymServerError("run_root must be a non-root absolute path")
        try:
            self.run_root.relative_to(self.seed_repo)
            raise CyberGymServerError("run_root must not be inside seed_repo")
        except ValueError:
            pass
        try:
            self.seed_repo.relative_to(self.run_root)
            raise CyberGymServerError("run_root must not contain seed_repo")
        except ValueError:
            pass
        if not self.applied_settings.is_file():
            raise CyberGymServerError("applied_settings must name an existing JSON file")
        if self.clone_root.exists() or self.data_root.exists():
            raise CyberGymServerError("isolated server child paths already exist; use a fresh run root")

    def _git(self, argv: Sequence[str], cwd: pathlib.Path) -> int:
        return int(self._git_runner(argv, cwd))

    def _git_value(self, argv: Sequence[str], cwd: pathlib.Path, label: str) -> str:
        # Keep mutation injectable, but use a separate read-only probe for the
        # commit/status values that are part of provenance.
        code, value = _run_git_output(argv, cwd)
        if code != 0 or not value:
            raise CyberGymServerError(f"git probe failed: {label}")
        return value

    def _clone(self) -> None:
        if self._git(("clone", "--no-hardlinks", "--quiet", str(self.seed_repo), str(self.clone_root)), self.run_root) != 0:
            raise CyberGymServerError("unable to clone the pinned Ouroboros seed")
        # The local clone must not retain a remote back to the operator checkout.
        self._git(("remote", "remove", "origin"), self.clone_root)
        commit = self._git_value(("rev-parse", "HEAD"), self.clone_root, "HEAD").lower()
        if self.expected_commit and commit != self.expected_commit:
            raise CyberGymServerError("isolated Ouroboros clone commit does not match the pinned seed")
        code, status = _run_git_output(("status", "--porcelain=v1", "--untracked-files=all"), self.clone_root)
        if code != 0:
            raise CyberGymServerError("isolated Ouroboros clone status probe failed")
        if status:
            raise CyberGymServerError("isolated Ouroboros clone is dirty")

    def _copy_settings(self) -> None:
        try:
            payload = json.loads(self.applied_settings.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CyberGymServerError("applied settings are unreadable") from exc
        if not isinstance(payload, Mapping):
            raise CyberGymServerError("applied settings must be a JSON object")
        self.data_root.mkdir(parents=True, exist_ok=False)
        (self.data_root / "state").mkdir(parents=True, exist_ok=False)
        temporary = self.settings_path.with_name(self.settings_path.name + f".tmp.{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.settings_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CyberGymServerError("unable to copy applied settings into isolated data") from exc
        self.settings_path.chmod(0o600)
        try:
            from supervisor import state as supervisor_state

            sentinel = self.data_root / supervisor_state.ISOLATED_BENCHMARK_SENTINEL
        except (ImportError, AttributeError):
            sentinel = self.data_root / ".ouroboros_isolated_benchmark"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("isolated CyberGym server\n", encoding="utf-8")

    def prepare(self) -> "CyberGymIsolatedServer":
        if self._prepared:
            return self
        self.run_root.mkdir(parents=True, exist_ok=True)
        self._clone()
        self._copy_settings()
        self._prepared = True
        return self

    def start(self, *, ready_timeout: float = 180) -> "CyberGymIsolatedServer":
        if self._started:
            return self
        self.prepare()
        factory = self._server_factory
        if factory is None:
            factory = _RootlessIsolatedServer
        self._server = factory(
            self.clone_root,
            self.data_root,
            self.settings_path,
            docker_host=self.docker_host,
        )
        try:
            self._server.start(ready_timeout=ready_timeout)
            observed = dict(getattr(self._server, "attestation", {}) or {})
            observed_head = str(observed.get("repo_head") or "").lower()
            if self.expected_commit and observed_head != self.expected_commit:
                raise CyberGymServerError("isolated Ouroboros runtime attested a different commit")
            self.attestation = {
                "base_url": str(self._server.base_url),
                "docker_host": self.docker_host.value,
                "clone_root": str(self.clone_root),
                "data_root": str(self.data_root),
                "settings_path": str(self.settings_path),
                "repo_head": observed_head,
                "runtime": observed,
            }
            self._started = True
            return self
        except BaseException:
            self.close()
            raise

    @property
    def base_url(self) -> str:
        if self._server is None or not self._started:
            raise CyberGymServerError("isolated Ouroboros server is not started")
        return str(self._server.base_url)

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
        self._started = False

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> "CyberGymIsolatedServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = ["CyberGymIsolatedServer", "CyberGymServerError"]
