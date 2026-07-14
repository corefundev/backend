"""
scripts/bench_wf_ab.py — канонический честный A/B-стенд (QH-1 #381).

Единственный легитимный способ мерить эффект фичи/модели на точность.
История вопроса: решения QW2/QW3 принимались на невоспроизводимом стенде
(одно 14-дневное окно, скрипты вне репо) — три ретракции подряд:
static −5.66%→−0.51% (#358), FX +12%→вредит, market −7.32%→+1.46% (#380).
Этот инструмент закрывает класс: фиксированный харнесс, плечи из реестра,
запуск одной командой, JSON-вывод, декомпозиция ошибки.

Харнесс (держится констанстным между плечами — константы сокращаются
в дельтах):
  • 3-fold expanding walk-forward (validation.n_splits из конфига);
  • static-фичи fold-clean через AUD-6 hook (кроме плеч statics_*);
  • HPO выключен, anomaly-веса выключены;
  • MIMO/tweedie валидатор по умолчанию (плечо `ensemble` — ансамбль).

Запуск на prod-данных — ТОЛЬКО в throwaway-контейнере (см.
docs/benchmarks.md): собственный memory-cap, нулевые trainings в очереди.

    BENCH_CLIENT_ID=test BENCH_DATA_PATH=s3://... \
        python scripts/bench_wf_ab.py --arms base,market_on

Вывод: одна JSON-строка на плечо (aggregated + декомпозиция
band×horizon) + summary с дельтами против первого плеча.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Optional

# ── Реестр плеч ──────────────────────────────────────────────────────────
# statics: "fold_clean" (честно, как в prod после AUD-6) | "leaky"
# (до-AUD-6 поведение, для оценки утечки) | "off".
# market:  False | "full" (абсолютные тоталы — снятый с default вариант,
# #380) | "share" (только стационарная доля SKU) | "momentum" (доля +
# отношение рынка к неделе назад — оба стационарные возврат-кандидаты).
# exclude: имена фич, скрываемые от модели В ЭТОМ ПЛЕЧЕ (колонки в фрейме
# остаются — исключение только из feature_cols; так выключается фича,
# вшитая в build_features безусловно, напр. payday).
# recency_half_life: дни; включает recency-decay sample-веса (#319) в этом
# плече через sample_weight_fn (в остальных плечах весов нет).
# model_overrides: dict, домердживается в arm_config["model"] ПОСЛЕ
# выставления type/objective — фиксированные HPO-пробы (#407): дешевле и
# интерпретируемее Optuna, дозы видны явно.
# features_overrides: dict, домердживается в config["features"] ЭТОГО
# плеча; признаки пересобираются из СЫРОГО фрейма (#414 длинная память).
# Warmup-дроп удлиняется честно (как в prod); тестовые окна — те же
# последние даты, поэтому дельты корректны.
ARMS: dict[str, dict] = {
    "base":          {"model": "mimo",     "statics": "fold_clean", "market": False},
    "mimo":          {"model": "mimo",     "statics": "fold_clean", "market": False},
    "ensemble":      {"model": "ensemble", "statics": "fold_clean", "market": False},
    "market_on":     {"model": "mimo",     "statics": "fold_clean", "market": "full"},
    "market_share_only": {"model": "mimo", "statics": "fold_clean", "market": "share"},
    "market_momentum":   {"model": "mimo", "statics": "fold_clean", "market": "momentum"},
    "statics_off":   {"model": "mimo",     "statics": "off",        "market": False},
    "statics_leaky": {"model": "mimo",     "statics": "leaky",      "market": False},
    "payday_off":    {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "exclude": ("days_to_payday", "is_payday_window")},
    # B1 #319: recency-decay sample-weights (0.5 ** (age/half_life), floor
    # 0.05, якорь = cutoff трейн-фолда → fold-clean по построению). Два
    # half-life — dose-response: если 90d лучше 180d, сигнал реален.
    "recency_hl180": {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "recency_half_life": 180},
    "recency_hl90":  {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "recency_half_life": 90},
    # QH-3 #414 → QH-4 #418: длинная память, dose-response 28/56/112.
    # per_sku_lags снимает глобальный min-history кап — без него стенд
    # молча резал 56/112 и все три плеча были бит-в-бит идентичны
    # (журнал #414). Warmup при флаге — по короткому обязательному лагу,
    # короткие SKU остаются в трейне с NaN в длинных лагах.
    "mem28":         {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "features_overrides": {
                          "per_sku_lags": True,
                          "lags": [1, 7, 14, 28],
                          "rolling_windows": [7, 14, 28]}},
    "mem56":         {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "features_overrides": {
                          "per_sku_lags": True,
                          "lags": [1, 7, 14, 28, 56],
                          "rolling_windows": [7, 14, 28, 56]}},
    "mem112":        {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "features_overrides": {
                          "per_sku_lags": True,
                          "lags": [1, 7, 14, 28, 56, 112],
                          "rolling_windows": [7, 14, 28, 56, 112]}},
    # Кандидат на флип дефолта (#418): lag-список НЕ трогаем — ровно тот
    # конфиг, что получит prod при включении флага (config.yaml до 365).
    "psl_default":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "features_overrides": {
                          "per_sku_lags": True}},
    # QH-5 #422: пост-hoc мультипликативная калибровка по velocity-band
    # (mid недо- 0.86 / slow пере- 1.18). Fold-clean: факторы оцениваются
    # на ВНУТРЕННЕМ хвосте трейн-фолда (последние H строк каждого SKU),
    # тест-фолд их не видит. Пара клипов — dose-response.
    "band_cal":      {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "band_calibration": {"clip": (0.8, 1.25)}},
    "band_cal_wide": {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "band_calibration": {"clip": (0.65, 1.5)}},
    # #384: band-роутинг MIMO↔ensemble. Гипотеза из живого замера:
    # ансамбль выигрывает MASE/per-SKU (малые позиции), MIMO — headline
    # WMAPE (fast = 83% массы). Роутим по fold-clean velocity_band
    # (статик-фича фолда): band из route → ensemble, иначе калиброванный
    # MIMO (как prod). Две дозы маршрута — dose-response.
    "route_slow_ens":    {"model": "mimo", "statics": "fold_clean", "market": False,
                          "band_route": (0,)},
    "route_slowmid_ens": {"model": "mimo", "statics": "fold_clean", "market": False,
                          "band_route": (0, 1)},
    # Продуктовый HybridForecaster (equivalence-плечо: обязано ≈ повторить
    # route_slowmid_ens — тот же роутинг, но боевым классом)
    "hybrid_prod":       {"model": "hybrid", "statics": "fold_clean", "market": False},
    # QH-8 #441: value-weighted обучение. Метрика WMAPE_g взвешена
    # объёмом, лосс — нет: модель равно старается на копеечных и тяжёлых
    # строках. Вес ∝ |y| (полное выравнивание) и ∝ sqrt|y| (полдозы) —
    # dose-response. Нормировка к среднему весу 1, +1e-3 — нулевые строки
    # не выпадают из обучения совсем.
    "vw_abs":        {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "value_weight": "abs"},
    "vw_sqrt":       {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "value_weight": "sqrt"},
    # QH-7 #440: event-ramp окна (пред-НГ ramp, мёртвый январь, 23фев/8мар,
    # 1мая). Именные разгоны вместо generic days_to_holiday (тот прыгает к
    # ЛЮБОМУ ближайшему празднику и размывает НГ). Две дозы — dose-response:
    # если full ≈ ny, сигнал несёт только НГ-пара.
    "event_ramp_ny":   {"model": "mimo",   "statics": "fold_clean", "market": False,
                        "features_overrides": {
                            "event_ramp": {"enabled": True, "set": "ny"}}},
    "event_ramp_full": {"model": "mimo",   "statics": "fold_clean", "market": False,
                        "features_overrides": {
                            "event_ramp": {"enabled": True, "set": "full"}}},
    # QH-10 #443: конверт — панель до-кампании. MIMO без band-калибровки
    # (#422 off) = Free/Start до ship'а; ensemble-арм (выше) = платные до
    # #384. Вместе с base и hybrid_prod образуют 2×2 ship/pre-ship панель.
    "mimo_uncal":    {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "band_cal_off": True},
    # #460 M5-калибровка: seasonal-naive (lag-7 паттерн недели) — эталонный
    # пол. Смысл плеча: измерить, НАСКОЛЬКО движок бьёт наивку на том же
    # датасете/метрике (M5-литература: хороший ML ≈ 10-25% MASE над snaive).
    "snaive":        {"model": "snaive",   "statics": "fold_clean", "market": False},
    # #460 прогон 2: US-праздники для американских данных (M5) — снимает
    # RU-календарный гандикап базы; сравнивать с base на том же датасете.
    "holidays_us":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "features_overrides": {
                          "holidays": {"enabled": True, "country": "US"}}},
    # QH-9 #442: per-step калибровка горизонта (WMAPE растёт 0.33@1д →
    # ~0.50@13-14д; band-калибровка #422 шаги не различает). Факторы =
    # clip(Σфакт_h/Σпрогноз_h) на внутреннем хвосте ТОГО ЖЕ probe, по
    # band-скорректированному прогнозу (step правит остаток — уровень не
    # считается дважды). Прод-путь MIMOForecaster (model.step_calibration).
    # Пара плеч: поверх прод-band (ship-кандидат) и solo (изолирует сигнал).
    "step_cal":      {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "step_calibration": {"clip": (0.8, 1.25)}},
    "step_cal_solo": {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "step_calibration": {"clip": (0.8, 1.25)},
                      "band_cal_off": True},
    # QW3-1b #316: category target-encoding ЛОКАЛЬНЫМ join-ом (карта
    # sku->категория из BENCH_CATEGORY_PATH; adapt_1c --categories-out).
    # Fold-clean: TE = smoothed mean спроса категории, посчитанный на
    # ТРЕЙН-строках фолда (m = байесовское сглаживание к глобальному
    # среднему). Пара m — dose-response. Ingestion-path строим только
    # при вердикте >= ~1% (условие заморозки #316).
    "cat_te":        {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "category_te": {"smoothing": 20}},
    "cat_te_m100":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "category_te": {"smoothing": 100}},
    # QH-2 #407: HPO-пробы против mid-band недопрогноза (bias 0.86).
    # tweedie p→1 сильнее штрафует недооценку больших значений; 1.7 —
    # контрольная доза в другую сторону (default 1.5 = base).
    "tweedie_p11":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "model_overrides": {"tweedie_variance_power": 1.1}},
    "tweedie_p13":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "model_overrides": {"tweedie_variance_power": 1.3}},
    "tweedie_p17":   {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "model_overrides": {"tweedie_variance_power": 1.7}},
    "leaves128":     {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "model_overrides": {"num_leaves": 128}},
    "lr003_est800":  {"model": "mimo",     "statics": "fold_clean", "market": False,
                      "model_overrides": {"learning_rate": 0.03,
                                          "n_estimators": 800}},
}

# Абсолютные market-колонки — нестационарная часть (#380): плечи
# share/momentum считают их (share строится из тотала), но прячут от модели.
_MARKET_ABSOLUTE = ("market_total_lag_1", "market_total_lag_7")


def _fingerprint(df, sku_col: str, date_col: str, target_col: str) -> dict:
    """Отпечаток ДАННЫХ в отчёте — защита класса «мерили не то»
    (feedback_dataset_provenance): контент-хэш + размерности позволяют
    сверить прогон с реестром датасетов в журнале замеров."""
    import hashlib
    import pandas as pd
    key = (df[[sku_col, date_col, target_col]]
           .sort_values([sku_col, date_col]).reset_index(drop=True))
    h = hashlib.sha256(
        pd.util.hash_pandas_object(key, index=False).to_numpy().tobytes()
    ).hexdigest()[:12]
    return {"content_sha12": h, "rows": len(df),
            "skus": int(df[sku_col].nunique()),
            "dates": int(df[date_col].nunique())}


def _wmape(g) -> Optional[float]:
    denom = g["actual"].abs().sum()
    if denom == 0:
        return None
    return round(float((g["actual"] - g["predicted"]).abs().sum() / denom), 5)


def decompose(combined, split_points, band_by_sku: dict, sku_col: str) -> dict:
    """WMAPE по velocity-band и по шагу горизонта.

    band — метка по ПОЛНОЙ истории (только для отчёта, не фича — утечки
    в модель нет); horizon_step = (date − split_date фолда) в днях, 1..H."""
    import pandas as pd
    df = combined.copy()
    df["band"] = df[sku_col].map(band_by_sku)
    split_by_fold = {i: pd.Timestamp(s) for i, s in enumerate(split_points)}
    df["horizon_step"] = (
        (df["date"] - df["fold"].map(split_by_fold)).dt.days.astype(int)
    )
    by_band = {
        str(int(band)): {"wmape": _wmape(g), "rows": len(g),
                         "abs_err_share": round(float(
                             (g["actual"] - g["predicted"]).abs().sum()
                             / (df["actual"] - df["predicted"]).abs().sum()), 4)}
        for band, g in df.groupby("band") if band == band  # NaN-safe
    }
    by_step = {
        str(int(step)): {"wmape": _wmape(g), "rows": len(g)}
        for step, g in df.groupby("horizon_step")
    }
    return {"by_band": by_band, "by_horizon_step": by_step}


class _BandRoutedModel:
    """#384: фиксированный band-роутинг между калиброванным MIMO (как
    prod) и ансамблем. Band берётся из fold-clean статик-фичи
    velocity_band ПРЯМО ИЗ СТРОКИ прогноза (никакого пересчёта на тесте
    → утечки нет по построению). Строка без band'а → MIMO (fallback).
    Фабрики инжектируются — тесты подставляют стабы (libomp-free)."""

    is_mimo = True    # walk_forward: direct multi-step режим

    def __init__(self, mimo_factory, ens_factory, route_bands,
                 band_col: str = "velocity_band"):
        self._mimo = mimo_factory()
        self._ens = ens_factory()
        self._route = {int(b) for b in route_bands}
        self._band_col = band_col

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        self._mimo.fit(X, y, groups=groups, sample_weight=sample_weight,
                       target_censor=target_censor)
        self._ens.fit(X, y, groups=groups, sample_weight=sample_weight,
                      target_censor=target_censor)
        return self

    def compute_blend_weights(self, **kw):
        # walk_forward зовёт hook по hasattr — проксируем ансамблю
        if hasattr(self._ens, "compute_blend_weights"):
            self._ens.compute_blend_weights(**kw)

    def predict(self, X):
        import numpy as np
        cols = getattr(X, "columns", [])
        band = None
        if self._band_col in cols and len(X) > 0:
            v = X[self._band_col].iloc[0]
            band = int(v) if v == v else None    # NaN-safe
        target = self._ens if band in self._route else self._mimo
        return np.asarray(target.predict(X), dtype=float)


class _SeasonalNaive:
    """#460: сезонная наивка — прогноз t+h = значение того же дня недели
    из последних 7 наблюдений трейна. Никакого обучения: это измеренный
    ПОЛ, относительно которого калибруется движок (MASE-язык статей)."""

    is_mimo = True    # walk_forward: direct multi-step режим

    def __init__(self, config: dict):
        self._h = int(config["model"]["horizon"])
        self._sku_col = config["data"]["sku_col"]

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        import numpy as np
        import pandas as pd
        g = pd.Series(groups).reset_index(drop=True)
        yr = pd.Series(y).reset_index(drop=True)
        self._last7 = {}
        for sku, idx in g.groupby(g).groups.items():
            vals = yr.loc[idx].to_numpy(dtype=float)
            if len(vals) >= 7:
                self._last7[str(sku)] = vals[-7:]
            elif len(vals) > 0:
                self._last7[str(sku)] = np.resize(vals, 7)
        return self

    def predict(self, X):
        import numpy as np
        out = []
        for sku in X[self._sku_col].astype(str):
            last7 = self._last7.get(sku)
            if last7 is None:
                out.append(np.zeros(self._h))
                continue
            # t+h — тот же день недели, что last7[(h-1) % 7] (h=7 → последний
            # день трейна, h=1 → 6 дней до него).
            out.append(np.array([last7[(h - 1) % 7] for h in range(1, self._h + 1)]))
        return np.stack(out)


class _BandCalibratedMIMO:
    """QH-5 #422: MIMO + пост-hoc мультипликативная калибровка по
    velocity-band. Fold-clean по построению:
      1) внутренний фит на «голове» трейна (без последних H строк каждого
         SKU); band'ы — тершили среднего спроса ПО ГОЛОВЕ (та же логика,
         что velocity_band в static_features);
      2) факторы = clip(Σactual/Σpred) по band'у на внутреннем хвосте
         (участвуют SKU с головой ≥ 2H строк);
      3) финальный фит — на всём трейне; predict() умножает вектор
         прогноза на фактор band'а SKU.
    Тест-фолд не участвует в оценке факторов нигде."""

    is_mimo = True    # walk_forward: direct multi-step режим

    def __init__(self, config: dict, clip: tuple):
        from src.models.mimo import MIMOForecaster
        self._mk = lambda: MIMOForecaster(config)   # noqa: E731
        self._inner = self._mk()
        self._clip = clip
        self._h = int(config["model"]["horizon"])
        self._sku_col = config["data"]["sku_col"]
        self.factors: dict = {}
        self._band_by_sku: dict = {}

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        import numpy as np
        import pandas as pd
        g = pd.Series(groups).reset_index(drop=True)
        Xr = X.reset_index(drop=True)
        yr = pd.Series(y).reset_index(drop=True)
        sw = pd.Series(sample_weight).reset_index(drop=True) if sample_weight is not None else None
        tc = pd.Series(target_censor).reset_index(drop=True) if target_censor is not None else None

        fwd = g.groupby(g).cumcount()
        cnt = g.map(g.value_counts())
        tail = (cnt - fwd) <= self._h
        head = ~tail

        probe = self._mk()
        probe.fit(Xr[head], yr[head], groups=g[head],
                  sample_weight=None if sw is None else sw[head].to_numpy(),
                  target_censor=None if tc is None else tc[head])

        mean_by_sku = yr[head].groupby(g[head]).mean()
        t1, t2 = np.quantile(mean_by_sku.to_numpy(), [1 / 3, 2 / 3])
        self._band_by_sku = {s: int(np.digitize(v, [t1, t2]))
                             for s, v in mean_by_sku.items()}

        head_counts = g[head].value_counts()
        eligible = set(head_counts[head_counts >= 2 * self._h].index)
        sums_p = {0: 0.0, 1: 0.0, 2: 0.0}
        sums_a = {0: 0.0, 1: 0.0, 2: 0.0}
        head_idx_by_sku = {s: i for s, i in g[head].groupby(g[head]).groups.items()}
        tail_idx_by_sku = {s: i for s, i in g[tail].groupby(g[tail]).groups.items()}
        for sku in eligible:
            try:
                raw = probe.predict(Xr.loc[[head_idx_by_sku[sku][-1]]])
            except Exception:    # noqa: BLE001 — SKU пропускается, стенд не падает
                continue
            preds = np.clip(np.asarray(raw, dtype=float)[0], 0, None)
            actual = yr.loc[tail_idx_by_sku.get(sku, [])].to_numpy()[: len(preds)]
            k = min(len(preds), len(actual))
            if k == 0:
                continue
            b = self._band_by_sku.get(sku, 1)
            sums_p[b] += float(preds[:k].sum())
            sums_a[b] += float(actual[:k].sum())
        lo, hi = self._clip
        self.factors = {b: float(np.clip(sums_a[b] / sums_p[b], lo, hi))
                        for b in sums_p if sums_p[b] > 0}

        self._inner = self._mk()
        self._inner.fit(Xr, yr, groups=g,
                        sample_weight=None if sw is None else sw.to_numpy(),
                        target_censor=tc)
        return self

    def predict(self, X):
        import numpy as np
        raw = np.asarray(self._inner.predict(X), dtype=float)
        cols = getattr(X, "columns", [])
        if self._sku_col in cols and len(X) > 0:
            sku = X[self._sku_col].iloc[0]
            f = self.factors.get(self._band_by_sku.get(sku, -1), 1.0)
            return raw * f
        return raw


def run_arm(arm_name: str, base_df, config: dict,
            model_factory: Optional[Callable[[], Any]] = None,
            raw_df=None,
            category_map: Optional[dict] = None) -> dict:
    """Одно плечо на подготовленном фрейме (build_features уже сделан,
    static/market НЕ смержены — этим управляет спецификация плеча).
    Плечи с features_overrides пересобирают признаки из raw_df."""
    from src.features.engineering import build_features, get_feature_columns
    from src.features.market import compute_market_tail, merge_market_features
    from src.features.static_features import (
        STATIC_COLS, compute_static_map, fold_static_recompute,
        merge_static_features,
    )
    from src.validation.walk_forward import _get_split_points, walk_forward_validate

    spec = ARMS[arm_name]
    sku_col, date_col, target_col = (config["data"]["sku_col"],
                                     config["data"]["date_col"],
                                     config["data"]["target_col"])
    feat_overrides = spec.get("features_overrides")
    if feat_overrides:
        if raw_df is None:
            raise ValueError(
                f"arm {arm_name!r} needs raw_df (features_overrides rebuild)")
        config = {**config,
                  "features": {**config.get("features", {}), **feat_overrides}}
        df = build_features(raw_df.copy(), config)
    else:
        df = base_df.copy()

    if spec["statics"] != "off":
        smap = compute_static_map(df, sku_col, target_col)
        df = merge_static_features(df, smap, sku_col)
    market_mode = spec.get("market") or False
    market_excluded: tuple = ()
    if market_mode:
        market = compute_market_tail(df, date_col, target_col)
        df = merge_market_features(df, market, date_col)
        if market_mode == "momentum":
            # Стационарный momentum: рынок вчера к рынку неделю назад.
            # NaN на warmup ОСТАЁТСЯ NaN (LightGBM various handling) —
            # никакого ложного 0.0 (урок sku_share, #380).
            import numpy as np
            df["market_momentum_7"] = (
                df["market_total_lag_1"]
                / df["market_total_lag_7"].replace(0, np.nan)
            )
        if market_mode in ("share", "momentum"):
            market_excluded = _MARKET_ABSOLUTE

    fold_feature_fn = (
        (lambda tr, te: fold_static_recompute(tr, te, sku_col, target_col))
        if spec["statics"] == "fold_clean" else None
    )

    # QW3-1b #316: fold-clean category TE поверх статик-hook'а. Статистика
    # категории — ТОЛЬКО из трейн-строк фолда; неизвестная категория /
    # SKU вне карты → глобальное среднее фолда (как unknown-SKU fallback
    # в статиках).
    extra_features: list = []
    _cte = spec.get("category_te")
    if _cte:
        if not category_map:
            raise ValueError(
                f"arm {arm_name!r} needs a category map "
                f"(BENCH_CATEGORY_PATH / category_map)")
        _m = float(_cte.get("smoothing", 20))
        _base_fn = fold_feature_fn

        def fold_feature_fn(tr, te, _base=_base_fn, _sm=_m):    # noqa: F811
            if _base is not None:
                tr, te = _base(tr, te)
            tr, te = tr.copy(), te.copy()
            cat_tr = tr[sku_col].astype(str).map(category_map)
            g_mean = float(tr[target_col].mean())
            stats = tr.groupby(cat_tr)[target_col].agg(["sum", "count"])
            te_map = (stats["sum"] + _sm * g_mean) / (stats["count"] + _sm)
            tr["category_te"] = cat_tr.map(te_map).fillna(g_mean)
            te["category_te"] = (te[sku_col].astype(str).map(category_map)
                                 .map(te_map).fillna(g_mean))
            return tr, te

        extra_features.append("category_te")

    # B1 #319: recency-плечо весит строки фолда от ЕГО cutoff'а (train_df
    # max date) — тот же hook, что anomaly-веса в prod (#183); в остальных
    # плечах sample_weight_fn=None (харнесс: веса выключены).
    sample_weight_fn = None
    _rd_hl = spec.get("recency_half_life")
    if _rd_hl:
        from src.data.recency import recency_weights
        sample_weight_fn = (
            lambda tr: recency_weights(tr, date_col, half_life_days=_rd_hl)
        )

    # QH-8 #441: value-веса — из ТАРГЕТА трейн-фолда (прошлое, лика нет)
    _vw = spec.get("value_weight")
    if _vw:
        import numpy as _np

        def _vw_fn(tr, _mode=_vw):
            import pandas as _pd
            y = _np.abs(_pd.to_numeric(tr[target_col], errors="coerce")
                        .fillna(0.0).to_numpy())
            w = y if _mode == "abs" else _np.sqrt(y)
            w = w + 1e-3
            return w * (len(w) / w.sum())

        sample_weight_fn = _vw_fn

    if model_factory is None:
        arm_config = {**config, "model": {**config["model"]}}
        if spec["model"] == "snaive":
            model_factory = lambda: _SeasonalNaive(arm_config)      # noqa: E731
        elif spec["model"] == "hybrid":
            arm_config["model"]["objective"] = "hybrid"
            from src.models.hybrid import HybridForecaster
            model_factory = lambda: HybridForecaster(arm_config)    # noqa: E731
        elif spec["model"] == "ensemble":
            arm_config["model"]["objective"] = "ensemble"
            from src.models.ensemble import EnsembleForecaster
            model_factory = lambda: EnsembleForecaster(arm_config)  # noqa: E731
        else:
            arm_config["model"]["type"] = "mimo"
            arm_config["model"]["objective"] = "tweedie"
            # #407: фиксированные HPO-пробы — ПОСЛЕ type/objective, чтобы
            # override мог менять и их при необходимости
            arm_config["model"].update(spec.get("model_overrides", {}))
            # QH-9 #442: прод-путь step-калибровки + опциональный выключатель
            # band'а (solo-плечо изолирует сигнал шага)
            _sc = spec.get("step_calibration")
            if _sc:
                arm_config["model"]["step_calibration"] = {
                    "enabled": True, "clip": list(_sc["clip"])}
            if spec.get("band_cal_off"):
                arm_config["model"]["band_calibration"] = {"enabled": False}
            _bc = spec.get("band_calibration")
            _br = spec.get("band_route")
            if _br:
                # #384: MIMO (prod-конфиг, с калибровкой из config.yaml) +
                # ансамбль (дети сами отключают калибровку — R14)
                import copy as _copy
                from src.models.ensemble import EnsembleForecaster
                from src.models.mimo import MIMOForecaster
                ens_config = _copy.deepcopy(arm_config)
                ens_config["model"]["objective"] = "ensemble"
                model_factory = lambda: _BandRoutedModel(            # noqa: E731
                    lambda: MIMOForecaster(arm_config),
                    lambda: EnsembleForecaster(ens_config),
                    _br)
            elif _bc:
                # QH-5 #422: MIMO с fold-clean band-калибровкой
                model_factory = lambda: _BandCalibratedMIMO(         # noqa: E731
                    arm_config, tuple(_bc["clip"]))
            else:
                from src.models.mimo import MIMOForecaster
                model_factory = lambda: MIMOForecaster(arm_config)   # noqa: E731

    excluded = set(spec.get("exclude", ())) | set(market_excluded)
    feature_cols = [c for c in get_feature_columns(df, config)
                    if (spec["statics"] != "off" or c not in STATIC_COLS)
                    and c not in excluded] + extra_features

    t0 = time.time()
    res = walk_forward_validate(df, model_factory, feature_cols, config,
                                fold_feature_fn=fold_feature_fn,
                                sample_weight_fn=sample_weight_fn)

    # Метки band по полной истории — для декомпозиции отчёта.
    full_map = compute_static_map(df, sku_col, target_col)
    band_by_sku = dict(zip(full_map[sku_col], full_map["velocity_band"]))
    split_points = _get_split_points(
        df[date_col].sort_values().unique(),
        config["model"]["horizon"],
        config.get("validation", {}).get("n_splits", 3),
    )
    agg = res.aggregated
    return {
        "arm": arm_name, "spec": spec, "n_features": len(feature_cols),
        "wmape_global": round(float(agg.get("wmape_global", float("nan"))), 5),
        "wmape_mean":   round(float(agg.get("wmape_mean", float("nan"))), 5),
        "mase_global":  round(float(agg.get("mase_global", float("nan"))), 5),
        "elapsed_s":    round(time.time() - t0, 1),
        "decomposition": decompose(res.combined, split_points, band_by_sku, sku_col),
    }


def _pct(a: float, b: float) -> Optional[float]:
    return round(100.0 * (a - b) / b, 2) if b else None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--arms", default="base",
                        help=f"через запятую из: {', '.join(sorted(ARMS))}")
    parser.add_argument("--client-id", default=os.environ.get("BENCH_CLIENT_ID"))
    parser.add_argument("--data-path", default=os.environ.get("BENCH_DATA_PATH"))
    parser.add_argument("--category-path",
                        default=os.environ.get("BENCH_CATEGORY_PATH"))
    parser.add_argument("--horizon", type=int, default=None,
                        help="переопределить model.horizon (напр. 28 — #460 прогон 3)")
    args = parser.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown or not arms:
        parser.error(f"unknown arms: {unknown or '(none)'}")
    if not args.client_id or not args.data_path:
        parser.error("--client-id/--data-path (или BENCH_CLIENT_ID/BENCH_DATA_PATH) обязательны")

    from src.auth.vault_agent import bootstrap_secrets
    if not bootstrap_secrets():
        print("bootstrap_secrets failed", file=sys.stderr)
        return 2

    from src.clients.config_manager import get_config_manager
    from src.clients.registry import get_registry
    from src.data.loader import load_data
    from src.features.engineering import build_features

    config = get_config_manager("configs/config.yaml").get_effective(
        args.client_id, get_registry())
    # Харнесс-константы: см. докстринг модуля.
    config.setdefault("hpo", {})["enabled"] = False
    if args.horizon:
        config["model"]["horizon"] = int(args.horizon)

    raw_df = load_data(args.data_path, config)
    cat_map = None
    if args.category_path:
        import pandas as _pd
        _c = _pd.read_csv(args.category_path, dtype=str)
        cat_map = dict(zip(_c.iloc[:, 0], _c.iloc[:, 1]))
    df = build_features(raw_df, config)
    print(json.dumps({"harness": {
        "data": _fingerprint(df, config["data"]["sku_col"],
                             config["data"]["date_col"],
                             config["data"]["target_col"]),
        "source": args.data_path,
        "horizon": config["model"]["horizon"],
        "n_splits": config.get("validation", {}).get("n_splits", 3),
        "arms": arms,
    }}, ensure_ascii=False), flush=True)

    results = []
    for arm in arms:
        out = run_arm(arm, df, config, raw_df=raw_df, category_map=cat_map)
        results.append(out)
        print(json.dumps(out, ensure_ascii=False), flush=True)

    ref = results[0]
    print(json.dumps({"summary": {
        "reference_arm": ref["arm"],
        "deltas_pct_wmape_global": {
            r["arm"]: _pct(r["wmape_global"], ref["wmape_global"])
            for r in results[1:]
        },
        "deltas_pct_mase_global": {
            r["arm"]: _pct(r["mase_global"], ref["mase_global"])
            for r in results[1:]
        },
    }}, ensure_ascii=False))
    print("BENCH: DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
