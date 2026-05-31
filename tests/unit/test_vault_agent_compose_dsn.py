"""
Tests for `src.auth.vault_agent._compose_database_urls_from_components`
— the R10 Phase 0-B refactor that makes POSTGRES_PASSWORD the single
source of truth instead of the duplicate-with-DATABASE_URL pattern that
triggered the R8-12 v1 SASL trap (2026-05-25).
"""
from __future__ import annotations

import importlib
import os

import pytest

from src.auth import vault_agent


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """Each test starts with a clean DB-related env slate."""
    for key in (
        "POSTGRES_PASSWORD",
        "POSTGRES_PASSWORD_REPLICA",
        "DB_USER", "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_USER_REPLICA", "DB_HOST_REPLICA", "DB_PORT_REPLICA",
        "DB_NAME_REPLICA", "DB_SSLMODE_REPLICA",
        "DATABASE_URL", "DATABASE_URL_REPLICA",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_compose_raises_when_password_missing(monkeypatch):
    """No POSTGRES_PASSWORD → RuntimeError.

    Behavior change in R10 Phase 0-B PR-C (2026-05-31): the previous
    silent-return-and-preserve-existing-DATABASE_URL fallback was
    removed. After this PR, POSTGRES_PASSWORD is the only source of
    truth — no composed-with-pwd DATABASE_URL entry exists in Lockbox
    to fall back to. A missing component is a misconfiguration that
    must surface immediately, not a state to silently accept.
    See [[feedback_enterprise_grade]] fail-closed-by-default.
    """
    # Even with a pre-existing DATABASE_URL, the composer raises —
    # because the pre-existing value cannot be trusted to match the
    # current POSTGRES_PASSWORD (the R8-12 v1 SASL trap precisely).
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:1/d")
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD is empty"):
        vault_agent._compose_database_urls_from_components()


def test_compose_raises_with_both_unset(monkeypatch):
    """Both POSTGRES_PASSWORD AND DATABASE_URL unset → RuntimeError.
    The error message must point at the resolution path so the operator
    can fix it without source-diving."""
    with pytest.raises(RuntimeError) as exc_info:
        vault_agent._compose_database_urls_from_components()
    msg = str(exc_info.value)
    assert "POSTGRES_PASSWORD" in msg, "error must name the missing var"
    assert "Lockbox" in msg, "error must point at Lockbox configuration"
    assert "LOCKBOX_ALLOWED_KEYS" in msg, "error must mention the allowlist gate"


def test_compose_main_from_components_with_defaults(monkeypatch):
    """POSTGRES_PASSWORD + no other components → defaults sku/postgres/5432/sku_forecasting."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret-pwd")
    vault_agent._compose_database_urls_from_components()
    assert os.environ["DATABASE_URL"] == \
        "postgresql://sku:secret-pwd@postgres:5432/sku_forecasting"


def test_compose_main_with_all_components(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("DB_USER", "appuser")
    monkeypatch.setenv("DB_HOST", "db.example.com")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "appdb")
    vault_agent._compose_database_urls_from_components()
    assert os.environ["DATABASE_URL"] == \
        "postgresql://appuser:p@db.example.com:5433/appdb"


def test_compose_overrides_existing_database_url(monkeypatch):
    """If POSTGRES_PASSWORD is set, composed value REPLACES whatever DATABASE_URL was there.

    This is the property that closes the R8-12 v1 SASL trap: a stale
    composed DATABASE_URL entry in Lockbox is ignored once the
    components are present + correct.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", "new-pwd")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://sku:OLD-PWD@postgres:5432/sku_forecasting",
    )
    vault_agent._compose_database_urls_from_components()
    assert os.environ["DATABASE_URL"] == \
        "postgresql://sku:new-pwd@postgres:5432/sku_forecasting"


def test_compose_skips_replica_when_no_host(monkeypatch):
    """Staging has no replica → DB_HOST_REPLICA empty → no _REPLICA composition."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    vault_agent._compose_database_urls_from_components()
    assert "DATABASE_URL_REPLICA" not in os.environ


def test_compose_replica_with_sslmode(monkeypatch):
    """Prod replica: ssl + dedicated host, all other components default to main values."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("DB_HOST_REPLICA", "replica.example.com")
    monkeypatch.setenv("DB_SSLMODE_REPLICA", "require")
    vault_agent._compose_database_urls_from_components()
    assert os.environ["DATABASE_URL_REPLICA"] == \
        "postgresql://sku:p@replica.example.com:5432/sku_forecasting?sslmode=require"


def test_compose_replica_uses_dedicated_components_when_present(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "main-pwd")
    monkeypatch.setenv("POSTGRES_PASSWORD_REPLICA", "replica-pwd")
    monkeypatch.setenv("DB_HOST_REPLICA", "r.example.com")
    monkeypatch.setenv("DB_PORT_REPLICA", "5499")
    monkeypatch.setenv("DB_NAME_REPLICA", "replica-db")
    monkeypatch.setenv("DB_USER_REPLICA", "replica-user")
    monkeypatch.setenv("DB_SSLMODE_REPLICA", "verify-full")
    vault_agent._compose_database_urls_from_components()
    assert os.environ["DATABASE_URL_REPLICA"] == \
        "postgresql://replica-user:replica-pwd@r.example.com:5499/replica-db?sslmode=verify-full"


def test_compose_url_encodes_special_chars_in_pwd(monkeypatch):
    """Defensive — current pwd policy is [A-Za-z0-9_-]{32} but a
    future rotation policy could include reserved URL chars. The
    composition must percent-encode so the DSN parses correctly."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:wd/foo")
    vault_agent._compose_database_urls_from_components()
    # `@`, `:`, `/` must be percent-encoded inside the user-info section,
    # otherwise the URL parser splits user@host@host etc.
    assert os.environ["DATABASE_URL"] == \
        "postgresql://sku:p%40ss%3Awd%2Ffoo@postgres:5432/sku_forecasting"


def test_compose_replica_password_falls_back_to_main(monkeypatch):
    """Today's topology: streaming replica uses the same user, so
    POSTGRES_PASSWORD_REPLICA is not separately stored. The compose
    function falls back to POSTGRES_PASSWORD."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "shared-pwd")
    monkeypatch.setenv("DB_HOST_REPLICA", "r.example.com")
    vault_agent._compose_database_urls_from_components()
    assert "shared-pwd" in os.environ["DATABASE_URL_REPLICA"]
