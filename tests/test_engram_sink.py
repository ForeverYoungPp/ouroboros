"""Contract tests for the Engram memory sink.

Covers the seed's Phase-2 acceptance criteria
(``.ouroboros/seed-engram-memory.yaml``):

- AC19 spool: zero loss when Engram is unreachable, idempotent forward, crash-safe
- AC20/C18 negative: a *to-do* is never stored as a memory
- AC8  gate: per-field and per-run caps, no auto-delete
- AC21/WO two-phase evolution outcome upserts on ``task_id``
- AC22 evolution outcome carries what it tried to do, not just the verdict
- AC4  stable identity ⇒ upsert rather than duplicate
- F7  an unresolved project scope refuses to send rather than leaking
"""

from __future__ import annotations

import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ouroboros.engram_client import EngramClient
from ouroboros.engram_sink import (
    FIELD_CAP_CHARS,
    MAX_EMITS_PER_RUN,
    SPOOL_REL,
    EngramSink,
    _looks_like_todo,
    _pending_records,
    build_sink,
)

# --------------------------------------------------------------------------- #
# stub Engram
# --------------------------------------------------------------------------- #


class _State:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail = False
        self.auth_seen: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *args):
        return

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        self.state.requests.append(
            {
                "method": method,
                "path": parsed.path,
                "params": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                "body": body,
            }
        )
        if self.state.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"down"}')
            return
        if parsed.path == "/observations" and method == "POST":
            payload = json.dumps({"id": len(self.state.requests), "ok": True}).encode()
        else:
            payload = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


@pytest.fixture()
def stub():
    state = _State()
    handler = type("_H", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _sink(tmp_path: pathlib.Path, base_url: str, **kw) -> EngramSink:
    repo = tmp_path / "ouroboros"
    repo.mkdir(exist_ok=True)
    client = EngramClient.from_repo(repo, base_url=base_url, timeout=0.5)
    return EngramSink(client=client, drive_root=tmp_path, session_id="s1", **kw)


def _saved(state: _State) -> list[dict]:
    return [r for r in state.requests if r["path"] == "/observations" and r["method"] == "POST"]


def _events(tmp_path: pathlib.Path) -> list[dict]:
    """Disclosures live in their own log so they cannot displace canonical events."""
    path = tmp_path / "logs" / "engram.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------- #
# AC18 / AC20 — the memory-vs-to-do boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "下一步要做 X",
        "TODO: fix the gate",
        "- Next steps: ship it",
        "待办：修 commit gate",
        "需要修 verification receipt",
        "Action items: rotate the token",
        "Open obligations: none",
        "proposed_next_step: rerun the suite",
    ],
)
def test_todo_shaped_content_is_rejected(text):
    assert _looks_like_todo(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "bcrypt cost=12 is the right balance",
        "Learned: refresh tokens need atomic rotation",
        "Chose Zustand over Redux because of bundle size",
        "The gate is green on both lanes",
    ],
)
def test_memory_shaped_content_passes_the_gate(text):
    assert _looks_like_todo(text) is False


def test_todo_is_never_sent_or_spooled(stub, tmp_path):
    """AC20/F: a work item must not reach Engram, and must not even spool."""
    state, url = stub
    sink = _sink(tmp_path, url)
    receipt = sink.emit("memory_action", title="next", content="下一步：把 gate 修了")
    assert receipt.status == "rejected" and receipt.reason == "todo_not_memory"
    assert _saved(state) == []
    assert sink.pending() == []


def test_todo_only_field_payload_is_rejected(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    receipt = sink.emit(
        "memory_action", title="t", content="a durable fact", fields={"proposed_next_step": "do X"}
    )
    assert receipt.status == "rejected" and receipt.reason == "todo_only_payload"


def test_proposed_next_step_is_not_forwarded_on_a_real_memory(stub, tmp_path):
    """C18: the field is dropped, the memory still lands."""
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_memory_action(
        {"type": "knowledge_write", "topic": "auth", "content": "tokens rotate atomically",
         "proposed_next_step": "next: document it"},
        task_id="t1",
    )
    body = _saved(state)[-1]["body"]
    assert body["topic_key"] == "auth"
    assert body["title"] and body["content"]
    # The work item is not carried into the memory payload at all.
    assert "proposed_next_step" not in json.dumps(body)
    assert "document it" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# AC8 / C3 — caps
# --------------------------------------------------------------------------- #


def test_long_field_is_truncated_not_rejected(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit("memory_action", title="t" * 400, content="c" * 5000)
    body = _saved(state)[-1]["body"]
    assert len(body["title"]) <= FIELD_CAP_CHARS
    assert body["title"].endswith("…")


def test_per_run_emit_cap_is_enforced(stub, tmp_path):
    """AC8: the run stops at the cap instead of flooding the store."""
    state, url = stub
    sink = _sink(tmp_path, url)
    results = [
        sink.emit("memory_action", title=f"t{i}", content=f"durable fact {i}")
        for i in range(MAX_EMITS_PER_RUN + 3)
    ]
    assert sum(1 for r in results if r.status == "sent") == MAX_EMITS_PER_RUN
    assert all(r.status == "capped" for r in results[MAX_EMITS_PER_RUN:])
    assert len(_saved(state)) == MAX_EMITS_PER_RUN


def test_empty_content_is_rejected(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    assert sink.emit("memory_action", title="t", content="   ").status == "rejected"


def test_unknown_kind_is_rejected(stub, tmp_path):
    state, url = stub
    assert _sink(tmp_path, url).emit("not_a_kind", title="t", content="c").status == "rejected"


# --------------------------------------------------------------------------- #
# AC19 / C17 — spool
# --------------------------------------------------------------------------- #


def test_unreachable_engram_spools_with_zero_loss_and_discloses_once(stub, tmp_path):
    state, url = stub
    state.fail = True
    sink = _sink(tmp_path, url)
    receipts = [
        sink.emit("memory_action", title=f"t{i}", content=f"durable fact {i}") for i in range(3)
    ]
    assert all(r.status == "spooled" for r in receipts)
    assert sink.pending_count() == 3                      # zero loss
    disclosures = [e for e in _events(tmp_path) if e.get("type") == "engram_unavailable"]
    assert len(disclosures) == 1                          # exactly one per run, no storm


def test_spool_forwards_idempotently_after_recovery(stub, tmp_path):
    state, url = stub
    state.fail = True
    sink = _sink(tmp_path, url)
    sink.emit("memory_action", title="t", content="a durable fact")
    assert sink.pending_count() == 1

    state.fail = False
    state.requests.clear()          # the failed attempt above is not a delivery
    forwarded = sink.flush()
    assert forwarded == 1
    assert sink.pending_count() == 0
    sent = _saved(state)
    assert len(sent) == 1
    # Idempotent identity travels with the record, so a repeat send upserts.
    assert sent[0]["body"]["topic_key"]


def test_spool_is_crash_safe_against_a_torn_final_line(tmp_path):
    path = tmp_path / SPOOL_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"v": 1, "op": "add", "spool_id": "a", "title": "t", "content": "c"}
    path.write_text(json.dumps(good) + "\n" + '{"v":1,"op":"add","spool_id":"b"', encoding="utf-8")
    pending = _pending_records(path)
    assert [r["spool_id"] for r in pending] == ["a"]


def test_acked_records_are_not_pending(tmp_path):
    path = tmp_path / SPOOL_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"v": 1, "op": "add", "spool_id": "a", "content": "c"},
        {"v": 1, "op": "ack", "spool_id": "a"},
        {"v": 1, "op": "add", "spool_id": "b", "content": "c"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert [r["spool_id"] for r in _pending_records(path)] == ["b"]


def test_successful_send_acks_immediately(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    assert sink.emit("memory_action", title="t", content="durable fact").status == "sent"
    assert sink.pending_count() == 0


def test_sink_never_raises_on_garbage_input(tmp_path):
    """C6: nothing here may escape as an exception into a task."""
    sink = _sink(tmp_path, "http://127.0.0.1:9")
    for bad in (None, 123, {"weird": object()}):
        receipt = sink.emit_memory_action(bad, task_id="t")  # type: ignore[arg-type]
        assert receipt.status in {"sent", "spooled", "rejected", "capped", "failed"}


# --------------------------------------------------------------------------- #
# F7 — scope safety
# --------------------------------------------------------------------------- #


def test_unresolved_project_refuses_to_send_and_spools(tmp_path):
    """F7: a bad scope must not scatter memories; it must spool and disclose."""
    sink = build_sink(
        tmp_path, tmp_path, base_url="http://127.0.0.1:9", project="local"
    )
    assert sink.config_error
    receipt = sink.emit("memory_action", title="t", content="a durable fact")
    assert receipt.status == "spooled"
    assert sink.pending_count() == 1


# --------------------------------------------------------------------------- #
# the four adapters
# --------------------------------------------------------------------------- #


def test_continuation_narrative_is_one_per_task(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_continuation_narrative({"text": "## Summary\n\nwhat happened"}, task_id="T9")
    body = _saved(state)[-1]["body"]
    assert body["topic_key"] == "continuation:T9"      # one narrative per task
    assert body["type"] == "episodic_memory"


def test_evolution_checkpoint_carries_objective_and_outcome(stub, tmp_path):
    """AC22: a verdict without what it tried to do is not a usable memory."""
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_evolution_checkpoint(
        {
            "task_id": "E1",
            "campaign_objective": "make context assembly cheaper",
            "cycle_outcome": "abandoned",
            "abandoned_reason": "regressed prefix reuse",
            "commit_sha": "abc123",
            "outcome_axes": {"execution": {"status": "failed"}},
        }
    )
    body = _saved(state)[-1]["body"]
    assert "make context assembly cheaper" in body["content"]
    assert "abandoned" in body["content"]
    assert "regressed prefix reuse" in body["content"]
    assert body["topic_key"] == "evolution_cycle:E1"


def test_evolution_outcome_upserts_the_task_done_row(stub, tmp_path):
    """AC21/C19: both phases share one identity, so the second is an upsert."""
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_evolution_checkpoint(
        {"task_id": "E2", "campaign_objective": "o", "cycle_outcome": "waiting_for_restart"}
    )
    sink.emit_evolution_checkpoint(
        {"task_id": "E2", "campaign_objective": "o", "cycle_outcome": "absorbed"}
    )
    keys = [r["body"]["topic_key"] for r in _saved(state)]
    assert keys == ["evolution_cycle:E2", "evolution_cycle:E2"]
    assert len(set(keys)) == 1                       # same memory, updated — not two


def test_review_verdict_forwards_verdict_and_drops_obligations(stub, tmp_path):
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_review_verdict(
        {"verdict": "FAIL", "reason": "missing receipt", "open_obligations": ["do X"]}, task_id="T3"
    )
    body = _saved(state)[-1]["body"]
    assert "FAIL" in body["content"]
    assert "open_obligations" not in json.dumps(body)


def test_same_identity_and_content_is_emitted_once(stub, tmp_path):
    """F2: an exact repeat is a duplicate, not a second memory."""
    state, url = stub
    sink = _sink(tmp_path, url)
    action = {"type": "knowledge_write", "topic": "auth-model", "content": "tokens rotate"}
    first = sink.emit_memory_action(action, task_id="t")
    second = sink.emit_memory_action(action, task_id="t")
    assert first.status == "sent"
    assert second.status == "duplicate"
    assert len(_saved(state)) == 1


def test_same_identity_with_new_content_upserts(stub, tmp_path):
    """C19/AC4: an evolving topic updates one memory instead of piling up."""
    state, url = stub
    sink = _sink(tmp_path, url)
    sink.emit_memory_action(
        {"type": "knowledge_write", "topic": "auth-model", "content": "tokens rotate"}, task_id="t"
    )
    sink.emit_memory_action(
        {"type": "knowledge_write", "topic": "auth-model", "content": "tokens rotate atomically"},
        task_id="t",
    )
    keys = [r["body"]["topic_key"] for r in _saved(state)]
    assert keys == ["auth-model", "auth-model"]      # same memory, updated
    assert len(set(keys)) == 1


# --------------------------------------------------------------------------- #
# AC20 — the whole payload, across every write source, carries no work item
# --------------------------------------------------------------------------- #

#: Phrases that mark a payload as carrying "what to fix" rather than a memory.
#: Deliberately broader than the sink's own gate: the gate is a LEAD check (it
#: only refuses content whose *job* is to instruct), while this scan is a
#: whole-payload audit of what actually reached the store.
_ACTION_PHRASES = (
    "proposed_next_step",
    "open_obligation",
    "next steps",
    "next_step",
    "todo",
    "to-do",
    "action item",
    "follow-up",
    "下一步",
    "待办",
    "待处理",
    "需要修",
    "要修复",
)


def test_no_write_source_lets_a_work_item_into_the_payload(stub, tmp_path):
    """AC20: scan EVERY adapter's payload for work-item content, not just the gate.

    Each call is fed a realistic payload that *contains* a work item alongside a
    genuine memory — the hard case, and the one the local stores actually produce
    (a `memory_action` carries `proposed_next_step`; a review verdict carries
    `open_obligations`). The memory must land; the work item must not ride along.
    """
    state, url = stub
    sink = _sink(tmp_path, url)

    sink.emit_memory_action(
        {"type": "knowledge_write", "topic": "auth",
         "content": "tokens rotate atomically",
         "proposed_next_step": "下一步：把 token 轮换文档补上"},
        task_id="t1",
    )
    sink.emit_continuation_narrative(
        {"text": "Summary for Ouroboros Episodic Memory: shipped the rotation gate.",
         "proposed_next_step": "TODO: write the release note"},
        task_id="t1",
    )
    sink.emit_evolution_checkpoint(
        {"kind": "cycle_outcome", "task_id": "t1", "campaign_objective": "cut prompt bloat",
         "cycle_outcome": "absorbed", "proposed_next_step": "next: measure again",
         "open_obligations": ["verify the digest"]},
    )
    sink.emit_review_verdict(
        {"verdict": "PASS", "summary": "gate green",
         "open_obligations": ["待办：补一个回归测试"],
         "proposed_next_step": "rerun the reviewer lane"},
        task_id="t1",
    )

    payloads = _saved(state)
    assert len(payloads) == 4, payloads
    for record in payloads:
        blob = json.dumps(record["body"], ensure_ascii=False).lower()
        for phrase in _ACTION_PHRASES:
            assert phrase.lower() not in blob, (phrase, record["body"])


def test_the_store_never_receives_a_backlog_candidate(stub, tmp_path):
    """KEPT-1: `backlog_candidates` is a work queue and has no Engram path at all.

    The strongest form of the guarantee is structural: there is no sink kind it
    could be sent under, so no caller can accidentally route it here.
    """
    from ouroboros.engram_sink import SINK_KINDS

    state, url = stub
    sink = _sink(tmp_path, url)

    for kind in ("backlog", "backlog_candidate", "improvement_backlog", "work_item"):
        receipt = sink.emit(kind, title="backlog", content="ship the fix")
        assert receipt.status == "rejected" and receipt.reason == "unknown_kind"
    assert "backlog" not in " ".join(SINK_KINDS)
    assert _saved(state) == []


def test_the_positive_half_the_local_backlog_keeps_growing(stub, tmp_path):
    """AC20's other half: work items are not merely refused here, they still LAND.

    Refusing a to-do from Engram is only correct if the local queue keeps taking
    it. Otherwise the fix would have silently deleted the agent's action list.
    """
    from ouroboros.improvement_backlog import backlog_path, format_backlog_digest, merge_backlog_text

    state, url = stub
    drive = tmp_path / "drive"
    (drive / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    block = (
        "### ibl-engram-1\n"
        "- status: open\n"
        "- created_at: 2026-01-01T00:00:00+00:00\n"
        "- summary: Harden the receipt check\n"
        "- proposed_next_step: add a regression test\n"
    )

    assert merge_backlog_text(drive, block) >= 0

    assert backlog_path(drive).exists(), "the local action queue is still the home of work items"
    assert "Harden the receipt check" in format_backlog_digest(drive)
    assert _saved(state) == [], "and none of it went to Engram"


def test_the_memory_action_adapter_selects_its_fields_explicitly(stub, tmp_path):
    """W1's real protection is structural, not the `_TODO_FIELDS` backstop.

    ``_memory_action`` builds ``fields`` from an allow-list rather than forwarding
    the action dict, so a new work-item key on a reflection action cannot reach the
    store by default. Verified by mutation: emptying ``_TODO_FIELDS`` does NOT leak
    ``proposed_next_step`` here, because the adapter never passed it down. This
    test pins that structural property so a refactor to ``fields=action`` is caught.
    """
    state, url = stub
    sink = _sink(tmp_path, url)

    sink.emit_memory_action(
        {"type": "knowledge_write", "topic": "auth", "content": "tokens rotate atomically",
         "proposed_next_step": "next: document it", "open_obligations": ["x"],
         "requester": "owner", "urgency": "high", "deadline": "tomorrow"},
        task_id="t1",
    )

    spooled = [
        line
        for line in sink.spool_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and '"op": "add"' in line
    ]
    assert len(spooled) == 1
    record = json.loads(spooled[0])
    assert set(record["fields"]) <= {"task_id"}, record["fields"]


def test_only_wire_visible_fields_are_transmitted(stub, tmp_path):
    """Pin what Engram actually receives — `fields` never leave the process.

    ``EngramClient.save`` sends session_id / type / title / content / scope /
    project / topic_key, and Engram's schema has no generic metadata column. So the
    spool's ``fields`` serve the local C18 gate and the local audit trail ONLY.
    A reader who assumed otherwise would look for ``task_id`` in a remote column
    that does not exist; it is recoverable from ``topic_key`` instead. This test
    exists so that assumption cannot drift unnoticed.
    """
    state, url = stub
    sink = _sink(tmp_path, url)

    sink.emit_evolution_checkpoint(
        {"kind": "cycle_outcome", "task_id": "T-join", "campaign_id": "C-1",
         "campaign_objective": "obj", "cycle_outcome": "absorbed"}
    )

    body = _saved(state)[-1]["body"]
    assert set(body) == {
        "session_id", "type", "title", "content", "tool_name", "project", "scope", "topic_key",
    }, sorted(body)
    # The join key survives as the identity, which IS transmitted.
    assert body["topic_key"] == "evolution_cycle:T-join"


# --------------------------------------------------------------------------- #
# AC19(e) — the spool is append-only and crash-safe
# --------------------------------------------------------------------------- #


def _spool_by_failing_the_store(state, sink, count: int, *, prefix: str = "fact") -> None:
    """Fill the spool the way an outage does: every send fails, every add stays."""
    state.fail = True
    try:
        for index in range(count):
            sink.emit("memory_action", title=f"{prefix} {index}", content=f"durable fact {index}")
    finally:
        state.fail = False
    assert sink.pending_count() == count


def test_a_crash_after_a_successful_send_does_not_lose_the_record(stub, tmp_path):
    """Kill the process between the send and the ack: the record must survive.

    Write-then-forward has to be at-least-once across a crash, so the ack is what
    marks a record done. Losing the ack may cost a retry; it must never cost the
    record.
    """
    state, url = stub
    sink = _sink(tmp_path, url)
    _spool_by_failing_the_store(state, sink, 1)
    record = sink.pending()[0]
    state.requests.clear()  # ignore the outage attempts; count only the recovery

    # The store is back and the send succeeds...
    assert sink._send(record).ok
    # ...but the ack never landed (crash, power loss, unwritable spool).
    assert sink.pending_count() == 1

    # A later run re-forwards it. Engram upserts on topic_key, so the retry cannot
    # duplicate the memory — the idempotency half of AC19(c).
    assert sink.flush() == 1
    assert sink.pending_count() == 0
    keys = [r["body"]["topic_key"] for r in _saved(state)]
    assert len(keys) == 2  # the recovery send plus the retry...
    assert len(set(keys)) == 1  # ...both addressed to ONE identity


def test_a_crash_mid_flush_keeps_every_unacked_record(stub, tmp_path):
    """Forwarding some records then dying must leave exactly the unacked remainder."""
    from ouroboros.engram_sink import SPOOL_SCHEMA_VERSION, _spool_append

    state, url = stub
    sink = _sink(tmp_path, url)
    _spool_by_failing_the_store(state, sink, 3)

    first = sink.pending()[0]
    assert sink._send(first).ok
    _spool_append(
        sink.spool_path,
        {"v": SPOOL_SCHEMA_VERSION, "op": "ack", "spool_id": first["spool_id"], "ts": "now"},
    )

    remaining = sink.pending()
    assert len(remaining) == 2
    assert first["spool_id"] not in {rec["spool_id"] for rec in remaining}
    # Nothing lost: the other two are still there and still forwardable.
    assert sink.flush() == 2
    assert sink.pending_count() == 0


def test_the_spool_file_is_only_ever_appended_to(stub, tmp_path):
    """Append-only is what makes a torn tail recoverable instead of fatal."""
    state, url = stub
    sink = _sink(tmp_path, url)
    _spool_by_failing_the_store(state, sink, 3)

    lengths = [len(sink.spool_path.read_text(encoding="utf-8").splitlines())]
    for _ in range(3):
        assert sink.flush(limit=1) == 1
        lengths.append(len(sink.spool_path.read_text(encoding="utf-8").splitlines()))

    assert lengths == sorted(lengths), f"the spool was rewritten, not appended: {lengths}"
    assert lengths[0] == 3 and lengths[-1] == 6  # three adds, then three acks


def test_a_torn_spool_tail_is_skipped_not_fatal(stub, tmp_path):
    """A half-written last line must not make the whole spool unreadable."""
    state, url = stub
    sink = _sink(tmp_path, url)
    _spool_by_failing_the_store(state, sink, 1)
    good = sink.pending()
    assert len(good) == 1

    with open(sink.spool_path, "a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "op": "add", "spool_id": "torn", "tit')  # no newline, truncated

    recovered = sink.pending()
    assert [rec["spool_id"] for rec in recovered] == [good[0]["spool_id"]]
    assert sink.flush() == 1


def test_an_identity_candidate_is_stored_as_a_proposal_not_a_fact(stub, tmp_path):
    """W1: retrieval must not surface a proposed trait as one the agent has.

    Identity is the one memory that must not drift autonomously — the reflection
    path routes refinements through a review candidate precisely so nothing is
    auto-applied. Engram is read back AS memory, so an unmarked candidate would
    defeat that at the one place a future reader actually looks.
    """
    from ouroboros.engram_sink import IDENTITY_CANDIDATE_MARKER

    state, url = stub
    sink = _sink(tmp_path, url)

    sink.emit_memory_action(
        {"type": "identity_update_candidate", "content": "I am rigorous and always verify."},
        task_id="t1",
    )

    body = _saved(state)[-1]["body"]
    assert body["type"] == "identity_update_candidate"
    assert body["content"].startswith(IDENTITY_CANDIDATE_MARKER)
    assert "I am rigorous and always verify." in body["content"]
    assert "candidate" in body["title"].lower()
    # Not mistaken for a to-do either: it is a memory, just an unadopted one.
    assert body["content"].startswith("IDENTITY")
