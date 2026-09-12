"""Engram read tool — on-demand durable-memory retrieval, in three layers.

The main-chat prompt no longer carries durable knowledge or consolidated
dialogue (see ``prompts/SYSTEM.md`` § Memory and Context). That content lives in
Engram and is fetched **here**, by the agent's own judgment.

Progressive disclosure is enforced by what each ``op`` returns, not by asking the
model to behave (seed constraint C20):

* ``op="search"``   — candidates only: ``[id] (type) title``. No bodies.
* ``op="timeline"`` — the neighbourhood of one hit, so a candidate can be judged.
* ``op="read"``     — **exactly one** full record.

Every response is bounded, and the bounds are deliberately smaller than the
prompt sections this replaces (AC23d). If a read cannot be justified as standing
in for a specific section, it has not earned the tokens.

Unreachable memory is a typed message, never an exception and never a silent
empty list: "Engram returned nothing" and "Engram is down" are different facts
and the caller is told which one it is.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolEntry

log = logging.getLogger(__name__)

#: Candidates are titles, so a generous count is still cheap — but bounded.
MAX_SEARCH_LIMIT = 20
MAX_SEARCH_OUTPUT_CHARS = 2_000
MAX_TIMELINE_OUTPUT_CHARS = 3_000
#: One record. Well under the smallest section this replaces (6,569 chars).
MAX_READ_CONTENT_CHARS = 4_000
#: AC23(c): how many full records one retrieval turn may pull. The seed's default.
MAX_READS_PER_TURN = 3
#: AC23(d): the whole turn's rendered output may not exceed the LARGEST section a
#: retrieval is allowed to stand in for (dialogue history, 30,345 chars). Per-op
#: caps alone do not bound a turn: 2,000 + 3,000 + 4,000 × N does not converge.
MAX_TURN_OUTPUT_CHARS = 30_345
#: How many distinct turns are remembered. Oldest scopes are evicted, so a long
#: process cannot accumulate one entry per task forever.
MAX_TRACKED_TURNS = 256

_TURN_LOCK = threading.Lock()
_TURN_USAGE: "Dict[tuple, Dict[str, int]]" = {}


def _turn_scope(ctx: Any) -> "tuple | None":
    """The identity of one retrieval turn, or ``None`` when it cannot be known.

    In the owner chat a turn IS a task (each owner message becomes its own task),
    so ``task_id`` scopes it correctly. A long task legitimately retrieves across
    many rounds, so the budget is per task rather than a hard process-wide limit —
    and the counter is exposed so an operator can see a turn that hit the ceiling.

    An unidentifiable caller is NOT budgeted rather than pooled into a shared
    bucket: guessing would let two unrelated callers exhaust each other's budget,
    and a spurious refusal is worse than an unenforced cap.
    """
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return None
    chat_id = getattr(ctx, "current_chat_id", None)
    return (task_id, str(chat_id if chat_id is not None else "-"))


def turn_usage(ctx: Any) -> Dict[str, int]:
    """``{"reads": n, "chars": n}`` already spent by this turn."""
    scope = _turn_scope(ctx)
    if scope is None:
        return {"reads": 0, "chars": 0}
    with _TURN_LOCK:
        return dict(_TURN_USAGE.get(scope, {}))


def reset_turn_budgets() -> None:
    """Drop all turn accounting. For tests and a deliberate run boundary."""
    with _TURN_LOCK:
        _TURN_USAGE.clear()


def _spend(ctx: Any, *, chars: int, reads: int = 0) -> None:
    scope = _turn_scope(ctx)
    if scope is None:
        return
    with _TURN_LOCK:
        used = _TURN_USAGE.get(scope)
        if used is None:
            if len(_TURN_USAGE) >= MAX_TRACKED_TURNS:
                _TURN_USAGE.pop(next(iter(_TURN_USAGE)))
            used = {"reads": 0, "chars": 0}
            _TURN_USAGE[scope] = used
        used["reads"] += reads
        used["chars"] += chars


def _budget_refusal(ctx: Any, *, op: str) -> str:
    """The typed message for a turn that has spent its retrieval budget."""
    used = turn_usage(ctx)
    return (
        f"ENGRAM RETRIEVAL BUDGET SPENT — this turn has already pulled "
        f"{used.get('reads', 0)} full record(s) and {used.get('chars', 0)} chars from memory "
        f"(caps: {MAX_READS_PER_TURN} records, {MAX_TURN_OUTPUT_CHARS} chars). Retrieval must "
        f"never cost more than the prompt section it replaces, so {op!r} is refused rather than "
        "quietly inflating the context. Narrow the question, use what you already retrieved, or "
        "wait for the next turn."
    )


def _client(ctx: Any):
    """The write sink's client, not a parallel construction.

    Resolution parity is the whole point: the write side resolves the project
    through THREE defenses the tool must not skip if reads and writes are to
    never disagree — an explicit ``ENGRAM_PROJECT`` override, a drive-local
    ``.engram/config.json`` pin (an operator who pins the drive means it for
    every consumer of that drive), and the server's own ``/project/current``
    answer. Building a client here from ``repo_dir`` alone pretended the
    basename was enough; the container shape (no local pin anywhere) is exactly
    where that split reads project "repo" while writes land in "ouroboros".

    ``client_for`` accepts an env/ctx-shaped object (``drive_root`` or
    ``REPO_DIR`` spellings) and makes the sink ready so the project-scoped routes
    do not 404 on a store that does not yet know the project. It raises
    ``EngramConfigError`` when the scope cannot be resolved (a blank or forbidden
    project name), so every caller must carry the typed not-configured branch —
    ``_engram`` returns its ``ENGRAM NOT CONFIGURED`` notice — rather than letting
    the raise escape into a task.
    """
    from ouroboros.engram_read import client_for

    return client_for(ctx)


def _unavailable(result: Any) -> str:
    """A typed, honest gap — never an empty result that reads as 'nothing exists'."""
    if getattr(result, "unavailable", False):
        return (
            "ENGRAM UNAVAILABLE — the memory service could not be reached "
            f"({getattr(result, 'error_kind', 'unknown')}). This is NOT 'no relevant memory': "
            "the store was never read. Proceed without it, and do not claim you checked."
        )
    return (
        f"ENGRAM READ FAILED — {getattr(result, 'error_kind', 'unknown')}"
        f" (status={getattr(result, 'status', None)}): {getattr(result, 'detail', '')}"[:400]
    )


def _one_line(record: Dict[str, Any]) -> str:
    """Layer-1/2 shape: identity and kind, never the body."""
    obs_id = record.get("id", "?")
    kind = str(record.get("type") or "?").strip() or "?"
    title = " ".join(str(record.get("title") or "").split())[:120]
    when = str(record.get("created_at") or "")[:10]
    return f"[{obs_id}] ({kind}) {title}" + (f" — {when}" if when else "")


def _bounded(lines: List[str], limit: int) -> str:
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)].rstrip() + "\n…[truncated to stay within budget]"


def _engram(
    ctx: Any,
    op: str = "search",
    query: str = "",
    observation_id: int = 0,
    limit: int = 10,
    before: int = 5,
    after: int = 5,
    offset: int = 0,
    **kwargs: Any,
) -> str:
    """Retrieve durable memory from Engram. Never raises."""
    op = str(op or "search").strip().lower()
    try:
        client = _client(ctx)
    except Exception as exc:
        return (
            f"ENGRAM NOT CONFIGURED — could not resolve a project scope for this repo "
            f"({type(exc).__name__}). Reading memory is unavailable this turn; that is a "
            "configuration fact, not an empty result."
        )

    used = turn_usage(ctx)
    if used.get("chars", 0) >= MAX_TURN_OUTPUT_CHARS:
        return _budget_refusal(ctx, op=op)

    if op == "search":
        if not str(query or "").strip():
            return "op='search' needs a non-empty `query`."
        result = client.search(str(query), limit=max(1, min(int(limit or 10), MAX_SEARCH_LIMIT)))
        if not result.ok:
            return _unavailable(result)
        items = result.items()
        if not items:
            return f"No candidate memories for {query!r} in project {client.config.project!r}."
        lines = [_one_line(item) for item in items[:MAX_SEARCH_LIMIT]]
        out = _bounded(
            [f"{len(items)} candidate(s) in project {client.config.project!r} "
             "(titles only — read one to see its body):", *lines],
            MAX_SEARCH_OUTPUT_CHARS,
        )
        _spend(ctx, chars=len(out))
        return out

    if op == "timeline":
        if not int(observation_id or 0):
            return "op='timeline' needs an `observation_id` (from a search hit)."
        result = client.timeline(
            int(observation_id),
            before=max(0, min(int(before or 5), 10)),
            after=max(0, min(int(after or 5), 10)),
        )
        if not result.ok:
            if getattr(result, "unavailable", False):
                return _unavailable(result)
            # Not a transport failure: name the project this client asked IN and the
            # route that can still reach the record, so the reader never has to guess
            # whether the id or the scope was wrong.
            return (
                f"ENGRAM TIMELINE MISS — observation {int(observation_id)} is not in project "
                f"{client.config.project!r} (status={getattr(result, 'status', None)}): "
                f"{str(getattr(result, 'detail', '') or '')[:200]} "
                "Re-find it with op='search' (a search can match records outside this project), "
                "then op='read' the id it returns."
            )
        items = result.items()
        if not items:
            return f"No timeline around observation {observation_id}."
        out = _bounded(
            [f"Neighbourhood of observation {observation_id}:", *[_one_line(i) for i in items]],
            MAX_TIMELINE_OUTPUT_CHARS,
        )
        _spend(ctx, chars=len(out))
        return out

    if op == "read":
        if not int(observation_id or 0):
            return "op='read' needs an `observation_id` (from a search hit)."
        # AC23(c): a turn may pull a bounded number of FULL records. Refusing here
        # is the honest failure: silently truncating would hand back a partial
        # record that reads like a complete one.
        if used.get("reads", 0) >= MAX_READS_PER_TURN:
            return _budget_refusal(ctx, op="read")
        result = client.get(int(observation_id))
        if not result.ok:
            return _unavailable(result)
        record = result.one()
        if not record:
            return f"Observation {observation_id} not found."
        content = str(record.get("content") or "")
        total = len(content)
        start = max(0, int(offset or 0))
        if start and start >= total:
            return (
                f"offset {start} is past the end of observation {int(observation_id)} "
                f"({total} chars) — nothing further to read."
            )
        window = content[start:start + MAX_READ_CONTENT_CHARS]
        pieces = []
        if start:
            pieces.append(f"[continued from char {start} of {total}]")
        pieces.append(window)
        following = start + len(window)
        if following < total:
            # The record is here in full: the bound is a display bound, so the note
            # names where to resume rather than just saying the text stopped.
            pieces.append(
                f"…[truncated at char {following} of {total} — continue with op='read' "
                f"offset={following}]"
            )
        content = "\n".join(pieces)
        out = _bounded(
            [
                _one_line(record),
                f"project: {record.get('project')}  scope: {record.get('scope')}  "
                f"updated: {record.get('updated_at')}",
                "",
                content,
            ],
            MAX_READ_CONTENT_CHARS + 400,
        )
        _spend(ctx, chars=len(out), reads=1)
        return out

    return f"Unknown op {op!r}. Use one of: search, timeline, read."


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="engram",
            schema={
                "name": "engram",
                "description": (
                    "Retrieve durable memory from Engram. Durable knowledge and consolidated "
                    "history are NOT in your prompt any more; they live here and you fetch them "
                    "when you decide they matter. Progressive disclosure — use the cheapest layer "
                    "that answers the question: op='search' finds candidates by keyword and returns "
                    "TITLES ONLY; op='timeline' shows the neighbourhood of one hit so you can judge "
                    "it; op='read' returns exactly ONE full record. Each layer is bounded, and one "
                    "retrieval must never cost more than the prompt section it stands in for. "
                    "If Engram is unreachable you get a typed UNAVAILABLE notice — that is NOT "
                    "'no relevant memory'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["search", "timeline", "read"],
                            "description": "search = find candidates (titles); timeline = neighbourhood of one hit; read = one full record.",
                        },
                        "query": {
                            "type": "string",
                            "description": "op='search': keywords to search for.",
                        },
                        "observation_id": {
                            "type": "integer",
                            "description": "op='timeline' | 'read': the id from a search hit.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "op='search': max candidates (default 10, max 20).",
                            "default": 10,
                        },
                        "before": {
                            "type": "integer",
                            "description": "op='timeline': records before the hit (default 5, max 10).",
                            "default": 5,
                        },
                        "after": {
                            "type": "integer",
                            "description": "op='timeline': records after the hit (default 5, max 10).",
                            "default": 5,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "op='read': character offset to continue a truncated record — pass the exact offset named in the truncation note.",
                            "default": 0,
                        },
                    },
                    "required": ["op"],
                },
            },
            handler=_engram,
            timeout_sec=15,
        ),
    ]
