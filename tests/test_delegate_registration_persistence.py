"""#362: a persistent (stable-target) registration survives every retire path."""
from __future__ import annotations

from ouroboros import delegate_custody as custody
from ouroboros.delegate_registration_policy import persistent_registration


class _Gateway:
    def __init__(self):
        self.removals = []

    def handshake(self, **_kw):
        return {}

    def remove_project(self, pid):
        self.removals.append(pid)

    def close(self):
        pass


def test_persistent_registration_predicate():
    # Stable execution workspace + the user's own tree => durable identity.
    assert persistent_registration("/home/user/project", "workspace_write") is True
    assert persistent_registration("", "workspace_write") is False  # snapshot route
    assert persistent_registration("/home/user/project", "readonly") is False
    assert persistent_registration("  ", "workspace_write") is False


def test_registration_survives_settlement(tmp_path):
    """The issue's first regression: settling a run over a persistent
    registration must NOT delete the user's project."""
    dc = custody
    gateway = _Gateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-p", task_id="t-p", route_id="r", model="m",
        project_id="prj-user", project_owned=True, project_persistent=True,
        ledger_root=str(tmp_path)))
    row = dc._CUSTODY["run-p"]
    dc.retire_project(tmp_path, gateway, row)
    assert gateway.removals == [], "persistent registration must survive retirement"
    assert row.project_owned is False, "ownership duty is discharged without deletion"

    # The durable row and its replay carry the marker (or-merge keeps it).
    dc._CUSTODY.clear()
    replayed = dc.replay(tmp_path)
    assert replayed["run-p"].project_persistent is True

    # settle_run's summary tells the truth: nothing was retired.
    settled = dc.settle_run(tmp_path, gateway, replayed["run-p"], {
        "summary": {"state": "succeeded"},
    })
    assert settled.get("project_persistent") is True
    assert settled.get("project_retired") is False
    assert gateway.removals == []


def test_pending_recovery_retains_the_marker(tmp_path):
    """The issue's second regression: a pending invocation keeps the marker,
    and the definite-refusal retire path skips a persistent registration."""
    dc = custody
    gateway = _Gateway()
    dc.emit(tmp_path, dc.START_REQUESTED, {
        "invocation_id": "inv-1", "task_id": "t-p", "route": "r",
        "project_id": "prj-user", "project_owned": True,
        "project_persistent": True, "idempotency_key": "inv-1",
        "request": {"model": "m"}, "max_seconds": 60,
    })
    from ouroboros.delegate_pending import pending_invocations

    rows = pending_invocations(tmp_path)
    mine = [r for r in rows if r["invocation_id"] == "inv-1"]
    assert mine and mine[0]["project_persistent"] is True

    record = dict(mine[0])
    assert dc._retire_recovered_registration(gateway, record) is False
    assert gateway.removals == [], "a persistent registration is never deleted on refusal"

    # A plain owned (non-persistent) record still retires — the guard is
    # narrow, not a blanket skip.
    plain = dict(record)
    plain["project_persistent"] = False
    dc._retire_recovered_registration(gateway, plain)
    assert gateway.removals == ["prj-user"]


def test_replacement_refusal_names_retry_of_and_live_run(tmp_path):
    """#364 remainder: the typed refusal must name the live identity and the
    retry_of escape hatch in its human-readable detail."""
    from ouroboros.delegate_start_claims import claimed_start_request

    dc = custody
    dc.emit(tmp_path, dc.START_REQUESTED, {
        "invocation_id": "inv-busy", "task_id": "t-b", "route": "r",
        "project_id": "", "project_owned": False,
        "idempotency_key": "inv-busy", "request": {"model": "m"},
        "max_seconds": 60,
    })
    ok, refusal = claimed_start_request(
        tmp_path, claim_target="", payload_busy=lambda *_a: "",
        enforce_actor_idle=True,
        task_id="t-b", invocation_id="inv-2", route="r",
        request={"model": "m"}, idempotency_key="inv-2",
    )
    assert ok is False
    assert refusal["reason"] == "replacement_requires_settlement"
    assert "retry_of" in refusal["detail"]
    assert "inv-busy" in refusal["detail"]


def test_orphan_refusal_keeps_persistent_registration(tmp_path):
    """The pre-run refusal path is the same class: a definite refusal must not
    delete the user's stable project either (the f9356572 skip, restored)."""
    from ouroboros.tools.delegate import _retire_orphaned_registration

    gateway = _Gateway()

    class _Ctx:
        drive_root = tmp_path
        task_metadata = {}

    out = _retire_orphaned_registration(
        _Ctx(), gateway, "prj-user", project_persistent=True,
        definite_refusal=True, reason="claim_refused", invocation_id="inv-9")
    assert gateway.removals == []
    assert out["project_retention_reason"] == "persistent_registration"
    # The invocation's fate row still LANDS (definite) — a persistent skip
    # must not leave the id forever-pending and livelock the delegate lane.
    rows = [r for r in custody._iter_rows(custody.event_log_path(tmp_path))
            if r.get("type") == custody.START_FAILED and r.get("invocation_id") == "inv-9"]
    assert rows and rows[0].get("definite") is True

    out = _retire_orphaned_registration(
        _Ctx(), gateway, "prj-tmp", project_persistent=False,
        definite_refusal=True, reason="claim_refused", invocation_id="inv-9")
    assert gateway.removals == ["prj-tmp"]


def test_sweep_converges_after_persistent_discharge(tmp_path):
    """C1 regression: the persistent discharge is DURABLE — after replay the
    row is no longer owned, so the sweep stops selecting it forever."""
    dc = custody
    gateway = _Gateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-p", task_id="t-p", route_id="r", model="m",
        project_id="prj-user", project_owned=True, project_persistent=True,
        ledger_root=str(tmp_path)))
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-p", "task_id": "t-p", "route": "r"})
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == []
    dc._CUSTODY.clear()
    replayed = dc.replay(tmp_path)
    assert replayed["run-p"].project_owned is False, "discharge must survive replay"
    assert dc.owned_project_registrations(tmp_path) == []


def test_persistent_sharer_shields_a_plain_creator(tmp_path):
    """M1 regression: ANY persistent sharer makes the project undeletable,
    even when the retiring creator's own row is not persistent."""
    dc = custody
    gateway = _Gateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-ro", task_id="t-ro", route_id="r", model="m",
        project_id="prj-shared", project_owned=True, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-w", task_id="t-w", route_id="r", model="m",
        project_id="prj-shared", project_owned=False, project_persistent=True,
        ledger_root=str(tmp_path)))
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-ro", "task_id": "t-ro", "route": "r"})
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-w", "task_id": "t-w", "route": "r"})
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == [], "a persistent sharer shields the project"


def test_or_merge_keeps_the_marker_across_duplicate_started(tmp_path):
    dc = custody
    base = dict(run_id="run-d", task_id="t-d", route_id="r", model="m",
                project_id="prj-user", ledger_root=str(tmp_path))
    dc.record_started(tmp_path, dc.RunCustody(project_owned=True, project_persistent=True, **base))
    dc.record_started(tmp_path, dc.RunCustody(project_owned=True, project_persistent=False, **base))
    dc._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-d"].project_persistent is True


def test_pre_marker_record_falls_back_to_the_stored_request(tmp_path):
    """Upgrade path THROUGH the projections: a legacy durable row without the
    key must reach recovery with the key ABSENT (not a fabricated False), so
    the stored-request fallback protects the stable project."""
    from ouroboros.delegate_pending import pending_invocations
    from ouroboros.delegate_registration_policy import record_persistent

    dc = custody
    stable_request = {
        "access": "workspace_write",
        "execution": {"isolation": "live", "workspaceRoot": "/home/u/proj"},
    }
    # A pre-marker build wrote NO project_persistent key on the row.
    dc.emit(tmp_path, dc.START_REQUESTED, {
        "invocation_id": "inv-legacy", "task_id": "t-l", "route": "r",
        "project_id": "prj-user", "project_owned": True,
        "idempotency_key": "inv-legacy", "request": stable_request,
        "max_seconds": 60,
    })
    record = next(r for r in pending_invocations(tmp_path)
                  if r["invocation_id"] == "inv-legacy")
    assert "project_persistent" not in record, "projection must not fabricate the key"
    assert record_persistent(record) is True
    # The custody twin (the _RetryBinding path) must propagate absence too.
    custody_record = dc.invocation_record(tmp_path, "inv-legacy")
    assert "project_persistent" not in custody_record
    assert record_persistent(custody_record) is True

    gateway = _Gateway()
    assert dc._retire_recovered_registration(gateway, record) is False
    assert gateway.removals == [], "legacy stable registration survives recovery refusal"

    # Direct predicate table (authoritative key beats derivation).
    assert record_persistent({"project_persistent": False, "request": stable_request}) is False
    assert record_persistent({"project_persistent": True}) is True
    assert record_persistent({"request": {"access": "workspace_write",
                                          "execution": {"isolation": "live"}}}) is False
    assert record_persistent({}) is False
