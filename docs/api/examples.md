# Sprosly API — сквозные сценарии

Готовые bash-скрипты (нужны `curl` и `jq`). Перед запуском задайте переменные:

```bash
export API=https://api.sprosly.com
export CLIENT_ID=my-shop
export API_KEY=sku_...            # ваш API-ключ (см. README → Аутентификация)

# Получение токена (годен 60 минут)
export TOKEN=$(curl -s -X POST "$API/auth/token" \
  -H "Content-Type: application/json" \
  -d "{\"client_id\": \"$CLIENT_ID\", \"secret\": \"$API_KEY\"}" | jq -r .access_token)
```

---

## Сценарий 1. Загрузить CSV в датасет → подготовить → дождаться `processed`

```bash
#!/usr/bin/env bash
set -euo pipefail

AUTH=(-H "Authorization: Bearer $TOKEN")

# 1. Датасет: берём первый существующий или создаём новый.
DATASET_ID=$(curl -s "${AUTH[@]}" "$API/clients/$CLIENT_ID/datasets" \
  | jq -r '.datasets[0].dataset_id // empty')
if [ -z "$DATASET_ID" ]; then
  DATASET_ID=$(curl -s -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
    "$API/clients/$CLIENT_ID/datasets" \
    -d '{"name": "Основной"}' | jq -r .dataset_id)
fi
echo "dataset_id = $DATASET_ID"

# 2. Загрузка файла (CSV или XLSX, до 50 МБ). dataset_id в форме —
#    файл прикрепится к датасету автоматически после подготовки.
UPLOAD_ID=$(curl -s -X POST "${AUTH[@]}" \
  "$API/clients/$CLIENT_ID/uploads" \
  -F "file=@sales.csv" \
  -F "dataset_id=$DATASET_ID" | jq -r .upload_id)
echo "upload_id = $UPLOAD_ID"

# 3. Ждём антивирусную проверку: uploaded → scanning → scanned_clean.
while :; do
  STATUS=$(curl -s "${AUTH[@]}" "$API/uploads/$UPLOAD_ID" | jq -r .status)
  echo "status: $STATUS"
  case "$STATUS" in
    scanned_clean) break ;;
    infected|processing_failed)
      curl -s "${AUTH[@]}" "$API/uploads/$UPLOAD_ID" | jq .error_message; exit 1 ;;
  esac
  sleep 5
done

# 4. Запускаем подготовку (распознавание формата + автосопоставление колонок).
curl -s -X POST "${AUTH[@]}" \
  "$API/clients/$CLIENT_ID/uploads/$UPLOAD_ID/prepare" | jq .

# 5. Ждём processed и смотрим превью распознанных данных.
while :; do
  PREP=$(curl -s "${AUTH[@]}" "$API/clients/$CLIENT_ID/uploads/$UPLOAD_ID/prep")
  STATUS=$(echo "$PREP" | jq -r .status)
  echo "prep status: $STATUS"
  case "$STATUS" in
    processed) break ;;
    processing_failed) echo "$PREP" | jq .error_message; exit 1 ;;
  esac
  sleep 5
done
echo "$PREP" | jq '{row_count, sku_count, detected, sample}'

# 6. Контроль: файл прикреплён к датасету, собрана новая версия.
curl -s "${AUTH[@]}" "$API/clients/$CLIENT_ID/datasets/$DATASET_ID" \
  | jq '{current_version, files, row_count, sku_count, date_min, date_max}'
```

---

## Сценарий 2. Запустить обучение → поллинг статуса → прочитать метрики

```bash
#!/usr/bin/env bash
set -euo pipefail

AUTH=(-H "Authorization: Bearer $TOKEN")
DATASET_ID=...   # из сценария 1

# 1. Запуск обучения на текущей версии датасета.
#    409 (reason_code=in_flight) — обучение уже идёт;
#    429 (reason_code=cooldown) — пауза тарифа, см. Retry-After / cooldown_until.
RESP=$(curl -s -w '\n%{http_code}' -X POST "${AUTH[@]}" \
  "$API/clients/$CLIENT_ID/datasets/$DATASET_ID/train")
CODE=$(echo "$RESP" | tail -n1); BODY=$(echo "$RESP" | sed '$d')
if [ "$CODE" != "202" ]; then echo "train refused ($CODE):"; echo "$BODY" | jq .; exit 1; fi

JOB_ID=$(echo "$BODY" | jq -r .job_id)
RUN_ID=$(echo "$BODY" | jq -r .run_id)
echo "job_id = $JOB_ID, run_id = $RUN_ID"

# 2. Поллинг статуса задачи (обучение занимает от минут до ~часа
#    в зависимости от тарифа и объёма данных).
while :; do
  JOB=$(curl -s "${AUTH[@]}" "$API/jobs/$JOB_ID")
  STATUS=$(echo "$JOB" | jq -r .status)
  echo "job: $STATUS  progress: $(echo "$JOB" | jq -c .progress)"
  case "$STATUS" in
    finished) break ;;
    failed) echo "$JOB" | jq .error; exit 1 ;;
  esac
  sleep 30
done

# 3. Метрики качества — из постоянной истории обучений.
curl -s "${AUTH[@]}" "$API/clients/$CLIENT_ID/training-runs?limit=5" \
  | jq --arg run "$RUN_ID" \
    '.runs[] | select(.run_id == $run)
     | {status, n_skus, n_rows, wmape, wmape_order_7, wmape_order_14,
        baseline_wmape, mase, elapsed_sec, gate_passed}'

# gate_passed=false означает: новая модель не прошла контроль качества
# и НЕ опубликована — прогнозы продолжает давать прежняя модель.
```

---

## Сценарий 3. Рекомендации заказа CSV для 1С

```bash
#!/usr/bin/env bash
set -euo pipefail

AUTH=(-H "Authorization: Bearer $TOKEN")

# Окно заказа 14 дней (1–60). Опционально:
#   dataset_id=...        — конкретный датасет (по умолчанию первый активный)
#   service_level=0.85    — сервис-уровень 0.5–0.95 (тарифы Start/Business)
curl -s "${AUTH[@]}" \
  "$API/clients/$CLIENT_ID/order-recommendations?window=14&format=csv" \
  -o order-recommendations-14d.csv

head -3 order-recommendations-14d.csv
# sku;Остаток;Спрос (прогноз);Страховой запас;К заказу;Мин (p10);Макс (p90);Дней в окне;Дней с рекомендацией
# SKU-1;12;71,40;9,30;69;52,00;104,00;14;14
# ...
```

Свойства файла: UTF-8 с BOM, разделитель `;`, десятичная запятая, строки CRLF —
открывается в 1С и русском Excel без перекодировки. «К заказу» = прогноз спроса +
страховой запас − остаток, округление вверх до целых штук.

Тот же расчёт в JSON (для программной обработки):

```bash
curl -s "${AUTH[@]}" \
  "$API/clients/$CLIENT_ID/order-recommendations?window=14" \
  | jq '.items[] | {sku, stock, demand_sum, order_qty, days_covered}'
```

Если прогнозов ещё нет: `format=csv` вернёт `404` («Прогнозов ещё нет — обучите
модель»), JSON-вариант — `200` с `count: 0`.
