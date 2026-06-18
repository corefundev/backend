"""
R10-S2 regression — column-name SQL-injection guard on registry.update().

PostgresClientRegistry.update() / AsyncClientRegistry.update() build their
SET clause by interpolating **fields KEYS into the SQL string as column
names. Values are parameterised; column names cannot be. Before R10-S2
any caller that passed an attacker-influenced key opened a full SQL
injection in the position of a column name.

These tests pin the contract: every key must be a known sku_clients
column (CLIENT_UPDATABLE_COLUMNS) or update() raises ValueError BEFORE
building or executing any SQL.
"""
from __future__ import annotations

import pytest

from src.clients.registry import (
    CLIENT_UPDATABLE_COLUMNS,
    PostgresClientRegistry,
)


# ── Fakes — let update() run to completion without a real database ──────

class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()
        self.committed = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _registry() -> PostgresClientRegistry:
    # __init__ is connection-free — it only imports psycopg2 and stores
    # the URL — so a bogus DSN is fine for unit testing.
    return PostgresClientRegistry("postgresql://fake:fake@localhost/fake")


# ── The allowlist itself ───────────────────────────────────────────────

def test_allowlist_excludes_primary_key_and_immutable_column():
    # client_id is the PK (WHERE target, never a SET target); created_at
    # is immutable. Neither may be updatable.
    assert "client_id" not in CLIENT_UPDATABLE_COLUMNS
    assert "created_at" not in CLIENT_UPDATABLE_COLUMNS


def test_allowlist_covers_every_real_caller_kwarg():
    # The union of kwargs every production caller of registry.update()
    # passes (verified by grep across src/ during the R10 audit).
    real_caller_kwargs = {
        "config", "status", "api_key_hash", "plan", "horizon", "notes",
        "oauth_provider", "oauth_subject",
        "last_trained_at", "last_mlflow_run_id", "last_wmape", "last_mase",
        "model_version", "trained_sku_count", "storage_path",
        "email", "email_canonical",
        "email_verified_at",
    }
    missing = real_caller_kwargs - CLIENT_UPDATABLE_COLUMNS
    assert not missing, f"allowlist would break real callers: {missing}"


# ── Injection rejection ────────────────────────────────────────────────

@pytest.mark.parametrize("evil_column", [
    "status = 'x'; DROP TABLE sku_clients; --",
    "notes = (SELECT api_key_hash FROM sku_clients)",
    "horizon\" = 1, \"status",
    "plan",  # control: real column — must NOT be rejected (see below)
])
def test_update_rejects_injection_column_names(evil_column):
    reg = _registry()
    if evil_column in CLIENT_UPDATABLE_COLUMNS:
        pytest.skip("control row — real column, covered by happy-path test")
    # Must raise BEFORE any DB connection is attempted.
    with pytest.raises(ValueError, match="non-allowlisted column"):
        reg.update("client-1", **{evil_column: "payload"})


def test_update_rejects_unknown_but_plausible_column():
    reg = _registry()
    with pytest.raises(ValueError, match="non-allowlisted column"):
        reg.update("client-1", is_admin=True)


# ── Happy path — real columns build a safe parameterised query ─────────

def test_update_with_real_columns_builds_parameterised_sql():
    reg = _registry()
    fake = _FakeConn()
    reg._conn = lambda: fake  # type: ignore[method-assign]

    reg.update("client-1", status="ready", horizon=30)

    assert fake.committed is True
    sql, params = fake.cur.calls[0]
    # Column names are fixed literals; values are %s placeholders only.
    assert sql == (
        "UPDATE sku_clients SET status = %s, horizon = %s "
        "WHERE client_id = %s"
    )
    assert params == ["ready", 30, "client-1"]
    # No injected payload ever reaches the SQL text.
    assert ";" not in sql and "DROP" not in sql.upper()


def test_update_with_no_fields_is_a_noop():
    reg = _registry()
    fake = _FakeConn()
    reg._conn = lambda: fake  # type: ignore[method-assign]
    reg.update("client-1")  # no kwargs
    assert fake.cur.calls == []
    assert fake.committed is False
