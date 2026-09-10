"""A delegated run's interactive questions, and the nanny's answer to them.

Extracted from ``ouroboros/tools/delegate.py`` when that module crossed its size
gate — the same split as ``delegate_output`` / ``delegate_progress`` /
``delegate_containment``, and one coherent concern: the engine parks a run on an
AskUserQuestion-style interaction (full question text/options on the run detail;
a row carrying ``timeout_at`` benign-declines at the engine timeout, a null one
waits until answered), and the nanny — the task that owns the
run — is the party that answers (owner decision 7=A, poltergeist phase B).
``tools.delegate`` re-exports these names, so every existing reference (and the
tests) still finds them there, and ``_REPORTED_INTERACTIONS`` stays one object.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.delegate_output import _PAYLOAD_ENVELOPE_HEADROOM, _stage_full_output
from ouroboros.delegate_shared import _fail, _owned_run
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import truncate_within_limit

log = logging.getLogger(__name__)

# Which interaction ids each run has ALREADY had returned as an immediate
# `waiting_on_user` payload. Process-local by design: the durable fact is the
# engine's own pending-interaction store, and the only cost of losing this memo
# (worker restart) is one duplicate immediate return. Without it, a nanny that
# deliberately escalated a question up the task hierarchy and re-waited would busy-loop —
# every delegate_wait would return instantly with the same known question.
_REPORTED_INTERACTIONS: Dict[str, frozenset] = {}
_REPORTED_INTERACTIONS_MAX_KEYS = 128

# Inline bounds for the waiting_on_user preview when the full question set spills.
_QUESTION_PREVIEW_CHARS = 600
_OPTION_PREVIEW_CHARS = 200
# EVERY harness-authored DISPLAY scalar is bounded, not just question/options
# (F2): the header, source tool and timestamps are engine/harness-authored
# strings too, and a 50k header pushed a "bounded" projection to 3x the tool
# budget. The one exception is the ANSWER KEYS (R2-8): `interaction_id` and
# `question_id` are echoed verbatim into delegate_answer, so a truncated key
# with an embedded marker yields an engine not_found — keys ride whole, and a
# row whose keys alone overflow the budget is dropped WHOLE with the counter
# instead (the full set is staged to the artifact regardless).
_SCALAR_PREVIEW_CHARS = 200
_MAX_INLINE_QUESTIONS = 3
_MAX_INLINE_OPTIONS = 12
# The compact advances ride-along on the immediate waiting_on_user return (F17):
# enough for the window's journal spine, small enough that the QUESTION stays
# the payload's point.
_WAITING_ADVANCES_BUDGET_CHARS = 2_000


def _interactions_are_news(run_id: str, pending: List[Dict[str, Any]]) -> bool:
    """True when this pending set has not been returned for this run yet — and
    record it. A re-ask after a decline is a NEW interaction id, so it is news."""
    ids = frozenset(str(row.get("interaction_id") or "") for row in pending)
    if _REPORTED_INTERACTIONS.get(run_id) == ids:
        return False
    if run_id not in _REPORTED_INTERACTIONS \
            and len(_REPORTED_INTERACTIONS) >= _REPORTED_INTERACTIONS_MAX_KEYS:
        _REPORTED_INTERACTIONS.pop(next(iter(_REPORTED_INTERACTIONS)))
    _REPORTED_INTERACTIONS[run_id] = ids
    return True


def _preview_scalar(value: Any, limit: int) -> Any:
    """Bound ONE harness-authored scalar for the inline projection; non-strings
    pass through untouched (a ``None`` header stays ``None``). Strict cut
    (marker INSIDE the limit): these are preview fields — the full values ride
    the spilled artifact — so the budget wins over the anti-waste floor."""
    return truncate_within_limit(value, limit) if isinstance(value, str) else value


def _bounded_interactions(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A budget-safe projection of the pending questions, with the cuts COUNTED.

    EVERY harness-authored display scalar is bounded (F2) — question, options,
    header, source tool, timestamps — because each of them crosses the trust
    boundary from the engine/harness: a single 50k header made the "bounded"
    projection 3x the tool budget while every question field obeyed its cap.
    The ANSWER KEYS (`interaction_id`, `question_id`) ride WHOLE (R2-8): they
    are echoed verbatim into delegate_answer, so a 160-char cut with an
    embedded marker produced an id the engine has never issued (not_found).
    An oversized-key row is handled by the shed loop dropping the row whole.
    """
    out: List[Dict[str, Any]] = []
    for row in pending:
        questions = row.get("questions") or []
        shown = [{
            "question_id": str(question.get("question_id") or ""),
            "question": truncate_within_limit(
                str(question.get("question") or ""), _QUESTION_PREVIEW_CHARS),
            "header": _preview_scalar(question.get("header"), _SCALAR_PREVIEW_CHARS),
            "options": [
                {"label": truncate_within_limit(
                    str(option.get("label") or ""), _OPTION_PREVIEW_CHARS)}
                for option in (question.get("options") or [])[:_MAX_INLINE_OPTIONS]
            ],
            "multi_select": bool(question.get("multi_select")),
        } for question in questions[:_MAX_INLINE_QUESTIONS]]
        out.append({
            "interaction_id": str(row.get("interaction_id") or ""),
            "source_tool": _preview_scalar(row.get("source_tool"), _SCALAR_PREVIEW_CHARS),
            "requested_at": _preview_scalar(row.get("requested_at"), _SCALAR_PREVIEW_CHARS),
            "timeout_at": _preview_scalar(row.get("timeout_at"), _SCALAR_PREVIEW_CHARS),
            "questions": shown,
            "questions_omitted": max(0, len(questions) - len(shown)),
        })
    return out


def _waiting_on_user_note(pending: List[Dict[str, Any]]) -> str:
    """The immediate waiting note, its expiry claim keyed on the rows' own
    ``timeout_at`` (R2-7e): a null ``timeout_at`` means NO automatic expiry, so
    promising a benign decline there would invite waiting for a timeout that
    never comes."""
    from ouroboros.delegate_progress import waiting_expiry_clause

    return (
        "The run is PAUSED on the question(s) above and stays paused until "
        f"answered; {waiting_expiry_clause(pending)}. Answer with "
        "delegate_answer(run_id, interaction_id, answers=[{question_id, "
        "selected_labels, free_text}]) — answer from the task context you "
        "already hold. A question ABOVE your authority (spending money, "
        "changing scope, external actions) is not yours to guess: escalate it "
        "with escalate(question, options, stake, assumption) — as a subagent "
        "you escalate to your PARENT task (the owner sees only what no "
        "ancestor answers), the reply reaches your mailbox on a later round, "
        "and you relay it back with delegate_answer — meanwhile keep waiting "
        "with delegate_wait. If the question carries a source-request envelope, answer "
        "with source_response={schema:1, kind:'source_response', complete_sha256, "
        "source, start_char, end_char, text}; the host verifies the exact canonical "
        "range before delivering it. Do not promote a partial preview to complete "
        "authority. Do not cancel a run merely because it asked a question."
    )


def _waiting_on_user_payload(ctx: ToolContext, run_id: str, state: str,
                             last_seq: int, pending: List[Dict[str, Any]],
                             seen: Any = None, source_request: Any = None,
                             source_verification: Any = None) -> str:
    """The IMMEDIATE typed return for a run that is waiting on an answer.

    The full question text rides inline when it fits the tool budget; otherwise
    the WHOLE set spills to the task drive (sha256/size receipt — never the
    generic head-truncation, which severs JSON mid-string) and the inline view is
    a counted bounded preview. The spill file's name is INTERACTION-ADDRESSED
    (``<run>.<sha12>.interactions.json``, F15): a different pending set writes a
    different file, so a receipt handed out for an earlier set keeps describing
    bytes that still exist instead of being silently overwritten.

    ``seen`` is the wait's ``WindowObservations`` (F17): the window's observed
    journal advances ride the immediate return too — compact and bounded — so
    cutting the window short for a question does not lose the run's sequence.
    """
    full: Dict[str, Any] = {
        "status": "waiting_on_user",
        "run_id": run_id,
        "state": state,
        "last_seq": last_seq,
        "pending_interactions": pending,
        "note": _waiting_on_user_note(pending),
    }
    if isinstance(source_request, dict) and source_request:
        full["work_order_source_request"] = dict(source_request)
    if isinstance(source_verification, dict) and source_verification:
        full["work_order_verification"] = dict(source_verification)
    if seen is not None and getattr(seen, "advances", None):
        full["advances"] = seen.rows(_WAITING_ADVANCES_BUDGET_CHARS)
    budget = tool_result_limit("delegate_wait")
    text = json.dumps(full, ensure_ascii=False, indent=2)
    if len(text) <= budget - _PAYLOAD_ENVELOPE_HEADROOM:
        return text
    spill = json.dumps({
        "run_id": run_id,
        "pending_interactions": pending,
        **({"work_order_source_request": source_request}
           if isinstance(source_request, dict) and source_request else {}),
        **({"work_order_verification": source_verification}
           if isinstance(source_verification, dict) and source_verification else {}),
    },
                       ensure_ascii=False, indent=2)
    spill_sha = hashlib.sha256(spill.encode("utf-8", "replace")).hexdigest()[:12]
    artifact = _stage_full_output(ctx, run_id, spill,
                                  suffix=f".{spill_sha}.interactions")
    full["interactions_delivery"] = {
        "complete": False,
        "artifact": artifact,
        "read_next": ({"tool": "read_file", "root": artifact["root"],
                       "path": artifact["path"], "start_line": 1, "max_lines": 2000}
                      if artifact else None),
        "note": (
            "The inline questions are a bounded preview; the artifact holds the FULL "
            "question set, and its sha256/size above is the completeness receipt — "
            "read it to EOF before answering."
            if artifact else
            "PARTIAL AND UNRECOVERABLE INLINE: the full question set could not be "
            "staged to the task drive. Treat the preview as incomplete."
        ),
    }
    # The preview must FIT, not merely be smaller: rows shed from the tail with
    # the cut counted (same discipline as every other bounded delivery here),
    # measured on the rendered payload rather than estimated.
    bounded = _bounded_interactions(pending)
    while True:
        full["pending_interactions"] = bounded
        full["interactions_omitted"] = max(0, len(pending) - len(bounded))
        rendered = json.dumps(full, ensure_ascii=False, indent=2)
        if len(rendered) <= budget - _PAYLOAD_ENVELOPE_HEADROOM:
            return rendered
        if len(bounded) <= 1 and "advances" in full:
            # The question is the point of this payload: the compact advances
            # ride-along yields before the last question row does.
            full.pop("advances", None)
            continue
        if not bounded:
            # Nothing left to shed: the remaining bytes are this module's own
            # envelope plus the artifact receipt, which cannot realistically
            # overflow — hand back what there is.
            return rendered
        bounded = bounded[:-1]
        if not bounded:
            # R2-4 last resort (proven 15 210 > 15 000): when even the single
            # remaining bounded row is over budget after the advances yielded,
            # the row is DROPPED too — the counted omission plus the staged
            # artifact (sha256/size receipt above) is what ships, never a
            # payload the generic truncator would sever mid-structure.
            full["interactions_note"] = (
                "not even one bounded question row fits this payload; the run "
                "is still PAUSED on the full set — read the staged artifact "
                "above to EOF before answering."
            )


# What each typed answer outcome MEANS for the nanny — relayed verbatim so the
# model never re-derives semantics from an enum member.
_ANSWER_NOTES = {
    "delivered": "The answer reached the live session; the run resumes on it. "
                 "Keep watching with delegate_wait.",
    "already_resolved": "The interaction was resolved before this answer arrived — "
                        "the engine timed out into a benign decline, the run ended, "
                        "or an earlier answer landed. The run already continued; do "
                        "NOT re-post this or any other answer for it.",
    "not_found": "No such pending interaction (or run) exists on the daemon. "
                 "Re-read the run with delegate_wait before doing anything else.",
    "rejected": "The engine refused the answer set as invalid for these questions "
                "(see detail). Fix the answer rows — same interaction, corrected "
                "shape — or stop answering (a question with a timeout_at declines "
                "benignly at the engine timeout; one without waits until answered).",
}


def _normalized_answers(
    answers: Any, source_response: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The wire-shaped answer rows, or a typed argument refusal.

    Model-facing snake_case in, engine camelCase out — one translation, here.
    STRICT before the POST (F14): labels must be a list of STRINGS and every row
    must carry a non-empty label set and/or free text. Nothing is coerced — a
    number silently str()-ed into a label, or an empty row posted as "an answer",
    changes the intent the engine acts on, which is worse than a typed refusal
    the model can fix.
    """
    if not isinstance(answers, list) or not answers:
        return None, _fail(
            "delegate_answer", "answers_required",
            "answers must be a non-empty list of {question_id, selected_labels, "
            "free_text} rows — one per question you are answering. To deliberately "
            "NOT answer, simply keep waiting (a question with a timeout_at declines "
            "benignly at the engine timeout; timeout_at=null waits until answered).",
        )
    wire: List[Dict[str, Any]] = []
    source_text = ""
    source_attached = False
    if isinstance(source_response, dict):
        source_text = json.dumps(
            {"schema": 1, "kind": "source_response", **source_response},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    for row in answers:
        if not isinstance(row, dict) or not str(row.get("question_id") or "").strip():
            return None, _fail(
                "delegate_answer", "answer_row_invalid",
                "Every answers row needs the question_id from the waiting_on_user "
                "payload (plus selected_labels and/or free_text).",
            )
        labels = row.get("selected_labels")
        if labels is None:
            labels = []
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            return None, _fail(
                "delegate_answer", "answer_row_invalid",
                "selected_labels must be a list of STRINGS — the option labels "
                "verbatim from the waiting_on_user payload. Nothing is coerced: a "
                "number or object in the list would silently change which option "
                "you picked.",
            )
        free_text = row.get("free_text")
        if free_text is not None and not isinstance(free_text, str):
            return None, _fail(
                "delegate_answer", "answer_row_invalid",
                "free_text must be a string when present — nothing is coerced.",
            )
        if not any(label.strip() for label in labels) and not (free_text or "").strip():
            return None, _fail(
                "delegate_answer", "answer_row_empty",
                "Every answers row needs a non-empty selected_labels and/or "
                "free_text — an empty row is not an answer. To deliberately NOT "
                "answer, keep waiting (a question with a timeout_at declines "
                "benignly at the engine timeout; timeout_at=null waits until "
                "answered).",
            )
        encoded_free_text = free_text if isinstance(free_text, str) and free_text else None
        if source_text and not source_attached:
            encoded_free_text = (
                f"{encoded_free_text}\n\n[SOURCE_RESPONSE]\n{source_text}"
                if encoded_free_text else source_text
            )
            source_attached = True
        wire.append({
            "questionId": str(row.get("question_id")).strip(),
            "selectedLabels": list(labels),
            "freeText": encoded_free_text,
        })
    return wire, ""


def _answer_delivery_unknown(gateway: Any, run_id: str, interaction_id: str,
                             exc: Exception,
                             seconds_left: Optional[float] = None) -> str:
    """The typed outcome for a transport that died mid-answer: re-read, never re-guess.

    An ambiguous failure means the answer MAY have landed. The one forbidden move
    is auto-retrying a DIFFERENT answer — that turns one ambiguous delivery into
    two conflicting intents — so the hint names the same-answer retry or the
    re-read, from what the detail actually shows.

    ``seconds_left`` is the caller's remaining internal time budget (F8/sol #5):
    a non-positive value SKIPS the re-read entirely — a call whose budget is
    spent issues no further wire calls — and otherwise bounds it.

    The re-read itself RETIRED with the Claudexor subsystem: it asked the gateway
    which interactions were still pending, and that reader is gone with the rest of
    the transport. The answer is therefore the EMPTY one — nothing KNOWN to be
    pending — never a guessed ``False``, which would tell the model an ambiguous
    answer had landed. ``gateway`` stays only because every caller already holds
    one; no wire call is issued from here any more, so ``seconds_left`` now decides
    only which honest hint the caller gets. The gap is logged, not swallowed: the
    hint below routes the caller to delegate_wait, the run's own remaining reader.
    """
    still_pending: Optional[bool] = None
    if seconds_left is None or seconds_left > 0:
        log.debug("pending-interaction re-read unavailable for %s/%s: the Claudexor "
                  "gateway retired with the subsystem", run_id, interaction_id)
    if still_pending is True:
        hint = ("the interaction is STILL PENDING, so the answer most likely never "
                "arrived: retry delegate_answer with the SAME answers.")
    elif still_pending is False:
        hint = ("the interaction is NO LONGER PENDING: the answer may have been "
                "delivered, or it resolved another way (timeout, run end). Re-read "
                "the run with delegate_wait; do NOT re-post, and NEVER post a "
                "different answer for this interaction.")
    elif seconds_left is not None and seconds_left <= 0:
        hint = ("this call's own time budget is spent, so the detail was NOT "
                "re-read. Re-check with delegate_wait before anything else, and "
                "NEVER post a different answer for this interaction.")
    else:
        hint = ("the run detail could not be re-read either. Re-check with "
                "delegate_wait before anything else, and NEVER post a different "
                "answer for this interaction.")
    return json.dumps({
        "status": "delivery_unknown",
        "run_id": run_id,
        "interaction_id": interaction_id,
        "still_pending": still_pending,
        "transport_error": f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
        "note": f"The answer POST did not come back typed; {hint}",
    }, ensure_ascii=False, indent=2)


def _delegate_answer(
    ctx: ToolContext, run_id: str, interaction_id: str, answers: Any,
    source_response: Optional[Dict[str, Any]] = None,
) -> str:
    """Answer a delegated run's pending interactive question (B4, owner 7=A).

    Custody-gated like cancel: the bearer token reaches every run, so only the
    task that started a run may speak for its user. The ARGUMENT contract is
    unchanged — a missing run/interaction id and malformed answer rows are still
    refused with the same typed payloads.

    DELIVERY is retired: the answer POST travelled over the Claudexor gateway, and
    ``ouroboros.gateways.claudexor`` goes with the rest of the subsystem, so no
    answer can reach the engine any more. The verb reports that as UNAVAILABLE
    rather than pretending, and the engine outcomes it used to relay (delivered /
    already_resolved / not_found / rejected / subscription_window_exhausted /
    delivery_unknown) retired with the transport that produced them.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return _fail("delegate_answer", "missing_run_id", "run_id is required")
    iid = str(interaction_id or "").strip()
    if not iid:
        return _fail("delegate_answer", "missing_interaction_id",
                     "interaction_id is required (from the waiting_on_user payload)")
    not_mine, entry = _owned_run(ctx, "delegate_answer", rid)
    if not_mine or entry is None:
        return not_mine or _fail("delegate_answer", "run_ownership_unknown",
                                 "custody unresolved", run_id=rid)
    wire, arg_error = _normalized_answers(answers, source_response)
    if arg_error or wire is None:
        return arg_error
    # The answer POST travelled over the Claudexor gateway, and that transport is
    # retired in this build (`ouroboros.gateways.claudexor` is gone with the rest
    # of the subsystem): there is no wire left to carry an answer to the engine.
    # The capability is therefore reported UNAVAILABLE — typed, never a NameError
    # and never a pretend delivery.
    from ouroboros.subagents import CLAUDEXOR_RETIRED

    log.warning("delegate_answer unavailable: the Claudexor transport is retired (%s/%s)",
                rid, iid)
    return json.dumps({
        "status": "unavailable",
        "tool": "delegate_answer",
        "reason": CLAUDEXOR_RETIRED,
        "run_id": rid,
        "interaction_id": iid,
        "detail": ("The Claudexor transport that carried interaction answers is "
                   "retired in this build, so no answer can be posted. The run and "
                   "its question are unaffected: keep watching with delegate_wait, "
                   "or stop the run with delegate_cancel."),
    }, ensure_ascii=False, indent=2)
