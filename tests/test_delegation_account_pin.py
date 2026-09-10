"""Unified-accounts sprint: the Delegation account pin (D-U5/D-U6, §K.7, §L.4).

The config-grammar half of the pin: `OUROBOROS_SUBAGENT_PROFILE` as a sibling
key of the route grammar, and the strict per-subject exhaustion verdict. The
transport half — the wire `credentialProfileId`, the custody row and the
requested-vs-applied receipt — retired with the Claudexor gateway.
"""

from __future__ import annotations

from ouroboros import subagents


def test_the_account_pin_is_a_sibling_key_folded_into_the_route(monkeypatch):
    """Unified-accounts sprint (D-U5, frozen contract §L.4): the OPTIONAL account
    pin lives in its OWN key, OUROBOROS_SUBAGENT_PROFILE, read ONLY here — the
    route grammar (`harness[=model][:effort]`) gains no fourth position, so no
    other parser of that grammar moves. Empty pin = the engine's rotation pool
    (D28), and a pin with no route pins nothing."""
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=some-model:high")
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROFILE", "koshak")
    route = subagents.get_subagent_harness()
    assert route == subagents.DelegationRoute(
        "some-route", "some-model", "high", profile_id="koshak")

    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROFILE", "")
    assert subagents.get_subagent_harness().profile_id == ""

    # A pin without a route is not a route: delegation stays off.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "")
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROFILE", "koshak")
    assert subagents.get_subagent_harness() is None


def test_a_pinned_route_is_judged_by_its_own_subject_exactly():
    """Unified-accounts sprint (§K.7, D-U6 strict pin): with a non-empty account
    pin the run can only ever land on THAT subject, so a healthy sibling must
    not vouch a spent pinned account into a dispatch the engine is certain to
    refuse — the exact inverse of the harness-wide rule, which stays in force
    for automatic rotation (empty pin). Every fail-open rule holds per subject:
    an unreadable pinned quota is UNKNOWN, not spent."""
    from ouroboros.subagents import _exhausted_window

    def _snap(profile, *, spent, reset="2026-08-03T12:00:00Z"):
        constraint = ({"used_ratio": 1.0, "resets_at": reset} if spent
                      else {"used_ratio": 0.4, "resets_at": reset})
        return {"subject": {"harness": "some-route", "subject_id": profile},
                "freshness": "fresh", "constraints": [constraint]}

    class _Quota:
        def __init__(self, snaps, absences=None):
            self._snaps, self._absences = snaps, absences
        def quota_snapshots(self): return self._snaps
        def quota_absences(self): return self._absences or []

    spent_pin_live_sibling = _Quota([_snap("koshak", spent=True),
                                     _snap("acct-b", spent=False)])
    # Harness-wide (unpinned): the live sibling keeps the route usable.
    assert _exhausted_window(spent_pin_live_sibling, "some-route") == (False, "")
    # Pinned to the spent account: the sibling cannot vouch for it.
    assert _exhausted_window(spent_pin_live_sibling, "some-route",
                             "", "koshak") == (True, "2026-08-03T12:00:00Z")
    # Pinned to the live account while the sibling is spent: healthy.
    assert _exhausted_window(_Quota([_snap("koshak", spent=False),
                                     _snap("acct-b", spent=True)]),
                             "some-route", "", "koshak") == (False, "")
    # A pinned account with NO readable snapshot is unknown, not spent —
    # whatever the siblings say (positive-evidence rule, per subject).
    assert _exhausted_window(_Quota([_snap("acct-b", spent=True)]),
                             "some-route", "", "koshak") == (False, "")
    # A typed absence row for the pinned subject fail-opens it exactly as the
    # harness-wide reader does for the whole route…
    absence = {"subject": {"harness": "some-route", "subject_id": "koshak"}}
    assert _exhausted_window(_Quota([_snap("koshak", spent=True)], [absence]),
                             "some-route", "", "koshak") == (False, "")
    # …and a sibling's absence says nothing about the pinned subject.
    sibling_absence = {"subject": {"harness": "some-route", "subject_id": "acct-b"}}
    assert _exhausted_window(_Quota([_snap("koshak", spent=True)], [sibling_absence]),
                             "some-route", "", "koshak") == (True, "2026-08-03T12:00:00Z")


