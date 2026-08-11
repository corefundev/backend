# Support Assistant (эпик #503)

Контур AI-ассистента поддержки. Живёт на ОТДЕЛЬНОМ VPS
(supbot, 90.156.252.30 — 4vCPU/8GB/38GB; НЕ на прод-VPS: LLM туда не
влезает, изоляция по 152-ФЗ). Данные не покидают контур.

Состав (по фазам эпика):
- `docker-compose.yml` — pgvector (изолированный Postgres для векторов;
  НЕ прод-БД) + (SUP-2) llama.cpp-server + chat-api.
- `ingest/` — SUP-1 #504: корпус БЗ → чанки → эмбеддинги
  (intfloat/multilingual-e5-small, CPU) → pgvector. Запуск по кнопке/крону.

Секреты — только через env на хосте (`/srv/supbot/.env`, chmod 600);
пароль pgvector генерится при бутстрапе, в git не живёт.

Bootstrap на чистом VPS: `bash support/bootstrap_supbot.sh` (по SSH).

## Retention диалогов (#567)

Журнал `dialogs` хранится 30 дней (решение владельца 2026-08-06; диалоги —
потенциально ПДн). Ежедневный cron на хосте (по образцу канарейки):

```
5 4 * * * /srv/supbot/guard/retention.sh >> /srv/supbot/retention.log 2>&1
```

Период переопределяется `DIALOG_RETENTION_DAYS` в `/srv/supbot/.env`.

## Деплой chatapi (ручной, CD не покрывает support/**)

Контекст сборки на VPS обязан быть ТОЧНОЙ копией репо — деплой только
rsync с удалением лишнего (tar-поверх оставляет мусор, а COPY *.py
запечёт его в образ):

```
rsync -az --delete \
  -e 'ssh -i ~/.ssh/id_ed25519_supbot -o ProxyCommand="ssh -i ~/.ssh/claude/deploy-key -W %h:%p deploy@api.sprosly.com"' \
  support/api/ root@10.16.0.2:/srv/supbot/api/
ssh … root@10.16.0.2 'cd /srv/supbot && docker compose build chatapi && docker compose up -d chatapi'
```

Билд самопроверяется пробой импорта (`python -c "import app"`).
