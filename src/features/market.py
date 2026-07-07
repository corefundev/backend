"""
src/features/market.py

#229 (QW2-B2): cross-series (market) features as MODEL-CARRIED state.

Почему не в build_features: контракт serve-parity (TEST-5 #186) требует,
чтобы фичи SKU были воспроизводимы из его ОДИНОЧНОГО среза — так работает
одиночный /predict-путь. «Рынок» из среза невычислим по определению
(вырождается в продажи самого SKU). Поэтому рыночный ряд считается ОДИН
раз на полном фрейме при обучении, его хвост вшивается в артефакт модели
(по образцу conformal-таблиц), а на serve колонки МЕРЖАТСЯ из хвоста на
любом фрейме — значения совпадают по построению.

Лика нет: в фичи идут только лагированные суммы (total дня t включает
продажи самого SKU → shift обязателен); все нужные serve-даты — прошлое
(якорь−1, якорь−7). Для дат за пределами хвоста (recursive-путь) —
carry-forward последнего известного тотала, документированное приближение
недефолтного пути.

Измерено (сидированный A/B, честный 14д-holdout, реальный 1С):
WMAPE 0.6087 → 0.5642 (−7.32%) — крупнейший одиночный рычаг кампании.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MARKET_COLS = ("market_total_lag_1", "market_total_lag_7", "sku_share_lag_1")
TAIL_DAYS = 60          # покрывает lag_7 + запас на нерегулярные даты


def compute_market_tail(df: pd.DataFrame, date_col: str,
                        target_col: str) -> pd.DataFrame:
    """Дневные суммы каталога, последние TAIL_DAYS дней + ВЕСЬ ряд для
    train-мержа. Возвращает frame[date, market_total] по всем датам —
    сам хвост для персиста режет вызывающий (train-мерж требует полного
    ряда, артефакту достаточно хвоста)."""
    m = (df.groupby(date_col)[target_col].sum()
           .rename("market_total").reset_index()
           .sort_values(date_col).reset_index(drop=True))
    return m


def tail_for_artifact(market: pd.DataFrame) -> pd.DataFrame:
    return market.tail(TAIL_DAYS).reset_index(drop=True)


def merge_market_features(df: pd.DataFrame, market: pd.DataFrame,
                          date_col: str) -> pd.DataFrame:
    """Добавляет MARKET_COLS к ЛЮБОМУ фрейму (полному или срезу одного
    SKU) — по date-джойну лагированного ряда. Даты за границей ряда
    (будущие шаги recursive-пути) получают carry-forward последнего
    известного тотала. Доля — через per-SKU `lag_1` (имя без префикса,
    см. _build_lag_features), если он есть в фрейме."""
    if market is None or market.empty:
        return df
    if all(c in df.columns for c in MARKET_COLS):
        return df          # идемпотентность: повторный merge — no-op
    m = market.sort_values(date_col).reset_index(drop=True)
    lag1 = m.copy()
    lag1[date_col] = lag1[date_col] + pd.Timedelta(days=1)
    lag7 = m.copy()
    lag7[date_col] = lag7[date_col] + pd.Timedelta(days=7)
    df = df.merge(lag1.rename(columns={"market_total": "market_total_lag_1"}),
                  on=date_col, how="left")
    df = df.merge(lag7.rename(columns={"market_total": "market_total_lag_7"}),
                  on=date_col, how="left")
    # carry-forward за границей ряда (только ВПЕРЁД — прошлое до начала
    # ряда остаётся NaN, как у обычных лагов на warmup-е)
    last_total = float(m["market_total"].iloc[-1])
    last_date = m[date_col].max()
    fwd = df[date_col] > last_date
    df.loc[fwd & df["market_total_lag_1"].isna(), "market_total_lag_1"] = last_total
    df.loc[fwd & df["market_total_lag_7"].isna(), "market_total_lag_7"] = last_total
    if "lag_1" in df.columns:
        df["sku_share_lag_1"] = (
            df["lag_1"] / df["market_total_lag_1"].replace(0, np.nan)
        ).fillna(0.0)
    return df


def attach_market_to_model(model, market: pd.DataFrame) -> None:
    """Хвост ряда → атрибут модели (persist — забота save() конкретного
    класса; Ensemble пиклится целиком, MIMO/SKUForecaster сохраняют dict)."""
    model.market_tail = tail_for_artifact(market)


def apply_model_market(model, df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    """Serve-хелпер: no-op для моделей без market_tail (старые pickle —
    обратная совместимость: их feature_cols этих колонок и не требуют)."""
    tail = getattr(model, "market_tail", None)
    if tail is None:
        return df
    return merge_market_features(df, tail, date_col)
