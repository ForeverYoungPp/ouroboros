"""Poltergeist phase A — durable cancel-intent lifecycle (owner batch-2 4=A, batch-4 1=A).

Closes the incident classes with tests:
- the wedged ``cancel_requested`` latch (intent survives a lost event; the
  supervisor watchdog feeds custody, the ONE settle owner);
- completed-result erasure by a late cancel (natural completion WINS, E2E
  through the real kill path with a live split-drive worker);
- the fabricated final-$0 cancel accounting;
- the undelivered salvaged answer (durable outbox seam, honest omitted counts);
- nonterminal ``task_done`` publication (durable lifecycle fault, no release).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import types

import pytest

from ouroboros import cancel_intents as ci
from ouroboros.task_results import (
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    load_task_result,
    write_task_result,
)


# --------------------------------------------------------------------------
# Intent store semantics
# --------------------------------------------------------------------------


def test_request_cancel_is_idempotent_and_forensically_logged(tmp_path):
    first = ci.request_cancel(tmp_path, "t1", reason="stop it", source="agent_tool",
                              requested_by="parent1")
    assert first["state"] == ci.INTENT_REQUESTED
    assert first["already_requested"] is False
    assert first["request_id"].startswith("ci_")

    second = ci.request_cancel(tmp_path, "t1", reason="again")
    assert second["already_requested"] is True
    assert second["request_id"] == first["request_id"]
    # The projection stays compact: one active row.
    assert list(ci.active_intents(tmp_path)) == ["t1"]

    trail = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in trail.splitlines() if line.strip()]
    requested = [r for r in rows if r.get("type") == "cancel_intent" and r.get("event") == "requested"]
    assert len(requested) == 1 and requested[0]["task_id"] == "t1"


def test_claim_settle_and_release_lifecycle(tmp_path):
    ci.request_cancel(tmp_path, "t2", source="http_single")
    claimed = ci.claim_intent(tmp_path, "t2", owner="cancel_task_custody")
    assert claimed["state"] == ci.INTENT_CLAIMED
    assert claimed["generation"] == 1

    # A failed custody attempt releases the claim; the watchdog can re-feed it.
    assert ci.release_claim(tmp_path, "t2", error="worker refused to die") is True
    row = ci.active_intent(tmp_path, "t2")
    assert row["state"] == ci.INTENT_REQUESTED
    assert row["last_error"] == "worker refused to die"

    reclaimed = ci.claim_intent(tmp_path, "t2", owner="cancel_task_custody")
    assert reclaimed["generation"] == 2

    settled = ci.settle_intent(tmp_path, "t2", outcome="cancelled", detail="teardown ok")
    assert settled["request_id"] == row["request_id"]
    # Settled rows LEAVE the projection (compactness is the design).
    assert ci.active_intent(tmp_path, "t2") is None
    assert ci.settle_intent(tmp_path, "t2", outcome="cancelled") is None  # idempotent

    assert ci.release_claim(tmp_path, "t2", error="already released") is False

    # Claim staleness: a fresh claim is respected; unreadable provenance is stale.
    ci.request_cancel(tmp_path, "t3")
    fresh = ci.claim_intent(tmp_path, "t3", owner="x")
    assert ci.claim_is_stale(fresh) is False
    assert ci.claim_is_stale({**fresh, "claimed_at": "not-a-time"}) is True


def test_cancel_state_fields_and_migration(tmp_path):
    assert ci.cancel_state_fields(tmp_path, "none") == {}
    ci.request_cancel(tmp_path, "t4", reason="why not")
    fields = ci.cancel_state_fields(tmp_path, "t4")
    assert fields == {"cancel_state": "pending", "cancel_reason": "why not"}

    # Boot migration: a legacy latch file becomes a synthetic active intent;
    # the file itself is untouched (legacy read-path).
    write_task_result(tmp_path, "legacy1", STATUS_CANCEL_REQUESTED, result="wedged")
    migrated = ci.migrate_legacy_cancel_latches(tmp_path)
    assert migrated == ["legacy1"]
    intent = ci.active_intent(tmp_path, "legacy1")
    assert intent["source"] == "boot_migration"
    assert load_task_result(tmp_path, "legacy1")["status"] == STATUS_CANCEL_REQUESTED
    # Idempotent at the next boot.
    assert ci.migrate_legacy_cancel_latches(tmp_path) == []


def test_effective_read_projects_pending_for_intent_and_legacy_latch(tmp_path):
    from ouroboros.task_status import load_effective_task_result

    write_task_result(tmp_path, "run1", STATUS_RUNNING, result="working")
    ci.request_cancel(tmp_path, "run1")
    eff = load_effective_task_result(tmp_path, "run1")
    assert eff["status"] == STATUS_RUNNING and eff["cancel_state"] == "pending"

    write_task_result(tmp_path, "legacy2", STATUS_CANCEL_REQUESTED)
    eff = load_effective_task_result(tmp_path, "legacy2")
    assert eff["cancel_state"] == "pending"

    # A settled task never carries the pending projection.
    write_task_result(tmp_path, "done1", STATUS_COMPLETED, result="ok")
    ci.request_cancel(tmp_path, "done1")
    assert "cancel_state" not in load_effective_task_result(tmp_path, "done1")


def test_fail_tasks_honors_active_intent(tmp_path):
    from ouroboros.task_results import fail_tasks

    write_task_result(tmp_path, "b1", "scheduled")
    ci.request_cancel(tmp_path, "b1", reason="owner cancel")
    written = fail_tasks(
        tmp_path, [{"id": "b1"}], reason_code="budget_exhausted", result="drained",
    )
    assert written == 1
    assert load_task_result(tmp_path, "b1")["status"] == STATUS_CANCELLED
    assert ci.active_intent(tmp_path, "b1") is None  # settled by the drain


# --------------------------------------------------------------------------
# Supervisor integration: custody, watchdog, restore, pending drop
# --------------------------------------------------------------------------


@pytest.fixture()
def qenv(tmp_path, monkeypatch):
    import supervisor.queue as q
    from supervisor import task_lifecycle, workers

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "RUNNING", {}, raising=False)
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(workers, "respawn_worker", lambda wid: None, raising=False)
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(task_lifecycle, "CANCELLED_ROOT_FENCES", {}, raising=False)
    monkeypatch.setattr(task_lifecycle, "_ACTIVE_CASCADE_FENCES", {}, raising=False)
    return types.SimpleNamespace(q=q, tl=task_lifecycle, workers=workers, drive=tmp_path)


def test_custody_settles_an_intent_for_a_missing_task(qenv):
    """The incident's wedge: intent recorded, task neither queued nor running —
    custody's finalize-on-miss settles it as cancelled with the parent decision
    stamped at OUTCOME (never at intent time)."""
    ci.request_cancel(qenv.drive, "ghost1", reason="tree teardown",
                      requested_by="parent9")
    write_task_result(qenv.drive, "ghost1", STATUS_RUNNING, result="was running")

    outcome = qenv.tl.cancel_task_custody("ghost1")

    assert outcome == qenv.tl.CANCEL_CANCELLED
    stored = load_task_result(qenv.drive, "ghost1")
    assert stored["status"] == STATUS_CANCELLED
    assert stored["parent_decision"] == "cancelled"
    assert stored["parent_decision_reason"] == "tree teardown"
    # Honest accounting: reconstructed (confirmed zero here), never a missing block.
    assert "cost_accounting_status" in stored
    assert ci.active_intent(qenv.drive, "ghost1") is None


def test_watchdog_sweep_feeds_open_and_stale_claimed_intents(qenv, monkeypatch):
    fed: list[str] = []
    monkeypatch.setattr(qenv.tl, "cancel_task_custody",
                        lambda tid, **_kw: fed.append(tid) or "cancelled")

    now = 1_000_000.0
    # Open old intent: fed.
    ci.request_cancel(qenv.drive, "old1")
    # Freshly claimed intent: custody in flight — left alone.
    ci.request_cancel(qenv.drive, "claimed1")
    ci.claim_intent(qenv.drive, "claimed1", owner="cancel_task_custody")

    from datetime import datetime, timezone
    aged = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    stale = datetime.fromtimestamp(now - ci.CLAIM_STALE_SEC - 5, tz=timezone.utc).isoformat()
    # Rewrite provenance directly (test-only): age the open intent past the
    # watchdog min-age and make one claim stale.
    store = qenv.drive / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["old1"]["requested_at"] = aged
    claimed_now = datetime.fromtimestamp(now - 1, tz=timezone.utc).isoformat()
    data["intents"]["claimed1"]["claimed_at"] = claimed_now
    data["intents"]["claimed1"]["requested_at"] = aged
    store.write_text(json.dumps(data), encoding="utf-8")

    outcomes = qenv.tl.sweep_cancel_intents(now=now)
    assert fed == ["old1"]
    assert outcomes == {"old1": "cancelled"}
    ci.settle_intent(qenv.drive, "old1", outcome="cancelled")  # what real custody does

    # GR3-2: the same claim gone STALE while its claimant pid (this test
    # process) probes ALIVE is NEVER stolen by age — the live owner settles or
    # releases; stealing it would let two custodies double-settle.
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["claimed1"]["claimed_at"] = stale
    store.write_text(json.dumps(data), encoding="utf-8")
    fed.clear()
    qenv.tl.sweep_cancel_intents(now=now)
    assert fed == []

    # Stale with liveness UNKNOWN (pid missing — the incident shape: custody
    # died mid-teardown before/without a readable pid) IS still recoverable.
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["claimed1"].pop("claim_pid", None)
    store.write_text(json.dumps(data), encoding="utf-8")
    fed.clear()
    qenv.tl.sweep_cancel_intents(now=now)
    assert fed == ["claimed1"]

    # A brand-new intent is left one tick for its own control event.
    fed.clear()
    ci.request_cancel(qenv.drive, "young1")
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["young1"]["requested_at"] = datetime.fromtimestamp(
        now - 1, tz=timezone.utc,
    ).isoformat()
    store.write_text(json.dumps(data), encoding="utf-8")
    qenv.tl.sweep_cancel_intents(now=now)
    assert "young1" not in fed


def test_drop_cancelled_pending_consults_the_intent_projection(qenv, monkeypatch):
    from supervisor import workers

    emitted: list = []
    monkeypatch.setattr(workers, "_emit_task_done_terminal",
                        lambda task, tid, status, **kw: emitted.append((tid, status, kw)) or True)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)

    qenv.q.PENDING[:] = [
        {"id": "keepme", "chat_id": 1},
        {"id": "dropme", "chat_id": 1},
    ]
    write_task_result(qenv.drive, "dropme", "scheduled")
    ci.request_cancel(qenv.drive, "dropme", reason="parent stopped the plan")

    workers._drop_cancelled_pending()

    assert [t["id"] for t in qenv.q.PENDING] == ["keepme"]
    stored = load_task_result(qenv.drive, "dropme")
    assert stored["status"] == STATUS_CANCELLED
    assert "cost_accounting_status" in stored  # reconstructed, not omitted
    assert ci.active_intent(qenv.drive, "dropme") is None
    assert emitted and emitted[0][0] == "dropme" and emitted[0][1] == "cancelled"


def test_snapshot_restore_refuses_a_task_with_active_intent(qenv, monkeypatch):
    from ouroboros.utils import utc_now_iso

    ci.request_cancel(qenv.drive, "restoreme")
    snapshot = {
        "ts": utc_now_iso(),
        "pending": [{"task": {"id": "restoreme", "chat_id": 1, "type": "chat"}}],
        "running": [],
        "acceptance_fences": [],
        "budget_root_fences": [],
    }
    state_dir = qenv.drive / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "queue_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(qenv.q, "QUEUE_SNAPSHOT_PATH", state_dir / "queue_snapshot.json",
                        raising=False)

    restored = qenv.q.restore_pending_from_snapshot()

    assert restored == 0
    assert qenv.q.PENDING == []


# --------------------------------------------------------------------------
# task_done validation (A1.7)
# --------------------------------------------------------------------------


def _fault_rows(tmp_path) -> list:
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "task_done_invalid_status"
    ]


def test_nonterminal_task_done_with_a_cancel_intent_is_left_to_custody(tmp_path):
    """The incident's shape: task_done carrying the cancel latch must be REFUSED.

    With a cancellation pending, the row STAYS in RUNNING — custody and the
    watchdog own it and settle it honestly."""
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    running = {"t9": {"task": {"id": "t9"}}}
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda **_kw: True,
    )
    ci.request_cancel(tmp_path, "t9", reason="owner stopped it")
    _handle_task_done({"task_id": "t9", "status": "cancel_requested"}, ctx)

    assert "t9" in running, "a task whose cancellation is pending stays owned by custody"
    fault = _fault_rows(tmp_path)
    assert fault and fault[0]["task_id"] == "t9" and fault[0]["status"] == "cancel_requested"
    assert load_task_result(tmp_path, "t9") in (None, {})  # custody writes the terminal


def test_nonterminal_task_done_without_an_owner_terminalizes_and_frees_the_slot(tmp_path):
    """A refused task_done that NOBODY owns must not wedge the worker slot.

    Refusing the publication is right; refusing it and walking away left the task
    in RUNNING with its worker still marked busy and nothing scheduled to finish
    it. With no cancel intent and no legacy latch the event is a genuine
    lifecycle bug, so the supervisor terminalizes the task as ``failed`` with a
    typed reason and releases the slot."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    running = {"t11": {"task": {"id": "t11"}}}
    slot = types.SimpleNamespace(busy_task_id="t11", reaping=False)
    snapshots: list = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        WORKERS={3: slot},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": snapshots.append(reason),
    )
    _handle_task_done({"task_id": "t11", "status": "running", "worker_id": 3}, ctx)

    assert _fault_rows(tmp_path)
    assert "t11" not in running, "an unowned lifecycle fault must release RUNNING"
    assert slot.busy_task_id is None, "the worker slot must not stay wedged"
    stored = load_task_result(tmp_path, "t11") or {}
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"
    # GR3-6: the synthetic terminal rides the NORMAL dispatch seam (its
    # snapshot reason), not a private partial copy.
    assert snapshots == ["task_done"]


def test_lifecycle_fault_never_frees_a_reaping_slot(tmp_path):
    """A ``reaping`` slot is owned by the reaper/custody: releasing it here would
    hand a mid-kill process back to assignment."""
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    running = {"t12": {"task": {"id": "t12"}}}
    slot = types.SimpleNamespace(busy_task_id="t12", reaping=True)
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        WORKERS={0: slot},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda **_kw: True,
    )
    _handle_task_done({"task_id": "t12", "status": "running", "worker_id": 0}, ctx)

    assert slot.busy_task_id == "t12" and slot.reaping is True


def test_interrupted_task_done_is_the_formalized_transient_not_a_fault(tmp_path):
    """A1.11: the update/restart teardown publishes ``interrupted`` for this
    generation — a real transient with an owner (snapshot restore / orphan
    reconcile), exempt from the settled-status guard."""
    from ouroboros.utils import append_jsonl as _append_jsonl
    from supervisor.events import _handle_task_done

    running = {"t10": {"task": {"id": "t10"}}}

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = running
        append_jsonl = staticmethod(_append_jsonl)

    try:
        _handle_task_done({"task_id": "t10", "status": "interrupted"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; entering it is the point
    events_path = tmp_path / "logs" / "events.jsonl"
    if events_path.exists():
        rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert not [r for r in rows if r.get("type") == "task_done_invalid_status"]


def test_steering_is_refused_while_a_cancel_intent_is_active(tmp_path, monkeypatch):
    """A1.8: no NEW steering writes into a task whose cancellation is pending."""
    import supervisor.events as events_mod
    from ouroboros.owner_mailbox import drain_owner_messages
    from supervisor.events import _handle_steer_task

    ci.request_cancel(tmp_path, "steerme", reason="tearing down")
    receipts: list = []
    monkeypatch.setattr(
        events_mod, "_emit_routing_receipt",
        lambda ctx, evt, **kw: receipts.append(kw) or {},
    )
    sent: list = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"steerme": {"task": {"id": "steerme", "chat_id": 1}}},
        PENDING=[],
        get_chat_agent=lambda: None,
        send_with_budget=lambda *a, **k: sent.append(a),
        persist_queue_snapshot=lambda **_kw: True,
    )
    _handle_steer_task(
        {"target_task_id": "steerme", "message": "new orders", "chat_id": 1}, ctx,
    )
    assert receipts and receipts[0]["status"] == "rejected"
    assert receipts[0]["reason"] == "cancel_pending"
    assert drain_owner_messages(tmp_path, "steerme") == []
    assert sent and "cancellation is pending" in sent[0][1]


# --------------------------------------------------------------------------
# A2: durable terminal delivery seam
# --------------------------------------------------------------------------


class _CaptureQueue:
    def __init__(self):
        self.events = []

    def put(self, evt):
        self.events.append(evt)


def test_delivery_registry_is_durable_and_send_ordered(tmp_path):
    from supervisor import terminal_delivery as td

    did = td.delivery_id_for("t1", "answer text")
    assert not td.already_delivered(tmp_path, did)
    assert td.register_delivery(tmp_path, did) is True
    assert td.already_delivered(tmp_path, did)          # survives on disk
    assert td.register_delivery(tmp_path, did) is False  # duplicate registration


def test_deliver_unreviewed_salvage_builds_honest_message(tmp_path):
    from supervisor import terminal_delivery as td

    preserved = tmp_path / "full.txt"
    long_text = "line of salvage\n" * 600
    preserved.write_text(long_text, encoding="utf-8")
    write_task_result(tmp_path, "task-a", "cancelled", result="stopped")
    queue = _CaptureQueue()
    delivered = td.deliver_unreviewed_salvage(
        tmp_path,
        {"chat_id": 7},
        "task-a",
        outcome="cancelled",
        salvaged_text=long_text,
        preserved_path=str(preserved),
        children=[{"task_id": "c1", "outcome": "cancelled", "salvaged": True}],
        event_queue=queue,
    )
    assert delivered is True
    (event,) = queue.events
    assert event["chat_id"] == 7 and event["task_id"] == "task-a"
    assert event["delivery_id"].startswith("final:task-a:")
    # Q4 non-mimicry: the receipt is typed SYSTEM end to end.
    assert event["role"] == "system" and event["system_type"] == "cancel_receipt"
    text = event["text"]
    assert "WITHOUT review" in text
    assert "last persisted intermediate model message" in text
    assert "NOT a final answer" in text
    omitted = len(long_text.strip()) - td.SALVAGE_PREVIEW_CHARS
    assert f"{omitted} chars omitted" in text           # exact disclosed count
    assert "1 descendant task(s) were settled with it" in text
    # Q5=A: the technical facts stay OUT of chat and live in the durable
    # cancel_receipt block the details panel renders.
    assert str(preserved) not in text
    assert "sha256" not in text
    assert "task's details panel" in text
    stored = load_task_result(tmp_path, "task-a")
    receipt = stored["cancel_receipt"]
    full_digest = hashlib.sha256(preserved.read_bytes()).hexdigest()
    assert receipt["salvage"]["path"] == str(preserved)
    assert receipt["salvage"]["sha256"] == full_digest
    assert receipt["salvage"]["size_bytes"] == preserved.stat().st_size
    assert receipt["preview_omitted_chars"] == omitted
    assert receipt["children"] == [
        {"task_id": "c1", "outcome": "cancelled", "salvaged": True}
    ]
    assert receipt["delivery_id"] == event["delivery_id"]

    # Second delivery of the same content is suppressed only AFTER registration.
    td.register_delivery(tmp_path, event["delivery_id"])
    queue.events.clear()
    assert td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 7}, "task-a",
        outcome="cancelled", salvaged_text=long_text,
        preserved_path=str(preserved),
        children=[{"task_id": "c1", "outcome": "cancelled", "salvaged": True}],
        event_queue=queue,
    ) is False
    assert queue.events == []


def test_real_salvage_block_heals_placeholder_and_survives_replay(tmp_path):
    """m6-preserved-key: a REAL salvage receipt carries preserved=True, so a
    late real block heals an early placeholder, while a placeholder replay
    still never clobbers a persisted real block (the original minor-6 pin)."""
    from supervisor import terminal_delivery as td

    write_task_result(tmp_path, "task-m6", "cancelled", result="stopped")
    # An early placeholder persisted first (no durable copy existed yet).
    td._persist_cancel_receipt(
        tmp_path, "task-m6",
        settled_status="cancelled", outcome="cancelled",
        delivery_id="d-m6", preserved_path="", preview_omitted=0,
    )
    stored = load_task_result(tmp_path, "task-m6")
    assert stored["cancel_receipt"]["salvage"] == {"path": "", "preserved": False}

    # A late REAL salvage block replayed over it -> the real block WINS.
    preserved = tmp_path / "m6-full.txt"
    preserved.write_text("the whole salvaged text", encoding="utf-8")
    td._persist_cancel_receipt(
        tmp_path, "task-m6",
        settled_status="cancelled", outcome="cancelled",
        delivery_id="d-m6", preserved_path=str(preserved), preview_omitted=0,
    )
    stored = load_task_result(tmp_path, "task-m6")
    salvage = stored["cancel_receipt"]["salvage"]
    assert salvage["path"] == str(preserved)
    assert salvage["preserved"] is True
    assert salvage["sha256"] == hashlib.sha256(preserved.read_bytes()).hexdigest()
    assert salvage["size_bytes"] == preserved.stat().st_size

    # A placeholder replay after the real block -> the real block SURVIVES.
    td._persist_cancel_receipt(
        tmp_path, "task-m6",
        settled_status="cancelled", outcome="cancelled",
        delivery_id="d-m6", preserved_path="", preview_omitted=0,
    )
    stored = load_task_result(tmp_path, "task-m6")
    assert stored["cancel_receipt"]["salvage"] == salvage


def test_completed_outcome_reads_as_result_not_salvage(tmp_path):
    """GR2-12: the completed-vs-salvage branch keys on the TYPED stored status,
    never on the presentation prose in ``outcome``."""
    from supervisor import terminal_delivery as td

    queue = _CaptureQueue()
    td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 3}, "task-b",
        outcome="completed before the cancellation (result preserved)",
        salvaged_text="the finished answer", settled_status="completed",
        event_queue=queue,
    )
    (event,) = queue.events
    assert event["text"].startswith("✅ Task task-b completed before the cancellation")
    assert "WITHOUT review" not in event["text"]

    # Prose that merely STARTS with "completed" no longer forges the ✅ frame:
    # without the typed status the message stays an honest unreviewed salvage.
    queue.events.clear()
    td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 3}, "task-c",
        outcome="completed-looking prose without a typed status",
        salvaged_text="salvaged text", event_queue=queue,
    )
    (event,) = queue.events
    assert event["text"].startswith("⚠️ Task task-c")
    assert "WITHOUT review" in event["text"]


def test_receipt_identity_is_the_stop_episode_and_survives_the_settle(tmp_path):
    """CF-04: the receipt delivery id is ``cancel:<tid>:<request_id>`` — bound
    to the stop episode, stable across wording changes AND across the settle
    (the publish half rebuilds after the intent row is gone and must re-derive
    the SAME id from the owed row the pre-settle half registered)."""
    from supervisor import terminal_delivery as td

    write_task_result(tmp_path, "ep-1", STATUS_RUNNING, result="working")
    intent = ci.request_cancel(tmp_path, "ep-1")
    rid = intent["request_id"]

    # Pre-settle half (owed registration): id comes from the ACTIVE intent.
    event = td.build_unreviewed_salvage_event(
        tmp_path, {"chat_id": 4}, "ep-1", outcome="cancelled",
        salvaged_text="partial work", settled_status="cancelled",
    )
    assert event["delivery_id"] == f"cancel:ep-1:{rid}"
    assert event["role"] == "system" and event["system_type"] == "cancel_receipt"
    assert td.register_pending_delivery(tmp_path, event) is True

    # Settle removes the active intent; the publish half re-derives the id
    # from the pending owed row instead of falling back to a content digest.
    ci.settle_intent(tmp_path, "ep-1", outcome="cancelled", request_id=rid)
    rebuilt = td.build_unreviewed_salvage_event(
        tmp_path, {"chat_id": 4}, "ep-1", outcome="cancelled",
        salvaged_text="partial work", settled_status="cancelled",
    )
    assert rebuilt["delivery_id"] == event["delivery_id"]

    # No episode at all (e.g. a reap without an intent): content-derived
    # fallback keeps the pre-S3 vocabulary.
    other = td.build_unreviewed_salvage_event(
        tmp_path, {"chat_id": 4}, "no-episode", outcome="cancelled",
        salvaged_text="text", settled_status="cancelled",
    )
    assert other["delivery_id"].startswith("final:no-episode:")


# --------------------------------------------------------------------------
# Mandatory E2E class tests: live split-drive worker through the REAL kill path
# --------------------------------------------------------------------------


class _LiveProc:
    """A REAL OS process behind the worker-proc surface custody expects.

    Tests spawning these belong to the SERIAL lane (`@pytest.mark.serial`,
    tests/conftest policy: real-subprocess tests flake or crash xdist workers
    under `-n auto`) and every spawn is registered so the autouse reaper below
    terminates AND waits it even when the test fails before its own kill path
    runs — a leaked 120s sleeper must never outlive its test (GR2-10).
    """

    _SPAWNED: list = []

    def __init__(self):
        self._proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
        )
        self.pid = self._proc.pid
        _LiveProc._SPAWNED.append(self._proc)

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def join(self, timeout=None):
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self):
        self._proc.terminate()


@pytest.fixture(autouse=True)
def _reap_spawned_live_procs():
    """Terminate AND reap (wait) every _LiveProc spawned by a test (GR2-10)."""
    yield
    while _LiveProc._SPAWNED:
        proc = _LiveProc._SPAWNED.pop()
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            # poll() already reaped an exited child; nothing more owed.
        except Exception:
            pass


def _seed_llm_response(drive: pathlib.Path, task_id: str, text: str) -> None:
    from ouroboros import observability

    blob = observability.write_blob(drive, {"message": {"content": text}})
    observability.write_call_manifest(
        drive, task_id=task_id, call_id="llm_0001_response",
        manifest={"full_payload_ref": blob},
    )


def _live_split_drive_task(qenv, task_id: str) -> tuple[dict, pathlib.Path, _LiveProc]:
    from ouroboros.headless import HEADLESS_TASKS_DIR

    child_drive = qenv.drive / HEADLESS_TASKS_DIR / task_id / "data"
    child_drive.mkdir(parents=True)
    task = {
        "id": task_id,
        "chat_id": 5,
        "delegation_role": "subagent",
        "parent_task_id": "parent-e2e",
        "root_task_id": "parent-e2e",
        "child_drive_root": str(child_drive),
    }
    proc = _LiveProc()
    worker = types.SimpleNamespace(wid=0, proc=proc, busy_task_id=task_id, reaping=False)
    qenv.workers.WORKERS[0] = worker
    qenv.q.RUNNING[task_id] = {"task": task, "worker_id": 0}
    return task, child_drive, proc


@pytest.mark.serial
def test_e2e_tool_cancel_kills_live_worker_and_settles_with_cost(qenv, monkeypatch):
    """tool cancel → durable intent → custody kills a LIVE worker process →
    post-kill copy runs → settled cancelled result with reconstructed cost and
    salvage → intent settled → typed task_done."""
    from ouroboros.tools.join_ledger import _cancel_task

    task_id = "e2e-cancel"
    task, child_drive, proc = _live_split_drive_task(qenv, task_id)
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working",
                      parent_task_id="parent-e2e", root_task_id="parent-e2e",
                      delegation_role="subagent")
    write_task_result(child_drive, task_id, STATUS_RUNNING, result="child mirror")
    _seed_llm_response(child_drive, task_id, "the partial answer so far")

    done_events: list = []
    monkeypatch.setattr(
        qenv.q, "_emit_cancel_task_done",
        lambda t, tid, cost_fields=None, status="cancelled": done_events.append(
            {"task_id": tid, "status": status, **(cost_fields or {})},
        ),
    )

    # Ingress through the TOOL (the one request_cancel seam).
    ctx = types.SimpleNamespace(
        task_depth=0, pending_events=[], event_queue=_CaptureQueue(),
        drive_root=qenv.drive, task_id="parent-e2e",
        task_metadata={"root_task_id": "parent-e2e"},
        is_direct_chat=False, is_workspace_mode=lambda: False,
    )
    assert "Cancel requested" in _cancel_task(ctx, task_id, reason="no longer needed")
    assert ci.active_intent(qenv.drive, task_id) is not None
    assert load_task_result(qenv.drive, task_id)["status"] == STATUS_RUNNING

    outcome = qenv.tl.cancel_task_custody(task_id)

    assert outcome == qenv.tl.CANCEL_CANCELLED
    assert not proc.is_alive(), "custody must actually kill the live worker"
    stored = load_task_result(qenv.drive, task_id)
    assert stored["status"] == STATUS_CANCELLED
    assert "the partial answer so far" in stored["result"]  # salvage in the result
    assert stored["parent_decision"] == "cancelled"          # stamped at OUTCOME
    assert stored.get("cost_accounting_status") == "available"  # reconstructed
    assert ci.active_intent(qenv.drive, task_id) is None
    assert not child_drive.exists(), "cancelled subagent drive is cleaned up"
    # task_done carries the reconstructed accounting — never a fabricated final $0
    # (an empty ledger reconstructs to a CONFIRMED zero, which is fine).
    (done,) = done_events
    assert done["status"] == STATUS_CANCELLED
    assert done["cost_accounting_status"] == "available"


@pytest.mark.serial
def test_e2e_child_finishing_before_the_kill_keeps_its_completed_result(qenv, monkeypatch):
    """The race the incident erased: the child wrote its COMPLETED result on the
    split child drive before the kill — custody copies it back, publishes it,
    and the completed payload + artifacts + cost survive."""
    task_id = "e2e-race"
    task, child_drive, proc = _live_split_drive_task(qenv, task_id)
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working",
                      parent_task_id="parent-e2e", root_task_id="parent-e2e",
                      delegation_role="subagent")
    write_task_result(
        child_drive, task_id, STATUS_COMPLETED,
        result="the finished child answer",
        final_answer="the finished child answer",
        trace_summary="did the work",
        cost_usd=0.42, cost_final=True, cost_accounting_status="available",
    )

    done_events: list = []
    monkeypatch.setattr(
        qenv.q, "_emit_cancel_task_done",
        lambda t, tid, cost_fields=None, status="cancelled": done_events.append(
            {"task_id": tid, "status": status, **(cost_fields or {})},
        ),
    )
    ci.request_cancel(qenv.drive, task_id, reason="late cancel", requested_by="parent-e2e")

    outcome = qenv.tl.cancel_task_custody(task_id)

    assert outcome == qenv.tl.CANCEL_ALREADY_SETTLED
    assert not proc.is_alive()
    stored = load_task_result(qenv.drive, task_id)
    assert stored["status"] == STATUS_COMPLETED
    assert stored["result"] == "the finished child answer"
    assert stored["final_answer"] == "the finished child answer"
    assert stored["cost_usd"] == 0.42
    # Completion wins WITHOUT a parent_decision overwrite of the kept result.
    assert "parent_decision" not in stored
    assert ci.active_intent(qenv.drive, task_id) is None
    (done,) = done_events
    assert done["status"] == STATUS_COMPLETED
    assert done["cost_usd"] == 0.42


# --------------------------------------------------------------------------
# Fix batch (A-F1..A-F23): abandoned slots, generation fencing, honest
# statuses, cascade delivery/scope, durable outbox, disclosure
# --------------------------------------------------------------------------


def test_request_cancel_refuses_to_mint_an_intent_for_a_settled_task(tmp_path):
    """A-F8: a settled task would otherwise wear a false 'Cancelling…' badge."""
    write_task_result(tmp_path, "done2", STATUS_COMPLETED, result="finished on its own")
    outcome = ci.request_cancel(tmp_path, "done2", reason="too late", source="agent_tool")
    assert outcome["already_settled"] is True
    assert outcome["status"] == STATUS_COMPLETED
    assert ci.active_intent(tmp_path, "done2") is None
    assert ci.cancel_state_fields(tmp_path, "done2") == {}


def test_cancel_tool_reports_a_settled_task_instead_of_requesting(tmp_path, monkeypatch):
    """A-F8 at the ingress the agent actually calls.

    GR7-1a: "Nothing to cancel" now requires a FRESH queue snapshot that
    positively proves no live ownership — a missing/stale snapshot fails OPEN
    and mints (see test_gate_round7_fixes)."""
    from ouroboros.tools.join_ledger import _cancel_task
    from ouroboros.utils import utc_now_iso

    write_task_result(tmp_path, "settled-child", STATUS_COMPLETED, result="done")
    snap = tmp_path / "state" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps({"ts": utc_now_iso(), "running": [], "pending": []}),
        encoding="utf-8",
    )
    ctx = types.SimpleNamespace(
        task_depth=0, pending_events=[], event_queue=_CaptureQueue(),
        drive_root=tmp_path, task_id="parent1",
        task_metadata={"root_task_id": "parent1"},
        is_direct_chat=False, is_workspace_mode=lambda: False,
    )
    monkeypatch.setattr("ouroboros.tools.control._emit_control_event", lambda *_a, **_k: "live")
    reply = _cancel_task(ctx, "settled-child")
    assert "Nothing to cancel" in reply and STATUS_COMPLETED in reply
    assert ci.active_intent(tmp_path, "settled-child") is None


def test_a_live_claim_is_never_stolen_and_an_abandoned_one_is(tmp_path):
    """A-F11 + A-F1c: exclusive while alive, taken over once abandoned."""
    ci.request_cancel(tmp_path, "excl1")
    first = ci.claim_intent(tmp_path, "excl1", owner="custody-1")
    assert first["generation"] == 1 and not first.get("claim_refused")

    refused = ci.claim_intent(tmp_path, "excl1", owner="custody-2")
    assert refused["claim_refused"] is True
    assert ci.active_intent(tmp_path, "excl1")["generation"] == 1

    # GR3-2: age ALONE is not abandonment while the claimant pid (this test
    # process) probes ALIVE — the stale claim is still refused.
    from datetime import datetime, timezone
    store = tmp_path / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["excl1"]["claimed_at"] = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - ci.CLAIM_STALE_SEC - 10, tz=timezone.utc,
    ).isoformat()
    store.write_text(json.dumps(data), encoding="utf-8")
    still_refused = ci.claim_intent(tmp_path, "excl1", owner="custody-2")
    assert still_refused["claim_refused"] is True
    assert ci.claim_is_abandoned(ci.active_intent(tmp_path, "excl1")) is False
    # A provably DEAD claiming process makes the takeover legitimate — even
    # with a fresh claim timestamp (no three-minute wait on a dead owner).
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["excl1"]["claimed_at"] = ci.utc_now_iso()
    data["intents"]["excl1"]["claim_pid"] = 2 ** 22  # never a live pid
    store.write_text(json.dumps(data), encoding="utf-8")
    assert ci.claim_is_abandoned(ci.active_intent(tmp_path, "excl1")) is True
    taken = ci.claim_intent(tmp_path, "excl1", owner="custody-2")
    assert not taken.get("claim_refused") and taken["generation"] == 2
    # Stale with liveness UNKNOWN (pid missing) is also recoverable.
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["excl1"]["claimed_at"] = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - ci.CLAIM_STALE_SEC - 10, tz=timezone.utc,
    ).isoformat()
    data["intents"]["excl1"].pop("claim_pid", None)
    store.write_text(json.dumps(data), encoding="utf-8")
    assert ci.claim_is_abandoned(ci.active_intent(tmp_path, "excl1")) is True


def test_stale_claimants_release_and_settle_are_fenced_by_generation(tmp_path):
    """A-F2: ``generation`` is a FENCE, not forensics."""
    ci.request_cancel(tmp_path, "fence1")
    stale = ci.claim_intent(tmp_path, "fence1", owner="custody-1")
    # Custody-1 is taken over (its claiming process is provably DEAD — GR3-2:
    # age alone never abandons a live claimant); custody-2 owns generation 2.
    store = tmp_path / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"]["fence1"]["claim_pid"] = 2 ** 22  # never a live pid
    store.write_text(json.dumps(data), encoding="utf-8")
    fresh = ci.claim_intent(tmp_path, "fence1", owner="custody-2")
    assert fresh["generation"] == stale["generation"] + 1

    # The stale claimant's release must NOT revert the newer claim.
    ci.release_claim(
        tmp_path, "fence1", error="stale", expected_generation=stale["generation"],
        request_id=stale["request_id"],
    )
    current = ci.active_intent(tmp_path, "fence1")
    assert current["state"] == ci.INTENT_CLAIMED
    assert current["claim_owner"] == "custody-2"

    # Nor may its settle delete the intent the new owner is still working.
    assert ci.settle_intent(
        tmp_path, "fence1", outcome="cancelled",
        expected_generation=stale["generation"], request_id=stale["request_id"],
    ) is None
    assert ci.active_intent(tmp_path, "fence1") is not None

    # The real owner settles it.
    assert ci.settle_intent(
        tmp_path, "fence1", outcome="cancelled",
        expected_generation=fresh["generation"], request_id=fresh["request_id"],
    ) is not None
    assert ci.active_intent(tmp_path, "fence1") is None
    trail = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert "claim_release_refused" in trail and "settle_refused" in trail


def test_concurrent_custody_on_a_pending_task_settles_exactly_once(qenv):
    """A-F11 probe shape: the second custody must give the capture back."""
    ci.request_cancel(qenv.drive, "pending-race", reason="stop")
    qenv.q.PENDING[:] = [{"id": "pending-race", "chat_id": 1}]
    write_task_result(qenv.drive, "pending-race", "scheduled")
    # Custody-1 holds a FRESH claim (it is mid-teardown).
    ci.claim_intent(qenv.drive, "pending-race", owner="custody-1")

    outcome = qenv.tl.cancel_task_custody("pending-race")

    assert outcome == qenv.tl.CANCEL_FAILED
    assert [t["id"] for t in qenv.q.PENDING] == ["pending-race"], "capture returned"
    assert load_task_result(qenv.drive, "pending-race")["status"] == "scheduled"
    assert ci.active_intent(qenv.drive, "pending-race")["claim_owner"] == "custody-1"


@pytest.mark.serial
def test_custody_raising_mid_teardown_releases_the_reaping_slot(qenv, monkeypatch):
    """A-F1a: a crash between capture and respawn must not strand the slot."""
    task_id = "raiser"
    task, _child_drive, proc = _live_split_drive_task(qenv, task_id)
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working")
    ci.request_cancel(qenv.drive, task_id)
    monkeypatch.setattr(
        qenv.tl, "_finish_captured_running",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("teardown exploded")),
    )
    try:
        outcome = qenv.tl.cancel_task_custody(task_id)
    finally:
        proc.terminate()

    assert outcome == qenv.tl.CANCEL_FAILED
    assert qenv.workers.WORKERS[0].reaping is False, "the slot must be reopened"
    # The intent stays OPEN (back to requested) so the watchdog retries.
    intent = ci.active_intent(qenv.drive, task_id)
    assert intent is not None and intent["state"] == ci.INTENT_REQUESTED


@pytest.mark.serial
def test_custody_takes_over_a_slot_stranded_by_an_abandoned_claim(qenv):
    """A-F1c: the infinite CANCEL_FAILED loop a dead custody used to cause."""
    task_id = "stranded"
    task, _child_drive, proc = _live_split_drive_task(qenv, task_id)
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working")
    ci.request_cancel(qenv.drive, task_id)
    ci.claim_intent(qenv.drive, task_id, owner="dead-custody")
    qenv.workers.WORKERS[0].reaping = True  # marker its owner never cleared

    # A FRESH claim is respected: no takeover, honest failure.
    assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_FAILED

    store = qenv.drive / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"][task_id]["claim_pid"] = 2 ** 22  # the owner's process is gone
    store.write_text(json.dumps(data), encoding="utf-8")

    try:
        outcome = qenv.tl.cancel_task_custody(task_id)
    finally:
        proc.terminate()
    assert outcome == qenv.tl.CANCEL_CANCELLED
    assert load_task_result(qenv.drive, task_id)["status"] == STATUS_CANCELLED
    assert ci.active_intent(qenv.drive, task_id) is None


def test_settled_branch_recovers_a_slot_stranded_by_a_dead_custody(qenv):
    """A-F1b: the task settled on its own — nothing else revisits that worker."""
    task_id = "stranded-settled"
    respawned: list = []
    qenv.workers.WORKERS[0] = types.SimpleNamespace(
        wid=0, busy_task_id=task_id, reaping=True,
        proc=types.SimpleNamespace(pid=None, is_alive=lambda: False),
    )
    import supervisor.workers as workers_mod
    qenv_respawn = workers_mod.respawn_worker
    assert qenv_respawn is not None
    workers_mod.respawn_worker = lambda wid: respawned.append(wid)
    try:
        write_task_result(qenv.drive, task_id, STATUS_COMPLETED, result="finished")
        ci.request_cancel(qenv.drive, task_id)  # settled: no intent minted
        # Force the wedged shape: an intent whose claim owner is a dead process.
        ci.request_cancel(qenv.drive, task_id + "-x")  # keep the store non-empty
        store = qenv.drive / "state" / "cancel_intents.json"
        data = json.loads(store.read_text(encoding="utf-8"))
        data["intents"][task_id] = {
            "request_id": "ci_dead", "task_id": task_id, "state": ci.INTENT_CLAIMED,
            "claim_owner": "dead-custody", "claim_pid": 2 ** 22,
            "claimed_at": ci.utc_now_iso(), "generation": 1, "scope": "single",
            "requested_at": ci.utc_now_iso(),
        }
        store.write_text(json.dumps(data), encoding="utf-8")

        assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_ALREADY_SETTLED
    finally:
        workers_mod.respawn_worker = qenv_respawn
    assert respawned == [0], "a dead worker behind an abandoned claim is respawned"
    assert ci.active_intent(qenv.drive, task_id) is None


def test_cancel_of_a_never_scheduled_id_is_not_found_not_a_fabricated_row(qenv):
    """A-F22: no phantom cancelled task with a fabricated $0."""
    ci.request_cancel(qenv.drive, "ghost-typo", reason="mistyped id")
    assert qenv.tl.cancel_task_custody("ghost-typo") == qenv.tl.CANCEL_NOT_FOUND
    assert load_task_result(qenv.drive, "ghost-typo") in (None, {})
    assert ci.active_intent(qenv.drive, "ghost-typo") is None
    trail = (qenv.drive / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert '"outcome": "not_found"' in trail


def test_finalize_on_miss_promotes_a_child_result_before_cancelling(qenv):
    """A-F23: a crash mid-custody must not bury a completed child result."""
    task_id = "miss-with-child"
    child_drive = qenv.drive / "child-of-miss"
    write_task_result(child_drive, task_id, STATUS_COMPLETED,
                      result="the child's finished answer", final_answer="answer")
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="mirror",
                      child_drive_root=str(child_drive), delegation_role="subagent")
    ci.request_cancel(qenv.drive, task_id, reason="late cancel")

    assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_ALREADY_SETTLED
    stored = load_task_result(qenv.drive, task_id)
    assert stored["status"] == STATUS_COMPLETED
    assert stored["result"] == "the child's finished answer"


@pytest.mark.serial
def test_cancel_of_a_task_with_no_durable_result_never_fabricates_completed(qenv, monkeypatch):
    """A-F5, PROVEN class: killed inside the spawn→RUNNING-write window.

    The artifact capture used to default-stamp ``completed`` on a workspace task
    with no durable row, after which the monotonic guard defended the invented
    completion against the real ``cancelled`` write — and it was published AND
    delivered to the owner."""
    task_id = "no-result-yet"
    workspace = qenv.drive / "ws"
    workspace.mkdir()
    child_drive = qenv.drive / "state" / "headless_tasks" / task_id / "data"
    child_drive.mkdir(parents=True)
    task = {
        "id": task_id, "chat_id": 4, "workspace_root": str(workspace),
        "child_drive_root": str(child_drive),
    }
    proc = _LiveProc()
    qenv.workers.WORKERS[0] = types.SimpleNamespace(
        wid=0, proc=proc, busy_task_id=task_id, reaping=False,
    )
    qenv.q.RUNNING[task_id] = {"task": task, "worker_id": 0}
    assert load_task_result(qenv.drive, task_id) in (None, {}), "no durable row yet"
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    ci.request_cancel(qenv.drive, task_id, reason="kill it")

    try:
        outcome = qenv.tl.cancel_task_custody(task_id)
    finally:
        proc.terminate()

    assert outcome == qenv.tl.CANCEL_CANCELLED
    stored = load_task_result(qenv.drive, task_id)
    assert stored["status"] == STATUS_CANCELLED, "never a fabricated completed"
    # AR2-9 (§8-A4: провал capture = failed, не missing): the capture was OWED —
    # a RUNNING workspace task was killed — and could not run; that is a capture
    # FAILURE, never an honest "nothing was ever due".
    assert stored["artifact_status"] == "failed"
    assert "owed" in str(stored.get("artifact_error") or "")


def test_cascade_over_a_settled_root_with_live_children_still_delivers(qenv, monkeypatch):
    """A-F6, the incident's exact ending: root dead on budget, children live,
    ZERO chat messages. The routing chat comes from a live descendant."""
    delivered: list = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.deliver_unreviewed_salvage",
        lambda drive, task, tid, **kw: delivered.append({"task": task, "task_id": tid, **kw}),
    )
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    # Root already settled (budget hard stop) and gone from both live maps.
    write_task_result(qenv.drive, "root-dead", "failed", reason_code="budget_exhausted",
                      result="root died on budget")
    qenv.q.PENDING[:] = [
        {"id": "kid1", "chat_id": 77, "parent_task_id": "root-dead", "root_task_id": "root-dead"},
    ]
    write_task_result(qenv.drive, "kid1", "scheduled")

    assert qenv.tl.cancel_task_by_id("root-dead", cascade=True) is True

    assert delivered, "a settled root with live children must still report to chat"
    (row,) = delivered
    from supervisor.terminal_delivery import lineage_chat_id
    assert lineage_chat_id(qenv.drive, row["task"], row["task_id"]) == 77
    # A-F21: the root's REAL status, never "cancelled" over a failed root.
    assert "failed" in row["outcome"]


def test_cascade_mints_child_intents_and_records_scope(qenv, monkeypatch):
    """A-F9: a crash mid-cascade leaves every live descendant fenced, and the
    root intent replays as a CASCADE."""
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **_kw: qenv.q.CANCEL_FAILED)
    qenv.q.PENDING[:] = [
        {"id": "c-root", "chat_id": 2},
        {"id": "c-kid", "chat_id": 2, "parent_task_id": "c-root", "root_task_id": "c-root"},
    ]
    ci.request_cancel(qenv.drive, "c-root", reason="stop the tree")

    assert qenv.tl.cancel_task_by_id("c-root", cascade=True) is False  # custody refused

    intents = ci.active_intents(qenv.drive)
    assert "c-kid" in intents, "every captured descendant carries its own intent"
    assert intents["c-kid"]["requested_by"] == "c-root"
    assert intents["c-root"]["scope"] == ci.SCOPE_CASCADE


def test_watchdog_replays_a_cascade_intent_as_a_cascade(qenv, monkeypatch):
    """A-F9: replaying a cascade as a single cancel would leave descendants live."""
    calls: list = []
    monkeypatch.setattr(qenv.tl, "cancel_task_by_id",
                        lambda tid, **kw: calls.append((tid, kw)) or True)
    monkeypatch.setattr(qenv.tl, "cancel_task_custody",
                        lambda tid, **kw: calls.append((tid, "single")) or "cancelled")
    ci.request_cancel(qenv.drive, "casc-root", scope=ci.SCOPE_CASCADE)
    store = qenv.drive / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    from datetime import datetime, timezone
    data["intents"]["casc-root"]["requested_at"] = datetime.fromtimestamp(
        1_000_000 - 600, tz=timezone.utc,
    ).isoformat()
    store.write_text(json.dumps(data), encoding="utf-8")

    qenv.tl.sweep_cancel_intents(now=1_000_000.0)

    assert calls == [("casc-root", {"cascade": True})]


def test_drop_cancelled_pending_stamps_the_decision_and_honors_the_stored_status(
    qenv, monkeypatch,
):
    """A-F4: the pre-assignment drop follows custody's rules."""
    from supervisor import workers

    emitted: list = []
    monkeypatch.setattr(workers, "_emit_task_done_terminal",
                        lambda task, tid, status, **kw: emitted.append((tid, status)) or True)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)

    qenv.q.PENDING[:] = [
        {"id": "drop-decided", "chat_id": 1},
        {"id": "drop-completed", "chat_id": 1},
    ]
    write_task_result(qenv.drive, "drop-decided", "scheduled")
    ci.request_cancel(qenv.drive, "drop-decided", reason="parent stopped the plan",
                      requested_by="parent7")
    # This one finished on its own between the intent and the drop.
    write_task_result(qenv.drive, "drop-completed", "scheduled")
    ci.request_cancel(qenv.drive, "drop-completed")
    write_task_result(qenv.drive, "drop-completed", STATUS_COMPLETED, result="won the race")

    workers._drop_cancelled_pending()

    decided = load_task_result(qenv.drive, "drop-decided")
    assert decided["status"] == STATUS_CANCELLED
    assert decided["parent_decision"] == "cancelled"
    assert decided["parent_decision_reason"] == "parent stopped the plan"
    # Completion wins: the stored status is what the card resolves to.
    assert load_task_result(qenv.drive, "drop-completed")["status"] == STATUS_COMPLETED
    assert ("drop-completed", STATUS_COMPLETED) in emitted
    assert ("drop-decided", STATUS_CANCELLED) in emitted


def test_drop_cancelled_pending_retains_custody_until_task_done_is_published(
    qenv, monkeypatch,
):
    """A durable cancellation without task_done remains non-dispatchable."""
    from supervisor import workers

    emitted: list = []
    publish_results = iter([False, True])
    snapshots: list[str] = []

    def publish(_task, task_id, status, **_kwargs):
        emitted.append((task_id, status))
        return next(publish_results)

    monkeypatch.setattr(workers, "_emit_task_done_terminal", publish)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)
    monkeypatch.setattr(
        qenv.q,
        "persist_queue_snapshot",
        lambda reason="": snapshots.append(reason) or True,
    )
    qenv.q.PENDING[:] = [{"id": "drop-event-gap", "chat_id": 1, "depth": 0}]
    write_task_result(qenv.drive, "drop-event-gap", "scheduled")
    ci.request_cancel(
        qenv.drive,
        "drop-event-gap",
        reason="parent stopped the plan",
        requested_by="parent7",
    )

    workers._drop_cancelled_pending()

    assert [row["id"] for row in qenv.q.PENDING] == ["drop-event-gap"]
    retry = qenv.q.PENDING[0]["_terminalization_retry"]
    assert retry["status"] == STATUS_CANCELLED
    assert retry["trigger"] == "pending_cancel_event"
    assert retry["reconcile_delegate_custody"] is False
    assert snapshots == ["pending_terminal_event_retry"]
    stored = load_task_result(qenv.drive, "drop-event-gap")
    assert stored["status"] == STATUS_CANCELLED
    assert stored["parent_decision"] == "cancelled"
    assert ci.active_intent(qenv.drive, "drop-event-gap") is None

    assert workers._retry_terminalization_pending() == (["drop-event-gap"], [])
    assert qenv.q.PENDING == []
    assert emitted == [
        ("drop-event-gap", STATUS_CANCELLED),
        ("drop-event-gap", STATUS_CANCELLED),
    ]


@pytest.mark.parametrize("settle_failure", ["returns_none", "raises"])
def test_drop_cancelled_pending_releases_a_failed_intent_claim(
    qenv, monkeypatch, settle_failure,
):
    """A failed intent settle cannot strand a claim while event custody retries."""
    from supervisor import workers

    emitted: list[tuple[str, str]] = []
    publish_results = iter([False, True])

    def publish(_task, task_id, status, **_kwargs):
        emitted.append((task_id, status))
        return next(publish_results)

    monkeypatch.setattr(workers, "_emit_task_done_terminal", publish)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)
    monkeypatch.setattr(
        qenv.q,
        "persist_queue_snapshot",
        lambda reason="": True,
    )

    def failed_settle(*_args, **_kwargs):
        if settle_failure == "raises":
            raise OSError("intent projection temporarily unavailable")
        return None

    released: list[dict] = []
    real_release = ci.release_claim

    def release_claim(root, task_id, **kwargs):
        released.append({"task_id": task_id, **kwargs})
        return real_release(root, task_id, **kwargs)

    monkeypatch.setattr(ci, "settle_intent", failed_settle)
    monkeypatch.setattr(ci, "release_claim", release_claim)

    task_id = "drop-settle-gap"
    qenv.q.PENDING[:] = [{"id": task_id, "chat_id": 1, "depth": 0}]
    write_task_result(qenv.drive, task_id, "scheduled")
    ci.request_cancel(qenv.drive, task_id, reason="parent stopped the plan")

    workers._drop_cancelled_pending()

    assert released and released[0]["task_id"] == task_id
    intent = ci.active_intent(qenv.drive, task_id)
    assert intent is not None
    assert intent["state"] == ci.INTENT_REQUESTED
    assert "claim_owner" not in intent
    assert intent["last_error"] == "pending-drop intent settlement failed"
    retry = qenv.q.PENDING[0]["_terminalization_retry"]
    assert retry["status"] == STATUS_CANCELLED
    assert retry["trigger"] == "pending_cancel_event"
    assert retry["reconcile_delegate_custody"] is False

    # The durable result/event retry is independent of the re-opened intent;
    # the watchdog can settle that intent on its next custody pass.
    assert workers._retry_terminalization_pending() == ([task_id], [])
    assert qenv.q.PENDING == []
    assert emitted == [(task_id, STATUS_CANCELLED), (task_id, STATUS_CANCELLED)]
    intent = ci.active_intent(qenv.drive, task_id)
    assert intent is not None and intent["state"] == ci.INTENT_REQUESTED


def test_drop_cancelled_pending_does_not_assume_settled_when_settle_helper_missing(
    qenv, monkeypatch,
):
    """A missing settle helper must retain the active claim for a later retry."""
    from supervisor import workers

    task_id = "drop-missing-settle"
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)
    monkeypatch.setattr(
        workers,
        "_emit_task_done_terminal",
        lambda _task, tid, status, **_kwargs: emitted.append((tid, status)) or True,
    )
    monkeypatch.setattr(qenv.q, "persist_queue_snapshot", lambda reason="": True)

    qenv.q.PENDING[:] = [{"id": task_id, "chat_id": 1, "depth": 0}]
    write_task_result(qenv.drive, task_id, "scheduled")
    ci.request_cancel(qenv.drive, task_id, reason="parent stopped the plan")

    real_settle = ci.settle_intent
    real_release = ci.release_claim
    monkeypatch.setattr(ci, "settle_intent", None)
    monkeypatch.setattr(ci, "release_claim", lambda *_args, **_kwargs: False)

    workers._drop_cancelled_pending()

    assert [row["id"] for row in qenv.q.PENDING] == [task_id]
    retry = qenv.q.PENDING[0]["_terminalization_retry"]
    assert retry["trigger"] == "pending_cancel_intent"
    assert retry["event_published"] is True
    intent = ci.active_intent(qenv.drive, task_id)
    assert intent is not None and intent["state"] == ci.INTENT_CLAIMED
    assert emitted == [(task_id, STATUS_CANCELLED)]

    # Restore the real helpers: the retained claim and marker can now finish
    # without emitting a second terminal event.
    monkeypatch.setattr(ci, "settle_intent", real_settle)
    monkeypatch.setattr(ci, "release_claim", real_release)
    assert workers._retry_terminalization_pending() == ([task_id], [])
    assert qenv.q.PENDING == []
    intent = ci.active_intent(qenv.drive, task_id)
    assert intent is not None and intent["state"] == ci.INTENT_REQUESTED
    assert emitted == [(task_id, STATUS_CANCELLED)]


def test_drop_cancelled_pending_leaves_the_intent_open_when_the_write_fails(
    qenv, monkeypatch,
):
    """A-F4: never publish a cancellation that is not on disk."""
    from supervisor import workers

    emitted: list = []
    monkeypatch.setattr(workers, "_emit_task_done_terminal",
                        lambda task, tid, status, **kw: emitted.append((tid, status)) or True)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)
    monkeypatch.setattr(
        "ouroboros.task_results.write_task_result",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    qenv.q.PENDING[:] = [{"id": "drop-nowrite", "chat_id": 1}]
    write_task_result(qenv.drive, "drop-nowrite", "scheduled")
    ci.request_cancel(qenv.drive, "drop-nowrite")

    workers._drop_cancelled_pending()

    assert qenv.q.PENDING == [], "it must not be assigned to a worker"
    assert emitted == [], "no task_done for a cancellation that never persisted"
    assert ci.active_intent(qenv.drive, "drop-nowrite") is not None


def _age_pending_rows(drive) -> None:
    """Backdate every owed row past the (backoff-spaced) replay min-age (test-only)."""
    from datetime import datetime, timedelta, timezone

    store = pathlib.Path(drive) / "state" / "terminal_deliveries.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    # Past the LARGEST backoff step (60 * 2**5 = 1920s), so every attempt is due.
    old = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    for row in (data.get("pending") or {}).values():
        row["registered_at"] = old
        if "last_replay_at" in row:
            row["last_replay_at"] = old
    store.write_text(json.dumps(data), encoding="utf-8")


def test_pending_outbox_replays_an_unsent_answer_exactly_once(tmp_path):
    """A-F7: crash between settle and send used to lose the answer forever."""
    from supervisor import terminal_delivery as td

    event = {
        "type": "send_message", "chat_id": 9, "task_id": "outbox1",
        "text": "the salvaged answer", "format": "markdown",
        "delivery_id": td.delivery_id_for("outbox1", "the salvaged answer"),
    }
    assert td.register_pending_delivery(tmp_path, event) is True
    owed = td.pending_deliveries(tmp_path)
    assert [row["delivery_id"] for row in owed] == [event["delivery_id"]]

    queue = _CaptureQueue()
    # A row younger than the min age is presumed still in flight, not lost.
    assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == []
    assert queue.events == []
    _age_pending_rows(tmp_path)

    replayed = td.replay_pending_deliveries(tmp_path, event_queue=queue)
    assert replayed == [event["delivery_id"]]
    (sent,) = queue.events
    assert sent["type"] == "send_message" and sent["chat_id"] == 9
    assert sent["text"] == "the salvaged answer" and sent["format"] == "markdown"

    # A confirmed send clears the row in the SAME write as the delivered mark.
    td.register_delivery(tmp_path, event["delivery_id"])
    assert td.pending_deliveries(tmp_path) == []
    queue.events.clear()
    assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == []
    assert queue.events == []
    # An already-delivered id is never registered as owed again — but the
    # answer IS durably tracked, so the GR3-4 contract answers True (False is
    # reserved for a real durability gap that must keep a cancel intent open).
    assert td.register_pending_delivery(tmp_path, event) is True
    assert td.pending_deliveries(tmp_path) == []


def test_pending_outbox_gives_up_loudly_instead_of_retrying_forever(tmp_path, monkeypatch):
    """A-F7 bound + AR2-7: an unreachable chat must not become a tick-rate retry
    storm — and exhaustion is a DISCLOSED outcome, never a silent drop: the full
    text is preserved on disk, a typed ``terminal_delivery_exhausted`` event
    lands in events.jsonl, and the owner gets a chat notice naming both."""
    from supervisor import terminal_delivery as td

    notices: list = []
    monkeypatch.setattr(
        "supervisor.message_bus.send_with_budget",
        lambda chat_id, text, **kw: notices.append((chat_id, text, kw)),
    )
    event = {
        "type": "send_message", "chat_id": 9, "task_id": "outbox2",
        "text": "never lands", "delivery_id": td.delivery_id_for("outbox2", "never lands"),
    }
    td.register_pending_delivery(tmp_path, event)
    queue = _CaptureQueue()
    for _ in range(td._PENDING_MAX_REPLAYS):
        _age_pending_rows(tmp_path)
        assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == [
            event["delivery_id"],
        ]
    assert notices == [], "no give-up notice while attempts remain"
    _age_pending_rows(tmp_path)
    assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == []
    assert td.pending_deliveries(tmp_path) == []
    assert len(queue.events) == td._PENDING_MAX_REPLAYS
    # The disclosure: durable typed event + preserved full copy + chat notice.
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    (exhausted,) = [r for r in rows if r.get("type") == "terminal_delivery_exhausted"]
    assert exhausted["task_id"] == "outbox2"
    assert exhausted["delivery_id"] == event["delivery_id"]
    assert exhausted["chat_id"] == 9
    preserved = pathlib.Path(str(exhausted["preserved_path"]))
    assert preserved.is_file() and preserved.read_text(encoding="utf-8") == "never lands"
    (notice,) = notices
    assert notice[0] == 9
    assert "could not be delivered" in notice[1]
    assert str(preserved) in notice[1]
    assert notice[2] == {"role": "system", "system_type": "terminal_incident"}


def test_pending_outbox_spaces_replays_with_backoff(tmp_path):
    """AR2-7: ``registered_at`` alone let all five attempts burn on consecutive
    ticks. Each bump stamps ``last_replay_at`` and the next attempt waits an
    exponentially longer min-age, so the cap covers a realistic outage window."""
    from supervisor import terminal_delivery as td

    event = {
        "type": "send_message", "chat_id": 9, "task_id": "outbox3",
        "text": "spaced", "delivery_id": td.delivery_id_for("outbox3", "spaced"),
    }
    td.register_pending_delivery(tmp_path, event)
    queue = _CaptureQueue()
    _age_pending_rows(tmp_path)
    assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == [
        event["delivery_id"],
    ]
    # Immediately after a replay the row is NOT due again (fresh last_replay_at,
    # and the min-age has doubled) — the next tick must not burn attempt 2.
    assert td.replay_pending_deliveries(tmp_path, event_queue=queue) == []
    (row,) = td.pending_deliveries(tmp_path)
    assert row["replay_attempts"] == 1
    assert row.get("last_replay_at"), "each bump stamps the replay time"
    assert td._replay_due(row) is False
    # The doubled min-age: attempt 2 is due only after 2 * base seconds.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).timestamp()
    assert td._replay_due(row, now=now + td._REPLAY_MIN_AGE_SEC + 1) is False
    assert td._replay_due(row, now=now + 2 * td._REPLAY_MIN_AGE_SEC + 1) is True


def test_salvage_receipt_is_complete_for_a_short_answer_too(tmp_path):
    """A-F14 under Q5=A: every salvage still gets its verification receipt —
    the exact-completeness half in chat, the path/sha half in the durable
    ``cancel_receipt`` block the details panel renders."""
    from supervisor import terminal_delivery as td

    preserved = tmp_path / "short.txt"
    preserved.write_text("a short but whole answer", encoding="utf-8")
    write_task_result(tmp_path, "short-task", "cancelled", result="stopped")
    queue = _CaptureQueue()
    td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 5}, "short-task", outcome="cancelled",
        salvaged_text="a short but whole answer", preserved_path=str(preserved),
        event_queue=queue,
    )
    (event,) = queue.events
    digest = hashlib.sha256(preserved.read_bytes()).hexdigest()
    assert "nothing omitted" in event["text"]
    assert "task's details panel" in event["text"]
    receipt = load_task_result(tmp_path, "short-task")["cancel_receipt"]
    assert receipt["salvage"]["sha256"] == digest
    assert receipt["salvage"]["path"] == str(preserved)

    # An unreadable preservation is stamped UNVERIFIED in the durable block
    # instead of silently claiming a verified copy.
    queue.events.clear()
    write_task_result(tmp_path, "short-task-2", "cancelled", result="stopped")
    td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 5}, "short-task-2", outcome="cancelled",
        salvaged_text="another whole answer", preserved_path=str(tmp_path / "gone.txt"),
        event_queue=queue,
    )
    (event,) = queue.events
    receipt = load_task_result(tmp_path, "short-task-2")["cancel_receipt"]
    assert receipt["salvage"].get("unreadable") is True

    # No preserved copy at all is disclosed in CHAT (the owner must know the
    # preview is the only copy).
    queue.events.clear()
    td.deliver_unreviewed_salvage(
        tmp_path, {"chat_id": 5}, "short-task-3", outcome="cancelled",
        salvaged_text="third whole answer", preserved_path="", event_queue=queue,
    )
    (event,) = queue.events
    assert "NO durable full copy" in event["text"]


@pytest.mark.serial
def test_unreconciled_delegated_runs_are_disclosed_on_the_cancelled_result(
    qenv, monkeypatch,
):
    """A-F12: 'cancelled + salvage' while a workspace_write run may still mutate."""
    task_id = "delegating"
    task, child_drive, proc = _live_split_drive_task(qenv, task_id)
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working")
    ci.request_cancel(qenv.drive, task_id)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    monkeypatch.setattr("ouroboros.delegate_custody.reconcile_task_runs",
                        lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "ouroboros.delegate_custody.open_runs",
        lambda *_a, **_kw: [types.SimpleNamespace(task_id=task_id, run_id="run-abc")],
    )
    notes: list = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.deliver_unreviewed_salvage",
        lambda *_a, **kw: notes.append(kw),
    )

    try:
        assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_CANCELLED
    finally:
        proc.terminate()

    stored = load_task_result(qenv.drive, task_id)
    assert stored["delegated_runs_unreconciled"] == ["run-abc"]
    rows = [
        json.loads(line)
        for line in (qenv.drive / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r for r in rows if r.get("type") == "delegated_runs_unreconciled"]


def test_nested_scoped_home_is_disclosed_even_with_an_os_boundary(tmp_path, monkeypatch):
    """A-F13: a nested home + recorded boundary was promoted to verified=true and
    its durable unconfined row was suppressed."""
    from ouroboros.gateways.claudexor import AttemptContainment
    from ouroboros.tools import delegate as dg

    operator_home = tmp_path / "home"
    nested = operator_home / ".claudexor" / "v3" / "scoped" / "a01"
    nested.mkdir(parents=True)
    attempts = [AttemptContainment(
        attempt_id="a01", home_isolated=True, home_dir=str(nested),
        boundary_mechanism="seatbelt",
    )]
    monkeypatch.setattr("ouroboros.gateways.claudexor.attempt_containment",
                        lambda run_dir: attempts)
    monkeypatch.setattr("ouroboros.gateways.claudexor.operator_home",
                        lambda: str(operator_home))
    detail = {"summary": {"runDir": str(tmp_path / "run")}}

    evidence = dg._containment_evidence(detail)

    assert evidence["nested_under_operator_home"] is True
    assert evidence["verified"] is False, "a nested home is not isolation"
    assert "not isolation from the operator's home" in evidence["note"]
    assert "seatbelt boundary WAS applied" in evidence["note"]

    # And the durable unconfined row is still emitted for that shape.
    emitted: list = []
    monkeypatch.setattr(dg, "_emit", lambda ctx, kind, payload: emitted.append((kind, payload)))
    dg._record_containment(None, None, {"containment": evidence, "state": "succeeded"})
    assert emitted and emitted[0][1]["nested_under_operator_home"] is True


def test_steering_refusal_covers_the_legacy_latch_too(tmp_path, monkeypatch):
    """A-F19: a pre-migration wedged task must not accept new owner messages."""
    import supervisor.events as events_mod
    from ouroboros.owner_mailbox import drain_owner_messages
    from supervisor.events import _handle_steer_task

    write_task_result(tmp_path, "legacy-steer", STATUS_CANCEL_REQUESTED, result="wedged")
    receipts: list = []
    monkeypatch.setattr(events_mod, "_emit_routing_receipt",
                        lambda ctx, evt, **kw: receipts.append(kw) or {})
    sent: list = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"legacy-steer": {"task": {"id": "legacy-steer", "chat_id": 1}}},
        PENDING=[],
        get_chat_agent=lambda: None,
        send_with_budget=lambda *a, **k: sent.append(a),
        persist_queue_snapshot=lambda **_kw: True,
    )
    _handle_steer_task(
        {"target_task_id": "legacy-steer", "message": "new orders", "chat_id": 1}, ctx,
    )
    assert receipts and receipts[0]["reason"] == "cancel_pending"
    assert drain_owner_messages(tmp_path, "legacy-steer") == []


# --------------------------------------------------------------------------
# Round-2 fixes (AR2-1..AR2-13): secondary-ingress fail-closed, settle-owner
# unity, durable task_done validation, delivery ordering, takeover race
# --------------------------------------------------------------------------


def test_evolution_stop_refuses_teardown_when_the_intent_write_fails(qenv, monkeypatch):
    """AR2-1 (owner 1=A) + GR2-13: no cancel without a durable intent — the task
    is KEPT (pending rows stay queued, nothing is killed) and the failure is in
    the caller's typed view instead of vanishing behind a clean 'stopped'."""
    qenv.q.RUNNING["evo1"] = {"task": {"id": "evo1", "chat_id": 1, "type": "evolution"},
                              "worker_id": 0}
    qenv.q.PENDING[:] = [{"id": "evo-queued", "chat_id": 1, "type": "evolution"}]
    killed: list = []
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **_kw: killed.append(tid) or qenv.q.CANCEL_CANCELLED)
    monkeypatch.setattr(
        "ouroboros.cancel_intents.request_cancel",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("intent store io")),
    )

    out = qenv.q.stop_evolution_tasks("owner stop")

    assert out["cancelled"] == []
    assert sorted(out["intent_write_failed"]) == ["evo-queued", "evo1"]
    assert killed == [], "no unfenced teardown"
    assert [t["id"] for t in qenv.q.PENDING] == ["evo-queued"], "the task is kept"
    lines, incomplete = qenv.q.evolution_stop_report(out)
    assert incomplete is True and any("INCOMPLETE" in line for line in lines)


def test_project_delete_refuses_teardown_when_the_intent_write_fails(qenv, monkeypatch):
    """AR2-1: the project-delete ingress fails CLOSED — the task stays live and
    the deletion fails visibly instead of tearing down without a durable fence."""
    from supervisor import queue_transitions as qt

    monkeypatch.setattr(
        qt, "_live_project_task_ids",
        lambda root, pid, roots_only=False, covering=None: ["p-task1"],
    )
    failed: list = []
    monkeypatch.setattr("ouroboros.projects_registry.fail_project_deletion",
                        lambda root, pid, err: failed.append((pid, err)))
    monkeypatch.setattr(
        "ouroboros.projects_registry.complete_project_deletion",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not complete")),
    )
    killed: list = []
    monkeypatch.setattr(qenv.q, "cancel_task_by_id",
                        lambda tid, **_kw: killed.append(tid) or True)
    monkeypatch.setattr(
        "ouroboros.cancel_intents.request_cancel",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("intent store io")),
    )

    qt.run_project_deletion(qenv.drive, "proj1", 1)

    assert killed == [], "no unfenced teardown"
    assert failed and "cancel_intent_write_failed" in failed[0][1]


def test_cascade_descendant_intent_failure_is_surfaced_not_silent(qenv, monkeypatch):
    """AR2-1: a child whose per-descendant intent write fails is still cancelled
    THIS sweep, and the failure is a typed forensic row — never a debug line."""
    calls: list = []

    def _mock_custody(tid, **_kw):
        calls.append(tid)
        qenv.q.PENDING[:] = [t for t in qenv.q.PENDING if str(t.get("id")) != tid]
        write_task_result(qenv.drive, tid, STATUS_CANCELLED, result="cancelled")
        ci.settle_intent(qenv.drive, tid, outcome="cancelled")
        return qenv.q.CANCEL_CANCELLED

    monkeypatch.setattr(qenv.q, "cancel_task_custody", _mock_custody)
    monkeypatch.setattr("supervisor.terminal_delivery.deliver_cascade_summary",
                        lambda *_a, **_kw: None)
    real_request = ci.request_cancel

    def _flaky(root, tid, **kw):
        if tid == "d-kid":
            raise OSError("intent store io")
        return real_request(root, tid, **kw)

    monkeypatch.setattr("ouroboros.cancel_intents.request_cancel", _flaky)
    qenv.q.PENDING[:] = [
        {"id": "d-root", "chat_id": 2},
        {"id": "d-kid", "chat_id": 2, "parent_task_id": "d-root", "root_task_id": "d-root"},
    ]
    real_request(qenv.drive, "d-root", reason="stop the tree")

    assert qenv.tl.cancel_task_by_id("d-root", cascade=True) is True
    assert "d-kid" in calls, "custody still runs on the child this sweep"
    trail = (qenv.drive / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert "cascade_descendant_intent_write_failed" in trail


def test_drop_cancelled_pending_yields_to_a_live_claim_owner(qenv, monkeypatch):
    """AR2-2: the pre-assignment drop CLAIMS before it settles. A live custody's
    claim wins — the task still leaves the queue (it must not be assigned) but
    nothing is written, settled, or emitted here; the claim owner does all three."""
    from supervisor import workers

    emitted: list = []
    monkeypatch.setattr(workers, "_emit_task_done_terminal",
                        lambda task, tid, status, **kw: emitted.append((tid, status)) or True)
    monkeypatch.setattr(workers, "PENDING", qenv.q.PENDING, raising=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive, raising=False)
    qenv.q.PENDING[:] = [{"id": "drop-owned", "chat_id": 1}]
    write_task_result(qenv.drive, "drop-owned", "scheduled")
    ci.request_cancel(qenv.drive, "drop-owned")
    ci.claim_intent(qenv.drive, "drop-owned", owner="cancel_task_custody")  # live owner

    workers._drop_cancelled_pending()

    assert qenv.q.PENDING == [], "it must not be assigned to a worker"
    assert emitted == [], "the claim owner emits, not the drop"
    assert load_task_result(qenv.drive, "drop-owned")["status"] == "scheduled"
    intent = ci.active_intent(qenv.drive, "drop-owned")
    assert intent["state"] == ci.INTENT_CLAIMED
    assert intent["claim_owner"] == "cancel_task_custody"


def test_fail_tasks_yields_to_a_live_claim_owner(tmp_path):
    """AR2-2: the budget drain claims before settling; a live custody's claim
    wins and the drain leaves the task entirely to that owner."""
    from ouroboros.task_results import fail_tasks

    write_task_result(tmp_path, "b2", "scheduled")
    ci.request_cancel(tmp_path, "b2")
    ci.claim_intent(tmp_path, "b2", owner="cancel_task_custody")

    written = fail_tasks(
        tmp_path, [{"id": "b2"}], reason_code="budget_exhausted", result="drained",
    )

    assert written == 0
    assert load_task_result(tmp_path, "b2")["status"] == "scheduled"
    assert ci.active_intent(tmp_path, "b2")["claim_owner"] == "cancel_task_custody"


def test_custody_refuses_when_the_claim_cannot_be_read(qenv, monkeypatch):
    """AR2-2: a claim attempt that RAISED cannot prove exclusivity — custody
    refuses and gives the capture back instead of settling unfenced."""
    ci.request_cancel(qenv.drive, "claim-io", reason="stop")
    qenv.q.PENDING[:] = [{"id": "claim-io", "chat_id": 1}]
    write_task_result(qenv.drive, "claim-io", "scheduled")
    monkeypatch.setattr(
        "ouroboros.cancel_intents.claim_intent",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("intent store io")),
    )

    assert qenv.tl.cancel_task_custody("claim-io") == qenv.tl.CANCEL_FAILED
    assert [t["id"] for t in qenv.q.PENDING] == ["claim-io"], "capture returned"
    assert load_task_result(qenv.drive, "claim-io")["status"] == "scheduled"


def test_custody_without_any_intent_is_the_documented_legacy_path(qenv, monkeypatch):
    """AR2-2: claim → None (no active intent) is the legacy/no-intent path —
    capture under the queue lock is the mutual exclusion and custody proceeds."""
    qenv.q.PENDING[:] = [{"id": "no-intent", "chat_id": 1}]
    write_task_result(qenv.drive, "no-intent", "scheduled")
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)

    assert qenv.tl.cancel_task_custody("no-intent") == qenv.tl.CANCEL_CANCELLED
    assert load_task_result(qenv.drive, "no-intent")["status"] == STATUS_CANCELLED


def test_task_done_with_settled_claim_but_nonsettled_durable_row_is_a_fault(tmp_path):
    """AR2-3 (§8-A1): the DURABLE result decides — an event claiming
    ``completed`` over a ``running`` row is refused as a durable lifecycle
    fault, terminalized, and the slot freed by the existing fault resolution."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    write_task_result(tmp_path, "t13", STATUS_RUNNING, result="still working")
    running = {"t13": {"task": {"id": "t13"}}}
    slot = types.SimpleNamespace(busy_task_id="t13", reaping=False)
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING=running, WORKERS={3: slot},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    _handle_task_done({"task_id": "t13", "status": "completed", "worker_id": 3}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == "running"
    assert "t13" not in running and slot.busy_task_id is None
    stored = load_task_result(tmp_path, "t13")
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"


def test_task_done_claiming_settled_with_no_durable_row_is_a_fault(tmp_path):
    """AR2-3: a worker that emitted task_done(completed) without EVER writing a
    result row is the purest durable fault — refused, never admitted."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING={"t14": {"task": {"id": "t14"}}}, WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    _handle_task_done({"task_id": "t14", "status": "completed"}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == ""
    assert load_task_result(tmp_path, "t14")["status"] == STATUS_FAILED


def test_task_done_with_a_settled_durable_row_passes_the_durable_gate(tmp_path):
    """AR2-3 negative: an honest completion (settled row on disk) is admitted."""
    from ouroboros.utils import append_jsonl as _append_jsonl
    from supervisor.events import _handle_task_done

    write_task_result(tmp_path, "t15", STATUS_COMPLETED, result="done")

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = {"t15": {"task": {"id": "t15"}}}
        WORKERS: dict = {}
        append_jsonl = staticmethod(_append_jsonl)
        persist_queue_snapshot = staticmethod(lambda **_kw: True)

    try:
        _handle_task_done({"task_id": "t15", "status": "completed"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; passing the gate is the point
    assert not _fault_rows(tmp_path)


def test_deliver_final_message_live_registers_owed_before_enqueue(tmp_path):
    """AR2-4 (§8-A2): the NORMAL terminal path enters the durable outbox — the
    answer is owed BEFORE the enqueue, so a crash between put and processing
    replays it; the shared delivery id keeps it single-delivery."""
    from ouroboros.task_finalization import deliver_final_message_live
    from supervisor import terminal_delivery as td

    events = [{"type": "send_message", "chat_id": 3, "task_id": "fin1", "text": "the answer"}]

    class _BoomQueue:
        def put(self, evt):
            raise RuntimeError("queue died")

    # Even when the put dies, the answer is already OWED — the crash window the
    # incident lived in is closed for this seam.
    assert deliver_final_message_live(_BoomQueue(), events, "fin1", drive_root=tmp_path) is False
    owed = td.pending_deliveries(tmp_path)
    assert [row["task_id"] for row in owed] == ["fin1"]
    did = str(events[0]["delivery_id"])
    assert owed[0]["delivery_id"] == did

    # The normal path enqueues the same id; a confirmed send clears the row.
    queue = _CaptureQueue()
    assert deliver_final_message_live(queue, events, "fin1", drive_root=tmp_path) is True
    (sent,) = queue.events
    assert sent["delivery_id"] == did
    td.register_delivery(tmp_path, did)
    assert td.pending_deliveries(tmp_path) == []

    # A final without a chat id is never registered: replay could not send it.
    events2 = [{"type": "send_message", "chat_id": 0, "task_id": "fin2", "text": "x"}]
    assert deliver_final_message_live(_CaptureQueue(), events2, "fin2", drive_root=tmp_path) is True
    assert td.pending_deliveries(tmp_path) == []


def test_reaper_registers_the_salvage_before_task_done(qenv, monkeypatch):
    """AR2-5a crash order: the owed salvage delivery precedes the task_done
    enqueue, so a crash between them can no longer resolve the card while
    losing the owner's answer."""
    from supervisor import task_reaper as tr
    from supervisor import workers as workers_mod

    calls: list = []
    monkeypatch.setattr(tr, "_kill_and_confirm_worker_dead", lambda *_a, **_kw: True)
    monkeypatch.setattr(tr, "_deliver_reap_salvage",
                        lambda _q, task, tid, reason, unreconciled_runs=None:
                        calls.append(("salvage", tid)))
    monkeypatch.setattr(
        workers_mod, "get_event_q",
        lambda: types.SimpleNamespace(
            put=lambda evt: calls.append((str(evt.get("type")), str(evt.get("task_id")))),
        ),
    )
    monkeypatch.setattr(workers_mod, "respawn_worker", lambda wid: None)
    monkeypatch.setattr(
        qenv.q, "reconstruct_task_cost",
        lambda tid, fields=True, **_kw: {"cost_accounting_status": "available",
                                         "cost_final": True, "cost_usd": 0.0},
    )

    tr.reap_timed_out_task({
        "worker_id": 0, "proc": None, "task_id": "reap1",
        "task": {"id": "reap1", "chat_id": 4}, "task_type": "chat",
        "terminal_reason": "idle_timeout", "attempt": 3, "owner_chat_id": 0,
        "runtime_sec": 10.0, "will_retry": False,
    })

    assert ("salvage", "reap1") in calls
    assert ("task_done", "reap1") in calls
    assert calls.index(("salvage", "reap1")) < calls.index(("task_done", "reap1"))


def test_finalize_on_miss_delivers_the_unreviewed_salvage(qenv, monkeypatch):
    """AR2-5b (owner 5=A): the miss lane used to emit NO delivery at all — a
    cancelled outcome now ships the unreviewed salvage through the shared seam."""
    delivered: list = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.deliver_unreviewed_salvage",
        lambda drive, task, tid, **kw: delivered.append({"task_id": tid, **kw}),
    )
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    write_task_result(qenv.drive, "miss-del", STATUS_RUNNING, result="was working",
                      chat_id=6)
    ci.request_cancel(qenv.drive, "miss-del", reason="stop")

    assert qenv.tl.cancel_task_custody("miss-del") == qenv.tl.CANCEL_CANCELLED
    (row,) = delivered
    assert row["task_id"] == "miss-del"
    assert row["outcome"] == "cancelled"


def test_finalize_on_miss_completion_wins_delivers_the_completed_result(qenv, monkeypatch):
    """AR2-5b: the completion-wins branch of the miss lane delivers the KEPT
    answer through the normal deduped seam — owed BEFORE enqueued."""
    from supervisor import terminal_delivery as td
    from supervisor import workers as workers_mod

    queue = _CaptureQueue()
    monkeypatch.setattr(workers_mod, "get_event_q", lambda: queue)
    child_drive = qenv.drive / "child-of-misswin"
    write_task_result(child_drive, "miss-win", STATUS_COMPLETED,
                      result="the finished answer", chat_id=6)
    write_task_result(qenv.drive, "miss-win", STATUS_RUNNING, result="mirror",
                      chat_id=6, child_drive_root=str(child_drive))
    ci.request_cancel(qenv.drive, "miss-win", reason="late cancel")

    assert qenv.tl.cancel_task_custody("miss-win") == qenv.tl.CANCEL_ALREADY_SETTLED
    (sent,) = [e for e in queue.events if e.get("type") == "send_message"]
    assert sent["text"] == "the finished answer"
    assert sent["chat_id"] == 6
    owed = td.pending_deliveries(qenv.drive)
    assert [r["delivery_id"] for r in owed] == [sent["delivery_id"]], "owed before enqueued"


def test_snapshot_restore_consults_the_intent_projection_under_the_queue_lock(
    qenv, monkeypatch,
):
    """AR2-10 (§8-A1): the projection read at restore holds the queue lock, so
    the "no active intent" view and the enqueue are one serialized step."""
    from ouroboros.utils import utc_now_iso

    consults: list = []

    def _spy(root, tid):
        consults.append(qenv.q._queue_lock._is_owned())
        return True  # refusal path: no enqueue side effects in this harness

    monkeypatch.setattr("ouroboros.cancel_intents.has_active_intent", _spy)
    snapshot = {
        "ts": utc_now_iso(),
        "pending": [{"task": {"id": "locked-restore", "chat_id": 1, "type": "chat"}}],
        "running": [],
        "acceptance_fences": [],
        "budget_root_fences": [],
    }
    state_dir = qenv.drive / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "queue_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(qenv.q, "QUEUE_SNAPSHOT_PATH", state_dir / "queue_snapshot.json",
                        raising=False)

    assert qenv.q.restore_pending_from_snapshot() == 0
    assert qenv.q.PENDING == []
    assert consults == [True], "the intent consult must hold the queue lock"


# --------------------------------------------------------------------------
# Gate round-2 fixes (GR2-1..GR2-13): cascade durable ownership, claim-before-
# capture, unconditional durable task_done validation, owed-before-settle,
# outbox coverage + loud eviction, reconciliation honesty, staging order
# --------------------------------------------------------------------------


def test_mark_intent_scope_is_widen_only(tmp_path):
    """GR2-1d: single→cascade widens; cascade→single is refused as a no-op plus
    a forensic row (a narrowed record would replay the root alone)."""
    ci.request_cancel(tmp_path, "w1")
    assert ci.active_intent(tmp_path, "w1")["scope"] == ci.SCOPE_SINGLE
    assert ci.mark_intent_scope(tmp_path, "w1", ci.SCOPE_CASCADE) is True
    assert ci.active_intent(tmp_path, "w1")["scope"] == ci.SCOPE_CASCADE

    assert ci.mark_intent_scope(tmp_path, "w1", ci.SCOPE_SINGLE) is False
    assert ci.active_intent(tmp_path, "w1")["scope"] == ci.SCOPE_CASCADE
    trail = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert "scope_narrow_refused" in trail

    # The re-request path is widen-only too: an explicit single re-request over
    # a cascade intent must not narrow the recorded shape.
    again = ci.request_cancel(tmp_path, "w1", scope=ci.SCOPE_SINGLE)
    assert again["already_requested"] is True
    assert ci.active_intent(tmp_path, "w1")["scope"] == ci.SCOPE_CASCADE


def test_request_cancel_mints_a_cascade_coordination_intent_over_a_settled_target(tmp_path):
    """GR2-1b (store half): the cascade ingress may mint over a SETTLED root —
    that intent is the watchdog's replay trigger for the live descendants."""
    write_task_result(tmp_path, "sr0", "failed", result="died on budget")
    refused = ci.request_cancel(tmp_path, "sr0", scope=ci.SCOPE_CASCADE)
    assert refused["already_settled"] is True and ci.active_intent(tmp_path, "sr0") is None

    minted = ci.request_cancel(
        tmp_path, "sr0", scope=ci.SCOPE_CASCADE, allow_settled_target=True,
    )
    assert minted["already_requested"] is False
    row = ci.active_intent(tmp_path, "sr0")
    assert row is not None and row["scope"] == ci.SCOPE_CASCADE
    # The effective-status read never projects a false "Cancelling…" badge onto
    # the settled card (the projection only rides non-settled results).
    from ouroboros.task_status import load_effective_task_result

    assert "cancel_state" not in load_effective_task_result(tmp_path, "sr0")


def test_cascade_over_settled_root_keeps_the_intent_until_the_postcondition(qenv, monkeypatch):
    """GR2-1b/1e: a settled root with a live child keeps its durable cascade
    intent through a failed sweep (the crash-mid-sweep shape) — per-task custody
    defers the settle while descendants remain — and the intent settles only
    when a later cascade's no-live postcondition passes."""
    delivered: list = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.deliver_unreviewed_salvage",
        lambda drive, task, tid, **kw: delivered.append(tid),
    )
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    write_task_result(qenv.drive, "sr1", "failed", reason_code="budget_exhausted",
                      result="root died on budget")
    qenv.q.PENDING[:] = [
        {"id": "sr1-kid", "chat_id": 9, "parent_task_id": "sr1", "root_task_id": "sr1"},
    ]
    write_task_result(qenv.drive, "sr1-kid", "scheduled")
    ci.request_cancel(qenv.drive, "sr1", scope=ci.SCOPE_CASCADE, allow_settled_target=True)

    # Sweep 1: the child's custody FAILS (simulated crash / stubborn teardown).
    real_custody = qenv.tl.cancel_task_custody
    monkeypatch.setattr(
        qenv.q, "cancel_task_custody",
        lambda tid, **kw: qenv.q.CANCEL_FAILED if tid == "sr1-kid" else real_custody(tid, **kw),
    )
    assert qenv.tl.cancel_task_by_id("sr1", cascade=True) is False
    row = ci.active_intent(qenv.drive, "sr1")
    assert row is not None and row["scope"] == ci.SCOPE_CASCADE, (
        "the durable cascade intent must survive a failed sweep — it is the "
        "watchdog's replay trigger for the live descendants"
    )

    # The watchdog replay converges: custody works now, postcondition settles it.
    monkeypatch.setattr(qenv.q, "cancel_task_custody", real_custody)
    assert qenv.tl.cancel_task_by_id("sr1", cascade=True) is True
    assert ci.active_intent(qenv.drive, "sr1") is None
    assert load_task_result(qenv.drive, "sr1-kid")["status"] == STATUS_CANCELLED
    assert delivered, "the tree's summary still reaches chat"


def test_two_concurrent_custodies_on_a_pending_task_settle_exactly_once(qenv, monkeypatch):
    """GR2-2 (sol's repro shape): two threads racing custody over one pending
    task used to produce TWO cancelled writes and TWO task_done events — the
    loser entered the miss lane before the winner claimed. Claim-before-capture
    makes exactly one settle owner in every interleaving."""
    import threading

    ci.request_cancel(qenv.drive, "race-2t", reason="stop")
    qenv.q.PENDING[:] = [{"id": "race-2t", "chat_id": 1}]
    write_task_result(qenv.drive, "race-2t", "scheduled")
    done_events: list = []
    monkeypatch.setattr(
        qenv.q, "_emit_cancel_task_done",
        lambda t, tid, **kw: done_events.append(tid),
    )
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _run():
        barrier.wait()
        outcomes.append(qenv.tl.cancel_task_custody("race-2t"))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count(qenv.tl.CANCEL_CANCELLED) == 1, outcomes
    assert done_events == ["race-2t"], "exactly ONE task_done"
    assert load_task_result(qenv.drive, "race-2t")["status"] == STATUS_CANCELLED
    assert ci.active_intent(qenv.drive, "race-2t") is None
    assert qenv.q.PENDING == [], "the loser must not re-insert the captured row"


def test_blank_status_task_done_over_a_running_row_is_a_durable_fault(tmp_path):
    """GR2-3a (reproduced): the PRIMARY producer emits task_done with NO status,
    so the settled-claim gate skipped validation entirely — a blank-status event
    over a non-settled durable row now faults like any dishonest terminal."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    write_task_result(tmp_path, "blank1", STATUS_RUNNING, result="working")
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING={"blank1": {"task": {"id": "blank1"}}}, WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    _handle_task_done({"task_id": "blank1"}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == STATUS_RUNNING
    stored = load_task_result(tmp_path, "blank1")
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"


def test_blank_status_task_done_over_a_settled_row_is_admitted(tmp_path):
    """GR2-3a negative: the honest ordinary completion (durable settled row,
    blank event status) passes the durable gate."""
    from ouroboros.utils import append_jsonl as _append_jsonl
    from supervisor.events import _handle_task_done

    write_task_result(tmp_path, "blank2", STATUS_COMPLETED, result="done")

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = {"blank2": {"task": {"id": "blank2"}}}
        WORKERS: dict = {}
        append_jsonl = staticmethod(_append_jsonl)
        persist_queue_snapshot = staticmethod(lambda **_kw: True)

    try:
        _handle_task_done({"task_id": "blank2"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; passing the gate is the point
    assert not _fault_rows(tmp_path)
    assert load_task_result(tmp_path, "blank2")["status"] == STATUS_COMPLETED


def test_copy_back_exception_never_synthesizes_a_completed_row(tmp_path, monkeypatch):
    """GR2-3b: a copy-back exception used to skip validation AND default a
    MISSING row's status to "completed" — a fabricated completion the monotonic
    guard then defended. The exception path now annotates only existing rows
    and still routes through the durable lifecycle-fault seam."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl
    from supervisor.events import _handle_task_done

    monkeypatch.setattr(
        "ouroboros.headless.copy_child_task_result",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("child drive unreadable")),
    )
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"cb1": {"task": {"id": "cb1", "child_drive_root": str(tmp_path / "nope")}}},
        WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    _handle_task_done({"task_id": "cb1"}, ctx)

    stored = load_task_result(tmp_path, "cb1")
    assert stored["status"] == STATUS_FAILED, "never a synthesized completed"
    assert stored["reason_code"] == "task_done_lifecycle_fault"
    assert _fault_rows(tmp_path), "the fault is recorded, not swallowed"


@pytest.mark.serial
def test_kill_path_registers_the_owed_answer_before_the_intent_settles(qenv, monkeypatch):
    """GR2-4 crash order: the owner's terminal answer is durably OWED before the
    intent settles — a crash between the two replays instead of losing both the
    watchdog trigger and the answer."""
    from supervisor import terminal_delivery as td

    order: list = []
    real_register = td.register_pending_delivery
    monkeypatch.setattr(
        "supervisor.terminal_delivery.register_pending_delivery",
        lambda root, evt: order.append(("owed", str(evt.get("task_id") or ""))) or real_register(root, evt),
    )
    real_settle = ci.settle_intent
    monkeypatch.setattr(
        "ouroboros.cancel_intents.settle_intent",
        lambda root, tid, **kw: order.append(("settle", tid)) or real_settle(root, tid, **kw),
    )
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)

    task_id = "owed-order"
    task = {"id": task_id, "chat_id": 9}
    proc = _LiveProc()
    qenv.workers.WORKERS[0] = types.SimpleNamespace(
        wid=0, proc=proc, busy_task_id=task_id, reaping=False,
    )
    qenv.q.RUNNING[task_id] = {"task": task, "worker_id": 0}
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working", chat_id=9)
    _seed_llm_response(qenv.drive, task_id, "the salvaged partial answer")
    ci.request_cancel(qenv.drive, task_id, reason="stop")

    try:
        assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_CANCELLED
    finally:
        proc.terminate()

    owed_at = order.index(("owed", task_id))
    settle_at = order.index(("settle", task_id))
    assert owed_at < settle_at, f"owed must precede the settle: {order}"
    owed_rows = td.pending_deliveries(qenv.drive)
    assert any(row.get("task_id") == task_id for row in owed_rows), (
        "the durable outbox holds the answer until a send confirms"
    )


def test_fast_settled_reentry_delivers_idempotently_and_settles_with_the_claim(
    qenv, monkeypatch,
):
    """GR2-4 (fast already-settled re-entry): delivery runs BEFORE the settle
    and the settle is fenced by the claimed generation — never an unfenced
    removal of an intent another owner may hold."""
    order: list = []
    monkeypatch.setattr(
        "supervisor.terminal_delivery.deliver_miss_lane_outcome",
        lambda *a, **kw: order.append(("deliver", str(a[3]))),
    )
    real_settle = ci.settle_intent
    monkeypatch.setattr(
        "ouroboros.cancel_intents.settle_intent",
        lambda root, tid, **kw: order.append(("settle", tid)) or real_settle(root, tid, **kw),
    )
    write_task_result(qenv.drive, "fast1", STATUS_RUNNING, result="working", chat_id=6)
    ci.request_cancel(qenv.drive, "fast1", reason="stop")
    # Natural completion wins the race before custody arrives.
    write_task_result(qenv.drive, "fast1", STATUS_COMPLETED, result="the answer", chat_id=6)

    assert qenv.tl.cancel_task_custody("fast1") == qenv.tl.CANCEL_ALREADY_SETTLED

    assert order.index(("deliver", "fast1")) < order.index(("settle", "fast1"))
    assert ci.active_intent(qenv.drive, "fast1") is None
    settled_rows = [
        json.loads(line)
        for line in (qenv.drive / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    settle_row = next(
        r for r in settled_rows
        if r.get("type") == "cancel_intent" and r.get("event") == "settled"
        and r.get("task_id") == "fast1"
    )
    assert int(settle_row.get("generation") or 0) >= 1, (
        "the settle must ride the claimed generation, not an unfenced removal"
    )


def _emit_root_results(tmp_path, monkeypatch, *, text="the final answer"):
    """Drive the REAL emit_task_results for an ordinary nonblocking root."""
    import time

    import ouroboros.agent_task_pipeline as atp

    drive_root = tmp_path / "data"
    logs = drive_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    class _FakeEnv:
        def __init__(self, root):
            self.drive_root = root

        def drive_path(self, sub):
            p = self.drive_root / sub
            p.mkdir(parents=True, exist_ok=True)
            return p

    class _FakeMemory:
        def load_identity(self):
            return "id"

    monkeypatch.setattr(atp, "_run_post_task_processing_async", lambda *a, **kw: None)
    pending_events: list = []
    task = {"id": "nb-root", "type": "task", "chat_id": 3, "text": "hello"}
    atp.emit_task_results(
        env=_FakeEnv(drive_root), memory=_FakeMemory(), llm=None,
        pending_events=pending_events,
        task=task, text=text,
        usage={"cost": 0.0, "rounds": 1, "prompt_tokens": 1, "completion_tokens": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=time.time() - 1.0,
        drive_logs=logs,
        ctx=types.SimpleNamespace(pending_restart_reason=None),
    )
    return drive_root, pending_events


def test_every_nonblocking_root_answer_enters_the_durable_outbox(tmp_path, monkeypatch):
    """GR2-5 crash shape: an ordinary (nonblocking) root's final answer is owed
    in the durable outbox right after result persistence — a worker crash before
    the buffered drain no longer loses the answer; boot replay delivers once."""
    from supervisor import terminal_delivery as td

    drive_root, pending_events = _emit_root_results(tmp_path, monkeypatch)

    send = next(e for e in pending_events if e.get("type") == "send_message")
    assert str(send.get("delivery_id") or "").startswith("final:nb-root:")
    owed = td.pending_deliveries(drive_root)
    assert [row["delivery_id"] for row in owed] == [send["delivery_id"]]

    # Crash shape: the buffered send never went out. Boot replay delivers ONCE.
    _age_pending_rows(drive_root)
    queue = _CaptureQueue()
    assert td.replay_pending_deliveries(drive_root, event_queue=queue) == [send["delivery_id"]]
    (replayed,) = queue.events
    assert replayed["text"] == "the final answer" and replayed["chat_id"] == 3

    # The confirmed send clears the owed row; nothing double-delivers after.
    td.register_delivery(drive_root, send["delivery_id"])
    queue.events.clear()
    assert td.replay_pending_deliveries(drive_root, event_queue=queue) == []
    assert queue.events == []


def test_normal_path_stays_single_send_with_the_owed_registration(tmp_path, monkeypatch):
    """GR2-5 no-double half: the pipeline registration and the blocking path's
    deliver_final_message_live mint the SAME delivery id, so registration is
    idempotent and the send handler's dedupe keeps one delivery."""
    from ouroboros.task_finalization import deliver_final_message_live
    from supervisor import terminal_delivery as td

    drive_root, pending_events = _emit_root_results(tmp_path, monkeypatch)
    send = next(e for e in pending_events if e.get("type") == "send_message")
    did = send["delivery_id"]

    # The live-delivery seam re-registers the same event: still ONE owed row.
    queue = _CaptureQueue()
    assert deliver_final_message_live(queue, pending_events, "nb-root", drive_root=drive_root)
    assert send["delivery_id"] == did, "the id is stable across both seams"
    owed = td.pending_deliveries(drive_root)
    assert [row["delivery_id"] for row in owed] == [did], "no second owed row"


def test_outbox_capacity_eviction_is_disclosed(tmp_path, monkeypatch):
    """GR2-6 (reproduced): the 65th registration used to silently pop the oldest
    owed answer. The eviction now preserves the full text, emits the typed
    durable event with the distinct outbox_capacity reason, and notifies chat."""
    from supervisor import terminal_delivery as td

    notices: list = []
    monkeypatch.setattr(
        "supervisor.message_bus.send_with_budget",
        lambda chat_id, text, **kw: notices.append((chat_id, text)),
    )
    for i in range(td._PENDING_CAP):
        td.register_pending_delivery(tmp_path, {
            "type": "send_message", "chat_id": 5, "task_id": f"cap{i}",
            "text": f"answer {i}", "delivery_id": td.delivery_id_for(f"cap{i}", f"answer {i}"),
        })
    assert len(td.pending_deliveries(tmp_path)) == td._PENDING_CAP

    td.register_pending_delivery(tmp_path, {
        "type": "send_message", "chat_id": 5, "task_id": "cap-new",
        "text": "the newest answer", "delivery_id": td.delivery_id_for("cap-new", "the newest answer"),
    })

    ids = {row["task_id"] for row in td.pending_deliveries(tmp_path)}
    assert "cap-new" in ids and "cap0" not in ids, "oldest evicted, newest kept"
    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    (evicted,) = [r for r in rows if r.get("type") == "terminal_delivery_exhausted"]
    assert evicted["task_id"] == "cap0"
    assert evicted["reason"] == "outbox_capacity"
    preserved = pathlib.Path(str(evicted["preserved_path"]))
    assert preserved.is_file() and "answer 0" in preserved.read_text(encoding="utf-8")
    assert notices and "cap0" in notices[0][1], "owner-visible notice"


def test_reconcile_discloses_open_runs_even_when_outcomes_are_nonempty(qenv, monkeypatch):
    """GR2-7: a non-empty reconcile outcome list proves an ATTEMPT, not a
    settlement — unreadable/requested/failed outcomes and raising transports
    must still surface every run the durable custody rows say is open."""
    monkeypatch.setattr(
        "ouroboros.delegate_custody.reconcile_task_runs",
        lambda *_a, **_kw: [{"outcome": "unreadable", "run_id": "run-open"}],
    )
    monkeypatch.setattr(
        "ouroboros.delegate_custody.open_runs",
        lambda *_a, **_kw: [types.SimpleNamespace(task_id="rt1", run_id="run-open")],
    )
    assert qenv.tl._reconcile_delegated_runs_on_kill(qenv.q, "rt1") == ["run-open"]
    rows = [
        json.loads(line)
        for line in (qenv.drive / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r for r in rows if r.get("type") == "delegated_runs_unreconciled"]

    # A RAISING reconcile is audited the same way, never swallowed into [].
    monkeypatch.setattr(
        "ouroboros.delegate_custody.reconcile_task_runs",
        lambda *_a, **_kw: (_ for _ in ()).throw(ConnectionError("daemon gone")),
    )
    assert qenv.tl._reconcile_delegated_runs_on_kill(qenv.q, "rt1") == ["run-open"]


def test_steer_refusal_removes_the_just_staged_attachments(tmp_path, monkeypatch):
    """GR2-9: a steering message refused by the transactional cancel re-check
    must not leave its just-staged input files in the dying task's store."""
    import supervisor.events as events_mod
    import supervisor.queue as queue_mod
    from supervisor.events import _handle_steer_task

    monkeypatch.setattr(queue_mod, "DRIVE_ROOT", tmp_path)
    write_task_result(tmp_path, "steer-stage", STATUS_RUNNING, result="working")
    source = tmp_path / "owner-input.txt"
    source.write_text("owner attachment", encoding="utf-8")

    # The up-front check passes (no cancel yet); the cancel ingress lands in the
    # window before the transactional re-check — exactly the staged-then-refused
    # shape the fix removes.
    checks = {"n": 0}
    real_pending = ci.cancel_pending

    def _racing_cancel_pending(root, tid):
        checks["n"] += 1
        if checks["n"] == 2 and tid == "steer-stage":
            ci.request_cancel(tmp_path, "steer-stage", reason="race")
        return real_pending(root, tid)

    monkeypatch.setattr("ouroboros.cancel_intents.cancel_pending", _racing_cancel_pending)
    receipts: list = []
    monkeypatch.setattr(events_mod, "_emit_routing_receipt",
                        lambda ctx, evt, **kw: receipts.append(kw) or {})
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"steer-stage": {"task": {"id": "steer-stage", "chat_id": 1}}},
        PENDING=[],
        get_chat_agent=lambda: None,
        send_with_budget=lambda *a, **k: None,
        persist_queue_snapshot=lambda **_kw: True,
    )
    _handle_steer_task(
        {"target_task_id": "steer-stage", "message": "new orders", "chat_id": 1,
         "attachment_uploads": [{"path": str(source), "label": "input"}]},
        ctx,
    )

    assert receipts and receipts[-1]["reason"] == "cancel_pending"
    from ouroboros.artifacts import task_artifact_dir_path

    attach_dir = task_artifact_dir_path(tmp_path, "steer-stage") / "attachments"
    staged = list(attach_dir.glob("*")) if attach_dir.exists() else []
    assert staged == [], f"staged inputs must be removed on refusal: {staged}"
    from ouroboros.owner_mailbox import drain_owner_messages

    assert drain_owner_messages(tmp_path, "steer-stage") == []


def test_double_takeover_loser_restores_the_reaping_marker_as_found(qenv, monkeypatch):
    """AR2-11 (fable probe: two custodies over one abandoned claim): the LOSER'S
    refused-claim restore must put the reaping marker back exactly as found —
    blanking it would hand the winner's mid-kill process to assignment."""
    task_id = "double-takeover"
    worker = types.SimpleNamespace(
        wid=0, busy_task_id=task_id, reaping=True,  # marker left by the dead custody
        proc=types.SimpleNamespace(pid=None, is_alive=lambda: True,
                                   join=lambda timeout=None: None,
                                   terminate=lambda: None),
    )
    qenv.workers.WORKERS[0] = worker
    qenv.q.RUNNING[task_id] = {"task": {"id": task_id, "chat_id": 1}, "worker_id": 0}
    write_task_result(qenv.drive, task_id, STATUS_RUNNING, result="working")
    ci.request_cancel(qenv.drive, task_id)
    # The on-disk claim is ABANDONED (dead pid): the takeover gate passes.
    store = qenv.drive / "state" / "cancel_intents.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["intents"][task_id].update({
        "state": ci.INTENT_CLAIMED, "claim_owner": "dead-custody",
        "claim_pid": 2 ** 22, "claimed_at": ci.utc_now_iso(), "generation": 3,
    })
    store.write_text(json.dumps(data), encoding="utf-8")
    # ...but the WINNER claims in the window between this loser's capture and its
    # own claim: the claim comes back REFUSED.
    refused = {**data["intents"][task_id], "claim_refused": True}
    monkeypatch.setattr("ouroboros.cancel_intents.claim_intent",
                        lambda *_a, **_kw: refused)

    assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_FAILED
    assert qenv.workers.WORKERS[0].reaping is True, (
        "the loser must restore the marker as found — the winner is mid-kill behind it"
    )
