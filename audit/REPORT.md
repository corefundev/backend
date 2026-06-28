# Independent Evidentiary Audit — corefundev/backend — REPORT

**Дата:** 2026-06-28 · **Ветка:** `main @ 5e4f82f` (синхронна с origin) · **Режим:** read-only
**Метод:** ручная трассировка тела кода + фактические прогоны; 10 finder-агентов по 9 фазам → adversarial-верификация каждой CRIT/HIGH-находки независимым скептиком → личная переверификация. Comments/names/docstrings трактовались как гипотезы, не доказательства.

## Executive summary

Кодовая база зрелая и многократно проаудированная (R5–R13): безопасность сильная (fail-closed подписи моделей, нет untrusted-pickle ingress, авторизация на маршрутах присутствует), классических утечек данных в ML НЕТ, формулы метрик **математически верны** (пересчитал независимо — совпало). **Критических и однозначно-High находок не подтверждено**: все 3 первоначально-HIGH после adversarial-проверки и моего перечтения понижены до MEDIUM (реальный, но degraded-mode / per-worker / условный импакт).

Главная содержательная тема — **не корректность результата, а честность ОЦЕНКИ качества и доступность под нагрузкой**:
1. Метрика точности, показываемая платным клиентам, **оптимистично смещена** (HPO selection-bias + слабый MASE-бейзлайн + train/serve skew). Прогнозы не повреждены — искажена их оценка.
2. Горячие async-роуты (`/predict`, `/auth/token`, training-trigger, telegram-webhook) выполняют **блокирующий sync I/O и CPU на event-loop** без offload → деградация конкурентности воркера.
3. Несколько подсистем **инертны/мертвы** (anomaly down-weighting не применяется, IsolationForest не исполняется, dead-модули).

### Счётчики (после adversarial-верификации)

| Severity | Кол-во | Примечание |
|---|---|---|
| CRITICAL | **0** | — |
| HIGH | **0** | 3 первоначальных HIGH понижены верификатором до MEDIUM |
| MEDIUM | **17** | оценка качества + блокировка event-loop + архитектура |
| LOW | **16** | локальные баги, dead-код, тесты |
| INFO | **7** | наблюдения/подтверждения чистоты |
| **Всего** | **43** (+ подтверждение чистоты сплитов) | |

### Топ-риски (по убыванию практической важности)
1. **M-A1 HPO selection-bias** — клиенту показывается оптимистично смещённый WMAPE/MASE (платные тарифы). Подрывает champion/challenger-гейт.
2. **BUG-1 sync-fallback обучения** — при Redis-blip «успешная» тренировка теряет историю при инкрементальном ретрейне, не генерит прогнозы, замораживает воркер на ~десятки минут.
3. **ASYNC-1 блокирующий event-loop** на `/predict` и др. — эффективная конкурентность воркера ≈1 на горячем пути.
4. **M-A2 нет honest baseline-гейта** в проде — регрессия качества/проигрыш seasonal-naive уедет незамеченным.
5. **M-A8 anomaly-подсистема инертна** — выбросы НЕ понижаются в весе вопреки дизайну; жжёт компьют.

---

## MEDIUM (17)

### Точность модели → детально в `model_accuracy.md`
- **[M-A1] HPO selection-bias: заявленная метрика = цель отбора гиперпараметров, не honest holdout.** `train.py:218-253`, `hpo.py:96-102`, `task_queue.py:251`. Только Start/Business. *CONFIRMED (мной независимо + finder).*
- **[M-A2] Нет автоматического baseline/champion-challenger гейта; backtest docstring заявляет «GATE», но в проде/CI порога нет; нет head-to-head с seasonal-naive на holdout.** `backtest.py:6-9`, `backtest_runner.py`. *CONFIRMED.*
- **[M-A5] MASE на lag-1 (m=1), а продукт деплоит seasonal-naive(7); письмо клиенту «MASE<1 = лучше базового» вводит в заблуждение.** `metrics.py:18`, `fallback.py:152`, `training_email.py:101`. *CONFIRMED.*
- **[M-A6] per-SKU MASE искажён concat-дублированием train-серии (−15% в численном примере, оптимистично).** `metrics.py:64`, `walk_forward.py:142-149`. *CONFIRMED ФАКТОМ (численный прогон).*
- **[M-A8] Anomaly-подсистема инертна:** IsolationForest не исполняется (порядок шагов), sample_weight не применяется → выбросы не down-weight'ятся; docstring лжёт. `train.py:179-212`, `anomaly_detection.py:65-87`. *CONFIRMED.*

### Доступность / event-loop
- **[BUG-1 → MEDIUM] Sync-fallback обучения при Redis-down расходится с worker-путём:** теряет `extend_from_paths` (история инкрементального ретрейна), не генерит `sku_forecasts`, блокирует event-loop на время тренировки. *(Верификатор опроверг под-claim «client stuck» — router сбрасывает status; остальное CONFIRMED.)* `task_queue.py:761-807` vs `_training_job:391-447`; `training.py:309`.
- **[ASYNC-1 → MEDIUM] Горячие `async def` роуты выполняют блокирующий sync I/O+CPU на loop без offload** (`predict`, `get_token` bcrypt cost12, training-trigger). Личная переверификация: `inference.py:366-445` — нет `await`. Per-worker деградация. `registry.py:208`, `loaders.py:46`, `signup_rate_limit.py:202`.
- **[ASYNC-2 → MEDIUM] Sync ML-инференс в async `/predict`** (дубль корня ASYNC-1, perspective производительности). `inference.py:366,416,440`.
- **[PERF-batch / MEDIUM] `predict_batch` `asyncio.gather` не даёт реальной конкурентности** (нет await-точек в `predict`) — заявленный N× speedup иллюзорен, и весь батч держит loop. `inference.py:484-523`.
- **[CONTRACT-batch / MEDIUM] `/predict/batch` всё-или-ничего:** первая `HTTPException` любого элемента (422/404/429) рушит весь батч (`return_exceptions=False`); + амплификация rate-limit (100 элементов = 100 токенов). `inference.py:518`.
- **[ASYNC-tg / MEDIUM] `telegram_webhook` (async) блокирует loop на `time.sleep`+sync `urlopen` до ~37s.** `notifications.py:149-151` → `telegram.py:100-107`.

### Производительность
- **[PERF-1 / MEDIUM] Каждый `/predict`/`/sku-history` скачивает и парсит ВЕСЬ processed-parquet ради одного SKU** (нет `columns=`/`filters=` pushdown). `loader.py:22-45,90-110`, `inference.py:193`.
- **[PERF-2 / MEDIUM] `recursive_forecast` пересобирает полный DataFrame через `pd.concat` на каждом шаге горизонта (квадратично per-SKU).** `inference_utils.py:342`.

### Архитектура
- **[ARCH-1 / MEDIUM] `task_queue.py` (878 LOC) — god-module:** rq-инфра + training-lifecycle + forecast/anomaly business-logic + notifications; 25 function-local import'ов прячут реальный граф зависимостей (static cycle-scan = 0 частично из-за этого). `task_queue.py`.
- **[ARCH-2 / MEDIUM] `auth.py` (1230 LOC) god-router:** 9 маршрутов (token/OTP/OAuth) + handler'ы 200+ LOC (`oauth_callback` ~238, `auth_signup_verify` ~197) с inline captcha/OTP/rate-limit/OAuth-state.

### Тесты
- **[TEST-1 / MEDIUM] `test_no_recursive_leakage` проверяет только равенство `feature_cols`, НЕ отсутствие утечки** — named-guard против H2 ничего не проверяет. `test_improvements.py:74-85`.
- **[TEST-2 / MEDIUM] `walk_forward_validate` (источник headline-метрик) не покрыт прямым тестом no-overlap/date-mapping;** покрыт только приватный `_get_split_points`, и тот не проверяет no-overlap. `walk_forward.py:57-221`.

## LOW (16)

- **[L-A3] Anomaly preprocessing-leakage латентная** (no-op сегодня; станет leaky при wiring весов без fold-aware). `anomaly_detection.py:54-63`.
- **[L-A4] Train/serve skew по weather/FX** (same-day, carry-forward на serve). `weather.py:121`, `external_regressors_ru.py:265`, `inference_utils.py:194`.
- **[L-A7] mase_global: несогласованные окна** числитель(все фолды)/знаменатель(первый фолд). `metrics.py:138`.
- **[L-A9] Non-default `type:lgbm`: 1-step метрика vs рекурсивный serve.** `walk_forward.py:124-133`.
- **[L-A10] Недетерминизм обучения** — нет `random_state`/`deterministic` на LGBM при стохастике+`n_jobs=-1`. `forecaster.py:76-98`, `mimo.py:49-70`.
- **[L-A11] Gap-fill оставляет price/promo/stock=NaN** (дефолты до `_fill_time_gaps`). `loader.py:148-156`.
- **[SEC-1] `POST /clients` пишет config без `validate_client_config`** → обход R13-1 forbidden-path-key guard + range + plan-tier (admin-gated, потому LOW). Личная переверификация: `clients.py:117-126`. *CONFIRMED.*
- **[BUG-2] `validate_client_config` lat/lon: незащищённый `float()` → 500 вместо 422** на non-numeric (Business). `config_manager.py:252-257`.
- **[ASYNC-race] `RateLimiter._check_memory` — незащищённый read-modify-write по shared global** (но класс не в проде-пути; live-limiter атомарный). `rate_limit.py:98-117`.
- **[CONTRACT-1] `GET /training-runs` глотает ошибки backend → 200 `{runs:[]}`** (outage неотличим от «нет истории»). `training.py:401-403`.
- **[CONTRACT-2] Нет app-level exception-handler'ов → 3 разных формы error-envelope** (string detail / list-of-objects 422 / readyz). `main.py`, `ops.py:129-173`.
- **[DEAD-1] `RateLimiter`/`get_limiter` — мёртвый параллельный rate-limiter** (~124 LOC; live = `signup_rate_limit`). `rate_limit.py`.
- **[DEAD-2] `_LazyStr`+`_jwt_secret_prop` — сироты заброшенной `__getattr__`-схемы**; комментарий описывает несуществующий механизм. `jwt_auth.py:115-127`.
- **[DEAD-3] `_batch_inference_job` rq-job никогда не enqueue'ится** (~34 LOC). `task_queue.py:681-714`.
- **[PERF-3] Holiday-фичи: per-row Python `apply`, O(rows×holidays)** на полном multi-SKU фрейме. `holidays_features.py:65-78`.
- **[PERF-4] Каждый запрос к registry/storage открывает новый psycopg2-коннект** + дефолт `FORECASTS_LIST_HARD_CAP=200000` sync на async-пути. `registry.py:207`, `forecasts.py`.
- **[TEST-3] SMAPE: нет known-value теста** (factor-of-2 не закреплён). `test_metrics.py:45-55`.
- **[TEST-4] Нет теста детерминизма обучения.** (связано с L-A10).
- **[TEST-5] Train/serve parity тестируется только на уровне набора колонок, не значений.** `test_features.py:69-79`.

> (LOW перечислено больше 16 из-за под-пунктов тестов; канонический счёт по структурному выводу workflow — см. `counts`.)

## INFO (7)
- **[INFO-split] Подтверждение чистоты сплитов/фич** (см. `model_accuracy.md` 3.1). *CONFIRMED.*
- **[INFO-A12] Интервалы p10/p90 неоткалиброваны** (нет conformal/coverage). `mimo.py:120-149`.
- **[INFO-A13] MIMO quantile наследует лишний `tweedie_variance_power`** (безвредно). `mimo.py:62,142`.
- **[INFO-metrics-token] `/metrics` token-gate инертен при пустом `METRICS_TOKEN`** (enum client_id внутри docker-сети; известный R10-S6). `ops.py:176-203`.
- **[INFO-hmac] Model-signing HMAC падает на `JWT_SECRET_KEY` при отсутствии `MODEL_SIGNING_KEY`** (key reuse, не RCE — verify fail-closed). `backend.py:60-94`.
- **[INFO-dead-FP] `ForecastPoint` dataclass не используется.** `forecasts.py:38-42`.
- **[INFO-dead-vault] `vault_agent.ensure_bootstrapped()` не вызывается.** `vault_agent.py:475-481`.

---

## Замечание о severity-дисциплине

Adversarial-верификатор понизил все 3 первоначальных HIGH→MEDIUM и опроверг одно ложное под-утверждение (sync-fallback «client stuck» — router фактически сбрасывает status). Ноль подтверждённых CRITICAL/HIGH — это согласуется с историей зрелого кода (R5–R13). Личная переверификация трёх самых весомых (blocking-async `predict`, R13-1 обход в `register_client`, sync-fallback) подтвердила тело кода. Подробности доказательств — в `model_accuracy.md`, `reproduction.md`; границы — в `BLIND_SPOTS.md`.
