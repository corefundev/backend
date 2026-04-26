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
)

logger = logging.getLogger(__name__)


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
    config  = get_config(config_path)
    horizon = config["model"]["horizon"]

    logger.info(f"Batch inference: client={client_id}, horizon={horizon}d")

    df           = load_data(data_path, config)
    df           = validate_data(df, config)
    df           = build_features(df, config)
    feature_cols = get_feature_columns(df, config)

    model  = load_model_any_format(model_path, config)
    result = forecast_all_skus(model, df, feature_cols, config)

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        result.to_parquet(output_path, index=False)
        logger.info(f"Forecasts saved → {output_path}")

    logger.info(f"Batch inference done: {len(result)} forecast rows for {result['sku'].nunique()} SKUs")
    return result
