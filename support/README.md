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
