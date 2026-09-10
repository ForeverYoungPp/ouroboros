#!/usr/bin/env python3
"""Run every acceptance criterion's ``verify_command`` against a tree.

The criteria in a Seed are self-contained shell commands with a known exit
code, so they can be checked without an orchestrator, an agent runtime, or an
evidence/verifier layer.  This runs them in order and reports which pass.

    uv run --locked python scripts/check_seed.py docs/superpowers/specs/seed-0-import-graph.yaml
    uv run --locked python scripts/check_seed.py <seed> -C ~/.ouroboros/worktrees/ouroboros/orch_xxxx

Exit code is 0 only when every criterion that has a command passed.  Criteria
that carry only a ``verify_exemption_reason`` are reported as skip, never as a
pass.  Commands are expected to be non-destructive; they run with the tree as
their working directory.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

import yaml

# A stale VIRTUAL_ENV in the caller's shell makes uv print a warning on every
# invocation, which would otherwise be the last line and hide the real result.
_NOISE = ("warning: `VIRTUAL_ENV=",)


def run_one(cmd: str, tree: pathlib.Path, timeout: float) -> dict[str, object]:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit": None, "detail": f"exceeded {timeout:g}s"}
    lines = [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.strip() and not line.startswith(_NOISE)
    ]
    detail = ""
    if lines:
        # Prefer the failure itself over incidental trailing output.
        interesting = [
            line for line in lines if "Error" in line or "assert" in line.lower() or "FAIL" in line
        ]
        detail = (interesting[-1] if interesting else lines[-1])[:200]
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "exit": proc.returncode,
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seed", help="seed YAML path")
    ap.add_argument("-C", "--tree", default=".", help="tree to check (default: cwd)")
    ap.add_argument("--timeout", type=float, default=900.0, help="per-criterion seconds (default 900)")
    ap.add_argument("--only", type=int, action="append", metavar="N", help="1-based index (repeatable)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    tree = pathlib.Path(args.tree).expanduser().resolve()
    if not tree.is_dir():
        sys.exit(f"not a directory: {tree}")
    seed = yaml.safe_load(pathlib.Path(args.seed).read_text(encoding="utf-8"))
    criteria = seed.get("acceptance_criteria") or []

    results: list[dict[str, object]] = []
    for index, ac in enumerate(criteria, 1):
        if args.only and index not in args.only:
            continue
        cmd = ac.get("verify_command")
        if not cmd:
            results.append(
                {
                    "ac": index,
                    "status": "skip",
                    "exit": None,
                    "detail": (ac.get("verify_exemption_reason") or "no verify_command")[:200],
                }
            )
            continue
        row = {"ac": index, **run_one(cmd, tree, args.timeout)}
        results.append(row)

    if args.json:
        print(json.dumps({"tree": str(tree), "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"tree: {tree}")
        for r in results:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "timeout": "TIMEOUT"}[str(r["status"])]
            exit_code = "" if r["exit"] is None else f" exit={r['exit']}"
            print(f"  AC-{r['ac']:<3} {mark:<8}{exit_code:<10} {r['detail']}")
        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] in ("fail", "timeout"))
        skipped = sum(1 for r in results if r["status"] == "skip")
        tail = f", {skipped} manual/skipped (no verify_command — not counted as passed)" if skipped else ""
        print(f"\n  {passed} passed, {failed} failed{tail}")

    return 1 if any(r["status"] in ("fail", "timeout") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
