-- Auto-generated batch forecasts written by the worker after every
-- training run. One row per (client_id, sku, forecast_date).

CREATE TABLE IF NOT EXISTS sku_forecasts (
    client_id      TEXT             NOT NULL,
    sku            TEXT             NOT NULL,
    forecast_date  DATE             NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    run_id         TEXT,
    generated_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (client_id, sku, forecast_date)
);

CREATE INDEX IF NOT EXISTS idx_sku_forecasts_client_date
    ON sku_forecasts (client_id, forecast_date);
