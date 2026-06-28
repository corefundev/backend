# Audit — Model Accuracy (Phase 3, highest priority)

> Презумпция: «всё неверно, пока не доказано». Заявленным цифрам не доверяю — пересчитываю.
> Все формулы и утечки проверены чтением ТЕЛА кода + фактическим прогоном. Дата 2026-06-28, ветка `main @ 5e4f82f`.

## Краткий вывод

**Доверять заявленной точности можно ЧАСТИЧНО.**
- ✅ Ядро корректно: временные сплиты строгие, lag/rolling строго past-only, нет preprocessing-leakage, формулы метрик математически верны (пересчитал — совпало до 6 знаков).
- ⚠️ Но **заявленная пользователю цифра систематически оптимистична** по нескольким независимым причинам (HPO selection-bias, слабый m=1 MASE-бейзлайн, train/serve skew по weather/FX, нет honest-holdout-гейта в проде). Деградации самих прогнозов нет — искажается ОЦЕНКА качества, показываемая клиенту.

---

## 3.1 Утечка данных — проверено

### ✅ Чисто (подтверждено чтением тела)
| Вектор | Проверка | Вердикт |
|---|---|---|
| Shuffle-сплит на рядах | `walk_forward.py:94-97` train `date≤split`, test `split<date≤split+H`; нет KFold/`shuffle=True` нигде | НЕТ утечки |
| Train/test overlap по времени | Тест-окна фолдов не пересекаются (`_get_split_points:261-263` end−i·H) | НЕТ |
| Lag-фичи | `engineering.py:141` `target.shift(lag)` внутри per-SKU группы, sorted by date | past-only ✓ |
| Rolling-фичи | `engineering.py:147-148` база `target.shift(1)`, окно по ней | past-only ✓ |
| Preprocessing на полном df | Грепнул — **нет** StandardScaler/encoder/`fit_transform`; LightGBM не требует масштаба; фичи — построчные детерминированные трансформы (значение train-строки не зависит от будущих) | НЕТ preprocessing-leakage |
| Групповая утечка (SKU в train+test) | Сплит временной, не по сущности — один SKU по времени в обоих сетах для рядов КОРРЕКТНО | НЕТ |
| MIMO target shift через границы SKU | `mimo.py:104` `y.groupby(groups).shift(-h)` — per-SKU | НЕТ смазывания будущего |
| sku_encoded (известный #58 skew) | `forecaster.py:183` «sku_encoded was killed in #58»; в фичах SKU как числа нет | закрыто |

### ⚠️ Найденные риски (НЕ нулевые)

**[M-A3] Anomaly preprocessing-leakage — латентная (LOW, сейчас no-op).** `anomaly_detection.fit_detect` считает per-SKU IQR-пороги + IsolationForest по ВСЕЙ серии (включая будущие тест-фолды) и возвращает `sample_weight`. Сегодня безвредно (веса не применяются, см. M-A8), НО как только wiring весов в `fit()` доделают без fold-aware — метрики станут leaky. Локация `anomaly_detection.py:54-63`, `train.py:206-212`.

**[M-A4] Train/serve skew по weather/FX (LOW→MEDIUM по сути).** `weather.py:121` same-day temp/rolling-7 без shift; `external_regressors_ru.py:265` `{ccy}_rub_change = pct_change()` (same-day). На origin-день это НЕ утечка будущего (origin известен), но при serve будущие даты не имеют реальных weather/FX → `inference_utils.py:194-207` `carry_cols` тащит последнее значение вперёд. Модель училась на реальной same-day вариативности, которой на инференсе не получит ⇒ систематический оптимизм валидации для платных тарифов (FX вкл. Start/Business). **Stock-фичи** (`engineering.py:198-230` `is_oos/stockout_streak/days_since_restock`) — тот же класс: same-day stock; в MIMO-direct serve берётся последняя train-строка (безопасно), в baseline-режиме — stock тест-даты.

---

## 3.2 Корректность сплитов/валидации — проверено

- Строго временной expanding-window walk-forward (`walk_forward.py:93-149`), per-fold **свежая** модель через factory (`:106`) — изоляция фолдов ✓.
- Direct-multi-step: прогноз из ПОСЛЕДНЕЙ train-строки SKU, тест-фичи в `predict` не подаются, тест нужен лишь для lookup actual по дате (`:177-221`) ✓.

**[H/M-A1 → MEDIUM] HPO selection-bias: заявленная метрика = цель отбора гиперпараметров, не honest-holdout.** *(независимо найдено и мной, и leakage-finder'ом)*
- `train.py:218-221` HPO на полном `df` → `config["model"].update(best_params)` → `train.py:253` walk-forward на ТОМ ЖЕ `df` → `agg` идёт в `result["metrics"]` → `task_queue.py:251-253` в `sku_training_runs` (видит клиент).
- `hpo.py:96-102` каждый trial минимизирует `wmape_global` из `walk_forward_validate(df,...)`.
- Смягчение: HPO тюнит на `trial_horizon=5`, репорт на `horizon=14` — фолды не идентичны, но данные те же (HPO-окна вложены в репорт-окна). Held-out набора, не виденного HPO, НЕТ.
- Затронуты **только Start/Business** (HPO вкл, 15/30 trials); Free (`n_trials=0`) — нет.
- **Impact:** WMAPE/MASE платных тарифов оптимистично смещены; подрывает champion/challenger-гейт (R13-A4).

---

## 3.3 Корректность метрик — ПЕРЕСЧИТАНО НЕЗАВИСИМО

**Формулы математически верны (ПОДТВЕРЖДЕНО ФАКТОМ).** Прогон против ручного эталона (`python3`, system py 3.9):

```
WMAPE  code=0.110000  ref=0.110000  match=True
SMAPE  code=0.121896  ref=0.121896  match=True
MASE   code=1.527778  ref=1.527778  match=True
краевые: WMAPE(all-zero y)→nan; SMAPE(both-zero)→nan; MASE(const train)→nan  ✓
```
- WMAPE = `Σ|y−ŷ|/Σ|y|` (`metrics.py:24-33`) — корректный WAPE.
- SMAPE = `mean(2|y−ŷ|/(|y|+|ŷ|))`, форма [0,2], маскирует нулевой знаменатель (`:36-45`) — корректно.
- MASE = `MAE / mean(|diff(y_train)|)`, несезонный m=1 (`:11-21`) — корректная формула, но см. M-A5.

### Найденные баги расчёта

**[M-A6] MASE concat-дублирование искажает per-SKU MASE (MEDIUM, CONFIRMED ФАКТОМ).** *(нашёл независимо + численно доказал)*
- `walk_forward.py:142-149` merge даёт каждой строке SKU ОДНУ И ТУ ЖЕ train-серию; `metrics.py:64` `np.concatenate(group[train_col].values)` склеивает её K раз → `np.diff` создаёт K−1 ложных «стыковых» скачков → раздувает `naive_mae`.
- Численный прогон (3 копии): `naive_mae` 1.60→1.88, **MASE 0.625→0.531 (−15%, оптимистично)**.
- Затрагивает `mase_mean/median/p90` и сохранённые per-SKU метрики (`train.py:256`). Headline `mase_global` использует `.iloc[0]` (дедуп) — НЕ затронут.

**[M-A5] MASE-знаменатель = lag-1 random walk (m=1), а продукт деплоит weekly-seasonal naive (MEDIUM).** `metrics.py:18` `np.diff` (m=1), при этом fallback = `SeasonalNaiveModel(seasonality=7)` (`fallback.py:152`), а письмо клиенту (`training_email.py:101`) утверждает «MASE<1 значит лучше базового». Для weekly-сезонного спроса lag-1 — слабейший бейзлайн ⇒ MASE систематически оптимистичен; «базовый» в письме ≠ тот naive, что система сама использует как fallback. Модель может иметь MASE<1, но проигрывать seasonal-naive.

**[L-A7] mase_global: числитель по всем фолдам, знаменатель из первого (короткого) фолда (LOW).** `metrics.py:138` `tv_first = group[train_col].iloc[0]` (fold-0, кратчайшая train-серия), а `mae_errors` (`:145`) — по всем тест-строкам всех фолдов. Несогласованные окна в одном отношении.

## 3.4 Бейзлайны и реальная ценность

**[M-A2] Нет автоматического baseline-гейта и нет honest head-to-head с наивной моделью на holdout (MEDIUM).**
- Единственное сравнение с naive — неявное через MASE (а оно само на слабом m=1, см. M-A5).
- `backtest.py:6-9` docstring заявляет «measurement GATE — no model/feature change ships without a non-regression backtest», но `run_backtest`/`run_baseline` вызываются ТОЛЬКО из CLI (`backtest_runner.main`, `scripts/backtest_ab_serve.py`) и тестов — **в проде/CI нет порога**, нет champion/challenger-проверки. Гейт декларативный, де-факто ручной.
- На том же holdout НЕ обучается/сервится seasonal-naive/MA/group-mean для сравнения дельты.
- **Вывод:** на вопрос «бьёт ли модель простой бейзлайн за пределами шума» автоматического ответа нет.

## 3.5 Target & train↔prod consistency

- Таргет НЕ лог-трансформируется (нет `log1p/expm1`) ⇒ прогнозы в исходных единицах, класс «метрика в log, заявлена в raw» исключён ✓. `np.clip(preds,0,None)` гарантирует неотрицательность.
- Serve-путь: MIMO/Ensemble → `direct_forecast` (как обучено, не рекурсивно) — `inference_utils.py:449`. Train↔serve механизм согласован для дефолтного `type:mimo`.
- **[L-A9] Non-default `type:lgbm`:** валидация через `_predict_baseline` меряет 1-step, а serve рекурсивный (ошибка компаундится) ⇒ репорт оптимистичен. Только при явном переключении на lgbm (`walk_forward.py:124-133`).

## 3.6 Переобучение / воспроизводимость / данные

**[L-A10] Недетерминизм обучения (LOW, CONFIRMED).** `forecaster.py:76-98` и `mimo.py:49-70` строят `LGBMRegressor` со стохастикой (`bagging_fraction=0.8, bagging_freq=5, feature_fraction=0.8`) и `n_jobs=-1`, но **без** `random_state`/`seed`/`deterministic`. Сиды зафиксированы лишь в HPO TPESampler и anomaly IF — НЕ в forecasting-пути. ⇒ две тренировки на одних данных дают разные модели/метрики; same-model-A/B-методология (память) зашумлена, конкретная модель невоспроизводима для аудита.

**[M-A8] Anomaly-подсистема инертна (MEDIUM, CONFIRMED).** *(нашёл независимо)* (1) `fit_detect` вызывается шаг 3 (`train.py:188`) ДО `build_features` шаг 4 (`:196`); ветка IsolationForest гейтится на наличие `lag_/rolling_` колонок (`anomaly_detection.py:66`) — их ещё нет ⇒ IF никогда не исполняется, только IQR. (2) Веса НЕ применяются: `train.py:211-212` `_ = sample_weights_full[df.index]` — явный no-op; `fit(X,y,groups=...)` (273/290/295) без `sample_weight`. (3) `is_anomaly` исключён из фич. ⇒ модель учится на сырых выбросах вопреки заявленному down-weighting; шаг 3 жжёт компьют впустую. Docstring `anomaly_detection.py:6` — ложь.

**[L-A11] Gap-fill оставляет price/promo/stock=NaN (LOW).** `loader.py:148-150` дефолты опциональных колонок ДО `_fill_time_gaps` (`:153`), который доливает только target (`:194`) ⇒ imputed gap-дни несут NaN в promo (контракт обещал 0)/price/stock. LightGBM терпит NaN — тихая несогласованность качества.

**[INFO-A12] Интервалы p10/p90 неоткалиброваны.** Чистые LightGBM-quantile без conformal/coverage-проверки (`mimo.py:120-149`); на рекурсивном пути band условен на median предыдущего шага ⇒ занижает неопределённость на длинных горизонтах (`inference_utils.py:296`). R13-A2 (conformal) — будущая работа. Честность интервалов не проверена.

**[INFO-A13] MIMO quantile-модели наследуют лишний `tweedie_variance_power`** при tweedie/huber base (`mimo.py:62,142`) — функционально безвредно (LightGBM игнорит для не-tweedie).

---

## Можно ли доверять заявленной точности — итог

**ЧАСТИЧНО.** Основания (всё проверено руками/прогоном):
1. **ДА** по математике метрик — пересчитал, формулы верны до 6 знаков.
2. **ДА** по отсутствию классических утечек — сплиты временные, фичи past-only, нет preprocessing-leakage.
3. **НЕТ** по абсолютному значению цифры на платных тарифах — она оптимистично смещена минимум четырьмя независимыми механизмами:
   - HPO selection-bias (M-A1): метрика = цель отбора, не honest holdout;
   - слабый m=1 MASE-бейзлайн (M-A5): «лучше базового» вводит в заблуждение;
   - train/serve skew по weather/FX (M-A4): училась на сигнале, которого на инференсе нет;
   - per-SKU MASE искажён concat-дублированием на −15% в примере (M-A6).
4. **НЕ ПРОВЕРЕНО численно на реальных данных:** фактический WMAPE/MASE на `Dataset/1c_*.csv` — обучение требует libomp + версии прода (локаль дрейфует) + ~37 мин; вынесено в BLIND_SPOTS. Сами формулы и пайплайн-логика проверены полностью.
