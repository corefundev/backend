# Честный A/B-стенд точности (`scripts/bench_wf_ab.py`)

Единственный легитимный способ мерить эффект фичи/модели на точность
прогноза. Появился после трёх ретракций подряд (static −5.66%→−0.51%
[#358], FX +12%→вредит, market −7.32%→**+1.46% вредит** [#380]) — все три
числа пришли с невоспроизводимого одно-оконного стенда, скрипты которого
не были закоммичены. Правило: **число, не полученное этим инструментом,
не цитируется в решениях.**

## Харнесс (что фиксировано)

- 3-fold expanding walk-forward (`validation.n_splits` клиента);
- static-фичи fold-clean через AUD-6 `fold_feature_fn` (кроме
  диагностических плеч `statics_*`);
- HPO выключен, anomaly-веса выключены — константы между плечами
  сокращаются в дельтах; абсолютные числа поэтому НЕ равны prod-метрике,
  сравнимы только дельты;
- дефолтный валидатор MIMO/tweedie; плечо `ensemble` — полный ансамбль.

## Плечи

| плечо | модель | статика | market | прочее |
|---|---|---|---|---|
| `base` / `mimo` | MIMO/tweedie | fold-clean | нет | |
| `ensemble` | Ensemble | fold-clean | нет | |
| `market_on` | MIMO/tweedie | fold-clean | full (абсолютные тоталы — снятый default, #380) | |
| `market_share_only` | MIMO/tweedie | fold-clean | только доля SKU (стационарная) | возврат-кандидат |
| `market_momentum` | MIMO/tweedie | fold-clean | доля + momentum (рынок/неделя назад) | возврат-кандидат |
| `payday_off` | MIMO/tweedie | fold-clean | нет | скрывает days_to_payday + is_payday_window |
| `statics_off` | MIMO/tweedie | нет | нет | |
| `statics_leaky` | MIMO/tweedie | full-frame (до-AUD-6) | нет | |
| `recency_hl180` | MIMO/tweedie | fold-clean | нет | recency-decay веса, half-life 180d (#319) |
| `recency_hl90` | MIMO/tweedie | fold-clean | нет | то же, half-life 90d (dose-response) |
| `tweedie_p11` / `p13` / `p17` | MIMO/tweedie | fold-clean | нет | tweedie_variance_power 1.1/1.3/1.7 (#407; default 1.5 = base) |
| `leaves128` | MIMO/tweedie | fold-clean | нет | num_leaves 128 (#407) |
| `lr003_est800` | MIMO/tweedie | fold-clean | нет | lr 0.03 + 800 деревьев (#407) |

Механика `exclude`: колонки остаются в фрейме, но прячутся от модели —
так выключается фича, вшитая в build_features безусловно.

Механика `model_overrides`: dict домердживается в model-конфиг плеча
после type/objective — фиксированные HPO-пробы вместо Optuna (#407).

Механика `recency_half_life`: включает sample-веса `0.5 ** (age/half_life)`
(floor 0.05) через тот же `sample_weight_fn`-hook, что prod-anomaly-веса;
якорь = cutoff трейн-фолда → fold-clean по построению. Пара 180/90 —
dose-response: согласованное направление обеих доз = реальный сигнал.

Вывод — JSON-строки (flush после каждого плеча — прогресс виден в логе
по мере готовности): харнесс **с отпечатком данных** (content-sha12 +
размерности — сверять с реестром датасетов в журнале замеров!), по строке
на плечо (метрики + декомпозиция WMAPE **по velocity-band и по шагу
горизонта**), summary с дельтами против первого плеча (`delta < 0` =
плечо лучше reference).

## Запуск на prod-данных

Только в throwaway-контейнере — никогда в живом worker (его cgroup —
1.5GiB; OOM убьёт RQ-процесс клиентской тренировки, класс #93):

```sh
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru
# 0. Очередь пуста? (правило: не гонять при живых тренировках)
docker exec docker-postgres-1 psql -U sku -d sku_forecasting -tAc \
  "SELECT count(*) FROM sku_training_runs WHERE status IN ('queued','running');"
# 1. Память хоста: available ≥ ~1.5GiB
free -m
# 2. Контейнер с собственным капом, env+mounts воркера
IMG=$(docker inspect docker-worker-1 --format '{{.Config.Image}}')
NET=$(docker inspect docker-worker-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
docker run -d --name bench --rm --memory=1200m --cpu-shares=256 --network "$NET" \
  $(docker inspect docker-worker-1 --format '{{range .Config.Env}}-e {{.}} {{end}}') \
  $(docker inspect docker-worker-1 --format '{{range .Mounts}}-v {{.Source}}:{{.Destination}}:ro {{end}}') \
  -e BENCH_CLIENT_ID=test \
  -e BENCH_DATA_PATH="s3://<processed-zone>/<client>/<upload>/data.parquet" \
  "$IMG" sleep 7200
# 3. Прогон (1c-live: MIMO-плечо ≈ 7 мин, ensemble ≈ 21 мин)
docker exec bench python scripts/bench_wf_ab.py --arms base,ensemble > /tmp/bench.json
# 4. Уборка
docker rm -f bench
```

Ориентир длительности: 60 SKU × 1081 день, MIMO — ~215 c/плечо.

## Дисциплина интерпретации

- Дельта в пределах ±0.5% WMAPE на этом датасете — шум; не принимать
  решение по одному прогону такой величины.
- Замер под решением о ПЛАТНОМ тарифе гонять и под `ensemble`-плечом —
  MIMO-дельта не переносится автоматически (R14: платная метрика была
  оптимистично смещена).
- Отрицательный результат публикуется так же, как положительный
  (прецеденты: weather ≈0, cold-start KILL, market → #380).
