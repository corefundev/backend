# SKU Forecasting — развёртывание на чистых VPS (Beget)

Цельная инструкция «с нуля» — от покупки VPS до живой HTTPS-витрины с
админом, который может залогиниться и сменить тариф.

Структура:
1. [Обзор и что нам понадобится](#1-обзор)
2. [Создание ресурсов в Beget (S3, VPS, домены)](#2-beget)
3. [Backend-VPS: подготовка ОС](#3-backend-vps-ос)
4. [Backend-VPS: секреты и Vault](#4-backend-vps-секреты)
5. [Backend-VPS: docker-сервисы](#5-backend-vps-docker)
6. [Backend-VPS: TLS через Let's Encrypt](#6-backend-vps-tls)
7. [Backend-VPS: smoke-тесты](#7-backend-vps-smoke)
8. [Frontend-VPS: сборка и деплой](#8-frontend-vps)
9. [Первый запуск админки и пользователя](#9-первый-запуск)
10. [Операционка: бэкапы, мониторинг, апдейты, откат](#10-опс)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Обзор

Система живёт на **двух VPS**:

| VPS | Роль | Порты | Зачем отдельно |
|---|---|---|---|
| `backend.example.com` | API (FastAPI) + очередь RQ + воркеры (training/scan/process) + Postgres + Redis + MinIO-proxy + ClamAV + MLflow + Airflow + Prometheus + Grafana + Alertmanager + Nginx + Certbot | 22, 80, 443 | Хранит данные и обучает модель |
| `web.example.com` | Только SPA (Vite build под Nginx) + Certbot | 22, 80, 443 | Статика, максимально близко к юзеру, легко масштабируется через CDN |

Данные ходят через **четыре независимых S3 Beget**:

| Зона | Для чего | Кто пишет | Кто читает |
|---|---|---|---|
| `sku-untrusted`  | Сырые CSV/XLSX от клиента | API | scan-worker |
| `sku-quarantine` | Чистые после ClamAV | scan-worker | process-worker (RO) |
| `sku-processed`  | Валидные parquet'ы + модели + метрики | process-worker, training | API, training |
| `sku-backups`    | Зашифрованные pg_dump'ы | backup (crond) | только восстановление вручную |

Каждая зона — **отдельный аккаунт Beget S3** со своим keypair'ом. Компрометация
одного ключа не даёт доступа к трём другим. Backup-бакет особенно важен:
даже если злоумышленник получит processed-ключ, стереть резервные копии
он не сможет.

### Что нужно заготовить заранее

- Аккаунт **Beget** с доступом к Object Storage и созданию VPS.
- Домен (или два поддомена), DNS под контролем.
- Slack webhook (опц., для алертов).
- Локальная машина с `ssh`, `curl`, `pwgen` (или `openssl rand`).

---

## 2. Beget

### 2.1. Четыре S3-аккаунта

В панели Beget → **Object Storage** создать:

1. Владелец 1 → ключ `KEY_UNTRUSTED` → бакет `sku-untrusted-<random4>`
2. Владелец 2 → ключ `KEY_QUARANTINE` → бакет `sku-quarantine-<random4>`
3. Владелец 3 → ключ `KEY_PROCESSED` → бакет `sku-processed-<random4>`
4. Владелец 4 → ключ `KEY_BACKUP` → бакет `sku-backups-<random4>`
   *(идеально — этот владелец в отдельном биллинге / другом сервисе:
   AWS S3, Hetzner Object Storage. Если бэкапы лежат рядом с prod-данными,
   катастрофа прода одновременно убивает бэкапы.)*

> Beget иногда не даёт заводить несколько владельцев в одном аккаунте —
> тогда используйте подаккаунты или временные аккаунты только ради
> keypair'ов. Главное: **четыре разных keypair'а**.

На каждый бакет в политике объектов:
- **Public access:** `Private` (никакого listing извне).
- **Endpoint:** обычно `https://s3.ru1.storage.beget.cloud` (уточнить в панели).
- **Region:** `ru-1`.

Сохранить на бумаге (или в менеджере паролей):

```
S3_UNTRUSTED_BUCKET            = sku-untrusted-abcd
S3_UNTRUSTED_ENDPOINT_URL      = https://s3.ru1.storage.beget.cloud
S3_UNTRUSTED_ACCESS_KEY_ID     = …
S3_UNTRUSTED_SECRET_ACCESS_KEY = …

S3_QUARANTINE_BUCKET            = sku-quarantine-efgh
S3_QUARANTINE_ENDPOINT_URL      = https://s3.ru1.storage.beget.cloud
S3_QUARANTINE_ACCESS_KEY_ID     = …
S3_QUARANTINE_SECRET_ACCESS_KEY = …

S3_PROCESSED_BUCKET            = sku-processed-ijkl
S3_PROCESSED_ENDPOINT_URL      = https://s3.ru1.storage.beget.cloud
S3_PROCESSED_ACCESS_KEY_ID     = …
S3_PROCESSED_SECRET_ACCESS_KEY = …

S3_BACKUP_BUCKET            = sku-backups-mnop
S3_BACKUP_ENDPOINT_URL      = https://s3.ru1.storage.beget.cloud
S3_BACKUP_ACCESS_KEY_ID     = …
S3_BACKUP_SECRET_ACCESS_KEY = …
```

### 2.2. Два VPS

Минимальные характеристики:

| | Backend | Frontend |
|---|---|---|
| CPU | 4 vCPU | 1–2 vCPU |
| RAM | 8 ГБ | 1 ГБ |
| Disk | 80 ГБ SSD | 20 ГБ |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| IPv4 | публичный | публичный |

### 2.3. DNS

Поставить A-записи (TTL 300):

```
api.example.com  →  <BACKEND_VPS_IP>
web.example.com  →  <FRONTEND_VPS_IP>
```

Подождать распространения (`dig +short api.example.com`).

---

## 3. Backend-VPS: ОС

### 3.1. Базовая подготовка (как root)

```bash
ssh root@<BACKEND_VPS_IP>

# Обновление
apt-get update && apt-get -y upgrade

# Timezone + locale
timedatectl set-timezone UTC
locale-gen en_US.UTF-8 ru_RU.UTF-8

# Создать деплой-пользователя (не root-ом пользуемся дальше)
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

Пере-залогиниться под `deploy`:

```bash
ssh deploy@<BACKEND_VPS_IP>
```

### 3.2. Docker

```bash
# Официальный Docker репо
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Разрешить deploy без sudo
sudo usermod -aG docker deploy

# ВАЖНО: выйти и зайти заново, чтобы groupadd подхватился
exit
ssh deploy@<BACKEND_VPS_IP>

docker version
docker compose version
```

### 3.3. Firewall + fail2ban

```bash
sudo apt-get install -y ufw fail2ban

# UFW: закрыто всё, открыты 22/80/443
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

# fail2ban — защита SSH (дефолтный jail sshd уже включён)
sudo systemctl enable --now fail2ban
```

> **Осторожно с Docker.** Docker пробивает UFW через iptables напрямую.
> Наш docker-compose биндит только те порты, которые реально должны
> быть наружу (80/443 через nginx). Всё остальное — на внутренней
> сети docker'а. **Не выставляйте в compose `ports:` для БД, redis,
> minio и т.д.** — проверяйте при каждой правке compose.

### 3.4. Клонирование и checkout

```bash
mkdir -p /srv && cd /srv
sudo chown deploy:deploy /srv

git clone https://github.com/your-org/sku-forecasting.git backend
cd backend

# Работаем на стабильном теге
git fetch --tags
git checkout v1.0.0    # подставьте последний релиз
```

---

## 4. Backend-VPS: секреты

> **Выбор хранилища секретов — сделай ОДИН раз перед чтением остальных
> подразделов:**
>
> | Стек | Что читать | Где живут секреты |
> |---|---|---|
> | **Beget + Yandex Cloud (твой случай)** | §4.1 → **§4.3 (Lockbox)** | Yandex Lockbox |
> | Любой облачный AWS/Azure/GCP | §4.1 → §4.2 (Vault + KMS) | Self-hosted Vault, auto-unseal через provider KMS |
> | Локальная разработка / тесты | §4.1 (Level 1, всё в `.env`) | Файл `.env` на диске |
>
> **§4.2 (Vault + AWS KMS) НЕ для тебя.** Пропусти его и сразу к §4.3.
> Вернёшься позже, если будут жёсткие compliance-требования или
> переезд на AWS.

### 4.1. Сгенерировать ключи

```bash
cd /srv/backend

cp .env.example .env
chmod 600 .env

# Все секреты заранее:
python3 -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('API_KEY=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('ADMIN_API_KEY=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('MODEL_SIGNING_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('BACKUP_PASSPHRASE=' + secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('AIRFLOW_FERNET_KEY=' + Fernet.generate_key().decode())" || \
python3 -c "import base64, os; print('AIRFLOW_FERNET_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())"
python3 -c "import secrets; print('AIRFLOW_SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('MINIO_USER=minio-' + secrets.token_hex(4))"
python3 -c "import secrets; print('MINIO_PASSWORD=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('GRAFANA_PASSWORD=' + secrets.token_urlsafe(16))"
```

Скопировать в `.env`, плюс доменные:

```bash
nano .env
```

```ini
# Обязательные переменные — подставить из пункта 2.1 и сгенерированных выше
APP_ENV=production
JWT_SECRET_KEY=...
API_KEY=...
ADMIN_API_KEY=...
MODEL_SIGNING_KEY=...
BACKUP_PASSPHRASE=...
POSTGRES_PASSWORD=...
MINIO_USER=...
MINIO_PASSWORD=...
AIRFLOW_USER=admin
AIRFLOW_PASSWORD=...
AIRFLOW_FERNET_KEY=...
AIRFLOW_SECRET_KEY=...
GRAFANA_PASSWORD=...

# Storage mode
STORAGE_BACKEND=s3

# ── Три независимых Beget S3 (из пункта 2.1) ────────────────
S3_UNTRUSTED_BUCKET=sku-untrusted-abcd
S3_UNTRUSTED_ENDPOINT_URL=https://s3.ru1.storage.beget.cloud
S3_UNTRUSTED_ACCESS_KEY_ID=…
S3_UNTRUSTED_SECRET_ACCESS_KEY=…
S3_UNTRUSTED_REGION=ru-1

S3_QUARANTINE_BUCKET=sku-quarantine-efgh
S3_QUARANTINE_ENDPOINT_URL=https://s3.ru1.storage.beget.cloud
S3_QUARANTINE_ACCESS_KEY_ID=…
S3_QUARANTINE_SECRET_ACCESS_KEY=…
S3_QUARANTINE_REGION=ru-1

S3_PROCESSED_BUCKET=sku-processed-ijkl
S3_PROCESSED_ENDPOINT_URL=https://s3.ru1.storage.beget.cloud
S3_PROCESSED_ACCESS_KEY_ID=…
S3_PROCESSED_SECRET_ACCESS_KEY=…
S3_PROCESSED_REGION=ru-1

# processed используется как основной бакет для моделей + бэкапов MLflow
S3_BUCKET=sku-processed-ijkl
S3_ENDPOINT_URL=https://s3.ru1.storage.beget.cloud
AWS_ACCESS_KEY_ID=…       # = S3_PROCESSED_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=…   # = S3_PROCESSED_SECRET_ACCESS_KEY
AWS_DEFAULT_REGION=ru-1

# Бэкапы — отдельный 4-й S3-аккаунт со своим keypair'ом
S3_BACKUP_BUCKET=sku-backups-mnop
S3_BACKUP_ENDPOINT_URL=https://s3.ru1.storage.beget.cloud
S3_BACKUP_ACCESS_KEY_ID=…
S3_BACKUP_SECRET_ACCESS_KEY=…
S3_BACKUP_REGION=ru-1

# CORS — фронт на другом домене
FRONTEND_ORIGINS=https://web.example.com

# Домен API + почта для LE
API_DOMAIN=api.example.com
CERTBOT_EMAIL=ops@example.com
CERTBOT_STAGING=0   # 1 — пока тестируем, убрать когда всё рабочее

# Admin CIDR (куда открываем /metrics через публичный nginx)
ADMIN_CIDR=<ВАШ_IP>/32

# Sync fallback выключен в проде
ALLOW_SYNC_UPLOAD_FALLBACK=0
```

### 4.2. Level 3 (вариант A) — Vault + AWS/GCP/Azure KMS

> **⚠ Это путь для облачных стеков AWS/Azure/GCP.** На Beget+YC есть
> более простая альтернатива — **Yandex Lockbox** в §4.3. Если ты на
> Beget — пропускай этот раздел и иди к §4.3.

Этот вариант имеет смысл когда:
- проект мигрирует с условного AWS и уже есть KMS
- compliance требует именно HashiCorp Vault (есть аудиторские профили под него)
- нужны dynamic database credentials, leases, secret-engines из Vault

Если ничего из этого — переходи к §4.3.

`.env` из пункта 4.1 — это **Level 1**: секреты лежат файлом. Этого
достаточно для первого прогона и для проектов с доступом только у тебя.
В production хочется **Level 3**: в `.env` остаются только идентификаторы
и короткоживущий `SECRET_ID`, а настоящие ключи хранит отдельный Vault.

#### Что ты получаешь

- `.env` на диске не содержит ни одного валидного секрета (ролевая
  идентификация + одноразовый токен с TTL 10 мин).
- Утечка диска ≠ компрометация прода.
- Аудит-лог: Vault пишет, кто и когда прочитал какой ключ.
- Возможность ротации: поменять `JWT_SECRET_KEY` — одна команда в Vault,
  приложения подхватят при рестарте.

#### Цена

Vault после каждого reboot'а стартует **sealed** (запечатан). Пока его
не раскроют — приложение не получит секреты. Три решения:

| Режим | Поведение при reboot | Что нужно |
|---|---|---|
| `awskms` (рекомендуется) | Автоматически распечатывается через AWS KMS | AWS-аккаунт, один CMK, IAM user (~$1/мес) |
| `transit` | Автоматически через другой «корневой» Vault | Второй Vault где-то снаружи |
| `manual` | Оператор вводит 3 ключа командой `vault operator unseal` | Ничего |

**Если твои ребуты случаются 1–4 раза в год** (security-patches на ядро
ОС) — `manual` вполне рабочий вариант, только держи 5 ключей у 5 разных
людей (Shamir), чтобы команду могли собрать без тебя.

**Если ребуты чаще** — ставь `awskms`.

#### Шаг 1. Подготовить AWS KMS (только для режима `awskms`)

```
AWS Console → KMS → Create key → Symmetric, Encrypt/Decrypt
Alias: sku-vault-unseal
Region: eu-north-1 (или любой, но тот же укажем в .env)

IAM Console → Users → Create user (sku-vault-kms)
  Access type: Programmatic access
  Permissions: inline JSON →
    {
      "Version":"2012-10-17",
      "Statement":[{
        "Effect":"Allow",
        "Action":["kms:Encrypt","kms:Decrypt","kms:DescribeKey"],
        "Resource":"arn:aws:kms:eu-north-1:<acct>:key/<key-id>"
      }]
    }

Сохранить: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, key ARN.
```

#### Шаг 2. Заменить `.env` на Level 3 шаблон

```bash
cd /srv/backend

# Сохрани текущий .env — он нам ещё нужен для seeding'а Vault'а:
mv .env .env.level1.bak
chmod 600 .env.level1.bak

cp .env.level3.example .env
chmod 600 .env
nano .env
```

Заполни обязательные поля:

```ini
API_DOMAIN=api.example.com
CERTBOT_EMAIL=ops@example.com
ADMIN_CIDR=<твой IP>/32
FRONTEND_ORIGINS=https://web.example.com

VAULT_ADDR=http://vault:8200
VAULT_ROLE_ID=         # заполним после vault_init.sh
VAULT_SECRET_ID=       # заполним после vault_init.sh

VAULT_SEAL_MODE=awskms                              # или "manual" для Shamir
VAULT_AWSKMS_KEY_ID=arn:aws:kms:...:key/...
VAULT_AWS_REGION=eu-north-1
VAULT_AWS_ACCESS_KEY_ID=AKIA...
VAULT_AWS_SECRET_ACCESS_KEY=...
```

#### Шаг 3. Поднять Vault + приложение

Используем три overlay'а:

```bash
docker compose \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.vault.yml \
    up -d vault
```

Пока приложение **НЕ ПОДНИМАЕМ** — в новом `.env` нет реальных секретов,
и оно упадёт на старте. Сначала загрузим всё в Vault.

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.vault.yml logs -f vault | head
# ...Vault started: core: successfully initialized a new seal: type=awskms
# (ждём, пока появится строка "core: security barrier not initialized")
```

#### Шаг 4. Инициализировать Vault (один раз в жизни)

```bash
./scripts/vault_init.sh
# ════════════════════════════════════════════════════════════════════
#   Vault initialised. Credentials written to ../vault-init.secrets
#   NEXT STEPS:
#   1. cat ../vault-init.secrets   # сохрани всё в менеджер паролей
#   2. shred -u ../vault-init.secrets
#   3. ./scripts/vault_load_env.sh .env.level1.bak
#   4. Put VAULT_ROLE_ID=... into .env
# ════════════════════════════════════════════════════════════════════
```

**КРИТИЧЕСКИ ВАЖНО** сейчас:

1. `cat vault-init.secrets` → скопировать весь вывод в **1Password / KeePass / Bitwarden**.
   Там пять recovery-ключей (если `awskms`) или пять unseal-ключей (если `manual`), root token, ROLE_ID, начальный SECRET_ID.
2. `shred -u vault-init.secrets` — удалить с диска без возможности восстановления.
3. Скопировать `VAULT_ROLE_ID` из менеджера паролей в `.env`.

#### Шаг 5. Загрузить секреты из старого `.env` в Vault

```bash
# Достаём root token из password manager'а:
export VAULT_TOKEN='hvs.ABCDE...'

./scripts/vault_load_env.sh .env.level1.bak
# [vault_load_env] pushing 23 keys to secret/sku-forecasting
# [vault_load_env] done.

# Убедиться, что всё на месте:
docker compose -f docker/docker-compose.yml -f docker/docker-compose.vault.yml exec \
    -e VAULT_TOKEN="$VAULT_TOKEN" vault \
    vault kv get secret/sku-forecasting | head -20

# Убить root token — больше не понадобится, дальше AppRole
unset VAULT_TOKEN

# Стираем старый .env
shred -u .env.level1.bak
```

#### Шаг 6. Ротация SECRET_ID и первый запуск

`SECRET_ID` живёт 10 минут и на одно использование — значит, каждый
`docker compose up -d` должен получать новый. Нужен root token один раз
для генерации — но потом настроим cron/CI, которые делают это без него.

```bash
# Один раз — используем root token чтобы выдать первый SECRET_ID:
export VAULT_TOKEN='hvs.ABCDE...'
SECRET_ID=$(./scripts/vault_rotate_secret_id.sh)
unset VAULT_TOKEN

# Вписываем в .env (или экспортируем inline)
echo "VAULT_SECRET_ID=$SECRET_ID" >> .env

# Запускаем приложение — оно аутентифицируется через AppRole и
# подтягивает все секреты из Vault в os.environ
docker compose \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.vault.yml \
    up -d
```

Проверка:

```bash
docker compose logs api | grep -i vault
# Bootstrap: secrets from Vault AppRole (Level 3 via env)

# Убедиться, что SECRET_ID уже израсходован (одноразовый)
# — любая попытка использовать его снова упадёт с 400
```

#### Шаг 7. Убрать SECRET_ID из `.env` (финальный штрих)

Приложение уже подтянуло секреты и использовало SECRET_ID. Его можно
убрать — при следующем `up -d` мы сгенерируем новый:

```bash
sed -i '/^VAULT_SECRET_ID=/d' .env
```

Теперь в `.env` нет ничего секретного, кроме `VAULT_AWS_*` (которые
нужны самому Vault для auto-unseal). Если хочешь убрать и их — вынеси
в `/etc/environment` от root'а или в docker secrets (но тогда compose
надо переделывать под swarm).

#### Шаг 8. Автоматизация ротации SECRET_ID (для будущих деплоев)

Создай `/srv/backend/deploy.sh`:

```bash
#!/bin/sh
set -eu
cd /srv/backend
# Root token храним в небольшом CI-only контейнере или используем
# response-wrapping (vault-agent auto_auth). Для простоты — из stdin:
read -s -p "Vault root token: " VAULT_TOKEN; echo
export VAULT_TOKEN

VAULT_SECRET_ID="$(./scripts/vault_rotate_secret_id.sh)" \
  docker compose \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.vault.yml \
    up -d
```

Или, если настроил CI: пусть pipeline хранит root token в GH Secrets /
GitLab CI и вызывает `vault_rotate_secret_id.sh` + `docker compose up -d`
через SSH.

#### Поведение после reboot

| Seal mode | Что происходит | Что надо делать |
|---|---|---|
| `awskms`  | Vault сам распечатывается через KMS; app дожидается healthy Vault и подтягивает секреты. | **Ничего.** Но помни: SECRET_ID в `.env` если остался после старта — уже протух, app после reboot уже получила свежие секреты и в памяти они есть. Если app рестартнётся (не VPS), новый SECRET_ID понадобится. |
| `transit` | Аналогично | Ничего |
| `manual`  | Vault sealed, app получает 503 от `bootstrap_secrets()` и не стартует | Зайти по SSH, `docker compose exec vault vault operator unseal <key>` три раза, потом `docker compose up -d` |

#### Бэкап самого Vault

Vault — это ещё один источник истины. Его надо бэкапить отдельно от
Postgres:

```bash
# raft snapshot
docker compose exec vault \
    sh -c 'VAULT_TOKEN=... vault operator raft snapshot save /tmp/vault.snap'
docker cp sku-vault:/tmp/vault.snap ./vault-$(date -u +%Y%m%d).snap
# Зашифровать + положить в тот же бэкап-бакет
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in vault-*.snap -out vault-*.snap.enc -pass env:BACKUP_PASSPHRASE
mc cp vault-*.snap.enc bak/$S3_BACKUP_BUCKET/vault/
shred -u vault-*.snap*
```

Добавь это в `backup.sh` как шаг 5, если хочешь унифицировать.

---

### 4.3. Level 3 (вариант B) — Yandex Lockbox  ⭐ для Beget+YC

**Это твой путь.** Lockbox — встроенный secret manager Yandex Cloud,
проще Vault и без AWS-зависимости:

- **Нет seal-состояния** — после reboot'а ничего не нужно распечатывать.
- **YC IAM как access control** — никакого AppRole, просто service account
  с ролью `lockbox.payloadViewer` на конкретный секрет.
- **Версионирование из коробки** — можно откатиться на предыдущую
  версию секрета одной командой.
- **Цена:** порядка 50 копеек за 10 тыс. запросов к API — при нашем
  ~10 запросов/сутки практически бесплатно.

**Ограничение:** vendor lock. Код через REST API (см.
`src/auth/lockbox_agent.py`), а не Vault-SDK.

#### Шаг 1. Создать сервисный аккаунт и секрет

На локальной машине (нужен `yc` CLI и `yc init`):

```bash
FOLDER_ID=$(yc config get folder-id)

# 1. Сервисный аккаунт
yc iam service-account create --name sku-lockbox-reader \
    --description "Read-only access to Lockbox for SKU forecasting"

SA_ID=$(yc iam service-account get --name sku-lockbox-reader --format json | \
        jq -r .id)

# 2. Выдать ему роль lockbox.payloadViewer на папку
yc resource-manager folder add-access-binding "$FOLDER_ID" \
    --role lockbox.payloadViewer \
    --subject serviceAccount:"$SA_ID"

# 3. Сгенерить авторизованный ключ (JSON с RSA private_key внутри)
yc iam key create --service-account-name sku-lockbox-reader \
    --output sku-sa-key.json

chmod 600 sku-sa-key.json
```

#### Шаг 2. Загрузить `.env` в Lockbox

```bash
cd /srv/backend    # или где лежит .env.level1.bak

./scripts/lockbox_load_env.sh .env.level1.bak sku-forecasting-secrets
# ...
# ──────────────────────────────────────────────────────────────
#   Put this into your .env on the backend VPS:
#     YC_LOCKBOX_SECRET_ID=e6q123abc...
# ──────────────────────────────────────────────────────────────
```

Скрипт фильтрует инфра-переменные (VAULT_*, YC_*, домены, CORS) —
в Lockbox летят только настоящие секреты.

#### Шаг 3. Подготовить backend VPS

```bash
# По SSH на backend-VPS:
cd /srv/backend

# Переложить .env на Lockbox-шаблон
mv .env .env.level1.bak
cp .env.lockbox.example .env
chmod 600 .env
nano .env   # впиши YC_LOCKBOX_SECRET_ID=e6q... + API_DOMAIN + CORS

# Положить ключ SA в ./secrets/yc/yc-sa-key.json (DIR-mount, #303 — контейнеры
#   монтируют каталог secrets/yc, а не файл: file-mount пиннит инод и
#   ротация оставляет контейнер на отозванном ключе, инцидент #190)
mkdir -p secrets/yc && chmod 700 secrets && chmod 755 secrets/yc
# Скопируй туда файл sku-sa-key.json с локальной машины через scp:
#   scp sku-sa-key.json deploy@<BACKEND_IP>:/srv/backend/secrets/yc/yc-sa-key.json
chmod 644 secrets/yc/yc-sa-key.json   # 644: containers run as 3 UIDs (root/999/65534); parent dir 700 is the guard (R5-21)

# Добавить /secrets/ в .gitignore (один раз)
grep -qxF "secrets/" .gitignore || echo "secrets/" >> .gitignore
```

#### Шаг 4. Поднять с Lockbox-overlay

```bash
docker compose \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.lockbox.yml \
    up -d
```

Первый `up -d` → контейнеры монтируют `yc-sa-key.json` → `lockbox_agent.py`
при старте обменивает JWT на IAM-токен → читает `secret/sku-forecasting-secrets` →
раскладывает всё в `os.environ` → API стартует как обычно.

Проверка:

```bash
docker compose logs api | grep -i "lockbox\|bootstrap"
# ... Bootstrap: secrets from Yandex Lockbox
# ... Lockbox bootstrap: injected N variables from secret e6q...

# На уровне сервиса — всё поднялось?
curl https://api.example.com/readyz
# {"ready":true,"checks":{...}}
```

#### Шаг 5. Стереть старый `.env.level1.bak`

```bash
shred -u .env.level1.bak
```

#### Ротация секрета

Новая версия секрета — без даунтайма:

```bash
# На локальной машине (там yc CLI с правами lockbox.admin)
./scripts/lockbox_load_env.sh .env.new.txt sku-forecasting-secrets
# → add-version, старая версия остаётся для отката

# На backend VPS — перезапустить контейнеры, чтобы они прочитали новую версию:
ssh deploy@<BACKEND_IP> \
    "cd /srv/backend && docker compose \
        -f docker/docker-compose.yml \
        -f docker/docker-compose.prod.yml \
        -f docker/docker-compose.lockbox.yml \
        up -d --force-recreate api worker scan-worker process-worker"
```

Откат:

```bash
# Найти предыдущий version ID
yc lockbox secret list-versions --name sku-forecasting-secrets
# Отметить его current:
yc lockbox secret schedule-version-destruction --id <version-id>
# (или через activate предыдущей версии — см. yc lockbox secret activate-version --help)
```

#### Поведение после reboot

| | Zero-touch? |
|---|---|
| Backend VPS | ✅ Да. Docker рестартует контейнеры, lockbox_agent.py фетчит секреты, всё поднимается ~за 60–90 секунд. |

На диске остаётся только **SA-ключ**. Защита:
- `chmod 644 secrets/yc/yc-sa-key.json   # 644: containers run as 3 UIDs (root/999/65534); parent dir 700 is the guard (R5-21)`
- SA имеет ровно одну роль — `lockbox.payloadViewer` на **один конкретный** секрет
- Сам ключ **можно ротировать** без даунтайма: `yc iam key create`,
  подменить файл, `docker compose restart api worker scan-worker process-worker`,
  `yc iam key delete <старый-ключ-id>`

#### Сравнение Vault vs Lockbox

| | Vault (awskms) | Vault (manual) | Lockbox |
|---|---|---|---|
| Reboot автомат? | ✅ | ❌ | ✅ |
| Нужен внешний KMS? | AWS | нет | нет |
| Новая инфра на VPS | +1 контейнер | +1 контейнер | нет |
| Секрет на диске | AWS creds | ничего | SA-ключ |
| Ротация секрета | 2 команды | 2 команды | 1 команда |
| Аудит чтений | встроен | встроен | YC audit trails |
| Vendor lock | AWS + Vault | Vault | YC |

#### Авторотация SA-ключа (важно)

SA-ключ Lockbox-reader — это единственный живой секрет на VPS.
Хорошая практика — ротировать его раз в неделю. Даже если ключ
утёк в понедельник, в воскресенье он протухнет.

**Почему ротация запускается не на VPS, а снаружи:**
для ротации нужны права «создавать/удалять authorized key» — это
МОЩНЕЕ чем сам Lockbox-reader. Если положить эти creds на prod-VPS,
компрометация VPS даст атакующему не read-only ключ, а возможность
создавать новые. Ротация **ухудшит** posture, а не улучшит.

**Правильная схема:** мастер-креды живут в GitHub Actions secrets,
workflow запускается по cron раз в неделю, подключается к VPS по SSH
deploy-ключу, ротирует и кладёт новый JSON через атомарный rename.

##### Шаг 1. Создать отдельный SA `sku-key-rotator`

```bash
yc iam service-account create --name sku-key-rotator \
    --description "Rotates authorized keys for sku-lockbox-reader"

# Права — ТОЛЬКО на один конкретный SA, не на всю папку.
# Это важно: rotator не должен иметь права лезть ни в S3, ни в
# Lockbox, ни создавать других SA. Только ротация "подопечного".
SA_ROTATOR_ID=$(yc iam service-account get --name sku-key-rotator --format json | jq -r .id)
SA_READER_ID=$(yc iam service-account get --name sku-lockbox-reader --format json | jq -r .id)

yc iam service-account add-access-binding "$SA_READER_ID" \
    --role iam.serviceAccounts.admin \
    --subject serviceAccount:"$SA_ROTATOR_ID"

# Ключ для rotator'а — тот, что пойдёт в GitHub Secrets.
yc iam key create --service-account-name sku-key-rotator \
    --output rotator-key.json
chmod 600 rotator-key.json
```

##### Шаг 2. Deploy-ключ для SSH на VPS

На VPS создать пользователя, от имени которого будет ходить CI.
У пользователя должны быть права на `docker compose` (через группу
docker), но НЕ на sudo — иначе компрометация CI даст root на VPS.

```bash
# На VPS:
sudo useradd -m -s /bin/bash -G docker ci-deploy
sudo mkdir -p /home/ci-deploy/.ssh
sudo chown ci-deploy:ci-deploy /home/ci-deploy/.ssh
sudo chmod 700 /home/ci-deploy/.ssh

# Разрешить запись в /srv/backend/secrets/yc/yc-sa-key.json и restart compose
sudo chown -R ci-deploy:ci-deploy /srv/backend/secrets
```

На локальной машине:

```bash
# Сгенерировать dedicated deploy key (ed25519, без passphrase)
ssh-keygen -t ed25519 -f ./ci-deploy-key -N "" -C "github-actions-rotate"

# Публичную — на VPS
ssh-copy-id -i ./ci-deploy-key.pub ci-deploy@<VPS_IP>

# known_hosts — зафиксировать fingerprint для MITM-защиты
ssh-keyscan <VPS_IP> > ./ci-known-hosts
```

##### Шаг 3. Настроить GitHub Secrets

В репо → Settings → Secrets and variables → Actions:

| Secret | Значение |
|---|---|
| `YC_KEY_ROTATOR_SA_KEY` | содержимое `rotator-key.json` |
| `YC_FOLDER_ID` | `yc config get folder-id` |
| `VPS_HOST` | `api.example.com` |
| `VPS_USER` | `ci-deploy` |
| `VPS_SSH_PRIVATE_KEY` | содержимое `./ci-deploy-key` |
| `VPS_SSH_KNOWN_HOSTS` | содержимое `./ci-known-hosts` |
| `API_HEALTH_URL` | `https://api.example.com/readyz` (optional) |
| `ALERT_WEBHOOK_URL` | (optional) Slack webhook для уведомления о провалах |

После этого удалить локальные копии:

```bash
shred -u rotator-key.json ci-deploy-key ci-deploy-key.pub ci-known-hosts
```

##### Шаг 4. Активировать workflow

```bash
# Уже в репо: .github/workflows/rotate-lockbox-key.yml
# Тестовый прогон — ручной trigger через UI:
#   Actions → Rotate Lockbox SA key → Run workflow

# После первого успешного запуска cron пойдёт автоматически
# каждое воскресенье в 03:00 UTC.
```

##### Ручная срочная ротация

Если подозреваешь утечку — можешь ротировать прямо сейчас, с локальной машины (нужен `yc` CLI + SSH-доступ к VPS):

```bash
cd backend
VPS_HOST=api.example.com \
  ./scripts/rotate_lockbox_key.sh
```

Скрипт атомарен: если любая стадия упала (создание, scp, restart, readyz-проверка), старый ключ остаётся рабочим. Старые ключи удаляются ТОЛЬКО после успешного `/readyz`.

##### Что делать, если ротация упала

1. **Во время `scp` / `restart`** — старый ключ живой, VPS работает на нём. Разберись с причиной (сеть / compose), запусти снова.
2. **После restart, но `/readyz` не отвечает** — скрипт НЕ удалит старые ключи (safety window). Можно зайти по SSH и вручную вернуть предыдущий ключ из YC:
   ```bash
   yc iam key list --service-account-name sku-lockbox-reader
   # выбери второй по свежести (предыдущий)
   yc iam key create --service-account-name sku-lockbox-reader --output previous.json
   scp previous.json ci-deploy@VPS:/srv/backend/secrets/yc/yc-sa-key.json
   ssh ci-deploy@VPS "cd /srv/backend && docker compose restart api worker scan-worker process-worker"
   ```
3. **Alertmanager notify** — в workflow есть шаг `Notify on failure`, который дёрнет `ALERT_WEBHOOK_URL` (Slack/Telegram). Настрой это, чтобы не узнать через неделю.

##### Мониторинг

YC audit log пишет событие на каждое `iam.authorizedKey.create/delete`. Можно подписаться на него:
```bash
yc logging read --group-id <audit-log-group> \
    --filter 'json_payload.event_name ~ "iam.AuthorizedKey"'
```

И в Prometheus можно смотреть на возраст активного ключа (есть ли ключи старше 14 дней — alert), если завести кастомный exporter. Пока не сделано — можно проверять вручную раз в месяц.

---

### 4.4. Ротация прикладных секретов (JWT / API / MODEL / BACKUP / S3)

Lockbox-авторотация из §4.3 закрывает только SA-ключ, через который
приложение ДОХОДИТ до секретов. Сами секреты внутри Lockbox никто
автоматически не ротирует — это живые приложение-уровня ключи.

| Секрет | Что ломается при утечке | Частота ротации |
|---|---|---|
| `JWT_SECRET_KEY` | атакующий подписывает произвольные JWT | ≤ 90 дней |
| `API_KEY` | атакующий получает клиентский JWT | по событию |
| `ADMIN_API_KEY` | атакующий получает админский JWT | по событию |
| `MODEL_SIGNING_KEY` | подмена model.pkl → RCE при predict | ≤ 180 дней |
| `BACKUP_PASSPHRASE` | расшифровка pg_dump из S3 backup | при кадровых изменениях |
| `S3_*_ACCESS_KEY_ID` (×4) | доступ к соответствующей зоне | ≤ 90 дней |

Процедуры ниже предполагают Lockbox-путь (§4.3). Для Vault-пути
принцип тот же, команды отличаются (`vault kv patch` вместо
`yc lockbox`).

#### JWT_SECRET_KEY

**Эффект:** при смене **все** существующие JWT становятся невалидными
мгновенно. Пользователи получат 401, фронт автоматически редиректит
на `/login`. Для пользователей это выглядит как «разлогинили».

**Координация:** выбирай тихое окно (ночь/выходной) или сообщи
клиентам «в 03:00 UTC будет перезаход».

```bash
# 1. Сгенерить новое значение
NEW_JWT=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 2. Прочитать текущий payload, заменить поле, отправить новой версией
yc lockbox payload get --name sku-forecasting-secrets --format json \
    | jq --arg v "$NEW_JWT" \
        '.entries |= map(if .key=="JWT_SECRET_KEY" then .text_value=$v else . end)
         | {entries: .entries}' \
    > /tmp/payload.json
yc lockbox secret add-version --name sku-forecasting-secrets \
    --payload-file /tmp/payload.json
shred -u /tmp/payload.json

# 3. Перезапустить api (воркеры JWT не верифицируют, их не трогаем)
cd /srv/backend
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.prod.yml \
               -f docker/docker-compose.lockbox.yml \
               restart api

# 4. Verify — старый JWT должен получить 401
curl -H "Authorization: Bearer <OLD_JWT>" https://api.example.com/metrics
# → 401
```

**Rollback:** `yc lockbox secret activate-version --id <prev-id>` + restart.

#### API_KEY и ADMIN_API_KEY

**Эффект:** старые клиенты не смогут получить новый JWT через
`/auth/token`. Существующие JWT продолжают работать до exp (1 час).
Полный переход занимает ≤1 час.

Процедура та же, что для JWT_SECRET_KEY — поменять поле в payload,
`add-version`, `restart api`. Дополнительно:

- Разослать новый ADMIN_API_KEY админам через защищённый канал
  (1Password shared item / PGP / signed message), не через email/Slack.
- Старое значение держать в password manager 24 часа как fallback
  на случай, если кто-то не успеет обновиться.

#### MODEL_SIGNING_KEY (требует переподписки моделей)

**Критично:** если поменять без переподписки, все `/predict`
возвращают 500 с `HMAC mismatch` — приложение отказывается
загружать model.pkl.

```bash
# 1. Сгенерить новое значение (ПОКА не обновляй в Lockbox)
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 2. Получить текущее (нужно знать обе версии во время переподписки)
OLD_KEY=$(yc lockbox payload get --name sku-forecasting-secrets --format json \
          | jq -r '.entries[] | select(.key=="MODEL_SIGNING_KEY").text_value')

# 3. Переподписать все модели в S3 processed
cd /srv/backend
export S3_PROCESSED_ACCESS_KEY_ID=...
export S3_PROCESSED_SECRET_ACCESS_KEY=...
export S3_PROCESSED_BUCKET=...
export S3_PROCESSED_ENDPOINT_URL=...

# Dry-run первым (посмотреть, что скрипт найдёт)
MODEL_SIGNING_KEY_OLD="$OLD_KEY" \
MODEL_SIGNING_KEY_NEW="$NEW_KEY" \
  python3 scripts/resign_models.py --dry-run

# Настоящая переподпись
MODEL_SIGNING_KEY_OLD="$OLD_KEY" \
MODEL_SIGNING_KEY_NEW="$NEW_KEY" \
  python3 scripts/resign_models.py

# 4. Теперь безопасно обновить значение в Lockbox
yc lockbox payload get ... | jq ... | yc lockbox secret add-version ...

# 5. Рестартнуть api + training worker (кешируют модели в RAM)
docker compose restart api worker

# 6. Verify: любой /predict проходит без ошибок
```

**Важно:** храни `MODEL_SIGNING_KEY_OLD` минимум 24 часа в password
manager — на случай если надо будет проверить содержимое старых
бекапов S3 processed.

#### BACKUP_PASSPHRASE

Особенность: старые `.dump.enc` остаются зашифрованными **старым**
passphrase'ом навсегда. Новая ротация **не** перешифровывает прошлые
бекапы.

```bash
NEW_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")

# 1. Сохранить СТАРЫЙ passphrase в password manager с пометкой:
#    "для расшифровки pg_dump до YYYY-MM-DD"

# 2. Обновить в Lockbox
yc lockbox payload get ... | jq ... | yc lockbox secret add-version ...

# 3. Рестартнуть backup контейнер
docker compose restart backup

# 4. Сделать свежий дамп сейчас — чтобы последний бекап был с новым passphrase
docker compose exec backup sh -c "/scripts/backup.sh"

# 5. (Опционально) Перешифровать старые бекапы новым passphrase'ом,
#    чтобы иметь один "живой" passphrase. Для этого — ручной цикл
#    openssl -d old_pass → openssl -e new_pass для каждого файла.
```

**Compliance-примечание:** если твоя политика требует "один живой
passphrase, остальные уничтожены", см. примечание выше — нужен цикл
пересшифровки.

#### S3-ключи Beget (4 штуки)

Это ключи **Beget Object Storage**, не путать с SA-ключом Lockbox.
Каждый даёт прямой доступ к соответствующей зоне.

**Порядок — по одному ключу за раз, не всё сразу:**

```bash
# Для КАЖДОГО keypair'а (untrusted / quarantine / processed / backups):

# 1. Панель Beget → Object Storage → Keys → «Создать новый»
#    Записать new_access_key_id и new_secret_access_key.

# 2. Обновить в Lockbox соответствующие поля.
#    Для untrusted: S3_UNTRUSTED_ACCESS_KEY_ID + S3_UNTRUSTED_SECRET_ACCESS_KEY
yc lockbox payload get ... | jq ... | yc lockbox secret add-version ...

# 3. Перезапустить зависимые контейнеры:
#    untrusted  → api, scan-worker
#    quarantine → scan-worker, process-worker
#    processed  → api, worker, process-worker
#    backup     → backup
cd /srv/backend
docker compose restart api scan-worker   # пример для untrusted

# 4. Smoke: тестовая загрузка файла должна пройти весь пайплайн
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@test.csv" \
     https://api.example.com/clients/<test>/uploads

# 5. В панели Beget УДАЛИТЬ старый keypair. Только сейчас.
#    Если удалить раньше restart'а — контейнеры, которые ещё не
#    подхватили новый ключ, получат 403.
```

**Принципы:**
- По одному ключу за раз — иначе легко запутаться, какой где.
- Между ротацией двух ключей — прогнать полный smoke (§7).
- Если что-то упало — старый keypair ещё не удалён, можно откатить
  `lockbox activate-version` назад.

---

### 4.5. Self-service signup (email + OTP, по образцу claude.ai)

Этот блок открывает регистрацию для широкой аудитории — клиент сам
проходит signup → получает OTP в email → подтверждает → получает
свой client_id и api_key. Без участия админа.

#### Что нужно для запуска

| Компонент | Зачем |
|---|---|
| Email-провайдер | Доставка OTP (Resend / Yandex Mail Sender / SMTP) |
| Cloudflare Turnstile | Captcha на `/signup` и `/login` против ботов |
| DNS-записи | DKIM / SPF / DMARC чтобы письма не попадали в спам |
| Redis | Уже есть — используется как backend для rate-limit (опц.) |

#### Шаг 1. Email-провайдер (Resend, рекомендую)

Резкий старт через [resend.com](https://resend.com). Free tier =
3000 писем/мес, 100/день — хватает с запасом для signup-flow малого
SaaS. Альтернативы: Yandex Cloud Mail Sender (если ты в YC-экосистеме),
Postmark, SendGrid.

```bash
# 1. Регистрация на resend.com (через Google / GitHub)
# 2. Domains → Add → ввести example.com → Resend выдаст:
#       3 TXT записи (для верификации владения)
#       3 CNAME записи (для DKIM)
#    Прописать в DNS Beget / Cloudflare. Подождать 5-30 мин.
# 3. После Verified — API Keys → Create → "sku-prod"
#    Сохранить значение re_xxxxxxxx — это RESEND_API_KEY.

# 4. Прописать в .env (или Lockbox secret):
EMAIL_PROVIDER=resend
EMAIL_FROM=noreply@example.com         # ⚠ должен совпадать с verified domain
EMAIL_FROM_NAME="SKU Forecasting"
RESEND_API_KEY=re_xxxxxxxx
```

#### Шаг 2. SPF / DKIM / DMARC (без них — спам)

```dns
; SPF (если не выдал Resend в шаге 1):
example.com.    TXT  "v=spf1 include:_spf.resend.com ~all"

; DMARC (после того как SPF и DKIM работают):
_dmarc.example.com.   TXT  "v=DMARC1; p=quarantine; rua=mailto:dmarc@example.com"
```

Проверить:
```bash
dig +short TXT example.com
dig +short TXT _dmarc.example.com
# Прислать тестовое письмо себе на gmail → открыть headers → SPF=PASS DKIM=PASS DMARC=PASS
```

#### Шаг 3. Cloudflare Turnstile

```
1. Cloudflare Dashboard → Turnstile → Add site
2. Domain: web.example.com
3. Получить Site Key (для фронта) и Secret Key (для бэка)
4. В backend .env:    TURNSTILE_SECRET_KEY=...
5. В frontend сборку: VITE_TURNSTILE_SITE_KEY=...
6. Frontend rebuild:
     docker compose -f docker-compose.prod.yml build \
        --build-arg VITE_API_URL=https://api.example.com \
        --build-arg VITE_TURNSTILE_SITE_KEY=...
```

В dev можно обойтись:
```bash
DISABLE_CAPTCHA=1     # обязательно вернуть в 0 на prod!
```

#### Шаг 4. Smoke

```bash
# Console-режим (dev) — запросить OTP, увидеть его в логах api
curl -X POST https://api.example.com/auth/signup \
    -H "Content-Type: application/json" \
    -d '{"email":"test@example.com","desired_client_id":"test-shop","captcha_token":""}'
# → {"status":"otp_sent","email":"test@example.com","expires_in_minutes":10}

docker compose logs api | grep "EMAIL (console)"
# 2026-04-26  EMAIL (console)
#   To:      test@example.com
#   Subject: Подтверждение регистрации в SKU Forecasting
#   ...Ваш код для завершения регистрации: 482917

# Подтвердить
curl -X POST https://api.example.com/auth/signup/verify \
    -H "Content-Type: application/json" \
    -d '{"email":"test@example.com","code":"482917"}'
# → {"client_id":"test-shop","api_key":"sku_...","access_token":"eyJ..."}
```

В production-режиме шаг 1 шлёт реальное письмо через Resend.

#### Поведение и краевые случаи

| Сценарий | Что происходит |
|---|---|
| `email` уже зарегистрирован | 409 Conflict, **общая** ошибка "Email or client_id already in use" — не leak'аем что именно занято (защита от enumeration) |
| `client_id` занят | то же сообщение — admin → /admin/clients увидит реальную причину |
| Неверный код | 401, счётчик `attempts` +1 |
| 5 неверных попыток | код "сжигается" (mark_used), нужно запросить новый |
| Истёк TTL (10 мин) | 401, нужен новый код |
| Email-провайдер не отвечает | signup падает с 503; для login — 202 без письма (защита от enumeration) |
| Уже залогиненный заходит на `/signup` | страница доступна; backend всё равно отклонит из-за конфликта email |

#### Rate-limit

В nginx уже настроены лимиты на `/auth/token` (10 r/m). Добавить такие же на новые эндпоинты:

```nginx
# В docker/nginx/api.conf.template, раздел /auth/signup и /auth/login
location ~ ^/auth/(signup|login)/?$ {
    limit_req zone=auth_rl burst=5 nodelay;
    proxy_pass http://api_upstream;
    ...
}
```

(уже подмаскировано общим `auth_rl` зоной — кастомизируй если нужно
больше для signup чем для token).

#### Что увидит пользователь

1. Заходит на `web.example.com`
2. Жмёт "Создать аккаунт" → форма email + желаемый client_id + captcha
3. После submit'а: `Получить код` → редирект на `/signup/verify?email=...`
4. Получает 6-значный код в почту, вводит в OTP-инпут
5. Видит свой `api_key` ОДИН РАЗ на тёплой paper-карточке с кнопкой "Скопировать"
6. Жмёт "Я сохранил, продолжить" → попадает в `/welcome` (онбординг)
7. Дальше как обычно — загрузка файла, прогноз

---

### 4.6. OAuth — Google + Яндекс

Self-service регистрация через "Continue with Google / Войти через
Яндекс". В UI кнопки рендерятся **только** если соответствующий
провайдер включён на бэке — отключить можно одним env флагом без
deploy фронтенда.

#### Архитектурные решения этого блока

| Вопрос | Решение |
|---|---|
| Account linking | **Auto-link**, если email провайдера совпал с уже существующим `email_canonical` И тот email был ранее verified. Иначе создаём новый client. |
| client_id для нового OAuth-юзера | Генерируется из локальной части email (`vasya@gmail.com` → `vasya`), коллизии через суффикс `-2`, `-3`. Юзер может переименовать в `/settings` позже. |
| Refresh token | НЕ запрашиваем (не нужен — только для signup/login). |
| State / CSRF | HMAC-signed token с `MODEL_SIGNING_KEY`, nonce в HttpOnly cookie, TTL 10м. |
| Yandex без OIDC | Отдельный запрос к `login.yandex.ru/info` для email; subject = `id` из ответа. |

#### Шаг 1. Зарегистрировать приложение в Google

```
1. console.cloud.google.com → проект (или создать новый)
2. APIs & Services → OAuth consent screen
   • User Type: External
   • App name: SKU Forecasting
   • Support email: ops@example.com
   • Authorized domains: example.com
   • Scopes: openid + email + profile
   • Опубликовать (Publishing status: In production)
     либо оставить "Testing" с белым списком до 100 юзеров

3. Credentials → Create Credentials → OAuth client ID
   • Application type: Web application
   • Authorized redirect URIs:
       https://api.example.com/auth/oauth/google/callback
   • Получить: client_id и client_secret

4. В Lockbox / .env прописать:
       OAUTH_GOOGLE_ENABLED=1
       OAUTH_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
       OAUTH_GOOGLE_CLIENT_SECRET=GOCSPX-...
       OAUTH_GOOGLE_REDIRECT_URI=https://api.example.com/auth/oauth/google/callback
```

#### Шаг 2. Зарегистрировать приложение в Яндексе

```
1. https://oauth.yandex.ru/ → "Зарегистрировать новое приложение"

2. Платформа: ✓ "Веб-сервисы"
   Callback URL: https://api.example.com/auth/oauth/yandex/callback
   (Можно добавить второй для dev: http://localhost:8000/...)

3. Доступы → отметить:
   ✓ "Доступ к адресу электронной почты"          (login:email)
   ✓ "Доступ к информации о пользователе"          (login:info)

4. После создания — взять "ID приложения" (client_id)
   и "Пароль приложения" (client_secret)

5. В Lockbox / .env прописать:
       OAUTH_YANDEX_ENABLED=1
       OAUTH_YANDEX_CLIENT_ID=...
       OAUTH_YANDEX_CLIENT_SECRET=...
       OAUTH_YANDEX_REDIRECT_URI=https://api.example.com/auth/oauth/yandex/callback
```

#### Шаг 3. Указать FRONTEND_OAUTH_RETURN_URL

Адрес, куда бэк перенаправит юзера после успешного callback'а.
Должен указывать на ФРОНТ, не на бэк:

```ini
FRONTEND_OAUTH_RETURN_URL=https://web.example.com/oauth/return
```

#### Шаг 4. Перезапустить API + проверить

```bash
docker compose restart api

# Проверить, что провайдеры видны фронту:
curl https://api.example.com/auth/oauth/providers
# {"providers":[
#   {"id":"google","display_name":"Google","start_url":"/auth/oauth/google/start"},
#   {"id":"yandex","display_name":"Яндекс","start_url":"/auth/oauth/yandex/start"}
# ]}
```

После этого на `/login` и `/signup` появятся кнопки "Continue with
Google" и "Войти через Яндекс".

#### Включить / отключить отдельный провайдер

Без redeploy фронта, без рестарта инфры:

```bash
# Отключить Google:
yc lockbox payload get ... | jq '... OAUTH_GOOGLE_ENABLED → "0" ...'
yc lockbox secret add-version ...

docker compose restart api
# Кнопка "Google" больше не рендерится — фронт получает свежий список.

# Включить обратно:
yc lockbox payload get ... | jq '... OAUTH_GOOGLE_ENABLED → "1" ...'
docker compose restart api
```

#### Поведение flow

```
[Юзер] /login → жмёт "Continue with Google"
   │
   ▼
GET /auth/oauth/google/start
   • backend генерит state + nonce
   • set HttpOnly cookie sku_oauth_nonce=<nonce>
   • 302 → accounts.google.com/o/oauth2/v2/auth?state=<state>
   ▼
[Google] показывает chooser → юзер подтверждает
   ▼
302 → https://api.example.com/auth/oauth/google/callback?code=...&state=...
   │
   • verify_state(state, nonce) — HMAC + nonce + TTL 10м
   • exchange_code → access_token + id_token
   • verify_id_token → проверка JWKS, iss, aud, exp, email_verified
   • identity = {provider: 'google', subject, email, ...}
   │
   ┌── client_id с этим (provider, subject) уже есть? ──┐
   │ Да → login                                          │
   │ Нет → email_canonical уже зарегистрирован verified? │
   │       Да → LINK к существующему client              │
   │       Нет → создать новый client + api_key          │
   └─────────────────────────────────────────────────────┘
   │
   • create_access_token (JWT)
   • 302 → https://web.example.com/oauth/return?token=...&client_id=...&new_user=0|1[&api_key=...]
   ▼
[OAuthReturnPage]
   • setAuth(token, client_id) ← залогинен
   • new_user=0 → /
   • new_user=1 → показ api_key one-time → /welcome (онбординг)
```

#### Что отделить в дизайн-решениях

- **api_key выдаётся даже OAuth-юзерам.** Он нужен для интеграций
  (CI/cron-скрипты не умеют OAuth-flow). Юзер может его проигнорировать
  если не нужны интеграции.
- **OAuth-юзер может потом залогиниться через email-OTP** под тем же
  email — это будет корректно работать, оба пути ведут к одному
  client_id (благодаря account-linking по email_canonical).
- **OAuth-юзер НЕ может стать админом** через OAuth — admin role
  только через ADMIN_API_KEY на /auth/token.

#### Edge cases

| Сценарий | Поведение |
|---|---|
| Email Google-аккаунта not verified | 403, отказ создавать аккаунт |
| State протух (10м) | 400 "Invalid OAuth state" — заставляет начать заново |
| Юзер отказал в правах в Google | redirect c `?error=access_denied`, бэк отдаст 400 — фронт покажет toast |
| Один Google-аккаунт пытается создать два client_id | Блокирует UNIQUE на `(oauth_provider, oauth_subject)` |
| Утёкший redirect_uri | Google/Yandex отказывают сами — мы заранее зарегистрировали точный URI |

---

## 5. Backend-VPS: docker

### 5.1. Собрать образы

```bash
cd /srv/backend

# Сначала sandbox — иначе process-worker стартует с «нет образа»
docker compose -f docker/docker-compose.yml up sandbox-image

# Базовые сервисы (без nginx + certbot — они в prod-overlay)
docker compose -f docker/docker-compose.yml build
```

Сборка первого раза ~10–15 минут.

### 5.2. Поднять базовую инфраструктуру

```bash
docker compose -f docker/docker-compose.yml up -d \
    postgres redis minio minio-init clamav mlflow
```

Подождать 1–2 минуты, проверить здоровье:

```bash
docker compose -f docker/docker-compose.yml ps
# Все должны быть "healthy" или "running"
```

### 5.3. Запустить приложение

```bash
docker compose -f docker/docker-compose.yml up -d \
    api worker scan-worker process-worker \
    airflow-init airflow-webserver airflow-scheduler \
    prometheus alertmanager grafana backup
```

Проверка:

```bash
# API пока на 8000 (до nginx)
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz   # должен вернуть 200 если Redis+PG живые

docker compose ps --format "{{.Name}}\t{{.Status}}" | column -t
```

Все строки должны иметь статус `healthy`.

---

## 6. Backend-VPS: TLS

Nginx + certbot добавляются **overlay-compose-файлом**, не ломая базу.

```bash
cd /srv/backend

# Убедиться, что в .env есть API_DOMAIN + CERTBOT_EMAIL (см. 4.1)

# ВАЖНО: первый запуск с CERTBOT_STAGING=1 — LE staging не имеет
# rate-limit'ов и даёт быстро дебажить проблемы с DNS и проксированием.
# После успеха поменять на 0 и повторить init_tls.sh.

docker compose \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    up -d

# Запросить сертификат через ACME http-01
./scripts/init_tls.sh
```

После успеха:

```bash
curl -I https://api.example.com/healthz
# HTTP/2 200
# server: nginx

# Проверить TLS-гигиену
curl -sIv https://api.example.com/ 2>&1 | grep -i "TLS\|HSTS"
```

**Если `CERTBOT_STAGING=1`:** браузер ругается на сертификат. Переключить
на `0` в `.env` и заново:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml run --rm certbot \
    delete --cert-name api.example.com

./scripts/init_tls.sh
```

Renewal уже работает автоматически (certbot-контейнер в цикле каждые 12 ч).

---

## 7. Backend-VPS: smoke

```bash
# /healthz — не должен требовать ничего внешнего
curl https://api.example.com/healthz
# {"status":"ok"}

# /readyz — требует живого Redis + Postgres
curl https://api.example.com/readyz
# {"ready": true, "checks": {...}}

# /plans — публичный (нужен для фронт-витрины)
curl https://api.example.com/plans
# {"plans":[{"id":"free",...},{"id":"start",...},{"id":"business",...}]}

# Проверить /metrics закрыт — должен быть 403
curl -i https://api.example.com/metrics
# HTTP/2 403
```

Из ADMIN_CIDR:

```bash
# с ADMIN_CIDR /metrics виден
curl https://api.example.com/metrics | head
# # HELP forecast_requests_total ...
```

---

## 8. Frontend-VPS

### 8.1. Подготовка ОС — аналогично backend (пункт 3.1–3.3)

```bash
ssh root@<FRONTEND_VPS_IP>
# ... те же команды: deploy-пользователь, Docker, UFW, fail2ban ...
```

### 8.2. Клонирование

```bash
cd /srv
git clone https://github.com/your-org/sku-forecasting.git frontend-repo
cd frontend-repo/frontend   # <-- работаем в подпапке frontend
git checkout v1.0.0

cp .env.example .env
nano .env
```

```ini
VITE_API_URL=https://api.example.com
WEB_DOMAIN=web.example.com
CERTBOT_EMAIL=ops@example.com
CERTBOT_STAGING=1                            # сначала staging
CSP_CONNECT_SRC=https://api.example.com
```

### 8.3. Собрать образ

```bash
# VITE_API_URL запекается в бандл при сборке образа
docker compose -f docker-compose.prod.yml build --build-arg VITE_API_URL=https://api.example.com
docker compose -f docker-compose.prod.yml up -d
```

### 8.4. TLS

```bash
./scripts/init_tls.sh
```

### 8.5. Проверка

```bash
curl -I https://web.example.com/
# HTTP/2 200
# server: nginx
# strict-transport-security: max-age=31536000; includeSubDomains

# Проверить SPA fallback
curl -I https://web.example.com/some-deep-path
# HTTP/2 200 (отдаётся index.html)
```

Открыть в браузере: **https://web.example.com** — должна появиться
страница логина с брендингом `#004743`.

---

## 9. Первый запуск

### 9.1. Зарегистрировать админ-клиента

У нас есть `ADMIN_API_KEY` → получаем admin-JWT:

```bash
# На локальной машине:
curl -sX POST https://api.example.com/auth/token \
    -H "Content-Type: application/json" \
    -d '{"client_id":"admin","secret":"<ADMIN_API_KEY>"}' | jq
# { "access_token": "eyJ...", "token_type": "bearer" }

export TOKEN="eyJ..."

# Зарегистрировать первого реального клиента на тарифе Start
curl -sX POST https://api.example.com/clients \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"client_id":"acme","plan":"start","horizon":14}' | jq
```

### 9.2. Логин как админ в UI

1. Открыть https://web.example.com/login.
2. **Client ID:** `admin`
3. **API-ключ:** значение `ADMIN_API_KEY`
4. В сайдбаре должен появиться пункт «Клиенты».

### 9.3. Логин как клиент

1. Client ID: `acme`
2. API-ключ: значение `API_KEY` (не admin!)
3. Увидит пункты «Загрузки», «Обучение», «Прогнозы», «Настройки».

### 9.4. Тест upload-пайплайна

Через UI: «Загрузки» → drag-drop CSV (формат `date,sku,sales`).
Или curl:

```bash
curl -sX POST https://api.example.com/clients/acme/uploads \
    -H "Authorization: Bearer $TOKEN_ACME" \
    -F "file=@data.csv" | jq
# {"upload_id":"abc...","status":"uploaded","scan_job_id":"..."}

# Поллить статус
curl -s https://api.example.com/uploads/<upload_id> \
    -H "Authorization: Bearer $TOKEN_ACME" | jq .status
# "uploaded" → "scanning" → "scanned_clean" → "processing" → "processed"
```

Всё ожидаемо около **10–30 секунд** с чистым файлом.

### 9.5. Тест EICAR (заведомо заражённый тест-файл)

```bash
# Скачать стандартный EICAR
curl -L -o eicar.csv https://secure.eicar.org/eicar.com.txt

# Попытаться залить
curl -sX POST https://api.example.com/clients/acme/uploads \
    -H "Authorization: Bearer $TOKEN_ACME" \
    -F "file=@eicar.csv"
# → либо 400 «extension not allowed»; либо status="infected" через минуту
```

> Если заливается без проблем, а потом становится `infected` — всё ок,
> ClamAV работает. Если остаётся `uploaded` / `scanning` >5 минут,
> смотрите логи: `docker compose logs scan-worker clamav`.

---

## 10. Опс

### 10.1. Бэкапы

`docker-compose.yml` уже поднял `backup` сервис (crond + `scripts/backup.sh`).
В 02:10 UTC каждый день он:

1. `pg_dump` клиентского реестра и загрузок.
2. Шифрует AES-256-CBC (`BACKUP_PASSPHRASE`).
3. Заливает в `s3://$S3_BACKUP_BUCKET/backups/<YYYY/MM/DD>/...`.

Проверка:

```bash
docker compose exec backup sh -c "ls -la /var/log && tail -20 /var/log/backup.log"
docker compose exec backup sh -c "/scripts/backup.sh"   # ручной прогон
```

**Восстановление на другую машину:**

```bash
# Скачать дамп ИСПОЛЬЗУЯ BACKUP-КРЕДЫ (не processed)
mc alias set bak "$S3_BACKUP_ENDPOINT_URL" \
    "$S3_BACKUP_ACCESS_KEY_ID" \
    "$S3_BACKUP_SECRET_ACCESS_KEY"
mc cp bak/<S3_BACKUP_BUCKET>/backups/2026/04/24/sku_forecasting_<ts>.dump.enc .

# Восстановить (потребует интерактивного подтверждения)
cd /srv/backend
BACKUP_PASSPHRASE=... ./scripts/restore.sh ./sku_forecasting_<ts>.dump.enc

# Или напрямую из S3 (restore.sh сам разрулит S3_BACKUP_* -> mc)
S3_BACKUP_ENDPOINT_URL=... \
S3_BACKUP_ACCESS_KEY_ID=... \
S3_BACKUP_SECRET_ACCESS_KEY=... \
BACKUP_PASSPHRASE=... \
    ./scripts/restore.sh s3://<S3_BACKUP_BUCKET>/backups/2026/04/24/sku_forecasting_<ts>.dump.enc
```

### 10.2. Мониторинг

Grafana — https://api.example.com:3000 (публичных портов не открывайте!
используйте SSH-туннель: `ssh -L 3000:127.0.0.1:3000 deploy@<IP>`).

Дашборды по умолчанию нет — заведите сами, подключив источник
Prometheus (`http://prometheus:9090`).

Прометеевские алерты в `docker/alerts.yml` отправляются через Alertmanager
на `ALERT_WEBHOOK_URL`. Задайте его в `.env` (Slack Incoming Webhook).

### 10.3. Ротация сертификатов

Встроенный certbot-контейнер проверяет раз в 12 часов. Если renewal не
сработал (например, сдохли 80/443), вручную:

```bash
cd /srv/backend && ./scripts/renew_tls.sh
# На frontend VPS
cd /srv/frontend-repo/frontend && ./scripts/init_tls.sh
```

### 10.4. Обновление версии

```bash
cd /srv/backend
git fetch --tags
git checkout v1.1.0

# Пересобрать изменённые образы
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml build

# Rolling update — nginx уже healthcheck'нул, трафик не провиснет
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d
```

Frontend аналогично:

```bash
cd /srv/frontend-repo/frontend
git pull
docker compose -f docker-compose.prod.yml build --build-arg VITE_API_URL=https://api.example.com
docker compose -f docker-compose.prod.yml up -d web
```

### 10.5. Откат

```bash
cd /srv/backend
git checkout v1.0.0   # предыдущий тег
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d

# Если нужно откатить БД — восстановите из бэкапа (10.1)
```

### 10.6. Масштабирование воркеров (Docker compose)

```bash
# На очереди 50+ обучений → 4 воркера
docker compose -f docker/docker-compose.yml up -d --scale worker=4
```

### 10.7. Disaster recovery — полный restore на новый VPS

Сценарий: backend-VPS недоступен (диск сгорел, Beget прилёг, IP
заблокирован). Фронт-VPS ещё жив, но API за ним ничего не отдаёт.

**Что уже сохранено в облаке (ничего копировать не нужно):**
- модели, обработанные данные, метрики — S3 processed
- pg_dump каждый день — S3 backups (зашифровано BACKUP_PASSPHRASE)
- секреты приложения — Yandex Lockbox
- исходный код — git-репозиторий

**Что нужно иметь под рукой:**
- `BACKUP_PASSPHRASE` (из password manager — без него бекап бесполезен!)
- `ADMIN_API_KEY` (для smoke после восстановления)
- YC creds с правами создавать authorized key для `sku-lockbox-reader`
- SSH-доступ к Beget-панели или другому провайдеру

**Процедура (~30 минут):**

```bash
# ──────────── 1. Создать новый VPS ────────────
# В панели Beget (или любого другого провайдера): Ubuntu 22.04,
# минимум 4 vCPU / 8GB RAM / 80GB SSD, публичный IPv4.
# Далее — всё с локальной машины или из SSH.

ssh root@<NEW_VPS_IP>

# ──────────── 2. Base OS prep — как в §3 ────────────
# Сокращённо, копии команд оттуда:
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh

# Docker (см. §3.2)
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
# ... (копия §3.2 целиком)

# Firewall (см. §3.3)
apt-get install -y ufw fail2ban
ufw default deny incoming && ufw allow 22 && ufw allow 80 && ufw allow 443
ufw --force enable

# ──────────── 3. Клонировать репо + checkout релиза ────────────
sudo chown deploy:deploy /srv
exit    # relogin как deploy
ssh deploy@<NEW_VPS_IP>

cd /srv
git clone https://github.com/your-org/sku-forecasting.git backend
cd backend
git checkout v1.0.0         # тот же тег, что был на упавшем VPS

# ──────────── 4. .env — Lockbox-стиль ────────────
cp .env.lockbox.example .env
chmod 600 .env
nano .env     # вписать: API_DOMAIN, WEB_DOMAIN, CERTBOT_EMAIL,
              # ADMIN_CIDR, FRONTEND_ORIGINS, YC_LOCKBOX_SECRET_ID
              # (все эти значения известны, они были до катастрофы)

# ──────────── 5. Выдать новый SA-ключ Lockbox ────────────
# Старый SA-ключ лежал на упавшем VPS — он либо недоступен, либо
# утерян. Делаем новый через yc на локальной машине:

yc iam key create --service-account-name sku-lockbox-reader \
    --output new-sa-key.json
chmod 600 new-sa-key.json

# Копируем на новый VPS
mkdir -p /srv/backend/secrets
scp new-sa-key.json deploy@<NEW_VPS_IP>:/srv/backend/secrets/yc/yc-sa-key.json
ssh deploy@<NEW_VPS_IP> "chmod 644 /srv/backend/secrets/yc/yc-sa-key.json"   # 644: 3 container UIDs (R5-21)

# Локально — удалить копию
shred -u new-sa-key.json

# ──────────── 6. Поднять Postgres + Redis + MinIO ────────────
# БЕЗ api пока — сначала накатим backup в БД.
cd /srv/backend
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.prod.yml \
               -f docker/docker-compose.lockbox.yml \
               up -d postgres redis minio minio-init clamav mlflow

# Подождать health
sleep 30
docker compose ps

# ──────────── 7. Восстановить Postgres из backup ────────────
# Последний дамп — в S3_BACKUP_BUCKET под вчерашней датой.
# Он зашифрован BACKUP_PASSPHRASE (из password manager).

# Найти самый свежий:
mc alias set bak "$S3_BACKUP_ENDPOINT_URL" \
    "$S3_BACKUP_ACCESS_KEY_ID" "$S3_BACKUP_SECRET_ACCESS_KEY"
mc ls --recursive "bak/$S3_BACKUP_BUCKET/backups/" | tail -5
# → выбираешь свежий, например 2026/04/25/sku_forecasting_20260425T021010Z.dump.enc

# Восстановить через restore.sh (он сам скачает, расшифрует, pg_restore)
BACKUP_PASSPHRASE=$(pass show sku-forecasting/backup-passphrase) \
  ./scripts/restore.sh \
    s3://$S3_BACKUP_BUCKET/backups/2026/04/25/sku_forecasting_20260425T021010Z.dump.enc

# (подтвердить restore нажатием Enter)

# ──────────── 8. Поднять остальное приложение ────────────
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.prod.yml \
               -f docker/docker-compose.lockbox.yml \
               up -d

# Подождать пока всё healthy (1–3 минуты)
watch docker compose ps

# ──────────── 9. TLS ────────────
./scripts/init_tls.sh    # выпустит новый cert для того же API_DOMAIN
                          # (стартуй со staging=1 чтобы не спалить rate-limit LE)

# ──────────── 10. Переключить DNS ────────────
# В панели Beget: A-запись api.example.com → <NEW_VPS_IP>
# TTL был 300с — за 5 минут весь мир увидит новый IP.

# ──────────── 11. Smoke ────────────
curl https://api.example.com/healthz      # {"status":"ok"}
curl https://api.example.com/readyz       # {"ready":true}
curl https://api.example.com/plans        # список тарифов

# Авторизоваться админом — проверить что клиенты на месте
curl -sX POST https://api.example.com/auth/token \
    -H "Content-Type: application/json" \
    -d '{"client_id":"admin","secret":"'"$ADMIN_API_KEY"'"}' | jq

# Получить JWT и запросить список клиентов
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/clients

# ──────────── 12. Закрыть хвосты ────────────
# Старый SA-ключ (на упавшем VPS) в YC через несколько дней удалить
# вручную — yc iam key list + yc iam key delete. Авторотация сделает
# это сама в ближайшее воскресенье, но если срочно — сейчас.

# Frontend-VPS в большинстве случаев трогать не надо — VITE_API_URL
# указывает на api.example.com, а DNS мы уже переключили.
```

**Что критично проверить перед закрытием инцидента:**

| Проверка | Команда |
|---|---|
| Клиент может залогиниться | curl POST /auth/token |
| Старый upload'ы видны в UI | curl /clients/<id>/uploads |
| Новый upload проходит весь pipeline | curl POST /uploads + poll |
| Training можно запустить | curl POST /clients/<id>/train |
| Predict работает на существующих моделях | curl POST /predict |
| Backup-cron работает | `docker compose exec backup /scripts/backup.sh` |
| Мониторинг онлайн | SSH-туннель на Grafana 3000 |

**Что НЕ восстанавливается автоматически:**
- Uploads, которые были в статусах `uploaded`/`scanning`/`processing`
  в момент падения VPS — они потеряны, клиенты должны перезалить.
  Это отражено в БД как "потерянные" job'ы в RQ.
- In-flight training job'ы — аналогично, перезапуск вручную через UI.

### 10.8. Удаление клиента (GDPR / по запросу)

Когда клиент требует полного удаления своих данных, нужно пройти по
**всем семи местам**, где эти данные могут жить. Ниже — готовые команды;
запускать на backend-VPS от пользователя `deploy`.

**Переменные для копипасты:**

```bash
export CLIENT=acme                    # подставить реальный client_id
export TOKEN=<admin JWT>              # получить через POST /auth/token с ADMIN_API_KEY
```

#### Шаг 1. Удалить модели в памяти api (cache invalidation)

```bash
curl -X POST https://api.example.com/clients/$CLIENT/reload \
     -H "Authorization: Bearer $TOKEN"
# → service.load_primary() попытается загрузить модель — получит 404
#   после шага 3, и кеш очистится. Но сейчас сбрасываем принудительно.
```

#### Шаг 2. Удалить все uploads из трёх S3 зон

```bash
cd /srv/backend
# Используем mc с учётными данными каждой зоны
for zone in UNTRUSTED QUARANTINE PROCESSED; do
    ep_var="S3_${zone}_ENDPOINT_URL"
    kid_var="S3_${zone}_ACCESS_KEY_ID"
    sec_var="S3_${zone}_SECRET_ACCESS_KEY"
    bucket_var="S3_${zone}_BUCKET"

    # Значения из Lockbox — через env контейнера api:
    docker compose exec api sh -c "
        mc alias set del \$$ep_var \$$kid_var \$$sec_var &&
        mc rm --recursive --force del/\$$bucket_var/$CLIENT/ &&
        echo 'cleaned $zone'
    "
done
```

*(Если работаешь напрямую с yc-панелью — можно просто зайти в
Object Storage, найти префикс `$CLIENT/` в каждом из трёх бакетов,
удалить всю папку.)*

#### Шаг 3. Удалить записи из Postgres

```bash
docker compose exec postgres psql -U sku -d sku_forecasting <<SQL
BEGIN;
DELETE FROM sku_uploads    WHERE client_id = '$CLIENT';
DELETE FROM sku_clients    WHERE client_id = '$CLIENT';
COMMIT;
SELECT 'deleted' as status;
SQL
```

Если у тебя больше таблиц в БД — добавь туда. По состоянию на текущий
репозиторий только эти две имеют колонку `client_id`.

#### Шаг 4. Удалить MLflow-эксперименты клиента

```bash
# Получить id эксперимента(ов) клиента
docker compose exec mlflow bash -c "
    mlflow experiments search --view 'active_only' \
        | grep '^$CLIENT' || echo 'no experiments'
"

# Удалить найденный
docker compose exec mlflow bash -c "
    mlflow experiments delete --experiment-id <id>
"
```

MLflow мягко удаляет (soft delete) — метаданные помечаются в Postgres,
а артефакты в S3 processed остаются. Нужно ещё:

```bash
# Доудалить артефакты вручную
docker compose exec api sh -c "
    mc rm --recursive --force processed/\$S3_PROCESSED_BUCKET/mlflow/<exp-id>/
"
```

#### Шаг 5. Метрики Prometheus

Prometheus хранит метрики с `client_id` в labels
(`forecast_requests_total{client_id="..."}`). Полностью чистить — дорого
(нужен restart Prometheus с `--query.lookback-delta=0s` и tombstones).
Реалистичный путь:

- Ретеншн 30 дней (`--storage.tsdb.retention.time=30d` в compose) —
  метрики ушедшего клиента сами протухнут через месяц.
- Если GDPR требует **немедленно** — admin API Prometheus'а
  `POST /api/v1/admin/tsdb/delete_series` + `clean_tombstones`. См.
  docs.prometheus.io/admin_api.

#### Шаг 6. Backup бакет

Последние 30 дней pg-дампов **содержат** данные клиента (до момента
удаления). Если контракт требует полного wipe немедленно:

```bash
# Для каждого дампа — перешифровать без строк клиента и залить обратно.
# Это **нетривиально**: надо decrypt → load в tmp Postgres →
# DELETE WHERE client_id → pg_dump → encrypt → upload.

# Прагматичный путь: дождаться 30-дневного ротации по S3 lifecycle
# + в случае аудита объяснить, что восстановление дампа без
# client_id-строк возможно (они удалены из prod БД, даже если в
# старом дампе ещё есть).
```

Настоящий compliance требует отразить это в контракте: "полное
удаление произойдёт в течение 30 дней".

#### Шаг 7. Redis — job-история

```bash
docker compose exec redis redis-cli --scan --pattern "*$CLIENT*" | \
    xargs -r -n 100 docker compose exec -T redis redis-cli del
```

TTL на rq-job'ах и так 24 часа (`result_ttl=86400` в `task_queue.py`),
так что само протухнет.

#### Шаг 8. Финальный чек-лист

После всех операций убедиться:

```bash
# Клиент пропал из реестра
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/clients \
    | jq ".[] | select(.client_id==\"$CLIENT\")"
# → пусто

# Попытка авторизоваться под его client_id → 404 / 403
curl -X POST https://api.example.com/auth/token \
    -H "Content-Type: application/json" \
    -d "{\"client_id\":\"$CLIENT\",\"secret\":\"\"}"
# → 401 Invalid credentials (нормально — record'а нет)

# /predict → 404
curl -H "Authorization: Bearer $TOKEN" https://api.example.com/clients/$CLIENT/predict \
    -d '{"sku":"X","history":[...]}'
# → 404 Client not found
```

**Задокументируй факт удаления:** в компании обычно есть лог операций
над персональными данными (GDPR Art. 30). Запиши там: client_id,
дата, список затронутых систем, кто выполнял.

---

## 11. Troubleshooting

### `readyz` возвращает 503

```bash
docker compose exec api curl -s http://localhost:8000/readyz
```

Смотрите `checks.*.error`:
- `postgres` не ok → `docker compose logs postgres`, проверить `DATABASE_URL`
- `redis` не ok → `docker compose logs redis`

### Upload висит в `scanning`

```bash
docker compose logs -f scan-worker clamav
```

- `clamd transport failed` → ClamAV ещё качает сигнатуры (первый запуск
  ~2 минуты). Убедитесь, что healthcheck `clamav` перешёл в `healthy`.
- Пусто в логах скан-воркера — смотрите `docker compose logs redis`,
  возможно, очередь `sku-scan` не дошла.

### Upload висит в `processing`

```bash
docker compose logs -f process-worker
```

- `cannot connect to Docker daemon` → process-worker не пробросил
  `/var/run/docker.sock` (проверьте `volumes` в compose).
- `SANDBOX_IMAGE not found` → сервис `sandbox-image` не отработал.
  Запустить вручную: `docker compose up sandbox-image`.

### TLS-сертификат не выписывается

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml logs certbot
```

Частые причины:
- DNS ещё не устаканилось → `dig +short api.example.com` должен отвечать IP VPS
- UFW блокирует 80 → `sudo ufw status` — должно быть `80/tcp ALLOW`
- `--staging` использован один раз, теперь LE помнит его как staging.
  `docker compose run --rm certbot delete --cert-name api.example.com` + повторить

### CORS ошибка в браузере

В DevTools → Network → запрос 401/блокируется.
Проверьте, что `FRONTEND_ORIGINS` в `.env` backend'а **точно** совпадает
с origin'ом фронта (без завершающего `/`), после правки:

```bash
docker compose up -d api   # пересоздаст контейнер с новыми env
```

### Админ-роль не появляется

- Проверьте, что используете `ADMIN_API_KEY`, а не `API_KEY`.
- В DevTools → Application → LocalStorage → `sku-auth`: поле `roles`
  должно содержать `["admin","forecast"]`.
- Нет → дебаг декодера: в консоли `JSON.parse(atob('<middle-part-of-jwt>'))`.

### Диск кончается

```bash
df -h
docker system df
# Крупные тома: postgres_data, minio_data, prometheus_data
```

- Prometheus: ретеншн 30 дней уже стоит (`--storage.tsdb.retention.time=30d`).
- Postgres: `VACUUM FULL` через `docker compose exec postgres psql -U sku -d sku_forecasting -c "VACUUM FULL"`.
- MinIO: старые uploads в `untrusted` должны удаляться после сканирования,
  `quarantine` — после парсинга. Проверьте, что scan/process-воркеры живы.

### 🚨 S3-ключ Beget скомпрометирован

**Сценарий:** один из четырёх keypair'ов Beget Object Storage попал
в чужие руки (случайный коммит в публичный репо, утечка через логи,
украденный ноутбук с `.env`).

**Цель:** отсечь доступ по скомпрометированному ключу за 5 минут
и проверить, не успели ли навредить.

**Шаги в строгом порядке:**

```bash
# ── 1. НЕМЕДЛЕННО: заблокировать keypair в панели Beget ──────
# Beget панель → Object Storage → [нужный аккаунт] → Keys →
# напротив утёкшего ключа «Удалить» (или «Отключить»).
#
# После этого ЛЮБОЙ запрос по старым credentials возвращает 403.
# Это главное — сначала оторвать, потом разбираться.

# ── 2. Создать новый keypair на том же S3-аккаунте ───────────
# Panel → «Создать новый ключ». Записать:
#   NEW_ACCESS_KEY_ID=...
#   NEW_SECRET_ACCESS_KEY=...

# ── 3. Обновить Lockbox ──────────────────────────────────────
# Определи зону:  untrusted | quarantine | processed | backup
ZONE=untrusted   # подставить реальную

yc lockbox payload get --name sku-forecasting-secrets --format json \
    | jq --arg id "$NEW_ACCESS_KEY_ID" --arg s "$NEW_SECRET_ACCESS_KEY" \
         --arg zu "S3_${ZONE^^}_ACCESS_KEY_ID" \
         --arg zs "S3_${ZONE^^}_SECRET_ACCESS_KEY" \
         '.entries |= map(
             if .key==$zu then .text_value=$id
             elif .key==$zs then .text_value=$s
             else . end)
          | {entries: .entries}' \
    > /tmp/payload.json
yc lockbox secret add-version --name sku-forecasting-secrets \
    --payload-file /tmp/payload.json
shred -u /tmp/payload.json

# ── 4. Рестартнуть зависимые контейнеры ──────────────────────
cd /srv/backend
case "$ZONE" in
    untrusted)
        docker compose restart api scan-worker
        ;;
    quarantine)
        docker compose restart scan-worker process-worker
        ;;
    processed)
        docker compose restart api worker process-worker
        ;;
    backup)
        docker compose restart backup
        ;;
esac

# ── 5. Smoke — проверить, что всё снова работает ─────────────
curl https://api.example.com/readyz              # → 200
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@tiny.csv" \
     https://api.example.com/clients/test/uploads   # для untrusted

# ── 6. Расследование: что успели сделать утёкшим ключом ─────
# В панели Beget → Object Storage → тот же аккаунт → Аудит-лог
# (если провайдер его хранит). Ищи:
#   — GetObject с незнакомых IP (если были скачивания данных)
#   — PutObject с подозрительным content (если подменили model.pkl)
#   — DeleteObject (если пытались удалить evidence)
#
# Временной диапазон: от момента предполагаемой утечки
# (когда ключ впервые мог попасть в чужие руки) до шага 1.
```

**Критически важная проверка для зоны `processed`:**

Если скомпрометирован `S3_PROCESSED_ACCESS_KEY_ID`, атакующий мог
**подменить** `model.pkl` на вредоносный. При следующем `/predict`
он будет расшифрован через `pickle.loads` → RCE. Защита HMAC сработает,
**если** атакующий не знает `MODEL_SIGNING_KEY` — проверь в audit-логах,
были ли PutObject'ы по ключам вида `*/model.pkl` или `*/fallback.pkl`:

```bash
# Полный ресет моделей: удалить все model.pkl, заставить клиентов
# переобучить. Максимально параноидальный путь — когда нет уверенности.
docker compose exec api sh -c "
    mc alias set proc \$S3_PROCESSED_ENDPOINT_URL \
       \$S3_PROCESSED_ACCESS_KEY_ID \$S3_PROCESSED_SECRET_ACCESS_KEY &&
    mc find proc/\$S3_PROCESSED_BUCKET --name 'model.pkl' -exec 'mc rm {}'
"
```

**Что не защищает ротация ключа:**
- Данные, которые атакующий уже **скачал** (клиентские history CSV из
  untrusted, parquet из processed) — их уже не вернуть, нужно уведомить
  затронутых клиентов.
- Вредоносные объекты, которые атакующий залил, если `MODEL_SIGNING_KEY`
  тоже утёк. В таком случае ротируй ещё и его (§4.4).

**После инцидента:**

- Задокументируй: какой ключ, когда обнаружено, когда заблокировано,
  что делали ключом (по audit).
- Уведоми клиентов, если есть признаки чтения их данных.
- Ротируй `API_KEY` и `ADMIN_API_KEY` заодно (§4.4) — если утечка шла
  через общий источник (например, украденный `.env`), остальные секреты
  тоже в опасности.

---

## Приложение: чек-лист перед production

- [ ] `CERTBOT_STAGING=0` на **обоих** VPS
- [ ] `ADMIN_API_KEY` ≠ `API_KEY` и ≠ `JWT_SECRET_KEY`
- [ ] `BACKUP_PASSPHRASE` записан в менеджере паролей (если потеряете —
      не восстановите!)
- [ ] Бэкап-бакет (`S3_BACKUP_BUCKET`) в **четвёртом** независимом S3-аккаунте
- [ ] `S3_BACKUP_ACCESS_KEY_ID` ≠ `S3_PROCESSED_ACCESS_KEY_ID` (проверить `sort -u` трёх ключей должно дать 4 строки)
- [ ] `ALERT_WEBHOOK_URL` — Slack/Telegram/Discord webhook работает
- [ ] UFW реально блокирует — `nmap` извне видит только 22/80/443
- [ ] `docker compose ps` — **все** контейнеры `(healthy)`
- [ ] Пробный откат — `git checkout <предыдущий-тег> && docker compose up -d`
      поднимается чисто
- [ ] Все логи ротейтятся (`docker` по умолчанию — да; `journalctl` — да)
- [ ] `.env` с правами 600, не лежит в git
**Если идёшь через Lockbox (рекомендуется для Beget+YC, см. §4.3):**
- [ ] `.env` содержит ТОЛЬКО `YC_LOCKBOX_SECRET_ID` + `YC_SA_KEY_FILE` (никаких других секретов)
- [ ] `secrets/yc/` смонтирован read-only как КАТАЛОГ (не файл — инод-ловушка #190), ключ 644, каталог-родитель 700
- [ ] Все ключи приложения (JWT_SECRET_KEY, API_KEY, ADMIN_API_KEY, MODEL_SIGNING_KEY, BACKUP_PASSPHRASE, S3-ключи 4 зон) положены в Lockbox через `lockbox_load_env.sh`
- [ ] `sku-lockbox-reader` SA имеет роль `lockbox.payloadViewer` ТОЛЬКО на конкретный секрет, не на всю папку
- [ ] Авторотация SA-ключа настроена через GitHub Actions (`.github/workflows/rotate-lockbox-key.yml`) — хотя бы один цикл прошёл вручную
- [ ] `sku-key-rotator` SA имеет `iam.serviceAccounts.admin` ТОЛЬКО на `sku-lockbox-reader`, не на всю папку
- [ ] `ci-deploy` пользователь на VPS не имеет sudo и использует dedicated SSH-ключ
- [ ] После заливки в Lockbox: `shred -u .env.level1.bak` — оригинал секретов уничтожен

**Если идёшь через Vault (нестандартно для YC, см. §4.2):**
- [ ] `VAULT_ROLE_ID` в `.env`, `VAULT_SECRET_ID` инжектируется CI и протухает за 10 мин
- [ ] `vault-init.secrets` (5 ключей + root token) в password manager'е, удалён с диска (`shred -u`)
- [ ] Snapshot Vault'а ежедневно попадает в backup-бакет
- [ ] Админ знает `ADMIN_API_KEY`; клиенты знают только свои client_id + API_KEY
