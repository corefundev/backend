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
import copy
import json
import os
import tempfile
from typing import Callable

import yaml

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


# Tier overrides matching src/plans/plans.py (verified 2026-06-09): Free =
# objective mse, HPO off; Business = objective ensemble (3× MIMO blend), HPO 30
# trials. RU regressors stay at the config default (off) to match the original
# 60-feature business baseline (the +RU 81-feature run was a separate study).
_TIER_OVERRIDES = {
    "free":     {"objective": "mse",      "hpo_enabled": False, "hpo_n_trials": 0},
    "business": {"objective": "ensemble", "hpo_enabled": True,  "hpo_n_trials": 30},
}


def _isolated_config(base_config: dict, workdir: str, tier: str) -> dict:
    """Deep-copy the base config and isolate it for a backtest:

    - MLflow tracking → a LOCAL file store under workdir (NEVER the production
      mlflow server / S3 artifact bucket — a backtest must not pollute either).
    - apply the tier's objective + HPO (Free vs Business).
    """
    cfg = copy.deepcopy(base_config)
    cfg.setdefault("mlflow", {})
    cfg["mlflow"]["tracking_uri"] = "file://" + os.path.join(workdir, "mlflow")
    cfg["mlflow"]["experiment_name"] = f"backtest-{tier}"

    ov = _TIER_OVERRIDES[tier]
    cfg.setdefault("model", {})["objective"] = ov["objective"]
    cfg.setdefault("hpo", {})["enabled"] = ov["hpo_enabled"]
    cfg["hpo"]["n_trials"] = ov["hpo_n_trials"]
    return cfg


def run_baseline(
    data_path: str = "data/test/sample_start.csv",
    config_path: str = "configs/config.yaml",
    holdout_days: int = 14,
    label: str = "baseline",
    tier: str = "free",
) -> BacktestResult:
    """Train on the pre-cutoff slice, serve the holdout through the real path,
    score against actuals. Returns the BacktestResult (also see .as_dict())."""
    if tier not in _TIER_OVERRIDES:
        raise ValueError(f"tier must be one of {sorted(_TIER_OVERRIDES)}, got {tier!r}")

    # FORCE local model storage (not setdefault): the worker's env has
    # STORAGE_BACKEND=s3, and a backtest must never write its throwaway model to
    # the production S3 model store. train_fn trains a FRESH model and loads it
    # right back in-process, so a local temp store is always correct.
    os.environ["STORAGE_BACKEND"] = "local"

    base_config = load_config(config_path)
    df = pd.read_csv(data_path)
    # The pipeline parses dates on load, but the harness slices the in-memory df
    # straight into serve_fn → build_features, which needs datetime (.dt). Parse
    # once here so train + serve see the same datetime column.
    date_col = base_config["data"]["date_col"]
    df[date_col] = pd.to_datetime(df[date_col])

    with tempfile.TemporaryDirectory(prefix="backtest-") as workdir:
        # isolate model artifacts to a writable temp dir (the worker's
        # ARTIFACTS_DIR=/data/artifacts is not writable from a bare exec).
        os.environ["ARTIFACTS_DIR"] = os.path.join(workdir, "artifacts")

        iso_config = _isolated_config(base_config, workdir, tier)
        iso_config_path = os.path.join(workdir, "backtest_config.yaml")
        with open(iso_config_path, "w") as f:
            yaml.safe_dump(iso_config, f)

        return run_backtest(
            df,
            iso_config,
            train_fn=_make_train_fn(iso_config_path, workdir),
            serve_fn=_make_serve_fn(iso_config_path),
            holdout_days=holdout_days,
            label=label,
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Run a forecast-quality backtest baseline.")
    p.add_argument("--data", default="data/test/sample_start.csv")
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--holdout-days", type=int, default=14)
    p.add_argument("--label", default="baseline")
    p.add_argument("--tier", choices=sorted(_TIER_OVERRIDES), default="free")
    args = p.parse_args()

    res = run_baseline(args.data, args.config, args.holdout_days, args.label, args.tier)
    print(json.dumps({**res.as_dict(), "tier": args.tier}, indent=2, default=str))


if __name__ == "__main__":
    main()
