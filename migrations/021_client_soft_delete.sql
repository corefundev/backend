-- 021_client_soft_delete.sql
--
-- B4 #156 (152-ФЗ PII compliance) — self-service account closure.
--
-- `deleted_at` marks a soft-deleted (closed) account: access is revoked
-- immediately (require_client_access, alongside suspended_at), the row is
-- retained through a grace window for support-side restore, then a daily
-- cron (scripts/pii_purge.sh) hard-anonymises the PII of accounts deleted
-- longer than PII_RETENTION_DAYS. The client_id row itself survives purge
-- (audit_log is append-only / legally retained and references it) — only
-- the PII columns are cleared and status flips to 'purged'.
--
-- NULL = active account. Distinct from suspended_at (operator action,
-- reversible, not a deletion).

ALTER TABLE sku_clients
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;

-- Partial index: the purge cron scans only closed-not-yet-purged rows.
CREATE INDEX IF NOT EXISTS idx_sku_clients_deleted_at
    ON sku_clients (deleted_at)
    WHERE deleted_at IS NOT NULL;
