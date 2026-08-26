"""Pure requested/permitted/attempted/achieved depth evidence projections."""

from __future__ import annotations

from typing import Any, Dict, Iterable

from ouroboros.contracts.task_contract import normalize_depth_provenance


def task_depth_provenance(row: Any) -> Dict[str, Any]:
    """Read one task row's normalized depth facts from either preserved projection."""

    if not isinstance(row, dict):
        return {}
    direct = normalize_depth_provenance(row.get("depth_provenance"))
    if direct:
        return direct
    contract = row.get("task_contract") if isinstance(row.get("task_contract"), dict) else {}
    budget = contract.get("delegation_budget") if isinstance(contract.get("delegation_budget"), dict) else {}
    return normalize_depth_provenance(budget.get("depth_provenance"))


def build_depth_summary(
    root_contract: Any, subtree_statuses: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summarize host-visible depth without forcing a topology or parsing prose."""

    contract = root_contract if isinstance(root_contract, dict) else {}
    budget = contract.get("delegation_budget") if isinstance(contract.get("delegation_budget"), dict) else {}
    root_provenance = normalize_depth_provenance(budget.get("depth_provenance"))
    requested = root_provenance.get("requested_depth")
    if requested is None and "depth_remaining" in budget:
        try:
            requested = max(0, int(budget.get("depth_remaining")))
        except (TypeError, ValueError):
            requested = None
    statuses = [row for row in subtree_statuses if isinstance(row, dict)]
    provenances = [task_depth_provenance(row) for row in statuses]
    provenances = [row for row in provenances if row]
    if requested is None:
        requested = next(
            (row.get("requested_depth") for row in provenances if row.get("requested_depth") is not None),
            None,
        )
    permitted_values = [
        value
        for value in [
            root_provenance.get("permitted_depth"),
            *(row.get("permitted_depth") for row in provenances),
        ]
        if value is not None
    ]
    permitted = min(permitted_values) if permitted_values else None

    def _maximum(key: str) -> Any:
        values = [row.get(key) for row in provenances if row.get(key) is not None]
        if values:
            return max(values)
        return 0 if not statuses else None

    attempted = _maximum("attempted_depth")
    achieved = _maximum("achieved_depth")
    if requested is None:
        status = "request_unknown"
    elif permitted is None or attempted is None or achieved is None:
        status = "evidence_unknown"
    elif permitted < requested:
        status = "capability_reduced"
    elif achieved >= requested:
        status = "achieved"
    else:
        status = "chosen_shallower"
    return {
        "requested_depth": requested,
        "permitted_depth": permitted,
        "attempted_depth": attempted,
        "achieved_depth": achieved,
        "status": status,
        "host_visible_only": True,
    }
