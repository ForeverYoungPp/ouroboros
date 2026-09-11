"""S2 — the read-through cache: content sections survive an unreachable store.

The sections that stand in for local files must degrade to "last known, possibly
behind", never to a blank that reads as "there is no history". These tests drive
the real HTTP path against the shared fake service: a fresh hit must make no
request at all, and an outage must serve what was read before.
"""

from __future__ import annotations

from ouroboros.context import _engram_recall_section, _engram_verdict_section
from ouroboros.engram_cache import cached_read, reset_cache
from ouroboros.engram_read import client_for, type_digest
from ouroboros.engram_sink import reset_sinks
from ouroboros.memory import Memory
from tests.test_engram_instruments import stub  # noqa: F401  (shared HTTP stub)


def _age_the_cache(drive_root) -> None:
    """Make every mirror file look old, i.e. simulate time passing between reads."""
    import json
    import pathlib
    import time

    for path in (pathlib.Path(drive_root) / "state" / "engram_cache").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["fetched_at"] = time.time() - 3_600
        path.write_text(json.dumps(payload), encoding="utf-8")


def _verdicts() -> list[dict]:
    return [{
        "id": 1, "type": "review_verdict", "title": "Review verdict: blocked",
        "topic_key": "review_verdict:t1:1", "content": "verdict: blocked",
    }]


def _count(state, path):
    return len([r for r in state.requests if r["path"] == path])


def test_a_fresh_hit_makes_no_second_request(stub):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)
    fetch = lambda: type_digest(client, "review_verdict")  # noqa: E731

    first = cached_read("verdict", client, fetch, drive_root=env.drive_root)
    asks = _count(state, "/observations/recent")
    second = cached_read("verdict", client, fetch, drive_root=env.drive_root)

    assert first.status == second.status == "ok"
    assert _count(state, "/observations/recent") == asks, "a fresh cache hit still hit the store"


def test_an_expired_entry_refetches(stub):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)
    fetch = lambda: type_digest(client, "review_verdict")  # noqa: E731

    cached_read("verdict", client, fetch, drive_root=env.drive_root, ttl_seconds=0)
    asks = _count(state, "/observations/recent")
    cached_read("verdict", client, fetch, drive_root=env.drive_root, ttl_seconds=0)

    assert _count(state, "/observations/recent") > asks


def test_an_outage_serves_the_last_good_read_as_stale(stub):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)
    fetch = lambda: type_digest(client, "review_verdict")  # noqa: E731

    assert cached_read("verdict", client, fetch, drive_root=env.drive_root).status == "ok"
    state.fail = True
    read = cached_read("verdict", client, fetch, drive_root=env.drive_root, ttl_seconds=0)

    assert read.status == "stale"
    assert read.readable and not read.unknown
    assert "blocked" in read.text


def test_an_outage_without_a_cache_is_still_typed_unavailable(stub):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.fail = True
    client = client_for(env)

    read = cached_read(
        "verdict", client, lambda: type_digest(client, "review_verdict"),
        drive_root=env.drive_root,
    )

    assert read.status in {"unavailable", "rejected"}
    assert read.unknown and not read.readable


def test_the_disk_mirror_survives_an_empty_process_cache(stub):  # noqa: F811
    """A restart must not lose the cold copy: the whole point is that the last
    good read outlives the process that fetched it."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)
    fetch = lambda: type_digest(client, "review_verdict")  # noqa: E731
    cached_read("verdict", client, fetch, drive_root=env.drive_root)

    reset_cache()  # memory gone, disk mirror intact
    state.fail = True
    read = cached_read(
        "verdict", client, fetch, drive_root=env.drive_root, ttl_seconds=0,
    )

    assert read.status == "stale"
    assert "blocked" in read.text


def test_the_recall_seam_discloses_a_stale_cache(stub, monkeypatch):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "Dialogue summary: span",
        "topic_key": "dialogue:summary:0-10", "content": "we shipped the cache",
    }]
    memory = Memory(env.drive_root, env.repo_dir)

    first = _engram_recall_section(memory, "cache")
    assert "Dialogue summary: span" in first
    assert "UNKNOWN" not in first

    # A query-keyed recall entry lives IN-PROCESS only: its key is the owner's
    # message, which a restart can never re-ask, so a mirror file would be write-
    # only churn. Age the process clock instead of a file.
    import ouroboros.engram_cache as cache_mod

    real_time = cache_mod.time.time
    monkeypatch.setattr(cache_mod.time, "time", lambda: real_time() + 3_600)
    state.fail = True
    stale = _engram_recall_section(memory, "cache")

    assert "may be BEHIND" in stale
    # Layer 1 only: the cached section carries titles, never bodies (the `engram`
    # tool is what reads a body) — so the title is the observable content here.
    assert "Dialogue summary: span" in stale


def test_a_query_keyed_read_leaves_no_mirror_behind(stub):  # noqa: F811
    """One file per distinct question is write-only churn; the stable keys are
    what a restart can actually serve."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)

    cached_read("verdict", client, lambda: type_digest(client, "review_verdict"),
                drive_root=env.drive_root, key="a question")
    assert not list((env.drive_root / "state" / "engram_cache").glob("*.json"))

    cached_read("verdict", client, lambda: type_digest(client, "review_verdict"),
                drive_root=env.drive_root)
    assert list((env.drive_root / "state" / "engram_cache").glob("*.json"))


def test_the_in_process_cache_stays_bounded(stub, monkeypatch):  # noqa: F811
    """The recall key is the owner's message, so without the bound the process
    would hold one entry per distinct question for its whole life. `_CACHE` is the
    observable the bound exists for: the size stays at or below
    `MAX_CACHE_ENTRIES`, and eviction drops the OLDEST while keeping the newest.
    The clock is driven so "oldest" is deterministic instead of a microsecond tie.
    """
    import ouroboros.engram_cache as cache_mod
    from ouroboros.engram_read import MachineRead

    state, env = stub
    reset_cache()
    client = client_for(env)

    clock = [1_000.0]

    def _now() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(cache_mod.time, "time", _now)

    keys = [f"question {i}" for i in range(cache_mod.MAX_CACHE_ENTRIES + 8)]
    for key in keys:
        read = cached_read(
            "recall",
            client,
            lambda: MachineRead(True, status="ok", text="- [1] (dialogue_summary) title"),
            drive_root=env.drive_root,
            key=key,
        )
        assert read.status == "ok", key

    assert len(cache_mod._CACHE) <= cache_mod.MAX_CACHE_ENTRIES
    assert len(cache_mod._CACHE) == cache_mod.MAX_CACHE_ENTRIES  # eviction ran
    scope = cache_mod._scope_of(client, env.drive_root)
    assert cache_mod._cache_key("recall", scope, keys[-1]) in cache_mod._CACHE
    assert cache_mod._cache_key("recall", scope, keys[0]) not in cache_mod._CACHE


def test_the_disk_mirror_is_pruned_to_its_bound(tmp_path):
    """One file per distinct message is exactly the growth `MAX_CACHE_FILES`
    exists for, and `_prune` runs after every persist. mtimes are written
    explicitly, so "newest" is deterministic and the test needs no sleeps."""
    import os

    import ouroboros.engram_cache as cache_mod

    directory = tmp_path / "state" / "engram_cache"
    directory.mkdir(parents=True)
    total = cache_mod.MAX_CACHE_FILES + 5
    for index in range(total):
        path = directory / f"entry_{index:03d}.json"
        path.write_text("{}", encoding="utf-8")
        stamp = 1_000_000 + index          # entry_000 is the OLDEST
        os.utime(path, (stamp, stamp))

    cache_mod._prune(directory)

    remaining = sorted(path.name for path in directory.glob("*.json"))
    assert len(remaining) == cache_mod.MAX_CACHE_FILES
    assert f"entry_{total - 1:03d}.json" in remaining      # newest kept
    assert "entry_000.json" not in remaining               # oldest removed


def test_an_outage_is_not_re_probed_on_every_read(stub):  # noqa: F811
    """Serving stale is not enough: without a failure backoff every read pays the
    full transport timeout again to learn the same thing."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()
    client = client_for(env)
    fetch = lambda: type_digest(client, "review_verdict")  # noqa: E731

    assert cached_read("verdict", client, fetch, drive_root=env.drive_root).status == "ok"
    state.fail = True
    import ouroboros.engram_cache as cache_mod

    real_time = cache_mod.time.time
    # Age past the TTL so the first failing read really does probe...
    import unittest.mock as _mock
    with _mock.patch.object(cache_mod.time, "time", lambda: real_time() + 3_600):
        assert cached_read("verdict", client, fetch, drive_root=env.drive_root).status == "stale"
        probes = _count(state, "/observations/recent")
        again = cached_read("verdict", client, fetch, drive_root=env.drive_root)
        assert again.status == "stale"
        assert _count(state, "/observations/recent") == probes, (
            "a failed refresh was re-attempted on the very next read"
        )


def test_the_verdict_section_discloses_a_stale_cache(stub):  # noqa: F811
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = _verdicts()

    assert "blocked" in _engram_verdict_section(env)
    _age_the_cache(env.drive_root)
    reset_cache()
    state.fail = True
    stale = _engram_verdict_section(env)

    assert "cached from the last successful read" in stale
    assert "blocked" in stale


def test_two_questions_do_not_share_one_cached_answer(stub):  # noqa: F811
    """The key is part of the cache contract: serving question B the titles cached
    for question A would render another question's memories as relevant."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = [
        {"id": 1, "type": "dialogue_summary", "title": "Alpha decision",
         "topic_key": "dialogue:summary:0-10", "content": "we chose alpha"},
        {"id": 2, "type": "dialogue_summary", "title": "Beta migration",
         "topic_key": "dialogue:summary:10-20", "content": "we migrated beta"},
    ]
    memory = Memory(env.drive_root, env.repo_dir)

    alpha = _engram_recall_section(memory, "alpha")
    beta = _engram_recall_section(memory, "beta")

    assert "Alpha decision" in alpha
    assert "Beta migration" in beta
    assert "Alpha decision" not in beta


def test_the_recency_fallback_asks_only_for_what_it_renders(stub):  # noqa: F811
    """The fallback exists to fill five title lines. Pulling the module's whole
    50-record window (each with its body) to do it is ten times the traffic."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = [
        {"id": i, "type": "dialogue_summary", "title": f"span {i}",
         "topic_key": f"dialogue:summary:{i}", "content": f"body {i}"}
        for i in range(1, 40)
    ]
    memory = Memory(env.drive_root, env.repo_dir)
    from ouroboros.context import MAX_RECALL_ITEMS

    # No query ⇒ the recency path; matching query ⇒ the search path.
    _engram_recall_section(memory, "")
    recent_asks = [r for r in state.requests if r["path"] == "/observations/recent"]
    assert recent_asks, "the recency fallback never ran"
    assert int(recent_asks[-1]["params"]["limit"]) == MAX_RECALL_ITEMS


def test_the_recall_search_asks_for_or_matching(stub):  # noqa: F811
    """Engram's /search default is FTS5 AND: a natural-language question would
    only match a block containing EVERY one of its tokens, so multi-word recall
    always came back empty and the seam silently used its recency branch.

    The wire is only half the claim: ``match_mode=any`` can be sent while the
    caller throws the hits away, so the RENDERED section must carry the matched
    title too. Five newer blocks saturate the bounded continuity half, which
    leaves the search result as the only way it can reach the prompt.
    """
    state, env = stub
    reset_cache()
    reset_sinks()
    query = "what did we decide about the zstd frames"
    state.observations = [
        {"id": i, "type": "dialogue_summary", "title": f"span {i}",
         "topic_key": f"dialogue:summary:{i}", "content": f"body {i}"}
        for i in range(1, 12)
    ] + [{
        "id": 99, "type": "dialogue_summary", "title": "zstd frames",
        "topic_key": "dialogue:summary:0-10", "content": f"the owner asked: {query}",
    }]
    memory = Memory(env.drive_root, env.repo_dir)

    section = _engram_recall_section(memory, query)

    asks = [r for r in state.requests if r["path"] == "/search"]
    assert asks, "the recall seam never searched"
    assert asks[-1]["params"].get("match_mode") == "any"
    assert "zstd frames" in section, "the search hit never reached the prompt"


def test_a_single_token_query_does_not_ask_for_any_matching(stub):  # noqa: F811
    """For ONE token, any-mode and the default build the same expression (Engram
    pins that in TestSearchMatchMode_SingleToken: AND and OR over a single term are
    identical), so asking for it buys nothing — and it opens a path the default
    mode does not have (see the quote-only test below)."""
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "Dialogue summary: cache span",
        "topic_key": "dialogue:summary:0-10", "content": "we shipped the cache",
    }]
    memory = Memory(env.drive_root, env.repo_dir)

    _engram_recall_section(memory, "cache")

    asks = [r for r in state.requests if r["path"] == "/search"]
    assert asks, "the recall seam never searched"
    assert asks[-1]["params"].get("match_mode") is None


def test_a_query_with_no_usable_word_never_searches_and_still_shows_recency(stub):  # noqa: F811
    """A quote/punctuation-only query has no keyword to retrieve on, and Engram
    cannot represent it in the retrieval path: the default mode quotes the WHOLE
    query (so `'" "'` is a multi-phrase expression whose production parsing is
    unverified), while any-mode drops the empty fields and the engine rejects the
    result (SQL error -> HTTP 500 -> typed `unavailable`, a healthy store reported
    as unreachable). Neither path is worth depending on, so no request is sent at
    all — and the assertion is the OUTGOING request, the one thing a stubbed
    server cannot fake. The rendered continuity branch is the user-visible half.
    """
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "Dialogue summary: span",
        "topic_key": "dialogue:summary:0-10", "content": "the newest span",
    }]
    memory = Memory(env.drive_root, env.repo_dir)

    for query in ('"', '""', '" "'):
        before = _count(state, "/search")
        section = _engram_recall_section(memory, query)
        assert _count(state, "/search") == before, query  # no search was issued
        assert "Dialogue summary: span" in section, (query, section)
        assert "UNKNOWN" not in section, query
        assert "could not be reached" not in section, query

    # ...and this is a guard against unrepresentable queries, not a retreat from
    # OR recall: a real multi-word query still asks for it.
    _engram_recall_section(memory, "which span did we ship")
    asks = [r for r in state.requests if r["path"] == "/search"]
    assert asks[-1]["params"].get("match_mode") == "any"


def test_an_empty_read_is_not_cached_so_a_later_summary_appears_at_once(stub):  # noqa: F811
    """`readable` includes `empty`, so a blank answer used to be written (and, for
    a stable key, mirrored to disk) for the whole TTL: a dialogue summary written
    moments later stayed invisible for 60s. Both halves of that claim are pinned
    here — the store is asked AGAIN inside the TTL (the mechanism), and the new
    title reaches the reader immediately (the symptom) — because either alone
    would still pass if some other path kept serving the blank.

    Serving the last good read across an outage is a different path, untouched.
    """
    state, env = stub
    reset_cache()
    reset_sinks()
    state.observations = []
    memory = Memory(env.drive_root, env.repo_dir)

    assert _engram_recall_section(memory, "cache") == ""
    asks = _count(state, "/observations/recent")

    state.observations = [{
        "id": 1, "type": "dialogue_summary", "title": "Dialogue summary: span",
        "topic_key": "dialogue:summary:0-10", "content": "written a moment later",
    }]
    section = _engram_recall_section(memory, "cache")

    assert _count(state, "/observations/recent") > asks, (
        "the blank answer was served from cache for the rest of the TTL"
    )
    assert "Dialogue summary: span" in section, section
