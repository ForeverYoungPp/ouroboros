"""Pure CyberGym protocol and ledger tests; no upstream or Docker dependency."""

from __future__ import annotations

import hashlib

import pytest

from devtools.benchmarks.cybergym.cybergym_adapter import (
    DEFAULT_FINAL_POC_PATH,
    OFFICIAL_MODEL,
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
    parse_strict_bool,
    pre_admission_report,
    project_budget,
    run_campaign,
    safe_task_id,
    safe_task_path,
    task_contract_metadata,
    validate_model_pin,
    validate_positive_finite,
    verify_mask_map,
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
        trials=[
            {"trial_id": "old", "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0},
            {"trial_id": "final", "poc_hash": "b" * 64, "vul_exit_code": 71, "fix_exit_code": 0},
        ],
        final_trial={"trial_id": "final", "poc_hash": "b" * 64, "vul_exit_code": 71, "fix_exit_code": 0},
        final_poc_sha256="b" * 64,
        status="completed",
    )
    assert row["metric_name"] == "final_submission"
    assert row["final_submission_success"] is False
    assert row["any_of_success"] is True
    assert row["trial_count"] == 2


def test_explicit_final_trial_cannot_rebind_a_stale_record():
    digest = "a" * 64
    trials = [{"trial_id": "final", "poc_id": "p1", "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}]
    projection = final_submission(
        {"trial_id": "final", "poc_id": "p1", "poc_hash": "b" * 64, "vul_exit_code": 1, "fix_exit_code": 0},
        trials=trials,
    )
    assert projection["final_submission_status"] == "unknown"
    assert projection["final_submission_reason"] == "invalid_final_trial"


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


def test_exact_model_and_positive_launcher_values_are_strict():
    assert validate_model_pin(OFFICIAL_MODEL) == OFFICIAL_MODEL
    with pytest.raises(ValueError):
        validate_model_pin("deepseek/deepseek-v4-flash")
    assert validate_positive_finite("1.5", field="budget") == 1.5
    for value in (0, -1, float("nan"), float("inf"), True, ""):
        with pytest.raises(ValueError):
            validate_positive_finite(value, field="timeout")


def test_strict_trial_bool_rejects_truthy_strings_and_contract_is_pinned():
    assert parse_strict_bool("false") is False
    assert parse_strict_bool("TRUE") is True
    with pytest.raises(ValueError):
        parse_strict_bool("yes")
    contract = task_contract_metadata("arvo:1")
    assert contract["model"] == OFFICIAL_MODEL
    assert contract["final_poc_path"] == DEFAULT_FINAL_POC_PATH
    assert contract["no_swarm"] is True
    assert "schedule_subagent" in contract["disabled_tools"]


def test_completed_row_requires_marker_bound_final_evidence():
    row = build_task_result_row(
        "arvo:1",
        trials=[{"trial_id": "final", "poc_hash": "a" * 64, "vul_exit_code": 1, "fix_exit_code": 0}],
        status="completed",
    )
    assert row["status"] == "infra_failed"
    assert row["infra_reason"] == "final_evidence_missing"


def test_run_campaign_rejects_duplicate_ids_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match="duplicate task id"):
        run_campaign(
            ["arvo:1", "arvo:1"],
            run_root=tmp_path / "run",
            executor=None,
            estimated_cost_usd=1,
        )
    assert not (tmp_path / "run").exists()


def test_run_campaign_requires_regular_marker_and_binds_hash(tmp_path):
    def no_marker(_task, _task_dir):
        return {
            "status": "completed",
            "trials": [{"trial_id": "final", "poc_hash": "a" * 64, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 0.5,
        }

    rows = run_campaign(
        ["arvo:1"],
        run_root=tmp_path / "missing",
        executor=no_marker,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    assert rows[0]["status"] == "infra_failed"
    assert rows[0]["infra_reason"] == "FinalPocRefused"

    def good_marker(_task, task_dir):
        marker = task_dir / "final.poc"
        marker.write_bytes(b"poc")
        digest = hashlib.sha256(b"poc").hexdigest()
        return {
            "status": "completed",
            "trials": [{"trial_id": "final", "is_final": True, "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 0.5,
        }

    rows = run_campaign(
        ["arvo:2"],
        run_root=tmp_path / "good",
        executor=good_marker,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    assert rows[0]["status"] == "completed"
    assert rows[0]["final_submission_success"] is True


def test_mask_map_is_private_but_checked_for_selected_rows(tmp_path):
    path = tmp_path / "mask_map.json"
    path.write_text('{"arvo:1":"abc123456789"}', encoding="utf-8")
    info = verify_mask_map(path, ["arvo:1"])
    assert info["coverage"] == "complete"
    assert info["entries"] == 1
    assert "abc123456789" not in str(info)


def test_applied_settings_metadata_is_read_back_from_written_snapshot(tmp_path):
    from types import SimpleNamespace

    from devtools.benchmarks.cybergym.run_cybergym import _prepare_applied_settings

    template = tmp_path / "settings.json"
    template.write_text(
        '{"OUROBOROS_MODEL": "wrong", "OUROBOROS_MODEL_LIGHT": "wrong"}',
        encoding="utf-8",
    )
    output_root = tmp_path / "run"
    output_root.mkdir()
    path, metadata = _prepare_applied_settings(
        template,
        output_root,
        SimpleNamespace(model=OFFICIAL_MODEL, budget_usd=3, timeout_sec=4),
    )
    assert path.exists()
    assert metadata["model"] == OFFICIAL_MODEL
    assert metadata["model_slots"]["OUROBOROS_MODEL"] == OFFICIAL_MODEL
