-- 019 (ADM-10 #278): operator suspension. NULL = active; a timestamp =
-- suspended since then (who/why lives in audit_log EVT_ADMIN_ACTION).
-- Suspension denies token issuance AND all client-scoped API access
-- (enforced in require_client_access); data stays intact.
ALTER TABLE sku_clients ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
