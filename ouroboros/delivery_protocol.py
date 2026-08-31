"""Delivery-finalization protocol vocabulary and parsers.

Extracted from ``ouroboros/loop.py`` at its module-size ceiling; loop
re-exports the historical underscore names, so its callers, tests, and
monkeypatch targets keep one import surface. Protocol domain ONLY — the
candidate dataclass, the control/hold vocabulary, the typed control prompt,
and the pure parsers. The ctx-touching resolvers (arming, holding, resolving
a control round) stay in ``loop``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.observability import strip_protocol_fence


@dataclass
class DeliveryCandidate:
    """Loop-local complete answer retained across service/finalization rounds."""

    full_text: str
    content_sha256: str
    revision: int
    evidence_revision: int
    evidence_fingerprint: str
    acceptance_binding: Dict[str, Any]
    finalization_control: str = "candidate"
    repair_attempted: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    model_text: str = ""


# Action-gate holds: a gate closable ONLY by a tool call (skill lifecycle
# action, child-result disposition) retains the candidate WITHOUT arming the
# JSON-only control instruction — the instruction would conflict with the
# required tool call. Gates closable by a reconsidered answer arm normally.
SKILL_ACTION_HOLD_CONTROL = "skill_action_or_revision_required"
CHILD_ABSORPTION_HOLD_CONTROL = "child_absorption_or_revision_required"
DELIVERY_HOLD_CONTROLS = frozenset({
    SKILL_ACTION_HOLD_CONTROL,
    CHILD_ABSORPTION_HOLD_CONTROL,
})


def delivery_control_prompt(candidate: DeliveryCandidate, *, keep_allowed: bool) -> str:
    keep_line = (
        "keep is allowed because no answer-invalidating evidence changed."
        if keep_allowed
        else "keep is NOT allowed because owner/tool/child/verification evidence changed."
    )
    return (
        "[DELIVERY_FINALIZATION_CONTROL]\n"
        f"A complete answer candidate (revision {candidate.revision}, sha256 "
        f"{candidate.content_sha256[:12]}) is retained by the loop; do not replace it with a "
        f"service notice. {keep_line}\n"
        "Return exactly one JSON object and no other text:\n"
        '{"delivery_control":"keep"}\n'
        "or\n"
        '{"delivery_control":"replace","full_answer":"<the complete user-facing answer>"}'
    )


def delivery_replace_required(candidate: DeliveryCandidate) -> bool:
    """Return whether a typed full replacement is mandatory for this control round."""

    return candidate.finalization_control.startswith(
        ("effect_revision_required", "skill_revision_required")
    )


def delivery_keep_allowed(
    candidate: DeliveryCandidate,
    evidence_revision: int,
    evidence_fingerprint: str,
) -> bool:
    return (
        not delivery_replace_required(candidate)
        and candidate.evidence_revision == evidence_revision
        and candidate.evidence_fingerprint == evidence_fingerprint
    )


def parse_delivery_control_object(
    raw: str,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Parse a delivery-control object while rejecting duplicate JSON keys.

    The boolean preserves protocol intent for the repair path when a duplicate
    ``delivery_control`` or ``full_answer`` key made the object invalid.
    """

    duplicate_protocol_key = False

    def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        nonlocal duplicate_protocol_key
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                if key in {"delivery_control", "full_answer"}:
                    duplicate_protocol_key = True
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        # RecursionError: a degenerate deeply-nested blob (repetition-loop
        # model output) must classify as not-a-control, not crash the round.
        return None, duplicate_protocol_key
    if not isinstance(payload, dict):
        return None, False
    return payload, False


def parse_delivery_control_body(
    raw: str,
) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    """Normalize a response body and locate its delivery-control object.

    Returns ``(parsed, duplicate_protocol_key, embedded)``. Normalization
    strips one whole-body markdown fence (shared with
    ``observability._is_delivery_control_payload``). ``embedded`` is True only
    when the protocol object sits as a balanced trailing JSON object carrying
    the ``delivery_control`` key at the very END of surrounding prose — a
    protocol attempt mixed with text, never a valid control. A control object
    quoted MID-prose is NOT matched and stays prose: Ouroboros legitimately
    quotes the literal in its own PR bodies and docs (disclosed residual).
    """

    body = strip_protocol_fence(raw)
    parsed, duplicate_protocol_key = parse_delivery_control_object(body)
    if duplicate_protocol_key or isinstance(parsed, dict):
        return parsed, duplicate_protocol_key, False
    # Trailing scan: ONE O(n) string-aware pass over the body (fenced and
    # double-fenced tails peeled, duplicate keys flagged, RecursionError
    # degraded, bounded line-anchor retries after an unbalanced prose brace
    # or quote) — the per-`{` raw_decode walk this replaces was O(n*braces)
    # and measured ~10s on a large code-bearing forced answer. The extractor
    # is key-agnostic; the protocol judgment stays HERE: only a trailing
    # object carrying `delivery_control` at its top level (or a duplicated
    # protocol key) is an embedded protocol attempt — an ordinary trailing
    # JSON object, and a protocol object NESTED inside one, is prose.
    from ouroboros.utils import extract_trailing_json_object

    _prefix, tail_parsed, tail_duplicate = extract_trailing_json_object(
        body, duplicate_flag_keys=("delivery_control", "full_answer"),
    )
    if tail_duplicate:
        return None, True, True
    if isinstance(tail_parsed, dict) and "delivery_control" in tail_parsed:
        return tail_parsed, False, True
    return None, False, False


def extract_plain_text_from_content(content: Any) -> str:
    """Extract text from strings or multipart content for transcript sealing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content) if content is not None else ""
