"""Containment verification for ONE delegated run (extracted from tools/delegate.py
for the module-size gate — same logic, same names, no behavioural change).

The nanny asks the ENGINE's own artifacts what a delegated run actually ran under
— the applied access profile and the applied scoped HOME — and compares them with
what the host's authority entitled the child to. A recorded WIDER profile or an
unexcused operator-home landing is a containment breach the nanny cancels on; an
absent fact stays absence ("unproven"), reported by the evidence reader instead of
enforced as a fault.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ouroboros import delegate_custody as custody
from ouroboros.subagents import CLAUDEXOR_RETIRED
from ouroboros.utils import resolve_path_allow_missing

log = logging.getLogger(__name__)

_TERMINAL_STATES = custody.TERMINAL_STATES

# Ordering, not a set: honest verification needs to tell "narrower than asked" (fine)
# from "wider than asked" (a refusal). An unlisted profile ranks above everything
# known, so a profile added by a future engine is treated as widening, not ignored.
_ACCESS_RANK: Dict[str, int] = {
    "readonly": 0,
    "workspace_write": 1,
    "full": 2,
    "external_sandbox_full": 2,
    "inherit_native": 3,
}
_ACCESS_UNVERIFIED = "access_unverified"
_UNKNOWN_ACCESS_RANK = 99


def _resolved(path: Any) -> Optional[pathlib.Path]:
    try:
        return (
            resolve_path_allow_missing(pathlib.Path(str(path)))
            if str(path or "").strip()
            else None
        )
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _widened_access(detail: Dict[str, Any], expected: str) -> str:
    """The effective profile when the engine ran WIDER than the host asked, else ''.

    Claudexor derives effective access itself, so the request is a request. Honest
    verification means reading back what was enforced instead of trusting the echo; a
    narrower effective profile is fine, a wider one is a containment breach.
    """
    summary = custody.summary_of(detail)
    # `access` is NOT an independent witness: the daemon computes it as
    # `effectiveAccess ?? the client's own parsed request`, so falling back to it compares
    # our request against itself and always passes. Only the derived field testifies.
    effective = str(summary.get("effectiveAccess") or "")
    state = str(summary.get("state") or "")
    # The journal cursor is the honest "has this run produced anything yet" signal: a
    # freshly dequeued run has not written its contract, so it cannot have disclosed.
    # It lives on the run DETAIL, not on the summary — reading it from the summary made
    # this whole branch unreachable against the real wire shape while its test, whose
    # fixture put it in the summary, went on asserting the gate worked.
    try:
        seq_seen = int(detail.get("lastSeq") or 0) > 0
    except (TypeError, ValueError):
        seq_seen = False
    if not effective:
        # An UNDISCLOSED profile is not a verified narrow one — but absence only means
        # "no evidence" while the run can still act. The daemon marks a run `running` at
        # DEQUEUE, before the orchestrator writes the contract that derives the profile,
        # and a run that failed or was cancelled before that write never has one at all.
        # Treating those as breaches cancelled healthy runs and reported a failed start
        # as a containment fault. Judge only a run that is admitted, past its first
        # disclosure, and not already over.
        if state in ("", "queued") or state in _TERMINAL_STATES:
            return ""
        if not seq_seen:
            return ""
        return _ACCESS_UNVERIFIED
    if _ACCESS_RANK.get(effective, _UNKNOWN_ACCESS_RANK) > _ACCESS_RANK.get(expected, 0):
        return effective
    return ""


@dataclass(frozen=True)
class _Breach:
    """A containment guarantee the ENGINE did not deliver. Typed, with its evidence."""

    code: str
    detail: str
    facts: Dict[str, Any]


def _home_isolation_breach(detail: Dict[str, Any]) -> Optional[_Breach]:
    """Did the scoped harness HOME the host ASKED for actually get APPLIED?

    Asking is not evidence: ``execution.delegated`` is a request, and a request that
    the engine accepted and then did not honour leaves the harness holding the
    operator's real ``$HOME`` — with ``~/.claudexor/v3/daemon/token`` in it, which
    grants the entire ``/v2`` control API. Claudexor records the APPLIED fact on each
    attempt (``harness_home_isolated`` / ``harness_home_dir``) and projects it onto no
    ``/v2`` response, so the artifact is the only witness there is.

    THE RULE IS TWO EXACT FACTS (phase A3, Poltergeist sprint — the simplified
    form): a breach is ONLY a recorded ``harness_home_isolated: false``, or an
    applied home EQUAL to the operator's own (the claim is the lie, whatever
    boundary sits beside it). Everything else is reported, not enforced:

    - A MISSING fact stays absence. Current Claudexor spreads the applied facts
      into ``attemptFailureRecord`` too, but ``harness_home_isolated`` is the one
      optional member, omitted when the attempt died before its home was decided
      — "a01 errored, a02 repaired it" is the ordinary converge loop, and reading
      absence as a fault cancelled healthy finished runs. Unproven is REPORTED by
      ``_containment_evidence``, never enforced.
    - A scoped home NESTED under ``$HOME`` — with or without a recorded OS
      boundary — is NOT a breach. The engine roots every scoped home under its
      runtime dir, which lives under ``$HOME`` on every host it supports, and on
      hosts with no boundary mechanism (every non-macOS host today) it CANNOT
      record one, so the old nested-without-mechanism rule cancelled every
      mutating Linux run post-factum (the colleague's issue-2 class). The nested
      shape flows to the EXISTING disclosed-unconfined path instead
      (``delegate_run_unconfined`` + the three-place disclosure): the child
      already holds a shell in this worktree, and the marginal step from "shell"
      to "``~``-relative token reachability" does not justify cutting the lane on
      every boundary-less host (AGENTS.md "Disclose instead of forbid";
      ``confinement_unavailable_reason`` from the same attempt
      artifact amplifies that disclosure — telemetry, never an admission token.

    THE READER RETIRED WITH CLAUDEXOR: the applied facts live only in the engine's
    own run tree (``attempt_containment`` / ``operator_home``), so no attempt record
    is readable any more. This reports the same honest ``None`` an ABSENT fact always
    produced — absence stays absence, never a fabricated breach.
    """
    log.debug("home-isolation verification unavailable: %s", CLAUDEXOR_RETIRED)
    return None


def home_nested_under_operator_home(detail: Dict[str, Any]) -> bool:
    """Did any attempt's applied scoped HOME land INSIDE the operator's own home?

    NOT a breach (see ``_home_isolation_breach``) — the engine roots every scoped
    home under its runtime dir, which lives under ``$HOME``. It is a REPORTING
    fact, and one the evidence reader must never drop: a nested home leaves
    ``~/.claudexor/v3/daemon/token`` reachable by absolute path, so a run that
    also recorded an OS boundary was being promoted to ``verified: true`` with a
    note claiming a home "outside the operator's own" — and, because the durable
    unconfined row is only emitted for runs with no boundary, the disclosure
    disappeared entirely. Absence of the fact stays absence: a run that recorded
    no home is unproven, not nested.

    THE READER RETIRED WITH CLAUDEXOR (``attempt_containment`` / ``operator_home``):
    no attempt record is readable any more, so this answers the same ``False`` an
    unrecorded home always did — unproven, never asserted as nested.
    """
    log.debug("nested-home reporting unavailable: %s", CLAUDEXOR_RETIRED)
    return False

