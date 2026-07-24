"""
src/explain/engine.py

XP-1 (#469, эпик #463): объяснимость-лайт — «почему такой прогноз».

Механика: точный TreeSHAP, встроенный в LightGBM (`pred_contrib=True`) —
вклад каждой фичи в КОНКРЕТНЫЙ прогноз конкретного SKU, не «важности
вообще». Вклады суммируются по direct-головам горизонта (1..H модели;
рекурсивный хвост честно НЕ объясняем — его вклады были бы условными),
затем ~54 сырых фичи сворачиваются в человеческие группы («недавний
спрос», «цена», …) — клиент видит топ-3 группы со стрелками, не lag_224.

Честность единиц: для objective=tweedie/poisson вклады живут в raw-score
пространстве (лог-линк) — их СУММА равна log(прогноза), а не прогнозу.
Направление и относительные доли валидны (линк монотонный); абсолютные
величины наружу не показываем — только доли |вклада|. Для диффа «прогноз
вырос на X%» рядом сохраняется настоящая сумма predict() в штуках.

Ансамбль (платный дефолт): вклады считаем по ведущему ребёнку (tweedie —
у него же живут квантильные головы); блендинг весов игнорируем — это
приближение, дети сильно коррелируют, направление групп совпадает.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FALLBACK_GROUP = "Другое"

# Порядок важен: первый матч побеждает (prefix/suffix-правила).
# Инвентарь имён — src/features/*.py (engineering, holidays, market, static).
_GROUP_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Недавний спрос", ("lag_", "rolling_", "sku_share_")),
    ("Цена", ("price",)),
    ("Промо", ("promo",)),
    ("Наличие на складе",
     ("stock", "is_oos", "oos_", "days_since_restock", "stockout_")),
    ("Праздники и события",
     ("is_holiday", "days_to_holiday", "days_since_holiday",
      "is_pre_holiday", "is_post_holiday", "holidays", "event_ramp")),
    ("Сезонность и календарь",
     ("dayof", "month", "quarter", "year", "weekofyear", "is_weekend",
      "is_month_", "days_to_payday", "is_payday")),
    ("Погода", ("temp_", "rain_", "precipitation", "wind_",
                "is_cold_day", "is_hot_day", "is_rainy_day")),
    ("Профиль товара", ("category_te", "static_", "sku_te")),
    ("Рынок", ("market_", "currency", "fx_",
               # FE currency selection #94: пары <ccy>_rub_*
               "usd_rub", "eur_rub", "cny_rub", "byn_rub", "kzt_rub")),
    # velocity_band — внутренняя сегментация SKU по скорости продаж
    ("Профиль товара", ("velocity_band",)),
]
_RAMP_SUFFIX = "_ramp"    # ny_ramp, feb23_ramp, mar8_ramp, may1_ramp …


def group_of(feature: str) -> str:
    """Человеческая группа для сырого имени фичи; неизвестное → 'Другое'."""
    f = feature.lower()
    if f.endswith(_RAMP_SUFFIX):
        return "Праздники и события"
    for label, prefixes in _GROUP_RULES:
        for p in prefixes:
            if f.startswith(p):
                return label
    return FALLBACK_GROUP


def _primary_mimo(model: Any) -> Any:
    """MIMOForecaster из чего угодно: ансамбль → ведущий ребёнок."""
    if hasattr(model, "primary_objective") and hasattr(model, "models_"):
        return model.models_[model.primary_objective]
    return model


def explain_anchor(model: Any, anchor: pd.DataFrame,
                   horizon: int | None = None) -> dict:
    """Вклады групп в прогноз одного SKU (одна анкер-строка).

    Возвращает:
      groups     — {группа: суммарный вклад по головам, raw-score units}
      base       — суммарный base value голов (raw-score units)
      prediction — НАСТОЯЩАЯ сумма predict() за те же головы (штуки)
      heads      — сколько direct-голов вошло
    """
    mimo = _primary_mimo(model)
    feats = list(mimo.feature_cols)
    # Гард serve-path (inference_utils M4): best-effort фичи (FX при
    # упавшем фетче ЦБ, погода) могут отсутствовать во фрейме — модель
    # ждёт колонку. 0.0 = «нет сигнала», как в recursive_forecast;
    # без гарда KeyError ронял объяснения ВСЕХ SKU (0 rows, 2026-07-23).
    X = anchor.copy()
    for c in feats:
        if c not in X.columns:
            X[c] = 0.0
    X = X[feats]
    n_heads = len(mimo.models_)
    if horizon is not None:
        n_heads = max(1, min(n_heads, int(horizon)))

    total = np.zeros(len(feats), dtype=float)
    base = 0.0
    for head in mimo.models_[:n_heads]:
        contrib = np.asarray(head.predict(X, pred_contrib=True),
                             dtype=float)[0]
        total += contrib[:-1]
        base += float(contrib[-1])

    pred = np.asarray(mimo.predict(X), dtype=float)[0]
    prediction = float(np.clip(pred[:n_heads], 0, None).sum())

    groups: dict[str, float] = {}
    for name, c in zip(feats, total):
        g = group_of(name)
        groups[g] = groups.get(g, 0.0) + float(c)
    return {"groups": groups, "base": base,
            "prediction": prediction, "heads": n_heads}


def build_explanations(model: Any, df: pd.DataFrame, config: dict,
                       horizon: int | None = None,
                       max_skus: int | None = None) -> pd.DataFrame:
    """Объяснения для всех SKU фрейма (анкер = последняя строка фичей SKU,
    тот же выбор, что у forecast_all_skus/walk_forward).

    Long-формат: sku | group | contribution | prediction_sum | base | heads.
    Ошибка на отдельном SKU не роняет остальных (пропуск с warning'ом).
    """
    sku_col = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    rows: list[dict] = []
    processed = skipped = 0
    for sku, group in df.groupby(sku_col, sort=False):
        if max_skus is not None and processed >= max_skus:
            break
        anchor = group.sort_values(date_col).iloc[[-1]]
        try:
            ex = explain_anchor(model, anchor, horizon=horizon)
        except Exception as e:    # noqa: BLE001 — один SKU ≠ весь батч
            skipped += 1
            logger.warning("explain: SKU %s skipped: %s", sku, e)
            continue
        processed += 1
        for g, c in ex["groups"].items():
            rows.append({
                "sku": str(sku), "group": g, "contribution": c,
                "prediction_sum": ex["prediction"], "base": ex["base"],
                "heads": ex["heads"],
            })
    if skipped:
        logger.warning("explain: skipped %d SKUs (of %d attempted)",
                       skipped, processed + skipped)
    return pd.DataFrame(
        rows, columns=["sku", "group", "contribution",
                       "prediction_sum", "base", "heads"])
