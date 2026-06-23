-- Audit-log DB-level append-only enforcement.  R13-3 / #144.
--
-- Migration 008 declared the table "append-only by convention" — that was a
-- COMMENT, nothing the database enforced. A compromised app role (or a logic
-- bug) could UPDATE/DELETE rows and erase its tracks. 010's HMAC chain catches
-- content edits and MIDDLE deletes of SIGNED rows, but not deletion of unsigned
-- rows, and not tail-truncation.
--
-- Enforcement is a BEFORE UPDATE/DELETE trigger, NOT a REVOKE: the app role
-- `sku` OWNS audit_log, and a table owner keeps its privileges implicitly —
-- `REVOKE UPDATE,DELETE FROM sku` is a no-op for the owner. A trigger fires for
-- EVERY role, including the owner.
--
-- The one legitimate mutation is retention: prune_audit_log() deletes rows
-- older than the window. It signals that legitimate delete with a
-- transaction-local GUC the trigger checks, so the trigger lets it through
-- while blocking every other UPDATE/DELETE.

-- 1. Guard: block UPDATE/DELETE unless the prune flag is set for this txn.
CREATE OR REPLACE FUNCTION audit_log_block_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF COALESCE(current_setting('audit_log.allow_prune', TRUE), '') = 'on' THEN
        -- legitimate retention prune (see prune_audit_log below)
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    RAISE EXCEPTION
        'audit_log is append-only — % is not permitted. Rows are removed only '
        'by the retention job (prune_audit_log).', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

-- 2. Trigger fires for ALL roles (incl. owner `sku`) before any row mutation.
DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log;
CREATE TRIGGER trg_audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE PROCEDURE audit_log_block_mutation();

-- 3. Defense-in-depth: a no-op for the owner today, but denies UPDATE/DELETE to
--    any future non-owner role that is granted access to the table.
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;

-- 4. Re-define prune_audit_log (identical to migration 012) but set the
--    transaction-local allow flag before the DELETE so the trigger permits it.
--    set_config(..., is_local := TRUE) scopes the flag to this transaction; it
--    resets at COMMIT, so only prune's own DELETE is ever authorised.
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

    -- Authorise this transaction's DELETE past the append-only trigger (R13-3).
    PERFORM set_config('audit_log.allow_prune', 'on', TRUE);

    DELETE FROM audit_log
     WHERE id IN (
            SELECT id FROM audit_log
             WHERE ts < NOW() - (older_than_days || ' days')::interval
             ORDER BY id ASC
             LIMIT batch_size
           );
    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    -- Emit a sentinel entry when we actually delete something — a paper trail
    -- of "the retention job ran and removed N rows". Unsigned (raw INSERT
    -- bypasses the application HMAC path); verify_chain treats the
    -- 'audit_log_retention' type as a legitimate unsigned row.
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

COMMENT ON FUNCTION prune_audit_log(INTEGER, INTEGER) IS
    'Audit-log retention — call daily via cron. Returns rows deleted per batch. '
    'Sets audit_log.allow_prune to pass the append-only trigger (migration 015). '
    'Default 2-year window matches typical GDPR-aligned enterprise policy.';
