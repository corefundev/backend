-- 018 (NC-1 #241): in-account notification center.
-- Product events (training finished/failed, gate-blocked, model staleness,
-- later quota/PII notices) get a persistent per-client inbox row with an
-- unread state. dedup_key makes emits idempotent (e.g. one model_stale_45
-- per client, training rows keyed by run_id survive RQ retries).
CREATE TABLE IF NOT EXISTS sku_notifications (
    id          BIGSERIAL PRIMARY KEY,
    client_id   TEXT NOT NULL,
    type        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    dedup_key   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sku_notifications_dedup
    ON sku_notifications (client_id, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sku_notifications_client_created
    ON sku_notifications (client_id, created_at DESC);
