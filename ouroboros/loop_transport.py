"""Transport-outage wait episodes and provider-failure terminal text for the main loop.

A REMOTE pre-dispatch transport failure (typed ``released`` custody: connect
refused/timed out before request bytes left this host, $0 in the ledger) is not a
model failure. Instead of burning the fallback chain or terminalizing, the round
gate in ``loop.py`` latches a :class:`TransportWaitEpisode` and waits: durable
``network_wait`` events + owner progress notes, an interruptible backoff sleep,
then a free redial of the SAME round (the round budget is not consumed). The wait
is bounded only by the task's existing rails — owner deadline, budget, Stop, and
the supervisor's absolute ceiling — never by a new limit. When the rails run out,
``_handle_provider_unavailable`` takes a deterministic no-resend terminal keyed on
the episode's ``wait_cause`` (no forced-final provider call).

Also hosts the owner-facing provider-failure text helpers and terminal salvage
readers used by that terminal path (extracted from ``loop.py``, which is at its
size-ratchet byte cap).
"""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import (
    NETWORK_WAIT_NOTE_INTERVAL_SEC,
    get_finalization_grace_sec,
    get_task_idle_timeout_sec,
)
from ouroboros.deadline_utils import dispatch_window_remaining_sec, parse_deadline_ts
from ouroboros.loop_llm_call import _TRANSIENT_BACKOFF_CAP_SEC
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

# First free-redial pause; doubles per wait iteration up to the existing
# transient backoff cap (Q10: the 60s cap is an existing bound, not a new knob).
_WAIT_BACKOFF_START_SEC = 4.0


@dataclass
class TransportWaitEpisode:
    """Episode-local latch for one remote pre-dispatch transport outage.

    The latch — not the mutable ``_last_llm_error_kind`` projection — carries the
    terminal cause: later failures (a failed local fallback pass, the deadline
    admission gate) overwrite the usage projection, and the terminal decision
    must stay deterministic (no forced-final resend after a waited-out outage).
    """

    wait_cause: str = "transport_unavailable"
    started_monotonic: float = 0.0
    wait_eligible: bool = True
    redials: int = 0
    wait_iterations: int = 0
    last_note_monotonic: float = 0.0
    local_pass_used: bool = False
    final_redial_done: bool = False


def emit_network_wait_event(
    drive_logs: pathlib.Path,
    *,
    task_id: str,
    phase: str,
    elapsed_sec: float,
    redials: int,
    model: str,
    next_sleep_sec: Optional[float] = None,
    detail: str = "",
) -> None:
    """Durable episode evidence in events.jsonl (typed rows; no keyword scans)."""
    try:
        append_jsonl(pathlib.Path(drive_logs) / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "network_wait",
            "task_id": task_id,
            "phase": phase,
            "elapsed_sec": round(float(elapsed_sec), 1),
            "redials": int(redials),
            "model": model,
            "next_sleep_sec": (
                round(float(next_sleep_sec), 1) if next_sleep_sec is not None else None
            ),
            **({"detail": detail} if detail else {}),
        })
    except Exception:
        log.debug("Failed to append network_wait event", exc_info=True)


def _use_local_fallback_configured() -> bool:
    return os.environ.get("USE_LOCAL_FALLBACK", "").lower() in ("true", "1")


def fallback_chain_allowed(ctx: Any, last_error_kind: str, episode: Optional[TransportWaitEpisode]) -> bool:
    """Whether this round may walk the cross-model fallback chain."""
    if bool(getattr(ctx, "exact_model_route", False)):
        return False
    if episode is not None:
        # Q4: during a remote transport outage the chain runs at most ONCE per
        # episode, and only when USE_LOCAL_FALLBACK makes the whole chain local —
        # remote candidates never dial over a proven dead egress.
        if (
            last_error_kind != "transport_unavailable"
            or episode.local_pass_used
            or not _use_local_fallback_configured()
        ):
            return False
        episode.local_pass_used = True
        return True
    return last_error_kind not in (
        "context_overflow", "provider_outcome_unknown", "deadline_exhausted",
    )


def reconcile_transport_wait(
    episode: Optional[TransportWaitEpisode],
    ctx: Any,
    *,
    msg_present: bool,
    error_kind: str,
    drive_logs: pathlib.Path,
    task_id: str,
    model: str,
    emit_progress: Callable[[str], None],
    after_local_pass: bool = False,
) -> Optional[TransportWaitEpisode]:
    """Reconcile the episode latch with one dispatch outcome.

    Enters a new episode on a fresh ``transport_unavailable`` failure (durable
    ``entered`` event; the first owner note fires immediately for wait-eligible
    turns). On a redial outcome: a response ends the episode as ``recovered``
    (mandatory owner note), a NON-transport failure ends it as evidence the
    transport is passable again, while ``transport_unavailable`` and a
    pre-dispatch deadline refusal keep the latch for the wait/terminal step.
    A failed local fallback pass never clears the latched remote cause.
    """
    if episode is None:
        if msg_present or error_kind != "transport_unavailable":
            return None
        episode = TransportWaitEpisode(
            started_monotonic=time.monotonic(),
            wait_eligible=(
                not bool(getattr(ctx, "is_direct_chat", False))
                and not bool(getattr(ctx, "is_ephemeral_turn", False))
            ),
        )
        emit_network_wait_event(
            drive_logs, task_id=task_id, phase="entered",
            elapsed_sec=0.0, redials=0, model=model,
        )
        if episode.wait_eligible:
            episode.last_note_monotonic = time.monotonic()
            emit_progress(
                "🌐 Could not establish a provider connection — waiting and "
                "redialing automatically (failed attempts are $0). Stop cancels."
            )
        return episode
    elapsed = time.monotonic() - episode.started_monotonic
    if msg_present:
        if after_local_pass:
            emit_network_wait_event(
                drive_logs, task_id=task_id, phase="ended", elapsed_sec=elapsed,
                redials=episode.redials, model=model, detail="local_fallback_adopted",
            )
        else:
            emit_network_wait_event(
                drive_logs, task_id=task_id, phase="recovered", elapsed_sec=elapsed,
                redials=episode.redials, model=model,
            )
            emit_progress(
                f"🌐 Provider connection restored after {elapsed / 60.0:.1f} min — resuming."
            )
        return None
    if (
        not after_local_pass
        and error_kind not in ("transport_unavailable", "deadline_exhausted")
    ):
        # The redial got past the connect phase and failed differently: the
        # transport is provably passable, so ordinary failure policy resumes.
        emit_network_wait_event(
            drive_logs, task_id=task_id, phase="ended", elapsed_sec=elapsed,
            redials=episode.redials, model=model,
            detail=f"error_kind_changed:{error_kind}",
        )
        return None
    return episode


def interruptible_wait_sleep(seconds: float, wake_check: Callable[[], bool]) -> bool:
    """Sleep up to ``seconds`` in <=1s slices.

    Returns True the moment ``wake_check`` reports a pending owner signal — the
    caller re-enters the round top, whose ordinary drain delivers the message or
    control (finalize_now/hurry/dialogue) — and False after the full sleep.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        try:
            if wake_check():
                return True
        except Exception:
            log.debug("wake check failed during transport wait", exc_info=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))


def _owner_signal_pending(
    incoming_messages: Optional[queue.Queue],
    drive_root: Optional[pathlib.Path],
    task_id: str,
    owner_msg_seen: Optional[set],
    attempt: Any,
) -> bool:
    """Non-destructive peek: is an owner message or typed control waiting?"""
    if incoming_messages is not None and not incoming_messages.empty():
        return True
    if drive_root is None or not task_id:
        return False
    try:
        from ouroboros.owner_mailbox import drain_owner_entries

        # A COPY of the seen-set: this is a peek — the round top performs the
        # real drain, delivery, and acknowledgement.
        return bool(drain_owner_entries(
            pathlib.Path(drive_root), task_id, set(owner_msg_seen or ()), attempt,
        ))
    except Exception:
        log.debug("owner-signal peek failed during transport wait", exc_info=True)
        return False


def transport_wait_step(
    episode: TransportWaitEpisode,
    *,
    tools: Any,
    error_kind: str,
    drive_root: Optional[pathlib.Path],
    drive_logs: pathlib.Path,
    task_id: str,
    model: str,
    emit_progress: Callable[[str], None],
    incoming_messages: Optional[queue.Queue],
    owner_msg_seen: Optional[set],
) -> bool:
    """One wait iteration of an active episode.

    Returns True to redial (the caller re-enters the round top WITHOUT consuming
    a round) and False to terminalize via the no-resend branch. The wait is
    bounded by the owner deadline minus the existing dispatch-admission reserve
    (so a granted redial actually dials); the acceptance-review percentage
    reserve is deliberately NOT a wait ceiling (Q18), and the supervisor's
    absolute 6h ceiling stays an external rail, not duplicated here.
    """
    elapsed = time.monotonic() - episode.started_monotonic

    def _ended(detail: str) -> bool:
        emit_network_wait_event(
            drive_logs, task_id=task_id, phase="ended", elapsed_sec=elapsed,
            redials=episode.redials, model=model, detail=detail,
        )
        return False

    if not episode.wait_eligible:
        # Direct/ephemeral chat turns keep the responsive lane responsive:
        # fast honest failure; the owner retries when connectivity returns (Q12).
        return _ended("wait_ineligible_turn")
    if error_kind == "deadline_exhausted":
        # The redial was refused before dispatch: the owner window is spent.
        return _ended("deadline_refused_dispatch")
    if episode.final_redial_done:
        return _ended("deadline_after_final_redial")
    remaining = dispatch_window_remaining_sec(
        deadline_ts=task_deadline_epoch(tools),
        reserve_sec=get_finalization_grace_sec(),
    )
    if remaining is not None and remaining <= 0:
        return _ended("deadline_exhausted")
    backoff = min(
        _WAIT_BACKOFF_START_SEC * (2.0 ** min(episode.wait_iterations, 4)),
        _TRANSIENT_BACKOFF_CAP_SEC,
    )
    note_interval = max(
        1.0,
        min(float(NETWORK_WAIT_NOTE_INTERVAL_SEC), get_task_idle_timeout_sec() / 2.0),
    )
    # The sleep never exceeds the note interval, so waiting notes keep the idle
    # rail alive even on owner-lowered idle timeouts.
    sleep_sec = min(backoff, note_interval)
    if remaining is not None and remaining < sleep_sec + 1.0:
        # One last free redial just before the admission window closes (Q14).
        sleep_sec = max(0.0, remaining - 1.0)
        episode.final_redial_done = True
    if time.monotonic() - episode.last_note_monotonic >= note_interval:
        episode.last_note_monotonic = time.monotonic()
        emit_progress(
            f"🌐 Still waiting for a provider connection — {elapsed / 60.0:.0f} min "
            f"elapsed, {episode.redials} redials; will resume automatically."
        )
    emit_network_wait_event(
        drive_logs, task_id=task_id, phase="waiting", elapsed_sec=elapsed,
        redials=episode.redials, model=model, next_sleep_sec=sleep_sec,
    )
    episode.wait_iterations += 1
    interruptible_wait_sleep(
        sleep_sec,
        lambda: _owner_signal_pending(
            incoming_messages, drive_root, task_id, owner_msg_seen,
            getattr(getattr(tools, "_ctx", None), "task_attempt", None),
        ),
    )
    episode.redials += 1
    return True


def task_deadline_epoch(tools: Any) -> Optional[float]:
    """Return the task deadline for retry backoff."""
    meta = getattr(tools._ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return None
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    return deadline.timestamp() if deadline is not None else None


def last_assistant_text(messages: List[Dict[str, Any]]) -> str:
    """Last real assistant text already produced this task — salvaged into the
    terminal answer when provider-death prevents a fresh final response, so
    useful work is never silently discarded (workspace files persist on disk
    regardless)."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def provider_failure_hint(accumulated_usage: Dict[str, Any]) -> str:
    detail = " ".join(str(accumulated_usage.get("_last_llm_error") or "").split()).strip()
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def provider_recovery_hint(accumulated_usage: Dict[str, Any]) -> str:
    """Explain whether retrying later is likely to help."""
    kind = str(accumulated_usage.get("_last_llm_error_kind") or "").strip()
    if kind == "provider_outcome_unknown":
        return (
            " The dispatched request has no terminal provider outcome, so no "
            "retry or paid fallback was sent; either could duplicate live work."
        )
    if kind == "transport_unavailable":
        return (
            " No provider connection could be established (typed pre-dispatch "
            "failure, $0 spent); the exact exception class is in the durable "
            "llm_api_error event. Retrying when connectivity returns will help."
        )
    if kind == "subscription_window_exhausted":
        reset_at = str(accumulated_usage.get("_last_llm_reset_at") or "").strip()
        when = f" It resets at {reset_at}." if reset_at else ""
        return (
            " The subscription window for the delegated route is spent. This is "
            f"TRANSIENT, not a billing refusal — waiting cures it.{when} Retrying is "
            "scheduled against that reset time, not the ordinary short backoff."
        )
    if kind in {"quota_exhausted", "auth_error", "request_too_large", "bad_request", "context_overflow"}:
        guidance = {
            "quota_exhausted": "The provider rejected the request for quota/billing reasons; retrying the same request will not help until the key/account limit changes.",
            "auth_error": "The provider rejected authentication/authorization; retrying the same request will not help until the configured key or provider access is fixed.",
            "request_too_large": "The provider rejected the request size/output-token shape; retrying the same request will not help without reducing context/output demand or changing model capacity.",
            "bad_request": "The provider rejected the request shape; retrying the same request will not help until the transcript/tool payload is fixed.",
            "context_overflow": "The context overflowed the model window; retrying the same request will not help without reducing context or changing model capacity.",
        }.get(kind, "Retrying the same provider request will not help until the underlying request/account issue changes.")
        return f" {guidance}"
    detail = str(accumulated_usage.get("_last_llm_error") or "").lower()
    if "prefill" in detail or "conversation must end with a user message" in detail:
        return (
            " This looks like a client-side transcript-shape error, not a "
            "provider outage; retrying the same input will not help."
        )
    if "provider returned incomplete response" in detail or "finish_reason=null" in detail:
        return (
            " The provider returned incomplete responses repeatedly; this may "
            "be transient, but it can also indicate malformed client input."
        )
    return " If background consciousness is running, it will retry when the provider recovers."
