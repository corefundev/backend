#!/bin/sh
# scripts/postgres_exporter_entrypoint.sh
#
# Same shape as alertmanager's wrapper: optional Lockbox bootstrap, then
# exec the real binary. Removes DATABASE_URL from /srv/backend/.env —
# the postgres password no longer has to live on disk in plaintext.
#
# In Lockbox-mode (prod):
#   YC_SA_KEY_FILE + YC_LOCKBOX_SECRET_ID set, LOCKBOX_ALLOWED_KEYS
#   exports POSTGRES_PASSWORD + DB_HOST + DB_PORT + DB_NAME + DB_USER.
#   The DSN is composed at runtime from these components.
#
# In dev (no Lockbox):
#   set DATABASE_URL directly via env, OR set all the components.
#
# R10 Phase 0-B PR-C (2026-05-31) — the composed DATABASE_URL entry was
# dropped from Lockbox. POSTGRES_PASSWORD is the only source of truth.
# See src/auth/vault_agent.py::_compose_database_urls_from_components
# for the Python-side mirror of this logic.

set -eu

if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${PGEXP_BOOTED:-}" ]; then
    export PGEXP_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# If DATABASE_URL is not provided directly but component env vars
# ARE (POSTGRES_PASSWORD + DB_HOST + DB_PORT + DB_NAME + DB_USER),
# compose the DSN here. Mirrors vault_agent.py's
# `_compose_database_urls_from_components` on the Python side.
if [ -z "${DATABASE_URL:-}" ] && [ -n "${POSTGRES_PASSWORD:-}" ]; then
    _db_user="${DB_USER:-sku}"
    _db_host="${DB_HOST:-postgres}"
    _db_port="${DB_PORT:-5432}"
    _db_name="${DB_NAME:-sku_forecasting}"
    DATABASE_URL="postgresql://${_db_user}:${POSTGRES_PASSWORD}@${_db_host}:${_db_port}/${_db_name}"
    export DATABASE_URL
    echo "postgres_exporter_entrypoint: DATABASE_URL composed from components (user=${_db_user} host=${_db_host} port=${_db_port} db=${_db_name})" >&2
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "postgres_exporter_entrypoint: DATABASE_URL is empty AND no components for fallback compose." >&2
    echo "  In Lockbox-mode: ensure POSTGRES_PASSWORD + DB_HOST + DB_PORT + DB_NAME + DB_USER are in '${YC_LOCKBOX_SECRET_ID:-<secret>}' AND in LOCKBOX_ALLOWED_KEYS." >&2
    echo "  In dev-mode:     export DATABASE_URL (or all components) before starting." >&2
    exit 2
fi

# postgres-exporter wants DATA_SOURCE_NAME, not DATABASE_URL. Ours runs
# against an internal-network postgres without TLS, so sslmode=disable
# is correct.
export DATA_SOURCE_NAME="${DATABASE_URL}?sslmode=disable"

exec /bin/postgres_exporter "$@"
