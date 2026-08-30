"""Pure usage and terminal-state projections for delegated-run custody."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def summary_of(detail: Dict[str, Any]) -> Dict[str, Any]:
    return detail.get("summary") if isinstance(detail.get("summary"), dict) else {}


def disclosed_spend(summary: Dict[str, Any]) -> Tuple[Optional[float], bool]:
    """The cash the harness reported AND whether it is settled — never one without both.

    ``spendUsd`` is only half the disclosure: the engine populates the sibling
    ``spendEstimated``. Reading the amount alone makes an estimate
    indistinguishable from settled cash, so callers receive the pair atomically.
    """
    raw = summary.get("spendUsd")
    if raw is None:
        return None, False
    try:
        return float(raw), summary.get("spendEstimated") is True
    except (TypeError, ValueError):
        return None, False


def disclosed_tokens(raw: Any) -> Optional[int]:
    """A reported token count, or ``None`` when the harness reported nothing.

    The control schema keeps token counts null until a harness reports them;
    converting absence to zero would erase that distinction.
    """
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def is_terminal(detail: Dict[str, Any], terminal_states: frozenset[str]) -> bool:
    return str(summary_of(detail).get("state") or "") in terminal_states


def complete_custody_rows(path, marker: str):
    """Every custody row, or ``None`` when the log's view is INCOMPLETE.

    The lenient reader skips unreadable lines to keep liveness surfaces
    working; an authority decision (removing a shared project) must instead
    fail closed: a marker-bearing line that cannot parse means a sibling's
    state may be invisible, so no complete view exists."""
    import json

    rows = []
    try:
        for raw in path.read_bytes().splitlines():
            if marker.encode("ascii") not in raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                return None
            if isinstance(row, dict) and str(row.get("type") or "").startswith(marker):
                rows.append(row)
    except FileNotFoundError:
        return rows
    except OSError:
        return None
    return rows
