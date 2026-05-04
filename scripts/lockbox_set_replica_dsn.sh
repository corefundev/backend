#!/bin/bash
# scripts/lockbox_set_replica_dsn.sh
#
# Idempotent helper to set DATABASE_URL_REPLICA in YC Lockbox correctly.
# Reads the current secret payload, swaps in / appends our entry, posts
# the merged payload as a new version. Other entries (POSTGRES_PASSWORD,
# JWT_SECRET_KEY, etc.) are preserved unchanged.
#
# Usage:
#   ./scripts/lockbox_set_replica_dsn.sh
#
# Env overrides:
#   SECRET_NAME, REPLICA_HOST, REPLICA_PORT, DB_NAME, DB_USER

set -euo pipefail

# Pick up yc from common install paths if it's not on PATH already.
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
Install: curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash
EOF
    exit 1
fi

SECRET_NAME="${SECRET_NAME:-sku-forecasting-secrets}"
REPLICA_HOST="${REPLICA_HOST:-db-replica.testcore.ru}"
REPLICA_PORT="${REPLICA_PORT:-5432}"
DB_NAME="${DB_NAME:-sku_forecasting}"
DB_USER="${DB_USER:-sku}"

echo "→ Fetching POSTGRES_PASSWORD from Lockbox secret '$SECRET_NAME'…"
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
# Sanity-check we're not about to write garbage.
if [[ "$DSN" =~ ^[\'\"] ]] || [[ "$DSN" == *'${'* ]] || [[ "$DSN" == *$'\n'* ]]; then
    echo "ERROR: built DSN looks malformed — refusing to write." >&2
    exit 3
fi

echo "→ Reading current payload (all entries) for merge…"
# yc returns one JSON object per entry, separated by newlines (NOT a JSON
# array). Need to wrap it before json.loads.
CURRENT_PAYLOAD=$(yc lockbox payload get --name "$SECRET_NAME" --format json)

echo "→ Building merged payload…"
NEW_PAYLOAD=$(SECRET_NAME="$SECRET_NAME" DSN="$DSN" CURRENT_PAYLOAD="$CURRENT_PAYLOAD" \
    python3 - <<'PYEOF'
import json
import os
import sys

current = os.environ["CURRENT_PAYLOAD"]
target_key = "DATABASE_URL_REPLICA"
new_dsn   = os.environ["DSN"]

# yc emits an outer object with "entries":[…]
data = json.loads(current)
if isinstance(data, dict) and "entries" in data:
    entries = data["entries"]
else:
    print("UNEXPECTED yc payload format — wrapping list", file=sys.stderr)
    entries = data if isinstance(data, list) else [data]

# Drop any prior copy, append the fresh one.
entries = [e for e in entries if e.get("key") != target_key]
entries.append({"key": target_key, "textValue": new_dsn})

# yc add-version accepts a JSON array per its --payload help.
print(json.dumps(entries, ensure_ascii=False))
PYEOF
)

echo "→ Writing new secret version with merged payload (entries: $(echo "$NEW_PAYLOAD" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'))…"
yc lockbox secret add-version --name "$SECRET_NAME" --payload "$NEW_PAYLOAD" >/dev/null

echo "→ Verifying via Lockbox readback…"
STORED=$(yc lockbox payload get --name "$SECRET_NAME" --key DATABASE_URL_REPLICA --format text)
if [ "$STORED" = "$DSN" ]; then
    echo "  match: ok"
else
    echo "  WARN: stored differs from intended (len stored=${#STORED}, intended=${#DSN})" >&2
fi

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  DATABASE_URL_REPLICA set correctly."
echo "  Now: trigger 'Restart App' workflow on GitHub Actions."
echo "═══════════════════════════════════════════════════════════════════"
