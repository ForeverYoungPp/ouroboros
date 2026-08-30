"""The native tool-round review delivery (configured-subagent api rows).

A reviewer row bound to an ``api_model`` configured subagent RETRIEVES the
subject through bounded read-only inspection rounds instead of receiving the
assembled packet. Not a third public route kind — ``api_chat`` stays the wire
vocabulary; the slot's actor binding selects this executor at the one transport
seam (``review_execution._review_route_executor``).

ONE episode is ONE logical review attempt: up to the configured round cap of
``LLMClient.chat(tools=…)`` calls against a fresh, instance-local
inspection-only ``ToolRegistry``. Every provider call is its own ledger row;
the caps fail closed (typed refusal, never mid-episode compaction or resume);
the coordinator's second actor attempt repairs FORMAT locally, exactly like
the session executor.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from ouroboros.config import get_finalization_grace_sec
from ouroboros.deadline_utils import owner_deadline_exhausted, review_transport_timeout
from ouroboros.review_dispatch import bind_api_review_paid_stamp, invoke_review_paid_stamp
from ouroboros.review_verdict_extraction import canonicalize_session_verdict
from ouroboros.triad_review import REVIEW_JSON_ARRAY_CONTRACT
from ouroboros.usage_accounting import (
    POSITIVE_PHYSICAL_ATTEMPT_STATES,
    physical_attempt_limit,
)

from ouroboros.review_execution import (
    ReviewAssignment,
    ReviewAttemptResult,
    ReviewRouteKind,
    ReviewRouteUnavailable,
    ReviewSlotExecutor,
    _deadline_exhausted_error,
)

log = logging.getLogger("review_native_episode")


def review_native_max_rounds() -> int:
    """Provider-call cap of one native episode (config-owned, fail closed)."""
    from ouroboros.config import _clamped_number_setting

    return _clamped_number_setting("OUROBOROS_REVIEW_NATIVE_MAX_ROUNDS", low=2, high=64, cast=int)


def review_native_max_transcript_chars() -> int:
    """Episode transcript bound; exceeding it is a typed refusal, never compaction."""
    from ouroboros.config import _clamped_number_setting

    return _clamped_number_setting(
        "OUROBOROS_REVIEW_NATIVE_MAX_TRANSCRIPT_CHARS", low=50_000, high=2_000_000, cast=int)


def native_or_packet_attempt_rail(slot: Any, two_send_surface: bool) -> Any:
    """The physical-send rail for one actor: the episode cap for a native
    tool-round slot, the historical two-send rail for a packet/session actor
    on the P3/acceptance surfaces, no rail otherwise."""
    if not two_send_surface:
        return contextlib.nullcontext()
    if bool(getattr(slot, "native_retrieval", False)):
        return physical_attempt_limit(native_episode_physical_send_cap())
    return physical_attempt_limit(2)

# ---------------------------------------------------------------------------
# The native tool-round route variant: a configured-subagent api row RETRIEVES
# the subject through bounded read-only inspection rounds instead of receiving
# the assembled packet. Not a third public route kind — ``api_chat`` stays the
# wire vocabulary; the slot's actor binding selects this executor internally.
# ---------------------------------------------------------------------------

_INSPECTION_TOOL_NAMES = (
    "read_file", "list_files", "search_code", "query_code",
    "vcs_status", "vcs_diff",
)

_NATIVE_REVIEW_INSTRUCTIONS = (
    "You are an independent Ouroboros reviewer running a bounded read-only "
    "inspection episode. Retrieve the evidence yourself with the tools you are "
    "given inside the repository root — read_file, list_files, search_code, "
    "query_code, vcs_status, vcs_diff; no other tools exist here. You cannot "
    "modify anything and have no shell. Read LARGE files in bounded chunks "
    "(read_file supports offset/limit) instead of requesting a whole large "
    "document at once: the episode has hard round and transcript budgets, and "
    "an oversized read spends both. Read what the checklist requires, then "
    "answer. Your FINAL message must contain no tool calls and must follow "
    "the output contract in the task EXACTLY; your host parses it "
    "structurally, and prose around the verdict is a non-response."
)

# Per-tool-result bound inside the episode: one greedy full read of a giant
# artifact must not consume the whole transcript budget in a single round.
# Disclosed truncation with an offset/limit continuation handle is honest —
# unlike compaction, nothing unseen is summarized into the record.
_EPISODE_TOOL_RESULT_CHAR_CAP = 120_000

# Physical-send headroom per provider call: llm.py may spend one internal
# one-shot recovery send per chat() (param-drop/wire-recovery/reroute), and the
# limit counts PHYSICAL sends. +2 keeps the final-answer round and the
# coordinator's bookkeeping honest without unbounding anything.
def native_episode_physical_send_cap() -> int:
    return 2 * review_native_max_rounds() + 2


class NativeToolRoundReviewExecutor(ReviewSlotExecutor):
    """Bounded native inspection episode for a configured-subagent api row.

    ONE episode is ONE logical review attempt: up to the configured round cap
    of ``LLMClient.chat(tools=…)`` calls against a fresh, instance-local
    inspection-only ``ToolRegistry``. Every provider call is its own ledger
    row (the ambient usage scope attributes them); the coordinator's second
    actor attempt repairs FORMAT locally over the collected answer, exactly
    like the session executor — there is no mid-episode resume, transcript
    compaction, or per-round durable ledger, and the caps fail closed.
    """

    route = ReviewRouteKind.API_CHAT

    def __init__(self, assignment: ReviewAssignment, *, llm: Any = None):
        super().__init__(assignment, llm=llm)
        self._episode_prompt: Optional[str] = None
        self._raw_transcript: Optional[str] = None
        self._episode_usage: Dict[str, Any] = {}
        self._tool_receipts: List[Dict[str, Any]] = []
        self._tool_calls_total = 0
        self._rounds_used = 0
        self._settled_failure: Optional[BaseException] = None

    # -- prompt (route-owned; never the api pack) ------------------------------

    def _output_contract(self) -> str:
        contract = str((self.assignment.request.policy or {}).get("output_contract") or "")
        return contract or REVIEW_JSON_ARRAY_CONTRACT

    def prompt_payload(self) -> Dict[str, Any]:
        return {
            "native_episode_prompt": self.episode_prompt,
            "inspection_tools": list(_INSPECTION_TOOL_NAMES),
        }

    def prompt_chars(self) -> int:
        return len(self.episode_prompt)

    @property
    def episode_prompt(self) -> str:
        """The SAME compact work-order the session route sends (D12): task,
        criteria and output contract, minus any assembled evidence — the actor
        retrieves context itself, so the packet is never built here."""
        if self._episode_prompt is None:
            request, slot = self.assignment.request, self.assignment.slot
            task = str(request.session_task or "").strip()
            if not task:
                raise ReviewRouteUnavailable(
                    "native tool-round slot has no session task: the surface must "
                    "supply the route-owned task text (request.session_task) — the "
                    "assembled api pack is deliberately not sendable to a retrieving "
                    "actor", code="session_task_missing")
            parts = [
                "You are an independent Ouroboros reviewer slot running a bounded "
                "read-only native inspection episode.",
                f"Surface: {request.surface}",
                f"Role hint: {slot.role_hint or 'general reviewer'}",
                "",
                task,
                "",
                "OUTPUT CONTRACT (your host parses this structurally):",
                self._output_contract()
                + "\nYour final message must contain the deliverable alone, with no tool calls.",
                f"Slot: {slot.slot_id}",
            ]
            self._episode_prompt = "\n".join(parts)
        return self._episode_prompt

    # -- delivery --------------------------------------------------------------

    def execute(self) -> ReviewAttemptResult:
        if self._raw_transcript is not None:
            # The permitted resend repairs FORMAT locally over the collected
            # final answer; it never runs a second paid episode.
            return self._verdict_result(force_extraction=True)
        if self._settled_failure is not None:
            raise self._settled_failure
        try:
            self._run_episode()
        except BaseException as exc:
            self._settled_failure = exc
            raise
        return self._verdict_result()

    def _inspection_registry(self, root: str, scratch: Any) -> tuple[Any, List[Dict[str, Any]]]:
        """A fresh instance-local registry pinned to ``root``, read-only.

        Reuses the existing capability machinery wholesale instead of a new
        allowlist mechanism: the ``local_readonly_subagent`` constraint gives
        the read-only operation/root matrix, ``disabled_tools`` trims its
        broader name allowlist down to the inspection six, and the resource
        contract keeps extension/MCP discovery off. ``registry._ctx`` is
        per-instance, so the worker's own registry/context is never touched.
        """
        import pathlib as _pathlib

        from ouroboros.tool_capabilities import LOCAL_READONLY_SUBAGENT_TOOL_NAMES
        from ouroboros.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry(repo_dir=_pathlib.Path(root), drive_root=_pathlib.Path(scratch))
        ctx = ToolContext(
            repo_dir=_pathlib.Path(root),
            drive_root=_pathlib.Path(scratch),
            task_id=str(self.assignment.request.task_id or "") or None,
            task_constraint={"mode": "local_readonly_subagent"},
            task_contract={
                "allowed_resources": {"network": False, "web": False},
                "disabled_tools": sorted(
                    set(LOCAL_READONLY_SUBAGENT_TOOL_NAMES) - set(_INSPECTION_TOOL_NAMES)
                ),
            },
        )
        registry.set_context(ctx)
        schemas = []
        for name in _INSPECTION_TOOL_NAMES:
            schema = registry.get_schema_by_name(name)
            if schema:
                schemas.append(schema)
        if not schemas:
            raise ReviewRouteUnavailable(
                "no inspection tool schemas are projectable for the native "
                "tool-round episode", code="native_inspection_unavailable")
        return registry, schemas

    def _run_episode(self) -> None:
        import shutil
        import tempfile

        from ouroboros.llm import add_usage
        from ouroboros.openai_chat_dispatch import (
            custom_validation_by_call_id,
            pop_custom_validation_receipts,
        )

        request, slot = self.assignment.request, self.assignment.slot
        root = str(request.session_root or "").strip()
        if not root:
            raise ReviewRouteUnavailable(
                "native tool-round slot has no session root: the surface must name "
                "the repository root the reviewer episode runs in",
                code="session_root_missing")
        chat = getattr(self.llm, "chat", None)
        if not callable(chat):
            raise ReviewRouteUnavailable(
                "native tool-round episode needs a synchronous chat transport",
                code="api_chat_unavailable")
        deadline_at = str(getattr(request, "deadline_at", "") or "")
        rounds_cap = review_native_max_rounds()
        transcript_cap = review_native_max_transcript_chars()
        scratch = tempfile.mkdtemp(prefix="ouro-native-review-")
        registry = None
        total_usage: Dict[str, Any] = {}
        transcript_chars = len(self.episode_prompt)
        final_answer: Optional[str] = None
        try:
            registry, schemas = self._inspection_registry(root, scratch)
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": _NATIVE_REVIEW_INSTRUCTIONS},
                {"role": "user", "content": self.episode_prompt},
            ]
            for round_idx in range(1, rounds_cap + 1):
                if owner_deadline_exhausted(
                    deadline_at=deadline_at, reserve_sec=get_finalization_grace_sec(),
                ):
                    raise _deadline_exhausted_error(
                        "owner deadline exhausted mid native review episode")
                # The bound is a SEND bound: it is enforced BEFORE every
                # provider call, because the transcript IS the growing context
                # the next send would carry. A final content-only answer that
                # lands past the number is accepted — no further send exists
                # for it to poison, and refusing a completed verdict would
                # protect nothing.
                if transcript_chars > transcript_cap:
                    raise ReviewRouteUnavailable(
                        f"native review episode transcript ({transcript_chars} chars) "
                        f"exceeded its bound ({transcript_cap}); the episode fails "
                        "closed — compaction would review a fabricated cut",
                        code="native_transcript_cap_exceeded")
                chat_kwargs: Dict[str, Any] = {
                    "messages": messages,
                    "model": slot.model,
                    "tools": schemas,
                    "tool_choice": "auto",
                    "reasoning_effort": slot.effort,
                    "max_tokens": int(request.max_tokens or slot.max_tokens),
                    "no_proxy": bool(request.no_proxy),
                    "use_local": bool(slot.use_local),
                    "cache_affinity": f"{request.surface}:{request.task_id or 'review'}",
                }
                if request.temperature is not None or slot.temperature is not None:
                    chat_kwargs["temperature"] = (
                        request.temperature if request.temperature is not None
                        else slot.temperature
                    )
                transport = review_transport_timeout(
                    slot.model, getattr(slot, "transport_timeout_sec", None), deadline_at,
                )
                if transport is not None:
                    chat_kwargs["timeout"] = transport
                with bind_api_review_paid_stamp(self.assignment.dispatch_stamp):
                    try:
                        msg, usage = chat(**chat_kwargs)
                    except BaseException as exc:
                        capture = getattr(exc, "physical_attempt_capture", None)
                        if str(getattr(capture, "state", "") or "") in POSITIVE_PHYSICAL_ATTEMPT_STATES:
                            invoke_review_paid_stamp(self.assignment.dispatch_stamp)
                        raise
                self._rounds_used = round_idx
                tool_calls = (msg.get("tool_calls") or []) if isinstance(msg, dict) else []
                usage = dict(usage or {})
                # Pop the wire-validation sidecar BEFORE accumulation, exactly
                # like the existing bounded loops — receipts are per-round
                # execution facts, not usage numbers.
                wire_validation = pop_custom_validation_receipts(usage, tool_calls)
                validation_by_id = custom_validation_by_call_id(wire_validation)
                add_usage(total_usage, usage)
                # add_usage accumulates only token/cost keys: carry the ledger
                # linkage and provenance facts the substrate's actor records
                # consume, or the episode rollup would echo the REQUESTED model
                # dressed as resolved and lose its physical attempt ids.
                for _attempt_id in (usage.get("ledger_attempt_ids") or []):
                    total_usage.setdefault("ledger_attempt_ids", []).append(_attempt_id)
                for _fact in ("resolved_model", "provider"):
                    if usage.get(_fact):
                        total_usage[_fact] = usage[_fact]
                content = str(msg.get("content") or "") if isinstance(msg, dict) else ""
                transcript_chars += len(content)
                if content and not tool_calls:
                    final_answer = content
                    break
                if not tool_calls:
                    # An empty round is the episode's honest end; the empty
                    # answer rides the ordinary empty-response rail upstream.
                    final_answer = content
                    break
                assistant = dict(msg)
                assistant.setdefault("role", "assistant")
                messages.append(assistant)
                for tc in tool_calls:
                    call_id = str(tc.get("id") or "")
                    function = tc.get("function") if isinstance(tc, dict) else {}
                    name = str((function or {}).get("name") or "")
                    raw_args = (function or {}).get("arguments")
                    args: Optional[Dict[str, Any]] = None
                    outcome = "executed"  # provenance from CONTROL FLOW, never string-sniffing
                    verdict = validation_by_id.get(call_id)
                    if verdict is not None and not getattr(verdict, "allows_execution", True):
                        outcome = "refused"
                        result = f"⚠️ TOOL_ARG_ERROR: {getattr(verdict, 'error', 'invalid arguments')}"
                    elif name not in _INSPECTION_TOOL_NAMES:
                        outcome = "refused"
                        result = (
                            f"⚠️ tool {name!r} is not available in this read-only "
                            "inspection episode"
                        )
                    else:
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                            if not isinstance(args, dict):
                                raise ValueError("arguments must be a JSON object")
                        except (TypeError, ValueError) as exc:
                            outcome = "refused"
                            args, result = None, f"⚠️ TOOL_ARG_ERROR: {exc}"
                        if isinstance(args, dict):
                            try:
                                result = str(registry.execute(name, args))
                            except Exception as exc:  # tool errors feed the model, not the rail
                                outcome = "error"
                                result = f"⚠️ {type(exc).__name__}: {exc}"
                            if len(result) > _EPISODE_TOOL_RESULT_CHAR_CAP:
                                # Disclosed bound with a continuation handle — the
                                # reader keeps reading in chunks (read_file supports
                                # offset/limit), so nothing is silently cut.
                                kept = result[:_EPISODE_TOOL_RESULT_CHAR_CAP]
                                result = (
                                    kept
                                    + f"\n⚠️ RESULT TRUNCATED: showed {_EPISODE_TOOL_RESULT_CHAR_CAP}"
                                    f" of {len(result)} chars. Continue reading the remainder"
                                    " in bounded chunks (read_file supports offset/limit)."
                                )
                    # Host-observed evidence (bounded): which artifacts THIS
                    # episode actually opened — disclosure, never a claim of
                    # full-surface coverage.
                    self._tool_calls_total += 1
                    if len(self._tool_receipts) < 200:
                        receipt: Dict[str, Any] = {"round": round_idx, "tool": name}
                        if isinstance(args, dict):
                            for key in ("path", "root", "query", "pattern"):
                                if args.get(key):
                                    receipt[key] = str(args[key])[:300]
                        receipt["result_chars"] = len(result)
                        # Honesty: a refused call must not read as an executed one.
                        receipt["outcome"] = outcome
                        self._tool_receipts.append(receipt)
                    transcript_chars += len(result)
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "content": result,
                    })
                if transcript_chars > transcript_cap:
                    raise ReviewRouteUnavailable(
                        f"native review episode transcript ({transcript_chars} chars) "
                        f"exceeded its bound ({transcript_cap}); the episode fails "
                        "closed — compaction would review a fabricated cut",
                        code="native_transcript_cap_exceeded")
            if final_answer is None:
                raise ReviewRouteUnavailable(
                    f"native review episode spent its round budget ({rounds_cap}) "
                    "without a final answer; the episode fails closed",
                    code="native_rounds_exhausted")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._raw_transcript = final_answer
        self._episode_usage = dict(total_usage)
        self._episode_usage.update({
            "provider": str(total_usage.get("provider") or ""),
            "resolved_model": str(total_usage.get("resolved_model") or slot.model),
            "native_rounds": self._rounds_used,
            "native_tool_calls": self._tool_calls_total,
            "native_tool_receipts": list(self._tool_receipts),
            # Provenance class of this delivery: the host SAW these reads.
            "host_file_read_attestation": "host_observed",
            "delivery": "native_tool_rounds",
        })

    def _verdict_result(self, force_extraction: bool = False) -> ReviewAttemptResult:
        text = self._raw_transcript or ""
        canonical, method, extraction_usage = canonicalize_session_verdict(
            text,
            conformance_passed=False,  # no structured-output channel on this route
            contract=self._output_contract(),
            llm=self.llm,
            deadline_at=getattr(self.assignment.request, "deadline_at", "") or "",
            transport_timeout_sec=getattr(self.assignment.slot, "transport_timeout_sec", None),
        )
        usage = dict(self._episode_usage)
        deltas: List[Dict[str, Any]] = []
        if method == "light_model_extraction":
            usage["extraction"] = extraction_usage
            deltas.append({
                "kind": "capability_delta",
                "requested": "contract-shaped verdict from the episode",
                "effective": "light-model extraction over the collected answer",
                "reason": "extraction_instead_of_contract",
            })
        elif method == "extraction_incomplete":
            deltas.append({
                "kind": "capability_delta",
                "requested": "contract-shaped verdict from the episode",
                "effective": (
                    f"no verdict: answer ({len(text)} chars) exceeds the "
                    "single-send extraction bound"
                ),
                "reason": "extraction_incomplete_transcript_exceeds_bound",
            })
        usage["verdict_method"] = method
        usage["verdict_provenance"] = {
            "raw_transcript_chars": len(text),
            "raw_transcript_sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "canonical_chars": len(canonical),
            "canonical_sha256": hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest(),
            "output_conformance": "",
            "conformance_trusted": False,
            "verdict_method": method,
            "raw_transcript_carrier": "message.native_transcript (durable response_ref)",
        }
        if deltas:
            usage["capability_delta"] = deltas
        message = {
            "content": canonical,
            "native_transcript": text,
            "verdict_method": method,
        }
        return ReviewAttemptResult(message=message, usage=usage, raw_text=canonical)
