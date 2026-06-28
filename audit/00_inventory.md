# Audit 00 — Inventory & Territory Map

> Независимый доказательный аудит. Все факты получены фактическими командами (см. `reproduction.md`).
> Дата: 2026-06-28. Аудитор: principal/staff engineer (read-only).

## 0. Расположение кода и git-состояние (ПОДТВЕРЖДЕНО ФАКТОМ)

- Корень `/Users/ilailin/Desktop/core` — **НЕ** git-репозиторий (`fatal: not a git repository`).
- `backend/` — git-репо `https://github.com/corefundev/backend.git`, ветка `main @ 5e4f82f`.
  - `git rev-list --left-right --count origin/main...HEAD` → `0  0` (синхронно с remote после `git fetch`).
  - HEAD: `5e4f82f Revert "C1.3 slice 1 (#173): migrate SANDBOX_* env reads to settings"`.
- `frontend/` — git-репо `https://github.com/corefundev/frontend.git`, `main @ 91f0f41`, `0 0` (синхронно).
- `Dataset/` — реальные CSV (1C, 2021): `1c_train.csv` (8.8 MB), `1c_test.csv` (776 KB), `1c_items.csv` (11 KB). → материал для независимого пересчёта метрик (Фаза 3).

## 1. Стек и версии (из requirements.txt — pinned)

| Компонент | Pinned (requirements.txt) | Установлено локально (system py 3.9.6) |
|---|---|---|
| python | (Dockerfile TBD) | 3.9.6 |
| fastapi | 0.135.0 | **0.128.8** ❌ drift |
| pydantic | 2.7.1 | **2.13.4** ❌ drift |
| starlette | 1.3.1 | TBD |
| pydantic-settings | 2.3.4 | TBD |
| scikit-learn | 1.5.2 | **1.6.1** ❌ drift |
| lightgbm | 4.6.0 | TBD (import OK) |
| numpy | 1.26.4 | TBD |
| pandas | 2.2.2 | TBD |
| scipy | 1.13.0 | TBD |
| mlflow | 3.13.0 | TBD |
| sqlalchemy/alembic | TBD | TBD |
| redis / rq | 5.0.4 / 1.16.2 | TBD |
| optuna | 3.6.1 | TBD |
| evidently | 0.5.0 | TBD |
| great-expectations | 0.18.15 | TBD |
| holidays | 0.46 | TBD |

**[FINDING-0-A] Локальное окружение ≠ pinned deps.** fastapi/pydantic/sklearn установлены в других версиях.
Любой локальный прогон тестов или пересчёт метрик не воспроизводит прод-поведение байт-в-байт. Прод-факты
надо брать из CI (Linux) / контейнера. Severity: INFO (для аудита) / влияет на доверие к локальным прогонам.

**[OBS] pyproject комментарии устарели:** filterwarnings ссылается на «mlflow 2.13», но pinned mlflow==3.13.0.
Комментарий — гипотеза, не факт (правило №1).

## 2. Точки входа

- HTTP app: `src/api/main.py` (FastAPI). Роутеры (11): `src/api/routers/{audit,auth,clients,config,inference,legal,notifications,ops,plans,training}.py`.
- Прочее в api: `_helpers, _origins, loaders, metrics, rate_limit, service_cache, startup_safety, uploads, webhooks`.
- Воркеры/очереди: `src/pipeline/{task_queue,upload_workers,train,batch_inference}.py` (rq).
- Миграции: `migrations/` (alembic).
- Скрипты: `scripts/` (~51), включая cron-обёртки (`cron_wrapper.sh`, `audit_log_retention.sh`, `archive_mode_check.sh`, `mirror_prune.sh`), DR-дриллы, lockbox.
- DAGs: `dags/` (Airflow? — TBD).

## 3. ML-карта (сердце аудита — Фаза 3)

| Слой | Файлы |
|---|---|
| Данные | `src/data/{loader,anomaly_detection,ge_validator}.py` |
| Признаки | `src/features/{engineering,external_regressors_ru,holidays_features,weather}.py` |
| Модели | `src/models/{forecaster,mimo,ensemble,cold_start,fallback,hpo,explainer}.py` |
| Пайплайн | `src/pipeline/{train,batch_inference,inference_utils}.py` |
| Валидация/метрики | `src/validation/{metrics,walk_forward,backtest,backtest_runner,secure_parser}.py` |
| Скрипты | `scripts/backtest_ab_serve.py`, `scripts/eval/` |

Ключевые цели Фазы 3:
- `src/validation/metrics.py` — ручная верификация формул (MAPE/WMAPE/MASE/SMAPE и пр.).
- `src/validation/walk_forward.py` + `backtest.py` — корректность временных сплитов (no-shuffle, no overlap).
- `src/features/engineering.py` — look-ahead/target/preprocessing leakage.
- `src/pipeline/train.py` — fit-on-train-only, train↔serve skew.

## 4. Размер

- `src/`: 101 Python-файлов, ~21 274 LOC.
- Тестов собрано pytest: **1143**.

## 5. Статус сборки/тестов (фактический)

- Локально нет venv; зависимости стоят в `~/Library/Python/3.9` (user site), версии дрейфуют (см. FINDING-0-A).
- `pytest --collect-only` → 1143 теста собраны за 2.6s, БЕЗ ошибок коллекции.
- `asyncio_mode` → «Unknown config option» ⇒ pytest-asyncio в этом окружении отсутствует ⇒ async-тесты могут
  не выполняться как ожидается локально (CI — отдельно). **[FINDING-0-B]** caveat воспроизводимости.
- mypy/lint/полный pytest-run — TBD (см. reproduction.md).

## Статус Фазы 0: ЗАВЕРШЕНА (с явными границами)
- git/стек/точки входа/ML-карта зафиксированы фактически; сбор тестов (1143) — ок.
- Полный прогон pytest/mypy/lint и запуск приложения НЕ выполнялись (libomp + version drift + потребность в БД/Redis/S3) — вынесено в `BLIND_SPOTS.md §2,§3`.
- Дальнейшие фазы 1–9 — см. `REPORT.md`; глубокий разбор точности — `model_accuracy.md`.
