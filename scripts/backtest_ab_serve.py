"""
scripts/backtest_ab_serve.py — honest A/B for R11-#59 (H2).

Trains ONE model, then serves the SAME model + SAME temporal holdout two ways —
recursive (old) vs direct (H2 fix) — and scores both. This isolates the serve-
path effect from training variance (the naive before/after retrains a fresh
model each run, so its delta is confounded).

Run on staging (libomp):
    docker exec -e MODEL_SIGNING_KEY=x -e MLFLOW_ALLOW_FILE_STORE=true \
        docker-worker-1 python scripts/backtest_ab_serve.py \
        --data /tmp/sample_free.csv --tier free --holdout-days 7
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

import pandas as pd
import yaml


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data/test/sample_free.csv")
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--tier", default="free")
    p.add_argument("--holdout-days", type=int, default=7)
    args = p.parse_args()

    os.environ["STORAGE_BACKEND"] = "local"
    os.environ.pop("DATABASE_URL", None)

    from src.data.loader import load_config
    from src.features.engineering import build_features, get_feature_columns
    from src.pipeline.inference_utils import forecast_all_skus, recursive_forecast
    from src.validation.backtest import run_backtest, temporal_split
    from src.validation.backtest_runner import (
        _isolated_config, _make_train_fn, _register_backtest_client,
    )

    base = load_config(args.config)
    df = pd.read_csv(args.data)
    df[base["data"]["date_col"]] = pd.to_datetime(df[base["data"]["date_col"]])

    with tempfile.TemporaryDirectory(prefix="ab-") as wd:
        os.environ["ARTIFACTS_DIR"] = os.path.join(wd, "art")
        os.environ["REGISTRY_PATH"] = os.path.join(wd, "reg.json")
        _register_backtest_client(args.tier)
        iso = _isolated_config(base, wd, args.tier)
        icp = os.path.join(wd, "cfg.yaml")
        with open(icp, "w") as f:
            yaml.safe_dump(iso, f)

        # Train ONCE on the pre-cutoff slice. Cache the fitted model (keyed by
        # tier+holdout+data) so serve-side iterations don't pay the full retrain
        # — an ensemble+HPO business fit is ~70min and the A/B only varies the
        # serve path, never the model.
        import pickle
        _key = f"{args.tier}_{args.holdout_days}_{os.path.basename(args.data)}"
        _cache = os.path.join(tempfile.gettempdir(), f"ab_model_{_key}.pkl")
        if os.path.exists(_cache):
            with open(_cache, "rb") as fh:
                model = pickle.load(fh)
        else:
            train, _, _ = temporal_split(df, args.holdout_days, base["data"]["date_col"])
            model = _make_train_fn(icp, wd)(train, iso)
            with open(_cache, "wb") as fh:
                pickle.dump(model, fh)
        fixed_train_fn = lambda tr, c: model     # noqa: E731 — return the one model

        sku_col = base["data"]["sku_col"]
        date_col = base["data"]["date_col"]
        target_col = base["data"]["target_col"]

        # R12-#100 — pin to the model's trained lag/rolling set so a serve frame
        # whose shortest SKU can't support the long lags doesn't silently drop
        # columns the model expects (KeyError on heterogeneous-history real data).
        # Mirrors backtest_runner._make_serve_fn / the prod serve path.
        from src.pipeline.inference_utils import serve_feature_set
        _lags, _rw = serve_feature_set(model)

        def _feat(hist):
            c = load_config(icp)
            f = build_features(hist, c, pin_lags=_lags or None,
                               pin_rolling=_rw, drop_warmup=False)
            return c, f, get_feature_columns(f, c)

        def serve_direct(m, hist, h):
            c, f, fc = _feat(hist)
            out = forecast_all_skus(m, f, fc, c, horizon=h)       # H2 dispatch → direct
            return out[[sku_col, date_col, "predicted_sales"]]

        def serve_recursive(m, hist, h):
            c, f, fc = _feat(hist)
            rows = []
            for sku, g in f.groupby(sku_col, sort=False):
                rows += recursive_forecast(m, g.sort_values(date_col), fc, h, sku,
                                           sku_col, date_col, target_col, config=c)
            return pd.DataFrame(rows)[[sku_col, date_col, "predicted_sales"]]

        rd = run_backtest(df, iso, train_fn=fixed_train_fn, serve_fn=serve_direct,
                          holdout_days=args.holdout_days, label="direct")
        rr = run_backtest(df, iso, train_fn=fixed_train_fn, serve_fn=serve_recursive,
                          holdout_days=args.holdout_days, label="recursive")

    def _ph(r):
        return {int(k): round(v["wmape"], 3) for k, v in r.per_horizon.items()}

    print(json.dumps({
        "tier": args.tier, "holdout_days": args.holdout_days, "n_skus": rd.n_skus,
        "recursive": {"wmape": rr.wmape_global, "mase": rr.mase_global, "smape": rr.smape_global},
        "direct":    {"wmape": rd.wmape_global, "mase": rd.mase_global, "smape": rd.smape_global},
        "recursive_per_h": _ph(rr),
        "direct_per_h":    _ph(rd),
    }, indent=2))


if __name__ == "__main__":
    main()
