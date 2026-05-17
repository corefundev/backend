#!/bin/bash
# scripts/lockbox_set_s3_backup_creds.sh
#
# Idempotent helper to point S3_BACKUP_* at a separate, dedicated
# backups bucket — required by R5-18 (commingled client-data +
# backups in a single Beget bucket). Other entries
# (POSTGRES_PASSWORD, JWT_SECRET_KEY, S3_ENDPOINT_URL for client
# data, etc.) are preserved unchanged.
#
# Replaces three Lockbox keys in a single new-version POST:
#   S3_BACKUP_BUCKET
#   S3_BACKUP_ACCESS_KEY_ID
#   S3_BACKUP_SECRET_ACCESS_KEY
#
# S3_BACKUP_ENDPOINT_URL is left unchanged — same Beget endpoint
# `https://s3.ru1.storage.beget.cloud` serves both buckets.
#
# Usage:
#   BUCKET=… ACCESS_KEY=… SECRET_KEY=… ./scripts/lockbox_set_s3_backup_creds.sh
#
# After this runs, restart the backup container so it picks up
# the new bucket via lockbox_bootstrap on next cron invocation:
#   ssh deploy@<host> 'docker compose ... up -d --force-recreate backup'
# Then re-apply lifecycle rules on the new bucket:
#   docker exec docker-backup-1 sh /scripts/init_s3_lifecycle.sh

set -euo pipefail

: "${BUCKET:?ERROR: set BUCKET env (e.g. 762089886d76-zone-backup)}"
: "${ACCESS_KEY:?ERROR: set ACCESS_KEY env}"
: "${SECRET_KEY:?ERROR: set SECRET_KEY env}"

SECRET_NAME="${SECRET_NAME:-sku-forecasting-secrets}"

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
    echo "ERROR: yc CLI not found. Install via" >&2
    echo "  curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash" >&2
    exit 1
fi

echo "→ Reading current payload (all entries) for merge…"
CURRENT_PAYLOAD=$(yc lockbox payload get --name "$SECRET_NAME" --format json)
CURRENT_COUNT=$(echo "$CURRENT_PAYLOAD" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("entries",[])))')
echo "  current entry count: $CURRENT_COUNT"
if [ "$CURRENT_COUNT" -lt 50 ]; then
    echo "ERROR: current payload has fewer than 50 entries — refusing to overwrite" >&2
    echo "       (sanity guard against reading an empty/corrupt payload)" >&2
    exit 2
fi

echo "→ Building merged payload (replacing 3 S3_BACKUP_* keys)…"
NEW_PAYLOAD=$(
    SECRET_NAME="$SECRET_NAME" \
    BUCKET="$BUCKET" \
    ACCESS_KEY="$ACCESS_KEY" \
    SECRET_KEY="$SECRET_KEY" \
    CURRENT_PAYLOAD="$CURRENT_PAYLOAD" \
    python3 - <<'PYEOF'
import json
import os
import sys

current = os.environ["CURRENT_PAYLOAD"]
data = json.loads(current)
if not (isinstance(data, dict) and "entries" in data):
    print("ERROR: unexpected yc payload format", file=sys.stderr)
    sys.exit(3)

entries = data["entries"]

# Drop any prior copies of the three keys we replace.
replaced_keys = {
    "S3_BACKUP_BUCKET":            os.environ["BUCKET"],
    "S3_BACKUP_ACCESS_KEY_ID":     os.environ["ACCESS_KEY"],
    "S3_BACKUP_SECRET_ACCESS_KEY": os.environ["SECRET_KEY"],
}
filtered = [e for e in entries if e.get("key") not in replaced_keys]
for k, v in replaced_keys.items():
    filtered.append({"key": k, "textValue": v})

# Sanity: should end up with the SAME total count (3 dropped, 3 added)
# IF all 3 keys were present before. If any were missing, count grows
# by the diff — but never shrinks.
print(json.dumps(filtered, ensure_ascii=False))
PYEOF
)

NEW_COUNT=$(echo "$NEW_PAYLOAD" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
echo "  new entry count: $NEW_COUNT"
if [ "$NEW_COUNT" -lt "$CURRENT_COUNT" ]; then
    echo "ERROR: new payload would have FEWER entries ($NEW_COUNT < $CURRENT_COUNT) — refusing." >&2
    exit 4
fi

echo "→ Writing new secret version with merged payload…"
yc lockbox secret add-version --name "$SECRET_NAME" --payload "$NEW_PAYLOAD" >/dev/null

echo "→ Verifying via Lockbox readback…"
for k in S3_BACKUP_BUCKET S3_BACKUP_ACCESS_KEY_ID S3_BACKUP_SECRET_ACCESS_KEY; do
    STORED=$(yc lockbox payload get --name "$SECRET_NAME" --key "$k" --format text 2>/dev/null | tr -d '[:space:]')
    case "$k" in
        S3_BACKUP_BUCKET)
            INTENDED="$BUCKET" ;;
        S3_BACKUP_ACCESS_KEY_ID)
            INTENDED="$ACCESS_KEY" ;;
        S3_BACKUP_SECRET_ACCESS_KEY)
            INTENDED="$SECRET_KEY" ;;
    esac
    if [ "$STORED" = "$INTENDED" ]; then
        echo "  $k: ok"
    else
        echo "  $k: MISMATCH (stored len=${#STORED}, intended len=${#INTENDED})" >&2
        exit 5
    fi
done

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  S3_BACKUP_* keys updated in Lockbox '$SECRET_NAME'."
echo "  Next: recreate backup container so it picks up new bucket:"
echo "    ssh deploy@api.testcore.ru \\"
echo "      'cd /srv/backend && docker compose ... up -d --force-recreate backup'"
echo "  Then init lifecycle rules on the new bucket:"
echo "    docker exec docker-backup-1 sh /scripts/init_s3_lifecycle.sh"
echo "═══════════════════════════════════════════════════════════════════"
