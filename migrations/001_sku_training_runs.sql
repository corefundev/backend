-- Persistent log of training runs. One row per /clients/{id}/train
-- invocation — survives RQ's 24h result_ttl.

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
