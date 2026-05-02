"""
src/storage/training_runs.py

Persistent log of training runs. One row per /clients/{id}/train invocation
— survives RQ's 24h result_ttl so the user can see history months later.

Lifecycle (state machine):
    queued ─► running ─► finished
                   └──► failed
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


QUEUED   = "queued"
RUNNING  = "running"
FINISHED = "finished"
FAILED   = "failed"

ALL_STATUSES = {QUEUED, RUNNING, FINISHED, FAILED}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return uuid.uuid4().hex


@dataclass
class TrainingRunRecord:
    run_id:        str
    client_id:     str
    plan:          str
    data_path:     str
    status:        str = QUEUED
    job_id:        Optional[str]   = None
    upload_id:     Optional[str]   = None
    enqueued_at:   str             = field(default_factory=_now_iso)
    started_at:    Optional[str]   = None
    ended_at:      Optional[str]   = None
    elapsed_sec:   Optional[float] = None
    n_skus:        Optional[int]   = None
    n_features:    Optional[int]   = None
    n_rows:        Optional[int]   = None
    wmape:         Optional[float] = None
    mase:          Optional[float] = None
    smape:         Optional[float] = None
    model_path:    Optional[str]   = None
    mlflow_run_id: Optional[str]   = None
    error:         Optional[str]   = None


class PostgresTrainingRunsRegistry:
    DDL = """
    CREATE TABLE IF NOT EXISTS sku_training_runs (
        run_id          TEXT PRIMARY KEY,
        client_id       TEXT NOT NULL,
        plan            TEXT NOT NULL,
        data_path       TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'queued',
        job_id          TEXT,
        upload_id       TEXT,
        enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at      TIMESTAMPTZ,
        ended_at        TIMESTAMPTZ,
        elapsed_sec     DOUBLE PRECISION,
        n_skus          INTEGER,
        n_features      INTEGER,
        n_rows          BIGINT,
        wmape           DOUBLE PRECISION,
        mase            DOUBLE PRECISION,
        smape           DOUBLE PRECISION,
        model_path      TEXT,
        mlflow_run_id   TEXT,
        error           TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sku_training_runs_client
        ON sku_training_runs (client_id, enqueued_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sku_training_runs_status
        ON sku_training_runs (status);
    """

    _UPDATABLE_COLUMNS = {
        "status", "job_id", "started_at", "ended_at", "elapsed_sec",
        "n_skus", "n_features", "n_rows",
        "wmape", "mase", "smape",
        "model_path", "mlflow_run_id", "error",
    }

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras   = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required") from e
        self._url = database_url
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(self.DDL)
            conn.commit()
        logger.info("sku_training_runs table ready")

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def create(self, record: TrainingRunRecord) -> None:
        sql = """
        INSERT INTO sku_training_runs
            (run_id, client_id, plan, data_path, status,
             job_id, upload_id, enqueued_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    record.run_id, record.client_id, record.plan,
                    record.data_path, record.status,
                    record.job_id, record.upload_id, record.enqueued_at,
                ))
            conn.commit()

    def update(self, run_id: str, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - self._UPDATABLE_COLUMNS
        if bad:
            raise ValueError(f"Cannot update columns: {bad}")
        if "status" in fields and fields["status"] not in ALL_STATUSES:
            raise ValueError(f"Unknown status: {fields['status']!r}")

        set_parts = [f"{k} = %s" for k in fields]
        values    = list(fields.values()) + [run_id]
        sql = f"UPDATE sku_training_runs SET {', '.join(set_parts)} WHERE run_id = %s"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, values)
                if cur.rowcount == 0:
                    logger.warning("update for unknown run_id %s", run_id)
            conn.commit()

    def get(self, run_id: str) -> Optional[TrainingRunRecord]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM sku_training_runs WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
        return None if row is None else self._row_to_record(dict(row))

    def reap_stale_runs(self, stale_after_minutes: int = 240) -> int:
        """
        Mark `running` and `queued` rows older than `stale_after_minutes`
        as failed. Called at API startup.

        Why: workers can die mid-run (container redeploy, OOM, RQ timeout)
        before the in-process FAILED update fires. The DB row stays in
        `running` forever, the frontend's resume-active-job effect picks
        it up on every load and re-shows the red error frame even after
        the user dismisses it. A periodic reap during boot is the
        cheapest source of truth: any run that hasn't progressed in 4+
        hours is dead.
        """
        sql = """
        UPDATE sku_training_runs
           SET status      = 'failed',
               ended_at    = NOW(),
               error       = COALESCE(error,
                             'Worker died before completion (auto-reaped at startup)')
         WHERE status IN ('running', 'queued')
           AND enqueued_at < NOW() - (%s || ' minutes')::interval
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (str(stale_after_minutes),))
                affected = cur.rowcount
            conn.commit()
        if affected:
            logger.info(
                "reap_stale_runs: marked %d stale run(s) as failed (older than %d min)",
                affected, stale_after_minutes,
            )
        return affected

    def list_for_client(self, client_id: str, limit: int = 50) -> list[TrainingRunRecord]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_training_runs WHERE client_id = %s "
                    "ORDER BY enqueued_at DESC LIMIT %s",
                    (client_id, limit),
                )
                rows = cur.fetchall()
        return [self._row_to_record(dict(r)) for r in rows]

    @staticmethod
    def _row_to_record(row: dict) -> TrainingRunRecord:
        def _iso(v):
            return None if v is None else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        return TrainingRunRecord(
            run_id=row["run_id"],
            client_id=row["client_id"],
            plan=row["plan"],
            data_path=row["data_path"],
            status=row["status"],
            job_id=row.get("job_id"),
            upload_id=row.get("upload_id"),
            enqueued_at=_iso(row.get("enqueued_at")) or "",
            started_at=_iso(row.get("started_at")),
            ended_at=_iso(row.get("ended_at")),
            elapsed_sec=row.get("elapsed_sec"),
            n_skus=row.get("n_skus"),
            n_features=row.get("n_features"),
            n_rows=row.get("n_rows"),
            wmape=row.get("wmape"),
            mase=row.get("mase"),
            smape=row.get("smape"),
            model_path=row.get("model_path"),
            mlflow_run_id=row.get("mlflow_run_id"),
            error=row.get("error"),
        )


_singleton: Optional[PostgresTrainingRunsRegistry] = None


def get_training_runs_registry() -> PostgresTrainingRunsRegistry:
    """
    Returns the singleton registry. Postgres is required — there is no
    JSON-file fallback because (a) we always run with Postgres in prod
    and (b) lifecycle updates from the worker process need a shared
    backing store, which a per-process JSON file can't provide.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set — training runs registry requires Postgres"
        )
    _singleton = PostgresTrainingRunsRegistry(db_url)
    return _singleton


def to_dict(rec: TrainingRunRecord) -> dict:
    return asdict(rec)
