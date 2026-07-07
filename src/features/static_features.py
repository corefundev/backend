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

Категориальный target-encoding (ещё −1.19%) вынесен в #316: он требует
ingestion-пути под колонку категории, которой в общем пайплайне нет.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

STATIC_COLS = ("velocity_band", "price_tier")
_FALLBACK_BAND = 1.0          # средний тершиль — для неизвестных на serve SKU
_PRICE_COL = "price"          # опциональная колонка (data.optional_cols)


def _terciles(series: pd.Series) -> np.ndarray:
    """Границы 1/3 и 2/3 по непустым значениям (для np.digitize)."""
    vals = series.dropna().to_numpy()
    if vals.size == 0:
        return np.array([np.inf, np.inf])     # всё уйдёт в бэнд 0 — безвредно
    return np.quantile(vals, [1 / 3, 2 / 3])


def compute_static_map(df: pd.DataFrame, sku_col: str,
                       target_col: str) -> pd.DataFrame:
    """Per-SKU карта {sku → velocity_band, price_tier} по ПОЛНОМУ train-фрейму.

    velocity_band — тершиль по средним продажам; price_tier — тершиль по
    средней цене (если есть колонка price, иначе константа 1)."""
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
    return out.reset_index(drop=True)


def merge_static_features(df: pd.DataFrame, static_map: pd.DataFrame,
                          sku_col: str) -> pd.DataFrame:
    """Добавляет STATIC_COLS к ЛЮБОМУ фрейму (полному или срезу одного SKU)
    по join'у карты на sku_col. Неизвестный SKU → средний бэнд (_FALLBACK_BAND).
    Идемпотентно: повторный merge — no-op."""
    if static_map is None or static_map.empty:
        return df
    if all(c in df.columns for c in STATIC_COLS):
        return df
    m = static_map[[sku_col, *STATIC_COLS]]
    df = df.merge(m, on=sku_col, how="left")
    for c in STATIC_COLS:
        df[c] = df[c].fillna(_FALLBACK_BAND).astype(float)
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
