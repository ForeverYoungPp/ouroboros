"""Phase D (review-custody sprint): the advisory lane's route-aware pack and
honest overflow classification.

Item 7 (owner-accepted A): the advisory used to inline ~830KB of governance
docs on BOTH delivery routes while its only size gate was the 1.6M char
constant — far above any real route window — so oversize prompts died
downstream as a false "harness crashed / Retry" classification. Now:

* api route: admission consults the REAL route window from the reviewer-window
  SSOT (``reviewer_window.resolve_reviewer_window``), not the 1.6M constant
  (which survives only as an emergency sanity ceiling);
* agent_session route: governance BODIES are replaced by resolvable pointers
  plus mandatory-read instructions (the plan-review agent_session precedent) —
  the session reads the docs itself;
* a dispatched failure matching the ``context_budget`` overflow SSOT becomes
  the typed non-blocking ``ADVISORY_SKIPPED: context_window_exceeded`` outcome;
  every other failure keeps the ``ADVISORY_ERROR`` shape.

Offline fixtures throughout: the window resolver and the transports are faked.
"""

import json
from types import SimpleNamespace

import pytest

import ouroboros.tools.claude_advisory_review as advisory
from ouroboros.reviewer_window import ReviewerWindow


_ADVISORY_ITEMS = json.dumps([
    {"item": "correctness", "verdict": "PASS", "severity": "advisory",
     "reason": "checked end to end"},
])


def _ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir(exist_ok=True)
    drive.mkdir(exist_ok=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


def _write_governance_docs(repo):
    """Governance docs with one distinctive body marker each."""
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "BIBLE.md").write_text(
        "# BIBLE\nBIBLE-BODY-MARKER-7Q\n", encoding="utf-8")
    (repo / "docs" / "CHECKLISTS.md").write_text(
        "## Repo Commit Checklist\nCHECKLIST-BODY-MARKER-7Q\n", encoding="utf-8")
    (repo / "docs" / "DEVELOPMENT.md").write_text(
        "# DEV\nDEVELOPMENT-BODY-MARKER-7Q\n", encoding="utf-8")
    (repo / "docs" / "DESIGN.md").write_text(
        "# DESIGN\nDESIGN-BODY-MARKER-7Q\n", encoding="utf-8")
    (repo / "docs" / "ARCHITECTURE.md").write_text(
        "# ARCH\nARCHITECTURE-BODY-MARKER-7Q\n", encoding="utf-8")


_DOC_MARKERS = (
    "BIBLE-BODY-MARKER-7Q",
    "CHECKLIST-BODY-MARKER-7Q",
    "DEVELOPMENT-BODY-MARKER-7Q",
    "DESIGN-BODY-MARKER-7Q",
    "ARCHITECTURE-BODY-MARKER-7Q",
)


@pytest.fixture()
def api_env(monkeypatch):
    # Native (routed) advisory delivery: credentials follow the routed model.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, raising=False)
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)


def _fake_window(monkeypatch, tokens: int):
    monkeypatch.setattr(
        "ouroboros.reviewer_window.resolve_reviewer_window",
        lambda model, **kw: ReviewerWindow(
            window_tokens=tokens, status="confirmed", model=str(model)),
    )


def _no_dispatch(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - failure signal only
        raise AssertionError("provider dispatch must not happen")
    monkeypatch.setattr(advisory, "_run_advisory_native", _boom)


def _stub_run_readonly(monkeypatch, **overrides):
    """Stub the NATIVE episode runner with the shared rehydrated result shape."""
    result = SimpleNamespace(
        success=True, result_text=_ADVISORY_ITEMS, session_id="sess-1",
        cost_usd=0.0, usage={}, error="", stderr_tail="",
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    monkeypatch.setattr(
        advisory, "_run_advisory_native",
        lambda prompt, repo_dir, ctx_, slot, model: (result, model),
    )
    return result


# ---------------------------------------------------------------------------
# 1. api admission consults the REAL route window, not the 1.6M constant
# ---------------------------------------------------------------------------


def test_api_admission_small_window_skips_before_dispatch(tmp_path, monkeypatch, api_env):
    """A small evidenced window skips the advisory BEFORE any dispatch even
    though the prompt is far below the 1.6M char constant — the constant is no
    longer the admission gate."""
    _fake_window(monkeypatch, 1_000)
    _no_dispatch(monkeypatch)
    ctx = _ctx(tmp_path)
    items, raw, model, chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED:")
    assert "does not fit the api route window" in raw
    # The reason names the window and the measured size.
    assert "1,000-token window" in raw
    assert f"{chars:,} chars" in raw
    assert chars < advisory._ADVISORY_PROMPT_MAX_CHARS  # constant did not decide
    assert model == advisory._advisory_default_model()
    # The pre-dispatch window skip stamps the meta snapshot like every skip.
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "skipped"
    assert meta.get("skip_reason") == "route_window_exceeded"


def test_api_admission_big_window_proceeds(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(monkeypatch)
    ctx = _ctx(tmp_path)
    items, raw, model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert not raw.startswith("⚠️ ADVISORY_SKIPPED"), raw
    assert not raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert [i["item"] for i in items] == ["correctness"]
    assert model == advisory._advisory_default_model()


def test_api_window_skip_is_the_existing_typed_skip_status(tmp_path, monkeypatch, api_env):
    """The window skip rides the EXISTING non-blocking skip path: the handler
    persists a 'skipped' run for the snapshot, never an error."""
    _fake_window(monkeypatch, 1_000)
    _no_dispatch(monkeypatch)
    # Out-of-scope deterministic gate (P9 release metadata) — stubbed exactly as
    # the existing handler-path tests stub it (test_git_review_pipeline.py).
    monkeypatch.setattr(advisory, "_release_metadata_preflight", lambda *a, **kw: None)
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    payload = json.loads(advisory._handle_advisory_pre_review(
        ctx, commit_message="m", skip_tests=True,
    ))
    assert payload["status"] == "skipped"
    assert "does not fit the api route window" in payload["message"]


# ---------------------------------------------------------------------------
# 2. agent_session prompt: pointers instead of governance bodies
# ---------------------------------------------------------------------------


def test_agent_session_prompt_uses_pointers_not_bodies(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    prompt = advisory._build_advisory_prompt(
        repo, "commit msg",
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "file-a"},
        governance_by_retrieval=True,
    )
    for marker in _DOC_MARKERS:
        assert marker not in prompt
    # Resolvable absolute pointers + the mandatory-read instruction.
    assert "MANDATORY FULL READ" in prompt
    for rel in ("BIBLE.md", "docs/CHECKLISTS.md", "docs/DEVELOPMENT.md",
                "docs/DESIGN.md", "docs/ARCHITECTURE.md"):
        assert str((repo / rel).resolve()) in prompt
    assert "'## Repo Commit Checklist' section" in prompt
    # The non-governance sections are unchanged.
    assert "DIFF-SENTINEL" in prompt
    assert "commit msg" in prompt
    assert "file-a" in prompt


def test_api_prompt_keeps_inlining_governance_bodies(tmp_path):
    """The api-route governance contract is unchanged: full bodies inline."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    prompt = advisory._build_advisory_prompt(
        repo, "commit msg",
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "file-a"},
    )
    # The checklist section loads from the host repo's canonical CHECKLISTS.md
    # (load_checklist_section), so only the four repo-dir docs are asserted.
    assert "BIBLE-BODY-MARKER-7Q" in prompt
    assert "DEVELOPMENT-BODY-MARKER-7Q" in prompt
    assert "DESIGN-BODY-MARKER-7Q" in prompt
    assert "ARCHITECTURE-BODY-MARKER-7Q" in prompt
    assert "MANDATORY FULL READ" not in prompt


def test_delegated_route_dispatches_the_pointer_pack(tmp_path, monkeypatch):
    """_run_claude_advisory on the agent_session route hands the delegated
    session the compact pointer pack, never the inlined governance bodies."""
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)
    ctx = _ctx(tmp_path)
    _write_governance_docs(ctx.repo_dir)
    captured = {}

    def _capture(prompt, repo_dir, ctx_):
        captured["prompt"] = prompt
        return SimpleNamespace(
            success=True, result_text=_ADVISORY_ITEMS, session_id="run-1",
            cost_usd=0.0, usage={}, error="", stderr_tail="",
        ), "fake-session-model"

    monkeypatch.setattr(advisory, "_run_advisory_delegated", _capture)
    items, raw, model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert not raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert [i["item"] for i in items] == ["correctness"]
    assert model == "fake-session-model"
    prompt = captured["prompt"]
    for marker in _DOC_MARKERS:
        assert marker not in prompt
    assert "MANDATORY FULL READ" in prompt
    assert str((ctx.repo_dir / "BIBLE.md").resolve()) in prompt


# ---------------------------------------------------------------------------
# 3. post-dispatch overflow classification (context_budget SSOT)
# ---------------------------------------------------------------------------


def test_api_overflow_failure_becomes_typed_skip(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="API Error: prompt is too long: 251078 tokens > 200000 maximum",
    )
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw
    assert "native route" in raw
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "skipped"
    assert meta.get("skip_reason") == "context_window_exceeded"


def test_delegated_overflow_failure_becomes_typed_skip(tmp_path, monkeypatch):
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)

    def _failed(prompt, repo_dir, ctx_):
        return SimpleNamespace(
            success=False, result_text="(no output)", session_id="",
            cost_usd=0.0, usage={},
            error="ReviewSessionError: Prompt is too long for the selected route",
            stderr_tail="",
        ), ""

    monkeypatch.setattr(advisory, "_run_advisory_delegated", _failed)
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw
    assert "agent_session route" in raw


def test_raised_overflow_exception_becomes_typed_skip(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)

    def _raise(*a, **k):
        raise RuntimeError("provider rejected: context_length_exceeded")

    monkeypatch.setattr(advisory, "_run_advisory_native", _raise)
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw


def test_generic_failure_stays_advisory_error(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="transport reset by peer",
    )
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert "context_window_exceeded" not in raw


def test_output_limit_rejection_is_not_reclassified(tmp_path, monkeypatch, api_env):
    """The SSOT's output-size precedence holds: an output/body-limit rejection
    is NOT a window overflow and keeps the error shape."""
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="max_tokens 65536 exceeds the maximum allowed for this model",
    )
    ctx = _ctx(tmp_path)
    _items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert raw.startswith("⚠️ ADVISORY_ERROR"), raw


# ---------------------------------------------------------------------------
# 4. evidence by retrieval: the touched pack is not inlined
#
# Measured 2026-09-13 over the last six real commits' file sets: the touched
# pack is the full content of every changed file and carries no TOTAL budget
# (938,483-1,147,342 chars, empty omission list — _FILE_SIZE_LIMIT bounds each
# FILE, nothing bounds the sum). Inlined, it made every advisory prompt
# ~970K-1.2M chars: it overran the inspection episode's transcript bound
# outright, and once that bound was widened the episode spent its whole round
# budget re-reading content the prompt already carried while
# role_requirements ordered exactly that re-read.
# ---------------------------------------------------------------------------

_PACK_SENTINEL = "TOUCHED-PACK-BODY-9K"


def test_retrieval_delivery_drops_the_touched_pack(tmp_path):
    """The retrieving form keeps the subject (diff + changed-file list) and the
    retrieval recipe, and drops the pack body."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    prompt = advisory._build_advisory_prompt(
        repo, "commit msg",
        prompt_context={
            "diff": "DIFF-SENTINEL",
            "changed_files": "M ouroboros/big_module.py",
            "touched_pack": _PACK_SENTINEL,
        },
        governance_by_retrieval=True,
        evidence_by_retrieval=True,
    )
    assert _PACK_SENTINEL not in prompt
    # The subject survives: the diff is inlined (bounded by _MAX_DIFF_CHARS_ERROR,
    # and vcs_diff defaults to the UNSTAGED side, so pointing at it could silently
    # review the wrong subject) and the changed paths are still listed.
    assert "DIFF-SENTINEL" in prompt
    assert "M ouroboros/big_module.py" in prompt
    # And the reviewer is told how to fetch what it needs.
    assert "retrieve them yourself" in prompt
    assert "staged=true" in prompt
    assert str(repo) in prompt


def test_legacy_delivery_still_inlines_the_touched_pack(tmp_path):
    """Control: without the flag the historical inlining form is unchanged."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    prompt = advisory._build_advisory_prompt(
        repo, "commit msg",
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M f.py",
                        "touched_pack": _PACK_SENTINEL},
        governance_by_retrieval=True,
    )
    assert _PACK_SENTINEL in prompt
    assert "retrieve them yourself" not in prompt


def test_retrieval_prompt_is_not_sized_by_the_changed_file(tmp_path):
    """The class pinned structurally: the retrieving prompt is byte-identical
    whatever the touched pack weighs, so no commit size can re-inflate it."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    sizes = []
    for pad in (200, 20_000):
        prompt = advisory._build_advisory_prompt(
            repo, "commit msg",
            prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M f.py",
                            "touched_pack": _PACK_SENTINEL * pad},
            governance_by_retrieval=True,
            evidence_by_retrieval=True,
        )
        sizes.append(len(prompt))
    assert sizes[0] == sizes[1]


def _dirty_repo(tmp_path):
    """A real git repo with one modified file: a marker on line 1 that the diff
    cannot reach (3 lines of context) and the change ~59 lines below it."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    body = ["UNCHANGED-BODY-MARKER-8Z"] + [f"line_{i} = {i}" for i in range(60)]
    (repo / "mod.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    body[-1] = "line_59 = 999"
    (repo / "mod.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    return repo


def test_native_route_dispatches_the_retrieval_form(tmp_path, monkeypatch, api_env):
    """Wiring: a repo-diff advisory run hands the native episode the retrieval
    form, not the ~1M-char pack. The flag is driven by include_repo_diff at the
    one production call site, so this is the assertion that keeps it wired."""
    _fake_window(monkeypatch, 1_000_000)
    captured = {}

    def _capture(prompt, repo_dir, ctx_, slot, model):
        captured["prompt"] = prompt
        return SimpleNamespace(
            success=True, result_text=_ADVISORY_ITEMS, session_id="",
            cost_usd=0.0, usage={}, error="", stderr_tail="",
        ), model

    monkeypatch.setattr(advisory, "_run_advisory_native", _capture)
    repo = _dirty_repo(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.repo_dir = repo
    items, raw, _model, chars = advisory._run_claude_advisory(repo, "msg", ctx)

    assert not raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert [i["item"] for i in items] == ["correctness"]
    prompt = captured["prompt"]
    # The unchanged body of the changed file is NOT carried.
    assert "UNCHANGED-BODY-MARKER-8Z" not in prompt
    # The change itself IS: the diff is the subject.
    assert "line_59 = 999" in prompt
    assert "retrieve them yourself" in prompt
    # And the prompt is a normal size, not a whole-file pack.
    assert chars == len(prompt)
    assert chars < 100_000, chars
