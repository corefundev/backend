#!/usr/bin/env bash
# scripts/restart_api.sh
# Перезапуск API с генерацией нового AppRole SECRET_ID.
# Использовать после каждого перезапуска Vault или API.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[API]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

cd /opt/sku-forecasting
COMPOSE_FILE="docker/docker-compose.beget.yml"
VAULT_TOKEN_VAL=$(grep '^VAULT_TOKEN=' .env | cut -d= -f2)

# Генерировать новый SECRET_ID
info "Генерация AppRole SECRET_ID..."
VAULT_SECRET_ID=$(docker compose -f "$COMPOSE_FILE" exec \
  -e VAULT_TOKEN="${VAULT_TOKEN_VAL:-dev-root-change-me}" \
  -e VAULT_ADDR=http://127.0.0.1:8200 \
  -T vault vault write -field=secret_id -f \
  auth/approle/role/sku-forecasting/secret-id 2>/dev/null) || \
  error "Не удалось получить SECRET_ID. Vault запущен?"

info "SECRET_ID получен: ${VAULT_SECRET_ID:0:8}..."

# Записать временно в .env
sed -i "s|^VAULT_SECRET_ID=.*|VAULT_SECRET_ID=$VAULT_SECRET_ID|" .env

# Перезапустить API
info "Перезапуск API..."
docker compose -f "$COMPOSE_FILE" --env-file .env up -d --force-recreate api

# Очистить SECRET_ID из .env (уже использован)
sed -i "s|^VAULT_SECRET_ID=.*|VAULT_SECRET_ID=|" .env

# Подождать и проверить
sleep 20
HEALTH=$(curl -s --max-time 10 http://localhost:8000/health 2>/dev/null)
if echo "$HEALTH" | grep -q '"ok"'; then
    info "API запущен: $HEALTH"
else
    error "API не отвечает. Логи: docker compose -f $COMPOSE_FILE logs api --tail=30"
fi
