"""Delegated skill-payload capability (R1): the standalone private snapshot, the
payload capture adapter, and the parent-only CAS apply.

The restored D10 target class: a delegated run edits ONE exact skill payload
through a private standalone Git snapshot and never the live payload; the parent
applies the captured diff explicitly. The delegated TRANSPORT retired with
Claudexor, so the start/wait legs of that walk are gone; what remains pinned
here is the custody, capture and apply machinery that still runs.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import (
    find_execution_snapshot,
    provision_payload_snapshot,
    remove_execution_snapshot,
)


@pytest.fixture(autouse=True)


def _owned_gateway_uses_each_test_transport(monkeypatch):
    """Same seam as the transport suite: the owned-daemon lifecycle has its own
    focused tests; here every case supplies a fake gateway class."""
    from ouroboros import claudexor_daemon
    from ouroboros.gateways import claudexor as gateway_module

    monkeypatch.setattr(
        claudexor_daemon,
        "ensure_owned_gateway",
        lambda: gateway_module.ClaudexorGateway(),
    )


def _seed_skill(data: pathlib.Path, name: str = "alpha", bucket: str = "external") -> pathlib.Path:
    skill = data / "skills" / bucket / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# {name}\n\nA test skill.\n", encoding="utf-8")
    (skill / "plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    (skill / "notes.txt").write_text("PENDING\n", encoding="utf-8")
    return skill


def _payload_ctx(tmp_path: pathlib.Path, monkeypatch):
    """A genuine TOP-LEVEL context (self_modification profile, no workspace)."""
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    monkeypatch.setenv("OUROBOROS_SUBAGENT_WORKTREE_ROOT", str(tmp_path / "snaps"))
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    configured = {
        "enabled": True,
        "items": [{
            "subagent_id": "payload-session",
            "name": "Payload session",
            "recommended_use": "Edit an exact delegated skill payload.",
            "route": {
                "kind": "agent_session",
                "target_id": "some-route=weak-model",
                "credential_profile_id": "",
            },
            "effort": "low",
        }],
    }
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps(configured))
    ctx = ToolContext(repo_dir=repo, drive_root=data)
    ctx.task_id = "t-payload"
    ctx.task_metadata = {"root_task_id": "t-payload"}
    from ouroboros.subagent_runtime import select_subagent_snapshot

    ctx._payload_subagent_snapshot = select_subagent_snapshot(
        {"OUROBOROS_SUBAGENTS": json.dumps(configured)},
        subagent_id="payload-session",
    )[0]
    return ctx


# -- 1A: custody rows carry the payload binding ---------------------------------


def test_duplicate_started_rows_keep_first_binding_facts(tmp_path):
    drive = tmp_path
    entry = custody.RunCustody(
        run_id="run-d", task_id="t-a", route_id="r",
        snapshot_id="snap1", execution_root="/x/exec", baseline_sha="b1",
        target_root="/x/target", authority_source="skill_payload",
        resource_ref={"skill_name": "alpha", "payload_hash": "h1"})
    custody.record_started(drive, entry, shape={
        "access": "workspace_write", "mode": "agent", "isolation": "live",
        "delegated": True, "root": "/x/exec"})
    # A later idempotent STARTED row minted WITHOUT the binding facts.
    custody.record_started(drive, custody.RunCustody(run_id="run-d", task_id="t-a",
                                                     route_id="r"))
    replayed = custody.replay(drive)["run-d"]
    assert replayed.snapshot_id == "snap1" and replayed.baseline_sha == "b1"
    assert replayed.target_root == "/x/target"
    assert replayed.authority_source == "skill_payload"
    assert replayed.resource_ref["payload_hash"] == "h1"
    assert replayed.access == "workspace_write" and replayed.delegated is True
    custody._CUSTODY.clear()


def test_pending_invocation_and_retry_records_carry_the_resource_ref(tmp_path):
    drive = tmp_path
    ref = {"root": "skill_payload", "source": "external", "skill_name": "alpha",
           "target_root": "/x/target", "payload_hash": "h1"}
    custody.record_start_requested(
        drive, run_id="", task_id="t-a", idempotency_key="k", invocation_id="inv1",
        max_seconds=60, request={"prompt": "x"}, project_id="p", project_owned=False,
        route="r", root_task_id="t-a", parent_task_id="", snapshot_id="snap1",
        execution_root="/x/exec", baseline_sha="b1", target_root="/x/target",
        authority_source="skill_payload", resource_ref=ref)
    record = custody.invocation_record(drive, "inv1")
    assert record["resource_ref"] == ref
    pending = custody.pending_invocations(drive)
    assert pending and pending[0]["resource_ref"] == ref


# -- 1B: standalone snapshot + capture adapter -----------------------------------


def test_snapshot_copies_symlinks_as_symlinks_and_drops_escapes(tmp_path):
    data = tmp_path / "data"
    skill = _seed_skill(data)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink("SKILL.md", skill / "rel.md")                    # confined relative
    os.symlink(str(skill / "plugin.py"), skill / "abs.py")      # confined absolute
    os.symlink(str(outside), skill / "esc.txt")                 # escaping
    handle = provision_payload_snapshot(
        target_root=skill, task_id="t1", snapshot_id="snapS",
        worktree_root=tmp_path / "snaps", data_dir=data)
    snap = pathlib.Path(handle.path)
    assert handle.standalone is True and handle.payload_hash
    assert (snap / "rel.md").is_symlink()
    assert os.readlink(snap / "rel.md") == "SKILL.md"
    assert (snap / "abs.py").is_symlink()
    rewritten = os.readlink(snap / "abs.py")
    assert not os.path.isabs(rewritten)                          # rewritten relative
    assert (snap / "abs.py").resolve() == (snap / "plugin.py").resolve()
    assert not os.path.lexists(snap / "esc.txt")                 # not copied
    record = find_execution_snapshot("snapS", data_dir=data)
    assert record and record["standalone"] is True
    # Standalone cleanup: only the private dir + registry row disappear.
    assert remove_execution_snapshot("snapS", worktree_root=tmp_path / "snaps", data_dir=data)
    assert not snap.exists() and skill.is_dir()


def _payload_entry(handle, skill, *, run_id="run-p1", settled=True):
    entry = custody.RunCustody(
        run_id=run_id, task_id="t-payload", route_id="some-route",
        snapshot_id=handle.snapshot_id, execution_root=handle.path,
        baseline_sha=handle.baseline_sha, target_root=str(skill.resolve()),
        authority_source="skill_payload", settled=settled,
        access="workspace_write", mode="agent", isolation="live", delegated=True,
        resource_ref={"root": "skill_payload", "source": "external",
                      "skill_name": skill.name, "target_root": str(skill.resolve()),
                      "payload_hash": handle.payload_hash})
    custody._CUSTODY[entry.run_id] = entry
    return entry


def _provisioned(tmp_path, monkeypatch, *, name="alpha"):
    ctx = _payload_ctx(tmp_path, monkeypatch)
    skill = _seed_skill(tmp_path / "data", name=name)
    handle = provision_payload_snapshot(
        target_root=skill, task_id="t-payload", snapshot_id="snapP")
    custody._CUSTODY.clear()
    return ctx, skill, handle


def test_capture_transports_utf8_with_nul_and_loader_junk_stays_out(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    # The harness edits the SNAPSHOT: a UTF-8-with-NUL file (git's binary
    # heuristic would veto it in a text diff), a plain edit, a deletion, junk.
    (exec_root / "table.txt").write_bytes("col1\0col2\nrow\0data\n".encode("utf-8"))
    (exec_root / "plugin.py").write_text("VALUE = 2\n", encoding="utf-8")
    (exec_root / "notes.txt").unlink()
    (exec_root / "node_modules").mkdir()
    (exec_root / "node_modules" / "junk.js").write_text("x\n", encoding="utf-8")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text())
    assert manifest["capture_kind"] == "skill_payload"
    assert set(manifest["tracked_changed"]) == {"table.txt", "plugin.py", "notes.txt"}
    assert manifest["blocked_reserved_paths"] == []
    assert manifest["result_content_hash"] and manifest["baseline_payload_hash"]
    assert manifest["result_content_hash"] != manifest["baseline_payload_hash"]
    custody._CUSTODY.clear()


def test_non_utf8_addition_is_a_typed_capture_failure(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    (pathlib.Path(handle.path) / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    assert entry.patch_captured is False
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_CAPTURE_FAILED" in out, out
    assert find_execution_snapshot("snapP") is not None    # snapshot preserved
    custody._CUSTODY.clear()


# -- 1C: parent-only apply ---------------------------------------------------------


def _captured(tmp_path, monkeypatch, edit=None):
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    if edit is None:
        (exec_root / "notes.txt").write_text("DONE\n", encoding="utf-8")
        (exec_root / "extra.txt").write_bytes("nul\0ok\n".encode("utf-8"))
    else:
        edit(exec_root)
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    return ctx, skill, handle, entry, capture


def test_apply_from_a_foreign_cwd_writes_the_live_payload(tmp_path, monkeypatch):
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    assert capture["status"] == "ready_with_changes", capture
    state_dir = tmp_path / "data" / "state" / "skills" / "alpha"
    state_dir.mkdir(parents=True)
    (state_dir / "grants.json").write_text('{"granted": []}\n', encoding="utf-8")
    grants_before = (state_dir / "grants.json").read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old_cwd = os.getcwd()
    os.chdir(elsewhere)   # a foreign process cwd must not misroute the apply
    try:
        out = _integrate_delegated_patch(ctx, "run-p1", "apply", "looks good")
    finally:
        os.chdir(old_cwd)
    assert "✅ Integrated" in out, out
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "DONE\n"
    assert (skill / "extra.txt").read_bytes() == "nul\0ok\n".encode("utf-8")
    assert not (skill / ".git").exists()
    assert entry.patch_disposed == "applied"
    assert find_execution_snapshot("snapP") is None
    # Lifecycle state stays byte-identical; the stale review is a HASH fact.
    assert (state_dir / "grants.json").read_bytes() == grants_before
    assert "STALE" in out and "skill_review" in out
    # The extension-reconcile marker was queued for the mutated skill; the
    # receipt says QUEUED and never claims the reconcile completed (Sol P2-2).
    assert "QUEUED" in out and "reconciled off" not in out, out
    from ouroboros.extension_reconcile_queue import list_extension_reconcile_requests

    requests = list_extension_reconcile_requests(tmp_path / "data")
    assert any(r["skill"] == "alpha" for r in requests), requests
    custody._CUSTODY.clear()


def test_stale_cas_conflict_preserves_material_and_changes_nothing(tmp_path, monkeypatch):
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    (skill / "plugin.py").write_text("VALUE = 99  # drifted\n", encoding="utf-8")
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_CONFLICT" in out, out
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "PENDING\n"
    assert entry.patch_disposed == ""
    assert find_execution_snapshot("snapP") is not None
    assert pathlib.Path(capture["patch_artifact"]).exists()
    custody._CUSTODY.clear()


def test_already_applied_content_disposes_idempotently(tmp_path, monkeypatch):
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    # A crashed earlier attempt landed the patch but never recorded disposition.
    subprocess.run(["git", "apply", capture["patch_artifact"]], cwd=str(skill),
                   capture_output=True, check=True)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "ALREADY carries" in out, out
    assert entry.patch_disposed == "applied"
    assert find_execution_snapshot("snapP") is None
    custody._CUSTODY.clear()


def test_reserved_path_patch_refuses_whole_apply_and_preserves_candidate(tmp_path, monkeypatch):
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    def edit(exec_root):
        (exec_root / "notes.txt").write_text("DONE\n", encoding="utf-8")
        (exec_root / ".clawhub.json").write_text('{"forged": true}\n', encoding="utf-8")

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch, edit=edit)
    assert capture["status"] == "ready_with_changes", capture
    manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text())
    assert manifest["blocked_reserved_paths"] == [".clawhub.json"]
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_RESERVED_PATHS" in out, out
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "PENDING\n"
    assert not (skill / ".clawhub.json").exists()
    assert entry.patch_disposed == ""
    assert pathlib.Path(capture["patch_artifact"]).exists()
    assert find_execution_snapshot("snapP") is not None
    custody._CUSTODY.clear()


def test_moved_or_deleted_target_is_refused_at_apply(tmp_path, monkeypatch):
    import shutil

    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    shutil.rmtree(skill)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "payload_target_unresolved" in out or "payload_target_moved" in out, out
    assert entry.patch_disposed == ""
    assert find_execution_snapshot("snapP") is not None
    custody._CUSTODY.clear()


def test_reject_needs_no_live_target_and_releases_the_snapshot(tmp_path, monkeypatch):
    import shutil

    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    shutil.rmtree(skill)   # owner deleted the skill; reject must still work
    out = _integrate_delegated_patch(ctx, "run-p1", "reject", "not wanted")
    assert "🚫 Rejected" in out, out
    assert entry.patch_disposed == "rejected"
    assert find_execution_snapshot("snapP") is None
    custody._CUSTODY.clear()


# -- registry-level constraint gate ---------------------------------------------


def test_legacy_disabled_claude_code_edit_blocks_the_selector_call(tmp_path, monkeypatch):
    from ouroboros.contracts.task_contract import build_task_contract
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    _seed_skill(data)
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    contract = build_task_contract({"description": "x",
                                    "disabled_tools": ["claude_code_edit"]})
    registry.set_context(ToolContext(repo_dir=repo, drive_root=data,
                                     task_metadata={"task_contract": contract}))
    blocked = registry.execute("delegate_start", {
        "prompt": "x", "root": "skill_payload", "bucket": "external",
        "skill_name": "alpha"})
    assert "RESOURCE_CONSTRAINT_BLOCKED" in blocked and "disabled_tools" in blocked


# -- 5-lane gate fix batch ---------------------------------------------------------


def test_file_replaced_by_escaping_symlink_rides_as_deletion(tmp_path, monkeypatch):
    """Gate fix 1b (reviewer repro): plugin.py → /tmp/.../secret.txt symlink in the
    snapshot must NOT carry stale baseline content or the symlink itself — the
    inventory drops the escape, so the candidate stages the path as a DELETION."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET\n", encoding="utf-8")
    (exec_root / "plugin.py").unlink()
    os.symlink(str(secret), exec_root / "plugin.py")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    patch_bytes = pathlib.Path(capture["patch_artifact"]).read_bytes()
    assert b"deleted file mode 100644" in patch_bytes
    assert b"120000" not in patch_bytes and b"SECRET" not in patch_bytes
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "✅ Integrated" in out, out
    # The live payload never receives the symlink; the honest candidate deleted it.
    assert not (skill / "plugin.py").is_symlink()
    assert not (skill / "plugin.py").exists()
    custody._CUSTODY.clear()


def test_escaping_symlink_patch_is_refused_whole_at_apply(tmp_path, monkeypatch):
    """Gate fix 1a: apply-time containment judges the CANDIDATE — a patch hunk
    introducing a symlink whose target escapes the live payload refuses the
    WHOLE apply with the candidate preserved."""
    import hashlib

    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    assert capture["status"] == "ready_with_changes"
    # Forge a candidate introducing an escaping symlink (crafted in a scratch
    # repo; the guard must judge patch CONTENT, not trust capture provenance).
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "--allow-empty", "-q", "-m", "base"], cwd=scratch, check=True)
    os.symlink("../../secret.txt", scratch / "evil_link")
    subprocess.run(["git", "add", "-A"], cwd=scratch, check=True)
    forged = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD"],
                            cwd=scratch, capture_output=True, check=True).stdout
    patch_path = pathlib.Path(capture["patch_artifact"])
    patch_path.write_bytes(forged)
    manifest_path = pathlib.Path(capture["manifest_artifact"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(forged).hexdigest()
    manifest["tracked_changed"] = ["evil_link"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_RESERVED_PATHS" in out and "evil_link" in out, out
    assert not os.path.lexists(skill / "evil_link")
    assert entry.patch_disposed == ""
    assert patch_path.exists()                      # candidate preserved
    assert find_execution_snapshot("snapP") is not None
    custody._CUSTODY.clear()


def test_child_git_config_diff_driver_does_not_execute_at_capture(tmp_path, monkeypatch):
    """Gate fix 2 (reviewer repro): a snapshot-local `diff.evil.command` written
    by the child must not execute in the PARENT at capture, and the patch must
    carry the real content, not driver output."""
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    pwned = tmp_path / "pwned.txt"
    (exec_root / ".git" / "config").write_text(
        "[diff \"evil\"]\n\tcommand = sh -c 'touch %s; echo'\n" % pwned,
        encoding="utf-8")
    (exec_root / ".gitattributes").write_text("*.py diff=evil\n", encoding="utf-8")
    (exec_root / "plugin.py").write_text("VALUE = 2\n", encoding="utf-8")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    assert not pwned.exists(), "child-controlled git config executed in the parent"
    patch_bytes = pathlib.Path(capture["patch_artifact"]).read_bytes()
    assert b"+VALUE = 2" in patch_bytes
    custody._CUSTODY.clear()


def test_payload_instructions_variant_is_payload_only(tmp_path, monkeypatch):
    """Gate fix 3: ordinary runs keep the blanket ban byte-identically; only a
    payload run gets the narrowed ban plus the explicit permission block."""
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _HOST_INSTRUCTIONS, _host_instructions

    ordinary = _host_instructions(delegated_run_shape(False))
    assert "runtime controls, skills, or memory" in ordinary
    assert "PAYLOAD ASSIGNMENT" not in ordinary
    payload = _host_instructions(delegated_run_shape(True), payload_skill="alpha")
    assert "runtime controls, skills, or memory" not in payload
    assert "runtime controls or memory" in payload
    assert "PAYLOAD ASSIGNMENT" in payload and "'alpha'" in payload
    assert "runtime controls, skills, or memory" in _HOST_INSTRUCTIONS  # source intact


def test_idempotent_already_applied_branch_runs_the_finalizer(tmp_path, monkeypatch):
    """Gate fix 4: the already-applied branch reconciles and reports staleness
    exactly like a fresh apply (it used to dispose and skip both)."""
    from ouroboros.extension_reconcile_queue import list_extension_reconcile_requests
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    subprocess.run(["git", "apply", capture["patch_artifact"]], cwd=str(skill),
                   capture_output=True, check=True)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "ALREADY carries" in out, out
    assert "STALE" in out and "skill_review" in out
    # Sol P2-2: the receipt claims a QUEUED request, never a completed reconcile.
    assert "QUEUED" in out and "reconciled off" not in out, out
    requests = list_extension_reconcile_requests(tmp_path / "data")
    assert any(r["skill"] == "alpha" for r in requests), requests
    custody._CUSTODY.clear()


def test_reconcile_queue_failure_degrades_the_receipt_honestly(tmp_path, monkeypatch):
    """Gate fix 4: a failed reconcile queue-write must not claim the extension
    was reconciled off — the receipt states the failure; the apply stands."""
    import ouroboros.extension_reconcile_queue as reconcile_queue

    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise OSError("queue disk full")

    monkeypatch.setattr(reconcile_queue, "request_extension_reconcile", _boom)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "✅ Integrated" in out and "STALE" in out, out
    assert "could NOT be queued" in out, out
    assert "reconciled off until re-review" not in out, out
    assert entry.patch_disposed == "applied"
    custody._CUSTODY.clear()


def test_snapshot_root_under_runtime_data_refuses_provisioning(tmp_path):
    """Gate fix 6 (reviewer repro): a snapshot root resolving inside the runtime
    data root is refused — no child-writable Git repo inside live state."""
    data = tmp_path / "data"
    skill = _seed_skill(data)
    with pytest.raises(ValueError, match="runtime data"):
        provision_payload_snapshot(
            target_root=skill, task_id="t1", snapshot_id="snapBad",
            worktree_root=data / "state" / "snaps", data_dir=data)
    assert not (data / "state" / "snaps").exists()


def test_registry_save_failure_leaves_no_orphan_snapshot_dir(tmp_path, monkeypatch):
    """Gate fix 7: a registry-write failure removes the snapshot directory —
    an unregistered directory would be invisible to disposal/retention."""
    import ouroboros.subagent_worktrees as worktrees

    data = tmp_path / "data"
    skill = _seed_skill(data)

    def _boom(*_a, **_k):
        raise OSError("registry disk full")

    monkeypatch.setattr(worktrees, "_save_registry", _boom)
    with pytest.raises(OSError, match="registry disk full"):
        provision_payload_snapshot(
            target_root=skill, task_id="t1", snapshot_id="snapReg",
            worktree_root=tmp_path / "snaps", data_dir=data)
    leftovers = list((tmp_path / "snaps").glob("dlgp_*")) if (tmp_path / "snaps").exists() else []
    assert leftovers == [], leftovers


def test_first_wins_is_keyed_on_the_first_shape_carrying_row(tmp_path):
    """Gate fix 8b: a recorded delegated=False survives a later True, and an
    empty resource_ref is never 'filled' by a later row — in BOTH the replay
    and the in-process memo (8a: same merge, no raw replacement)."""
    drive = tmp_path
    custody._CUSTODY.clear()
    first = custody.RunCustody(run_id="run-f", task_id="t-a", route_id="r")
    custody.record_started(drive, first, shape={
        "access": "readonly", "mode": "ask", "isolation": "", "delegated": False})
    # Later duplicate claims a WIDER shape and a filled-in resource_ref.
    later = custody.RunCustody(
        run_id="run-f", task_id="t-a", route_id="r",
        access="workspace_write", mode="agent", isolation="live", delegated=True,
        resource_ref={"skill_name": "late", "payload_hash": "hX"})
    custody.record_started(drive, later, shape={
        "access": "workspace_write", "mode": "agent", "isolation": "live",
        "delegated": True})
    replayed = custody.replay(drive)["run-f"]
    assert replayed.delegated is False and replayed.access == "readonly"
    assert replayed.mode == "ask" and replayed.resource_ref == {}
    status, memo = custody.lookup(drive, "t-a", "run-f")
    assert status == custody.OWNED
    assert memo.delegated is False and memo.access == "readonly"
    assert memo.resource_ref == {}
    # The memo answers EXACTLY what a restart would replay.
    for attr in ("access", "mode", "isolation", "delegated", "resource_ref",
                 "snapshot_id", "target_root", "authority_source"):
        assert getattr(memo, attr) == getattr(replayed, attr), attr
    custody._CUSTODY.clear()


def test_recovered_pending_invocation_object_carries_the_shape(tmp_path, monkeypatch):
    """Gate fix 8c: recovery copies access/mode/isolation/delegated onto the
    in-memory object, not only the resource_ref."""
    drive = tmp_path
    custody._CUSTODY.clear()
    ref = {"root": "skill_payload", "source": "external", "skill_name": "alpha",
           "target_root": "/x/target", "payload_hash": "h1"}
    body = {"prompt": "x", "access": "workspace_write", "mode": "agent",
            "execution": {"isolation": "live", "delegated": True},
            "scope": {"root": "/x/exec"}, "primaryHarness": "r"}
    custody.record_start_requested(
        drive, run_id="", task_id="t-a", idempotency_key="k", invocation_id="invR",
        max_seconds=60, request=body, project_id="p", project_owned=False,
        route="r", root_task_id="t-a", parent_task_id="", snapshot_id="snap1",
        execution_root="/x/exec", baseline_sha="b1", target_root="/x/target",
        authority_source="skill_payload", resource_ref=ref)
    record = custody.pending_invocations(drive)[0]
    monkeypatch.setattr(custody, "_reconcile_one", lambda *_a, **_k: {"ok": True})

    class _Gw:
        def start_run(self, request, *, idempotency_key=""):
            return {"runId": "run-rec"}

    custody._recover_pending_invocation(drive, _Gw(), record)
    obj = custody._CUSTODY["run-rec"]
    assert obj.access == "workspace_write" and obj.mode == "agent"
    assert obj.isolation == "live" and obj.delegated is True
    assert obj.resource_ref == ref and obj.authority_source == "skill_payload"
    custody._CUSTODY.clear()


# -- Sol scope-review fix batch (P1 trust defects, P2 contract gaps) ----------------


def _capture_manifest(capture) -> dict:
    return json.loads(pathlib.Path(capture["manifest_artifact"]).read_text(encoding="utf-8"))


def test_child_forged_index_only_blob_is_invisible_to_capture(tmp_path, monkeypatch):
    """Sol P1-1 (reviewer repro): a non-UTF-8 blob the child staged ONLY into the
    snapshot's own .git/index (absent from the worktree) must not exist for the
    capture — the parent diffs a FRESH parent-owned index seeded from the
    RECORDED baseline commit and never reads child .git/index — while a
    legitimate worktree edit still captures and applies."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    (exec_root / "notes.txt").write_text("DONE\n", encoding="utf-8")  # legitimate edit
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(exec_root),
        input=b"\x00\xff\xfe forged opaque bytes", capture_output=True, env=env, check=True)
    sha = blob.stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"100644,{sha},evil.bin"],
        cwd=str(exec_root), capture_output=True, env=env, check=True)
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    manifest = _capture_manifest(capture)
    assert manifest["tracked_changed"] == ["notes.txt"], manifest
    patch_bytes = pathlib.Path(capture["patch_artifact"]).read_bytes()
    assert b"evil.bin" not in patch_bytes and b"\xff\xfe" not in patch_bytes
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "✅ Integrated" in out, out
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "DONE\n"
    assert not (skill / "evil.bin").exists()
    custody._CUSTODY.clear()


def test_symlinked_git_config_is_typed_capture_failure_without_writethrough(
        tmp_path, monkeypatch):
    """Sol P1-2 (reviewer repro): .git/config -> sentinel must fail capture typed
    and the sentinel must stay byte-identical (nothing writes through the link)."""
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    sentinel = tmp_path / "sentinel.cfg"
    sentinel.write_bytes(b"[core]\n\tsentinel = untouched\n")
    before = sentinel.read_bytes()
    config = exec_root / ".git" / "config"
    config.unlink()
    os.symlink(str(sentinel), config)
    (exec_root / "notes.txt").write_text("DONE\n", encoding="utf-8")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    note = _capture_manifest(capture)["note"]
    assert "snapshot git metadata untrusted" in note and ".git/config" in note
    assert sentinel.read_bytes() == before
    assert (skill / "notes.txt").read_text(encoding="utf-8") == "PENDING\n"
    custody._CUSTODY.clear()


def test_symlinked_git_dir_is_typed_capture_failure(tmp_path, monkeypatch):
    """Sol P1-2: the whole .git replaced by a symlink to an outside directory is
    refused before any parent git operation."""
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    outside = tmp_path / "outside_git"
    shutil.move(str(exec_root / ".git"), str(outside))
    os.symlink(str(outside), exec_root / ".git")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    note = _capture_manifest(capture)["note"]
    assert "snapshot git metadata untrusted" in note
    assert ".git is not a real directory" in note
    custody._CUSTODY.clear()


def test_schema_and_docs_split_git_staging_from_payload_live_apply():
    """Sol P2-3 pin: integrate_delegated_patch's schema and the deep-delegation
    docs describe the Git lane as staged-into-active-root and the payload lane
    as a live non-Git CAS apply — no universal 'staged' claim covers both."""
    from ouroboros.tools.subagent_integration import get_tools

    entry = next(t for t in get_tools() if t.name == "integrate_delegated_patch")
    desc = entry.schema["description"]
    assert "LIVE apply into the non-Git payload" in desc
    assert "nothing is staged into your active root" in desc
    decision = entry.schema["parameters"]["properties"]["decision"]["description"]
    assert "STAGED into your active root" in decision
    assert "applied LIVE into the non-Git payload" in decision
    arch = (pathlib.Path(__file__).resolve().parents[1] / "docs" /
            "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "differs is the staging substrate" in arch
    assert "A SKILL-PAYLOAD target captures through the payload adapter" in arch
    assert "QUEUES the extension reconcile request" in arch


def test_registry_and_custody_baseline_disagreement_fails_capture_typed(
        tmp_path, monkeypatch):
    """Sol P1-1: the baseline identity is the HOST registry's; a custody row that
    disagrees (or a missing registry record) is a typed failure, not a diff
    against whichever sha happens to be replayed."""
    from ouroboros.tools.delegate import _capture_terminal_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    entry = _payload_entry(handle, skill)
    entry.baseline_sha = "0" * 40  # forged/corrupt custody row
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    assert "disagree on the baseline" in _capture_manifest(capture)["note"]
    custody._CUSTODY.clear()


# -- Sol P1 representation batch (raw bytes, modes, post-apply assert) --------------


def test_existing_gitattributes_crlf_edit_transports_raw_bytes(tmp_path, monkeypatch):
    """Sol P1 (reviewer repro a): a payload-authored `.gitattributes text eol=lf`
    must not forge the staged content — a CRLF edit rides RAW, applies cleanly,
    and the live raw bytes equal the candidate with equal loader hashes."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.delegate_integration import payload_content_hash
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx = _payload_ctx(tmp_path, monkeypatch)
    skill = _seed_skill(tmp_path / "data")
    (skill / ".gitattributes").write_text("notes.txt text eol=lf\n", encoding="utf-8")
    handle = provision_payload_snapshot(
        target_root=skill, task_id="t-payload", snapshot_id="snapP")
    custody._CUSTODY.clear()
    (pathlib.Path(handle.path) / "notes.txt").write_bytes(b"DONE\r\nWITH CRLF\r\n")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    manifest = _capture_manifest(capture)
    assert manifest["tracked_changed"] == ["notes.txt"]
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "✅ Integrated" in out, out
    assert (skill / "notes.txt").read_bytes() == b"DONE\r\nWITH CRLF\r\n"
    assert payload_content_hash(skill) == manifest["result_content_hash"]
    custody._CUSTODY.clear()


def test_child_added_gitattributes_and_crlf_file_transport_raw(tmp_path, monkeypatch):
    """Sol P1 (reviewer repro b): a CHILD-added `.gitattributes` is ordinary raw
    content and cannot LF-normalize the sibling CRLF file it names."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.delegate_integration import payload_content_hash
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    (exec_root / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8")
    (exec_root / "table.csv").write_bytes(b"a,b\r\n1,2\r\n")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    manifest = _capture_manifest(capture)
    assert set(manifest["tracked_changed"]) == {".gitattributes", "table.csv"}
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "✅ Integrated" in out, out
    assert (skill / "table.csv").read_bytes() == b"a,b\r\n1,2\r\n"
    assert payload_content_hash(skill) == manifest["result_content_hash"]
    custody._CUSTODY.clear()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows has no POSIX executable bit: os.chmod cannot flip 0644->0755, "
           "so the mode-only divergence this test pins cannot exist there")


def test_exec_bit_only_flip_is_typed_unreviewable_metadata_change(tmp_path, monkeypatch):
    """Sol P1 (reviewer repro c): 0644→0755 with identical bytes is invisible to
    the payload review hash — a typed unreviewable_metadata_change refusal, so
    the stale review can never stay falsely authoritative through a success."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle = _provisioned(tmp_path, monkeypatch)
    exec_root = pathlib.Path(handle.path)
    os.chmod(exec_root / "plugin.py", 0o755)
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    manifest = _capture_manifest(capture)
    assert manifest.get("refusal_kind") == "unreviewable_metadata_change"
    assert manifest.get("normalized_mode_paths") == ["plugin.py"]
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_CAPTURE_FAILED" in out, out
    assert "unreviewable_metadata_change" in out, out
    assert not os.access(skill / "plugin.py", os.X_OK)   # live payload untouched
    assert find_execution_snapshot("snapP") is not None  # snapshot preserved
    custody._CUSTODY.clear()


def test_symlink_topology_change_with_equal_loader_hash_is_typed_refusal(
        tmp_path, monkeypatch):
    """Sol P1 (reviewer repro d): retargeting a confined symlink between two
    equal-content files changes the patch but not the loader hash (it reads
    THROUGH links) — a typed unreviewable_metadata_change refusal, no apply."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx = _payload_ctx(tmp_path, monkeypatch)
    skill = _seed_skill(tmp_path / "data")
    (skill / "a.txt").write_text("same\n", encoding="utf-8")
    (skill / "b.txt").write_text("same\n", encoding="utf-8")
    os.symlink("a.txt", skill / "link.txt")
    handle = provision_payload_snapshot(
        target_root=skill, task_id="t-payload", snapshot_id="snapP")
    custody._CUSTODY.clear()
    exec_root = pathlib.Path(handle.path)
    (exec_root / "link.txt").unlink()
    os.symlink("b.txt", exec_root / "link.txt")
    entry = _payload_entry(handle, skill)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "failed", capture
    manifest = _capture_manifest(capture)
    assert manifest.get("refusal_kind") == "unreviewable_metadata_change"
    assert manifest.get("tracked_changed") == ["link.txt"]
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_CAPTURE_FAILED" in out, out
    assert os.readlink(skill / "link.txt") == "a.txt"    # live payload untouched
    custody._CUSTODY.clear()


def test_post_apply_hash_mismatch_yields_no_success_and_ambiguous_state(
        tmp_path, monkeypatch):
    """Sol P1 (reviewer repro e): if the LIVE loader hash after a real apply is
    not the recorded result hash, no success receipt is emitted, nothing is
    disposed (the stale-extension reconcile marker IS still queued — final Sol
    scope P1), and the PENDING apply intent routes the next integrate to
    the existing APPLY_AMBIGUOUS owner-recovery machinery."""
    import ouroboros.tools.delegate_integration as integration
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    assert capture["status"] == "ready_with_changes", capture
    real = integration.payload_content_hash
    calls = {"n": 0}

    def _hash_diverges_after_apply(root):
        calls["n"] += 1     # call 1 = pre-apply CAS check, call 2 = post-apply
        return "0" * 64 if calls["n"] >= 2 else real(root)

    monkeypatch.setattr(integration, "payload_content_hash", _hash_diverges_after_apply)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_APPLY_HASH_MISMATCH" in out, out
    assert "✅" not in out and "No success is claimed" in out
    assert entry.patch_disposed == ""
    assert find_execution_snapshot("snapP") is not None  # forensics preserved
    from ouroboros.extension_reconcile_queue import list_extension_reconcile_requests

    assert list_extension_reconcile_requests(tmp_path / "data") != []
    monkeypatch.setattr(integration, "payload_content_hash", real)
    again = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_APPLY_AMBIGUOUS" in again, again
    custody._CUSTODY.clear()


def test_apply_hash_mismatch_queues_stale_extension_reconcile_marker(
        tmp_path, monkeypatch):
    """Final Sol scope P1 («Reconcile stale extensions after apply-hash
    mismatch»): the mismatch branch used to return before any reconcile
    queueing, so a stale enabled extension's subscriptions/companions stayed
    live although the payload DID mutate. The marker must be queued WITHOUT
    any success/disposition record, APPLY_AMBIGUOUS routing and the forensic
    material (snapshot + captured patch + verdict) must be preserved."""
    import ouroboros.tools.delegate_integration as integration
    from ouroboros.extension_reconcile_queue import list_extension_reconcile_requests
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    assert capture["status"] == "ready_with_changes", capture
    real = integration.payload_content_hash
    calls = {"n": 0}

    def _hash_diverges_after_apply(root):
        calls["n"] += 1     # call 1 = pre-apply CAS check, call 2 = post-apply
        return "0" * 64 if calls["n"] >= 2 else real(root)

    monkeypatch.setattr(integration, "payload_content_hash", _hash_diverges_after_apply)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_APPLY_HASH_MISMATCH" in out, out
    # The reconcile marker IS set, typed with the mismatch-specific reason.
    requests = list_extension_reconcile_requests(tmp_path / "data")
    assert [r["skill"] for r in requests] == ["alpha"], requests
    assert requests[0]["reason"] == "delegated_payload_apply_hash_mismatch"
    # The receipt reports the QUEUED marker and never a completed reconcile.
    assert "QUEUED" in out and "reconciled off" not in out, out
    # No success/disposition is recorded; forensic custody is intact.
    assert "No success is claimed" in out and "✅" not in out
    assert entry.patch_disposed == ""
    assert find_execution_snapshot("snapP") is not None
    assert pathlib.Path(capture["patch_artifact"]).exists()
    assert "Verdict:" in out, out
    # The durable apply intent stays PENDING: APPLY_AMBIGUOUS answers next.
    monkeypatch.setattr(integration, "payload_content_hash", real)
    again = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_DELEGATED_APPLY_AMBIGUOUS" in again, again
    custody._CUSTODY.clear()


def test_mismatch_reconcile_queue_failure_keeps_honest_ambiguity(
        tmp_path, monkeypatch):
    """A reconcile queue-write failure in the mismatch branch must not fake the
    marker or a success: the receipt reports the failed queueing, nothing is
    disposed, and the PENDING intent still routes to APPLY_AMBIGUOUS."""
    import ouroboros.extension_reconcile_queue as reconcile_queue
    import ouroboros.tools.delegate_integration as integration
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    ctx, skill, handle, entry, capture = _captured(tmp_path, monkeypatch)
    real = integration.payload_content_hash
    calls = {"n": 0}

    def _hash_diverges_after_apply(root):
        calls["n"] += 1
        return "0" * 64 if calls["n"] >= 2 else real(root)

    def _boom(*_a, **_k):
        raise OSError("queue disk full")

    monkeypatch.setattr(integration, "payload_content_hash", _hash_diverges_after_apply)
    monkeypatch.setattr(reconcile_queue, "request_extension_reconcile", _boom)
    out = _integrate_delegated_patch(ctx, "run-p1", "apply", "")
    assert "INTEGRATE_APPLY_HASH_MISMATCH" in out, out
    assert "could NOT be queued" in out and "✅" not in out, out
    assert entry.patch_disposed == ""
    assert find_execution_snapshot("snapP") is not None
    custody._CUSTODY.clear()
