"""Panic stop must complete every teardown step, disclose what it cannot do, and hard-exit.

``execute_panic_stop`` kills workers, tracked shells, services, companions and
ports, then hard-exits. The BIBLE Emergency Stop Invariant requires ALL
subprocess trees to die on panic, and nothing (including a failing teardown
step) may delay the hard exit.

The owned ``claudexord`` is no longer one of those trees. Its custody module
retired with the Claudexor cut, so panic has no process group to kill there —
and it now says so on the log instead of leaving an operator to read a clean
panic as "every tree was stopped".
"""

from types import SimpleNamespace

import pytest


class _ExitCalled(RuntimeError):
    pass


def _run_panic(monkeypatch, tmp_path):
    """Run execute_panic_stop with every destructive teardown op neutralized.

    Returns the recorded kill_workers_fn calls (proof that teardown ran) and the
    log records it emitted.
    """
    from ouroboros import server_control

    monkeypatch.setattr("ouroboros.tools.shell.kill_all_tracked_subprocesses", lambda: None)
    monkeypatch.setattr("ouroboros.workspace_executor.kill_all_foreground", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.tools.services.kill_all_services", lambda *a, **k: None)
    monkeypatch.setattr(
        "ouroboros.local_model.get_manager",
        lambda: SimpleNamespace(stop_server=lambda: None),
    )
    monkeypatch.setattr("supervisor.state.load_state", dict)
    monkeypatch.setattr("supervisor.state.save_state", lambda _state: None)
    monkeypatch.setattr(
        "supervisor.evolution_lifecycle.complete_evolution_campaign", lambda *a, **k: {}
    )
    monkeypatch.setattr("ouroboros.post_task_evolution.drop_pending_request", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda: None)
    monkeypatch.setattr("multiprocessing.active_children", list)
    monkeypatch.setattr("ouroboros.platform_layer.kill_process_on_port", lambda _port: None)
    monkeypatch.setattr("ouroboros.platform_layer.force_kill_pid", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.gateway.host_service.host_service_port", lambda: 8767)
    monkeypatch.setattr(
        server_control.os, "_exit", lambda code: (_ for _ in ()).throw(_ExitCalled(code))
    )

    logged: list[tuple[str, str]] = []
    worker_calls = []
    with pytest.raises(_ExitCalled):
        server_control.execute_panic_stop(
            consciousness=SimpleNamespace(stop=lambda: None),
            kill_workers_fn=lambda **kw: worker_calls.append(kw),
            data_dir=tmp_path,
            panic_exit_code=120,
            log=SimpleNamespace(
                critical=lambda msg, *a, **k: logged.append(("critical", str(msg))),
                warning=lambda msg, *a, **k: logged.append(("warning", str(msg))),
            ),
        )
    return worker_calls, logged


def test_panic_stop_tears_down_and_hard_exits(monkeypatch, tmp_path):
    """Every teardown step still runs and the hard exit still happens."""
    worker_calls, _ = _run_panic(monkeypatch, tmp_path)
    assert worker_calls == [{
        "force": True, "archive_service_logs": False,
        "reconcile_delegate_custody": False,
    }]


def test_panic_stop_discloses_retired_daemon_custody(monkeypatch, tmp_path):
    """The retired daemon custody is announced, not silently skipped.

    A panic that cannot kill something must say so: silence here is how an
    operator concludes every subprocess tree died when one was never touched.
    """
    _, logged = _run_panic(monkeypatch, tmp_path)
    assert any(
        level == "warning" and "daemon custody is unavailable" in message
        for level, message in logged
    )
