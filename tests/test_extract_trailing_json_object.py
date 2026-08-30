"""Contract tests for utils.extract_trailing_json_object.

The helper is the shared SSOT behind the loop's delivery-control parse and the
observability salvage rail: prose with one TRAILING protocol object must split
into (prose, object), while an object followed by prose stays quoted material.
"""

from __future__ import annotations

import json

from ouroboros.utils import extract_trailing_json_object

_PROTOCOL_KEYS = ("delivery_control", "full_answer")


def test_whole_text_object_parses_with_empty_prefix():
    raw = json.dumps({"delivery_control": "keep"})
    prose, parsed, duplicate = extract_trailing_json_object(raw)
    assert prose == ""
    assert parsed == {"delivery_control": "keep"}
    assert duplicate is False


def test_whole_text_object_with_nested_object_still_parses():
    # The last "{" belongs to a nested object; the scan must back up to the
    # real opening brace instead of rejecting the whole text.
    raw = json.dumps({"delivery_control": "replace", "full_answer": "x", "meta": {"a": 1}})
    prose, parsed, _duplicate = extract_trailing_json_object(raw)
    assert prose == ""
    assert parsed is not None and parsed["meta"] == {"a": 1}


def test_prose_plus_trailing_object_splits():
    control = json.dumps({"delivery_control": "replace", "full_answer": "THE REAL ANSWER"})
    prose, parsed, duplicate = extract_trailing_json_object("Here is the summary.\n\n" + control)
    assert prose.rstrip() == "Here is the summary."
    assert parsed == {"delivery_control": "replace", "full_answer": "THE REAL ANSWER"}
    assert duplicate is False


def test_object_with_prose_after_it_is_not_trailing():
    # Quoting the protocol mid-prose must never be mistaken for a directive.
    raw = 'prose {"delivery_control": "keep"} more prose'
    assert extract_trailing_json_object(raw) == (raw, None, False)


def test_duplicate_protocol_key_invalidates_object_but_flags_intent():
    raw = 'prose\n{"delivery_control":"keep","delivery_control":"replace"}'
    prose, parsed, duplicate = extract_trailing_json_object(
        raw, duplicate_flag_keys=_PROTOCOL_KEYS,
    )
    assert prose == "prose\n"
    assert parsed is None
    assert duplicate is True


def test_duplicate_nonflagged_key_invalidates_without_flag():
    raw = '{"a":1,"a":2}'
    prose, parsed, duplicate = extract_trailing_json_object(
        raw, duplicate_flag_keys=_PROTOCOL_KEYS,
    )
    assert prose == ""
    assert parsed is None
    assert duplicate is False


def test_fenced_trailing_object_parses_and_trims_dangling_fence():
    raw = 'prose\n```json\n{"delivery_control":"keep"}\n```'
    prose, parsed, _duplicate = extract_trailing_json_object(raw)
    assert prose == "prose"
    assert parsed == {"delivery_control": "keep"}


def test_fully_fenced_whole_text_object_has_empty_prefix():
    raw = '```json\n{"delivery_control":"keep"}\n```'
    prose, parsed, _duplicate = extract_trailing_json_object(raw)
    assert prose == ""
    assert parsed == {"delivery_control": "keep"}


def test_text_without_object_passes_through():
    assert extract_trailing_json_object("just prose, no json at all") == (
        "just prose, no json at all", None, False,
    )
    assert extract_trailing_json_object("") == ("", None, False)


def test_non_dict_json_is_not_extracted():
    assert extract_trailing_json_object("[1, 2, 3]") == ("[1, 2, 3]", None, False)


def test_brace_inside_string_value_does_not_break_the_scan():
    control = '{"delivery_control":"replace","full_answer":"use {braces} here"}'
    prose, parsed, _duplicate = extract_trailing_json_object("lead\n" + control)
    assert prose == "lead\n"
    assert parsed is not None and parsed["full_answer"] == "use {braces} here"
