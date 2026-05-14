-- Enforce the email_canonical lowercase invariant at the DB level.
--
-- Audit M2 (2026-05-13): canonical_email() in application code already
-- lowercases input, but the invariant wasn't enforced by the schema.
-- A future code path that writes raw SQL bypassing the helper could
-- silently insert mixed-case rows, creating duplicates that the
-- UNIQUE INDEX on email_canonical (migration 005) failed to catch —
-- 'User@x.com' and 'user@x.com' are distinct TEXT values to PG even
-- though they should map to the same account.
--
-- Why CHECK constraint, not CITEXT (as the audit literally suggested):
--   • CITEXT requires `CREATE EXTENSION citext` — on Beget Cloud
--     Postgres that needs superuser, which our migration role doesn't
--     have. Adding the dependency makes the migration brittle in any
--     environment without the extension pre-installed.
--   • CITEXT also changes the column type, which takes an ACCESS
--     EXCLUSIVE lock on the table for the duration of the rewrite.
--     CHECK + NOT VALID lets us add the invariant without that lock,
--     then VALIDATE in a separate transaction.
--   • The actual protection both approaches provide is identical:
--     refuse non-lowercase canonical values. CHECK does it as an
--     explicit, schema-visible rule; CITEXT does it via comparison
--     semantics. Both combine with the existing UNIQUE INDEX to give
--     us "no duplicates by case".
--
-- The constraint allows NULL (legacy rows that never had email set).

DO $migration$
DECLARE bad_count INT;
BEGIN
    -- Safety: refuse to apply if any row already violates the
    -- invariant. canonical_email() has always lowercased so this
    -- should be 0 in practice, but if a future hand-INSERT slipped
    -- something in, we want a loud failure before the constraint
    -- silently fails some other write.
    SELECT COUNT(*) INTO bad_count
      FROM sku_clients
     WHERE email_canonical IS NOT NULL
       AND email_canonical <> LOWER(email_canonical);

    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'Cannot apply email_canonical lowercase CHECK: % rows '
            'have non-lowercase values. Backfill via '
            '`UPDATE sku_clients SET email_canonical = LOWER(email_canonical) '
            'WHERE email_canonical <> LOWER(email_canonical)` and re-run.',
            bad_count;
    END IF;

    -- Idempotency: pg_constraint lookup, not the (non-existent)
    -- `ADD CONSTRAINT IF NOT EXISTS` syntax.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'sku_clients_email_canonical_lowercase'
    ) THEN
        -- NOT VALID lets us add without a full-table scan under
        -- ACCESS EXCLUSIVE; subsequent VALIDATE picks up only a
        -- SHARE UPDATE EXCLUSIVE lock, which doesn't block reads or
        -- normal DML. For sku_clients (small table) the distinction
        -- is academic, but the pattern transfers to larger tables.
        ALTER TABLE sku_clients
            ADD CONSTRAINT sku_clients_email_canonical_lowercase
            CHECK (
                email_canonical IS NULL
                OR email_canonical = LOWER(email_canonical)
            )
            NOT VALID;

        ALTER TABLE sku_clients
            VALIDATE CONSTRAINT sku_clients_email_canonical_lowercase;
    END IF;
END
$migration$;
