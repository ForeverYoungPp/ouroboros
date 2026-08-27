"""Pure CyberGym protocol and ledger tests; no upstream or Docker dependency."""

from __future__ import annotations

import hashlib

import pytest

from devtools.benchmarks.cybergym.cybergym_adapter import (
    BudgetLedger,
    BudgetRefused,
    ClaimRefused,
    FinalPocRefused,
    LedgerError,
    build_generate_task_argv,
    build_task_result_row,
    classify_official_exit,
    final_poc_record,
    final_submission,
    pre_admission_report,
    project_budget,
    safe_task_id,
    safe_task_path,
)


def test_safe_ids_and_argv_are_path_safe(tmp_path):
    assert safe_task_id("arvo:47101") == "arvo:47101"
    assert safe_task_path(tmp_path, "arvo:47101").name == "arvo__47101"
    with pytest.raises(ValueError):
        safe_task_id("../../etc:passwd")
    argv = build_generate_task_argv(
        "oss-fuzz:42535201",
        out_dir=tmp_path / "task",
        data_dir=tmp_path / "data",
        server="http://cybergym-internal:8666",
        mask_map=tmp_path / "mask.json",
        agent_id="lane-1",
        with_flag=True,
    )
    assert argv[:4] == [argv[0], "-m", "cybergym.task.gen_task", "--task-id"]
    assert "--with-flag" in argv
    assert all(isinstance(part, str) for part in argv)


def test_pre_admission_is_pure_and_fail_closed(tmp_path):
    report = pre_admission_report(
        task_ids=["arvo:1"],
        output_root=tmp_path / "out",
        repo_dir=tmp_path / "repo",
        source_root=tmp_path / "source",
        data_root=tmp_path / "data",
        settings_path=tmp_path / "settings.json",
        require_settings=True,
        server_url="http://cybergym-internal:8666",
        model="deepseek/deepseek-v4-flash-0731",
    )
    assert report["ok"]
    assert not (tmp_path / "out").exists()
    denied = pre_admission_report(
        task_ids=["arvo:1"],
        output_root=tmp_path / "repo" / "out",
        repo_dir=tmp_path / "repo",
        server_url="http://0.0.0.0:8666",
        model="m",
    )
    assert not denied["ok"]
    assert "output_root_overlaps_repo" in denied["reasons"]
    assert "server_url_wildcard_host" in denied["reasons"]


def test_issue15_and_final_hash_binding(tmp_path):
    assert classify_official_exit(1, 0)["official_success"] is True
    assert classify_official_exit(71, 0)["official_success"] is False
    assert classify_official_exit(None, 0)["official_success"] is None
    payload = b"poc"
    marker = tmp_path / "final.poc"
    marker.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert final_poc_record(tmp_path).sha256 == digest
    trial = {"trial_id": "final", "poc_id": "p1", "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}
    projection = final_submission(trial, final_poc_sha256=digest)
    assert projection["final_submission_success"] is True
    assert projection["any_of_success"] is True
    with pytest.raises(FinalPocRefused):
        (tmp_path / "link.poc").symlink_to(marker)
        final_poc_record(tmp_path / "link.poc")


def test_result_row_preserves_final_and_any_of_columns():
    digest = "a" * 64
    row = build_task_result_row(
        "arvo:1",
        trials=[{"trial_id": "old", "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}],
        final_trial={"trial_id": "final", "poc_hash": "b" * 64, "vul_exit_code": 71, "fix_exit_code": 0},
        final_poc_sha256="b" * 64,
        status="completed",
    )
    assert row["metric_name"] == "final_submission"
    assert row["final_submission_success"] is False
    assert row["any_of_success"] is True
    assert row["trial_count"] == 2


def test_budget_claims_are_atomic_and_unknown_cost_blocks(tmp_path):
    ledger = BudgetLedger(tmp_path / "claims.jsonl", cap_usd=5)
    ledger.claim("arvo:1", 4, attempt_id="a1")
    with pytest.raises(ClaimRefused):
        ledger.claim("arvo:1", 1, attempt_id="a2")
    ledger.mark_unresolved("a1")
    assert not ledger.projection().can_dispatch
    with pytest.raises(BudgetRefused):
        ledger.claim("arvo:2", 1, attempt_id="a2")


def test_budget_projection_replays_terminal_states():
    projection = project_budget(
        [
            {"event": "claim", "task_id": "arvo:1", "attempt_id": "a1", "reserved_usd": 2},
            {"event": "settle", "attempt_id": "a1", "cost_usd": 1.5},
        ],
        cap_usd=3,
    )
    assert projection.projected_usd == 1.5
    assert projection.can_dispatch
    with pytest.raises(LedgerError):
        project_budget([{"event": "settle", "attempt_id": "orphan", "cost_usd": 1}])
