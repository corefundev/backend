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


def run_arm(arm_name: str, base_df, config: dict,
            model_factory: Optional[Callable[[], Any]] = None) -> dict:
    """Одно плечо на подготовленном фрейме (build_features уже сделан,
    static/market НЕ смержены — этим управляет спецификация плеча)."""
    from src.features.engineering import get_feature_columns
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

    if model_factory is None:
        arm_config = {**config, "model": {**config["model"]}}
        if spec["model"] == "ensemble":
            arm_config["model"]["objective"] = "ensemble"
            from src.models.ensemble import EnsembleForecaster
            model_factory = lambda: EnsembleForecaster(arm_config)  # noqa: E731
        else:
            arm_config["model"]["type"] = "mimo"
            arm_config["model"]["objective"] = "tweedie"
            from src.models.mimo import MIMOForecaster
            model_factory = lambda: MIMOForecaster(arm_config)      # noqa: E731

    excluded = set(spec.get("exclude", ())) | set(market_excluded)
    feature_cols = [c for c in get_feature_columns(df, config)
                    if (spec["statics"] != "off" or c not in STATIC_COLS)
                    and c not in excluded]

    t0 = time.time()
    res = walk_forward_validate(df, model_factory, feature_cols, config,
                                fold_feature_fn=fold_feature_fn)

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

    df = load_data(args.data_path, config)
    df = build_features(df, config)
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
        out = run_arm(arm, df, config)
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
