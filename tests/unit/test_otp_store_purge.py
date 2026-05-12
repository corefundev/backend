"""
Tests for src.auth.otp_store.OtpStore.purge_old — guards against
the SQL-injection foothold that existed before audit 2026-05-13.

The DB path can't be exercised here without Postgres, so we test:
  • The input validation that rejects non-int / negative inputs
    (defense in depth — the SQL placeholder is the primary fix).
  • The file-backed fallback's behaviour with assorted inputs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.auth.otp_store import LocalFileOtpStore


# ── Input validation on the SQL path ─────────────────────────────────────
#
# The validation lives in the Postgres impl (PostgresOtpStore.purge_old).
# We can construct an instance with a no-op connect — the validation
# raises BEFORE the SQL ever runs.

class _DummyPg:
    """PostgresOtpStore subclass that calls validation only, never DB."""

    def __init__(self):
        # Skip the parent __init__ to avoid pulling in psycopg/asyncpg
        # imports and a real DSN. We're only testing the input guard
        # which lives at the top of purge_old.
        pass

    # Inherit purge_old from PostgresOtpStore via duck-typing
    purge_old = None  # placeholder


def _get_pg_purge_old():
    """Pull the real method off the class so we can call it with a mock."""
    from src.auth import otp_store as mod
    return mod.PostgresOtpStore.purge_old


def test_purge_rejects_negative_hours():
    pg = _DummyPg()
    method = _get_pg_purge_old()
    with pytest.raises(ValueError, match="non-negative"):
        method(pg, older_than_hours=-1)


def test_purge_rejects_non_int_hours():
    pg = _DummyPg()
    method = _get_pg_purge_old()
    with pytest.raises(ValueError, match="non-negative"):
        method(pg, older_than_hours=24.5)  # float


def test_purge_rejects_string_hours():
    pg = _DummyPg()
    method = _get_pg_purge_old()
    # The historic injection vector: a string like "24; DROP TABLE …"
    with pytest.raises(ValueError, match="non-negative"):
        method(pg, older_than_hours="24")  # type: ignore[arg-type]


def test_purge_rejects_sql_injection_attempt():
    pg = _DummyPg()
    method = _get_pg_purge_old()
    # The classic injection payload — must be rejected at the type
    # guard, never reaching the SQL layer.
    with pytest.raises(ValueError):
        method(pg, older_than_hours="24'; DROP TABLE sku_otp_codes; --")  # type: ignore[arg-type]


# ── File-backed fallback: actual purge behavior ──────────────────────────

def _make_local_store(tmp_path: Path) -> LocalFileOtpStore:
    p = tmp_path / "otp.json"
    # LocalFileOtpStore auto-creates the file if missing, so we let it.
    return LocalFileOtpStore(str(p))


def test_local_purge_removes_only_old_rows(tmp_path):
    store = _make_local_store(tmp_path)
    now = datetime.now(timezone.utc)
    # Write three rows directly: 1d old, 12h old, 1h old.
    state_path = store._path  # type: ignore[attr-defined]
    state = json.loads(state_path.read_text())
    for hours_ago, label in [(25, "stale"), (12, "boundary"), (1, "fresh")]:
        state["rows"].append({
            "id": label,
            "email": f"{label}@example.com",
            "purpose": "login",
            "code_hash": "h",
            "attempts": 0,
            "expires_at": now.isoformat(),
            "created_at": (now - timedelta(hours=hours_ago)).isoformat(),
        })
    state_path.write_text(json.dumps(state))

    removed = store.purge_old(older_than_hours=24)
    assert removed == 1  # only the 25h-old row

    remaining_ids = {r["id"] for r in json.loads(state_path.read_text())["rows"]}
    assert remaining_ids == {"boundary", "fresh"}
