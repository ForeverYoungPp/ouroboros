"""Tests for crash report lifecycle and health invariant integrity.

Verifies:
- crash_report.json is NOT deleted during startup verification
- build_health_invariants detects crash_report.json
"""
import inspect
import json
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_verify_system_state_does_not_delete_crash_file():
    """Startup verification must NOT call unlink() on the crash report file.

    The crash_report.json must persist so build_health_invariants() surfaces
    it on every task until the agent investigates and removes it.
    """
    from ouroboros import agent_startup_checks
    source = inspect.getsource(agent_startup_checks.verify_system_state)
    source += inspect.getsource(agent_startup_checks.inject_crash_report)
    assert "unlink" not in source, (
        "startup verification still deletes crash_report.json — "
        "health_invariants won't see it. File must persist until agent clears it."
    )


def test_the_engram_boot_work_is_not_downstream_of_another_boot_check():
    """F2: the durable-memory work must not be able to be muted at boot.

    It used to live inside verify_system_state, after the git and budget checks,
    under the caller's single broad handler whose once-guard is set BEFORE the
    work. A raise in any of those checks therefore skipped the spool drain and the
    dialogue carry-over — and kept skipping them for the whole life of the process,
    because the guard was already set and nothing re-runs it. The property is an
    ORDER, so pin the order.
    """
    from ouroboros import agent_startup_checks
    from ouroboros.agent import OuroborosAgent

    verify = inspect.getsource(agent_startup_checks.verify_system_state)
    assert "flush_engram_spool" not in verify
    assert "reconcile_local_dialogue_blocks" not in verify

    boot_actions = inspect.getsource(agent_startup_checks.run_engram_boot_actions)
    assert "flush_engram_spool" in boot_actions
    assert "reconcile_local_dialogue_blocks" in boot_actions

    boot = inspect.getsource(OuroborosAgent._log_worker_boot_once)
    assert boot.index("run_engram_boot_actions") < boot.index("verify_restart")
    assert boot.index("run_engram_boot_actions") < boot.index("verify_system_state")
    # ...and before the git lookup, which can itself raise.
    assert boot.index("run_engram_boot_actions") < boot.index("get_git_info")


def test_health_invariants_detects_crash_report():
    """build_health_invariants must check for crash_report.json."""
    from ouroboros.context import build_health_invariants
    source = inspect.getsource(build_health_invariants)
    assert "crash_report.json" in source, (
        "build_health_invariants does not check for crash_report.json"
    )
    assert "CRASH ROLLBACK" in source, (
        "build_health_invariants does not produce CRASH ROLLBACK warning"
    )


def test_crash_event_logged_at_startup():
    """Startup crash-report injection must log crash_rollback_detected event."""
    from ouroboros.agent_startup_checks import inject_crash_report
    source = inspect.getsource(inject_crash_report)
    assert "crash_rollback_detected" in source, (
        "startup crash-report injection does not log crash_rollback_detected event"
    )


def test_invalid_crash_report_logs_corruption_event(tmp_path):
    """A corrupt crash report must stay visible instead of being treated as absent."""
    from ouroboros.agent_startup_checks import inject_crash_report

    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "state" / "crash_report.json").write_text("{broken", encoding="utf-8")
    env = types.SimpleNamespace(drive_path=lambda rel: tmp_path / rel)

    inject_crash_report(env)

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["type"] == "crash_report_invalid"
