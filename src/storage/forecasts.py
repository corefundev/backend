"""
src/storage/forecasts.py

Postgres-backed storage of generated forecasts.

After every successful training run, the worker writes a row per
(client_id, sku, forecast_date). Re-running training overwrites the
prior values via ON CONFLICT DO UPDATE — there's no history of past
forecasts, only the current set produced by the latest model.

Why Postgres and not parquet+S3 for now: at the current plan ceilings
(Free 30×7=210, Start 1500×30=45 000) row counts are small and
queries by (client_id, sku) are fast with the composite index. If a
Business customer with millions of rows shows up, migrate just that
tier to parquet — the table schema is a thin layer.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ForecastPoint:
    sku:           str
    forecast_date: str   # ISO YYYY-MM-DD
    value:         float


class PostgresForecastsRegistry:
    DDL = """
    CREATE TABLE IF NOT EXISTS sku_forecasts (
        client_id      TEXT        NOT NULL,
        sku            TEXT        NOT NULL,
        forecast_date  DATE        NOT NULL,
        value          DOUBLE PRECISION NOT NULL,
        run_id         TEXT,
        generated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (client_id, sku, forecast_date)
    );
    CREATE INDEX IF NOT EXISTS idx_sku_forecasts_client_date
        ON sku_forecasts (client_id, forecast_date);
    """

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
        logger.info("sku_forecasts table ready")

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def replace_for_client(
        self,
        client_id: str,
        run_id:    str,
        rows:      list[tuple[str, str, float]],   # (sku, forecast_date, value)
    ) -> int:
        """
        Atomically replace this client's forecasts with the given rows.
        Returns count of rows inserted.

        Why DELETE-then-INSERT and not pure upsert: when a new training
        run produces fewer SKUs (e.g. user removed some) or a shorter
        horizon, leftover rows from the prior run would otherwise stay
        and confuse the UI. A clean wipe per client per run is simpler
        than tracking which rows belong to which run.
        """
        if not rows:
            return 0
        ts = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sku_forecasts WHERE client_id = %s",
                    (client_id,),
                )
                self._extras.execute_values(
                    cur,
                    "INSERT INTO sku_forecasts "
                    "(client_id, sku, forecast_date, value, run_id, generated_at) "
                    "VALUES %s",
                    [
                        (client_id, sku, fdate, float(v), run_id, ts)
                        for (sku, fdate, v) in rows
                    ],
                )
                inserted = cur.rowcount
            conn.commit()
        logger.info(
            "sku_forecasts: replaced for client=%s run_id=%s rows=%d",
            client_id, run_id, inserted,
        )
        return inserted

    def list_for_client(self, client_id: str, sku: Optional[str] = None) -> list[dict]:
        """
        Return rows for a client, optionally filtered by SKU. Sorted by
        (sku, forecast_date) so the caller can group easily.
        """
        sql = "SELECT sku, forecast_date, value, run_id, generated_at FROM sku_forecasts WHERE client_id = %s"
        params: list = [client_id]
        if sku:
            sql += " AND sku = %s"
            params.append(sku)
        sql += " ORDER BY sku, forecast_date"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "sku":           r["sku"],
                "forecast_date": r["forecast_date"].isoformat(),
                "value":         float(r["value"]),
                "run_id":        r["run_id"],
                "generated_at":  r["generated_at"].isoformat(),
            })
        return out


_singleton: Optional[PostgresForecastsRegistry] = None


def get_forecasts_registry() -> PostgresForecastsRegistry:
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set — sku_forecasts requires Postgres"
        )
    _singleton = PostgresForecastsRegistry(db_url)
    return _singleton
