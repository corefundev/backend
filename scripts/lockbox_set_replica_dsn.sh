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

# Pick up yc from common install paths if it's not on PATH already.
# YC's installer drops it in ~/yandex-cloud/bin and adds it to shell
# rc, but a fresh non-login terminal might not have inherited that.
if ! command -v yc >/dev/null 2>&1; then
    for candidate in "$HOME/yandex-cloud/bin" /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$candidate/yc" ]; then
            export PATH="$candidate:$PATH"
            break
        fi
    done
fi
if ! command -v yc >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: yc CLI not found on PATH or in ~/yandex-cloud/bin.

Install:
  curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash
  exec \$SHELL -l

Then re-run this script.
EOF
    exit 1
fi

SECRET_NAME="${SECRET_NAME:-sku-forecasting-secrets}"
REPLICA_HOST="${REPLICA_HOST:-db-replica.testcore.ru}"
REPLICA_PORT="${REPLICA_PORT:-5432}"
DB_NAME="${DB_NAME:-sku_forecasting}"
DB_USER="${DB_USER:-sku}"

echo "→ Fetching POSTGRES_PASSWORD from Lockbox secret '$SECRET_NAME'…"
# Stash to a temp file so we can see stderr if yc fails. set -e would
# otherwise kill the script silently on any yc error.
YC_OUT=$(mktemp)
YC_ERR=$(mktemp)
set +e
yc lockbox payload get --name "$SECRET_NAME" --key POSTGRES_PASSWORD --format text \
    >"$YC_OUT" 2>"$YC_ERR"
YC_RC=$?
set -e

if [ $YC_RC -ne 0 ]; then
    echo "ERROR: yc lockbox payload get failed (rc=$YC_RC):" >&2
    cat "$YC_ERR" >&2
    rm -f "$YC_OUT" "$YC_ERR"
    echo "" >&2
    echo "Check that:" >&2
    echo "  - yc CLI is initialized: yc config list" >&2
    echo "  - You have access to secret '$SECRET_NAME'" >&2
    echo "  - The key 'POSTGRES_PASSWORD' exists: yc lockbox secret describe --name $SECRET_NAME" >&2
    exit 2
fi
PG_PW=$(tr -d '[:space:]' < "$YC_OUT")
rm -f "$YC_OUT" "$YC_ERR"

if [ -z "$PG_PW" ]; then
    echo "ERROR: POSTGRES_PASSWORD entry empty in $SECRET_NAME." >&2
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
