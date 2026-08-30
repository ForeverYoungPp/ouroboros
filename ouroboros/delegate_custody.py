"""Durable custody for delegated (Claudexor) runs.

A delegated run is an OVERPOWERED, MUTATING process that Ouroboros does not own the
process tree of: it lives inside the Claudexor daemon and survives our worker, so
custody cannot live in a module dict — a crash, restart, or lost POST response would
leave a LIVE run nothing can wait on, cancel or settle. ``maxSeconds`` is damage
limitation, not custody.

**The authority is the durable record that is already written.** The task's own event
log (``logs/events.jsonl`` under the canonical data root) is the SSOT; the
process-local dict below is a pure memoization of these rows, and every question is
answered by replaying them. Rows survive a restart, so custody does too.

Three lookup answers, never two: OWNED, FOREIGN (another task started it) and UNKNOWN
(no durable record at all). Collapsing UNKNOWN into "not yours" is what made a restarted
owner indistinguishable from an intruder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros._usage_rows import REVIEW_ATTRIBUTION_KEYS
from ouroboros.delegate_custody_usage import (
    disclosed_spend,
    disclosed_tokens,
    summary_of,
)
from ouroboros.utils import append_jsonl, utc_now_iso
log = logging.getLogger(__name__)
# The harness's own terminal vocabulary — one definition for the tool, the settler and
# the reconciler (a second copy is how a "cancelled" run stayed live on one branch).
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
# The terminal states in which the run DID reach its contract write and DID act, so
# absent containment evidence is a missing proof rather than "too early to tell".
SUCCEEDED_STATES = frozenset({"succeeded"})
# Durable row kinds. Every one of them is written to the event log the agent, the
# supervisor and the forensic tooling already read.
START_REQUESTED = "delegate_run_start_requested"
STARTED = "delegate_run_started"
START_FAILED = "delegate_run_start_failed"
LEDGER_RECORDED = "delegate_run_ledger_recorded"
SETTLED = "delegate_run_settled"
SETTLE_FAILED = "delegate_run_settle_failed"
# The daemon answered that it has no such run. Custody closes WITHOUT a settlement: there
# is no terminal detail to settle against, and inventing a $0 ledger row for a run nobody
# can describe is the one shape the ledger must never produce.
CLOSED_ABSENT = "delegate_run_closed_absent"
PROJECT_RETIRED = "delegate_run_project_retired"
PROJECT_RETIRE_FAILED = "delegate_run_project_retire_failed"
CANCEL_OUTCOME = "delegate_run_cancel_outcome"
UNCONFINED = "delegate_run_unconfined"
CONTAINMENT_FAULT = "delegate_run_containment_fault"
CONTAINMENT_RESOLVED = "delegate_run_containment_resolved"
RECONCILED = "delegate_run_reconciled"
OUTPUT_SPILLED = "delegate_run_output_spilled"
# Owner doctrine D7: a result is OBTAINED only after the artifact is read to
# EOF - the ack covers the staged artifact CONTINUOUSLY start-to-EOF,
# hash-bound, over verified FULL content only (never the 256 KiB preview).
# Owner directive 2026-08-03: consumption is LOAD-BEARING before settlement;
# its ABSENCE is ``SETTLED_UNREAD``; it does not BLOCK settling (settle_run).
OUTPUT_CONSUMED = "delegate_run_output_consumed"
# A run whose VERIFIED FULL output was staged, paid for, and settled without ever being
# read to EOF: the "launched and never collected" class, named at the moment it becomes
# permanent instead of inferred later from a field nobody joined.
SETTLED_UNREAD = "delegate_run_settled_unread"
# C1 (delegated-run isolation): a MUTATING run executes in a private snapshot; its
# diff is captured durably at terminal, then EXPLICITLY applied or rejected by the
# nanny. Capture is idempotent (the flag replays); the disposition row is what
# releases the snapshot for GC — until then conflict material persists on disk.
PATCH_CAPTURED = "delegate_run_patch_captured"
PATCH_DISPOSED = "delegate_run_patch_disposed"
# CR1-3 (owed-before-sent, one row earlier than the disposition): APPLY-INTENT lands
# BEFORE the target tree is mutated; RESOLVED lands only when the attempt is KNOWN to
# have left the tree unmutated. An intent with neither resolution nor disposition
# replays as AMBIGUOUS — the tree may carry the patch — and a later reject/apply must
# refuse typed instead of pretending "not applied" over a modified tree.
PATCH_APPLY_STARTED = "delegate_run_patch_apply_started"
PATCH_APPLY_RESOLVED = "delegate_run_patch_apply_resolved"
# Q31: a partial external work order earns authority only through exact, host-
# verified source ranges. These append-only receipts survive worker restart and
# are merged by the same custody replay that owns every other run fact.
SOURCE_RANGE_VERIFIED = "delegate_run_work_order_source_range_verified"
SOURCE_RANGE_DELIVERY_CONFIRMED = "delegate_run_work_order_source_delivery_confirmed"

# Cheap prefilter: every custody row's type starts with this, so a multi-hundred-MB
# event log is scanned without JSON-parsing the 99.9% of lines that are not ours.
_ROW_MARKER = "delegate_run"
# The containment-fault projection is read on every context build, so it reads a bounded
# tail; ownership replay (rare, and correctness-critical) reads the whole log.
_FAULT_SCAN_TAIL_BYTES = 4_000_000

# Lookup answers.
OWNED = "owned"
FOREIGN = "foreign"
UNKNOWN = "unknown"

# Cancel outcomes. This vocabulary is what the tool reports verbatim: nothing may say
# terminal or cancelled without a verified terminal receipt.
CANCEL_REQUESTED = "requested"
CANCEL_CONFIRMED = "confirmed"
CANCEL_FAILED = "failed"
CANCEL_CONTAINMENT_FAULT = "containment_fault_run_may_still_be_live"

@dataclass
class RunCustody:
    """One delegated run's lifecycle facts, replayed from the durable rows."""

    run_id: str = ""
    task_id: str = ""
    route_id: str = ""
    model: str = ""
    # Requested pin (`credentialProfileId`) from the start body; '' = automatic; applied half = settlement authRoute.
    profile_id: str = ""
    project_id: str = ""
    project_owned: bool = False
    root_task_id: str = ""
    parent_task_id: str = ""
    category: str = "subagent"
    source: str = "delegated_subagent"
    review_skill: str = ""
    review_wave_id: str = ""
    review_slot_id: str = ""
    ledger_root: str = ""
    idempotency_key: str = ""
    # Minted once per logical invocation and reused verbatim as the wire
    # Idempotency-Key only for an exact transport retry; deliberately new work
    # gets a fresh id. ``idempotency_key`` above only finds pending work.
    invocation_id: str = ""
    # Configured-actor binding. Empty on historical/root-legacy delegate starts.
    selected_subagent_id: str = ""
    config_fingerprint: str = ""
    work_order_fingerprint: str = ""
    work_order_coverage: str = ""
    work_order_source_request: Dict[str, Any] = field(default_factory=dict)
    verified_source_ranges: List[Tuple[int, int]] = field(default_factory=list)
    authority_fingerprint: str = ""
    ledger_recorded: bool = False
    settled: bool = False
    # Written once (a re-polling nanny must not read as repeated findings).
    containment_disclosed: bool = False
    unread_disclosed: bool = False  # settled-never-read omission named durably
    # Staged-output half of the terminal story (D7). ``output_artifact``:
    # task-drive-relative staging path (empty when inline). ``output_complete``:
    # staged body matches the run's OWN report - served size or carried preview
    # prefix (no engine content hash, so equal length binds to the CLAIM; an
    # artifact matching neither stages incomplete, never acknowledgeable).
    # ``output_sha``: hash of what is staged NOW; ``output_consumed``: the
    # canonical ack exists FOR THAT HASH (an ack names bytes, not a path).
    output_artifact: str = ""
    output_complete: bool = False
    output_sha: str = ""
    output_consumed: bool = False
    # C1 isolation binding for a MUTATING run: private execution root, diff
    # baseline commit, AUTHORITY target tree. ``snapshot_id`` keys the
    # worktree-service registry entry (= the provisioning invocation id). All
    # durable/replayed: retries reproduce the binding, terminal capture knows
    # where to diff, startup GC tells live snapshots from disposable ones.
    snapshot_id: str = ""
    execution_root: str = ""
    baseline_sha: str = ""
    target_root: str = ""
    authority_source: str = ""
    # The GRANTED run shape, replayed off the STARTED row's `shape` dict:
    # delegate_wait replays this as the entitled authority - re-deriving from
    # live context read `readonly` and cancelled the run as widened (R1-2).
    access: str = ""
    mode: str = ""
    isolation: str = ""
    delegated: bool = False
    # Host-minted semantic resource reference for an exact-resource run (logical
    # root, source, skill name, recorded target, baseline payload hash). Consumed
    # only by retry rebind and owned apply rebind; recovery carries it opaquely.
    resource_ref: Dict[str, Any] = field(default_factory=dict)
    # Capture/disposition lifecycle (replayed): capture happens once at terminal;
    # ``patch_disposed`` is "" until the nanny explicitly applies ("applied") or
    # rejects ("rejected") the captured patch — only then may the snapshot be removed.
    patch_captured: bool = False
    patch_disposed: str = ""
    # CR1-3: an apply-intent row exists with no resolution and no disposition —
    # the target tree MAY carry the patch (crash between apply and the disposition
    # row), so any later disposition over this run is ambiguous until inspected.
    patch_apply_pending: bool = False


# Process-local MEMOIZATION of the rows above — never the authority. A miss falls
# through to the durable scan, which is why a restart no longer loses custody.
_CUSTODY: Dict[str, RunCustody] = {}
# -- durable record ------------------------------------------------------------

def event_log_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "logs" / "events.jsonl"

def custody_root(ctx: Any) -> pathlib.Path:
    """The drive whose event log is the custody authority for this context.

    A live subagent's isolated child drive is pruned, so a custody row written
    there cannot outlive the run; the canonical (budget) root is the durable SSOT.
    """
    from ouroboros.tool_access import canonical_data_root

    return canonical_data_root(ctx)

def emit(drive_root: Any, kind: str, payload: Dict[str, Any]) -> bool:
    """Append one custody row and REPORT whether it landed. Never raises.

    ``append_jsonl`` already owns the success predicate for this class of write.
    Discarding it is how a run could be reported as started, waited on and settled
    while the rows that ARE its custody never reached disk; the row's fate is the
    answer.
    """
    try:
        written = bool(append_jsonl(event_log_path(drive_root), {"ts": utc_now_iso(), "type": kind, **payload}))
    except Exception:
        log.warning("delegate custody row could not be written (%s)", kind, exc_info=True)
        return False
    if not written:
        log.warning("delegate custody row was rejected by the event log (%s)", kind)
    return written

def daemon_says_absent(exc: Any) -> bool:
    """True when the daemon ANSWERED that the named resource does not exist.

    A 404 from a reachable daemon is a definitive fact, not a failure to find out: a run
    the daemon does not have is not mutating anything, and a registration it does not have
    is already retired. Collapsing it into "unreachable" is what left a finished run in
    ``open_runs`` forever, re-faulted on every pass, with a CRITICAL health invariant no
    settlement or cancel could ever clear.

    Deliberately NOT "any 4xx": a 400 or a 403 is the daemon refusing US, which says
    nothing about whether the resource is there.
    """
    return int(getattr(exc, "status_code", 0) or 0) == 404

def custody_log_unreadable(drive_root: Any) -> bool:
    """Whether the custody event log EXISTS but cannot be opened (GR6-4).

    ``_iter_rows`` swallows its own ``OSError`` — the right behavior for the
    fail-soft readers — but the KILL/MISS/REAP AUDIT must not let an
    unreadable log audit as "cleanly reconciled": ABSENT is a positively
    established empty state (no custody row could exist), while
    existing-but-unreadable means the open-run answer is UNKNOWN. Same probe
    the evidence reader (``task_execution_evidence``) already uses; its own
    semantics are unchanged.
    """
    path = event_log_path(drive_root)
    try:
        if not path.exists():
            return False
        with path.open("rb"):
            pass
    except OSError:
        return True
    return False


@contextmanager
def actor_decision_lock(drive_root: Any, task_id: str) -> Iterator[None]:
    """Serialize one actor's mutually exclusive zero-run/start decisions.

    The custody log and the verification-receipt store are separate append-only
    authorities.  This short critical section joins only their decision edge:
    re-read current authority, then append either a zero-run receipt or a fresh
    START_REQUESTED row.  Transport, waiting and settlement stay outside it.
    """

    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
    )

    identity = str(task_id or "").strip()
    if not identity:
        raise ValueError("actor decision lock requires a task id")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    lock_path = pathlib.Path(drive_root) / "state" / "delegate_actor_claims" / f"{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = acquire_exclusive_file_lock(lock_path, timeout_sec=20.0, stale_sec=120.0)
    if fd is None:
        raise TimeoutError("actor decision lock is unavailable")
    try:
        yield
    finally:
        release_exclusive_file_lock(lock_path, fd)

def _iter_rows(path: pathlib.Path, tail_bytes: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            if tail_bytes is not None and size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()          # drop the partial first line
            for raw in handle:
                if _ROW_MARKER.encode("ascii") not in raw:
                    continue
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(row, dict) and str(row.get("type") or "").startswith(_ROW_MARKER):
                    yield row
    except OSError:
        return


# The STARTED row's string facts as ``(RunCustody attribute, row key)`` pairs —
# one table shared by the replay and the ``record_started`` emit.
_STARTED_STR_FIELDS: Tuple[Tuple[str, str], ...] = tuple(
    (attr, "route" if attr == "route_id" else attr) for attr in (
        "task_id", "route_id", "model", "profile_id", "project_id", "root_task_id",
        "parent_task_id", "category", "source", *REVIEW_ATTRIBUTION_KEYS,
        "ledger_root", "idempotency_key", "invocation_id",
        "snapshot_id", "execution_root", "baseline_sha", "target_root",
        "authority_source", "access", "mode", "isolation",
        "selected_subagent_id", "config_fingerprint", "work_order_fingerprint",
        "work_order_coverage", "authority_fingerprint",
    )
)
# Progress carried forward from a previous row: an idempotent re-start writes a
# SECOND started row; replacing wholesale would forget a settlement and put a
# finished run back into the orphan sweep (which would cancel it).
_STARTED_PROGRESS_FLAGS: Tuple[str, ...] = (
    "ledger_recorded", "settled", "containment_disclosed", "unread_disclosed",
    "output_artifact", "output_complete", "output_sha", "output_consumed",
    "patch_captured", "patch_disposed", "patch_apply_pending")
# Binding/authority facts are FIRST-WINS (R1-2): a later idempotent STARTED row
# may be minted by a context that no longer knows the original binding; the
# first recorded fact is authoritative and is never erased or retargeted.
_STARTED_FIRST_WINS_FACTS: Tuple[str, ...] = (
    "snapshot_id", "execution_root", "baseline_sha", "target_root",
    "authority_source", "resource_ref", "selected_subagent_id",
    "config_fingerprint", "work_order_fingerprint", "work_order_coverage",
    "authority_fingerprint", "work_order_source_request", "category", "source",
    *REVIEW_ATTRIBUTION_KEYS)

from ouroboros.delegate_source_coverage import (
    apply_source_delivery_confirmation,
    _merge_verified_source_range,
    merge_source_delivery_confirmations,
    _source_range_receipt_valid,
    record_source_range_verified,
    work_order_source_verification,
)

def _merge_started_into(entry: RunCustody, previous: RunCustody) -> None:
    """Project a duplicate STARTED fact set onto an existing run — the ONE
    merge, used by both the durable replay and the in-process memo (gate fix 8).

    Progress always carries forward; binding facts are truthy-first-wins per
    field; the SHAPE GROUP (access/mode/isolation/delegated) plus the resource
    reference is first-wins as a UNIT, keyed on the first row that CARRIED a
    shape — so a recorded ``delegated=False`` survives a later ``True`` and an
    empty ``resource_ref`` is never "filled" by a later row.
    """
    for attr in _STARTED_PROGRESS_FLAGS:
        setattr(entry, attr, getattr(previous, attr))
    entry.project_owned = previous.project_owned and entry.project_owned
    for attr in _STARTED_FIRST_WINS_FACTS:
        prior = getattr(previous, attr)
        if prior:
            setattr(entry, attr, prior)
    if previous.work_order_source_request:
        entry.work_order_source_request = dict(previous.work_order_source_request)
    for start, end in previous.verified_source_ranges:
        _merge_verified_source_range(entry, start, end)
    merge_source_delivery_confirmations(entry, previous)
    if previous.access:
        entry.access, entry.mode = previous.access, previous.mode
        entry.isolation, entry.delegated = previous.isolation, previous.delegated
        entry.resource_ref = previous.resource_ref


def _apply(state: Dict[str, RunCustody], row: Dict[str, Any]) -> None:
    run_id = str(row.get("run_id") or "")
    if not run_id:
        return
    kind = str(row.get("type") or "")
    if kind == STARTED:
        ref = row.get("resource_ref")
        source_request = row.get("work_order_source_request")
        entry = RunCustody(
            run_id=run_id,
            project_owned=bool(row.get("project_owned")),
            delegated=row.get("delegated") is True,
            resource_ref=dict(ref) if isinstance(ref, dict) else {},
            work_order_source_request=(
                dict(source_request) if isinstance(source_request, dict) else {}
            ),
            **{attr: str(row.get(key) or "") for attr, key in _STARTED_STR_FIELDS},
        )
        entry.category = entry.category or "subagent"
        entry.source = entry.source or "delegated_subagent"
        previous = state.get(run_id)
        setattr(entry, "_source_delivery_confirmations", [])
        if previous is not None:
            _merge_started_into(entry, previous)
        state[run_id] = entry
        return
    custody = state.get(run_id)
    if custody is None:
        return
    if kind == LEDGER_RECORDED:
        custody.ledger_recorded = True
    elif kind == UNCONFINED:
        # The unconfined finding is written once per RUN, not once per process. Replaying
        # it means a restarted worker that polls an already-terminal run does not append a
        # second identical disclosure, which would read as a second finding.
        custody.containment_disclosed = True
    elif kind == SETTLED_UNREAD:
        custody.unread_disclosed = True
    elif kind == PROJECT_RETIRED:
        # Recorded on SUCCESS too, not only on failure: without it, a retirement that
        # landed before a failed ledger write would be replayed as still-owned after a
        # restart, and the retry would keep failing on an already-removed project.
        custody.project_owned = False
    elif kind == OUTPUT_SPILLED:
        if row.get("staged") and str(row.get("artifact") or ""):
            custody.output_artifact = str(row.get("artifact") or "")
            # Strict: a row that does not POSITIVELY claim full content replays as
            # incomplete. An absent flag must never bless a preview as the result.
            custody.output_complete = row.get("full_content") is True
            sha = str(row.get("sha256") or "")
            if custody.output_consumed and sha and custody.output_sha and sha != custody.output_sha:
                # Different content re-staged at the same path: the old acknowledgement
                # named other bytes and does not transfer to content never read.
                custody.output_consumed = False
            custody.output_sha = sha
    elif kind == OUTPUT_CONSUMED:
        ack_sha = str(row.get("sha256") or "")
        # The ack is HASH-BOUND: it marks consumed only the content it actually names.
        # A stale ack row (older than the current staging) must not bless new bytes.
        if not ack_sha or not custody.output_sha or ack_sha == custody.output_sha:
            custody.output_consumed = True
    elif kind == PATCH_CAPTURED:
        custody.patch_captured = True
    elif kind == PATCH_APPLY_STARTED:
        custody.patch_apply_pending = True
    elif kind == PATCH_APPLY_RESOLVED:
        custody.patch_apply_pending = False
    elif kind == SOURCE_RANGE_VERIFIED:
        if _source_range_receipt_valid(
            custody,
            start_char=row.get("start_char"),
            end_char=row.get("end_char"),
            complete_sha256=row.get("complete_sha256"),
            source=row.get("source"),
            text_sha256=row.get("text_sha256"),
            text_chars=row.get("text_chars"),
        ):
            _merge_verified_source_range(custody, row.get("start_char"), row.get("end_char"))
    elif kind == SOURCE_RANGE_DELIVERY_CONFIRMED:
        apply_source_delivery_confirmation(custody, row)
    elif kind == PATCH_DISPOSED:
        disposition = str(row.get("disposition") or "")
        if disposition:
            custody.patch_disposed = disposition
            # A recorded disposition completes the apply-intent story too.
            custody.patch_apply_pending = False
    elif kind == SETTLED:
        # A RUN-level fact: it no longer clears the registration obligation -
        # PROJECT_RETIRED is the only discharging row (historical logs always
        # emitted it before SETTLED, so replay is unaffected).
        custody.ledger_recorded = True
        custody.settled = True
    elif kind == CLOSED_ABSENT:
        # Closed, not settled: custody is over, the run leaves ``open_runs``.
        # The registration survives independently (wholesale clearing here was
        # the leak shape); no ledger row exists and none ever will.
        custody.settled = True


def replay(drive_root: Any,
           rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, RunCustody]:
    """Rebuild every known run's custody from the durable rows (one pass).

    ``rows`` replays a pre-read snapshot so several projections can share ONE
    consistent traversal (the atomic payload busy claim, gate fix 5a)."""
    state: Dict[str, RunCustody] = {}
    for row in rows if rows is not None else _iter_rows(event_log_path(drive_root)):
        _apply(state, row)
    return state

def lookup(drive_root: Any, task_id: str, run_id: str) -> Tuple[str, Optional[RunCustody]]:
    """Answer OWNED / FOREIGN / UNKNOWN for ``run_id`` as seen by ``task_id``."""
    rid = str(run_id or "").strip()
    custody = _CUSTODY.get(rid)
    if custody is None:
        custody = replay(drive_root).get(rid)
        if custody is not None:
            _CUSTODY[rid] = custody
    if custody is None:
        return UNKNOWN, None
    custody.run_id = custody.run_id or rid
    owner, mine = str(custody.task_id or ""), str(task_id or "")
    # A custody guard that skips itself when either identity is missing is not a guard.
    if not owner or not mine or owner != mine:
        return FOREIGN, custody
    return OWNED, custody


# The read-side evidence projection lives in `ouroboros/delegate_evidence.py`
# (extracted at this module's size ceiling); re-exported here because the
# completion seam, the tests and monkeypatch targets name it on THIS surface.
from ouroboros.delegate_evidence import task_execution_evidence  # noqa: F401,E402


def delegated_capture_dir(drive_root: Any, task_id: str, run_id: str) -> pathlib.Path:
    """The canonical artifact directory for ONE delegated run's captured patch.

    Named here (custody owns durable naming) so the terminal capture and the
    explicit apply/reject seam cannot disagree about where the patch lives. Under
    the task's artifact store on the CANONICAL drive, so the capture survives
    child-drive pruning exactly like the custody rows themselves.
    """
    from ouroboros.artifacts import DELEGATED_CAPTURE_PREFIX, task_artifact_dir_path

    safe_run = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(run_id or ""))[:64]
    return task_artifact_dir_path(drive_root, str(task_id or "")) / DELEGATED_CAPTURE_PREFIX / (safe_run or "run")


def record_patch_captured(drive_root: Any, custody: RunCustody, **payload: Any) -> bool:
    """Durably mark a terminal mutating run's patch as captured (idempotent flag)."""
    custody.patch_captured = True
    return emit(drive_root, PATCH_CAPTURED, {
        "run_id": custody.run_id, "task_id": custody.task_id,
        "snapshot_id": custody.snapshot_id, **payload,
    })


def record_patch_disposed(drive_root: Any, custody: RunCustody, *, disposition: str,
                          **payload: Any) -> bool:
    """Durably record the EXPLICIT apply/reject disposition of a captured patch.

    This is the row that releases the execution snapshot for cleanup: until it
    lands, the snapshot and the captured patch persist as conflict material.
    """
    custody.patch_disposed = str(disposition or "")
    custody.patch_apply_pending = False  # a disposition completes the intent story
    return emit(drive_root, PATCH_DISPOSED, {
        "run_id": custody.run_id, "task_id": custody.task_id,
        "snapshot_id": custody.snapshot_id, "disposition": str(disposition or ""),
        **payload,
    })


def record_patch_apply_started(drive_root: Any, custody: RunCustody, **payload: Any) -> bool:
    """Durably record the APPLY INTENT before the target tree is mutated (CR1-3).

    Returns whether the row LANDED, and the caller must not mutate when it did
    not: an apply whose intent row never reached the disk leaves a crash window
    where a modified tree replays as "never applied" — the false-rejection class
    this row exists to close. Same doctrine as ``record_start_requested``.
    """
    landed = emit(drive_root, PATCH_APPLY_STARTED, {
        "run_id": custody.run_id, "task_id": custody.task_id,
        "snapshot_id": custody.snapshot_id, **payload,
    })
    if landed:
        custody.patch_apply_pending = True
    return landed


def record_patch_apply_resolved(drive_root: Any, custody: RunCustody, *, reason: str,
                                **payload: Any) -> bool:
    """Resolve an apply intent whose attempt is PROVEN to have left the tree
    unmutated (drift refusal, atomic apply failure, clean revert).

    Memory clears regardless of the row's fate — the caller just observed the
    unmutated outcome — so an in-process retry stays open; a failed write only
    means a post-restart replay stays ambiguous, which is the fail-closed
    direction. A disposition row resolves the intent too (see ``_apply``).
    """
    custody.patch_apply_pending = False
    return emit(drive_root, PATCH_APPLY_RESOLVED, {
        "run_id": custody.run_id, "task_id": custody.task_id,
        "snapshot_id": custody.snapshot_id, "reason": str(reason or ""),
        **payload,
    })


def open_snapshot_ids(drive_root: Any) -> set:
    """Snapshot ids custody still holds OPEN — the startup GC's keep-set.

    A snapshot is open while its run is unsettled OR its captured patch has no
    explicit disposition, and while a PENDING invocation names it (the POST may
    have bound a live run the worker died before recording). Everything else is
    disposable.
    """
    ids: set = set()
    for custody in replay(drive_root).values():
        if custody.snapshot_id and not (custody.settled and custody.patch_disposed):
            ids.add(custody.snapshot_id)
    for record in pending_invocations(drive_root):
        snap = str(record.get("snapshot_id") or "")
        if snap:
            ids.add(snap)
    return ids


def run_timing(drive_root: Any, run_id: str) -> Tuple[str, int]:
    """``(started_ts_iso, max_seconds)`` for a run, from its durable STARTED row.

    Empty/0 when the run is unknown or the row predates the ``max_seconds``
    field — absent facts stay absent, never invented.
    """
    rid = str(run_id or "").strip()
    started_ts, max_seconds = "", 0
    if not rid:
        return started_ts, max_seconds
    for row in _iter_rows(event_log_path(drive_root)):
        if str(row.get("run_id") or "") != rid or str(row.get("type") or "") != STARTED:
            continue
        started_ts = started_ts or str(row.get("ts") or "")
        if not max_seconds:
            try:
                max_seconds = int(row.get("max_seconds") or 0)
            except (TypeError, ValueError):
                max_seconds = 0
    return started_ts, max_seconds


def idempotency_key(*parts: Any) -> str:
    """A deterministic IDENTITY for one logical start — the lookup key, not the wire key.

    A random key per POST means an accepted start whose response was lost is retried as
    a SECOND live run nobody knows about. But a key derived from CONTENT alone has the
    opposite defect: a deliberate re-run of the same prompt presents the same key, and
    the engine hands back the finished old run instead of starting the intended new one.
    So this hash names the logical start for FINDING its pending invocation, and the
    invocation id (below) is what actually rides the wire as Idempotency-Key.
    """
    digest = hashlib.sha256("\0".join(str(part or "") for part in parts).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def new_invocation_id() -> str:
    """A logical invocation id: minted ONCE per intended invocation, never per POST.

    The wire Idempotency-Key derives from it (verbatim — it is already opaque and
    unique). An ordinary ``delegate_start`` ALWAYS mints a fresh one — the owner's
    contract: an intended new start is a NEW id, even for an identical prompt. Reuse
    happens only when the caller EXPLICITLY names a pending invocation (the retry
    token from a start whose outcome was unknown), so Claudexor's replay check can
    return the run it already accepted; nothing is ever reused by content-matching,
    because two identical intentions are still two intentions.
    """
    return uuid.uuid4().hex


def invocation_record(drive_root: Any, invocation_id: str) -> Optional[Dict[str, Any]]:
    """One invocation's durable fate: who requested it, the EXACT body it sent,
    the resources that attempt bound, and how it resolved.
    ``state`` is ``pending`` (requested, never bound, never definitely refused —
    the only state an explicit retry may replay), ``started`` (a run bound to it;
    carried with its ``run_id`` so the caller can wait instead of re-posting), or
    ``failed_definite`` (the daemon definitively refused; the id is dead — replaying
    it against a since-reconfigured route is how a key wedges into a permanent 409).
    ``request`` is the canonical POST body recorded before the wire attempt: a retry
    replays THESE bytes, never a re-derivation, because the engine's replay match
    digests the request and answers a same-key-different-digest POST with 409
    ``idempotency_conflict``.
    ``route``, ``project_id``, ``project_owned`` and ``idempotency_key`` are the
    attempt facts that never ride the wire, read from the FIRST request row — the
    minting — because the invocation stores its facts ONCE and a retry replays them.
    A retry that re-derived any of these from the current route/model/workspace
    context wrote a durable record contradicting the body it actually POSTed.
    First-request lineage, usage attribution (category/source/skill/wave/slot),
    and isolation facts are likewise replayed rather than re-derived.
    """
    target = str(invocation_id or "").strip()
    if not target:
        return None
    found: Optional[Dict[str, Any]] = None
    state, run_id = "pending", ""
    for row in _iter_rows(event_log_path(drive_root)):
        if str(row.get("invocation_id") or "") != target:
            continue
        kind = str(row.get("type") or "")
        if kind == START_REQUESTED and found is None:
            found = {
                "task_id": str(row.get("task_id") or ""),
                "surface": str(row.get("surface") or ""),
                "slot_id": str(row.get("slot_id") or ""),
                "operation_id": str(row.get("operation_id") or ""),
                "request": row.get("request") if isinstance(row.get("request"), dict) else None,
                "route": str(row.get("route") or ""),
                "project_id": str(row.get("project_id") or ""),
                "project_owned": bool(row.get("project_owned")),
                "idempotency_key": str(row.get("idempotency_key") or ""),
                "root_task_id": str(row.get("root_task_id") or ""),
                "parent_task_id": str(row.get("parent_task_id") or ""),
                "category": str(row.get("category") or "subagent"),
                "source": str(row.get("source") or "delegated_subagent"),
                **{key: str(row.get(key) or "") for key in REVIEW_ATTRIBUTION_KEYS},
                # The C1 isolation binding: a retry reproduces EXACTLY these — the
                # snapshot, the execution root, the baseline and the authority
                # target the original attempt bound — never a re-derivation.
                "snapshot_id": str(row.get("snapshot_id") or ""),
                "execution_root": str(row.get("execution_root") or ""),
                "baseline_sha": str(row.get("baseline_sha") or ""),
                "target_root": str(row.get("target_root") or ""),
                "authority_source": str(row.get("authority_source") or ""),
                "resource_ref": row.get("resource_ref") if isinstance(row.get("resource_ref"), dict) else {},
                "selected_subagent_id": str(row.get("selected_subagent_id") or ""),
                "config_fingerprint": str(row.get("config_fingerprint") or ""),
                "work_order_fingerprint": str(row.get("work_order_fingerprint") or ""),
                "work_order_coverage": str(row.get("work_order_coverage") or ""),
                "authority_fingerprint": str(row.get("authority_fingerprint") or ""),
                "work_order_source_request": (
                    row.get("work_order_source_request")
                    if isinstance(row.get("work_order_source_request"), dict) else {}
                ),
            }
        elif kind == STARTED:
            state, run_id = "started", str(row.get("run_id") or "")
        elif kind == START_FAILED and row.get("definite") is True and state != "started":
            state = "failed_definite"
    if found is None:
        return None
    return {**found, "state": state, "run_id": run_id}


def record_start_requested(drive_root: Any, **payload: Any) -> bool:
    """Durably name the resources a start is about to bind, BEFORE the POST.

    Returns whether the row LANDED; the caller must not POST when it did not —
    a run whose request row never reached disk is live, mutating and unfindable
    if the worker dies before ``record_started``.
    """
    return emit(drive_root, START_REQUESTED, payload)


def record_started(drive_root: Any, custody: RunCustody,
                   shape: Optional[Dict[str, Any]] = None) -> bool:
    """Memoize the run and write its authoritative row. Returns whether the row LANDED.

    A start whose row did not land is custodied by this process only: after this
    worker dies nothing can name, wait on, cancel or settle the live run.
    ``shape`` (access/mode/isolation/delegated/root) rides the SAME row — a shape
    recorded separately can lose one half of itself to a crash between writes.
    The memo update goes through the SAME first-wins merge the replay uses (gate
    fix 8): a duplicate start must not diverge the in-process view from replay.
    """
    # Fold the row-only shape onto the object first (the row spreads it last),
    # so the memo and a replay of this same row start from identical facts.
    for attr in ("access", "mode", "isolation"):
        if shape and attr in shape:
            setattr(custody, attr, str(shape.get(attr) or ""))
    if shape and "delegated" in shape:
        custody.delegated = shape.get("delegated") is True
    previous = _CUSTODY.get(custody.run_id)
    if previous is not None and previous is not custody:
        _merge_started_into(custody, previous)
    _CUSTODY[custody.run_id] = custody
    # The C1 binding and the resource reference ride the SAME row (a binding
    # recorded separately can lose half of itself to a crash); shape spreads LAST.
    return emit(drive_root, STARTED, {
        "run_id": custody.run_id,
        "project_owned": custody.project_owned,
        "resource_ref": custody.resource_ref or {},
        "work_order_source_request": custody.work_order_source_request or {},
        **{key: getattr(custody, attr) for attr, key in _STARTED_STR_FIELDS},
        **(shape or {}),
    })


def record_output_consumed(drive_root: Any, custody: RunCustody, *,
                           artifact: str, byte_length: int, sha256: str,
                           chars: int, lines: int) -> bool:
    from ouroboros.delegate_output import record_output_consumed as _record

    return _record(
        drive_root, custody, artifact=artifact, byte_length=byte_length,
        sha256=sha256, chars=chars, lines=lines,
    )


def output_disposition(custody: RunCustody) -> Dict[str, Any]:
    from ouroboros.delegate_output import output_disposition as _disposition

    return _disposition(custody)


def is_terminal(detail: Dict[str, Any]) -> bool:
    from ouroboros.delegate_custody_usage import is_terminal as _is_terminal

    return _is_terminal(detail, TERMINAL_STATES)


def retire_project(drive_root: Any, gateway: Any, custody: RunCustody) -> None:
    """Discharge the registration obligation. Absence IS discharge (a 404 on
    the project is the asked-for outcome, never a failure). REFCOUNT
    DEFERRAL: the daemon refuses removal while runs live, so only the
    LOWEST-run_id sharer keeps attempting; the rest defer quietly and
    discharge on the daemon's 404 (deterministic tie-break: someone always
    attempts)."""
    if not (custody.project_owned and custody.project_id):
        return
    try:
        # Sharers = EVERY run in the project (only the creator carries
        # project_owned, but the daemon refuses removal while ANY sibling
        # lives); the caller is mid-settlement, so only OTHERS defer.
        # Removal is an AUTHORITY decision: it needs the COMPLETE view (the
        # lenient reader can hide a sibling) with the caller's own row in it.
        from ouroboros.delegate_custody_usage import complete_custody_rows

        rows_raw = complete_custody_rows(event_log_path(drive_root), _ROW_MARKER)
        if rows_raw is None:
            log.warning("Retirement deferred: custody log view incomplete")
            return
        state = replay(drive_root, rows=rows_raw)
        if custody.run_id and custody.run_id not in state:
            log.warning("Retirement deferred: run %s not in replay", custody.run_id)
            return
        rows = [run for run in state.values()
                if run.project_id == custody.project_id and run.run_id]
        if any(not run.settled and run.run_id != custody.run_id for run in rows):
            return
        sharers = sorted(run.run_id for run in rows if run.project_owned)
    except Exception:
        log.warning("Retirement deferred: replay failed for %s",
                    custody.run_id, exc_info=True)
        return
    if sharers and custody.run_id and custody.run_id != sharers[0]:
        return  # deferred quietly: the canonical sharer carries the lane
    try:
        gateway.remove_project(custody.project_id)
    except Exception as exc:
        if not daemon_says_absent(exc):
            log.warning("Failed to retire delegated project %s", custody.project_id, exc_info=True)
            # The daemon's own refusal text rides the row: "failed" without the
            # WHY made every retire loop a forensic dig.
            emit(drive_root, PROJECT_RETIRE_FAILED, {"run_id": custody.run_id, "task_id": custody.task_id,
                                                     "project_id": custody.project_id,
                                                     "reason": str(exc)[:500]})
            return
    custody.project_owned = False
    emit(drive_root, PROJECT_RETIRED, {"run_id": custody.run_id, "task_id": custody.task_id,
                                       "project_id": custody.project_id})


def close_absent_run(drive_root: Any, gateway: Any, custody: RunCustody, reason: str) -> None:
    """Close custody over a run the daemon says it does not have.

    "Absent" is a fact about the daemon that answered, not the run: across the
    D30 provisioning boundary a 404 can mean a DIFFERENT daemon whose child may
    still be writing. Closing is a deliberate trade - an unreachable run cannot
    be cancelled/verified/settled, and holding it open re-faults it forever; no
    terminal detail, so no settlement (that would invent tokens/spend). The
    absent-run FACT and the registration are recorded INDEPENDENTLY (P34R.4):
    CLOSED_ABSENT closes custody now, the registration survives on
    ``project_owned`` until PROJECT_RETIRED - retried by later sharers and the
    sweep; a 404 on the PROJECT counts as discharged."""
    retire_project(drive_root, gateway, custody)
    # The absent-run FACT lands regardless of the registration (holding the
    # run open as cleanup-debt proxy was the same category error).
    custody.settled = emit(drive_root, CLOSED_ABSENT, {
        "run_id": custody.run_id, "task_id": custody.task_id,
        "route": custody.route_id, "project_id": custody.project_id, "reason": reason,
    })
    if custody.settled:
        # The compact fault projection is cleared by its OWN writer pair, so a run
        # closed absent through reconciliation (which never passes _cancel_result)
        # does not stay a CRITICAL invariant forever.
        resolve_containment_fault(drive_root, custody, "closed_absent")


def settle_run(drive_root: Any, gateway: Any, custody: RunCustody, detail: Dict[str, Any]) -> Dict[str, Any]:
    """Settle a TERMINAL run: the claim follows the DURABLE FACT, not the call.
    Ledger row and registration are independent idempotent duties; unfinished
    settlement is retried; ``settled`` means the durable fact exists."""
    if custody.settled:
        return {"settled": True, "ledger_recorded": True,
                "project_retired": not custody.project_owned, "retried": False}
    summary = summary_of(detail)
    # Claudexor reports CASH in `spendUsd`, EXACTNESS in `spendEstimated`. A run
    # is only free when the amount is really zero AND really settled: expired
    # sessions, bill-by-construction routes and auth fallbacks all charge, and
    # writing 0.0/cost_final=True over them hides money from every budget fence.
    spend, estimated = disclosed_spend(summary)
    # D29: the applied credential-profile id + access profile the deciding
    # attempt disclosed (authRoute receipt / effectiveAccess), written to the
    # durable row by default. Null on runs whose engine telemetry predates the
    # receipt — empty string, never invented.
    applied_profile = str((summary.get("authRoute") or {}).get("profileId") or "")
    # Only `effectiveAccess` testifies. The daemon computes `access` as
    # `effectiveAccess ?? the client's own parsed request`, so falling back to it wrote
    # our own ASK into the durable row under a column that promises applied facts.
    applied_access = str(summary.get("effectiveAccess") or "")
    if not custody.ledger_recorded:
        try:
            from ouroboros.usage_accounting import record_subscription_session

            record_subscription_session(
                custody.run_id,
                drive_root=pathlib.Path(custody.ledger_root or drive_root),
                route=custody.route_id,
                model=str(summary.get("model") or ""),
                task_id=custody.task_id,
                root_task_id=custody.root_task_id,
                parent_task_id=custody.parent_task_id,
                category=custody.category,
                source=custody.source,
                prompt_tokens=disclosed_tokens(summary.get("inputTokens")),
                completion_tokens=disclosed_tokens(summary.get("outputTokens")),
                cached_tokens=disclosed_tokens(summary.get("cachedInputTokens")),
                spend_usd=spend,
                spend_estimated=estimated,
                credential_profile_id=applied_profile,
                access_profile=applied_access,
                **{key: getattr(custody, key) for key in REVIEW_ATTRIBUTION_KEYS},
            )
        except Exception:
            log.exception("Failed to record delegated subscription session %s", custody.run_id)
            emit(drive_root, SETTLE_FAILED, {"run_id": custody.run_id, "task_id": custody.task_id,
                                             "route": custody.route_id, "step": "ledger_write"})
        else:
            custody.ledger_recorded = True
            emit(drive_root, LEDGER_RECORDED, {"run_id": custody.run_id, "task_id": custody.task_id,
                                               "route": custody.route_id})
    retire_project(drive_root, gateway, custody)
    if custody.ledger_recorded:
        # The claim follows the row, not the call. DECOUPLED from retirement:
        # a sibling holding the shared project must not convert a SUCCEEDED
        # run into an unreconciled failure - the registration debt stays on
        # ``project_owned`` for the sweep and later sharers.
        custody.settled = emit(drive_root, SETTLED, {
            "run_id": custody.run_id,
            "task_id": custody.task_id,
            "route": custody.route_id,
            # The ENGINE-reported model (the STARTED row carries only the requested
            # pin, which is usually empty) — so execution evidence can name what
            # the harness really ran without joining to the ledger.
            "model": str(summary.get("model") or ""),
            "state": str(summary.get("state") or ""),
            # The SAME facts the ledger row just recorded. An undisclosed spend was emitted
            # here as `0.0` beside a flag — the render-unknown-as-zero shape the ledger row
            # itself stopped doing — and finality ignored the estimated half exactly as the
            # ledger write did. One envelope, one story.
            "cost_usd": spend,
            "cost_final": spend is not None and not estimated,
            "spend_disclosed": spend is not None,
            "spend_estimated": estimated,
            # D29: the applied account rides the settlement event too, so the
            # durable event stream answers "which account paid" without joining
            # to the ledger row.
            "credential_profile_id": applied_profile,
            "access_profile": applied_access,
        })
    if custody.settled:
        resolve_containment_fault(drive_root, custody, "settled_terminal")
    # CONSUMPTION BEFORE SETTLEMENT is the owner's directive - a LOUD FACT,
    # not a gate, and NOT recorded here: this runs BEFORE staging, so asking
    # now would answer "no omission" for every first settlement (the render-
    # unknown-as-a-fact shape this module refuses). ``record_settled_unread``
    # records it where staging IS known: the wait path and reconciliation.
    return {
        "settled": custody.settled,
        "ledger_recorded": custody.ledger_recorded,
        "project_retired": not custody.project_owned,
        "retried": True,
    }


# -- cancellation --------------------------------------------------------------


def _faults_path(drive_root: Any) -> pathlib.Path:
    """The compact durable projection of containment incidents.

    The canonical event log carries every custody row and grows without bound, so a
    tail-bounded scan of it let an UNRESOLVED fault silently fall out of the health
    invariants once ~4 MB of later unrelated traffic buried its row — the exact
    opposite of "CRITICAL until a terminal receipt resolves it". Incidents are rare
    and small, so they get their own append-only file that a full read stays cheap
    on forever. The event-log rows remain, unchanged, as the forensic record.
    """
    return pathlib.Path(drive_root) / "logs" / "containment_faults.jsonl"


def open_containment_faults(drive_root: Any) -> List[Dict[str, Any]]:
    """Containment faults with no later resolution: runs that MAY still be live.

    Read on every context build. The compact projection is read WHOLE — an open
    incident can never age out of it — and the recent tail of the canonical event
    log is unioned in as the fallback for a fault whose compact write failed (the
    event row is appended second precisely so at least one surface always has it).
    A resolution always FOLLOWS its fault, so neither read can turn a resolved
    incident back into an open one.
    """
    faults: Dict[str, Dict[str, Any]] = {}
    resolved: set = set()

    def _apply_row(row: Dict[str, Any]) -> None:
        kind, run_id = str(row.get("type") or ""), str(row.get("run_id") or "")
        if not run_id:
            return
        if kind == CONTAINMENT_FAULT:
            if run_id not in resolved:
                faults[run_id] = row
        elif kind in (CONTAINMENT_RESOLVED, SETTLED, CLOSED_ABSENT):
            faults.pop(run_id, None)
            resolved.add(run_id)

    for row in _iter_rows(_faults_path(drive_root)):
        _apply_row(row)
    for row in _iter_rows(event_log_path(drive_root), tail_bytes=_FAULT_SCAN_TAIL_BYTES):
        _apply_row(row)
    return list(faults.values())


def settled_output_unread(custody: RunCustody) -> bool:
    """One predicate for "settled, staged in full, and never read to EOF".

    Shared by the settlement row, the parent-facing payload and the health invariant so
    the three cannot drift into disagreeing about the same run.
    """
    return bool(custody.settled and custody.output_artifact
                and custody.output_complete and not custody.output_consumed)


def record_settled_unread(drive_root: Any, custody: RunCustody) -> bool:
    """Name a settled-but-never-read result durably, once per RUN. Returns the fact.

    Once per run and not once per poll: a re-wait on an already settled run must not
    append a second identical row, which would read as a second omission. The
    ``unread_disclosed`` flag replays like every other custody fact, so a restarted
    worker does not repeat it either.
    """
    if not settled_output_unread(custody):
        return False
    if not custody.unread_disclosed:
        custody.unread_disclosed = emit(drive_root, SETTLED_UNREAD, {
            "run_id": custody.run_id, "task_id": custody.task_id,
            "route": custody.route_id, "artifact": custody.output_artifact,
        })
    return True


def settled_unread_outputs(drive_root: Any) -> List[RunCustody]:
    """Settled runs whose verified FULL output was never read to EOF.

    The counterpart of ``open_containment_faults`` for the D7 class, and self-clearing
    for the same reason: the acknowledgement row flips ``output_consumed`` in the very
    replay this reads, so the entry disappears the moment the nanny performs the read.
    Only VERIFIED FULL staged content qualifies — a preview was never acknowledgeable,
    and a run that staged nothing (inline result, cancelled, failed with no output) owes
    nothing and must never appear here.
    """
    return [custody for custody in replay(drive_root).values()
            if settled_output_unread(custody)]


def undisposed_patches(drive_root: Any) -> List[RunCustody]:
    """Settled mutating runs whose snapshot work awaits an explicit apply/reject.

    The C1 counterpart of ``settled_unread_outputs``: a run that executed in a
    private snapshot and settled — through the nanny OR through reconciliation —
    holds real work that reaches the shared tree only via
    ``integrate_delegated_patch``. Until that disposition lands, the snapshot and
    the captured patch persist (the GC keeps them), and this projection keeps the
    obligation VISIBLE instead of letting an orphaned run's work sit on disk
    forever, preserved but findable by nobody. Self-clearing: the
    ``PATCH_DISPOSED`` row flips ``patch_disposed`` in the very replay this reads.
    """
    return [custody for custody in replay(drive_root).values()
            if custody.snapshot_id and custody.settled and not custody.patch_disposed]


def record_containment_fault(drive_root: Any, custody: RunCustody, reason: str,
                             detail: str = "", **facts: Any) -> None:
    """A run we tried to stop and could not verify stopped is a LOUD, durable incident.

    It surfaces as a CRITICAL health invariant until a verified terminal receipt (or a
    settlement) resolves it — never as a reassuring string in a tool result.

    ``facts`` carries the typed evidence for the specific guarantee that was not
    delivered (which access profile was enforced, which harness HOME was applied). ONE
    author for this row: an unverified cancel and a breached containment produce the
    same incident shape, so a reader never has to know which path wrote it.

    Written to the compact projection FIRST — that is the surface the health invariant
    actually reads — then to the canonical event log for forensics; either landing
    alone keeps the incident visible. ``detail`` is the incident's EVIDENCE, and a
    durable incident whose evidence was silently cut is half an incident: the bound
    goes through the shared disclosed-truncation contract (marker + original length),
    never a bare slice (P34R.5, DEVELOPMENT.md "No silent truncation").
    """
    from ouroboros.utils import truncate_review_artifact

    row = {
        "ts": utc_now_iso(), "type": CONTAINMENT_FAULT,
        "run_id": custody.run_id, "task_id": custody.task_id, "route": custody.route_id,
        "project_id": custody.project_id, "reason": reason,
        "detail": truncate_review_artifact(str(detail or ""), 2000),
        **facts,
    }
    try:
        append_jsonl(_faults_path(drive_root), row)
    except Exception:
        log.warning("containment-fault projection row could not be written", exc_info=True)
    emit(drive_root, CONTAINMENT_FAULT, {k: v for k, v in row.items() if k not in ("ts", "type")})


def resolve_containment_fault(drive_root: Any, custody: RunCustody, reason: str) -> None:
    row = {"ts": utc_now_iso(), "type": CONTAINMENT_RESOLVED,
           "run_id": custody.run_id, "task_id": custody.task_id, "reason": reason}
    try:
        append_jsonl(_faults_path(drive_root), row)
    except Exception:
        log.warning("containment-fault resolution row could not be written", exc_info=True)
    emit(drive_root, CONTAINMENT_RESOLVED, {k: v for k, v in row.items() if k not in ("ts", "type")})


def cancel_and_verify(drive_root: Any, gateway: Any, custody: RunCustody, reason: str) -> Dict[str, Any]:
    """Cancel a run and report ONLY what a terminal receipt proves.

    ``confirmed`` needs the run to read back terminal — or a durable settlement, or the
    daemon answering that it has no such run. ``requested`` is an accepted control the run
    has not obeyed yet. ``failed`` is a reachable daemon refusing while the run keeps
    mutating, and an unverifiable attempt is a containment fault. The old path answered
    all four with ``status: cancelled``.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    # The same durable fact ``settle_run`` short-circuits on, consulted by its twin. A run
    # this module already recorded as terminal is not an overpowered live process, and a
    # later ordinary cancel of it must not manufacture a permanent CRITICAL against a run
    # the settlement had closed.
    if custody.settled:
        return _cancel_result(drive_root, custody, CANCEL_CONFIRMED, accepted=False,
                              control_status="", state="settled")
    accepted, control_status, control_error = False, "", ""
    try:
        receipt = gateway.cancel_run(custody.run_id, reason=str(reason or ""))
        accepted = bool((receipt or {}).get("accepted"))
        control_status = str((receipt or {}).get("status") or "")
    except ClaudexorUnavailable as exc:
        if daemon_says_absent(exc):
            close_absent_run(drive_root, gateway, custody, "cancel_run_absent")
            return _cancel_result(drive_root, custody, CANCEL_CONFIRMED, accepted=False,
                                  control_status="", state="absent")
        # A refused control is not a verdict on the RUN. The read below is what decides
        # whether anything is still mutating; declaring the fault here — with the read
        # sitting three lines away, unused — faulted runs that had already stopped.
        control_error = f"{exc.code}: {exc}"
    try:
        detail = gateway.get_run(custody.run_id)
    except ClaudexorUnavailable as exc:
        if daemon_says_absent(exc):
            close_absent_run(drive_root, gateway, custody, "get_run_absent")
            return _cancel_result(drive_root, custody, CANCEL_CONFIRMED, accepted=accepted,
                                  control_status=control_status, state="absent")
        return _cancel_result(drive_root, custody, CANCEL_CONTAINMENT_FAULT, accepted=accepted,
                              control_status=control_status, state="",
                              fault_reason="cancel_unreachable" if control_error else "cancel_unverified",
                              detail=control_error or f"{exc.code}: {exc}")
    state = str(summary_of(detail).get("state") or "")
    if state in TERMINAL_STATES:
        settle_run(drive_root, gateway, custody, detail)
        # The verify read's own detail rides the result (BR2-1, purely additive):
        # a caller consuming a discovered natural terminal (completion wins) must
        # not depend on a SECOND fetch succeeding after the run is settled.
        return _cancel_result(drive_root, custody, CANCEL_CONFIRMED, accepted=accepted,
                              control_status=control_status, state=state,
                              terminal_detail=detail)
    if control_error:
        return _cancel_result(drive_root, custody, CANCEL_CONTAINMENT_FAULT, accepted=False,
                              control_status=control_status, state=state,
                              fault_reason="cancel_unreachable", detail=control_error)
    if accepted:
        return _cancel_result(drive_root, custody, CANCEL_REQUESTED, accepted=True,
                              control_status=control_status, state=state)
    return _cancel_result(drive_root, custody, CANCEL_FAILED, accepted=False,
                          control_status=control_status, state=state,
                          fault_reason="cancel_rejected_run_still_live",
                          detail=f"daemon refused the cancel; run state is {state or 'unknown'}")


def _cancel_result(drive_root: Any, custody: RunCustody, outcome: str, *, accepted: bool,
                   control_status: str, state: str, fault_reason: str = "",
                   detail: str = "",
                   terminal_detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    emit(drive_root, CANCEL_OUTCOME, {
        "run_id": custody.run_id, "task_id": custody.task_id, "outcome": outcome,
        "accepted": accepted, "control_status": control_status, "state": state,
    })
    if fault_reason:
        record_containment_fault(drive_root, custody, fault_reason, detail)
    elif outcome == CANCEL_CONFIRMED:
        resolve_containment_fault(drive_root, custody, "verified_terminal")
    result = {"outcome": outcome, "accepted": accepted, "control_status": control_status,
              "state": state, "fault_reason": fault_reason, "detail": detail}
    # BR2-1, backward-compatible: the key exists only when a verify read produced
    # the run detail — absent otherwise, so every pre-existing consumer of the
    # six-key shape is untouched (the emit above deliberately excludes it too).
    if terminal_detail is not None:
        result["terminal_detail"] = terminal_detail
    return result

# -- reconciliation ------------------------------------------------------------


def open_runs(drive_root: Any) -> List[RunCustody]:
    """Runs with a durable start and no durable settlement."""
    return [custody for custody in replay(drive_root).values() if not custody.settled]


def owned_project_registrations(drive_root: Any) -> List[RunCustody]:
    """Runs whose registration is still owned - settled or not (``open_runs``
    cannot see registrations that outlive their runs)."""
    return [
        custody for custody in replay(drive_root).values()
        if custody.project_owned and custody.project_id
    ]


def retire_settled_registrations(drive_root: Any, gateway: Any) -> None:
    """Retire projects every sharer has settled; a LIVE sharer (owned or not
    - only the creator carries the registration, but any live sibling makes
    the daemon refuse) defers the attempt. Idempotent, fail-soft."""
    by_project: Dict[str, List[RunCustody]] = {}
    for row in replay(drive_root).values():
        if row.project_id and row.run_id:
            by_project.setdefault(row.project_id, []).append(row)
    for rows in by_project.values():
        owned = [row for row in rows if row.project_owned]
        if not owned or any(not row.settled for row in rows):
            continue  # nothing registered here, or a live sharer defers
        try:
            retire_project(drive_root, gateway, min(owned, key=lambda row: row.run_id))
        except Exception:
            log.warning("Registration sweep failed for project %s",
                        rows[0].project_id, exc_info=True)


def pending_invocations(drive_root: Any,
                        rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Invocations with a durable request row, no bound run, no definite refusal.

    The launched-never-collected class one step EARLIER than ``open_runs``: a
    worker death between the accepted POST and ``record_started`` leaves only the
    ``START_REQUESTED`` row. Facts come from the FIRST request row (the minting,
    same rule as ``invocation_record``); a record whose canonical body never
    landed is excluded (nothing byte-identical can be replayed). ``rows`` shares
    one pre-read snapshot with ``replay`` (atomic payload busy claim)."""
    from ouroboros.delegate_pending import pending_invocations as replay_pending

    return replay_pending(drive_root, rows)

def release_task_runs(drive_root: Any, task_id: str, *,
                      gateway_factory: Optional[Callable[[], Any]] = None) -> List[Dict[str, Any]]:
    """Run the one non-panic terminal custody boundary for a normal loop exit."""

    from ouroboros.delegate_terminal import (
        record_terminal_reconciliation, terminal_reconcile_task,
    )

    result = terminal_reconcile_task(
        drive_root, task_id, gateway_factory=gateway_factory, trigger="loop_exit",
    )
    record_terminal_reconciliation(drive_root, task_id, result)
    return list(result.get("outcomes") or [])

def reconcile_task_runs(drive_root: Any, task_id: str, *,
                        gateway_factory: Optional[Callable[[], Any]] = None) -> List[Dict[str, Any]]:
    """Settle or cancel ONE task's open runs from the DURABLE rows (kill path).

    The supervisor-side twin of ``release_task_runs`` for a task whose worker was
    just KILLED (cancellation custody / reap): the graceful release never ran and
    its memo died with the process, so the durable rows are the only complete
    view. Covers pending invocations like the orphan sweep; cheap when the task
    delegated nothing.
    """
    mine = str(task_id or "")
    if not mine:
        return []
    held = [c for c in open_runs(drive_root) if c.task_id == mine]
    stray = [record for record in pending_invocations(drive_root)
             if record["task_id"] == mine]
    # Also the registration retry lane for the task's OWN settled-but-owned
    # runs in retire-eligible projects (a one-shot process may never see a
    # sweep tick); deferred registrations stay with the periodic sweep.
    snapshot = replay(drive_root).values()
    live = {r.project_id for r in snapshot
            if r.project_id and r.run_id and not r.settled}
    owed = [r for r in snapshot
            if r.task_id == mine and r.project_owned and r.project_id
            and r.settled and r.project_id not in live]
    if not held and not stray and not owed:
        return []
    return _reconcile_each(drive_root, held, gateway_factory, pending=stray)

def reconcile_orphaned_runs(
    drive_root: Any,
    running_task_ids: Optional[set] = None,
    *,
    gateway_factory: Optional[Callable[[], Any]] = None,
    recoverable_task_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Settle or cancel every open run whose owning task is no longer running.

    The owner-is-gone predicate is the SAME one ``process_custody.reap_orphaned_processes``
    already uses (the supervisor's live task set), so a delegated run and a spawned
    process cannot disagree about whether their owner still exists. ``running_task_ids``
    of None means UNKNOWN and reconciles nothing — never mass-cancel on missing info.
    """
    if running_task_ids is None:
        return []
    spared = set(recoverable_task_ids or ())
    live_or_reserved = set(running_task_ids) | spared
    orphans = [c for c in open_runs(drive_root) if c.task_id and c.task_id not in live_or_reserved]
    # The class ONE STEP EARLIER (P34R.2): an invocation whose POST the daemon may have
    # accepted but whose worker died before record_started has no run row for the sweep
    # above to find — a live mutating run nobody could ever collect. Recovered here on
    # the SAME owner-is-gone predicate; a pending invocation whose owner is ALIVE stays
    # untouched, because that owner holds the retry token and decides.
    stray = [record for record in pending_invocations(drive_root)
             if record["task_id"] and record["task_id"] not in live_or_reserved]
    return _reconcile_each(drive_root, orphans, gateway_factory, pending=stray)


def _reconcile_each(drive_root: Any, runs: List[RunCustody],
                    gateway_factory: Optional[Callable[[], Any]],
                    pending: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One transport, one settle-or-cancel pass. Shared by both release surfaces.

    ``pending`` is the durable sweep's extra duty: START_REQUESTED-only invocations
    (P34R.2). The in-process twin ``release_task_runs`` never passes it — its memo is
    run-keyed and cannot name an unbound invocation — so that class is covered by the
    startup/periodic sweep, within its cadence. The registration pass at the end is
    the third duty: settlement no longer discharges project ownership, and the
    startup/periodic sweep surface is where settled-but-owned registrations get
    their retry lane (the task-scoped surface can exit early with none open).
    """
    # Registration duty is real work only for a retire-ELIGIBLE project:
    # a deferred one must not spin the daemon up.
    snapshot = replay(drive_root).values()
    unsettled_projects = {row.project_id for row in snapshot
                          if row.project_id and row.run_id and not row.settled}
    registrations = [row for row in snapshot
                     if row.project_owned and row.project_id
                     and row.project_id not in unsettled_projects]
    if not runs and not pending and not registrations:
        return []
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    if gateway_factory is None:
        # The startup sweep REAPS the prior generation's owned daemon just
        # before this, so a discovery-only gateway always found a corpse and
        # reconciliation silently no-opped on every restart. The ensure path
        # starts our own daemon when there is real work (never on the empty
        # early-return above), activating any staged runtime update.
        from ouroboros.claudexor_daemon import ensure_owned_gateway

        gateway_factory = ensure_owned_gateway
    try:
        gateway = gateway_factory()
        gateway.handshake()
    except ClaudexorUnavailable:
        log.debug("delegated-run reconciliation skipped: transport unavailable", exc_info=True)
        return []
    outcomes: List[Dict[str, Any]] = []
    try:
        for custody in runs:
            outcomes.append(_reconcile_one(drive_root, gateway, custody))
        for record in pending or []:
            outcomes.append(_recover_pending_invocation(drive_root, gateway, record))
        # Recomputed inside: a run settled this very pass may have made its
        # project eligible - the pre-pass gate is not the last word.
        if registrations or runs:
            retire_settled_registrations(drive_root, gateway)
    finally:
        try:
            gateway.close()
        except Exception:
            log.debug("delegated-run reconciliation close failed", exc_info=True)
    return outcomes


def _recover_pending_invocation(drive_root: Any, gateway: Any,
                                record: Dict[str, Any]) -> Dict[str, Any]:
    """Recover the run (if any) behind an orphaned pending invocation, idempotently.

    The stored canonical body is re-POSTed under the invocation's own wire key:
    the engine returns the ORIGINAL handle when the first POST was accepted, and
    starts fresh only when the daemon truly never saw it. A definite 4xx retires
    the invocation and its registration; an unknown outcome stays pending.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    invocation_id = str(record["invocation_id"])
    task_id = str(record["task_id"])
    try:
        handle = gateway.start_run(dict(record["request"]), idempotency_key=invocation_id)
    except ClaudexorUnavailable as exc:
        status = int(getattr(exc, "status_code", 0) or 0)
        if 400 <= status < 500:
            retired = _retire_recovered_registration(gateway, record)
            emit(drive_root, START_FAILED, {
                "run_id": "", "task_id": task_id, "project_id": record["project_id"],
                "project_retired": retired, "reason": f"recovery_refused_{exc.code}",
                "invocation_id": invocation_id, "definite": True,
            })
            result = {"invocation_id": invocation_id, "task_id": task_id,
                      "action": "invocation_retired"}
        else:
            result = {"invocation_id": invocation_id, "task_id": task_id,
                      "action": "recovery_unreachable"}
        emit(drive_root, RECONCILED, result)
        return result
    run_id = str(handle.get("runId") or handle.get("jobId") or "")
    if not run_id:
        # Queued without an id: durably enqueued, still unnameable. Leave the
        # invocation pending; the next sweep replays the same key and tries again.
        result = {"invocation_id": invocation_id, "task_id": task_id,
                  "action": "recovery_pending"}
        emit(drive_root, RECONCILED, result)
        return result
    body = record["request"]
    execution = body.get("execution") if isinstance(body.get("execution"), dict) else {}
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    custody = RunCustody(
        run_id=run_id, task_id=task_id,
        route_id=str(record["route"] or body.get("primaryHarness") or ""),
        model=str(body.get("model") or ""),
        profile_id=str(body.get("credentialProfileId") or ""),
        project_id=record["project_id"], project_owned=bool(record["project_owned"]),
        root_task_id=str(record.get("root_task_id") or ""),
        parent_task_id=str(record.get("parent_task_id") or ""),
        category=str(record.get("category") or "subagent"),
        source=str(record.get("source") or "delegated_subagent"),
        **{key: str(record.get(key) or "") for key in REVIEW_ATTRIBUTION_KEYS},
        # The sweep runs against the canonical root; a recovered run's ledger row
        # belongs there like every other (P34R.1).
        ledger_root=str(drive_root),
        idempotency_key=str(record["idempotency_key"]), invocation_id=invocation_id,
        selected_subagent_id=str(record.get("selected_subagent_id") or ""),
        config_fingerprint=str(record.get("config_fingerprint") or ""),
        work_order_fingerprint=str(record.get("work_order_fingerprint") or ""),
        work_order_coverage=str(record.get("work_order_coverage") or ""),
        work_order_source_request=(
            dict(record.get("work_order_source_request"))
            if isinstance(record.get("work_order_source_request"), dict) else {}
        ),
        authority_fingerprint=str(record.get("authority_fingerprint") or ""),
        # The C1 isolation binding survives recovery VERBATIM: the recovered
        # run executes in the originally provisioned snapshot (the replayed
        # body's scope.root), so its STARTED row must name that binding or the
        # snapshot and the child's work become GC food once no longer pending.
        snapshot_id=str(record.get("snapshot_id") or ""),
        execution_root=str(record.get("execution_root") or ""),
        baseline_sha=str(record.get("baseline_sha") or ""),
        target_root=str(record.get("target_root") or ""),
        authority_source=str(record.get("authority_source") or ""),
        # Carried opaquely VERBATIM — recovery never re-authorizes a target (R1-2).
        resource_ref=record.get("resource_ref") if isinstance(record.get("resource_ref"), dict) else {},
        # The GRANTED shape on the recovered OBJECT too, not only the row (gate
        # fix 8c): the memo must answer the same lookups the replay does.
        access=str(body.get("access") or ""),
        mode=str(body.get("mode") or ""),
        isolation=str(execution.get("isolation") or ""),
        delegated=bool(execution.get("delegated")))
    record_started(drive_root, custody, shape={
        # The stored invocation is the single source of a replay's facts — the same
        # doctrine the explicit retry path follows.
        "effort": str(body.get("effort") or ""), "access": str(body.get("access") or ""),
        "mode": str(body.get("mode") or ""), "isolation": str(execution.get("isolation") or ""),
        "delegated": bool(execution.get("delegated")), "root": str(scope.get("root") or ""),
        "recovered_from_pending_invocation": True,
    })
    return _reconcile_one(drive_root, gateway, custody)


def _retire_recovered_registration(gateway: Any, record: Dict[str, Any]) -> bool:
    """Discharge the registration an ORIGINAL attempt owned, when its invocation dies."""
    if not (record.get("project_owned") and record.get("project_id")):
        return False
    try:
        gateway.remove_project(record["project_id"])
        return True
    except Exception as exc:
        if daemon_says_absent(exc):
            return True
        log.warning("Failed to retire project %s of a dead invocation",
                    record["project_id"], exc_info=True)
        return False


def _reconcile_one(drive_root: Any, gateway: Any, custody: RunCustody) -> Dict[str, Any]:
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.tools.delegate_integration import capture_stranded_patch

    try:
        detail = gateway.get_run(custody.run_id)
    except ClaudexorUnavailable as exc:
        if daemon_says_absent(exc):
            close_absent_run(drive_root, gateway, custody, "reconcile_absent")
            result = {"run_id": custody.run_id, "task_id": custody.task_id, "action": "absent"}
        else:
            record_containment_fault(drive_root, custody, "reconcile_unreadable", f"{exc.code}: {exc}")
            result = {"run_id": custody.run_id, "task_id": custody.task_id, "action": "unreadable"}
        # NO capture here (C1-R2): across the D30 boundary an absent run may
        # still be WRITING its snapshot; an eager capture would freeze an
        # incomplete patch the idempotent core then serves forever. Custody
        # closes, the snapshot survives (undisposed - GC keeps it), the duty
        # surfaces via undisposed_patches(); capture happens at disposition.
        emit(drive_root, RECONCILED, result)
        return result
    if is_terminal(detail):
        settled = settle_run(drive_root, gateway, custody, detail)
        # Sweep custody REPLAYS with staged fields, so unlike the wait path
        # the omission is already knowable here - and an ownerless run is the
        # one nobody will come back to read (the D7 launched-never-collected
        # half becomes durable in this row instead of inferred).
        record_settled_unread(drive_root, custody)
        # The action names the ATTEMPT; the facts ride separately (the old
        # shape wrote action="settled" even when the returned flag was false).
        result = {"run_id": custody.run_id, "task_id": custody.task_id,
                  "action": "settle_attempted",
                  "settled": settled["settled"],
                  "project_retired": settled.get("project_retired"),
                  **output_disposition(custody)}
        # The C1 half: a TERMINAL DETAIL proves the run is over, so the sweep — its
        # last terminal observer — captures the diff eagerly here.
        result.update(capture_stranded_patch(drive_root, custody))
    else:
        cancelled = cancel_and_verify(drive_root, gateway, custody, "owner_task_gone")
        result = {"run_id": custody.run_id, "task_id": custody.task_id, "action": "cancelled",
                  "outcome": cancelled["outcome"], **output_disposition(custody)}
        # Capture ONLY on a verified terminal receipt (the read-back proved the run
        # over). A cancel merely requested leaves the run live and its snapshot
        # still being written; a cancel confirmed by ABSENCE proves nothing about
        # the run (same unknowable-state doctrine as above) — both leave the
        # capture to disposition.
        if cancelled["state"] in TERMINAL_STATES:
            result.update(capture_stranded_patch(drive_root, custody))
    emit(drive_root, RECONCILED, result)
    return result


__all__ = [
    "CANCEL_CONFIRMED",
    "CANCEL_CONTAINMENT_FAULT",
    "CANCEL_FAILED",
    "CANCEL_REQUESTED",
    "FOREIGN",
    "OWNED",
    "RunCustody",
    "SOURCE_RANGE_DELIVERY_CONFIRMED",
    "TERMINAL_STATES",
    "UNKNOWN",
    "actor_decision_lock",
    "cancel_and_verify",
    "close_absent_run",
    "custody_log_unreadable",
    "custody_root",
    "daemon_says_absent",
    "delegated_capture_dir",
    "disclosed_spend",
    "emit",
    "idempotency_key",
    "is_terminal",
    "invocation_record",
    "lookup",
    "new_invocation_id",
    "open_containment_faults",
    "open_runs",
    "open_snapshot_ids",
    "output_disposition",
    "pending_invocations",
    "reconcile_orphaned_runs",
    "record_containment_fault",
    "record_output_consumed",
    "record_patch_apply_resolved",
    "record_patch_apply_started",
    "record_patch_captured",
    "record_patch_disposed",
    "record_start_requested",
    "record_settled_unread",
    "record_started",
    "release_task_runs",
    "reconcile_task_runs",
    "retire_project",
    "run_timing",
    "settle_run",
    "settled_output_unread",
    "settled_unread_outputs",
    "summary_of",
    "task_execution_evidence",
    "undisposed_patches",
    "work_order_source_verification",
    "record_source_range_verified",
]
