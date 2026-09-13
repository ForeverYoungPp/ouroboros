"""capinv-447 J5: knowledge_list is registered read-only (safety.py POLICY_SKIP)
and granted to children that may not write cognitive memory — yet on an index
miss it used to rebuild the index on disk: mkdir the knowledge dir, create a
never-unlinked index-full.md.lock and write index-full.md. For a project-scoped
child that mutated the live shared store. A list must write NOTHING."""
from __future__ import annotations

import pathlib

from ouroboros.tools.knowledge import INDEX_FILE, _knowledge_list


class _Ctx:
    def __init__(self, drive_root, project_id=""):
        self.drive_root = pathlib.Path(drive_root)
        self.project_id = project_id
        self.task_id = "t1"

    def drive_path(self, rel):
        return self.drive_root / rel


def _tree(root: pathlib.Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_list_without_index_writes_nothing_and_still_lists_topics(tmp_path):
    ctx = _Ctx(tmp_path / "drive")
    kdir = ctx.drive_root / "memory" / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "git-recipes.md").write_text("# Git recipes\n\nUse rebase sparingly.\n", encoding="utf-8")
    (kdir / "browser.md").write_text("# Browser\n\nHeadless needs a display shim.\n", encoding="utf-8")
    before = _tree(ctx.drive_root)

    listing = _knowledge_list(ctx)

    assert "git-recipes" in listing
    assert "browser" in listing
    assert "rebase" in listing  # summaries are rendered, not just names
    # A pure read: no index, no lock sidecar, no new files anywhere in the drive.
    assert _tree(ctx.drive_root) == before
    assert not (kdir / INDEX_FILE).exists()
    assert not (kdir / f"{INDEX_FILE}.lock").exists()


def test_list_with_no_knowledge_dir_creates_nothing(tmp_path):
    ctx = _Ctx(tmp_path / "drive")
    listing = _knowledge_list(ctx)
    assert "empty" in listing
    assert not (ctx.drive_root / "memory" / "knowledge").exists()


def test_list_prefers_existing_index_verbatim(tmp_path):
    """0-regression: when the write path has maintained an index, list returns it.

    Re-anchored for the scope note (the draft adds one): the INDEX CONTENT is still
    carried verbatim and the file is never rewritten on a read, while the result now
    also names what the list is — the local archive — so a topic that lives only in
    Engram cannot read as absent. Both facts are asserted; neither replaces the other.
    """
    ctx = _Ctx(tmp_path / "drive")
    kdir = ctx.drive_root / "memory" / "knowledge"
    kdir.mkdir(parents=True)
    index_body = "# Knowledge Base Index\n\n- **a**: alpha\n"
    (kdir / INDEX_FILE).write_text(index_body, encoding="utf-8")

    listing = _knowledge_list(ctx)

    assert index_body in listing, "the maintained index must be carried, not rebuilt"
    assert "ARCHIVE INDEX" in listing, "the list must name its scope"
    assert "Engram" in listing, "and the route to the remote records"
    assert (kdir / INDEX_FILE).read_text(encoding="utf-8") == index_body, "a read never rewrites it"


def test_first_write_into_indexless_store_seeds_the_full_index(tmp_path, monkeypatch):
    """#447 C1, retargeted to the store whose write path still authors an index.

    The guard is unchanged: a first write into a store that has topic files but no
    index must seed ALL of them, because a one-topic seed would hide every
    pre-existing topic from later listings. Only the PATH moved — S2 retired the
    canonical local write, so the per-project facts store is now the only local
    write path that maintains ``index-full.md``.
    """
    import ouroboros.config as cfg
    from ouroboros.project_facts import project_knowledge_dir
    from ouroboros.tools.knowledge import _knowledge_write

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data-root")
    ctx = _Ctx(tmp_path / "drive", project_id="proj_idx")
    kdir = project_knowledge_dir("proj_idx")
    kdir.mkdir(parents=True)
    (kdir / "alpha.md").write_text("# alpha\n\nSummary of alpha.\n", encoding="utf-8")
    (kdir / "beta.md").write_text("# beta\n\nSummary of beta.\n", encoding="utf-8")
    assert not (kdir / INDEX_FILE).exists()

    _knowledge_write(ctx, topic="gamma", content="# gamma\n\nSummary of gamma.\n")

    listing = _knowledge_list(ctx)
    for topic in ("alpha", "beta", "gamma"):
        assert topic in listing, (topic, listing)
    assert (kdir / INDEX_FILE).exists()


def test_canonical_write_no_longer_authors_a_local_index(tmp_path):
    """S2: the canonical write path stops maintaining ``index-full.md`` too."""
    from ouroboros.tools.knowledge import _knowledge_write

    ctx = _Ctx(tmp_path / "drive")
    _knowledge_write(ctx, topic="gamma", content="# gamma\n\nSummary of gamma.\n")

    kdir = ctx.drive_root / "memory" / "knowledge"
    assert not (kdir / INDEX_FILE).exists()
    assert not (kdir / "gamma.md").exists()
