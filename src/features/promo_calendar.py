"""
src/features/promo_calendar.py — #570 PC-2: known-future промо-фичи из
«Календаря акций» (вариант B, решение владельца).

Контракт no-skew — тот же, что у compute_holiday_features (#58): ЕДИНАЯ
чистая функция для train и serve; будущие даты прогноза кодируются ровно
так же, как исторические строки обучения. Календарь — known-future по
природе (план акций известен заранее), это не утечка.

Fail-open (#570): нет календаря / нет событий → нулевые фичи, поведение
модели как раньше. v1 — бинарный эффект: depth в фичи НЕ входит.

Матчинг события к строке: событие с sku — только этому SKU; событие с
category — всем SKU этой категории (колонка category в данных продаж,
если её нет — категорийные события не применяются). Пересечения =
«есть хоть одна акция» (решение владельца).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PROMO_CAL_FEATURE_COLS = (
    "promo_cal_active",          # сегодня идёт акция (0/1)
    "promo_cal_days_next_7",     # дней с акцией в (t, t+7]
    "promo_cal_days_next_14",    # … в (t, t+14]
    "promo_cal_days_next_28",    # … в (t, t+28]
    # promo_cal_days_to_start: дней до начала ближайшей акции; 0 если уже
    # идёт; cap 60 = «в обозримом будущем акций нет»
    "promo_cal_days_to_start",
)

_HORIZONS = (7, 14, 28)
_CAP_DAYS = 60
_MAX_HORIZON = max(_HORIZONS)


def _coverage_bool(index: pd.DatetimeIndex, spans: "list[tuple]") -> np.ndarray:
    """Булев массив по дням index: день покрыт хотя бы одним span'ом."""
    cov = np.zeros(len(index), dtype=bool)
    start, end = index[0], index[-1]
    for d_from, d_to in spans:
        if d_to < start or d_from > end:
            continue
        i = max(0, (d_from - start).days)
        j = min(len(index) - 1, (d_to - start).days)
        cov[i:j + 1] = True
    return cov


def compute_promo_calendar_features(
    dates,
    sku_values,
    category_values,
    events: "pd.DataFrame | None",
) -> pd.DataFrame:
    """Фичи календаря для произвольных строк (date, sku, category).

    `events`: DataFrame c колонками sku, category, date_from, date_to
    (события активного календаря; sku XOR category заполнено). None/пусто →
    нулевые фичи (fail-open). Возвращает DataFrame с PROMO_CAL_FEATURE_COLS
    (0..N-1 index) — единый источник для train И serve."""
    dt = pd.to_datetime(pd.Series(list(dates))).dt.normalize().reset_index(drop=True)
    n = len(dt)
    out = pd.DataFrame(index=range(n))
    out["promo_cal_active"] = 0
    for h in _HORIZONS:
        out[f"promo_cal_days_next_{h}"] = 0
    out["promo_cal_days_to_start"] = _CAP_DAYS
    if events is None or len(events) == 0 or n == 0:
        return out

    ev = events.copy()
    ev["date_from"] = pd.to_datetime(ev["date_from"]).dt.normalize()
    ev["date_to"] = pd.to_datetime(ev["date_to"]).dt.normalize()

    sku_s = pd.Series(list(sku_values)).astype(str).reset_index(drop=True)
    cat_raw = pd.Series(list(category_values)).reset_index(drop=True)
    cat_s = cat_raw.astype(str).where(cat_raw.notna(), other="")

    # спаны по сущностям
    sku_spans: "dict[str, list]" = {}
    cat_spans: "dict[str, list]" = {}
    for r in ev.to_dict("records"):
        span = (r["date_from"], r["date_to"])
        if r.get("sku") is not None and not pd.isna(r.get("sku")):
            sku_spans.setdefault(str(r["sku"]), []).append(span)
        elif r.get("category") is not None and not pd.isna(r.get("category")):
            cat_spans.setdefault(str(r["category"]), []).append(span)

    # общий дневной диапазон с хвостом на максимум горизонта
    day_index = pd.date_range(dt.min(), dt.max() + pd.Timedelta(days=_MAX_HORIZON),
                              freq="D")

    # покрытие на (sku, category)-пару = OR sku-спанов и category-спанов;
    # пар не больше числа SKU — массивы дешёвые
    for (s, c), grp in pd.DataFrame({"s": sku_s, "c": cat_s}).groupby(
            ["s", "c"], sort=False):
        spans = sku_spans.get(s, []) + cat_spans.get(c, [])
        if not spans:
            continue
        cov = _coverage_bool(day_index, spans)
        csum = np.concatenate([[0], np.cumsum(cov)])
        # ближайший старт: день i, где cov[i] и не cov[i-1]
        starts = cov & ~np.concatenate([[False], cov[:-1]])
        start_positions = np.flatnonzero(starts)

        idx = grp.index
        pos = (dt.iloc[idx] - day_index[0]).dt.days.to_numpy()
        out.loc[idx, "promo_cal_active"] = cov[pos].astype(int)
        for h in _HORIZONS:
            end = np.minimum(pos + h, len(cov) - 1)
            out.loc[idx, f"promo_cal_days_next_{h}"] = (
                csum[end + 1] - csum[pos + 1]).astype(int)
        # days_to_start: 0 если идёт; иначе ближайший будущий старт; cap
        d2s = np.full(len(pos), _CAP_DAYS, dtype=int)
        if len(start_positions):
            nxt = np.searchsorted(start_positions, pos, side="right")
            has_next = nxt < len(start_positions)
            gap = np.where(has_next,
                           start_positions[np.minimum(nxt, len(start_positions) - 1)] - pos,
                           _CAP_DAYS)
            d2s = np.minimum(gap, _CAP_DAYS)
        d2s = np.where(cov[pos], 0, d2s)
        out.loc[idx, "promo_cal_days_to_start"] = d2s

    return out


def build_promo_calendar_features(
    df: pd.DataFrame,
    config: dict,
    date_col: str = "date",
    events: "pd.DataFrame | None" = None,
) -> pd.DataFrame:
    """Тренировочная обёртка (образец build_holiday_features): считает фичи
    на строках df через ту же compute_promo_calendar_features."""
    sku_col = config.get("data", {}).get("sku_col", "sku")
    cat = df["category"] if "category" in df.columns else pd.Series(
        [None] * len(df), index=df.index)
    feats = compute_promo_calendar_features(
        df[date_col], df[sku_col], cat, events)
    feats.index = df.index
    for col in PROMO_CAL_FEATURE_COLS:
        df[col] = feats[col]
    return df


def load_active_events(dataset_id: "str | None") -> "pd.DataFrame | None":
    """События активного календаря датасета как DataFrame — или None.

    Fail-open по КОНТРАКТУ #570: нет dataset_id / нет активного календаря /
    реестр недоступен → None (нулевые фичи), но сбой реестра логируется
    ГРОМКО — молчаливых деградаций не бывает."""
    if not dataset_id:
        return None
    try:
        from src.storage.promo_calendar import get_promo_calendar_registry
        reg = get_promo_calendar_registry()
        active = reg.get_active(dataset_id)
        if active is None:
            return None
        events = reg.list_events(active.calendar_id)
        if not events:
            return None
        return pd.DataFrame([{
            "sku": e.sku, "category": e.category,
            "date_from": e.date_from, "date_to": e.date_to,
        } for e in events])
    except Exception as e:    # noqa: BLE001 — fail-open по контракту, но громко
        logger.error("promo calendar unavailable for dataset %s: %s "
                     "(forecast continues with zero promo features)",
                     dataset_id, e)
        return None
