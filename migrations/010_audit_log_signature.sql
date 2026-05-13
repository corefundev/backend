-- Audit log tamper-evidence: HMAC chain over each row.
--
-- Adds two columns to audit_log:
--   • prev_signature  — the `signature` of the previous signed row.
--   • signature       — HMAC-SHA256(key, prev_signature || canonical_json(row)).
--
-- The HMAC key lives in Lockbox (env: AUDIT_LOG_HMAC_KEY, 32-byte hex).
-- An attacker who compromises Postgres credentials but NOT the Lockbox
-- key can't rewrite rows undetectably — re-running the verifier
-- (src.audit.log.verify_chain) over the rewritten table would surface
-- a broken chain at the tampered row.
--
-- Backward-compat:
--   • Existing rows keep signature=NULL ("legacy"). The verifier
--     skips them and starts fresh chain at the first NOT NULL.
--   • record_event() falls back to unsigned insert if the key isn't
--     configured — audit must NEVER fail-closed on a logging issue.

ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS prev_signature CHAR(64),
    ADD COLUMN IF NOT EXISTS signature      CHAR(64);

-- Partial index on signature so the "latest signed row" lookup at insert
-- time and the chain walk at verify time are fast. Excludes legacy
-- NULL rows so the index stays small.
CREATE INDEX IF NOT EXISTS idx_audit_log_signature
    ON audit_log (id)
 WHERE signature IS NOT NULL;
