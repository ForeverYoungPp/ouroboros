"""Reviewer dispatch primitives: the row-identity mint (moved whole from
``review_substrate.py`` at the module-size gate, same split shape as
``skill_review_cycles.py``) and the write-ahead PAID stamp seam (owner
Q16/Q17; Max-Review-Cycles fix round).

The ``paid`` fact of Max-Review-Cycles accounting is recorded at PHYSICAL
dispatch: a gate that must durably record "this wave spent reviewer money"
installs a :class:`ReviewPaidStamp` on ``ctx._review_paid_stamp`` for the
duration of its wave. The coordinator captures that exact once-only object,
and each route executor invokes it at its physical point of no return.
Assembly-only refusals (triad fit ladder, scope pack
signals, skill prompt building) exit before the seam, so a $0 attempt stays
outside every ceiling; a crash after dispatch keeps the durable paid fact
(write-ahead). This seam is also where the L-review lane's two-phase
admission slots in at synthesis.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

# Identity prefixes for the configured reviewer surfaces. A surface that fans
# rows out registers its prefix here rather than spelling one inline, so
# ``slot_id_for_row`` stays the only place a row id is built.
SLOT_ID_PREFIX = "slot"
SCOPE_SLOT_ID_PREFIX = "scope_slot"
PLAN_SLOT_ID_PREFIX = "plan_slot"


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


def stamp_review_paid_on_dispatch(ctx: Any) -> None:
    """Invoke the caller-installed stamp; retained for legacy/test callers."""
    invoke_review_paid_stamp(
        getattr(ctx, "_review_paid_stamp", None) if ctx is not None else None
    )
