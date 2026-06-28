# Roadmap — R14 remediation + R13 improvement integration

> Составлено 2026-06-28 на основе `audit/REPORT.md` + `model_accuracy.md`. Увязывает новые R14-issues (#180–187)
> с открытым R13-roadmap (#150–158) и инфра/блокированными пунктами.
>
> **Центральный принцип (из аудита):** сначала сделать ОЦЕНКУ честной, потом улучшать КАЧЕСТВО.
> Улучшать прогноз, меряя его смещённой метрикой (HPO selection-bias + слабый m=1 baseline), —
> значит оптимизировать против кривой линейки. Поэтому Wave 1 (честность метрики) предшествует Wave 2 (качество).
>
> **Дисциплина (для каждого пункта):** один PR на item → CI green → deploy staging+prod → prod-verify →
> memory update → close. NFOR-линтер перед коммитом. Forecast-изменения — через same-model A/B
> ([[feedback_same_model_ab]]): заморозить одну модель, сервить двумя путями. Никаких monthly-limits.

## Карта зависимостей (почему такой порядок)

```
#182 (MASE concat fix)  ─┐
#181 (seasonal m=7 base) ─┤→  #150 (backtest gate +     ─→ #152 (clean-holdout blend)
#180 (HPO honest holdout)─┘     champion/challenger)        #151 (conformal intervals)
                                  ↑ меряет на честном        #154 (intermittent routing)
ENABLER: Linux-CI run on        числе и блокирует            #158 (direct-vs-recursive)
Dataset/1c_*.csv (BLIND_SPOTS §1)  регресс                   └ все через same-model A/B + gate

независимо (не ждут метрику):  #185, #184, #183 (correctness/availability/cleanup)
```

---

## Wave 0 — Correctness & availability (параллельно, не ждёт метрику) · ~1–1.5 нед
Высокая уверенность, не зависят от доверия к метрике. Закрывают реальный degraded-mode риск.

| # | Item | Размер | Действие |
|---|---|---|---|
| **#185** | BUG-1 sync-fallback обучения | M | Роутить sync-fallback через тот же оркестратор, что worker (`_training_job` с `extend_from_paths` + forecast-gen), ИЛИ честно отдавать 503; offload через `anyio.to_thread`; gate за env-флагом как `ALLOW_SYNC_UPLOAD_FALLBACK`. |
| **#184** | ASYNC-1 блокировка event-loop | M | Горячие хендлеры → `def` (threadpool Starlette) или `anyio.to_thread.run_sync` вокруг sync DB/S3/bcrypt/ML; проверить, что `/healthz`/`/readyz` живы под тяжёлым `/predict`. |
| **#183** | M-A8 anomaly inert | S→M | РЕШЕНИЕ: (a) дотянуть `sample_weight` в fit + перенести anomaly ПОСЛЕ `build_features` (fold-aware, чтобы не внести L-A3 leakage), ИЛИ (b) удалить мёртвый шаг + поправить лживый docstring. Дёшево и снимает «потёмкинскую» защиту. |

## Wave 0.5 — ENABLER: измеримость (разблокирует Wave 1–2) · ~2–3 дня
Без фактического прогона на реальных данных все quality-числа — гипотезы (BLIND_SPOTS §1).
- Поднять прогон backtest на `Dataset/1c_*.csv` в Linux-CI с **pinned-deps** (локаль дрейфует: fastapi/pydantic/sklearn).
- Зафиксировать baseline-числа (honest holdout + seasonal-naive-7) — это вход для Wave 1.

## Wave 1 — Честность метрики (фундамент) · ~1.5–2 нед
После этого числу в `sku_training_runs` можно доверять; champion/challenger становится осмысленным.

| # | Item | Размер | Зависит | Действие |
|---|---|---|---|---|
| **#182** | M-A6 MASE concat −15% | **S** | — | `compute_metrics_per_sku` → `iloc[0]` вместо `np.concatenate`; regression-тест (K строк == K=1). Тривиально, делать первым. |
| **#181** | M-A5 seasonal m=7 baseline | S→M | 0.5 | MASE-знаменатель = seasonal-naive(7) (или репортить m=1 И m=7); выровнять формулировку «базового» в письме. |
| **#180** | M-A1 HPO selection-bias | **L** | 0.5 | Вынести honest-holdout, который HPO не видит (nested split: HPO тюнит на внутренних фолдах строго раньше репорт-блока), ИЛИ помечать число «tuned». Самый важный по влиянию на доверие. |
| **#150** | A4 automated backtest gate | L | #180,#181 | MLflow champion/challenger, fail-closed на регресс WMAPE/MASE + head-to-head с seasonal-naive(7) на том же holdout + alert. Меряет уже на честном числе. |

## Wave 2 — Forecast-quality (на честной метрике + под gate) · ~3–4 нед
Каждый — через same-model A/B + новый gate (#150). Порядок по «дёшево→ценно».

| # | Item | Размер | Действие |
|---|---|---|---|
| **#152** | A1 clean-holdout blend weights | M | Веса блендинга на holdout, который дети не видели (~33% доп. train); A/B vs baseline #76. Самый дешёвый чистый прирост точности. |
| **#151** | A2 conformal intervals (CQR) | L | In-house numpy CQR вокруг существующих quantile; temporal-калибровка; coverage stratified by horizon×SKU-band + pinball. Бизнес-ценность (safety-stock). [[feedback_no_third_party_runtimes]]. |
| **#154** | A3 intermittent-demand routing | L | CV²/ADI-классификатор → Croston / zero-infl / log1p; A/B на intermittent-SKU. |
| **#158** | A5 direct-vs-recursive default | S | Business-tier A/B на ensemble far-heads; решить tier-gate vs revert; заодно L-A9 (lgbm 1-step метрика vs recursive serve). |

## Wave 3 — Performance / availability hardening · ~2 нед
Из umbrella #186, MEDIUM-перф.
- **PERF-1**: `/predict` тянет весь parquet ради 1 SKU → `columns=`/`filters=` pushdown или кэш.
- **PERF-2**: `recursive_forecast` quadratic `pd.concat` → буфер list/numpy, materialize один раз.
- **predict_batch**: реальная конкурентность (threadpool) ИЛИ убрать ложный «N× speedup» из docstring; `return_exceptions=True` + per-item envelope; rate-limit 1-per-batch.
- **telegram_webhook**: offload `handle_update` в thread / enqueue в rq.
- PERF-3 (holiday per-row apply → vectorize), PERF-4 (psycopg2 pool + снизить `FORECASTS_LIST_HARD_CAP`).

## Wave 4 — Architecture / hygiene / tests · ~2–3 нед
Из umbrella #186.
- **TEST-1/2** (приоритет — защищают Wave 1): переписать `test_no_recursive_leakage` на реальный probe; добавить no-overlap/date-mapping тест на `walk_forward_validate`. + TEST-3/4/5 (SMAPE known-value, determinism, train/serve value-parity).
- **L-A10** determinism: зафиксировать `random_state`/`deterministic` на LGBM (увязать с TEST-4).
- **ARCH-1/2**: расщепить god-модули `task_queue.py` (878) / `auth.py` (1230) — оценить value-per-unit ([[feedback_rightsize_refactor_campaigns]]), не правка-ради-правки.
- **Dead-code** DEAD-1/2/3 (RateLimiter/`_LazyStr`/`_batch_inference_job`) — удалить с осиротевшими импортами ([[feedback_no_dead_code]]).
- Мелкие correctness: **SEC-1** (register_client → `validate_client_config`), **BUG-2** (lat/lon 422), **CONTRACT-1/2**, L-A4/7/11 — фолдить в смежные PR.

## Wave 5 — Business / compliance · по бизнес-приоритету
- **#155** B3 quota transparency — осталась только UI-часть + machine-readable reason-codes (backend готов, `quota.py:165-180`).
- **#156** B4 RU PII 152-ФЗ — export/soft-delete/retention (зависит от закрытого #144).
- **#145** B2 billing gate — ⛔ BLOCKED до payment-acquiring (by-design free upgrade). НЕ трогать до эквайринга.
- R13 LOW umbrella **#148** — 3 frontend-пункта (CSP, esbuild) в `corefundev/frontend`.

---

## Резюме приоритетов (если коротко)
1. **Сейчас:** #182 (тривиально) → #185, #184, #183 (correctness/availability, параллельно).
2. **Фундамент:** Wave 0.5 enabler → #181 → #180 → #150. После этого числу можно верить.
3. **Затем:** #152 → #151 → #154/#158 (качество, под gate).
4. **Фоном:** Wave 3–4 (perf/arch/tests), Wave 5 по бизнес-готовности.

**Один принцип, который нельзя нарушать:** ни один forecast-quality PR (Wave 2) не мержится без same-model A/B
И зелёного backtest-gate (#150). Иначе «улучшение» может быть шумом обучения (нет seed, L-A10) или артефактом
смещённой метрики.
