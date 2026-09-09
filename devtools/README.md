# Ouroboros Devtools

`devtools/` contains operator-side and benchmark support code that should be
versioned with Ouroboros without becoming part of the runtime core.

Rules:

- Generated logs, datasets, run outputs, Docker layers, and secrets do not live
  here.
- Default benchmark outputs go under `/Users/anton/Ouroboros/bench_runs/`.
- Runtime modules must not import `devtools`.
- This is not an immune-system bypass: touched files are reviewed normally.
- Promote code out of `devtools` only through a separate reviewed runtime plan.

## bulk_inventory — bulk repository inventory

`bulk_inventory.py` is a read-only, pure-stdlib CLI: one invocation walks a
file tree and reports, per labeled pattern, how many files contain at least
one matching line and how many lines match (grep -c line semantics), plus
aggregate coverage. JSON on stdout; every skip (binary, oversize, symlink,
unreadable, glob filter, walk error) is counted and disclosed — never silent.

```bash
# Coverage of "Verification" across all skills, one call:
python3 devtools/bulk_inventory.py --root <repo>/skills --match ver:Verification

# Several inventory questions in one walk:
python3 devtools/bulk_inventory.py --root <dir> \
  --match ver:Verification --match desc:'^description:' --regex

# Scope with globs and case handling:
python3 devtools/bulk_inventory.py --root <dir> --include '*.md' \
  --exclude 'archive/*' --ignore-case --match ver:verification
```

Notes:

- Patterns are literal substrings by default; `--regex` applies to all
  `--match` specs of that invocation (regex vs literal coverage is compared
  across separate invocations).
- Read-only subagents cannot execute this via `run_command`
  (source inspection 2026-09-09: `run_command` is absent from
  `LOCAL_READONLY_SUBAGENT_TOOL_NAMES` in `ouroboros/tool_capabilities.py`):
  the parent agent (self-modification profile) and acting subagents invoke it;
  read-only subagents keep using `search_code` until a separately reviewed
  runtime promotion (reachability_status=inspected_negative).
- Runtime promotion out of `devtools` (e.g. a `bulk_count` read-family tool
  in `ouroboros/tools/`) follows the promotion rule above.

