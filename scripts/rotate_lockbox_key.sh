#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# rotate_lockbox_key.sh — безопасная ротация authorized key для
# SA, читающего Lockbox-секрет на prod-VPS.
#
# Концепция (см. DEPLOY.md §4.3):
#
#   [где запускается скрипт]
#        │ yc iam key create (новый JSON)            ← master-creds
#        │                                              (не на VPS!)
#        ▼
#   [prod VPS / /srv/backend/secrets/yc/yc-sa-key.json]
#        │ atomic rename: .new → actual
#        │ docker compose restart api worker …         ← подхватят ключ
#        │                                              при старте
#        ▼
#   [yc iam key delete] для всех старых ключей,
#   кроме 2 последних (safety window).
#
# ПРИНЦИПЫ
#
#   1. Ротация НИКОГДА не запускается на самом VPS.
#      Master-creds (право создавать/удалять ключи) должны жить где-то
#      снаружи prod-поверхности: dev-машина оператора или CI runner.
#
#   2. Скрипт идемпотентен. Если любая стадия упала — старый ключ
#      остаётся рабочим (новый либо не создан, либо не применён).
#      Старые ключи удаляются ТОЛЬКО после успешного restart+readyz.
#
#   3. Safety window: всегда оставляем 2 самых новых ключа. Если
#      readyz вдруг начинает сбоить ПОСЛЕ ротации (обычно из-за
#      кэша IAM или clock skew), предыдущий ключ ещё существует и
#      его можно быстро вернуть.
#
# ENV (все можно указать через environment или argv):
#
#   YC_SA_NAME        имя SA, чей ключ ротируем    (default: sku-lockbox-reader)
#   VPS_USER          ssh-пользователь на VPS       (default: deploy)
#   VPS_HOST          hostname или IP VPS           (required)
#   VPS_PATH          путь к json-файлу на VPS      (default: /srv/backend/secrets/yc/yc-sa-key.json — DIR-mount source, #303/#353)
#   VPS_COMPOSE_DIR   где `docker compose`          (default: /srv/backend)
#   API_HEALTH_URL    URL для post-check           (default: https://$VPS_HOST/readyz)
#   KEEP_KEYS         сколько самых новых оставить  (default: 2)
#
# Требует на запускающей машине:
#   • yc CLI (https://yandex.cloud/ru/docs/cli/)
#   • jq
#   • ssh + scp
#   • curl
#
# Использование:
#
#   # Ручной прогон с dev-машины:
#   VPS_HOST=api.example.com ./scripts/rotate_lockbox_key.sh
#
#   # Внутри CI — просто импортировать env и запускать.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

# ── Config ───────────────────────────────────────────────────────────────
YC_SA_NAME="${YC_SA_NAME:-sku-lockbox-reader}"
VPS_USER="${VPS_USER:-deploy}"
VPS_HOST="${VPS_HOST:?ERROR: set VPS_HOST}"
# AUD-1 (#353): MUST be the dir-mount SOURCE (secrets/yc/), not the legacy
# flat file. #303 moved all 14 container mounts to `../secrets/yc` →
# /run/secrets/yc/; writing the legacy path rotated a file nothing reads,
# so the fleet kept authenticating on the OLD key until step 5 revoked it
# (fleet-wide Lockbox 401 on the SECOND rotation — the #190 incident class).
VPS_PATH="${VPS_PATH:-/srv/backend/secrets/yc/yc-sa-key.json}"
VPS_COMPOSE_DIR="${VPS_COMPOSE_DIR:-/srv/backend}"
API_HEALTH_URL="${API_HEALTH_URL:-https://${VPS_HOST}/readyz}"
KEEP_KEYS="${KEEP_KEYS:-2}"

# Compose overlays that need restart. --env-file is required because the
# project root from -f flags is the docker/ subdir, so .env at /srv/backend
# is not auto-loaded.
COMPOSE_ARGS="--env-file .env -f docker/docker-compose.yml -f docker/docker-compose.prod.yml -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml -f docker/docker-compose.replication.yml"

# Sanity checks
for cmd in yc jq ssh scp curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: $cmd not found in PATH" >&2
        exit 2
    }
done

SA_ID="$(yc iam service-account get --name "$YC_SA_NAME" --format json 2>/dev/null | jq -r .id || true)"
if [ -z "$SA_ID" ] || [ "$SA_ID" = "null" ]; then
    echo "ERROR: service account $YC_SA_NAME not found in current yc folder" >&2
    exit 3
fi

echo "═══════════════════════════════════════════════════════════════════"
echo "  Lockbox SA key rotation"
echo "   SA            = $YC_SA_NAME ($SA_ID)"
echo "   VPS           = ${VPS_USER}@${VPS_HOST}"
echo "   file          = $VPS_PATH"
echo "   keep last     = $KEEP_KEYS keys"
echo "═══════════════════════════════════════════════════════════════════"

# ── Step 1: create new key (temp file; always cleaned up) ────────────────
NEW_KEY_FILE="$(mktemp)"
trap 'rm -f "$NEW_KEY_FILE"' EXIT

echo "[1/5] creating new authorized key via yc"
yc iam key create \
    --service-account-name "$YC_SA_NAME" \
    --output "$NEW_KEY_FILE" \
    > /dev/null

NEW_KEY_ID="$(jq -r .id "$NEW_KEY_FILE")"
if [ -z "$NEW_KEY_ID" ] || [ "$NEW_KEY_ID" = "null" ]; then
    echo "ERROR: could not parse new key id" >&2
    exit 4
fi
echo "       new key id = $NEW_KEY_ID"

# ── Step 2: scp to VPS with atomic rename ────────────────────────────────
# We write to a .new sibling and rename atomically. If the scp fails or
# gets interrupted, the live file is untouched.
echo "[2/5] scp → ${VPS_PATH}.new"
scp -q "$NEW_KEY_FILE" "${VPS_USER}@${VPS_HOST}:${VPS_PATH}.new"
# R5-21 (2026-05-17, post-mortem) — chmod 644 (NOT 600). The
# original 644 was correct after all; my first R5-21 fix to 600
# was a false positive. Containers that read this file run as
# THREE different UIDs:
#   - backup / postgres   → root (uid 0) — root bypasses 600 OK
#   - alertmanager        → uid 65534 (nobody)
#   - postgres-exporter   → uid 65534 (nobody)
#   - api / migrate       → uid 999 (sku)
# chmod 600 (owner-deploy-only) blocked the last three, breaking
# `Lockbox bootstrap: injected … from secret` at container start
# → migrate container exited 1 → CD `Deploy → production` failed
# on commit ecbfe90 (2026-05-17 13:34 UTC). Properly tightening
# this would need Linux ACLs (`setfacl -m u:999:r,u:65534:r`) or
# Dockerfile changes to align container UIDs to a host group —
# both out of scope for now. Threat model under 644 + parent dir
# 700 + deploy-only VPS user is acceptable.
ssh -q "${VPS_USER}@${VPS_HOST}" \
    "chmod 644 ${VPS_PATH}.new && mv ${VPS_PATH}.new ${VPS_PATH}"

# ── Step 3: restart containers — they'll pick up new key at startup ─────
# Order matters: api first (shortest downtime users feel), then workers.
echo "[3/5] docker compose restart on VPS"
ssh -q "${VPS_USER}@${VPS_HOST}" \
    "cd ${VPS_COMPOSE_DIR} && docker compose ${COMPOSE_ARGS} restart api worker scan-worker process-worker"

# Reload the front-door nginx so it re-resolves the api upstream's IP.
# `docker compose restart` is supposed to keep container IPs stable, but
# in practice the brief window when the api isn't accepting connections
# leaves nginx in a "no live upstream" state until its DNS cache (default
# 30-300s) refreshes. A signal-based reload is instantaneous and harmless
# if the IP hasn't moved.
ssh -q "${VPS_USER}@${VPS_HOST}" \
    "docker exec docker-nginx-1 nginx -s reload" || \
    echo "       warning: nginx reload failed (continuing — readiness probe will tell)"

# ── Step 4: post-check — verify API is healthy under the new key ────────
# 18 attempts × 6s = ~108s upper bound. The previous 60s was tight when
# the api had to bootstrap Lockbox secrets (~10-20s on cold start) plus
# rebuild the OTel/Telegram poll loops.
echo "[4/5] waiting for /readyz to pass"
OK=0
for attempt in $(seq 1 18); do
    sleep 6
    if curl -fsS --max-time 5 "$API_HEALTH_URL" >/dev/null 2>&1; then
        OK=1
        break
    fi
    echo "       attempt $attempt: not ready yet"
done
if [ "$OK" -ne 1 ]; then
    echo "ERROR: API did not become ready after rotation." >&2
    echo "       New key is in place; old keys NOT deleted (safety window)." >&2
    echo "       Rollback: ssh to VPS, restore previous .json, restart." >&2
    exit 5
fi

# ── Step 4b: prove the CONTAINERS read the new key (AUD-1 #353) ─────────
# /readyz alone is NOT evidence: until step 5 prunes it, the old key is
# still valid, so a container reading a stale path stays healthy — exactly
# how the legacy-path bug hid. Assert the key id inside the container's
# mount matches the one we just created, BEFORE revoking anything.
echo "[4b/5] verifying containers read key id $NEW_KEY_ID"
IN_CONTAINER_ID="$(ssh -q "${VPS_USER}@${VPS_HOST}" \
    "docker exec docker-api-1 sh -c 'cat \"\$YC_SA_KEY_FILE\"' 2>/dev/null | jq -r .id" 2>/dev/null || true)"
if [ "$IN_CONTAINER_ID" != "$NEW_KEY_ID" ]; then
    echo "ERROR: container reads key id '${IN_CONTAINER_ID:-<none>}', expected $NEW_KEY_ID." >&2
    echo "       The rotated file is NOT the one the fleet mounts (VPS_PATH wrong?)." >&2
    echo "       ABORTING before key pruning — no key revoked, prod untouched." >&2
    exit 6
fi
echo "       confirmed: containers are on the new key"

# ── Step 5: prune old keys (keep $KEEP_KEYS newest) ─────────────────────
# Only runs after readyz passes — ensures we never delete a key that the
# running app might still be caching.
echo "[5/5] pruning old keys (keeping newest $KEEP_KEYS)"
yc iam key list \
    --service-account-name "$YC_SA_NAME" \
    --format json \
    | jq -r --argjson keep "$KEEP_KEYS" \
        'sort_by(.created_at) | reverse | .[$keep:] | .[].id' \
    | while read -r old_id; do
        [ -z "$old_id" ] && continue
        echo "       deleting $old_id"
        yc iam key delete --id "$old_id" > /dev/null || echo "       WARN: could not delete $old_id"
      done

echo "═══════════════════════════════════════════════════════════════════"
echo "  Rotation complete. Active key: $NEW_KEY_ID"
echo "═══════════════════════════════════════════════════════════════════"
