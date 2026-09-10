"""Review execution: how a reviewer slot is delivered.

This module is everything BELOW the review seam — delivery routes and the typed result handed
back, and each route's own prompt rendering. ``review_substrate`` keeps the
policy above the seam (attempt rails, persistence, parsing, actor projection,
quorum) and knows only that a route exists.

The dependency runs one way: this module never imports the coordinator.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional

from ouroboros.review_slot_cancel import (  # noqa: F401 — re-exported seam surface
    ReviewSessionSucceededResultUnavailable,
    _cancel_honesty_clause,
    _interaction_outlives_slot,
    _natural_success_terminal,
    _slot_cancel_outcome,
)
from ouroboros.review_dispatch import bind_api_review_paid_stamp, invoke_review_paid_stamp
from ouroboros.usage_accounting import POSITIVE_PHYSICAL_ATTEMPT_STATES
from ouroboros.triad_review import (
    ACCEPTANCE_SURFACE_RULES,
    REVIEW_JSON_ARRAY_CONTRACT,
    TIER_CLASSIFICATION_RULES,
)
from ouroboros.deadline_utils import (
    owner_deadline_exhausted,
    review_transport_timeout,
)
from ouroboros.config import get_finalization_grace_sec
from ouroboros.errors import OuroborosUnavailableError
if TYPE_CHECKING:  # annotations only — importing the substrate here would cycle
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

log = logging.getLogger("review_execution")
class ReviewRouteKind(str, Enum):
    """Closed set of review delivery routes.

    A route says HOW a reviewer slot is driven, never WHICH vendor tool runs it:
    ``api_chat`` is a chat-completions call, ``agent_session`` is a hosted agent
    session. There is deliberately no ``codex``/``claude``/``cursor`` member —
    per-harness knowledge belongs behind the transport, not in the substrate.
    """

    API_CHAT = "api_chat"
    AGENT_SESSION = "agent_session"

class ReviewRouteUnavailable(RuntimeError):
    """Typed refusal for a route with no executor in this build.

    A missing route fails loudly on its own slot; it never falls back to another
    route, model, or profile. ``code`` is the machine-readable refusal vocabulary
    (a ``route_health`` reason or a site code); "" is an uncoded raise.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "")

def _deadline_exhausted_error(
    message: str = "owner deadline leaves no dispatch window",
) -> ReviewRouteUnavailable:
    return ReviewRouteUnavailable(message, code="deadline_exhausted")

class ReviewSessionWaitingOnUser(RuntimeError):
    """A delegated review session parked on an interactive question (F18).

    Review slots are non-interactive by contract: nothing host-side answers a
    reviewer's AskUserQuestion, so waiting out the engine's answer timeout burns
    the whole slot budget in silence. The poller terminates the slot EARLY —
    cancelled through the verified-cancel path under the typed reason
    ``review_session_waiting_on_user`` — and this failure names the pending
    question plus the cancel's HONEST outcome (BR1-1): "host-cancelled" only on
    a ``confirmed`` verified receipt whose terminal is the cancel's own — a
    confirmed natural ``failed``/``interrupted`` is attributed to the run
    itself (BR2-2); anything unverified says the run may still be live, and a
    verify read that finds the run already SUCCEEDED never raises this at all
    (completion wins). Answering support for hosted review lanes is a
    deliberate non-goal (owner: no acceptance host-wait; see docs/ARCHITECTURE.md).
    """

def _render_prompt_parts(request: ReviewRequest, slot: ReviewSlot) -> tuple[str, str, str]:
    """Return (stable_governance, task_stable, dynamic_evidence) for one slot.

    Cache segmentation (v6.74.0, B1): the byte-stable governance instruction and
    the task-stable contract (goal/scope/checklist/policy — stable across the
    improvement passes of ONE task) are the two cache-marked segments; the
    mutable tail (subject, evidence, refs) is never marked, and the slot label
    lives at its TAIL so concurrent same-model slots share a warm prefix."""
    evidence = json.dumps(request.evidence, ensure_ascii=False, indent=2, default=str)
    refs = json.dumps(request.evidence_refs, ensure_ascii=False, indent=2, default=str)
    policy = json.dumps(request.policy, ensure_ascii=False, indent=2, default=str)
    classify_tier = bool(request.policy.get("classify_outcome_tier"))
    # The tier keys belong in the REQUIRED key list, not trailing prose — models
    # honor the explicit "Return JSON with keys" list and otherwise drop them,
    # which silently kills the best_effort/completion-coach lexicon.
    tier_keys = (
        ', outcome_tier ("solved"|"best_effort"|"blocked_with_evidence"), completion_coach'
        if classify_tier
        else ""
    )
    # For task acceptance the reviewer makes its derived acceptance criteria
    # VISIBLE — recorded per-actor in the review trace / objective axis (M4) so
    # "for whom we review" is auditable. Reviewer reasoning, not a new
    # authoritative gate (criteria live in actors[].parsed, not a separate phase).
    criteria_key = (
        ', criteria_used (the acceptance criteria you re-derived from the full goal narrative '
        'and checked, as [{criterion, status (supported|missing|partial|rejected), evidence_refs}]; evidence_refs must name concrete '
        'host-attested receipts/artifacts/tool results for every contributing criterion; '
        # D-Q5: the host resolves each ref by EXACT match against the packet's
        # enumerable exhibit keys — the vocabulary below is the closed set of forms.
        'each evidence_ref is resolved by EXACT match against the evidence packet, so use these exact forms: '
        'claim ids from task_contract.acceptance_claims (which count as evidence ONLY while '
        'acceptance_support_refs shows that claim supported by a passing receipt — otherwise cite the exhibit itself), '
        'verification_receipts[i] receipt ids (rows of the verification_receipts exhibit list; '
        'only a green pass/observed receipt supports a criterion), acceptance_obligations ids, artifact manifest names, '
        'or HOST-ATTESTED top-level packet section names (the agent-supplied sections — reasoning_notes, '
        'candidate_answers, agent_supplied — and task_contract itself are NOT evidence: cite the exhibit '
        'that proves the work instead) — a ref that resolves to nothing cannot support a criterion)'
        if request.surface == "task_acceptance"
        else ""
    )
    # v6.74.0 acceptance-dialogue keys (A3/A5): reviewer-authored obligation
    # identity and the typed dialogue judgement. Both live in the REQUIRED key
    # list for the same reason as the tier keys above.
    dialogue_key = (
        ', dialogue_status ("continue_actionable"|"unreachable_here"|"stable_disagreement")'
        if request.surface == "task_acceptance"
        else ""
    )
    findings_shape = (
        '[{severity, item, evidence, recommendation, disposition_kind ("new"|"re_raise"), '
        'obligation_id (required when disposition_kind="re_raise")}]'
        if request.surface == "task_acceptance"
        else "[{severity, item, evidence, recommendation}]"
    )
    tier_rules = TIER_CLASSIFICATION_RULES if classify_tier else ""
    acceptance_rules = (
        ACCEPTANCE_SURFACE_RULES if request.surface == "task_acceptance" else ""
    )
    stable = (
        "You are an independent Ouroboros reviewer slot.\n"
        f"Surface: {request.surface}\n"
        f"Role hint: {slot.role_hint or 'general reviewer'}\n\n"
        "The review subject and evidence packet arrive in the user message.\n\n"
        f"Return JSON with keys: verdict (PASS|FAIL|DEGRADED){tier_keys}{criteria_key}{dialogue_key}, findings "
        f"({findings_shape}), and summary. "
        + tier_rules
        + acceptance_rules
        + "If you cannot judge because evidence is missing, return DEGRADED and explain."
        + "\n\n"  # trailing separator: block-flattening providers glue segments
    )
    task_stable = (
        "Review goal:\n"
        f"{request.goal}\n\n"
        "Declared scope:\n"
        f"{request.scope or '(not specified)'}\n\n"
        "Checklist / acceptance criteria:\n"
        f"{request.checklist or '(none supplied)'}\n\n"
        "Policy:\n"
        f"{policy}"
        "\n\n"  # trailing separator inside the cache-marked segment (r1 #3)
    )
    dynamic = (
        "Subject:\n"
        f"{request.subject}\n\n"
        "Evidence refs:\n"
        f"{refs}\n\n"
        "Evidence packet:\n"
        f"{evidence}\n\n"
        # Slot identity stays at the TAIL of the mutable part so duplicate
        # same-model reviewer slots share one warm prefix for the whole prompt.
        f"Slot: {slot.slot_id}"
    )
    return stable, task_stable, dynamic


def _render_prompt(request: ReviewRequest, slot: ReviewSlot) -> str:
    """Flat compatibility view; segments carry their own trailing separators,
    so this equals what a block-flattening provider actually receives."""
    stable, task_stable, dynamic = _render_prompt_parts(request, slot)
    return stable + task_stable + dynamic


# Provider hard limit on declared cache breakpoints; asserted on every final payload (B1).
_MAX_PROMPT_CACHE_BREAKPOINTS = 4


def assert_cache_breakpoint_cap(messages: List[Dict[str, Any]]) -> None:
    count = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            count += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("cache_control")
            )
    if count > _MAX_PROMPT_CACHE_BREAKPOINTS:
        raise AssertionError(
            f"prompt declares {count} cache breakpoints "
            f"(cap {_MAX_PROMPT_CACHE_BREAKPOINTS})"
        )


def _request_messages(request: ReviewRequest, slot: ReviewSlot) -> List[Dict[str, Any]]:
    slot_messages = (request.slot_messages or {}).get(str(slot.slot_id or ""))
    source_messages = slot_messages if slot_messages is not None else request.messages
    if source_messages:
        messages = [
            dict(message) if isinstance(message, dict) else {"role": "user", "content": str(message)}
            for message in source_messages
        ]
        assert_cache_breakpoint_cap(messages)  # the cap covers EVERY final payload
        return messages
    # Default shape is cache-friendly (v6.74.0, B1): two cache-marked system
    # segments — the byte-stable governance instruction and the task-stable
    # contract (goal/scope/checklist/policy, unchanged across a task's
    # improvement passes) — followed by the unmarked mutable evidence tail as
    # the user message. The large evidence body changes every pass by design
    # and is honestly not cached.
    from ouroboros.tools.review_helpers import cached_prompt_blocks

    stable, task_stable, dynamic = _render_prompt_parts(request, slot)
    system_blocks = cached_prompt_blocks(stable)
    system_blocks.extend(cached_prompt_blocks(task_stable))
    messages = [
        {"role": "system", "content": system_blocks},
        {"role": "user", "content": dynamic},
    ]
    assert_cache_breakpoint_cap(messages)
    return messages


def _messages_char_count(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, list):
            total += sum(len(str(block.get("text", block))) if isinstance(block, dict) else len(str(block)) for block in content)
        else:
            total += len(str(content or ""))
    return total


@dataclass(frozen=True)
class ReviewAssignment:
    """Immutable slot job: same task and evidence, route-specific delivery."""

    request: ReviewRequest
    slot: ReviewSlot
    call_id: str = ""
    call_type: str = ""
    # Canonical/budget drive for delegated custody; the API route never reads it.
    custody_root: Any = None
    dispatch_stamp: Any = None

    @property
    def route(self) -> ReviewRouteKind:
        return self.slot.route


@dataclass(frozen=True)
class ReviewAttemptResult:
    """Typed outcome of ONE physical attempt, whatever the route."""

    message: Any
    usage: Dict[str, Any]
    raw_text: str


class ReviewSlotExecutor:
    """Route-specific delivery for one assignment.

    Owns ONLY transport: how the assignment is rendered for its route and how a
    single physical attempt is sent. Attempt policy, persistence, parsing, actor
    projection and quorum stay with ``ReviewCoordinator``.
    """

    route: ClassVar[ReviewRouteKind]

    def __init__(self, assignment: ReviewAssignment, *, llm: Any = None):
        self.assignment = assignment
        self.llm = llm

    def prompt_payload(self) -> Dict[str, Any]:
        """Route-owned projection of what will actually be sent (for the durable
        prompt record). Rendered lazily: a route that never builds the big API
        pack must never pay for building it."""
        raise NotImplementedError

    def prompt_chars(self) -> int:
        raise NotImplementedError

    def execute(self) -> ReviewAttemptResult:
        raise NotImplementedError

    def failure_custody(self) -> Dict[str, Any]:
        return {}

    def restore_custody(self, _state: Dict[str, Any]) -> None:
        return None

    def set_pending_invocation_checkpoint(
        self, _checkpoint: Optional[Callable[[str], None]],
    ) -> None:
        return None


class ApiChatReviewExecutor(ReviewSlotExecutor):
    """The chat-completions route: the historical ``LLMClient.chat`` path."""

    route = ReviewRouteKind.API_CHAT

    def __init__(self, assignment: ReviewAssignment, *, llm: Any = None):
        super().__init__(assignment, llm=llm)
        self._messages: List[Dict[str, Any]] | None = None
        self._chat_kwargs: Dict[str, Any] | None = None

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """Lazily rendered, then memoized: the prompt record and every physical
        attempt of this slot share ONE rendering, byte-identical to what the
        substrate has always produced."""
        if self._messages is None:
            self._messages = _request_messages(self.assignment.request, self.assignment.slot)
        return self._messages

    def prompt_payload(self) -> Dict[str, Any]:
        return {"messages": self.messages}

    def prompt_chars(self) -> int:
        return _messages_char_count(self.messages)

    def _kwargs(self) -> Dict[str, Any]:
        request, slot = self.assignment.request, self.assignment.slot
        if self._chat_kwargs is None:
            self._chat_kwargs = {
                "messages": self.messages,
                "model": slot.model,
                "reasoning_effort": slot.effort,
                "max_tokens": int(request.max_tokens or slot.max_tokens),
                "temperature": request.temperature if request.temperature is not None else slot.temperature,
                "no_proxy": bool(request.no_proxy),
                # Keep stable per-surface affinity; same-model slots intentionally share it.
                "cache_affinity": f"{request.surface}:{request.task_id or 'review'}",
                "use_local": bool(slot.use_local),
            }
        # Recompute this per physical send because the executor is reused for retries.
        self._chat_kwargs["timeout"] = review_transport_timeout(
            slot.model,
            getattr(slot, "transport_timeout_sec", None),
            getattr(request, "deadline_at", ""),
        )
        return self._chat_kwargs

    def execute(self) -> ReviewAttemptResult:
        chat_kwargs = self._kwargs()
        chat = getattr(self.llm, "chat", None)
        async_chat = getattr(self.llm, "chat_async", None)
        if not callable(chat) and not callable(async_chat):
            raise ReviewRouteUnavailable("api_chat client exposes no callable transport", code="api_chat_unavailable")
        deadline_at = str(getattr(self.assignment.request, "deadline_at", "") or "")
        if owner_deadline_exhausted(deadline_at=deadline_at, reserve_sec=get_finalization_grace_sec()):
            raise _deadline_exhausted_error()
        with bind_api_review_paid_stamp(self.assignment.dispatch_stamp):
            try:
                if callable(chat):
                    msg, usage = chat(**chat_kwargs)
                else:
                    msg, usage = asyncio.run(async_chat(**chat_kwargs))
            except BaseException as exc:
                # A provider-ambiguous exception is positive evidence that the
                # physical boundary was crossed even when a test adapter or old
                # transport did not enter usage_accounting's canonical marker.
                # The coordinator wraps raw stamps once-only, so this fallback
                # cannot double-charge a route that already marked dispatch.
                capture = getattr(exc, "physical_attempt_capture", None)
                if str(getattr(capture, "state", "") or "") in POSITIVE_PHYSICAL_ATTEMPT_STATES:
                    invoke_review_paid_stamp(self.assignment.dispatch_stamp)
                raise
        # Null/non-object provider messages follow the caller's empty-response rail.
        raw_text = str(msg.get("content") or "") if isinstance(msg, dict) else ""
        return ReviewAttemptResult(message=msg, usage=usage, raw_text=raw_text)


# ---------------------------------------------------------------------------
# Route configuration (phase-5 shape).
#
# One key per surface names each configured row's DELIVERY, aligned by index
# with that surface's model list; one shared key names the session target as an
# OPAQUE ``harness[=model][:effort]`` spec (Claudexor's own reviewer-panel
# spelling — no codex/claude/cursor member anywhere in this module). Phase 6's
# structured slot config (one settings SSOT per D14/6.1) supersedes these keys;
# they are read here, below the seam, so ``config.py`` stays untouched.
# ---------------------------------------------------------------------------

TRIAD_REVIEW_ROUTES_ENV = "OUROBOROS_REVIEW_ROUTES"
SCOPE_REVIEW_ROUTES_ENV = "OUROBOROS_SCOPE_REVIEW_ROUTES"
REVIEW_SESSION_ROUTE_ENV = "OUROBOROS_REVIEW_SESSION_ROUTE"

_ROUTE_TOKENS: Dict[str, ReviewRouteKind] = {
    "": ReviewRouteKind.API_CHAT,
    "api": ReviewRouteKind.API_CHAT,
    "api_chat": ReviewRouteKind.API_CHAT,
    "agent_session": ReviewRouteKind.AGENT_SESSION,
}


def configured_review_routes(env_key: str, count: int) -> List[ReviewRouteKind]:
    """Per-row delivery routes for one review surface.

    An empty key (the shipped default) is the historical behavior: every row is
    ``api_chat``. An unknown token raises — mapping a typo to ``api_chat`` would
    silently spend the API money the owner configured the row to move off of,
    and mapping it to ``agent_session`` would silently delegate a row the owner
    never delegated.
    """
    raw = os.environ.get(env_key, "") if env_key else ""
    tokens = [t.strip().lower() for t in raw.split(",")] if raw.strip() else []
    routes: List[ReviewRouteKind] = []
    for idx in range(max(0, int(count))):
        token = tokens[idx] if idx < len(tokens) else ""
        if token not in _ROUTE_TOKENS:
            raise ValueError(
                f"{env_key} row {idx + 1} names an unknown review route {token!r}; "
                f"valid: api_chat, agent_session"
            )
        routes.append(_ROUTE_TOKENS[token])
    return routes


def review_session_route() -> Any:
    """The configured session target for delegated review slots (or ``None``).

    Reuses the subagent-harness spelling and parser verbatim; when the review
    key is unset the subagent route is the target, so an owner who configured
    ONE delegated route does not have to configure it twice.
    """
    from ouroboros.subagents import get_subagent_harness, parse_subagent_harness

    raw = str(os.environ.get(REVIEW_SESSION_ROUTE_ENV, "")).strip()
    route = parse_subagent_harness(raw)
    if route is not None: return route
    if raw and raw.lower() != "off":
        # Same silent-typo class as the subagent key's reader: a non-empty value
        # that parses to nothing would quietly re-route review sessions onto the
        # subagent route as if the review key were never set.
        log.warning(
            "%s is set but unparseable (%r) — review sessions are OFF until it "
            "reads harness[=model][:effort]",
            REVIEW_SESSION_ROUTE_ENV, raw)
    return None if raw else get_subagent_harness()


# ---------------------------------------------------------------------------
# Typed verdict for a delegated session (D19 / plan 5.4).
# ---------------------------------------------------------------------------

# The ASK: sent as ``outputSchema`` only when the EFFECTIVE route can carry it
# (D19) — judged on the pinned harness's live manifest, never on the static
# adapter flag alone, because the flag describes the adapter and not the
# transport this run actually rides. The run's own reported
# ``outputConformance == "passed"`` is the only thing that lets the structured
# payload be TRUSTED as the verdict (never run success).
REVIEW_SESSION_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["item", "verdict", "severity", "reason"],
                "properties": {
                    "item": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                    "severity": {"type": "string", "enum": ["critical", "advisory"]},
                    "reason": {"type": "string"},
                    "obligation_id": {"type": "string"},
                },
            },
        },
    },
}


def review_session_output_schema(surface: str) -> Dict[str, Any]:
    """The session verdict schema, shaped to the SURFACE's own clean contract.

    The shared schema admits ``{"findings": []}`` — the honest clean verdict for a
    triad or ordinary advisory reviewer. Scope's coverage contract requires all
    checklist rows (PASS included); Skill Review has the same matrix shape. Their
    schemas demand ``minItems: 1`` so an engine cannot conform with an empty answer;
    each surface's downstream parser still verifies exact item coverage.
    """
    if surface == "plan_review":
        # plan review's own element contract (4e133c8a): the generic item/verdict shape
        # would conform-and-launder — an unknown class demotes to a note.
        from ouroboros.tools.plan_spec import PLAN_REVIEW_SESSION_OUTPUT_SCHEMA

        return PLAN_REVIEW_SESSION_OUTPUT_SCHEMA
    if surface not in {"scope_review", "skill_review"}:
        return REVIEW_SESSION_OUTPUT_SCHEMA
    shaped = json.loads(json.dumps(REVIEW_SESSION_OUTPUT_SCHEMA))
    shaped["properties"]["findings"]["minItems"] = 1
    return shaped

# Verdict canonicalization lives in its own module (altitude, P7): both the
# session route and the native tool-round route consume it.
from ouroboros.review_verdict_extraction import (  # noqa: F401 — re-exported seam surface
    _EXTRACT_MAX_CHARS,
    _UNEXTRACTABLE,
    _extract_verdict_via_light_model,
    _findings_array,
    _strictly_parseable,
    canonicalize_session_verdict,
)


# ---------------------------------------------------------------------------
# The agent-session route: a delegated read-only Claudexor run per slot.
# ---------------------------------------------------------------------------

# Layered by Claudexor into the harness's native system-prompt channel — a
# statement of role, not the enforcement (the enforcement is the readonly
# access profile the engine derives).
_REVIEW_SESSION_INSTRUCTIONS = (
    "You are a delegated read-only REVIEWER session. Retrieve the evidence "
    "yourself with the tools your read-only mode actually gives you inside this "
    "repository root: read files and search, and — only if command execution is "
    "among them, which read-only frequently withholds — read-only git commands "
    "(git diff --cached, git log, git show). Reading the tree directly is a "
    "complete substitute; a review is not incomplete for lacking a shell. Do "
    "not modify anything and do not run history-moving git commands. Your final "
    "answer must follow the output contract in the task EXACTLY; your host "
    "parses it structurally, and prose around the verdict is a non-response."
)

@dataclass(frozen=True)
class SessionInvocation:
    """WHO is asking and HOW it should be delivered, as one immutable value.

    The same parameter-object pattern as ``ReviewAssignment``: these knobs are the
    caller's identity and delivery policy, they always travel together, and passing
    them as nine parallel keyword arguments put the function past the parameter
    budget while inviting a silent mis-pairing (a slot id from one row beside
    another row's route). ``retry_state`` is deliberately the caller's OWN mutable
    dict — the pending invocation id must survive back out to whoever owns the
    permitted retry — so this value freezes the reference, not the contents.
    """

    task_id: str
    surface: str
    slot_id: str
    timeout_sec: float
    logical_key_extra: tuple = ()
    output_schema: Optional[Dict[str, Any]] = None
    session_route: Any = None
    instructions: str = _REVIEW_SESSION_INSTRUCTIONS
    retry_state: Optional[Dict[str, Any]] = None
    reconcile_only: bool = False
    use_thread: bool = False
    thread_id: str = ""
    dispatch_stamp: Any = None
    operation_id: str = ""
    pending_invocation_checkpoint: Optional[Callable[[str], None]] = None
    owner_deadline_at: str = ""

def run_delegated_review_session(
    *,
    prompt: str,
    root: str,
    custody_drive: Any,
    invocation: SessionInvocation,
) -> Dict[str, Any]:
    """Start, watch, settle and collect one delegated read-only review.

    Claudexor retired (Seed 0): the owned gateway carried every step of this
    transport — project registration, the POST, the poll, the verified cancel
    and the settlement — and it went with the gateway modules. The capability
    is therefore explicitly unavailable: a typed refusal on the caller's own
    slot, never an empty session result a caller could read as an honest zero.
    """
    raise OuroborosUnavailableError(
        "delegated review sessions are unavailable: the Claudexor gateway retired"
    )


class AgentSessionReviewExecutor(ReviewSlotExecutor):
    """One pinned Claudexor run per reviewer slot.

    The coordinator owns policy; this executor never restarts for format repair.
    """
    route = ReviewRouteKind.AGENT_SESSION

    def __init__(self, assignment: ReviewAssignment, *, llm: Any = None):
        super().__init__(assignment, llm=llm)
        self._session_prompt: Optional[str] = None
        self._raw_transcript: Optional[str] = None
        self._conformance_passed = False
        self._run_id = ""
        self._session_usage: Dict[str, Any] = {}
        self._deltas: List[Dict[str, Any]] = []
        # Unknown starts retain the exact invocation token for the permitted retry.
        self._retry_state: Dict[str, Any] = {}
        self._pending_invocation_checkpoint: Optional[Callable[[str], None]] = None
        # A settled run failure is replayed rather than billed twice.
        self._settled_failure: Optional[BaseException] = None

    # -- prompt (route-owned; never the api pack) ------------------------------

    def _output_contract(self) -> str:
        contract = str((self.assignment.request.policy or {}).get("output_contract") or "")
        return contract or REVIEW_JSON_ARRAY_CONTRACT

    def prompt_payload(self) -> Dict[str, Any]:
        return {"session_prompt": self.session_prompt}

    def prompt_chars(self) -> int:
        return len(self.session_prompt)

    @property
    def session_prompt(self) -> str:
        """The compact task this route sends — the SAME task, criteria and
        output contract as the api pack, minus the assembled evidence: a
        delegated reviewer retrieves context on the fly with its tools (D12),
        so the giant pack the session replaces is never built here (plan 5.2)."""
        if self._session_prompt is None:
            request, slot = self.assignment.request, self.assignment.slot
            task = str(request.session_task or "").strip()
            if not task:
                raise ReviewRouteUnavailable(
                    "agent_session slot has no session task: the surface must supply "
                    "the route-owned task text (request.session_task) — the assembled "
                    "api pack is deliberately not sendable to a session", code="session_task_missing")
            parts = [
                "You are an independent Ouroboros reviewer slot running as a "
                "read-only agent session.",
                f"Surface: {request.surface}",
                f"Role hint: {slot.role_hint or 'general reviewer'}",
                "",
                task,
                "",
                "OUTPUT CONTRACT (your host parses this structurally):",
                self._output_contract() + "\nThis contract governs the unwrapped substantive deliverable; emit any host-required transport metadata outside it exactly as separately instructed.",
                f"Slot: {slot.slot_id}",
            ]
            self._session_prompt = "\n".join(parts)
        return self._session_prompt
    # -- delivery --------------------------------------------------------------

    def execute(self) -> ReviewAttemptResult:
        if self._raw_transcript is not None:
            # Plan 5.5: the permitted resend repairs FORMAT locally over the
            # collected transcript; it never launches a second session.
            return self._verdict_result(force_extraction=True)
        if self._settled_failure is not None:
            # Pre-start transients retain a pending invocation and do not land here.
            raise self._settled_failure
        try:
            self._run_session()
        except BaseException as exc:
            self._run_id = self._run_id or str(getattr(exc, "delegated_run_id", "") or "")
            if not self._retry_state.get("pending_invocation_id"):
                self._settled_failure = exc
            raise
        return self._verdict_result()

    def failure_custody(self) -> Dict[str, Any]:
        failure = self._settled_failure
        run_id = self._run_id or str(getattr(failure, "delegated_run_id", "") or "")
        pending = str(self._retry_state.get("pending_invocation_id") or "")
        return {"delegated_run_started": bool(run_id), "delegated_run_id": run_id,
                "pending_invocation_id": pending}

    def restore_custody(self, state: Dict[str, Any]) -> None:
        # The logical waiter and the physical worker share this small mutable
        # custody cell so a timeout actor can durably carry a just-started run.
        self._retry_state = state

    def set_pending_invocation_checkpoint(
        self, checkpoint: Optional[Callable[[str], None]],
    ) -> None:
        # Captured by the physical worker before the logical caller may return.
        # Commit review uses it to patch the exact reserved slot before POST.
        self._pending_invocation_checkpoint = checkpoint
    def _session_route(self) -> Any:
        # 6.1: a structured row carries ITS OWN opaque target; the shared
        # session-route key stays as the legacy fallback for rows without one.
        spec = str(getattr(self.assignment.slot, "session_target", "") or "")
        if spec:
            import dataclasses
            from ouroboros.subagents import parse_subagent_harness

            route = parse_subagent_harness(spec)
            if route is None:
                raise ReviewRouteUnavailable(
                    f"agent_session slot {self.assignment.slot.slot_id} has an "
                    f"unparsable session target {spec!r}", code="session_target_unparsable")
            # D1/6.3: effort has ONE source — the per-slot effort field. The
            # target_id carries route identity only; any effort a caller
            # embedded in the spec (`harness=model:effort`) is dropped so the
            # field can never be silently overridden by the identity string.
            route = dataclasses.replace(route, effort=str(self.assignment.slot.effort or ""))
            pin = str(getattr(self.assignment.slot, "session_profile", "") or "")
            if pin:
                route = dataclasses.replace(route, profile_id=pin)
            return route
        route = review_session_route()
        if route is None:
            raise ReviewRouteUnavailable(
                "agent_session review slot has no configured session route "
                f"({REVIEW_SESSION_ROUTE_ENV} / OUROBOROS_SUBAGENT_HARNESS are empty or `off`)",
                code="session_route_unconfigured")
        return route

    def _custody_drive(self) -> Any:
        drive = self.assignment.custody_root
        if drive is None:
            raise ReviewRouteUnavailable(
                "agent_session slot has no custody root: a delegated review run "
                "must be durably custodied before it may start", code="custody_root_missing")
        return drive

    def _run_session(self) -> None:
        request, slot = self.assignment.request, self.assignment.slot
        root = str(request.session_root or "").strip()
        if not root:
            raise ReviewRouteUnavailable(
                "agent_session slot has no session root: the surface must name the "
                "repository root the reviewer session runs in", code="session_root_missing")
        from ouroboros.config import get_finalization_grace_sec
        from ouroboros.deadline_utils import review_operation_timeout_sec
        logical_deadline = getattr(self, "_logical_deadline_monotonic", None)
        logical_timeout = (
            max(0.001, float(logical_deadline) - time.monotonic())
            if logical_deadline is not None else
            review_operation_timeout_sec(getattr(slot, "timeout_sec", None),
                route=getattr(slot, "route", None),
                deadline_at=getattr(request, "deadline_at", "") or "",
                transport_timeout_sec=getattr(slot, "transport_timeout_sec", None),
                reserve_sec=get_finalization_grace_sec())
        )
        facts = run_delegated_review_session(
            prompt=self.session_prompt,
            root=root,
            custody_drive=self._custody_drive(),
            invocation=SessionInvocation(
                task_id=str(request.task_id or ""),
                surface=request.surface,
                slot_id=slot.slot_id,
                timeout_sec=logical_timeout,
                logical_key_extra=(self.assignment.call_id,),
                output_schema=review_session_output_schema(request.surface),
                session_route=self._session_route(),
                retry_state=self._retry_state,
                reconcile_only=bool(getattr(request, "reconcile_only", False)),
                use_thread=request.surface == "plan_review",
                thread_id=str((request.session_threads or {}).get(slot.slot_id) or ""),
                dispatch_stamp=self.assignment.dispatch_stamp,
                operation_id=self.assignment.call_id,
                pending_invocation_checkpoint=self._pending_invocation_checkpoint,
                owner_deadline_at=str(getattr(request, "deadline_at", "") or ""),
            ),
        )
        self._run_id = facts["run_id"]
        conformance = facts["conformance"]
        self._conformance_passed = conformance == "passed"
        if not facts["schema_asked"]:
            # This route always REQUESTS the structured verdict; an effective
            # transport that cannot carry the schema is a landing below the
            # ask, disclosed rather than silently downgraded to prose (D4).
            self._deltas.append({
                "kind": "capability_delta",
                "requested": "outputSchema (structured verdict)",
                "effective": f"no structured output on effective route {facts['route_id']}",
                "reason": "schema_unavailable_on_effective_route",
            })
        elif not self._conformance_passed:
            self._deltas.append({
                "kind": "capability_delta",
                "requested": "outputSchema (structured verdict)",
                "effective": f"outputConformance={conformance or 'absent'}",
                "reason": "schema_not_conformed_on_effective_route",
            })
        effective_routes = facts.get("effective_route_ids") or []
        if effective_routes and set(effective_routes) != {facts["route_id"]}:
            # Belt over the pin: the request names exactly one eligible
            # harness, so the engine's receipt disagreeing is drift that must
            # surface loudly, never a quietly accepted substitute route.
            self._deltas.append({
                "kind": "capability_delta",
                "requested": f"route {facts['route_id']} (pinned pool)",
                "effective": "route(s) " + ", ".join(effective_routes),
                "reason": "session_ran_off_pinned_route",
            })
        spend, estimated = facts["spend"], facts["spend_estimated"]
        self._session_usage = {
            "provider": "claudexor",
            "resolved_model": facts["model"],
            "delegated_run_id": facts["run_id"],
            "delegated_route": facts["route_id"],
            "review_thread_id": str(facts.get("thread_id") or ""),
            "review_turn_id": str(facts.get("turn_id") or ""),
            "review_thread_receipt": facts.get("thread_receipt") or {},
            "auth_route_receipt": facts.get("auth_route_receipt") or {},
            "profile_continuity_receipt": facts.get("profile_continuity_receipt") or {},
            # APPLIED account/access (D29): what the engine's receipt disclosed,
            # '' when telemetry predates it — shown as absent, never as the
            # requested value dressed up as applied.
            "applied_profile": facts.get("applied_profile", ""),
            "applied_access": facts.get("applied_access", ""),
            # Whether the durable start row actually landed. `record_started`'s answer
            # is already a fact the caller acts on; carrying it into the actor record
            # too means a verdict delivered by a run with NO durable custody is legible
            # afterwards instead of looking identical to a custodied one.
            "custody_durable": bool(facts.get("custody_durable")),
            "output_conformance": conformance,
            "settlement": facts["settlement"],
            # The ledger row is written by settle_run (record_subscription_session);
            # cost rides here for the actor record only, finality following the
            # spendEstimated fact, never re-derived.
            "cost": spend if (spend is not None and not estimated) else None,
            "cost_disclosed_usd": spend,
            "cost_estimated": estimated,
        }
        slot_model = str(slot.model or "")
        session_target = str(getattr(slot, "session_target", "") or "")
        from ouroboros.provider_models import normalize_model_identity
        if session_target:
            # Structured rows keep the opaque ``harness[=model]`` target in
            # ``slot.model`` for row identity/display, while the daemon sees
            # only the parsed model component. Compare like with like: the old
            # full-spec-vs-model comparison invented a capability delta for
            # every healthy pinned session row.
            from ouroboros.subagents import parse_subagent_harness

            parsed_target = parse_subagent_harness(session_target)
            slot_model = str(getattr(parsed_target, "model", "") or "")
        if (
            slot_model and facts["model"]
            and normalize_model_identity(slot_model) != normalize_model_identity(facts["model"])
        ):
            self._deltas.append({
                "kind": "capability_delta",
                "requested": f"model {slot_model}",
                "effective": f"model {facts['model']}",
                "reason": "session_route_resolves_its_own_model",
            })
        # PAID EVIDENCE: the transcript always feeds the parser whole. A profile
        # continuity `cannot_verify` is telemetry, never a reason to blank it.
        self._raw_transcript = facts["text"]

    def _verdict_result(self, force_extraction: bool = False) -> ReviewAttemptResult:
        text = self._raw_transcript or ""
        canonical, method, extraction_usage = canonicalize_session_verdict(
            text,
            conformance_passed=self._conformance_passed and not force_extraction,
            contract=self._output_contract(),
            llm=self.llm,
            deadline_at=getattr(self.assignment.request, "deadline_at", "") or "",
            transport_timeout_sec=getattr(self.assignment.slot, "transport_timeout_sec", None),
        )
        usage = dict(self._session_usage)
        deltas = list(self._deltas)
        if method == "light_model_extraction":
            usage["extraction"] = extraction_usage
            deltas = deltas + [{
                "kind": "capability_delta",
                "requested": "structured verdict from the session",
                "effective": "light-model extraction over the collected transcript",
                "reason": "extraction_instead_of_schema",
            }]
        elif method == "extraction_incomplete":
            deltas = deltas + [{
                "kind": "capability_delta",
                "requested": "structured verdict from the session",
                "effective": (
                    f"no verdict: transcript ({len(text)} chars) exceeds the "
                    "single-send extraction bound"
                ),
                "reason": "extraction_incomplete_transcript_exceeds_bound",
            }]
        usage["verdict_method"] = method
        # P1: the cognitive artifact is the SESSION's own output, and canonicalization
        # legitimately destroys it — a schema-conformant `{"findings": []}` becomes `[]`
        # and light extraction replaces the narrative wholesale. Keeping the transcript
        # only in this object made the decision unreconstructible the moment the process
        # ended. The raw text rides the MESSAGE, which the coordinator persists whole via
        # persist_call (redacted projection, no truncation), and the provenance below
        # says exactly which text produced which verdict.
        usage["verdict_provenance"] = {
            "raw_transcript_chars": len(text),
            "raw_transcript_sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "canonical_chars": len(canonical),
            "canonical_sha256": hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest(),
            "output_conformance": self._session_usage.get("output_conformance") or "",
            "conformance_trusted": bool(self._conformance_passed and not force_extraction),
            "verdict_method": method,
            "raw_transcript_carrier": "message.session_transcript (durable response_ref)",
        }
        if deltas:
            usage["capability_delta"] = deltas
            self._emit_capability_delta(deltas, method)
        message = {
            "content": canonical,
            # The unmodified session output, persisted alongside the canonical form.
            "session_transcript": text,
            "delegated_run_id": self._run_id,
            "verdict_method": method,
        }
        return ReviewAttemptResult(message=message, usage=usage, raw_text=canonical)
    def _emit_capability_delta(self, deltas: List[Dict[str, Any]], method: str) -> None:
        """Durable half of the disclosure (D4): every landing below what was
        asked reaches the event log, not only the actor record."""
        try:
            from ouroboros import delegate_custody as custody

            custody.emit(self._custody_drive(), "review_slot_capability_delta", {
                "run_id": self._run_id,
                "surface": self.assignment.request.surface,
                "slot_id": self.assignment.slot.slot_id,
                "verdict_method": method,
                "deltas": deltas,
            })
        except Exception:
            log.warning("capability_delta disclosure write failed", exc_info=True)

# Closed route table. Adding a route means adding an executor here; it never
# means adding a branch to the coordinator.
_REVIEW_ROUTE_EXECUTORS: Dict[ReviewRouteKind, type[ReviewSlotExecutor]] = {
    ReviewRouteKind.API_CHAT: ApiChatReviewExecutor,
    ReviewRouteKind.AGENT_SESSION: AgentSessionReviewExecutor,
}

def _review_route_executor(assignment: ReviewAssignment, *, llm: Any = None) -> ReviewSlotExecutor:
    """Bind a route to its executor. The ONLY place a review transport is chosen.

    Bound once per slot because the durable prompt record must be written from
    the route's own (lazily rendered) projection BEFORE the first send.
    """
    try:
        route = ReviewRouteKind(assignment.route)
    except ValueError:
        raise ReviewRouteUnavailable(f"unknown review route: {assignment.slot.route!r}", code="unknown_review_route") from None
    if route is ReviewRouteKind.API_CHAT and bool(
        getattr(assignment.slot, "native_retrieval", False)
    ):
        # A configured-subagent api row is the RETRIEVES class: same wire kind,
        # different delivery — bounded native tool rounds, never the packet.
        # Imported lazily: the native module subclasses this module's seam.
        from ouroboros.review_native_episode import NativeToolRoundReviewExecutor

        return NativeToolRoundReviewExecutor(assignment, llm=llm)
    executor_cls = _REVIEW_ROUTE_EXECUTORS.get(route)
    if executor_cls is None:
        raise ReviewRouteUnavailable(f"review route not implemented in this build: {route.value}",
                                     code="review_route_not_implemented")
    return executor_cls(assignment, llm=llm)


def _execute_slot_attempt(
    assignment: ReviewAssignment,
    *,
    llm: Any = None,
    executor: ReviewSlotExecutor | None = None,
) -> ReviewAttemptResult:
    """Run ONE physical attempt for ``assignment`` — the single execution seam.

    Everything route-specific happens at or below this call; everything above it
    (attempt rails, persistence, parsing, actor projection, quorum) is route
    agnostic. Pass the ``executor`` already bound for the prompt record so a slot
    renders its prompt once instead of once per attempt.
    """
    return (executor or _review_route_executor(assignment, llm=llm)).execute()
