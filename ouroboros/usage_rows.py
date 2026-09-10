"""Consolidated usage-row substrate: response normalization, row-math
projections, and the in-process read/render caches.

Merged verbatim from ``_usage_response.py``, ``_usage_rows.py``, and
``_usage_rows_memo.py`` so the retained accounting layer (``usage_accounting``,
``skill_review_usage``, and the row-math consumers) no longer imports the
underscore-prefixed modules slated for whole-module deletion. Every name keeps
its exact semantics; the historical import and monkeypatch sites keep working
unchanged through ``usage_accounting``'s re-exports, and the substrate
(``usage_ledger``) stays cache-ignorant — this module lives beside it, never
inside it.

The two ``_number`` spellings are reconciled here: the ledger's ``_number``
(row-math / cost parsing over durable rows) is imported for ``_summary`` and
``_breakdown_bucket``, while the provider-response cost-trust predicate keeps
its public name ``provider_cost_value`` (``usage_from_response`` calls it
directly; ``_usage_response``'s ``_number = provider_cost_value`` alias was
internal-only and is dropped).
"""

from __future__ import annotations

import collections
import copy
import logging
import math
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ouroboros.usage_ledger import QUARANTINE_REL, LedgerResumeState, _number

log = logging.getLogger(__name__)

REVIEW_ATTRIBUTION_KEYS = ("review_skill", "review_wave_id", "review_slot_id")


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return value


def provider_cost_value(value: Any) -> Optional[float]:
    """Parse a provider-reported cost; ``None`` means the value cannot be trusted.

    THE cost-trust predicate, defined once at the AUTHORITATIVE boundary (what
    survives here is what the durable attempt ledger settles as final money) and
    imported by the loop-side projection, so the two lanes cannot fork: ``bool``
    is rejected FIRST (``float(True)`` is a plausible-looking 1.0 that would
    settle as a FINAL $1.00 and eat real budget admission), then anything
    unparseable, non-finite, or negative — ``OverflowError`` included, because
    ``float(10**1000)`` raises rather than returning inf. A reported ``0.0``
    stays a legitimate zero. Rejecting settles the attempt as unknown rather
    than as a fabricated amount (BIBLE P1). Never raises.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _reported_token_count(usage: Dict[str, Any], *keys: str) -> Optional[int]:
    """Return the first reported count; absence stays distinct from explicit zero."""
    for key in keys:
        if key in usage and usage.get(key) is not None:
            return max(0, int(usage[key]))
    return None


def usage_from_response(response: Any) -> Tuple[Dict[str, Any], Optional[float], bool]:
    """Extract common usage/cost facts without retaining response text."""
    payload: Any = _plain(response)
    if not isinstance(payload, dict) and callable(getattr(response, "json", None)):
        try:
            payload = response.json()
        except Exception:
            payload = None
    usage: Any = payload.get("usage") if isinstance(payload, dict) else getattr(response, "usage", None)
    usage = _plain(usage)
    if not isinstance(usage, dict):
        usage = {}
    native_cache_read = _reported_token_count(usage, "cache_read_input_tokens")
    native_cache_write = _reported_token_count(usage, "cache_creation_input_tokens")
    cache_read = _reported_token_count(
        usage, "cache_read_input_tokens", "cached_tokens", "precached_prompt_tokens",
    )
    cache_write = _reported_token_count(
        usage, "cache_creation_input_tokens", "cache_write_tokens",
    )
    input_tokens = _reported_token_count(usage, "input_tokens")
    prompt = _reported_token_count(usage, "prompt_tokens")
    if prompt is None and any(
        value is not None for value in (input_tokens, native_cache_read, native_cache_write)
    ):
        # Anthropic native input_tokens excludes cache reads and writes.
        prompt = int(input_tokens or 0) + int(native_cache_read or 0) + int(native_cache_write or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict):
        detail_read = _reported_token_count(details, "cached_tokens")
        cache_read = detail_read if detail_read is not None else cache_read
        detail_write = _reported_token_count(
            details, "cache_write_tokens", "cache_creation_tokens", "cache_creation_input_tokens",
        )
        cache_write = detail_write if detail_write is not None else cache_write
    normalized = {
        **usage,
        "prompt_tokens": prompt,
        "completion_tokens": _reported_token_count(usage, "completion_tokens", "output_tokens"),
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        split = {
            tier: int(creation.get(key) or 0)
            for tier, key in (("5m", "ephemeral_5m_input_tokens"),
                              ("1h", "ephemeral_1h_input_tokens"))
            if int(creation.get(key) or 0) > 0
        }
        if split:
            normalized["cache_write_tokens_by_ttl"] = split
    completion = normalized["completion_tokens"]
    cache_usage_reported = bool(
        (cache_read or 0)
        or (cache_write or 0)
        or any((normalized.get("cache_write_tokens_by_ttl") or {}).values())
    )
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("error"), dict)
        and not (prompt or 0)
        and not (completion or 0)
        and not cache_usage_reported
    ):
        normalized.update(prompt_tokens=0, completion_tokens=0, cached_tokens=0, cache_write_tokens=0)
        return normalized, 0.0, True
    candidates = (
        usage.get("cost"), usage.get("total_cost"),
        payload.get("total_cost_usd") if isinstance(payload, dict) else None,
        getattr(response, "total_cost_usd", None),
    )
    cost = next(
        (number for value in candidates if (number := provider_cost_value(value)) is not None),
        None,
    )
    return normalized, cost, cost is not None


def _summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    settled = confirmed = estimated = reserved = unresolved = 0.0
    unknown = 0
    # Finality is a COUNT of OPEN ROWS, not a truthiness test on dollar sums. Three of the
    # four old terms asked a STATE question of a float, so any row that is genuinely open
    # while holding $0.00 disappeared from the predicate entirely:
    #   * an ESTIMATED $0.00 — what the engine reports for a delegated subscription run
    #     whose cash it has not settled (all 8 estimated rows on a live 60-run page); and
    #   * a DISPATCHED row whose reservation is exactly 0.0, which `_reservation_cost`
    #     returns for `provider="local"`, so the projection claimed `cost_final: True`
    #     with a physical send still in flight.
    # A row is final when it is SETTLED at a known price its writer called final; anything
    # else is open, however little it costs. `_final_rows` keys by attempt_id, so a settled
    # row REPLACES its own reserved/dispatched predecessor — a row still open here is
    # really still open, and a released reservation is in neither branch.
    #
    # The count is also the DISCLOSED CAUSE, returned as `non_final_rows`: a projection
    # reporting `cost_final: false` with every dollar bucket at zero and `unknown` at zero
    # is a flag no reader can reconstruct.
    non_final_rows = 0
    counts: Dict[str, int] = {}
    # Separate "sessions and quota" axis: subscription work is already paid for, so
    # it contributes exactly $0 to money, and its real scarce resource (sessions and
    # the window that grants them) is counted here instead of being faked as cash.
    sessions = 0
    session_windows: Dict[str, str] = {}
    for row in rows:
        state = str(row.get("state") or "")
        if str(row.get("kind") or "") == "subscription_session":
            sessions += 1
            route = str(row.get("subscription_route") or "")
            reset_at = str(row.get("subscription_reset_at") or "")
            if route and reset_at:
                session_windows[route] = max(session_windows.get(route, ""), reset_at)
        if str(row.get("kind") or "") == "legacy_metadata":
            ambiguous = max(1, int(row.get("ambiguous_call_count") or 1))
            counts["metadata_only"] = counts.get("metadata_only", 0) + ambiguous
            continue
        counts[state] = counts.get(state, 0) + 1
        pricing_unknown = row.get("pricing_known") is False
        if state == "settled":
            cost = _number(row.get("cost_usd"))
            if cost is None:
                unknown += 1
                non_final_rows += 1
                bound = _number(row.get("reservation_upper_bound_usd"))
                if bound is not None:
                    unresolved += bound
            else:
                settled += cost
                if bool(row.get("cost_final")):
                    confirmed += cost
                else:
                    estimated += cost
                    non_final_rows += 1
        elif state == "reserved":
            non_final_rows += 1
            bound = _number(row.get("reservation_upper_bound_usd"))
            if bound is None or pricing_unknown:
                unknown += 1
            if bound is not None:
                reserved += bound
        elif state in {"dispatched", "unresolved"}:
            non_final_rows += 1
            bound = _number(row.get("reservation_upper_bound_usd"))
            if bound is None or pricing_unknown:
                unknown += 1
            if bound is not None:
                unresolved += bound
    settled, confirmed, estimated, reserved, unresolved = (
        round(value, 6) for value in (settled, confirmed, estimated, reserved, unresolved)
    )
    return {
        "settled_usd": settled,
        "confirmed_usd": confirmed,
        "estimated_usd": estimated,
        "reserved_usd": reserved,
        "unresolved_upper_bound_usd": unresolved,
        "accounted_usd": round(settled + reserved + unresolved, 6),
        "unknown_unmetered": unknown,
        # Every row that increments `unknown` is open, so the old `not unknown` term is
        # subsumed here rather than dropped.
        "non_final_rows": non_final_rows,
        "cost_final": not non_final_rows,
        "attempt_counts": counts,
        "subscription_sessions": sessions,
        "subscription_windows": session_windows,
    }


def _with_limit(summary: Dict[str, Any], limit: Optional[float]) -> Dict[str, Any]:
    if limit is None:
        return summary
    summary["limit_usd"] = round(max(0.0, float(limit)), 6)
    summary["remaining_known_usd"] = round(max(0.0, summary["limit_usd"] - float(summary["accounted_usd"])), 6)
    return summary


def _with_integrity(summary: Dict[str, Any], degraded: bool) -> Dict[str, Any]:
    """Attach ledger integrity and prevent a torn tail from claiming final cost."""
    summary["integrity_degraded"] = bool(degraded)
    if degraded:
        summary["cost_final"] = False
    return summary


def _physical_call_count(row: Dict[str, Any]) -> int:
    kind = str(row.get("kind") or "attempt")
    if kind == "legacy_metadata":
        return 0
    if kind == "legacy_delta":
        return 0
    # A subscription session is not a core-mediated physical provider send; it is
    # counted on the sessions axis instead (see record_subscription_session).
    if kind == "subscription_session":
        return 0
    return 1 if str(row.get("state") or "") in {"dispatched", "settled", "unresolved"} else 0


def _breakdown_bucket(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    bucket = _summary(rows)

    def summed(field: str) -> Optional[int]:
        """Sum the rows that HAVE this count; None when not one of them does.

        `int(row.get(field) or 0)` collapsed an ABSENT count into a reported zero, so a
        bucket whose provider returned no token counts at all published a confident
        "0 tokens" — the render-unknown-as-zero shape this module refuses everywhere
        else (`cost = None  # legacy zero may mean unknown pricing, never "free"`). A
        zero here is now only ever a MEASURED zero, and a partially-reporting bucket
        still sums the rows that did report rather than being erased by the ones that
        did not."""
        present = [row.get(field) for row in rows if row.get(field) is not None]
        return sum(max(0, int(value)) for value in present) if present else None

    prompt = summed("prompt_tokens")
    completion = summed("completion_tokens")
    prompt_cache_ttls: Dict[str, int] = {}
    for row in rows:
        ttl = str(row.get("prompt_cache_ttl") or "").strip()
        if ttl:
            prompt_cache_ttls[ttl] = prompt_cache_ttls.get(ttl, 0) + _physical_call_count(row)
    bucket.update({
        "physical_calls": sum(_physical_call_count(row) for row in rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        # Unknown on BOTH halves is an unknown total; one known half is a real total
        # of what was measured, reported as the number it is.
        "total_tokens": (None if prompt is None and completion is None
                         else (prompt or 0) + (completion or 0)),
        "cached_tokens": summed("cached_tokens"),
        "cache_write_tokens": summed("cache_write_tokens"),
        "prompt_cache_ttls": prompt_cache_ttls,
    })
    return bucket


_SKILL_ATTEMPT_FIELDS = (
    "attempt_id", "review_slot_id", "kind", "state", "model", "provider", "source",
    "cost_usd", "cost_final", "reservation_upper_bound_usd", "pricing_known",
    "prompt_tokens", "completion_tokens", "cached_tokens", "subscription_route",
    "subscription_reset_at", "credential_profile_id", "access_profile",
)


def _skill_review_usage_bucket(
    rows: Sequence[Dict[str, Any]], *, review_skill: str, review_wave_id: str,
    integrity_degraded: bool,
) -> Dict[str, Any]:
    """Project one exact Skill Review wave from canonical final attempt rows."""
    selected = sorted(
        (
            row for row in rows
            if str(row.get("review_skill") or "") == review_skill
            and str(row.get("review_wave_id") or "") == review_wave_id
        ),
        key=lambda row: str(row.get("attempt_id") or ""),
    )
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for row in selected:
        grouped.setdefault(str(row.get("review_slot_id") or "(unattributed)"), []).append(row)
    by_slot = {
        slot: _with_integrity(_breakdown_bucket(grouped[slot]), integrity_degraded)
        for slot in sorted(grouped)
    }
    result = _with_integrity(_breakdown_bucket(selected), integrity_degraded)
    result.update({
        "review_skill": review_skill,
        "review_wave_id": review_wave_id,
        "attempt_ids": [str(row.get("attempt_id") or "") for row in selected],
        "attempts": [
            {key: row.get(key) for key in _SKILL_ATTEMPT_FIELDS if key in row}
            for row in selected
        ],
        "by_slot": by_slot,
        "attribution_complete": bool(selected) and all(
            str(row.get("review_slot_id") or "") for row in selected
        ),
    })
    return result


def _ua():
    """The accounting namespace, resolved lazily (import cycle + test pins)."""
    from ouroboros import usage_accounting

    return usage_accounting


@dataclass
class _LedgerRowsMemo:
    """In-process cache of one drive root's per-attempt FINAL rows.

    Holds only the ``_final_rows`` dict (one row per attempt, first-occurrence
    order) plus the resume fingerprint — O(final rows), not O(ledger rows);
    superseded transition rows are not retained.

    ``renders`` is the fingerprint-keyed cache of finished display renders
    (``usage_projection``/``usage_breakdown`` bodies) computed over these rows:
    valid exactly while the rows are, so it is cleared on refold and on every
    non-empty advance, never by TTL. ``generation`` increments on those same
    two events and guards the clear-then-publish race (see ``_render_cached``).
    """

    resume: LedgerResumeState
    final_rows: Dict[str, Dict[str, Any]]
    generation: int = 0
    renders: Dict[Tuple[Any, ...], Dict[str, Any]] = field(default_factory=dict)


# Read-side memo per RESOLVED drive root. Populated and advanced only under the
# cross-process ledger lock; the module lock guards the dict itself. Write paths
# (reserve/_transition/settle/import) never touch it — they read through their
# own in-lock cache below (full ordered records, which seq assignment and
# whole-history append validation need; this memo keeps only final rows), and
# the stat + seq-continuity check on the next read is what makes a stale memo
# impossible to serve, so correctness never depends on any writer remembering
# to invalidate.
_ROWS_MEMO: Dict[str, _LedgerRowsMemo] = {}
_ROWS_MEMO_LOCK = threading.Lock()


def _memoized_final_rows(
    root: pathlib.Path,
) -> Tuple[list, bool, "_LedgerRowsMemo", int]:
    """Validated final rows for display projections, resumed incrementally.

    Cold (or whenever the resume fingerprint is rejected — file replacement,
    size shrink, same-size rewrite, seq discontinuity, a non-row-aligned tail,
    structural corruption) this is one full ``_read_records_locked`` replay,
    which owns quarantine. Warm, it parses only the bytes appended since the
    previous read. Locks and substrate reads resolve through the
    ``usage_accounting`` namespace at call time (tests monkeypatch those
    names). Returned row dicts are shared read-only snapshots; row ORDER
    matches a from-scratch ``_final_rows`` exactly (first-occurrence order,
    updates in place), so aggregation over them is bit-identical to a fresh
    replay.

    Returns ``(rows, cacheable, memo, generation)`` — the render-cache
    transport: ``cacheable`` is False for the deliberately NON-RESUMABLE
    crash-tail fingerprint (``st_ino == -2``), whose every read stays a full
    replay, so caching a render of it would hide exactly the reads that must
    keep re-checking the torn tail. ``memo``/``generation`` let
    ``_render_cached`` publish a render computed OUTSIDE the lock only if the
    rows have not moved since.
    """
    ua = _ua()
    key = str(pathlib.Path(root).resolve(strict=False))
    with ua._locked(root):
        with _ROWS_MEMO_LOCK:
            memo = _ROWS_MEMO.get(key)
        advanced = ua._read_new_records_locked(root, memo.resume) if memo is not None else None
        if advanced is None:
            records = ua._read_records_locked(root)
            memo = _LedgerRowsMemo(
                resume=ua._ledger_resume_state(root, records),
                final_rows=ua._final_rows(records),
                generation=(memo.generation + 1) if memo is not None else 0,
            )
        else:
            new_records, new_resume = advanced
            if new_records:
                for row in new_records:
                    memo.final_rows[str(row["attempt_id"])] = row
                # The generation bump and the renders clear MUST happen under
                # _ROWS_MEMO_LOCK: the publisher in _render_cached checks the
                # generation and writes under that lock only (it never holds
                # the ledger lock), so without it the check-then-publish pair
                # could interleave with this clear — a stale render published
                # right after the clear would then serve pre-append data to
                # every warm reader until the next append. Lock order stays
                # "ledger lock → memo lock" (same as above/below); the
                # publisher takes the memo lock alone, so no deadlock. The
                # refold branch needs no such section: it swaps in a NEW memo
                # object and the publisher's `is memo` identity check already
                # rejects publications against a replaced object.
                with _ROWS_MEMO_LOCK:
                    memo.generation += 1
                    memo.renders.clear()
            memo.resume = new_resume
        with _ROWS_MEMO_LOCK:
            _ROWS_MEMO[key] = memo
        cacheable = memo.resume.st_ino != -2
        return list(memo.final_rows.values()), cacheable, memo, memo.generation


def _render_cached(
    root: pathlib.Path,
    cache_key: Tuple[Any, ...],
    render: Callable[[list, bool], Dict[str, Any]],
) -> Dict[str, Any]:
    """Serve one display render through the memo's fingerprint-keyed cache.

    The cache lives INSIDE the memo, so its lifetime is exactly the rows':
    refold and non-empty advance both replace/clear ``renders`` under the
    ledger lock. The quarantine stat happens HERE — outside the memo but after
    the row read, because that read itself may quarantine a torn tail — and the
    resulting bool joins the cache key, so an integrity change alone can never
    serve a stale render. The render itself runs OUTSIDE any lock; publication
    happens under ``_ROWS_MEMO_LOCK`` and only when the memo object and its
    generation are unchanged since the rows were read — a concurrent append
    between read and publish means the render is returned to this caller but
    never cached. Both directions hand out deep copies: the cached object is
    shared between requests, and callers (``_with_limit``/``_with_integrity``,
    gateway handlers) mutate nested buckets in place."""
    rows, cacheable, memo, generation = _memoized_final_rows(root)
    integrity_degraded = (root / QUARANTINE_REL).is_file()
    full_key = (*cache_key, integrity_degraded)
    if cacheable:
        with _ROWS_MEMO_LOCK:
            if memo.generation == generation:
                cached = memo.renders.get(full_key)
                if cached is not None:
                    return copy.deepcopy(cached)
    result = render(rows, integrity_degraded)
    if cacheable:
        key = str(pathlib.Path(root).resolve(strict=False))
        with _ROWS_MEMO_LOCK:
            if _ROWS_MEMO.get(key) is memo and memo.generation == generation:
                memo.renders[full_key] = copy.deepcopy(result)
    return result


# razzant/ouroboros#129: the in-lock write paths (reserve/settle/_transition/
# release/legacy-import) each did a full parse+validate of the whole ledger
# under the 45s monetary flock, and the file grows unboundedly. This is their
# per-process warm cache of the last validated read per drive root: the next
# in-lock read parses only the bytes appended since. It is distinct from
# ``_ROWS_MEMO`` because writers need the FULL ordered records list (seq
# assignment + whole-history append validation), not just final rows. Rows are
# shared read-only snapshots, same as the memo's.
_LEDGER_READ_CACHE: "collections.OrderedDict[str, Tuple[LedgerResumeState, list]]" = (
    collections.OrderedDict()
)
_LEDGER_READ_CACHE_LOCK = threading.Lock()
_LEDGER_READ_CACHE_MAX_ROOTS = 8


def _ledger_cache_put(key: str, value: "Tuple[LedgerResumeState, list]") -> None:
    with _LEDGER_READ_CACHE_LOCK:
        _LEDGER_READ_CACHE[key] = value
        _LEDGER_READ_CACHE.move_to_end(key)
        while len(_LEDGER_READ_CACHE) > _LEDGER_READ_CACHE_MAX_ROOTS:
            _LEDGER_READ_CACHE.popitem(last=False)


def _read_records_locked_cached(root: pathlib.Path) -> list:
    """``_read_records_locked`` with an incremental warm path. Call under the
    held ledger lock (same contract as ``_read_records_locked``)."""
    ua = _ua()
    key = str(pathlib.Path(root).resolve(strict=False))  # one slot per physical root
    with _LEDGER_READ_CACHE_LOCK:
        cached = _LEDGER_READ_CACHE.get(key)
    if cached is not None:
        resume, rows = cached
        try:
            delta = ua._read_new_records_locked(root, resume)
        except Exception:  # noqa: BLE001 — any doubt = fall back to the full read
            delta = None
        if delta is not None:
            new_rows, new_resume = delta
            merged = rows if not new_rows else [*rows, *new_rows]
            _ledger_cache_put(key, (new_resume, merged))
            return list(merged)
    records = ua._read_records_locked(root)
    try:
        resume = ua._ledger_resume_state(root, records)
        _ledger_cache_put(key, (resume, list(records)))
    except Exception:  # noqa: BLE001 — caching is best-effort; correctness is the full read
        log.debug("ledger read-cache seed failed for %s", key, exc_info=True)
        with _LEDGER_READ_CACHE_LOCK:
            _LEDGER_READ_CACHE.pop(key, None)
    return records
