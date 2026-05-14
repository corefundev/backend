-- audit_log retention policy — purge rows older than the retention
-- window. Audit R2-9 (2026-05-15).
--
-- Why this exists:
--   • Schema 008 ships audit_log as "append-only by convention" — comment
--     promises a retention policy "if one lands later". This is later.
--   • Without retention, the table grows unbounded. At realistic ops
--     volume (~100s of auth events / day, more during campaigns) the
--     table reaches millions of rows in a few years, slowing every
--     INSERT (via the partial-index update) and bloating backups.
--   • GDPR-aligned: 2-year ceiling on tying auth events to actor_email.
--     If your legal team mandates shorter, override
--     `AUDIT_LOG_RETENTION_DAYS` env on the API container.
--
-- How it runs:
--   • This migration defines the SQL function `prune_audit_log()` that
--     does one bounded DELETE. It does NOT schedule itself — that's
--     the cron container's job (configurable; see
--     scripts/audit_log_retention.sh and docker-compose backup service
--     where the cron lives).
--   • Function is idempotent. Calling it twice in a row simply finds
--     fewer rows to delete the second time.
--   • DELETE chunks at 10,000 rows per call to keep the lock window
--     short. The cron runs daily; it will iterate over many calls if
--     a backlog ever accumulates.

CREATE OR REPLACE FUNCTION prune_audit_log(
    older_than_days INTEGER DEFAULT 730,   -- 2 years
    batch_size      INTEGER DEFAULT 10000
) RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    IF older_than_days < 30 THEN
        RAISE EXCEPTION
            'prune_audit_log: refusing to set retention < 30 days '
            '(got %); audit retention is a compliance signal, '
            'not a tuning knob.', older_than_days;
    END IF;

    DELETE FROM audit_log
     WHERE id IN (
            SELECT id FROM audit_log
             WHERE ts < NOW() - (older_than_days || ' days')::interval
             ORDER BY id ASC
             LIMIT batch_size
           );
    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    -- Emit a sentinel entry into audit_log when we actually delete
    -- something — gives ops a paper trail of "the retention job ran
    -- on $TIMESTAMP and removed N rows". The sentinel itself is signed
    -- by the application HMAC chain if AUDIT_LOG_HMAC_KEY is set (the
    -- signature columns stay NULL otherwise — chain skip handles it).
    IF deleted_count > 0 THEN
        INSERT INTO audit_log (event_type, event_subtype, success, metadata)
        VALUES (
            'audit_log_retention',
            'pruned',
            TRUE,
            jsonb_build_object(
                'deleted_rows',     deleted_count,
                'older_than_days',  older_than_days,
                'batch_size',       batch_size
            )
        );
    END IF;

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Comment on the function so `\df+ prune_audit_log` shows intent.
COMMENT ON FUNCTION prune_audit_log(INTEGER, INTEGER) IS
    'Audit-log retention — call daily via cron. Returns rows deleted '
    'per batch. Default 2-year window matches typical GDPR-aligned '
    'enterprise policy; tune via the older_than_days argument or by '
    'wrapping in a SQL function that reads env at call-time.';
