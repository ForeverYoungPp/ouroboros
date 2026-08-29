"""Strict settings-snapshot integrity pin (benchmark isolation trust root).

An isolated benchmark server seeds its child with an exact settings snapshot
and pins its sha256 in ``OUROBOROS_SETTINGS_SHA256``: while the pin is
present the snapshot is an owner-authored trust root — every read verifies
the complete byte stream and every writer refuses. Extracted from
``config.py`` (which re-exports the public names, so every existing
``config.X`` import keeps working) to keep the SSOT module inside its size
ratchet.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

SETTINGS_INTEGRITY_ENV = "OUROBOROS_SETTINGS_SHA256"


class SettingsIntegrityError(RuntimeError):
    """The settings snapshot changed or became unreadable under a strict pin."""


def guard_settings_snapshot_mutation() -> None:
    """Refuse every settings writer while a benchmark snapshot is pinned."""
    if os.environ.get(SETTINGS_INTEGRITY_ENV):
        raise SettingsIntegrityError("strict isolated settings snapshot is immutable")


def guard_live_settings_write(settings_path: Path, home: Path) -> None:
    """Every settings-write precondition: the snapshot pin, then the pytest
    guard on the LIVE settings file (an isolated test root passes through)."""
    guard_settings_snapshot_mutation()
    if os.environ.get("OUROBOROS_ALLOW_LIVE_DATA_TESTS") == "1":
        return
    try:
        live_settings = settings_path.resolve(strict=False) == (
            home / "Ouroboros" / "data" / "settings.json"
        ).resolve(strict=False)
    except OSError:
        live_settings = False
    if ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules) and live_settings:
        raise RuntimeError(
            "Refusing to write live Ouroboros settings.json from pytest. "
            "Set OUROBOROS_SETTINGS_PATH/OUROBOROS_DATA_DIR to a temp path, "
            "or OUROBOROS_ALLOW_LIVE_DATA_TESTS=1 for an explicit live-data test."
        )


def expected_settings_sha256() -> str:
    value = str(os.environ.get(SETTINGS_INTEGRITY_ENV, "") or "").strip().lower()
    if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
        raise SettingsIntegrityError("settings integrity digest is malformed")
    return value


def read_settings_bytes_verified(settings_path: Path) -> bytes | None:
    """Read one stable file descriptor and verify its complete byte stream."""
    expected = expected_settings_sha256()
    try:
        with settings_path.open("rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        if expected:
            raise SettingsIntegrityError("settings snapshot is missing") from None
        return None
    except OSError as exc:
        if expected:
            raise SettingsIntegrityError("settings snapshot is unreadable") from exc
        return None
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise SettingsIntegrityError("settings snapshot changed")
    return raw


def read_settings_json_verified(settings_path: Path):
    """Decode the verified snapshot; under a pin a decode failure is typed.

    Returns the parsed JSON value (any type) or ``None`` for an absent or —
    without a pin — unreadable/undecodable file. One helper so the two
    config.py read paths cannot drift apart in their pin semantics.
    """
    raw_bytes = read_settings_bytes_verified(settings_path)
    if raw_bytes is None:
        return None
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if expected_settings_sha256():
            raise SettingsIntegrityError("settings snapshot is unreadable") from exc
        return None


def verify_settings_integrity(settings_path: Path) -> str | None:
    """Verify the strict child pin, returning the observed digest when present."""
    raw = read_settings_bytes_verified(settings_path)
    return hashlib.sha256(raw).hexdigest() if raw is not None else None
