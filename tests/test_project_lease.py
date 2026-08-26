"""Multi-project lease + registry + chat-id policy (v6.32.0)."""

from __future__ import annotations

from ouroboros.contracts.chat_id_policy import (
    PROJECT_CHAT_ID_MIN,
    WEB_UI_CHAT_ID,
    is_a2a_chat_id,
    is_project_chat_id,
    project_chat_id,
)
from ouroboros.project_lease import candidate_is_leasable, running_project_ids


def _task(project_id="", role="", tid="t1"):
    task = {"id": tid, "type": "task"}
    if project_id:
        task["project_id"] = project_id
    if role:
        task["delegation_role"] = role
    return task


def _meta(task):
    """Production RUNNING value shape: meta dict wrapping the task."""
    return {"task": task, "worker_id": 0, "last_heartbeat_at": 1.0}


def test_running_project_ids_counts_top_level_scoped_tasks_only():
    # Mix the PRODUCTION meta shape (workers.py RUNNING values) with bare task
    # dicts — running_project_ids must unwrap meta and still count both.
    running = [
        _meta(_task("alpha")),               # production shape
        _task("beta"),                       # bare task dict
        _meta(_task("", tid="plain")),       # unscoped: no lane
        _meta(_task("gamma", role="subagent")),  # swarm member: no lease of its own
        "garbage",
        None,
    ]
    assert running_project_ids(running) == {"alpha", "beta"}


def test_running_project_ids_unwraps_production_meta_shape():
    """Regression for the inert-lease bug: RUNNING.values() are meta dicts."""
    running = {"t1": _meta(_task("racer"))}.values()
    ids = running_project_ids(running)
    assert ids == {"racer"}
    assert candidate_is_leasable(_task("racer", tid="t2"), ids) is False


def test_candidate_is_leasable_matrix():
    leased = {"alpha"}
    # Unscoped tasks never serialize.
    assert candidate_is_leasable(_task(""), leased) is True
    # A second writer for a leased project waits.
    assert candidate_is_leasable(_task("alpha"), leased) is False
    # A different project proceeds in parallel.
    assert candidate_is_leasable(_task("beta"), leased) is True
    # The leased project's OWN subagents must not deadlock the swarm.
    assert candidate_is_leasable(_task("alpha", role="subagent"), leased) is True


def test_project_chat_id_policy():
    assert is_project_chat_id(WEB_UI_CHAT_ID) is False
    assert is_project_chat_id(-5) is False
    cid = project_chat_id("my-game")
    assert cid >= PROJECT_CHAT_ID_MIN
    assert is_project_chat_id(cid) is True
    assert is_a2a_chat_id(cid) is False
    # Deterministic and id-sensitive.
    assert project_chat_id("my-game") == cid
    assert project_chat_id("other") != cid
    # Empty scope falls back to the main chat.
    assert project_chat_id("") == WEB_UI_CHAT_ID


def test_registry_create_idempotent_and_summary(tmp_path):
    from ouroboros.projects_registry import (
        create_project,
        get_project,
        list_projects,
        projects_summary,
    )

    entry = create_project(tmp_path, "racer", name="Cyber Racer")
    assert entry["id"] == "racer"
    assert "status" not in entry  # statuses removed (v6.33.0)
    assert entry["chat_id"] == project_chat_id("racer")

    again = create_project(tmp_path, "racer", name="ignored on existing")
    assert again["name"] == "Cyber Racer"
    assert len(list_projects(tmp_path)) == 1

    rows = projects_summary(tmp_path)
    assert rows and rows[0]["id"] == "racer" and rows[0]["chat_id"] == entry["chat_id"]
    assert "status" not in rows[0]
    assert get_project(tmp_path, "missing") is None


def test_registry_reconcile_registers_existing_stores_never_prunes(tmp_path):
    from ouroboros.projects_registry import create_project, list_projects, reconcile_projects

    create_project(tmp_path, "kept")
    (tmp_path / "projects" / "legacy-store" / "knowledge").mkdir(parents=True)

    added = reconcile_projects(tmp_path)

    assert added == 1
    ids = {p["id"] for p in list_projects(tmp_path)}
    assert ids == {"kept", "legacy-store"}
    # Second run is a no-op (idempotent) and nothing is pruned.
    assert reconcile_projects(tmp_path) == 0
    assert {p["id"] for p in list_projects(tmp_path)} == ids


_GHOST_ID = "proj_deadbeef1234"
_TOMB_ID = "proj_deadtomb0001"


def _store(drive_root, project_id):
    """Materialize the on-disk shape project_facts creates for a project store."""
    (drive_root / "projects" / project_id / "knowledge").mkdir(parents=True)


def _write_bindings(drive_root, rows):
    """Write the durable task->project bindings file directly.

    bind_task_to_project would CREATE the registry row it binds to, which is
    exactly the state these tests must not have: the guard only matters for a
    store whose registry row is missing.
    """
    import json

    state = drive_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "project_task_bindings.json").write_text(
        json.dumps({"bindings": rows}), encoding="utf-8"
    )


def _read_bindings(drive_root):
    import json

    return json.loads(
        (drive_root / "state" / "project_task_bindings.json").read_text(encoding="utf-8")
    )["bindings"]


def _binding(project_id, task_id="t-1"):
    return {
        "task_id": task_id,
        "project_id": project_id,
        "project_chat_id": project_chat_id(project_id),
        "bound_at": "2026-08-26T00:00:00Z",
        "origin_absent": "post_hoc_unresolved",
    }


def test_reconcile_skips_workspace_derived_store_without_a_binding(tmp_path):
    """A proj_<hash> store minted by project_facts for a workspace path is not a
    project room: with no durable binding it must never become a sidebar row."""
    from ouroboros.projects_registry import list_projects, reconcile_projects

    _store(tmp_path, _GHOST_ID)

    assert reconcile_projects(tmp_path) == 0
    assert list_projects(tmp_path) == []
    # Idempotent: the ghost is skipped again on the next 300s tick, never pruned.
    assert reconcile_projects(tmp_path) == 0
    assert (tmp_path / "projects" / _GHOST_ID / "knowledge").is_dir()


def test_reconcile_registers_workspace_derived_store_with_a_binding(tmp_path):
    """A durable task binding is the proof of user ownership that admits the
    store — the registry-row-lost recovery case."""
    from ouroboros.projects_registry import list_projects, reconcile_projects

    _store(tmp_path, _GHOST_ID)
    _write_bindings(tmp_path, {"t-1": _binding(_GHOST_ID)})

    assert reconcile_projects(tmp_path) == 1
    assert {p["id"] for p in list_projects(tmp_path)} == {_GHOST_ID}


def test_reconcile_still_registers_named_store_beside_a_skipped_ghost(tmp_path):
    """The guard is prefix-scoped: an ordinary named store registers exactly as
    before, even while an unbound proj_ store in the same scan is skipped."""
    from ouroboros.projects_registry import list_projects, reconcile_projects

    _store(tmp_path, "legacy-store")
    _store(tmp_path, _GHOST_ID)

    assert reconcile_projects(tmp_path) == 1
    assert {p["id"] for p in list_projects(tmp_path)} == {"legacy-store"}


def test_reconcile_never_prunes_existing_rows_while_skipping_a_ghost(tmp_path):
    """Function contract: reconcile only ever ADDS. A pass that skips a ghost
    must leave every pre-existing row untouched."""
    from ouroboros.projects_registry import create_project, list_projects, reconcile_projects

    create_project(tmp_path, "kept", name="Kept Project")
    _store(tmp_path, _GHOST_ID)

    assert reconcile_projects(tmp_path) == 0
    rows = list_projects(tmp_path)
    assert [p["id"] for p in rows] == ["kept"]
    assert rows[0]["name"] == "Kept Project"


def test_reconcile_survives_malformed_binding_rows(tmp_path):
    """A malformed legacy binding row must fail closed for ITSELF only: the
    reconcile completes, named stores still register, unbound ghosts stay out."""
    from ouroboros.projects_registry import list_projects, reconcile_projects

    _store(tmp_path, "legacy-store")
    _store(tmp_path, _GHOST_ID)
    _store(tmp_path, "proj_00000000cafe")
    _write_bindings(tmp_path, {
        "t-str": "not-a-dict",
        "t-noid": {"task_id": "t-noid", "bound_at": "2026-08-26T00:00:00Z"},
        "t-null": {"task_id": "t-null", "project_id": None},
        "t-ok": _binding(_GHOST_ID, task_id="t-ok"),
    })

    # legacy-store (named) + the ONE ghost a valid row vouches for.
    assert reconcile_projects(tmp_path) == 2
    assert {p["id"] for p in list_projects(tmp_path)} == {"legacy-store", _GHOST_ID}


def test_reconcile_leaves_a_tombstoned_workspace_store_tombstoned(tmp_path):
    """Deletion PRESERVES bindings, so a tombstoned proj_ store still carries
    the very binding this guard accepts as ownership proof — the guard alone
    would re-admit it. The pre-existing `known` check runs FIRST and is what
    keeps it dead: a tombstoned id is never resurrected as active."""
    from ouroboros.projects_registry import (
        PROJECT_TOMBSTONED,
        begin_project_deletion,
        complete_project_deletion,
        create_project,
        list_projects,
        list_reserved_projects,
        reconcile_projects,
    )

    create_project(tmp_path, _TOMB_ID)
    _store(tmp_path, _TOMB_ID)
    _write_bindings(tmp_path, {"t-tomb": _binding(_TOMB_ID, task_id="t-tomb")})
    begin_project_deletion(tmp_path, _TOMB_ID)
    complete_project_deletion(tmp_path, _TOMB_ID)

    # The binding survived the deletion (nothing removed it).
    assert _read_bindings(tmp_path)["t-tomb"]["project_id"] == _TOMB_ID

    assert reconcile_projects(tmp_path) == 0
    assert list_projects(tmp_path) == []
    reserved = {p["id"]: p["lifecycle"] for p in list_reserved_projects(tmp_path)}
    assert reserved == {_TOMB_ID: PROJECT_TOMBSTONED}


def test_journal_and_workpad_roundtrip(tmp_path, monkeypatch):
    import types

    # Scope the project store to tmp_path WITHOUT importlib.reload(config): a
    # reload permanently rebinds ouroboros.config.DATA_DIR for the rest of the
    # pytest process (monkeypatch restores only the env var, not the reloaded
    # module), polluting later tests. project_facts reads config.DATA_DIR at call
    # time, so monkeypatch.setattr (auto-restored) is sufficient and isolated.
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    from ouroboros.tools import project_journal as pj

    ctx = types.SimpleNamespace(project_id="racer", task_id="t-9", drive_root=tmp_path)
    tools = {t.name: t for t in pj.get_tools()}

    out = tools["journal_write"].handler(ctx, kind="start", text="Bootstrapping the racer")
    assert out.startswith("OK:")
    out = tools["journal_write"].handler(ctx, kind="bogus", text="x")
    assert "TOOL_ARG_ERROR" in out
    listing = tools["journal_read"].handler(ctx)
    assert "Bootstrapping the racer" in listing and "START" in listing

    assert tools["workpad_write"].handler(ctx, content="## plan\n- wheels").startswith("OK:")
    assert "wheels" in tools["workpad_read"].handler(ctx)

    digest = pj.journal_tail_digest("racer")
    assert "Bootstrapping the racer" in digest

    # Unscoped ctx without explicit id refuses honestly.
    bare = types.SimpleNamespace(project_id="", task_id="t", drive_root=tmp_path)
    assert "no project scope" in tools["journal_write"].handler(bare, kind="note", text="x")
