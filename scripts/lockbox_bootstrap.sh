#!/bin/sh
# scripts/lockbox_bootstrap.sh
#
# Pure-shell Yandex Lockbox secret loader. Reimplements
# src/auth/lockbox_agent.py for containers without Python (specifically
# the backup container, which is alpine + cron).
#
# Reads SA key from $YC_SA_KEY_FILE, generates JWT signed via PS256,
# exchanges for IAM token, fetches Lockbox payload, exports each
# textValue entry as an env var, then exec's "$@".
#
# Designed to be `crontab` wrapper too — every cron run gets fresh
# secrets, no caching.
#
# Required env:
#   YC_SA_KEY_FILE          path to authorized_key.json (read-only mount)
#   YC_LOCKBOX_SECRET_ID    Lockbox secret id
#
# Required tools:
#   jq, openssl, curl, base64, mktemp  (alpine packages)

set -eu

: "${YC_SA_KEY_FILE:?YC_SA_KEY_FILE not set}"
: "${YC_LOCKBOX_SECRET_ID:?YC_LOCKBOX_SECRET_ID not set}"

if [ ! -r "$YC_SA_KEY_FILE" ]; then
    echo "lockbox_bootstrap: SA key file not readable: $YC_SA_KEY_FILE" >&2
    exit 1
fi

# ── 1. Parse SA key ────────────────────────────────────────────────
SA_ID=$(jq -r '.service_account_id' "$YC_SA_KEY_FILE")
KEY_ID=$(jq -r '.id' "$YC_SA_KEY_FILE")
PRIVATE_KEY=$(jq -r '.private_key' "$YC_SA_KEY_FILE")

if [ -z "$SA_ID" ] || [ "$SA_ID" = "null" ] \
    || [ -z "$KEY_ID" ] || [ "$KEY_ID" = "null" ] \
    || [ -z "$PRIVATE_KEY" ] || [ "$PRIVATE_KEY" = "null" ]; then
    echo "lockbox_bootstrap: SA key missing required fields" >&2
    exit 1
fi

# Stage the private key for openssl. mktemp dir gets removed via trap.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
KEY_FILE="$TMP/key.pem"
printf '%s' "$PRIVATE_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
unset PRIVATE_KEY

# ── 2. Build JWT ───────────────────────────────────────────────────
NOW=$(date +%s)
EXP=$((NOW + 3600))

HEADER=$(printf '{"typ":"JWT","alg":"PS256","kid":"%s"}' "$KEY_ID")
PAYLOAD=$(printf '{"iss":"%s","aud":"https://iam.api.cloud.yandex.net/iam/v1/tokens","iat":%d,"exp":%d}' \
    "$SA_ID" "$NOW" "$EXP")

# base64url = base64 minus padding, with -_ instead of +/.
b64url() { base64 -w0 2>/dev/null || base64 | tr -d '\n'; }
b64url_to_url() { tr '+/' '-_' | tr -d '='; }

HEADER_B64=$(printf '%s' "$HEADER"  | b64url | b64url_to_url)
PAYLOAD_B64=$(printf '%s' "$PAYLOAD" | b64url | b64url_to_url)
SIGNING_INPUT="${HEADER_B64}.${PAYLOAD_B64}"

# PS256 = RSA-PSS with SHA-256, MGF1+SHA-256, salt length = digest length.
SIGNATURE_B64=$(printf '%s' "$SIGNING_INPUT" \
    | openssl dgst -sha256 -binary \
        -sigopt rsa_padding_mode:pss \
        -sigopt rsa_pss_saltlen:-1 \
        -sign "$KEY_FILE" \
    | b64url | b64url_to_url)

JWT="${SIGNING_INPUT}.${SIGNATURE_B64}"

# ── 3. Exchange JWT → IAM token ────────────────────────────────────
IAM_RESP=$(curl -fsS -X POST -H 'Content-Type: application/json' \
    -d "{\"jwt\":\"$JWT\"}" \
    https://iam.api.cloud.yandex.net/iam/v1/tokens)
IAM_TOKEN=$(printf '%s' "$IAM_RESP" | jq -r '.iamToken')
unset JWT IAM_RESP

if [ -z "$IAM_TOKEN" ] || [ "$IAM_TOKEN" = "null" ]; then
    echo "lockbox_bootstrap: failed to obtain IAM token" >&2
    exit 1
fi

# ── 4. Fetch Lockbox payload ───────────────────────────────────────
PAYLOAD_JSON=$(curl -fsS \
    -H "Authorization: Bearer $IAM_TOKEN" \
    "https://payload.lockbox.api.cloud.yandex.net/lockbox/v1/secrets/$YC_LOCKBOX_SECRET_ID/payload")
unset IAM_TOKEN

# ── 5. Export textValue entries ───────────────────────────────────
N=$(printf '%s' "$PAYLOAD_JSON" | jq -r '.entries | length')
i=0
INJECTED=0
while [ "$i" -lt "$N" ]; do
    K=$(printf '%s' "$PAYLOAD_JSON" | jq -r ".entries[$i].key")
    V=$(printf '%s' "$PAYLOAD_JSON" | jq -r ".entries[$i].textValue // empty")
    if [ -n "$K" ] && [ -n "$V" ]; then
        # Don't clobber explicit env overrides (matches lockbox_agent.py).
        eval "EXISTING=\${$K-__UNSET__}"
        if [ "$EXISTING" = "__UNSET__" ] || [ -z "$EXISTING" ]; then
            export "$K=$V"
            INJECTED=$((INJECTED + 1))
        fi
    fi
    i=$((i + 1))
done
unset PAYLOAD_JSON N i K V EXISTING

echo "lockbox_bootstrap: injected $INJECTED variables from $YC_LOCKBOX_SECRET_ID" >&2

# ── 6. exec the rest ──────────────────────────────────────────────
if [ "$#" -eq 0 ]; then
    # Sourcing-mode: just exit 0 so the parent shell keeps the exports.
    # But we ran in a subshell — exports won't persist. Sourcing-mode
    # only makes sense if user does `. lockbox_bootstrap.sh` not exec.
    exit 0
fi
exec "$@"
