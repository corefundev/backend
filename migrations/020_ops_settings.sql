-- 020 (ADM-12 #280): generic operator-facing key-value markers.
-- First use: admin_api_key_rotated_at — the rotation recipe (OPERATIONS.md)
-- stamps it so the Безопасность page can show key age. A KV table (not an
-- audit row) because manual INSERTs into audit_log would break the HMAC
-- chain; ops markers are mutable state, not events.
CREATE TABLE IF NOT EXISTS ops_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
