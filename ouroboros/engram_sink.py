"""The single sink that carries Ouroboros memory into Engram.

Everything that should become *memory* passes through here, so the policies live
in exactly one place instead of being re-implemented at four call sites:

- **C17 spool.** A record is appended to a durable local spool *before* it is
  sent (write-then-forward). If Engram is unreachable the record survives; the
  next run forwards it. The spool is append-only and crash-safe: a torn final
  line is skipped, and a "pending" record is one whose ``add`` has no ``ack``.
- **C18 gate.** Engram stores memories and outcomes, **not work items**. A
  backlog nomination, an open obligation or a next-step is an action queue; it
  belongs in the local backlog and is refused here. The test is straightforward:
  if the content's job is to tell the agent what to do next, it is not a memory.
- **C3 caps.** Per-field 300 chars, per-run 20 emits — both reused from the
  existing local conventions rather than invented.
- **C19 two-phase evolution outcome.** A cycle records
  ``waiting_for_restart`` at task-done and its real verdict only after the
  restart/boot reconcile. Both writes carry the same ``task_id`` identity, so
  the second is an upsert of the first, never a second memory.
- **C6 soft dependency.** Nothing here raises. A failure returns a receipt and
  discloses itself once per run; it never blocks or fails a task.

See ``.ouroboros/seed-engram-memory.yaml`` for the full contract.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

log = logging.getLogger(__name__)

from ouroboros.engram_client import (  # noqa: E402
    _FORBIDDEN_PROJECTS,
    EngramClient,
    EngramResult,
    resolve_project,
)

# --------------------------------------------------------------------------- #
# Policy constants (all reused from existing local conventions — C3)
# --------------------------------------------------------------------------- #

#: Per-field character cap. Reuses ``improvement_backlog._sanitize(limit=300)``.
FIELD_CAP_CHARS = 300
#: Per-run emit cap. Reuses ``improvement_backlog._DEDUP_CANDIDATE_CAP``.
MAX_EMITS_PER_RUN = 20
#: Spool growth bound. Exceeding it is disclosed, never silently ignored.
SPOOL_MAX_ENTRIES = 500
#: Character cap for a knowledge topic. Topics are DOCUMENTS — markdown with
#: tables, lists and code blocks — not one-line memory actions, so the one-liner
#: cap (``FIELD_CAP_CHARS * 4`` = 1200) would amputate every real one (the live
#: store's largest is ~10.6 KB). This bound is deliberately stated rather than
#: reused because no equivalent document cap exists, and it is generous enough
#: that truncation is a genuine anomaly worth a visible marker.
KNOWLEDGE_DOC_CHARS = 16_000

SPOOL_REL = pathlib.Path("state") / "engram_spool.jsonl"
#: When a spool with NOTHING pending exceeds this, it is rewritten empty.
#: Housekeeping only, not a correctness bound: a fully-acknowledged spool carries
#: no information, and the append-only format keeps two rows per write forever — so
#: without this the file grows without bound AND every send re-reads all of it
#: (``_send`` asks ``pending_count()``). A spool that still holds unforwarded memory
#: is NEVER touched.
SPOOL_COMPACT_BYTES = 256 * 1024
SPOOL_SCHEMA_VERSION = 1

#: Recognised sink kinds. ``evolution_cycle_outcome`` shares the checkpoint's
#: identity so its second write upserts the first (C19).
SINK_KINDS = frozenset(
    {
        "memory_action",
        "knowledge",
        # The consolidator's distilled dialogue blocks. They are the agent's
        # continuity memory (100 messages -> one block, 4 blocks -> one era) and
        # they used to ride in every prompt as `## Dialogue History`; mirroring
        # them is what lets that seam inject Engram instead of a local file.
        "dialogue_summary",
        "continuation_narrative",
        "evolution_checkpoint",
        "evolution_cycle_outcome",
        "review_verdict",
    }
)

# --------------------------------------------------------------------------- #
# C18 — the memory / to-do boundary
# --------------------------------------------------------------------------- #

#: Stamped onto an identity refinement so it can never read back as an established
#: trait (W1: "标为候选而非既成事实").
IDENTITY_CANDIDATE_MARKER = (
    "IDENTITY UPDATE CANDIDATE — a PROPOSAL, not an adopted trait. "
    "Pending review; never treat this as an established fact about who I am."
)

#: Content whose *job* is to say what to do next. Refused by the sink.
_TODO_LEAD = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:"
    r"todo\b|to-do\b|next steps?\b|action items?\b|follow-?ups?\b|"
    r"open obligations?\b|proposed[_ ]next[_ ]step\b|"
    r"下一步|待办|待处理|需要修|要修复|后续要|必须修|接下来要"
    r")",
    re.IGNORECASE,
)

#: Field names that are structurally a work item, never a memory (C18).
_TODO_FIELDS = frozenset(
    {"proposed_next_step", "open_obligations", "next_step", "next_steps", "todo", "action_items"}
)


def _looks_like_todo(text: Any) -> bool:
    """True when the content leads with an instruction about what to do next."""
    return bool(_TODO_LEAD.match(str(text or "")))


def _sanitize(value: Any, limit: int = FIELD_CAP_CHARS) -> str:
    """Collapse whitespace and bound one field. Mirrors the backlog convention."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _sanitize_document(value: Any, limit: int = KNOWLEDGE_DOC_CHARS) -> str:
    """Bound a DOCUMENT without destroying it.

    The one-liner sanitizer collapses every run of whitespace, which is correct
    for "verdict: approved | ..." and fatal for a knowledge topic: it would fold a
    markdown table, a list and a code block into one unreadable line. Now that
    Engram is the only record of a topic, that is silent degradation of the
    durable store (BIBLE P1). So: keep newlines, strip trailing blanks, and mark
    truncation explicitly instead of hiding it behind an ellipsis.
    """
    lines = [line.rstrip() for line in str(value or "").replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    marker = "\n\n…[truncated before storing — the topic exceeded the document bound]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _merge_document(base: str, fragment: str) -> str:
    """Concatenate a deferred fragment, matching the local append's byte behaviour."""
    joined = f"{base}\n{fragment}" if base and not base.endswith("\n") else f"{base}{fragment}"
    return _sanitize_document(joined)


def _fingerprint(*parts: Any) -> str:
    """Stable identity for upsert. Same shape as the backlog's local fingerprint."""
    import hashlib

    key = " | ".join(re.sub(r"\s+", " ", str(p or "")).strip().lower() for p in parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _as_mapping(value: Any) -> Dict[str, Any]:
    """Coerce anything into a plain dict without ever raising.

    Adapters receive values straight from other subsystems, so a ``None``, a
    list or a stray int must degrade to "nothing to send" rather than propagate
    a ``TypeError`` into a task (C6 / failure mode F1).
    """
    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SinkReceipt:
    """Outcome of one emit attempt. Never an exception (C6)."""

    kind: str
    spool_id: str
    status: str                  # sent | spooled | rejected | capped | failed
    reason: str = ""
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {"sent", "spooled"}


# --------------------------------------------------------------------------- #
# The sink
# --------------------------------------------------------------------------- #


@dataclass
class EngramSink:
    """Bounded, crash-safe, soft-failing memory sink."""

    client: EngramClient
    drive_root: pathlib.Path
    session_id: str = ""
    #: The repository this sink speaks for. The session is created with it, and it
    #: is what the server resolves the project from when no override was given.
    repo_root: Any = None
    #: Non-empty when the sink could not resolve a safe project scope. Every
    #: send is then refused locally so nothing is scattered into a wrong
    #: project; records still spool and disclose (C6 + F7).
    config_error: str = ""
    max_emits: int = MAX_EMITS_PER_RUN
    _emits: int = field(default=0, init=False, repr=False)
    _disclosed: bool = field(default=False, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)
    _ready_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    #: identity -> content fingerprint already emitted this run. Same identity
    #: with *different* content is an update (C19) and must still be sent.
    _seen: Dict[str, str] = field(default_factory=dict, init=False, repr=False)

    # -- paths ------------------------------------------------------------- #

    @property
    def spool_path(self) -> pathlib.Path:
        return pathlib.Path(self.drive_root) / SPOOL_REL

    # -- bootstrap (Engram's own recommended flow) ------------------------- #

    @property
    def session(self) -> str:
        """The session this sink writes under.

        Engram requires a session on every observation and inherits the PROJECT
        from it, so the session is the write identity. Ouroboros has no runtime
        session id of its own, so one session per project is the stable choice.
        """
        return str(self.session_id or self.client.config.project or "ouroboros")

    def ensure_ready(self) -> bool:
        """Create the session once per process. True when reads/writes may proceed.

        Engram's documented client flow is ``GET /project/current`` (the server owns
        project policy) then ``POST /sessions`` — and the second call is not
        bookkeeping: project-scoped routes reject a project the store does not know,
        and ``POST /observations`` rejects a session that does not exist. Until the
        session exists EVERY read and write 404s, which is how a "wired up"
        integration can be entirely non-functional while looking fine.

        Idempotent (re-creating returns 201 and changes nothing), best-effort, and
        cached; a failure leaves it unready so the next call retries.
        """
        if self._ready or self.config_error:
            return self._ready
        with self._ready_lock:
            if self._ready:
                return True
            try:
                result = self.client.create_session(
                    self.session,
                    self.client.config.project,
                    str(self.repo_root or self.client.config.drive_root or ""),
                )
            except Exception:
                return False
            self._ready = bool(result.ok)
            return self._ready

    # -- public API -------------------------------------------------------- #

    def emit(
        self,
        kind: str,
        *,
        title: str,
        content: str,
        identity: str = "",
        type: str = "",
        scope: str = "project",
        fields: Optional[Mapping[str, Any]] = None,
        document: bool = False,
    ) -> SinkReceipt:
        """Gate → bound → spool → send. Never raises.

        ``document=True`` keeps newlines and uses the larger document bound; it is
        for knowledge topics, which are prose/code rather than one-line records.

        ``fields`` do NOT reach Engram: ``EngramClient.save`` transmits session_id /
        type / title / content / scope / project / topic_key, and Engram's schema has
        no generic metadata column. They serve the local C18 gate and the local spool
        audit trail only — so anything a future reader must be able to find remotely
        has to be in the title, the content, or the identity.
        """
        try:
            return self._emit(kind, title=title, content=content, identity=identity,
                              type=type, scope=scope, fields=fields, document=document)
        except Exception as exc:  # absolutely nothing may escape into a task
            return SinkReceipt(
                kind=str(kind), spool_id="", status="failed",
                reason="internal_error", detail=f"{type(exc).__name__}: {exc}",
            )

    def flush(self, *, limit: int = MAX_EMITS_PER_RUN) -> int:
        """Forward pending spool records. Returns how many were acknowledged."""
        try:
            return self._flush(limit=limit)
        except Exception:
            return 0

    def pending(self) -> List[Dict[str, Any]]:
        """Spool records that were added but never acknowledged."""
        try:
            return _pending_records(self.spool_path)
        except Exception:
            return []

    def pending_count(self) -> int:
        return len(self.pending())

    # -- four thin adapters (one per confirmed write source) --------------- #

    def _guard(self, kind: str, fn: Any, *args: Any, **kwargs: Any) -> SinkReceipt:
        """Run one adapter body without letting anything escape (C6 / F1).

        The public adapters are called from task finalisation, where a raised
        exception is a failed task. Malformed input must degrade to a receipt.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return SinkReceipt(
                kind,
                "",
                "failed",
                reason="internal_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

    def emit_memory_action(self, action: Mapping[str, Any], *, task_id: str = "") -> SinkReceipt:
        """W1 — a reflection-nominated durable memory.

        ``proposed_next_step`` is deliberately NOT forwarded: it is a work item
        (C18), and the local entry keeps it.
        """
        return self._guard("memory_action", self._memory_action, action, task_id=task_id)

    def _memory_action(self, action: Any, *, task_id: str = "") -> SinkReceipt:
        payload = _as_mapping(action)
        content = str(payload.get("content") or "")
        topic = str(payload.get("topic") or "").strip()
        action_type = str(payload.get("type") or "memory_action")
        title = _sanitize(payload.get("title") or topic or f"memory action ({action_type})")
        if action_type == "identity_update_candidate":
            # W1: an identity refinement is a PROPOSAL, and Engram is read back as
            # memory — so an unmarked one would surface, on demand retrieval, as a
            # trait the agent already has. The whole point of routing identity
            # through a review candidate is that autonomous learning cannot drift
            # the personality; storing it as a bare fact would defeat that at the
            # one place a future reader actually looks.
            content = f"{IDENTITY_CANDIDATE_MARKER}\n\n{content}"
            if not str(payload.get("title") or "").strip():
                title = _sanitize(f"Identity update candidate: {topic or content[:120]}")
        return self.emit(
            "memory_action",
            title=title,
            content=content,
            # Topic is the upsert identity when present; fall back to content.
            identity=topic or _fingerprint(action_type, content),
            type=action_type,
            fields={"task_id": task_id or payload.get("task_id")},
        )

    def emit_continuation_narrative(self, narrative: Mapping[str, Any], *, task_id: str) -> SinkReceipt:
        """W4 — the authored end-of-task narrative ("episodic memory") summary."""
        return self._guard(
            "continuation_narrative", self._continuation_narrative, narrative, task_id=task_id
        )

    def _continuation_narrative(self, narrative: Any, *, task_id: str = "") -> SinkReceipt:
        text = str(_as_mapping(narrative).get("text") or "")
        return self.emit(
            "continuation_narrative",
            title=f"Task narrative {task_id}".strip(),
            content=text,
            identity=f"continuation:{task_id}",   # one narrative per task → upsert
            type="episodic_memory",
            fields={"task_id": task_id},
        )

    def emit_deferred_append(
        self,
        *,
        title: str,
        content: str,
        identity: str,
        type: str = "knowledge",
        scope: str = "global",
        fields: Optional[Mapping[str, Any]] = None,
    ) -> SinkReceipt:
        """Spool a fragment to be MERGED onto the live record at forward time.

        Used when the write side cannot read the record it is appending to. The
        alternative — refusing — loses the fragment whenever the store is down,
        and the other alternative — merging onto a guessed base — upserts over
        content nobody read. Deferring is the only option that neither loses nor
        overwrites (C17).
        """
        return self._guard(
            "knowledge",
            self._emit,
            "knowledge",
            title=title,
            content=content,
            identity=identity,
            type=type,
            scope=scope,
            fields=fields,
            document=True,
            deferred_merge=True,
        )

    def emit_evolution_checkpoint(self, entry: Mapping[str, Any]) -> SinkReceipt:
        """WO — an evolution cycle's outcome.

        Both the task-done checkpoint and the later ``cycle_outcome`` tag land
        on the same identity, so the second upserts the first (C19) instead of
        leaving a permanent half-finished memory.
        """
        return self._guard("evolution_checkpoint", self._evolution_checkpoint, entry)

    def _evolution_checkpoint(self, entry: Any) -> SinkReceipt:
        row = _as_mapping(entry)
        kind = str(row.get("kind") or "evolution_checkpoint")
        task_id = str(row.get("task_id") or "")
        objective = _sanitize(row.get("campaign_objective"), 300)
        # An empty verdict must NOT be laundered into "waiting_for_restart": that
        # turns "nobody recorded an answer" into "an answer is coming", which is
        # how a pending half-memory becomes permanent (AC21a).
        outcome = str(row.get("cycle_outcome") or "").strip() or "unknown"
        reason = _sanitize(row.get("abandoned_reason"))
        parts = [f"objective: {objective or '(unknown)'}", f"outcome: {outcome}"]
        if reason:
            parts.append(f"reason: {reason}")
        elif outcome == "abandoned":
            # AC22: "abandoned" with no reason is not a memory, it is a shrug —
            # a future reader cannot tell hopeless from blocked from deferred.
            # Say so explicitly rather than leaving a silent gap.
            parts.append("reason: (not recorded)")
        if row.get("commit_sha"):
            parts.append(f"commit: {row['commit_sha']}")
        axes = row.get("outcome_axes")
        if isinstance(axes, Mapping) and axes:
            parts.append("outcome_axes: " + json.dumps(axes, ensure_ascii=False, sort_keys=True))
        return self.emit(
            "evolution_checkpoint",
            title=_sanitize(f"Evolution cycle: {objective or task_id or kind}"),
            content=" | ".join(parts),
            identity=f"evolution_cycle:{task_id or row.get('campaign_id') or kind}",
            type="evolution_outcome",
            fields={
                k: row.get(k)
                for k in ("task_id", "campaign_id", "cycle_outcome", "commit_sha", "git_sha")
                if row.get(k)
            },
        )

    def emit_dialogue_summary(self, block: Mapping[str, Any]) -> SinkReceipt:
        """One distilled dialogue block from the consolidator.

        Identity is interval-addressed, so re-summarising the same span (a
        startup reconciliation, a retried consolidation, a cursor that did not
        advance) UPDATES the one record instead of piling up near-duplicates.
        """
        return self._guard("dialogue_summary", self._dialogue_summary, block)

    def _dialogue_summary(self, block: Any) -> SinkReceipt:
        row = _as_mapping(block)
        content = str(row.get("content") or "")
        block_kind = str(row.get("type") or "summary")
        span = _sanitize(row.get("range"), 120)
        gap_id = _sanitize(row.get("gap_id"), 120)
        # The interval, when there is one, is the span the consolidator derived
        # from the chunk's endpoints — a pure function of them, so the same span
        # retried produces the same identity and therefore an update rather than
        # a second record for one interval.
        #
        # Two cases have no usable interval and must NOT collapse onto one shared
        # identity (P1: a memory gap is a fact, and two gaps are two facts):
        #   * a gap marker, whose range is the literal "unknown" sentinel while
        #     the distinguishing datum lives in gap_id;
        #   * any block with no range at all (this function takes arbitrary
        #     mappings, so it cannot assume the consolidator's producers).
        # Those fall back to a label that is unique per block, and only a block
        # with neither label nor interval falls back to the content fingerprint.
        interval = span if span and span != "unknown" else ""
        label = gap_id or interval
        # One label drives BOTH the title and the identity so they cannot drift:
        # Engram's fallback dedupe keys on the title as well as the content hash,
        # so two records that must stay distinct must not share a title.
        identity = f"dialogue:{block_kind}:{label or _fingerprint(block_kind, span, content)}"
        return self.emit(
            "dialogue_summary",
            title=_sanitize(f"Dialogue {block_kind}: {label or span}".strip()),
            content=content,
            identity=identity,
            type="dialogue_summary",
            # Repo-wide: this is the one identity's continuity, not a project fact.
            scope="global",
            document=True,
            fields={
                "range": span,
                "block_kind": block_kind,
                "message_count": str(row.get("message_count") or ""),
                # Carried so a gap record read back from Engram still says WHICH
                # discontinuity it marks; the local block is keyed on this too.
                "gap_id": gap_id,
            },
        )

    def emit_review_verdict(self, row: Mapping[str, Any], *, task_id: str = "") -> SinkReceipt:
        """W9 — a review verdict.

        Only the *verdict* is forwarded. ``open_obligations`` inside the review
        evidence is a work item and stays local (C18).
        """
        return self._guard("review_verdict", self._review_verdict, row, task_id=task_id)

    def _review_verdict(self, row: Any, *, task_id: str = "") -> SinkReceipt:
        entry = _as_mapping(row)
        verdict = _sanitize(entry.get("verdict") or entry.get("status"), 120)
        tid = task_id or str(entry.get("task_id") or "")
        detail = _sanitize(entry.get("summary") or entry.get("reason") or entry.get("detail"))
        return self.emit(
            "review_verdict",
            title=_sanitize(f"Review verdict: {verdict or 'unknown'}"),
            content=f"verdict: {verdict or 'unknown'}" + (f" | {detail}" if detail else ""),
            identity=f"review_verdict:{tid}:{entry.get('attempt') or entry.get('id') or ''}",
            type="review_verdict",
            fields={"task_id": tid, "verdict": verdict},
        )

    # -- internals --------------------------------------------------------- #

    def _emit(
        self,
        kind: str,
        *,
        title: str,
        content: str,
        identity: str,
        type: str,
        scope: str,
        fields: Optional[Mapping[str, Any]],
        document: bool = False,
        deferred_merge: bool = False,
    ) -> SinkReceipt:
        if kind not in SINK_KINDS:
            return SinkReceipt(kind, "", "rejected", reason="unknown_kind")
        # C18: a work item is not a memory.
        if _looks_like_todo(content) or _looks_like_todo(title):
            return SinkReceipt(kind, "", "rejected", reason="todo_not_memory")
        safe_fields = {
            k: v for k, v in (fields or {}).items() if k not in _TODO_FIELDS and v not in (None, "")
        }
        if not safe_fields and fields:
            return SinkReceipt(kind, "", "rejected", reason="todo_only_payload")
        # C3: per-run cap.
        if self._emits >= int(self.max_emits):
            return SinkReceipt(kind, "", "capped", reason="per_run_emit_cap")

        clean_title = _sanitize(title)
        clean_content = (
            _sanitize_document(content) if document else _sanitize(content, FIELD_CAP_CHARS * 4)
        )
        if not clean_title or not clean_content:
            return SinkReceipt(kind, "", "rejected", reason="empty")

        # In-run dedupe (F2: writing the same thing twice is a failure). The key
        # is identity AND content, not identity alone: C19's second evolution
        # write shares the task_id identity but carries the real verdict, so it
        # must go through as an update. Only an exact repeat is dropped.
        clean_identity = _sanitize(identity or _fingerprint(kind, clean_title), 200)
        content_key = _fingerprint(clean_title, clean_content)
        if self._seen.get(clean_identity) == content_key:
            return SinkReceipt(kind, "", "duplicate", reason="identity_and_content_already_emitted")
        self._seen[clean_identity] = content_key

        spool_id = uuid.uuid4().hex
        record = {
            "v": SPOOL_SCHEMA_VERSION,
            "op": "add",
            "spool_id": spool_id,
            "ts": _now(),
            "kind": kind,
            "title": clean_title,
            "content": clean_content,
            "identity": clean_identity,
            "type": type or kind,
            "scope": scope or "project",
            "fields": {k: _sanitize(v, 200) for k, v in safe_fields.items()},
        }
        if deferred_merge:
            # The fragment is not the record: it must be merged onto whatever the
            # live record holds at FORWARD time, because the write side could not
            # read it. Flagged rather than encoded as a separate spool op so the
            # pending/ack bookkeeping stays one shape.
            record["deferred_merge"] = True
        # C17: durable BEFORE the network call.
        if not _spool_append(self.spool_path, record):
            return SinkReceipt(kind, spool_id, "failed", reason="spool_unwritable")
        self._emits += 1

        result = self._send(record)
        if result.ok:
            _spool_append(self.spool_path, {"v": SPOOL_SCHEMA_VERSION, "op": "ack",
                                            "spool_id": spool_id, "ts": _now()})
            self.compact_if_idle()
            return SinkReceipt(kind, spool_id, "sent")
        self._disclose_once(result)
        return SinkReceipt(kind, spool_id, "spooled",
                           reason=result.error_kind or "send_failed", detail=result.detail[:200])

    def _send(self, record: Mapping[str, Any]) -> EngramResult:
        if self.config_error:
            # No safe project scope: refuse locally rather than let Engram fall
            # back to its own detection and scatter the memory (F7).
            return EngramResult(False, error_kind="config", detail=self.config_error)
        if self.pending_count() > SPOOL_MAX_ENTRIES:
            return EngramResult(False, error_kind="config", detail="spool over bound")
        if not self.ensure_ready():
            return EngramResult(
                False, error_kind="config", detail="session bootstrap failed"
            )
        if record.get("deferred_merge"):
            return self._send_deferred_merge(record)
        return self.client.save(
            session_id=self.session,
            type=str(record.get("type") or "memory"),
            title=str(record.get("title") or ""),
            content=str(record.get("content") or ""),
            scope=str(record.get("scope") or "project"),
            topic_key=str(record.get("identity") or ""),
        )

    def _send_deferred_merge(self, record: Mapping[str, Any]) -> EngramResult:
        """Merge a deferred fragment onto the LIVE record, now that it can be read.

        The write side could not read the record it was appending to, so the merge
        was deferred instead of guessed: merging blind would upsert over content
        nobody saw and delete the older half (BIBLE P1). Here the base is re-read,
        and the fragment is only written if the record does not already end with
        it — that is what keeps a retry after a lost ack from appending twice
        (C17: forward retries must not duplicate).
        """
        identity = str(record.get("identity") or "")
        fragment = str(record.get("content") or "")
        topic = identity[len("knowledge:") :] if identity.startswith("knowledge:") else ""
        if not topic or not fragment:
            return EngramResult(
                False, error_kind="config", detail="deferred merge without a topic and fragment"
            )
        from ouroboros.engram_read import KNOWLEDGE_BASE_HARD_CHARS, knowledge_topic

        read = knowledge_topic(
            self.client,
            topic,
            scope=str(record.get("scope") or ""),
            max_chars=KNOWLEDGE_BASE_HARD_CHARS,
        )
        if not read.ok:
            # Still unreadable. Keep it spooled — never merge onto a base we could
            # not read, and never drop the fragment for want of a read.
            return EngramResult(
                False, error_kind="transport", detail=f"append base unreadable: {read.detail}"
            )
        base = read.text if read.status == "ok" else ""
        if fragment.strip() and base.rstrip().endswith(fragment.rstrip()):
            return EngramResult(True, data={"already_applied": True})
        return self.client.save(
            session_id=self.session,
            type=str(record.get("type") or "knowledge"),
            title=str(record.get("title") or ""),
            content=_merge_document(base, fragment),
            scope=str(record.get("scope") or "global"),
            topic_key=identity,
        )

    def compact_if_idle(self) -> bool:
        """Rewrite the spool empty when it holds nothing pending. Never raises.

        Follows the canonical JSONL-rewrite pattern in this codebase (see
        ``process_custody._rewrite_ledger``): take the SAME sidecar lock the
        appenders take, write a temp file, ``replace_atomic``. Somebody else's
        in-flight append can therefore never be lost to this rewrite, and a crash
        leaves either the old file or the new one — never a partial one.
        """
        try:
            path = self.spool_path
            if not path.exists() or path.stat().st_size <= SPOOL_COMPACT_BYTES:
                return False
            from ouroboros.platform_layer import (
                acquire_exclusive_file_lock,
                release_exclusive_file_lock,
            )
            from ouroboros.utils import jsonl_append_lock_path, replace_atomic

            lock_path = jsonl_append_lock_path(path)
            lock_fd = acquire_exclusive_file_lock(lock_path, timeout_sec=2.0, stale_sec=10.0)
            try:
                # Decided UNDER the lock: a record appended after this point would
                # be erased by the rewrite, and unforwarded memory is not ours to
                # drop.
                if _pending_records(path):
                    return False
                tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
                tmp.write_text("", encoding="utf-8")
                replace_atomic(tmp, path)
                return True
            finally:
                release_exclusive_file_lock(lock_path, lock_fd)
        except Exception:
            log.debug("Engram spool compaction failed", exc_info=True)
            return False

    def _flush(self, *, limit: int) -> int:
        acked = 0
        for record in self.pending()[: max(1, int(limit))]:
            result = self._send(record)
            if not result.ok:
                self._disclose_once(result)
                break
            _spool_append(self.spool_path, {"v": SPOOL_SCHEMA_VERSION, "op": "ack",
                                            "spool_id": record.get("spool_id"), "ts": _now()})
            acked += 1
        self.compact_if_idle()
        return acked

    def _disclose_once(self, result: EngramResult) -> None:
        """Disclose at most ONE unreachable-Engram event per run. Never silent.

        The disclosure goes to a **dedicated** ``logs/engram.jsonl``, not to
        ``logs/events.jsonl``. The canonical events log has consumers that reason
        about its *ordering* — several startup checks assert on the last event —
        so appending a memory-transport notice there would displace their
        records and break contracts this change has no business touching.
        """
        if self._disclosed:
            return
        self._disclosed = True
        try:
            from ouroboros.utils import append_jsonl

            append_jsonl(
                pathlib.Path(self.drive_root) / "logs" / "engram.jsonl",
                {
                    "ts": _now(),
                    "type": "engram_unavailable",
                    "error_kind": result.error_kind,
                    "status": result.status,
                    "detail": str(result.detail or "")[:300],
                    # A socket-only deployment is a client limitation, not an
                    # outage; naming it here keeps the operator off the wrong trail.
                    "socket_configured": bool(_env_socket()),
                    "spooled": self.pending_count(),
                },
            )
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# spool primitives — append-only, crash-safe, never raise on read
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spool_append(path: pathlib.Path, record: Mapping[str, Any]) -> bool:
    try:
        from ouroboros.utils import append_jsonl

        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(append_jsonl(path, dict(record)))
    except Exception:
        return False


def _read_spool(path: pathlib.Path) -> List[Dict[str, Any]]:
    """Read the spool, skipping a torn/corrupt tail rather than failing."""
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue          # torn final line: skip, do not fail
            if isinstance(value, dict):
                rows.append(value)
    except OSError:
        return rows
    return rows


def _pending_records(path: pathlib.Path) -> List[Dict[str, Any]]:
    """Adds that have no matching ack. Later adds win on duplicate spool_id."""
    adds: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    acked: set = set()
    for row in _read_spool(path):
        spool_id = str(row.get("spool_id") or "")
        if row.get("op") == "add" and spool_id:
            if spool_id not in adds:
                order.append(spool_id)
            adds[spool_id] = row
        elif row.get("op") == "ack" and spool_id:
            acked.add(spool_id)
    return [adds[sid] for sid in order if sid not in acked]


def build_sink(
    repo_root: Any,
    drive_root: Any,
    *,
    base_url: str = "",
    project: str = "",
    session_id: str = "",
    timeout: float = 5.0,
) -> EngramSink:
    """Construct a sink for one run. Never raises (C6).

    A misconfigured sink degrades to "everything spools, one disclosure" instead
    of failing the task — and it refuses to send, so a bad scope can never leak
    memories into a neighbouring project (F7).
    """
    from ouroboros.engram_client import (
        DEFAULT_BASE_URL,
        EngramConfig,
        env_base_url,
        env_project,
        env_token,
    )

    resolved_base = str(base_url or env_base_url() or DEFAULT_BASE_URL)
    explicit = str(project or env_project() or "")
    token = env_token()
    config_error = ""
    try:
        # Offline first so a forbidden/blank name is caught here rather than on the
        # first send, and so there is always something usable when the server is
        # down.
        resolved_project = resolve_project(repo_root, explicit=explicit)
    except Exception as exc:
        # The name is unusable. Build a client that CANNOT send — the placeholder
        # never reaches the wire because config_error short-circuits every send —
        # so a bad scope spools and discloses instead of scattering memories.
        resolved_project = "_unresolved"
        config_error = f"{type(exc).__name__}: {exc}"

    # Then let the SERVER own project policy, which is what Engram's own reference
    # client does ("Resolve the project through the server, which owns project
    # policy"): one GET /project/current?cwd=, so the two sides cannot disagree
    # about which project this repository is. An operator override still wins, and
    # an unusable answer (blank/forbidden/ambiguous) is ignored rather than
    # adopted — the offline resolution then stands.
    if not explicit and not config_error:
        server_project = _server_project(
            resolved_base, token, repo_root, timeout=float(timeout or 5.0)
        )
        if server_project:
            resolved_project = server_project

    client = EngramClient(
        config=EngramConfig(
            base_url=resolved_base,
            project=resolved_project,
            token=token,
            timeout=float(timeout or 5.0),
        )
    )
    return EngramSink(
        client=client,
        drive_root=pathlib.Path(drive_root),
        session_id=session_id,
        repo_root=pathlib.Path(repo_root),
        config_error=config_error,
    )


def _server_project(
    base_url: str, token: str, repo_root: Any, *, timeout: float
) -> str:
    """Ask the service which project this repository is. Best-effort, never raises.

    Returns "" when the service is unreachable, the answer is ambiguous, or the
    answer is a name this client refuses (blank / ``local``) — in every one of
    those cases the caller keeps its offline resolution rather than adopting a
    scope nobody vouched for.
    """
    try:
        from ouroboros.engram_client import (
            EngramClient,
            EngramConfig,
            normalize_project_name,
        )

        probe = EngramClient(
            config=EngramConfig(base_url=base_url, project="", token=token, timeout=timeout)
        )
        result = probe.project_current(cwd=str(repo_root or ""))
        if not result.ok:
            return ""
        payload = result.one() or {}
        if str(payload.get("error_hint") or "").strip():
            return ""  # ambiguous or refused: detection did not decide
        name = normalize_project_name(payload.get("project"))
        if not name or name in _FORBIDDEN_PROJECTS:
            return ""
        return name
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# wiring helpers
#
# Call sites stay one line each: they hand us ``env`` and the artefact they
# already produced. One sink is shared per drive root because the per-run emit
# cap and the "disclose once" rule are only meaningful if a run shares state —
# a fresh sink per emit would defeat both.
# --------------------------------------------------------------------------- #

_SINKS: Dict[Any, EngramSink] = {}
_SINKS_LOCK = threading.Lock()


def sink_for(target: Any) -> EngramSink:
    """Process-scoped sink for a drive root. Never raises.

    ``target`` may be an ``env``-like object or a bare path to the drive root —
    several call sites only have the root in hand. Both attribute spellings are
    accepted (``drive_root``/``repo_dir`` as ``Env`` uses them, and
    ``DRIVE_ROOT``/``REPO_DIR`` as the supervisor's ctx uses them): a shape
    mismatch here silently resolves the WRONG PROJECT rather than failing, and a
    wrong project is the one error this function exists to prevent.

    The cache is keyed on the drive root **and** the resolved repo root. Keying on
    the drive alone made the project the whole process uses depend on which caller
    happened to touch the sink first — a bare-drive caller (an evolution
    checkpoint at boot) would pin every later Env-shaped caller to the drive's own
    directory name. Two callers that would resolve different projects must never
    share a sink.
    """
    try:
        explicit = getattr(target, "drive_root", None) or getattr(target, "DRIVE_ROOT", None)
        drive_root = pathlib.Path(explicit) if explicit else pathlib.Path(target)
        repo_attr = getattr(target, "repo_dir", None) or getattr(target, "REPO_DIR", None)
        repo_root = pathlib.Path(repo_attr) if repo_attr else _repo_root_hint(drive_root)
        key = (str(drive_root.resolve(strict=False)), str(repo_root.resolve(strict=False)))
    except Exception:
        drive_root, repo_root, key = pathlib.Path("."), pathlib.Path("."), (".", ".")
    with _SINKS_LOCK:
        sink = _SINKS.get(key)
        if sink is None:
            sink = build_sink(repo_root, drive_root, project=_drive_local_project(drive_root))
            _SINKS[key] = sink
        return sink


def _repo_root_hint(drive_root: pathlib.Path) -> pathlib.Path:
    """Best available repo root when the caller only handed us a drive root."""
    from ouroboros.engram_client import env_repo_root

    candidate = pathlib.Path(env_repo_root()) if env_repo_root() else None
    if candidate is not None and candidate.is_dir():
        return candidate
    return drive_root


def _env_socket() -> str:
    try:
        from ouroboros.engram_client import env_socket

        return env_socket()
    except Exception:
        return ""


def _drive_local_project(drive_root: pathlib.Path) -> str:
    """A ``.engram/config.json`` sitting next to the drive wins over the repo name.

    An operator who pins a project on the drive means it for every consumer of
    that drive, including the ones that never see the repo root.
    """
    try:
        config = pathlib.Path(drive_root) / ".engram" / "config.json"
        if not config.exists():
            return ""
        from ouroboros.engram_client import normalize_project_name

        payload = json.loads(config.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            return normalize_project_name(payload.get("project_name"))
    except Exception:
        return ""
    return ""


def reset_sinks() -> None:
    """Drop cached sinks. Used by tests and at a deliberate run boundary."""
    with _SINKS_LOCK:
        _SINKS.clear()


def flush_engram_spool(env: Any, *, limit: int = MAX_EMITS_PER_RUN) -> int:
    """Forward whatever a previous run had to spool. Returns how many landed.

    This is the second half of write-then-forward (C17): a run that could not
    reach Engram leaves records behind, and the next run drains them.
    """
    try:
        return sink_for(env).flush(limit=limit)
    except Exception:
        return 0


def emit_reflection_memory_actions(
    env: Any, actions: Any, *, task_id: str = "", project_id: str = ""
) -> int:
    """W1 — mirror the reflection's durable memories. Returns how many landed.

    ``actions`` is exactly what the local path already applied, so local
    behaviour is untouched; this only adds the remote sink. Capped at 3 to match
    ``reflection._validate_memory_actions``.
    """
    count = 0
    try:
        sink = sink_for(env)
        for action in list(actions or [])[:3]:
            if sink.emit_memory_action(action, task_id=task_id).accepted:
                count += 1
    except Exception:
        return count
    return count


def emit_task_narrative(env: Any, narrative: Any, *, task_id: str) -> bool:
    """W4 — the authored end-of-task narrative ("episodic memory") summary."""
    try:
        return sink_for(env).emit_continuation_narrative(
            _as_mapping(narrative), task_id=task_id
        ).accepted
    except Exception:
        return False


def push_local_dialogue_blocks(target: Any, blocks: Any) -> int:
    """Mirror the local distilled dialogue blocks into Engram. Returns how many landed.

    Idempotent by identity, so calling it on every boot costs a few no-op upserts
    and can never duplicate a block. This is the one-time/repeatable half of
    replacing `## Dialogue History` with an Engram injection: without it, the
    distilled history that already exists locally would simply stop being
    reachable when the seam changes.
    """
    count = 0
    sink = sink_for(target)
    for block in blocks or ():
        receipt = sink.emit_dialogue_summary(_as_mapping(block))
        if receipt.accepted:
            count += 1
    return count


def emit_evolution_outcome(env: Any, entry: Any) -> bool:
    """WO — an evolution cycle's outcome (task-done or the later cycle verdict)."""
    try:
        return sink_for(env).emit_evolution_checkpoint(_as_mapping(entry)).accepted
    except Exception:
        return False


def emit_review_verdicts(env: Any, rows: Any, *, task_id: str = "") -> int:
    """W9 — review verdicts. Returns how many landed."""
    count = 0
    try:
        sink = sink_for(env)
        for row in list(rows or []):
            if sink.emit_review_verdict(_as_mapping(row), task_id=task_id).accepted:
                count += 1
    except Exception:
        return count
    return count


__all__ = [
    "FIELD_CAP_CHARS",
    "MAX_EMITS_PER_RUN",
    "SINK_KINDS",
    "SPOOL_MAX_ENTRIES",
    "SPOOL_REL",
    "EngramSink",
    "SinkReceipt",
    "build_sink",
    "emit_evolution_outcome",
    "push_local_dialogue_blocks",
    "emit_reflection_memory_actions",
    "emit_review_verdicts",
    "emit_task_narrative",
    "flush_engram_spool",
    "reset_sinks",
    "sink_for",
]
