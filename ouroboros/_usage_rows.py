"""Re-export shim: row-math projections now live in ``ouroboros.usage_rows``.
This module is retained only so historical import sites keep resolving until
the underscore-prefixed module is deleted."""

from ouroboros.usage_rows import (
    _SKILL_ATTEMPT_FIELDS,
    REVIEW_ATTRIBUTION_KEYS,
    _breakdown_bucket,
    _physical_call_count,
    _skill_review_usage_bucket,
    _summary,
    _with_integrity,
    _with_limit,
)

__all__ = [
    "REVIEW_ATTRIBUTION_KEYS",
    "_summary",
    "_with_limit",
    "_with_integrity",
    "_physical_call_count",
    "_breakdown_bucket",
    "_SKILL_ATTEMPT_FIELDS",
    "_skill_review_usage_bucket",
]
