"""Tests for the adapter-owned isolated Ouroboros server wrapper."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from devtools.benchmarks.cybergym.cybergym_server import (
    CyberGymIsolatedServer,
    CyberGymServerError,
    _RootlessIsolatedServer,
)


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL)


def _seed_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = tmp_path / "seed"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "VERSION").write_text("test-version\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "seed"],
        cwd=repo,
        check=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, commit


def _settings(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "settings_applied.json"
    path.write_text(json.dumps({"OUROBOROS_MODEL": "deepseek/deepseek-v4-flash-0731"}), encoding="utf-8")
    return path


def _host():
    from devtools.benchmarks.cybergym.cybergym_sidecar import resolve_rootless_docker_host

    return resolve_rootless_docker_host("unix:///run/user/1006/docker.sock")


def test_prepare_clones_pinned_seed_and_copies_settings(tmp_path):
    seed, commit = _seed_repo(tmp_path)
    wrapper = CyberGymIsolatedServer(seed, tmp_path / "run", _settings(tmp_path), _host(), expected_commit=commit)
    wrapper.prepare()
    assert wrapper.clone_root.is_dir()
    assert wrapper.settings_path.read_text(encoding="utf-8").startswith("{")
    assert wrapper.settings_path.stat().st_mode & 0o777 == 0o600
    assert (wrapper.data_root / ".ouroboros_isolated_benchmark").is_file()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wrapper.clone_root, text=True).strip() == commit
    assert subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=wrapper.clone_root, check=False, stdout=subprocess.DEVNULL).returncode != 0


def test_wrapper_requires_fresh_root_and_explicit_commit(tmp_path):
    seed, commit = _seed_repo(tmp_path)
    with pytest.raises(CyberGymServerError, match="expected_commit"):
        CyberGymIsolatedServer(seed, tmp_path / "run", _settings(tmp_path), _host())
    run = tmp_path / "run"
    run.mkdir()
    (run / "ouroboros-clone").mkdir()
    with pytest.raises(CyberGymServerError, match="child paths"):
        CyberGymIsolatedServer(seed, run, _settings(tmp_path), _host(), expected_commit=commit)


def test_rootless_wrapper_injects_selected_socket(monkeypatch, tmp_path):
    seed, _commit = _seed_repo(tmp_path)
    host = _host()
    from devtools.benchmarks.common.server_runner import IsolatedServer

    delegate = _RootlessIsolatedServer(seed, tmp_path / "data", _settings(tmp_path), docker_host=host)
    monkeypatch.setattr(delegate._delegate, "_env", lambda: {"DOCKER_HOST": "unix:///var/run/docker.sock"})
    assert delegate._env()["DOCKER_HOST"] == host.value
    assert isinstance(delegate._delegate, IsolatedServer)


def test_rootless_wrapper_makes_applied_settings_authoritative(monkeypatch, tmp_path):
    seed, _commit = _seed_repo(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "OPENROUTER_API_KEY": "",
        "OUROBOROS_MODEL": "deepseek/deepseek-v4-flash-0731",
        "CLAUDE_CODE_MODEL": "",
        "OUROBOROS_RUNTIME_MODE": "pro",
    }), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-router")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-anthropic")
    monkeypatch.setenv("OUROBOROS_MODEL", "ambient/model")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_MODEL", "ambient-claude")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")

    delegate = _RootlessIsolatedServer(
        seed,
        tmp_path / "data",
        settings,
        docker_host=_host(),
        provider_key="selected-router",
        settings_authoritative_env=True,
    )
    env = delegate._env()

    assert env["OPENROUTER_API_KEY"] == "selected-router"
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OUROBOROS_MODEL" not in env
    assert "CLAUDE_AGENT_SDK_MODEL" not in env
    assert "OUROBOROS_RUNTIME_MODE" not in env
    assert env["DOCKER_HOST"] == _host().value


def test_authoritative_env_scrubs_legacy_and_future_runtime_overrides(monkeypatch, tmp_path):
    seed, _commit = _seed_repo(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "OUROBOROS_MODEL": "deepseek/deepseek-v4-flash-0731",
        "OUROBOROS_RUNTIME_MODE": "pro",
    }), encoding="utf-8")
    inherited = {
        "OUROBOROS_MODEL_CODE": "legacy-code",
        "OUROBOROS_VISION_MODEL": "legacy-vision",
        "OUROBOROS_MODEL_FALLBACK": "legacy-fallback",
        "OUROBOROS_EFFORT_TASK": "low",
        "OUROBOROS_RUNTIME_MODE": "light",
        "OUROBOROS_SAFETY_MODE": "full",
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_REVIEW_MODELS": "wrong/reviewer",
        "OUROBOROS_MAX_ROUNDS": "7",
        "OUROBOROS_MAX_WORKERS": "1",
        "OUROBOROS_SUBAGENT_HARNESS": "cursor",
        "USE_LOCAL_HEAVY": "true",
        "USE_LOCAL_CODE": "true",
        "CLAUDE_AGENT_SDK_MODEL": "ambient-claude",
        "OPENAI_BASE_URL": "https://ambient.invalid/v1",
        "OUROBOROS_FUTURE_RUNTIME_SWITCH": "ambient",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    delegate = _RootlessIsolatedServer(
        seed,
        tmp_path / "data",
        settings,
        docker_host=_host(),
        provider_key="selected-router",
        settings_authoritative_env=True,
    )
    env = delegate._env()

    for key in inherited:
        assert key not in env, key
    assert env["OPENROUTER_API_KEY"] == "selected-router"
    assert env["OUROBOROS_APP_ROOT"] == str(seed.parent)
    assert "PATH" in env


def test_authoritative_server_refuses_unreadable_settings_before_spawn(monkeypatch, tmp_path):
    seed, _commit = _seed_repo(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text("{not-json", encoding="utf-8")
    from devtools.benchmarks.common.server_runner import IsolatedServer

    server = IsolatedServer(
        seed,
        tmp_path / "data",
        settings,
        settings_authoritative_env=True,
    )
    with pytest.raises(RuntimeError, match="settings snapshot is unreadable"):
        server.start()
    assert server.proc is None


def test_rootless_wrapper_start_does_not_recurse_when_delegate_calls_env(monkeypatch, tmp_path):
    seed, _commit = _seed_repo(tmp_path)
    host = _host()
    from devtools.benchmarks.common.server_runner import IsolatedServer

    delegate = _RootlessIsolatedServer(
        seed,
        tmp_path / "data",
        _settings(tmp_path),
        docker_host=host,
        provider_key="provider-secret",
    )
    observed = {}

    def fake_start(*, ready_timeout):
        observed["env"] = delegate._delegate._env()  # noqa: SLF001 - lifecycle seam

    monkeypatch.setattr(delegate._delegate, "start", fake_start)
    delegate.start(ready_timeout=1)
    assert observed["env"]["DOCKER_HOST"] == host.value
    assert observed["env"]["OPENROUTER_API_KEY"] == "provider-secret"
    assert isinstance(delegate._delegate, IsolatedServer)


class _FakeServer:
    base_url = "http://127.0.0.1:19001"
    attestation = {"repo_head": "a" * 40, "runtime_version": "test-version"}

    def __init__(self, *_args, **_kwargs):
        self.started = False
        self.stopped = False

    def start(self, **_kwargs):
        self.started = True

    def stop(self):
        self.stopped = True


def test_start_exposes_attested_base_url_and_closes(tmp_path):
    seed, commit = _seed_repo(tmp_path)
    seen = {}

    def factory(seed_repo, data_root, settings_path, *, docker_host, provider_key, provider_key_env):
        seen["args"] = (seed_repo, data_root, settings_path)
        seen["kwargs"] = {
            "docker_host": docker_host,
            "provider_key": provider_key,
            "provider_key_env": provider_key_env,
        }
        fake = _FakeServer()
        fake.attestation = {"repo_head": commit, "runtime_version": "test-version"}
        return fake

    wrapper = CyberGymIsolatedServer(
        seed,
        tmp_path / "run",
        _settings(tmp_path),
        _host(),
        expected_commit=commit,
        server_factory=factory,
    )
    wrapper.start()
    assert wrapper.base_url == "http://127.0.0.1:19001"
    assert wrapper.attestation["repo_head"] == commit
    assert seen["kwargs"]["docker_host"].value == _host().value
    server = wrapper._server
    wrapper.close()
    assert server.stopped is True
