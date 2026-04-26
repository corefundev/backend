"""
src/models/hpo.py

Hyperparameter optimisation with Optuna (TPE sampler).
Optimises LightGBM params by minimising median WMAPE on walk-forward CV.

Usage:
    best_params = run_hpo(df, feature_cols, config, n_trials=30)
    config["model"].update(best_params)
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def run_hpo(
    df: pd.DataFrame,
    feature_cols: list[str],
    config: dict,
    n_trials: int = 30,
    timeout_sec: int = 600,
) -> dict:
    """
    Run Optuna HPO. Returns dict of best LightGBM hyperparameters.
    Falls back to config defaults if Optuna not installed.
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        logger.warning("optuna not installed — skipping HPO. pip install optuna")
        return {}

    from src.validation.walk_forward import walk_forward_validate
    from src.models.forecaster import SKUForecaster

    target_col = config["data"]["target_col"]

    def objective(trial: "optuna.Trial") -> float:
        params = {
            "n_estimators":      trial.suggest_int("n_estimators",    100, 1000, step=100),
            "learning_rate":     trial.suggest_float("learning_rate",  0.01, 0.2, log=True),
            "num_leaves":        trial.suggest_int("num_leaves",       16, 128),
            "min_child_samples": trial.suggest_int("min_child_samples", 5, 50),
            "feature_fraction":  trial.suggest_float("feature_fraction", 0.5, 1.0),
            "bagging_fraction":  trial.suggest_float("bagging_fraction", 0.5, 1.0),
            "bagging_freq":      trial.suggest_int("bagging_freq",      1, 10),
            "reg_alpha":         trial.suggest_float("reg_alpha",      0.0, 1.0),
            "reg_lambda":        trial.suggest_float("reg_lambda",     0.0, 1.0),
        }
        trial_config = {**config, "model": {**config["model"], **params}}

        try:
            forecaster = SKUForecaster(trial_config)
            result     = walk_forward_validate(df, forecaster, feature_cols, trial_config)
            return result.aggregated.get("wmape_median", 999.0)
        except Exception as e:
            logger.debug(f"HPO trial failed: {e}")
            return 999.0

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=5),
    )
    study.optimize(objective, n_trials=n_trials, timeout=timeout_sec, show_progress_bar=False)

    best = study.best_params
    best_val = study.best_value
    logger.info(f"HPO done: best WMAPE_median={best_val:.4f} params={best}")
    return best
