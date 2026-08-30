"""Registration-sweep lifecycle: sharer-aware deferral and discharge.

Split from test_delegated_run_isolation.py (module line cap)."""
from __future__ import annotations

from ouroboros import delegate_custody as custody
def test_registration_sweep_defers_behind_a_live_unowned_sharer(tmp_path):
    """Sharers are ALL runs in a project, owned or not: only the creator
    carries the registration, but the daemon refuses removal while any
    sibling lives - attempting anyway spammed PROJECT_RETIRE_FAILED on
    every sweep tick for the sibling's whole lifetime."""
    dc = custody

    class _Gateway:
        def __init__(self):
            self.removals = []

        def handshake(self, **_kw):
            return {}

        def remove_project(self, pid):
            self.removals.append(pid)

        def close(self):
            pass

    gateway = _Gateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-a", task_id="t-a", route_id="r", model="m",
        project_id="prj-shared", project_owned=True, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-b", task_id="t-b", route_id="r", model="m",
        project_id="prj-shared", project_owned=False, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-a", "task_id": "t-a", "route": "r"})

    # Owner settled, unowned sibling still live: the sweep must not attempt.
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == [], "a live unowned sharer defers the attempt"

    # Sibling settles: the very next sweep discharges the registration.
    dc.emit(tmp_path, dc.SETTLED, {"run_id": "run-b", "task_id": "t-b", "route": "r"})
    dc._CUSTODY.clear()
    dc.retire_settled_registrations(tmp_path, gateway)
    assert gateway.removals == ["prj-shared"]
