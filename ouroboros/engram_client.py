"""Engram HTTP client — the ONLY place Ouroboros talks to persistent memory.

Engram is an external persistent-memory service (curated *observations*, not a
raw transcript store). Ouroboros reaches it over HTTP, default
``127.0.0.1:7437``.

This module is deliberately **independent and high-cohesion**:

- It does **not** import ``ouroboros.mcp_client``. The memory path must not share
  a transport with the tool runtime (seed constraint C1). If you are tempted to
  route this through the generic MCP client, don't: that coupling is exactly
  what this module exists to avoid.
- It never lets a transport failure escape as an exception. Every call returns
  an :class:`EngramResult`; ``ok=False`` with a typed ``error_kind`` is how
  callers learn that memory was unreachable (constraint C6: never silent, never
  fatal).
- Every call carries an **explicit bound**. Engram silently clamps ``limit`` to
  500 and ``max_bytes`` to 65536; callers must not rely on that
  (constraint C14).

Reads are layered for progressive disclosure (constraint C20):
``search``/``recent``/``context`` (cheap index) → ``timeline`` (neighbourhood)
→ ``get`` (one full record). Never pull a corpus in one shot; the point of the
memory loop is that it does not re-inflate the context it replaced.

See ``.ouroboros/seed-engram-memory.yaml`` for the full contract.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

# --------------------------------------------------------------------------- #
# Bounds (constraint C14). Engram's own ceilings are the outer wall; these are
# what callers are allowed to ask for.
# --------------------------------------------------------------------------- #

#: Engram's server-side ceiling for any ``limit`` query parameter.
ENGRAM_HARD_LIMIT = 500
#: Engram's server-side ceiling for ``max_bytes`` on ``GET /context``.
ENGRAM_MAX_BYTES_CEILING = 65536
#: Model-facing search default. The documented MCP default is 10, max 20.
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 20
#: ``GET /timeline`` neighbourhood radius cap.
MAX_TIMELINE_RADIUS = 10
#: ``GET /context`` default cap for the whole rendered context.
DEFAULT_CONTEXT_MAX_BYTES = 16_384

DEFAULT_BASE_URL = "http://127.0.0.1:7437"
#: The port Engram's own ``engram serve`` binds when no port is given.
DEFAULT_PORT = 7437
DEFAULT_TIMEOUT_SECONDS = 5.0


def env_base_url() -> str:
    """The Engram endpoint, honouring the SERVICE'S OWN knobs.

    ``ENGRAM_BASE_URL`` is an Ouroboros-specific override (Engram does not define
    it). ``ENGRAM_PORT`` IS Engram's documented knob for ``engram serve``, so it is
    read as well — an operator who starts the daemon on a non-default port must not
    have this client keep talking to 7437 and silently report "unreachable".
    ``ENGRAM_SOCKET`` (Unix-socket-only mode) is NOT supported: this client is
    HTTP-only, and that case must surface as unreachable rather than as a wrong
    answer.
    """
    explicit = str(os.environ.get("ENGRAM_BASE_URL", "") or "").strip()
    if explicit:
        return explicit
    port = str(os.environ.get("ENGRAM_PORT", "") or "").strip()
    if port.isdigit() and 1 <= int(port) <= 65535:
        return f"http://127.0.0.1:{int(port)}"
    return ""


def env_socket() -> str:
    """Engram's Unix-socket transport knob. Supported only to REPORT the mismatch.

    Engram's own reference client selects its transport with ``ENGRAM_PORT`` or
    ``ENGRAM_SOCKET``. This client speaks HTTP over TCP only, so a socket-only
    deployment is not a service outage and must not be reported as one: the
    disclosure names the socket so the operator sees the real reason.
    """
    return str(os.environ.get("ENGRAM_SOCKET", "") or "").strip()


def env_project() -> str:
    """Operator override for the Engram project name.

    Mirrors Engram's own ``ENGRAM_PROJECT`` knob so both sides agree on the name.
    """
    return str(os.environ.get("ENGRAM_PROJECT", "") or "").strip()


def env_token() -> str:
    """Bearer token for the routes that require ``ENGRAM_HTTP_TOKEN``."""
    return str(os.environ.get("ENGRAM_HTTP_TOKEN", "") or "").strip()


def env_repo_root() -> str:
    """Configured repository root, for callers that hold only the drive root.

    Several consumers legitimately have just the drive in hand (deep self-review,
    evolution checkpoints, background consciousness). Resolving the Engram
    project from ``.../data`` would file one system's memories under a *second*
    project name and quietly split its memory in half — the same class of leak as
    F7, arriving through the back door. The repo root is the scope anchor; this
    reads the launcher-provided value at call time (never at import).
    """
    return str(os.environ.get("OUROBOROS_REPO_DIR", "") or "").strip()

#: Project names Engram itself falls back to when detection fails. We refuse
#: them: writing under an unresolved scope is how memories leak across projects
#: (seed failure mode F7).
_FORBIDDEN_PROJECTS = frozenset({"", "local"})

_PROJECT_NAME_RE = re.compile(r"[^a-z0-9._-]+")


class EngramConfigError(RuntimeError):
    """Raised only for *configuration* mistakes, never for transport failures.

    A missing/forbidden project name is a programming error we want to surface
    loudly at wiring time; an unreachable server is a runtime condition the
    caller handles through :class:`EngramResult`.
    """


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EngramResult:
    """Outcome of one Engram HTTP call. Never raises for transport problems."""

    ok: bool
    status: Optional[int] = None
    data: Any = None
    error_kind: str = ""          # transport | timeout | http | decode | config
    detail: str = ""

    @property
    def unavailable(self) -> bool:
        """True when memory could not be reached at all (soft dependency)."""
        return (not self.ok) and self.error_kind in {"transport", "timeout"}

    def items(self) -> List[Dict[str, Any]]:
        """Return list payloads uniformly (Engram returns ``[]``, never null)."""
        if isinstance(self.data, list):
            return [x for x in self.data if isinstance(x, dict)]
        if isinstance(self.data, dict):
            for key in ("observations", "relations", "sessions", "prompts", "items", "results"):
                value = self.data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    def one(self) -> Optional[Dict[str, Any]]:
        return self.data if isinstance(self.data, dict) else None


@dataclass(frozen=True)
class EngramConfig:
    """Resolved Engram connection settings."""

    base_url: str = DEFAULT_BASE_URL
    project: str = ""
    token: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    drive_root: Optional[pathlib.Path] = None

    def headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def normalize_project_name(value: Any) -> str:
    """Mirror Engram's own normalization: lowercase, trim, collapse separators.

    Hyphens and underscores are NOT interchangeable in Engram, matching its
    documented behavior.
    """
    text = str(value or "").strip().lower()
    text = _PROJECT_NAME_RE.sub("-", text).strip("-.")
    return text


def resolve_project(repo_root: Any, *, explicit: str = "") -> str:
    """Pin the Engram project for a repository.

    Precedence is deliberately narrow and **never** uses cwd detection:

    1. an explicit name passed by the caller,
    2. ``.engram/config.json`` → ``project_name`` at ``repo_root``,
    3. the repository directory name, normalized.

    Falls back to the directory name rather than Engram's own detection chain,
    because this process runs from many working directories (task drives, headless
    roots, worktrees) and cwd detection would scatter one project's memories
    across several names (seed failure mode F7).
    """
    name = normalize_project_name(explicit)
    if not name:
        cfg_path = pathlib.Path(repo_root) / ".engram" / "config.json"
        try:
            if cfg_path.exists():
                payload = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(payload, Mapping):
                    name = normalize_project_name(payload.get("project_name"))
        except (OSError, ValueError):
            name = ""
    if not name:
        try:
            name = normalize_project_name(pathlib.Path(repo_root).resolve().name)
        except OSError:
            name = ""
    if name in _FORBIDDEN_PROJECTS:
        raise EngramConfigError(
            f"refusing unresolved Engram project {name!r} for {repo_root!r}: "
            "set .engram/config.json project_name or pass explicit"
        )
    return name


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass
class EngramClient:
    """Thin, bounded, fail-soft HTTP client for Engram.

    Instantiate with :meth:`from_repo` so the project scope is pinned from the
    repository, not from the process working directory.
    """

    config: EngramConfig
    _session: Any = field(default=None, repr=False, compare=False)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_repo(
        cls,
        repo_root: Any,
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str = "",
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        drive_root: Any = None,
    ) -> "EngramClient":
        return cls(
            config=EngramConfig(
                base_url=str(base_url or DEFAULT_BASE_URL).rstrip("/"),
                project=resolve_project(repo_root, explicit=project),
                token=str(token or ""),
                timeout=float(timeout or DEFAULT_TIMEOUT_SECONDS),
                drive_root=pathlib.Path(drive_root) if drive_root else None,
            )
        )

    # -- transport --------------------------------------------------------- #

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> EngramResult:
        """One HTTP call. Returns EngramResult; never raises for I/O."""
        import requests  # local import: keeps module import cheap and explicit

        url = f"{self.config.base_url}{path}"
        clean: Dict[str, Any] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                clean[key] = "true" if value else "false"
            else:
                clean[key] = value
        # Scope every project-aware call explicitly; never rely on server-side
        # detection (AC2). Only an all_projects=true VALUE legitimately drops the
        # scope, so test the value rather than the key's presence — otherwise a
        # default all_projects=False silently strips the project from every call
        # and memories leak across projects (failure mode F7).
        wants_all = str(clean.get("all_projects", "")).strip().lower() in {"true", "1", "yes"}
        # /health is unscoped by definition, and /project/current IS the resolver —
        # injecting a project into either would make the client assert the answer it
        # is asking the server for.
        if self.config.project and path not in _UNSCOPED_PATHS and not wants_all:
            clean["project"] = self.config.project
        try:
            response = requests.request(
                method,
                url,
                params=clean or None,
                json=dict(body) if body is not None else None,
                headers=self.config.headers(),
                timeout=self.config.timeout,
            )
        except Exception as exc:  # requests.Timeout, ConnectionError, ...
            kind = "timeout" if "imeout" in type(exc).__name__ else "transport"
            return EngramResult(False, error_kind=kind, detail=f"{type(exc).__name__}: {exc}")
        if response.status_code >= 400:
            return EngramResult(
                False,
                status=response.status_code,
                error_kind="http",
                detail=str(response.text or "")[:400],
            )
        if not (response.content or b"").strip():
            return EngramResult(True, status=response.status_code, data=None)
        try:
            return EngramResult(True, status=response.status_code, data=response.json())
        except ValueError as exc:
            return EngramResult(
                False,
                status=response.status_code,
                error_kind="decode",
                detail=f"{type(exc).__name__}: {exc}",
            )

    # -- liveness ---------------------------------------------------------- #

    def health(self) -> EngramResult:
        """``GET /health`` — availability probe (drives the soft-dependency branch)."""
        return self._request("GET", "/health")

    # -- layer 1: cheap discovery (C20) ------------------------------------ #

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        type: str = "",
        scope: str = "",
        all_projects: bool = False,
    ) -> EngramResult:
        """``GET /search`` — FTS5 hit list, **bodies included**.

        Verified against the server implementation rather than assumed:
        ``store.buildSearchFTSQuery`` selects the full observation (including
        ``content``) and weights ``title`` 5.0 / ``content`` 1.0 / ``topic_key`` 3.0,
        so a caller that needs one record's body does not need a second
        ``GET /observations/{id}``. Engram only has a *preview* shape for the
        endpoints that ask for it explicitly.
        """
        return self._request(
            "GET",
            "/search",
            params={
                "q": query,
                "limit": _clamp(limit, 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT),
                "type": type or None,
                "scope": scope or None,
                "all_projects": all_projects or None,
            },
        )

    def recent(
        self, *, limit: int = DEFAULT_SEARCH_LIMIT, scope: str = "", all_projects: bool = False
    ) -> EngramResult:
        """``GET /observations/recent`` — newest first, bounded."""
        return self._request(
            "GET",
            "/observations/recent",
            params={
                "limit": _clamp(limit, 1, ENGRAM_HARD_LIMIT, DEFAULT_SEARCH_LIMIT),
                "scope": scope or None,
                "all_projects": all_projects or None,
            },
        )

    def project_current(self, cwd: str = "") -> EngramResult:
        """``GET /project/current`` — the SERVER's project policy for one directory.

        Engram's own reference client resolves the project this way rather than
        reimplementing the rules (its helper is commented "Resolve the project
        through the server, which owns project policy"). The envelope carries
        ``project``, ``project_source`` and, for an ambiguous directory,
        ``available_projects`` + ``error_hint`` rather than an error.
        """
        return self._request("GET", "/project/current", params={"cwd": cwd or None})

    def create_session(
        self, session_id: str, project: str, directory: str = ""
    ) -> EngramResult:
        """``POST /sessions`` — create (or re-assert) one session.

        Idempotent in practice, and it is the ONLY way a project comes into
        existence over HTTP: ``POST /observations`` requires a session that already
        exists, and project-scoped routes reject an explicit project the store does
        not know (``404 unknown_project``). So this call is the bootstrap, not an
        optional nicety.
        """
        return self._request(
            "POST",
            "/sessions",
            body={
                "id": str(session_id),
                "project": str(project),
                "directory": str(directory or ""),
            },
        )

    def stats(self) -> EngramResult:
        """``GET /stats`` — project counts, **no record bodies at all**.

        The server answers a count question with a count
        (``total_observations``; ``StatsProject`` when a project resolves), so a
        caller that only needs "how much memory is there" must not pull a window of
        full records to measure them.
        """
        return self._request("GET", "/stats")

    def context(
        self,
        *,
        observations: int = 0,
        prompts: int = 0,
        sessions: int = 0,
        pinned: int = 0,
        compact: bool = True,
        max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
        scope: str = "",
    ) -> EngramResult:
        """``GET /context`` — bounded, pre-rendered context block.

        ``compact=True`` is the default here on purpose: it drops inline content
        previews and keeps ``- [type] **title**``, which is what the discovery
        layer should see.
        """
        return self._request(
            "GET",
            "/context",
            params={
                "observations": observations,
                "prompts": prompts,
                "sessions": sessions,
                "pinned": pinned,
                "compact": compact,
                "max_bytes": _clamp(max_bytes, 1, ENGRAM_MAX_BYTES_CEILING, DEFAULT_CONTEXT_MAX_BYTES),
                "scope": scope or None,
            },
        )

    # -- layer 2: neighbourhood (C20) -------------------------------------- #

    def timeline(
        self, observation_id: int, *, before: int = 5, after: int = 5
    ) -> EngramResult:
        """``GET /timeline`` — chronological neighbourhood of one record."""
        return self._request(
            "GET",
            "/timeline",
            params={
                "observation_id": int(observation_id),
                "before": _clamp(before, 0, MAX_TIMELINE_RADIUS, 5),
                "after": _clamp(after, 0, MAX_TIMELINE_RADIUS, 5),
            },
        )

    # -- layer 3: one full record (C20) ------------------------------------ #

    def get(self, observation_id: Any) -> EngramResult:
        """``GET /observations/{id}`` — one full record. Callers fetch one at a time."""
        return self._request("GET", f"/observations/{observation_id}")

    # -- writes ------------------------------------------------------------ #

    def save(
        self,
        *,
        session_id: str,
        type: str,
        title: str,
        content: str,
        tool_name: str = "",
        scope: str = "project",
        topic_key: str = "",
        project: str = "",
    ) -> EngramResult:
        """``POST /observations`` — create one observation.

        ``title`` and ``content`` are mandatory server-side; the caller is
        responsible for having filtered out anything that is a *to-do* rather
        than a memory (constraint C18).
        """
        if not str(title or "").strip() or not str(content or "").strip():
            raise EngramConfigError("Engram observation requires non-empty title and content")
        return self._request(
            "POST",
            "/observations",
            body={
                "session_id": session_id,
                "type": type,
                "title": title,
                "content": content,
                "tool_name": tool_name or None,
                # Deliberately NOT defaulted to the client's project. Engram
                # inherits the project from the SESSION ("Normal writes should not
                # pass `project` as an arbitrary override"), and an inherited scope
                # can never disagree with the session that authorises the write.
                # Pass an explicit project only to target another known project.
                "project": project or None,
                "scope": scope,
                "topic_key": topic_key or None,
            },
        )

    def patch(self, observation_id: Any, **fields: Any) -> EngramResult:
        """``PATCH /observations/{id}`` — update fields (idempotent upsert path)."""
        body = {k: v for k, v in fields.items() if v is not None}
        return self._request("PATCH", f"/observations/{observation_id}", body=body)

    def delete(self, observation_id: Any, *, hard: bool = False) -> EngramResult:
        """``DELETE /observations/{id}``.

        Reserved for **explicit retraction only**. Automatic conflict
        resolution must never call this (constraint C5).
        """
        return self._request(
            "DELETE", f"/observations/{observation_id}", params={"hard": hard} if hard else None
        )

    def save_passive(
        self, *, session_id: str, content: str, source: str = "", project: str = ""
    ) -> EngramResult:
        """``POST /observations/passive`` — persist only parser-recognised learnings."""
        return self._request(
            "POST",
            "/observations/passive",
            body={
                "content": content,
                "session_id": session_id,
                "source": source or None,
                # Same rule as save(): the session owns the project association.
                "project": project or None,
            },
        )

    # -- incremental consumption (C16) ------------------------------------- #

    def review(self, *, limit: int = DEFAULT_SEARCH_LIMIT, all_projects: bool = False) -> EngramResult:
        """``GET /review`` — observations due for review (native consumption cursor)."""
        return self._request(
            "GET",
            "/review",
            params={
                "limit": _clamp(limit, 1, ENGRAM_HARD_LIMIT, DEFAULT_SEARCH_LIMIT),
                "all_projects": all_projects or None,
            },
        )

    def mark_reviewed(self, observation_id: Any) -> EngramResult:
        """``POST /review/mark_reviewed`` — advance the review cycle for one record."""
        return self._request(
            "POST", "/review/mark_reviewed", body={"observation_id": observation_id}
        )

    # -- conflict loop (C4 / C18) ------------------------------------------ #

    def conflicts(
        self, *, status: str = "", since: str = "", limit: int = 50, offset: int = 0
    ) -> EngramResult:
        """``GET /conflicts`` — list recorded memory relations."""
        return self._request(
            "GET",
            "/conflicts",
            params={
                "status": status or None,
                "since": since or None,
                "limit": _clamp(limit, 1, ENGRAM_HARD_LIMIT, 50),
                "offset": max(0, int(offset)),
            },
        )

    def conflicts_compare(
        self,
        *,
        memory_id_a: int,
        memory_id_b: int,
        relation: str,
        confidence: float,
        reasoning: str,
        model: str = "",
    ) -> EngramResult:
        """``POST /conflicts/compare`` — persist a semantic verdict for two records."""
        if relation not in _RELATIONS:
            raise EngramConfigError(f"unknown relation {relation!r}")
        return self._request(
            "POST",
            "/conflicts/compare",
            body={
                "memory_id_a": int(memory_id_a),
                "memory_id_b": int(memory_id_b),
                "relation": relation,
                "confidence": float(confidence),
                "reasoning": str(reasoning)[:200],
                "model": model or None,
            },
        )

    def conflicts_judge(
        self,
        *,
        judgment_id: str,
        relation: str,
        reason: str = "",
        evidence: str = "",
        confidence: float = 1.0,
    ) -> EngramResult:
        """``POST /conflicts/judge`` — resolve a pending candidate surfaced by a save."""
        if relation not in _RELATIONS:
            raise EngramConfigError(f"unknown relation {relation!r}")
        return self._request(
            "POST",
            "/conflicts/judge",
            body={
                "judgment_id": judgment_id,
                "relation": relation,
                "reason": reason or None,
                "evidence": evidence or None,
                "confidence": float(confidence),
            },
        )

    def suggest_topic_key(
        self, *, type: str = "", title: str = "", content: str = ""
    ) -> EngramResult:
        """``POST /topics/suggest`` — stable topic key for upsert identity."""
        if not (str(title or "").strip() or str(content or "").strip()):
            raise EngramConfigError("topics/suggest needs a non-empty title or content")
        return self._request(
            "POST",
            "/topics/suggest",
            body={"type": type or None, "title": title or None, "content": content or None},
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

#: Routes that must never carry the client's project: one is unscoped by
#: definition, the other is the authority the client asks ABOUT the project.
_UNSCOPED_PATHS = frozenset({"/health", "/project/current", "/sessions"})

_RELATIONS = frozenset(
    {"related", "compatible", "scoped", "conflicts_with", "supersedes", "not_conflict"}
)


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """Bound a caller-supplied number. Never trusts the caller (constraint C14)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), number))


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CONTEXT_MAX_BYTES",
    "DEFAULT_SEARCH_LIMIT",
    "ENGRAM_HARD_LIMIT",
    "ENGRAM_MAX_BYTES_CEILING",
    "MAX_SEARCH_LIMIT",
    "MAX_TIMELINE_RADIUS",
    "EngramClient",
    "EngramConfig",
    "EngramConfigError",
    "EngramResult",
    "normalize_project_name",
    "resolve_project",
]
