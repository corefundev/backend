# Reproduction — фактически выполненные команды и вывод

> Каждый вывод аудита воспроизводим. Окружение: macOS Darwin 25.2.0, system `python3 3.9.6` (user site `~/Library/Python/3.9`).
> ⚠️ Локальные версии библиотек ДРЕЙФУЮТ от pinned (см. ниже) — локальные прогоны не воспроизводят прод байт-в-байт.

## 0. Git / расположение
```
$ cd /Users/ilailin/Desktop/core && git rev-parse --is-inside-work-tree
fatal: not a git repository            # корень — не репо
$ git -C backend remote -v
origin  https://github.com/corefundev/backend.git
$ git -C backend log --oneline -1
5e4f82f Revert "C1.3 slice 1 (#173): migrate SANDBOX_* env reads to settings"
$ git -C backend fetch origin && git -C backend rev-list --left-right --count origin/main...HEAD
0  0                                   # синхронно с remote
$ git -C frontend rev-list --left-right --count origin/main...HEAD
0  0
```
`Dataset/`: `1c_train.csv` (8.8MB), `1c_test.csv` (776KB), `1c_items.csv` (11KB), даты файлов 2021-02-10.

## 1. Стек / дрейф версий (FINDING-0-A)
```
$ python3 -c "import fastapi,pydantic,sklearn; print(fastapi.__version__,pydantic.__version__,sklearn.__version__)"
0.128.8 2.13.4 1.6.1
# requirements.txt pinned: fastapi==0.135.0  pydantic==2.7.1  sklearn==1.5.2   → ДРЕЙФ
```

## 2. Сбор тестов (FINDING-0-B)
```
$ python3 -m pytest --collect-only -q | tail -3
1143 tests collected in 2.60s
# WARNING: Unknown config option: asyncio_mode  → pytest-asyncio отсутствует в этом окружении
#          ⇒ async-тесты локально могут не исполняться как в CI (Linux)
```
Полный прогон pytest / mypy / запуск приложения / миграции локально НЕ выполнялись — см. `BLIND_SPOTS.md` (libomp + version drift + потребность в БД/Redis/S3).

## 3. Независимый пересчёт метрик (Phase 3.3) — ПОДТВЕРЖДЕНО
```
$ python3 -c "<src.validation.metrics против ручного эталона>"
WMAPE  code=0.110000  ref=0.110000  match=True
SMAPE  code=0.121896  ref=0.121896  match=True
MASE   code=1.527778  ref=1.527778  match=True
WMAPE(all-zero y)→nan ; SMAPE(both-zero)→nan ; MASE(const train)→nan ; MASE(len1 train)→nan(+RuntimeWarning)
```
Вывод: формулы WMAPE/SMAPE/MASE математически верны; нулевые знаменатели обрабатываются.

## 4. Численное доказательство MASE concat-бага (M-A6) — ПОДТВЕРЖДЕНО
```
$ python3 -c "<compute_metrics_per_sku с 3 копиями train-серии, как делает merge в walk_forward>"
naive_mae correct          = 1.6000
naive_mae buggy(concat x3) = 1.8824
MASE correct               = 0.6250
MASE from compute_metrics_per_sku = 0.5312
=> per-SKU MASE distorted by -15.0%   # оптимистично (модель выглядит лучше)
```

## 5. Ключевые grep'ы (трассировка)
```
# preprocessing-leakage: НЕТ scaler/encoder fit на полном df
$ grep -rniE "StandardScaler|fit_transform|TargetEncoder|sku_encoded" src/pipeline/train.py src/features/
forecaster.py:183: "sku_encoded was killed in #58"   # известная утечка закрыта
# таргет НЕ лог-трансформируется
$ grep -rnE "log1p|expm1" src/pipeline src/models   → (пусто)
# метрика клиента = walk_forward agg (НЕ чистый backtest)
$ grep -rn "run_backtest" src scripts → только backtest_runner.py + scripts + tests (НЕ run_training_pipeline)
$ grep -rn "wmape_global" src/pipeline/task_queue.py → :251 persist в sku_training_runs
```

## 6. Личная переверификация (чтение тела)
- `inference.py:366-445` — `predict` это `async def`, в теле нет `await`; sync `registry.get`/`build_features`/`serve_forecast` → ASYNC-1 CONFIRMED.
- `clients.py:89-126` — `register_client` пишет `config=req.config` напрямую без `validate_client_config` → SEC-1 CONFIRMED.
- `train.py:179-296` — порядок шагов: `fit_detect`(3) до `build_features`(4); `_ = sample_weights_full[...]` no-op; `fit(...)` без `sample_weight` → M-A8 CONFIRMED.
- `walk_forward.py:93-263`, `engineering.py:139-152`, `metrics.py:11-163`, `backtest.py`, `anomaly_detection.py`, `hpo.py` — прочитаны полностью.

## 7. Многоагентный workflow
`wh2zik7ch` (run `wf_edb807a3-f79`): 10 finder-агентов (9 фаз) → adversarial-верификация 3 CRIT/HIGH. 13 агентов, ~970k токенов, 296 tool-uses, 739s. Результат: 43 находки (3 HIGH→все понижены до MEDIUM, 0 refuted-as-fake, 40 MEDIUM/LOW/INFO). Полный JSON: `tasks/wh2zik7ch.output`.
