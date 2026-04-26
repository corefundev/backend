#!/usr/bin/env bash
# vault/setup_vault.sh
#
# Запускать после каждой перезагрузки / unseal Vault.
#
# Что делает:
#   1. Включает KV v2, AppRole, Policy (идемпотентно — если уже есть, пропускает)
#   2. Проверяет что секреты уже есть в Vault
#   3. Если секретов нет — предупреждает и просит ввести
#   4. Перезапускает API с новым SECRET_ID
#
# Чего НЕ делает:
#   - НЕ читает секреты из .env
#   - НЕ перезаписывает существующие секреты в Vault
#   - НЕ пишет ничего в .env

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[VAULT]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Читаем только несекретные переменные из .env:
if [[ -f ".env" ]]; then
    set -o allexport
    source <(grep -v '^#' .env | grep -v '^$' | sed 's/[[:space:]]*#.*$//')
    set +o allexport
fi

COMPOSE_FILE="${COMPOSE_FILE:-docker/docker-compose.beget.yml}"
VAULT_TOKEN="${VAULT_TOKEN:-}"
VAULT_INNER_ADDR="http://127.0.0.1:8200"

[[ -z "$VAULT_TOKEN" ]] && error "VAULT_TOKEN не задан в .env"

# Проверить что Vault запущен и не запечатан:
SEALED=$(docker compose -f "$COMPOSE_FILE" exec \
    -e VAULT_ADDR="$VAULT_INNER_ADDR" \
    -e VAULT_TOKEN="$VAULT_TOKEN" \
    -T vault vault status 2>/dev/null | grep "^Sealed" | awk '{print $2}' || echo "true")

[[ "$SEALED" == "true" ]] && \
    error "Vault запечатан! Сначала выполните: bash scripts/unseal_vault.sh"

# Удобная функция для vault команд:
v() {
    docker compose -f "$COMPOSE_FILE" exec \
        -e VAULT_TOKEN="$VAULT_TOKEN" \
        -e VAULT_ADDR="$VAULT_INNER_ADDR" \
        -T vault vault "$@"
}

info "=== Vault Setup (production) ==="
info "Token: ${VAULT_TOKEN:0:8}..."

# ── 1. KV v2 ──────────────────────────────────────────────────
info "1/5 KV v2 secrets engine..."
v secrets enable -version=2 -path=secret kv 2>/dev/null && \
    info "  enabled" || info "  already enabled"

# ── 2. AppRole ────────────────────────────────────────────────
info "2/5 AppRole auth method..."
v auth enable approle 2>/dev/null && \
    info "  enabled" || info "  already enabled"

# ── 3. Policy ─────────────────────────────────────────────────
info "3/5 Policy: sku-forecasting..."
v policy write sku-forecasting - << 'POLICY'
path "secret/data/sku-forecasting" {
  capabilities = ["read"]
}
path "secret/metadata/sku-forecasting" {
  capabilities = ["read"]
}
path "database/creds/sku-forecasting-role" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/approle/role/sku-forecasting/secret-id" {
  capabilities = ["create", "update"]
}
POLICY
info "  created"

# ── 4. AppRole role ───────────────────────────────────────────
info "4/5 AppRole role..."
v write auth/approle/role/sku-forecasting \
    token_policies="sku-forecasting" \
    token_ttl="1h" \
    token_max_ttl="4h" \
    secret_id_ttl="10m" \
    secret_id_num_uses=1 \
    bind_secret_id=true
ROLE_ID=$(v read -field=role_id auth/approle/role/sku-forecasting/role-id)
info "  Role ID: $ROLE_ID"

# ── 5. Проверить секреты ──────────────────────────────────────
info "5/5 Проверка секретов в Vault..."
SECRETS_OK=true
for field in jwt_secret_key api_key aws_access_key_id aws_secret_access_key; do
    VALUE=$(v kv get -field="$field" secret/sku-forecasting 2>/dev/null || echo "")
    if [[ -z "$VALUE" ]]; then
        warn "  Секрет '$field' пустой или не задан!"
        SECRETS_OK=false
    else
        info "  $field: ${VALUE:0:6}... ✓"
    fi
done

if [[ "$SECRETS_OK" == "false" ]]; then
    echo ""
    warn "Некоторые секреты не заданы. Запишите их вручную:"
    warn "  vault kv put secret/sku-forecasting \\"
    warn "    jwt_secret_key=\"...\" \\"
    warn "    api_key=\"...\" \\"
    warn "    aws_access_key_id=\"...\" \\"
    warn "    aws_secret_access_key=\"...\" \\"
    warn "    grafana_password=\"...\""
    echo ""
fi

# ── Перезапустить API ─────────────────────────────────────────
echo ""
info "Перезапуск API с новым AppRole SECRET_ID..."
bash scripts/restart_api.sh

# ── Итог ──────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Vault setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  VAULT_ROLE_ID=$ROLE_ID"
echo ""
