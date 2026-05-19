#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# SKU Forecasting Platform v8.0 — установка на чистый VPS
#
# Протестировано: Ubuntu 22.04 LTS / Debian 12
#
# ИСПОЛЬЗОВАНИЕ:
#   Шаг 1. Загрузите архив на сервер и распакуйте:
#     scp core.zip root@YOUR_IP:/root/
#     ssh root@YOUR_IP
#     unzip core.zip && cd core
#
#   Шаг 2. Создайте .env из шаблона и заполните:
#     cp .env.example .env && nano .env
#
#   Шаг 3. Запустите скрипт:
#     bash scripts/deploy_vps.sh
# ══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }

DEPLOY_USER="deploy"
PROJECT_DIR="/opt/sku-forecasting"
COMPOSE="docker compose -f ${PROJECT_DIR}/docker/docker-compose.beget.yml"

# ════════════════════════════════════════════════════════════
# ШАГ 0: Предварительные проверки
# ════════════════════════════════════════════════════════════
step "Шаг 0/8: Проверки"

[[ $EUID -eq 0 ]] || error "Запустите от root: sudo bash scripts/deploy_vps.sh"

[[ -f "docker/docker-compose.beget.yml" ]] || \
    error "Запустите из корня проекта (где есть docker/ и src/).\nТекущая директория: $(pwd)"

[[ -f "requirements.txt" ]] || \
    error "requirements.txt не найден — архив распакован не полностью."

if [[ ! -f ".env" ]]; then
    error ".env не найден.\nСоздайте: cp .env.example .env && nano .env\nЗатем запустите скрипт снова."
fi

set -o allexport; source .env; set +o allexport

[[ "${JWT_SECRET_KEY:-CHANGE_ME}" == CHANGE_ME* ]] && \
    error "Установите JWT_SECRET_KEY в .env\nГенерация: python3 -c \"import secrets; print(secrets.token_hex(32))\""
[[ "${API_KEY:-CHANGE_ME}" == CHANGE_ME* ]] && \
    error "Установите API_KEY в .env\nГенерация: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
[[ "${GRAFANA_PASSWORD:-CHANGE_ME}" == CHANGE_ME* ]] && \
    error "Установите GRAFANA_PASSWORD в .env"
[[ "${AWS_ACCESS_KEY_ID:-CHANGE_ME}" == CHANGE_ME* ]] && \
    warn "AWS_ACCESS_KEY_ID не задан — S3 не будет работать. Добавьте в .env."

TOTAL_RAM_GB=$(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1024 / 1024 ))
[[ $TOTAL_RAM_GB -lt 3 ]] && warn "RAM: ${TOTAL_RAM_GB}GB — рекомендуется минимум 4GB."

FREE_DISK_GB=$(df / --output=avail | tail -1 | awk '{print int($1/1024/1024)}')
[[ $FREE_DISK_GB -lt 10 ]] && error "Мало места: ${FREE_DISK_GB}GB. Нужно минимум 10GB."

info "✓ Проверки пройдены (RAM: ${TOTAL_RAM_GB}GB, Диск свободно: ${FREE_DISK_GB}GB)"

# ════════════════════════════════════════════════════════════
# ШАГ 1: Системные пакеты
# ════════════════════════════════════════════════════════════
step "Шаг 1/8: Системные пакеты"
apt-get update -qq
apt-get install -y -q \
    curl wget git ca-certificates gnupg lsb-release \
    python3 python3-pip \
    build-essential libpq-dev \
    ufw fail2ban \
    htop nano unzip rsync
info "✓ Пакеты установлены"

# ════════════════════════════════════════════════════════════
# ШАГ 2: Docker
# ════════════════════════════════════════════════════════════
step "Шаг 2/8: Docker"
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    systemctl start docker
    info "✓ Docker установлен: $(docker --version)"
else
    info "✓ Docker: $(docker --version)"
fi

if ! docker compose version &>/dev/null; then
    apt-get install -y -q docker-compose-plugin
fi
info "✓ Docker Compose: $(docker compose version)"

# Настройки Docker daemon (лимиты логов)
if [[ ! -f /etc/docker/daemon.json ]]; then
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json << 'DAEMON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "default-ulimits": {
    "nofile": { "Name": "nofile", "Hard": 65536, "Soft": 65536 }
  }
}
DAEMON
    systemctl restart docker
    info "✓ Docker daemon настроен"
fi

# ════════════════════════════════════════════════════════════
# ШАГ 3: Пользователь deploy
# ════════════════════════════════════════════════════════════
step "Шаг 3/8: Пользователь deploy"
if ! id "${DEPLOY_USER}" &>/dev/null; then
    useradd -m -s /bin/bash -G docker,sudo "${DEPLOY_USER}"
    info "✓ Пользователь ${DEPLOY_USER} создан"
else
    usermod -aG docker "${DEPLOY_USER}" 2>/dev/null || true
    info "✓ Пользователь ${DEPLOY_USER} уже существует"
fi

# Копировать SSH ключи root → deploy
if [[ -f "/root/.ssh/authorized_keys" ]]; then
    mkdir -p "/home/${DEPLOY_USER}/.ssh"
    cp -n /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/" 2>/dev/null || true
    chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
    chmod 700 "/home/${DEPLOY_USER}/.ssh"
    chmod 600 "/home/${DEPLOY_USER}/.ssh/authorized_keys" 2>/dev/null || true
    info "✓ SSH ключи скопированы — можно подключаться как deploy@сервер"
fi

# ════════════════════════════════════════════════════════════
# ШАГ 4: Копирование проекта
# ════════════════════════════════════════════════════════════
step "Шаг 4/8: Копирование проекта"
mkdir -p "${PROJECT_DIR}"/{logs,artifacts,data}

rsync -a \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache/' \
    --exclude='artifacts/' \
    --exclude='data/' \
    --exclude='catboost_info/' \
    --exclude='registry.json' \
    --exclude='mlflow/' \
    ./ "${PROJECT_DIR}/"

install -m 600 .env "${PROJECT_DIR}/.env"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${PROJECT_DIR}"
chmod 600 "${PROJECT_DIR}/.env"
info "✓ Проект скопирован в ${PROJECT_DIR}"

# ════════════════════════════════════════════════════════════
# ШАГ 5: Nginx
# ════════════════════════════════════════════════════════════
step "Шаг 5/8: Nginx"
apt-get install -y -q nginx certbot python3-certbot-nginx

SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "localhost")
SERVER_NAME="${DOMAIN:-${SERVER_IP}}"

cat > /etc/nginx/sites-available/sku-forecasting << NGINX
server {
    listen 80;
    server_name ${SERVER_NAME};

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
        client_max_body_size 100M;
    }

    location /metrics {
        deny all;
        return 403;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/sku-forecasting /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
info "✓ Nginx настроен (server_name: ${SERVER_NAME})"

# ════════════════════════════════════════════════════════════
# ШАГ 6: Firewall + fail2ban
# ════════════════════════════════════════════════════════════
step "Шаг 6/8: Firewall"

# ВАЖНО: Docker обходит ufw через iptables напрямую.
# Поэтому все Docker-порты в docker-compose привязаны к 127.0.0.1 —
# это единственный надёжный способ закрыть их снаружи.
# ufw здесь защищает только SSH/HTTP/HTTPS.

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp  comment 'SSH'
ufw allow 80/tcp  comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable
info "✓ ufw настроен (открыты только 22, 80, 443)"

# fail2ban — защита SSH от брутфорса
cat > /etc/fail2ban/jail.local << 'F2B'
[sshd]
enabled  = true
port     = ssh
maxretry = 5
bantime  = 3600
findtime = 600
F2B
systemctl enable fail2ban
systemctl restart fail2ban
info "✓ fail2ban настроен"

# ════════════════════════════════════════════════════════════
# ШАГ 7: Сборка и запуск Docker
# ════════════════════════════════════════════════════════════
step "Шаг 7/8: Docker сборка и запуск"
cd "${PROJECT_DIR}"

if docker images 2>/dev/null | grep -q "sku-forecasting-api"; then
    info "Образы найдены — пересобираем (быстрее, без --no-cache)..."
    ${COMPOSE} build
else
    info "Первая сборка — около 5-15 минут..."
    ${COMPOSE} build --no-cache
fi

${COMPOSE} up -d
info "✓ Контейнеры запущены"

# ════════════════════════════════════════════════════════════
# ШАГ 8: Systemd автозапуск
# ════════════════════════════════════════════════════════════
step "Шаг 8/8: Systemd автозапуск"

cat > /etc/systemd/system/sku-forecasting.service << SYSTEMD
[Unit]
Description=SKU Forecasting Platform v8.0
After=network.target docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=/usr/bin/docker compose -f docker/docker-compose.beget.yml up -d
ExecStop=/usr/bin/docker compose  -f docker/docker-compose.beget.yml down
ExecReload=/usr/bin/docker compose -f docker/docker-compose.beget.yml restart api worker
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable sku-forecasting.service
info "✓ Systemd сервис создан: автозапуск при перезагрузке включён"

# ════════════════════════════════════════════════════════════
# Финальная проверка
# ════════════════════════════════════════════════════════════
step "Финальная проверка"
info "Ожидание запуска API (до 60 сек)..."

API_OK=false
for i in $(seq 1 12); do
    if curl -sf --max-time 5 http://localhost:8000/health > /dev/null 2>&1; then
        API_OK=true
        break
    fi
    echo -n "  ожидание [${i}/12]..."$'\r'
    sleep 5
done
echo ""

echo ""
${COMPOSE} ps
echo ""

if ${API_OK}; then
    HEALTH=$(curl -s http://localhost:8000/health)
    info "✓ API работает: ${HEALTH}"
else
    warn "API не ответил за 60 сек. Проверьте:"
    warn "  ${COMPOSE} logs api --tail=30"
fi

# ════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Установка завершена!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo ""
echo "  API:        http://${SERVER_IP}"
echo ""
echo "  Внутренние сервисы — только через SSH-туннель:"
echo "  ┌─────────────────────────────────────────────────────────────┐"
echo "  │ Сервис   │ Команда туннеля                     │ Браузер    │"
echo "  ├─────────────────────────────────────────────────────────────┤"
echo "  │ Vault UI │ ssh -L 8200:localhost:8200 deploy@${SERVER_IP} │ :8200 │"
echo "  │ Grafana  │ ssh -L 3000:localhost:3000 deploy@${SERVER_IP} │ :3000 │"
echo "  │ MLflow   │ ssh -L 5000:localhost:5000 deploy@${SERVER_IP} │ :5000 │"
echo "  └─────────────────────────────────────────────────────────────┘"
echo ""
echo "  Полезные команды:"
echo "    Логи API:    ${COMPOSE} logs api -f --tail=100"
echo "    Статус:      ${COMPOSE} ps"
echo "    Restart:     ${COMPOSE} restart api"
echo "    Обновить:    ${COMPOSE} pull && ${COMPOSE} up -d"
echo ""
if [[ "${SERVER_NAME}" == "${SERVER_IP}" ]]; then
    echo -e "${YELLOW}  ⚠ TLS не настроен. Если есть домен, выполните:${NC}"
    echo "    certbot --nginx -d ваш-домен.com"
    echo ""
fi
echo "  Следующий шаг — настроить Yandex Lockbox:"
echo "    см. deploy/DEPLOY.md §4.3 (Lockbox setup)"
echo "    1) создать секрет в YC + viewer SA-ключ"
echo "    2) положить SA-ключ в ${PROJECT_DIR}/secrets/yc-sa-key.json"
echo "    3) выставить YC_SA_KEY_FILE + YC_LOCKBOX_SECRET_ID в .env"
echo "    4) docker compose ... up -d (Lockbox bootstrap автоматический)"
echo ""
