#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Vault entrypoint — renders config.hcl from template and starts the server.
#
# Reads VAULT_SEAL_MODE:
#   • awskms   — AWS KMS auto-unseal (requires VAULT_AWSKMS_* env vars)
#   • transit  — Transit auto-unseal via another Vault (advanced)
#   • manual   — Shamir (no seal block; default)
#
# All other VAULT_* env vars are passed through by Docker.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

: "${VAULT_SEAL_MODE:=manual}"
: "${VAULT_API_ADDR:=http://127.0.0.1:8200}"
: "${VAULT_CLUSTER_ADDR:=http://127.0.0.1:8201}"

case "$VAULT_SEAL_MODE" in
  awskms)
    # AWS KMS auto-unseal. Requires the running process to have AWS creds
    # (env vars or IAM role on the VPS). Key ARN comes from
    # VAULT_AWSKMS_KEY_ID.
    : "${VAULT_AWSKMS_KEY_ID:?ERROR: set VAULT_AWSKMS_KEY_ID}"
    : "${AWS_REGION:?ERROR: set AWS_REGION}"
    : "${AWS_ACCESS_KEY_ID:?ERROR: set AWS_ACCESS_KEY_ID (or use IAM role)}"
    : "${AWS_SECRET_ACCESS_KEY:?ERROR: set AWS_SECRET_ACCESS_KEY}"
    export VAULT_SEAL_SNIPPET="
seal \"awskms\" {
    region     = \"${AWS_REGION}\"
    kms_key_id = \"${VAULT_AWSKMS_KEY_ID}\"
}
"
    ;;
  transit)
    # Transit seal — unseal key lives in ANOTHER Vault. Useful only if you
    # already run a "root" Vault somewhere trusted.
    : "${VAULT_TRANSIT_ADDRESS:?}"
    : "${VAULT_TRANSIT_TOKEN:?}"
    : "${VAULT_TRANSIT_KEY_NAME:?}"
    export VAULT_SEAL_SNIPPET="
seal \"transit\" {
    address         = \"${VAULT_TRANSIT_ADDRESS}\"
    token           = \"${VAULT_TRANSIT_TOKEN}\"
    key_name        = \"${VAULT_TRANSIT_KEY_NAME}\"
    mount_path      = \"${VAULT_TRANSIT_MOUNT:-transit/}\"
}
"
    ;;
  manual)
    # Shamir seal — default. No seal stanza.
    export VAULT_SEAL_SNIPPET="# seal mode: manual (Shamir 5-of-3 by default)"
    ;;
  *)
    echo "ERROR: unknown VAULT_SEAL_MODE=$VAULT_SEAL_MODE" >&2
    exit 2
    ;;
esac

mkdir -p /vault/config /vault/data
envsubst '${VAULT_SEAL_SNIPPET} ${VAULT_API_ADDR} ${VAULT_CLUSTER_ADDR}' \
    < /vault/config.hcl.template \
    > /vault/config/vault.hcl

echo "[vault-entrypoint] starting with seal_mode=$VAULT_SEAL_MODE"
exec vault server -config=/vault/config/vault.hcl
