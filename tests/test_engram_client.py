"""Contract tests for the isolated Engram HTTP client.

Covers the seed's Phase-1 acceptance criteria
(``.ouroboros/seed-engram-memory.yaml``):

- AC1  independent module, no ``mcp_client`` coupling
- AC2  project pinned per repo, never Engram's ``local`` fallback (F7)
- AC16 every request carries an explicit bound
- C6   unreachable memory is soft: typed result, never an exception
- C5   ``DELETE`` is an explicit-retraction path only

No network access is required: a local stub server stands in for Engram.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ouroboros import engram_client as ec

# --------------------------------------------------------------------------- #
# Stub Engram server
# --------------------------------------------------------------------------- #


class _StubState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[str, tuple[int, object]] = {}
        self.default: tuple[int, object] = (200, {"status": "ok"})


class _Handler(BaseHTTPRequestHandler):
    state: _StubState

    def log_message(self, *args):  # silence test output
        return

    def _record(self, method: str):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        entry = {
            "method": method,
            "path": parsed.path,
            "params": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "body": body,
            "auth": self.headers.get("Authorization"),
        }
        self.state.requests.append(entry)
        status, payload = self.state.responses.get(parsed.path, self.state.default)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._record("GET")

    def do_POST(self):
        self._record("POST")

    def do_PATCH(self):
        self._record("PATCH")

    def do_DELETE(self):
        self._record("DELETE")


@pytest.fixture()
def stub():
    state = _StubState()
    handler = type("_BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _client(base_url: str, repo: pathlib.Path, **kw) -> ec.EngramClient:
    return ec.EngramClient.from_repo(repo, base_url=base_url, **kw)


# --------------------------------------------------------------------------- #
# AC1 — independence
# --------------------------------------------------------------------------- #


def test_module_does_not_import_mcp_client():
    """AC1/F6: the memory path must not share the generic MCP transport.

    Assert on real import statements rather than the substring: this module's
    own docstring explains *why* it avoids ``mcp_client``, so a naive
    ``in source`` check would fail on its own commentary.
    """
    source = pathlib.Path(ec.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if re.match(r"\s*(from|import)\s+", line)
    ]
    offenders = [ln for ln in imports if "mcp_client" in ln or "MCPServerConfig" in ln]
    assert offenders == [], f"Engram client must not import the MCP transport: {offenders}"
    # ...and prove it at runtime, not just in the text.
    assert not hasattr(ec, "MCPServerConfig")
    assert not hasattr(ec, "MCPServerRuntime")


def test_module_exports_the_documented_surface():
    for name in ("EngramClient", "EngramConfig", "EngramResult", "resolve_project"):
        assert hasattr(ec, name)
    client = ec.EngramClient
    for method in ("health", "search", "save", "patch", "delete", "recent", "timeline",
                   "get", "context", "review", "mark_reviewed", "conflicts_compare"):
        assert callable(getattr(client, method)), method


# --------------------------------------------------------------------------- #
# AC2 — project scoping
# --------------------------------------------------------------------------- #


def test_resolve_project_prefers_engram_config(tmp_path):
    cfg_dir = tmp_path / ".engram"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"project_name": "  My-Repo  "}', encoding="utf-8")
    assert ec.resolve_project(tmp_path) == "my-repo"


def test_resolve_project_falls_back_to_directory_name(tmp_path):
    repo = tmp_path / "Some Project"
    repo.mkdir()
    assert ec.resolve_project(repo) == "some-project"


def test_resolve_project_rejects_engram_local_fallback(tmp_path):
    """F7: Engram's own detection falls back to 'local'; we refuse that name."""
    for bad in ("local", "LOCAL", "  local  "):
        with pytest.raises(ec.EngramConfigError):
            ec.resolve_project(tmp_path, explicit=bad)


def test_blank_explicit_means_unset_and_falls_back_to_dirname(tmp_path):
    """A blank explicit name is 'unset', not an error: use the repo dirname."""
    repo = tmp_path / "MyRepo"
    repo.mkdir()
    assert ec.resolve_project(repo, explicit="   ") == "myrepo"


def test_client_pins_project_never_local(stub, tmp_path):
    state, url = stub
    repo = tmp_path / "ouroboros"
    repo.mkdir()
    client = _client(url, repo)
    assert client.config.project == "ouroboros" != "local"
    client.search("anything")
    assert state.requests[-1]["params"]["project"] == "ouroboros"


# --------------------------------------------------------------------------- #
# AC16 / C14 — every request carries an explicit bound
# --------------------------------------------------------------------------- #


def test_search_limit_is_clamped_to_model_facing_max(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    client.search("q", limit=9999)
    assert int(state.requests[-1]["params"]["limit"]) == ec.MAX_SEARCH_LIMIT


def test_a_bogus_match_mode_never_reaches_the_wire(stub, tmp_path):
    """Engram's server accepts only ""/"all"/"any" and answers HTTP 400 for
    anything else — which the read layer maps to ``rejected``, so a caller's typo
    surfaces to the operator as "the memory service refused this request".

    Asserted on the outgoing request, not on an internal variable: the value the
    store sees is the only thing that can turn a typo into a fake outage.
    """
    state, url = stub
    client = _client(url, tmp_path)

    # Values the server would refuse: dropped, never forwarded.
    for bogus in (False, "or", "none", "OR", "xyzzy"):
        client.search("q", match_mode=bogus)
        assert state.requests[-1]["params"].get("match_mode") is None, bogus

    # Case/whitespace variants of the two literals are the SAME value, so they are
    # normalized rather than forwarded as the caller spelled them.
    client.search("q", match_mode=" ANY ")
    assert state.requests[-1]["params"]["match_mode"] == "any"
    client.search("q", match_mode="ALL")
    assert state.requests[-1]["params"]["match_mode"] == "all"

    # The empty default stays absent from the wire, exactly as it was.
    client.search("q")
    assert "match_mode" not in state.requests[-1]["params"]


def test_a_query_with_no_usable_word_never_reaches_the_store(stub, tmp_path):
    """`search` is the choke point every caller shares, and one of them — the
    model-facing `engram` tool's ``op='search'`` — takes its query straight from
    the model, so it can be punctuation or quotes. Such a query has nothing to
    match on, so the request is not sent at all: the assertion is the ABSENT
    request (the one thing a stubbed server cannot fake), and the caller-visible
    outcome is the zero-hit shape rather than a failure.
    """
    state, url = stub
    client = _client(url, tmp_path)

    for query in ('"', '" "'):
        before = len(state.requests)
        result = client.search(query)
        assert len(state.requests) == before, query
        assert result.ok and result.items() == [], query
        assert not result.unavailable, query

    # A normal query is unaffected: it still goes to the wire, and it carries
    # match_mode ONLY when the caller asked for it.
    client.search("which span did we ship")
    entry = state.requests[-1]
    assert entry["path"] == "/search" and entry["params"]["q"] == "which span did we ship"
    assert "match_mode" not in entry["params"]

    client.search("which span did we ship", match_mode="any")
    assert state.requests[-1]["params"]["match_mode"] == "any"


def test_context_max_bytes_is_clamped_to_engram_ceiling(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    client.context(max_bytes=10_000_000)
    assert int(state.requests[-1]["params"]["max_bytes"]) == ec.ENGRAM_MAX_BYTES_CEILING


def test_context_defaults_to_compact(stub, tmp_path):
    """C20 layer 1: discovery reads must not pull inline bodies."""
    state, url = stub
    client = _client(url, tmp_path)
    client.context(observations=5)
    assert state.requests[-1]["params"]["compact"] == "true"


def test_timeline_radius_is_clamped(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    client.timeline(7, before=500, after=500)
    assert int(state.requests[-1]["params"]["before"]) == ec.MAX_TIMELINE_RADIUS


def test_every_recorded_request_is_bounded(stub, tmp_path):
    """AC16: sweep every call the client can make and assert bounds are present."""
    state, url = stub
    client = _client(url, tmp_path)
    client.search("q")
    client.recent()
    client.context()
    client.timeline(1)
    client.get(1)
    client.review()
    client.conflicts()

    skip = {"/health", "/observations/1", "/review/mark_reviewed"}
    for entry in state.requests:
        if entry["path"] in skip:
            continue
        assert entry["params"].get("project"), f"unscoped request: {entry}"
        bounded = any(k in entry["params"] for k in ("limit", "max_bytes", "before", "after"))
        assert bounded, f"unbounded request: {entry}"


# --------------------------------------------------------------------------- #
# C6 — soft dependency
# --------------------------------------------------------------------------- #


def test_health_reports_ok_on_live_stub(stub, tmp_path):
    state, url = stub
    state.responses["/health"] = (200, {"status": "ok", "service": "engram"})
    result = _client(url, tmp_path).health()
    assert result.ok and result.status == 200


def test_unreachable_is_soft_not_fatal(tmp_path):
    """C6/F1: a dead Engram must never raise into the caller."""
    client = ec.EngramClient.from_repo(tmp_path, base_url="http://127.0.0.1:9", timeout=0.4)
    result = client.health()
    assert result.ok is False
    assert result.unavailable is True
    assert result.error_kind in {"transport", "timeout"}


def test_http_error_is_typed_not_raised(stub, tmp_path):
    state, url = stub
    state.responses["/observations/recent"] = (500, {"error": "boom"})
    result = _client(url, tmp_path).recent()
    assert result.ok is False and result.error_kind == "http" and result.status == 500


def test_empty_body_parses_as_success(stub, tmp_path):
    state, url = stub
    state.responses["/review"] = (200, None)
    assert _client(url, tmp_path).review().ok is True


def test_plain_list_and_wrapped_payloads_both_iterate(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    state.responses["/observations/recent"] = (200, [{"id": 1}, {"id": 2}])
    assert len(client.recent().items()) == 2
    state.responses["/search"] = (200, {"observations": [{"id": 3}]})
    assert len(client.search("q").items()) == 1
    state.responses["/search"] = (200, {"nothing": True})
    assert client.search("q").items() == []


# --------------------------------------------------------------------------- #
# writes and the retraction boundary
# --------------------------------------------------------------------------- #


def test_save_sends_project_in_body_and_path_is_correct(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    client.save(session_id="s1", type="learning", title="T", content="C")
    entry = state.requests[-1]
    assert entry["method"] == "POST" and entry["path"] == "/observations"
    # Engram inherits the project from the session, and its own guidance says not to
    # pass it as an arbitrary override. An explicit project still wins when given.
    assert entry["body"]["project"] is None
    assert entry["body"]["title"] == "T"


def test_save_rejects_empty_title_or_content(tmp_path):
    client = ec.EngramClient.from_repo(tmp_path, base_url="http://127.0.0.1:9")
    with pytest.raises(ec.EngramConfigError):
        client.save(session_id="s", type="t", title="  ", content="c")
    with pytest.raises(ec.EngramConfigError):
        client.save(session_id="s", type="t", title="t", content="")


def test_delete_is_soft_by_default(stub, tmp_path):
    """C5: DELETE is explicit retraction, and soft unless hard is asked for."""
    state, url = stub
    client = _client(url, tmp_path)
    client.delete(42)
    entry = state.requests[-1]
    assert entry["method"] == "DELETE" and entry["path"] == "/observations/42"
    assert "hard" not in entry["params"]
    client.delete(42, hard=True)
    assert state.requests[-1]["params"]["hard"] == "true"


def test_mark_reviewed_uses_observation_id_field(stub, tmp_path):
    """C16: the review cycle is Engram's native consumption cursor."""
    state, url = stub
    _client(url, tmp_path).mark_reviewed(11)
    entry = state.requests[-1]
    assert entry["path"] == "/review/mark_reviewed"
    assert entry["body"] == {"observation_id": 11}


def test_auth_header_only_when_token_present(stub, tmp_path):
    state, url = stub
    _client(url, tmp_path).health()
    assert state.requests[-1]["auth"] is None
    ec.EngramClient.from_repo(tmp_path, base_url=url, token="secret").health()
    assert state.requests[-1]["auth"] == "Bearer secret"


def test_unknown_relation_is_rejected_before_sending(stub, tmp_path):
    state, url = stub
    client = _client(url, tmp_path)
    with pytest.raises(ec.EngramConfigError):
        client.conflicts_compare(
            memory_id_a=1, memory_id_b=2, relation="whatever", confidence=0.9, reasoning="r"
        )
    assert state.requests == []


# --------------------------------------------------------------------------- #
# F1/F7 draft: the scope must never be decided by the SERVER (AC2), and a
# single-record lookup must not read "wrong project" as "no such record".
# --------------------------------------------------------------------------- #


def test_a_blank_project_refuses_scoped_paths_but_not_the_resolver(stub, tmp_path):
    state, url = stub
    client = ec.EngramClient(config=ec.EngramConfig(base_url=url, project=""))

    for call in (
        lambda: client.timeline(123),
        lambda: client.get(123),
        lambda: client.search("q"),
        lambda: client.recent(),
    ):
        result = call()
        assert result.ok is False and result.error_kind == "config", result.detail
        assert "unscoped" in result.detail
    assert state.requests == [], "an unscoped scoped-path request reached the wire"

    # The resolver itself (and /health) are unscoped BY DEFINITION and still run.
    client.project_current(cwd="/tmp")
    client.health()
    assert [r["path"] for r in state.requests] == ["/project/current", "/health"]


def test_a_wrong_project_timeline_miss_is_retried_scope_dropped(stub, tmp_path):
    state, url = stub
    state.responses["/timeline"] = (404, {"error": "observation not found in resolved project"})
    client = _client(url, tmp_path)

    result = client.timeline(123)

    assert len(state.requests) == 2, state.requests
    first, second = state.requests
    assert first["params"]["project"] and "all_projects" not in first["params"]
    assert second["params"].get("all_projects") == "true"
    assert "project" not in second["params"], "the retry must drop the resolved scope"
    # Both attempts answered the same 404, so the FIRST typed failure is kept: it is
    # the one whose detail names the scope problem.
    assert result.ok is False and result.status == 404


def test_the_same_fallback_covers_the_single_record_read(stub, tmp_path):
    state, url = stub
    state.responses["/observations/9"] = (
        404, {"error": "observation not found in resolved project"},
    )
    client = _client(url, tmp_path)

    client.get(9)

    assert len(state.requests) == 2, state.requests
    assert state.requests[1]["params"].get("all_projects") == "true"


def test_an_unknown_project_404_is_not_retried(stub, tmp_path):
    """The OTHER 404 on these routes is a configuration fact, not a scope miss."""
    state, url = stub
    state.responses["/timeline"] = (404, {"error": 'project "x" not found', "code": "unknown_project"})
    client = _client(url, tmp_path)

    result = client.timeline(123)

    assert len(state.requests) == 1, "an unknown_project 404 must keep its typed failure"
    assert result.ok is False and result.status == 404



def test_patch_updates_one_record_and_drops_unchanged_fields(stub, tmp_path):
    """AC8(c)'s PATCH leg — the update path, which no other test exercises.

    The seed names this behaviour explicitly: "内容有实质更新 → 走 PATCH", i.e. a
    change to an existing record is an UPDATE of it, not a second POST. The
    contract is the whole observable surface — the request targets one record's
    id, carries ONLY the fields being changed (a ``None`` means "leave it as it
    is", so it must not travel as a null), returns the store's answer as an
    ``EngramResult``, and a refusal comes back TYPED rather than raised (C6: no
    memory path may fail a task).
    """
    state, url = stub
    client = _client(url, tmp_path)

    result = client.patch(7, title="T", content="C", topic_key="k", scope=None)

    entry = state.requests[-1]
    assert entry["method"] == "PATCH" and entry["path"] == "/observations/7"
    assert entry["body"] == {"title": "T", "content": "C", "topic_key": "k"}
    assert result.ok is True and result.status == 200

    # A refusal is typed, never raised.
    state.responses["/observations/7"] = (404, {"code": "unknown_observation"})
    refused = client.patch(7, title="T")

    assert refused.ok is False and refused.error_kind == "http"
    assert refused.status == 404 and refused.unavailable is False
