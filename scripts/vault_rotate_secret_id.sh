#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# vault_rotate_secret_id.sh — mint a fresh short-lived SECRET_ID.
#
# Run this BEFORE every deploy. SECRET_IDs in our AppRole have:
#   secret_id_ttl       = 10m
#   secret_id_num_uses  = 1
#
# i.e. the container must use it within 10 minutes and only once. After
# that, the only VAULT_* value left in .env is VAULT_ROLE_ID (harmless
# on its own — without a SECRET_ID it authenticates nothing).
#
# Usage (must have an admin/root token in VAULT_TOKEN):
#   export VAULT_TOKEN=<admin token>
#   ./scripts/vault_rotate_secret_id.sh
#
# Prints the new SECRET_ID to stdout. Common pipelines:
#
#   # Inject into docker-compose .env
#   VAULT_SECRET_ID=$(./scripts/vault_rotate_secret_id.sh) \
#     docker compose up -d api worker scan-worker process-worker
#
#   # Or write it to .env (then immediately `rm` after containers start):
#   ./scripts/vault_rotate_secret_id.sh >> .env.secret-id
# ─────────────────────────────────────────────────────────────────────────────
set -eu

: "${VAULT_TOKEN:?ERROR: set VAULT_TOKEN (admin token)}"

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/docker"
cd "$COMPOSE_DIR"

docker compose -f docker-compose.yml -f docker-compose.vault.yml exec -T \
    -e VAULT_TOKEN="$VAULT_TOKEN" \
    vault \
    vault write -field=secret_id -f auth/approle/role/sku-forecasting/secret-id
