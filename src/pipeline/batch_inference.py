"""
src/pipeline/batch_inference.py

Daily batch forecast generation for all SKUs.
Uses shared inference_utils to avoid code duplication with API.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src.data.loader import load_data, validate_data
from src.features.engineering import build_features, get_feature_columns
from src.pipeline.inference_utils import (
    get_config,
    load_model_any_format,
    forecast_all_skus,
    serve_feature_set,
)

logger = logging.getLogger(__name__)


def _serving_config(config_path: str, client_id: str):
    """Модель-аудит H1: клиентский serving-конфиг; None — CLI/дев-путь
    без реестра (caller падает обратно на системный)."""
    try:
        from src.clients.config_manager import get_config_manager
        from src.clients.registry import get_registry
        return get_config_manager(config_path).get_effective_serving(
            client_id, get_registry())
    except Exception as e:    # noqa: BLE001 — фолбэк осознанный и логируемый
        logger.warning(f"serving config unavailable ({e}) — system config")
        return None


def run_batch_inference(
    data_path:   str,
    model_path:  str,
    config_path: str = "configs/config.yaml",
    client_id:   str = "default",
    output_path: str | None = None,
) -> pd.DataFrame:
    """
    Generate horizon-day forecasts for all SKUs.
    Returns DataFrame with columns: sku, date, predicted_sales, step.
    """
    # Модель-аудит H1: serving-конфиг клиента (override + план-дефолты),
    # иначе фичи разойдутся с feature_cols платной модели.
    config = _serving_config(config_path, client_id) or get_config(config_path)
    horizon = config["model"]["horizon"]

    logger.info(f"Batch inference: client={client_id}, horizon={horizon}d")

    df           = load_data(data_path, config)
    df           = validate_data(df, config)
    # R12-#100 — load the model first so build_features can pin the lag/
    # rolling set to what it was trained with (no frame-dependent drop).
    model  = load_model_any_format(model_path, config)
    _lags, _rw = serve_feature_set(model)
    df           = build_features(
        df, config, pin_lags=_lags or None, pin_rolling=_rw, drop_warmup=False,
    )
    # #229: market-колонки из хвоста модели (no-op для старых pickle)
    from src.features.market import apply_model_market
    df = apply_model_market(model, df, config["data"]["date_col"])
    from src.features.static_features import apply_model_static
    df = apply_model_static(model, df, config["data"]["sku_col"])
    feature_cols = get_feature_columns(df, config)
    # #570 PC-2: batch-CLI не несёт dataset_id (работает по data_path) —
    # календарь не подключаем (fail-open, нулевые promo_cal_*); боевые
    # прогнозы идут через post_training/serve, где события передаются.
    result = forecast_all_skus(model, df, feature_cols, config)

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        result.to_parquet(output_path, index=False)
        logger.info(f"Forecasts saved → {output_path}")

    logger.info(f"Batch inference done: {len(result)} forecast rows for {result['sku'].nunique()} SKUs")
    return result
