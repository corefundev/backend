"""
dags/auto_retrain_dag.py

Airflow DAG: automated drift-triggered retraining pipeline.

Schedule: hourly check, daily full retrain.
Triggers retraining when:
  - data drift (PSI) exceeds threshold
  - concept drift (MAE degradation) detected
  - cron schedule (daily 03:00 UTC)

On retraining:
  - validates new model beats old via validation gate
  - starts canary deployment (10% traffic)
  - promotes or rolls back after evaluation window
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.empty import EmptyOperator

DEFAULT_ARGS = {
    "owner":                  "ml-platform",
    "retries":                2,
    "retry_delay":            timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "email_on_failure":       True,
    "depends_on_past":        False,
}

CONFIG_PATH = "configs/config.yaml"


# ── Task functions ─────────────────────────────────────────────

def _get_conf(context: dict) -> tuple[str, str]:
    conf      = context["dag_run"].conf or {}
    client_id = conf.get("client_id", "default")
    data_path = conf.get("data_path", f"s3://sku-forecasting/{client_id}/raw/data.parquet")
    return client_id, data_path


def task_detect_drift(**context) -> str:
    """Check drift. Returns branch: 'trigger_retrain' or 'no_retrain'."""
    client_id, _ = _get_conf(context)

    from src.storage.backend import ClientStorage
    from src.monitoring.advanced import run_evidently_report, evaluate_retraining_need

    storage = ClientStorage(client_id)

    try:
        features_df  = storage.load_features()
        drift_result = run_evidently_report(
            features_df.head(1000),
            features_df.tail(500),
            [c for c in features_df.columns if not c.startswith("_")],
        )
        decision = evaluate_retraining_need(drift_result=drift_result)
        context["ti"].xcom_push(key="drift_result",   value=drift_result)
        context["ti"].xcom_push(key="should_retrain", value=decision.should_retrain)
        context["ti"].xcom_push(key="urgency",        value=decision.urgency)

        if decision.should_retrain:
            return "trigger_retrain"
        return "no_retrain"

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Drift check failed: {e}")
        return "no_retrain"


def task_trigger_retrain(**context) -> None:
    """Run auto-retraining pipeline."""
    client_id, data_path = _get_conf(context)
    urgency = context["ti"].xcom_pull(task_ids="detect_drift", key="urgency")

    from src.pipeline.auto_retrain import run_auto_retrain

    result = run_auto_retrain(
        client_id    = client_id,
        data_path    = data_path,
        config_path  = CONFIG_PATH,
        triggered_by = urgency or "drift",
        canary_pct   = 0.10,
    )
    context["ti"].xcom_push(key="retrain_result", value={
        "success":     result.success,
        "new_wmape":   result.new_metrics.get("wmape_mean"),
        "old_wmape":   result.old_metrics.get("wmape"),
        "promoted":    result.promoted,
        "elapsed_sec": result.elapsed_sec,
    })


def task_validate_and_promote(**context) -> None:
    """Validate new model via registry gate, promote if better."""
    client_id, _ = _get_conf(context)
    retrain_result = context["ti"].xcom_pull(task_ids="trigger_retrain", key="retrain_result") or {}

    new_wmape = retrain_result.get("new_wmape")
    if new_wmape is None:
        return

    from src.models.registry_gates import get_registry_gate
    gate = get_registry_gate()
    ok, reason = gate.validate_and_promote(
        run_id    = "latest",
        client_id = client_id,
        new_wmape = float(new_wmape),
    )
    context["ti"].xcom_push(key="promoted", value=ok)
    context["ti"].xcom_push(key="reason",   value=reason)


def task_start_canary(**context) -> None:
    """Start 10% canary traffic for new model."""
    client_id, _ = _get_conf(context)
    promoted = context["ti"].xcom_pull(task_ids="validate_and_promote", key="promoted")
    if not promoted:
        return

    from src.pipeline.auto_retrain import get_canary_router
    router = get_canary_router()
    router.start_canary(client_id, canary_pct=0.10)


def task_compute_business_metrics(**context) -> None:
    """Compute business metrics after retraining."""
    client_id, _ = _get_conf(context)

    from src.storage.backend import ClientStorage
    from src.monitoring.advanced import send_alert

    retrain_result = context["ti"].xcom_pull(
        task_ids="trigger_retrain", key="retrain_result"
    ) or {}

    if retrain_result.get("success"):
        new_wmape = retrain_result.get("new_wmape", 0)
        old_wmape = retrain_result.get("old_wmape", 0)
        improvement = (old_wmape - new_wmape) / (old_wmape + 1e-8) if old_wmape else 0
        msg = (
            f"Model retrained for {client_id}: "
            f"WMAPE {old_wmape:.3f} → {new_wmape:.3f} "
            f"({improvement:+.1%})"
        )
        send_alert(msg, level="info", client_id=client_id)

    context["ti"].xcom_push(key="business_metrics_done", value=True)


def task_send_alert(**context) -> None:
    """Send drift alert notification."""
    client_id, _ = _get_conf(context)
    drift_result  = context["ti"].xcom_pull(task_ids="detect_drift", key="drift_result") or {}

    from src.monitoring.advanced import send_alert
    drift_share = drift_result.get("drift_share", 0)
    send_alert(
        f"Data drift detected: {drift_share:.0%} of features drifted",
        level="warning", client_id=client_id,
    )


# ── DAG definition ─────────────────────────────────────────────

with DAG(
    dag_id           = "auto_retrain",
    description      = "Drift-triggered auto-retraining with canary deployment",
    default_args     = DEFAULT_ARGS,
    schedule_interval = "0 3 * * *",   # 03:00 UTC daily
    start_date       = datetime(2024, 1, 1),
    catchup          = False,
    max_active_runs  = 5,
    tags             = ["auto-retrain", "drift", "canary"],
) as dag:

    detect = BranchPythonOperator(
        task_id="detect_drift",
        python_callable=task_detect_drift,
    )

    alert = PythonOperator(
        task_id="send_alert",
        python_callable=task_send_alert,
    )

    retrain = PythonOperator(
        task_id="trigger_retrain",
        python_callable=task_trigger_retrain,
    )

    validate = PythonOperator(
        task_id="validate_and_promote",
        python_callable=task_validate_and_promote,
    )

    canary = PythonOperator(
        task_id="start_canary",
        python_callable=task_start_canary,
    )

    biz_metrics = PythonOperator(
        task_id="compute_business_metrics",
        python_callable=task_compute_business_metrics,
    )

    no_retrain = EmptyOperator(task_id="no_retrain")
    done       = EmptyOperator(task_id="done", trigger_rule="none_failed_min_one_success")

    detect >> [alert, no_retrain]
    alert  >> retrain >> validate >> canary >> biz_metrics >> done
    no_retrain >> done
