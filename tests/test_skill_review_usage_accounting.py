"""Exact physical-attempt accounting for one Skill Review wave."""

from __future__ import annotations

import json

import pytest

from ouroboros import usage_accounting as ua


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    (root / "state").mkdir(parents=True)
    return root


def _request(data_root, **overrides):
    values = {
        "model": "openai/gpt-5.2", "provider": "openai", "reservation_usd": 1.0,
        "drive_root": data_root, "task_id": "child", "root_task_id": "root",
        "source": "test",
    }
    values.update(overrides)
    return ua.AttemptRequest(**values)


def _ledger(data_root):
    path = data_root / ua.LEDGER_REL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_review_attribution_survives_api_transitions_and_subscription_identity(data_root):
    scope = ua.UsageScope(
        drive_root=data_root, task_id="skill-review", root_task_id="root-review",
        category="skill_review_review", source="review_substrate",
        review_skill="happy_farm", review_wave_id="wave-7", review_slot_id="skill-triad-1",
    )
    with ua.usage_scope(scope):
        reservation = ua.reserve_attempt(_request(data_root, task_id="", root_task_id=""))
        ua.mark_dispatched(reservation)
        ua.settle_attempt(
            reservation, {"prompt_tokens": 11, "completion_tokens": 3},
            cost_usd=0.125, cost_final=True,
        )
        session_id = ua.record_subscription_session(
            "session-wave-7", drive_root=data_root, route="claude", model="claude-fable-5",
            prompt_tokens=20, completion_tokens=5, spend_usd=0.0,
            category="skill_review_review", source="review_substrate",
        )

    rows = _ledger(data_root)
    attributed = [row for row in rows if row.get("attempt_id") == reservation.attempt_id]
    assert [row["state"] for row in attributed] == ["reserved", "dispatched", "settled"]
    assert all(
        (row["review_skill"], row["review_wave_id"], row["review_slot_id"])
        == ("happy_farm", "wave-7", "skill-triad-1")
        for row in attributed
    )
    session = next(row for row in rows if row.get("attempt_id") == session_id)
    assert (session["category"], session["source"]) == (
        "skill_review_review", "review_substrate",
    )
    assert session["review_skill"] == "happy_farm"
    assert session["review_wave_id"] == "wave-7"
    assert session["review_slot_id"] == "skill-triad-1"

    # One physical session cannot be silently rebound to a different or legacy wave.
    with pytest.raises(ua.UsageAccountingError, match="conflicting settled-row identity"):
        ua.record_subscription_session(
            "session-wave-7", drive_root=data_root, route="claude", model="claude-fable-5",
            prompt_tokens=20, completion_tokens=5, spend_usd=0.0,
        )


def test_legacy_subscription_replay_treats_missing_review_attribution_as_empty(data_root):
    session_id = ua.record_subscription_session(
        "session-before-attribution", drive_root=data_root, route="claude",
        model="claude-fable-5", task_id="task-old", root_task_id="root-old",
        prompt_tokens=20, completion_tokens=5, spend_usd=0.0,
    )
    path = data_root / ua.LEDGER_REL
    rows = _ledger(data_root)
    for key in ("review_skill", "review_wave_id", "review_slot_id"):
        rows[-1].pop(key, None)
    path.write_text("".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ))

    assert ua.record_subscription_session(
        "session-before-attribution", drive_root=data_root, route="claude",
        model="claude-fable-5", task_id="task-old", root_task_id="root-old",
        prompt_tokens=20, completion_tokens=5, spend_usd=0.0,
    ) == session_id
    with pytest.raises(ua.UsageAccountingError, match="conflicting settled-row identity"):
        ua.record_subscription_session(
            "session-before-attribution", drive_root=data_root, route="claude",
            model="claude-fable-5", task_id="task-old", root_task_id="root-old",
            prompt_tokens=20, completion_tokens=5, spend_usd=0.0,
            review_skill="happy_farm", review_wave_id="wave-new",
            review_slot_id="skill-triad-1",
        )


def test_skill_review_usage_projects_only_exact_wave_and_keeps_slots_and_unknowns(
    data_root, monkeypatch,
):
    def scope(slot: str, wave: str = "wave-9") -> ua.UsageScope:
        return ua.UsageScope(
            drive_root=data_root, task_id="skill-review", root_task_id="root-review",
            category="skill_review_review", source="review_substrate",
            review_skill="happy_farm", review_wave_id=wave, review_slot_id=slot,
        )

    with ua.usage_scope(scope("slot-api")):
        settled = ua.reserve_attempt(_request(data_root, task_id="", root_task_id=""))
        ua.mark_dispatched(settled)
        ua.settle_attempt(
            settled, {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 7},
            cost_usd=0.5, cost_final=True,
        )
    with ua.usage_scope(scope("slot-open")):
        unresolved = ua.reserve_attempt(_request(
            data_root, task_id="", root_task_id="", reservation_usd=0.75,
        ))
        ua.mark_dispatched(unresolved)
        ua.mark_unresolved(unresolved, "provider outcome unknown")
    with ua.usage_scope(scope("slot-unknown")):
        monkeypatch.setattr(ua, "_reservation_cost", lambda _request: None)
        unknown = ua.reserve_attempt(_request(
            data_root, task_id="", root_task_id="", reservation_usd=None,
            force_unknown_reservation=True,
        ))
        ua.mark_dispatched(unknown)
        ua.mark_unresolved(unknown, "pricing and outcome unknown")
    with ua.usage_scope(scope("slot-session")):
        session_id = ua.record_subscription_session(
            "session-wave-9", drive_root=data_root, route="cursor",
            model="cursor-grok-4.6-high", prompt_tokens=50, completion_tokens=10,
            cached_tokens=None, reset_at="2026-08-25T00:00:00Z", spend_usd=0.0,
            credential_profile_id="cursor-profile", access_profile="readonly",
        )
    with ua.usage_scope(scope("foreign", wave="wave-other")):
        foreign = ua.record_subscription_session(
            "foreign-session", drive_root=data_root, route="codex", spend_usd=0.0,
        )

    projection = ua.skill_review_usage(
        data_root, review_skill="happy_farm", review_wave_id="wave-9",
    )
    assert projection["attempt_ids"] == sorted([
        settled.attempt_id, unresolved.attempt_id, unknown.attempt_id, session_id,
    ])
    assert foreign not in projection["attempt_ids"]
    assert set(projection["by_slot"]) == {
        "slot-api", "slot-open", "slot-session", "slot-unknown",
    }
    assert projection["physical_calls"] == 3
    assert projection["subscription_sessions"] == 1
    assert projection["confirmed_usd"] == 0.5
    assert projection["unresolved_upper_bound_usd"] == 0.75
    assert projection["unknown_unmetered"] == 1
    assert projection["non_final_rows"] == 2
    assert projection["prompt_tokens"] == 150
    assert projection["completion_tokens"] == 30
    assert projection["cached_tokens"] == 7
    assert projection["subscription_windows"] == {
        "cursor": "2026-08-25T00:00:00Z",
    }
    assert projection["attribution_complete"] is True
    assert projection["integrity_degraded"] is False
    empty = ua.skill_review_usage(
        data_root, review_skill="happy_farm", review_wave_id="wave-with-no-attempts",
    )
    assert empty["attempt_ids"] == [] and empty["attribution_complete"] is False


def test_usage_markdown_never_claims_final_cash_from_degraded_or_open_rows():
    from ouroboros.skill_review_usage import skill_review_usage_markdown

    base = {
        "attempt_ids": ["a1"],
        "attempts": [{
            "attempt_id": "a1", "review_slot_id": "slot-1", "kind": "attempt",
            "state": "settled", "prompt_tokens": 10, "completion_tokens": None,
            "cached_tokens": None,
        }],
        "settled_usd": 0.25, "confirmed_usd": 0.25, "estimated_usd": 0.0,
        "unresolved_upper_bound_usd": 0.0, "physical_calls": 1,
        "subscription_sessions": 0, "prompt_tokens": 10, "completion_tokens": None,
        "cached_tokens": None, "unknown_unmetered": 0, "non_final_rows": 0,
        "attribution_complete": True, "by_slot": {},
    }
    degraded = skill_review_usage_markdown(
        {**base, "cost_final": False, "integrity_degraded": True},
        coverage_known=True, expected=1, recorded=1,
    )
    assert "Recorded-row cash: settled $0.250000" in degraded
    assert "Wave attempt coverage: unverified (ledger integrity degraded; 1/1 visible)" in degraded
    assert "whole-wave cash and finality are unavailable" in degraded
    assert "Reported tokens: prompt=10; completion=unknown; cached=unknown" in degraded
    assert "completion=1/1 unreported; cached=1/1 unreported" in degraded

    open_row = skill_review_usage_markdown(
        {**base, "cost_final": False, "integrity_degraded": False, "non_final_rows": 1},
        coverage_known=True, expected=1, recorded=1,
    )
    assert "Wave attempt coverage: complete (1/1 recorded)" in open_row
    assert "Recorded-row cash" in open_row and "whole-wave cash and finality are unavailable" in open_row
