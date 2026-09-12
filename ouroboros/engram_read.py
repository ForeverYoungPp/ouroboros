"""Bounded, machine-side Engram reads.

This is the read half of Phase 4. Every machine consumer that gets repointed away
from a local file must keep the bound its local predecessor had (seed constraint
C14 / AC16) — if a "bounded digest" silently becomes an unbounded pull, the
memory loop re-inflates exactly the context it was built to shrink.

It also fixes the failure that makes the old instruments lie (C15 / AC17). The
local probes answered "is this file big / how many bytes", and an *empty* answer
was indistinguishable from a *broken* one. Here the outcomes this module mints
are separate and named (``stale`` is not one of them — ``engram_cache`` mints it
at the read-through layer when it serves the last good read):

* ``status="ok"``          — read succeeded, data present
* ``status="empty"``       — read succeeded, the store genuinely holds nothing
* ``status="unavailable"`` — the store could not be read at all
* ``status="rejected"``    — the request was REFUSED: the store ANSWERED and refused it
                             (a 4xx), or the CLIENT refused to send it because the scope is
                             unresolved — never a transport failure

A consumer that cannot tell ``empty`` from ``unavailable``/``rejected`` apart will
report "no memory" when the truth is "no service" (or a refused request), which is
how a memory-loss signal becomes noise.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List

from ouroboros.engram_client import EngramConfigError

#: Mirrors ``improvement_backlog.format_backlog_digest(max_chars=3000)``.
MAX_DIGEST_CHARS = 3_000
#: Mirrors ``improvement_backlog.format_backlog_digest(limit=8)``.
MAX_DIGEST_ITEMS = 8
#: Engram's own ceiling for any ``limit``.
MAX_COUNT_LIMIT = 500
#: How many records a RECORD-SHAPED recency read may pull. Separate from
#: ``MAX_COUNT_LIMIT`` on purpose, and much smaller.
#:
#: ``GET /observations/recent`` (and its ``GET /observations`` alias) accepts only
#: project / scope / limit / sort: there is no type filter and no ``compact`` mode,
#: so every row that comes back carries its full body. Asking for the ceiling would
#: therefore mean "fetch the entire store's bodies in one request" — exactly the
#: unbounded pull the three-layer design exists to prevent. A bounded window is the
#: honest substitute, and callers that filter it (``type_digest``) disclose when the
#: window saturated rather than implying they saw everything.
MAX_WINDOW = 50
#: How many due-for-review records one wake-up processes (AC16 backlog class).
MAX_REVIEW_ITEMS = 8
#: Largest body returned for a single knowledge topic.
MAX_TOPIC_CHARS = 4_000
#: Safety ceiling for a read-modify-write base. Deliberately far above any real
#: topic: truncating the base of an append is SILENT DATA LOSS (the write would
#: upsert a shortened record), so this is a guard against an unbounded read, not
#: a tuning knob to be trimmed.
KNOWLEDGE_BASE_HARD_CHARS = 1_000_000


@dataclass(frozen=True)
class MachineRead:
    """Outcome of one machine-side read. Never an exception."""

    ok: bool
    status: str = "ok"            # ok | empty | rejected | unavailable | stale
    count: int = 0
    version: str = ""
    text: str = ""
    detail: str = ""

    @property
    def readable(self) -> bool:
        """True when the caller has CONTENT — including a cached-but-stale read."""
        return self.status in {"ok", "empty", "stale"}

    @property
    def stale(self) -> bool:
        """True when the content came from cache because the store could not be read."""
        return self.status == "stale"

    @property
    def unknown(self) -> bool:
        """True when the store did NOT answer, so presence is UNKNOWN, not absent.

        Two different failures live here and both mean "absence is unproven":

        * ``unavailable`` — the service could not be reached (transport/timeout);
        * ``rejected`` — the service answered and refused the request (an unknown
          project, a missing session, a bad selector).

        They are kept distinct because the FIX differs (start the service vs. fix
        the scope), but every consumer that would otherwise conclude "there is no
        memory" must treat both as unknown. Collapsing them into one status made a
        configuration error read as "Engram is unreachable" while /health was 200.
        """
        return self.status in {"unavailable", "rejected"}

    def as_metadata(self) -> Dict[str, Any]:
        """Compact projection for a durable record (no bodies)."""
        return {"status": self.status, "count": self.count, "version": self.version}


@dataclass(frozen=True)
class ReviewBatch:
    """Records Engram says are due, plus the ids needed to advance the cycle.

    C16 moves consciousness onto Engram's own ``review_after`` decay instead of a
    locally maintained watermark. Advancing the cycle is a separate, later step
    (``POST /review/mark_reviewed``) and must not happen when the cycle did not
    actually complete — the ids ride back with the read so the caller can decide.
    """

    read: MachineRead
    ids: tuple = ()
    titles: tuple = ()

    @property
    def ok(self) -> bool:
        return self.read.ok

    @property
    def readable(self) -> bool:
        return self.read.readable


def _read_failure(result: Any) -> MachineRead:
    """Classify a failed read from what the store actually did.

    A 4xx is the server ANSWERING — an unknown project, a missing session, an
    invalid selector. Calling that "unavailable" told the reader the service was
    down when it was up and had refused the request, which sends the operator to
    the wrong problem entirely.

    The same reasoning covers a CLIENT-side refusal: when the scope could not be
    resolved the request is never sent at all, and reporting that as
    ``unavailable`` would print "the memory service could not be reached" for a
    configuration problem. The status WORD is what the section renderers print, so
    the `detail` alone cannot carry it.
    """
    kind = str(getattr(result, "error_kind", "") or "unknown")
    status = getattr(result, "status", None)
    detail = str(getattr(result, "detail", ""))[:200]
    http_rejected = kind == "http" and isinstance(status, int) and 400 <= status < 500
    refused = http_rejected or kind == "config"
    return MachineRead(
        False,
        status="rejected" if refused else "unavailable",
        detail=f"{kind} {status if status is not None else ''}: {detail}".strip(),
    )


def recent(client: Any, *, limit: int = MAX_WINDOW) -> MachineRead:
    """Bounded read of the newest records. Returns counts and a version token."""
    bounded = max(1, min(int(limit or MAX_WINDOW), MAX_COUNT_LIMIT))
    result = client.recent(limit=bounded)
    if not result.ok:
        return _read_failure(result)
    items: List[Dict[str, Any]] = result.items()
    if not items:
        return MachineRead(True, status="empty", count=0, version=_version_token("", 0, ""))
    latest = max(str(item.get("updated_at") or item.get("created_at") or "") for item in items)
    project = str(client.config.project or "")
    return MachineRead(
        True,
        status="ok",
        count=len(items),
        version=_version_token(project, len(items), latest),
        text="\n".join(_line(item) for item in items[:MAX_DIGEST_ITEMS]),
    )


def memory_version(client: Any) -> MachineRead:
    """A stable token that changes when the project's Engram memory changes.

    Replaces the old ``knowledge_index_sha256`` for records whose knowledge no
    longer lives in a local file. It reports ``status`` alongside the token so a
    reader can tell "unchanged" from "could not read" — the old sha could not,
    and an unreadable file hashed exactly like an empty one.
    """
    return recent(client)


def entry_count(client: Any) -> MachineRead:
    """How many records this project holds. Typed when unreachable, and BODY-FREE.

    ``GET /stats`` answers a count question with a count (``total_observations``);
    pulling a window of full records just to take ``len()`` of them would be the
    same "one request, all bodies" mistake in miniature. The version token is left
    empty here because a count is not a version — ``memory_version`` is.
    """
    try:
        result = client.stats()
    except Exception as exc:
        return MachineRead(False, status="unavailable", detail=type(exc).__name__)
    if not result.ok:
        return _read_failure(result)
    payload = result.one() or {}
    total = payload.get("total_observations")
    if not str(total if total is not None else "").lstrip("-").isdigit():
        return MachineRead(
            True, status="empty", count=0, detail="stats carried no total_observations"
        )
    count = int(total)
    return MachineRead(True, status="ok" if count else "empty", count=count)


def digest(
    client: Any,
    *,
    limit: int = MAX_DIGEST_ITEMS,
    max_chars: int = MAX_DIGEST_CHARS,
    window: int = MAX_WINDOW,
) -> MachineRead:
    """A bounded, human-readable digest — the shape ``format_backlog_digest`` had.

    Bounds come from the local predecessor by default, so a repointed consumer
    costs what it used to cost rather than whatever the store happens to hold.

    ``window`` is the same knob ``type_digest`` exposes, for the same reason: the
    recency read returns full bodies, so asking for 50 to render 5 lines is ten
    times the traffic for the same answer. A caller that wants only a few lines
    passes the few it wants.
    """
    read = recent(client, limit=_bounded_items(window, MAX_COUNT_LIMIT))
    if not read.ok:
        return read
    if read.status == "empty":
        return read
    lines = read.text.splitlines()[: _bounded_items(limit, MAX_WINDOW)]
    body = _bounded_body(lines, max_chars)
    return MachineRead(True, status="ok", count=read.count, version=read.version, text=body)


def due_for_review(
    client: Any, *, limit: int = MAX_REVIEW_ITEMS, max_chars: int = MAX_DIGEST_CHARS
) -> ReviewBatch:
    """``GET /review`` — the native consumption cursor, bounded like its predecessor.

    Consciousness used to keep its own watermark file; C16 replaces that with
    Engram's ``review_after`` decay. The bound stays explicit (AC16) so one
    wake-up cannot silently turn into an unbounded pull of the whole store.
    """
    result = client.review(limit=_bounded_items(limit, MAX_REVIEW_ITEMS))
    if not result.ok:
        return ReviewBatch(_read_failure(result))
    items: List[Dict[str, Any]] = result.items()
    if not items:
        return ReviewBatch(MachineRead(True, status="empty", count=0, version=_version_token("", 0, "")))
    ids = tuple(int(item["id"]) for item in items[:MAX_REVIEW_ITEMS] if str(item.get("id", "")).lstrip("-").isdigit())
    titles = tuple(" ".join(str(item.get("title") or "").split())[:120] for item in items[:MAX_REVIEW_ITEMS])
    latest = max(str(item.get("updated_at") or item.get("created_at") or "") for item in items)
    project = str(client.config.project or "")
    return ReviewBatch(
        MachineRead(
            True,
            status="ok",
            count=len(items),
            version=_version_token(project, len(items), latest),
            text=_bounded_body([_line(item) for item in items[:MAX_REVIEW_ITEMS]], max_chars),
        ),
        ids=ids,
        titles=titles,
    )


def _bounded_items(limit: Any, ceiling: int) -> int:
    return max(1, min(int(limit or ceiling), int(ceiling)))


#: The `engram` tool's own read op — the default continuation surface.
_SURFACE_ENGRAM_READ = "op='read' offset={offset}"


def _truncation_note(shown: int, total: int, *, surface: str) -> str:
    """The truncation marker, carrying the offset that continues the record.

    A bare ``…[truncated]`` says the text stopped but not where to resume, and the
    record is already here in full — the bound is a display bound. ``surface`` is a
    parameter because the note lands in whatever tool produced the text: a lane that
    holds ``knowledge_read`` and not ``engram`` cannot act on a pointer to the wrong
    one, and an ephemeral turn holds neither.
    """
    return (
        f"\n…[truncated at char {shown} of {total} — continue with "
        + surface.format(offset=shown)
        + "]"
    )


def _truncate_body(body: str, budget: int, *, surface: str = _SURFACE_ENGRAM_READ) -> str:
    """Cut a body to ``budget`` INCLUDING the note, so the caller's bound still holds.

    The note costs more than the 30 chars the bare marker did, so the cut has to
    account for it: a continuation hint that pushes the text past the bound it was
    supposed to respect would trade one dishonesty for another.
    """
    if len(body) <= budget:
        return body
    total = len(body)
    shown = max(0, budget - 30)
    for _ in range(3):
        note = _truncation_note(shown, total, surface=surface)
        if shown + len(note) <= budget:
            break
        shown = max(0, budget - len(note))
    trimmed = body[:shown].rstrip()
    return trimmed + _truncation_note(len(trimmed), total, surface=surface)


def _window_body(
    body: str, *, offset: int, budget: int, surface: str, past_end: str
) -> str:
    """One window of a full body, with head and tail continuation markers.

    Offsets index the RECORD, so the offset a note names is directly reusable as the
    next call's argument — that is the whole point of naming it. The result never
    exceeds ``budget``, and an offset that starts past the end is disclosed by the
    caller's own sentence rather than answered with an empty body.
    """
    total = len(body)
    start = max(0, int(offset or 0))
    if start and start >= total:
        return past_end
    head = f"[continued from char {start} of {total}]\n" if start else ""
    room = max(0, budget - len(head))
    window = body[start:start + room]
    following = start + len(window)
    if following >= total:
        return head + window
    note = _truncation_note(following, total, surface=surface)
    if len(head) + len(window) + len(note) > budget:
        window = window[:max(0, budget - len(head) - len(note))]
        following = start + len(window)
        note = _truncation_note(following, total, surface=surface)
    return head + window + note


def _bounded_body(lines: List[str], max_chars: Any) -> str:
    budget = max(200, min(int(max_chars or MAX_DIGEST_CHARS), MAX_DIGEST_CHARS))
    return _truncate_body("\n".join(lines), budget)


def type_digest(
    client: Any,
    type_name: str,
    *,
    limit: int = MAX_DIGEST_ITEMS,
    max_chars: int = MAX_DIGEST_CHARS,
    window: int = MAX_WINDOW,
) -> MachineRead:
    """Bounded digest of the newest records of ONE type.

    Engram has no type-filtered recency read: ``GET /observations/recent`` and its
    ``GET /observations`` alias accept only project / scope / limit / sort, and
    ``GET /search`` requires a text query. So the type filter is applied here, over
    a **bounded window** of the newest records.

    When the window is full and holds fewer matches than were asked for, the result
    says so: there may be older records of this type that the window could not
    reach. Under-reporting a bounded read is acceptable; presenting it as the
    complete set is not, and that distinction is the whole point of this module.
    """
    wanted = _bounded_items(limit, MAX_DIGEST_ITEMS)
    span = _bounded_items(window, MAX_COUNT_LIMIT)
    raw = client.recent(limit=span)
    if not raw.ok:
        return _read_failure(raw)
    items: List[Dict[str, Any]] = raw.items()
    if not items:
        return MachineRead(True, status="empty", count=0, version=_version_token("", 0, ""))
    matches = [item for item in items if str(item.get("type") or "") == str(type_name)]
    if not matches:
        return MachineRead(
            True, status="empty", count=0, detail=f"no {type_name!r} record in the newest {span}"
        )
    chosen = matches[:wanted]
    latest = max(str(item.get("updated_at") or item.get("created_at") or "") for item in matches)
    text = _bounded_body([_line(item) for item in chosen], max_chars)
    if len(matches) < wanted and len(items) >= span:
        # The window saturated before it could satisfy the request. Say it.
        text += (
            f"\n(window saturated at {span} records: older {type_name!r} records may exist "
            "beyond this window)"
        )
    return MachineRead(
        True,
        status="ok",
        count=len(matches),
        version=_version_token(str(client.config.project or ""), len(matches), latest),
        text=text,
    )


def search_titles(
    client: Any,
    query: str,
    *,
    type_name: str = "",
    limit: int = MAX_DIGEST_ITEMS,
    max_chars: int = MAX_DIGEST_CHARS,
    match_mode: str = "",
) -> MachineRead:
    """Layer-1 keyword recall: ONE title line per hit, never a body.

    ``match_mode="any"`` is what makes a natural-language query usable as a
    retrieval question: under Engram's default (AND) a sentence only matches a
    record that contains EVERY one of its tokens, so a multi-word query from an
    owner message or an observation payload returns nothing and the caller
    silently falls back to recency. Callers that want precision pass "" (the
    server default) instead.

    The server returns full observations from ``/search`` (verified against
    ``buildSearchFTSQuery``), so the layering is enforced HERE: rendering titles
    is what keeps an automatic per-turn recall from shipping bodies into the
    prompt — which is exactly how the section this replaces reached 30 KB.
    """
    text = str(query or "").strip()
    if not text:
        return MachineRead(True, status="empty", count=0, detail="empty query")
    wanted = _bounded_items(limit, MAX_DIGEST_ITEMS)
    result = client.search(
        text, limit=wanted, type=type_name or "", match_mode=match_mode or ""
    )
    if not result.ok:
        return _read_failure(result)
    items: List[Dict[str, Any]] = result.items()
    if type_name:
        items = [item for item in items if str(item.get("type") or "") == type_name]
    if not items:
        return MachineRead(True, status="empty", count=0, detail=f"no {type_name or 'record'} matched")
    latest = max(str(item.get("updated_at") or item.get("created_at") or "") for item in items)
    return MachineRead(
        True,
        status="ok",
        count=len(items),
        version=_version_token(str(client.config.project or ""), len(items), latest),
        text=_bounded_body([_line(item) for item in items[:wanted]], max_chars),
    )


def knowledge_topic(
    client: Any,
    topic: str,
    *,
    scope: str = "",
    max_chars: int = MAX_TOPIC_CHARS,
    offset: int = 0,
) -> MachineRead:
    """One knowledge topic by ``topic_key`` — bounded, targeted, typed.

    The local ``memory/knowledge/<topic>.md`` write is what S2 stops, so this is
    the read path that must exist *before* the stop (C13 read-before-stop). It is
    a single-record fetch, not a store sweep: layer 2 searches for the row, layer
    3 reads its body. An unreachable store reports ``unavailable`` so a caller can
    say "unknown" instead of "you never learned this".
    """
    name = str(topic or "").strip()
    if not name:
        return MachineRead(False, status="empty", detail="empty topic")
    identity = f"knowledge:{name}"
    found = client.search(name, limit=MAX_DIGEST_ITEMS, type="knowledge", scope=scope)
    if not found.ok:
        return _read_failure(found)
    items: List[Dict[str, Any]] = found.items()
    match = next((r for r in items if str(r.get("topic_key") or "") == identity), None)
    if match is None:
        # Defensive: an Engram build that omits ``topic_key`` from search hits
        # still carries the title the sink wrote.
        match = next((r for r in items if str(r.get("title") or "") == f"Knowledge: {name}"), None)
    if match is None:
        # A search window is a RANKED SLICE, not the set: the store grew from 3 to
        # 45 knowledge records, so a target can rank below ``limit`` and the old
        # code called that "no Engram record" — an under-report dressed as a
        # whole-set absence, which this module's own doctrine forbids. The fallback
        # sweeps the newest records by exact ``topic_key``, and it is bounded by
        # ``MAX_WINDOW`` — the module's own bound for record-shaped recency reads
        # (see that block above): ``/observations/recent`` has no type filter and no
        # compact mode, so EVERY row carries its full body, which makes this a
        # bounded read and not a store dump. Same resolved scope, only in the miss
        # case; a second miss is reported as the BOUNDED absence it is.
        scan = client.recent(limit=MAX_WINDOW, scope=scope)
        if not scan.ok:
            # Isomorphic with the two sibling reads above: a FAILED read is not an
            # absence. Falling through to the bounded-absence branch would assert
            # "an exact-key scan of the newest N records" for a scan that never
            # completed — the collapse `unavailable`/`rejected` exist to prevent.
            return _read_failure(scan)
        scanned: List[Dict[str, Any]] = scan.items()
        match = next(
            (r for r in scanned if str(r.get("topic_key") or "") == identity), None
        )
        if match is None:
            match = next(
                (r for r in scanned if str(r.get("title") or "") == f"Knowledge: {name}"),
                None,
            )
    if match is None:
        return MachineRead(
            True,
            status="empty",
            count=0,
            detail=(
                f"no Engram record for {identity!r} within the read bounds: the knowledge "
                f"search window (limit={MAX_DIGEST_ITEMS}) and an exact-key scan of the "
                f"newest {MAX_WINDOW} records — absent from these bounds is not proof the "
                "record was never stored"
            ),
        )
    # The server's search shape already carries the body (``buildSearchFTSQuery``
    # selects the full observation), so a hit with content costs ONE round trip.
    # Only fall back to fetching the record when the store answered with a
    # body-less shape — a preview-style response or an older build.
    record = match
    if not str(match.get("content") or "").strip():
        raw_id = str(match.get("id", ""))
        if not raw_id.lstrip("-").isdigit():
            # A MATCH was found: reporting it as ``empty`` would claim the store
            # holds nothing, which is the collapse every other branch here refuses.
            return MachineRead(
                False,
                status="unavailable",
                count=len(items),
                detail=(
                    f"matched {identity!r} but its id is unusable ({raw_id!r}): the record "
                    "exists and could not be read — this is not an absence"
                ),
            )
        got = client.get(int(raw_id))
        if not got.ok:
            return _read_failure(got)
        fetched = got.one()
        if not fetched:
            # got.ok with no record is a FAILURE dressed as success: never return
            # ``status="ok"`` with an empty body for a record that never arrived.
            return MachineRead(
                False,
                status="unavailable",
                count=len(items),
                detail=(
                    f"matched {identity!r} but the store answered id {raw_id} with no record: "
                    "the record exists and could not be read — this is not an absence"
                ),
            )
        record = fetched
    body = str(record.get("content") or "")
    budget = max(200, min(int(max_chars or MAX_TOPIC_CHARS), KNOWLEDGE_BASE_HARD_CHARS))
    body = _window_body(
        body,
        offset=offset,
        budget=budget,
        # The note must name a tool THIS reader holds: `knowledge_read` carries the
        # same offset argument, so a lane without the `engram` tool can still continue.
        surface=f"knowledge_read({topic!r}, offset={{offset}})",
        past_end=(
            f"offset {max(0, int(offset or 0))} is past the end of this topic "
            f"({len(body)} chars) — nothing further to read."
        ),
    )
    return MachineRead(
        True,
        status="ok",
        count=1,
        version=_version_token(
            str(client.config.project or ""), 1, str(record.get("updated_at") or "")
        ),
        text=body,
    )


def continuation_narrative(
    client: Any, task_id: str, *, max_chars: int = MAX_TOPIC_CHARS
) -> MachineRead:
    """The authored end-of-task narrative Engram holds for ``task_id`` (W4 / S4).

    Located by the identity the sink writes (``continuation:<task_id>``), which the
    server indexes as part of ``topic_key``, so a task_id query finds it. The match
    is confirmed on the identity rather than trusted from ranking: a task_id also
    appears inside other records' bodies, and returning one of those as "the
    predecessor's own account" would be a fabrication, not a lookup.
    """
    tid = str(task_id or "").strip()
    if not tid:
        return MachineRead(False, status="empty", detail="empty task_id")
    identity = f"continuation:{tid}"
    found = client.search(tid, limit=MAX_DIGEST_ITEMS, type="episodic_memory")
    if not found.ok:
        return _read_failure(found)
    items: List[Dict[str, Any]] = found.items()
    match = next((r for r in items if str(r.get("topic_key") or "") == identity), None)
    if match is None:
        return MachineRead(
            True, status="empty", count=0, detail=f"no {identity!r} record in the newest matches"
        )
    body = str(match.get("content") or "")
    if not body.strip():
        raw_id = str(match.get("id", ""))
        if not raw_id.lstrip("-").isdigit():
            return MachineRead(
                False,
                status="unavailable",
                count=len(items),
                detail=(
                    f"matched {identity!r} but its id is unusable ({raw_id!r}): the record "
                    "exists and could not be read — this is not an absence"
                ),
            )
        got = client.get(int(raw_id))
        if not got.ok:
            return _read_failure(got)
        fetched = got.one()
        if not fetched:
            return MachineRead(
                False,
                status="unavailable",
                count=len(items),
                detail=(
                    f"matched {identity!r} but the store answered id {raw_id} with no record: "
                    "the record exists and could not be read — this is not an absence"
                ),
            )
        body = str(fetched.get("content") or "")
    budget = max(200, min(int(max_chars or MAX_TOPIC_CHARS), KNOWLEDGE_BASE_HARD_CHARS))
    body = _truncate_body(body, budget)
    return MachineRead(
        True,
        # The record was obtained; ``empty`` here would claim absence. An existing
        # record with no body is a successful read of an empty record, and the
        # version token carries which one — never a claim that nothing exists.
        status="ok",
        count=1,
        version=_version_token(str(client.config.project or ""), 1, str(match.get("updated_at") or "")),
        text=body,
    )


def client_for(target: Any) -> Any:
    """Repo-pinned client for reads — same configuration path as the write sink.

    The sink is made ready first, because a project-scoped READ is also refused
    until the project exists in the store (``404 unknown_project``). Reads create
    nothing beyond that one idempotent session call.

    When the project cannot be resolved offline (blank/forbidden name), the sink
    builds a client marked ``_unresolved`` and its WRITE path refuses at
    ``_send`` — but its read methods would still go to the wire and come back
    as a bare 404, which reads ``there is no memory`` rather than ``this is a
    configuration fact``. Reads must fail closed on the same signal, so an
    unresolvable scope raises here; every call site already carries the typed
    not-configured / unknown branch for exactly this outcome.
    """
    from ouroboros.engram_sink import sink_for

    sink = sink_for(target)
    if sink.config_error:
        raise EngramConfigError(str(sink.config_error))
    sink.ensure_ready()
    return sink.client


def _version_token(project: str, count: int, latest: str) -> str:
    payload = json.dumps(
        {"project": project, "count": int(count), "latest": latest},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _line(record: Dict[str, Any]) -> str:
    obs_id = record.get("id", "?")
    kind = str(record.get("type") or "?").strip() or "?"
    title = " ".join(str(record.get("title") or "").split())[:120]
    return f"- [{obs_id}] ({kind}) {title}"


__all__ = [
    "KNOWLEDGE_BASE_HARD_CHARS",
    "MAX_COUNT_LIMIT",
    "MAX_DIGEST_CHARS",
    "MAX_DIGEST_ITEMS",
    "MAX_REVIEW_ITEMS",
    "MAX_TOPIC_CHARS",
    "MAX_WINDOW",
    "MachineRead",
    "ReviewBatch",
    "client_for",
    "continuation_narrative",
    "digest",
    "due_for_review",
    "entry_count",
    "knowledge_topic",
    "memory_version",
    "recent",
    "search_titles",
    "type_digest",
]
