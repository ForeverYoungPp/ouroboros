"""Reviewer dispatch primitives: the row-identity mint (moved whole from
``review_substrate.py`` at the module-size gate, same split shape as
``skill_review_cycles.py``) and the write-ahead PAID stamp seam (owner
Q16/Q17; Max-Review-Cycles fix round).

The ``paid`` fact of Max-Review-Cycles accounting is recorded at PHYSICAL
dispatch: a gate that must durably record "this wave spent reviewer money"
installs a :class:`ReviewPaidStamp` on ``ctx._review_paid_stamp`` for the
duration of its wave. The coordinator captures that exact once-only object;
session routes invoke it at their physical point of no return, while API routes
bind it for the canonical physical-attempt boundary to invoke.
Assembly-only refusals (triad fit ladder, scope pack
signals, skill prompt building) exit before the seam, so a $0 attempt stays
outside every ceiling; a crash after dispatch keeps the durable paid fact
(write-ahead). This seam is also where the L-review lane's two-phase
admission slots in at synthesis.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import pathlib
import threading
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)
_BOUND_API_PAID_STAMP: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "ouroboros_review_api_paid_stamp", default=None,
)

# Identity prefixes for the configured reviewer surfaces. A surface that fans
# rows out registers its prefix here rather than spelling one inline, so
# ``slot_id_for_row`` stays the only place a row id is built.
SLOT_ID_PREFIX = "slot"
SCOPE_SLOT_ID_PREFIX = "scope_slot"
PLAN_SLOT_ID_PREFIX = "plan_slot"


def task_acceptance_zero_physical_refusal(evidence: Any) -> dict[str, str]:
    """Describe an acceptance refusal that needs no reviewer transport."""
    packet = evidence if isinstance(evidence, dict) else {}
    if packet.get("__unresolved_partial_artifacts__"):
        return {
            "status": "degraded_partial_source",
            "summary": (
                "A decision-bearing tool result remains partial or its exact source "
                "is unavailable; acceptance cannot treat that projection as complete."
            ),
        }
    if packet.get("__immutable_core_overflow__"):
        return {
            "status": "degraded_core_overflow",
            "summary": (
                "Immutable owner requirements do not fit the acceptance evidence "
                "budget; no requirement was silently truncated."
            ),
        }
    return {}


def run_zero_physical_task_acceptance(
    request: Any, slots: Any, *, drive_root: Any, usage_ctx: Any,
) -> Any:
    """Return the substrate's synthetic refusal, or ``None`` for physical work."""
    if not task_acceptance_zero_physical_refusal(request.evidence):
        return None
    from ouroboros.review_substrate import run_review_request

    return run_review_request(
        request, slots=slots, drive_root=pathlib.Path(drive_root), usage_ctx=usage_ctx,
    )


def claim_task_acceptance_dispatch(
    drive_root: Any,
    root_task_id: str,
    task_id: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Atomically claim the canonical wallet immediately before dispatch."""
    from ouroboros.task_results import claim_task_acceptance_review_cycle

    return claim_task_acceptance_review_cycle(
        drive_root, root_task_id, binding, claimed_by_task_id=task_id,
    )


def task_acceptance_preclaim_refusal(ctx: Any) -> Any:
    """Recheck the deadline rail after assembly and before a paid wallet claim."""
    from ouroboros import task_pacing
    from ouroboros.review_substrate import ReviewRunResult

    budget = task_pacing.build_budget_snapshot(
        ctx.tools._ctx, profile=ctx.budget_profile,
    )
    allowed, reason = task_pacing.review_launch_allowed(
        budget,
        estimated_sec=task_pacing.acceptance_review_estimate_sec(
            ctx.tools._ctx, passes_done=ctx.passes_done,
        ),
    )
    if allowed:
        return None
    return ReviewRunResult(
        request={"surface": "task_acceptance", "task_id": str(ctx.task_id)},
        actors=[], parsed_findings=[], aggregate_signal="DEGRADED", degraded=True,
        degraded_reasons=[f"{reason} (no reviewer was called)"],
    )


def slot_id_for_row(index: int, *, prefix: str = SLOT_ID_PREFIX) -> str:
    """Identity of the ``index``-th (1-based) configured reviewer row.

    The single mint for reviewer-slot identity, and the reason the substrate
    contract says slot identity is separate from model identity. Naming a row
    after its own model instead collides two rows that share a model (a supported
    configuration — ``get_scope_review_models`` preserves duplicates on purpose),
    collides two model spellings that sanitize alike (``openai::gpt-5`` and
    ``openai/gpt/5``), and moves a row's identity the moment the owner edits its
    model, so the row's receipts stop lining up with its own history. The model,
    the route and the effort are PROPERTIES of a row, never its name.
    """
    return f"{prefix}_{int(index)}"


class ReviewPaidStamp:
    """Idempotent, thread-safe once-only wrapper around one durable write.

    Parallel dispatch means two sides can race to be "the first transport
    call" (the commit gate dispatches triad and scope concurrently): the first
    caller performs the durable write-ahead, later callers block on the lock
    until it lands and then no-op — so EVERY side is guaranteed the paid fact
    is durable before its own transport begins. A failing write is not
    retried and still marks the stamp fired: the terminal record is the
    primary ledger, and this writer is fail-open cost accounting, never a
    safety gate.
    """

    def __init__(self, write: Callable[[], None]) -> None:
        self._write = write
        self._lock = threading.Lock()
        self.fired = False

    def __call__(self) -> None:
        with self._lock:
            if self.fired:
                return
            try:
                self._write()
            finally:
                self.fired = True


def invoke_review_paid_stamp(stamp: Any) -> None:
    """Invoke one captured write-ahead stamp, fail-open."""
    if not callable(stamp):
        return
    try:
        stamp()
    except Exception:
        log.debug("review paid dispatch stamp failed (fail-open)", exc_info=True)


@contextlib.contextmanager
def bind_api_review_paid_stamp(stamp: Any) -> Iterator[None]:
    """Bind one API review stamp until a canonical physical dispatch occurs."""
    token = _BOUND_API_PAID_STAMP.set(stamp)
    try:
        yield
    finally:
        _BOUND_API_PAID_STAMP.reset(token)


def invoke_bound_api_review_paid_stamp() -> None:
    """Mark the bound API review paid, if any; always fail-open."""
    invoke_review_paid_stamp(_BOUND_API_PAID_STAMP.get())


def stamp_review_paid_on_dispatch(ctx: Any) -> None:
    """Invoke the caller-installed stamp; retained for legacy/test callers."""
    invoke_review_paid_stamp(
        getattr(ctx, "_review_paid_stamp", None) if ctx is not None else None
    )
