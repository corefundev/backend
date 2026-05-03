"""
src/models/online_learning.py

Online / incremental learning для LightGBM.

Стратегия:
  - Ежедневно: дообучить модель на последних N днях (partial_fit)
    через init_model — сохраняет знания старой модели
  - Еженедельно: полное переобучение (full retrain) с нуля
  - Отслеживать drift WMAPE: если растёт → принудительный full retrain

Экономия: ~90% времени при daily updates.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class IncrementalForecaster:
    """
    LightGBM с поддержкой partial fit через init_model.
    Хранит историю WMAPE для детектирования деградации.
    """

    def __init__(self, config: dict):
        self.config      = config
        # Set by fit() / partial_fit() / load(). LGBMRegressor at runtime
        # but typed Any so mypy lets predictors and partial_fit go through
        # without spurious "None has no attribute" errors at every call.
        self.model: Any  = None
        self.feature_cols: list[str] = []
        self._wmape_history: list[float] = []
        self._n_updates  = 0
        self._full_retrain_interval = config.get("online_learning", {}).get(
            "full_retrain_every_n_updates", 7
        )

    def _build_lgbm_params(self) -> dict:
        m = self.config["model"]
        return dict(
            n_estimators     = m.get("n_estimators", 300),
            learning_rate    = m.get("learning_rate", 0.05),
            num_leaves       = m.get("num_leaves", 64),
            min_child_samples= m.get("min_child_samples", 20),
            feature_fraction = m.get("feature_fraction", 0.8),
            bagging_fraction = m.get("bagging_fraction", 0.8),
            bagging_freq     = m.get("bagging_freq", 5),
            n_jobs=-1, verbose=-1,
        )

    # ── Initial full fit ──────────────────────────────────────

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "IncrementalForecaster":
        """Full initial training from scratch."""
        import lightgbm as lgb
        self.feature_cols = list(X.columns)
        self.model = lgb.LGBMRegressor(**self._build_lgbm_params())
        self.model.fit(X, y, callbacks=[lgb.log_evaluation(period=-1)])
        self._n_updates = 0
        logger.info(f"IncrementalForecaster: full fit on {len(X)} rows")
        return self

    # ── Incremental update ────────────────────────────────────

    def partial_fit(
        self,
        X_new: pd.DataFrame,
        y_new: pd.Series,
        n_new_estimators: int = 50,
    ) -> "IncrementalForecaster":
        """
        Add n_new_estimators trees on top of existing model.
        Uses LightGBM init_model — existing trees are preserved.
        Much faster than full retrain for daily updates.
        """
        import lightgbm as lgb

        if self.model is None:
            logger.warning("No model to update — doing full fit instead")
            return self.fit(X_new, y_new)

        # Check if full retrain is due
        self._n_updates += 1
        if self._n_updates % self._full_retrain_interval == 0:
            logger.info(
                f"IncrementalForecaster: scheduled full retrain "
                f"(update #{self._n_updates})"
            )
            return self.fit(X_new, y_new)

        # Incremental update via init_model
        params = self._build_lgbm_params()
        params["n_estimators"] = n_new_estimators

        new_model = lgb.LGBMRegressor(**params)
        new_model.fit(
            X_new[self.feature_cols],
            y_new,
            init_model = self.model,       # ← start from existing model
            callbacks  = [lgb.log_evaluation(period=-1)],
        )
        self.model = new_model
        logger.info(
            f"IncrementalForecaster: partial fit +{n_new_estimators} trees "
            f"on {len(X_new)} rows (update #{self._n_updates})"
        )
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Call fit() first")
        return np.clip(self.model.predict(X[self.feature_cols]), 0, None)

    # ── WMAPE tracking ────────────────────────────────────────

    def record_wmape(self, wmape: float) -> None:
        self._wmape_history.append(wmape)

    def is_degrading(self, window: int = 5, threshold_pct: float = 0.20) -> bool:
        """
        Returns True if recent WMAPE increased > threshold_pct vs window average.
        Triggers forced full retrain.
        """
        if len(self._wmape_history) < window + 1:
            return False
        recent  = np.mean(self._wmape_history[-window:])
        earlier = np.mean(self._wmape_history[-2 * window:-window])
        degrading = (recent - earlier) / (earlier + 1e-8) > threshold_pct
        if degrading:
            logger.warning(
                f"Model degradation detected: "
                f"recent WMAPE={recent:.3f} vs earlier={earlier:.3f} "
                f"(+{(recent-earlier)/earlier:.1%} > {threshold_pct:.0%} threshold)"
            )
        return bool(degrading)

    # ── Persistence ───────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model":         self.model,
                "feature_cols":  self.feature_cols,
                "wmape_history": self._wmape_history,
                "n_updates":     self._n_updates,
            }, f)

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "IncrementalForecaster":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls(config)
        obj.model            = state["model"]
        obj.feature_cols     = state["feature_cols"]
        obj._wmape_history   = state.get("wmape_history", [])
        obj._n_updates       = state.get("n_updates", 0)
        return obj


def run_incremental_update(
    client_id:    str,
    new_data_path: str,
    config_path:  str = "configs/config.yaml",
    n_new_days:   int = 14,
) -> dict:
    """
    High-level: load existing model, fit on latest N days, save back.
    Returns dict with update stats.
    """
    from src.data.loader import load_config, load_data, validate_data
    from src.features.engineering import build_features, get_feature_columns
    from src.storage.backend import ClientStorage
    from src.validation.metrics import wmape as compute_wmape
    import time

    t0     = time.perf_counter()
    config = load_config(config_path)
    storage = ClientStorage(client_id)

    # Load existing model
    try:
        existing = IncrementalForecaster.load(
            storage.path("models/incremental.pkl"), config
        )
    except Exception:
        logger.info(f"No incremental model for {client_id} — starting fresh")
        existing = IncrementalForecaster(config)

    # Load new data
    df = load_data(new_data_path, config)
    df = validate_data(df, config)
    df = build_features(df, config)
    fc = get_feature_columns(df, config)

    # Use only last n_new_days per SKU
    sku_col  = config["data"]["sku_col"]
    df_new   = df.groupby(sku_col).tail(n_new_days)

    X_new = df_new[fc]
    y_new = df_new[config["data"]["target_col"]]

    # Check if full retrain needed due to degradation
    if existing.is_degrading():
        existing.fit(X_new, y_new)
        update_type = "full_retrain"
    else:
        existing.partial_fit(X_new, y_new)
        update_type = "incremental"

    # Quick WMAPE estimate
    preds = existing.predict(X_new)
    current_wmape = float(compute_wmape(y_new.values, preds))
    existing.record_wmape(current_wmape)

    # Save updated model. ClientStorage delegates blob writes through
    # `.backend` (LocalStorageBackend / S3StorageBackend); save_pickle
    # lives on the backend with the HMAC-signed envelope wrapper.
    storage.backend.save_pickle(existing, f"{client_id}/models/incremental.pkl")

    elapsed = time.perf_counter() - t0
    logger.info(
        f"Incremental update done: client={client_id} "
        f"type={update_type} wmape={current_wmape:.3f} elapsed={elapsed:.1f}s"
    )
    return {
        "client_id":   client_id,
        "update_type": update_type,
        "wmape":       current_wmape,
        "n_rows":      len(X_new),
        "n_updates":   existing._n_updates,
        "elapsed_sec": elapsed,
    }
