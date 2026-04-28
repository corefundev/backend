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
) -> dict:
    """
    Actual training work executed inside an rq worker.

    If `run_id` is supplied, lifecycle transitions are written to the
    training_runs registry so the API can serve a "training history"
    independent of RQ's 24h result_ttl.
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

    from src.pipeline.train import run_training_pipeline
    try:
        result = run_training_pipeline(
            data_path=data_path,
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
    return result


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
) -> Optional[str]:
    """
    Enqueue a training job. Returns rq job_id.
    Falls back to synchronous execution if Redis is unavailable.

    `run_id` is the training_runs registry row created upstream. The
    worker uses it to update lifecycle status — see `_training_job`.
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
