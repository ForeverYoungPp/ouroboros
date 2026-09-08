# Arbor Contract — ARCHITECTURE.md Loading Strategy Selection

## Metric
- Gate: `uv run --locked python -m pytest tests/test_scope_review.py tests/test_deep_self_review.py -q` all green
- `scope_pack_fit_ratio` (maximize): scope review pack assembly success rate on large changes
- `agent_locate_accuracy` (maximize): agent locating 10 designated sections via the loading form (steps/accuracy)

## Baseline
- scope_pack_fit_ratio = 0 (current full-text form: 170,660 rendered row_cost > 41,198 remaining, FATAL)
- agent_locate_accuracy: measured during INIT

## Ambition
- Assembly success rate 0 → 100%; agent locate accuracy not below full-text form; best effort within budget

## Scope
- mixed

## Hard Constraints
- Do not modify tests/ or eval scripts themselves (no metric gaming)
- B_test used only for merge verification
- P1: zero semantic loss of the document (indexing is relocation, not omission)
- P3 required fail-closed semantics must not be relaxed
- Protected paths: BIBLE.md, docs/ARCHITECTURE.md, data/, ~/Ouroboros/data

## Budget
- ≤ 6 cycles, ≤ 48h per experiment

## Candidate Priors (coordinator priors)
1. Full nav-map coverage (atlas + fixed part) — first unlock
2. Physical chunking (Corpus2Skill-style) — falsified by controlled studies for single documents; expect round-1 elimination
3. Force-include whitelisting (contracts/ directory prefix → explicit file table)
4. Doc partitioning (resident layer / archive layer reorganization)
5. Hybrid (nav-map coverage + force-include whitelisting + doc partitioning/hot-sections) — end-state; #1 unlocks first, #4 orthogonal to #1, #3 is the direct fix for the 37/40 assembly failures and must be part of the end-state
6. Hot-sections (frequent sections resident + nav map for the rest) — quality hedge variant of nav map, if navigation tests show repeated-read cost exceeds residency

## Background (root causes, for coordinator context)
- deep_self_review lane: full ARCH (149k) as required atlas artifact in max mode; 170,660 row_cost does not fit → FATAL
- scope review lane: 37/40 "could not assemble required artifact(s)" — contracts/+ci.yml force-include directory prefix too coarse; touched required files rendered in full compete for the same hard budget (2 model failures, 1 prompt overflow are the other cases)
- scope_review.py:303-319 `_load_canonical_context_docs` inlines five governance docs into the scope prompt fixed part — second scale risk (149k ARCH + 59k DEV); canonical docs sit outside the atlas competition but the fixed part can blow independently
- A 1M reviewer is NOT the fix: ref adjusts window size, never the required ladder; owner self-tested and refuted (ATLAS_MIXED_ASSEMBLY_REMEDY's "configure a larger-window reviewer" is refuted — do not treat as a fallback)

## Owner Constraints & INIT
- No content deletion ("the doc genuinely holds this much"); candidate set must cover: (1) current full-text (baseline), (2) nav-map conversion, (3) doc partitioning (pure editing, zero code), (4) hot-sections
- INIT must first build the reproduction fixture: extract campaign 4518 touched-path set from ~/Ouroboros/data/memory/consciousness_observations.json (~18-26 contracts files + ci.yml); compare pre/post-fix build_scope_review_prompt+atlas assembly and token accounting (170,660 row_cost → ~766 nav map, ~211k freed); use measured numbers to eliminate most candidates statically, save budget for navigation tests of survivors
- Mechanical navigation-test criterion: nav-map line ranges as ground truth; success = agent hits target section within ≤K read_file calls; count tokens/turns
