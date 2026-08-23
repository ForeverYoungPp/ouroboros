"""Atomic durable state records for marketplace-installed and published skills.

Records live beside skill review/enablement state and preserve durable
owner-visible facts across payload updates. Two record families live here:

* ClawHub install provenance (``clawhub.json``) — the frozen legacy API
  (``write_provenance``/``read_provenance``/``delete_provenance``); its
  full-replace write semantics and record shape must not change.
* Per-hub state records written through the generalized atomic merge-writer
  (``merge_state_record``), currently the OuroborosHub publication receipt
  (``ouroboroshub.json`` with a single ``published`` section).
"""

from __future__ import annotations

import logging
import pathlib
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from ouroboros.skill_loader import skill_state_dir
from ouroboros.utils import atomic_write_json, read_json_dict, update_json_locked, utc_now_iso

log = logging.getLogger(__name__)


_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "clawhub.json"
PUBLICATION_FILENAME = "ouroboroshub.json"

_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def write_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
    record: Dict[str, Any],
) -> pathlib.Path:
    """Persist a provenance record and return its path on disk."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    payload = dict(record or {})
    payload.setdefault("schema_version", _SCHEMA_VERSION)
    payload.setdefault("source", "clawhub")
    now_iso = utc_now_iso()
    payload.setdefault("installed_at", now_iso)
    payload["updated_at"] = now_iso
    atomic_write_json(target, payload, trailing_newline=True)
    return target


def read_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the persisted provenance for ``skill_name`` or ``None``."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    if not target.is_file():
        return None
    return read_json_dict(target)


def delete_provenance(drive_root: pathlib.Path, skill_name: str) -> None:
    """Remove the provenance file (idempotent)."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    try:
        if target.is_file():
            target.unlink()
    except OSError:
        log.warning("Failed to delete provenance file %s", target, exc_info=True)


def merge_state_record(
    drive_root: pathlib.Path,
    skill_name: str,
    filename: str,
    sections: Mapping[str, Any],
) -> pathlib.Path:
    """Atomically merge *sections* into ``state/skills/<name>/<filename>``.

    Unknown sibling keys already present in the file survive; each provided
    section is replaced wholesale. The read-modify-write runs under the
    shared ``update_json_locked`` sidecar lock, so concurrent writers of
    different sections cannot drop each other. A malformed existing file is
    replaced by the provided sections alone — a reader never repairs it, so
    the next write owns the record. Callers pass a module-level filename
    constant such as ``PUBLICATION_FILENAME``.
    """
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / filename

    def _merge(current: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}
        payload.update({str(key): value for key, value in sections.items()})
        return payload

    # Locked read-modify-write (utils SSOT): closes the lost-update window
    # between two concurrent section writers without a bespoke lock file.
    update_json_locked(target, _merge)
    return target


def write_publication_record(
    drive_root: pathlib.Path,
    skill_name: str,
    published: Mapping[str, Any],
) -> pathlib.Path:
    """Persist the OuroborosHub publication receipt (merge-write).

    The ``published`` section is replaced wholesale on republish while
    unknown sibling sections of a future schema are preserved.
    """
    return merge_state_record(
        drive_root,
        skill_name,
        PUBLICATION_FILENAME,
        {"schema_version": _SCHEMA_VERSION, "published": dict(published)},
    )


def _publication_diagnostic(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a typed diagnostic for an invalid publication record, else None."""
    if not isinstance(record, dict):
        return "publication record is unreadable or not a JSON object"
    schema_version = record.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != _SCHEMA_VERSION:
        return "publication record has an unsupported schema_version"
    published = record.get("published")
    if not isinstance(published, dict):
        return "publication record is missing a published object"
    slug = published.get("slug")
    if not isinstance(slug, str) or not slug:
        return "published.slug must be a non-empty string"
    for key in ("version", "repository", "pr_url", "published_at"):
        if not isinstance(published.get(key), str):
            return f"published.{key} must be a string"
    content_hash = published.get("content_hash")
    if not isinstance(content_hash, str) or not _CONTENT_HASH_RE.fullmatch(content_hash):
        return "published.content_hash must be 64 lowercase hex characters"
    pr_number = published.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return "published.pr_number must be a positive integer"
    return None


def read_publication_record(
    drive_root: pathlib.Path,
    skill_name: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(published, diagnostic)`` for the publication receipt.

    * ``(None, None)`` — no record file on disk (never published from here).
    * ``(dict, None)`` — the validated ``published`` section, as stored.
    * ``(None, str)`` — the file exists but is malformed or fails the
      schema-v1 validation contract; the string is a typed diagnostic.
      Reading never repairs or rewrites the file.
    """
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PUBLICATION_FILENAME
    if not target.is_file():
        return None, None
    record = read_json_dict(target)
    diagnostic = _publication_diagnostic(record)
    if diagnostic is not None:
        return None, diagnostic
    return dict(record["published"]), None


__all__ = [
    "PROVENANCE_FILENAME",
    "PUBLICATION_FILENAME",
    "delete_provenance",
    "merge_state_record",
    "read_provenance",
    "read_publication_record",
    "write_provenance",
    "write_publication_record",
]
