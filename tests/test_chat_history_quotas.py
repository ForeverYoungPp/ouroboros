"""Regression tests for chat-history separate quotas (PR-D2, issue #8).

A progress/telemetry burst must never evict the user's real conversation. Subagent
lineage is kept on top of the progress quota so a flood can't drop a RECENT child's
lifecycle events — but only WITHIN the recent telemetry window, so a long-finished
swarm does not re-materialise as a stuck "Working" parent card on reload.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.history import make_chat_history_endpoint
from ouroboros.subagent_messages import SUBAGENT_MESSAGE_FIELDS


def _run_full(tmp_path, params):
    endpoint = make_chat_history_endpoint(tmp_path)
    resp = asyncio.run(endpoint(SimpleNamespace(query_params=params)))
    return json.loads(resp.body.decode("utf-8"))


def _run(tmp_path, params):
    return _run_full(tmp_path, params)["messages"]


def _lineage_row(ts, child, ev):
    return json.dumps({
        "ts": ts, "content": f"child {ev}", "task_id": child,
        "delegation_role": "subagent", "parent_task_id": "root", "subagent_event": ev,
    })


def test_progress_flood_does_not_evict_human_messages(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    chat_lines = [
        json.dumps({"ts": f"2026-06-05T00:00:0{i}Z",
                    "direction": "in" if i % 2 == 0 else "out", "text": f"human-{i}"})
        for i in range(5)
    ]
    (logs / "chat.jsonl").write_text("\n".join(chat_lines) + "\n", encoding="utf-8")
    prog_lines = [
        json.dumps({"ts": f"2026-06-05T01:00:{i:02d}Z", "content": f"telemetry-{i}", "task_id": "t1"})
        for i in range(50)
    ]
    (logs / "progress.jsonl").write_text("\n".join(prog_lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_human": "3", "n_progress": "2"})
    human = [m["text"] for m in msgs if not m.get("is_progress")]
    progress = [m for m in msgs if m.get("is_progress")]
    assert human == ["human-2", "human-3", "human-4"]   # not evicted by the flood
    assert len(progress) == 2                            # telemetry bounded by n_progress


def test_recent_lineage_survives_progress_flood(tmp_path):
    """A RECENT child's lifecycle events survive even a tiny progress quota."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = [
        json.dumps({"ts": f"2026-06-05T00:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root"})
        for i in range(50)
    ]
    # 3 subagent lifecycle events INSIDE the recent window (near the latest noise)
    for i, ev in zip((47, 48, 49), ("scheduled", "update", "completed")):
        lines.append(_lineage_row(f"2026-06-05T00:00:{i:02d}Z", "child1", ev))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert len(lineage) == 3
    assert {m.get("subagent_event") for m in lineage} == {"scheduled", "update", "completed"}


def test_old_lineage_does_not_resurrect_finished_swarm(tmp_path):
    """Lineage OLDER than the retained telemetry window is dropped, so a
    long-finished swarm does not re-materialise as a stuck parent card."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = []
    # OLD swarm lineage (4 days earlier)
    for i, ev in zip((1, 2, 3), ("scheduled", "running", "completed")):
        lines.append(_lineage_row(f"2026-06-01T00:00:0{i}Z", "oldchild", ev))
    # RECENT telemetry flood
    for i in range(50):
        lines.append(json.dumps({"ts": f"2026-06-05T02:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root"}))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert lineage == []  # old swarm does NOT resurface


def test_delegation_role_root_respects_progress_quota(tmp_path):
    """delegation_role='root' is NOT subagent lineage and must obey the quota."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = [
        json.dumps({"ts": f"2026-06-05T00:00:{i:02d}Z", "content": f"root-{i}",
                    "task_id": f"root-{i}", "delegation_role": "root"})
        for i in range(50)
    ]
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    progress = [m for m in _run(tmp_path, {"n_progress": "5"}) if m.get("is_progress")]
    assert len(progress) == 5  # 'root' did not bypass the quota


def test_default_quotas_keep_recent_history(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:00Z", "direction": "in", "text": "hello"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    msgs = _run(tmp_path, {})
    assert any(m.get("text") == "hello" and not m.get("is_progress") for m in msgs)


def test_history_annotates_terminal_from_effective_status(tmp_path, monkeypatch):
    """A SIGKILLed/panic'd task whose raw result is stuck "running" but whose
    EFFECTIVE status is failed (stale-orphan guard) must get task_terminal_status,
    so its card finalizes instead of replaying "Working" forever."""
    import ouroboros.task_status as ts_mod
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:00Z", "content": "step", "task_id": "stuck1"}) + "\n",
        encoding="utf-8",
    )
    # Simulate the orphan guard: effective status resolves to "failed" even though
    # the raw on-disk file is still "running".
    monkeypatch.setattr(
        ts_mod, "load_effective_task_result",
        lambda dr, tid, **kw: {"status": "failed"} if tid == "stuck1" else {},
    )
    msgs = _run(tmp_path, {})
    row = next(m for m in msgs if m.get("task_id") == "stuck1")
    assert row.get("task_terminal_status") == "failed"


def test_history_projects_terminal_review_truth_without_task_summary(tmp_path, monkeypatch):
    """The last retained progress row is the bounded replay anchor when the
    best-effort task summary is absent; earlier progress must not duplicate the
    panel details."""
    import ouroboros.task_status as ts_mod

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-06-05T00:00:00Z", "content": "step one", "task_id": "review1"}),
            json.dumps({"ts": "2026-06-05T00:00:01Z", "content": "step two", "task_id": "review1"}),
        ]) + "\n",
        encoding="utf-8",
    )
    projection = {"panels": [{"panel_id": "terminal-review", "aggregate_signal": "DEGRADED"}]}
    monkeypatch.setattr(
        ts_mod,
        "load_effective_task_result",
        lambda dr, tid, **kw: {
            "status": "completed",
            "reason_code": "acceptance_degraded",
            "outcome_axes": {
                "lifecycle": {"status": "completed"},
                "execution": {"status": "ok"},
                "objective": {"status": "best_effort"},
                "review": {"status": "degraded"},
                "artifacts": {"status": "ready"},
            },
            "review_projection": projection,
        } if tid == "review1" else {},
    )

    rows = [m for m in _run(tmp_path, {}) if m.get("task_id") == "review1"]
    assert len(rows) == 2
    assert all(row.get("task_terminal_status") == "completed" for row in rows)
    assert "review_projection" not in rows[0]
    assert rows[1]["outcome_axes"]["review"]["status"] == "degraded"
    assert rows[1]["reason_code"] == "acceptance_degraded"
    assert rows[1]["review_projection"] == projection


def test_history_projects_terminal_review_truth_only_on_existing_summary(tmp_path, monkeypatch):
    """A retained summary is the one review-truth anchor; progress keeps only
    terminal lifecycle so replay cannot feed the same panel through two rows."""
    import ouroboros.task_status as ts_mod

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-05T00:00:01Z",
            "direction": "system",
            "type": "task_summary",
            "task_id": "review2",
            "chat_id": 1,
            "text": "terminal summary",
            "tool_calls": 1,
            "rounds": 2,
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-06-05T00:00:00Z",
            "content": "last progress",
            "task_id": "review2",
        }) + "\n",
        encoding="utf-8",
    )
    projection = {
        "panels": [{"panel_id": "terminal-review", "aggregate_signal": "DEGRADED"}],
    }
    monkeypatch.setattr(
        ts_mod,
        "load_effective_task_result",
        lambda _dr, tid, **kw: {
            "status": "completed",
            "reason_code": "acceptance_degraded",
            "outcome_axes": {
                "objective": {"status": "best_effort"},
                "review": {"status": "degraded"},
            },
            "review_projection": projection,
        } if tid == "review2" else {},
    )

    rows = [row for row in _run(tmp_path, {}) if row.get("task_id") == "review2"]
    progress = next(row for row in rows if row.get("is_progress"))
    summary = next(row for row in rows if row.get("system_type") == "task_summary")
    assert progress["task_terminal_status"] == "completed"
    assert "review_projection" not in progress
    assert "reason_code" not in progress
    assert summary["review_projection"] == projection
    assert summary["reason_code"] == "acceptance_degraded"
    assert summary["outcome_axes"]["review"]["status"] == "degraded"


# --- perf2 P3: default window constants, truncation metadata, variant A ------


def test_default_request_does_not_open_archives_when_live_tail_sufficient(
    tmp_path, monkeypatch,
):
    """A DEFAULT (no explicit quotas) request whose live files already satisfy
    the module-constant window must never open rotated archive segments."""
    from ouroboros.gateway import _helpers

    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "chat_20260605T000000.jsonl").write_text(
        json.dumps({"ts": "2026-06-04T00:00:00Z", "direction": "in", "text": "archived human"}) + "\n",
        encoding="utf-8",
    )
    (archive / "progress_20260605T000000.jsonl").write_text(
        json.dumps({"ts": "2026-06-04T00:00:00Z", "content": "archived step", "task_id": "t-old"}) + "\n",
        encoding="utf-8",
    )
    # Live tails exceed the default quotas (150 human / 60 progress).
    (logs / "chat.jsonl").write_text(
        "\n".join(
            json.dumps({
                "ts": f"2026-06-05T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}Z",
                "direction": "in" if i % 2 else "out", "text": f"human-{i}",
            })
            for i in range(160)
        ) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(
            json.dumps({
                "ts": f"2026-06-05T01:{i // 60:02d}:{i % 60:02d}Z",
                "content": f"telemetry-{i}", "task_id": "t1",
            })
            for i in range(70)
        ) + "\n",
        encoding="utf-8",
    )

    seen = []
    real_iter = _helpers.iter_jsonl_objects

    def counted(path, *args, **kwargs):
        seen.append(str(path))
        return real_iter(path, *args, **kwargs)

    monkeypatch.setattr(_helpers, "iter_jsonl_objects", counted)

    msgs = _run(tmp_path, {})
    assert seen  # the bounded reader served the endpoint
    assert not any("/archive/" in path for path in seen)  # archives untouched
    human = [m for m in msgs if not m.get("is_progress")]
    progress = [m for m in msgs if m.get("is_progress")]
    assert len(human) == 150   # default n_human window
    assert len(progress) == 60  # default n_progress window
    texts = [m["text"] for m in msgs]
    assert "archived human" not in texts
    assert "archived step" not in texts


def test_window_metadata_complete_when_everything_fits(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:00Z", "direction": "in", "text": "hello"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:01Z", "content": "step", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )
    window = _run_full(tmp_path, {})["window"]
    assert window == {"complete": True, "truncated_by": []}


def test_window_metadata_reports_quota_truncation(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-06-05T00:00:{i:02d}Z",
                        "direction": "in" if i % 2 else "out", "text": f"human-{i}"})
            for i in range(10)
        ) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    payload = _run_full(tmp_path, {"n_human": "3"})
    assert payload["window"] == {"complete": False, "truncated_by": ["quota"]}
    assert len(payload["messages"]) == 3  # the slice actually applied


def test_window_metadata_reports_quota_when_slice_cuts_only_system_rows(tmp_path):
    """MAJOR review fix: direction:"system" rows (per-task task_summary) do not
    count toward the reader's in/out quota, but the n_human tail slice still
    drops them — the window must honestly report "quota", never complete."""
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [
        json.dumps({
            "ts": f"2026-06-05T00:00:0{i}Z",
            "direction": "system",
            "type": "task_summary",
            "task_id": f"t{i}",
            "text": f"Task t{i} finished.",
        })
        for i in range(4)
    ]
    # One newest human row; n_human=3 keeps it + the 2 newest system rows and
    # drops ONLY the 2 oldest system rows (in/out quota count stays below 3).
    rows.append(json.dumps({
        "ts": "2026-06-05T00:01:00Z", "direction": "in", "text": "hello",
    }))
    (logs / "chat.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    payload = _run_full(tmp_path, {"n_human": "3"})
    assert payload["window"] == {"complete": False, "truncated_by": ["quota"]}
    human = [m for m in payload["messages"] if not m.get("is_progress")]
    assert len(human) == 3  # the slice really dropped the two oldest system rows


def test_window_metadata_reports_archive_floor(tmp_path):
    """More rotated segments exist than the 3-archive backfill bound and the
    quota is still unmet -> the reader stopped at the archive floor."""
    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    for idx in range(4):  # 4 archives > the 3-newest backfill bound
        (archive / f"progress_2026060{idx + 1}T000000.jsonl").write_text(
            json.dumps({
                "ts": f"2026-06-0{idx + 1}T00:00:00Z",
                "content": f"archived-{idx + 1}", "task_id": "t1",
            }) + "\n",
            encoding="utf-8",
        )
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T01:00:00Z", "content": "live step", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )
    (logs / "chat.jsonl").write_text("", encoding="utf-8")

    payload = _run_full(tmp_path, {})
    assert payload["window"] == {"complete": False, "truncated_by": ["archive_floor"]}
    texts = [m["text"] for m in payload["messages"]]
    # The 3 newest archives were backfilled; the oldest stayed beyond the floor.
    assert "archived-2" in texts and "archived-4" in texts and "live step" in texts
    assert "archived-1" not in texts


def test_window_metadata_reports_lineage_cap(tmp_path):
    """A swarm fan-out larger than the 300-row lineage cap keeps only the
    newest lineage rows and discloses the cap in the window metadata."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        "\n".join(
            _lineage_row(f"2026-06-05T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}Z",
                         "bigswarm", "update")
            for i in range(310)
        ) + "\n",
        encoding="utf-8",
    )
    payload = _run_full(tmp_path, {})
    assert payload["window"]["complete"] is False
    assert payload["window"]["truncated_by"] == ["lineage_cap"]
    lineage = [m for m in payload["messages"] if m.get("delegation_role") == "subagent"]
    assert len(lineage) == 300  # the cap kept only the newest rows


def test_active_silent_child_lineage_survives_recency_floor(tmp_path, monkeypatch):
    """perf2 P3 variant A (owner decision): a QUIET but still-ACTIVE child whose
    lineage rows are all OLDER than the recency floor keeps them — the child's
    card must be reproducible on reload during a live swarm. Each task_results
    read happens ONCE per request (the pre-floor map feeds the annotation)."""
    import ouroboros.task_status as ts_mod

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = []
    # The quiet child's lifecycle events, all far older than the floor below.
    for i, ev in zip((1, 2, 3), ("scheduled", "running", "update")):
        lines.append(_lineage_row(f"2026-06-05T00:00:0{i}Z", "quietchild", ev))
    # A recent telemetry flood establishes a much newer recency floor.
    for i in range(50):
        lines.append(json.dumps({
            "ts": f"2026-06-05T02:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root",
        }))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    calls: dict = {}

    def fake_load(_dr, tid, **_kw):
        calls[tid] = calls.get(tid, 0) + 1
        return {"status": "running"} if tid == "quietchild" else {}

    monkeypatch.setattr(ts_mod, "load_effective_task_result", fake_load)

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert len(lineage) == 3
    assert all(m.get("task_id") == "quietchild" for m in lineage)
    assert {m.get("subagent_event") for m in lineage} == {"scheduled", "running", "update"}
    # Still active: no terminal annotation may finalize the replayed card.
    assert all("task_terminal_status" not in m for m in lineage)
    # task_results were read once per task for the whole request.
    assert calls.get("quietchild") == 1
    assert calls.get("root") == 1


def test_terminal_silent_child_older_than_floor_stays_dropped(tmp_path):
    """Anti-zombie preserved: a child with a TERMINAL task result whose lineage
    rows are older than the recency floor is still dropped entirely."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = []
    for i, ev in zip((1, 2, 3), ("scheduled", "running", "completed")):
        lines.append(_lineage_row(f"2026-06-05T00:00:0{i}Z", "donechild", ev))
    for i in range(50):
        lines.append(json.dumps({
            "ts": f"2026-06-05T02:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root",
        }))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "donechild.json").write_text(
        json.dumps({"task_id": "donechild", "status": "completed"}), encoding="utf-8",
    )

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert lineage == []  # terminal quiet child does NOT resurface


# --- Stream S: closed lineage window on chat FINALS (floor-symmetric strip) ---


def _child_final_row(ts, task_id, *, lineage=True):
    row = {
        "ts": ts, "direction": "out", "chat_id": 1,
        "text": "child answer", "task_id": task_id,
    }
    if lineage:
        row.update({
            "delegation_role": "subagent", "parent_task_id": "root",
            "root_task_id": "root", "subagent_task_id": task_id,
        })
    return json.dumps(row)


def _fresh_noise_lines(count=5):
    return [
        json.dumps({"ts": f"2026-06-05T02:00:{i:02d}Z", "content": f"noise-{i}",
                    "task_id": "other"})
        for i in range(count)
    ]


def test_stale_child_final_loses_raw_lineage_fields(tmp_path):
    """NEG-1: a child FINAL chat row (raw lineage fields) older than a fresh
    progress floor, with the child settled, loses every subagent lineage field
    on the emitted payload — so the client cannot re-mint an orphaned
    "Working" parent card whose own rows aged out of the window."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        _child_final_row("2026-06-01T00:00:00Z", "child") + "\n", encoding="utf-8",
    )
    # Fresh non-lineage telemetry NEWER than the child final -> fresh floor.
    (logs / "progress.jsonl").write_text(
        "\n".join(_fresh_noise_lines()) + "\n", encoding="utf-8",
    )
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "root.json").write_text(
        json.dumps({"task_id": "root", "status": "completed"}), encoding="utf-8",
    )
    (results / "child.json").write_text(
        json.dumps({"task_id": "child", "status": "completed"}), encoding="utf-8",
    )

    row = next(m for m in _run(tmp_path, {}) if m.get("task_id") == "child")
    assert row["text"] == "child answer"  # the row itself survives, de-roled
    assert not any(field in row for field in SUBAGENT_MESSAGE_FIELDS)


def test_stale_legacy_child_final_skips_task_result_revival(tmp_path):
    """NEG-2: a legacy child FINAL (no raw lineage fields in chat.jsonl) older
    than a fresh floor must NOT get lineage re-injected from task_results."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        _child_final_row("2026-06-01T00:00:00Z", "child-legacy", lineage=False) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(_fresh_noise_lines()) + "\n", encoding="utf-8",
    )
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "root.json").write_text(
        json.dumps({"task_id": "root", "status": "completed"}), encoding="utf-8",
    )
    (results / "child-legacy.json").write_text(json.dumps({
        "task_id": "child-legacy", "status": "completed",
        "delegation_role": "subagent", "parent_task_id": "root",
        "root_task_id": "root", "role": "reviewer",
    }), encoding="utf-8")

    row = next(m for m in _run(tmp_path, {}) if m.get("task_id") == "child-legacy")
    assert not any(field in row for field in SUBAGENT_MESSAGE_FIELDS)


def test_stale_child_final_keeps_lineage_while_child_active(tmp_path, monkeypatch):
    """POS-2: a child final older than the floor keeps its lineage when the
    child is still ACTIVE (non-terminal effective status + in-window lineage
    progress row makes it active_children-eligible)."""
    import ouroboros.task_status as ts_mod

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        _child_final_row("2026-06-01T00:00:00Z", "child1") + "\n", encoding="utf-8",
    )
    lines = _fresh_noise_lines()
    lines.append(_lineage_row("2026-06-05T02:00:06Z", "child1", "update"))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        ts_mod, "load_effective_task_result",
        lambda _dr, tid, **_kw: {"status": "running"} if tid == "child1" else {},
    )

    finals = [m for m in _run(tmp_path, {"n_progress": "5"})
              if m.get("task_id") == "child1" and not m.get("is_progress")]
    assert finals and finals[0]["delegation_role"] == "subagent"
    assert finals[0]["parent_task_id"] == "root"


def test_recent_child_final_keeps_lineage_fields(tmp_path):
    """POS-3: a child final with ts >= floor keeps its lineage fields — fresh
    swarms keep the nested-card UX."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        _child_final_row("2026-06-05T03:00:00Z", "child2") + "\n", encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(_fresh_noise_lines()) + "\n", encoding="utf-8",
    )
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "child2.json").write_text(
        json.dumps({"task_id": "child2", "status": "completed"}), encoding="utf-8",
    )

    row = next(m for m in _run(tmp_path, {}) if m.get("task_id") == "child2")
    assert row["delegation_role"] == "subagent"
    assert row["parent_task_id"] == "root"
