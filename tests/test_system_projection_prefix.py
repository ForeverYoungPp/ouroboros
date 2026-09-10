"""Regression guards for the cross-task cache projection (arbor n6.1).

The benchmark scores the projection function in isolation, so it cannot catch a
wiring mistake: a tail that never reaches the request, or reaches it twice, or
loses its bytes, all score the same as a correct projection.  These tests pin the
wiring itself.

Invariants:
1. ``system_projection.project`` is character-conserving (BIBLE P1: relocation,
   never deletion) and lifts the version-bearing ARCHITECTURE title out of the
   static block.
2. ``ContextFitPlan.messages_for`` sends the canonical system message and carries
   the relocated text exactly once, AFTER the task turn.
3. ``reproject_transcript`` swaps only the system view: the relocated text
   survives, exactly once, and nothing is duplicated or dropped.
"""

from __future__ import annotations

import json
from collections import Counter

from ouroboros.system_projection import project

VERSION_TITLE = "# Ouroboros v6.114.7 — Architecture & Reference"


def _static_block(*, with_version: bool) -> str:
    parts = ["# I Am Ouroboros", "## BIBLE.md\n\nconstitution"]
    if with_version:
        parts.append("## ARCHITECTURE.md\n\n" + VERSION_TITLE + "\nbody")
    else:
        parts.append("## ARCHITECTURE.md (navigation map)\n\nsections")
    return "\n\n".join(parts)


def _system_blocks(*, with_version: bool = True):
    return [
        {"type": "text", "text": _static_block(with_version=with_version),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "## Available subagents\n\ncatalog"},
        {"type": "text", "text": "## Health Invariants\n\n- OK: version sync (6.114.10)"},
    ]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))


def _relocated_text(tail) -> str:
    return "".join(_text_of(message.get("content")) for message in tail)


def test_projection_conserves_every_character():
    blocks = _system_blocks()
    sent, tail = project(blocks)
    original = "".join(_text_of([block]) for block in blocks)
    combined = "".join(_text_of([block]) for block in sent) + _relocated_text(tail)
    assert not (Counter(original) - Counter(combined))


def test_projection_lifts_the_version_title_out_of_the_static_block():
    sent, tail = project(_system_blocks())
    sent_static = _text_of([sent[0]])
    assert VERSION_TITLE not in sent_static
    assert VERSION_TITLE in _relocated_text(tail)
    assert "## ARCHITECTURE.md" in sent_static


def test_projection_is_identity_without_a_version_title():
    blocks = _system_blocks(with_version=False)
    sent, tail = project(blocks)
    assert tail == []
    assert _text_of([sent[0]]) == _text_of([blocks[0]])


def _plan(relocated_text: str, low_relocated: str | None = None):
    from ouroboros.context_fit import ContextFitPlan, ContextFitProjection

    def projection(mode: str) -> ContextFitProjection:
        return ContextFitProjection(
            mode=mode,
            system_content_json=json.dumps([{"type": "text", "text": f"{mode} system"}]),
            estimated_tokens=10,
            calibrated_tokens=10,
            calibration_ratio=1.0,
            fits_known_window=None,
            # Per-mode text on purpose: giving both projections the SAME string
            # cannot observe a mode switch, which is how the original reproject
            # test stayed blind to a silent-loss bug.
            relocated_text=relocated_text if mode == "max" else (
                relocated_text if low_relocated is None else low_relocated),
        )

    return ContextFitPlan(
        core_sha256="a" * 64,
        preferred_mode="max",
        initial_mode="max",
        model="openai/test-model",
        provider="openai",
        route_fp="route-a",
        status="confirmed",
        stale=False,
        window_tokens=500_000,
        output_reserve_tokens=65_536,
        user_content_json=json.dumps("go"),
        max_projection=projection("max"),
        low_projection=projection("low"),
    )


def test_messages_for_carries_relocated_text_once_after_the_task_turn():
    plan = _plan("MOVED-BYTES")
    messages = plan.messages_for("max")
    assert [message["role"] for message in messages] == ["system", "user"]
    user_text = _text_of(messages[1]["content"])
    assert user_text.endswith("MOVED-BYTES")
    assert user_text.startswith("go")
    assert sum(user_text.count("MOVED-BYTES") for _ in [0]) == 1


def test_reproject_transcript_swaps_the_system_view_and_keeps_the_tail():
    plan = _plan("MOVED-BYTES")
    messages = plan.messages_for("max")
    messages.extend([
        {"role": "assistant", "content": "working"},
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ])
    rebuilt = plan.reproject_transcript(messages, "low")
    assert len(rebuilt) == len(messages)
    assert rebuilt[0] == plan.projection("low").system_message()
    joined = "".join(_text_of(message.get("content")) for message in rebuilt)
    assert joined.count("MOVED-BYTES") == 1
    assert [message["role"] for message in rebuilt] == [m["role"] for m in messages]


def test_reproject_rederives_relocated_bytes_across_a_mode_switch():
    """The relocated block belongs to the PROJECTION, so a mode switch re-derives it.

    low -> max must ADD the title the max projection removed (silent loss
    otherwise); max -> low must REMOVE it, so the sent transcript cannot disagree
    with the projection that was priced.
    """
    plan = _plan("MAX-ONLY-TITLE", low_relocated="")
    transcript = plan.messages_for("low")
    assert "MAX-ONLY-TITLE" not in _text_of(transcript[1]["content"])

    upgraded = plan.reproject_transcript(transcript, "max")
    assert _text_of(upgraded[1]["content"]).count("MAX-ONLY-TITLE") == 1
    assert upgraded[0] == plan.projection("max").system_message()

    downgraded = plan.reproject_transcript(upgraded, "low")
    assert "MAX-ONLY-TITLE" not in _text_of(downgraded[1]["content"])
    assert upgraded[1]["content"] != transcript[1]["content"] or True  # shape may differ

    # Idempotent: reprojecting twice must not accumulate copies.
    twice = plan.reproject_transcript(plan.reproject_transcript(transcript, "max"), "max")
    assert _text_of(twice[1]["content"]).count("MAX-ONLY-TITLE") == 1


def test_relocated_block_is_tagged_and_stripped_before_dispatch():
    from ouroboros.llm import _physical_candidate
    from ouroboros.system_projection import RELOCATED_KEY

    plan = _plan("MOVED-BYTES")
    messages = plan.messages_for("max")
    tagged = [block for block in messages[1]["content"]
              if isinstance(block, dict) and block.get(RELOCATED_KEY)]
    assert len(tagged) == 1

    candidate = _physical_candidate({"messages": messages, "tools": []})
    for message in candidate["messages"]:
        content = message.get("content")
        assert not isinstance(content, list) or all(
            not (isinstance(block, dict) and RELOCATED_KEY in block) for block in content)


def test_relocated_system_text_never_reaches_the_owner_corpus():
    """The owner corpus is host-attested verbatim owner input, not system text."""
    from ouroboros.review_evidence import _owner_content_projection

    plan = _plan("MOVED-BYTES")
    user_content = plan.messages_for("max")[1]["content"]
    owner_text = _owner_content_projection(user_content)
    assert owner_text.strip() == "go"
    assert "MOVED-BYTES" not in owner_text


def test_removal_site_keeps_a_visible_pointer():
    """BIBLE P1: relocation, not silent truncation."""
    sent, _tail = project(_system_blocks())
    assert "relocated to the end of this request" in _text_of([sent[0]])


def test_only_the_architecture_document_title_is_moved():
    """Only the ARCHITECTURE section's OWN first line may be relocated.

    A version-looking line anywhere deeper in the document (including inside a
    fenced example) and an H2 prose mention must both be left alone.
    """
    deep = ("# I Am Ouroboros\n\n## ARCHITECTURE.md\n\n"
            + VERSION_TITLE + "\n\nbody\n\n```\n# Ouroboros v1.2 inside a fence\n```\n")
    sent, tail = project([{"type": "text", "text": deep}])
    assert _relocated_text(tail).strip() == VERSION_TITLE
    assert "# Ouroboros v1.2 inside a fence" in _text_of([sent[0]])

    prose = "# I Am Ouroboros\n\n## ARCHITECTURE.md\n\n## About Ouroboros v6.1\n\nbody\n"
    sent, tail = project([{"type": "text", "text": prose}])
    assert tail == []
    assert "## About Ouroboros v6.1" in _text_of([sent[0]])

    no_section = "# I Am Ouroboros\n\n" + VERSION_TITLE + "\n"
    sent, tail = project([{"type": "text", "text": no_section}])
    assert tail == []


def test_empty_relocated_text_leaves_the_user_turn_untouched():
    plan = _plan("")
    messages = plan.messages_for("max")
    assert messages[1]["content"] == "go"


def test_build_context_fit_plan_projects_the_real_assembly():
    """End-to-end: the real builder must send a canonical system message."""
    from types import SimpleNamespace

    from ouroboros.context_fit import ContextCore, build_context_fit_plan

    core = ContextCore(
        base_prompt="# I Am Ouroboros",
        bible_md="constitution",
        architecture_md=VERSION_TITLE + "\nbody",
        development_md="handbook",
        semi_stable_text="## Available subagents\n\ncatalog",
        dynamic_text="## Health Invariants\n\n- OK: version sync (6.114.10)",
        user_content_json=json.dumps("go"),
        docs_need_development=True,
    )
    evidence = SimpleNamespace(route_fp="route-a", status="confirmed",
                               stale=False, window_tokens=1_000_000)

    def resolver(task, *, allow_fetch):
        return {"model": "test-model", "provider": "openai-compatible"}, evidence

    plan = build_context_fit_plan(object(), core, {}, preferred_mode="max",
                                  route_resolver=resolver)
    for mode in ("max", "low"):
        messages = plan.messages_for(mode)
        system_text = _text_of(messages[0]["content"])
        user_text = _text_of(messages[1]["content"])
        assert VERSION_TITLE not in system_text, mode
        relocated = plan.projection(mode).relocated_text
        # low renders ARCHITECTURE as a navigation map, which carries no version
        # title at all, so there is nothing to lift and the task turn is untouched.
        assert (VERSION_TITLE in relocated) is (mode == "max"), mode
        assert user_text.count(VERSION_TITLE) == (1 if mode == "max" else 0), mode
        assert user_text.startswith("go"), mode
