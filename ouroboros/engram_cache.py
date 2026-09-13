"""A read-through cache for Engram content sections. Never raises.

Why this exists at all: with Engram as the store, "the service is unreachable"
must not become "there is no history". Every prompt section that stands in for a
local file already has a replacement guarantee — always retrievable, bounded, and
**disclosed when unreachable** — and a bare ``unavailable`` gives the reader a
blank where the local file used to be. Serving the last good read, marked stale,
keeps the reader's information and its uncertainty at the same time.

What it deliberately does NOT do:

* No version PROBE. ``memory_version()`` is ``recent()`` — up to 50 records with
  full bodies — so keying on it would cost more traffic than the digest it
  replaces. Freshness is a TTL, and the ``version`` each fetch already carries is
  remembered for disclosure only.
* No caching of the review QUEUE. That read is a consumption cursor whose result
  authorizes a write (``mark_reviewed``); serving a stale cursor could advance
  the decay clock on a set that has moved. Cursors re-read; content caches.
* No caching of TASK-KEYED reads either. ``main_context_authority.
  _narrative_from_engram`` reads one narrative by ``task_id``, at most once per
  assembly, and a later turn asks a different key — an entry could never be asked
  for twice, so there is no stale copy to bound and nothing saved by the lookup.
  Its outage behaviour is that read's own typed gap, not a stale disclosure.
* No hiding of a real outage: a cache MISS with the store down still returns the
  typed ``unavailable``/``rejected`` the caller already handles.

Bounded by construction: the caller's own byte budget is what a fetch enforces,
and the cache stores at most ``max_chars`` of it on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import pathlib
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional

from ouroboros.engram_read import MachineRead

log = logging.getLogger(__name__)

#: How long a good read is served without asking again. Short enough that a
#: changed store is picked up within a turn or two, long enough that a project
#: with several consumers makes one request, not one per section.
DEFAULT_TTL_SECONDS = 60.0
#: Disk mirror bound. The sections this serves are already budgeted well under
#: this (2000–3000 chars); the ceiling is here so a future caller cannot turn the
#: cache into an unbounded copy of the store.
MAX_CACHE_CHARS = 8_000
#: How many mirror files may exist. The recall key is the owner's message, so
#: without a bound the directory grows by one file per distinct question forever.
MAX_CACHE_FILES = 64
#: How many entries may live in the process. Recall is keyed on the owner's
#: message, so without this a long session accumulates one entry per question.
MAX_CACHE_ENTRIES = 256

_CACHE: Dict[str, "_Entry"] = {}


@dataclass(frozen=True)
class _Entry:
    status: str
    text: str
    count: int
    version: str
    fetched_at: float
    #: When the last REFRESH ATTEMPT happened and how it failed. Without these a
    #: store outage costs the caller the full transport timeout on EVERY read —
    #: the cached content is served, but nothing stops the next attempt.
    last_attempt_at: float = 0.0
    last_error: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "count": self.count,
            "version": self.version,
            "fetched_at": self.fetched_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "Optional[_Entry]":
        if not isinstance(payload, dict):
            return None
        try:
            fetched_at = float(payload.get("fetched_at") or 0.0)
            last_attempt_at = float(payload.get("last_attempt_at") or 0.0)
            if not (math.isfinite(fetched_at) and math.isfinite(last_attempt_at)):
                # A corrupt/hand-edited mirror can hold inf/NaN (json.loads makes
                # both from `1e400`/`NaN`). Formatting that timestamp later would
                # raise OUTSIDE cached_read's only guard, turning the outage the
                # cache exists for into a blank section.
                return None
            return cls(
                status=str(payload.get("status") or ""),
                text=str(payload.get("text") or ""),
                count=int(payload.get("count") or 0),
                version=str(payload.get("version") or ""),
                fetched_at=fetched_at,
                last_attempt_at=last_attempt_at,
                last_error=str(payload.get("last_error") or ""),
            )
        except (TypeError, ValueError):
            return None


def _cache_key(kind: str, scope: str, key: str) -> str:
    """One entry per (kind, project+drive, variant).

    The scope component is load-bearing: the in-process cache outlives a single
    drive, so a key without it would serve one project's (or one test's) content
    to another — the same failure mode F7 guards against for the project name.
    """
    digest = hashlib.sha256(f"{scope}|{key}".encode("utf-8")).hexdigest()[:10]
    return f"{kind}:{digest}"


def _cache_path(drive_root: Any, cache_key: str) -> Optional[pathlib.Path]:
    if not drive_root:
        return None
    safe = cache_key.replace(":", "_").replace("/", "_")
    return pathlib.Path(drive_root) / "state" / "engram_cache" / f"{safe}.json"


def _load_persisted(drive_root: Any, cache_key: str) -> "Optional[_Entry]":
    path = _cache_path(drive_root, cache_key)
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        return _Entry.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _persist(drive_root: Any, cache_key: str, entry: _Entry) -> None:
    path = _cache_path(drive_root, cache_key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer: the background loop and the foreground turn can build
        # the same section concurrently, and a shared temp name lets two
        # write+replace pairs publish an interleaved (unparseable) file.
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(entry.to_json(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        _prune(path.parent)
    except Exception:
        log.debug("engram cache: persist failed for %s", cache_key, exc_info=True)


def _prune(directory: pathlib.Path) -> None:
    """Keep the mirror bounded. A recall key is the owner's message, so an
    unbounded directory is one file per distinct question, forever."""
    try:
        files = sorted(
            (p for p in directory.glob("*.json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale_path in files[MAX_CACHE_FILES:]:
            stale_path.unlink(missing_ok=True)
    except Exception:
        log.debug("engram cache: prune failed", exc_info=True)


def _as_read(entry: _Entry, *, stale: bool, cause: str = "") -> MachineRead:
    detail = ""
    if stale:
        detail = (
            f"cached at {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.fetched_at))}"
            " — the memory service could not be re-read"
            + (f" (it reported {cause})" if cause else "")
        )
    return MachineRead(
        True,
        status="stale" if stale else entry.status,
        count=entry.count,
        version=entry.version,
        text=entry.text,
        detail=detail,
    )


def _scope_of(client: Any, drive_root: Any) -> str:
    """The (project, drive) identity a cached entry belongs to."""
    project = ""
    try:
        project = str(getattr(getattr(client, "config", None), "project", "") or "")
    except Exception:
        project = ""
    try:
        drive = str(pathlib.Path(drive_root).resolve(strict=False)) if drive_root else ""
    except Exception:
        drive = str(drive_root or "")
    return f"{project}|{drive}"


def cached_read(
    kind: str,
    client: Any,
    fetch: Callable[[], MachineRead],
    *,
    drive_root: Any = None,
    key: str = "",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    max_chars: int = MAX_CACHE_CHARS,
) -> MachineRead:
    """Serve ``fetch()`` through a TTL cache, degrading to the last good read.

    ``kind`` names the section (for cache files and logs); ``key`` distinguishes
    variants of the same section (a recall query, for instance) so a cached answer
    is never served for a different question. A fresh cache hit performs NO
    request at all.
    """
    cache_key = _cache_key(kind, _scope_of(client, drive_root), key)
    ttl = float(ttl_seconds)
    now = time.time()
    entry = _CACHE.get(cache_key) or _load_persisted(drive_root, cache_key)
    if entry is not None:
        _remember(cache_key, entry)
        if 0 <= now - entry.fetched_at < ttl:
            return _as_read(entry, stale=False)
        if entry.last_attempt_at and 0 <= now - entry.last_attempt_at < ttl:
            # A recent attempt already failed. Re-trying now would spend the
            # transport timeout again to learn the same thing, so serve what we
            # have and say why it is behind.
            return _as_read(entry, stale=True, cause=entry.last_error)

    read: MachineRead
    try:
        read = fetch()
    except Exception as exc:  # a fetcher is documented never to raise; be sure
        read = MachineRead(False, status="unavailable", detail=type(exc).__name__)

    if read.status == "ok":
        # `readable` also covers `empty`, and storing one was a real defect: a
        # blank answer is not content, so a 60s TTL (and, for a stable key, a
        # disk mirror) hid a summary written moments later behind "there is
        # nothing". An empty read costs one bounded request to repeat, and the
        # failed-refresh path below is what holds content across an outage — so
        # only `ok` is worth remembering.
        fresh = _Entry(
            status=read.status,
            text=str(read.text or "")[: max(1, int(max_chars))],
            count=int(read.count or 0),
            version=str(read.version or ""),
            fetched_at=now,
        )
        _remember(cache_key, fresh)
        # Query-keyed entries (a recall question) are per-turn by construction, so
        # mirroring them would mean one file write per message for a cache that can
        # never be hit again. Stable keys are what a restart can actually serve.
        if not key:
            _persist(drive_root, cache_key, fresh)
        return read

    if entry is not None:
        # The store could not be read, but it WAS read before: content plus the
        # explicit "may be behind" beats a blank that reads as "nothing there".
        failed = replace(
            entry, last_attempt_at=now, last_error=str(read.status or "")
        )
        _remember(cache_key, failed)
        if not key:
            _persist(drive_root, cache_key, failed)
        log.warning(
            "engram cache: serving the last good %s read (store reported %s)",
            kind, read.status,
        )
        return _as_read(entry, stale=True, cause=str(read.status or ""))

    return read


def _remember(cache_key: str, entry: _Entry) -> None:
    """Store one entry, evicting the oldest when the process holds too many."""
    _CACHE[cache_key] = entry
    if len(_CACHE) <= MAX_CACHE_ENTRIES:
        return
    oldest = sorted(_CACHE.items(), key=lambda item: item[1].fetched_at)[
        : len(_CACHE) - MAX_CACHE_ENTRIES
    ]
    for stale_key, _ in oldest:
        _CACHE.pop(stale_key, None)


def reset_cache() -> None:
    """Drop all in-process entries. For tests and a deliberate run boundary."""
    _CACHE.clear()


__all__ = [
    "cached_read",
    "reset_cache",
    "DEFAULT_TTL_SECONDS",
    "MAX_CACHE_CHARS",
    "MAX_CACHE_FILES",
    "MAX_CACHE_ENTRIES",
]
