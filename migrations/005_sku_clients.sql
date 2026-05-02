-- Per-client metadata: plan, quotas, model status, auth credentials,
-- email + OAuth identities. Originally created in src/clients/registry.py
-- and grown column-by-column via in-process ALTER ... IF NOT EXISTS.
-- Captured here as a single declarative migration; this same DDL was
-- already applied to prod long ago, so the runner records it as
-- "applied" without doing any work.

CREATE TABLE IF NOT EXISTS sku_clients (
    client_id          TEXT PRIMARY KEY,
    config             JSONB NOT NULL,
    storage_path       TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_trained_at    TIMESTAMPTZ,
    last_mlflow_run_id TEXT,
    last_wmape         FLOAT,
    last_mase          FLOAT,
    status             TEXT NOT NULL DEFAULT 'registered',
    model_version      INT  NOT NULL DEFAULT 0,
    horizon            INT  NOT NULL DEFAULT 14,
    notes              TEXT
);

-- Plan + quota columns (added in Phase 6).
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS plan                       TEXT NOT NULL DEFAULT 'free';
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS training_runs_this_month   INT  NOT NULL DEFAULT 0;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS training_runs_window_start TIMESTAMPTZ;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS trained_sku_count          INT;

-- Auth + email (Phase 7).
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS api_key_hash               TEXT;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS email                      TEXT;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS email_canonical            TEXT;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS email_verified_at          TIMESTAMPTZ;

-- Anti-multi-account: UNIQUE on canonical email; NULL rows allowed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sku_clients_email_canonical
    ON sku_clients (email_canonical) WHERE email_canonical IS NOT NULL;

-- Lookup index for the email-login flow.
CREATE INDEX IF NOT EXISTS idx_sku_clients_email_lookup
    ON sku_clients (LOWER(email)) WHERE email IS NOT NULL;

-- OAuth identity columns + UNIQUE on (provider, subject) (Phase 8).
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS oauth_provider TEXT;
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS oauth_subject  TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sku_clients_oauth
    ON sku_clients (oauth_provider, oauth_subject)
    WHERE oauth_provider IS NOT NULL AND oauth_subject IS NOT NULL;
