-- Inventory of every CSV/XLSX a client has uploaded — written by the
-- upload pipeline as the file moves through scan → process → ready.
-- Original DDL lived inside PostgresUploadRegistry.__init__.

CREATE TABLE IF NOT EXISTS sku_uploads (
    upload_id       TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    filename        TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    sha256          TEXT NOT NULL,
    status          TEXT NOT NULL,
    scan_result     TEXT,
    error_message   TEXT,
    processed_key   TEXT,
    row_count       BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backfill column added after v1.0 — populated by the process worker
-- once the parquet is parsed and the SKU catalogue counted.
ALTER TABLE sku_uploads ADD COLUMN IF NOT EXISTS sku_count BIGINT;

CREATE INDEX IF NOT EXISTS idx_sku_uploads_client
    ON sku_uploads (client_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sku_uploads_status
    ON sku_uploads (status);
