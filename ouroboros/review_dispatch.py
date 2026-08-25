"""Reviewer dispatch primitives: the row-identity mint (moved whole from
``review_substrate.py`` at the module-size gate, same split shape as
``skill_review_cycles.py``) and the write-ahead PAID stamp seam (owner
Q16/Q17; Max-Review-Cycles fix round).

The ``paid`` fact of Max-Review-Cycles accounting is recorded at PHYSICAL
dispatch: a gate that must durably record "this wave spent reviewer money"
installs a :class:`ReviewPaidStamp` on ``ctx._review_paid_stamp`` for the
duration of its wave, and the shared reviewer transport entry
(``review_custody.run_custodied_review_slots``) invokes it after slot resolution and
immediately before worker fan-out. The coordinator also captures that exact
once-only object: session routes invoke it before their replayable
``START_REQUESTED`` row, while API routes bind it for the canonical physical-
attempt boundary. Assembly-only refusals (triad fit ladder, scope pack signals,
skill prompt building) exit before the seam, so a $0 attempt stays outside
every ceiling; a worker that outlives its logical caller cannot race the
write-ahead fact, and a crash after dispatch keeps the durable paid fact.
Commit review verifies this write fail-closed; other callers retain historical
fail-open accounting. This seam also hosts the L-review lane's two-phase admission.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
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
    until it lands and then no-op. The default consumes a failed write and
    remains fail-open for existing accounting callers. ``fail_closed=True``
    propagates the error and leaves the stamp unfired, so a paid transport may
    start only after a later idempotent write succeeds.
    """

    def __init__(self, write: Callable[[], None], *, fail_closed: bool = False) -> None:
        self._write = write
        self._lock = threading.Lock()
        self.fail_closed = bool(fail_closed)
        self.fired = False

    def __call__(self) -> None:
        with self._lock:
            if self.fired:
                return
            try:
                self._write()
            except Exception:
                if self.fail_closed:
                    raise
                self.fired = True
                raise
            self.fired = True


def invoke_review_paid_stamp(stamp: Any) -> None:
    """Invoke one captured write-ahead stamp under its selected failure policy."""
    if not callable(stamp):
        return
    try:
        stamp()
    except Exception:
        if bool(getattr(stamp, "fail_closed", False)):
            raise
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
    """Invoke the caller-installed stamp at the shared dispatch boundary."""
    invoke_review_paid_stamp(
        getattr(ctx, "_review_paid_stamp", None) if ctx is not None else None
    )
