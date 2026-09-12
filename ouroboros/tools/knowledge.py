"""Persistent topic-based knowledge files with an auto-maintained index."""

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List

from ouroboros.platform_layer import file_lock_exclusive, file_unlock
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

KNOWLEDGE_DIR = "memory/knowledge"
INDEX_FILE = "index-full.md"
#: The listing is the LOCAL (pre-switch) archive index. Topics written to Engram
#: after the canonical local write was retired do NOT appear in it, so the list has
#: to say so — otherwise it reads as "these are all the topics" (the same
#: misreading ``_engram_list_note`` guards on the empty path).
_LOCAL_INDEX_SCOPE_NOTE = (
    "ℹ️ LOCAL (pre-switch) ARCHIVE INDEX — `memory/knowledge` on this drive. Durable "
    "knowledge written to Engram after the local write was retired does NOT appear "
    "here; reach those with the `engram` tool (op=search, then op=read).\n\n"
)
# The immune improvement backlog is ONE global store, never per-project (C10.1).
BACKLOG_TOPIC = "improvement-backlog"

#: Topics whose only home is the LOCAL archive under the Engram-only ruling
#: (option i): `patterns` and the local index have no Engram record, and the
#: improvement backlog is its own branch above (C10.1). Every other topic
#: resolves in Engram alone — a miss there is not an invitation to serve a
#: pre-switch local file that Engram has since superseded.
LOCAL_ARCHIVE_TOPICS = frozenset({"patterns", "index-full", BACKLOG_TOPIC})

#: The STORE-SIDE title index, maintained by the write path. It is the answer to a
#: question nothing else on this drive could answer: WHICH topics exist in Engram.
#: `index-full.md` is the retired canonical local index — frozen at the pre-switch
#: archive and unable to name anything written since — and Engram itself has no
#: type-enumerating route (`/observations/recent` takes no type filter, `/stats` has
#: no breakdown, `/search` needs a non-empty query). So the index is built where the
#: facts already are: the write path holds the topic, the identity and the outcome
#: at the moment it mirrors the record.
INDEX_LEDGER = "knowledge_index.jsonl"

#: Bound on DISTINCT indexed topics (not on writes). Past it the least-recently
#: written topic is evicted and the eviction is written as its own audit row, so a
#: drop is recoverable and never silent (BIBLE P1 / no silent truncation).
KNOWLEDGE_INDEX_TOPIC_CAP = 400

#: Status for a row reconstructed from the provenance log by the seed: the write is
#: recorded there, but that file does not carry the transport outcome.
INDEX_STATUS_SEEDED = "seeded"

#: S2 stop switch. The CANONICAL ``memory/knowledge/<topic>.md`` + ``index-full.md``
#: write is RETIRED: its readers (main chat, consciousness, deep self-review) now
#: read Engram, so leaving the local write on would keep feeding a store nobody
#: consults — and would keep 20 KB of always-on context alive in the background
#: loop. It is an explicit switch rather than a deleted code path so the stop is
#: greppable and a rollback is one line (C2 asks for "comment out / disable", not
#: for removal). Existing local files are NEVER deleted: they stay as the
#: read-only archive of everything learned before the switch (C2 / BIBLE P1).
CANONICAL_LOCAL_WRITE = False


def _local_write_live(ctx: ToolContext) -> bool:
    """Whether this write still lands in a local ``<topic>.md`` file.

    Project-scoped facts keep their local store. The stop names only the canonical
    ``memory/knowledge/`` path, and per-project isolation lives in the *path*
    (``projects/<id>/knowledge``) with no Engram scope equivalent — stopping it
    would silently merge one project's facts into the repo-wide scope, which is a
    guarded contract (see ``tests/test_project_facts.py``), not a free win.
    """
    return bool(str(getattr(ctx, "project_id", "") or "").strip()) or CANONICAL_LOCAL_WRITE


def _valid_topic_or_empty(topic: Any) -> str:
    """``topic`` when it is a usable topic name, else ``""`` (never raises)."""
    try:
        return _sanitize_topic(str(topic or ""))
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# The store-side title index (see INDEX_LEDGER).
# --------------------------------------------------------------------------- #


def _history_path(ctx: ToolContext) -> Path:
    """The provenance log this index is seeded from — same dir, same expression."""
    return _knowledge_dir(ctx).parent / "knowledge_history.jsonl"


def _index_ledger_path(ctx: ToolContext) -> Path:
    return _knowledge_dir(ctx).parent / INDEX_LEDGER


def _index_rows_from_history(ctx: ToolContext) -> List[dict]:
    """Newest row per topic, reconstructed from the provenance log. Never raises.

    This is what makes the index useful on a drive that predates it: the log already
    records every topic written since the switch (including the backlog's
    topic-only rows — see ``_record_backlog_history``), so a reader never needs a
    window read or a server change to name them. Rows the log cannot describe
    (patterns/index-full, which are local-only by ruling) are left to the archive.
    """
    path = _history_path(ctx)
    if not path.exists():
        return []
    rows: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            topic = _valid_topic_or_empty(row.get("topic"))
            if not topic or topic in LOCAL_ARCHIVE_TOPICS:
                continue
            ts = str(row.get("ts") or "")
            previous = rows.get(topic)
            if previous is not None and str(previous.get("ts") or "") > ts:
                continue
            rows[topic] = {
                "ts": ts,
                "topic": topic,
                "identity": f"knowledge:{topic}",
                "scope": "",
                "mode": str(row.get("mode") or ""),
                "status": INDEX_STATUS_SEEDED,
            }
    except Exception:
        log.debug("knowledge index: provenance log unreadable", exc_info=True)
        return []
    return list(rows.values())


def _index_rows_from_ledger(path: Path) -> List[dict]:
    """Newest row per topic from the ledger itself. Eviction rows are audit only."""
    rows: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("event") or ""):
                continue  # an audit row (an eviction), not a topic row
            topic = _valid_topic_or_empty(row.get("topic"))
            if not topic:
                continue
            ts = str(row.get("ts") or "")
            previous = rows.get(topic)
            if previous is not None and str(previous.get("ts") or "") > ts:
                continue
            rows[topic] = {
                "ts": ts,
                "topic": topic,
                "identity": str(row.get("identity") or f"knowledge:{topic}"),
                "scope": str(row.get("scope") or ""),
                "mode": str(row.get("mode") or ""),
                "status": str(row.get("status") or ""),
            }
    except Exception:
        log.debug("knowledge index: ledger unreadable", exc_info=True)
        return []
    return list(rows.values())


def knowledge_index_rows(ctx: ToolContext) -> List[dict]:
    """Newest row per topic, newest first — the ONE reader both surfaces use.

    Ledger first (it also carries the transport outcome); the provenance log stands
    in until the first write has seeded the ledger, so the index works on a drive
    that has never written since this feature landed. Reads only: no file is created
    here, which is what lets ``knowledge_list`` stay a POLICY_SKIP read.
    """
    path = _index_ledger_path(ctx)
    rows = (
        _index_rows_from_ledger(path)
        if path.exists()
        else _index_rows_from_history(ctx)
    )
    rows = [r for r in rows if str(r.get("topic") or "") not in LOCAL_ARCHIVE_TOPICS]
    rows.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("topic") or "")), reverse=True)
    return rows


def knowledge_index_line(row: dict) -> str:
    """One rendered index entry. Shared so the listing and the prompt cannot drift."""
    line = f"- {row.get('topic')} — updated {str(row.get('ts') or '')[:10]}"
    identity = str(row.get("identity") or "")
    if identity:
        line += f" · {identity}"
    status = str(row.get("status") or "")
    if status == "spooled":
        line += " (queued, not yet in Engram)"
    elif status in ("refused", "failed", "capped", "no_receipt"):
        line += f" ({status or 'not in Engram'})"
    return line


def _index_write_rows(path: Path, rows: List[dict]) -> bool:
    """Atomically replace the ledger with ``rows`` (tmp + rename). Never raises."""
    try:
        from ouroboros.utils import replace_atomic

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        replace_atomic(tmp, path)
        return True
    except Exception:
        log.debug("knowledge index: rewrite failed", exc_info=True)
        return False


def _index_seed(ctx: ToolContext) -> None:
    """Materialise the ledger from the provenance log, ONCE, before the first append.

    Idempotent (a ledger that exists is never re-seeded) and atomic (a partial seed
    would hide every topic it did not reach from later readers — the same trap the
    local index seeds against, see ``_update_index_entry``). Never raises.
    """
    path = _index_ledger_path(ctx)
    if path.exists():
        return
    rows = _index_rows_from_history(ctx)
    if not rows:
        return
    _index_write_rows(path, rows)


def _index_record(
    ctx: ToolContext, *, topic: str, scope: str, mode: str, status: str
) -> None:
    """Append this write to the ledger (write-through), then keep it bounded.

    Best-effort by design: the memory is already durable (the provenance row and the
    sink), so an index failure must degrade the LISTING, never the write (C6).
    """
    path = _index_ledger_path(ctx)
    row = {
        "ts": utc_now_iso(),
        "topic": topic,
        "identity": f"knowledge:{topic}",
        "scope": scope,
        "mode": mode,
        "status": status,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("knowledge index: append failed", exc_info=True)
        return

    # Bound maintenance, on the write path only (reads never rewrite anything):
    # collapse superseded rows, then enforce the topic cap by evicting the
    # least-recently-written topics WITH an audit row each.
    try:
        current = _index_rows_from_ledger(path)
        keep, evicted = current, []
        if len(current) > KNOWLEDGE_INDEX_TOPIC_CAP:
            ordered = sorted(
                current,
                key=lambda r: (str(r.get("ts") or ""), str(r.get("topic") or "")),
                reverse=True,
            )
            keep = ordered[:KNOWLEDGE_INDEX_TOPIC_CAP]
            evicted = ordered[KNOWLEDGE_INDEX_TOPIC_CAP:]
        if evicted or len(current) > len({r["topic"] for r in current}):
            audit = [
                {"ts": row["ts"], "event": "evict", "topic": r["topic"]}
                for r in evicted
            ]
            _index_write_rows(path, keep + audit)
    except Exception:
        log.debug("knowledge index: maintenance skipped", exc_info=True)



def _backlog_root(ctx: ToolContext) -> Path:
    """Canonical drive root for the global immune backlog. Prefer the canonical
    status root (``budget_drive_root``, set for forked/child drives) so the backlog
    is ONE store that survives forks — never a project-scoped or child-drive copy."""
    return Path(str(getattr(ctx, "budget_drive_root", "") or ctx.drive_root))


def _knowledge_dir(ctx: ToolContext) -> Path:
    """Resolve the knowledge base dir: a per-project store under the CANONICAL data
    dir when the task is project-scoped (Phase 3b), else the canonical
    ``memory/knowledge`` under the task's drive. Project facts thus persist across
    forked/empty child drives and stay isolated from the global memory tree."""
    pid = str(getattr(ctx, "project_id", "") or "").strip()
    if pid:
        from ouroboros.project_facts import project_knowledge_dir

        return project_knowledge_dir(pid)
    return ctx.drive_path(KNOWLEDGE_DIR)

_VALID_TOPIC = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,98}[a-zA-Z0-9]$|^[a-zA-Z0-9]$')
_RESERVED = frozenset({"_index", "index-full", "con", "prn", "aux", "nul"})


def _sanitize_topic(topic: str) -> str:
    """Validate a topic name and raise ValueError on bad input."""
    if not topic or not isinstance(topic, str):
        raise ValueError("Topic must be a non-empty string")

    topic = topic.strip()

    if '/' in topic or '\\' in topic or '..' in topic:
        raise ValueError(f"Invalid characters in topic: {topic}")

    if not _VALID_TOPIC.match(topic):
        raise ValueError(f"Invalid topic name: {topic}. Use alphanumeric, underscore, hyphen, dot.")

    if topic.lower() in _RESERVED:
        raise ValueError(f"Reserved topic name: {topic}")

    return topic


def _safe_path(ctx: ToolContext, topic: str) -> tuple[Path, str]:
    """Build a knowledge path and verify containment."""
    sanitized_topic = _sanitize_topic(topic)
    kdir = _knowledge_dir(ctx)
    path = kdir / f"{sanitized_topic}.md"

    resolved = path.resolve()
    kdir_resolved = kdir.resolve()

    try:
        resolved.relative_to(kdir_resolved)
    except ValueError:
        raise ValueError(f"Path escape detected: {topic}")

    return path, sanitized_topic


def _ensure_dir(ctx: ToolContext):
    """Create the knowledge directory."""
    _knowledge_dir(ctx).mkdir(parents=True, exist_ok=True)


@contextmanager
def _knowledge_write_lock(knowledge_dir: Path):
    """Serialize topic and index mutation on one stable directory sidecar."""
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(knowledge_dir / f"{INDEX_FILE}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        file_lock_exclusive(fd)
    except Exception:
        os.close(fd)
        raise
    try:
        yield
    finally:
        try:
            file_unlock(fd)
        finally:
            os.close(fd)


def _extract_summary(text: str, max_chars: int = 150) -> str:
    """Extract up to three non-heading snippets for the index."""
    lines = text.strip().split("\n")
    snippets = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        clean = stripped.lstrip("-*").strip().lstrip("#").strip()
        if clean:
            snippets.append(clean)
        if len(snippets) >= 3:
            break

    summary = " | ".join(snippets)
    if len(summary) > max_chars:
        summary = summary[:max_chars - 1] + "…"
    return summary


def _update_index_entry(ctx: ToolContext, topic: str):
    """Update the index entry for one topic."""
    kdir = _knowledge_dir(ctx)
    index_path = kdir / INDEX_FILE
    topic_path = kdir / f"{topic}.md"

    _ensure_dir(ctx)

    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")
    else:
        # Seed a FULL index, not just this topic: with the read-path rebuild
        # deleted (#447 J5, "index maintenance stays on the write path"), a
        # one-topic seed would persist a partial index that hides every
        # pre-existing topic file from all later listings.
        seeded = []
        for f in sorted(kdir.glob("*.md")) if kdir.exists() else []:
            if f.name == INDEX_FILE or f.stem == topic:
                continue
            try:
                seeded_topic = _sanitize_topic(f.stem)
            except ValueError:
                continue
            try:
                summary = _extract_summary(f.read_text(encoding="utf-8").strip())
            except Exception:
                # A transient read failure must not hide the topic from every
                # later listing (the persisted index wins over the files): the
                # NAME is known from the filename — keep the entry, mark it.
                log.debug(f"Failed to seed index summary for: {f.stem}", exc_info=True)
                summary = "(summary unavailable at index seed)"
            seeded.append(f"- **{seeded_topic}**: {summary}")
        index_content = "# Knowledge Base Index\n\n" + ("\n".join(seeded) + "\n" if seeded else "")

    lines = index_content.split("\n")
    header_end = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            header_end = i + 1
            if i + 1 < len(lines) and lines[i + 1].strip() == "":
                header_end = i + 2
            break

    header = "\n".join(lines[:header_end])
    entries = [line for line in lines[header_end:] if line.strip() and line.strip() != "(empty)"]

    pattern = f"- **{topic}**:"
    entries = [e for e in entries if not e.strip().startswith(pattern)]

    if topic_path.exists():
        try:
            text = topic_path.read_text(encoding="utf-8").strip()
            summary = _extract_summary(text)
            new_entry = f"- **{topic}**: {summary}"
        except Exception:
            log.debug(f"Failed to read knowledge file for index update: {topic}", exc_info=True)
            new_entry = f"- **{topic}**: (unreadable)"

        entries.append(new_entry)
        entries.sort(key=lambda e: e.lower())

    if entries:
        new_index = header.rstrip("\n") + "\n\n" + "\n".join(entries) + "\n"
    else:
        new_index = header.rstrip("\n") + "\n\n(empty)\n"

    temp_path = index_path.with_suffix(".tmp")
    temp_path.write_text(new_index, encoding="utf-8")
    temp_path.replace(index_path)


def _knowledge_read(ctx: ToolContext, topic: str, offset: int = 0) -> str:
    """Read a knowledge topic, optionally continuing a truncated body at ``offset``."""
    try:
        sanitized_topic = _sanitize_topic(topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    # The improvement backlog always resolves to the ONE global store, regardless
    # of project scope or a forked child drive (C10.1) — never a project copy.
    if sanitized_topic == BACKLOG_TOPIC:
        from ouroboros.improvement_backlog import backlog_path

        path = backlog_path(_backlog_root(ctx))
        if not path.exists():
            return f"Topic '{sanitized_topic}' not found. Use knowledge_list to see available topics."
        return path.read_text(encoding="utf-8")

    try:
        path, sanitized_topic = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    # Where the local write is still live (project facts), the local file is the
    # authoritative copy and is read first. Where it has been retired (the
    # canonical store, S2), Engram holds anything written since the switch, so a
    # local-first read would happily serve a STALE file for a topic that was
    # overwritten afterwards. Read the live store first, and fall back to the
    # local archive ONLY for the topics that live there (LOCAL_ARCHIVE_TOPICS).
    #
    # Owner ruling (option i): knowledge topics live in Engram and the pre-switch
    # local archive is no longer consulted for them — the 43-topic backfill closed
    # that gap; `patterns` and `index-full` stay local by design, and the backlog
    # is its own branch above.
    if not _local_write_live(ctx):
        remote = _engram_topic_read(ctx, sanitized_topic, offset)
        if remote is not None:
            return remote
        if sanitized_topic in LOCAL_ARCHIVE_TOPICS and path.exists():
            return path.read_text(encoding="utf-8")
        return _engram_topic_fallback(ctx, sanitized_topic)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return _engram_topic_fallback(ctx, sanitized_topic)


def _engram_topic_read(ctx: ToolContext, topic: str, offset: int = 0) -> str | None:
    """The Engram record for one topic, or ``None`` when it has none / is down.

    ``None`` means "the local archive is the best answer available" — including
    when the store is unreachable, because the archive is strictly more
    informative than an error for a topic that predates the switch. The
    *unreachable-and-nothing-local* case is reported by the caller, where it can
    be phrased as UNKNOWN rather than as an absence. ``offset`` continues a
    truncated body; it indexes the record's own content.
    """
    try:
        from ouroboros.engram_read import client_for, knowledge_topic

        read = knowledge_topic(
            client_for(ctx), topic, scope=_engram_scope(ctx), offset=offset,
        )
    except Exception:
        return None
    if read.status == "ok" and read.text.strip():
        return (
            f"# {topic}\n\n{read.text}\n\n"
            "_(from Engram: durable knowledge is stored remotely. The local file, if any, "
            "is the pre-switch archive and may be older.)_"
        )
    return None


def _engram_scope(ctx: ToolContext) -> str:
    """Engram scope matching how this caller writes: project facts vs global."""
    return "project" if str(getattr(ctx, "project_id", "") or "").strip() else "global"


def _engram_topic_fallback(ctx: ToolContext, topic: str) -> str:
    """Read path for topics whose local file is gone (or never existed).

    S2 stops the local ``<topic>.md`` write, so a topic written after that point
    lives only in Engram. Without this, ``knowledge_write`` followed by
    ``knowledge_read`` would report "not found" for a record the agent had just
    durably stored — the tool pair would lie about its own memory (C13
    read-before-stop). The three outcomes stay distinct: an unreachable store is
    *unknown*, not *absent*.
    """
    try:
        from ouroboros.engram_read import client_for, knowledge_topic

        read = knowledge_topic(
            client_for(ctx),
            topic,
            scope="project" if str(getattr(ctx, "project_id", "") or "").strip() else "global",
        )
    except Exception as exc:
        return (
            f"Topic '{topic}' not found locally, and Engram could not be reached "
            f"({type(exc).__name__}) — whether a durable record exists is UNKNOWN, not absent."
        )
    if read.unknown:
        return (
            f"Topic '{topic}' not found locally, and Engram could not answer "
            f"({read.status}) — whether a durable record exists is UNKNOWN, not absent. "
            "Do not conclude the knowledge was never learned."
        )
    if read.status == "ok" and read.text.strip():
        return (
            f"# {topic}\n\n{read.text}\n\n"
            "_(from Engram: this topic has no local file — durable knowledge is stored remotely "
            "once the local write is retired.)_"
        )
    return f"Topic '{topic}' not found. Use knowledge_list to see available topics."


def _record_backlog_history(backlog_file: Path, topic: str, mode: str, task_id: str) -> None:
    """Audit a backlog write to the GLOBAL knowledge history (C10.1), mirroring the
    generic knowledge-history schema so the backlog's audit trail lives with the
    other global knowledge, not in a project store. Best-effort; never raises."""
    try:
        history_path = backlog_file.parent.parent / "knowledge_history.jsonl"
        new_content = backlog_file.read_text(encoding="utf-8") if backlog_file.exists() else ""
        with open(history_path, "a", encoding="utf-8") as hf:
            hf.write(json.dumps({
                "ts": utc_now_iso(),
                "task_id": task_id,
                "topic": topic,
                "mode": f"{mode}->merge",
                "new_sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest() if new_content else "",
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _knowledge_write(ctx: ToolContext, topic: str, content: str, mode: str = "overwrite") -> str:
    """Write or append a knowledge topic."""
    try:
        sanitized_topic = _sanitize_topic(topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    if mode not in ("overwrite", "append"):
        return f"⚠️ Invalid mode '{mode}'. Use 'overwrite' or 'append'."

    # The improvement backlog is ONE global, immune store (C10.1 Fix A): route the
    # WHOLE write — not just the path — to the global backlog regardless of project
    # scope or a forked drive, and MERGE non-destructively. Both modes union the
    # written items in (append never truncated; overwrite no longer can wipe the
    # immune backlog). An unparseable write fails CLOSED — the backlog is preserved.
    if sanitized_topic == BACKLOG_TOPIC:
        from ouroboros.improvement_backlog import backlog_path, merge_backlog_text

        root = _backlog_root(ctx)
        merged = merge_backlog_text(root, content)
        if merged < 0:
            return (
                "⚠️ Refused: the improvement-backlog write contained no parseable item "
                "blocks, so the global immune backlog was left intact (never wiped). "
                "Write `### ibl-<id>` blocks with `- summary: …` lines."
            )
        _record_backlog_history(backlog_path(root), sanitized_topic, mode, str(getattr(ctx, "task_id", "") or ""))
        return f"✅ Knowledge '{sanitized_topic}' merged into the global backlog ({merged} item(s))."

    try:
        path, sanitized_topic = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    scope = _engram_scope(ctx)
    live_local = _local_write_live(ctx)
    # Seed the store-side index from the provenance log ONCE, before this write's own
    # row is appended: this is the moment the log is complete for everything written
    # before this feature landed, and seeding then means the very first post-landing
    # write makes every earlier topic visible (the same "seed a FULL index, never a
    # one-topic seed" rule the local index follows in ``_update_index_entry``).
    _index_seed(ctx)
    base_content, base_source = _write_base(ctx, sanitized_topic, path, scope, live_local=live_local)

    # An append must build on what is ACTUALLY there. When the live store could
    # not be read, the merge is DEFERRED rather than guessed: merging onto a
    # guessed base would upsert over content nobody saw and delete the older half
    # (BIBLE P1), while refusing outright would drop the fragment for the whole
    # outage. The fragment goes to the spool and is merged on the next forward,
    # when the base can be read (C17).
    deferred = mode == "append" and base_source == "unreachable"

    if live_local:
        _ensure_dir(ctx)
        with _knowledge_write_lock(_knowledge_dir(ctx)):
            if mode == "append":
                needs_newline = False
                if path.exists() and path.stat().st_size > 0:
                    with open(path, "rb") as rf:
                        rf.seek(-1, 2)
                        if rf.read(1) != b"\n":
                            needs_newline = True

                with open(path, "a", encoding="utf-8") as f:
                    if needs_newline:
                        f.write("\n")
                    f.write(content)
            else:
                path.write_text(content, encoding="utf-8")

            _update_index_entry(ctx, sanitized_topic)
            new_content = path.read_text(encoding="utf-8") if path.exists() else ""
    elif deferred:
        new_content = content
    else:
        # S2: the canonical local write is retired. The record goes to Engram, and
        # the local file — if one exists — is left exactly as it is, as the
        # read-only archive of what was learned before the switch (C2 / P1).
        new_content = _merged_content(base_content, content, mode)

    # The LOCAL provenance row FIRST, and that order is load-bearing. Now that the
    # canonical ``.md`` write is retired, this row IS the local copy of the memory,
    # and the sink emit below can legitimately be REFUSED — C3's per-run cap, which
    # returns before the spool, so a refused record is not durable anywhere. Writing
    # the row first is what makes a refused write a DELAYED memory instead of a lost
    # one (P1). It used to be written after the emit, which made that only a hope.
    history_path = _knowledge_dir(ctx).parent / "knowledge_history.jsonl"
    try:
        # The canonical write no longer runs ``_ensure_dir`` (nothing local is
        # written), so the audit trail's directory is created here. Losing the
        # old/new provenance of a memory write is exactly the silent erosion the
        # history exists to prevent.
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "a", encoding="utf-8") as hf:
            hf.write(json.dumps({
                "ts": utc_now_iso(),
                "task_id": str(getattr(ctx, "task_id", "") or ""),
                "topic": sanitized_topic,
                "mode": mode,
                "base_source": base_source,
                "old_sha256": hashlib.sha256(base_content.encode("utf-8")).hexdigest() if base_content else "",
                "new_sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest() if new_content else "",
                "old_content": base_content,
                "new_content": new_content,
            }, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Knowledge provenance row could not be written", exc_info=True)

    # Then the durable remote record. Identity is the topic, so an evolving topic
    # upserts in place instead of piling up near-duplicates (and the append base
    # above was read from exactly this identity).
    receipt = None
    try:
        from ouroboros.engram_sink import sink_for

        sink = sink_for(ctx)
        payload = {
            "title": f"Knowledge: {sanitized_topic}",
            "content": new_content or content,
            "identity": f"knowledge:{sanitized_topic}",
            "type": "knowledge",
            "scope": scope,
            "fields": {
                "task_id": str(getattr(ctx, "task_id", "") or ""),
                "topic": sanitized_topic,
                "mode": mode,
            },
        }
        if deferred:
            receipt = sink.emit_deferred_append(**payload)
        else:
            receipt = sink.emit("knowledge", document=True, **payload)
    except Exception:
        log.debug("Engram knowledge mirror failed", exc_info=True)

    try:
        journal_path = _knowledge_dir(ctx).parent / "knowledge_journal.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        total_kb = 0
        knowledge_dir = _knowledge_dir(ctx)
        if knowledge_dir.exists():
            for f in knowledge_dir.iterdir():
                if f.is_file() and f.suffix == ".md":
                    total_kb += f.stat().st_size / 1024
        entry = {
            "ts": utc_now_iso(),
            "topic": sanitized_topic,
            "mode": mode,
            "content_chars": len(new_content),
            "total_knowledge_kb": round(total_kb, 2),
        }
        if path.exists():
            entry["file_kb"] = path.stat().st_size / 1024
        with open(journal_path, "a", encoding="utf-8") as jf:
            jf.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # Report what ACTUALLY happened. Claiming "saved to Engram" for a spooled,
    # capped, refused or failed write tells the model a memory is durable when it
    # is not — the one lie this whole path exists to prevent. The local row above
    # is what keeps the pessimistic branches honest rather than lossy.
    status = str(getattr(receipt, "status", "") or "")
    reason = str(getattr(receipt, "reason", "") or "")
    # WRITE-THROUGH INDEX, after the outcome is known: this is the one place that can
    # name the topic AND say whether the record actually reached Engram. A status of
    # "" (the sink was never reached: no receipt) is recorded as such rather than
    # dropped — a topic missing from the index is indistinguishable from a topic that
    # was never learned, which is the misreading this index exists to prevent.
    _index_record(ctx, topic=sanitized_topic, scope=scope, mode=mode, status=status or "no_receipt")
    if deferred and status in {"sent", "spooled"}:
        return (
            f"✅ Knowledge '{sanitized_topic}' append ({mode}) DEFERRED: Engram is unreachable, so "
            "the fragment is spooled and will be merged onto the existing record when it can be "
            "read. Nothing was overwritten and nothing was lost."
        )
    if status == "sent":
        if not live_local:
            return (
                f"✅ Knowledge '{sanitized_topic}' saved ({mode}) to Engram — the canonical local "
                "knowledge file is no longer written; the on-disk copy is the pre-switch archive."
            )
        return f"✅ Knowledge '{sanitized_topic}' saved ({mode})."
    if status == "spooled":
        return (
            f"✅ Knowledge '{sanitized_topic}' ({mode}) is QUEUED, not yet in Engram "
            f"({reason or 'send failed'}). It is spooled locally and forwarded on the next run."
        )
    if status == "duplicate":
        return (
            f"✅ Knowledge '{sanitized_topic}' ({mode}) was already in Engram unchanged; "
            "the existing record still holds it."
        )
    return (
        f"⚠️ Knowledge '{sanitized_topic}' ({mode}) reached the LOCAL provenance log "
        f"({history_path.name}) but NOT Engram "
        f"({status or 'no receipt'}: {reason or 'unknown'}). Nothing was lost — the local row is "
        "the record — but the remote memory is missing."
    )


def _merged_content(base: str, content: str, mode: str) -> str:
    """The record an ``append`` produces, matching the local file's byte behaviour."""
    if mode != "append":
        return content
    if base and not base.endswith("\n"):
        return base + "\n" + content
    return base + content


def _write_base(
    ctx: ToolContext,
    topic: str,
    path: Path,
    scope: str,
    *,
    live_local: bool,
) -> tuple[str, str]:
    """``(content, source)`` that a write builds on.

    ``source`` is ``local`` | ``engram`` | ``none`` | ``unreachable``. The last one
    is the signal that makes a blind append refusable rather than destructive, so
    it is reported separately from "the store genuinely has nothing".
    """
    if live_local:
        # Project facts: the local file is the authoritative copy.
        try:
            if path.exists():
                return path.read_text(encoding="utf-8"), "local"
        except Exception:
            log.debug("Failed to read project knowledge file", exc_info=True)
        return "", "none"

    unreachable = False
    try:
        from ouroboros.engram_read import (
            KNOWLEDGE_BASE_HARD_CHARS,
            client_for,
            knowledge_topic,
        )

        read = knowledge_topic(
            client_for(ctx), topic, scope=scope, max_chars=KNOWLEDGE_BASE_HARD_CHARS
        )
        if read.status == "ok" and read.text:
            return read.text, "engram"
        unreachable = read.unknown
    except Exception:
        unreachable = True

    if unreachable:
        # The archive cannot stand in for the live store: it may be older than a
        # remote record we simply could not read, and appending to it would then
        # overwrite that newer record.
        return "", "unreachable"
    return "", "none"


def _engram_list_note(ctx: ToolContext) -> str:
    """Spoken only when the LOCAL answer is "empty" — the one case that lies.

    A non-empty local listing is an honest answer to "what does the local
    knowledge base hold"; SYSTEM.md already says durable knowledge lives in
    Engram and how to reach it. But "Knowledge base is empty" reads as "I know
    nothing", and after S2 that is false: knowledge written since the local write
    was retired exists only remotely. So this is emitted on the empty path only —
    where the misreading is severe — and it costs a network call only there.

    It is deliberately a pointer, not a pull (C20 progressive disclosure):
    inlining the remote index would recreate the always-on injection this phase
    removed.
    """
    try:
        from ouroboros.engram_read import MAX_REVIEW_ITEMS, client_for, recent

        read = recent(client_for(ctx), limit=MAX_REVIEW_ITEMS)
    except Exception:
        # An unresolvable project scope (EngramConfigError) reaches here, and a
        # blank would let the caller's "Knowledge base is empty" stand as a claim
        # about the REMOTE store — the one thing this note exists to prevent.
        return (
            " The memory service is unreachable OR its project scope could not be "
            "resolved, so whether durable knowledge exists remotely is UNKNOWN, not "
            "absent — do not conclude nothing was learned."
        )
    if read.unknown:
        return (
            " Engram could not answer, so whether durable knowledge exists remotely is "
            "UNKNOWN, not absent — do not conclude nothing was learned."
        )
    if read.count <= 0:
        return ""
    return (
        f" Engram additionally holds {read.count} durable record(s) for this project; "
        "use the `engram` tool (op=search, then op=read) to reach them."
    )


_INDEX_ENTRY_RE = re.compile(r"^- \*\*([^:*]+)\*\*")


def _index_entry_topic(line: str) -> str:
    """The topic an ``index-full.md`` entry line names, else ``""`` (other lines)."""
    match = _INDEX_ENTRY_RE.match(line.strip())
    return _valid_topic_or_empty(match.group(1)) if match else ""


def _knowledge_list(ctx: ToolContext) -> str:
    """List knowledge topics: the STORE index first, then the local archive half.

    Two surfaces, because there are two homes. The store-side index names every topic
    the write path has mirrored to Engram — the ONLY place a post-switch topic is
    nameable, since the local archive froze at the switch and Engram has no
    type-enumerating route (see ``INDEX_LEDGER``). The local ``index-full.md`` is the
    pre-switch archive and stays, under its own scope note, because three topics live
    there by ruling (``LOCAL_ARCHIVE_TOPICS``). The archive half is FILTERED to the
    topics the store half does not already name, so the answer is one line per topic
    rather than the same name twice — and a topic whose only local file predates the
    provenance log still appears (that is what the filter keeps).
    """
    kdir = _knowledge_dir(ctx)
    index_path = kdir / INDEX_FILE
    store_rows = knowledge_index_rows(ctx)
    known = {str(row.get("topic") or "") for row in store_rows}

    parts: List[str] = []
    if store_rows:
        header = (
            f"ℹ️ STORE INDEX (Engram) — {len(store_rows)} topic(s) written since the local "
            "write was retired; read one with `knowledge_read(topic=…)`. Durable memory "
            "lives in Engram, so a name absent here was never written to the store.\n\n"
        )
        if len(store_rows) >= KNOWLEDGE_INDEX_TOPIC_CAP:
            header += (
                f"(at the {KNOWLEDGE_INDEX_TOPIC_CAP}-topic cap: older topics were evicted; "
                f"each eviction is audited in `{INDEX_LEDGER}`.)\n\n"
            )
        parts.append(header + "\n".join(knowledge_index_line(r) for r in store_rows) + "\n")

    if index_path.exists():
        kept = [
            line
            for line in index_path.read_text(encoding="utf-8").splitlines()
            if _index_entry_topic(line) not in known
        ]
        archive = "\n".join(kept).strip()
        if archive:
            parts.append(_LOCAL_INDEX_SCOPE_NOTE + archive + "\n")
    else:
        # No index: render the archive listing IN MEMORY from the topic files.
        # knowledge_list is registered read-only (safety.py POLICY_SKIP) and granted
        # to children that may not write cognitive memory — the old on-miss index
        # rebuild here created the knowledge dir, a lock sidecar and index-full.md on
        # a pure read. Index maintenance stays on the write path (_update_index_entry).
        entries = []
        for f in sorted(kdir.glob("*.md")) if kdir.exists() else []:
            if f.name == INDEX_FILE or f.stem in known:
                continue
            try:
                topic = _sanitize_topic(f.stem)
            except ValueError:
                continue
            try:
                summary = _extract_summary(f.read_text(encoding="utf-8").strip())
                entries.append(f"- **{topic}**: {summary}")
            except Exception:
                log.debug(f"Failed to read knowledge file for listing: {topic}", exc_info=True)
                entries.append(f"- **{topic}**: (unreadable)")
        if entries:
            parts.append(
                _LOCAL_INDEX_SCOPE_NOTE + "# Knowledge Base Index\n\n" + "\n".join(entries) + "\n"
            )

    if parts:
        return "\n".join(parts)
    return "Knowledge base is empty. Use knowledge_write to add topics." + _engram_list_note(ctx)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("knowledge_read", {
            "name": "knowledge_read",
            "description": "Read a topic from the persistent knowledge base on Drive. On a project-scoped task, reads from that project's per-project facts store (isolated from global knowledge). When the topic has no local file, the durable Engram record is read instead (the local write is being retired), so a not-found local file never means the topic was never learned. A truncated body names the offset that continues it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (alphanumeric, hyphens, underscores). E.g. 'browser-automation', 'git-recipes'"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to continue a truncated body — pass the exact offset named in the truncation note.",
                        "default": 0
                    }
                },
                "required": ["topic"]
            },
        }, _knowledge_read),
        ToolEntry("knowledge_write", {
            "name": "knowledge_write",
            "description": "Write or append to a knowledge topic. Use for recipes, gotchas, patterns learned from experience. On a project-scoped task, reads/writes are automatically scoped to that project's per-project facts store (isolated from global knowledge).",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (alphanumeric, hyphens, underscores)"
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (markdown)"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "Write mode: 'overwrite' (default) or 'append'"
                    }
                },
                "required": ["topic", "content"]
            },
        }, _knowledge_write),
        ToolEntry("knowledge_list", {
            "name": "knowledge_list",
            "description": "List knowledge topics. Names the STORE index first — every topic the write path has mirrored to Engram, i.e. what durable knowledge exists — then the local pre-switch archive for the few topics that live on disk by ruling. On a project-scoped task, lists that project's store.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            },
        }, _knowledge_list),
    ]
