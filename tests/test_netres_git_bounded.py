"""Lane C1 contracts: bounded network git honors an explicit cwd, kills a hung
process tree on timeout, and the routed callers keep their result shapes."""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import time

from supervisor import git_ops, update_source


def _git(repo: pathlib.Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    return res.stdout.strip()


def _seed_repo(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-qm", "seed")
    return path


def test_git_network_bounded_honors_explicit_cwd(tmp_path, monkeypatch):
    """An explicit cwd selects the repository; the system repo is untouched."""
    upstream = _seed_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)],
        capture_output=True, text=True, check=True,
    )
    (upstream / "seed.txt").write_text("advanced\n", encoding="utf-8")
    _git(upstream, "commit", "-qam", "advance")
    new_tip = _git(upstream, "rev-parse", "HEAD")

    # A non-repo default proves the explicit cwd (not REPO_DIR) was used.
    sentinel = tmp_path / "not-a-repo"
    sentinel.mkdir()
    monkeypatch.setattr(git_ops, "REPO_DIR", sentinel)

    rc, _out, err = update_source._git_network_bounded(
        ["fetch", "origin"], cwd=clone, timeout=60,
    )

    assert rc == 0, err
    assert _git(clone, "rev-parse", "origin/main") == new_tip


def test_git_network_bounded_default_cwd_remains_system_repo(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path / "repo")
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    captured = {}

    def fake_bounded(cmd, *, timeout, cwd=None, env=None, text=True):
        captured["cwd"] = cwd
        return 0, "", ""

    monkeypatch.setattr(git_ops, "_run_git_process_bounded", fake_bounded)
    rc, _out, _err = update_source._git_network_bounded(["fetch", "origin"])
    assert rc == 0
    assert captured["cwd"] == repo


def test_git_network_bounded_rejects_missing_cwd(tmp_path):
    rc, out, err = update_source._git_network_bounded(
        ["fetch", "origin"], cwd=tmp_path / "does-not-exist",
    )
    assert rc != 0
    assert out == ""
    assert "cwd" in err


def test_git_network_bounded_timeout_kills_process_tree(tmp_path, monkeypatch):
    """A hung network git is killed together with its children and returns the
    typed timeout shape; nothing keeps holding the repository afterwards — a
    real follow-up network git command in the same clone must succeed (this
    passing follow-up IS the lock-release contract; no extra cleanup exists)."""
    upstream = _seed_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)],
        capture_output=True, text=True, check=True,
    )
    (upstream / "seed.txt").write_text("advanced\n", encoding="utf-8")
    _git(upstream, "commit", "-qam", "advance")
    new_tip = _git(upstream, "rev-parse", "HEAD")

    original_path = os.environ.get("PATH", "")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    fake_git = shim_dir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'echo $$ > "$NETRES_PID_DIR/parent.pid"\n'
        "sleep 300 &\n"
        'echo $! > "$NETRES_PID_DIR/child.pid"\n'
        "wait\n",
        encoding="utf-8",
    )
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{original_path}")
    monkeypatch.setenv("NETRES_PID_DIR", str(pid_dir))

    rc, out, err = update_source._git_network_bounded(
        ["fetch", "origin"], cwd=clone, timeout=1.0,
    )

    assert rc == update_source.FETCH_TIMEOUT_RC
    assert out == ""
    assert "exceeded" in err

    pids = []
    for name in ("parent.pid", "child.pid"):
        raw = (pid_dir / name).read_text(encoding="utf-8").strip()
        assert raw, f"{name} was never written — shim did not run"
        pids.append(int(raw))
    deadline = time.monotonic() + 5
    for pid in pids:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"process {pid} survived the bounded timeout kill")

    # Contract: the killed fetch left nothing behind that breaks a real
    # follow-up network git command in the same clone.
    monkeypatch.setenv("PATH", original_path)
    rc2, _out2, err2 = update_source._git_network_bounded(
        ["fetch", "origin"], cwd=clone, timeout=60,
    )
    assert rc2 == 0, err2
    assert _git(clone, "rev-parse", "origin/main") == new_tip


def test_push_to_remote_timeout_surfaces_as_todays_failure_shape(monkeypatch):
    monkeypatch.setattr(git_ops, "_has_remote", lambda _name: True)
    monkeypatch.setattr(
        git_ops,
        "_git_network_bounded",
        lambda _cmd, **_kw: (git_ops.FETCH_TIMEOUT_RC, "", "git push exceeded 300s and was terminated"),
    )
    ok, message = git_ops.push_to_remote("feature")
    assert ok is False
    assert message.startswith("git push failed:")
    assert "exceeded" in message


def test_push_to_remote_tags_timeout_stays_best_effort(monkeypatch):
    monkeypatch.setattr(git_ops, "_has_remote", lambda _name: True)
    results = iter([
        (0, "", ""),
        (git_ops.FETCH_TIMEOUT_RC, "", "git push exceeded 300s and was terminated"),
    ])
    monkeypatch.setattr(
        git_ops, "_git_network_bounded", lambda _cmd, **_kw: next(results),
    )
    ok, message = git_ops.push_to_remote("feature", push_tags=True)
    assert ok is True
    assert "Pushed feature to origin" in message
    assert "tags push failed" in message


def test_ff_pull_fetch_is_bounded_with_repo_cwd_and_keeps_error_shape(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_tools

    upstream = _seed_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)],
        capture_output=True, text=True, check=True,
    )
    captured = {}

    def fake_bounded(args, *, cwd=None, timeout=None):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        return 0, "", ""

    monkeypatch.setattr(update_source, "_git_network_bounded", fake_bounded)
    result = git_tools._ff_pull(clone)
    assert captured["args"][0] == "fetch"
    assert captured["cwd"] == clone
    assert "Already up to date" in result

    monkeypatch.setattr(
        update_source,
        "_git_network_bounded",
        lambda args, **_kw: (1, "", "fatal: could not read from remote repository"),
    )
    result = git_tools._ff_pull(clone)
    assert result.startswith("⚠️ PULL_ERROR: git fetch failed:")


def test_ci_push_branch_is_bounded_with_repo_cwd_and_keeps_shape(tmp_path, monkeypatch):
    from ouroboros.tools import ci

    repo = _seed_repo(tmp_path / "repo")
    captured = {}

    def fake_bounded(args, *, cwd=None, timeout=None):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        return 0, "pushed", ""

    monkeypatch.setattr(update_source, "_git_network_bounded", fake_bounded)
    ok, message = ci._push_branch(str(repo), "feature")
    assert ok is True
    assert message == "pushed"
    assert captured["args"] == ["push", "-u", "origin", "feature"]
    assert captured["cwd"] == repo

    monkeypatch.setattr(
        update_source,
        "_git_network_bounded",
        lambda args, **_kw: (update_source.FETCH_TIMEOUT_RC, "", "git push exceeded 300s and was terminated"),
    )
    ok, message = ci._push_branch(str(repo), "feature")
    assert ok is False
    assert "exceeded" in message
