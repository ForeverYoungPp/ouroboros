"""Max-Review-Cycles fix round: dispatch-time paid accounting and the
authority-preserving attempt-ledger eviction.

Contract under test (accepted panel fixes F1-F3, F4a, fable P3-2/P3-3):
* F1 — trimming the commit-attempt ledger never evicts paid rows or the
  verdict anchors of the identical-diff refusal streak: a capped root cannot
  loop free refusals until the ceiling and the quoted verdict forget
  themselves;
* F2 — the commit gate's paid fact lands WRITE-AHEAD at the first PHYSICAL
  reviewer dispatch (either side): an attempt where both packs refuse at
  assembly spends $0 and consumes no ceiling cycle, while one dispatched side
  makes the cycle paid;
* F3 — skill review shares ONE durable dispatch marker between lifecycle and
  direct callers: every dispatched wave (substantive verdict, quorum failure,
  transport failure, crash-after-dispatch) yields exactly one paid unit in
  the derived count, on a REAL history jsonl;
* F4a — a rebuttal's "spent" memory is scoped to the CURRENT panel-contract
  fingerprint;
* P3-3 — under advisory enforcement each free-replay reason drives the full
  stage cycle to a passing commit with its own honest progress note and a
  disclosure that lands in the commit result formatting.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import types

import pytest

from tests._shared import ensure_claude_agent_sdk_mock

ensure_claude_agent_sdk_mock()

KEY = "OUROBOROS_REVIEW_MAX_CYCLES"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv("OUROBOROS_REVIEW_ENFORCEMENT", raising=False)
    yield


# ---------------------------------------------------------------------------
# F1 — authority-preserving attempt-ledger eviction


def test_free_refusal_flood_cannot_evict_paid_rows_or_the_verdict_anchor(
    tmp_path, monkeypatch,
):
    """60+ free refusals after the cap is reached change NOTHING: the paid
    count stays, and the identical-diff refusal still quotes the original
    verdict — only the refusal noise is trimmed."""
    import ouroboros.tools.git as git_mod
    from ouroboros.review_state import (
        CommitAttemptRecord,
        load_state,
        make_repo_key,
        update_state,
        _utc_now,
    )
    from ouroboros.tools.commit_gate import count_paid_review_cycles

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv(KEY, "2")
    monkeypatch.setattr(git_mod, "commit_review_contract_fingerprint", lambda: "cf-1")
    monkeypatch.setattr(git_mod, "run_cmd", lambda *a, **k: "")
    monkeypatch.setattr(git_mod, "_authorized_managed_update_resolver", lambda ctx: False)
    (pathlib.Path(tmp_path) / "logs").mkdir(parents=True, exist_ok=True)
    ctx = types.SimpleNamespace(
        repo_dir=tmp_path, drive_root=pathlib.Path(tmp_path), task_id="root-1",
        task_metadata={}, event_queue=None,
        drive_logs=lambda: pathlib.Path(tmp_path) / "logs",
        _current_review_tool_name="commit_reviewed",
    )
    repo_key = make_repo_key(pathlib.Path(tmp_path))

    def _seed(state):
        for attempt, (status, phase, paid) in enumerate(
            [("succeeded", "commit", True), ("blocked", "blocking_review", True)], start=1,
        ):
            state.attempts.append(CommitAttemptRecord(
                ts=_utc_now(), commit_message="m", status=status, phase=phase,
                block_reason="critical_findings" if status == "blocked" else "",
                block_class="verdict" if status == "blocked" else "",
                critical_findings=(
                    [{"item": "bug_original", "reason": "the anchor", "severity": "critical"}]
                    if status == "blocked" else []
                ),
                repo_key=repo_key, tool_name="commit_reviewed", task_id="root-1",
                attempt=attempt, paid=paid, root_task_id="root-1",
                pre_review_fingerprint="fp-1",
                review_contract_fingerprint="cf-1",
            ))

    update_state(pathlib.Path(tmp_path), _seed)
    assert count_paid_review_cycles(ctx, root_task_id="root-1") == 2

    for _ in range(60):
        outcome = git_mod._free_cycle_gate(
            ctx, "msg", 0.0, pre_fingerprint={"fingerprint": "fp-1"}, review_rebuttal="",
        )
        assert outcome is not None and outcome["status"] == "blocked"
        assert outcome["block_reason"] == "identical_diff_refused"
        assert "bug_original" in outcome["message"]  # the anchor is still quoted

    assert count_paid_review_cycles(ctx, root_task_id="root-1") == 2
    state = load_state(pathlib.Path(tmp_path))
    rows = state.filter_attempts(repo_key=repo_key)
    # The authority rows survived the flood; the noise portion stayed capped.
    assert any(r.paid and r.status == "succeeded" for r in rows)
    assert any(r.block_class == "verdict" and r.pre_review_fingerprint == "fp-1" for r in rows)
    assert len([r for r in rows if not r.paid and r.block_class != "verdict"]) <= 50


def test_ledger_growth_stays_bounded_while_accounting_authority_survives(tmp_path):
    """F1 follow-up (strip-not-evict): flooding far past the 50-row window with
    HEAVY paid rows plus refusal noise must keep the serialized ledger bounded
    — over-window preserved rows lose their raw payloads (raw_stripped=True) —
    while every accounting fact and the refusal-quote behavior survive."""
    from ouroboros.review_state import (
        CommitAttemptRecord,
        load_state,
        make_repo_key,
        update_state,
        _utc_now,
    )
    from ouroboros.tools.commit_gate import (
        check_identical_verdict_refusal,
        count_paid_review_cycles,
    )

    repo_key = make_repo_key(pathlib.Path(tmp_path))
    heavy_raw = "HEAVYRAWX" * 600  # ~5.4KB per payload field

    def _flood(state):
        for i in range(1, 61):
            # A paid verdict-blocked wave with FULL forensic payloads …
            state.record_attempt(CommitAttemptRecord(
                ts=_utc_now(), commit_message="m" * 400, status="blocked",
                block_reason="critical_findings", block_class="verdict",
                block_details="details " + heavy_raw,
                repo_key=repo_key, tool_name="commit_reviewed", task_id="root-1",
                attempt=2 * i - 1, phase="blocking_review", paid=True,
                root_task_id="root-1", pre_review_fingerprint="fp-1",
                review_contract_fingerprint="cf-1",
                critical_findings=[{"item": "bug_anchor", "reason": "still broken",
                                    "severity": "critical"}],
                triad_raw_results=[{"model_id": "m1", "raw_text": heavy_raw}],
                scope_raw_result={"status": "responded", "raw_text": heavy_raw,
                                  "raw_results": [{"status": "responded",
                                                   "critical_findings": [{"item": "x"}]}]},
            ))
            # … followed by a free-refusal noise row.
            state.record_attempt(CommitAttemptRecord(
                ts=_utc_now(), commit_message="m", status="blocked",
                block_reason="identical_diff_refused", block_details="refused",
                repo_key=repo_key, tool_name="commit_reviewed", task_id="root-1",
                attempt=2 * i, phase="preflight", pre_review_fingerprint="fp-1",
                review_contract_fingerprint="cf-1",
            ))

    update_state(pathlib.Path(tmp_path), _flood)
    ctx = types.SimpleNamespace(
        repo_dir=tmp_path, drive_root=pathlib.Path(tmp_path), task_id="root-1",
        task_metadata={}, _current_review_tool_name="commit_reviewed",
    )
    # Every paid dispatch is still counted; the refusal still quotes the verdict.
    assert count_paid_review_cycles(ctx, root_task_id="root-1") == 60
    refusal = check_identical_verdict_refusal(ctx, "fp-1", contract_fingerprint="cf-1")
    assert "IDENTICAL_DIFF_REFUSED" in refusal and "bug_anchor" in refusal
    state = load_state(pathlib.Path(tmp_path))
    rows = state.filter_attempts(repo_key=repo_key)
    over_window, in_window = rows[:-50], rows[-50:]
    assert len(rows) > 50 and over_window  # preservation forced past the cap
    for row in over_window:
        assert row.raw_stripped is True
        assert row.triad_raw_results == [] and row.scope_raw_result == {}
        assert len(row.block_details) <= 700 and len(row.commit_message) <= 400
        # The accounting facts are intact on every compacted row.
        assert row.paid is True and row.block_class == "verdict"
        assert row.root_task_id == "root-1"
        assert row.review_contract_fingerprint == "cf-1"
        assert row.critical_findings and row.critical_findings[0]["item"] == "bug_anchor"
    # The serialized ledger carries full raw payloads ONLY inside the window:
    # every heavy-sentinel occurrence is accounted for by in-window rows, and
    # each compacted over-window row serializes to a small bounded record —
    # the immortal portion grows ~O(preserved rows x small record), never by
    # full reviewer raw output per reviewed commit.
    import dataclasses

    raw = (pathlib.Path(tmp_path) / "state" / "advisory_review.json").read_text(encoding="utf-8")
    heavy_in_window = sum(
        1 for row in in_window if row.triad_raw_results or row.scope_raw_result
    )
    assert heavy_in_window <= 50
    in_window_sentinels = sum(
        json.dumps(dataclasses.asdict(row)).count("HEAVYRAWX" * 600) for row in in_window
    )
    assert raw.count("HEAVYRAWX" * 600) == in_window_sentinels > 0
    for row in over_window:
        assert len(json.dumps(dataclasses.asdict(row))) < 4_000


# ---------------------------------------------------------------------------
# F2 / P3-3 — the write-ahead paid stamp on the real stage cycle


def _stage_cycle_harness(tmp_path, monkeypatch, *, fingerprint):
    """A REAL git repo with a stageable change plus the heavy collaborators
    (advisory gate, binding, fingerprint) pinned, so _run_reviewed_stage_cycle
    runs its true order: free gate -> advisory gate -> dispatch."""
    import ouroboros.tools.git as git_mod

    repo = pathlib.Path(tmp_path) / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")

    drive = pathlib.Path(tmp_path) / "drive"
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    progress: list = []
    ctx = types.SimpleNamespace(
        repo_dir=repo, drive_root=drive, task_id="root-1", task_metadata={},
        event_queue=None, branch_dev="dev",
        drive_logs=lambda: drive / "logs",
        emit_progress_fn=progress.append,
        _current_review_tool_name="commit_reviewed",
        _review_advisory=[],
        _last_triad_models=[], _last_scope_model="",
        _last_triad_raw_results=[], _last_scope_raw_result={},
        _review_degraded_reasons=[],
    )
    monkeypatch.setattr(git_mod, "commit_review_contract_fingerprint", lambda: "cf-1")
    monkeypatch.setattr(
        git_mod, "_fingerprint_staged_diff",
        lambda repo_dir: {"ok": True, "fingerprint": fingerprint},
    )
    monkeypatch.setattr(git_mod, "_advisory_and_tests_gate", lambda *a, **k: None)
    monkeypatch.setattr(git_mod, "_review_binding_precondition_error", lambda *a, **k: "")
    return git_mod, ctx, progress


def _overflow_wave(dispatch):
    """A wave whose BOTH sides land infra-blocked; ``dispatch`` controls
    whether the (real) transport seam was reached before the refusal."""
    from ouroboros.review_dispatch import stamp_review_paid_on_dispatch
    from ouroboros.tools.scope_review import ScopeReviewResult

    def _wave(ctx, commit_message, **kwargs):
        if dispatch:
            stamp_review_paid_on_dispatch(ctx)  # simulate the route-executor seam
        ctx._last_review_block_reason = "fixed_overflow"
        ctx._last_review_critical_findings = []
        scope = ScopeReviewResult(
            blocked=True,
            block_message="⚠️ SCOPE_REVIEW_BLOCKED: pack did not assemble.",
            status="fixed_overflow",
        )
        return "⚠️ REVIEW_BLOCKED: prompt cannot fit.", scope, "fixed_overflow", []

    return _wave


def test_all_assembly_refused_attempt_stays_unpaid(tmp_path, monkeypatch):
    """F2(a): BOTH packs refusing at assembly ($0 spent) must not consume a
    ceiling cycle — with the default cap this used to exhaust a root for free."""
    from ouroboros.tools.commit_gate import count_paid_review_cycles

    git_mod, ctx, _progress = _stage_cycle_harness(tmp_path, monkeypatch, fingerprint="fp-a")
    monkeypatch.setattr(git_mod, "_run_parallel_review", _overflow_wave(dispatch=False))
    outcome = git_mod._run_reviewed_stage_cycle(ctx, "msg", 0.0)
    assert outcome["status"] == "blocked" and outcome["block_reason"] == "fixed_overflow"
    assert count_paid_review_cycles(ctx, root_task_id="root-1") == 0
    from ouroboros.review_state import load_state, make_repo_key

    rows = load_state(ctx.drive_root).filter_attempts(
        repo_key=make_repo_key(pathlib.Path(ctx.repo_dir)))
    assert rows and rows[-1].paid is False and rows[-1].block_class == "infra"


def test_one_side_dispatched_attempt_counts_as_paid(tmp_path, monkeypatch):
    """F2(b): parallel dispatch means one side can spend while the other
    overflows at assembly — any side dispatching makes the cycle paid."""
    from ouroboros.tools.commit_gate import count_paid_review_cycles

    git_mod, ctx, _progress = _stage_cycle_harness(tmp_path, monkeypatch, fingerprint="fp-b")
    monkeypatch.setattr(git_mod, "_run_parallel_review", _overflow_wave(dispatch=True))
    outcome = git_mod._run_reviewed_stage_cycle(ctx, "msg", 0.0)
    assert outcome["status"] == "blocked"
    assert count_paid_review_cycles(ctx, root_task_id="root-1") == 1
    assert ctx._review_paid_stamp is None  # the seam never leaks past the wave


@pytest.mark.parametrize(
    "seed,expected_reason,expected_note_part",
    [
        ("verdict", "identical_diff_refused", "reusing the recorded"),
        ("ceiling", "review_cycles_exhausted", "no review outcome"),
    ],
)
def test_advisory_replay_reasons_drive_the_stage_cycle_to_a_disclosed_pass(
    tmp_path, monkeypatch, seed, expected_reason, expected_note_part,
):
    """fable P3-3 (end to end): under advisory each free-replay reason lets the
    REAL stage cycle pass without any dispatch, with its own honest progress
    note, and the disclosure lands in the commit result formatting."""
    from ouroboros.review_state import CommitAttemptRecord, make_repo_key, update_state, _utc_now

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    git_mod, ctx, progress = _stage_cycle_harness(tmp_path, monkeypatch, fingerprint="fp-r")
    monkeypatch.setattr(
        git_mod, "_run_parallel_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("free replay must not dispatch")),
    )
    repo_key = make_repo_key(pathlib.Path(ctx.repo_dir))
    if seed == "verdict":
        update_state(ctx.drive_root, lambda s: s.attempts.append(CommitAttemptRecord(
            ts=_utc_now(), commit_message="m", status="blocked",
            block_reason="critical_findings", block_class="verdict",
            repo_key=repo_key, tool_name="commit_reviewed", task_id="root-1",
            attempt=1, phase="blocking_review", pre_review_fingerprint="fp-r",
            review_contract_fingerprint="cf-1",
            critical_findings=[{"item": "bug_q", "reason": "r", "severity": "critical"}],
        )))
    else:
        monkeypatch.setenv(KEY, "1")
        update_state(ctx.drive_root, lambda s: s.attempts.append(CommitAttemptRecord(
            ts=_utc_now(), commit_message="m", status="succeeded", repo_key=repo_key,
            tool_name="commit_reviewed", task_id="root-1", attempt=1, phase="commit",
            paid=True, root_task_id="root-1", pre_review_fingerprint="fp-old",
        )))

    outcome = git_mod._run_reviewed_stage_cycle(ctx, "msg", 0.0)
    assert outcome["status"] == "passed"
    notes = [n for n in progress if "Max Review Cycles" in n]
    assert notes and expected_note_part in notes[0]
    # The loud disclosure reached the advisory channel AND the commit result.
    assert any(expected_reason in w for w in ctx._review_advisory)
    result = git_mod._format_commit_result(ctx, "msg", "", "")
    assert "no new triad+scope review was bought" in result
    assert expected_reason in result
    events = [json.loads(line) for line in
              (ctx.drive_root / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    replays = [e for e in events if e["type"] == "commit_review_free_replay"]
    assert replays and replays[-1]["reason"] == expected_reason


def test_managed_advisory_ceiling_replay_survives_stale_subject_trees(
    tmp_path, monkeypatch,
):
    """Synthesis wave W1 (cross-lane interference): the MANAGED resolver's
    advisory free replay skips run_parallel_review — previously the ONLY reset
    point of ``ctx._last_review_subject_trees`` — so subject-tree residue from
    a previous PAID attempt was compared against the CURRENT binding tree and
    blocked every retry with a typed review_subject_binding_mismatch (ceiling
    still exhausted -> replay again -> same stale set: the managed update
    dead-ended). The stage cycle now resets the set at every attempt start."""
    from ouroboros.review_state import CommitAttemptRecord, make_repo_key, update_state, _utc_now

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv(KEY, "1")
    git_mod, ctx, progress = _stage_cycle_harness(tmp_path, monkeypatch, fingerprint="fp-m")
    monkeypatch.setattr(
        git_mod, "_fingerprint_staged_diff",
        lambda repo_dir: {
            "ok": True, "fingerprint": "fp-m",
            "binding": {"tree_sha": "tree-NEW"},
        },
    )
    monkeypatch.setattr(git_mod, "_authorized_managed_update_resolver", lambda c: True)
    monkeypatch.setattr(
        git_mod, "_run_parallel_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("free replay must not dispatch")),
    )
    repo_key = make_repo_key(pathlib.Path(ctx.repo_dir))
    update_state(ctx.drive_root, lambda s: s.attempts.append(CommitAttemptRecord(
        ts=_utc_now(), commit_message="m", status="blocked", repo_key=repo_key,
        tool_name="commit_reviewed", task_id="root-1", attempt=1,
        phase="blocking_review", block_reason="quorum_failure", block_class="infra",
        paid=True, root_task_id="root-1", pre_review_fingerprint="fp-old",
    )))
    # Residue of the previous PAID attempt in this same resolver task/ctx.
    ctx._last_review_subject_trees = {"tree-OLD"}

    outcome = git_mod._run_reviewed_stage_cycle(ctx, "resolve", 0.0)

    assert outcome["status"] == "passed", outcome
    assert any("paid-cycle ceiling exhausted" in n for n in progress)


def test_managed_in_attempt_subject_mismatch_still_blocks(tmp_path, monkeypatch):
    """W1 control: the per-attempt reset must not weaken the assertion — a
    subject tree recorded DURING the attempt that diverges from the binding
    tree still blocks with the typed mismatch (and only in-attempt trees are
    asserted: pre-attempt residue never reaches the message)."""
    git_mod, ctx, _progress = _stage_cycle_harness(tmp_path, monkeypatch, fingerprint="fp-c")
    monkeypatch.setattr(
        git_mod, "_fingerprint_staged_diff",
        lambda repo_dir: {
            "ok": True, "fingerprint": "fp-c",
            "binding": {"tree_sha": "tree-REAL"},
        },
    )
    monkeypatch.setattr(git_mod, "_authorized_managed_update_resolver", lambda c: True)
    ctx._last_review_subject_trees = {"tree-STALE-RESIDUE"}  # must be irrelevant

    def _wave(inner_ctx, *a, **kw):
        inner_ctx._last_review_subject_trees.add("tree-WRONG")
        return None, None, "", []

    monkeypatch.setattr(git_mod, "_run_parallel_review", _wave)
    monkeypatch.setattr(
        git_mod, "_aggregate_review_verdict", lambda *a, **kw: (False, "", "", [], []),
    )

    outcome = git_mod._run_reviewed_stage_cycle(ctx, "resolve", 0.0)

    assert outcome["status"] == "blocked"
    assert outcome["block_reason"] == "review_subject_binding_mismatch"
    assert "tree-WRONG" in outcome["message"]
    assert "tree-STALE-RESIDUE" not in outcome["message"]


# ---------------------------------------------------------------------------
# F3 — the skill dispatch marker: four wave outcomes, one paid unit each


def _fake_manifest():
    return types.SimpleNamespace(
        name="demo", description="d", version="1", type="tool", runtime="python",
        timeout_sec=30, permissions=[], conflicts=[], env_from_settings=[],
        requires=[], scripts=[], scheduled_tasks=[], entry="main.py",
        is_extension=lambda: False,
    )


def _wire_skill_wave(monkeypatch, tmp_path, *, content_hash, passes):
    import ouroboros.skill_review_passes as passes_mod
    from ouroboros import config as cfg
    from ouroboros import skill_review

    drive = pathlib.Path(tmp_path)
    skill = types.SimpleNamespace(
        name="demo", manifest=_fake_manifest(), skill_dir=drive / "skill",
        load_error="", review=None, source="",
    )
    binding = types.SimpleNamespace(state_drive_root=drive)
    monkeypatch.setattr(skill_review, "build_resolved_resource_binding", lambda *a, **k: binding)
    monkeypatch.setattr(skill_review, "load_bound_skill", lambda b: skill)
    monkeypatch.setattr(skill_review, "compute_content_hash", lambda *a, **k: content_hash)
    monkeypatch.setattr(skill_review, "_run_deterministic_preflight", lambda *a, **k: None)
    monkeypatch.setattr(skill_review, "reviewer_slot_config_error", lambda: "")
    monkeypatch.setattr(skill_review, "_build_skill_file_packs", lambda *a, **k: ["pack"])
    monkeypatch.setattr(skill_review, "_official_hub_review_profile", lambda s: "")
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **k: None)
    monkeypatch.setattr(skill_review, "emit_review_model_error_events", lambda *a, **k: None)
    monkeypatch.setattr(skill_review, "save_review_state", lambda *a, **k: None)
    monkeypatch.setattr(
        skill_review, "auto_grant_if_enabled",
        lambda *a, **k: types.SimpleNamespace(
            requested_keys=[], granted_keys=[],
            requested_permissions=[], granted_permissions=[]),
    )
    monkeypatch.setattr(cfg, "get_review_models", lambda: ["m1", "m2"])
    monkeypatch.setattr(passes_mod, "run_skill_review_passes", passes)
    return skill


def _panel_result(models, *, verdict="PASS"):
    """A minimal parseable two-model panel body for the REAL parser: each actor
    returns a JSON array answering every required checklist item."""
    from ouroboros.skill_review import _SKILL_REVIEW_ITEMS

    items = [
        {"item": item, "verdict": verdict, "severity": "advisory", "reason": "checked"}
        for item in _SKILL_REVIEW_ITEMS
    ]
    return json.dumps({
        "model_count": len(models),
        "results": [
            {"model": model, "text": json.dumps(items), "slot_id": f"slot_{idx + 1}"}
            for idx, model in enumerate(models)
        ],
    })


def _dispatching(passes_body):
    from ouroboros.review_dispatch import stamp_review_paid_on_dispatch

    def _wave(ctx, *a, **k):
        stamp_review_paid_on_dispatch(ctx)
        return passes_body(ctx)

    return _wave


def _paid_units(tmp_path, content_hash):
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles

    return count_paid_skill_review_cycles(
        pathlib.Path(tmp_path), "demo", "manual:demo", content_hash=content_hash,
    )


def _history_rows(tmp_path):
    from ouroboros.skill_review_history import review_history_path

    path = review_history_path(pathlib.Path(tmp_path), "demo")
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_skill_wave_outcomes_yield_exactly_one_paid_unit_each(tmp_path, monkeypatch):
    """F3 on a REAL jsonl (persist=True): substantive verdict, quorum failure,
    transport failure, and exception-after-dispatch each leave exactly ONE
    paid unit in the derived count — via the terminal row when one lands, via
    the unmerged write-ahead dispatch marker when none does."""
    from ouroboros import skill_review
    from ouroboros.skill_review_history import load_dispatch_markers

    ctx = types.SimpleNamespace(task_id="", task_metadata={}, event_queue=None)

    # (1) substantive verdict: terminal row carries the paid facts, marker merged.
    _wire_skill_wave(
        monkeypatch, tmp_path, content_hash="h-verdict",
        passes=_dispatching(lambda c: ("prompt", {}, _panel_result(["m1", "m2"]), "")),
    )
    outcome = skill_review.review_skill(ctx, "demo", persist=True)
    assert outcome.status in ("clean", "warnings", "blockers")
    assert outcome.paid is True and outcome.wave_id
    assert _paid_units(tmp_path, "h-verdict") == 1
    assert load_dispatch_markers(pathlib.Path(tmp_path), "demo") == []  # merged+cleared
    row = _history_rows(tmp_path)[-1]
    assert row["paid"] is True and row["wave_id"] == outcome.wave_id
    assert row["review_contract_fingerprint"] == outcome.review_contract_fingerprint

    # (2) quorum failure: the internal history append carries the paid facts.
    _wire_skill_wave(
        monkeypatch, tmp_path, content_hash="h-quorum",
        passes=_dispatching(lambda c: ("prompt", {}, _panel_result(["m1"]), "")),
    )
    outcome = skill_review.review_skill(ctx, "demo", persist=True)
    assert outcome.status == "pending" and outcome.paid is True
    assert _paid_units(tmp_path, "h-quorum") == 1
    assert load_dispatch_markers(pathlib.Path(tmp_path), "demo") == []
    row = _history_rows(tmp_path)[-1]
    assert row["paid"] is True and row["content_hash"] == "h-quorum"

    # (3) transport failure: no history row lands, the unmerged marker counts.
    _wire_skill_wave(
        monkeypatch, tmp_path, content_hash="h-transport",
        passes=_dispatching(lambda c: ("prompt", {}, "", "provider exploded")),
    )
    outcome = skill_review.review_skill(ctx, "demo", persist=True)
    assert outcome.status == "pending" and outcome.paid is True
    assert _paid_units(tmp_path, "h-transport") == 1
    markers = load_dispatch_markers(pathlib.Path(tmp_path), "demo")
    assert len(markers) == 1
    assert markers[0].get("content_hash") == "h-transport" and markers[0].get("paid") is True

    # (4) exception after dispatch: the write-ahead marker survives the crash.
    def _boom(ctx_arg):
        raise RuntimeError("wave crashed after dispatch")

    _wire_skill_wave(
        monkeypatch, tmp_path, content_hash="h-crash", passes=_dispatching(_boom),
    )
    with pytest.raises(RuntimeError):
        skill_review.review_skill(ctx, "demo", persist=True)
    assert _paid_units(tmp_path, "h-crash") == 1
    # Per-wave markers are APPEND-ONLY: wave (4)'s write did NOT displace the
    # orphaned marker (3) — both coexist and each spend is still exactly one
    # unit derived from its own unmerged marker.
    assert _paid_units(tmp_path, "h-transport") == 1
    unmerged = load_dispatch_markers(pathlib.Path(tmp_path), "demo")
    assert {m.get("content_hash") for m in unmerged} == {"h-transport", "h-crash"}
    # The seam is restored after every wave.
    assert getattr(ctx, "_review_paid_stamp", None) is None


def test_lifecycle_timeout_terminal_merges_the_dispatch_marker(tmp_path, monkeypatch):
    """F3(b): a lifecycle timeout finalizes with NO result object — the
    terminal history row still carries the paid facts, merged from the
    write-ahead dispatch marker by job id."""
    from ouroboros.skill_review_history import load_dispatch_markers, write_dispatch_marker
    from ouroboros.skill_review_runner import _mark_review_job_timeout, review_job_state_path
    from ouroboros.utils import atomic_write_json

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    job_path = review_job_state_path(drive, "demo")
    job_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(job_path, {
        "job_id": "job-77", "status": "running", "skill": "demo",
        "content_hash": "h-t", "group_id": "task:root-5:demo", "root_task_id": "root-5",
    }, trailing_newline=True)
    write_dispatch_marker(
        drive, "demo", wave_id="job-77", group_id="task:root-5:demo",
        content_hash="h-t", root_task_id="root-5",
        review_contract_fingerprint="cf-9", rebuttal_sha256="reb-9",
    )
    _mark_review_job_timeout(drive, "demo", "h-t", reason="lifecycle_timeout")
    rows = _history_rows(tmp_path)
    assert rows and rows[-1]["status"] == "timeout"
    assert rows[-1]["paid"] is True  # merged from the marker, result was None
    assert rows[-1]["usage_attribution_schema"] == "physical_attempt_v1"
    assert rows[-1]["review_contract_fingerprint"] == "cf-9"
    assert rows[-1]["rebuttal_sha256"] == "reb-9"
    assert load_dispatch_markers(drive, "demo") == []  # merge cleared it
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles

    assert count_paid_skill_review_cycles(drive, "demo", "task:root-5:demo") == 1


def test_terminal_history_append_failure_is_a_loud_typed_event(tmp_path, monkeypatch):
    """F3(c): a swallowed terminal-history append surfaces as a typed event."""
    import ouroboros.skill_review_runner as runner
    from ouroboros import skill_review_history

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skill_review_history, "append_history_once", lambda *a, **k: False)
    ok = runner._append_terminal_history(
        drive, "demo", {"job_id": "j-1"}, status="failed",
        terminal_reason="boom", result=None, ts="t1",
    )
    assert ok is False
    events = [json.loads(line) for line in
              (drive / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    failed = [e for e in events if e["type"] == "skill_review_history_append_failed"]
    assert failed and failed[0]["skill"] == "demo" and failed[0]["job_id"] == "j-1"


# ---------------------------------------------------------------------------
# F4a — spent-rebuttal memory is scoped to the CURRENT panel contract


def test_spent_rebuttal_memory_lapses_with_the_panel_contract(tmp_path):
    from ouroboros.skill_review_history import review_history_path
    from ouroboros.skill_review_cycles import find_free_replay_row

    path = review_history_path(pathlib.Path(tmp_path), "demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        # reb-1 was answered by a substantive verdict — under the RETIRED contract.
        {"ts": "t1", "status": "warnings", "content_hash": "h1", "paid": True,
         "group_id": "manual:demo", "review_contract_fingerprint": "cf-old",
         "rebuttal_sha256": "reb-1", "job_id": "j1"},
        # The current contract has its own substantive verdict for the snapshot.
        {"ts": "t2", "status": "warnings", "content_hash": "h1", "paid": True,
         "group_id": "manual:demo", "review_contract_fingerprint": "cf-new",
         "job_id": "j2"},
    ]
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    # Under the current contract reb-1 has never been adjudicated: it buys the
    # paid rerun (no replay) instead of being refused as already spent.
    assert find_free_replay_row(
        pathlib.Path(tmp_path), "demo", group_id="manual:demo", content_hash="h1",
        contract_fingerprint="cf-new", rebuttal_sha256="reb-1",
    ) is None
    # Once the CURRENT contract has answered it, the same hash replays free.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"ts": "t3", "status": "warnings", "content_hash": "h1", "paid": True,
             "group_id": "manual:demo", "review_contract_fingerprint": "cf-new",
             "rebuttal_sha256": "reb-1", "job_id": "j3"}) + "\n")
    assert find_free_replay_row(
        pathlib.Path(tmp_path), "demo", group_id="manual:demo", content_hash="h1",
        contract_fingerprint="cf-new", rebuttal_sha256="reb-1",
    ) is not None


# ---------------------------------------------------------------------------
# C4 — append-only per-wave dispatch markers (concurrency + legacy migration)


def test_concurrent_wave_markers_are_append_only_and_each_merge_clears_its_own(tmp_path):
    """Two interleaved waves on ONE skill both keep their write-ahead paid
    fact (per-wave marker files — no single-file overwrite), both count
    toward the ceiling, and each terminal-row merge clears exactly its own
    marker without minting spurious infra rows for the live sibling."""
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.skill_review_history import (
        append_history_once,
        load_dispatch_markers,
        write_dispatch_marker,
    )
    from ouroboros.utils import utc_now_iso

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    write_dispatch_marker(
        drive, "demo", wave_id="wave-A", group_id="manual:demo", content_hash="h-1",
    )
    write_dispatch_marker(
        drive, "demo", wave_id="wave-B", group_id="manual:demo", content_hash="h-1",
    )

    # Append-only: writing B neither displaced A's marker nor flushed the LIVE
    # wave A into the history as a fake infra terminal.
    markers = load_dispatch_markers(drive, "demo")
    assert {m["wave_id"] for m in markers} == {"wave-A", "wave-B"}
    assert {m["usage_attribution_schema"] for m in markers} == {"physical_attempt_v1"}
    assert _history_rows(tmp_path) == []
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-1",
    ) == 2

    # Wave A's REAL terminal row lands (idempotent merge by wave id): the paid
    # fact merges from A's own marker and ONLY A's marker is cleared.
    assert append_history_once(drive, "demo", {
        "ts": utc_now_iso(), "status": "clean", "content_hash": "h-1",
        "group_id": "manual:demo", "job_id": "wave-A", "wave_id": "wave-A",
        "failure_signature": [], "fail_findings": [],
    })
    rows = _history_rows(tmp_path)
    assert [row["status"] for row in rows] == ["clean"]  # the verdict, not "interrupted"
    assert rows[0]["usage_attribution_schema"] == "physical_attempt_v1"
    assert rows[-1]["paid"] is True
    assert {m["wave_id"] for m in load_dispatch_markers(drive, "demo")} == {"wave-B"}
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-1",
    ) == 2  # one landed row + one still-unmerged marker

    # Wave B merges too: no markers left, the count is stable.
    assert append_history_once(drive, "demo", {
        "ts": utc_now_iso(), "status": "warnings", "content_hash": "h-1",
        "group_id": "manual:demo", "job_id": "wave-B", "wave_id": "wave-B",
        "failure_signature": [], "fail_findings": [],
    })
    assert load_dispatch_markers(drive, "demo") == []
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-1",
    ) == 2


def test_legacy_single_file_marker_is_read_and_flushed_on_the_next_write(tmp_path):
    """Migration: a pre-upgrade SINGLE-file marker is tolerated read-side
    (listed + counted) and the next wave's write flushes it into the history
    as a paid infra terminal and removes the file — the spend is never
    forgotten and never double-counted."""
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.skill_review_history import (
        legacy_dispatch_marker_path,
        load_dispatch_markers,
        write_dispatch_marker,
    )

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    legacy = legacy_dispatch_marker_path(drive, "demo")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({
        "ts": "2026-01-01T00:00:00Z", "wave_id": "wave-legacy",
        "group_id": "manual:demo", "content_hash": "h-1",
        "root_task_id": "", "paid": True,
        "review_contract_fingerprint": "cf-old", "rebuttal_sha256": "",
    }), encoding="utf-8")

    assert any(
        m["wave_id"] == "wave-legacy" for m in load_dispatch_markers(drive, "demo")
    )
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-1",
    ) == 1

    write_dispatch_marker(
        drive, "demo", wave_id="wave-new", group_id="manual:demo", content_hash="h-1",
    )
    assert not legacy.exists()
    flushed = [
        row for row in _history_rows(tmp_path)
        if row.get("terminal_reason") == "dispatched_wave_never_finalized"
    ]
    assert flushed and flushed[-1]["wave_id"] == "wave-legacy"
    assert flushed[-1]["paid"] is True
    assert flushed[-1]["review_contract_fingerprint"] == "cf-old"
    assert {m["wave_id"] for m in load_dispatch_markers(drive, "demo")} == {"wave-new"}
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-1",
    ) == 2  # the flushed legacy row + the new unmerged marker


def test_late_marker_overlays_unpaid_terminal_without_rewriting_or_double_counting(tmp_path):
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.skill_review_history import (
        append_history_once,
        load_dispatch_markers,
        load_history,
        review_history_path,
        write_dispatch_marker,
    )

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    terminal = {
        "ts": "t1", "status": "timeout", "terminal_reason": "slot timeout",
        "content_hash": "h-late", "group_id": "task:root-late:demo",
        "root_task_id": "root-late", "job_id": "wave-late",
        "failure_signature": [], "fail_findings": [],
    }
    assert append_history_once(drive, "demo", terminal)
    path = review_history_path(drive, "demo")
    raw_before = path.read_bytes()

    write_dispatch_marker(
        drive, "demo", wave_id="wave-late", group_id="task:root-late:demo",
        content_hash="h-late", root_task_id="root-late",
        review_contract_fingerprint="cf-late", rebuttal_sha256="reb-late",
    )
    effective = load_history(drive, "demo", limit=0)[0]
    assert effective["paid"] is True
    assert effective["wave_id"] == "wave-late"
    assert effective["usage_attribution_schema"] == "physical_attempt_v1"
    assert effective["review_contract_fingerprint"] == "cf-late"
    assert path.read_bytes() == raw_before  # overlay is read-only
    assert count_paid_skill_review_cycles(drive, "demo", "task:root-late:demo") == 1

    # An idempotent terminal retry must not erase the only late-dispatch fact.
    assert append_history_once(drive, "demo", terminal)
    assert path.read_bytes() == raw_before
    assert [row["wave_id"] for row in load_dispatch_markers(drive, "demo")] == ["wave-late"]
    assert count_paid_skill_review_cycles(drive, "demo", "task:root-late:demo") == 1


def test_duplicate_terminal_clears_only_a_marker_already_present_in_raw_row(tmp_path):
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.skill_review_history import (
        append_history_once,
        load_dispatch_markers,
        write_dispatch_marker,
    )

    drive = pathlib.Path(tmp_path)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    terminal = {
        "ts": "t1", "status": "clean", "content_hash": "h-stale",
        "group_id": "manual:demo", "root_task_id": "", "job_id": "wave-stale",
        "wave_id": "wave-stale", "paid": True,
        "usage_attribution_schema": "physical_attempt_v1",
        "review_contract_fingerprint": "cf-stale", "rebuttal_sha256": "reb-stale",
        "failure_signature": [], "fail_findings": [],
    }
    assert append_history_once(drive, "demo", terminal)
    write_dispatch_marker(
        drive, "demo", wave_id="wave-stale", group_id="manual:demo",
        content_hash="h-stale", review_contract_fingerprint="cf-stale",
        rebuttal_sha256="reb-stale",
    )
    assert len(load_dispatch_markers(drive, "demo")) == 1
    assert append_history_once(drive, "demo", terminal)
    assert load_dispatch_markers(drive, "demo") == []
    assert count_paid_skill_review_cycles(
        drive, "demo", "manual:demo", content_hash="h-stale",
    ) == 1


def test_direct_quorum_terminal_keeps_wave_identity_for_a_late_dispatch(tmp_path, monkeypatch):
    from ouroboros import skill_review
    from ouroboros.skill_review_history import load_history

    captured = {}

    def _terminal_before_worker_dispatch(ctx, *_args, **_kwargs):
        captured["stamp"] = ctx._review_paid_stamp
        return "prompt", {}, _panel_result(["m1"]), ""

    _wire_skill_wave(
        monkeypatch, tmp_path, content_hash="h-direct-late",
        passes=_terminal_before_worker_dispatch,
    )
    ctx = types.SimpleNamespace(task_id="", task_metadata={}, event_queue=None)
    outcome = skill_review.review_skill(ctx, "demo", persist=True)
    assert outcome.status == "pending" and outcome.paid is False and outcome.wave_id
    raw = _history_rows(tmp_path)[-1]
    assert raw.get("paid") is not True and raw["wave_id"] == outcome.wave_id

    captured["stamp"]()
    effective = load_history(pathlib.Path(tmp_path), "demo", limit=0)[-1]
    assert effective["paid"] is True and effective["wave_id"] == outcome.wave_id
    assert effective["usage_attribution_schema"] == "physical_attempt_v1"
    assert _paid_units(tmp_path, "h-direct-late") == 1


def test_bound_api_paid_stamp_waits_for_durable_sync_and_async_dispatch(tmp_path):
    import asyncio

    from ouroboros import usage_accounting as ua
    from ouroboros.review_dispatch import ReviewPaidStamp, bind_api_review_paid_stamp

    drive = pathlib.Path(tmp_path)
    request = ua.AttemptRequest(
        model="local-test", provider="local", reservation_usd=0.0,
        drive_root=drive, task_id="review", root_task_id="review",
    )
    writes = []

    def _write_paid():
        assert _ledger_state(drive) == "dispatched"
        writes.append("paid")

    def _ledger_state(root):
        rows = [json.loads(line) for line in (root / ua.LEDGER_REL).read_text().splitlines()]
        return rows[-1]["state"]

    stamp = ReviewPaidStamp(_write_paid)
    with bind_api_review_paid_stamp(stamp):
        with pytest.raises(RuntimeError, match="candidate refused"):
            ua.execute_physical_attempt(
                request, lambda: None,
                before_dispatch=lambda _reservation: (_ for _ in ()).throw(RuntimeError("candidate refused")),
            )
    assert writes == [] and stamp.fired is False

    with bind_api_review_paid_stamp(stamp):
        with pytest.raises(RuntimeError, match="wire failed"):
            ua.execute_physical_attempt(
                request, lambda: (_ for _ in ()).throw(RuntimeError("wire failed")),
            )
    assert writes == ["paid"] and stamp.fired is True

    async_stamp = ReviewPaidStamp(lambda: writes.append("async"))

    async def _send():
        assert async_stamp.fired is True
        return {"usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    async def _run():
        with bind_api_review_paid_stamp(async_stamp):
            return await ua.execute_physical_attempt_async(request, _send)

    assert asyncio.run(_run())["usage"]["prompt_tokens"] == 0
    assert writes == ["paid", "async"]


def test_fail_closed_paid_stamp_replays_one_failure_to_every_dispatcher():
    from ouroboros.review_dispatch import ReviewPaidStamp, invoke_review_paid_stamp

    writes = []

    def refuse():
        writes.append("attempted")
        raise RuntimeError("wallet unavailable")

    stamp = ReviewPaidStamp(refuse, fail_closed=True)
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        invoke_review_paid_stamp(stamp)
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        invoke_review_paid_stamp(stamp)

    assert writes == ["attempted"]
    assert stamp.fired is True


def test_strict_api_stamp_veto_releases_sync_and_async_attempts(tmp_path):
    import asyncio
    from ouroboros import usage_accounting as ua
    from ouroboros.review_dispatch import ReviewPaidStamp, bind_api_review_paid_stamp

    def request(root):
        return ua.AttemptRequest(model="local", provider="local", reservation_usd=1.0,
                                 drive_root=root, task_id="t", root_task_id="t")

    def refuse(root):
        rows = (root / ua.LEDGER_REL).read_text().splitlines()
        assert json.loads(rows[-1])["state"] == "reserved"
        raise RuntimeError("wallet unavailable")

    sync_root, sent = tmp_path / "sync", []
    with bind_api_review_paid_stamp(ReviewPaidStamp(lambda: refuse(sync_root), fail_closed=True)):
        with pytest.raises(ua.PhysicalAttemptPreparationFailed):
            ua.execute_physical_attempt(request(sync_root), lambda: sent.append("sync"))
    assert sent == []
    assert ua.usage_projection(sync_root)["attempt_counts"] == {"released": 1}

    async_root = tmp_path / "async"
    async def run():
        with bind_api_review_paid_stamp(ReviewPaidStamp(lambda: refuse(async_root), fail_closed=True)):
            return await ua.execute_physical_attempt_async(
                request(async_root), lambda: (_ for _ in ()).throw(AssertionError("sent")))
    with pytest.raises(ua.PhysicalAttemptPreparationFailed):
        asyncio.run(run())
    assert ua.usage_projection(async_root)["attempt_counts"] == {"released": 1}
