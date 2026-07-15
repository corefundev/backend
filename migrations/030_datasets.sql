-- DS-1 #466 (epic #463): client datasets — named collections of processed
-- uploads with deterministic merge and versioned snapshots.
-- Owner decisions 2026-07-16: dataset = its OWN model and forecasts
-- (wired in the follow-up slice); retrain is button-triggered; merge rule:
-- the newer file wins on (sku, date) overlap.

CREATE TABLE IF NOT EXISTS sku_datasets (
    dataset_id      TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | deleted
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One name per client among the live datasets (soft-deleted keep history).
CREATE UNIQUE INDEX IF NOT EXISTS sku_datasets_client_name_live
    ON sku_datasets (client_id, name) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS sku_datasets_client
    ON sku_datasets (client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sku_dataset_files (
    dataset_id TEXT NOT NULL,
    upload_id  TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at TIMESTAMPTZ,            -- soft-detach; rebuild drops the rows
    PRIMARY KEY (dataset_id, upload_id)
);

CREATE TABLE IF NOT EXISTS sku_dataset_versions (
    dataset_id   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ready',   -- ready | failed
    fingerprint  TEXT,                 -- content sha12 of the merged snapshot
    snapshot_key TEXT,                 -- PROCESSED-zone key of merged parquet
    row_count    BIGINT,
    sku_count    BIGINT,
    date_min     DATE,
    date_max     DATE,
    merge_report JSONB,                -- {added, replaced, source_upload_id, kind}
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, version)
);

-- Training runs / forecasts learn their dataset lineage (nullable: legacy
-- rows and the follow-up serving slice).
ALTER TABLE sku_training_runs ADD COLUMN IF NOT EXISTS dataset_id TEXT;
ALTER TABLE sku_training_runs ADD COLUMN IF NOT EXISTS dataset_version INTEGER;
ALTER TABLE sku_forecasts     ADD COLUMN IF NOT EXISTS dataset_id TEXT;
