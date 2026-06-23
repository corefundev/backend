"""
tests/integration/test_audit_append_only.py

R13-3 / #144 — DB-level append-only enforcement (migration 015) plus the
verify_chain no-gap detection. These are DB-coupled, so they run against the
postgres service the integration job spins up (DATABASE_URL); skipped locally
when no DB is reachable. The connecting role OWNS audit_log (same as prod),
so this also proves the trigger — not a REVOKE — is what enforces append-only.

Properties locked in:
  • the BEFORE UPDATE/DELETE trigger blocks UPDATE and DELETE on audit_log,
  • prune_audit_log() still deletes old rows (its txn-local flag passes the
    trigger),
  • verify_chain flags a row deleted inside the signed-id span (id_gap) — the
    hole a deleted UNSIGNED row leaves, which the HMAC chain cannot see.
"""
from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="integration test needs DATABASE_URL (CI postgres service)",
)


@pytest.fixture
def conn():
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def _insert_event(cur, event_type="r13_3_trig"):
    # Unsigned row: the trigger blocks UPDATE/DELETE regardless of signature,
    # and leaving signature NULL keeps these rows out of the verify_chain walk
    # so they don't pollute the chain the no-gap test builds.
    cur.execute(
        "INSERT INTO audit_log (event_type, success) VALUES (%s, TRUE) RETURNING id",
        (event_type,),
    )
    return cur.fetchone()[0]


# ── append-only trigger ────────────────────────────────────────────────────

def test_trigger_blocks_update(conn):
    with conn.cursor() as cur:
        rid = _insert_event(cur)
        with pytest.raises(psycopg2.Error) as ei:
            cur.execute("UPDATE audit_log SET success = FALSE WHERE id = %s", (rid,))
        assert "append-only" in str(ei.value).lower()


def test_trigger_blocks_delete(conn):
    with conn.cursor() as cur:
        rid = _insert_event(cur)
        with pytest.raises(psycopg2.Error) as ei:
            cur.execute("DELETE FROM audit_log WHERE id = %s", (rid,))
        assert "append-only" in str(ei.value).lower()


def test_prune_deletes_old_rows_through_trigger(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (ts, event_type, success) "
            "VALUES (NOW() - INTERVAL '800 days', 'r13_3_old', TRUE) RETURNING id"
        )
        old_id = cur.fetchone()[0]
        cur.execute("SELECT prune_audit_log(730, 10000)")
        assert cur.fetchone()[0] >= 1            # at least our old row deleted
        cur.execute("SELECT count(*) FROM audit_log WHERE id = %s", (old_id,))
        assert cur.fetchone()[0] == 0            # the trigger let prune through


# ── verify_chain no-gap ─────────────────────────────────────────────────────

def test_verify_chain_flags_deleted_row_in_span(conn, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "11" * 32)
    from src.audit import log as audit

    # Two real signed rows (chained) with a deletable UNSIGNED row between them.
    audit.record_event(event_type="r13_3_chain_a")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (event_type, success) "
            "VALUES ('r13_3_unsigned', TRUE) RETURNING id"
        )
        gap_id = cur.fetchone()[0]
    audit.record_event(event_type="r13_3_chain_b")

    # The HMAC chain a→b is intact; the unsigned row sits inside the span and is
    # still present, so there is no gap yet.
    assert audit.verify_chain().reason != "id_gap"

    # Simulate a privileged tamper: delete the unsigned middle row past the
    # trigger via the same flag prune uses (dedicated txn, session SET). The
    # chain a→b stays valid — only the span row-count drops, which is exactly
    # what the no-gap check catches.
    tamper = psycopg2.connect(DATABASE_URL)
    try:
        with tamper.cursor() as cur:
            cur.execute("SET audit_log.allow_prune = 'on'")
            cur.execute("DELETE FROM audit_log WHERE id = %s", (gap_id,))
        tamper.commit()
    finally:
        tamper.close()

    assert audit.verify_chain().reason == "id_gap"
