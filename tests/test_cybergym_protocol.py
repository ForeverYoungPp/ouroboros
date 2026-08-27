"""Pure CyberGym protocol and ledger tests; no upstream or Docker dependency."""

from __future__ import annotations

import hashlib
import json

import pytest

from devtools.benchmarks.cybergym.cybergym_adapter import (
    DEFAULT_FINAL_POC_PATH,
    DEFAULT_LEVEL,
    OFFICIAL_MODEL,
    BudgetLedger,
    BudgetOverspend,
    BudgetRefused,
    ClaimRefused,
    CyberGymIntegrationUnavailable,
    CyberGymPinRefused,
    FinalPocRefused,
    LedgerError,
    assert_fresh_output_root,
    build_generate_task_argv,
    build_task_result_row,
    classify_official_exit,
    directory_tree_digest,
    final_poc_record,
    final_submission,
    parse_strict_bool,
    pre_admission_report,
    project_budget,
    run_campaign,
    safe_task_id,
    safe_task_path,
    task_contract_metadata,
    validate_high_effort,
    validate_model_pin,
    validate_positive_finite,
    validate_positive_integral,
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


def test_applied_server_provenance_rewrites_manifest_command(tmp_path):
    from types import SimpleNamespace

    import devtools.benchmarks.cybergym.run_cybergym as launcher

    args = SimpleNamespace(
        server="http://cybergym-internal:8666",
        data_root=tmp_path / "data",
        mask_map=tmp_path / "mask.json",
        difficulty=DEFAULT_LEVEL,
    )
    manifest = {"harness": {"server": args.server}, "official_command": []}
    applied = "http://cybergym-server-campaign:8666"

    launcher._apply_server_provenance(manifest, args, applied)

    assert manifest["harness"] == {
        "server": applied,
        "requested_server": args.server,
        "applied_server": applied,
    }
    assert applied in manifest["official_command"]
    assert args.server not in manifest["official_command"]


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


def test_runtime_paths_are_confined_and_binary_is_nested(tmp_path):
    repo = tmp_path / "repo"
    denied = pre_admission_report(
        task_ids=["arvo:1"],
        output_root=tmp_path / "out",
        repo_dir=repo,
        source_root=tmp_path / "source",
        data_root=tmp_path / "data",
        mask_map=repo / "mask.json",
        server_root=tmp_path / "server",
        binary_dir=tmp_path / "elsewhere",
        require_inputs=True,
        server_url="http://cybergym-internal:8666",
        model=OFFICIAL_MODEL,
    )
    assert not denied["ok"]
    assert "mask_map_overlaps_repo" in denied["reasons"]
    assert "binary_dir_outside_server_root" in denied["reasons"]
    assert not (tmp_path / "out").exists()


def test_fresh_output_root_rejects_nonempty_and_symlink(tmp_path):
    fresh = tmp_path / "fresh"
    assert assert_fresh_output_root(fresh) == fresh
    fresh.mkdir()
    (fresh / "old.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CyberGymPinRefused, match="fresh"):
        assert_fresh_output_root(fresh)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(CyberGymPinRefused, match="symlink"):
        assert_fresh_output_root(link)


def test_directory_digest_allows_confined_upstream_symlink_and_rejects_external(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "libreal.so"
    target.write_bytes(b"binary")
    link = root / "lib.so"
    link.symlink_to("libreal.so")
    first = directory_tree_digest(root)
    assert first["links"] == 1
    assert first["files"] == 1
    assert first["bytes"] == len(b"binary")
    assert directory_tree_digest(root)["sha256"] == first["sha256"]

    link.unlink()
    link.symlink_to("/src/zeek/build/install-root/share/btest/data")
    virtual = directory_tree_digest(root, allowed_virtual_symlink_prefixes=("/src/",))
    assert virtual["links"] == 1

    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(CyberGymPinRefused, match="external link"):
        directory_tree_digest(root)


def test_issue15_and_final_hash_binding(tmp_path):
    assert classify_official_exit(1, 0)["official_success"] is True
    assert classify_official_exit(71, 0)["official_success"] is False
    assert classify_official_exit(300, None)["official_success"] is False
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


def test_budget_settlement_overspend_is_typed_and_stops_dispatch(tmp_path):
    ledger = BudgetLedger(tmp_path / "claims.jsonl", cap_usd=2)
    ledger.claim("arvo:1", 1, attempt_id="a1")
    with pytest.raises(BudgetOverspend):
        ledger.settle("a1", 3)
    projection = ledger.projection()
    assert projection.reason == "budget_cap_exceeded"
    assert projection.can_dispatch is False


def test_exact_model_and_positive_launcher_values_are_strict():
    assert validate_model_pin(OFFICIAL_MODEL) == OFFICIAL_MODEL
    with pytest.raises(ValueError):
        validate_model_pin("deepseek/deepseek-v4-flash")
    assert validate_positive_finite("1.5", field="budget") == 1.5
    for value in (0, -1, float("nan"), float("inf"), True, ""):
        with pytest.raises(ValueError):
            validate_positive_finite(value, field="timeout")
    assert validate_positive_integral("4.0", field="timeout") == 4
    for value in (0, -1, 1.5, float("nan"), True, ""):
        with pytest.raises(ValueError):
            validate_positive_integral(value, field="timeout")
    assert validate_high_effort("HIGH") == "high"
    with pytest.raises(ValueError):
        validate_high_effort("max")


def test_launcher_paid_limits_and_immutable_hash_declarations_are_bounded():
    from types import SimpleNamespace

    from devtools.benchmarks.cybergym.run_cybergym import _validate_launcher_values

    def args(**overrides):
        values = dict(
            model=OFFICIAL_MODEL,
            budget_usd=3000.0,
            timeout_sec=14_400,
            workers=10,
            per_task_estimate_usd=1.0,
            dry_run=False,
            allow_dirty_seed=False,
            expected_data_sha256="a" * 64,
            expected_binary_sha256="b" * 64,
            cybergym_python="python3",
            executor="",
            ouroboros_url="",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    for kwargs, message in (
        ({"budget_usd": 3000.01}, "budget_usd"),
        ({"timeout_sec": 14_401}, "timeout_sec"),
        ({"workers": 11}, "workers"),
        ({"allow_dirty_seed": True}, "allow-dirty-seed"),
        ({"expected_data_sha256": ""}, "expected-data-sha256"),
        ({"expected_binary_sha256": "not-a-hash"}, "expected-binary-sha256"),
    ):
        with pytest.raises(ValueError, match=message):
            _validate_launcher_values(args(**kwargs))

    normalized = args(workers="2", timeout_sec="120.0")
    _validate_launcher_values(normalized)
    assert normalized.workers == 2
    assert normalized.timeout_sec == 120
    with pytest.raises(ValueError, match="cybergym-python"):
        _validate_launcher_values(args(cybergym_python=""))


def test_paid_executor_observations_are_exact_and_provider_overhead_is_settled(tmp_path):
    from devtools.benchmarks.cybergym.run_cybergym import (
        _record_provider_probe_cost,
        _validate_paid_observations,
    )

    class FakeExecutor:
        provider_observation = {
            "status": "passed",
            "observed_model": OFFICIAL_MODEL,
            "provider": "provider-a",
            "response_id": "resp-1",
            "cost_usd": 0.125,
            "cost_estimated": False,
            "secret": "must-not-be-persisted",
        }
        data_observation = {"sha256": "a" * 64, "files": 1, "bytes": 2}
        binary_observation = {"sha256": "b" * 64, "files": 1, "bytes": 3}

    provider, data, binary, cost = _validate_paid_observations(
        FakeExecutor(),
        None,
        model=OFFICIAL_MODEL,
        expected_data_sha256="a" * 64,
        expected_binary_sha256="b" * 64,
    )
    assert provider["cost_usd"] == 0.125
    assert data["sha256"] == "a" * 64
    assert binary["sha256"] == "b" * 64
    event = _record_provider_probe_cost(tmp_path, 10.0, cost)
    assert event["label"] == "provider_probe"
    ledger = BudgetLedger(tmp_path / "claims.jsonl", cap_usd=10.0)
    assert ledger.projection().settled_usd == pytest.approx(0.125)

    FakeExecutor.provider_observation = {
        **FakeExecutor.provider_observation,
        "cost_estimated": True,
    }
    with pytest.raises(CyberGymIntegrationUnavailable, match="unknown or estimated"):
        _validate_paid_observations(
            FakeExecutor(),
            None,
            model=OFFICIAL_MODEL,
            expected_data_sha256="a" * 64,
            expected_binary_sha256="b" * 64,
        )


def test_run_campaign_does_not_settle_nonfinal_cost(tmp_path):
    """A numeric charge is not settled until the provider marks it final."""

    def callback(_task, task_dir):
        (task_dir / "final.poc").write_bytes(b"poc")
        digest = hashlib.sha256(b"poc").hexdigest()
        return {
            "status": "completed",
            "observed_effort": "high",
            "trials": [
                {
                    "trial_id": "final",
                    "is_final": True,
                    "poc_hash": digest,
                    "vul_exit_code": 1,
                    "fix_exit_code": 0,
                }
            ],
            "cost_usd": 0.5,
            "cost_estimated": False,
            "cost_final": False,
        }

    rows = run_campaign(
        ["arvo:1"],
        run_root=tmp_path / "nonfinal-cost",
        executor=callback,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    assert rows[0]["status"] == "infra_failed"
    assert rows[0]["infra_reason"] == "cost_unverifiable"
    projection = BudgetLedger(tmp_path / "nonfinal-cost" / "claims.jsonl", cap_usd=2).projection()
    assert projection.settled_usd == 0
    assert projection.unresolved_upper_bound_usd is None
    assert projection.can_dispatch is False


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
            "observed_effort": "high",
            "trials": [{"trial_id": "final", "poc_hash": "a" * 64, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 0.5,
            "cost_final": True,
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
            "observed_effort": "high",
            "trials": [{"trial_id": "final", "is_final": True, "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 0.5,
            "cost_final": True,
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
    assert rows[0]["attempt_id"]


def test_run_campaign_typed_overspend_row_and_retry_attempt_isolated(tmp_path):
    def overspend(_task, task_dir):
        marker = task_dir / "final.poc"
        marker.write_bytes(b"poc")
        digest = hashlib.sha256(b"poc").hexdigest()
        return {
            "status": "completed",
            "observed_effort": "high",
            "trials": [{"trial_id": "final", "is_final": True, "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 3,
            "cost_final": True,
        }

    rows = run_campaign(
        ["arvo:1"],
        run_root=tmp_path / "overspend",
        executor=overspend,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    assert rows[0]["status"] == "infra_failed"
    assert rows[0]["infra_reason"] == "budget_overspend"
    assert rows[0]["attempt_id"]

    with pytest.raises(ClaimRefused):
        run_campaign(
            ["arvo:1"],
            run_root=tmp_path / "overspend",
            executor=overspend,
            estimated_cost_usd=1,
            budget_cap_usd=2,
        )

    calls: list[tuple[str, str]] = []

    def retryable(task, task_dir):
        calls.append((task.metadata["attempt_id"], str(task_dir)))
        (task_dir / "final.poc").write_bytes(b"retry")
        digest = hashlib.sha256(b"retry").hexdigest()
        return {
            "status": "completed",
            "observed_effort": "high",
            "trials": [{"trial_id": "final", "is_final": True, "poc_hash": digest, "vul_exit_code": 1, "fix_exit_code": 0}],
            "cost_usd": 0.25,
            "cost_final": True,
        }

    retry_root = tmp_path / "retry"
    first = run_campaign(
        ["arvo:2"],
        run_root=retry_root,
        executor=retryable,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    second = run_campaign(
        ["arvo:2"],
        run_root=retry_root,
        executor=retryable,
        estimated_cost_usd=1,
        budget_cap_usd=2,
        allow_retries=True,
    )
    assert first[0]["status"] == second[0]["status"] == "completed"
    assert calls[0][0] != calls[1][0]
    assert calls[0][1] != calls[1][1]
    assert first[0]["attempt_id"] != second[0]["attempt_id"]


def test_run_campaign_rejects_missing_or_non_high_effort(tmp_path):
    def callback(_task, task_dir):
        (task_dir / "final.poc").write_bytes(b"poc")
        return {
            "status": "completed",
            "trials": [
                {
                    "trial_id": "final",
                    "is_final": True,
                    "poc_hash": hashlib.sha256(b"poc").hexdigest(),
                    "vul_exit_code": 1,
                    "fix_exit_code": 0,
                }
            ],
            "cost_usd": 0.5,
            "cost_final": True,
        }

    rows = run_campaign(
        ["arvo:1"],
        run_root=tmp_path / "effort",
        executor=callback,
        estimated_cost_usd=1,
        budget_cap_usd=2,
    )
    assert rows[0]["status"] == "infra_failed"
    assert rows[0]["infra_reason"] == "ValueError"


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


def test_launcher_row_counts_do_not_count_planned_as_completed():
    from devtools.benchmarks.cybergym.run_cybergym import _row_counts

    counts = _row_counts(
        [{"status": "planned"}, {"status": "completed"}, {"status": "infra_failed"}]
    )
    assert counts == {
        "rows_written": 3,
        "completed_count": 1,
        "planned_count": 1,
        "infra_count": 1,
    }


def test_launcher_rejects_fractional_timeout_before_output(tmp_path):
    from devtools.benchmarks.cybergym.run_cybergym import main

    out = tmp_path / "fractional"
    assert main(["--timeout-sec", "1.5", "--out-dir", str(out)]) == 2
    assert not out.exists()


def test_launcher_isolated_server_helper_uses_seed_and_closes(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import devtools.benchmarks.cybergym.cybergym_server as server_module
    from devtools.benchmarks.cybergym.run_cybergym import (
        _start_isolated_ouroboros_server,
    )

    expected_commit = "a" * 40
    events = []

    class FakeServer:
        attestation = {"repo_head": expected_commit, "runtime": "fake"}

        def __init__(self, seed_repo, run_root, settings, docker_host, **kwargs):
            events.append(
                (
                    "init",
                    seed_repo,
                    run_root,
                    settings,
                    docker_host,
                    kwargs,
                )
            )
            self.base_url = "http://127.0.0.1:18181"

        def start(self, *, ready_timeout):
            events.append(("start", ready_timeout))
            return self

        def close(self):
            events.append(("close",))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-provider-key")
    monkeypatch.setattr(server_module, "CyberGymIsolatedServer", FakeServer)
    args = SimpleNamespace(
        repo_dir=tmp_path / "seed",
        docker_host="unix:///run/user/1006/docker.sock",
    )
    applied = tmp_path / "settings_applied.json"
    applied.write_text("{}", encoding="utf-8")
    server = _start_isolated_ouroboros_server(
        args,
        tmp_path / "run",
        applied,
        expected_commit,
    )

    assert events[0][0] == "init"
    assert events[0][1:4] == (args.repo_dir, tmp_path / "run", applied)
    assert events[0][4] == args.docker_host
    assert events[0][5]["expected_commit"] == expected_commit
    assert events[0][5]["provider_key"] == "test-provider-key"
    assert events[1] == ("start", 180)
    assert server.base_url == "http://127.0.0.1:18181"
    server.close()
    assert events[-1] == ("close",)


def test_launcher_wraps_server_start_error_and_closes_partial(monkeypatch, tmp_path):
    """Expected server startup errors become typed refusals after cleanup."""
    from types import SimpleNamespace

    import devtools.benchmarks.cybergym.cybergym_server as server_module
    import devtools.benchmarks.cybergym.run_cybergym as launcher

    events: list[str] = []

    class FailingServer:
        def __init__(self, *_args, **_kwargs):
            events.append("init")

        def start(self, *, ready_timeout):
            assert ready_timeout == 180
            events.append("start")
            raise RuntimeError("synthetic startup failure")

        def close(self):
            events.append("close")

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-provider-key")
    monkeypatch.setattr(server_module, "CyberGymIsolatedServer", FailingServer)
    args = SimpleNamespace(
        repo_dir=tmp_path / "seed",
        docker_host="unix:///run/user/1006/docker.sock",
    )

    with pytest.raises(
        launcher.CyberGymIntegrationUnavailable,
        match="isolated Ouroboros server preparation failed: RuntimeError",
    ) as caught:
        launcher._start_isolated_ouroboros_server(
            args,
            tmp_path / "run",
            tmp_path / "settings_applied.json",
            "a" * 40,
        )

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert events == ["init", "start", "close"]


def test_launcher_closes_server_when_executor_construction_fails(monkeypatch, tmp_path):
    """A concrete executor failure must not leave the campaign server alive."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    import devtools.benchmarks.cybergym.run_cybergym as launcher

    repo = tmp_path / "seed"
    source = tmp_path / "cybergym-source"
    data = tmp_path / "cybergym-data"
    tasks = tmp_path / "tasks.json"
    mask_map = tmp_path / "mask-map.json"
    settings_template = tmp_path / "settings.json"
    server_root = tmp_path / "server-root"
    binary_dir = server_root / "bin"
    for directory in (repo, source, data, server_root, binary_dir):
        directory.mkdir(parents=True)
    tasks.write_text("{}", encoding="utf-8")
    mask_map.write_text("{}", encoding="utf-8")
    settings_template.write_text("{}", encoding="utf-8")
    applied = tmp_path / "run" / "settings_applied.json"
    expected_commit = "a" * 40
    events: list[str] = []

    class FakeServer:
        base_url = "http://127.0.0.1:18181"
        attestation = {"repo_head": expected_commit}

        def close(self):
            events.append("server.close")

    server = FakeServer()

    def fake_prepare(_template, out_root, _args):
        applied.parent.mkdir(parents=True, exist_ok=True)
        applied.write_text("{}", encoding="utf-8")
        return applied, {
            "model": OFFICIAL_MODEL,
            "model_slots": {"OUROBOROS_MODEL": OFFICIAL_MODEL},
            "provider_credentials": {},
        }

    @contextmanager
    def fake_finalize(_manifest_path, _manifest, *, outcome="completed", **_kwargs):
        yield {}

    args = SimpleNamespace(
        repo_dir=repo,
        source_root=source,
        data_root=data,
        tasks_file=tasks,
        task_id=["arvo:1"],
        server="http://cybergym-internal:8666",
        ouroboros_url="",
        docker_host="unix:///run/user/1006/docker.sock",
        server_image="cybergym-server",
        server_image_digest="sha256:" + "b" * 64,
        workspace_image="ouroboros-workspace",
        workspace_image_digest="sha256:" + "c" * 64,
        server_root=server_root,
        binary_dir=binary_dir,
        cybergym_api_key_env="CYBERGYM_API_KEY",
        mask_map=mask_map,
        difficulty=DEFAULT_LEVEL,
        model=OFFICIAL_MODEL,
        settings_path=settings_template,
        out_dir=tmp_path / "run",
        run_id="",
        budget_usd=2.0,
        per_task_estimate_usd=1.0,
        timeout_sec=1,
        workers=1,
        executor="",
        dry_run=False,
        allow_dirty_seed=False,
        expected_source_sha256="",
        expected_data_sha256="a" * 64,
        expected_binary_sha256="b" * 64,
        expected_tasks_sha256="",
        expected_mask_sha256="mask-digest",
        cybergym_python="python3",
        provider_only=["provider-a"],
        provider_order=["provider-a"],
    )
    monkeypatch.setattr(launcher, "parse_args", lambda _argv=None: args)
    monkeypatch.setattr(launcher, "pre_admission_report", lambda **_kwargs: {"ok": True, "reasons": []})
    monkeypatch.setattr(
        launcher,
        "admit_benchmark_run",
        lambda _path, **_kwargs: {
            "source": {"head": expected_commit},
            "extra": {},
            "harness": {},
            "output_paths": {},
        },
    )
    monkeypatch.setattr(launcher, "finalize_run_manifest", fake_finalize)
    monkeypatch.setattr(launcher, "verify_source_checkout", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(launcher, "source_tree_digest", lambda *_args, **_kwargs: "source-digest")
    monkeypatch.setattr(
        launcher,
        "verify_mask_map",
        lambda *_args, **_kwargs: {"sha256": "mask-digest"},
    )
    monkeypatch.setattr(
        launcher,
        "load_task_catalog",
        lambda *_args, **_kwargs: {"task_ids": ["arvo:1"]},
    )
    monkeypatch.setattr(launcher, "_prepare_applied_settings", fake_prepare)
    monkeypatch.setattr(
        launcher,
        "_start_isolated_ouroboros_server",
        lambda *_args, **_kwargs: server,
    )

    def fail_build(*_args, **_kwargs):
        events.append("executor.build")
        raise launcher.CyberGymIntegrationUnavailable("synthetic build failure")

    monkeypatch.setattr(launcher, "_build_default_executor", fail_build)
    rc = launcher.main()

    assert rc == 2
    assert events == ["executor.build", "server.close"]


def test_launcher_cleanup_report_preserves_pending_custody(tmp_path):
    """A pending executor close keeps the server alive and is manifest-visible."""
    from devtools.benchmarks.common.manifests import finalize_run_manifest
    from devtools.benchmarks.cybergym.run_cybergym import _cleanup_execution_resources

    events: list[str] = []

    class FakeExecutor:
        def close(self):
            events.append("executor.close")
            return {
                "ok": False,
                "status": "custody_pending",
                "attempt_id": "a01",
            }

    class FakeServer:
        def close(self):
            events.append("server.close")

    manifest = {"extra": {}, "run_root": str(tmp_path)}
    manifest_path = tmp_path / "run_manifest.json"
    with finalize_run_manifest(manifest_path, manifest):
        _cleanup_execution_resources(FakeExecutor(), FakeServer(), manifest)

    assert events == ["executor.close"]
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    extra = persisted["extra"]
    assert extra["executor_cleanup"]["ok"] is False
    assert extra["executor_cleanup"]["attempt_id"] == "a01"
    assert extra["server_cleanup"] == {
        "attempted": True,
        "close_skipped": True,
        "status": "skipped_custody",
    }
    assert extra["close_skipped"] is True
