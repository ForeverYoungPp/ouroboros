"""Durable verification-receipt storage and split-root replica reads."""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros import _outcome_receipts
from ouroboros.task_results import validate_task_id
from ouroboros.utils import iter_jsonl_objects

log = logging.getLogger(__name__)


def verification_receipts_path(
    drive_root: Any, task_id: str, *, create: bool = False,
) -> pathlib.Path:
    """Return the task's durable verification-receipt JSONL path."""

    safe = validate_task_id(task_id)
    artifact_dir = pathlib.Path(drive_root) / "task_results" / "artifacts" / safe
    if create:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir / "verification_receipts.jsonl"


def append_verification_receipt(
    drive_root: Any, task_id: str, receipt: Dict[str, Any],
) -> bool:
    """Append a receipt and report whether its durable custody succeeded."""

    try:
        from ouroboros.utils import append_jsonl

        return bool(
            append_jsonl(
                verification_receipts_path(drive_root, task_id, create=True),
                receipt,
                ensure_record_boundary=True,
            )
        )
    except Exception:
        log.warning(
            "Failed to append verification receipt for task %s", task_id,
            exc_info=True,
        )
        return False


def read_verification_receipts(
    drive_root: Any, task_id: str, *, gap_reasons: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    try:
        path = verification_receipts_path(drive_root, task_id, create=False)
        if not path.exists():
            return []
        if gap_reasons is None:
            # Preserve the historical all-or-nothing read used by observational
            # acceptance/ledger callers. Gap-aware authority reads opt into the
            # tolerant iterator below so partial green rows never become truth.
            return _outcome_receipts.read_receipts(path)
        return list(iter_jsonl_objects(path, gap_reasons=gap_reasons))
    except Exception:
        if gap_reasons is not None:
            gap_reasons.add("verification_receipts_unreadable")
        log.warning(
            "Failed to read verification receipts for task %s", task_id,
            exc_info=True,
        )
        return []


def merge_verification_receipts(
    *groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Union replicas chronologically with exact-row de-duplication.

    Receipt reconciliation is order-sensitive: a later pass may clear an older
    failure, while a later failure must stay red.  Split-root replicas therefore
    cannot use caller/source order as chronology.  Host receipts carry ISO UTC
    ``ts`` values; undated legacy rows stay stably before dated rows.
    """

    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for receipt in group if isinstance(group, list) else []:
            if not isinstance(receipt, dict):
                continue
            marker = json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(dict(receipt))
    indexed = list(enumerate(merged))
    indexed.sort(key=lambda item: (
        1 if str(item[1].get("ts") or "").strip() else 0,
        str(item[1].get("ts") or "").strip(),
        item[0],
    ))
    return [receipt for _index, receipt in indexed]


def read_verification_receipts_from_roots(
    roots: List[Any], task_id: str, *, gap_reasons: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Read every distinct receipt replica, preserving supplied root order."""

    groups: List[List[Dict[str, Any]]] = []
    seen_roots: set[str] = set()
    for root in roots if isinstance(roots, list) else []:
        if root in (None, ""):
            continue
        try:
            path = pathlib.Path(root).resolve(strict=False)
        except Exception:
            if gap_reasons is not None:
                gap_reasons.add("verification_receipts_root_unavailable")
            continue
        marker = str(path)
        if marker in seen_roots:
            continue
        seen_roots.add(marker)
        groups.append(
            read_verification_receipts(
                path, task_id, gap_reasons=gap_reasons,
            )
        )
    return merge_verification_receipts(*groups)


def read_context_verification_receipts(
    ctx: Any, task_id: str, *, fallback_root: Any = None,
) -> List[Dict[str, Any]]:
    """Read an active actor's local and canonical receipt replicas."""

    roots: List[Any] = [getattr(ctx, "drive_root", None)]
    try:
        from ouroboros.tool_access import canonical_data_root

        roots.append(canonical_data_root(ctx))
    except Exception:
        roots.append(getattr(ctx, "budget_drive_root", None))
    roots.append(fallback_root)
    return read_verification_receipts_from_roots(roots, task_id)


def task_verification_receipts(
    ctx: Any, env_root: Any, task: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Resolve one worker task's local/canonical receipt view."""

    task_id = str(task.get("id") or "")
    if ctx is not None:
        return read_context_verification_receipts(
            ctx, task_id, fallback_root=task.get("budget_drive_root") or env_root,
        )
    return read_verification_receipts_from_roots(
        [env_root, task.get("budget_drive_root")], task_id,
    )
