"""
src/storage/anomalies.py

Persistent log of historical anomalies — sales days that the per-SKU
IQR detector and the global Isolation Forest both flagged as outliers.

Why persist:
  • The detector runs during training to compute sample weights, then
    its output is thrown away. Without storage the user can't see WHICH
    days were considered anomalous.
  • The forecast page benefits from showing recent anomalies — they're
    the days a buyer should sanity-check before trusting a forecast
    that was learned through them.

Lifecycle: replaced wholesale per-client per-run, same shape as
sku_forecasts. We only persist the LAST 90 days of anomalies — older
ones are not actionable; the model already de-weighted them during
fit, the user shouldn't be alerted to them now.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class PostgresAnomaliesRegistry:
    DDL = """
    CREATE TABLE IF NOT EXISTS sku_anomalies (
        client_id      TEXT        NOT NULL,
        sku            TEXT        NOT NULL,
        anomaly_date   DATE        NOT NULL,
        value          DOUBLE PRECISION NOT NULL,
        run_id         TEXT,
        detected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (client_id, sku, anomaly_date)
    );
    CREATE INDEX IF NOT EXISTS idx_sku_anomalies_client_date
        ON sku_anomalies (client_id, anomaly_date DESC);
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
        logger.info("sku_anomalies table ready")

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def replace_for_client(
        self,
        client_id: str,
        run_id:    str,
        rows:      list[tuple[str, str, float]],   # (sku, anomaly_date, value)
    ) -> int:
        ts = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sku_anomalies WHERE client_id = %s",
                    (client_id,),
                )
                if rows:
                    self._extras.execute_values(
                        cur,
                        "INSERT INTO sku_anomalies "
                        "(client_id, sku, anomaly_date, value, run_id, detected_at) "
                        "VALUES %s",
                        [
                            (client_id, sku, adate, float(v), run_id, ts)
                            for (sku, adate, v) in rows
                        ],
                    )
                inserted = cur.rowcount
            conn.commit()
        logger.info(
            "sku_anomalies: replaced for client=%s run_id=%s rows=%d",
            client_id, run_id, inserted,
        )
        return inserted

    def list_for_client(
        self,
        client_id: str,
        sku:   Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        sql = (
            "SELECT sku, anomaly_date, value, run_id, detected_at "
            "FROM sku_anomalies WHERE client_id = %s"
        )
        params: list = [client_id]
        if sku:
            sql += " AND sku = %s"
            params.append(sku)
        sql += " ORDER BY anomaly_date DESC, sku LIMIT %s"
        params.append(limit)
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [
            {
                "sku":          r["sku"],
                "anomaly_date": r["anomaly_date"].isoformat(),
                "value":        float(r["value"]),
                "run_id":       r["run_id"],
                "detected_at":  r["detected_at"].isoformat(),
            }
            for r in rows
        ]


_singleton: Optional[PostgresAnomaliesRegistry] = None


def get_anomalies_registry() -> PostgresAnomaliesRegistry:
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set — sku_anomalies requires Postgres"
        )
    _singleton = PostgresAnomaliesRegistry(db_url)
    return _singleton
