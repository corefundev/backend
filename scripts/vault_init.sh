#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# vault_init.sh — one-time bootstrap of the Vault container.
#
# What it does:
#   1. `vault operator init` → 5 unseal keys + root token (Shamir)
#      OR with auto-unseal → 5 recovery keys + root token (recovery keys
#      are emergency-only, normal operation needs neither).
#   2. Unseals Vault if needed.
#   3. Enables KV v2 at path secret/sku-forecasting.
#   4. Writes a read-only policy for that path.
#   5. Enables AppRole auth, creates role `sku-forecasting`,
#      prints ROLE_ID + SECRET_ID.
#
# Run ONCE per Vault. Output must be saved securely — see WARNINGS below.
#
# Usage:
#   cd /srv/backend
#   ./scripts/vault_init.sh
#
# Writes:
#   ./vault-init.secrets   ← ⚠ CONTAINS UNSEAL KEYS, INITIAL ROOT TOKEN,
#                            AND ROLE_ID + SECRET_ID. Put in password
#                            manager IMMEDIATELY, then `shred -u`.
#
# ⚠ WARNINGS
#   • If you lose unseal keys AND have manual seal — Vault is bricked.
#     Losing recovery keys (auto-unseal mode) is recoverable as long as KMS
#     key still exists, but you lose emergency rotation ability.
#   • The initial root token here is USED ONCE for setup, then revoked.
#   • SECRET_ID has TTL of 10 minutes — rotate via vault_rotate_secret_id.sh
#     for every deploy.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/docker"
cd "$COMPOSE_DIR"

OUT="../vault-init.secrets"
if [ -e "$OUT" ]; then
    echo "ERROR: $OUT already exists — refusing to overwrite." >&2
    echo "       If Vault is genuinely new, move this file aside and re-run." >&2
    exit 3
fi

# Pick compose overlay (same args as prod stack + vault overlay).
COMPOSE="-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.vault.yml"

run_in_vault() {
    docker compose $COMPOSE exec -T vault "$@"
}

echo "[vault_init] waiting for Vault to respond on port 8200…"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if run_in_vault wget -qO- http://127.0.0.1:8200/v1/sys/seal-status > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# ── Detect seal mode (for correct init flags) ───────────────────────────
SEAL_MODE="${VAULT_SEAL_MODE:-manual}"
if [ "$SEAL_MODE" = "manual" ]; then
    INIT_FLAGS="-key-shares=5 -key-threshold=3"
else
    INIT_FLAGS="-recovery-shares=5 -recovery-threshold=3"
fi

echo "[vault_init] initializing (seal_mode=$SEAL_MODE)"
run_in_vault vault operator init -format=json $INIT_FLAGS > "$OUT"
chmod 600 "$OUT"

# Parse init output
ROOT_TOKEN="$(grep -o '"root_token":"[^"]*"' "$OUT" | head -1 | sed 's/.*:"\(.*\)"/\1/')"

# Manual seal needs unseal, auto-unseal modes don't.
if [ "$SEAL_MODE" = "manual" ]; then
    echo "[vault_init] unsealing with first 3 keys"
    for i in 0 1 2; do
        KEY="$(sed -n 's/.*"unseal_keys_b64":\[\(.*\)\].*/\1/p' "$OUT" | \
                tr ',' '\n' | sed -n "$((i+1))p" | tr -d '"')"
        run_in_vault vault operator unseal "$KEY" > /dev/null
    done
fi

echo "[vault_init] logging in with root token"
run_in_vault vault login -no-print "$ROOT_TOKEN"

# ── 1. KV v2 engine at secret/sku-forecasting ───────────────────────────
# Vault mounts KV v2 at path "secret" by default in dev mode, but our
# raft-backed prod Vault is empty. Enable it here.
echo "[vault_init] enabling KV v2"
if ! run_in_vault vault secrets list -format=json | grep -q '"secret/"'; then
    run_in_vault vault secrets enable -version=2 -path=secret kv
fi

# ── 2. Policy: read-only access to secret/sku-forecasting/* ─────────────
cat <<'POLICY' | run_in_vault sh -c 'cat > /tmp/app-policy.hcl'
path "secret/data/sku-forecasting" {
    capabilities = ["read"]
}
path "secret/data/sku-forecasting/*" {
    capabilities = ["read"]
}
path "secret/metadata/sku-forecasting*" {
    capabilities = ["read", "list"]
}
POLICY
run_in_vault vault policy write sku-forecasting-app /tmp/app-policy.hcl
run_in_vault rm -f /tmp/app-policy.hcl

# ── 3. AppRole ──────────────────────────────────────────────────────────
echo "[vault_init] enabling AppRole auth"
if ! run_in_vault vault auth list -format=json | grep -q '"approle/"'; then
    run_in_vault vault auth enable approle
fi

run_in_vault vault write auth/approle/role/sku-forecasting \
    token_policies=sku-forecasting-app \
    token_ttl=1h \
    token_max_ttl=4h \
    secret_id_ttl=10m \
    secret_id_num_uses=1

ROLE_ID="$(run_in_vault vault read -field=role_id auth/approle/role/sku-forecasting/role-id)"
SECRET_ID="$(run_in_vault vault write -field=secret_id -f auth/approle/role/sku-forecasting/secret-id)"

# ── 4. Revoke the root token (future ops use AppRole) ───────────────────
run_in_vault vault token revoke "$ROOT_TOKEN"

cat >> "$OUT" <<EOF

# ── Bootstrap artefacts (saved $(date -u +%FT%TZ)) ──
VAULT_ROLE_ID=$ROLE_ID
VAULT_SECRET_ID=$SECRET_ID
# SECRET_ID TTL = 10m, one-use. Rotate before every deploy with
#   ./scripts/vault_rotate_secret_id.sh
EOF

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  Vault initialised. Credentials written to $OUT"
echo ""
echo "  NEXT STEPS (do this NOW, before closing the terminal):"
echo "  1. cat $OUT        # copy everything into your password manager"
echo "  2. shred -u $OUT   # wipe it from disk"
echo "  3. ./scripts/vault_load_env.sh .env"
echo "  4. Put these in the new (minimal) .env:"
echo "       VAULT_ADDR=http://vault:8200"
echo "       VAULT_ROLE_ID=$ROLE_ID"
echo "       VAULT_SECRET_ID=<rotate with vault_rotate_secret_id.sh>"
echo "  5. Remove all other secrets from .env, restart app containers."
echo "════════════════════════════════════════════════════════════════════"
