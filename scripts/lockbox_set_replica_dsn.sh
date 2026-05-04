#!/bin/bash
# scripts/lockbox_set_replica_dsn.sh
#
# Idempotent helper to set DATABASE_URL_REPLICA in YC Lockbox correctly.
# Resolves the password from the existing POSTGRES_PASSWORD entry,
# scrubs whitespace, removes any prior DATABASE_URL_REPLICA entry,
# adds a fresh one. Safe to re-run.
#
# Usage (just run it, no args):
#   ./scripts/lockbox_set_replica_dsn.sh
#
# Requires: yc CLI authenticated, access to the `sku-app-secrets` secret.

set -euo pipefail

SECRET_NAME="${SECRET_NAME:-sku-app-secrets}"
REPLICA_HOST="${REPLICA_HOST:-db-replica.testcore.ru}"
REPLICA_PORT="${REPLICA_PORT:-5432}"
DB_NAME="${DB_NAME:-sku_forecasting}"
DB_USER="${DB_USER:-sku}"

echo "→ Fetching POSTGRES_PASSWORD from Lockbox secret '$SECRET_NAME'…"
PG_PW=$(yc lockbox payload get --name "$SECRET_NAME" --key POSTGRES_PASSWORD --format text 2>/dev/null \
        | tr -d '[:space:]')

if [ -z "$PG_PW" ]; then
    echo "ERROR: POSTGRES_PASSWORD entry empty or missing in $SECRET_NAME." >&2
    echo "       Check: yc lockbox payload get --name $SECRET_NAME --key POSTGRES_PASSWORD" >&2
    exit 2
fi
echo "  password length: ${#PG_PW}  (sanity: should be > 8)"

DSN="postgresql://${DB_USER}:${PG_PW}@${REPLICA_HOST}:${REPLICA_PORT}/${DB_NAME}?sslmode=require"
# Sanity check the DSN we're about to store — surrounding quotes,
# unexpanded ${...}, embedded newlines all indicate a build error.
if [[ "$DSN" =~ ^[\'\"] ]] || [[ "$DSN" == *'${'* ]] || [[ "$DSN" == *$'\n'* ]]; then
    echo "ERROR: built DSN looks malformed — refusing to write." >&2
    echo "       len=${#DSN}, head=${DSN:0:30}…" >&2
    exit 3
fi

echo "→ Removing any prior DATABASE_URL_REPLICA entry (creates new secret version)…"
yc lockbox secret remove-entry --name "$SECRET_NAME" --key DATABASE_URL_REPLICA 2>/dev/null \
    || echo "  (no prior entry to remove — fine)"

echo "→ Writing fresh DATABASE_URL_REPLICA entry…"
yc lockbox secret add-entry --name "$SECRET_NAME" --key DATABASE_URL_REPLICA --text-value "$DSN"

echo "→ Verifying via Lockbox readback…"
STORED=$(yc lockbox payload get --name "$SECRET_NAME" --key DATABASE_URL_REPLICA --format text)
if [ "$STORED" != "$DSN" ]; then
    echo "WARN: stored value differs from intended (length stored=${#STORED}, intended=${#DSN})." >&2
fi

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  DATABASE_URL_REPLICA set correctly."
echo "  Now: trigger 'Restart App' workflow on GitHub Actions."
echo "═══════════════════════════════════════════════════════════════════"
