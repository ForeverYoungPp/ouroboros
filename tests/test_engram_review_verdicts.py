"""S6 — review verdicts are reachable from Engram, and the section still exists.

The local review ledger is a live STATE MACHINE — the commit gate's attempts,
obligations and readiness debts — so it stays local (C4/C18: only the verdict is a
memory, and ``open_obligations`` is explicitly a work item). What Engram adds is the
durable half: the verdicts survive a lost or fresh drive.

The failing behaviour this guards is a lie of omission with a friendly face:
``format_status_section`` answers "No advisory runs recorded yet." when the local
state is empty, which on a fresh drive is indistinguishable from "this repo has
never been reviewed" — even though Engram holds the verdicts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.context import _capture_context_core, _engram_verdict_section
from ouroboros.engram_read import client_for, type_digest
from ouroboros.engram_sink import emit_review_verdicts, reset_sinks
from ouroboros.memory import Memory


@pytest.fixture()
def scaffold(engram_stub, tmp_path):
    """A drive minimal enough to build the context core, plus the shared Engram."""
    state, env_stub = engram_stub
    repo = env_stub.repo_dir
    root = env_stub.drive_root
    (repo / "prompts").mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "prompts" / "SYSTEM.md").write_text("base prompt", encoding="utf-8")
    (repo / "BIBLE.md").write_text("# BIBLE\n\nP1: continuity.", encoding="utf-8")
    (repo / "docs" / "ARCHITECTURE.md").write_text("# ARCH", encoding="utf-8")
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "identity.md").write_text("I am a test identity.", encoding="utf-8")
    (mem / "WORLD.md").write_text("host: test", encoding="utf-8")
    (mem / "scratchpad.md").write_text("scratchpad body", encoding="utf-8")
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    env = SimpleNamespace(
        repo_dir=repo,
        drive_root=root,
        budget_drive_root=root,
        branch_dev="ouroboros",
        repo_path=lambda rel: repo / rel,
        drive_path=lambda rel: root / rel,
    )
    return state, env, Memory(drive_root=root, repo_dir=repo)


def _dynamic_text(scaffold) -> str:
    state, env, mem = scaffold
    task = {"id": "t-verdicts", "type": "direct_chat", "chat_id": 1}
    core = _capture_context_core(env, mem, task, None, None)
    return core.dynamic_text or ""


# --------------------------------------------------------------------------- #
# The write half — only the verdict is a memory
# --------------------------------------------------------------------------- #


def test_only_the_verdict_reaches_engram(scaffold):
    state, env, _mem = scaffold
    emit_review_verdicts(
        env,
        [{
            "verdict": "FAIL",
            "task_id": "t1",
            "summary": "missing receipt",
            "open_obligations": ["write the regression test"],
            "proposed_next_step": "rerun the lane",
        }],
        task_id="t1",
    )

    stored = [rec for rec in state.knowledge.values() if rec.get("type") == "review_verdict"]
    assert len(stored) == 1
    assert stored[0]["topic_key"].startswith("review_verdict:t1")
    assert "FAIL" in stored[0]["content"]
    # C18: the work items did not ride along.
    blob = str(stored[0]).lower()
    assert "open_obligations" not in blob
    assert "write the regression test" not in blob
    assert "rerun the lane" not in blob
    reset_sinks()


def test_a_mutation_through_update_state_mirrors_the_verdict(scaffold):
    """The PRODUCTION mutation path must mirror, not just ``save_state``.

    Every real mutation goes through ``update_state``; ``save_state`` has no caller
    outside the tests. Mirroring only in ``save_state`` sent no verdict at all,
    however many were recorded — while the prompt section that reads them kept
    rendering, so the two halves never met. This drives the path production uses.
    """
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord, update_state

    state, env, _mem = scaffold

    def _record(review_state: AdvisoryReviewState) -> None:
        # An ATTEMPT is what the mirror reads (`latest_attempt`); advisory runs are
        # a different ledger.
        review_state.attempts.append(
            CommitAttemptRecord(
                ts="2026-01-01T00:00:00+00:00",
                status="FAIL",
                snapshot_hash="feedfacecafe",
                block_reason="gate refused: missing receipt",
                commit_message="the commit the gate refused",
                task_id="t-update-state",
                attempt=1,
            )
        )

    update_state(env.drive_root, _record)

    stored = [rec for rec in state.knowledge.values() if rec.get("type") == "review_verdict"]
    assert len(stored) == 1
    assert "FAIL" in stored[0]["content"]
    reset_sinks()


# --------------------------------------------------------------------------- #
# The read half — bounded, typed, and only on the cold path
# --------------------------------------------------------------------------- #


def test_verdicts_are_readable_back_as_a_bounded_digest(scaffold):
    state, env, _mem = scaffold
    emit_review_verdicts(
        env,
        [{"verdict": "PASS", "task_id": "t2", "summary": "gate green"}],
        task_id="t2",
    )
    reset_sinks()

    read = type_digest(client_for(env), "review_verdict", limit=8, window=50)

    assert read.ok and read.count == 1
    assert "Review verdict: PASS" in read.text
    assert len(state.requests) >= 1


def test_the_section_reports_verdicts_engram_holds(scaffold):
    state, env, _mem = scaffold
    emit_review_verdicts(
        env,
        [{"verdict": "PASS", "task_id": "t3", "summary": "gate green"}],
        task_id="t3",
    )
    reset_sinks()

    section = _engram_verdict_section(env)

    assert section.startswith("## Review verdicts (Engram, 1)")
    assert "historical" in section
    assert "review_status" in section


def test_an_unreachable_store_is_not_rendered_as_no_verdicts(scaffold):
    state, env, _mem = scaffold
    state.fail = True

    section = _engram_verdict_section(env)

    assert "could not be read" in section
    assert "UNKNOWN" in section
    assert "not absent" in section


def test_no_verdicts_at_all_renders_nothing(scaffold):
    """An empty store must add no prompt noise."""
    state, env, _mem = scaffold
    assert _engram_verdict_section(env) == ""


# --------------------------------------------------------------------------- #
# The wiring — the section actually reaches the assembled context
# --------------------------------------------------------------------------- #


def test_the_assembled_context_carries_the_engram_verdicts(scaffold):
    state, env, _mem = scaffold
    emit_review_verdicts(
        env,
        [{"verdict": "FAIL", "task_id": "t4", "summary": "missing receipt"}],
        task_id="t4",
    )
    reset_sinks()

    text = _dynamic_text(scaffold)

    assert "## Review verdicts (Engram, 1)" in text


def test_the_local_ledger_still_wins_when_it_has_something(scaffold):
    """The local state machine is authoritative; Engram is the durable fallback."""
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, save_state

    state, env, mem = scaffold
    emit_review_verdicts(
        env, [{"verdict": "FAIL", "task_id": "t5", "summary": "from engram"}], task_id="t5"
    )
    reset_sinks()
    save_state(
        env.drive_root,
        AdvisoryReviewState(advisory_runs=[
            AdvisoryRunRecord(
                ts="2026-01-01T00:00:00+00:00", status="PASS",
                snapshot_hash="deadbeefcafe", commit_message="local ledger row",
            )
        ]),
    )

    text = _dynamic_text(scaffold)

    assert "## Advisory Pre-Review Status" in text
    assert "local ledger row" in text
    assert "## Review verdicts (Engram" not in text


def test_an_unresolvable_scope_is_not_rendered_as_no_verdicts(scaffold, monkeypatch):
    """`client_for` fails closed on an unresolvable project. The verdict seam
    must map that to the UNKNOWN-family disclosure, not the blank that reads
    as "no verdicts ever recorded"."""
    monkeypatch.setenv("ENGRAM_PROJECT", "local")  # forbidden name
    from ouroboros.engram_sink import reset_sinks

    reset_sinks()
    state, env, _mem = scaffold
    section = _engram_verdict_section(env)
    assert "UNKNOWN" in section
    assert "not absent" in section
    reset_sinks()
