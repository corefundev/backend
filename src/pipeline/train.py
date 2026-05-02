"""
src/pipeline/train.py

End-to-end training pipeline v2.0:
  load → GE validate → anomaly detection → features (+ weather + holidays)
  → HPO (optional) → MIMO / LightGBM train → walk-forward validate
  → SHAP explainer → cold-start model → fallback → save (S3/local) → MLflow
"""
from __future__ import annotations

import logging
import sys
import time


from src.data.loader import load_config, load_data, validate_data
from src.data.ge_validator import validate_with_great_expectations
from src.clients.config_manager import get_config_manager
from src.data.anomaly_detection import SalesAnomalyDetector
from src.features.engineering import build_features, get_feature_columns
from src.models.forecaster import SKUForecaster, log_to_mlflow
from src.models.mimo import MIMOForecaster
from src.models.fallback import SeasonalNaiveModel
from src.models.cold_start import ColdStartRouter, ClusterBasedForecaster
from src.models.explainer import SKUExplainer
from src.storage.backend import ClientStorage, get_storage
from src.validation.walk_forward import walk_forward_validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _progress(step: int, total: int, label: str) -> None:
    """
    Publish progress to the current RQ job's meta dict so the API can
    surface it via /jobs/{id}. Safe to call when run outside a worker
    (e.g., synchronous fallback path) — get_current_job() returns None.
    """
    logger.info(f"Step {step}/{total}: {label}")
    try:
        from rq import get_current_job
        job = get_current_job()
        if job is None:
            return
        job.meta["progress"] = {"step": step, "total": total, "label": label}
        job.save_meta()
    except Exception as e:    # noqa: BLE001
        logger.debug(f"_progress save_meta skipped: {e}")


def run_training_pipeline(
    data_path: str,
    config_path: str = "configs/config.yaml",
    client_id:  str = "default",
    output_dir: str | None = None,
) -> dict:
    t0 = time.time()
    logger.info(f"=== Training pipeline START | client={client_id} ===")

    # ── Load system config ───────────────────────────────────
    config = load_config(config_path)

    # ── Apply per-client config overrides ────────────────────
    # Client config is merged ON TOP of system config.
    # System config.yaml is never modified.
    user_set_hpo = False
    user_set_objective = False
    try:
        from src.clients.registry import get_registry
        registry = get_registry()
        mgr      = get_config_manager(config_path)
        record   = registry.get(client_id)
        # Track explicit user choices so plan-tier defaults below
        # don't overwrite them.
        client_cfg = (record.config if record else None) or {}
        user_set_hpo = "hpo" in client_cfg
        user_set_objective = "objective" in (client_cfg.get("model") or {})
        config   = mgr.get_effective(client_id, registry)
        logger.info(f"Config for client={client_id}: applied per-client overrides")
    except Exception as e:
        logger.warning(f"Could not load per-client config overrides: {e}. Using system defaults.")
        record = None

    # ── Plan-tier defaults: HPO + objective ──────────────────
    # Each plan has its own HPO budget (Free 0 / Start 15 / Business 30)
    # and its own default loss objective (Free→mse / Start, Business→
    # tweedie). Applied only when the user hasn't explicitly set the
    # corresponding override — explicit user choice always wins.
    try:
        from src.plans.plans import get_plan_spec
        spec = get_plan_spec(record.plan if record else None)
        if not user_set_hpo:
            config.setdefault("hpo", {})
            config["hpo"]["enabled"]  = spec.hpo_n_trials > 0
            config["hpo"]["n_trials"] = spec.hpo_n_trials
        if not user_set_objective:
            config.setdefault("model", {})
            config["model"]["objective"] = spec.default_objective
        logger.info(
            f"Plan-tier defaults: plan={record.plan if record else 'free'} "
            f"hpo_n_trials={config['hpo'].get('n_trials', 0)} "
            f"objective={config['model'].get('objective', 'mse')}"
        )
    except Exception as e:    # noqa: BLE001
        logger.warning(f"Could not apply plan defaults: {e}")

    storage = ClientStorage(client_id)
    logger.info(f"Storage: {storage.backend.__class__.__name__} → {storage.path('models/model.pkl')}")

    # ── 1. Load ───────────────────────────────────────────────
    _progress(1, 9, "Загрузка данных")
    df = load_data(data_path, config)

    # ── 2. GE Validation ──────────────────────────────────────
    _progress(2, 9, "Проверка качества (Great Expectations)")
    ge_result = validate_with_great_expectations(df, config, raise_on_failure=True)
    df = validate_data(df, config)
    storage.save_raw_data(df)

    # ── 3. Anomaly detection ──────────────────────────────────
    _progress(3, 9, "Поиск аномалий")
    sample_weights = None
    anom_cfg = config.get("anomaly_detection", {})
    if anom_cfg.get("enabled", True):
        detector = SalesAnomalyDetector(
            contamination=anom_cfg.get("contamination", 0.05),
            iqr_factor=anom_cfg.get("iqr_factor", 3.0),
            anomaly_weight=anom_cfg.get("anomaly_weight", 0.1),
        )
        df, sample_weights_full = detector.fit_detect(
            df,
            sku_col=config["data"]["sku_col"],
            target_col=config["data"]["target_col"],
        )

    # ── 4. Feature engineering ────────────────────────────────
    _progress(4, 9, "Построение признаков (календарь, лаги, погода, праздники)")
    df = build_features(df, config)
    feature_cols = get_feature_columns(df, config)
    storage.save_features(df)
    logger.info(f"  {len(feature_cols)} features, {len(df)} rows")

    target_col = config["data"]["target_col"]
    sku_col    = config["data"]["sku_col"]
    X = df[feature_cols]
    y = df[target_col]

    # Align sample weights to feature-engineered rows
    if anom_cfg.get("enabled", True) and "is_anomaly" in df.columns:
        sample_weights = sample_weights_full[df.index] if hasattr(sample_weights_full, '__getitem__') else None

    # ── 5. HPO (optional) ─────────────────────────────────────
    hpo_cfg = config.get("hpo", {})
    if hpo_cfg.get("enabled", False):
        _progress(5, 9, "Подбор гиперпараметров (Optuna)")
        from src.models.hpo import run_hpo
        best_params = run_hpo(df, feature_cols, config, hpo_cfg.get("n_trials", 30))
        if best_params:
            config["model"].update(best_params)
            logger.info(f"  HPO best params: {best_params}")
    else:
        _progress(5, 9, "HPO пропущен")

    # ── 6. Walk-forward validation ────────────────────────────
    # The validator class MIRRORS the final production model class so
    # the metrics reported to the user are honest — the same prediction
    # mechanism that /app/forecasts will use is what gets graded here.
    # Walk-forward picks direct-multi-step mode automatically when the
    # model exposes the is_mimo / is_ensemble marker.
    #
    # A factory (lambda) gives each fold a fresh instance: stale state
    # from prior folds shouldn't leak into new ones.
    _progress(6, 9, "Walk-forward валидация")
    objective_cfg = str(config.get("model", {}).get("objective", "")).lower()
    if objective_cfg == "ensemble":
        from src.models.ensemble import EnsembleForecaster
        val_factory = lambda: EnsembleForecaster(config)
        val_label = "ensemble"
    elif config["model"].get("type", "lgbm") == "mimo":
        val_factory = lambda: MIMOForecaster(config)
        val_label = "mimo"
    else:
        val_factory = lambda: SKUForecaster(config)
        val_label = "lgbm-baseline"
    logger.info(f"  Walk-forward validator class: {val_label}")
    wf_result = walk_forward_validate(df, val_factory, feature_cols, config)
    agg = wf_result.aggregated
    logger.info(f"  WMAPE={agg.get('wmape_mean',0):.3f} MASE={agg.get('mase_mean',0):.3f}")
    storage.save_per_sku_metrics(wf_result.per_sku_metrics)

    # ── 7. Train final model ──────────────────────────────────
    _progress(7, 9, "Обучение финальной модели")
    model_type = config["model"].get("type", "lgbm")
    objective  = str(config.get("model", {}).get("objective", "")).lower()
    is_ensemble = objective == "ensemble"

    if is_ensemble:
        # 3 MIMO children with different objectives, blended per SKU.
        # Adds ~3× training time and memory for a meaningful per-SKU
        # accuracy gain on mixed catalogs.
        from src.models.ensemble import EnsembleForecaster
        final_model = EnsembleForecaster(config)
        final_model.fit(X, y, groups=df[sku_col])
        final_model.fit_quantiles(X, y, groups=df[sku_col])
        # Estimate per-SKU mixing weights from the most recent
        # window of training data. Needs the SKU + date columns
        # alongside the targets, so we pass df rather than X.
        final_model.compute_blend_weights(
            df_full=df,
            sku_col=sku_col,
            date_col=config["data"]["date_col"],
            target_col=target_col,
            lookback_days=int(
                config.get("model", {}).get("ensemble_lookback_days", 28)
            ),
        )
        logger.info("  Ensemble (Tweedie+MAE+MSE) + quantile models fitted")
    elif model_type == "mimo":
        final_model = MIMOForecaster(config)
        final_model.fit(X, y, groups=df[sku_col])
        final_model.fit_quantiles(X, y, groups=df[sku_col])
        logger.info("  MIMO model + quantile models fitted")
    else:
        final_model = SKUForecaster(config)
        final_model.fit(X, y)

    # ── 8. Cold-start model ───────────────────────────────────
    cs_cfg  = config.get("cold_start", {})
    router  = ColdStartRouter(min_history_days=cs_cfg.get("min_history_days", 28))
    cluster = ClusterBasedForecaster(n_neighbors=cs_cfg.get("n_neighbors", 5))
    cluster.fit(df, sku_col, target_col)

    # ── 9. SHAP explainer ─────────────────────────────────────
    if is_ensemble:
        # Use the primary (first) child's first MIMO model for SHAP.
        primary = final_model.models_[final_model.primary_objective]
        lgbm_inner = primary.models_[0] if primary.models_ else None
        explainer  = SKUExplainer(lgbm_inner, feature_cols) if lgbm_inner else None
    elif model_type == "lgbm":
        lgbm_inner = getattr(final_model, "model", None)
        explainer  = SKUExplainer(lgbm_inner, feature_cols) if lgbm_inner else None
    else:
        # Use first MIMO model for SHAP
        lgbm_inner = final_model.models_[0] if final_model.models_ else None
        explainer  = SKUExplainer(lgbm_inner, feature_cols) if lgbm_inner else None

    # ── Save fallback ─────────────────────────────────────────
    fallback = SeasonalNaiveModel(seasonality=7)
    fallback.fit(y.values)
    storage.save_fallback_model(fallback)

    # ── Save primary model ────────────────────────────────────
    model_path = storage.save_model(final_model)
    logger.info(f"  Saved → {model_path}")

    # ── 8. Cold-start + SHAP done above; mark step 8 ──────────
    _progress(8, 9, "Cold-start модель + SHAP объяснения")

    # ── MLflow logging ────────────────────────────────────────
    _progress(9, 9, "Запись эксперимента в MLflow")
    run_id = None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp:
            tmp_path = tmp.name
        if hasattr(final_model, "save"):
            final_model.save(tmp_path)
            run_id = log_to_mlflow(config, agg, final_model, tmp_path, client_id)
        import os as _os
        _os.unlink(tmp_path)
    except Exception as e:
        logger.warning(f"MLflow logging failed: {e}")

    elapsed = time.time() - t0
    logger.info(f"=== Training pipeline DONE in {elapsed:.1f}s ===")

    return {
        "client_id":       client_id,
        "model_path":      model_path,
        "model_type":      model_type,
        "storage_backend": storage.backend.__class__.__name__,
        "metrics":         agg,
        "mlflow_run_id":   run_id,
        "n_skus":          df[sku_col].nunique(),
        "n_features":      len(feature_cols),
        "n_rows":          len(df),
        "elapsed_sec":     elapsed,
        "ge_stats":        ge_result.statistics,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",   required=True)
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--client", default="default")
    args = parser.parse_args()
    result = run_training_pipeline(args.data, args.config, args.client)
    import json
    logger.info("Pipeline result: " + json.dumps({k: str(v) for k,v in result.items()}, default=str))
