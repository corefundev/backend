# Coverage Map — статус покрытия по модулям

Легенда: ✅ проверен (тело прочитано/трассировано) · 🟡 частично (греп/finder, не полное чтение) · ⬜ не проверен (+причина).
Фокус — `backend/src/` (приложение + ML). Инфра/frontend/docker — см. `BLIND_SPOTS.md`.

## ML-ядро (Phase 3 — наивысший приоритет)
| Файл | Статус | Примечание |
|---|---|---|
| src/validation/metrics.py | ✅ | Формулы пересчитаны (совпало); M-A6/A5/A7 |
| src/validation/walk_forward.py | ✅ | Сплиты чисты; M-A1; TEST-2 (нет теста) |
| src/validation/backtest.py | ✅ | Чистый holdout, но M-A2 (не в проде) |
| src/validation/backtest_runner.py | 🟡 | Через grep/finder; гейт-обвязка |
| src/features/engineering.py | ✅ | past-only ✓; stock same-day (L-A4) |
| src/features/weather.py | 🟡 | L-A4 (finder) |
| src/features/external_regressors_ru.py | 🟡 | L-A4 (finder) |
| src/features/holidays_features.py | 🟡 | PERF-3 (finder) |
| src/models/forecaster.py | ✅ | sku_encoded killed; L-A10 |
| src/models/mimo.py | ✅(чтение ключевых) | L-A10, INFO-A13 |
| src/models/ensemble.py | 🟡 | через train.py |
| src/models/fallback.py | 🟡 | SeasonalNaive(7), M-A5 |
| src/models/hpo.py | ✅ | M-A1 |
| src/models/cold_start.py, explainer.py | 🟡 | dead-compute в train (finder) |
| src/pipeline/train.py | ✅ | M-A8, M-A1, dead-compute |
| src/pipeline/inference_utils.py | 🟡 | PERF-2, INFO-A12, L-A4 (finder) |
| src/pipeline/batch_inference.py | 🟡 | finder |
| src/pipeline/task_queue.py | 🟡 | BUG-1, ARCH-1, DEAD-3 (finder + личная сверка BUG-1) |
| src/data/anomaly_detection.py | ✅ | M-A8, L-A3 |
| src/data/loader.py | 🟡 | PERF-1, L-A11 (finder) |
| src/data/ge_validator.py | ⬜ | не дочитан (GE-валидация, низкий риск) |

## API / роутеры
| Файл | Статус | Примечание |
|---|---|---|
| src/api/routers/inference.py | ✅ | ASYNC-1/2, PERF-batch, CONTRACT-batch (личная сверка predict) |
| src/api/routers/clients.py | ✅ | SEC-1 (личная сверка register_client) |
| src/api/routers/training.py | 🟡 | BUG-1 trigger, CONTRACT-1 (finder) |
| src/api/routers/config.py | 🟡 | BUG-2 (finder) |
| src/api/routers/auth.py | 🟡 | ARCH-2 (finder, 1230 LOC) |
| src/api/routers/notifications.py | 🟡 | ASYNC-tg (finder) |
| src/api/routers/ops.py | 🟡 | INFO-metrics-token, CONTRACT-2 (finder) |
| src/api/routers/{audit,legal,plans}.py | ⬜ | не дочитаны (низкий риск, finder-скан без находок) |
| src/api/main.py | 🟡 | CONTRACT-2 (нет exception-handler) |
| src/api/rate_limit.py | ✅ | DEAD-1, ASYNC-race (finder + сверка) |
| src/api/{loaders,service_cache,startup_safety,uploads,webhooks}.py | 🟡 | finder-скан |

## Auth / storage / clients / прочее
| Файл | Статус | Примечание |
|---|---|---|
| src/auth/jwt_auth.py | 🟡 | DEAD-2 (finder) |
| src/auth/vault_agent.py | 🟡 | INFO-dead-vault (finder) |
| src/auth/signup_rate_limit.py | 🟡 | live limiter, атомарный (finder) |
| src/auth/oauth/* | ⬜ | не проверены (отдельный security-проход) |
| src/storage/backend.py | 🟡 | INFO-hmac (finder) |
| src/storage/forecasts.py | 🟡 | PERF-4, INFO-dead-FP (finder) |
| src/storage/{anomalies,training_runs}.py | 🟡 | PERF-4 (finder) |
| src/clients/registry.py | ✅(ключевое) | PERF-4, sync conn (ASYNC-1) |
| src/clients/config_manager.py | 🟡 | SEC-1, BUG-2 (finder) |
| src/plans/* | 🟡 | plan-tier defaults (через train.py) |
| src/monitoring/*, src/notifications/*, src/audit/* | 🟡 | finder-скан |

## Вне фокуса (⬜ + причина)
- frontend/** — другой репозиторий, отдельный заход.
- docker/**, ansible/**, migrations/**, scripts/**, dags/** — инфра, см. BLIND_SPOTS §6.
- tests/** — мета-аудит проведён выборочно по ML/metric/backtest/walk-тестам (TEST-1…5); полный обход всех 1143 тестов не делался.
