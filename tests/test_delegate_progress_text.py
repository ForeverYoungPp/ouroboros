"""Typed Claudexor text fragments must not become punctuated event labels."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ouroboros.delegate_progress import (
    WindowObservations, emit, live_line, timeline_tail, window_payload,
)


def text_event(text, *, kind="thinking", delta=True, attempt="a01", harness="cursor"):
    return {
        "type": "harness.event", "title": text[:500], "detail": text,
        "severity": "info", "textKind": kind, "textDelta": delta,
        "attemptId": attempt, "harnessId": harness,
    }


def render(*events):
    advance = WindowObservations().record({"timeline": list(events)}, 18, 3)
    return live_line("run-1", advance)


@pytest.mark.parametrize("kind", ["thinking", "message"])
def test_native_fragments_preserve_words_spaces_and_authored_punctuation(kind):
    fragments = ["UX and interaction", " ", "plan for an auto", "nomous", " chess widget.",
                 "\n\n", "A · B", "  ", "next\tstep"]
    assert render(*(text_event(text, kind=kind) for text in fragments)) == (
        "🛰 delegated run run-1 @seq 18: " + "".join(fragments)
    )


def test_text_body_wins_over_the_engine_title_preview():
    event = text_event("  phrase\n\nnext paragraph ")
    event["title"] = "phrase next paragraph"
    original = deepcopy(event)
    assert render(event, text_event("continues")) == (
        "🛰 delegated run run-1 @seq 18:   phrase\n\nnext paragraph continues"
    )
    assert event == original


@pytest.mark.parametrize("delta", [False, None, "true", 1])
def test_only_a_boolean_delta_fact_authorizes_concatenation(delta):
    assert render(text_event("first", delta=delta), text_event("second", delta=delta)).endswith(
        "first · second"
    )


@pytest.mark.parametrize("boundary", [
    {"type": "harness.event", "title": "read", "severity": "info"},
    {"type": "harness.started", "title": "started", "severity": "info"},
    text_event("complete message", kind="message", delta=False),
    text_event("complete thought", delta=False),
])
def test_complete_messages_tools_and_statuses_preserve_event_boundaries(boundary):
    assert render(text_event("before"), boundary, text_event("after")).endswith(
        f"before · {boundary['title']} · after"
    )


@pytest.mark.parametrize("changes", [
    {"kind": "message"}, {"attempt": "a02"}, {"harness": "claude"},
])
def test_distinct_text_streams_are_never_concatenated(changes):
    assert render(text_event("first"), text_event("second", **changes)).endswith("first · second")


@pytest.mark.parametrize("metadata", [
    {}, {"textKind": None, "textDelta": False},
    {"textKind": "future_kind", "textDelta": True},
    {"textKind": "thinking", "textDelta": True, "detail": None},
])
def test_legacy_or_unknown_metadata_keeps_event_labels(metadata):
    rows = [{"type": "harness.event", "title": word, **metadata} for word in ["UX", "plan"]]
    assert render(*rows).endswith("UX · plan")
    assert timeline_tail({"timeline": rows}) == [
        {"type": "harness.event", "title": word, "severity": None} for word in ["UX", "plan"]
    ]


def test_missing_delta_flag_is_a_complete_text_event():
    event = text_event("one")
    event.pop("textDelta")
    assert render(event, text_event("two")).endswith("one · two")


def test_empty_text_breaks_a_stream_only_when_it_is_a_complete_event():
    assert render(text_event("a"), text_event(""), text_event("b")).endswith("ab")
    assert render(text_event("a"), text_event("", delta=False), text_event("b")).endswith("a · b")
    assert render(text_event(""), text_event("b")).endswith(": b")
    assert render({"title": "read"}, text_event(""), text_event("b")).endswith("read · b")


def test_poll_advances_remain_independent_and_emit_immediately():
    seen = WindowObservations()
    baseline = text_event("already shown ")
    seen.observe_baseline({"lastSeq": 5, "timeline": [baseline]}, 5)
    rows = [baseline, text_event("UX and interaction "), text_event("plan")]
    first = seen.record({"timeline": rows}, 18, 3)
    second = seen.record({"timeline": [*rows, text_event("more "), text_event("text")]}, 30, 6)
    output = []
    ctx = SimpleNamespace(emit_progress_fn=output.append)
    emit(ctx, "run-1", first)
    emit(ctx, "run-1", second)
    assert output == [
        "🛰 delegated run run-1 @seq 18: UX and interaction plan",
        "🛰 delegated run run-1 @seq 30: more text",
    ]
    assert len(first.events) == len(second.events) == 2
    assert [row["seq"] for row in seen.rows(10000)] == [18, 30]


def test_large_batch_keeps_the_existing_omission_count():
    events = [text_event(f"part{i} ") for i in range(16)]
    seen = WindowObservations()
    advance = seen.record({"timeline": events}, 38, 3)
    assert advance.events_omitted == 4
    assert len(advance.events) == 12
    assert live_line("run-1", advance) == (
        "🛰 delegated run run-1 @seq 38: " + "".join(event["detail"] for event in events[4:])
        + " (+4 earlier in this batch, on the run's timeline)"
    )
    row, = seen.rows(400)
    assert row["events"] == [] and row["events_omitted"] == 16


def test_verbose_text_remains_disclosed_and_the_payload_fits():
    events = [text_event("x" * 20000, kind="message", delta=False) for _ in range(16)]
    seen = WindowObservations()
    advance = seen.record({"timeline": events}, 38, 3)
    assert all("original length 20000" in row["title"] for row in advance.events)
    assert all("detail" not in row for row in advance.events)
    payload = window_payload(
        run_id="run-1", state="running", last_seq=38, window=120,
        elapsed_seconds=120, max_seconds=1800, waiting_on_user=False,
        detail={"timeline": events}, seen=seen, budget=15000,
    )
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(raw) <= 15000
    assert json.loads(raw) == payload
    assert "OMISSION NOTE" in live_line("run-1", advance)


def test_no_new_rows_keeps_the_existing_progress_fallback():
    assert render() == "🛰 delegated run run-1 @seq 18: (new session events)"
