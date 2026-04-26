# SKU Forecasting Platform v8.0

Production-grade MLOps платформа прогнозирования спроса.
**287 тестов · 59 модулей · 9 217 строк · 0 warnings из нашего кода**

---

## Быстрый старт

```bash
# 1. Скопировать и заполнить .env
cp .env.example .env && nano .env

# 2. Сгенерировать секреты (обязательно!)
python3 -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('API_KEY=' + secrets.token_urlsafe(32))"

# 3. Запустить
cd docker && docker compose -f docker-compose.beget.yml up -d

# 4. Проверить
curl http://localhost:8000/health
```

---

## Архитектура

```
Данные → GE Validate → Anomaly Detection → Feature Store (offline S3 + online Redis)
       → HPO (Optuna) → Walk-Forward CV → Train (MIMO / Ensemble / Clustered)
       → Conformal Prediction Intervals → Model Registry Gates → API (FastAPI / gRPC)
       → SLA Monitor → Drift Detection → Auto-Retrain → Canary Deploy
```

---

## Все модули (59)

### src/data/
| Файл | Назначение |
|---|---|
| `loader.py` | Загрузка CSV/Parquet, fill gaps, валидация |
| `ge_validator.py` | Great Expectations: 10 проверок |
| `anomaly_detection.py` | IsolationForest + IQR → sample_weight |
| `versioning.py` | SHA-256 dataset hash, DVC, MLflow lineage |

### src/features/
| Файл | Назначение |
|---|---|
| `engineering.py` | 51 признак (лаги, rolling, календарь, цена, промо, OOS) |
| `weather.py` | Open-Meteo API, 13 погодных признаков, кэш |
| `holidays_features.py` | 100+ стран, pre/post-holiday флаги |
| `store.py` | Feature Store: offline (S3/Parquet) + online (Redis <50ms) |

### src/models/
| Файл | Назначение |
|---|---|
| `forecaster.py` | LightGBM глобальная модель |
| `mimo.py` | Direct multi-step (нет накопления ошибок) + quantile p10/p50/p90 |
| `ensemble.py` | LightGBM + CatBoost + Ridge, веса по WMAPE |
| `hpo.py` | Optuna TPE hyperparameter search |
| `online_learning.py` | **NEW** Incremental fit через init_model, деградация WMAPE |
| `sku_clustering.py` | **NEW** k-means по паттернам → отдельная модель на кластер |
| `explainer.py` | SHAP global + per-prediction |
| `shap_storage.py` | SHAP → S3 Parquet |
| `cold_start.py` | Cluster-based fallback для новых SKU |
| `fallback.py` | SeasonalNaive + @retry |
| `hierarchical.py` | SKU→category→region, bottom-up/top-down/middle-out |
| `registry_gates.py` | None→Staging→Production→Archived, validation gate |

### src/validation/
| Файл | Назначение |
|---|---|
| `metrics.py` | MASE, WMAPE, SMAPE |
| `walk_forward.py` | Expanding window CV, no random splits |
| `calibration.py` | Quantile coverage, pinball loss, isotonic calibration |
| `conformal.py` | **NEW** Гарантированные coverage intervals P(y∈[l,u])≥1-α |

### src/storage/
| Файл | Назначение |
|---|---|
| `backend.py` | LocalStorageBackend / S3StorageBackend (Beget) |

### src/clients/
| Файл | Назначение |
|---|---|
| `registry.py` | PostgreSQL + JSON file registry |
| `config_manager.py` | Per-client config (deep merge поверх system defaults) |
| `async_registry.py` | **NEW** asyncpg async registry, pool min=5/max=20 |

### src/auth/
| Файл | Назначение |
|---|---|
| `jwt_auth.py` | JWT HS256 + API Key, RBAC |
| `secrets.py` | Vault KV v2 (hvac) → env vars fallback |
| `vault_agent.py` | **NEW** Level 3 zero-.env: AppRole, auto-renew, dynamic DB creds |

### src/monitoring/
| Файл | Назначение |
|---|---|
| `drift.py` | PSI feature drift, prediction drift (rolling WMAPE) |
| `advanced.py` | Evidently, business metrics, auto-retrain trigger |
| `logging_setup.py` | **NEW** Structured JSON logging (pythonjsonlogger → Loki) |
| `tracing.py` | **NEW** OpenTelemetry spans, graceful no-op |
| `sla.py` | **NEW** SLO tracking, error budget, Prometheus export |
| `chaos.py` | **NEW** Chaos experiments: inject_fault, run_standard_chaos_suite |

### src/pipeline/
| Файл | Назначение |
|---|---|
| `train.py` | 9-шаговый pipeline |
| `batch_inference.py` | Daily batch forecast |
| `inference_utils.py` | Shared forecast loop (dedup), @lru_cache config |
| `auto_retrain.py` | CanaryRouter 10%, auto-retrain, promote/rollback |
| `task_queue.py` | Redis rq async queue |
| `streaming.py` | **NEW** Kafka producer + consumer, in-memory fallback |
| `distributed_training.py` | **NEW** Ray parallel training, sequential fallback |

### src/api/
| Файл | Назначение |
|---|---|
| `main.py` | FastAPI: predict, train, explain, config CRUD, health |
| `rate_limit.py` | Redis sliding window, тиры free/pro/enterprise |
| `webhooks.py` | HMAC-SHA256, retry backoff |
| `grpc_server.py` | **NEW** gRPC servicer: predict_single, predict_batch, predict_stream |

---

## Безопасность: три уровня

```
Level 1: .env файл на диске          → STORAGE_BACKEND=s3, AWS_KEY=... (dev only)
Level 2: Vault + минимальный .env    → VAULT_ADDR + VAULT_TOKEN в .env
Level 3: Zero .env (production)      → VAULT_ROLE_ID + одноразовый VAULT_SECRET_ID от CI/CD
                                        Vault Agent инжектирует в os.environ через tmpfs
```

Настройка Level 3:
```bash
# 1. Настроить Vault
export VAULT_ADDR=http://localhost:8200 VAULT_TOKEN=root
bash vault/setup_vault.sh

# 2. При каждом деплое — одноразовый secret_id
VAULT_SECRET_ID=$(vault write -field=secret_id -f auth/approle/role/sku-forecasting/secret-id)

# 3. Запуск — secret_id уничтожается после использования
VAULT_ROLE_ID=<role_id> VAULT_SECRET_ID=$VAULT_SECRET_ID docker compose up -d
```

---

## S3 настройки (Beget)

Все в `.env`:
```bash
STORAGE_BACKEND=s3
S3_BUCKET=sku-forecasting
S3_ENDPOINT_URL=https://s3.beget.com
AWS_ACCESS_KEY_ID=ключ_из_beget
AWS_SECRET_ACCESS_KEY=секрет_из_beget
AWS_DEFAULT_REGION=ru-1
```

---

## Запуск тестов

```bash
# Все 287 тестов
APP_ENV=test JWT_SECRET_KEY=... API_KEY=... python -m pytest tests/ -v

# Только новые модули
python -m pytest tests/unit/test_missing_improvements.py -v

# Concurrent load test (5 клиентов параллельно)
python -m pytest tests/integration/test_concurrent_load.py -v -s

# Chaos engineering
python -m pytest tests/unit/test_missing_improvements.py::TestChaosEngineering -v
```

---

## CI/CD

```
git push → lint → mypy → unit → integration → chaos → docker build
                                                           ↓ (main only)
                                              staging deploy → smoke test
                                                           ↓ (manual approval)
                                              production rolling deploy
```

---

## Сервисы

| Сервис | Порт | Доступ |
|---|---|---|
| API (FastAPI) | 8000 → 80/443 | публичный через Nginx |
| MLflow | 5000 | SSH tunnel: `ssh -L 5000:localhost:5000 user@server` |
| Grafana | 3000 | SSH tunnel: `ssh -L 3000:localhost:3000 user@server` |
| Vault UI | 8200 | SSH tunnel: `ssh -L 8200:localhost:8200 user@server` |
| Prometheus | 9090 | SSH tunnel |

---

## Итоговые характеристики

| Метрика | Значение |
|---|---|
| Тестов | 287 (all green) |
| Warnings из нашего кода | 0 |
| Исходных модулей | 59 |
| Строк кода | 9 217 |
| Параллельное обучение | 5 клиентов → ускорение 5x |
| Inference p50 | ~200ms (с feature engineering) |
| Conformal coverage | ≥ 90% гарантировано |
| Секреты на диске | 0 (Level 3) |
