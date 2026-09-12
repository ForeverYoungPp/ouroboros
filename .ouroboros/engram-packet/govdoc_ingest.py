#!/usr/bin/env python3
"""Governance-doc Engram mirror — DRY RUN BY DEFAULT, one command away.

WHY  The four governance docs (SYSTEM.md, BIBLE.md, ARCHITECTURE.md,
     DEVELOPMENT.md) are mirrored into Engram as retrievable records, and the
     navigation maps the prompt injects name the record per entry. The mirror is
     never the authority: the file on disk stays canonical and ``read_file`` stays
     the lossless route (see ``ouroboros/context_layout.py``'s module docstring).

SIZE Every record — breadcrumb line included — is ≤ ``GOVDOC_READ_CHARS`` = 4,000
     chars, so ONE ``engram`` tool read returns it in full. That bound, not the
     sink's 16,000-char document cap, is what the chunker is sized against: a record
     over it would come back TRUNCATED and would read like a complete one
     (``ouroboros/tools/engram.py``: ``MAX_READ_CONTENT_CHARS``).

SOURCE    the docs in the repo, chunked by ``ouroboros.context_layout.chunk_doc``
          and shaped by ``govdoc_record`` — the SAME functions the navigation map
          uses to name records, so the map and the store cannot disagree.
TRANSPORT the PRODUCTION path — ``EngramSink.emit(..., document=True)`` with the
          kwargs ``ouroboros/tools/knowledge.py`` uses, except ``type="govdoc"``:
          a distinct record type keeps the mirror out of every ``type="knowledge"``
          read (``engram_read.type_digest``'s client-side filter), so the generic
          knowledge digest can never return a chunk.
IDENTITY  ``knowledge:govdoc:<doc>:<ordinal>``, stable across re-runs: Engram
          upserts on the record identity, so a later run REPLACES a chunk's body
          instead of duplicating it. Re-run after any edit to a doc.
STAGING   ``ceil(N / batch_size)`` batches with ``begin_engram_run()`` between them,
          so ONE process lands the whole mirror instead of tripping the per-run cap
          (``MAX_EMITS_PER_RUN`` = 20; ``--limit`` canaries included).
MANIFEST  ``govdoc_manifest.json`` is written on ``--go`` (what was actually sent);
          ``--check`` re-derives the plan from the docs on disk and diffs it against
          that file WITHOUT touching the service, which is how doc drift is spotted.

KNOWN COST, DISCLOSED NOT HIDDEN  the recency window (``MAX_WINDOW`` = 50) is shared
     and has no type filter, so ~312 new records push older records out of the
     bounded window that ``type_digest`` and ``knowledge_topic``'s miss-fallback scan.
     The BEFORE/AFTER block below prints that displacement; a knowledge record is
     neither rewritten nor unreachable through its primary typed search.

USAGE
  govdoc_ingest.py                       dry run: the whole plan, no writes
  govdoc_ingest.py --limit 3             dry canary
  govdoc_ingest.py --only architecture   dry run, one doc (repeatable)
  govdoc_ingest.py --check               manifest drift only, no service
  govdoc_ingest.py --go --limit 3        CANARY WRITE (3 records)
  govdoc_ingest.py --go                  full write (312 records, 16 batches)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

DEFAULT_DRIVE = pathlib.Path("/home/fy/Ouroboros/data")
DEFAULT_REPO = pathlib.Path("/home/fy/Projects/code/ouroboros")
DEFAULT_MANIFEST = pathlib.Path(__file__).resolve().parent / "govdoc_manifest.json"

#: The sink's title bound (``FIELD_CAP_CHARS``); a longer title is silently clipped.
TITLE_CAP_CHARS = 300


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plan(
    repo: pathlib.Path, only: List[str] | None
) -> List[Dict[str, Any]]:
    """Every record this run would write, derived from the docs on disk."""
    from ouroboros.context_layout import GOVDOC_DOCS, chunk_doc, govdoc_record

    docs: List[Dict[str, Any]] = []
    for slug, (rel_path, file_name) in GOVDOC_DOCS.items():
        if only and slug not in only:
            continue
        path = repo / rel_path
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        chunks = chunk_doc(text)
        records = [
            govdoc_record(slug, lines=lines, chunks=chunks, ordinal=i)
            for i in range(1, len(chunks) + 1)
        ]
        docs.append(
            {
                "slug": slug,
                "path": rel_path,
                "file_name": file_name,
                "text": text,
                "sha256": _sha(text),
                "bytes": len(text.encode("utf-8")),
                "lines": len(lines),
                "chunks": chunks,
                "records": records,
            }
        )
    return docs


def _records(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten the plan into the ordered list of records a run emits."""
    from ouroboros.context_layout import govdoc_identity

    out: List[Dict[str, Any]] = []
    for doc in docs:
        for ordinal, (title, content) in enumerate(doc["records"], start=1):
            identity = govdoc_identity(doc["slug"], ordinal)
            chunk = doc["chunks"][ordinal - 1]
            out.append(
                {
                    "slug": doc["slug"],
                    "identity": identity,
                    "title": title,
                    "content": content,
                    "start": chunk.start,
                    "end": chunk.end,
                    "bytes": len(content.encode("utf-8")),
                }
            )
    return out


def _guards(rows: List[Dict[str, Any]]) -> None:
    """Fail CLOSED before anything is emitted (a silent degradation is the bug)."""
    from ouroboros.context_layout import GOVDOC_READ_CHARS
    from ouroboros.engram_sink import _looks_like_todo

    seen = set()
    for row in rows:
        identity, title, content = row["identity"], row["title"], row["content"]
        assert identity.startswith("knowledge:govdoc:"), f"bad identity {identity!r}"
        assert identity not in seen, f"duplicate identity {identity!r}"
        seen.add(identity)
        assert len(identity) <= 200, f"identity too long: {identity!r}"
        assert title and len(title) <= TITLE_CAP_CHARS, f"title over cap: {title!r}"
        assert content.strip(), f"empty content: {identity!r}"
        # The read bound is the whole point of the chunk size: over it, `engram op=read`
        # truncates and the record reads complete. Refuse instead of storing one.
        assert len(content) <= GOVDOC_READ_CHARS, (
            f"{identity!r} is {len(content)} chars > {GOVDOC_READ_CHARS}: "
            "engram would return it truncated"
        )
        # C18: the sink refuses content whose job is to say what to do next.
        assert not _looks_like_todo(content) and not _looks_like_todo(title), (
            f"{identity!r} would be refused by the C18 gate"
        )


def _manifest(docs: List[Dict[str, Any]], rows: List[Dict[str, Any]], repo: pathlib.Path) -> Dict[str, Any]:
    """The drift witness: what the store should hold, per record."""
    version = ""
    try:
        version = (repo / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass

    from ouroboros.context_layout import (
        GOVDOC_BREADCRUMB_CHARS,
        GOVDOC_CHUNK_CHARS,
        GOVDOC_MIN_FILL_CHARS,
        GOVDOC_READ_CHARS,
    )

    by_doc: Dict[str, Any] = {}
    for doc in docs:
        by_doc[doc["slug"]] = {
            "path": doc["path"],
            "sha256": doc["sha256"],
            "bytes": doc["bytes"],
            "lines": doc["lines"],
            "chunks": len(doc["chunks"]),
            "records": [
                {
                    "identity": r["identity"],
                    "title": r["title"],
                    "start": r["start"],
                    "end": r["end"],
                    "bytes": r["bytes"],
                    "sha256": _sha(r["content"]),
                }
                for r in rows
                if r["slug"] == doc["slug"]
            ],
        }
    return {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": version,
        "source_repo": str(repo),
        "caps": {
            "read_chars": GOVDOC_READ_CHARS,
            "breadcrumb_chars": GOVDOC_BREADCRUMB_CHARS,
            "chunk_chars": GOVDOC_CHUNK_CHARS,
            "min_fill_chars": GOVDOC_MIN_FILL_CHARS,
        },
        "docs": by_doc,
    }


def _check(manifest_path: pathlib.Path, docs: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> int:
    """Diff the plan on disk against the manifest. No service, no writes."""
    if not manifest_path.exists():
        print(f"CHECK: no manifest at {manifest_path} — nothing has been written yet.")
        return 0
    old = json.loads(manifest_path.read_text(encoding="utf-8"))
    new = _manifest(docs, rows, pathlib.Path(old.get("source_repo") or "."))
    drift = 0
    skipped = sorted(set(old.get("docs", {})) - set(new["docs"]))
    if skipped:
        print(f"  not compared (excluded from this plan): {', '.join(skipped)}")
    for slug, doc in new["docs"].items():
        was = old.get("docs", {}).get(slug)
        if not was:
            print(f"  {slug}: ABSENT from the manifest (new doc)")
            drift += 1
            continue
        if was.get("sha256") != doc["sha256"]:
            print(f"  {slug}: doc changed ({was.get('bytes')} -> {doc['bytes']} bytes)")
            drift += 1
        old_rec = {r["identity"]: r for r in was.get("records", [])}
        new_rec = {r["identity"]: r for r in doc["records"]}
        for identity in sorted(set(new_rec) - set(old_rec)):
            print(f"  {slug}: MISSING from the store — {identity}")
            drift += 1
        for identity in sorted(set(old_rec) - set(new_rec)):
            print(f"  {slug}: STALE in the store (no longer planned) — {identity}")
            drift += 1
        for identity in sorted(set(old_rec) & set(new_rec)):
            if old_rec[identity]["sha256"] != new_rec[identity]["sha256"]:
                print(f"  {slug}: CHANGED — {identity} ({old_rec[identity]['title']})")
                drift += 1
    if drift:
        print(f"\nCHECK: {drift} drift(s) — re-run with --go to refresh the mirror.")
        return 1
    print("CHECK: the manifest matches the docs on disk; the mirror is current.")
    return 0


def _batch_plan(rows: List[Dict[str, Any]], batch_size: int) -> List[List[Dict[str, Any]]]:
    return [rows[i: i + batch_size] for i in range(0, len(rows), batch_size)]


def _witness(client: Any, kdir: pathlib.Path) -> str:
    """Read ONE existing knowledge topic, to show the corpus survived the mirror.

    A digest count cannot serve here: ``type_digest`` counts matches inside a bounded
    recency window, so it reports the window's contents, not the store's. One record,
    fetched by name through its own typed search, is the honest witness.
    """
    from ouroboros.engram_read import knowledge_topic

    stems = sorted(p.stem for p in kdir.glob("*.md")) if kdir.exists() else []
    if not stems:
        return "witness: no local knowledge topic to sample"
    topic = stems[0]
    read = knowledge_topic(client, topic, scope="global")
    body = read.text or ""
    return (
        f"witness: topic={topic!r} status={read.status} chars={len(body)} "
        f"sha256={_sha(body)[:12]}"
    )


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--go", action="store_true", help="actually write (default: dry run, no writes)")
    ap.add_argument("--limit", type=int, default=0, help="canary: only the first N records (0 = all)")
    ap.add_argument("--only", action="append", default=[], help="only this doc slug (repeatable)")
    ap.add_argument("--batch-size", type=int, default=0, help="records per run (0 = MAX_EMITS_PER_RUN)")
    ap.add_argument("--check", action="store_true", help="manifest drift only: no plan, no service")
    ap.add_argument("--drive", type=pathlib.Path, default=DEFAULT_DRIVE)
    ap.add_argument("--repo", type=pathlib.Path, default=DEFAULT_REPO)
    ap.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)

    sys.path.insert(0, str(args.repo))
    from ouroboros.context_layout import GOVDOC_DOCS, GOVDOC_READ_CHARS
    from ouroboros.engram_client import EngramResult
    from ouroboros.engram_read import MAX_DIGEST_ITEMS, MAX_WINDOW, type_digest
    from ouroboros.engram_sink import MAX_EMITS_PER_RUN, EngramSink, begin_engram_run, sink_for

    unknown = [s for s in args.only if s not in GOVDOC_DOCS]
    if unknown:
        print(f"--only got unknown doc(s) {unknown}; known: {sorted(GOVDOC_DOCS)}")
        return 2

    docs = _plan(args.repo, args.only)
    if not docs:
        print("no docs selected")
        return 2
    rows = _records(docs)
    _guards(rows)

    if args.check:
        print("== GOVDOC MIRROR CHECK ==")
        print(f"manifest: {args.manifest}")
        return _check(args.manifest, docs, rows)

    if args.limit > 0:
        rows = rows[: args.limit]
        docs = [d for d in docs if any(r["slug"] == d["slug"] for r in rows)]
    batch_size = int(args.batch_size or MAX_EMITS_PER_RUN)
    batches = _batch_plan(rows, batch_size)

    env = SimpleNamespace(drive_root=args.drive, repo_dir=args.repo)
    sink = sink_for(env)

    print("== GOVDOC MIRROR PLAN ==")
    print(f"mode            : {'--go (WRITES)' if args.go else 'DRY RUN (no writes)'}")
    print(f"repo            : {args.repo}")
    print(f"manifest        : {args.manifest}")
    print(f"docs selected   : {len(docs)} of {len(GOVDOC_DOCS)}")
    for doc in docs:
        mine = [r for r in rows if r["slug"] == doc["slug"]]
        print(
            f"  {doc['slug']:<13}: {doc['path']:<23} {doc['lines']:>5} lines "
            f"{len(mine):>4} chunks  max record "
            f"{max((len(r['content']) for r in mine), default=0):>5} B"
        )
    print(f"records total   : {len(rows)}")
    print(f"record cap      : {GOVDOC_READ_CHARS} chars (one `engram op=read` returns it whole)")
    print(f"batch size      : {batch_size} (MAX_EMITS_PER_RUN={MAX_EMITS_PER_RUN})")
    print(f"batches         : {len(batches)}  (ceil({len(rows)}/{batch_size}))")
    print(f"total payload   : {sum(len(r['content'].encode('utf-8')) for r in rows):,} bytes")

    # --- DRY RUN: capture the record the PRODUCTION builder assembles ----------
    captured: List[dict] = []
    if not args.go:
        sink_mod = sys.modules["ouroboros.engram_sink"]

        def _capture(self, record):
            captured.append(dict(record))
            return EngramResult(True, status=200, data={"id": 0, "dry_run": True})

        EngramSink._send = _capture  # never talk to the service
        sink_mod._spool_append = lambda path, record: True  # never write the spool
        EngramSink.compact_if_idle = lambda self: False  # never rewrite it

    spool = args.drive / "state" / "engram_spool.jsonl"
    spool_before = (spool.stat().st_size, spool.stat().st_mtime) if spool.exists() else None
    kdir = args.drive / "memory" / "knowledge"

    read_before = type_digest(sink.client, "knowledge", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    gov_before = type_digest(sink.client, "govdoc", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    witness_before = _witness(sink.client, kdir)
    print("\n== BEFORE ==")
    print(f"knowledge digest: status={read_before.status} count={read_before.count}")
    print(f"govdoc digest   : status={gov_before.status} count={gov_before.count}")
    print(witness_before)
    print(
        f"(cursor: {len(rows)} new records will occupy the newest slots of the shared "
        f"{MAX_WINDOW}-record recency window — the knowledge DIGEST count is expected to "
        "fall; the witness above is what shows the corpus itself is intact.)"
    )

    if args.go:
        print(
            f"\nWARNING: --go writes {len(rows)} records into the Engram store of project "
            f"{sink.client.config.project!r}; nothing is deleted; each identity is "
            "knowledge:govdoc:<doc>:<ordinal>; the docs on disk are untouched."
        )

    print("\n== RECORDS ==")
    failed: List[str] = []
    for n, batch in enumerate(batches, 1):
        begin_engram_run(env)  # a new emit run per batch: the cap cannot starve the tail
        for row in batch:
            project = sink.client.config.project
            assert project == "ouroboros", f"F7: resolved project {project!r} for {row['identity']!r}"
            assert not sink.config_error, f"config error would refuse every send: {sink.config_error!r}"
            receipt = sink.emit(
                "knowledge",
                title=row["title"],
                content=row["content"],
                identity=row["identity"],
                type="govdoc",
                scope="global",
                fields={"task_id": "govdoc-ingest", "topic": row["identity"], "mode": "overwrite"},
                document=True,
            )
            flag = "ok" if receipt.status in ("sent", "spooled") else receipt.status.upper()
            if flag != "ok":
                failed.append(f"{row['identity']}: {receipt.status} ({receipt.reason})")
            print(
                f"  [{n:>2}] identity={row['identity']:<34} lines={row['start']}-{row['end']:<5}"
                f" bytes={len(row['content'].encode('utf-8')):>5} -> {flag}"
            )

    read_after = type_digest(sink.client, "knowledge", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    gov_after = type_digest(sink.client, "govdoc", limit=MAX_DIGEST_ITEMS, window=MAX_WINDOW)
    witness_after = _witness(sink.client, kdir)
    print("\n== AFTER ==")
    print(f"knowledge digest: status={read_after.status} count={read_after.count} "
          f"(was {read_before.status}/{read_before.count})")
    print(f"govdoc digest   : status={gov_after.status} count={gov_after.count} "
          f"(was {gov_before.status}/{gov_before.count})")
    print(witness_after)
    if gov_after.text:
        print(f"govdoc digest body:\n{gov_after.text[:600]}")

    if args.go:
        manifest = _manifest(docs, rows, args.repo)
        args.manifest.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nmanifest written: {args.manifest}")

    if not args.go:
        spool_after = (spool.stat().st_size, spool.stat().st_mtime) if spool.exists() else None
        print("\n== NO-WRITES PROOF (dry run) ==")
        print(f"captured records (would-be sends): {len(captured)}")
        print(f"spool before={spool_before} after={spool_after} -> "
              f"{'UNCHANGED' if spool_before == spool_after else 'CHANGED (BUG!)'}")

    if failed:
        print("\nFAILED RECORDS:\n  " + "\n  ".join(failed))
        return 1
    print(f"\nDONE: {len(rows)} record(s) in {len(batches)} batch(es), "
          f"{'written' if args.go else 'NOT written (dry run)'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
