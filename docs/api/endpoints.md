# Sprosly API — справочник эндпоинтов

Базовый URL: `https://api.sprosly.com`. Аутентификация и формат ошибок — см. [README.md](README.md).

Все эндпоинты (кроме отмеченных «без авторизации») требуют заголовок
`Authorization: Bearer <token>`. `{client_id}` в путях — идентификатор вашего
workspace; доступ к чужому `client_id` запрещён (403), чужие ресурсы по id
отвечают 404.

Разделы:

- [Аутентификация](#аутентификация)
- [Данные (загрузки файлов)](#данные-загрузки-файлов)
- [Датасеты](#датасеты)
- [Обучение](#обучение)
- [Прогнозы](#прогнозы)
- [Автозаказ](#автозаказ)
- [Аккаунт и тариф](#аккаунт-и-тариф)
- [Уведомления](#уведомления)
- [Открытые вопросы](#открытые-вопросы)

---

## Аутентификация

### POST /auth/token

Обмен API-ключа на JWT (основной путь для интеграций).

| Параметр (JSON body) | Тип | Обязателен | Ограничения |
|---|---|---|---|
| `client_id` | string | да | 1–64 символа |
| `secret` | string | да | 1–256 символов; ваш ключ `sku_...` |

```bash
curl -s -X POST https://api.sprosly.com/auth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id": "my-shop", "secret": "sku_..."}'
```

Ответ 200:

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer"}
```

Ошибки: `401` неверные учётные данные; `403` аккаунт приостановлен/закрыт;
`429` превышен лимит попыток (заголовок `Retry-After`).

### POST /auth/login/password

Вход email + пароль (флоу веб-кабинета). Возвращает `access_token` и ставит
httpOnly refresh-куку (путь `/auth`, срок 30 дней).

| Параметр (JSON body) | Тип | Обязателен |
|---|---|---|
| `email` | string | да (≤254) |
| `password` | string | да (1–128) |
| `captcha_token` | string | после 2 неудачных попыток (Cloudflare Turnstile) |

Ответ 200: `{"client_id": "...", "access_token": "...", "token_type": "bearer"}`.

Ошибки: `401` неверный email или пароль (единый ответ, без раскрытия причины);
`403` аккаунт приостановлен/закрыт; `422` неверный формат email / captcha требуется;
`429` слишком много попыток (лимит 20/час на подсеть; блокировка на 15 минут после
10 неудач по одному email).

### POST /auth/refresh

Обмен refresh-куки на новый `access_token` с ротацией куки (браузерный флоу;
кука должна прийти в запросе). Ответ — как у `/auth/login/password`.
Ошибки: `401` сессия истекла (в т.ч. при обнаружении повторного использования
уже потраченного refresh-токена — вся цепочка отзывается).

### POST /auth/logout

Отзыв текущего JWT (и refresh-семейства, если пришла кука). Ответ: `204` без тела.

---

## Данные (загрузки файлов)

Жизненный цикл загрузки (поле `status`):

```
uploaded → scanning → scanned_clean → processing → processed
                    ↘ infected                   ↘ processing_failed
```

- `scanned_clean` — файл прошёл антивирус и ждёт вашей команды «Подготовить»;
- `processed` — данные распознаны и готовы к обучению;
- `infected`, `processing_failed` — терминальные ошибки (`error_message` объясняет причину).

### POST /clients/{client_id}/uploads

Загрузка файла с историей продаж. `multipart/form-data`, ответ `202`.

| Параметр (form) | Тип | Обязателен | Ограничения |
|---|---|---|---|
| `file` | file | да | `.csv` или `.xlsx`, ≤ 50 МБ |
| `dataset_id` | string | нет | 1–64; датасет, к которому файл прикрепится автоматически после подготовки |

```bash
curl -s -X POST "https://api.sprosly.com/clients/my-shop/uploads" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sales.csv" \
  -F "dataset_id=$DATASET_ID"
```

Ответ 202:

```json
{
  "upload_id": "…",
  "client_id": "my-shop",
  "filename": "sales.csv",
  "size_bytes": 123456,
  "sha256": "…",
  "status": "uploaded",
  "scan_job_id": "…"
}
```

Ошибки: `400` файл отвергнут (расширение/пустой файл/невалидное имя);
`404` указанный `dataset_id` не найден; `413` файл больше лимита;
`503` очередь проверки недоступна — повторите позже.

### GET /clients/{client_id}/uploads

Список последних загрузок.

| Параметр (query) | Тип | Дефолт | Ограничения |
|---|---|---|---|
| `limit` | int | 50 | 1–500 |

Ответ 200 — массив объектов статуса (см. ниже).

### GET /uploads/{upload_id}

Детальный статус загрузки (только своей).

```json
{
  "upload_id": "…",
  "client_id": "my-shop",
  "filename": "sales.csv",
  "size_bytes": 123456,
  "sha256": "…",
  "status": "processed",
  "scan_result": "clean",
  "error_message": null,
  "processed_key": "…",
  "row_count": 15000,
  "sku_count": 120,
  "dataset_id": "…",
  "date_min": "2025-01-01",
  "date_max": "2026-06-30",
  "created_at": "…",
  "updated_at": "…"
}
```

Ошибки: `404` загрузка не найдена (или принадлежит другому клиенту).

### DELETE /uploads/{upload_id}

Отмена/удаление загрузки. Ответ всегда `204` (идемпотентно).

### POST /clients/{client_id}/uploads/{upload_id}/prepare

Запуск подготовки данных (распознавание формата + автосопоставление колонок +
парсинг). Допустим только из статуса `scanned_clean`.

Ответ 200: `{"upload_id": "…", "status": "preparing", "job_id": "…"}`.

Ошибки: `404` загрузка не найдена; `409` неподходящий статус (уже готовится/готова).

### GET /clients/{client_id}/uploads/{upload_id}/prep

Статус подготовки + превью распознанных данных (read-only; колонки сопоставляются
системой автоматически, ручной маппинг не предусмотрен).

```json
{
  "upload_id": "…",
  "status": "processed",
  "filename": "sales.csv",
  "detected": {"format": "csv", "encoding": "utf-8", "delimiter": ";"},
  "row_count": 15000,
  "sku_count": 120,
  "error_message": null,
  "sample": {"columns": ["date", "sku", "sales", "price"], "rows": [["2026-06-01", "SKU-1", 5, 199.0]]}
}
```

`sample` заполняется только в статусе `processed`.

---

## Датасеты

Датасет — именованная коллекция подготовленных загрузок со слитым версионируемым
снапшотом (при пересечении по паре `(sku, date)` побеждает более новый файл).
У каждого датасета — своя модель и свои прогнозы. Количество датасетов ограничено
тарифом (Free 1 / Start 3 / Business 10).

Представление датасета (используется в ответах ниже):

```json
{
  "dataset_id": "…",
  "name": "Основной",
  "current_version": 3,
  "created_at": "…",
  "updated_at": "…",
  "files": 2,
  "row_count": 15000,
  "sku_count": 120,
  "date_min": "2025-01-01",
  "date_max": "2026-06-30",
  "fingerprint": "…",
  "model": {
    "trained_at": "…",
    "dataset_version": 3,
    "wmape": 0.41,
    "wmape_order_7": 0.22,
    "wmape_order_14": 0.19,
    "baseline_wmape": 0.55,
    "improvement_vs_naive": 0.25,
    "up_to_date": true
  }
}
```

`model` — карточка последней успешно обученной модели датасета (`null`, если модель
ещё не обучена); `up_to_date: false` означает, что данные в датасете новее модели.

### POST /clients/{client_id}/datasets

Создание датасета. Ответ `201` — представление датасета.

| Параметр (JSON body) | Тип | Обязателен | Ограничения |
|---|---|---|---|
| `name` | string | да | 1–80, уникально среди ваших датасетов |

Ошибки: `403` достигнут лимит датасетов тарифа; `409` имя занято; `422` пустое имя.

### GET /clients/{client_id}/datasets

Список датасетов: `{"datasets": [<представление>, …]}`.

### GET /clients/{client_id}/datasets/{dataset_id}

Карточка датасета: представление + `files_detail` (файлы с отчётом слияния
`merge_added`/`merge_replaced`), `versions` (до 20 последних версий со статусом,
`fingerprint`, счётчиками и `merge_report`), `pending_uploads` (загрузки,
нацеленные на датасет, но ещё не подготовленные/не прикреплённые).

Ошибки: `404` датасет не найден.

### POST /clients/{client_id}/datasets/{dataset_id}/files

Прикрепить подготовленную (`processed`) загрузку к датасету; собирается новая
версия снапшота. Ответ `201`:

```json
{
  "dataset_id": "…",
  "version": 4,
  "fingerprint": "…",
  "row_count": 17200,
  "sku_count": 130,
  "date_min": "2025-01-01",
  "date_max": "2026-07-15",
  "merge_report": {"added": 2200, "replaced": 0}
}
```

| Параметр (JSON body) | Тип | Обязателен |
|---|---|---|
| `upload_id` | string | да (1–64) |

Ошибки: `404` датасет/загрузка не найдены; `409` файл ещё не `processed` или уже
прикреплён; `422` слияние не удалось (файл откреплён обратно).

### DELETE /clients/{client_id}/datasets/{dataset_id}/files/{upload_id}

Открепить файл; из оставшихся файлов собирается новая версия. Ответ 200:
`{"dataset_id": "…", "detached": "<upload_id>", "rebuilt_version": 5}`
(`rebuilt_version: null`, если файлов не осталось).
Ошибки: `404` файл не в датасете; `422` пересборка не удалась.

### POST /clients/{client_id}/datasets/{dataset_id}/train

Запуск обучения модели на текущей версии датасета (рекомендуемый способ). Ответ `202`:

```json
{"dataset_id": "…", "dataset_version": 4, "run_id": "…", "job_id": "…", "status": "queued"}
```

Дальше — поллинг `GET /jobs/{job_id}`.

Ошибки: `409` в датасете нет данных / версия не готова / уже идёт обучение
(конверт `reason_code: in_flight`); `429` пауза тарифа (`reason_code: cooldown`,
заголовок `Retry-After`); `403` в датасете больше SKU, чем разрешает тариф;
`503` очередь обучения недоступна.

### DELETE /clients/{client_id}/datasets/{dataset_id}

Удаление датасета (мягкое). Ответ 200: `{"dataset_id": "…", "status": "deleted"}`.

---

## Обучение

### POST /clients/{client_id}/train

Прямой запуск обучения по одной подготовленной загрузке (легаси-путь; для
датасетов используйте `/datasets/{dataset_id}/train`).

| Параметр (JSON body) | Тип | Обязателен | Описание |
|---|---|---|---|
| `data_path` | string | да | игнорируется при наличии `upload_id` (сервер сам резолвит путь); произвольный путь доступен только администратору |
| `upload_id` | string | обязателен для клиентов | загрузка в статусе `processed` |
| `extend_from_history` | bool | нет (false) | переобучить на объединении ВСЕЙ истории подготовленных загрузок + текущей |
| `extend_from_upload_id` | string | нет | УСТАРЕЛО: любое значение = `extend_from_history=true` |

Ответ 200:

```json
{"client_id": "my-shop", "job_id": "…", "status": "queued", "message": "Training enqueued. Poll /jobs/…"}
```

Ошибки: `403` `upload_id` не указан (для клиентов обязателен) / превышен лимит SKU
тарифа; `404` загрузка не найдена; `409` данные не подготовлены / уже идёт обучение
(`reason_code: in_flight`) / манифест SKU нечитаем; `429` пауза тарифа
(`reason_code: cooldown`); `503` очередь недоступна.

### GET /jobs/{job_id}

Статус фоновой задачи (обучение, скан, подготовка). Только своих задач (чужие — 404).

```json
{
  "job_id": "…",
  "status": "started",
  "result": null,
  "error": null,
  "enqueued": "2026-07-17T10:00:00+00:00",
  "started": "2026-07-17T10:00:05+00:00",
  "ended": null,
  "progress": {"stage": "…"}
}
```

`status`: `queued | started | finished | failed | unknown`. При `failed` поле
`error` содержит общее сообщение — подробности сообщите в поддержку вместе с `job_id`.

### GET /clients/{client_id}/training-runs

Постоянная история обучений (не зависит от TTL очереди).

| Параметр (query) | Тип | Дефолт | Ограничения |
|---|---|---|---|
| `limit` | int | 50 | 1–200 |

Ответ 200: `{"runs": [<run>, …], "count": N}`. Поля run:
`run_id`, `client_id`, `plan`, `status`, `job_id`, `upload_id`, `dataset_id`,
`dataset_version`, `enqueued_at`, `started_at`, `ended_at`, `elapsed_sec`,
`n_skus`, `n_features`, `n_rows`, метрики `wmape`, `mase`, `mase_seasonal`,
`smape`, `wmape_order_7`, `wmape_order_14`, `baseline_wmape`, `model_path`,
`mlflow_run_id`, `error`, `mlflow_logging_error`, `gate_passed`
(false = модель не прошла контроль качества и не была опубликована; прежняя
модель продолжает обслуживать прогнозы).

Ошибки: `503` история временно недоступна (не путать с пустым списком — он приходит как 200).

---

## Прогнозы

### GET /clients/{client_id}/forecasts

Батч-прогнозы, автоматически посчитанные после последнего обучения.

| Параметр (query) | Тип | Дефолт | Описание |
|---|---|---|---|
| `sku` | string | — | фильтр по одному SKU |
| `dataset_id` | string | первый активный датасет | прогнозы партиционированы по датасету |

```json
{
  "client_id": "my-shop",
  "generated_at": "…",
  "first_date": "2026-07-01",
  "last_date": "2026-07-28",
  "horizon_days": 28,
  "count": 120,
  "skus": [
    {"sku": "SKU-1", "values": [5.2, …], "p10": [3.1, …], "p90": [8.4, …], "order_qty": [6.0, …]}
  ]
}
```

Массивы выровнены по датам `first_date..last_date`; пропуски — `null`. `p10`/`p90` —
границы интервала, `order_qty` — рекомендованное количество к заказу на день.
Если прогнозов нет — 200 с `count: 0`.

### POST /clients/{client_id}/predict

Онлайн-прогноз по одному SKU. История берётся либо из тела запроса, либо из
подготовленной загрузки.

| Параметр (JSON body) | Тип | Обязателен | Описание |
|---|---|---|---|
| `sku` | string | да | 1–128 символов |
| `history` | array | либо он, либо `upload_id` | ≥30 записей `{"date": "YYYY-MM-DD", "sales": число ≥ 0, "price"?: число, "promo"?: 0/1, "stock"?: число}` |
| `upload_id` | string | либо он, либо `history` | подготовленная загрузка — история читается из неё |
| `horizon` | int | нет (из конфигурации, по умолчанию 14) | 1–365; обрезается до потолка тарифа |

```bash
curl -s -X POST "https://api.sprosly.com/clients/my-shop/predict" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sku": "SKU-1", "upload_id": "'$UPLOAD_ID'", "horizon": 14}'
```

Ответ 200:

```json
{
  "sku": "SKU-1",
  "client_id": "my-shop",
  "forecast": [5.2, 4.9, …],
  "forecast_dates": ["2026-07-18", "2026-07-19", …],
  "horizon": 7,
  "model_source": "primary",
  "model_name": "Notus",
  "horizon_limited_by_plan": true
}
```

`model_source`: `primary` (ваша обученная модель) | `fallback` (резервная) | `zero`.

Ошибки: `404` клиент/загрузка/SKU не найдены; `409` загрузка не `processed`;
`422` невалидная история (<30 строк, NaN, отрицательные продажи и т.п.);
`429` превышен часовой лимит тарифа (`Retry-After`); `500` внутренняя ошибка
(с `trace_id` для поддержки).

### POST /clients/{client_id}/predict/batch

До 100 прогнозов за запрос, частичный успех.

| Параметр (JSON body) | Тип | Ограничения |
|---|---|---|
| `requests` | array of PredictRequest | ≤100 |

Ответ всегда 200:

```json
{
  "client_id": "my-shop",
  "results": [
    {"sku": "SKU-1", "forecast": [...], "...": "как одиночный /predict"},
    {"error": {"status": 429, "detail": "Predict request rate exceeded …"}}
  ],
  "count": 2,
  "succeeded": 1,
  "failed": 1
}
```

Порядок `results` совпадает с порядком `requests`; каждая позиция тратит 1 запрос
часового лимита.

### GET /clients/{client_id}/uploads/{upload_id}/skus

Каталог SKU подготовленной загрузки.

Ответ 200: `{"upload_id": "…", "count": N, "skus": [{"sku": "SKU-1", "row_count": 365, "first_date": "…", "last_date": "…"}]}`.
Ошибки: `404` загрузка не найдена; `409` не `processed`.

### GET /clients/{client_id}/anomalies

Аномалии истории продаж, найденные при последнем обучении (последние 90 дней данных).

| Параметр (query) | Тип | Описание |
|---|---|---|
| `sku` | string | фильтр по SKU |

Ответ 200: `{"client_id": "…", "count": N, "anomalies": [...]}`.

---

## Автозаказ

### GET /clients/{client_id}/order-recommendations

Рекомендации к заказу: по каждому SKU суммируются первые `window` дней прогноза —
спрос, страховой запас и количество к заказу (с вычетом последнего известного
остатка `stock`, если он есть в данных).

| Параметр (query) | Тип | Дефолт | Ограничения |
|---|---|---|---|
| `window` | int | 14 | 1–60 (дней) |
| `dataset_id` | string | первый активный датасет | |
| `service_level` | float | из конфигурации (по умолчанию 0.7) | 0.5–0.95; переопределение доступно на тарифах Start/Business |
| `format` | string | `json` | `json` \| `csv` |

Ответ 200 (JSON):

```json
{
  "client_id": "my-shop",
  "dataset_id": "…",
  "service_level": 0.7,
  "window": 14,
  "generated_at": "…",
  "first_date": "2026-07-18",
  "last_date": "2026-07-31",
  "count": 120,
  "items": [
    {
      "sku": "SKU-1",
      "stock": 12.0,
      "demand_sum": 71.4,
      "safety_sum": 9.3,
      "order_sum": 80.7,
      "order_qty": 69,
      "p10_sum": 52.0,
      "p90_sum": 104.0,
      "days": 14,
      "days_covered": 14
    }
  ]
}
```

- `order_qty` — итог к заказу: прогноз + страховой запас − остаток, не ниже нуля,
  округление вверх до целых штук;
- `days_covered` < `days` означает, что часть окна не покрыта калиброванным
  интервалом (для этих дней рекомендация не считается);
- `null` в полях — рекомендация по SKU недоступна.

`format=csv` — выгрузка для 1С/Excel: UTF-8 с BOM, разделитель `;`, десятичная
запятая, строки CRLF, `Content-Disposition: attachment`. Колонки:

```
sku;Остаток;Спрос (прогноз);Страховой запас;К заказу;Мин (p10);Макс (p90);Дней в окне;Дней с рекомендацией
```

Ошибки: `403` `service_level` на тарифе Free; `404` (только при `format=csv`)
прогнозов ещё нет; `422` параметры вне диапазонов.

---

## Аккаунт и тариф

### GET /plans — без авторизации

Каталог тарифов: `{"plans": [{"id", "model_display_name", "display_name", "max_skus", "max_horizon_days", "training_cooldown_hours", "datasets_limit", "config_allowed_keys", "price_label"}]}`.

### GET /clients/{client_id}

Профиль клиента (безопасная проекция): `client_id`, `config`, `storage_path`,
`created_at`, `last_trained_at`, `last_wmape`, `last_mase`, `status`,
`model_version`, `horizon`, `notes`, `plan`, `trained_sku_count`, `suspended_at`,
`deleted_at`, `email`, `email_verified_at`, `oauth_provider`, `last_mlflow_run_id`.

### GET /clients/{client_id}/usage

Текущий тариф и лимиты:

```json
{
  "client_id": "my-shop",
  "plan": "free",
  "model_display_name": "Notus",
  "display_name": "Free",
  "max_skus": 30,
  "max_horizon_days": 7,
  "config_allowed_keys": [],
  "training_cooldown_hours": 12,
  "cooldown_until": "2026-07-18T09:00:00+00:00",
  "trained_sku_count": 25,
  "current_sku_count": 28
}
```

`cooldown_until: null` — пауза не действует; `current_sku_count` — SKU в последней
подготовленной загрузке.

### POST /clients/{client_id}/upgrade

Смена тарифа. Body: `{"plan": "free" | "start" | "business"}`.
Ответ 200: `{"plan": "start", "display_name": "Start", "changed": true}`
(`changed: false` — тариф уже такой). Ошибки: `422` неизвестный тариф.

### GET /clients/{client_id}/config

Конфигурация прогнозирования: `{"client_id", "override" (ваши переопределения),
"effective" (итоговая слитая), "diff" (отличия от системной)}`.

### PUT /clients/{client_id}/config

Полная замена ваших переопределений. Body: `{"config": {<только отличающиеся ключи>}}`
(вложенность ≤10). Набор разрешённых ключей зависит от тарифа (Free — никаких,
Start — белый список «бизнес-регуляторов», Business — все; список — в
`config_allowed_keys` ответа `/usage`). Ответ — как GET.
Ошибки: `403` ключ запрещён тарифом; `422` значение вне диапазона / горизонт выше тарифного.

### PATCH /clients/{client_id}/config

Изменение одного ключа в dot-нотации. Body: `{"key": "model.horizon", "value": 28}`.
Ответ и ошибки — как у PUT.

### DELETE /clients/{client_id}/config

Сброс к системным настройкам. Ответ 200: `{"client_id": "…", "status": "reset to system defaults"}`.

### POST /clients/{client_id}/api-key/rotate

Выпуск нового API-ключа (старый немедленно перестаёт действовать; уже выданные JWT
живут до истечения срока). Ответ 200:

```json
{"client_id": "my-shop", "api_key": "sku_…", "warning": "Сохраните новый api_key. Старый теперь недействителен."}
```

Ключ показывается один раз. Ошибки: `429` больше 5 ротаций в час.

### GET /clients/{client_id}/audit

Журнал безопасности аккаунта (входы, смены пароля/тарифа, OAuth).

| Параметр (query) | Тип | Дефолт | Ограничения |
|---|---|---|---|
| `limit` | int | 100 | 1–500 |
| `offset` | int | 0 | ≥0 |

Ответ 200: `{"client_id", "count", "events": [{"id", "ts", "event_type", "event_subtype", "actor_email", "target_type", "target_id", "ip", "user_agent", "success", "metadata"}]}`.

### GET /clients/{client_id}/export

Экспорт всех данных аккаунта (право на доступ, 152-ФЗ): `{"profile", "training_runs", "uploads", "access_log"}`.

### DELETE /clients/{client_id}

Закрытие аккаунта (мягкое удаление): доступ прекращается немедленно, персональные
данные удаляются по истечении срока хранения. Идемпотентно. Ответ 200:

```json
{"client_id": "…", "deleted_at": "…", "message": "Аккаунт закрыт. …"}
```

---

## Уведомления

### GET /clients/{client_id}/notifications

Входящие уведомления (завершение обучения, лимиты тарифа, объявления).

| Параметр (query) | Тип | Дефолт | Ограничения |
|---|---|---|---|
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | ≥0 |

Ответ 200: `{"notifications": [...], "count": N, "unread": M}`.

### POST /clients/{client_id}/notifications/read

Отметить прочитанными. Body: `{"ids": [1, 2, 3]}` (≤500) или `{}`/`{"ids": null}` — все.
Ответ 200: `{"marked": N}`.

### POST /clients/{client_id}/telegram/link-token

Одноразовая ссылка для привязки Telegram-бота (уведомления о завершении обучения).
Ответ 200: `{"token": "…", "url": "https://t.me/…?start=…", "expires_in_sec": 600}`.
Ошибки: `503` бот не настроен.

### GET /clients/{client_id}/telegram

Статус привязки: `{"linked": true, "bot": "…", "training_complete_enabled": true}`.

### DELETE /clients/{client_id}/telegram

Отвязать Telegram. Ответ 200: `{"linked": false}`.

---

## Открытые вопросы

Пункты, требующие решения владельца продукта до/после публикации (в документации
выше зафиксировано фактическое поведение кода на момент написания):

1. **Первичная выдача API-ключа.** Для аккаунтов, зарегистрированных по паролю,
   ключ при регистрации не выдаётся; единственный путь получить ключ —
   `POST /clients/{id}/api-key/rotate` (работает и как первичная выдача).
   Отдельный «кабинет ключей» помечен в коде как будущий трек — подтвердить
   rotate как официальный способ.
2. **Заголовок `x-api-key`.** В коде существует legacy-механизм статического
   общего ключа через заголовок `x-api-key`, но в production он не задан и не
   привязан к аккаунту клиента — из клиентской документации исключён. Подтвердить
   план его удаления из кода.
3. **`POST /clients/{id}/upgrade` без оплаты.** Смена тарифа сейчас работает без
   платёжного шлюза (осознанная заглушка на период интеграции эквайринга).
   Решить, публиковать ли эндпоинт в клиентской документации до запуска оплаты.
4. **Горизонты тарифов.** В документации указаны значения, которые реально
   применяет код (`PLAN_SPECS`): Free 7 / Start 30 / Business 90 дней. Отдельные
   комментарии в коде упоминают устаревшие 90/365 — комментарии требуют чистки.
5. **`datasets_limit` (1/3/10).** В коде значения помечены как плейсхолдеры до
   решения владельца по тарифной сетке — подтвердить перед широкой публикацией.
6. **Политика версионирования.** Путей с версионным префиксом нет; формальная
   политика обратной совместимости не задана — определить и опубликовать.
7. **Captcha в парольном флоу.** `/auth/login/password` требует Turnstile-токен
   после 2 неудач — для полностью серверных интеграций документация рекомендует
   `/auth/token`; подтвердить эту рекомендацию как официальную.
