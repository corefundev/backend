# MIGR-1 (#424): переезд на sprosly.com — runbook исполнения

Прототип-решения владельца: поддомены news.sprosly.com / help.sprosly.com;
.ru = только 301; api.testcore.ru остаётся алиасом ≥ переходного периода.
Все per-фазовые prod-действия — только после prod-approve владельца.

## Фаза 1a — api.sprosly.com параллельно (без переключения)

После мержа этой ветки (compose-spec nginx менялся → CD НЕ пересоздаёт
nginx — правило cd-scope):

```sh
ssh deploy@api.testcore.ru
cd /srv/backend
echo 'API_DOMAIN_ALT=api.sprosly.com' >> .env
set -a && . .env && set +a
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
  -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml \
  -f docker/docker-compose.replication.yml up -d --no-deps --force-recreate nginx
# серт: расширить существующий (тот же live/-каталог, ничего не переименовывается)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
  -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml \
  -f docker/docker-compose.replication.yml run --rm certbot \
  certonly --webroot -w /var/www/certbot --expand \
  --cert-name api.testcore.ru -d api.testcore.ru -d api.sprosly.com
docker exec docker-nginx-1 nginx -s reload
curl -sI https://api.sprosly.com/healthz | head -1     # 200 OK
```

## Фаза 1b — sprosly.com (+www/news/help) параллельно

После мержа frontend#105 и его CD:

```sh
ssh root@212.67.15.32
cd /srv/frontend
# env: новый домен + CSP на ОБА API (переходный период)
sed -i 's|^CSP_CONNECT_SRC=.*|CSP_CONNECT_SRC=https://api.testcore.ru https://api.sprosly.com|' .env
grep -q NEW_DOMAIN .env || echo 'NEW_DOMAIN=sprosly.com' >> .env
docker compose -f docker-compose.prod.yml up -d --force-recreate web
# SAN-серт четырёх хостов (http-01 сквозь CF-proxy; SSL-mode зоны = Full)
docker compose -f docker-compose.prod.yml run --rm --entrypoint "" certbot \
  certbot certonly --webroot -w /var/www/certbot \
  --cert-name sprosly.com \
  -d sprosly.com -d www.sprosly.com -d news.sprosly.com -d help.sprosly.com
docker compose -f docker-compose.prod.yml restart web
for h in sprosly.com www.sprosly.com news.sprosly.com help.sprosly.com; do
  curl -sI "https://$h/nginx-healthz" | head -1
done
```

⚠️ Пока VITE_API_URL в бандле = api.testcore.ru, sprosly.com обслуживает
приложение через СТАРЫЙ API — это норма Фазы 1 (CORS уже пускает, см. 1c).

## Фаза 1c — CORS: пустить новые origin'ы в API (Lockbox, атомарно)

ОДНОЙ новой версией секрета (прецедент SCRAM-рассинхрона):

```
FRONTEND_ORIGINS = https://skusystem.ru,https://sprosly.com,https://www.sprosly.com,https://news.sprosly.com,https://help.sprosly.com
```
(остальные ключи в этой версии НЕ трогаем). Затем force-recreate
потребителей CORS (api) — `up -d` env не перечитывает:
app-tier пересоздаст ближайший CD, либо руками `--no-deps --force-recreate api`.

## Фаза 2 — окно переключения (≈15 мин, prod-approve владельца)

1. GH-секрет frontend `VITE_API_URL=https://api.sprosly.com` → пустой
   коммит в main фронта → CD пересобирает бандл (уже с host-routing).
2. Lockbox: одна атомарная версия:
   - `FRONTEND_OAUTH_RETURN_URL=https://sprosly.com`
   - `WEB_DOMAIN=sprosly.com` (если потребитель есть)
   - `REFRESH_COOKIE_SAMESITE=lax` (фронт и api теперь один registrable domain)
   - `EMAIL_FROM=noreply@sprosly.com` — ТОЛЬКО если DKIM уже зелёный,
     иначе отдельной версией позже.
   - `SMTP_USER=noreply@sprosly.com` + `SMTP_PASSWORD=<пароль ящика>` —
     ТОЙ ЖЕ версией, что и EMAIL_FROM: у Beget авторизация и From обязаны
     совпадать, раздельная ротация оставит рассылку с 535/rejected.
     Пароль владелец диктует в момент исполнения — в чат/файлы не пишем,
     сразу в Lockbox.
   Затем force-recreate api (+ worker'ы, если читают эти ключи).
3. Фронт-VPS: `LEGACY_REDIRECT=1` в .env → `docker compose ... up -d
   --force-recreate web` → skusystem.ru постранично 301-ит
   (/news/*→news.sprosly.com, /help/*→help.sprosly.com, прочее → apex).
4. CF sprosly.ru: Redirect Rule 301 → https://sprosly.com (dynamic,
   preserve path+query) — если не заведён ранее.

## Фаза 3 — верификация

```sh
# 301-цепочки (постранично, не в лоб)
for u in / /plans /news /news/some-slug /help /help/a/data-processing; do
  curl -sI "https://skusystem.ru$u" | grep -iE "^(HTTP|location)"; done
# e2e: логин (кука Lax в Safari!), капча на /signup, OTP-письмо
# (Authentication-Results: spf=pass dkim=pass), news./help. публично,
# кабинет на sprosly.com ходит в api.sprosly.com (вкладка Network).
```
+ prometheus: ssl-exporter targets += 4 новых хоста (отдельный PR в этой
фазе — раньше добавлять нельзя, probe-alert зашумит);
+ Я.Вебмастер / Search Console: «переезд сайта»;
+ DMARC-отчёты 2 недели; nginx 404-лог старого домена.

## Откат

- Фаза 1a/1b аддитивны — откат = убрать env-ключи + force-recreate.
- Фаза 2: `LEGACY_REDIRECT=0` + recreate web; VITE_API_URL назад + пустой
  коммит; Lockbox `--base-version-id` на прежнюю версию + recreate api.
