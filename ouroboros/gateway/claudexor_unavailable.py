"""Explicit-unavailable shims for the Claudexor harness routes.

The owned-Claudexor daemon, runtime, and gateway modules are slated for
whole-module deletion, but this stage does not remove endpoints:
``collect_routes()`` keeps every route (so ``endpoint_index.py`` parity and the
route count stay unchanged). Each handler therefore returns the repository's
existing 503 unavailable code explicitly — the harness backend is gone, and
"unavailable" is the honest answer, not a silently empty 200.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_error

_UNAVAILABLE = "Claudexor harness routing unavailable"


async def api_claudexor_status(request: Request) -> JSONResponse:
    """GET /api/claudexor/status — the owned harness backend is unavailable."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_wake(request: Request) -> JSONResponse:
    """POST /api/claudexor/wake — the owned harness backend is unavailable."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_login(request: Request) -> JSONResponse:
    """POST /api/claudexor/login — the owned harness backend is unavailable."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_login_job(request: Request) -> JSONResponse:
    """GET/DELETE /api/claudexor/login/{job_id} and POST …/{job_id}/input."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_login_job_reconcile(request: Request) -> JSONResponse:
    """POST /api/claudexor/login/{job_id}/reconcile — harness unavailable."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_credential_profile(request: Request) -> JSONResponse:
    """DELETE/PATCH /api/claudexor/credential-profiles/{harness}/{profile_id}."""
    return json_error(_UNAVAILABLE, 503)


async def api_claudexor_quota_refresh(request: Request) -> JSONResponse:
    """POST /api/claudexor/quota/refresh — the owned harness is unavailable."""
    return json_error(_UNAVAILABLE, 503)


__all__ = [
    "api_claudexor_status",
    "api_claudexor_wake",
    "api_claudexor_login",
    "api_claudexor_login_job",
    "api_claudexor_login_job_reconcile",
    "api_claudexor_credential_profile",
    "api_claudexor_quota_refresh",
]
