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
    # DS-1 #466: происхождение — на какой версии какого датасета учились.
    dataset_id:      Optional[str] = None
    dataset_version: Optional[int] = None
    enqueued_at:   str             = field(default_factory=_now_iso)
    started_at:    Optional[str]   = None
    ended_at:      Optional[str]   = None
    elapsed_sec:   Optional[float] = None
    n_skus:        Optional[int]   = None
    n_features:    Optional[int]   = None
    n_rows:        Optional[int]   = None
    wmape:         Optional[float] = None
    mase:          Optional[float] = None
    mase_seasonal: Optional[float] = None
    smape:         Optional[float] = None
    # HZ-1 #464: «точность для заказа» — WMAPE суммы за окно заказа 7/14 дн
    # (bench-parity math). NULL = pre-#464 run / no measurable demand.
    wmape_order_7:  Optional[float] = None
    wmape_order_14: Optional[float] = None
    # DS-2 #467: seasonal-naive WMAPE the promotion gate (#227) scores on
    # the same walk-forward windows — «точнее наивного на N%» in the UI.
    # NULL = pre-DS-2 run / gate skipped or errored.
    baseline_wmape: Optional[float] = None
    # MA-2 #520 (аудит F3): распределение per-SKU рядом с объёмным
    # headline — медиана (типичный товар) и p90 (хвост боли);
    # MA-1 #519: eval_coverage — доля клеток окна, попавших в скоринг.
    wmape_median:  Optional[float] = None
    wmape_p90:     Optional[float] = None
    eval_coverage: Optional[float] = None
    model_path:    Optional[str]   = None
    mlflow_run_id: Optional[str]   = None
    error:         Optional[str]   = None
    # R11: MLflow telemetry-logging failure on an otherwise-successful
    # run (NULL = logged fine). Distinct from `error` (= run FAILED).
    mlflow_logging_error: Optional[str] = None
    # QW2-4 #227: promotion-gate verdict. FALSE = blocked (champion kept
    # serving, model never saved); NULL = pre-gate runs / gate disabled.
    gate_passed: Optional[bool] = None


class PostgresTrainingRunsRegistry:
    _UPDATABLE_COLUMNS = {
        "status", "job_id", "started_at", "ended_at", "elapsed_sec",
        "n_skus", "n_features", "n_rows",
        "wmape", "mase", "mase_seasonal", "smape",
        "wmape_order_7", "wmape_order_14", "baseline_wmape",
        "wmape_median", "wmape_p90", "eval_coverage",
        "model_path", "mlflow_run_id", "error",
        "mlflow_logging_error", "gate_passed",
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
        # Optional read replica — reads of training history can tolerate
        # the seconds of streaming-replication lag, which lets primary
        # spend its connection budget on writes (training enqueues,
        # status updates from workers).
        # Strip surrounding quotes/whitespace defensively — operators
        # occasionally paste DSNs with extra quotes in vault commands,
        # which would break psycopg2 with cryptic errors.
        raw_replica = (os.environ.get("DATABASE_URL_REPLICA") or "").strip()
        for _q in ("'", '"'):
            if len(raw_replica) >= 2 and raw_replica.startswith(_q) and raw_replica.endswith(_q):
                raw_replica = raw_replica[1:-1]
                logger.warning("DATABASE_URL_REPLICA had surrounding %s — stripped.", _q)
        self._read_url = raw_replica or database_url
        # Schema is owned by migrations/ and applied by the dedicated
        # `migrate` compose service before api/worker start. Registries
        # are pure data accessors now.

    def _conn(self):
        """Primary connection — for INSERT/UPDATE."""
        return self._psycopg2.connect(self._url)

    def _conn_read(self):
        """
        Replica connection — for SELECT. Falls back to primary when the
        replica isn't reachable so list/get endpoints stay available
        through brief replication blips.
        """
        try:
            return self._psycopg2.connect(self._read_url, connect_timeout=3)
        except Exception:
            if self._read_url != self._url:
                logger.warning("training_runs: replica unreachable, falling back to primary")
                return self._psycopg2.connect(self._url)
            raise

    def create(self, record: TrainingRunRecord) -> None:
        sql = """
        INSERT INTO sku_training_runs
            (run_id, client_id, plan, data_path, status,
             job_id, upload_id, dataset_id, dataset_version, enqueued_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    record.run_id, record.client_id, record.plan,
                    record.data_path, record.status,
                    record.job_id, record.upload_id,
                    record.dataset_id, record.dataset_version,
                    record.enqueued_at,
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
        # Read path → replica when configured. Primary fallback handled
        # by _conn_read.
        with self._conn_read() as conn:
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

    def list_running(self) -> list[TrainingRunRecord]:
        """All runs currently marked 'running' — reconcile (#265) input.
        Reads the PRIMARY: healing decisions must not act on replica lag."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_training_runs WHERE status = 'running'"
                )
                return [self._row_to_record(dict(r)) for r in cur.fetchall()]

    def list_recent(self, limit: int = 50) -> list[TrainingRunRecord]:
        """ADM-2 (#255): cross-client feed for the admin console. Replica
        read — the oversight page tolerates seconds of replication lag."""
        with self._conn_read() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_training_runs "
                    "ORDER BY COALESCE(ended_at, started_at) DESC NULLS LAST "
                    "LIMIT %s",
                    (limit,),
                )
                return [self._row_to_record(dict(r)) for r in cur.fetchall()]

    def delete_for_client(self, client_id: str) -> int:
        """AUD-4 (#356): erase this client's training-run rows — data_path and
        model_path embed the client's own filenames/objects, so 152-ФЗ erasure
        must remove them. PRIMARY write; returns rows removed (idempotent: a
        second call returns 0). Tenant-scoped by client_id, never a bare
        DELETE."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sku_training_runs WHERE client_id = %s",
                (client_id,),
            )
            return cur.rowcount

    def list_for_client(self, client_id: str, limit: int = 50,
                        dataset_id: "str | None" = None) -> list[TrainingRunRecord]:
        # Read path → replica when configured. review #616 F2: фильтр по
        # датасету — в SQL, а не пост-фильтром окна (чемпион датасета,
        # вытесненный из top-N ранами других датасетов, «терялся» и
        # регрессия промоутилась как первая модель).
        if dataset_id is not None:
            with self._conn_read() as conn:
                with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM sku_training_runs WHERE client_id = %s "
                        "AND dataset_id = %s ORDER BY enqueued_at DESC LIMIT %s",
                        (client_id, dataset_id, limit))
                    return [self._row_to_record(dict(r)) for r in cur.fetchall()]
        with self._conn_read() as conn:
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
            # #574: dataset_id/version существовали в БД, но терялись при
            # маппинге — фронт не мог строить прайор ETA по датасету
            dataset_id=row.get("dataset_id"),
            dataset_version=row.get("dataset_version"),
            enqueued_at=_iso(row.get("enqueued_at")) or "",
            started_at=_iso(row.get("started_at")),
            ended_at=_iso(row.get("ended_at")),
            elapsed_sec=row.get("elapsed_sec"),
            n_skus=row.get("n_skus"),
            n_features=row.get("n_features"),
            n_rows=row.get("n_rows"),
            wmape=row.get("wmape"),
            mase=row.get("mase"),
            mase_seasonal=row.get("mase_seasonal"),
            wmape_order_7=row.get("wmape_order_7"),
            wmape_order_14=row.get("wmape_order_14"),
            baseline_wmape=row.get("baseline_wmape"),
            wmape_median=row.get("wmape_median"),
            wmape_p90=row.get("wmape_p90"),
            eval_coverage=row.get("eval_coverage"),
            gate_passed=row.get("gate_passed"),
            smape=row.get("smape"),
            model_path=row.get("model_path"),
            mlflow_run_id=row.get("mlflow_run_id"),
            error=row.get("error"),
            mlflow_logging_error=row.get("mlflow_logging_error"),
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
