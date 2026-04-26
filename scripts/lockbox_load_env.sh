#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# lockbox_load_env.sh — create or update a Lockbox secret from a .env file.
#
# Prerequisites:
#   • `yc` CLI installed locally (https://yandex.cloud/ru/docs/cli/)
#   • `yc init` done — profile points at the right folder and SA
#   • SA used for `yc` has lockbox.admin on the folder
#
# Usage:
#   ./scripts/lockbox_load_env.sh .env.level1.bak sku-forecasting-secrets
#
# Behaviour:
#   1. If secret with given NAME already exists → `add-version`
#      (old version kept for rollback; Lockbox is versioned by design).
#   2. Otherwise → `create` the secret and print its ID.
#   3. Filters out infrastructure keys (same set as vault_load_env.sh)
#      so we don't push domain names into Lockbox.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

ENV_FILE="${1:?Usage: lockbox_load_env.sh <env-file> <secret-name>}"
SECRET_NAME="${2:?Usage: lockbox_load_env.sh <env-file> <secret-name>}"

command -v yc >/dev/null 2>&1 || {
    echo "ERROR: yc CLI not found. Install: https://yandex.cloud/ru/docs/cli/" >&2
    exit 2
}
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 2; }

OMIT_PREFIXES='
VAULT_
YC_
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=
AWS_DEFAULT_REGION=
APP_ENV=
CONFIG_PATH=
FRONTEND_ORIGINS=
API_DOMAIN=
WEB_DOMAIN=
CERTBOT_
ADMIN_CIDR=
STORAGE_BACKEND=
ARTIFACTS_DIR=
MAX_UPLOAD_BYTES=
SANDBOX_
CLAMAV_HOST=
CLAMAV_PORT=
REDIS_URL=
MLFLOW_TRACKING_URI=
ALLOW_SYNC_UPLOAD_FALLBACK=
'

is_omitted() {
    for p in $OMIT_PREFIXES; do
        [ -z "$p" ] && continue
        case "$1" in
            "${p}"*|"$p") return 0 ;;
        esac
    done
    return 1
}

# Build the --payload '[{"key":"…","text_value":"…"}…]' JSON array.
PAYLOAD="["
COUNT=0
FIRST=1
while IFS= read -r line || [ -n "$line" ]; do
    line="$(printf '%s' "$line" | sed -e 's/[[:space:]]*#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in
        *=*) : ;;
        *)   continue ;;
    esac
    KEY="${line%%=*}"
    VAL="${line#*=}"
    case "$VAL" in
        \"*\") VAL="${VAL#\"}"; VAL="${VAL%\"}" ;;
        \'*\') VAL="${VAL#\'}"; VAL="${VAL%\'}" ;;
    esac
    [ -z "$VAL" ] && continue
    if is_omitted "$KEY"; then
        continue
    fi
    # JSON-escape the value — \ and " are what matter for simple YAML strings.
    ESC_VAL=$(printf '%s' "$VAL" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
    if [ "$FIRST" -eq 0 ]; then
        PAYLOAD="$PAYLOAD,"
    fi
    PAYLOAD="$PAYLOAD{\"key\":\"$KEY\",\"text_value\":\"$ESC_VAL\"}"
    FIRST=0
    COUNT=$((COUNT + 1))
done < "$ENV_FILE"
PAYLOAD="$PAYLOAD]"

[ "$COUNT" -eq 0 ] && { echo "ERROR: nothing to load" >&2; exit 3; }

# Check if secret already exists.
if yc lockbox secret get --name "$SECRET_NAME" >/dev/null 2>&1; then
    echo "[lockbox_load_env] adding new version to existing secret $SECRET_NAME ($COUNT keys)"
    yc lockbox secret add-version \
        --name "$SECRET_NAME" \
        --payload "$PAYLOAD"
else
    echo "[lockbox_load_env] creating secret $SECRET_NAME ($COUNT keys)"
    yc lockbox secret create \
        --name "$SECRET_NAME" \
        --description "SKU Forecasting backend secrets (loaded $(date -u +%FT%TZ))" \
        --payload "$PAYLOAD"
fi

SECRET_ID="$(yc lockbox secret get --name "$SECRET_NAME" --format json | grep -o '"id":"[^"]*"' | head -1 | sed 's/.*:"\(.*\)"/\1/')"
cat <<EOF

──────────────────────────────────────────────────────────────
  Put this into your .env on the backend VPS:

    YC_LOCKBOX_SECRET_ID=$SECRET_ID

  Also ensure a service account has role 'lockbox.payloadViewer'
  on this secret's folder, and its authorised-key JSON is at
  YC_SA_KEY_FILE inside the API container.
──────────────────────────────────────────────────────────────
EOF
