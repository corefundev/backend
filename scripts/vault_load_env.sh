#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# vault_load_env.sh — ingest a .env file into Vault KV.
#
# Usage:
#   export VAULT_TOKEN=<root or admin token>   # for one-shot seeding
#   ./scripts/vault_load_env.sh .env
#
# Behaviour:
#   • Reads KEY=VALUE lines from the given .env, skips comments + blanks.
#   • Strips surrounding quotes.
#   • Filters OUT bookkeeping keys that are meta-infra (VAULT_*, APP_ENV,
#     DOMAIN, CERTBOT_*, FRONTEND_ORIGINS, ADMIN_CIDR) — those stay in
#     .env because Vault itself needs them or they're not secrets.
#   • Also filters OUT auto-unseal credentials (VAULT_AWSKMS_*, AWS_*)
#     since those live alongside the Vault container at infra level.
#   • Pushes the rest into secret/sku-forecasting (KV v2).
#
# Idempotent: running it twice overwrites the KV entry with the latest
# .env contents (uses `vault kv put`, not `patch`).
# ─────────────────────────────────────────────────────────────────────────────
set -eu

ENV_FILE="${1:-.env}"
KV_PATH="${VAULT_KV_PATH:-secret/sku-forecasting}"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found" >&2
    exit 2
fi
if [ -z "${VAULT_TOKEN:-}" ]; then
    echo "ERROR: VAULT_TOKEN not set (use the root token from vault_init.sh)" >&2
    exit 2
fi

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/docker"

# Keys kept OUT of Vault. Two buckets:
#   a) Infrastructure-level (Vault itself needs them → must stay in .env)
#   b) Public-ish (domains, emails, CORS) — no benefit from Vault, adds
#      churn every time you rotate them.
OMIT_PREFIXES='
VAULT_
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=
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

# Build the argument list for `vault kv put`.
set --
COUNT=0
while IFS= read -r line || [ -n "$line" ]; do
    # Strip comments + whitespace
    line="$(printf '%s' "$line" | sed -e 's/[[:space:]]*#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in
        *=*) : ;;
        *)   continue ;;
    esac

    KEY="${line%%=*}"
    VAL="${line#*=}"

    # Unquote
    case "$VAL" in
        \"*\") VAL="${VAL#\"}"; VAL="${VAL%\"}" ;;
        \'*\') VAL="${VAL#\'}"; VAL="${VAL%\'}" ;;
    esac

    [ -z "$VAL" ] && continue
    if is_omitted "$KEY"; then
        continue
    fi

    # vault KV v2 stores lowercase keys — vault_agent upcases on read.
    LOWER="$(printf '%s' "$KEY" | tr '[:upper:]' '[:lower:]')"
    set -- "$@" "${LOWER}=${VAL}"
    COUNT=$((COUNT + 1))
done < "$ENV_FILE"

if [ "$COUNT" -eq 0 ]; then
    echo "ERROR: nothing to load — all keys filtered out" >&2
    exit 3
fi

echo "[vault_load_env] pushing $COUNT keys to $KV_PATH"
# Run inside the vault container so we don't need vault CLI locally.
cd "$COMPOSE_DIR"
docker compose -f docker-compose.yml -f docker-compose.vault.yml exec -T \
    -e VAULT_TOKEN="$VAULT_TOKEN" \
    vault \
    vault kv put "$KV_PATH" "$@" > /dev/null

echo "[vault_load_env] done. Verify:"
echo "   docker compose exec -e VAULT_TOKEN=\$TOK vault vault kv get $KV_PATH"
