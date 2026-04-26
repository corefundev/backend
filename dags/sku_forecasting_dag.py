"""
dags/sku_forecasting_dag.py

Apache Airflow DAG — daily SKU forecasting pipeline.

Schedule: daily at 02:00 UTC (after data lands in S3).
Each task mirrors one step from ТЗ §7 pipeline шаги.

Tasks:
    1. load_and_validate       — pull data from S3, run GE checks
    2. build_features          — compute lag/rolling/calendar features, persist to S3
    3. train_model             — fit LightGBM on all data
    4. walk_forward_validate   — evaluate with WF CV, write metrics
    5. save_model              — upload model.pkl to S3 via ClientStorage
    6. generate_forecasts      — batch inference for horizon days
    7. save_predictions        — write predictions parquet to S3
    8. drift_check             — compute PSI vs training baseline, alert if drifted

Each dag_run is tagged with client_id so multiple clients run in parallel
as separate DagRuns (triggered by client-specific conf).

Usage:
    # Trigger for a specific client
    airflow dags trigger sku_forecasting \
        --conf '{"client_id": "acme", "data_path": "s3://bucket/acme/raw/data.parquet"}'
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

# ── Default args ──────────────────────────────────────────────────────────────
DEFAULT_ARGS = {
    "owner": "ml-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "email_on_failure": True,
    "email_on_retry": False,
    "depends_on_past": False,
}

CONFIG_PATH = "configs/config.yaml"


# ── Task functions ─────────────────────────────────────────────────────────────

def _get_client_conf(context: dict) -> tuple[str, str]:
    conf       = context["dag_run"].conf or {}
    client_id  = conf.get("client_id", "default")
    data_path  = conf.get("data_path", f"s3://sku-forecasting/{client_id}/raw/data.parquet")
    return client_id, data_path


def task_load_and_validate(**context) -> None:
    """Step 1+2 from ТЗ: load data → GE validate → clean → save to S3."""
    client_id, data_path = _get_client_conf(context)

    from src.data.loader import load_config, load_data, validate_data
    from src.data.ge_validator import validate_with_great_expectations
    from src.storage.backend import ClientStorage

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)

    df = load_data(data_path, config)
    validate_with_great_expectations(df, config, raise_on_failure=True)
    df = validate_data(df, config)
    storage.save_raw_data(df)

    context["ti"].xcom_push(key="n_rows",  value=len(df))
    context["ti"].xcom_push(key="n_skus",  value=df[config["data"]["sku_col"]].nunique())


def task_build_features(**context) -> None:
    """Step 3 from ТЗ: feature engineering → persist features to S3."""
    client_id, _ = _get_client_conf(context)

    from src.data.loader import load_config
    from src.features.engineering import build_features, get_feature_columns
    from src.storage.backend import ClientStorage

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)

    df = storage.load_raw_data()
    df = build_features(df, config)
    storage.save_features(df)

    feature_cols = get_feature_columns(df, config)
    context["ti"].xcom_push(key="n_features", value=len(feature_cols))
    context["ti"].xcom_push(key="n_rows",     value=len(df))


def task_walk_forward_validate(**context) -> None:
    """Step 5 from ТЗ: walk-forward CV → metrics per SKU."""
    client_id, _ = _get_client_conf(context)

    from src.data.loader import load_config
    from src.features.engineering import get_feature_columns
    from src.models.forecaster import SKUForecaster
    from src.storage.backend import ClientStorage
    from src.validation.walk_forward import walk_forward_validate

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)
    df      = storage.load_features()

    feature_cols = get_feature_columns(df, config)
    model        = SKUForecaster(config)
    wf_result    = walk_forward_validate(df, model, feature_cols, config)

    storage.save_per_sku_metrics(wf_result.per_sku_metrics)
    context["ti"].xcom_push(key="metrics", value=wf_result.aggregated)


def task_train_model(**context) -> None:
    """Step 4 from ТЗ: train final model on all data → upload to S3."""
    client_id, _ = _get_client_conf(context)

    from src.data.loader import load_config
    from src.features.engineering import get_feature_columns
    from src.models.forecaster import SKUForecaster, log_to_mlflow
    from src.models.fallback import SeasonalNaiveModel
    from src.storage.backend import ClientStorage

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)
    df      = storage.load_features()

    feature_cols = get_feature_columns(df, config)
    target_col   = config["data"]["target_col"]

    model = SKUForecaster(config)
    model.fit(df[feature_cols], df[target_col])
    storage.save_model(model)

    fallback = SeasonalNaiveModel()
    fallback.fit(df[target_col].values)
    storage.save_fallback_model(fallback)

    # Log to MLflow
    metrics = context["ti"].xcom_pull(task_ids="walk_forward_validate", key="metrics") or {}
    try:
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tmp:
            tmp_path = tmp.name
        model.save(tmp_path)
        log_to_mlflow(config, metrics, model, tmp_path, client_id)
        os.unlink(tmp_path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"MLflow logging failed: {e}")


def task_generate_forecasts(**context) -> None:
    """Steps 7+8 from ТЗ: batch inference → save predictions to S3."""
    client_id, data_path = _get_client_conf(context)

    from src.data.loader import load_config
    from src.pipeline.batch_inference import run_batch_inference
    from src.storage.backend import ClientStorage
    from datetime import date
    import tempfile, os

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)
    today   = date.today().isoformat()

    # Download model to temp file for batch_inference
    with tempfile.TemporaryDirectory() as tmp:
        model_local = os.path.join(tmp, "model.pkl")
        storage.backend.download(f"{client_id}/models/model.pkl", model_local)
        df = run_batch_inference(
            data_path=data_path,
            model_path=model_local,
            config_path=CONFIG_PATH,
            client_id=client_id,
        )

    storage.save_predictions(df, today)
    context["ti"].xcom_push(key="forecast_rows", value=len(df))


def task_drift_check(**context) -> None:
    """Step monitoring from ТЗ §10: PSI feature drift + prediction drift alert."""
    client_id, _ = _get_client_conf(context)

    from src.data.loader import load_config
    from src.features.engineering import get_feature_columns
    from src.monitoring.drift import check_and_save_drift
    from src.storage.backend import ClientStorage

    config  = load_config(CONFIG_PATH)
    storage = ClientStorage(client_id)

    train_df     = storage.load_features()
    feature_cols = get_feature_columns(train_df, config)

    # Use training data itself as "inference" for baseline smoke test
    # In production: load yesterday's inference batch for comparison
    result = check_and_save_drift(
        train_df=train_df,
        inference_df=train_df.tail(500),
        feature_cols=feature_cols,
        client_storage=storage,
        client_id=client_id,
    )
    context["ti"].xcom_push(key="drift_summary", value={
        k: v for k, v in result.items() if not isinstance(v, dict)
    })


# ── DAG definition ────────────────────────────────────────────────────────────

with DAG(
    dag_id="sku_forecasting",
    description="Daily SKU demand forecasting pipeline — all 8 steps from ТЗ §7",
    default_args=DEFAULT_ARGS,
    schedule_interval="0 2 * * *",   # 02:00 UTC daily
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=10,              # allow parallel client runs
    tags=["forecasting", "sku", "ml"],
    doc_md=__doc__,
) as dag:

    load_validate = PythonOperator(
        task_id="load_and_validate",
        python_callable=task_load_and_validate,
    )

    build_feats = PythonOperator(
        task_id="build_features",
        python_callable=task_build_features,
    )

    wf_validate = PythonOperator(
        task_id="walk_forward_validate",
        python_callable=task_walk_forward_validate,
    )

    train = PythonOperator(
        task_id="train_model",
        python_callable=task_train_model,
    )

    forecasts = PythonOperator(
        task_id="generate_forecasts",
        python_callable=task_generate_forecasts,
    )

    drift = PythonOperator(
        task_id="drift_check",
        python_callable=task_drift_check,
    )

    # Task dependencies — matches ТЗ §7 pipeline order
    load_validate >> build_feats >> wf_validate >> train >> forecasts >> drift
