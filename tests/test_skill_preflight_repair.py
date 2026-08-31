"""#335: the typed preflight_failed fact behind the skill card Repair action.

A deterministic preflight FAIL persists as review_status=pending; the UI used
to offer Review/Re-review, which deterministically fails the same way. The
gate now carries ``preflight_failed`` when the caller has the persisted
findings — never fabricated when it cannot know.
"""

from __future__ import annotations

from ouroboros.skill_review_status import preflight_failed, skill_review_gate

_PREFLIGHT_FAIL = {
    "item": "skill_preflight",
    "verdict": "FAIL",
    "severity": "critical",
    "reason": '{"manifest": [{"item": "manifest_entry_exists", "ok": false}], "ok": false}',
    "model": "deterministic_preflight",
}


def test_preflight_predicate_matches_the_persisted_finding_shape():
    assert preflight_failed([_PREFLIGHT_FAIL]) is True
    assert preflight_failed([{"item": "skill_preflight", "verdict": "PASS"}]) is False
    assert preflight_failed([{"item": "other", "verdict": "FAIL"}]) is False
    # The model marker alone is enough (mirror of the pending aggregation).
    assert preflight_failed([{"verdict": "FAIL", "model": "deterministic_preflight"}]) is True
    assert preflight_failed([]) is False
    assert preflight_failed(None) is False
    assert preflight_failed(["not-a-dict"]) is False


def test_gate_carries_the_fact_only_when_findings_are_known():
    with_fact = skill_review_gate("pending", findings=[_PREFLIGHT_FAIL])
    assert with_fact["preflight_failed"] is True
    assert with_fact["executable_review"] is False

    # A STALE review's persisted failure belongs to the previous payload
    # bytes: the owner may have fixed it by hand, and Re-review (which reruns
    # the preflight) is the honest cheap action — not Repair.
    stale = skill_review_gate("pending", stale=True, findings=[_PREFLIGHT_FAIL])
    assert stale["preflight_failed"] is False

    benign = skill_review_gate("pending", findings=[])
    assert benign["preflight_failed"] is False

    # Absence is a fact: a status-only caller must not fabricate False —
    # older/leaner producers simply do not emit the key.
    status_only = skill_review_gate("pending")
    assert "preflight_failed" not in status_only


def test_gate_shape_is_otherwise_unchanged():
    base = skill_review_gate("pending")
    extended = skill_review_gate("pending", findings=[])
    assert set(extended) - set(base) == {"preflight_failed"}
    for key in base:
        assert base[key] == extended[key]


def test_owner_attestation_never_bypasses_a_failed_preflight(monkeypatch, tmp_path):
    """#335 acceptance: Skip review NEVER bypasses the deterministic preflight
    — a FAIL outcome is returned verbatim (pending, carrying the finding) and
    no CLEAN verdict or marker is produced."""
    import types

    import ouroboros.skill_owner_attestation as soa
    import ouroboros.skill_review as sr

    failed = sr.SkillReviewOutcome(
        skill_name="s", status=sr.STATUS_PENDING, content_hash="hash",
        findings=[_PREFLIGHT_FAIL],
    )
    monkeypatch.setattr(sr, "_run_deterministic_preflight", lambda *a, **k: failed)
    ctx = types.SimpleNamespace(drive_root=str(tmp_path))
    out = soa.run_owner_attestation(ctx, tmp_path, types.SimpleNamespace(name="s"), "hash")
    assert out is failed
    assert out.status == sr.STATUS_PENDING
    assert preflight_failed(out.findings) is True
