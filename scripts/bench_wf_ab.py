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
            raw_df=None) -> dict:
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

    if model_factory is None:
        arm_config = {**config, "model": {**config["model"]}}
        if spec["model"] == "ensemble":
            arm_config["model"]["objective"] = "ensemble"
            from src.models.ensemble import EnsembleForecaster
            model_factory = lambda: EnsembleForecaster(arm_config)  # noqa: E731
        else:
            arm_config["model"]["type"] = "mimo"
            arm_config["model"]["objective"] = "tweedie"
            # #407: фиксированные HPO-пробы — ПОСЛЕ type/objective, чтобы
            # override мог менять и их при необходимости
            arm_config["model"].update(spec.get("model_overrides", {}))
            _bc = spec.get("band_calibration")
            if _bc:
                # QH-5 #422: MIMO с fold-clean band-калибровкой
                model_factory = lambda: _BandCalibratedMIMO(         # noqa: E731
                    arm_config, tuple(_bc["clip"]))
            else:
                from src.models.mimo import MIMOForecaster
                model_factory = lambda: MIMOForecaster(arm_config)   # noqa: E731

    excluded = set(spec.get("exclude", ())) | set(market_excluded)
    feature_cols = [c for c in get_feature_columns(df, config)
                    if (spec["statics"] != "off" or c not in STATIC_COLS)
                    and c not in excluded]

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

    raw_df = load_data(args.data_path, config)
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
        out = run_arm(arm, df, config, raw_df=raw_df)
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
