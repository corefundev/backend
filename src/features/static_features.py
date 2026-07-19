"""
src/features/static_features.py

QW3-1 (#307): cross-series STATIC positioning features as MODEL-CARRIED state.

Идея — та же, что у market (#229, src/features/market.py): фичи, которые
описывают положение SKU ОТНОСИТЕЛЬНО всего каталога, невычислимы из его
одиночного среза (serve-parity, TEST-5 #186). Поэтому они считаются ОДИН раз
на полном train-фрейме, per-SKU карта вшивается в артефакт модели, а на serve
значения МЕРЖАТСЯ из карты по идентичности SKU — совпадают по построению.

Две фичи (velocity_band + price_tier — измерены как 83% эффекта статики,
−5.66% WMAPE на реальном 1c; slow-band 1.807→1.165, −35.5%):
  • velocity_band — тершиль SKU по средним продажам (0=медленный / 1 / 2=быстрый).
    Даёт модели «якорь» уровня для голодных на данные медленных SKU.
  • price_tier    — тершиль SKU по средней цене (нужна колонка price; иначе
    вырождается в константу 1 — no-op).

Лика нет: карта строится на train-срезе (leakage-clean), тершильные границы
фиксируются на обучении; неизвестный на serve SKU (появился после обучения)
получает fallback = средний бэнд 1 — задокументированное приближение.

Категориальный target-encoding: #316 замерил −1.19% и заморозил инжест
до порога ≥1%; на честной вселенной с памятью ряда эффект вырос до
−4.6%/−4.08% WMAPE (Э2 + подтверждение, 2026-07-19, #545) — category_te
теперь третья static-фича. Байесовское сглаживание к среднему трейна
(m из features.category_te.smoothing, замерено m=20; m=100 НЕ лучше),
unknown SKU/категория → глобальное среднее трейна (контракт бенча).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STATIC_COLS = ("velocity_band", "price_tier")
_CTE_COL = "category_te"
_CTE_FALLBACK_ATTR = "category_te_fallback"    # g_mean трейна в map.attrs


def _map_cols(static_map: pd.DataFrame) -> list:
    """Статик-колонки, реально присутствующие в карте (category_te —
    опциональна: датасет без категории её не несёт)."""
    return [c for c in (*STATIC_COLS, _CTE_COL) if c in static_map.columns]
_FALLBACK_BAND = 1.0          # средний тершиль — для неизвестных на serve SKU
_PRICE_COL = "price"          # опциональная колонка (data.optional_cols)


def _terciles(series: pd.Series) -> np.ndarray:
    """Границы 1/3 и 2/3 по непустым значениям (для np.digitize)."""
    vals = series.dropna().to_numpy()
    if vals.size == 0:
        return np.array([np.inf, np.inf])     # всё уйдёт в бэнд 0 — безвредно
    return np.quantile(vals, [1 / 3, 2 / 3])


def compute_static_map(df: pd.DataFrame, sku_col: str,
                       target_col: str, *,
                       category_col: str | None = None,
                       te_smoothing: float = 20.0) -> pd.DataFrame:
    """Per-SKU карта {sku → velocity_band, price_tier[, category_te]} по
    ПОЛНОМУ train-фрейму.

    velocity_band — тершиль по средним продажам; price_tier — тершиль по
    средней цене (если есть колонка price, иначе константа 1); category_te
    (#545) — сглаженное среднее target по категории SKU, если категория
    сконфигурирована И колонка есть в фрейме (иначе фича отсутствует —
    graceful no-op, НЕ нули). Фолбэк для unknown — g_mean в map.attrs."""
    vel = df.groupby(sku_col)[target_col].mean()
    vq = _terciles(vel)
    out = pd.DataFrame({sku_col: vel.index})
    out["velocity_band"] = np.digitize(vel.to_numpy(), vq).astype(float)

    if _PRICE_COL in df.columns:
        pr = df.groupby(sku_col)[_PRICE_COL].mean()
        pq = _terciles(pr)
        tier = pd.Series(np.digitize(pr.to_numpy(), pq).astype(float), index=pr.index)
        # NaN-цена у SKU → digitize даёт крайний индекс; принудительно средний бэнд
        tier = tier.where(pr.notna(), _FALLBACK_BAND)
        out["price_tier"] = out[sku_col].map(tier).fillna(_FALLBACK_BAND)
    else:
        out["price_tier"] = _FALLBACK_BAND

    if category_col and category_col in df.columns:
        cat = df.groupby(sku_col)[category_col].agg(
            lambda s: s.dropna().iloc[-1] if s.notna().any() else None)
        g_mean = float(df[target_col].mean())
        per_cat = df.assign(_cat=df[sku_col].map(cat)).groupby("_cat")[target_col]
        stats = per_cat.agg(["sum", "count"])
        m = float(te_smoothing)
        te_map = (stats["sum"] + m * g_mean) / (stats["count"] + m)
        out[_CTE_COL] = out[sku_col].map(cat).map(te_map).fillna(g_mean)
        out.attrs[_CTE_FALLBACK_ATTR] = g_mean
    return out.reset_index(drop=True)


def merge_static_features(df: pd.DataFrame, static_map: pd.DataFrame,
                          sku_col: str) -> pd.DataFrame:
    """Добавляет STATIC_COLS к ЛЮБОМУ фрейму (полному или срезу одного SKU)
    по join'у карты на sku_col. Неизвестный SKU → средний бэнд (_FALLBACK_BAND).
    Идемпотентно: повторный merge — no-op."""
    if static_map is None or static_map.empty:
        return df
    cols = _map_cols(static_map)
    if all(c in df.columns for c in cols):
        return df
    m = static_map[[sku_col, *cols]]
    fallback_te = static_map.attrs.get(
        _CTE_FALLBACK_ATTR,
        float(static_map[_CTE_COL].median()) if _CTE_COL in static_map.columns
        else _FALLBACK_BAND)
    df = df.merge(m, on=sku_col, how="left")
    for c in cols:
        fb = fallback_te if c == _CTE_COL else _FALLBACK_BAND
        df[c] = df[c].fillna(fb).astype(float)
    return df


def attach_static_to_model(model, static_map: pd.DataFrame) -> None:
    """Карта → атрибут модели (persist — забота save() конкретного класса:
    Ensemble пиклится целиком; MIMO/SKUForecaster кладут её в save-dict)."""
    model.static_map = static_map


def apply_model_static(model, df: pd.DataFrame, sku_col: str) -> pd.DataFrame:
    """Serve-хелпер: no-op для моделей без static_map (старые pickle —
    их feature_cols этих колонок не требуют)."""
    smap = getattr(model, "static_map", None)
    if smap is None:
        return df
    return merge_static_features(df, smap, sku_col)


def fold_static_recompute(train_df: pd.DataFrame, test_df: pd.DataFrame,
                          sku_col: str, target_col: str, *,
                          category_col: str | None = None,
                          te_smoothing: float = 20.0,
                          ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """AUD-6 (#358): честный walk-forward для static-фич.

    Полнофреймовая карта построена и на report-tail днях, на которых
    walk-forward затем меряет метрику: velocity_band SKU частично кодирует
    уровень продаж тестового окна → систематически оптимистичная метрика,
    promotion gate (#227), HPO-slice (#180) и clean-blend (#152). Внутри
    фолда карта строится ЗАНОВО и только на train-строках этого фолда;
    test-строки получают train-карту — ровно как serve получает карту,
    вшитую в артефакт на обучении (SKU без train-истории → _FALLBACK_BAND,
    тот же контракт). Финальный fit не меняется: там полный df = полный train.

    Перезаписывает STATIC_COLS в обоих срезах (merge_static_features
    идемпотентно пропускает присутствующие колонки — поэтому сначала drop)."""
    smap = compute_static_map(train_df, sku_col, target_col,
                              category_col=category_col,
                              te_smoothing=te_smoothing)
    drop = list(STATIC_COLS) + [_CTE_COL]
    train_df = merge_static_features(
        train_df.drop(columns=drop, errors="ignore"), smap, sku_col)
    test_df = merge_static_features(
        test_df.drop(columns=drop, errors="ignore"), smap, sku_col)
    return train_df, test_df
