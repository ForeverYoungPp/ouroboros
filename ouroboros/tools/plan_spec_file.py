"""``spec_file`` support for ``plan_task`` (ibl-5f3d7d49634e).

The failure class that created this module: a LARGE inline ``spec`` (roughly
4-5KB of generated JSON) is emitted by the model as one tool-call ``arguments``
string, and the model/endpoint occasionally appends a second JSON object or
trailing text after the first complete object, which then gets cut around
~4850 chars. The executor's ``json.loads`` sees "a complete object + more
content" and reports ``Extra data: line 1 column 4851`` — the classic sign of a
complete object followed by a truncated second one, not of a single mid-string
cut. The structural fix is to stop trusting one huge generated arguments
string: the agent writes the full SPEC to a JSON file under the active
workspace / system repository and passes its path as ``spec_file``. The host
reads the file verbatim, parses it, and feeds the resulting spec through the
EXACT same ``normalize_spec`` / fingerprint / evidence pipeline as an inline
``spec`` — so the review authority is byte-identical, only the transport
changed.

Pure-ish helpers, no reviewer logic. The caller (``ouroboros.tools.plan_review``)
owns the envelope-mode split and the inline-size pre-check.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ouroboros.tools import plan_spec


def plan_inline_spec_chars(spec: Any) -> int:
    """Serialized size of the inline ``spec`` envelope (used for the friendly pre-check).

    A spec above ``PLAN_SPEC_INLINE_LIMIT_CHARS`` is refused with a typed, actionable
    error (naming the ``spec_file`` usage) instead of being sent as a huge single
    tool-call argument — the failure class behind ibl-5f3d7d49634e. The bound is
    deliberately conservative: the observed failure cut complete generated JSON around
    ~4850 chars, so anything in that band must not be trusted to arrive intact.
    """
    if not isinstance(spec, dict):
        return 0
    try:
        return len(json.dumps(spec, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return 0


# Conservative inline limit (ibl-5f3d7d49634e): the observed failure cut complete
# tool-call argument JSON at ~4850 chars; anything in this band must be routed to
# ``spec_file`` instead of trusting one generated arguments string to arrive intact.
PLAN_SPEC_INLINE_LIMIT_CHARS = 4_000


def load_spec_file(ctx: Any, spec_file: str) -> dict | str:
    """Resolve and read a ``spec_file`` locator into a parsed spec dict.

    Paths resolve against the active workspace (like evidence locators); absolute
    ``file://`` paths are honored as-is. The file must live under the active
    workspace or the system repository (the same allowed roots evidence uses), so a
    spec_file can never smuggle an arbitrary host path into review. Returns the
    parsed dict on success, or an ``ERROR: PLAN_SPEC_FILE_*`` string on failure —
    the caller treats any ``ERROR:``-prefixed return as a typed refusal.
    """
    spec_file = str(spec_file or "").strip()
    if not spec_file:
        return "ERROR: PLAN_SPEC_FILE_INVALID: spec_file must be a non-empty string"
    from ouroboros.review_substrate import review_repo_dirs_for
    try:
        system_root, active_root = review_repo_dirs_for(ctx)
    except ValueError as exc:
        return f"ERROR: PLAN_SUBJECT_ROOT_INVALID: {exc}"
    active = pathlib.Path(active_root).resolve(strict=False)
    system = pathlib.Path(system_root).resolve(strict=False)
    candidate, reason = plan_spec._resolve_locator_path(spec_file, active)
    if candidate is None:
        return f"ERROR: PLAN_SPEC_FILE_INVALID: cannot resolve {spec_file!r}: {reason}"
    if not (plan_spec._under(candidate, active) or plan_spec._under(candidate, system)):
        return (
            f"ERROR: PLAN_SPEC_FILE_OUTSIDE_ROOTS: {spec_file!r} must live under the active "
            "workspace or the system repository"
        )
    try:
        if not candidate.is_file():
            return f"ERROR: PLAN_SPEC_FILE_MISSING: {spec_file!r} is not a regular file"
        raw = candidate.read_text(encoding="utf-8")
    except UnicodeError:
        return f"ERROR: PLAN_SPEC_FILE_UNDECODABLE: {spec_file!r} is not valid UTF-8"
    except OSError as exc:
        return f"ERROR: PLAN_SPEC_FILE_UNREADABLE: {spec_file!r}: {exc}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            f"ERROR: PLAN_SPEC_FILE_INVALID_JSON: {spec_file!r} is not valid JSON: "
            f"{exc}. The file must contain a JSON object matching the spec schema."
        )
    if not isinstance(parsed, dict):
        return (
            f"ERROR: PLAN_SPEC_FILE_INVALID_JSON: {spec_file!r} must contain a JSON object, "
            "not an array or scalar"
        )
    return parsed
