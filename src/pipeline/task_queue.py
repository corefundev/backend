"""
src/pipeline/task_queue.py

Redis-backed task queue for async training and inference jobs.
Uses rq (Redis Queue) — lightweight, no Celery overhead.

Env vars:
    REDIS_URL   — default: redis://localhost:6379

Usage:
    # Enqueue a training job (from API)
    queue = get_queue()
    job = enqueue_training(queue, client_id="acme", data_path="s3://bucket/acme/raw/data.parquet")

    # Check job status
    status = get_job_status(job.id)
"""
from __future__ import annotations

# RQ workers import this module to find _training_job; main.py never
# runs in a worker process, so we bootstrap secrets here too. Same
# rationale as src/pipeline/upload_workers.py — see the comment there.
# Idempotent: bootstrap_secrets is safe to call multiple times.
import logging as _early_logging
_early_logger = _early_logging.getLogger("worker.bootstrap")
try:
    from src.auth.vault_agent import bootstrap_secrets as _bootstrap_secrets
    _bootstrap_secrets()
except Exception as _e:    # noqa: BLE001
    _early_logger.warning("worker bootstrap_secrets failed: %s", _e)

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


def get_redis_connection():
    """
    Return a Redis connection from the process-wide singleton pool.

    Audit R3-4 — earlier this constructed `Redis.from_url(...)` per call
    and pinged each time, burning a socket+RTT on every /predict and RQ
    poll. Now routes through `src.redis_pool` so connections are reused
    within the process. Behaviour unchanged: raises ConnectionError on
    failure, ImportError if `redis` package missing.
    """
    try:
        from src.redis_pool import get_redis
        return get_redis(ping=True)
    except ImportError:
        raise ImportError("redis package required. pip install redis")
    except Exception as e:
        raise ConnectionError(f"Cannot connect to Redis at {REDIS_URL}: {e}") from e


def get_queue(queue_name: str = "sku-training"):
    """Return an rq Queue. Raises ImportError if rq not installed."""
    try:
        from rq import Queue
        conn = get_redis_connection()
        return Queue(queue_name, connection=conn)
    except ImportError:
        raise ImportError("rq package required. pip install rq")


# ── Job functions (executed by rq worker) ─────────────────────────────────────

# ── RQ training-job orchestration: R5-M5 split (2026-05-17) ────────────────
#
# The previous monolithic `_training_job` was 190 LOC + 22 bare
# `except Exception` clauses, mixing five distinct concerns with
# inconsistent error-swallow semantics. Each concern now lives in
# its own helper with a clearly-scoped error contract:
#
#   _now_iso()                  — utc-iso timestamp for run rows.
#   _start_run()                — open training_runs row (RUNNING).
#   _resolve_data_path()        — merge extend-from datasets if needed.
#   _run_pipeline_or_fail()     — actual ML training + FAILED handler.
#   _record_run_finished()      — training_runs FINISHED + sku_clients=ready.
#   _notify_finished_idempotent() — email+telegram with Redis SETNX guard.
#   _post_training_artifacts()  — forecasts + anomalies (best-effort).
#
# The orchestrator below is now linear + obvious. Behaviour preserved
# exactly — same Redis keys, same DB writes, same notifications.


def _now_iso() -> str:
    """UTC ISO timestamp for training_runs row writes."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _start_run(run_id: Optional[str]):
    """Open the training_runs row in RUNNING state.

    Returns the registry handle (or None if run_id is unset OR the
    registry write failed — both are best-effort, the actual
    training proceeds regardless).
    """
    if not run_id:
        return None
    try:
        from src.storage.training_runs import get_training_runs_registry, RUNNING
        runs = get_training_runs_registry()
        runs.update(run_id, status=RUNNING, started_at=_now_iso())
        return runs
    except Exception as e:    # noqa: BLE001
        logger.warning("training_runs RUNNING update failed: %s", e)
        return None


def _mark_run_failed(runs, run_id: Optional[str], error: str) -> None:
    """Write training_runs FAILED + ended_at + error column.

    Best-effort: a registry-write failure must not mask the original
    pipeline error that's about to re-raise.
    """
    if runs is None or not run_id:
        return
    try:
        from src.storage.training_runs import FAILED
        runs.update(run_id, status=FAILED, ended_at=_now_iso(), error=error)
    except Exception as upd_err:    # noqa: BLE001 — audit R2-25
        logger.warning(
            "training_runs FAILED update failed for run_id=%s: %s",
            run_id, upd_err,
        )


def _resolve_data_path(
    data_path: str,
    extend_from_path: Optional[str],
    config_path: str,
    runs,
    run_id: Optional[str],
):
    """Resolve the effective parquet path the training pipeline reads.

    Returns `(effective_data_path, cleanup_path_or_None)`:
      - effective_data_path: what `run_training_pipeline` should read.
      - cleanup_path_or_None: temp file to unlink after training; None
        when no merge was performed.

    Failure during merge writes training_runs FAILED and re-raises so
    the orchestrator's outer behaviour stays "merge fail → job fail".
    """
    if not extend_from_path:
        return data_path, None
    try:
        merged = _merge_datasets(
            base_path=extend_from_path,
            new_path=data_path,
            config_path=config_path,
        )
        logger.info(
            "extend_from: merged %s + %s → %s",
            extend_from_path, data_path, merged,
        )
        return merged, merged
    except Exception as e:    # noqa: BLE001
        _mark_run_failed(runs, run_id, f"dataset merge failed: {e}")
        raise


def _run_pipeline_or_fail(
    client_id: str,
    effective_data_path: str,
    config_path: str,
    runs,
    run_id: Optional[str],
) -> dict:
    """Invoke `run_training_pipeline`; on exception, write FAILED to
    training_runs + flip sku_clients.status=failed + fire failure
    notifications, then re-raise.

    R5-3 (2026-05-17) — the sku_clients.status flip is the regression
    fix that ensures the client row doesn't stay stuck at "training"
    forever after an RQ-mode failure (the legacy code only updated
    training_runs).
    """
    from src.pipeline.train import run_training_pipeline
    try:
        return run_training_pipeline(
            data_path=effective_data_path,
            config_path=config_path,
            client_id=client_id,
        )
    except Exception as e:
        _mark_run_failed(runs, run_id, str(e))
        # R5-3 — sku_clients.status="failed" so UI/API can stop showing the
        # client as "training" after the RQ-mode failure.
        try:
            from src.clients.registry import get_registry as _get_registry
            _get_registry().update(client_id, status="failed")
        except Exception as st_err:    # noqa: BLE001
            logger.warning(
                "R5-3: sku_clients.status FAILED update failed for %s: %s",
                client_id, st_err,
            )
        # Fire failure notifications — both channels best-effort,
        # silently skip if user hasn't opted in.
        for kind, dotted in (
            ("email", "src.notifications.training_email"),
            ("telegram", "src.notifications.telegram"),
        ):
            try:
                module = __import__(dotted, fromlist=["notify_training_failed"])
                module.notify_training_failed(client_id=client_id, error=str(e))
            except Exception as notif_err:    # noqa: BLE001 — audit R2-25
                logger.info("notify_training_failed (%s) skipped: %s", kind, notif_err)
        raise


def _record_run_finished(
    runs,
    run_id: Optional[str],
    result: dict,
    client_id: str,
) -> None:
    """Write training_runs FINISHED + metrics + paths AND flip
    sku_clients.status="ready". Best-effort throughout — registry
    failures must not retro-fail a completed training run.
    """
    if runs is not None and run_id:
        try:
            from src.storage.training_runs import FINISHED
            metrics = result.get("metrics") or {}
            # Prefer the pooled "global" metrics over the per-SKU mean.
            # _mean treats every SKU equally and explodes when a few SKUs
            # have intermittent zero-actual periods (NaN-handling
            # asymmetry between per-fold and combined aggregations).
            # _global = sum_all(|err|) / sum_all(|actual|), monotonic in
            # fold metrics and matches what users see on a chart.
            runs.update(
                run_id,
                status=FINISHED,
                ended_at=_now_iso(),
                elapsed_sec=result.get("elapsed_sec"),
                n_skus=result.get("n_skus"),
                n_features=result.get("n_features"),
                n_rows=result.get("n_rows"),
                wmape=metrics.get("wmape_global", metrics.get("wmape_mean")),
                mase=metrics.get("mase_global",  metrics.get("mase_mean")),
                smape=metrics.get("smape_global", metrics.get("smape_mean")),
                model_path=result.get("model_path"),
                mlflow_run_id=result.get("mlflow_run_id"),
            )
        except Exception as e:    # noqa: BLE001
            logger.warning("training_runs FINISHED update failed: %s", e)

    # R5-3 — sku_clients.status="ready" so UI/API knows training is over.
    try:
        from src.clients.registry import get_registry as _get_registry
        _get_registry().update(client_id, status="ready")
    except Exception as st_err:    # noqa: BLE001
        logger.warning(
            "R5-3: sku_clients.status READY update failed for %s: %s",
            client_id, st_err,
        )


def _notify_finished_idempotent(
    client_id: str,
    run_id: Optional[str],
    result: dict,
) -> None:
    """Fire "training complete" email + telegram, guarded by a
    Redis SETNX idempotency flag on `notif:training:<run_id>` (7-day
    TTL).

    R5-5 (2026-05-17) — without this, RQ retries on worker SIGTERM /
    OOM mid-finalisation produce duplicate notifications. Guard
    failure falls through to send — duplicate is better than miss.
    No run_id → no guard (legacy jobs preserve pre-R5 behaviour).
    """
    metrics = result.get("metrics") or {}
    notif_args = dict(
        client_id=client_id,
        duration_sec=result.get("elapsed_sec"),
        n_skus=result.get("n_skus"),
        wmape=metrics.get("wmape_global", metrics.get("wmape_mean")),
        mase=metrics.get("mase_global",  metrics.get("mase_mean")),
    )

    should_notify = True
    if run_id:
        try:
            from src.redis_pool import get_redis_or_none
            r = get_redis_or_none()
            if r is not None:
                key = f"notif:training:{run_id}"
                first = r.set(key, "1", nx=True, ex=7 * 24 * 3600)
                if not first:
                    should_notify = False
                    logger.info(
                        "R5-5: notifications already sent for run_id=%s — skipping (RQ retry)",
                        run_id,
                    )
        except Exception as idem_err:    # noqa: BLE001
            logger.warning(
                "R5-5: idempotency guard failed (%s) — sending anyway", idem_err,
            )

    if not should_notify:
        return

    for kind, dotted in (
        ("email", "src.notifications.training_email"),
        ("telegram", "src.notifications.telegram"),
    ):
        try:
            module = __import__(dotted, fromlist=["notify_training_finished"])
            module.notify_training_finished(**notif_args)
        except Exception as notif_err:    # noqa: BLE001 — audit R2-25
            logger.info(
                "notify_training_finished (%s) skipped: %s", kind, notif_err,
            )


def _post_training_artifacts(
    client_id: str,
    effective_data_path: str,
    config_path: str,
    run_id: Optional[str],
    result: dict,
) -> None:
    """Generate batch forecasts + persist anomalies after a successful
    training. Both best-effort — failure here doesn't fail the run,
    the trained model is still saved and serveable.
    """
    try:
        _generate_and_store_forecasts(
            client_id=client_id,
            data_path=effective_data_path,
            model_path=result.get("model_path"),
            config_path=config_path,
            run_id=run_id or "",
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("post-training batch forecast failed: %s", e, exc_info=True)

    try:
        _detect_and_store_anomalies(
            client_id=client_id,
            data_path=effective_data_path,
            config_path=config_path,
            run_id=run_id or "",
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("post-training anomaly detection failed: %s", e, exc_info=True)


def _cleanup_merged(merged_cleanup: Optional[str]) -> None:
    """Best-effort unlink of the temp merged parquet (only set when
    `extend_from_path` triggered a dataset merge). Failure is silent
    — the next cron / OS tmp clean will catch it."""
    if not merged_cleanup:
        return
    try:
        import os as _os
        _os.unlink(merged_cleanup)
    except Exception:    # noqa: BLE001
        pass


def _training_job(
    client_id: str,
    data_path: str,
    config_path: str = "configs/config.yaml",
    storage_backend: str = "local",
    run_id: Optional[str] = None,
    extend_from_path: Optional[str] = None,
) -> dict:
    """
    Actual training work executed inside an rq worker.

    If `run_id` is supplied, lifecycle transitions are written to the
    training_runs registry so the API can serve a "training history"
    independent of RQ's 24h result_ttl.

    If `extend_from_path` is supplied, the worker concatenates the
    parquet at that path with the new dataset at `data_path`,
    deduplicates by (sku, date) preferring new values, and trains
    the model from scratch on the combined set. This is the
    "продолжить обучение свежими данными" flow.

    R5-M5 (2026-05-17) — orchestrator only; concerns split into
    named helpers above (see comment block before `_now_iso`).
    """
    import os
    os.environ.setdefault("STORAGE_BACKEND", storage_backend)

    runs = _start_run(run_id)

    effective_data_path, merged_cleanup = _resolve_data_path(
        data_path=data_path,
        extend_from_path=extend_from_path,
        config_path=config_path,
        runs=runs,
        run_id=run_id,
    )

    result = _run_pipeline_or_fail(
        client_id=client_id,
        effective_data_path=effective_data_path,
        config_path=config_path,
        runs=runs,
        run_id=run_id,
    )

    _record_run_finished(runs, run_id, result, client_id)
    _notify_finished_idempotent(client_id, run_id, result)
    _post_training_artifacts(client_id, effective_data_path, config_path, run_id, result)
    _cleanup_merged(merged_cleanup)

    return result


def _merge_datasets(
    base_path:   str,
    new_path:    str,
    config_path: str,
) -> str:
    """
    Concatenate two processed parquet files into a single dataset
    used for full retraining. Rules:
      - sort by (sku_col, date_col)
      - drop duplicate (sku, date) rows, keeping the value from the
        NEWER dataset (so re-sent days override older history)
      - written to /tmp as a fresh parquet; caller is responsible
        for cleanup once training reads it

    Both input paths can be local or s3:// — load_data already handles
    both. Schema mismatches (different sku/date column names) raise.
    """
    import pandas as _pd
    import tempfile, os as _os
    from src.data.loader import load_config, load_data

    config = load_config(config_path)
    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]

    base = load_data(base_path, config)
    new  = load_data(new_path,  config)

    for col in (sku_col, date_col):
        if col not in base.columns:
            raise ValueError(f"base dataset missing column {col!r}")
        if col not in new.columns:
            raise ValueError(f"new dataset missing column {col!r}")

    base[date_col] = _pd.to_datetime(base[date_col])
    new[date_col]  = _pd.to_datetime(new[date_col])

    # `new` last in the concat → keep='last' on duplicates retains
    # rows from the new dataset when the (sku, date) collides.
    combined = _pd.concat([base, new], ignore_index=True)
    before   = len(combined)
    combined = combined.drop_duplicates(subset=[sku_col, date_col], keep="last")
    combined = combined.sort_values([sku_col, date_col]).reset_index(drop=True)
    after    = len(combined)
    if before != after:
        logger.info("merge: removed %d duplicate (sku,date) rows", before - after)

    fd, out_path = tempfile.mkstemp(suffix=".parquet", prefix="merged-")
    _os.close(fd)
    combined.to_parquet(out_path, index=False)
    return out_path


def _generate_and_store_forecasts(
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

    from src.clients.registry import get_registry
    from src.clients.config_manager import get_config_manager
    from src.plans.plans import get_plan_spec
    from src.storage.forecasts import get_forecasts_registry
    from src.pipeline.inference_utils import forecast_all_skus
    from src.data.loader import load_data, validate_data
    from src.features.engineering import build_features, get_feature_columns
    from src.storage.backend import ClientStorage

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
    df = build_features(df, config)
    feature_cols = get_feature_columns(df, config)

    # Use the same load path the API does — ClientStorage.load_model
    # handles s3 fetch + pickle deserialisation in one step. The
    # earlier custom "download to /tmp + load_model_any_format" path
    # silently produced an UnpicklingError when the local file was
    # touched before the S3 download fully landed.
    storage = ClientStorage(client_id)
    if not storage.model_exists():
        logger.info(
            "skip post-training forecasts: model not found in storage for client=%s",
            client_id,
        )
        return
    model = storage.load_model()
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
    import pandas as _pd
    has_p10 = "p10" in forecasts.columns
    has_p90 = "p90" in forecasts.columns
    for _, r in forecasts.iterrows():
        d = r["date"]
        if hasattr(d, "strftime"):
            fdate = d.strftime("%Y-%m-%d")
        else:
            fdate = str(_pd.Timestamp(d).date())
        p10 = float(r["p10"]) if has_p10 and _pd.notna(r["p10"]) else None
        p90 = float(r["p90"]) if has_p90 and _pd.notna(r["p90"]) else None
        rows.append((str(r["sku"]), fdate, float(r["predicted_sales"]), p10, p90))

    get_forecasts_registry().replace_for_client(
        client_id=client_id, run_id=run_id, rows=rows,
    )
    logger.info(
        "post-training forecasts: client=%s sku=%d horizon=%d rows=%d",
        client_id, forecasts["sku"].nunique(), horizon, len(rows),
    )


def _detect_and_store_anomalies(
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
    from src.clients.registry import get_registry
    from src.clients.config_manager import get_config_manager
    from src.data.anomaly_detection import SalesAnomalyDetector
    from src.data.loader import load_data, validate_data
    from src.features.engineering import build_features
    from src.storage.anomalies import get_anomalies_registry
    import pandas as _pd

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

    cutoff = _pd.Timestamp(df[date_col].max()) - _pd.Timedelta(days=lookback_days)
    anomalies = anomalies[anomalies[date_col] >= cutoff]

    rows: list[tuple[str, str, float]] = []
    for _, r in anomalies.iterrows():
        d = r[date_col]
        if hasattr(d, "strftime"):
            adate = d.strftime("%Y-%m-%d")
        else:
            adate = str(_pd.Timestamp(d).date())
        rows.append((str(r[sku_col]), adate, float(r[target_col])))

    get_anomalies_registry().replace_for_client(
        client_id=client_id, run_id=run_id, rows=rows,
    )
    logger.info(
        "anomalies: client=%s last_%dd rows=%d",
        client_id, lookback_days, len(rows),
    )


def _batch_inference_job(
    client_id: str,
    data_path: str,
    config_path: str = "configs/config.yaml",
    output_date: str | None = None,
) -> dict:
    """Batch inference job executed inside an rq worker."""
    from datetime import date
    from src.pipeline.batch_inference import run_batch_inference
    from src.storage.backend import ClientStorage

    storage   = ClientStorage(client_id)
    out_date  = output_date or date.today().isoformat()
    model_key = f"{client_id}/models/model.pkl"

    if not storage.model_exists():
        raise FileNotFoundError(f"No model found for client {client_id}")

    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        local_model = os.path.join(tmp, "model.pkl")
        storage.backend.download(model_key, local_model)

        df = run_batch_inference(
            data_path=data_path,
            model_path=local_model,
            config_path=config_path,
            client_id=client_id,
        )

    storage.save_predictions(df, out_date)
    return {"client_id": client_id, "date": out_date, "n_rows": len(df)}


# ── Enqueue helpers (called from API) ─────────────────────────────────────────

def enqueue_training(
    client_id: str,
    data_path: str,
    config_path: str = "configs/config.yaml",
    storage_backend: str | None = None,
    timeout: int = 7200,          # 2 hours max
    run_id: Optional[str] = None,
    extend_from_path: Optional[str] = None,
) -> Optional[str]:
    """
    Enqueue a training job. Returns rq job_id.
    Falls back to synchronous execution if Redis is unavailable.

    `run_id` is the training_runs registry row created upstream. The
    worker uses it to update lifecycle status — see `_training_job`.

    `extend_from_path` is the parquet of a prior dataset that should
    be merged with `data_path` before training (full retrain on the
    union, deduped by sku+date with the new file winning on overlap).
    """
    backend = storage_backend or os.environ.get("STORAGE_BACKEND", "local")
    try:
        queue = get_queue("sku-training")
        job = queue.enqueue(
            _training_job,
            kwargs=dict(
                client_id=client_id,
                data_path=data_path,
                config_path=config_path,
                storage_backend=backend,
                run_id=run_id,
                extend_from_path=extend_from_path,
            ),
            job_timeout=timeout,
            result_ttl=86400,       # keep result 24h
            # Audit R3-1: stamp client_id so /jobs/{job_id} can enforce
            # ownership without an extra DB lookup. Read back via
            # job.meta["client_id"] in get_job_status / poll_job.
            meta={"client_id": client_id},
        )
        logger.info(f"Training job enqueued: job_id={job.id} client={client_id}")
        return job.id

    except Exception as e:
        logger.warning(f"Redis unavailable ({e}); running training synchronously")
        from src.pipeline.train import run_training_pipeline
        # Synchronous path: still want the row marked finished/failed.
        try:
            from src.storage.training_runs import (
                get_training_runs_registry, RUNNING, FINISHED, FAILED,
            )
            from datetime import datetime, timezone
            now = lambda: datetime.now(timezone.utc).isoformat()
            runs = get_training_runs_registry() if run_id else None
        except Exception:
            runs = None
        if runs is not None:
            try:
                runs.update(run_id, status=RUNNING, started_at=now())
            except Exception:
                pass
        try:
            result = run_training_pipeline(data_path, config_path, client_id)
        except Exception as exc:
            if runs is not None:
                try:
                    runs.update(run_id, status=FAILED, ended_at=now(), error=str(exc))
                except Exception:
                    pass
            raise
        if runs is not None:
            try:
                metrics = result.get("metrics") or {}
                runs.update(
                    run_id,
                    status=FINISHED,
                    ended_at=now(),
                    elapsed_sec=result.get("elapsed_sec"),
                    n_skus=result.get("n_skus"),
                    n_features=result.get("n_features"),
                    n_rows=result.get("n_rows"),
                    wmape=metrics.get("wmape_global", metrics.get("wmape_mean")),
                    mase=metrics.get("mase_global",  metrics.get("mase_mean")),
                    smape=metrics.get("smape_global", metrics.get("smape_mean")),
                    model_path=result.get("model_path"),
                    mlflow_run_id=result.get("mlflow_run_id"),
                )
            except Exception:
                pass
        return None


def get_job_status(job_id: str) -> dict:
    """
    Return status of a queued job.
    Status values: queued | started | finished | failed | unknown
    """
    from datetime import datetime, timezone

    def _to_iso_utc(dt) -> str | None:
        """
        RQ stores enqueued_at/started_at/ended_at as naive UTC datetimes.
        Without a TZ suffix the frontend's parseISO falls back to the
        browser's LOCAL time, so a 21:40 UTC start gets rendered as
        21:40 MSK (off by browser-offset hours). Force a +00:00 stamp so
        the client converts correctly.
        """
        if dt is None:
            return None
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        return str(dt)

    try:
        from rq.job import Job
        conn = get_redis_connection()
        job  = Job.fetch(job_id, connection=conn)
        # RQ 1.16+ returns get_status() as a plain str. The previous
        # `.value` call worked on older RQ that exposed an Enum; on the
        # current version it raises AttributeError("'str' object has no
        # attribute 'value'") and the UI showed every job as "Неизвестно
        # + Ошибка: 'str' object has no attribute 'value'" regardless of
        # whether the job actually succeeded. Be permissive here.
        raw_status = job.get_status()
        status_str = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
        # job.meta is written by src.pipeline.train._progress(...) before
        # each pipeline stage; surface it so the frontend can render a
        # progress bar.
        meta = getattr(job, "meta", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        progress = meta.get("progress")
        # Audit R3-1: client_id is stamped at enqueue time so the route
        # layer can authorise polls. Surfaced as `_client_id` (private
        # field — not part of the user-facing response shape).
        return {
            "job_id":     job_id,
            "status":     status_str,
            "result":     job.result if job.is_finished else None,
            "error":      str(job.exc_info) if job.is_failed else None,
            "enqueued":   _to_iso_utc(job.enqueued_at),
            "started":    _to_iso_utc(job.started_at),
            "ended":      _to_iso_utc(job.ended_at),
            "progress":   progress,
            "_client_id": meta.get("client_id"),
        }
    except Exception as e:
        return {"job_id": job_id, "status": "unknown", "error": str(e)}
