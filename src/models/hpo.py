"""
src/models/hpo.py

Hyperparameter optimisation with Optuna (TPE sampler).

Tunes the architecture that actually runs in production. The HPO objective
fits a MIMOForecaster (each ensemble child IS a MIMO) and validates with
walk-forward direct multi-step. This is closer to honest than the v1
behaviour, which tuned a single-step LightGBM and copied the parameters
into ensemble children — choosing 1000 trees for a 1-day problem
catastrophically overfit the harder h=14 problem.

Cost knob: HPO trials use a SHORTER horizon (`hpo_trial_horizon`, default
5) than the full production horizon. Tuning at h=5 retains the multi-step
character (no recurrent simplification) but cuts trial cost ~3× compared
to h=14, keeping Start tier under ~10 min and Business tier under ~20 min
of HPO time. The chosen hyperparameters generalise from h=5 to h=14 well
because LightGBM regularisation knobs (n_estimators, num_leaves, min_child_
samples) are roughly horizon-invariant — what they prevent is feature-
interaction overfitting, which is similar at h=5 and h=14.

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
    Falls back to an empty dict (= keep config defaults) if Optuna isn't
    installed.
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        logger.warning("optuna not installed — skipping HPO. pip install optuna")
        return {}

    from src.validation.walk_forward import walk_forward_validate
    from src.models.mimo import MIMOForecaster

    # Cap trial horizon. The full-horizon production model picks up
    # the regularisation choices fine.
    full_horizon  = int(config["model"].get("horizon", 14))
    trial_horizon = int(config.get("hpo", {}).get("trial_horizon", min(full_horizon, 5)))

    # Search ranges — narrower than v1 to discourage the n_estimators=1000 +
    # learning_rate=0.13 corner that overfit single-step targets. Direct
    # multi-step needs more regularisation to fit a noisier signal.
    def objective(trial: "optuna.Trial") -> float:
        params = {
            "n_estimators":      trial.suggest_int("n_estimators",     200, 600, step=100),
            "learning_rate":     trial.suggest_float("learning_rate",  0.02, 0.10, log=True),
            "num_leaves":        trial.suggest_int("num_leaves",        31, 96),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 60),
            "feature_fraction":  trial.suggest_float("feature_fraction", 0.6, 1.0),
            "bagging_fraction":  trial.suggest_float("bagging_fraction", 0.6, 1.0),
            "bagging_freq":      trial.suggest_int("bagging_freq",      1, 10),
            "reg_alpha":         trial.suggest_float("reg_alpha",       0.0, 1.0),
            "reg_lambda":        trial.suggest_float("reg_lambda",      0.0, 1.0),
        }
        trial_config = {
            **config,
            "model": {
                **config["model"],
                **params,
                "horizon": trial_horizon,
                "type":    "mimo",
            },
        }

        # Pick the primary child's objective for tuning when an
        # ensemble is requested. Tweedie behaves most like the
        # blended model on retail demand; tuning on it gives the
        # closest sane proxy for the full ensemble.
        primary_obj = "tweedie" if config["model"].get("objective") == "ensemble" else config["model"].get("objective", "mse")
        trial_config["model"]["objective"] = primary_obj

        try:
            factory = lambda: MIMOForecaster(trial_config)
            result  = walk_forward_validate(df, factory, feature_cols, trial_config)
            # Prefer pooled-global WMAPE: per-SKU mean blows up when a
            # few SKUs have intermittent demand in the test window.
            agg = result.aggregated
            return float(
                agg.get("wmape_global", agg.get("wmape_median", 999.0))
            )
        except Exception as e:    # noqa: BLE001
            logger.debug(f"HPO trial failed: {e}")
            return 999.0

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=5),
    )
    study.optimize(
        objective,
        n_trials=n_trials,
        timeout=timeout_sec,
        show_progress_bar=False,
    )

    best     = study.best_params
    best_val = study.best_value
    logger.info(
        f"HPO done: best WMAPE_global={best_val:.4f} "
        f"(tuned at horizon={trial_horizon}) params={best}"
    )
    return best
