#!/usr/bin/env python3
"""FROZEN benchmark — ouroboros assembly-fit (INIT, task campaign_4518_fixture).

Measures whether ouroboros' two internal review lanes (scope review +
deep self review) can assemble their required artifacts inside the existing
reviewer window on a real-scale change, over the IMMUTABLE fixture
`.arbor/campaign_4518_fixture.json` (131 repo paths == the megacommit file set
of a real campaign). Prints `assembly_fit_rate=<float>` on the last line.

LANES
-----
* scope lane:  `ouroboros.tools.scope_review._build_scope_prompt` — the real
  pre-dispatch context assembler for the blocking scope gate. 40 deterministic
  trials; trial i stages a cumulative megacommit over the first `size_i` fixture
  paths (stable fixture order, sizes 2..131). A trial PASSES iff the assembler
  returns a prompt (status None) — i.e. all required artifacts (canonical docs,
  protected/review-stack touched files) fit inside the reviewer input limit and
  no ladder rung had to fail closed.

* deep lane:  one whole-repository trial through the deep self-review pack
  builder (`ouroboros.deep_self_review.build_review_pack`) plus the exact
  input gate / shrink-retry / final gate arithmetic of `run_deep_self_review`
  (replicated here so the bench never touches a provider). PASSES iff the pack
  assembles (no required-artifact omission, no FATAL) and the final estimated
  system+pack token count fits the model-family-calibrated input limit.

assembly_fit_rate = (scope_trial_pass_rate + deep_lane_pass) / 2.

HERMETIC / DETERMINISTIC
------------------------
* runs with a fresh temp OUROBOROS_DATA_DIR (no ambient capability evidence)
* strips provider credentials so no network probe can run
* stubs `capability_evidence.probe` (windows resolve to the shipped >=1M SSOT
  sentinel / full-window default, exactly like a cold-evidence install)
* freezes token density at the capability_evidence COLD_START_TOKEN_DENSITY
  floor (1.65) so both lanes' input limits are machine-independent constants
  (scope & deep reviewer window = 1,000,000 => input limit 545,454).
* lane model identities are the shipped defaults:
  scope openai/gpt-5.6-terra, deep openai/gpt-5.6-sol-pro.
* scenario edits are seeded (deterministic content rewrites of ~0.5% lines per
  touched file). Scratch repos are git worktrees of the repo under test, reset
  between trials; the outer repo's index is never touched.

The fixture is immutable B_dev; there is no separate B_test. The regression
gate `pytest tests/test_scope_review.py tests/test_deep_self_review.py -q`
must stay green alongside this metric.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# Frozen constants
# --------------------------------------------------------------------------
SCOPE_MODEL_DEFAULT = "openai/gpt-5.6-terra"
# Frozen immutable fixture location (the repo under test may be a worktree
# whose .arbor/ is not tracked; the fixture is DATA, never code).
FIXTURE_PATH_DEFAULT = "/home/fy/Projects/code/ouroboros/.arbor/campaign_4518_fixture.json"
DEEP_MODEL_DEFAULT = "openai/gpt-5.6-sol-pro"
DEFAULT_SCOPE_TRIALS = 40
FROZEN_WINDOW_TOKENS = 1_000_000
FROZEN_TOKEN_DENSITY = 1.65  # capability_evidence.COLD_START_TOKEN_DENSITY
LINE_EDIT_FRACTION = 0.005  # fraction of lines deterministically rewritten per touched file
MIN_EDITS_PER_FILE = 3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _hermetic_environment() -> pathlib.Path:
    """Isolate capability evidence + credentials; return the temp DATA_DIR."""
    fresh = pathlib.Path(tempfile.mkdtemp(prefix="assembly-fit-data-"))
    os.environ["OUROBOROS_DATA_DIR"] = str(fresh)
    for key in list(os.environ):
        up = key.upper()
        if (
            up.endswith("_API_KEY")
            or "BASE_URL" in up
            or up.startswith("OUROBOROS_USE_LOCAL")
            or key == "OUROBOROS_SCOPE_REVIEW_MODEL"
            or key == "OUROBOROS_MODEL_DEEP_SELF_REVIEW"
        ):
            os.environ.pop(key, None)
    return fresh


def _freeze_evidence(data_dir: pathlib.Path) -> None:
    """Deterministic cold-evidence semantics regardless of machine/network."""
    import ouroboros.capability_evidence as ce

    def _no_probe(*_a, **_k):
        return None

    def _cold_density(*_a, **_k):
        return (FROZEN_TOKEN_DENSITY, "frozen_cold_floor")

    ce.probe = _no_probe
    ce.resolve_review_token_density = _cold_density


def _edits_for_path(scratch: pathlib.Path, rel: str, seed: int) -> None:
    """Deterministic real-scale content edits for one megacommit file.

    Existing files: ~0.5% of lines rewritten (seeded by path + trial).
    Missing files (fixture paths absent at HEAD): created as additions.
    """
    fp = scratch / rel
    if not fp.is_file():
        fp.parent.mkdir(parents=True, exist_ok=True)
        suffix = pathlib.PurePosixPath(rel).suffix.lstrip(".") or "txt"
        stub = (
            f"# {rel}\n\nBenchmark fixture addition (campaign 4518 megacommit).\n"
            f"placeholder-{seed}\n" + ("x = 1\n" * 40) if suffix in {"py", "pyw"} else
            f"# {rel}\n\nBenchmark fixture addition (campaign 4518 megacommit).\n"
            f"placeholder-{seed}\n" + ("/* filler */\n" * 40) if suffix in {"js", "css"} else
            f"# {rel}\n\nBenchmark fixture addition (campaign 4518 megacommit).\nplaceholder-{seed}\n"
        )
        fp.write_text(stub, encoding="utf-8")
        return
    raw = fp.read_bytes()
    if not raw:
        return
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return
    lines = text.splitlines(keepends=True)
    if not lines:
        return
    rng = random.Random(seed * 1000003 + abs(hash(rel)) % 1000003)
    n = max(MIN_EDITS_PER_FILE, int(len(lines) * LINE_EDIT_FRACTION))
    n = min(n, len(lines))
    for i in rng.sample(range(len(lines)), n):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue
        if line.lstrip().startswith(("#", "//", "/*", "*", "--", "\"", "'")):
            lines[i] = line.rstrip("\n") + "  # megacommit\n"
        else:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = indent + line.strip() + "  # megacommit\n"
    fp.write_text("".join(lines), encoding="utf-8")


def _stage_megacommit(scratch: pathlib.Path, touched: list[str], seed: int) -> int:
    """Reset the scratch repo and stage a deterministic megacommit over `touched`.

    Returns the staged diff size in chars.
    """
    subprocess.run(["git", "-C", str(scratch), "reset", "--hard", "HEAD"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(scratch), "clean", "-fdx"],
                   check=True, capture_output=True)
    for rel in touched:
        _edits_for_path(scratch, rel, seed)
    subprocess.run(["git", "-C", str(scratch), "add", "-A"],
                   check=True, capture_output=True)
    diff = subprocess.run(["git", "-C", str(scratch), "diff", "--cached"],
                          capture_output=True, text=True).stdout
    return len(diff)


def _scope_trial(
    sr,
    scratch: pathlib.Path,
    touched: list[str],
    seed: int,
    scope_model: str,
):
    """Run one scope-lane assembly trial; return (ok, detail dict)."""
    from ouroboros.utils import estimate_tokens

    t0 = time.time()
    diff_chars = _stage_megacommit(scratch, touched, seed)
    context = sr._ScopePromptContext(scope_model=scope_model)
    try:
        prompt, status = sr._build_scope_prompt(
            scratch, f"bench assembly-fit scope trial seed={seed}",
            context=context,
        )
    except Exception as exc:  # assembly must never crash the harness silently
        detail = {"trial": seed, "touched": len(touched), "seconds": round(time.time() - t0, 1)}
        detail["ok"] = False
        detail["status"] = f"exception:{type(exc).__name__}"
        detail["reason"] = str(exc)[:400]
        return False, detail
    detail = {
        "trial": seed,
        "touched": len(touched),
        "diff_chars": diff_chars,
        "seconds": round(time.time() - t0, 1),
    }
    if prompt is not None:
        detail["ok"] = True
        detail["prompt_tokens"] = estimate_tokens(prompt)
        return True, detail
    detail["ok"] = False
    detail["status"] = getattr(status, "status", "?")
    detail["token_count"] = getattr(status, "token_count", 0)
    detail["unassembled_required"] = list(getattr(status, "unassembled_required", []) or [])
    detail["atlas_overflowed"] = bool(getattr(status, "atlas_overflowed", False))
    return False, detail


def _deep_trial(scratch: pathlib.Path, deep_model: str, data_dir: pathlib.Path):
    """Whole-repo deep-lane assembly, mirroring run_deep_self_review's gate."""
    from ouroboros.deep_self_review import _SYSTEM_PROMPT, build_review_pack
    from ouroboros.reviewer_window import (
        reviewer_context_window,
        window_scaled_reserves,
    )
    from ouroboros.tools.review_helpers import calibrated_input_token_limit
    from ouroboros.utils import estimate_tokens

    t0 = time.time()
    detail = {"lane": "deep", "model": deep_model}
    window = reviewer_context_window(deep_model)
    out_res, margin = window_scaled_reserves(
        window, output_reserve=100_000, tokenizer_margin=155_000
    )
    input_limit = calibrated_input_token_limit(
        deep_model, context_window=window,
        output_reserve=out_res, tokenizer_margin=margin,
    )
    detail["window"] = window
    detail["input_limit"] = input_limit
    pack_text, stats = build_review_pack(
        scratch, data_dir,
        fixed_prompt_tokens=estimate_tokens(_SYSTEM_PROMPT),
        input_token_limit=input_limit,
    )
    if not pack_text:
        detail["ok"] = False
        detail["reason"] = (stats.get("skipped") or ["empty pack"])[0]
        detail["seconds"] = round(time.time() - t0, 1)
        return False, detail
    estimated = estimate_tokens(_SYSTEM_PROMPT + pack_text)
    if estimated > input_limit:
        # Same deterministic shrink retry as run_deep_self_review.
        overage = estimated - input_limit
        pack_text, stats = build_review_pack(
            scratch, data_dir,
            fixed_prompt_tokens=estimate_tokens(_SYSTEM_PROMPT),
            hard_budget_reduction=overage + 8_000,
            input_token_limit=input_limit,
        )
        if not pack_text:
            detail["ok"] = False
            detail["reason"] = (stats.get("skipped") or ["empty pack"])[0]
            detail["seconds"] = round(time.time() - t0, 1)
            return False, detail
        estimated = estimate_tokens(_SYSTEM_PROMPT + pack_text)
    detail["file_count"] = stats.get("file_count")
    detail["pack_chars"] = len(pack_text)
    detail["estimated_tokens"] = estimated
    detail["ok"] = estimated <= input_limit
    if not detail["ok"]:
        detail["reason"] = (
            f"pack still ~{estimated:,} tokens after shrink retry "
            f"(limit ~{input_limit:,})"
        )
    manifest = stats.get("context_manifest") or {}
    detail["atlas_status"] = manifest.get("status")
    detail["atlas_total_tokens"] = manifest.get("estimated_total_tokens")
    unassembled = [row.get("path") for row in (manifest.get("unassembled_required") or [])]
    detail["unassembled_required"] = unassembled
    if unassembled:
        detail["ok"] = False
        detail["reason"] = f"required artifact omitted: {unassembled[:3]}"
    detail["seconds"] = round(time.time() - t0, 1)
    return bool(detail["ok"]), detail


def _scope_prefix_sizes(fixture_len: int, n_trials: int) -> list[int]:
    if n_trials <= 1:
        return [fixture_len]
    raw = sorted({max(2, min(fixture_len, round(2 + (fixture_len - 2) * i / (n_trials - 1)))) for i in range(n_trials)})
    if len(raw) < n_trials and fixture_len >= n_trials:  # rare collision: fill up with extras
        for k in range(2, fixture_len + 1):
            if len(raw) >= n_trials:
                break
            if k not in raw:
                raw.append(k)
        raw = sorted(raw)
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=str(pathlib.Path.cwd()))
    ap.add_argument("--fixture", default=FIXTURE_PATH_DEFAULT,
                    help="path to campaign_4518_fixture.json")
    ap.add_argument("--scope-trials", type=int, default=DEFAULT_SCOPE_TRIALS,
                    help=f"number of scope-lane cumulative trials (default {DEFAULT_SCOPE_TRIALS})")
    args = ap.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    fixture_path = pathlib.Path(args.fixture).resolve()
    if not repo_root.is_dir() or not (repo_root / ".git").exists():
        # tolerate a worktree whose git dir lives elsewhere
        probe = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            print(f"ERROR: {repo_root} is not a git work tree", file=sys.stderr)
            return 2
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, list) or not fixture:
        print(f"ERROR: fixture {fixture_path} is not a non-empty JSON list", file=sys.stderr)
        return 2

    data_dir = _hermetic_environment()
    _freeze_evidence(data_dir)

    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))

    import ouroboros.capability_evidence  # noqa: F401  (patch already applied)
    from ouroboros.tools import scope_review as sr
    from ouroboros.utils import estimate_tokens as _et  # noqa: F401

    n_trials = args.scope_trials
    quick = n_trials != DEFAULT_SCOPE_TRIALS
    print(f"# bench_assembly_fit fixture={fixture_path} fixture_files={len(fixture)}")
    print(f"# repo={repo_root} scope_trials={n_trials}{' (QUICK - not the frozen score)' if quick else ''}")
    print(f"# frozen window={FROZEN_WINDOW_TOKENS} density={FROZEN_TOKEN_DENSITY} "
          f"scope_model={SCOPE_MODEL_DEFAULT} deep_model={DEEP_MODEL_DEFAULT}")

    scratch_root = pathlib.Path(tempfile.mkdtemp(prefix="assembly-fit-scratch-"))
    scratch = scratch_root / "repo"
    try:
        made = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(scratch), "HEAD"],
            capture_output=True, text=True,
        )
        if made.returncode != 0:
            print(f"ERROR: cannot create scratch worktree: {made.stderr.strip()}", file=sys.stderr)
            return 2
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: worktree add failed: {exc}", file=sys.stderr)
        return 2

    # ---- scope lane trials -------------------------------------------------
    fixture_order = [p for p in fixture if p]
    present = [p for p in fixture_order]  # missing ones are modeled as additions
    sizes = _scope_prefix_sizes(len(present), n_trials)
    scope_ok = 0
    scope_details = []
    for idx, size in enumerate(sizes):
        touched = present[:size]
        seed = 4518_000 + idx
        ok, detail = _scope_trial(sr, scratch, touched, seed, SCOPE_MODEL_DEFAULT)
        scope_ok += bool(ok)
        scope_details.append(detail)
        flag = "PASS" if ok else "FAIL"
        extra = ""
        if not ok:
            extra = f" status={detail.get('status')} tokens={detail.get('token_count')} unassembled={len(detail.get('unassembled_required') or [])}"
        print(f"scope[{idx:02d}] {flag} touched={detail['touched']} diff_chars={detail['diff_chars']:,} "
              f"{detail['seconds']}s{extra}")
        if not ok and detail.get("unassembled_required"):
            print(f"        unassembled sample: {detail['unassembled_required'][:5]}")

    # ---- deep lane trial ---------------------------------------------------
    # reset scratch to a pristine HEAD state first
    subprocess.run(["git", "-C", str(scratch), "reset", "--hard", "HEAD"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(scratch), "clean", "-fdx"],
                   check=True, capture_output=True)
    deep_ok, deep_detail = _deep_trial(scratch, DEEP_MODEL_DEFAULT, data_dir)
    print(f"deep    {'PASS' if deep_ok else 'FAIL'} {json.dumps(deep_detail)}")

    scope_rate = scope_ok / n_trials
    deep_score = 1.0 if deep_ok else 0.0
    rate = (scope_rate + deep_score) / 2.0

    print(f"# scope_pass={scope_ok}/{n_trials} deep_pass={1 if deep_ok else 0}/1")
    print(f"assembly_fit_rate={rate:.6f}")

    # cleanup
    subprocess.run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(scratch)],
                   capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
