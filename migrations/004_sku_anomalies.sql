-- Historical anomalies flagged by SalesAnomalyDetector after each
-- training run. Capped to the last 90 days of dataset history by
-- the worker — older flags aren't actionable for the buyer today.

CREATE TABLE IF NOT EXISTS sku_anomalies (
    client_id      TEXT             NOT NULL,
    sku            TEXT             NOT NULL,
    anomaly_date   DATE             NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    run_id         TEXT,
    detected_at    TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, sku, anomaly_date)
);

CREATE INDEX IF NOT EXISTS idx_sku_anomalies_client_date
    ON sku_anomalies (client_id, anomaly_date DESC);
