#!/bin/bash
# scripts/lockbox_set_mlflow_s3.sh
#
# Idempotent helper to add / replace the S3_MLFLOW_* entries in the
# Yandex Lockbox secret — the dedicated Selectel bucket the MLflow
# server uses as its artifact backend (R8-12 / Phase 0 of the R10
# roadmap). Every other entry in the secret is preserved unchanged.
#
# Why namespaced under S3_MLFLOW_* (not AWS_*): the api / worker
# containers already carry AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# from Lockbox for the main client-data bucket. Reusing AWS_* would
# clash. The mlflow entrypoint renames S3_MLFLOW_* → MLFLOW_S3_ENDPOINT_URL
# + AWS_* in-process so the mlflow server's S3 client picks them up,
# WITHOUT exposing them to the api/worker namespace.
#
# Replaces five Lockbox keys in a single new-version POST:
#   S3_MLFLOW_BUCKET             (e.g. sku-mlflow)
#   S3_MLFLOW_ENDPOINT_URL       (e.g. https://s3.ru-7.storage.selcloud.ru)
#   S3_MLFLOW_ACCESS_KEY_ID
#   S3_MLFLOW_SECRET_ACCESS_KEY
#   S3_MLFLOW_REGION             (e.g. ru-7)
#
# Usage:
#   BUCKET=…  ENDPOINT=…  ACCESS_KEY=…  SECRET_KEY=…  REGION=…  \
#     ./scripts/lockbox_set_mlflow_s3.sh
#
# After this runs: build + force-recreate the mlflow container so it
# picks up the new artifact backend via lockbox_bootstrap on startup:
#   ssh deploy@<host> 'cd /srv/backend && docker compose ... build mlflow \
#                       && docker compose ... up -d --force-recreate mlflow'

set -euo pipefail

: "${BUCKET:?ERROR: set BUCKET env (e.g. sku-mlflow)}"
: "${ENDPOINT:?ERROR: set ENDPOINT env (e.g. https://s3.ru-7.storage.selcloud.ru)}"
: "${ACCESS_KEY:?ERROR: set ACCESS_KEY env}"
: "${SECRET_KEY:?ERROR: set SECRET_KEY env}"
: "${REGION:?ERROR: set REGION env (e.g. ru-7)}"

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
    echo "ERROR: yc CLI not found." >&2
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

echo "→ Building merged payload (setting 5 S3_MLFLOW_* keys, preserving all other entries)…"
NEW_PAYLOAD=$(
    BUCKET="$BUCKET" \
    ENDPOINT="$ENDPOINT" \
    ACCESS_KEY="$ACCESS_KEY" \
    SECRET_KEY="$SECRET_KEY" \
    REGION="$REGION" \
    CURRENT_PAYLOAD="$CURRENT_PAYLOAD" \
    python3 - <<'PYEOF'
import json
import os
import sys

data = json.loads(os.environ["CURRENT_PAYLOAD"])
if not (isinstance(data, dict) and "entries" in data):
    print("ERROR: unexpected yc payload format", file=sys.stderr)
    sys.exit(3)

replaced = {
    "S3_MLFLOW_BUCKET":             os.environ["BUCKET"],
    "S3_MLFLOW_ENDPOINT_URL":       os.environ["ENDPOINT"],
    "S3_MLFLOW_ACCESS_KEY_ID":      os.environ["ACCESS_KEY"],
    "S3_MLFLOW_SECRET_ACCESS_KEY":  os.environ["SECRET_KEY"],
    "S3_MLFLOW_REGION":             os.environ["REGION"],
}
filtered = [e for e in data["entries"] if e.get("key") not in replaced]
for k, v in replaced.items():
    filtered.append({"key": k, "textValue": v})
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
for k in S3_MLFLOW_BUCKET S3_MLFLOW_ENDPOINT_URL S3_MLFLOW_ACCESS_KEY_ID S3_MLFLOW_SECRET_ACCESS_KEY S3_MLFLOW_REGION; do
    STORED=$(yc lockbox payload get --name "$SECRET_NAME" --key "$k" --format text 2>/dev/null | tr -d '[:space:]')
    case "$k" in
        S3_MLFLOW_BUCKET)            INTENDED="$BUCKET" ;;
        S3_MLFLOW_ENDPOINT_URL)      INTENDED="$ENDPOINT" ;;
        S3_MLFLOW_ACCESS_KEY_ID)     INTENDED="$ACCESS_KEY" ;;
        S3_MLFLOW_SECRET_ACCESS_KEY) INTENDED="$SECRET_KEY" ;;
        S3_MLFLOW_REGION)            INTENDED="$REGION" ;;
    esac
    if [ "$STORED" = "$INTENDED" ]; then
        echo "  $k: ok (len=${#STORED})"
    else
        echo "  $k: MISMATCH (stored len=${#STORED}, intended len=${#INTENDED})" >&2
        exit 5
    fi
done

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  S3_MLFLOW_* keys set in Lockbox '$SECRET_NAME' (entries: $CURRENT_COUNT → $NEW_COUNT)."
echo "  Next:"
echo "    1. Build the custom Lockbox-aware mlflow image"
echo "       (docker/Dockerfile.mlflow + scripts/mlflow_entrypoint.sh)."
echo "    2. Force-recreate mlflow on prod so it fetches the new S3"
echo "       artifact backend via lockbox_bootstrap on startup."
echo "    3. Run a smoke training job; verify the new training_runs row"
echo "       has a non-NULL mlflow_run_id and artifacts appear in"
echo "       s3://${BUCKET}/mlflow/."
echo "═══════════════════════════════════════════════════════════════════"
