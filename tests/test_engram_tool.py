"""Contract tests for the `engram` read tool.

Covers the read side of the seed (``.ouroboros/seed-engram-memory.yaml``), which
is what makes AC12's prompt rule actionable rather than aspirational:

- AC23 progressive disclosure is enforced by the tool's shape, not by asking the
       model to behave: `search` yields titles only, `timeline` a neighbourhood,
       `read` exactly one record
- AC23 each layer is bounded, and a single retrieval costs less than the prompt
       section it stands in for
- C6   an unreachable service is a typed notice, never an exception and never an
       empty result that reads as "nothing exists"
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from ouroboros.tools import engram as engram_tool


class _State:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.payloads: dict[str, object] = {}
        self.fail = False


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        self.state.requests.append(
            {"path": parsed.path, "params": {k: v[0] for k, v in parse_qs(parsed.query).items()}}
        )
        if self.state.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"down"}')
            return
        payload = json.dumps(self.state.payloads.get(parsed.path, [])).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def stub(monkeypatch, tmp_path):
    state = _State()
    handler = type("_H", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ENGRAM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    repo = tmp_path / "ouroboros"
    repo.mkdir()
    ctx = SimpleNamespace(repo_dir=repo, drive_root=tmp_path)
    try:
        yield state, ctx
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# layer 1 — search returns identity, not bodies
# --------------------------------------------------------------------------- #


def test_search_returns_titles_only(stub):
    """AC23a: discovery must not pull bodies, or net subtraction is undone."""
    state, ctx = stub
    state.payloads["/search"] = [
        {"id": 1, "type": "decision", "title": "Chose zstd frames", "created_at": "2026-01-01",
         "content": "BODY-SHOULD-NOT-APPEAR"},
        {"id": 2, "type": "bugfix", "title": "Fixed N+1", "created_at": "2026-01-02",
         "content": "ALSO-SHOULD-NOT-APPEAR"},
    ]
    out = engram_tool._engram(ctx, op="search", query="zstd")
    assert "[1] (decision) Chose zstd frames" in out
    assert "[2] (bugfix) Fixed N+1" in out
    assert "BODY-SHOULD-NOT-APPEAR" not in out
    assert "ALSO-SHOULD-NOT-APPEAR" not in out


def test_search_sends_project_and_bounded_limit(stub):
    state, ctx = stub
    engram_tool._engram(ctx, op="search", query="x", limit=999)
    req = state.requests[-1]
    assert req["params"]["project"] == "ouroboros"
    assert int(req["params"]["limit"]) == engram_tool.MAX_SEARCH_LIMIT


def test_search_output_is_bounded(stub):
    state, ctx = stub
    state.payloads["/search"] = [
        {"id": i, "type": "t", "title": "T" * 300, "created_at": "2026-01-01"} for i in range(60)
    ]
    out = engram_tool._engram(ctx, op="search", query="x")
    assert len(out) <= engram_tool.MAX_SEARCH_OUTPUT_CHARS + 100


def test_search_without_query_says_so(stub):
    _, ctx = stub
    assert "needs a non-empty" in engram_tool._engram(ctx, op="search", query="  ")


def test_empty_search_is_not_confused_with_unavailable(stub):
    state, ctx = stub
    state.payloads["/search"] = []
    out = engram_tool._engram(ctx, op="search", query="nothing-here")
    assert "No candidate memories" in out
    assert "UNAVAILABLE" not in out


# --------------------------------------------------------------------------- #
# layer 2 — timeline
# --------------------------------------------------------------------------- #


def test_timeline_shows_the_neighbourhood_without_bodies(stub):
    state, ctx = stub
    state.payloads["/timeline"] = [
        {"id": 9, "type": "discovery", "title": "before", "content": "NO-BODY"},
        {"id": 10, "type": "decision", "title": "the hit", "content": "NO-BODY"},
    ]
    out = engram_tool._engram(ctx, op="timeline", observation_id=10, before=3, after=3)
    assert "Neighbourhood of observation 10" in out
    assert "[10] (decision) the hit" in out
    assert "NO-BODY" not in out


def test_timeline_radius_is_clamped(stub):
    state, ctx = stub
    engram_tool._engram(ctx, op="timeline", observation_id=5, before=99, after=99)
    req = state.requests[-1]
    assert int(req["params"]["before"]) == 10
    assert int(req["params"]["after"]) == 10


def test_timeline_needs_an_id(stub):
    _, ctx = stub
    assert "needs an `observation_id`" in engram_tool._engram(ctx, op="timeline")


# --------------------------------------------------------------------------- #
# layer 3 — one full record
# --------------------------------------------------------------------------- #


def test_read_returns_exactly_one_record(stub):
    state, ctx = stub
    state.payloads["/observations/7"] = {
        "id": 7, "type": "decision", "title": "the one", "project": "ouroboros",
        "scope": "project", "updated_at": "2026-01-01", "content": "the full body",
    }
    out = engram_tool._engram(ctx, op="read", observation_id=7)
    assert "the full body" in out
    assert "[7] (decision) the one" in out
    # The whole sequence, not just the observation fetch: an HTTP sequence is this
    # integration's contract, and a filtered count would stop detecting an extra
    # re-resolution, a duplicated session bootstrap, or a stray fan-out.
    # This stub is GET-only, so the session bootstrap is not part of its surface:
    # the resolver call plus exactly one observation fetch is the whole sequence.
    assert [r["path"] for r in state.requests] == ["/project/current", "/observations/7"]


def test_read_truncates_a_huge_record(stub):
    """The marker names the exact resume point, and the window honours the bound."""
    state, ctx = stub
    state.payloads["/observations/8"] = {
        "id": 8, "type": "t", "title": "big", "content": "x" * 50_000,
    }
    out = engram_tool._engram(ctx, op="read", observation_id=8)
    assert len(out) <= engram_tool.MAX_READ_CONTENT_CHARS + 500
    offset = int(out.rsplit("offset=", 1)[1].split("]")[0])
    # The note's offset IS the truncation point, it stays inside the read bound, and
    # the window really carried that many characters — the note cannot disagree with
    # the text it terminates.
    assert f"truncated at char {offset} of 50000" in out
    assert 0 < offset <= engram_tool.MAX_READ_CONTENT_CHARS
    assert out.count("x") >= offset - 200


def test_read_with_offset_continues_the_record(stub):
    """The record is here in full: the bound is a display bound, so it continues."""
    state, ctx = stub
    body = "A" * 4_000 + "B" * 4_000 + "C" * 100
    state.payloads["/observations/9"] = {"id": 9, "type": "t", "title": "long", "content": body}

    first = engram_tool._engram(ctx, op="read", observation_id=9)
    offset = int(first.rsplit("offset=", 1)[1].split("]")[0])
    assert f"truncated at char {offset} of {len(body)} — continue with op='read' offset={offset}" in first
    assert "knowledge_read" not in first, "the read tool must name its own surface"
    assert 4_000 - offset <= 200, "the window must leave room for the note, not overshoot it"

    second = engram_tool._engram(ctx, op="read", observation_id=9, offset=offset)
    assert f"[continued from char {offset} of {len(body)}]" in second
    assert "B" in second and second.count("A") <= 4_000 - offset
    assert "knowledge_read" not in second

    third = engram_tool._engram(ctx, op="read", observation_id=9, offset=8_000)
    assert "[continued from char 8000 of 8100]" in third
    assert "C" * 100 in third
    assert "truncated" not in third, "the end of the record must not be marked as truncated"


def test_read_offset_past_the_end_is_disclosed(stub):
    state, ctx = stub
    state.payloads["/observations/10"] = {"id": 10, "type": "t", "title": "short", "content": "short"}

    out = engram_tool._engram(ctx, op="read", observation_id=10, offset=99)

    assert "offset 99 is past the end" in out and "5 chars" in out
    assert "truncated" not in out


def test_read_budget_is_below_the_sections_it_replaces(stub):
    """AC23d: a read must not cost more than the section it stands in for."""
    # Smallest replaced section measured on the live drive: `## Last Deep Self-Review` (307).
    # The largest usable single read must stay well under the knowledge index (6,569).
    assert engram_tool.MAX_READ_CONTENT_CHARS < 6_569
    assert engram_tool.MAX_SEARCH_OUTPUT_CHARS < 6_569
    assert engram_tool.MAX_TIMELINE_OUTPUT_CHARS < 6_569


def test_read_needs_an_id(stub):
    _, ctx = stub
    assert "needs an `observation_id`" in engram_tool._engram(ctx, op="read")


def test_read_missing_record_says_not_found(stub):
    state, ctx = stub
    state.payloads["/observations/999"] = None
    out = engram_tool._engram(ctx, op="read", observation_id=999)
    assert "not found" in out


# --------------------------------------------------------------------------- #
# C6 — unreachable is typed, not silent
# --------------------------------------------------------------------------- #


def test_unreachable_is_typed_and_never_raises(stub):
    state, ctx = stub
    state.fail = True
    out = engram_tool._engram(ctx, op="search", query="x")
    assert "ENGRAM READ FAILED" in out or "ENGRAM UNAVAILABLE" in out
    assert "No candidate memories" not in out


def test_unreachable_does_not_claim_memory_was_checked(stub):
    state, ctx = stub
    state.fail = True
    out = engram_tool._engram(ctx, op="read", observation_id=1)
    assert "NOT 'no relevant memory'" in out or "not found" not in out.lower()


def test_unknown_op_is_reported(stub):
    _, ctx = stub
    assert "Unknown op" in engram_tool._engram(ctx, op="delete")


def test_tool_is_registered_read_only():
    """POLICY_SKIP: retrieval must not pay a per-call safety recheck."""
    from ouroboros.safety import POLICY_SKIP, TOOL_POLICY

    assert TOOL_POLICY["engram"] == POLICY_SKIP


def test_tool_schema_is_advertised():
    entries = {e.name: e for e in engram_tool.get_tools()}
    assert "engram" in entries
    schema = entries["engram"].schema
    assert schema["parameters"]["properties"]["op"]["enum"] == ["search", "timeline", "read"]
    assert "engram" in json.dumps(schema["description"]).lower()


# --------------------------------------------------------------------------- #
# operator overrides — the tool honours the SAME set as the write sink
# --------------------------------------------------------------------------- #
def test_engram_project_env_override_pins_the_tool(stub, monkeypatch):
    """An operator who pins the scope via ENGRAM_PROJECT (the container shape:
    no local .engram/) must get tool READS in the same project as writes —
    not a silent split where the tool reads the repo basename."""
    monkeypatch.setenv("ENGRAM_PROJECT", "pinned-scope")
    state, ctx = stub
    engram_tool._engram(ctx, op="search", query="x")
    assert state.requests[-1]["params"]["project"] == "pinned-scope"


def test_without_override_the_tool_resolves_the_repo_basename(stub):
    """No override: the offline resolution (repo basename) stands, matching
    the write path's offline-first resolution for the same repo."""
    state, ctx = stub
    engram_tool._engram(ctx, op="search", query="x")
    assert state.requests[-1]["params"]["project"] == "ouroboros"


def test_an_unresolvable_project_refuses_config_not_a_bare_404(stub, monkeypatch):
    """Fail-closed reads: a blank/forbidden offline scope is a CONFIGURATION
    fact (typed refusal), never a wire request against project "_unresolved"
    that comes back as a 404 and reads as "no memory"."""
    monkeypatch.setenv("ENGRAM_PROJECT", "local")  # forbidden name in ENGRAM's own registry
    state, ctx = stub
    out = engram_tool._engram(ctx, op="search", query="x")
    assert out.startswith("ENGRAM NOT CONFIGURED")
    assert all(r["path"] != "/search" for r in state.requests)
