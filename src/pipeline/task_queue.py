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
    """Return a Redis connection. Raises ImportError if redis not installed."""
    try:
        from redis import Redis
        from redis.exceptions import ConnectionError as RedisConnectionError
        conn = Redis.from_url(REDIS_URL, decode_responses=False, socket_connect_timeout=3)
        conn.ping()  # test connection
        return conn
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
    """
    import os
    from datetime import datetime, timezone
    os.environ.setdefault("STORAGE_BACKEND", storage_backend)

    def _now():
        return datetime.now(timezone.utc).isoformat()

    runs = None
    if run_id:
        try:
            from src.storage.training_runs import get_training_runs_registry, RUNNING
            runs = get_training_runs_registry()
            runs.update(run_id, status=RUNNING, started_at=_now())
        except Exception as e:    # noqa: BLE001
            logger.warning("training_runs RUNNING update failed: %s", e)

    # If asked to extend a prior dataset, build the merged file BEFORE
    # training; the rest of the pipeline only ever sees one path.
    effective_data_path = data_path
    merged_cleanup: Optional[str] = None
    if extend_from_path:
        try:
            effective_data_path = _merge_datasets(
                base_path=extend_from_path,
                new_path=data_path,
                config_path=config_path,
            )
            merged_cleanup = effective_data_path
            logger.info(
                "extend_from: merged %s + %s → %s",
                extend_from_path, data_path, effective_data_path,
            )
        except Exception as e:    # noqa: BLE001
            if runs is not None:
                try:
                    from src.storage.training_runs import FAILED
                    runs.update(
                        run_id, status=FAILED, ended_at=_now(),
                        error=f"dataset merge failed: {e}",
                    )
                except Exception:
                    pass
            raise

    from src.pipeline.train import run_training_pipeline
    try:
        result = run_training_pipeline(
            data_path=effective_data_path,
            config_path=config_path,
            client_id=client_id,
        )
    except Exception as e:
        if runs is not None:
            try:
                from src.storage.training_runs import FAILED
                runs.update(run_id, status=FAILED, ended_at=_now(), error=str(e))
            except Exception as upd_err:    # noqa: BLE001
                logger.warning("training_runs FAILED update failed: %s", upd_err)
        raise

    # Persist final metrics so the History tab can show them after
    # RQ has expired the job result.
    if runs is not None:
        try:
            from src.storage.training_runs import FINISHED
            metrics = result.get("metrics") or {}
            runs.update(
                run_id,
                status=FINISHED,
                ended_at=_now(),
                elapsed_sec=result.get("elapsed_sec"),
                n_skus=result.get("n_skus"),
                n_features=result.get("n_features"),
                n_rows=result.get("n_rows"),
                wmape=metrics.get("wmape_mean"),
                mase=metrics.get("mase_mean"),
                smape=metrics.get("smape_mean"),
                model_path=result.get("model_path"),
                mlflow_run_id=result.get("mlflow_run_id"),
            )
        except Exception as e:    # noqa: BLE001
            logger.warning("training_runs FINISHED update failed: %s", e)

    # ── Auto-batch-forecast for the whole catalogue ──────────────
    # The user shouldn't have to pick an SKU + horizon by hand after
    # training: we already know both. Generate forecasts for every
    # SKU over the plan's max_horizon_days and persist into
    # sku_forecasts. Best-effort — failure here doesn't fail the
    # training run, the model is still saved and trainable.
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

    # Tidy up the temp merged parquet now that both training and
    # post-training forecasts have read it.
    if merged_cleanup:
        try:
            import os as _os
            _os.unlink(merged_cleanup)
        except Exception:
            pass

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
    plan_max = get_plan_spec(record.plan if record else None).max_horizon_days

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
        model, df, feature_cols, config, horizon=horizon,
    )

    if forecasts.empty:
        logger.warning("forecast_all_skus returned 0 rows for client=%s", client_id)
        return

    # Normalise columns: forecast_all_skus returns sku/date/predicted_sales
    rows: list[tuple[str, str, float]] = []
    import pandas as _pd
    for _, r in forecasts.iterrows():
        d = r["date"]
        if hasattr(d, "strftime"):
            fdate = d.strftime("%Y-%m-%d")
        else:
            fdate = str(_pd.Timestamp(d).date())
        rows.append((str(r["sku"]), fdate, float(r["predicted_sales"])))

    get_forecasts_registry().replace_for_client(
        client_id=client_id, run_id=run_id, rows=rows,
    )
    logger.info(
        "post-training forecasts: client=%s sku=%d horizon=%d rows=%d",
        client_id, forecasts["sku"].nunique(), horizon, len(rows),
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
    from src.storage.backend import get_storage, ClientStorage

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
                    wmape=metrics.get("wmape_mean"),
                    mase=metrics.get("mase_mean"),
                    smape=metrics.get("smape_mean"),
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
        progress = meta.get("progress") if isinstance(meta, dict) else None
        return {
            "job_id":   job_id,
            "status":   status_str,
            "result":   job.result if job.is_finished else None,
            "error":    str(job.exc_info) if job.is_failed else None,
            "enqueued": str(job.enqueued_at),
            "started":  str(job.started_at),
            "ended":    str(job.ended_at),
            "progress": progress,
        }
    except Exception as e:
        return {"job_id": job_id, "status": "unknown", "error": str(e)}
