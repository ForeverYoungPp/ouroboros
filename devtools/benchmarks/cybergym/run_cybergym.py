#!/usr/bin/env python3
"""CyberGym launcher with a durable admission boundary.

The launcher owns only protocol bookkeeping.  Upstream task generation and
the private server/agent sidecar are injected after admission (or supplied by
the companion ``cybergym_sidecar`` module).  Running without an injected
executor therefore produces explicit blocked rows instead of silently falling
back to a host shell, Docker default network, or a different model.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import (
    BenchmarkAdmissionRefused,
    admit_benchmark_run,
    finalize_run_manifest,
)
from devtools.benchmarks.common.run_roots import (
    assert_file_output_outside_repo,
    assert_outside_repo,
    run_root,
)
from devtools.benchmarks.cybergym.cybergym_adapter import (
    BENCHMARK_NAME,
    BudgetLedger,
    DEFAULT_BUDGET_CAP_USD,
    DEFAULT_LEVEL,
    OFFICIAL_DATA_REVISION,
    OFFICIAL_SOURCE_PIN,
    OFFICIAL_TASKS_SHA256,
    CyberGymError,
    CyberGymIntegrationUnavailable,
    TaskSpec,
    append_cybergym_result,
    build_generate_task_argv,
    build_task_result_row,
    load_task_catalog,
    mask_task_id,
    pre_admission_report,
    run_campaign,
    safe_task_id,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_TIMEOUT_SEC = 4 * 60 * 60


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse only scalar/argv intent; no filesystem or network probes occur here."""
    parser = argparse.ArgumentParser(description="Run the Level-1 CyberGym adapter")
    parser.add_argument("--repo-dir", default=str(REPO_ROOT), help="clean Ouroboros execution seed")
    parser.add_argument("--source-root", default="", help="pinned CyberGym checkout")
    parser.add_argument("--data-root", default="", help="CyberGym data directory")
    parser.add_argument("--tasks-file", default="", help="pinned tasks.json catalog")
    parser.add_argument("--task-id", action="append", default=[], help="task id (repeatable, e.g. arvo:47101)")
    parser.add_argument("--server", default="", help="private CyberGym submit server URL")
    parser.add_argument("--mask-map", default="", help="task mask-map JSON")
    parser.add_argument("--difficulty", default=DEFAULT_LEVEL)
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument(
        "--settings-path",
        default=str(pathlib.Path(__file__).with_name("settings_base.json")),
        help="settings template (never the live Ouroboros settings file)",
    )
    parser.add_argument("--out-dir", default="", help="append-only benchmark output root")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_CAP_USD)
    parser.add_argument("--per-task-estimate-usd", type=float, default=None,
                        help="finite reservation required for a paid injected executor")
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--executor", default="", help="post-admission module:function callback")
    parser.add_argument("--dry-run", action="store_true", help="write a protocol plan without invoking an executor")
    parser.add_argument("--allow-dirty-seed", action="store_true",
                        help="record and proceed with a dirty seed (not submittable)")
    parser.add_argument("--expected-source-sha256", default="")
    parser.add_argument("--expected-tasks-sha256", default=OFFICIAL_TASKS_SHA256)
    return parser.parse_args(argv)


def _declared_task_ids(args: argparse.Namespace) -> list[str]:
    """Normalize explicit ids without touching the task catalog."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in list(args.task_id or []):
        task_id = safe_task_id(str(raw))
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        result.append(task_id)
    return result


def _generator_template(args: argparse.Namespace) -> list[str]:
    """A manifest-safe command shape, using a placeholder task until catalog load."""
    task = "arvo:0"
    return build_generate_task_argv(
        task,
        out_dir="<task-output>",
        data_dir=str(args.data_root or "<cybergym-data>"),
        server=str(args.server or "<private-server>"),
        mask_map=str(args.mask_map or "") or None,
        difficulty=str(args.difficulty or DEFAULT_LEVEL),
    )


def _load_executor(spec: str) -> Callable[[TaskSpec, pathlib.Path], Mapping[str, Any]]:
    """Resolve an explicitly requested callback only after durable admission."""
    text = str(spec or "").strip()
    if not text or ":" not in text:
        raise CyberGymIntegrationUnavailable(
            "no CyberGym executor supplied; pass --executor module:function or use --dry-run"
        )
    module_name, function_name = text.rsplit(":", 1)
    if not module_name or not function_name:
        raise CyberGymIntegrationUnavailable("--executor must be module:function")
    try:
        module = importlib.import_module(module_name)
        callback = getattr(module, function_name)
    except (ImportError, AttributeError) as exc:
        raise CyberGymIntegrationUnavailable(f"CyberGym executor could not be loaded: {text}") from exc
    if not callable(callback):
        raise CyberGymIntegrationUnavailable(f"CyberGym executor is not callable: {text}")
    return callback


def _task_specs(task_ids: Sequence[str], *, level: str = DEFAULT_LEVEL) -> list[TaskSpec]:
    return [TaskSpec(task_id, task_id.split(":", 1)[0], level) for task_id in task_ids]


def _write_planned_rows(out_root: pathlib.Path, task_ids: Sequence[str], *, level: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        row = build_task_result_row(
            task_id,
            status="planned",
            lifecycle="dry_run",
            level=level,
            masked_id=mask_task_id(task_id),
            infra_reason="dry_run",
            artifact_refs={"run_root": str(out_root)},
        )
        append_cybergym_result(out_root, row)
        rows.append(row)
    return rows


def _prepare_applied_settings(
    template_path: pathlib.Path, out_root: pathlib.Path, args: argparse.Namespace
) -> tuple[pathlib.Path, dict[str, Any]]:
    """Derive a sanitized settings snapshot after admission.

    The template is read only after ``admit_benchmark_run`` has persisted the
    manifest.  ``build_isolated_settings`` filters credentials and legacy keys;
    explicit benchmark overrides then become the applied, auditable settings
    for an injected server.  The snapshot is an ordinary run artifact, never
    the live settings path.
    """
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CyberGymIntegrationUnavailable(
            f"settings template is unreadable or invalid: {template_path}"
        ) from exc
    if not isinstance(template, dict):
        raise CyberGymIntegrationUnavailable("settings template must contain a JSON object")
    from devtools.benchmarks.common.manifests import write_json
    from devtools.benchmarks.common.server_runner import build_isolated_settings

    model = str(args.model or "").strip()
    overrides: dict[str, Any] = {
        "OUROBOROS_MODEL": model,
        "OUROBOROS_MODEL_LIGHT": model,
        "OUROBOROS_MODEL_VISION": model,
        "OUROBOROS_MODEL_CONSCIOUSNESS": model,
        "OUROBOROS_MODEL_FALLBACKS": model,
        "OUROBOROS_MODEL_DEEP_SELF_REVIEW": model,
        "OUROBOROS_SCOPE_REVIEW_MODELS": model,
        "OUROBOROS_SCOPE_REVIEW_MODEL": model,
        "OUROBOROS_REVIEW_MODELS": ",".join([model] * 3),
        "OUROBOROS_MAX_SUBAGENT_DEPTH": 0,
        "TOTAL_BUDGET": float(args.budget_usd),
        "OUROBOROS_TASK_ABS_CEILING_SEC": int(float(args.timeout_sec)),
    }
    # The provider pool is selected by a live probe in the sidecar lane.  Keep
    # the template's safe fallback-ready shape until that callback supplies an
    # evidence-backed ``only``/``order`` override.
    provider = {"allow_fallbacks": True, "require_parameters": True}
    overrides["OUROBOROS_OR_PROVIDER"] = json.dumps(provider, separators=(",", ":"))
    applied = build_isolated_settings(template, **overrides)
    output_path = out_root / "settings_applied.json"
    write_json(output_path, applied)
    return output_path, {
        "path": str(output_path),
        "template_path": str(template_path),
        "model": model,
        "budget_usd": float(args.budget_usd),
        "task_abs_ceiling_sec": int(float(args.timeout_sec)),
        "provider_policy": provider,
        "provider_probe_required": True,
        "keys": sorted(str(key) for key in applied if isinstance(key, str)),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    # Everything through this point is argument/path arithmetic.  In particular,
    # do not read tasks.json, inspect Docker, import the optional upstream package,
    # or create a directory before the shared admission manifest exists.
    try:
        declared_ids = _declared_task_ids(args)
    except ValueError as exc:
        print(f"[cybergym] pre-admission refusal: {exc}", file=sys.stderr)
        return 2

    repo_dir = pathlib.Path(args.repo_dir).expanduser().resolve(strict=False)
    out_root = pathlib.Path(args.out_dir).expanduser().resolve(strict=False) if args.out_dir else run_root(BENCHMARK_NAME, args.run_id)
    manifest_path = out_root / "run_manifest.json"
    ledger_path = out_root / "result_index.jsonl"
    settings_path = pathlib.Path(args.settings_path).expanduser().resolve(strict=False) if args.settings_path else None

    # Pure confinement is intentionally separate from ensure_* helpers: the
    # latter mkdir and are forbidden before admission by launcher_audit.
    try:
        assert_outside_repo(out_root, repo_dir)
        assert_file_output_outside_repo(manifest_path, repo_dir)
        assert_file_output_outside_repo(ledger_path, repo_dir)
        if args.tasks_file:
            assert_file_output_outside_repo(pathlib.Path(args.tasks_file), repo_dir)
        if args.source_root:
            assert_outside_repo(pathlib.Path(args.source_root), repo_dir)
        if args.data_root:
            assert_outside_repo(pathlib.Path(args.data_root), repo_dir)
    except (ValueError, OSError) as exc:
        print(f"[cybergym] pre-admission path refusal: {exc}", file=sys.stderr)
        return 2

    report = pre_admission_report(
        task_ids=declared_ids,
        output_root=out_root,
        repo_dir=repo_dir,
        source_root=args.source_root,
        data_root=args.data_root,
        server_url=args.server,
        difficulty=str(args.difficulty or DEFAULT_LEVEL),
        model=str(args.model or ""),
        api_key=None,
        require_api_key=False,
        settings_path=settings_path,
        require_settings=True,
        network_mode="cybergym-internal",
    )
    if not report["ok"]:
        print("[cybergym] pre-admission refusal: " + "; ".join(report["reasons"]), file=sys.stderr)
        return 2

    try:
        manifest = admit_benchmark_run(
            manifest_path,
            benchmark=BENCHMARK_NAME,
            run_root=out_root,
            repo_dir=repo_dir,
            requested_task_ids=declared_ids,
            require_clean=not bool(args.allow_dirty_seed),
            argv=list(sys.argv if argv is None else [sys.argv[0], *argv]),
            dataset="sunblaze-ucb/cybergym",
            harness={
                "model": str(args.model),
                "difficulty": str(args.difficulty),
                "server": str(args.server),
                "timeout_sec": float(args.timeout_sec),
                "executor": bool(args.executor),
            },
            official_command=_generator_template(args),
            isolated_data_root=str(args.data_root or ""),
            settings_path=settings_path,
            output_paths={
                "run_root": str(out_root),
                "manifest": str(manifest_path),
                "ledger": str(ledger_path),
            },
            extra={
                "source_pin": OFFICIAL_SOURCE_PIN,
                "data_revision": OFFICIAL_DATA_REVISION,
                "tasks_sha256_expected": str(args.expected_tasks_sha256 or ""),
                "metric_name": "final_submission",
                "any_of_projection": "diagnostic_only",
                "network_contract": "cybergym-internal",
                "final_poc_basename": "final.poc",
                "budget_cap_usd": float(args.budget_usd),
            },
        )
    except BenchmarkAdmissionRefused as exc:
        print(f"[cybergym] admission refused: {exc}", file=sys.stderr)
        return 2

    with finalize_run_manifest(manifest_path, manifest, outcome="completed") as final:
        try:
            task_ids = list(declared_ids)
            catalog: dict[str, Any] | None = None
            if args.tasks_file:
                catalog = load_task_catalog(
                    args.tasks_file,
                    expected_sha256=str(args.expected_tasks_sha256 or ""),
                    level=DEFAULT_LEVEL,
                )
                if task_ids:
                    allowed = set(catalog["task_ids"])
                    missing = [task_id for task_id in task_ids if task_id not in allowed]
                    if missing:
                        raise CyberGymError("requested task ids are absent from pinned catalog: " + ", ".join(missing))
                else:
                    task_ids = list(catalog["task_ids"])
                manifest["extra"]["task_catalog"] = catalog
            if not task_ids:
                final.update({
                    "outcome": "refused",
                    "exit_code": 2,
                    "refusal": {"stage": "task_selection", "reason": "no_task_ids", "exit_code": 2},
                })
                print("[cybergym] no tasks selected", file=sys.stderr)
                return 2

            manifest["requested_task_ids"] = task_ids
            manifest["requested_count"] = len(task_ids)

            applied_path, applied_metadata = _prepare_applied_settings(settings_path, out_root, args)
            manifest.setdefault("extra", {})["settings_snapshot"] = applied_metadata
            manifest.setdefault("output_paths", {})["settings_applied"] = str(applied_path)

            if args.dry_run:
                rows = _write_planned_rows(out_root, task_ids, level=DEFAULT_LEVEL)
            else:
                if args.per_task_estimate_usd is None:
                    raise CyberGymIntegrationUnavailable(
                        "paid CyberGym execution requires --per-task-estimate-usd; no price is invented"
                    )
                executor = _load_executor(args.executor)
                rows = run_campaign(
                    _task_specs(task_ids),
                    run_root=out_root,
                    executor=executor,
                    estimated_cost_usd=float(args.per_task_estimate_usd),
                    budget_cap_usd=float(args.budget_usd),
                )

            projection_path = out_root / "claims.jsonl"
            try:
                budget = BudgetLedger(projection_path, cap_usd=float(args.budget_usd)).projection()
                manifest["extra"]["budget_projection"] = budget.as_dict()
            except CyberGymError as exc:
                manifest["extra"]["budget_projection"] = {"available": False, "error": str(exc)}
            manifest["extra"].update({
                "rows_written": len(rows),
                "completed_count": sum(1 for row in rows if row.get("status") in {"completed", "planned"}),
                "infra_count": sum(1 for row in rows if row.get("status") in {"infra_failed", "blocked"}),
            })
            code = 0 if args.dry_run or all(row.get("status") == "completed" for row in rows) else 2
            if code:
                final.update({"outcome": "integration_or_task_failure", "exit_code": code})
            else:
                final.update({"outcome": "completed", "exit_code": 0})
            print(out_root)
            return code
        except CyberGymError as exc:
            final.update({
                "outcome": "refused",
                "exit_code": 2,
                "refusal": {"stage": "integration", "reason": type(exc).__name__, "exit_code": 2},
            })
            print(f"[cybergym] refused: {exc}", file=sys.stderr)
            return 2
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            final.update({
                "outcome": "refused",
                "exit_code": 2,
                "refusal": {"stage": "post_admission_preflight", "reason": type(exc).__name__, "exit_code": 2},
            })
            print(f"[cybergym] failed: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
