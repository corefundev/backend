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
#   [prod VPS / /srv/backend/secrets/yc-sa-key.json]
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
#   VPS_PATH          путь к json-файлу на VPS      (default: /srv/backend/secrets/yc-sa-key.json)
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
VPS_PATH="${VPS_PATH:-/srv/backend/secrets/yc-sa-key.json}"
VPS_COMPOSE_DIR="${VPS_COMPOSE_DIR:-/srv/backend}"
API_HEALTH_URL="${API_HEALTH_URL:-https://${VPS_HOST}/readyz}"
KEEP_KEYS="${KEEP_KEYS:-2}"

# Compose overlays that need restart
COMPOSE_ARGS="-f docker/docker-compose.yml -f docker/docker-compose.prod.yml -f docker/docker-compose.lockbox.yml"

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
ssh -q "${VPS_USER}@${VPS_HOST}" \
    "chmod 600 ${VPS_PATH}.new && mv ${VPS_PATH}.new ${VPS_PATH}"

# ── Step 3: restart containers — they'll pick up new key at startup ─────
# Order matters: api first (shortest downtime users feel), then workers.
echo "[3/5] docker compose restart on VPS"
ssh -q "${VPS_USER}@${VPS_HOST}" \
    "cd ${VPS_COMPOSE_DIR} && docker compose ${COMPOSE_ARGS} restart api worker scan-worker process-worker"

# ── Step 4: post-check — verify API is healthy under the new key ────────
echo "[4/5] waiting for /readyz to pass"
OK=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
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
