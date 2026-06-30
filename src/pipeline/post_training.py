"""
src/pipeline/post_training.py

Post-training artifact generation — runs AFTER a model is trained and saved:
  - generate_and_store_forecasts(): batch-forecast every (capped) SKU with the
    fresh model and persist into sku_forecasts for the forecast viewer.
  - detect_and_store_anomalies(): flag recent outlier sales days and persist
    into sku_anomalies for the "recent anomalies" panel.

Extracted from `task_queue.py` (ARCH-1, #205). These two functions are the
post-training *business logic* — pure load→features→model→store operations that
the RQ job invokes via `_post_training_artifacts`. They were the least
queue-coupled part of task_queue, carried its heaviest imports, and had no
direct unit coverage; pulling them here keeps task_queue an orchestration spine
and makes the forecast/anomaly generation independently testable.

Import discipline: this module is imported *locally* by task_queue (inside
`_post_training_artifacts`), so its heavy top-level imports (pandas, features,
inference_utils, …) load only on the worker path that actually runs them — the
API process, which only enqueues, never imports this module. That preserves the
lazy-load behaviour the scattered function-local imports used to provide, with
honest top-level imports here instead.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.clients.config_manager import get_config_manager
from src.clients.registry import get_registry
from src.data.anomaly_detection import SalesAnomalyDetector
from src.data.loader import load_data, validate_data
from src.features.engineering import build_features, get_feature_columns
from src.pipeline.inference_utils import forecast_all_skus, serve_feature_set
from src.plans.plans import get_plan_spec
from src.storage.anomalies import get_anomalies_registry
from src.storage.backend import ClientStorage
from src.storage.forecasts import get_forecasts_registry

logger = logging.getLogger(__name__)


def generate_and_store_forecasts(
    client_id:   str,
    data_path:   str,
    model_path:  str | None,
    config_path: str,
    run_id:      str,
) -> None:
    """
    Run forecast_all_skus for the just-trained model and persist into
    sku_forecasts. Horizon is taken from the client's plan, not from
    config.yaml, so Free/Start/Business each get their own ceiling.
    """
    if not model_path:
        logger.info("skip post-training forecasts: no model_path in result")
        return

    registry = get_registry()
    record   = registry.get(client_id)
    plan_spec = get_plan_spec(record.plan if record else None)
    plan_max  = plan_spec.max_horizon_days
    # SKU cap mirrors `assert_sku_count_within_limit` at train-enqueue.
    # Without it, batch inference would still loop over every SKU in
    # the user's dataset even though the model was only trained on
    # `plan_spec.max_skus` of them — 200 redundant recursive forecasts
    # for a Free user who uploaded 200 SKUs.
    plan_max_skus = plan_spec.max_skus

    # Effective config = system config + per-client overrides. The
    # user's chosen horizon (Settings → "Дней вперёд") lives there;
    # we cap it by the plan ceiling so a Free user can't sneak past
    # 7 days even if their config says 30.
    config = get_config_manager(config_path).get_effective(client_id, registry)
    user_horizon = int(config.get("model", {}).get("horizon", 0) or 0)

    if plan_max and plan_max > 0:
        horizon = min(user_horizon, plan_max) if user_horizon > 0 else plan_max
    else:
        horizon = user_horizon
    horizon = max(1, horizon)
    df = load_data(data_path, config)
    df = validate_data(df, config)

    # Use the same load path the API does — ClientStorage.load_model
    # handles s3 fetch + pickle deserialisation in one step. The
    # earlier custom "download to /tmp + load_model_any_format" path
    # silently produced an UnpicklingError when the local file was
    # touched before the S3 download fully landed.
    # R12-#100 — load BEFORE build_features so we can pin the lag/rolling
    # set to the model's trained one (no frame-dependent auto-drop); also
    # skips the feature build entirely when there's no model to serve.
    storage = ClientStorage(client_id)
    if not storage.model_exists():
        logger.info(
            "skip post-training forecasts: model not found in storage for client=%s",
            client_id,
        )
        return
    model = storage.load_model()
    _lags, _rw = serve_feature_set(model)
    df = build_features(
        df, config, pin_lags=_lags or None, pin_rolling=_rw, drop_warmup=False,
    )
    feature_cols = get_feature_columns(df, config)
    forecasts = forecast_all_skus(
        model, df, feature_cols, config,
        horizon=horizon,
        max_skus=plan_max_skus,
    )

    if forecasts.empty:
        logger.warning("forecast_all_skus returned 0 rows for client=%s", client_id)
        return

    # Normalise columns: forecast_all_skus returns sku/date/predicted_sales
    # plus p10/p90 (added in v0.8.26 for the confidence ribbon). Models
    # without quantile sub-models leave those columns as NaN/missing —
    # we surface that as None so the frontend can hide the ribbon.
    rows: list[tuple[str, str, float, "float | None", "float | None"]] = []
    has_p10 = "p10" in forecasts.columns
    has_p90 = "p90" in forecasts.columns
    for _, r in forecasts.iterrows():
        d = r["date"]
        if hasattr(d, "strftime"):
            fdate = d.strftime("%Y-%m-%d")
        else:
            fdate = str(pd.Timestamp(d).date())
        p10 = float(r["p10"]) if has_p10 and pd.notna(r["p10"]) else None
        p90 = float(r["p90"]) if has_p90 and pd.notna(r["p90"]) else None
        rows.append((str(r["sku"]), fdate, float(r["predicted_sales"]), p10, p90))

    get_forecasts_registry().replace_for_client(
        client_id=client_id, run_id=run_id, rows=rows,
    )
    logger.info(
        "post-training forecasts: client=%s sku=%d horizon=%d rows=%d",
        client_id, forecasts["sku"].nunique(), horizon, len(rows),
    )


def detect_and_store_anomalies(
    client_id:   str,
    data_path:   str,
    config_path: str,
    run_id:      str,
    lookback_days: int = 90,
) -> None:
    """
    Run SalesAnomalyDetector on the just-trained dataset and persist
    flagged (sku, date) rows from the last `lookback_days` into
    sku_anomalies. The forecast viewer reads from there to surface a
    "recent anomalies" panel — alongside the forecast for the same
    SKU it shows which past days were considered outliers, so the
    user can sanity-check whether the model learned through noise.
    """
    registry = get_registry()
    config = get_config_manager(config_path).get_effective(client_id, registry)
    sku_col    = config["data"]["sku_col"]
    date_col   = config["data"]["date_col"]
    target_col = config["data"]["target_col"]

    df = load_data(data_path, config)
    df = validate_data(df, config)
    # Build features so IsolationForest has lag/rolling cols available.
    df = build_features(df, config)

    detector = SalesAnomalyDetector()
    df_flagged, _ = detector.fit_detect(
        df, sku_col=sku_col, target_col=target_col, date_col=date_col,
    )

    anomalies = df_flagged[df_flagged["is_anomaly"] == True]   # noqa: E712
    if anomalies.empty:
        get_anomalies_registry().replace_for_client(
            client_id=client_id, run_id=run_id, rows=[],
        )
        logger.info("anomalies: none flagged for client=%s", client_id)
        return

    cutoff = pd.Timestamp(df[date_col].max()) - pd.Timedelta(days=lookback_days)
    anomalies = anomalies[anomalies[date_col] >= cutoff]

    rows: list[tuple[str, str, float]] = []
    for _, r in anomalies.iterrows():
        d = r[date_col]
        if hasattr(d, "strftime"):
            adate = d.strftime("%Y-%m-%d")
        else:
            adate = str(pd.Timestamp(d).date())
        rows.append((str(r[sku_col]), adate, float(r[target_col])))

    get_anomalies_registry().replace_for_client(
        client_id=client_id, run_id=run_id, rows=rows,
    )
    logger.info(
        "anomalies: client=%s last_%dd rows=%d",
        client_id, lookback_days, len(rows),
    )
