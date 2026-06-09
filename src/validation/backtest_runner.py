"""
src/validation/backtest_runner.py

Wires src/validation/backtest.run_backtest to the REAL training + serving
pipeline (Phase 2, layer-1 wiring — R11-#76). The harness (backtest.py) is
pipeline-agnostic (injected train_fn/serve_fn); this module supplies the real
ones so a backtest measures the CUSTOMER's actual forecast quality.

- train_fn: runs `run_training_pipeline` on the train slice, isolated
  (client_id="backtest", STORAGE_BACKEND=local → artifacts/backtest/, no real
  client touched), then loads the signed model artifact.
- serve_fn: build_features → forecast_all_skus — the SAME per-SKU
  `recursive_forecast` path production serves. So the measured number includes
  the H2 recursive-compounding the gate exists to surface (R11-#59); this
  baseline is therefore the "before" for the H1/H2 correctness fixes.

The heavy run needs libomp (LightGBM) — run it on Linux/staging, NOT the macOS
dev box (see project_ml_test_failures_unverified). Invoke on staging with:
    docker exec docker-worker-1 python -m src.validation.backtest_runner \
        --data data/test/sample_start.csv --holdout-days 14
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from typing import Callable

import pandas as pd

from src.data.loader import load_config
from src.features.engineering import build_features, get_feature_columns
from src.pipeline.inference_utils import forecast_all_skus, load_model_any_format
from src.pipeline.train import run_training_pipeline
from src.validation.backtest import BacktestResult, run_backtest


def _make_train_fn(config_path: str, workdir: str) -> Callable[[pd.DataFrame, dict], object]:
    """Return train_fn(train_df, config) -> loaded model.

    run_training_pipeline reads a FILE path (not a df) and saves a signed model
    artifact; we materialise the train slice to a temp CSV, train the isolated
    'backtest' client, then load + return the model object.
    """
    def train_fn(train_df: pd.DataFrame, config: dict) -> object:
        csv_path = os.path.join(workdir, "backtest_train.csv")
        train_df.to_csv(csv_path, index=False)
        result = run_training_pipeline(
            data_path=csv_path,
            config_path=config_path,
            client_id="backtest",
            output_dir=os.path.join(workdir, "out"),
        )
        return load_model_any_format(result["model_path"], config)

    return train_fn


def _make_serve_fn(config_path: str) -> Callable[..., pd.DataFrame]:
    """Return serve_fn(model, history_df, horizon) -> DataFrame[sku,date,predicted_sales].

    Replays EXACTLY the production serve path: engineer features, then
    forecast_all_skus (per-SKU recursive_forecast). Extra output columns
    (p10/p90/step/source) are dropped — the harness only needs the point forecast.
    """
    def serve_fn(model, history_df: pd.DataFrame, horizon: int) -> pd.DataFrame:
        config = load_config(config_path)
        df_feat = build_features(history_df, config)
        feature_cols = get_feature_columns(df_feat, config)
        out = forecast_all_skus(model, df_feat, feature_cols, config, horizon=horizon)
        sku_col = config["data"]["sku_col"]
        date_col = config["data"]["date_col"]
        return out[[sku_col, date_col, "predicted_sales"]]

    return serve_fn


def run_baseline(
    data_path: str = "data/test/sample_start.csv",
    config_path: str = "configs/config.yaml",
    holdout_days: int = 14,
    label: str = "baseline",
) -> BacktestResult:
    """Train on the pre-cutoff slice, serve the holdout through the real path,
    score against actuals. Returns the BacktestResult (also see .as_dict())."""
    # Isolate artifacts to local storage under artifacts/backtest/ (never a real
    # client). FORCE local (not setdefault): the worker's env has
    # STORAGE_BACKEND=s3, and a backtest must never write its throwaway model to
    # the production S3 model store. train_fn always trains a FRESH model and
    # loads it right back in-process, so a local temp store is always correct.
    os.environ["STORAGE_BACKEND"] = "local"

    config = load_config(config_path)
    df = pd.read_csv(data_path)

    with tempfile.TemporaryDirectory(prefix="backtest-") as workdir:
        return run_backtest(
            df,
            config,
            train_fn=_make_train_fn(config_path, workdir),
            serve_fn=_make_serve_fn(config_path),
            holdout_days=holdout_days,
            label=label,
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Run a forecast-quality backtest baseline.")
    p.add_argument("--data", default="data/test/sample_start.csv")
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--holdout-days", type=int, default=14)
    p.add_argument("--label", default="baseline")
    args = p.parse_args()

    res = run_baseline(args.data, args.config, args.holdout_days, args.label)
    print(json.dumps(res.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    main()
