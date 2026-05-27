"""
tests/integration/test_placeholder_pg_auth_rejected.py

R10 Phase 0-C PR-4 — property test: the structural placeholder value
that lives in `/srv/backend/.env`'s POSTGRES_PASSWORD MUST NEVER be
acceptable as a real postgres password. This is the regression-
protection layer for the Phase 0-C placeholder design.

Why this test exists:

  The R3-13 design (since deprecated) used `sku` as the placeholder
  seed in .env. `sku` was a real-and-historical postgres password
  before the first rotation, and the boundary between "placeholder"
  and "real value" was operational discipline alone — nothing in code
  prevented a misconfigured Lockbox bootstrap from letting the
  placeholder seep through to auth attempts.

  Phase 0-C swaps the placeholder to
  `_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD` — deliberately shaped
  so no random-pwd generator would ever produce it. This test asserts
  the structural property: even if some future code path accidentally
  uses the placeholder as an actual postgres auth credential, postgres
  rejects it with `OperationalError` (SCRAM auth failure). The test
  uses the postgres service container that CI's `integration-tests`
  job already spins up (`POSTGRES_USER=sku`, `POSTGRES_PASSWORD=sku`,
  `POSTGRES_DB=sku_test`). Locally the test is skipped unless that
  postgres is reachable.

  See:
    .env.example                                  — placeholder value
    docker/docker-compose.yml                     — strict-gate error msg
    scripts/mlflow_entrypoint.sh                  — FATAL on placeholder
    scripts/cd_deploy.sh R3-13/R10 Phase 0-C NOTE — full design
"""
from __future__ import annotations

import os
import socket

import pytest


# The canonical placeholder string. Must match:
#   * `.env.example` `POSTGRES_PASSWORD=…` line
#   * `docker/docker-compose.yml` strict-gate error message
#   * `scripts/mlflow_entrypoint.sh` placeholder-rejection case
#   * `scripts/cd_deploy.sh` operator-bootstrap NOTE
# A unit test in `tests/lint/` cross-checks this constant against the
# real files so they can't drift apart silently.
PG_PASSWORD_PLACEHOLDER = "_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD"


def _postgres_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Quick TCP probe — skip the test cleanly when there's no postgres
    on the expected port (local dev without docker-compose up)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def pg_endpoint():
    """Resolve postgres host:port from the CI service env, falling back
    to localhost:5432 for local runs. Skip if no postgres is reachable."""
    # CI integration-tests job exposes postgres via DATABASE_URL=
    # postgresql://sku:sku@localhost:5432/sku_test
    host = os.environ.get("PG_HOST", "localhost")
    port = int(os.environ.get("PG_PORT", "5432"))
    if not _postgres_reachable(host, port):
        pytest.skip(
            f"postgres not reachable at {host}:{port} — run via CI "
            f"integration-tests job or `docker compose up -d postgres`"
        )
    return host, port


def test_placeholder_string_is_well_known():
    """Sanity: the placeholder string has the documented prefix.
    Doubles as a guard against accidental rename in just one place."""
    assert PG_PASSWORD_PLACEHOLDER.startswith("_LOCKBOX_NOT_INJECTED_"), (
        f"placeholder must start with _LOCKBOX_NOT_INJECTED_ so any "
        f"reader recognises it as 'placeholder, not real pwd' — got "
        f"{PG_PASSWORD_PLACEHOLDER!r}"
    )
    # And the suffix carries enough context to identify which secret.
    assert "PG_PASSWORD" in PG_PASSWORD_PLACEHOLDER, (
        "placeholder must name the secret it stands in for"
    )


def test_postgres_rejects_placeholder_as_password(pg_endpoint):
    """The KEY property: a connection attempt using the placeholder
    string as the postgres password MUST fail with an auth error.

    The CI postgres service has user=sku with password=sku, so this
    test verifies postgres rejects ANY value that isn't `sku` —
    specifically the placeholder. If a future operator slip set
    postgres's real password to the placeholder value, this test
    fails and blocks the PR."""
    host, port = pg_endpoint
    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 not installed in this environment")

    with pytest.raises(psycopg2.OperationalError) as excinfo:
        psycopg2.connect(
            host=host,
            port=port,
            user="sku",
            dbname="sku_test",
            password=PG_PASSWORD_PLACEHOLDER,
            connect_timeout=5,
        )

    # The error must be an authentication failure, not a network /
    # database-missing / other class — that would mean the test
    # didn't actually exercise the auth gate.
    err = str(excinfo.value).lower()
    assert (
        "password authentication failed" in err
        or "sasl authentication failed" in err
        or "authentication" in err
    ), (
        f"expected auth-failure on placeholder, got different error: "
        f"{excinfo.value!r}"
    )


def test_postgres_accepts_real_password_for_control(pg_endpoint):
    """Control: a connection with the ACTUAL CI postgres password
    (`sku`) MUST succeed. Without this, the previous test could pass
    trivially because of network/config failure, defeating the point.
    """
    host, port = pg_endpoint
    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 not installed in this environment")

    # CI integration-tests sets POSTGRES_PASSWORD=sku on the service
    # container. If this test fails, the CI service config drifted —
    # not a Phase 0-C bug.
    conn = psycopg2.connect(
        host=host,
        port=port,
        user="sku",
        dbname="sku_test",
        password="sku",
        connect_timeout=5,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    finally:
        conn.close()
