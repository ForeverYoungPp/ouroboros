"""Shared error types for Ouroboros.

This module holds exceptions that outlive the capabilities they describe, so a
deleted subsystem can never take its own error type down with it: the type has
to be importable from somewhere that survives, or every ``except`` clause that
names it becomes an ImportError of its own.
"""

from __future__ import annotations


class OuroborosUnavailableError(RuntimeError):
    """A capability this build no longer has was asked to do work.

    Raised instead of silently degrading: the caller learns the capability is
    gone, rather than reading an empty result as an honest zero. HTTP surfaces
    answer 503 with the same vocabulary through
    :func:`ouroboros.gateway._helpers.json_error`; tool surfaces return the
    equivalent ``{"status": "unavailable", "reason": ...}`` marker.
    """
