"""AC21 / AC22 — an evolution cycle's outcome is a usable, truthful memory.

This is the user's stated flagship use for Engram: "Engram 适合保存自动进化任务结束后的内容"
— the verified record of which objectives actually improved the system. Two things
make such a record usable, and both are asserted here:

* **truthfulness** (AC21a): a cycle whose verdict is already decided must not be
  filed as still-pending. The live ledger made this concrete — nine cycles in a
  row are ``no_op`` with ``restart_required=False``, none of which would ever reach
  a second write, so a hardcoded ``waiting_for_restart`` would have been permanent.
* **completeness** (AC22): ``abandoned`` with no reason is not a memory, it is a
  shrug — a later reader cannot tell hopeless from blocked from deprioritised.
"""

from __future__ import annotations

from ouroboros.engram_sink import emit_evolution_outcome, reset_sinks, sink_for
from ouroboros.evolution_checkpoints import resolve_reported_cycle_outcome


def _records(state, prefix: str = "evolution_cycle:") -> list[dict]:
    return [
        rec
        for rec in state.knowledge.values()
        if str(rec.get("topic_key") or "").startswith(prefix)
    ]


def _content(state, prefix: str = "evolution_cycle:") -> str:
    records = _records(state, prefix)
    assert len(records) == 1, f"expected exactly one record, got {len(records)}: {records}"
    return str(records[0].get("content") or "")


# --------------------------------------------------------------------------- #
# AC21(a) — the task-done write must not lie about being final
# --------------------------------------------------------------------------- #


def test_a_settled_verdict_is_reported_as_settled():
    """`no_op` / `abandoned` have no second phase, so they must be filed now."""
    assert resolve_reported_cycle_outcome(
        {"cycle_outcome": "no_op", "restart_required": False}
    ) == "no_op"
    assert resolve_reported_cycle_outcome(
        {"cycle_outcome": "abandoned", "restart_required": False}
    ) == "abandoned"
    assert resolve_reported_cycle_outcome(
        {"cycle_outcome": "absorbed", "restart_required": False, "restart_verified": False}
    ) == "absorbed"


def test_an_absorbing_cycle_still_waits_for_its_restart():
    """The two-phase write is not abolished — only narrowed to the case it fits."""
    assert resolve_reported_cycle_outcome(
        {"cycle_outcome": "absorbed", "restart_required": True, "restart_verified": False}
    ) == "waiting_for_restart"
    # ...and a verified restart is no longer pending.
    assert resolve_reported_cycle_outcome(
        {"cycle_outcome": "absorbed", "restart_required": True, "restart_verified": True}
    ) == "absorbed"


def test_an_empty_verdict_is_never_laundered_into_pending():
    """'Nobody recorded an answer' must not be reported as 'an answer is coming'."""
    assert resolve_reported_cycle_outcome({}) == "unknown"
    assert resolve_reported_cycle_outcome({"cycle_outcome": "   "}) == "unknown"
    # But a genuinely pending restart with no verdict yet IS pending.
    assert resolve_reported_cycle_outcome({"restart_required": True}) == "waiting_for_restart"


def test_an_unknown_verdict_is_passed_through_not_rewritten():
    """A verdict we do not recognise is still the transaction's own word."""
    assert resolve_reported_cycle_outcome({"cycle_outcome": "infra_failed"}) == "infra_failed"


def test_the_live_ledger_shape_no_longer_produces_a_pending_lie(engram_stub):
    """Regression against the 9/9 real rows: `no_op`, no restart -> `no_op`."""
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "evolution_checkpoint",
            "task_id": "a7ce56aa",
            "campaign_id": "b4279442",
            "campaign_objective": "Autonomously improve Ouroboros.",
            "cycle_outcome": resolve_reported_cycle_outcome(
                {"cycle_outcome": "no_op", "restart_required": False}
            ),
            "outcome_axes": {"lifecycle": {"status": "failed"}},
        },
    )
    stored = _content(state)
    assert "outcome: no_op" in stored
    assert "waiting_for_restart" not in stored


def test_an_empty_verdict_reaches_engram_as_unknown(engram_stub):
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {"kind": "evolution_checkpoint", "task_id": "T-empty", "campaign_objective": "obj"},
    )
    assert "outcome: unknown" in _content(state)


# --------------------------------------------------------------------------- #
# AC21(b)(c) — the two-phase write upserts on task_id, and carries the evidence
# --------------------------------------------------------------------------- #


def test_the_two_phases_upsert_one_record_keyed_by_task_id(engram_stub):
    """C19: the later verdict must UPDATE the task-done row, not add a second one."""
    state, env = engram_stub
    axes = {"lifecycle": {"status": "succeeded"}, "artifacts": {"status": "committed"}}

    # Phase 1 — task-done: the commit is not verified yet, so this is genuinely pending.
    assert emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "evolution_checkpoint",
            "task_id": "T1",
            "campaign_id": "C1",
            "campaign_objective": "Make recall cheaper.",
            "cycle_outcome": "waiting_for_restart",
            "outcome_axes": axes,
        },
    )
    first = _content(state)
    assert "outcome: waiting_for_restart" in first
    assert "Make recall cheaper." in first
    assert "commit:" not in first

    # Phase 2 — after the restart verified the commit, the real verdict arrives.
    assert emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "cycle_outcome",
            "task_id": "T1",
            "campaign_id": "C1",
            "campaign_objective": "Make recall cheaper.",
            "cycle_outcome": "absorbed",
            "commit_sha": "abc1234",
            "outcome_axes": axes,
        },
    )

    records = _records(state)
    assert len(records) == 1, f"the second phase added a record: {records}"
    final = str(records[0]["content"])
    assert "outcome: absorbed" in final
    assert "waiting_for_restart" not in final
    assert "commit: abc1234" in final
    assert "outcome_axes:" in final and "committed" in final
    reset_sinks()


def test_the_phases_join_even_when_only_the_campaign_id_is_shared(engram_stub):
    """Identity falls back to campaign_id, so a task_id-less phase still joins."""
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {"kind": "evolution_checkpoint", "campaign_id": "C9", "campaign_objective": "obj"},
    )
    emit_evolution_outcome(
        env.drive_root,
        {"kind": "cycle_outcome", "campaign_id": "C9", "campaign_objective": "obj",
         "cycle_outcome": "absorbed", "commit_sha": "deadbee"},
    )
    assert len(_records(state)) == 1
    assert "outcome: absorbed" in _content(state)
    reset_sinks()


# --------------------------------------------------------------------------- #
# AC22 — an outcome without its objective, or a shrug without a reason
# --------------------------------------------------------------------------- #


def test_every_outcome_carries_what_it_was_trying_to_achieve(engram_stub):
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "cycle_outcome",
            "task_id": "T2",
            "campaign_objective": "Reduce prompt bloat without losing recall.",
            "cycle_outcome": "absorbed",
        },
    )
    stored = _content(state)
    assert "objective: Reduce prompt bloat without losing recall." in stored
    assert "outcome: absorbed" in stored


def test_an_abandoned_cycle_carries_its_reason(engram_stub):
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "cycle_outcome",
            "task_id": "T3",
            "campaign_objective": "Rewrite the storage layer.",
            "cycle_outcome": "abandoned",
            "abandoned_reason": "upstream merge conflict made the change unsafe",
        },
    )
    stored = _content(state)
    assert "outcome: abandoned" in stored
    assert "reason: upstream merge conflict made the change unsafe" in stored


def test_an_abandoned_cycle_without_a_reason_says_so(engram_stub):
    """A silent gap would read as 'there was no reason' — it must be visible."""
    state, env = engram_stub
    emit_evolution_outcome(
        env.drive_root,
        {
            "kind": "cycle_outcome",
            "task_id": "T4",
            "campaign_objective": "obj",
            "cycle_outcome": "abandoned",
        },
    )
    stored = _content(state)
    assert "outcome: abandoned" in stored
    assert "reason: (not recorded)" in stored


def test_the_objective_is_present_even_when_the_campaign_is_unknown(engram_stub):
    """An unknown objective must be marked, never omitted (the title depends on it)."""
    state, env = engram_stub
    emit_evolution_outcome(env.drive_root, {"kind": "cycle_outcome", "task_id": "T5"})
    records = _records(state)
    assert len(records) == 1
    assert "objective: (unknown)" in str(records[0]["content"])
    assert str(records[0]["title"])


def test_the_outcome_write_is_soft_when_engram_is_down(engram_stub):
    """C6: a memory-transport failure must never fail the evolution cycle."""
    state, env = engram_stub
    state.fail = True
    assert emit_evolution_outcome(
        env.drive_root,
        {"kind": "cycle_outcome", "task_id": "T6", "campaign_objective": "obj",
         "cycle_outcome": "no_op"},
    ) is True
    # Nothing lost: it is spooled for the next run (C17).
    assert sink_for(env).pending_count() == 1
    reset_sinks()


# --------------------------------------------------------------------------- #
# Wiring — the task-done path is what actually runs in production
# --------------------------------------------------------------------------- #


def _drive_terminal(env, *, transaction, task_id="T-evo"):
    """Run the real task-done handler with its heavy collaborators stubbed.

    The supervisor hands the handler its own ``ctx`` shape (``DRIVE_ROOT`` /
    ``REPO_DIR``), which is not the ``Env`` shape the rest of the suite uses — so
    the two are bridged here rather than pretending one object is the other.
    """
    from types import SimpleNamespace

    from supervisor import events

    ctx = SimpleNamespace(DRIVE_ROOT=env.drive_root, REPO_DIR=env.repo_dir)
    events._handle_evolution_task_done(
        ctx,
        evt={},
        task_id=task_id,
        task={"metadata": {"evolution_transaction": transaction}},
        task_done_event={},
        outcome_axes={"lifecycle": {"status": "succeeded"}},
        cost=0.0,
        rounds=1,
    )


def _stub_lifecycle(monkeypatch, transaction):
    from supervisor import evolution_lifecycle

    monkeypatch.setattr(
        evolution_lifecycle,
        "update_evolution_campaign_after_task",
        lambda *a, **k: {"accepted": True, "persisted": True, "transaction": transaction},
    )
    monkeypatch.setattr(
        evolution_lifecycle,
        "_read_evolution_campaign",
        lambda: {"id": "C1", "objective": "Improve recall."},
    )
    monkeypatch.setattr(
        "ouroboros.evolution_checkpoints.append_evolution_checkpoint", lambda *a, **k: None
    )


def test_task_done_files_the_real_verdict_for_a_settled_cycle(engram_stub, monkeypatch):
    """The production handler, not just the sink: a `no_op` must not read as pending."""
    state, env = engram_stub
    _stub_lifecycle(
        monkeypatch,
        {"task_id": "T-evo", "cycle_outcome": "no_op", "restart_required": False},
    )

    _drive_terminal(env, transaction={"task_id": "T-evo", "cycle_outcome": "no_op"})

    body = _content(state)
    assert "outcome: no_op" in body
    assert "waiting_for_restart" not in body
    assert "Improve recall." in body
    reset_sinks()


def test_task_done_still_defers_an_unverified_absorb(engram_stub, monkeypatch):
    """The narrowed two-phase rule must still hold for the case it exists for."""
    state, env = engram_stub
    _stub_lifecycle(
        monkeypatch,
        {
            "task_id": "T-evo2",
            "cycle_outcome": "absorbed",
            "restart_required": True,
            "restart_verified": False,
        },
    )

    _drive_terminal(env, transaction={"task_id": "T-evo2", "cycle_outcome": "absorbed"})

    body = _content(state)
    assert "outcome: waiting_for_restart" in body
    reset_sinks()
