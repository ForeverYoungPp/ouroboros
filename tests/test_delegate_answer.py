"""Poltergeist phase B (B4): the nanny's question surfaces.

The question READERS survived the Claudexor retirement; the delivery path they
fed did not. Both verbs that spoke to the engine — ``delegate_wait`` (the run's
journal/question stream) and ``delegate_answer`` (the custody-gated answer POST)
— rode the retired gateway, so each now reports the retirement as a typed
refusal: no run state is invented, and no answer is pretended delivered.

What stays worth pinning is what outlived the transport: the gateway readers that
normalize the question shape, the budget-bounded question projection that rides
the expiry payload and its notes, and the STRICT argument/custody gates inside
``delegate_answer`` — those still refuse exactly as before, one step before the
retirement is reached.
"""

import json
import queue as stdqueue

import pytest


def _pending_row(iid="int-1", question="Which port should the server use?"):
    return {
        "interactionId": iid,
        "runId": "run-1",
        "attemptId": "a01",
        "harnessId": "claude",
        "sourceTool": "AskUserQuestion",
        "questions": [{
            "id": "q1",
            "question": question,
            "header": "Port",
            "options": [{"label": "8080", "description": "the default"},
                        {"label": "9090", "description": None}],
            "multi_select": False,
        }],
        "requestedAt": "2026-08-11T10:00:00Z",
        "timeoutAt": "2026-08-11T10:15:00Z",
    }


# -- the gateway readers --------------------------------------------------------


def test_pending_interactions_normalizes_the_full_question_shape():
    from ouroboros.gateways.claudexor import pending_interactions

    rows = pending_interactions({"pendingInteractions": [
        _pending_row(),
        {"interactionId": "", "questions": []},   # unanswerable: dropped
        "junk",
    ]})
    assert len(rows) == 1
    row = rows[0]
    assert row["interaction_id"] == "int-1"
    assert row["source_tool"] == "AskUserQuestion"
    assert row["timeout_at"] == "2026-08-11T10:15:00Z"
    q = row["questions"][0]
    assert q["question_id"] == "q1"
    assert q["question"] == "Which port should the server use?"
    assert q["options"][0] == {"label": "8080", "description": "the default"}
    assert q["multi_select"] is False


def test_answer_interaction_returns_typed_statuses_at_any_http_code(monkeypatch):
    import httpx

    from ouroboros.gateways import claudexor as cx

    replies = {}

    class _Recorder:
        def request(self, method, path, **kwargs):
            replies["path"] = path
            replies["json"] = kwargs.get("json")
            return httpx.Response(replies["code"], json=replies["body"])

    gateway = cx.ClaudexorGateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret"))
    gateway.close()
    gateway._client = _Recorder()

    replies.update(code=200, body={"accepted": True, "status": "delivered"})
    body = gateway.answer_interaction("run-1", "int-1", [
        {"questionId": "q1", "selectedLabels": ["8080"], "freeText": None}])
    assert body["status"] == "delivered"
    assert replies["path"] == "/v2/runs/run-1/interactions/int-1/answer"
    assert replies["json"] == {"answers": [
        {"questionId": "q1", "selectedLabels": ["8080"], "freeText": None}]}

    # A 409 with a typed body is an ANSWER, not an outage.
    replies.update(code=409, body={"accepted": False, "status": "already_resolved",
                                   "message": "resolved earlier"})
    assert gateway.answer_interaction("run-1", "int-1", [])["status"] == "already_resolved"

    # A bodyless 404 ("no such run") stays the typed refusal it is.
    replies.update(code=404, body={"error": "no such run"})
    with pytest.raises(cx.ClaudexorUnavailable) as exc:
        gateway.answer_interaction("run-gone", "int-1", [])
    assert exc.value.status_code == 404

    # 501: this engine build has no answer service.
    replies.update(code=501, body={"error": "interaction answers are not supported"})
    with pytest.raises(cx.ClaudexorUnavailable) as exc:
        gateway.answer_interaction("run-1", "int-1", [])
    assert exc.value.status_code == 501


# -- delegate_wait: the retired transport ---------------------------------------


def _wait_ctx(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path,
                      task_constraint=TaskConstraint(mode="local_readonly_subagent"))
    ctx.task_id = "t-nanny"
    ctx.event_queue = stdqueue.Queue()
    return ctx


def _own_run(delegate):
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="t-nanny", route_id="some-route", model="m",
        project_id="prj", project_owned=False,
    )


def test_the_wait_verb_reports_the_retired_transport(tmp_path):
    """The engine poll retired with its gateway, so delegate_wait can no longer
    read a run — and it says that instead of reporting a state. A quiet wait
    would read as a run that finished and said nothing."""
    import ouroboros.tools.delegate as delegate

    ctx = _wait_ctx(tmp_path)
    out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=600, since_seq=5))
    assert out["status"] == "refused"
    assert out["tool"] == "delegate_wait"
    assert out["reason"] == "claudexor_retired"
    assert out["run_id"] == "run-1"


def _answer_ctx(tmp_path):
    return _wait_ctx(tmp_path)


def _answer_stub(monkeypatch, *, result=None, error=None, detail_pending=()):
    from ouroboros.gateways import claudexor as gw

    class _Stub:
        engine_version = "3.3.6"

        def handshake(self, **_kw): return {}
        def get_run(self, rid, *, timeout_sec=None):
            return {"lastSeq": 5, "pendingInteractions": list(detail_pending),
                    "summary": {"state": "running"}}
        def answer_interaction(self, rid, iid, answers):
            if error is not None:
                raise error
            return dict(result)
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())


def test_answers_are_validated_and_custody_gated(tmp_path, monkeypatch):
    import ouroboros.tools.delegate as delegate

    _answer_stub(monkeypatch, result={"accepted": True, "status": "delivered"})
    ctx = _answer_ctx(tmp_path)
    _own_run(delegate)

    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", []))
    assert out["status"] == "refused" and out["reason"] == "answers_required"

    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [{"free_text": "x"}]))
    assert out["status"] == "refused" and out["reason"] == "answer_row_invalid"

    # Another task's run: custody refuses before any daemon call.
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="someone-else", route_id="r", model="m",
        project_id="prj", project_owned=False,
    )
    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [
        {"question_id": "q1", "free_text": "x"},
    ]))
    delegate._CUSTODY.clear()
    assert out["status"] == "refused" and out["reason"] == "run_not_owned"


def test_a_valid_owned_answer_reports_the_retired_transport(tmp_path, monkeypatch):
    """Past every gate — owned run, well-formed rows — the answer is reported
    UNAVAILABLE: the gateway that carried the POST retired. Never a pretended
    delivery, and no durable 'answered' row for an answer that never left this
    machine (the engine never resolved anything)."""
    import ouroboros.tools.delegate as delegate

    _answer_stub(monkeypatch, result={"accepted": True, "status": "delivered"})
    ctx = _answer_ctx(tmp_path)
    _own_run(delegate)
    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [
        {"question_id": "q1", "selected_labels": ["8080"]},
    ]))
    delegate._CUSTODY.clear()
    assert out["status"] == "unavailable"
    assert out["reason"] == "claudexor_retired"
    assert out["run_id"] == "run-1" and out["interaction_id"] == "int-1"
    events_path = tmp_path / "logs" / "events.jsonl"
    events = ([json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
              if events_path.exists() else [])
    assert [e for e in events if e.get("type") == "delegate_interaction_answered"] == []


def test_answer_rows_are_validated_strictly(tmp_path):
    """F14 (sol #13): string-only labels, non-empty label-or-freeText per row,
    typed refusal on malformed input — no silent coercion that changes intent.
    The POST these rows used to travel on retired with its gateway; the strict
    validation that gated it did not, and it still runs before the retirement."""
    import ouroboros.tools.delegate as delegate

    ctx = _answer_ctx(tmp_path)
    _own_run(delegate)

    # Non-string label: refused (8080 as an int is NOT "8080").
    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [
        {"question_id": "q1", "selected_labels": [8080]},
    ]))
    assert out["status"] == "refused" and out["reason"] == "answer_row_invalid"

    # Non-string free_text: refused.
    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [
        {"question_id": "q1", "free_text": 42},
    ]))
    assert out["status"] == "refused" and out["reason"] == "answer_row_invalid"

    # Empty row (no labels, no text): refused as empty, never as "an answer".
    out = json.loads(delegate._delegate_answer(ctx, "run-1", "int-1", [
        {"question_id": "q1", "selected_labels": [], "free_text": "  "},
    ]))
    delegate._CUSTODY.clear()
    assert out["status"] == "refused" and out["reason"] == "answer_row_empty"


def _expiry_payload(pending_rows, *, advances=0, budget=None):
    from ouroboros.delegate_progress import WindowObservations, window_payload
    from ouroboros.gateways.claudexor import pending_interactions
    from ouroboros.tool_capabilities import tool_result_limit
    from ouroboros.tools.delegate import _bounded_interactions

    seen = WindowObservations()
    timeline = []
    for i in range(advances):
        timeline = timeline + [{"title": f"event {i}", "type": "tool"}]
        seen.record({"timeline": list(timeline)}, i + 1, i)
    pending = pending_interactions({"pendingInteractions": pending_rows})
    return window_payload(
        run_id="run-1", state="running", last_seq=max(5, advances),
        window=600, elapsed_seconds=600, max_seconds=1800,
        waiting_on_user=bool(pending), detail={"timeline": timeline}, seen=seen,
        pending_interactions=_bounded_interactions(pending) if pending else None,
        budget=budget if budget is not None else tool_result_limit("delegate_wait"))


def _giant_header_row(iid="int-1", header_chars=50_000):
    row = _pending_row(iid=iid)
    row["questions"][0]["header"] = "H" * header_chars
    return row


@pytest.mark.parametrize("n_rows", [2, 3])
def test_expiry_payload_with_giant_headers_fits_and_parses(n_rows):
    """F2 (two fable lenses + sol #4; probes 24 459 / 51 719 chars vs 15 000):
    harness-authored scalars beyond question/options — a 50k header — pushed the
    'bounded' expiry projection past the tool budget, where the EXTERNAL
    truncator severed the JSON mid-structure. Both branches (no_progress and
    progress) must ship a payload that fits whole and round-trips."""
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    rows = [_giant_header_row(iid=f"int-{i}") for i in range(n_rows)]
    limit = tool_result_limit("delegate_wait")

    # The early no_progress branch — the one that used to skip measurement.
    payload = _expiry_payload(rows, advances=0)
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw) <= limit, len(raw)
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    assert json.loads(raw) == payload
    assert payload["status"] == "no_progress"
    shown = len(payload.get("pending_interactions") or [])
    assert shown + int(payload.get("interactions_omitted") or 0) == n_rows

    # The progress branch, same rows plus a real advance sequence.
    payload = _expiry_payload(rows, advances=6)
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw) <= limit, len(raw)
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    assert json.loads(raw) == payload
    assert payload["status"] == "progress"
    assert payload["advances"], "the advance sequence survived beside the questions"
    shown = len(payload.get("pending_interactions") or [])
    assert shown + int(payload.get("interactions_omitted") or 0) == n_rows


def test_bounded_interactions_bounds_every_harness_authored_scalar():
    """F2: header, source tool and timestamps are harness-authored too. The
    ANSWER KEYS ride whole (R2-8) — they are echoed into delegate_answer, so a
    cut key is an id the engine never issued."""
    from ouroboros.gateways.claudexor import pending_interactions
    from ouroboros.tools.delegate import _bounded_interactions

    row = _pending_row()
    row["questions"][0]["header"] = "H" * 50_000
    row["sourceTool"] = "S" * 9_000
    row["requestedAt"] = "T" * 9_000
    row["timeoutAt"] = "T" * 9_000
    row["interactionId"] = "i" * 9_000
    row["questions"][0]["id"] = "q" * 9_000
    bounded = _bounded_interactions(pending_interactions({"pendingInteractions": [row]}))
    out = bounded[0]
    assert len(out["questions"][0]["header"]) <= 200
    assert len(out["source_tool"]) <= 200
    assert len(out["requested_at"]) <= 200
    assert len(out["timeout_at"]) <= 200
    assert out["interaction_id"] == "i" * 9_000  # a KEY, never truncated
    assert out["questions"][0]["question_id"] == "q" * 9_000
    assert "OMISSION NOTE" in out["questions"][0]["header"]


def test_even_one_unfittable_row_yields_to_the_counted_marker():
    """F2: when even a single bounded row cannot fit, the rows yield entirely —
    a counted omission plus the recovery pointer, never an oversized payload."""
    payload = _expiry_payload([_giant_header_row()], advances=0, budget=700)
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw) <= 700 + 400, len(raw)  # marker itself is small and bounded
    assert "pending_interactions" not in payload
    assert payload["interactions_omitted"] == 1
    assert "waiting_on_user" in payload["interactions_note"] or \
        "PAUSED" in payload["interactions_note"]


def test_immediate_branch_drops_even_the_last_unfittable_row(tmp_path, monkeypatch):
    """R2-4 (delta, proven 15 210 > 15 000): the IMMEDIATE waiting_on_user
    branch's shed loop used to return the last bounded row even when it was
    still over budget after the advances yielded — the external truncator then
    severed the JSON mid-structure. The last row is now DROPPED too: what ships
    is the counted omission plus the artifact pointer, it passes the REAL
    truncator untouched, and it round-trips."""
    import ouroboros.delegate_interactions as interactions
    from ouroboros.gateways.claudexor import pending_interactions
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    # One max-shape row whose BOUNDED projection alone exceeds the budget:
    # three shown questions, each with a whole-riding 2 500-char question_id
    # (R2-8 keys are never cut), maxed question/header text and 12 options.
    row = _pending_row()
    row["questions"] = [{
        "id": f"q{i}-" + "K" * 2_500,
        "question": "Q" * 5_000,
        "header": "H" * 5_000,
        "options": [{"label": "L" * 400, "description": None} for _ in range(12)],
        "multi_select": False,
    } for i in range(3)]
    pending = pending_interactions({"pendingInteractions": [row]})
    ctx = _wait_ctx(tmp_path)
    raw = interactions._waiting_on_user_payload(ctx, "run-1", "running", 5, pending)
    limit = tool_result_limit("delegate_wait")
    assert len(raw) <= limit, len(raw)
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    out = json.loads(raw)
    assert out["status"] == "waiting_on_user"
    assert out["pending_interactions"] == []
    assert out["interactions_omitted"] == 1
    assert "read the staged artifact" in out["interactions_note"]
    # The full set is recoverable: staged whole with its receipt.
    artifact = out["interactions_delivery"]["artifact"]
    assert artifact and artifact["sha256"]
    staged = json.loads(open(artifact["abs_path"], encoding="utf-8").read())
    assert len(staged["pending_interactions"]) == 1


def test_waiting_notes_key_the_expiry_claim_on_timeout_at(tmp_path):
    """R2-7e: a null timeout_at means NO automatic expiry — every waiting note
    then says the run waits until answered instead of promising a benign
    decline that never comes; rows that DO carry timeout_at keep the
    benign-decline claim."""
    import ouroboros.delegate_interactions as interactions
    from ouroboros.gateways.claudexor import pending_interactions

    # Immediate payload, rows WITH timeout_at: the benign-decline claim stands.
    ctx = _wait_ctx(tmp_path)
    with_timeout = pending_interactions({"pendingInteractions": [_pending_row()]})
    note = json.loads(interactions._waiting_on_user_payload(
        ctx, "run-1", "running", 5, with_timeout))["note"]
    assert "benign-declines" in note

    # Immediate payload, timeout_at null: no expiry is promised.
    row = _pending_row()
    row["timeoutAt"] = ""
    without_timeout = pending_interactions({"pendingInteractions": [row]})
    note = json.loads(interactions._waiting_on_user_payload(
        ctx, "run-1", "running", 5, without_timeout))["note"]
    assert "benign-declines" not in note
    assert "waits until answered" in note

    # Both expiry-branch notes follow the same key.
    paused = _expiry_payload([_pending_row()], advances=0)
    assert "benign-declines" in paused["note"]
    no_expiry_row = _pending_row()
    no_expiry_row["timeoutAt"] = ""
    paused = _expiry_payload([no_expiry_row], advances=0)
    assert "benign-declines" not in paused["note"]
    assert "waits until answered" in paused["note"]
    paused_progress = _expiry_payload([no_expiry_row], advances=4)
    assert "benign-declines" not in paused_progress["note"]
    assert "waits until answered" in paused_progress["note"]
    with_progress = _expiry_payload([_pending_row()], advances=4)
    assert "benign-declines" in with_progress["note"]


def test_the_paused_expiry_note_never_hints_a_cancel(tmp_path):
    """F13 (owner 7=A): a run paused on its own question is not 'stuck' — the
    expiry note says answer / escalate / keep waiting, in BOTH branches, and the
    generic delegate_cancel hint appears only for a genuinely silent run."""
    paused = _expiry_payload([_pending_row()], advances=0)
    assert paused["waiting_on_user"] is True
    assert "delegate_answer" in paused["note"]
    assert "Do not cancel" in paused["note"]
    assert "delegate_cancel if it is stuck" not in paused["note"]

    silent = _expiry_payload([], advances=0)
    assert silent["waiting_on_user"] is False
    assert "delegate_cancel if it is stuck" in silent["note"]

    paused_progress = _expiry_payload([_pending_row()], advances=4)
    assert "do not cancel over it" in paused_progress["note"]

    silent_progress = _expiry_payload([], advances=4)
    assert "do not cancel over it" not in silent_progress["note"]


def test_an_input_required_terminal_names_the_new_start_path():
    from ouroboros.subagents import DelegatedRunShape
    from ouroboros.tools.delegate import _terminal_payload

    detail = {"lastSeq": 9, "summary": {
        "state": "failed",
        "outcomeFacts": {"reason": "input_required",
                         "required_inputs": ["which database?"]},
    }}
    shape = DelegatedRunShape(access="readonly", mode="ask", isolation="", delegated=False)
    payload = _terminal_payload("run-1", detail, shape)
    assert "input_required_note" in payload
    assert "NEW delegate_start" in payload["input_required_note"]
    assert "rerun/decision" in payload["input_required_note"]


# -- the contract surfaces ------------------------------------------------------


def test_the_answer_verb_is_registered_on_every_contract_surface():
    from ouroboros.safety import TOOL_POLICY, POLICY_SKIP
    from ouroboros.tool_capabilities import (
        ACTING_SUBAGENT_TOOL_NAMES,
        LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
    )
    from ouroboros.tools import delegate

    names = {entry.name for entry in delegate.get_tools()}
    assert "delegate_answer" in names
    assert "delegate_answer" in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert "delegate_answer" in ACTING_SUBAGENT_TOOL_NAMES
    assert TOOL_POLICY.get("delegate_answer") == POLICY_SKIP


def test_waiting_note_routes_above_authority_questions_to_escalate():
    """#204 (owner batch 3, decision 31): a harness question above the nanny's
    authority rides the escalation channel — the note names the escalate verb
    and the parent-first hierarchy, never a dead-end progress message."""
    from ouroboros.delegate_interactions import _waiting_on_user_note

    note = _waiting_on_user_note([{"interaction_id": "i1", "timeout_at": None}])
    assert "escalate(question, options, stake, assumption)" in note
    assert "PARENT task" in note
    assert "delegate_answer" in note and "delegate_wait" in note
    assert "progress message" not in note


def test_delegate_schemas_teach_the_escalation_verb():
    """sol finding: the LLM-facing tool descriptions are decision points — they
    must name the escalate verb, never the retired progress-message dead end."""
    from ouroboros.tools.delegate import get_tools

    schemas = {entry.schema["name"]: entry.schema["description"]
               for entry in get_tools()}
    for name in ("delegate_answer", "delegate_wait"):
        assert "escalate" in schemas[name]
        assert "surface it to your human via progress" not in schemas[name]
        assert "escalate to your human" not in schemas[name]


def test_expiry_notes_teach_the_escalation_verb():
    """Delta finding: the REPEAT delegate_wait rides the expiry path, whose
    notes are decision points too — both branches name the escalate verb and
    never the retired progress/your-human dead ends."""
    import inspect

    import ouroboros.delegate_progress as dp

    source = inspect.getsource(dp)
    assert "escalate an above-authority question with the" in source
    assert "raise it with the escalate verb (parent-first)" in source
    assert "escalate to your human via a progress message" not in source
    assert "escalate to your human," not in source
    # The dispatch charter is an LLM-facing user message too (agent.py appends
    # it): the same dead end must not survive there.
    import ouroboros.subagent_dispatch_notes as dn

    charter = inspect.getsource(dn)
    assert "escalated with the escalate verb (parent-first" in charter
    assert "goes to your human via progress" not in charter
    # No delegated-question surface may teach direct-to-human ESCALATION: the
    # hierarchy (parent-first) is the ONLY route (decision 31). Semantic
    # variants of the escalation phrasing, not just the exact retired lines
    # (the live progress STREAM legitimately mentions the human).
    import ouroboros.tools.delegate as dtool

    import ouroboros.delegate_interactions as di

    for module in (dp, dn, di, dtool):
        text = inspect.getsource(module)
        for phrase in ("escalated to its human", "escalated to your human",
                       "escalated a question to its human",
                       "goes to your human", "surface it to your human"):
            assert phrase not in text, (module.__name__, phrase)
